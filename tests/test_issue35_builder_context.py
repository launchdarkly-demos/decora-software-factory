"""Regression coverage for #35: Builder request_context and concrete plan items."""
from __future__ import annotations

import json
from pathlib import Path

from factory.agents.prompts import BUILDER_INSTRUCTIONS, PLANNER_INSTRUCTIONS, REVIEWER_INSTRUCTIONS
from factory.harness.openai_agents import (
    REQUEST_CONTEXT_CHAR_CAP,
    builder_payload,
    request_context_for_builder,
)
from factory.models import Stage
from tests.support import ROOT, bare_job

FIXTURE = ROOT / "tests" / "fixtures" / "org_issue3_r2_job.json"
SUITES = (
    "test_api_project.py",
    "test_board.py",
    "test_events.py",
    "test_flinch_gate.py",
    "test_request_errors.py",
)


def _job_from_r2_fixture(tmp_path: Path):
    """Load run-2 feature+plan; clear completed ids so the task is current for Builder."""
    data = json.loads(FIXTURE.read_text())
    return bare_job(
        tmp_path,
        id=data["id"],
        stage=Stage.BUILD,
        feature=data["feature"],
        plan=data["plan"],
        completed_task_ids=[],
        task_summaries={},
    )


def test_replay_org_issue3_r2_builder_payload_has_request_context(tmp_path: Path) -> None:
    job = _job_from_r2_fixture(tmp_path)
    payload = builder_payload(job, files=[])
    context = payload["request_context"]
    steps_text = "\n".join(context["plan"]["steps"])
    hints_text = "\n".join(context["feature"]["hints"])
    assert all(name in steps_text for name in SUITES)
    assert "Problem:" in hints_text
    assert all(name in hints_text for name in SUITES)
    assert payload["task"]["id"] == "update_readme_tests_section"
    assert "plan" not in payload  # full plan stays out of the top-level Builder payload


def test_request_context_cap_keeps_steps_and_marker(tmp_path: Path) -> None:
    job = _job_from_r2_fixture(tmp_path)
    job.feature.summary = "S" * 4000
    job.feature.hints = ["Problem: " + ("hint-pad " * 800), *job.feature.hints]
    job.plan.assumptions = ["A" * 3000, *job.plan.assumptions]
    job.plan.summary = "P" * 3000
    context = request_context_for_builder(job, cap=REQUEST_CONTEXT_CHAR_CAP)
    serialized = json.dumps(context, ensure_ascii=False)
    assert len(serialized) <= REQUEST_CONTEXT_CHAR_CAP
    assert context["plan"]["steps"]
    assert "[truncated]" in serialized
    steps_text = "\n".join(context["plan"]["steps"])
    # Cap may truncate long steps, but the steps list must remain.
    assert isinstance(context["plan"]["steps"], list)
    assert len(context["plan"]["steps"]) >= 1
    # Prefer that suite names survive when possible; if truncated, marker proves the cap ran.
    assert any(name in steps_text for name in SUITES) or "[truncated]" in steps_text


def test_b1_b2_b3_instruction_phrases() -> None:
    assert "request_context" in BUILDER_INSTRUCTIONS
    assert "CURRENT task" in BUILDER_INSTRUCTIONS
    assert "expand scope beyond the task's files" in BUILDER_INSTRUCTIONS
    assert "copy them verbatim into the acceptance" in PLANNER_INSTRUCTIONS
    assert 'Do not summarize them as "all X"' in PLANNER_INSTRUCTIONS
    assert "plan.steps" in REVIEWER_INSTRUCTIONS
    assert "feature.hints" in REVIEWER_INSTRUCTIONS
    assert "request changes" in REVIEWER_INSTRUCTIONS
    assert "cite the missing item" in REVIEWER_INSTRUCTIONS
