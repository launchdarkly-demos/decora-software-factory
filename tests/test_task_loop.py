"""End-to-end regressions for bounded Builder tasks and correction routing."""
from __future__ import annotations

from pathlib import Path

import pytest

from factory.agents.apply import apply_proposal
from factory.errors import FactoryError, GovernanceError, IncompleteBuildError
from factory.models import BuildProposal, FileEdit, PlanTask, Stage, WorktreeInfo
from factory.publish.pr import render_pr
from factory.tools.layer import ToolLayer
from factory.verification.gate import VerificationGate
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


def queued_plan(count=5):
    paths = [f'app/part_{index}.py' for index in range(count)]
    return low_plan(change_class='tests_only', flag_key=None, files=paths, tasks=[
        PlanTask(id=f'T{index}', description=f'Implement part {index}', files=[path],
                 depends_on=[f'T{index - 1}'] if index else [], acceptance=[f'Part {index} works'])
        for index, path in enumerate(paths)
    ])


def write_task(job, tools, *, complete=True):
    task = next(task for task in job.plan.tasks if task.id == job.current_task_id)
    return apply_proposal(job, tools, BuildProposal(
        summary=f'Built {task.id}', commit_message=f'Build {task.id}', task_complete=complete,
        edits=[FileEdit(path=task.files[0], kind='write',
                        content=f'VALUE = {job.builder_calls}\n' + '# padding\n' * 500)]))


