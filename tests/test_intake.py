from pathlib import Path

from factory.agents.recipes import CAPABILITIES_RETURN, FLAGS_NEEDLE
from factory.context.repo import analyze_repo
from factory.intake.load import load_feature, parse_issue_body
from tests.support import ROOT, feature, init_repo


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


def test_mock_fixture_has_the_recipe_anchor(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    server = repo / "server.py"
    assert CAPABILITIES_RETURN in server.read_text()
    assert FLAGS_NEEDLE in (repo / "app" / "flags.py").read_text()


def test_decora_request_preserves_scope_verification_and_optional_rollout():
    body = '''## Goal
Fix stale room names after reload.
## Change type
bug fix
## Acceptance criteria
- [ ] The updated name survives reload.
- [x] Existing board selections stay intact.
- [ ]
## Scope and constraints
Do not change the project schema.
## Verification
Reproduce a rename and reload in the browser; run the project API tests.
## Rollout
user-facing: yes
feature flag: no
key:
default: off
Reason: restore existing behavior.
## Context
See server.py and the room title component.
<!-- ## Flag
key: not-a-real-flag
-->
'''
    result = parse_issue_body('Room rename', body)
    assert result.summary == 'Fix stale room names after reload.'
    assert result.change_type == 'bug fix'
    assert result.acceptance == ['The updated name survives reload.', 'Existing board selections stay intact.']
    assert result.scope == 'Do not change the project schema.'
    assert 'browser' in result.verification
    assert 'feature flag: no' in result.rollout
    assert result.user_facing is True
    assert result.flag_key is None
    assert 'server.py' in result.hints[0]
    # A requested rollout exception must not silently disable existing policy.
    from factory.governance.policy import review_plan
    from tests.support import bare_job, low_plan, policy
    plan = low_plan().model_copy(update={'flag_key': None})
    job = bare_job(ROOT, feature=result)
    assert review_plan(policy(), plan, job).pause


def test_unfilled_template_cannot_invent_acceptance_or_a_flag():
    template = (ROOT / '.github/ISSUE_TEMPLATE/factory-task.md').read_text().split('---', 2)[2]
    result = parse_issue_body('My idea', template)
    assert result.acceptance == []
    assert result.flag_key is None
    assert result.user_facing is True  # unsure never bypasses user-facing governance
    assert result.change_type == 'unspecified'
    assert 'room title can be changed' not in result.summary


def test_rollout_reads_explicit_internal_change_and_flag_key():
    body = '''## Goal
Expose a capability.
## Rollout
user-facing: no
feature flag: yes
key: decor-example
default: off
'''
    result = parse_issue_body('Capability', body)
    assert result.user_facing is False
    assert result.flag_key == 'decor-example'
    assert result.flag_default is False
