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
