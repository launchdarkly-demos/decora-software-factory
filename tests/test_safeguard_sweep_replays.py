"""Safeguard calibration, grounded in job-gh-5873513712-r1..r7 receipts.

r6 log 9-11: empty inserts. r5 log 38: cutoff after six splits.
r1 exhausted test corrections; r7 log 20-23: rewrite of job-created 35-line stub.
"""
import pytest

from factory.agents.apply import apply_proposal
from factory.errors import FactoryError, IncompleteBuildError
from factory.governance.flags import audit_flag
from factory.governance.policy import Policy, load_policy
from factory.models import FileEdit, JobState, PlanTask, Stage, WorktreeInfo
from factory.orchestrator.machine import progress_retry_credit
from factory.plan_normalization import normalize_test_tasks
from factory.tools.anchors import anchor_span
from factory.tools.layer import ToolLayer
from tests.support import ROOT, ScriptedHarness, bare_job, feature, init_repo, low_plan, make_factory, policy
from tests.test_issue22_replays import assert_draft, proposal, write


def edit_job(tmp_path, path='small.txt', content='alpha\nbeta\n'):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    if content is not None:
        tools.write(path, content)
        tools.commit('Base source', [path])
    job = bare_job(tmp_path, plan=low_plan(files=[path]), worktree=WorktreeInfo(
        path=str(repo), branch='main', base_sha=tools.head_sha()))
    return job, tools


@pytest.mark.parametrize('kind,expected', [
    ('insert_after', 'alpha\nbeta\nnew\n'), ('insert_before', 'new\nalpha\nbeta\n'),
])
def test_replay_r6_empty_insert_converts_and_logs(tmp_path, kind, expected):
    job, tools = edit_job(tmp_path)
    result = apply_proposal(job, tools, proposal([FileEdit(path='small.txt', kind=kind, old='', new='new\n')]))
    assert tools.read('small.txt') == expected
    assert any('Empty-anchor conversion' in line for line in result.anchor_fallbacks)
    assert 'Empty-anchor conversion' in tools.run(['git', 'log', '-1', '--format=%B']).output
    assert job.task_retries == {}


@pytest.mark.parametrize('content', [None, ''])
def test_empty_replace_missing_or_empty_is_write(tmp_path, content):
    job, tools = edit_job(tmp_path, content=content)
    result = apply_proposal(job, tools, proposal([FileEdit(path='small.txt', kind='replace', old='', new='new\n')]))
    assert tools.read('small.txt') == 'new\n'
    assert 'replace -> write' in result.anchor_fallbacks[0]


def test_empty_replace_nonempty_rejects_atomically_with_full_text(tmp_path):
    job, tools = edit_job(tmp_path)
    with pytest.raises(FactoryError, match='full current file') as error:
        apply_proposal(job, tools, proposal([write('other.txt', 'must not persist'),
            FileEdit(path='small.txt', kind='replace', old='', new='new\n')]))
    assert 'alpha\nbeta\n' in str(error.value)
    assert tools.read('small.txt') == 'alpha\nbeta\n'
    assert not (tools.worktree / 'other.txt').exists()


@pytest.mark.parametrize('old', ['alpha\nbeta missing', 'alpha\nBETA'])
def test_failed_replace_sharing_source_line_returns_full_file_without_writing(tmp_path, old):
    job, tools = edit_job(tmp_path)
    with pytest.raises(FactoryError, match='full current file') as error:
        apply_proposal(job, tools, proposal([FileEdit(path='small.txt', kind='replace', old=old, new='new\n')]))
    assert 'alpha\nbeta\n' in str(error.value)
    assert tools.read('small.txt') == 'alpha\nbeta\n'


@pytest.mark.parametrize('source,old,span', [
    ('head\n    x = 1   \r\n    y =  2\r\ntail', 'x = 1\ny = 2\n', 'x = 1   \r\n    y =  2\r\n'),
    ('head\n\tx =  1\n\ty = 2\ntail', '    x = 1\n    y = 2\n', 'x =  1\n\ty = 2\n'),
    ('head\n    café = 1  \nend', 'cafe\u0301 =  1\n', 'café = 1  \n'),
])
def test_whitespace_fallback_preserves_source_span(source, old, span):
    start, end, fallback = anchor_span('source.py', source, old)
    assert fallback and source[start:end] == span


def test_whitespace_ambiguity_rejected_and_exact_still_wins():
    source = 'x  = 1\nx\t= 1\n'
    with pytest.raises(FactoryError, match='whitespace anchor matched 2'):
        anchor_span('source.py', source, 'x = 1')
    assert anchor_span('source.py', source, 'x  = 1') == (0, 6, False)


