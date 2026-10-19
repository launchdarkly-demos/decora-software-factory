"""Regression coverage for #34: unrecognized issue sections and footer leaks."""
from __future__ import annotations

import json
from pathlib import Path

from factory.agents.prompts import PLANNER_INSTRUCTIONS, REVIEWER_INSTRUCTIONS
from factory.harness.openai_agents import _feature_for_model
from factory.intake.load import parse_issue_body
from tests.support import ROOT, bare_job

FIXTURE = ROOT / "tests" / "fixtures" / "org_issue3_r1_body.md"
SUITES = (
    "test_api_project.py",
    "test_board.py",
    "test_events.py",
    "test_flinch_gate.py",
    "test_request_errors.py",
)


def test_replay_org_issue3_r1_body() -> None:
    feature = parse_issue_body("README Tests section", FIXTURE.read_text())
    problem_hints = [hint for hint in feature.hints if hint.startswith("Problem:")]
    assert problem_hints
    problem = problem_hints[0]
    assert all(name in problem for name in SUITES)
    assert feature.change_class == "docs_only"
    assert "---" not in feature.change_type
    assert "factory test issue" not in feature.change_type
    assert feature.change_type == "docs_only, low risk."
    footer_hints = [hint for hint in feature.hints if hint.startswith("Footer:")]
    assert footer_hints
    assert "factory test issue" in footer_hints[0]
    assert len(feature.acceptance) == 4


def test_unknown_sections_preserved_in_order_known_unaffected() -> None:
    body = """\
Leading note about the bug.

## Goal
Ship the fix.

## Background
Prior art in server.py.

## Acceptance criteria
- [ ] One criterion

## Notes
Follow-up later.

## Context
See board.py for layout.

## Change type
docs_only
"""
    feature = parse_issue_body("Ordered sections", body)
    assert feature.summary == "Ship the fix."
    assert feature.acceptance == ["One criterion"]
    assert feature.change_class == "docs_only"
    assert feature.change_type == "docs_only"
    assert feature.hints[0] == "Leading note about the bug."
    assert feature.hints[1].startswith("Background:")
    assert "server.py" in feature.hints[1]
    assert feature.hints[2].startswith("Notes:")
    assert feature.hints[3] == "See board.py for layout."
    assert sum(1 for hint in feature.hints if "See board.py" in hint) == 1
    assert not any(hint.startswith("Context:") for hint in feature.hints)


def test_horizontal_rule_inside_fence_is_not_a_section_break() -> None:
    body = """\
## Goal
Document the separator.

## Notes
Example:

```markdown
---
title: kept
---
```

Still in Notes after the fence.

## Change type
docs_only, low risk.
"""
    feature = parse_issue_body("Fenced HR", body)
    notes = next(hint for hint in feature.hints if hint.startswith("Notes:"))
    assert "title: kept" in notes
    assert "Still in Notes after the fence." in notes
    assert feature.change_type == "docs_only, low risk."
    assert feature.change_class == "docs_only"
    assert not any(hint.startswith("Footer:") for hint in feature.hints)


def test_planner_and_reviewer_payloads_include_problem_text() -> None:
    feature = parse_issue_body("README Tests section", FIXTURE.read_text())
    job = bare_job(Path("."), feature=feature)
    feature_payload = _feature_for_model(job)
    planner_input = json.dumps({"feature": feature_payload}, ensure_ascii=False)
    reviewer_input = json.dumps({"feature": feature_payload}, ensure_ascii=False)
    for payload in (planner_input, reviewer_input):
        assert "Problem:" in payload
        assert all(name in payload for name in SUITES)
    assert "feature.hints" in PLANNER_INSTRUCTIONS
    assert "feature.hints" in REVIEWER_INSTRUCTIONS
