from pathlib import Path

import pytest

from factory.errors import GovernanceError
from factory.governance.flags import audit_flag, verifier_requires_default_off
from factory.models import JobState, Stage, VerifyResult
from factory.tools.layer import ToolLayer
from tests.support import feature, policy


def test_shipped_policy_refuses_push_and_keeps_pull_requests_draft() -> None:
    rules = policy()
    assert rules.pull_request.allow_push is False
    assert rules.pull_request.draft_only is True
    assert rules.flags.must_default_off is True
    with pytest.raises(GovernanceError, match="git push"):
        rules.check_command(["git", "push", "origin", "HEAD"])
    with pytest.raises(GovernanceError, match="denied"):
        rules.check_command(["git", "reset", "--hard"])
    with pytest.raises(GovernanceError, match="not allowlisted"):
        rules.check_command(["curl", "https://example.test"])


def test_tool_layer_denies_secrets_and_the_catalog(tmp_path: Path) -> None:
    tools = ToolLayer(tmp_path, policy())
    with pytest.raises(GovernanceError):
        tools.write(".env", "TOKEN=1\n")
    with pytest.raises(GovernanceError):
        tools.write("app/pim/catalog.json", "{}\n")
    tools.write("app/room_mood.py", "FLAG_DEFAULT = False\n")
    assert (tmp_path / "app" / "room_mood.py").read_text().startswith("FLAG_DEFAULT")


def test_flag_audit_requires_the_key_and_a_default_of_off() -> None:
    good = 'ROOM_MOOD_FLAG = "decor-room-mood"\nFLAG_DEFAULT = False\n'
    assert audit_flag(good, "decor-room-mood", must_default_off=True) == []
    bad = 'FLAG_DEFAULT = True\nFLAG_KEY = "decor-room-mood"\n'
    problems = audit_flag(bad, "decor-room-mood", must_default_off=True)
    assert "FLAG_DEFAULT must be False" in " ".join(problems)
    assert audit_flag("no flag here", None, must_default_off=True)


def test_flag_audit_ignores_removed_defaults_and_test_comments() -> None:
    diff = '''diff --git a/server.py b/server.py
--- a/server.py
+++ b/server.py
@@ -1,2 +1,2 @@
-FLAG_DEFAULT = True
+FLAG_DEFAULT = False
+FLAG_KEY = "decor-room-mood"
diff --git a/test_mood.py b/test_mood.py
+++ b/test_mood.py
+# Check that FLAG_DEFAULT = True fails.
+FLAG_DEFAULT = True
'''
    assert audit_flag(diff, "decor-room-mood", must_default_off=True) == []
    enabled = diff.replace("+FLAG_DEFAULT = False", "+FLAG_DEFAULT = True")
    assert audit_flag(enabled, "decor-room-mood", must_default_off=True)


def test_test_fixtures_cannot_satisfy_the_production_flag_audit() -> None:
    diff = '''diff --git a/test_mood.py b/test_mood.py
+++ b/test_mood.py
+FLAG_DEFAULT = False
+FLAG_KEY = "decor-room-mood"
'''
    assert len(audit_flag(diff, "decor-room-mood", must_default_off=True)) == 2


def test_command_environment_does_not_restore_factory_secrets(tmp_path: Path) -> None:
    rules = policy()
    rules.command_environment = {
        "ANTHROPIC_API_KEY": "dummy-test-value",
        "OPENAI_API_KEY": "should-be-stripped",
    }
    env = ToolLayer(tmp_path, rules)._env()
    assert env["ANTHROPIC_API_KEY"] == "dummy-test-value"
    assert "OPENAI_API_KEY" not in env
    assert env["PYTHON_DOTENV_DISABLED"] == "1"


def test_builder_repair_reads_the_verifier_text() -> None:
    from factory.agents.recipes import ship_flag_on

    job = JobState(
        id="job-test",
        stage=Stage.BUILD,
        feature=feature(demo_fail_first_build=True, user_facing=True, flag_key="decor-room-mood"),
        created_at=1,
        updated_at=1,
    )
    assert ship_flag_on(job) is True
    job.verify_runs.append(
        VerifyResult(
            passed=False,
            commands=[],
            actionable="FLAG_DEFAULT must be False",
            classification="flag",
        )
    )
    assert ship_flag_on(job) is False
    assert verifier_requires_default_off("assert FLAG_DEFAULT is False")


