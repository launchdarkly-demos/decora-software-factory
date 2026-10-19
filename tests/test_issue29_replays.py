"""#29 / r7: base-content guards and revised plan scope."""
import json
from pathlib import Path

import pytest

from factory.agents.apply import apply_proposal
from factory.errors import DestructiveEditError
from factory.governance.policy import load_policy, review_plan
from factory.models import FileEdit, ImplementationPlan, PlanTask, WorktreeInfo
from factory.plan_normalization import normalize_test_tasks
from factory.tools.layer import ToolLayer
from factory.verification.profiles import task_problems
from tests.support import ROOT, ScriptedHarness, bare_job, feature, init_repo, low_plan, make_factory, policy
from tests.test_issue22_replays import assert_draft, proposal, write

MODULE = 'migrations/migration_0001_room_home.py'
STUB = ''.join(f'# stub line {i}\n' for i in range(35))
FINAL = 'def migrate():\n    return "default-home"\n'


def test_replay_r7(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    rules = policy()
    rules.flags.enabled = False
    tools = ToolLayer(repo, rules)
    tools.write('tests/test_migration.py',
                'from migrations.migration_0001_room_home import migrate\n\n\n'
                'def test_migration():\n    assert migrate() == "default-home"\n')
    tools.commit('Migration acceptance check', ['tests/test_migration.py'])
    plan = low_plan(change_class='backend_internal', files=[MODULE], test_targets=['tests/test_migration.py'], tasks=[
        PlanTask(id='A', description='Create migration stub', files=[MODULE], acceptance=['Stub exists']),
        PlanTask(id='B', description='Implement migration', files=[MODULE], depends_on=['A'],
                 acceptance=['Migration returns default-home']),
    ])

    def create(job, tools):
        assert job.current_task_id == 'A'
        return apply_proposal(job, tools, proposal([write(MODULE, STUB)]))

    def replace(job, tools):
        assert job.current_task_id == 'B'
        assert tools.read(MODULE) == STUB and len(STUB.splitlines()) == 35
        return apply_proposal(job, tools, proposal([
            FileEdit(path=MODULE, kind='replace', old=STUB, new=FINAL)]))

    factory = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[create, replace]),
                           policy=rules, keep_worktree=True)
    job = factory.run(feature())
    assert_draft(job)
    assert job.task_retries == {} and job.builder_calls == 2
    assert job.worktree is not None
    assert not (repo / MODULE).exists()
    assert ToolLayer(Path(job.worktree.path), rules).read(MODULE) == FINAL
    assert factory.status(job.id).completed_task_ids == ['A', 'B']


@pytest.mark.parametrize('kind', ['write', 'replace', 'delete'])
def test_base_removal_rejected_with_actionable_feedback_and_restore(tmp_path, kind):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    source = ''.join(f'base line {i}\n' for i in range(80))
    tools.write('existing.txt', source)
    sha = tools.commit('Base source', ['existing.txt'])
    job = bare_job(tmp_path, plan=low_plan(files=['existing.txt']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=sha))
    tools.write('README.md', 'dirty source\n')
    fields = {'content': 'replacement\n'} if kind == 'write' else (
        {'old': source, 'new': 'replacement\n'} if kind == 'replace' else {})
    with pytest.raises(DestructiveEditError) as error:
        apply_proposal(job, tools, proposal([write('new.txt', 'never written\n'),
                                           FileEdit(path='existing.txt', kind=kind, **fields)]))
    feedback = str(error.value)
    assert '80/80 lines (100%)' in feedback
    assert 'keep existing lines' in feedback and 'needed region' in feedback
    assert 'several smaller replace/insert_* edits' in feedback
    assert 'rewrite_files' not in feedback
    assert tools.read('existing.txt') == source and tools.read('README.md') == 'mini decora\n'
    assert not (repo / 'new.txt').exists()
    assert tools.head_sha() == sha


@pytest.mark.parametrize('addition_position', ['before', 'after'])
def test_lines_added_earlier_in_job_are_exempt(tmp_path, addition_position):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    base = ''.join(f'base line {i}\n' for i in range(80))
    added = ''.join(f'job addition {i}\n' for i in range(80))
    tools.write('existing.txt', base)
    sha = tools.commit('Base source', ['existing.txt'])
    job = bare_job(tmp_path, plan=low_plan(files=['existing.txt']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=sha))
    current = added + base if addition_position == 'before' else base + added
    apply_proposal(job, tools, proposal([write('existing.txt', current)]))
    apply_proposal(job, tools, proposal([write('existing.txt', base)]))
    assert tools.read('existing.txt') == base
    # Prior additions must not inflate the denominator and permit base destruction.
    apply_proposal(job, tools, proposal([write('existing.txt', current)]))
    with pytest.raises(DestructiveEditError, match='80/80'):
        apply_proposal(job, tools, proposal([write('existing.txt', added)]))
    assert tools.read('existing.txt') == current


def test_normalization_drops_echoed_files_and_repoints_empty_tasks(tmp_path):
    plan = ImplementationPlan.model_validate_json((ROOT / 'tests/replays/issue_28_r7_plan.json').read_text())
    excluded = 'mcp_servers/decor_design.py'
    plan.files.remove(excluded)
    removed = 'update-mcp-server-for-home-compatibility'
    plan.tasks.append(PlanTask(id='stale_followup', description='Previous scope', files=[excluded],
                               depends_on=[removed]))
    plan.tasks.append(PlanTask(id='docs_followup', description='Docs', files=['docs/room_home_migration.md'],
                               depends_on=['stale_followup'], acceptance=['Document migration']))
    rules = load_policy(ROOT / 'policy.decora.yaml')
    job = bare_job(tmp_path, plan=plan, feature=feature(
        issue_url='https://github.com/arober39/decor-agent/issues/8'))
    decision = review_plan(rules, plan, job)
    assert not decision.retry_plan, decision.reason
    assert task_problems(plan) == []
    assert all(set(task.files) <= set(plan.files) for task in plan.tasks)
    assert all(excluded not in task.files for task in plan.tasks)
    assert not {removed, 'stale_followup'} & {task.id for task in plan.tasks}
    followup = next(task for task in plan.tasks if task.id == 'docs_followup')
    assert followup.depends_on == ['add-migration-flag-and-checks']
    notes = '\n'.join(plan.normalization_notes)
    assert f'Dropped files outside plan.files from {removed}: {excluded}' in notes
    assert f'Removed empty task {removed}' in notes and 'Repointed docs_followup' in notes
    before = plan.model_dump()
    review_plan(rules, plan, job)
    assert plan.model_dump() == before
    assert json.loads(plan.model_dump_json())['normalization_notes'] == plan.normalization_notes


def test_scope_cleanup_without_required_flag():
    plan = low_plan(files=['server.py'], tasks=[
        PlanTask(id='first', description='Production', files=['server.py', 'stale.py'],
                 rewrite_files=['stale.py'], quick_test_targets=['stale.py::test_old']),
        PlanTask(id='gone', description='Old', files=['stale.py'], depends_on=['first']),
        PlanTask(id='last', description='Production', files=['server.py'], depends_on=['gone']),
    ])
    normalize_test_tasks(plan)
    assert [(task.id, task.files, task.depends_on) for task in plan.tasks] == [
        ('first', ['server.py'], []), ('last', ['server.py'], ['first'])]
    assert plan.tasks[0].rewrite_files == []
