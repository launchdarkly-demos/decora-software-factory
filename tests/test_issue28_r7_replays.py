"""The captured r7 plan must survive normalization and governance repair."""
import pytest

from factory.governance.policy import load_policy, review_plan
from factory.models import ImplementationPlan, PlanTask
from factory.plan_normalization import is_test_task, normalize_flag_task, normalize_test_tasks
from factory.verification.profiles import task_problems
from tests.support import ROOT, bare_job, feature, low_plan, policy


@pytest.mark.parametrize('echo', [False, True, 'duplicates'])
def test_replay_r7_owner_order_and_idempotence(tmp_path, echo):
    plan = ImplementationPlan.model_validate_json((ROOT / 'tests/replays/issue_28_r7_plan.json').read_text())
    removed = {task.id for task in plan.tasks if task.id.startswith('provision_flag_')}
    if echo:
        removed.remove('provision_flag_decora_room_home_migration')
    if echo == 'duplicates':
        removed.clear()
    plan.tasks = [task for task in plan.tasks if task.id not in removed]
    for task in plan.tasks:
        task.depends_on = [dep for dep in task.depends_on if dep not in removed]
    plan.flag_task_id = None
    plan.normalization_notes = []
    rules = load_policy(ROOT / 'policy.decora.yaml')
    job = bare_job(tmp_path, plan=plan, feature=feature(
        issue_url='https://github.com/arober39/decor-agent/issues/8'))
    decision = review_plan(rules, plan, job)
    assert not decision.retry_plan, decision.reason
    assert decision.resume_stage == 'build'  # Only the existing migration approval remains.
    assert task_problems(plan) == []
    expected = 'provision_flag_decora_room_home_migration' if echo else 'add-migration-flag-and-checks'
    assert plan.flag_task_id == expected
    assert sum(task.id.startswith('provision_flag_') for task in plan.tasks) == bool(echo)
    owner = next(task for task in plan.tasks if task.id == expected)
    assert 'app/flags.py' in owner.files
    assert "DECORA_ROOM_HOME_MIGRATION_FLAG = 'decora-room-home-migration'" in ' '.join(owner.acceptance)
    assert 'get_flag(DECORA_ROOM_HOME_MIGRATION_FLAG, context, default=FLAG_DEFAULT)' in ' '.join(owner.acceptance)
    assert all(expected in task.depends_on for task in plan.tasks if is_test_task(task))
    docs = next(task for task in plan.tasks if task.id == 'document-migration-recovery-and-guidelines')
    if echo != 'duplicates':
        assert docs.depends_on == ['write-migration-tests_part_3']
        assert docs.id not in owner.depends_on
    before = plan.model_dump()
    review_plan(rules, plan, job)
    assert plan.model_dump() == before


def test_cycle_repair_prefers_added_edges_then_production_to_test(tmp_path):
    plan = low_plan(tasks=[
        PlanTask(id='owner', description='Flag', files=['app/flags.py'], acceptance=['Flag'], depends_on=['later']),
        PlanTask(id='later', description='Later', files=['app/main.py'], acceptance=['Later'], depends_on=['owner']),
        PlanTask(id='docs', description='Docs', files=['README.md'], acceptance=['Docs'], depends_on=['tests']),
        PlanTask(id='tests', description='Test', files=['tests/test_main.py'],
                 acceptance=['Test'], depends_on=['docs']),
    ], files=['app/flags.py', 'app/main.py', 'README.md', 'tests/test_main.py'])
    job = bare_job(tmp_path, plan=plan)
    normalize_flag_task(policy(), job, plan)
    assert task_problems(plan) == []
    assert 'tests' not in next(task for task in plan.tasks if task.id == 'docs').depends_on
    assert next(task for task in plan.tasks if task.id == 'later').depends_on == ['owner']
    assert any('owner -> later (normalization-added)' in note for note in plan.normalization_notes)
    assert any('docs -> tests (production-to-test)' in note for note in plan.normalization_notes)
    before = plan.model_dump()
    normalize_flag_task(policy(), job, plan)
    assert plan.model_dump() == before


