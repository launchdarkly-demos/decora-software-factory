import pytest

from factory.agents.apply import apply_proposal
from factory.errors import FactoryError, GovernanceError, PlanScopeError
from factory.models import BuildProposal, FileEdit, WorktreeInfo
from factory.tools.layer import ToolLayer
from tests.support import bare_job, init_repo, low_plan, policy


def setup_build(tmp_path, files):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=files),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    return repo, tools, job


def proposal(*edits):
    return BuildProposal(summary='Update approved files', commit_message='Update approved files', edits=list(edits))


def test_full_write_replaces_existing_file_and_stages_following_edits(tmp_path):
    repo, tools, job = setup_build(tmp_path, ['README.md', 'notes.md'])
    result = apply_proposal(job, tools, proposal(
        FileEdit(path='README.md', kind='write', content='Replacement\n'),
        FileEdit(path='README.md', kind='replace', old='Replacement', new='Updated'),
        FileEdit(path='notes.md', kind='write', content='New file\n')))
    assert (repo / 'README.md').read_text() == 'Updated\n'
    assert (repo / 'notes.md').read_text() == 'New file\n'
    assert set(result.changed_files) == {'README.md', 'notes.md'}
    assert result.commit == tools.head_sha()
    assert tools.run(['git', 'status', '--porcelain']).output == ''


@pytest.mark.parametrize('path,approved', [
    ('README.md', False),
    ('../outside.md', True),
    ('.env', True),
    ('escape.md', True),
])
def test_full_write_preserves_approval_and_path_checks_before_any_edit(tmp_path, path, approved):
    files = ['notes.md'] + ([path] if approved else [])
    repo, tools, job = setup_build(tmp_path, files)
    outside = tmp_path / 'outside.md'
    outside.write_text('Outside\n')
    (repo / '.env').write_text('Protected\n')
    (repo / 'escape.md').symlink_to(outside)
    original = (repo / 'README.md').read_text()
    base = tools.head_sha()
    with pytest.raises(GovernanceError if approved else PlanScopeError):
        apply_proposal(job, tools, proposal(
            FileEdit(path='notes.md', kind='write', content='New file\n'),
            FileEdit(path=path, kind='write', content='Overwrite\n')))
    assert not (repo / 'notes.md').exists()
    assert (repo / 'README.md').read_text() == original
    assert outside.read_text() == 'Outside\n'
    assert (repo / '.env').read_text() == 'Protected\n'
    assert tools.head_sha() == base


@pytest.mark.parametrize('bad_edit', [
    FileEdit(path='README.md', kind='replace', old='missing anchor', new='Updated'),
    FileEdit(path='README.md', kind='write', content='[excerpt; use read_file for more]'),
])
def test_invalid_later_edit_does_not_apply_staged_full_replacement(tmp_path, bad_edit):
    repo, tools, job = setup_build(tmp_path, ['README.md'])
    original = (repo / 'README.md').read_text()
    base = tools.head_sha()
    with pytest.raises(FactoryError):
        apply_proposal(job, tools, proposal(
            FileEdit(path='README.md', kind='write', content='Replacement\n'), bad_edit))
    assert (repo / 'README.md').read_text() == original
    assert tools.head_sha() == base