def test_import_rewrite_matches_the_decora_server_line() -> None:
    from factory.agents.recipes import add_import_name

    line = "from app.flags import BOARD_INTAKE_FLAG, build_context, get_flag, set_current_user_tier\n"
    rewritten = add_import_name(line, "ROOM_MOOD_FLAG")
    assert rewritten == (
        "from app.flags import BOARD_INTAKE_FLAG, ROOM_MOOD_FLAG, "
        "build_context, get_flag, set_current_user_tier\n"
    )


def test_missing_acceptance_cannot_be_invented_by_planner(tmp_path):
    from factory.governance.policy import review_plan
    from tests.support import bare_job, feature, low_plan, policy

    job = bare_job(tmp_path, feature=feature(acceptance=[]))
    decision = review_plan(policy(), low_plan(), job)
    assert decision.pause
    assert decision.resume_stage == "plan"


def test_frontend_flag_audit_checks_production_default():
    diff = '''diff --git a/web/flags.ts b/web/flags.ts
+++ b/web/flags.ts
+export const FLAG_DEFAULT = false;
+const FLAG_KEY = "decor-example";
'''
    assert audit_flag(diff, 'decor-example', must_default_off=True) == []
    assert audit_flag(diff.replace('= false', '= true'), 'decor-example', must_default_off=True)


@pytest.mark.parametrize('policy_name', ['policy.yaml', 'policy.decora.yaml'])
@pytest.mark.parametrize('risk', ['low', 'medium', 'high'])
def test_disabled_flags_never_block_or_call_launchdarkly(tmp_path, policy_name, risk):
    from factory.governance.policy import flag_required, load_policy, review_plan, review_ship
    from factory.launchdarkly.flags import LaunchDarklyService
    from factory.publish.pr import render_pr
    from tests.support import ROOT, bare_job, low_plan

    rules = load_policy(ROOT / policy_name)
    rules.flags.enabled = False
    job = bare_job(tmp_path, feature=feature(flag_key='stale-request-key', user_facing=True))
    job.plan = low_plan(risk=risk, large_change=True, important_ux_change=True, flag_default=True)
    assert not flag_required(rules, job)
    decision = review_plan(rules, job.plan, job)
    if risk == 'high':
        assert decision.pause and decision.resume_stage == 'build'
    else:
        assert decision.allowed
    assert review_ship(rules, job, 'no flag implementation').allowed

    class NoCalls:
        def request(self, *args, **kwargs):
            raise AssertionError('LaunchDarkly must not be contacted')

    job.launchdarkly = LaunchDarklyService(mode='live', policy=rules, target_repo=tmp_path,
                                         client=NoCalls(), environ={}).ensure(job)
    assert not job.launchdarkly.required
    assert not job.launchdarkly.blocks_review()
    body = render_pr(job, '')
    assert 'stale-request-key' not in body
    assert 'Behind `' not in body


def governed_job(tmp_path, **plan_fields):
    from tests.support import bare_job, low_plan
    return bare_job(tmp_path, plan=low_plan(flag_key=None, **plan_fields))


@pytest.mark.parametrize('classification,required,guarded', [
    ('user_facing_behavior', True, True), ('ui_cosmetic', False, False),
    ('backend_internal', False, False), ('data_migration', True, True),
    ('refactor', False, False), ('docs_only', False, False), ('tests_only', False, False),
    ('config_ci', False, False), ('dependency', False, False),
])
def test_class_rules(tmp_path, classification, required, guarded):
    from factory.governance.policy import flag_required, guarded_rollout_required, review_ship
    job = governed_job(tmp_path, change_class=classification)
    assert flag_required(policy(), job) is required
    assert guarded_rollout_required(policy(), job) is guarded
    assert review_ship(policy(), job, 'no flag surface').retry_build is required


@pytest.mark.parametrize('classification', ['refactor', 'docs_only', 'tests_only', 'config_ci', 'dependency'])
def test_exempt_classes_skip_flag_audit_and_provisioning(tmp_path, classification):
    from factory.governance.policy import review_plan, review_ship
    from factory.launchdarkly.flags import LaunchDarklyService
    job = governed_job(tmp_path, change_class=classification, risk='high', large_change=True)
    job.feature.flag_key = 'decor-legacy'
    rules = policy()
    assert review_plan(rules, job.plan, job).resume_stage == 'build'
    assert review_ship(rules, job, 'no flag surface').allowed
    record = LaunchDarklyService(mode='live', policy=rules, target_repo=tmp_path, environ={}).ensure(job)
    assert not record.required