def test_whitespace_insert_preserves_original_anchor_and_logs(tmp_path):
    job, tools = edit_job(tmp_path, content='head\n  alpha   beta  \ntail\n')
    result = apply_proposal(job, tools, proposal([FileEdit(
        path='small.txt', kind='insert_after', old='alpha beta\n', new='inserted\n')]))
    assert tools.read('small.txt') == 'head\n  alpha   beta  \ninserted\ntail\n'
    assert result.anchor_fallbacks


def test_replay_r5_cutoff_after_six_splits_reaches_draft_without_retry(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    rules = policy().model_copy(update={'split_attempts': 1, 'per_task_retries': 0})
    rules.flags.enabled = False
    path = 'tests/test_small.py'
    plan = low_plan(change_class='tests_only', flag_key=None, files=[path], test_targets=[path], tasks=[
        PlanTask(id='tests', description='Test behavior', files=[path], acceptance=['Pass'])])

    def build(job, tools):
        if job.builder_calls <= 7:
            if job.builder_calls > 2:
                assert 'only one small region or one test' in job.govern_feedback
            raise IncompleteBuildError('Your response was cut off at 16000 tokens; split the task.')
        return apply_proposal(job, tools, proposal([write(path, 'def test_ok():\n    assert True\n')]))

    factory = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[build]), policy=rules)
    job = factory.run(feature())
    assert_draft(job)
    assert job.builder_calls == 8 and job.task_split_attempts == {'tests': 7}
    assert job.task_retries == {}


@pytest.mark.parametrize('quick', [True, False], ids=['task', 'verify'])
def test_replay_r1_progress_retries_survive_reload_and_finish(tmp_path, quick):
    repo = init_repo(tmp_path / 'repo')
    rules = policy().model_copy(update={'per_task_retries': 1, 'verify_retries': 1})
    rules.flags.enabled = False
    path, target = 'app/progress.py', 'tests/test_progress.py'
    tools = ToolLayer(repo, rules)
    tests = 'from app.progress import VALUE\n' + ''.join(
        f'\ndef test_{i}():\n    assert VALUE >= {i}\n' for i in range(1, 5))
    tools.write(target, tests)
    tools.commit('Acceptance for r1 progress', [target])
    plan = low_plan(change_class='backend_internal', flag_key=None, files=[path], test_targets=[target], tasks=[
        PlanTask(id='production', description='Implement progress', files=[path], acceptance=['All pass'],
                 quick_test_targets=[target] if quick else [])])

    def build(job, tools):
        return apply_proposal(job, tools, proposal([write(path, f'VALUE = {job.builder_calls - 1}\n')]))

    factory = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[build]), policy=rules,
                           keep_worktree=True)
    job = bare_job(tmp_path, stage=Stage.INTAKE)
    for _ in range(50):
        factory.advance(job)
        job = factory.status(job.id)  # Durable signatures/streaks must survive every call.
        if job.stage in {Stage.DONE, Stage.FAILED, Stage.AWAIT_HUMAN}:
            break
    assert_draft(job)
    assert job.builder_calls == 5
    assert job.task_retries == ({'production': 1} if quick else {})
    assert job.verify_retries_used == (0 if quick else 1)
    assert len(job.quick_verify_runs.get('production', [])) == (5 if quick else 0)


def test_progress_credit_cap_no_progress_and_independent_budgets(tmp_path):
    job = bare_job(tmp_path)
    for count, expected in [(6, False), (5, True), (4, True), (3, True), (2, False), (1, True)]:
        ids = '\n'.join(f'FAILED tests/test_r1.py::test_{i}' for i in range(count))
        assert progress_retry_credit(job, 'owner', ids, 'task') is expected
        job = JobState.model_validate_json(job.model_dump_json())
    assert not progress_retry_credit(job, 'owner', 'FAILED tests/test_r1.py::test_0', 'task')
    assert not progress_retry_credit(job, 'owner', 'FAILED tests/test_r1.py::test_new', 'task')
    assert not progress_retry_credit(job, 'owner', '1 failed', 'verify')
    assert not progress_retry_credit(job, 'other', '1 failed', 'verify')
    assert not progress_retry_credit(job, 'owner', 'import failed', 'task')


def test_progress_explicit_count_decrease_works_without_ids(tmp_path):
    job = bare_job(tmp_path)
    assert not progress_retry_credit(job, 'owner', '=== 4 failed, 2 errors ===', 'task')
    assert progress_retry_credit(job, 'owner', '=== 3 failed, 2 errors ===', 'task')
    assert not progress_retry_credit(job, 'owner', '=== 4 failed, 2 errors ===', 'task')


