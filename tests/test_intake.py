from pathlib import Path

import pytest

from factory.agents.recipes import CAPABILITIES_RETURN, FLAGS_NEEDLE
from factory.context.repo import analyze_repo
from factory.intake.load import load_feature, parse_issue_body
from tests.support import ROOT, feature

DECORA = Path("/Users/alexisroberson/Documents/Claude_Code_Projects/langgraph-projects/decor-agent")


def test_json_spec_loads_the_room_mood_request() -> None:
    feature = load_feature(ROOT / "samples" / "decor-room-mood.json")
    assert feature.flag_key == "decor-room-mood"
    assert feature.flag_default is False
    assert feature.user_facing is True
    assert feature.demo_fail_first_build is True
    assert len(feature.acceptance) == 3


def test_issue_body_parser_reads_flag_and_acceptance() -> None:
    body = """
## Summary
Studio should show a mood line.

## Acceptance
- Omits room_mood when off
- Includes calm linen when on

## Flag
key: decor-room-mood
default: off

## User facing
yes
"""
    feature = parse_issue_body("Room mood", body, url="https://example.test/issues/4")
    assert feature.source == "github_issue"
    assert feature.flag_key == "decor-room-mood"
    assert feature.flag_default is False
    assert feature.acceptance == ["Omits room_mood when off", "Includes calm linen when on"]
    assert feature.issue_url == "https://example.test/issues/4"


def test_ambiguous_sample_has_no_acceptance() -> None:
    feature = load_feature(ROOT / "samples" / "ambiguous.json")
    assert feature.acceptance == []
    assert feature.flag_key is None


def test_context_ignores_nested_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "decor"
    repo.mkdir()
    (repo / "server.py").write_text('"/api/capabilities"\n')
    nested = repo / ".claude" / "worktrees" / "other"
    nested.mkdir(parents=True)
    (nested / "server.py").write_text("capabilities noise\n")
    (repo / "app").mkdir()
    (repo / "app" / "flags.py").write_text("BOARD_INTAKE_FLAG\n")
    context = analyze_repo(repo, feature(hints=["capabilities"], summary="capabilities flag"))
    assert context.has_capabilities_route is True
    assert all(".claude" not in path and "worktrees" not in path for path in context.top_files)
    assert "server.py" in context.top_files


def test_decora_checkout_has_the_recipe_anchor() -> None:
    server = DECORA / "server.py"
    if not server.exists():
        pytest.skip("Decora checkout is not on this machine")
    assert CAPABILITIES_RETURN in server.read_text()
    assert FLAGS_NEEDLE in (DECORA / "app" / "flags.py").read_text()
