"""Approved builder-spec §5 regressions, including issue #20's atomic staging failure."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from factory.agents.apply import apply_proposal
from factory.errors import FactoryError, InfrastructureError
from factory.harness.openai_agents import _builder_failure, _file_excerpts, _run_agent
from factory.models import BuildProposal, FileEdit, PlanTask, Stage, WorktreeInfo
from factory.publish.pr import render_pr
from factory.task_queue import reopen_task, verification_feedback
from factory.tools.layer import ToolLayer
from tests.support import (
    ScriptedHarness,
    ScriptedVerifier,
    bare_job,
    failed_verify,
    feature,
    init_repo,
    low_plan,
    make_factory,
    passed_verify,
    policy,
)
from tests.test_task_loop import queued_plan, write_task


@pytest.fixture
def checkpoint(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['server.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    return job, tools


def proposal(edits, **kwargs):
    return BuildProposal(summary='Implement task', commit_message='Implement task', edits=edits, **kwargs)


@pytest.mark.parametrize(('path', 'source'), [
    ('broken.py', '# before\n' * 14 + 'def broken(:\n    pass\n' + '# after\n' * 14),
    ('broken.json', '{\n  "value":\n}\n'),
    ('broken.yaml', 'value: [\n'),
    ('broken.yml', 'value: [\n'),
    ('broken.toml', '[section\nvalue = 1\n'),
])
def test_parse_failure_has_location_caret_and_staged_source_without_any_write(checkpoint, path, source):
    job, tools = checkpoint
    original = tools.read('server.py')
    head = tools.head_sha()
    with pytest.raises(FactoryError) as caught:
        apply_proposal(job, tools, proposal([
            FileEdit(path='server.py', kind='write', content='REPLACED = True\n'),
            FileEdit(path=path, kind='write', content=source)]))
    feedback = str(caught.value)
    assert path + ':' in feedback and '^' in feedback
    assert ' | ' in feedback
    if path.endswith('.py'):
        assert 'broken.py:15:12:' in feedback
        assert 'def broken(:' in feedback and '# before' in feedback and '# after' in feedback
    assert tools.read('server.py') == original
    assert not (tools.worktree / path).exists()
    assert tools.head_sha() == head
    assert not job.auto_added_files
    assert tools.run(['git', 'status', '--porcelain']).output == ''


def test_syntax_is_checked_after_all_ordered_edits(checkpoint):
    job, tools = checkpoint
    result = apply_proposal(job, tools, proposal([
        FileEdit(path='created.py', kind='create', content='def value():\n'),
        FileEdit(path='created.py', kind='append', new='    return 42\n')]))
    assert result.correction is None
    assert 'return 42' in tools.read('created.py')
    assert job.auto_added_file_tasks == {'created.py': 'T1'}


def test_formatter_failure_is_atomic_strips_ansi_and_includes_staged_region(checkpoint, monkeypatch):
    job, tools = checkpoint
    original = tools.read('server.py')

    def fail(*args):
        raise FactoryError('\x1b[31mserver.py:12:4: formatter failed\x1b[0m')

    monkeypatch.setattr('factory.agents.staging.format_python', fail)
    with pytest.raises(FactoryError) as caught:
        apply_proposal(job, tools, proposal([
            FileEdit(path='server.py', kind='write', content='# staged\n' * 25),
            FileEdit(path='new.txt', kind='write', content='never written')]))
    feedback = str(caught.value)
    assert 'server.py:12:4:' in feedback and '^' in feedback and '# staged' in feedback
    assert '\x1b' not in feedback
    assert tools.read('server.py') == original
    assert not (tools.worktree / 'new.txt').exists()
    assert not job.auto_added_files


def test_empty_complete_proposal_uses_current_checkpoint_and_incomplete_is_e4(checkpoint):
    job, tools = checkpoint
    head = tools.head_sha()
    result = apply_proposal(job, tools, proposal([], task_complete=True))
    assert result.commit == head and result.task_complete
    with pytest.raises(FactoryError) as caught:
        apply_proposal(job, tools, proposal([], task_complete=False))
    assert str(caught.value) == ('No edits returned. Make concrete edits for task T1, or set '
                                 '`task_complete=true` if the task is already done at this checkpoint.')


def test_anchor_feedback_distinguishes_missing_from_ambiguous(checkpoint):
    job, tools = checkpoint
    tools.write('source.txt', 'alpha line\nbeta line\nalpha line\ngamma line\n')
    with pytest.raises(FactoryError) as caught:
        apply_proposal(job, tools, proposal([FileEdit(path='source.txt', kind='replace', old='alpha', new='x')]))
    assert str(caught.value) == ('source.txt: anchor matched 2 times; line numbers: 1, 3. '
                                 "Extend the anchor until it's unique.")
    anchor = 'alpha lyne' + 'x' * 220
    with pytest.raises(FactoryError) as caught:
        apply_proposal(job, tools, proposal([FileEdit(path='source.txt', kind='replace', old=anchor, new='x')]))
    feedback = str(caught.value)
    assert repr(anchor[:200]) in feedback and anchor not in feedback
    assert len(feedback.split('Closest matching lines:\n')[1].splitlines()[:-1]) == 3
    assert feedback.endswith('Anchors must occur exactly once in the current file.')


@pytest.mark.parametrize('path', ['app/0001_room_home.py', 'app/class/model.py', 'bad-folder/model.py'])
def test_invalid_new_module_or_package_name_is_a_correction(checkpoint, path):
    job, tools = checkpoint
    with pytest.raises(FactoryError, match='suggested valid name:'):
        apply_proposal(job, tools, proposal([FileEdit(path=path, kind='write', content='VALUE = 1\n')]))
    assert not (tools.worktree / path).exists()


@pytest.mark.parametrize('kind', ['create', 'write', 'append', 'replace'])
def test_conflict_after_delete_is_atomic(checkpoint, kind):
    job, tools = checkpoint
    original = tools.read('server.py')
    with pytest.raises(FactoryError, match='Conflicting edits:'):
        apply_proposal(job, tools, proposal([
            FileEdit(path='server.py', kind='delete'),
            FileEdit(path='server.py', kind=kind, content='x', old='x', new='y')]))
    assert tools.read('server.py') == original


def test_policy_context_budget_prioritizes_first_task_file(checkpoint):
    job, tools = checkpoint
    paths = ['first.txt', 'second.txt', 'third.txt']
    job.plan.tasks = [PlanTask(id='context', description='Update files', files=paths)]
    job.current_task_id = 'context'
    tools.policy.whole_file_limit = 80
    tools.policy.excerpt_budget = 100
    for path in paths:
        tools.write(path, path[0] * 60)
    files = _file_excerpts(job, tools)
    assert files[0]['path'] == 'first.txt' and files[0]['text'] == 'f' * 60
    assert sum(len(item['text']) + len(item.get('outline', '')) for item in files) <= 100


def test_e18_retries_three_times_with_backoff_without_task_charge(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / 'repo')
    delays, calls = [], []
    # Scope the spy to the machine; patching the shared time module also records
    # subprocess.wait's polling sleeps, making this assertion timing-dependent.
    monkeypatch.setattr('factory.orchestrator.machine.time', SimpleNamespace(sleep=delays.append))

    def build(job, tools):
        calls.append(job.current_task_id)
        if len(calls) < 4:
            raise InfrastructureError('temporary transport failure')
        return write_task(job, tools)

    job = make_factory(tmp_path, repo, ScriptedHarness([queued_plan(1)], builds=[build]),
                       verifier=ScriptedVerifier([passed_verify()])).run(feature())
    assert job.stage == Stage.DONE, job.error
    assert delays == [1, 2, 4] and calls == ['T0'] * 4
    assert not job.task_retries and not job.task_split_attempts
    assert job.builder_calls == 4
    assert sum('E18 ' + job.id in event.message for event in job.events) == 3


@pytest.mark.parametrize('error', [TimeoutError(), ConnectionError(), SimpleNamespace(status_code=503)])
def test_provider_failures_are_infrastructure(error):
    if isinstance(error, SimpleNamespace):
        failure = RuntimeError('provider failure')
        failure.status_code = error.status_code
        error = failure
    assert isinstance(_builder_failure(error), InfrastructureError)


def test_builder_call_timeout_is_enforced_inside_sdk_run(monkeypatch):
    import asyncio

    import agents

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')

    def slow(*args, **kwargs):
        return asyncio.get_event_loop().run_until_complete(asyncio.sleep(10))

    monkeypatch.setattr(agents.Runner, 'run_sync', slow)
    with pytest.raises(TimeoutError):
        _run_agent(None, '{}', timeout=0.01)


def test_quick_failure_corrects_same_task_before_full_verification(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    plan = queued_plan(1)
    plan.tasks[0].quick_test_targets = ['test_task.py']

    class QuickVerifier(ScriptedVerifier):
        def run_quick(self, job, tools, targets):
            assert not job.verify_runs
            return failed_verify('FAILED test_task.py::test_value\n' + 'output\n' * 65) if job.builder_calls == 1 \
                else passed_verify()

    def build(job, tools):
        if job.builder_calls == 2:
            assert job.current_task_id == 'T0' and job.task_retries == {'T0': 1}
            assert 'test_task.py::test_value' in job.govern_feedback
            assert not job.verify_runs
        return write_task(job, tools)

    job = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[build]),
                       verifier=QuickVerifier([passed_verify()])).run(feature())
    assert job.stage == Stage.DONE, job.error
    assert [run.passed for run in job.quick_verify_runs['T0']] == [False, True]
    assert len(job.verify_runs) == 1


def test_routing_uses_last_owner_and_reopens_only_dependents_that_touched_it_later(checkpoint):
    job, tools = checkpoint
    job.plan = queued_plan(4)
    job.plan.tasks[0].files.append('shared.py')
    job.plan.tasks[1].files += ['shared.py', 'another.py']
    job.plan.tasks[2].files.append('another.py')
    job.completed_task_ids = ['T0', 'T1', 'T2', 'T3']
    job.task_file_calls = {'T1': {'shared.py': 2, 'another.py': 2}, 'T2': {'another.py': 3},
                           'T3': {'app/part_3.py': 4}}
    tools.write('shared.py', '# current\n' * 14 + 'VALUE = 42\n')
    output = 'shared.py:15: AssertionError\nFAILED tests/test_shared.py::test_value - mismatch'
    owner = reopen_task(job, output)
    assert owner.id == 'T1'
    assert job.completed_task_ids == ['T0', 'T3']
    feedback = verification_feedback(owner, output, tools)
    assert 'tests/test_shared.py::test_value' in feedback
    assert 'shared.py:15:' in feedback and '15 | VALUE = 42' in feedback
    assert reopen_task(job, 'unknown failure').id == 'T3'


def test_pr_sections_and_automatic_file_task_survive_roundtrip(checkpoint):
    job, tools = checkpoint
    apply_proposal(job, tools, proposal([FileEdit(path='app/new.py', kind='write', content='VALUE = 1\n')]))
    job = type(job).model_validate_json(job.model_dump_json())
    from factory.models import FlagAssurance

    job.feature.issue_url = 'https://github.com/owner/repo/issues/42'
    job.plan.requires_migration = True
    job.plan.migration_plan = 'Backfill; recover by disabling the flag.'
    job.plan.flag_rationale = 'Keep the migration reversible.'
    job.launchdarkly = FlagAssurance(mode='live', required=True, key=job.plan.flag_key,
                                    url='https://example.com/flag', production_on=False,
                                    production_serves_false=True, environments_off=True)
    body = render_pr(job, '')
    assert body.startswith(f'# {job.feature.title}\n\nDraft pull request. Do not merge.')
    for text in [job.feature.issue_url, job.plan.migration_plan, job.plan.flag_rationale,
                 'https://example.com/flag', 'code default: off.', '- `app/new.py` (task T1)']:
        assert text in body
    sections = ['## Summary', '## Rollout', '## Implementation plan', '## Migration and recovery',
                '## Files added to the plan automatically', '## Test evidence', '## Review findings',
                '## Remaining risks', '## Retries', '## Diff summary']
    assert [body.index(section) for section in sections] == sorted(body.index(section) for section in sections)
    assert 'Task T1 corrections: 0' in body and 'Task T1 split continuations: 0' in body


def test_anchor_matching_preserves_real_newline_bytes(checkpoint):
    from factory.context.repo import RepositoryReader

    job, tools = checkpoint
    (tools.worktree / 'windows.txt').write_bytes(b'header\r\nanchor\r\nfooter\r\n')
    reader = RepositoryReader(tools.worktree, tools.policy)
    assert reader.read('windows.txt', 1, 3) == 'header\r\nanchor\r\nfooter\r\n'
    with pytest.raises(FactoryError, match='anchor matched 0 times'):
        apply_proposal(job, tools, proposal([
            FileEdit(path='windows.txt', kind='replace', old='anchor\n', new='wrong\n')]))
    apply_proposal(job, tools, proposal([
        FileEdit(path='windows.txt', kind='replace', old='anchor\r\n', new='correct\r\n')]))
    assert (tools.worktree / 'windows.txt').read_bytes() == b'header\r\ncorrect\r\nfooter\r\n'


@pytest.mark.parametrize('edit', [FileEdit(path='', kind='delete'), FileEdit(path='server.py', kind='move')])
def test_missing_required_path_field_is_a_schema_correction(checkpoint, edit):
    from factory.errors import SafetyError

    job, tools = checkpoint
    with pytest.raises(FactoryError) as caught:
        apply_proposal(job, tools, proposal([edit]))
    assert not isinstance(caught.value, SafetyError)
    assert 'required non-empty field' in str(caught.value)
    assert str(caught.value).endswith('Return one complete BuildProposal.')


def test_approved_policy_budget_defaults():
    from factory.governance.policy import Policy

    rules = Policy()
    assert rules.verify_retries == 3
    assert (rules.builder_max_output_tokens, rules.builder_max_turns) == (16000, 40)
    assert (rules.per_task_retries, rules.split_attempts, rules.max_job_attempts) == (3, 4, 30)
    assert (rules.whole_file_limit, rules.excerpt_budget) == (64000, 160000)
    assert rules.timeout_for('build') == 600


@pytest.mark.parametrize('kind', ['create', 'write', 'replace', 'insert_before', 'insert_after', 'append', 'prepend'])
def test_required_edit_content_is_not_silently_defaulted(checkpoint, kind):
    job, tools = checkpoint
    with pytest.raises(FactoryError, match='required field. Return one complete BuildProposal.'):
        apply_proposal(job, tools, proposal([FileEdit(path='server.py', kind=kind, old='anchor')]))


def test_checkpoint_subject_names_task_part_and_commits_only_proposal_paths(checkpoint):
    job, tools = checkpoint
    tools.write('README.md', 'unrelated dirty work\n')
    tools.run(['git', 'add', '--', 'README.md'])
    result = apply_proposal(job, tools, proposal([
        FileEdit(path='server.py', kind='write', content='VALUE = 1\n')]))
    assert tools.run(['git', 'log', '-1', '--format=%s']).output.strip() == 'checkpoint T1/1'
    assert tools.run(['git', 'show', '--format=', '--name-only', result.commit]).output.strip() == 'server.py'
    assert 'README.md' in tools.run(['git', 'status', '--porcelain']).output
    repeated = apply_proposal(job, tools, proposal([
        FileEdit(path='server.py', kind='write', content='VALUE = 1\n')]))
    assert repeated.commit == result.commit


def test_deleted_context_marker_survives_reload(checkpoint):
    job, tools = checkpoint
    apply_proposal(job, tools, proposal([FileEdit(path='server.py', kind='delete')]))
    job = type(job).model_validate_json(job.model_dump_json())
    context = next(item for item in _file_excerpts(job, tools) if item['path'] == 'server.py')
    assert context['source_kind'] == 'deleted'
    assert 'Intentionally removed' in context['instruction']
    assert context['text'] == ''


def test_read_inventory_glob_is_capped_at_150_safe_paths(tmp_path):
    from factory.context.repo import RepositoryReader

    for index in range(151):
        (tmp_path / f'file_{index}.txt').write_text('safe\n')
    reader = RepositoryReader(tmp_path, policy())
    assert len(reader.list_files('file_*.txt')) == 150
    assert reader.list_files('missing*') == []


@pytest.mark.parametrize('value', ['true', 1, None])
def test_task_complete_requires_actual_boolean(value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match='task_complete'):
        proposal([], task_complete=value)
