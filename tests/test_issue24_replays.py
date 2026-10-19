"""Regression coverage for #24 / job-gh-5873513712-r3."""
from __future__ import annotations

import pytest

from factory.agents.prompts import BUILDER_INSTRUCTIONS, PLANNER_INSTRUCTIONS
from factory.governance.flags import audit_flag, verifier_requires_default_off

KEY = 'decora-room-home-migration'


@pytest.mark.parametrize('argument', ['default=False', 'False'])
def test_constant_bound_key(argument):
    code = f'KEY = {KEY!r}\nget_flag(KEY, ctx, {argument})\n'
    assert audit_flag(code, KEY, must_default_off=True) == []


@pytest.mark.parametrize('prefix', ['', 'FLAG_DEFAULT = False\n', 'get_flag(KEY, ctx, default=False)\n'])
def test_true_and_mixed_defaults_fail(prefix):
    code = f'KEY = {KEY!r}\n{prefix}get_flag(KEY, ctx, default=True)\n'
    problems = audit_flag(code, KEY, must_default_off=True)
    assert problems
    assert audit_flag(code, KEY, must_default_off=False) == []


def test_message_lists_forms_and_offending_lines():
    code = f'KEY = {KEY!r}\nget_flag(KEY, ctx, default=True)\n'
    message = '\n'.join(audit_flag(code, KEY, must_default_off=True))
    for evidence in ['FLAG_DEFAULT = False', 'default=False', 'positional False', '2:', 'default=True']:
        assert evidence in message
    assert verifier_requires_default_off(message)


def test_prompt_convention():
    for prompt in [BUILDER_INSTRUCTIONS, PLANNER_INSTRUCTIONS]:
        assert all(text in prompt for text in ['FLAG_KEY', 'FLAG_DEFAULT = False', 'default=FLAG_DEFAULT'])


def test_r3_receipt_diff_passes_audit():
    from pathlib import Path

    diff = (Path(__file__).parent / 'replays/issue_24_r3.diff').read_text()
    assert audit_flag(diff, KEY, must_default_off=True) == []