def test_test_split_repairs_forward_unknown_and_cyclic_dependencies():
    plan = low_plan(tasks=[
        PlanTask(id='docs', description='Docs', files=['README.md'], acceptance=['Docs'], depends_on=['tests']),
        PlanTask(id='tests', description='Tests', files=['tests/test_main.py'],
                 acceptance=[str(i) for i in range(7)], depends_on=['missing']),
    ], files=['README.md', 'tests/test_main.py'])
    normalize_test_tasks(plan)
    assert task_problems(plan) == []
    assert plan.tasks[-1].id == 'docs'
    assert plan.tasks[-1].depends_on == [plan.tasks[-2].id]
    assert any('missing' in note for note in plan.normalization_notes)
    before = plan.model_dump()
    normalize_test_tasks(plan)
    assert plan.model_dump() == before


def test_owner_identity_and_transitive_test_dependents_survive_sorting(tmp_path):
    plan = low_plan(tasks=[
        PlanTask(id='owner', description='Flag', files=['app/flags.py'], acceptance=['Flag']),
        PlanTask(id='other', description='Other flag work', files=['app/flags.py'], acceptance=['Other']),
        PlanTask(id='docs', description='Docs', files=['README.md'], acceptance=['Docs'], depends_on=['tests']),
        PlanTask(id='docs_followup', description='More docs', files=['README.md'],
                 acceptance=['More docs'], depends_on=['docs']),
        PlanTask(id='tests', description='Test', files=['tests/test_main.py'], acceptance=['Test']),
    ], files=['app/flags.py', 'README.md', 'tests/test_main.py'])
    job = bare_job(tmp_path, plan=plan)
    normalize_flag_task(policy(), job, plan)
    assert plan.flag_task_id == 'owner'
    assert plan.tasks[0].id == 'other'
    assert next(task for task in plan.tasks if task.id == 'owner').depends_on == ['other']
    assert task_problems(plan) == []
    before = plan.model_dump()
    normalize_flag_task(policy(), job, plan)
    assert plan.model_dump() == before


def test_provision_merge_preserves_scope_acceptance_and_repoints_dependencies(tmp_path):
    plan = low_plan(tasks=[
        PlanTask(id='module', description='Module work', files=['app/flags.py'], acceptance=['Existing work']),
        PlanTask(id='provision_flag_one', description='Flag', files=['app/main.py'], acceptance=['First check']),
        PlanTask(id='provision_flag_two', description='Flag', files=['README.md'], acceptance=['Second check']),
        PlanTask(id='consumer', description='Consumer', files=['app/main.py'],
                 acceptance=['Consume'], depends_on=['provision_flag_two']),
    ], files=['app/flags.py', 'app/main.py', 'README.md'])
    job = bare_job(tmp_path, plan=plan)
    normalize_flag_task(policy(), job, plan)
    owner = next(task for task in plan.tasks if task.id == plan.flag_task_id)
    assert owner.id == 'provision_flag_one'
    assert set(owner.files) == {'app/flags.py', 'app/main.py', 'README.md'}
    assert {'First check', 'Second check'} <= set(owner.acceptance)
    assert next(task for task in plan.tasks if task.id == 'consumer').depends_on == [owner.id]
    assert 'provision_flag_two' not in {task.id for task in plan.tasks}
    assert task_problems(plan) == []
    before = plan.model_dump()
    normalize_flag_task(policy(), job, plan)
    assert plan.model_dump() == before


def test_test_normalizer_breaks_production_cycle_deterministically():
    plan = low_plan(tasks=[
        PlanTask(id='a', description='A', files=['app/main.py'], acceptance=['A'], depends_on=['b']),
        PlanTask(id='b', description='B', files=['app/main.py'], acceptance=['B'], depends_on=['a']),
    ], files=['app/main.py'])
    normalize_test_tasks(plan)
    assert task_problems(plan) == []
    assert plan.normalization_notes == ['Dropped cycle dependency a -> b (fallback).']
    before = plan.model_dump()
    normalize_test_tasks(plan)
    assert plan.model_dump() == before
