"""#26 / r5 regressions: real Git checkpoints, imports, pytest and local publication."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.agents.apply import apply_proposal
from factory.errors import FactoryError, IncompleteBuildError
from factory.governance.policy import fixed_flag_key, load_policy, review_plan, selected_flag_key
from factory.harness.openai_agents import OpenAIAgentsHarness, _builder_failure, _cutoff_feedback
from factory.models import FileEdit, ImplementationPlan, PlanTask, Stage, WorktreeInfo
from factory.plan_normalization import normalize_test_tasks
from factory.task_queue import reopen_task
from factory.tools.layer import ToolLayer
from factory.verification.gate import VerificationGate, import_name
from tests.support import ScriptedHarness, bare_job, feature, init_repo, low_plan, make_factory, policy
from tests.test_issue22_replays import assert_draft, proposal, write

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'app/store.py'
TEST = 'tests/migrations/test_migration_0001_room_home.py'
KEY = 'decora-room-home-migration'
WRONG_KEY = 'decora-room-home-association'
SOURCE = ('"""Project store. Preserve module state and imports."""\n'
          'VALUE = 1\n\nclass DesignProject:\n    pass\n\n'
          + ''.join(f'# Existing storage behavior {i}\n' for i in range(235)))


def test_replay_r5(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    rules = policy()
    rules.flags.enabled = False
    rules.split_attempts = 1
    tools = ToolLayer(repo, rules)
    tools.write(MODULE, SOURCE)
    tools.commit('241-line existing store', [MODULE])
    owner, testing = 'refactor_project_and_room_storage', 'migration_tests'
    plan = low_plan(change_class='backend_internal', files=[MODULE, TEST], test_targets=[TEST], tasks=[
        PlanTask(id=owner, description='Associate rooms with homes', files=[MODULE], acceptance=['Preserve store']),
        PlanTask(id=testing, description='Test migration', files=[TEST], depends_on=[owner],
                 quick_test_targets=[TEST], acceptance=['Migration behavior']),
    ])
    checkpoint = []

    def destructive(job, tools):
        checkpoint.append(tools.head_sha())
        return apply_proposal(job, tools, proposal([
            write('README.md', 'partial proposal must never persist\n'),
            FileEdit(path=MODULE, kind='replace', old=tools.read(MODULE),
                     new='def get_store():\n    return DesignProject\n# truncated\n'),
        ]))

    def broken_import(job, tools):
        assert tools.head_sha() == checkpoint[0]
        assert tools.read(MODULE) == SOURCE
        assert tools.read('README.md') == 'mini decora\n'
        assert '241' in job.govern_feedback and 'Head of removed region' in job.govern_feedback
        return apply_proposal(job, tools, proposal([FileEdit(
            path=MODULE, kind='replace', old='VALUE = 1',
            new='VALUE = __import__("missing_issue26_dependency")')], task_complete=False))

    def repair_import(job, tools):
        assert job.current_task_id == owner and job.task_retries == {owner: 2}
        assert job.verify_retries_used == 0 and 'ModuleNotFoundError' in job.govern_feedback
        assert 'missing_issue26_dependency' in tools.read(MODULE)  # Failed checkpoint is retained.
        return apply_proposal(job, tools, proposal([FileEdit(
            path=MODULE, kind='replace', old='VALUE = __import__("missing_issue26_dependency")', new='VALUE = 1')]))

    def fail_test(job, tools):
        return apply_proposal(job, tools, proposal([write(TEST, 'def test_legacy():\n    assert 1 == 2\n')]))

    def cutoff(job, tools):
        assert job.current_task_id == testing and 'E13' in job.govern_feedback
        assert job.task_retries[testing] == 1
        # The same classifier is used on initial and reopened/correction provider calls.
        raise _builder_failure(IncompleteBuildError(_cutoff_feedback(16000)))

    def append_part(job, tools):
        assert job.task_split_attempts == {testing: 1}
        assert job.task_retries[testing] == 1
        assert 'at most 6 test functions per call using append' in job.govern_feedback
        return apply_proposal(job, tools, proposal([FileEdit(path=TEST, kind='append',
            new='\n\ndef test_store_preserved():\n    from app import store\n    assert store.VALUE == 1\n')],
            task_complete=False))

    def finish(job, tools):
        assert job.task_split_attempts == {testing: 1}  # Applied partial parts never spend E1/E2 allowance.
        return apply_proposal(job, tools, proposal([
            FileEdit(path=TEST, kind='replace', old='assert 1 == 2', new='assert 1 == 1')]))

    harness = ScriptedHarness([plan], builds=[destructive, broken_import, repair_import,
                                             fail_test, cutoff, append_part, finish])
    factory = make_factory(tmp_path, repo, harness, policy=rules, keep_worktree=True)
    job = factory.run(feature())
    assert_draft(job)
    assert job.task_retries == {owner: 2, testing: 1}
    assert job.task_split_attempts == {testing: 1} and job.verify_retries_used == 0
    saved = factory.status(job.id)
    assert saved.task_split_attempts == job.task_split_attempts
    receipts = factory.home / 'runs' / job.id / 'rejected_edits.jsonl'
    assert json.loads(receipts.read_text().splitlines()[0])['edits'][1]['path'] == MODULE


@pytest.mark.parametrize('kind', ['write', 'replace', 'delete'])
def test_guard_counts_removed_lines_and_restores_checkpoint(tmp_path, kind):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write('existing.txt', ''.join(f'line {i}\n' for i in range(20)))
    sha = tools.commit('Original', ['existing.txt'])
    job = bare_job(tmp_path, plan=low_plan(files=['existing.txt']), worktree=WorktreeInfo(
        path=str(repo), branch='main', base_sha=sha))
    old = tools.read('existing.txt')
    fields = {'content': 'replacement\n' * 20} if kind == 'write' else (
        {'old': old, 'new': 'three\nlines\nonly\n'} if kind == 'replace' else {})
    # Demonstrate the requested restore, even if another caller left dirty tracked files.
    tools.write('README.md', 'dirty before rejection\n')
    with pytest.raises(FactoryError, match=r'E9: existing.txt:1:1: destructive edit removes 20/20 lines'):
        apply_proposal(job, tools, proposal([write('new.txt', 'never written'),
            FileEdit(path='existing.txt', kind=kind, **fields)]))
    assert tools.read('existing.txt') == old and tools.read('README.md') == 'mini decora\n'
    assert not (repo / 'new.txt').exists() and tools.head_sha() == sha
    assert tools.run(['git', 'status', '--porcelain']).output == ''


@pytest.mark.parametrize(('size', 'removed', 'rewrite', 'allowed'), [
    (19, 19, False, True), (20, 10, False, True), (20, 11, False, False), (20, 20, True, True),
])
def test_guard_threshold_small_file_and_explicit_rewrite(tmp_path, size, removed, rewrite, allowed):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    lines = [f'line {i}\n' for i in range(size)]
    tools.write('existing.txt', ''.join(lines))
    sha = tools.commit('Original', ['existing.txt'])
    task = PlanTask(id='T', description='Edit', files=['existing.txt'],
                    rewrite_files=['existing.txt'] if rewrite else [])
    job = bare_job(tmp_path, plan=low_plan(files=task.files, tasks=[task]), worktree=WorktreeInfo(
        path=str(repo), branch='main', base_sha=sha))
    edits = proposal([write('existing.txt', ''.join(lines[removed:]))])
    if allowed:
        apply_proposal(job, tools, edits)
        assert tools.read('existing.txt') == ''.join(lines[removed:])
    else:
        with pytest.raises(FactoryError, match='11/20'):
            apply_proposal(job, tools, edits)


def test_guard_compares_final_proposal_and_combined_removals(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    source = ''.join(f'line {i}\n' for i in range(20))
    tools.write('existing.txt', source)
    sha = tools.commit('Original', ['existing.txt'])
    job = bare_job(tmp_path, plan=low_plan(files=['existing.txt']), worktree=WorktreeInfo(
        path=str(repo), branch='main', base_sha=sha))
    apply_proposal(job, tools, proposal([write('existing.txt', 'temporary\n'), write('existing.txt', source)]))
    with pytest.raises(FactoryError, match='12/20'):
        apply_proposal(job, tools, proposal([FileEdit(path='existing.txt', kind='replace',
            old=''.join(f'line {i}\n' for i in range(a, a + 6)), new='') for a in [0, 6]]))


def test_collection_traceback_uses_innermost_production_owner(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write(MODULE, 'VALUE = 1\n')
    tools.write(TEST, 'from app import store\n')
    plan = low_plan(files=[MODULE, TEST], tasks=[
        PlanTask(id='owner', description='Store', files=[MODULE]),
        PlanTask(id='tests', description='Tests', files=[TEST]),
    ])
    job = bare_job(tmp_path, plan=plan, current_task_id='tests', completed_task_ids=['owner', 'tests'],
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    feedback = (f'ERROR collecting {TEST}\n{TEST}:2: in <module>\n'
                f'  from app import store\n{repo / MODULE}:7: in <module>\n'
                "E NameError: name 'DesignProject' is not defined\n"
                f'ERROR {TEST} - NameError')
    assert reopen_task(job, feedback, tools).id == 'owner'
    assert job.completed_task_ids == ['tests']


def test_import_mapping_target_interpreter_environment_and_tests_excluded(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    rules = policy()
    rules.command_environment = {'ISSUE26_VALUE': 'yes'}
    tools = ToolLayer(repo, rules)
    files = {
        'app/store.py': 'import os\nassert os.environ["ISSUE26_VALUE"] == "yes"\n',
        'src/pkg/__init__.py': '', 'src/pkg/model.py': 'from . import VALUE\n',
        'scripts/standalone.py': 'raise RuntimeError("standalone mapped by path")\n',
        'tests/test_bad.py': 'raise RuntimeError("must not import")\n',
    }
    files['src/pkg/__init__.py'] = 'VALUE = 1\n'
    for path, text in files.items():
        tools.write(path, text)
    assert import_name(repo, 'src/pkg/model.py') == ('pkg.model', repo / 'src')
    assert import_name(repo, 'src/pkg/__init__.py') == ('pkg', repo / 'src')
    assert import_name(repo, 'scripts/standalone.py')[0] is None
    # A target interpreter wrapper proves the gate does not use the factory binary.
    target = tmp_path / 'target'
    interpreter = target / '.venv/bin/python'
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    interpreter.chmod(0o755)
    reports = dict(VerificationGate(target, tmp_path / 'home').run_imports(bare_job(tmp_path), tools, list(files)))
    assert 'tests/test_bad.py' not in reports
    assert reports['app/store.py'].passed and reports['src/pkg/model.py'].passed
    assert reports['src/pkg/__init__.py'].passed
    assert not reports['scripts/standalone.py'].passed
    assert 'RuntimeError: standalone mapped by path' in reports['scripts/standalone.py'].output
    assert all(report.argv[0] == str(interpreter) for report in reports.values())
    with pytest.raises(FactoryError, match='python -c'):
        tools.run(['python', '-c', 'print("not agent-accessible")'])


@pytest.mark.parametrize('identity', ['arober39/decor-agent#8', '8', 'https://github.com/arober39/decor-agent/issues/8'])
def test_r5_plan_flag_key_override(tmp_path, identity):
    plan = ImplementationPlan.model_validate_json((ROOT / 'tests/replays/issue_26_r5_plan.json').read_text())
    # The saved final r5 plan is corrected; replay the drift candidate reported in #24.
    plan.flag_key = WRONG_KEY
    rules = load_policy(ROOT / 'policy.decora.yaml')
    rules.flags.issue_flag_keys = {identity: KEY}
    job = bare_job(tmp_path, plan=plan, feature=feature(
        issue_url='https://github.com/arober39/decor-agent/issues/8'))
    decision = review_plan(rules, plan, job)
    assert plan.flag_key == KEY and selected_flag_key(job) == KEY
    assert not decision.retry_plan and decision.resume_stage == 'build'
    assert fixed_flag_key(rules, job) == KEY


def test_fixed_key_payload_log_and_default_fallback(tmp_path, monkeypatch):
    import agents

    rules = policy()
    rules.flags.default_key = 'decor-fixed'
    repo = init_repo(tmp_path / 'repo')
    plan = low_plan(flag_key='decor-wrong', files=['app/flags.py'], tasks=[
        PlanTask(id='flag', description='Flag', files=['app/flags.py'], acceptance=['Default off']),
    ])
    job = bare_job(tmp_path, plan=plan, stage=Stage.GOVERN_PLAN)
    factory = make_factory(tmp_path, repo, policy=rules)
    factory.advance(job)
    assert job.stage == Stage.BUILD and job.plan.flag_key == 'decor-fixed'
    event = next(event for event in job.events if event.message == 'Fixed flag key override')
    assert event.data == {'proposed_key': 'decor-wrong', 'fixed_key': 'decor-fixed'}
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')

    def runner(agent, payload, **kwargs):
        data = json.loads(payload)
        assert data['mandatory_flag_key'] == {'key': 'decor-fixed', 'mandatory_when_flag_required': True}
        return SimpleNamespace(raw_responses=[], final_output=plan)

    monkeypatch.setattr(agents.Runner, 'run_sync', runner)
    OpenAIAgentsHarness(policy=rules).plan(job, SimpleNamespace(root=str(repo), model_dump=lambda: {}))
    # Exempt classes do not acquire a policy flag.
    exempt = low_plan(change_class='tests_only', flag_key=None)
    review_plan(rules, exempt, job)
    assert exempt.flag_key is None


def test_normalize_r5_test_work_and_dependencies():
    plan = ImplementationPlan.model_validate_json((ROOT / 'tests/replays/issue_26_r5_plan.json').read_text())
    original = plan.tasks[-1]
    plan.tasks.append(PlanTask(id='later', description='Later', files=['README.md'], depends_on=[original.id]))
    normalize_test_tasks(plan)
    parts = [task for task in plan.tasks if task.id.startswith(original.id + '_part_')]
    assert len(parts) == len(original.acceptance)
    assert [check for task in parts for check in task.acceptance] == original.acceptance
    assert all(task.files == [TEST] and task.max_test_functions == 6 for task in parts)
    assert parts[0].depends_on == original.depends_on
    assert all(right.depends_on == [left.id] for left, right in zip(parts, parts[1:]))
    assert plan.tasks[-1].depends_on == [parts[-1].id]
    before = plan.model_dump()
    normalize_test_tasks(plan)
    assert before == plan.model_dump()


def test_normalize_oversized_tests_without_dropping_checks():
    checks = [f'behavior {i}' for i in range(13)]
    plan = low_plan(files=['tests/test_one.py', 'tests/test_two.py'], tasks=[
        PlanTask(id='tests', description='Tests', files=['tests/test_one.py', 'tests/test_two.py'], acceptance=checks),
    ])
    normalize_test_tasks(plan)
    assert len(plan.tasks) == 6
    assert all(len(task.acceptance) <= 6 and len(task.files) == 1 for task in plan.tasks)
    assert {check for task in plan.tasks for check in task.acceptance} == set(checks)


@pytest.mark.parametrize('exception', [IncompleteBuildError('cut off at 16000 tokens'),
                                       RuntimeError('max_output_tokens reached')])
def test_cutoff_classifier_preserves_split_on_correction(exception):
    assert isinstance(_builder_failure(exception), IncompleteBuildError)


def test_r5_traceback_does_not_route_to_broad_tests_or_unedited_later_owner(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write(MODULE, 'VALUE = 1\n')
    tools.write(TEST, 'from app import store\n')
    plan = ImplementationPlan.model_validate_json((ROOT / 'tests/replays/issue_26_r5_plan.json').read_text())
    owner = 'refactor_project_and_room_storage'
    job = bare_job(tmp_path, plan=plan, current_task_id='implement_migration_and_compatibility_tests',
                   task_file_calls={owner: {MODULE: 5}}, worktree=WorktreeInfo(
                       path=str(repo), branch='main', base_sha=tools.head_sha()))
    traceback = (f'ERROR collecting {TEST}\n{TEST}:2: in <module>\n'
                 'app/store.py:7: in <module>\nE NameError: DesignProject is not defined\n'
                 f'ERROR {TEST} - NameError')
    assert reopen_task(job, traceback, tools).id == owner
    from factory.task_queue import reopen_import_task

    assert reopen_import_task(job, MODULE).id == owner


def test_import_failure_without_declared_owner_stays_with_current_task(tmp_path):
    from factory.task_queue import reopen_import_task

    job = bare_job(tmp_path, current_task_id='current', plan=low_plan(tasks=[
        PlanTask(id='earlier', description='Earlier', files=['app/earlier.py']),
        PlanTask(id='current', description='Current', files=['app/current.py']),
    ]))
    assert reopen_import_task(job, 'app/unowned.py').id == 'current'
    assert 'app/unowned.py' in job.plan.tasks[-1].files


def test_r5_opening_hunk_below_half_still_fails_checkpoint_import(tmp_path):
    """The receipt deletes the first 238 lines of a module longer than 535 lines."""
    repo = init_repo(tmp_path / 'repo')
    rules = policy()
    rules.flags.enabled = False
    tools = ToolLayer(repo, rules)
    prefix = 'class DesignProject:\n    pass\n' + '# original store state\n' * 236
    suffix = ('def set_brief() -> DesignProject:\n    return DesignProject()\n'
              + '# remaining store behavior\n' * 320)
    tools.write(MODULE, prefix + suffix)
    tools.commit('Original store shape from r5 hunks', [MODULE])
    plan = low_plan(files=[MODULE], tasks=[PlanTask(
        id='refactor_project_and_room_storage', description='Storage', files=[MODULE])])

    def truncate(job, tools):
        return apply_proposal(job, tools, proposal([FileEdit(
            path=MODULE, kind='replace', old=prefix, new='')]))

    factory = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[truncate]),
                           policy=rules, keep_worktree=True)
    job = bare_job(tmp_path, plan=plan, stage=Stage.BUILD)
    factory.advance(job)
    assert job.stage == Stage.BUILD and job.builds
    assert job.task_retries == {'refactor_project_and_room_storage': 1}
    assert job.current_task_id == 'refactor_project_and_room_storage'
    assert 'NameError' in job.govern_feedback and 'DesignProject' in job.govern_feedback
    assert job.verify_retries_used == 0