def test_replay_r7_large_job_created_stub_can_be_rewritten(tmp_path):
    job, tools = edit_job(tmp_path, path='new.txt', content=None)
    stub = ''.join(f'job stub {i}\n' for i in range(100))
    apply_proposal(job, tools, proposal([write('new.txt', stub)]))
    apply_proposal(job, tools, proposal([write('new.txt', 'implementation\n')]))
    assert tools.read('new.txt') == 'implementation\n' and not job.task_retries


@pytest.mark.parametrize('declarations,evaluation', [
    ('FLAG_DEFAULT = False', ''), ('FLAG_DEFAULT: bool = False', ''),
    ('', "get_flag('decora-sweep', ctx, default=False)"),
    ('', "get_flag('decora-sweep', ctx, False)"),
    ('X_DEFAULT = False', "get_flag('decora-sweep', ctx, default=X_DEFAULT)"),
    ('X_DEFAULT: bool = False', "get_flag('decora-sweep', ctx, X_DEFAULT)"),
    ('', "ld.variation('decora-sweep', ctx, False)"),
    ("KEY: str = 'decora-sweep'\nX_DEFAULT: bool = False", 'ld.variation(KEY, ctx, X_DEFAULT)'),
])
def test_equivalent_flag_defaults(declarations, evaluation):
    code = f"KEY = 'decora-sweep'\n{declarations}\n{evaluation}"
    assert audit_flag(code, 'decora-sweep', must_default_off=True) == []


@pytest.mark.parametrize('default', ['True', 'X_DEFAULT', 'unknown', 'None'])
def test_false_declaration_cannot_hide_true_or_unknown_evaluation(default):
    code = ("KEY = 'decora-sweep'\nFLAG_DEFAULT: bool = False\nX_DEFAULT: bool = True\n"
            f"ld.variation(KEY, ctx, {default})")
    assert audit_flag(code, 'decora-sweep', must_default_off=True)


def test_more_than_six_tests_and_safe_fixes_are_checkpointed(tmp_path):
    path = 'tests/test_many.py'
    job, tools = edit_job(tmp_path, path=path, content=None)
    job.plan.tasks = [PlanTask(id='tests', description='Tests', files=[path], max_test_functions=8)]
    normalize_test_tasks(job.plan)
    assert job.plan.tasks[0].max_test_functions == 6
    text = 'import os\n' + ''.join(f'\ndef test_{i}():\n assert 1==1\n' for i in range(8))
    result = apply_proposal(job, tools, proposal([write(path, text)]))
    current = tools.read(path)
    assert current.count('def test_') == 8 and 'import os' not in current
    assert '    assert 1 == 1' in current and result.commit == tools.head_sha()
    assert tools.run(['git', 'status', '--porcelain']).output == ''


def test_calibrated_defaults():
    for rules in [Policy(), load_policy(ROOT / 'policy.decora.yaml')]:
        assert (rules.per_task_retries, rules.verify_retries, rules.max_job_attempts) == (4, 4, 40)


@pytest.mark.parametrize('kind', ['replace', 'insert_after'])
def test_replay_r6_empty_package_initializer_reaches_draft(tmp_path, kind):
    # r6 rejected_edits.jsonl:1-3: app/__init__.py, empty replace/insert_after.
    repo = init_repo(tmp_path / 'repo')
    rules = policy()
    rules.flags.enabled = False
    path, target = 'app/home.py', 'tests/test_home.py'
    plan = low_plan(change_class='backend_internal', flag_key=None, files=[path, target], test_targets=[target],
                    tasks=[PlanTask(id='introduce_home_and_update_room_model', description='Home model',
                                    files=[path, target], acceptance=['Home model imports'])])

    def build(job, tools):
        return apply_proposal(job, tools, proposal([
            write(path, 'class Home:\n    pass\n'),
            FileEdit(path='app/__init__.py', kind=kind, old='', new='from .home import Home\n'),
            write(target, 'from app import Home\n\ndef test_home():\n    assert Home()\n')]))

    job = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[build]), policy=rules).run(feature())
    assert_draft(job)
    assert job.task_retries == {} and job.builder_calls == 1
    assert job.auto_added_files == ['app/__init__.py']
    assert any('Empty-anchor conversion' in event.message for event in job.events)


def test_empty_replace_after_delete_keeps_conflict_guard(tmp_path):
    job, tools = edit_job(tmp_path)
    with pytest.raises(FactoryError, match='Conflicting edits'):
        apply_proposal(job, tools, proposal([FileEdit(path='small.txt', kind='delete'),
            FileEdit(path='small.txt', kind='replace', old='', new='resurrected')]))
    assert tools.read('small.txt') == 'alpha\nbeta\n'