def test_five_tasks_checkpoint_and_resume_before_one_full_verification(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    seen = []

    def build(job, tools):
        seen.append(job.current_task_id)
        assert len(job.completed_task_ids) == int(job.current_task_id[1:])
        assert not job.verify_runs
        return write_task(job, tools)

    harness = ScriptedHarness([queued_plan()], builds=[build])
    factory = make_factory(tmp_path, repo, harness, verifier=ScriptedVerifier([passed_verify()]), keep_worktree=True)
    job = bare_job(tmp_path, stage=Stage.INTAKE)
    while len(job.completed_task_ids) < 2:
        factory.advance(job)
    second = make_factory(tmp_path, repo, harness, home=factory.home,
                          verifier=ScriptedVerifier([passed_verify()]), keep_worktree=True)
    job = second.status(job.id)
    while job.stage != Stage.DONE:
        second.advance(job)
    assert seen == ['T0', 'T1', 'T2', 'T3', 'T4']
    assert len(job.verify_runs) == 1
    assert job.task_retries == {}
    assert len({shas[0] for shas in job.checkpoint_shas.values()}) == 5
    assert job.builder_calls == 5
    assert Path(job.pr.body_path).exists()


def test_cutoff_and_partial_output_keep_same_task_without_retry(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    seen = []

    def build(job, tools):
        seen.append(job.current_task_id)
        if job.builder_calls == 1:
            raise IncompleteBuildError('Output incomplete; split this task')
        if job.builder_calls == 2:
            assert 'split' in job.govern_feedback
            return write_task(job, tools, complete=False)
        assert 'remaining work' in job.govern_feedback
        return write_task(job, tools)

    job = make_factory(tmp_path, repo, ScriptedHarness([queued_plan(1)], builds=[build]),
                       verifier=ScriptedVerifier([passed_verify()])).run(feature())
    assert job.stage == Stage.DONE
    assert seen == ['T0'] * 3
    assert len(job.checkpoint_shas['T0']) == 2
    assert job.task_retries == {}
    assert job.task_split_attempts == {'T0': 2}


def test_split_cap_is_separate_from_task_budget_and_overall_call_cap(tmp_path):
    repo = init_repo(tmp_path / 'repo')

    def cutoff(job, tools):
        raise IncompleteBuildError('incomplete; split')

    rules = policy().model_copy(update={'split_attempts': 1, 'per_task_retries': 1})
    job = make_factory(tmp_path, repo, ScriptedHarness([queued_plan(1)], builds=[cutoff]),
                       policy=rules).run(feature())
    assert job.stage == Stage.FAILED
    assert job.builder_calls == 3
    assert job.task_retries == {"T0": 1}
    assert "exhausted 1 retries" in job.error
    rules.max_job_attempts = 1
    job = make_factory(tmp_path, repo, ScriptedHarness([queued_plan(1)], builds=[cutoff]),
                       policy=rules).run(feature())
    assert job.stage == Stage.FAILED
    assert job.builder_calls == 1
    assert 'Job reached max_job_attempts (1).' in job.error


def test_missing_task_file_feedback_names_coverage_and_waits_for_later_tasks(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    plan = queued_plan(2)
    plan.files.append('tests/migrations/test_conversion.py')
    plan.tasks[0].files.append(plan.files[-1])
    plan.tasks[0].acceptance = ['Preserve legacy data on repeat conversion']

    def build(job, tools):
        if job.builder_calls == 2:
            assert 'Create `tests/migrations/test_conversion.py` covering:' in job.govern_feedback
            assert 'Preserve legacy data' in job.govern_feedback
            return apply_proposal(job, tools, BuildProposal(summary='conversion tests', commit_message='Add tests',
                edits=[FileEdit(path=plan.files[-1], kind='write',
                                content='def test_conversion():\n    assert True\n')]))
        return write_task(job, tools)

    job = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[build]),
                       verifier=ScriptedVerifier([passed_verify()])).run(feature())
    assert job.stage == Stage.DONE
    assert job.task_retries == {'T0': 1}
    assert job.builder_calls == 3
    assert len(job.verify_runs) == 1


def test_full_failure_routes_to_owning_task_without_replaying_other_tasks(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    seen = []

    def build(job, tools):
        seen.append(job.current_task_id)
        return write_task(job, tools)

    failure = '$ pytest app/part_0.py app/part_1.py\napp/part_0.py:12: AssertionError'
    job = make_factory(tmp_path, repo, ScriptedHarness([queued_plan(2)], builds=[build]),
                       verifier=ScriptedVerifier([failed_verify(failure), passed_verify()])).run(feature())
    assert job.stage == Stage.DONE
    assert seen == ['T0', 'T1', 'T0']
    assert job.task_retries == {}
    assert job.verification_task_routes == {'T0': 1}
    assert len(job.verify_runs) == 2


def test_exhausted_quick_failure_is_advisory(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    plan = queued_plan(1)
    plan.tasks[0].quick_test_targets = ['tests/nested/test_value.py']
    plan.test_targets = ['tests/nested/test_required.py']

    def build(job, tools):
        assert not job.verify_runs
        value = job.builder_calls
        return apply_proposal(job, tools, BuildProposal(summary='Value and test', commit_message='Value and test',
            edits=[FileEdit(path='app/part_0.py', kind='write', content=f'VALUE = {value}\n'),
                   FileEdit(path='tests/nested/test_value.py', kind='write',
                            content='from app.part_0 import VALUE\n\ndef test_value():\n    assert VALUE == 2\n'),
                   FileEdit(path='tests/nested/test_required.py', kind='write',
                            content='from app.part_0 import VALUE\n\ndef test_required():\n    assert VALUE == 1\n')]))

    job = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[build]),
                       policy=policy().model_copy(update={"per_task_retries": 0}),
                       verifier=VerificationGate(repo, tmp_path / 'home')).run(feature())
    assert job.stage == Stage.DONE
    assert job.task_retries == {}
    assert job.builder_calls == 1
    assert [run.passed for run in job.quick_verify_runs['T0']] == [False]
    assert len(job.verify_runs) == 1
    assert job.auto_added_files == ['tests/nested/test_value.py', 'tests/nested/test_required.py']
    assert '## Files added to the plan automatically\n\n- `tests/nested/test_value.py`' in render_pr(job, '')


@pytest.mark.parametrize('unsafe', ['.env', 'app/pim/private.py', '../escape.py', '/tmp/escape.py',
                                   'app/../safe.py', 'escape.py', 'alias.py'])
def test_unsafe_edit_is_fatal_before_other_edits_or_plan_additions(tmp_path, unsafe):
    repo = init_repo(tmp_path / 'repo')
    (repo / 'escape.py').symlink_to(tmp_path / 'outside.py')
    (repo / 'alias.py').symlink_to(repo / '.env')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['server.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    proposal = BuildProposal(summary='unsafe', commit_message='unsafe', edits=[
        FileEdit(path='safe.py', kind='write', content='VALUE = 1\n'),
        FileEdit(path=unsafe, kind='write', content='VALUE = 2\n')])
    with pytest.raises(GovernanceError):
        apply_proposal(job, tools, proposal)
    assert job.auto_added_files == []
    assert not (repo / 'safe.py').exists()


def test_secret_is_fatal_before_writing_any_file(tmp_path, monkeypatch):
    repo = init_repo(tmp_path / 'repo')
    monkeypatch.setenv('OPENAI_API_KEY', 'private-test-provider-key')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    with pytest.raises(GovernanceError, match='Safety violation: credential-like content'):
        apply_proposal(job, tools, BuildProposal(summary='x', commit_message='x', edits=[
            FileEdit(path='safe.py', kind='write', content='VALUE = 1\n'),
            FileEdit(path='leak.py', kind='write', content='KEY = "private-test-provider-key"')]))
    assert not (repo / 'safe.py').exists()
    assert not job.auto_added_files


