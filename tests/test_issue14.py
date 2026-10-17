import sys
from pathlib import Path

import pytest

from factory.agents.apply import apply_proposal
from factory.agents.prompts import PLANNER_INSTRUCTIONS
from factory.errors import GovernanceError
from factory.models import BuildProposal, FileEdit, JobState, Stage, WorktreeInfo
from factory.tools.layer import ToolLayer
from factory.verification.gate import VerificationGate
from tests.support import (
    ScriptedHarness,
    ScriptedVerifier,
    bare_job,
    feature,
    init_repo,
    low_plan,
    make_factory,
    passed_verify,
    policy,
)


def proposal(*paths):
    return BuildProposal(summary='Add modules', commit_message='Add modules', edits=[
        FileEdit(path=path, kind='write', content='') for path in paths
    ])


def test_nested_test_imports_root_package_without_repo_pytest_config(tmp_path):
    repo = init_repo(tmp_path / 'repo with spaces')
    (repo / 'venv/bin').mkdir(parents=True)
    (repo / 'venv/bin/pytest').symlink_to(Path(sys.executable).parent / 'pytest')
    tools = ToolLayer(repo, policy(), target_repo=repo)
    tools.write('tests/migrations/test_import.py',
                'from app.flags import get_flag\n\ndef test_import():\n'
                '    assert get_flag("missing", {}, default=False) is False\n')
    job = bare_job(tmp_path, plan=low_plan(test_targets=['tests/migrations/test_import.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    reports = VerificationGate(repo, tmp_path / 'home')._pytest(job, tools)
    assert reports[0].passed, reports[0].output


def test_new_nested_package_initializers_are_approved(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['app/migrations/rooms/migrate.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    edits = proposal(
        'app/migrations/__init__.py', 'app/migrations/rooms/__init__.py',
        'app/migrations/rooms/migrate.py')
    edits.edits[0].content = '"""Migrations."""\n'
    result = apply_proposal(job, tools, edits)
    assert set(result.changed_files) == set(job.plan.files)
    assert 'app/__init__.py' not in job.plan.files
    # Automatic approvals survive subsequent attempts after the directories exist.
    apply_proposal(job, tools, BuildProposal(
        summary='Document package', commit_message='Document package', edits=[
            FileEdit(path='app/migrations/__init__.py', kind='replace',
                     old='"""Migrations."""', new='"""Room migrations."""')]))


def test_initializer_can_be_added_after_planned_module_creates_package(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['app/migrations/migrate.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    apply_proposal(job, tools, proposal('app/migrations/migrate.py'))
    assert (repo / 'app/migrations').is_dir()
    result = apply_proposal(job, tools, proposal('app/migrations/__init__.py'))
    assert 'app/migrations/__init__.py' in result.changed_files


@pytest.mark.parametrize('explicitly_planned', [False, True])
def test_missing_initializer_is_required_only_when_explicitly_planned(tmp_path, explicitly_planned):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    initializer = 'app/migrations/__init__.py'
    files = ['app/migrations/migrate.py'] + ([initializer] if explicitly_planned else [])
    job = bare_job(tmp_path, plan=low_plan(files=files, flag_key=None, test_targets=[]),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    apply_proposal(job, tools, proposal('app/migrations/migrate.py'))
    # Scope permissions and required deliverables survive a saved-job round trip.
    job = JobState.model_validate_json(job.model_dump_json())
    result = VerificationGate(repo, tmp_path / 'home').run(job, tools)
    assert not (repo / initializer).exists()
    if explicitly_planned:
        assert result.classification == 'missing_files'
        assert result.commands == []
        assert f'Create {initializer} covering' in result.actionable
    else:
        assert initializer in job.auto_approved_initializers
        assert result.classification != 'missing_files'
        assert result.commands  # Full verification still runs.
    apply_proposal(job, tools, proposal(initializer))
    assert (repo / initializer).is_file()


def test_replanning_clears_optional_initializer_permissions(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    initializer = 'app/migrations/__init__.py'
    plan = low_plan(files=[initializer])
    factory = make_factory(tmp_path, repo, ScriptedHarness([plan]))
    job = bare_job(tmp_path)
    factory._context(job)
    job.auto_approved_initializers = [initializer]
    assert factory._plan(job).stage == Stage.GOVERN_PLAN
    assert job.auto_approved_initializers == []
    assert job.plan.files == [initializer]


def test_unlisted_edit_is_corrected_then_job_completes(tmp_path):
    repo = init_repo(tmp_path / 'repo')

    def stray(job, tools):
        return apply_proposal(job, tools, proposal('unlisted.py'))

    def corrected(job, tools):
        assert 'unlisted.py' in job.govern_feedback
        assert 'app/room_mood.py' in job.govern_feedback
        assert not (tools.worktree / 'unlisted.py').exists()
        return apply_proposal(job, tools, proposal('app/room_mood.py'))

    harness = ScriptedHarness([low_plan()], builds=[stray, corrected])
    rules = policy()
    rules.flags.enabled = False
    job = make_factory(tmp_path, repo, harness, policy=rules,
                       verifier=ScriptedVerifier([passed_verify()])).run(feature())
    assert job.stage == Stage.DONE
    assert job.retries == {'plan_scope': 1}


@pytest.mark.parametrize('limit', [0, 1, 2, None])
def test_plan_scope_has_its_own_retry_cap(tmp_path, limit):
    repo = init_repo(tmp_path / 'repo')

    def stray(job, tools):
        return apply_proposal(job, tools, proposal('unlisted.py'))

    rules = policy()
    rules.max_retries = {'build': 0}
    if limit is not None:
        rules.max_retries['plan_scope'] = limit
    else:
        limit = 2
    harness = ScriptedHarness([low_plan()], builds=[stray])
    job = make_factory(tmp_path, repo, harness, policy=rules).run(feature())
    assert job.stage == Stage.FAILED
    assert harness.build_calls == limit + 1
    assert job.retries.get('plan_scope', 0) == limit
    assert 'unlisted.py' in job.error


@pytest.mark.parametrize('path', [
    '.env', 'app/pim/private.py', '../escape.py', '/tmp/escape.py',
    'app/../unlisted.py', 'escape/file.py', 'secret.py',
])
def test_unsafe_edits_stay_fatal_even_after_an_unlisted_edit(tmp_path, path):
    repo = init_repo(tmp_path / 'repo')
    (repo / 'escape').symlink_to(tmp_path, target_is_directory=True)
    (repo / 'secret.py').symlink_to(repo / '.env')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    with pytest.raises(GovernanceError):
        apply_proposal(job, tools, proposal('unlisted.py', path))
    assert not (repo / 'unlisted.py').exists()


def test_planner_requires_importable_python_module_names():
    assert 'valid Python identifiers' in PLANNER_INSTRUCTIONS
    assert 'start with a digit' in PLANNER_INSTRUCTIONS
    assert 'migration_0001_room_home.py' in PLANNER_INSTRUCTIONS


@pytest.mark.parametrize('path', ['app/__init__.py', 'app/other/__init__.py'])
def test_initializers_outside_new_planned_packages_need_correction(tmp_path, path):
    from factory.errors import PlanScopeError

    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['app/migrations/migrate.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    with pytest.raises(PlanScopeError, match=path):
        apply_proposal(job, tools, proposal(path))