@pytest.mark.parametrize('risk,large,required', [
    ('low', False, False), ('medium', False, True), ('high', False, True), ('low', True, True),
])
def test_backend_conditional_flags(tmp_path, risk, large, required):
    from factory.governance.policy import flag_required
    job = governed_job(tmp_path, change_class='backend_internal', risk=risk, large_change=large)
    assert flag_required(policy(), job) is required


@pytest.mark.parametrize('files,migration,expected', [
    (['docs/guide.txt', 'README.md'], False, 'docs_only'),
    (['tests/test_service.py'], False, 'tests_only'),
    (['app/store.py'], True, 'data_migration'), (['app/service.py'], False, None),
])
def test_class_fallback(tmp_path, files, migration, expected):
    from factory.governance.policy import change_class, review_plan
    job = governed_job(tmp_path, change_class=None, files=files, requires_migration=migration)
    assert change_class(job, job.plan) == expected
    if expected is None:
        decision = review_plan(policy(), job.plan, job)
        assert decision.pause and 'Which change_class' in decision.reason


def test_class_overrides_and_conflicts(tmp_path):
    from factory.governance.policy import change_class, review_plan
    job = governed_job(tmp_path, change_class='user_facing_behavior')
    job.feature.labels = ['factory:class/docs_only']  # No ready label needed by governance.
    assert change_class(job, job.plan) == 'docs_only'
    assert review_plan(policy(), job.plan, job).allowed
    job.feature.change_class = 'refactor'
    assert review_plan(policy(), job.plan, job).pause


@pytest.mark.parametrize('feature_key,plan_key', [
    ('decor-one', 'decor-two'), (None, 'decor-one,decor-two'),
    (None, 'Wrong_Key'), (None, 'decor-one decor-two'),
])
def test_invalid_or_multiple_flag_keys_retry_plan(tmp_path, feature_key, plan_key):
    from factory.governance.policy import review_plan
    job = governed_job(tmp_path)
    job.plan.flag_key = plan_key
    job.feature.flag_key = feature_key
    assert review_plan(policy(), job.plan, job).retry_plan


def test_default_naming_rule_and_optional_flag(tmp_path):
    from factory.governance.policy import Policy, flag_required, review_plan
    rules = Policy()
    rules.flags.enabled = True
    job = governed_job(tmp_path, change_class='ui_cosmetic')
    job.plan.flag_key = 'decora-button'
    assert review_plan(rules, job.plan, job).allowed
    assert flag_required(rules, job)
    job.plan.flag_key = 'decor-button'
    assert review_plan(rules, job.plan, job).retry_plan


def test_placeholder_is_no_flag(tmp_path):
    from factory.governance.policy import flag_keys, review_plan
    from factory.intake.load import parse_issue_body
    job = governed_job(tmp_path)
    job.feature = parse_issue_body('Example', '## Acceptance\n- works\n## Rollout\nkey: replace-with-your-flag-key')
    assert job.feature.flag_key is None
    job.plan.flag_key = 'replace-with-your-flag-key'
    assert not flag_keys(job)
    assert review_plan(policy(), job.plan, job).retry_plan


@pytest.mark.parametrize('metric,default,allowed,pause,retry', [
    (None, 'errors', ['errors'], False, False),
    ('latency', 'errors', ['latency', 'errors'], False, False),
    ('unapproved', 'errors', ['errors'], True, True),
    (None, '', [], True, False),
    (None, 'unapproved', ['errors'], True, True),
])
def test_guarded_rollout_metrics(tmp_path, metric, default, allowed, pause, retry):
    from factory.governance.policy import review_plan
    job = governed_job(tmp_path, guarded_rollout_metric=metric)
    job.plan.flag_key = 'decor-feature'
    rules = policy()
    rules.guarded_rollout.default_metric_key = default
    rules.guarded_rollout.allowed_metric_keys = allowed
    decision = review_plan(rules, job.plan, job)
    assert decision.pause is pause
    assert decision.retry_plan is retry
    if not pause:
        assert job.plan.guarded_rollout_metric == (metric or default)


def test_migration_always_pauses_and_requires_flag_off_compatibility(tmp_path):
    from factory.governance.policy import review_plan
    job = governed_job(tmp_path, change_class='data_migration')
    job.plan.flag_key = 'decor-migration'
    decision = review_plan(policy(), job.plan, job)
    assert decision.pause and decision.resume_stage == 'build'