@pytest.mark.parametrize('flag_path', ['app/flags.py', 'app/rollout.py'])
def test_flag_failure_routes_to_owner(tmp_path, flag_path):
    from factory.models import PlanTask, Stage, WorktreeInfo
    from factory.tools.layer import ToolLayer
    from tests.support import ScriptedVerifier, bare_job, failed_verify, init_repo, low_plan, make_factory, policy

    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write(flag_path, f'KEY = {KEY!r}\n')
    job = bare_job(tmp_path, plan=low_plan(flag_key=KEY, files=[flag_path, 'tests/test_flag.py'], tasks=[
        PlanTask(id='add_launchdarkly_flag', description='Flag', files=[flag_path]),
        PlanTask(id='migration_tests', description='Tests', files=['tests/test_flag.py'],
                 depends_on=['add_launchdarkly_flag']),
    ]), completed_task_ids=['add_launchdarkly_flag', 'migration_tests'],
        worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    tools.write('tests/test_flag.py', 'from app import flags\n')
    factory = make_factory(tmp_path, repo, verifier=ScriptedVerifier([
        failed_verify('FLAG_DEFAULT must be False', classification='flag')]))
    transition = factory._verify(job)
    assert transition.stage == Stage.BUILD
    assert job.current_task_id == 'add_launchdarkly_flag'
    assert job.completed_task_ids == ['migration_tests']
    assert job.verification_task_routes == {'add_launchdarkly_flag': 1}
    assert flag_path in job.govern_feedback


@pytest.mark.parametrize('repair', [False, True], ids=['default-off', 'repair-true-default'])
def test_replay_r3(tmp_path, repair):
    from factory.models import PlanTask
    from tests.support import low_plan, policy
    from tests.test_issue22_replays import assert_draft, proposal, run, write

    module = 'app/project.py'
    test = 'tests/test_migration.py'
    plan = low_plan(flag_key=KEY, files=[module, 'app/flags.py', test],
        test_targets=[test], tasks=[
            PlanTask(id='model_extend_home', description='Migration compatibility', files=[module]),
            PlanTask(id='add_launchdarkly_flag', description='Register migration flag', files=['app/flags.py'],
                     acceptance=[f"Define KEY = '{KEY}' and FLAG_DEFAULT = False in app/flags.py.",
                                 'Gate new behavior with get_flag(KEY, context, default=FLAG_DEFAULT).']),
            PlanTask(id='migration_tests', description='Migration tests', files=[test],
                     depends_on=['model_extend_home', 'add_launchdarkly_flag']),
        ])
    code = ('from app.flags import get_flag, KEY\n\n'
            'def migrate(ctx):\n'
            '    return get_flag(KEY, ctx, default=False)\n')
    flags = (f'KEY = {KEY!r}\n'
             'def get_flag(key, ctx, default=False):\n    return default\n')
    if repair:
        flags += 'FLAG_DEFAULT = True\n'
    tests = ('from app.project import migrate\n\n'
             'def test_legacy_migration_defaults_off():\n    assert migrate({}) is False\n')
    calls = [
        ('model_extend_home', proposal([write(module, code)]), None),
        ('add_launchdarkly_flag', proposal([write('app/flags.py', flags)]), None),
        ('migration_tests', proposal([write(test, tests)]), None),
    ]
    if repair:
        def check_owner(job, tools):
            assert job.current_task_id == 'add_launchdarkly_flag'
            assert job.verification_task_routes == {'add_launchdarkly_flag': 1}
            assert 'app/flags.py' in job.govern_feedback
        calls.append(('add_launchdarkly_flag', proposal([
            write('app/flags.py', flags.replace('FLAG_DEFAULT = True\n', ''))]), check_owner))
    flags_policy = policy().flags.model_copy(update={'key_pattern': '^decora-[a-z0-9-]+$'})
    factory, job, harness = run(tmp_path, plan, calls, flags=flags_policy,
                               base_files={'app/flags.py': flags.replace('FLAG_DEFAULT = True\n', '')})
    assert_draft(job)
    assert job.verify_retries_used == int(repair)
    assert job.verification_task_routes == ({'add_launchdarkly_flag': 1} if repair else {})
    assert harness.seen[-1] == ('add_launchdarkly_flag' if repair else 'migration_tests')
    assert factory.status(job.id).stage == job.stage


@pytest.mark.parametrize('code', [
    f'get_flag({KEY!r}, ctx, default=False)\n',
    f'get_flag({KEY!r}, ctx, False)\n',
    f'KEY = {KEY!r}\nFLAG_DEFAULT = False\nget_flag(KEY, ctx, default=FLAG_DEFAULT)\n',
    f'KEY = {KEY!r}\nget_flag(KEY, ctx, default=False)\nget_flag(KEY, ctx, False)\n',
])
def test_accepted_default_off_forms(code):
    assert audit_flag(code, KEY, must_default_off=True) == []


@pytest.mark.parametrize('argument', ['True', 'default=True', '', 'default=None'])
def test_literal_key_mixed_defaults_fail(argument):
    code = f'get_flag({KEY!r}, ctx, default=False)\nget_flag({KEY!r}, ctx, {argument})\n'
    assert audit_flag(code, KEY, must_default_off=True)


def test_multiline_diagnostic_names_default_line():
    diff = ('diff --git a/app/flags.py b/app/flags.py\n+++ b/app/flags.py\n@@ -0,0 +20,4 @@\n'
            f'+get_flag(\n+    {KEY!r}, ctx,\n+    default=True,\n+)\n')
    message = '\n'.join(audit_flag(diff, KEY, must_default_off=True))
    assert 'app/flags.py:22:     default=True,' in message
