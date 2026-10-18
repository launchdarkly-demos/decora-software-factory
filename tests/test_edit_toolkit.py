"""Builder edits validate together before any mutation, including move destinations."""

import pytest

from factory.agents.apply import apply_proposal
from factory.errors import FactoryError, GovernanceError
from factory.models import BuildProposal, FileEdit, JobState, WorktreeInfo
from factory.tools.layer import ToolLayer
from tests.support import bare_job, init_repo, low_plan, policy

KINDS = ['insert_after', 'insert_before', 'append', 'prepend', 'delete', 'move']


@pytest.fixture
def editing(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write('source.txt', 'header\nanchor\nfooter\n')
    tools.commit('Source', ['source.txt'])
    job = bare_job(tmp_path, plan=low_plan(files=['source.txt']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    return job, tools


def apply(job, tools, edits):
    return apply_proposal(job, tools, BuildProposal(summary='Edit files', commit_message='Edit files', edits=edits))


@pytest.mark.parametrize(('kind', 'expected'), [
    ('insert_after', 'header\nanchor\nadded\nfooter\n'),
    ('insert_before', 'header\nadded\nanchor\nfooter\n'),
    ('append', 'header\nanchor\nfooter\nadded\n'),
    ('prepend', 'added\nheader\nanchor\nfooter\n'),
    ('delete', None),
    ('move', None),
])
def test_edit_kinds_checkpoint_and_persist_deletions(editing, kind, expected):
    job, tools = editing
    result = apply(job, tools, [FileEdit(path='source.txt', kind=kind, old='anchor\n', new='added\n',
                                        destination='nested/renamed.txt' if kind == 'move' else '')])
    path = tools.worktree / 'source.txt'
    assert (path.read_text() if path.exists() else None) == expected
    assert tools.run(['git', 'status', '--porcelain']).output == ''
    if kind in {'delete', 'move'}:
        assert 'source.txt' in JobState.model_validate_json(job.model_dump_json()).deleted_files
        assert 'source.txt' in result.changed_files
        from factory.harness.openai_agents import _file_excerpts

        assert next(item for item in _file_excerpts(job, tools)
                    if item['path'] == 'source.txt')['source_kind'] == 'deleted'
    if kind == 'move':
        assert tools.read('nested/renamed.txt') == 'header\nanchor\nfooter\n'
        assert job.auto_added_files == ['nested/renamed.txt']
        assert 'nested/renamed.txt' in job.plan.tasks[0].files


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('unsafe', ['.env', '../escape.txt', '/tmp/escape.txt', 'app/pim/private.txt', 'escape.txt'])
def test_new_kinds_reject_unsafe_paths_atomically(editing, tmp_path, kind, unsafe):
    job, tools = editing
    (tools.worktree / 'escape.txt').symlink_to(tmp_path / 'outside.txt')
    with pytest.raises(GovernanceError):
        apply(job, tools, [FileEdit(path='new.txt', kind='write', content='safe'),
                           FileEdit(path=unsafe, kind=kind, old='anchor', new='added', destination='new-path.txt')])
    assert not (tools.worktree / 'new.txt').exists()
    assert tools.read('source.txt') == 'header\nanchor\nfooter\n'
    assert not job.auto_added_files and not job.deleted_files


@pytest.mark.parametrize('unsafe', ['.env', '../escape.txt', 'app/pim/private.txt', '.git/config', 'alias/file.txt'])
def test_move_checks_destination_before_deleting_source(editing, tmp_path, unsafe):
    job, tools = editing
    (tools.worktree / 'alias').symlink_to(tmp_path)
    with pytest.raises(GovernanceError):
        apply(job, tools, [FileEdit(path='source.txt', kind='move', destination=unsafe)])
    assert tools.read('source.txt') == 'header\nanchor\nfooter\n'
    assert not job.deleted_files and not job.auto_added_files


@pytest.mark.parametrize('kind', KINDS)
def test_missing_sources_are_correctable_without_partial_write(editing, kind):
    job, tools = editing
    with pytest.raises(FactoryError, match='missing file'):
        apply(job, tools, [FileEdit(path='new.txt', kind='write', content='safe'),
                           FileEdit(path='missing.txt', kind=kind, old='anchor', new='added', destination='dest.txt')])
    assert not (tools.worktree / 'new.txt').exists()
    assert not job.auto_added_files


@pytest.mark.parametrize('kind', ['replace', 'insert_after', 'insert_before'])
@pytest.mark.parametrize('old', ['', 'missing', 'e'])
def test_invalid_anchors_are_atomic_and_empty_replace_is_actionable(editing, kind, old):
    job, tools = editing
    message = 'replace needs a non-empty unique anchor; to add new code use insert_after/append/prepend'
    with pytest.raises(FactoryError, match=message if kind == 'replace' and not old else 'anchor matched'):
        apply(job, tools, [FileEdit(path='source.txt', kind='append', new='first\n'),
                           FileEdit(path='source.txt', kind=kind, old=old, new='bad')])
    assert tools.read('source.txt') == 'header\nanchor\nfooter\n'


def test_move_refuses_overwrite_and_rolls_back_io_failure(editing, monkeypatch):
    job, tools = editing
    with pytest.raises(FactoryError, match='already exists'):
        apply(job, tools, [FileEdit(path='source.txt', kind='move', destination='README.md')])
    original_write = tools.write

    def fail(rel, text):
        if rel == 'fail.txt':
            raise OSError('disk full')
        original_write(rel, text)

    monkeypatch.setattr(tools, 'write', fail)
    with pytest.raises(OSError):
        apply(job, tools, [FileEdit(path='source.txt', kind='move', destination='moved.txt'),
                           FileEdit(path='fail.txt', kind='write', content='safe')])
    assert tools.read('source.txt') == 'header\nanchor\nfooter\n'
    assert not (tools.worktree / 'moved.txt').exists()
    assert not job.auto_added_files and not job.deleted_files


def test_sequential_moves_insertions_and_recreation(editing):
    job, tools = editing
    (tools.worktree / 'source.txt').chmod(0o755)
    apply(job, tools, [FileEdit(path='source.txt', kind='move', destination='moved.txt'),
                       FileEdit(path='moved.txt', kind='insert_after', old='anchor\n', new='extra\n'),
                       FileEdit(path='source.txt', kind='write', content='recreated\n')])
    assert tools.read('moved.txt') == 'header\nanchor\nextra\nfooter\n'
    assert tools.read('source.txt') == 'recreated\n'
    assert not job.deleted_files
    assert (tools.worktree / 'moved.txt').stat().st_mode & 0o111


def test_move_cannot_modify_a_different_tasks_destination(editing):
    from factory.models import PlanTask

    job, tools = editing
    job.plan.files.append('future.txt')
    job.plan.tasks.append(PlanTask(id='future', description='Later', files=['future.txt'], acceptance=['later']))
    with pytest.raises(FactoryError, match='belong to other tasks'):
        apply(job, tools, [FileEdit(path='source.txt', kind='move', destination='future.txt')])
    assert not (tools.worktree / 'future.txt').exists()
