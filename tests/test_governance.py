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
