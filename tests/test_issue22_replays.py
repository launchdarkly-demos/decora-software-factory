"""#22 approved rules replayed through real worktrees, pytest, and draft records."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.agents.apply import apply_proposal
from factory.errors import FactoryError
from factory.governance.policy import VerificationProfile
from factory.harness.mock import MockHarness
from factory.harness.openai_agents import _file_excerpts
from factory.models import BuildProposal, FileEdit, ImplementationPlan, PlanTask, Stage, WorktreeInfo
from factory.task_queue import reopen_task
from factory.tools.anchors import anchor_span
from factory.tools.layer import ToolLayer, _anchor_error
from tests.support import bare_job, feature, init_repo, low_plan, make_factory, policy

MODULE = 'migrations/migration_0001_room_home.py'
TEST = 'tests/migrations/test_migration_0001_room_home.py'
BAD_MODULE = '''"""Migration → default home.
Manual invocation required.
"""


def migrate(payload, fail=False):
    payload['home_id'] = 'default-home'
    if fail:
        return payload
    return payload
'''
TESTS = '''from migrations.migration_0001_room_home import migrate
from migrations import migration_0001_room_home as migration


def test_partial_failure_and_recovery_documented():
    original = {'room_id': 'stable', 'chat': ['preserved']}
    result = migrate(original, fail=True)
    assert 'home_id' not in result
    assert original == {'room_id': 'stable', 'chat': ['preserved']}


def test_documented_recovery_rollback_notes():
    assert 'rollback' in migration.__doc__


def test_happy_path():
    assert migrate({'room_id': 'stable'})['home_id'] == 'default-home'
'''


def proposal(edits=(), **kwargs):
    return BuildProposal(summary='Implement migration replay', commit_message='Migration replay',
                         edits=list(edits), **kwargs)


def write(path, text):
    return FileEdit(path=path, kind='write', content=text)


def fix(*, fallback=False):
    return proposal([
        FileEdit(path=MODULE, kind='replace',
                 old='Migration \x12 default home.' if fallback else 'Migration → default home.',
                 new='Migration → default home. Manual recovery and rollback: restore the snapshot.'),
        FileEdit(path=MODULE, kind='replace',
                 old='    payload["home_id"] = "default-home"\n    if fail:\n        return payload',
                 new="    if fail:\n        return payload\n    payload = dict(payload)\n"
                     "    payload['home_id'] = 'default-home'"),
    ])


class Issue22Harness(MockHarness):
    def __init__(self, plan, calls):
        self.saved_plan = plan
        self.calls = calls
        self.seen = []

    def plan(self, job, context):
        return self.saved_plan.model_copy(deep=True)

    def build(self, job, tools):
        expected, response, check = self.calls[len(self.seen)]
        assert job.current_task_id == expected
        if check:
            check(job, tools)
        self.seen.append(expected)
        return apply_proposal(job, tools, response)


def migration_plan(*, quick=False):
    return low_plan(summary='Preserve rooms during migration', change_class='backend_internal',
        files=[MODULE, TEST], steps=['Migrate', 'Test'], risk='low', test_targets=[TEST],
        tasks=[PlanTask(id='T5', description='Implement migration', files=[MODULE]),
               PlanTask(id='T6', description='Test migration', files=[TEST], depends_on=['T5'],
                        quick_test_targets=[TEST] if quick else [])])


def assert_repair_context(job, tools):
    assert job.current_task_id in {'T5', 'implement_migration_script'}
    assert 'test_partial_failure_and_recovery_documented' in job.govern_feedback
    assert MODULE in next(task for task in job.plan.tasks if task.id == job.current_task_id).files
    assert any(item['path'] == MODULE and 'payload' in item['text'] for item in _file_excerpts(job, tools))


def run(tmp_path, plan, calls, *, base_files=None, approve=False, **overrides):
    for task in plan.tasks:
        if not task.acceptance:
            task.acceptance = [task.description]
    repo = init_repo(tmp_path / 'repo')
    if base_files:
        tools = ToolLayer(repo, policy())
        for path, text in base_files.items():
            tools.write(path, text)
        tools.commit('Base room models and store', list(base_files))
    rules = policy().model_copy(update={'flags': policy().flags.model_copy(update={'enabled': False}), **overrides})
    harness = Issue22Harness(plan, calls)
    factory = make_factory(tmp_path, repo, harness, policy=rules, keep_worktree=True)
    job = factory.run(feature(id='issue22', acceptance=['migration']))
    if approve:
        assert job.stage == Stage.AWAIT_HUMAN and job.resume_stage == Stage.BUILD
        job = factory.resume(job.id, 'approve')
    assert len(harness.seen) == len(calls), job.error or job.question
    return factory, job, harness


def assert_draft(job):
    assert job.stage == Stage.DONE, job.error or job.question
    assert job.pr and job.pr.dry_run
    assert json.loads(Path(job.pr.body_path).with_name('pr.json').read_text()) == job.pr.model_dump()
    assert job.verify_runs[-1].passed


def test_replay_12_module_owner_of_tests_only_task(tmp_path):
    _, job, harness = run(tmp_path, migration_plan(quick=True), [
        ('T5', proposal([write(MODULE, BAD_MODULE)]), None),
        ('T6', proposal([write(TEST, TESTS)]), None),
        ('T5', fix(), assert_repair_context),
    ])
    assert_draft(job)
    assert harness.seen == ['T5', 'T6', 'T5']
    assert job.task_retries == {'T5': 1}
    assert len(job.verify_runs) == 1
    assert job.task_original_files['T6'] == [TEST]


def test_replay_13_verify_budget_survives_spent_quick_budget(tmp_path):
    plan = migration_plan(quick=True)
    plan.tasks = [PlanTask(id='T5', description='Migrate and test', files=[MODULE, TEST], quick_test_targets=[TEST])]

    def repair(job, tools):
        assert_repair_context(job, tools)
        assert job.task_retries == {'T5': 3}
        assert job.verify_retries_used == 1
        assert job.verification_task_routes == {'T5': 1}

    calls = [('T5', proposal([write(MODULE, BAD_MODULE), write(TEST, TESTS)]), None)]
    calls += [('T5', proposal(), None)] * 3  # Spend the explicitly configured three quick-test retries.
    calls += [('T5', fix(), repair)]
    factory, job, _ = run(tmp_path, plan, calls, per_task_retries=3)
    assert_draft(job)
    assert [result.passed for result in job.verify_runs] == [False, True]
    restored = factory.status(job.id)
    assert restored.verify_retries_used == 1 and restored.verification_task_routes == {'T5': 1}
    assert restored.task_retries == {'T5': 3}


def test_replay_14_control_anchor_maps_to_original_and_logs(tmp_path):
    _, job, _ = run(tmp_path, migration_plan(), [
        ('T5', proposal([write(MODULE, BAD_MODULE)]), None),
        ('T6', proposal([write(TEST, TESTS)]), None),
        ('T5', fix(fallback=True), assert_repair_context),
    ])
    assert_draft(job)
    assert len(job.builds[-1].anchor_fallbacks) == 1
    assert any('Anchor fallback used' in event.message for event in job.events)
    tools = ToolLayer(Path(job.worktree.path), policy())
    assert 'Anchor fallback used' in tools.run(['git', 'log', '-1', '--format=%B']).output
    assert '\x12' not in tools.read(MODULE) and 'Migration → default home.' in tools.read(MODULE)


def test_replay_15_partial_no_tests_then_completed_exit5_names_file(tmp_path):
    def after_partial(job, tools):
        assert not job.quick_verify_runs and not job.task_retries

    def after_exit5(job, tools):
        assert 'No tests collected (pytest exit 5)' in job.govern_feedback
        assert TEST in job.govern_feedback
        assert job.task_retries == {'T6': 1}

    _, job, _ = run(tmp_path, migration_plan(quick=True), [
        ('T5', proposal([write(MODULE, BAD_MODULE)]), None),
        ('T6', proposal([write(TEST, 'from migrations.migration_0001_room_home import migrate\n')], task_complete=False,
                        commands=[['pytest', '-q', TEST]]), None),
        ('T6', proposal(task_complete=True), after_partial),
        ('T6', proposal([write(TEST, 'def test_ready():\n    assert True\n')]), after_exit5),
    ])
    assert_draft(job)
    assert [result.commands[0].exit_code for result in job.quick_verify_runs['T6']] == [5, 0]


def test_replay_r1_six_tasks_migration_repair_reaches_draft(tmp_path):
    # The original receipt's full approved six-task plan, not a synthetic task split.
    plan = ImplementationPlan.model_validate_json(
        (Path(__file__).parent / 'replays/issue_22_r1_plan.json').read_text())
    ids = [task.id for task in plan.tasks]

    def after_partial(job, tools):
        assert not job.quick_verify_runs
        assert job.task_retries == {}

    calls = [
        (ids[0], proposal(), None),
        (ids[1], proposal([write('app/project.py', "ROOM_FIELDS = ('room_id', 'home_id', 'context_key')\n"),
                          write(MODULE, '# Migration scaffold\n'), write(TEST, '# Test scaffold\n')]), None),
        (ids[2], proposal([write('app/store.py', "ROOMS_BY_ID = {}\nCONTEXT_KEYS_STABLE = True\n")]), None),
        (ids[3], proposal([write('app/flags.py', 'MIGRATION_FLAG = "decora-room-home-migration"\n'
                                              'FLAG_DEFAULT = False\n')]), None),
        (ids[4], proposal([write(MODULE, BAD_MODULE)]), None),
        (ids[5], proposal([write(TEST, '# Tests still being written\n')], task_complete=False,
                          commands=[['pytest', '-q', TEST]]), None),
        (ids[5], proposal([write(TEST, TESTS)]), after_partial),
        (ids[4], fix(fallback=True), assert_repair_context),
    ]
    migration = VerificationProfile(description='Migration behavioral checks', kind='migration',
                                    commands=[['{python}', '-m', 'pytest', '-q', TEST]])
    factory, job, harness = run(tmp_path, plan, calls, approve=True,
        base_files={'app/project.py': "ROOM_FIELDS = ('room_id', 'context_key')\n",
                    'app/store.py': 'ROOMS_BY_ID = {}\n'}, verification_profiles={'migration': migration})
    assert_draft(job)
    assert harness.seen == ids + [ids[5], ids[4]]
    assert job.task_retries == {ids[4]: 1} and job.verify_retries_used == 0
    assert job.task_split_attempts == {}
    assert job.builds[-1].anchor_fallbacks
    assert len(factory.status(job.id).completed_task_ids) == 6
    assert len(job.verify_runs) == 1


@pytest.mark.parametrize('budget', [0, 1, 3])
def test_verify_budget_fails_only_at_e22(tmp_path, budget):
    calls = [('T5', proposal([write(MODULE, BAD_MODULE)]), None),
             ('T6', proposal([write(TEST, TESTS)]), None)]
    calls += [('T5', proposal(), assert_repair_context)] * budget
    _, job, _ = run(tmp_path, migration_plan(), calls, verify_retries=budget, per_task_retries=0)
    assert job.stage == Stage.FAILED and job.pr is None
    assert f'E22: Job exhausted verify_retries ({budget})' in job.error
    assert len(job.verify_runs) == budget + 1
    assert job.verify_retries_used == budget and job.task_retries == {}


def test_normalized_anchor_rejects_ambiguity_and_escapes_diagnostics():
    with pytest.raises(FactoryError, match='normalized anchor matched 2 times'):
        anchor_span('doc.md', 'A → B\nA → B\n', 'A \x12 B')
    assert '\\u2192' in _anchor_error('doc.md', 'A → B\n', 'A \x13 C', 0)
    assert '\\x12' in _anchor_error('doc.md', 'A \x12 B\n', 'A C', 0)
    for control in ['\x1c', '\x85', '\r', '\t']:
        escaped = control.encode('unicode_escape').decode('ascii')
        assert escaped in _anchor_error('doc.md', 'A' + control + 'B\n', 'ACB', 0)
    text = 'prefix e\u0301 → tail'
    start, end, fallback = anchor_span('doc.md', text, 'é \x12')
    assert fallback and text[start:end] == 'e\u0301 →'


def test_routing_deepest_frame_relative_imports_and_stable_repair_scope(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write('app/other.py', 'VALUE = 1\n')
    tools.write('app/migration.py', 'VALUE = 2\n')
    tools.write('app/tests/test_relative.py', 'from .. import migration\n')
    plan = low_plan(summary='Routing', files=['app/other.py', 'app/tests/test_relative.py'],
        steps=[], tasks=[PlanTask(id='module', description='Other module', files=['app/other.py']),
                         PlanTask(id='tests', description='Tests', files=['app/tests/test_relative.py'])])
    job = bare_job(tmp_path, plan=plan)
    # Unowned imported module falls back to the test task and is added to repair scope.
    owner = reopen_task(job, 'FAILED app/tests/test_relative.py::test_value', tools)
    assert owner.id == 'tests' and 'app/migration.py' in owner.files
    assert 'app/migration.py' not in job.task_original_files['tests']
    assert any(item['path'] == 'app/migration.py' and 'VALUE = 2' in item['text']
               for item in _file_excerpts(job, tools))
    output = ('File "app/other.py", line 1\nFile "app/migration.py", line 1\n'
              'FAILED app/tests/test_relative.py::test_value')
    assert reopen_task(job, output, tools).id == 'tests'
    assert reopen_task(job, 'app/other.py:1: AssertionError', tools).id == 'module'


@pytest.mark.parametrize('kind', ['replace', 'insert_before', 'insert_after'])
def test_fallback_preserves_original_span_and_is_atomic(tmp_path, kind):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    original = 'prefix\ne\u0301 → home\nsuffix\n'
    tools.write('notes.txt', original)
    tools.commit('Unicode source', ['notes.txt'])
    job = bare_job(tmp_path, plan=low_plan(files=['notes.txt']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    edit = FileEdit(path='notes.txt', kind=kind, old='é \x12 home', new='NEW')
    # A later ambiguous fallback must undo the entire staged proposal.
    with pytest.raises(FactoryError, match='normalized anchor matched 2 times'):
        apply_proposal(job, tools, proposal([edit, write('ambiguous.txt', 'A → B\nA → B\n'),
            FileEdit(path='ambiguous.txt', kind='replace', old='A \x12 B', new='bad')]))
    assert tools.read('notes.txt') == original and not (repo / 'ambiguous.txt').exists()
    assert tools.head_sha() == job.worktree.base_sha
    result = apply_proposal(job, tools, proposal([edit]))
    expected = {'replace': 'NEW', 'insert_before': 'NEWe\u0301 → home', 'insert_after': 'e\u0301 → homeNEW'}
    assert tools.read('notes.txt') == 'prefix\n' + expected[kind] + '\nsuffix\n'
    assert result.anchor_fallbacks


@pytest.mark.parametrize(('source', 'old', 'span'), [
    ('Cafe\u0301', 'Café', 'Cafe\u0301'),
    ('\u1100\u1161\u11a8', '각', '\u1100\u1161\u11a8'),
    ('“hello”—world', '"hello"-world', '“hello”—world'),
    ('hel\u200blo\x00', 'hello', 'hel\u200blo'),
])
def test_nfc_and_explicit_character_folds(source, old, span):
    start, end, fallback = anchor_span('notes.txt', source, old)
    assert fallback and source[start:end] == span


def test_exact_anchor_wins_and_partial_expansion_is_rejected():
    text = 'A → B\nA -> B\n'
    assert anchor_span('notes.txt', text, 'A -> B') == (6, 12, False)
    with pytest.raises(FactoryError, match='anchor matched 0 times'):
        anchor_span('notes.txt', '→', '>')


def test_deepest_repo_frame_precedes_other_imported_module_owners(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write('app/inner.py', 'VALUE = 1\n')
    tools.write('app/outer.py', 'VALUE = 2\n')
    tools.write('tests/test_frames.py', 'from app.outer import VALUE\n')
    plan = low_plan(tasks=[PlanTask(id='inner', description='Inner', files=['app/inner.py']),
                           PlanTask(id='outer', description='Outer', files=['app/outer.py']),
                           PlanTask(id='tests', description='Tests', files=['tests/test_frames.py'])])
    job = bare_job(tmp_path, plan=plan)
    output = ('File "app/outer.py", line 1\nFile "app/inner.py", line 1\n'
              'FAILED tests/test_frames.py::test_value')
    assert reopen_task(job, output, tools).id == 'inner'