def test_issue_flag_reuse_never_creates_a_missing_flag(tmp_path):
    from factory.errors import FactoryError
    from factory.launchdarkly.flags import LaunchDarklyService
    from tests.test_launchdarkly import MemoryLaunchDarkly, boolean_flag
    job = governed_job(tmp_path)
    job.feature.flag_key = 'decor-room-mood'
    job.plan.flag_key = job.feature.flag_key
    memory = MemoryLaunchDarkly(None)
    service = LaunchDarklyService(mode='live', policy=policy(), target_repo=tmp_path,
                                 client=memory, environ={'LAUNCHDARKLY_API_TOKEN': 'test'})
    with pytest.raises(FactoryError, match='not found'):
        service.ensure(job)
    assert [call[0] for call in memory.calls] == ['GET']
    memory.flag = boolean_flag(on=False)
    assert service.ensure(job).reused
    assert not any(call[0] == 'POST' for call in memory.calls)


def test_offline_required_flag_records_rollout_without_provisioning(tmp_path):
    from factory.governance.policy import review_plan
    from factory.launchdarkly.flags import LaunchDarklyService
    from factory.publish.pr import Publisher
    job = governed_job(tmp_path)
    job.plan.flag_key = 'decor-feature'
    rules = policy()
    assert review_plan(rules, job.plan, job).allowed
    job.launchdarkly = LaunchDarklyService(mode='offline', policy=rules, target_repo=tmp_path).ensure(job)
    assert job.launchdarkly.required and not job.launchdarkly.blocks_review()
    draft = Publisher(rules).publish(job, '', tmp_path / 'run')
    body = Path(draft.body_path).read_text()
    for evidence in ['not provisioned/verified', 'demo-errors', 'minimum sample size: 100',
                     '1, 5, 25, 50, 100', 'Rollback on regression: true', 'human starts', 'off in production']:
        assert evidence in body


def test_live_ld_infrastructure_pause_can_resume_without_rebuilding(tmp_path):
    from factory.errors import FactoryError
    from factory.launchdarkly.flags import LaunchDarklyService
    from tests.support import make_factory
    from tests.test_launchdarkly import MemoryLaunchDarkly, boolean_flag

    class Unavailable:
        def request(self, *args, **kwargs):
            raise FactoryError('LaunchDarkly API error (503): unavailable')

    rules = policy()
    service = LaunchDarklyService(mode='live', policy=rules, target_repo=tmp_path,
                                 client=Unavailable(), environ={'LAUNCHDARKLY_API_TOKEN': 'test'})
    factory = make_factory(tmp_path, tmp_path, policy=rules, flag_service=service)
    job = governed_job(tmp_path)
    job.plan.flag_key = 'decor-room-mood'
    job.stage = Stage.ENSURE_FLAG
    factory.advance(job)
    assert job.stage == Stage.AWAIT_HUMAN
    assert job.resume_stage == Stage.ENSURE_FLAG
    assert 'infrastructure' in job.question
    assert job.error is None
    service._client = MemoryLaunchDarkly(boolean_flag(on=False))
    factory.accept_answer(job, 'Infrastructure restored; continue')
    factory.advance(job)
    assert job.stage == Stage.REVIEW
    assert job.launchdarkly.production_off()
    assert not job.builds


def test_issue_class_and_labels_are_preserved_by_intake():
    from factory.intake.load import parse_issue_body
    result = parse_issue_body('Style', '## Change type\nui_cosmetic\n## Rollout\nFlag key: decora-style',
                              labels=['factory:class/ui_cosmetic'])
    assert result.change_class == 'ui_cosmetic'
    assert result.labels == ['factory:class/ui_cosmetic']
    assert result.flag_key == 'decora-style'
    assert not result.user_facing


def test_intake_does_not_drop_multiple_issue_flag_keys(tmp_path):
    from factory.governance.policy import review_plan
    from factory.intake.load import parse_issue_body
    job = governed_job(tmp_path)
    job.feature = parse_issue_body('Example', '## Acceptance\n- works\n## Flag\nkey: decor-one\nkey: decor-two')
    assert review_plan(policy(), job.plan, job).retry_plan


def test_ld_transport_timeout_is_an_infrastructure_error():
    from factory.errors import FactoryError
    from factory.launchdarkly.client import LaunchDarklyClient

    def timeout(*args, **kwargs):
        raise TimeoutError('connection timed out')

    with pytest.raises(FactoryError, match='LaunchDarkly request failed'):
        LaunchDarklyClient('test', urlopen=timeout).request('GET', '/flags/project/example')