def test_missing_replace_file_is_correctable(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    with pytest.raises(FactoryError, match='missing file') as caught:
        apply_proposal(job, tools, BuildProposal(summary='x', commit_message='x', edits=[
            FileEdit(path='missing.py', kind='replace', old='x', new='y')]))
    assert not isinstance(caught.value, GovernanceError)


def test_nested_tests_import_repo_package_without_project_pytest_config(tmp_path):
    repo = init_repo(tmp_path / 'repository with spaces')
    tools = ToolLayer(repo, policy())
    tools.write('app/value.py', 'VALUE = 42\n')
    tools.write('tests/migrations/test_value.py',
                'from app.value import VALUE\n\ndef test_value():\n    assert VALUE == 42\n')
    job = bare_job(tmp_path, plan=low_plan(files=['app/value.py'], test_targets=['tests/migrations/test_value.py']))
    result = VerificationGate(repo, tmp_path / 'home').run_quick(job, tools, job.plan.test_targets)
    assert result.passed, result.actionable


def test_unplanned_new_package_initializers_are_auto_added_without_correction(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['app/migrations/convert.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    apply_proposal(job, tools, BuildProposal(summary='Package', commit_message='Add package', edits=[
        FileEdit(path='app/migrations/convert.py', kind='write', content='VALUE = 1\n'),
        FileEdit(path='app/migrations/__init__.py', kind='write', content='')]))
    assert job.auto_added_files == ['app/migrations/__init__.py']
    assert 'app/migrations/__init__.py' in job.plan.tasks[0].files
    assert job.task_retries == {}


def test_failed_proposal_command_is_checkpointed_before_task_correction(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    plan = queued_plan(1)

    def build(job, tools):
        if job.builder_calls == 2:
            assert 'failed' in job.govern_feedback
            assert job.checkpoint_shas['T0'] == [tools.head_sha()]
            assert tools.run(['git', 'status', '--porcelain']).output.strip() == ''
        return apply_proposal(job, tools, BuildProposal(summary='Implement task', commit_message='Implement task',
            edits=[FileEdit(path='app/part_0.py', kind='write', content=f'VALUE = {job.builder_calls}\n'),
                   FileEdit(path='test_part.py', kind='write',
                            content='from app.part_0 import VALUE\n\ndef test_part():\n    assert VALUE == 2\n')],
            commands=[['pytest', '-q', 'test_part.py']]))

    job = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[build]),
                       verifier=ScriptedVerifier([passed_verify()])).run(feature())
    assert job.stage == Stage.DONE
    assert job.task_retries == {'T0': 1}
    assert len(job.checkpoint_shas['T0']) == 2
    assert len(job.verify_runs) == 1


@pytest.mark.parametrize('prefix', ['./', 'absolute'])
def test_failure_paths_route_to_owner_in_worktrees_with_spaces(tmp_path, prefix):
    from factory.task_queue import reopen_task

    work = tmp_path / 'worktree with spaces'
    job = bare_job(tmp_path, plan=queued_plan(3), completed_task_ids=['T0', 'T1', 'T2'],
                   worktree=WorktreeInfo(path=str(work), branch='main', base_sha='base'))
    path = str(work) + '/' if prefix == 'absolute' else prefix
    owner = reopen_task(job, f'File "{path}app/part_0.py", line 4: AssertionError')
    assert owner.id == 'T0'
    assert job.completed_task_ids == ['T1', 'T2']


def test_task_can_edit_any_safe_planned_file(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=queued_plan(2),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    apply_proposal(job, tools, BuildProposal(summary='Shared edit', commit_message='Shared edit', edits=[
        FileEdit(path='app/part_0.py', kind='write', content='VALUE = 1\n'),
        FileEdit(path='app/part_1.py', kind='write', content='VALUE = 2\n')]))
    assert 'app/part_1.py' in job.plan.tasks[0].files
    assert not job.auto_added_files


def test_delete_and_move_complete_task_and_survive_job_reload(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    plan = low_plan(change_class='refactor', flag_key=None,
                    files=['server.py', 'README.md', 'test_structure.py'], test_targets=['test_structure.py'],
                    tasks=[PlanTask(id='files', description='Remove server and move readme',
                                    files=['server.py', 'README.md', 'test_structure.py'],
                                    acceptance=['Files reorganized'])])

    def build(job, tools):
        return apply_proposal(job, tools, BuildProposal(summary='Reorganize files', commit_message='Reorganize files',
            edits=[FileEdit(path='server.py', kind='delete'),
                   FileEdit(path='README.md', kind='move', destination='docs/README.md'),
                   FileEdit(path='test_structure.py', kind='write', content=(
                       'from pathlib import Path\n\ndef test_files():\n'
                       '    assert not Path("server.py").exists()\n'
                       '    assert Path("docs/README.md").is_file()\n'))]))

    rules = policy()
    rules.flags.enabled = False
    factory = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[build]),
                           policy=rules, verifier=VerificationGate(repo, tmp_path / "home"), keep_worktree=True)
    job = factory.run(feature())
    assert job.stage == Stage.DONE, job.error or job.question
    assert job.pr is not None
    assert not job.task_retries
    restored = factory.status(job.id)
    assert restored.deleted_files == ['server.py', 'README.md']
    assert restored.auto_added_files == ['docs/README.md']
    assert (Path(job.worktree.path) / 'docs/README.md').read_text() == 'mini decora\n'
