from __future__ import annotations

from pathlib import Path

from factory.agents.apply import apply_proposal
from factory.agents.recipes import room_mood_proposal
from factory.agents.reviewer import review_deterministically
from factory.harness.tokens import charge
from factory.models import ImplementationPlan, JobState, RepoContext
from factory.tools.layer import ToolLayer


class MockHarness:
    """Offline specialists. Same artifacts as the SDK path, no API call."""

    def plan(self, job: JobState, context: RepoContext) -> ImplementationPlan:
        feature = job.feature
        if _concrete(job):
            plan = ImplementationPlan(
                summary=feature.summary,
                files=["app/flags.py", "app/room_mood.py", "server.py", "test_room_mood_flag.py"],
                steps=[
                    "Add ROOM_MOOD_FLAG beside the existing flag constants.",
                    "Add app/room_mood.py with FLAG_DEFAULT False.",
                    "Return room_mood from GET /api/capabilities only when the flag is on.",
                    "Test that the code default stays off.",
                ],
                flag_key=feature.flag_key or "decor-room-mood",
                flag_default=False,
                test_targets=["test_room_mood_flag.py"],
                risk="low",
                ambiguous=False,
                questions=[],
                recipe="flagged_capability_field",
                context_files=list(context.top_files),
            )
            # Match the recipe's API test requirement to the target's server.
            if "FastAPI(" in (Path(context.root) / "server.py").read_text():
                plan.files.append("test_room_mood_api.py")
        else:
            plan = ImplementationPlan(
                summary=feature.summary,
                files=[],
                steps=[],
                flag_key=feature.flag_key,
                flag_default=False,
                test_targets=[],
                risk="high",
                ambiguous=True,
                questions=_questions(feature),
                recipe="unsupported",
                context_files=list(context.top_files),
            )
        charge(job, feature.model_dump_json() + context.model_dump_json(), plan.model_dump_json())
        return plan

    def build(self, job: JobState, tools: ToolLayer):
        proposal = room_mood_proposal(job, tools)
        for edit in proposal.edits:
            if edit.kind == "write" and tools._resolve(edit.path).exists():
                edit.kind = "replace"
                edit.old = tools.read(edit.path)
                edit.new = edit.content
                edit.content = ""
        result = apply_proposal(job, tools, proposal)
        charge(job, proposal.model_dump_json(), result.model_dump_json())
        return result

    def review(self, job: JobState, tools: ToolLayer):
        report = review_deterministically(job, tools)
        charge(job, report.model_dump_json(), report.summary)
        return report


def _concrete(job: JobState) -> bool:
    feature = job.feature
    if feature.flag_key == "decor-room-mood" and feature.acceptance and feature.user_facing:
        return True
    if not job.human_answers:
        return False
    text = job.human_answers[-1].lower()
    return "decor-room-mood" in text and "capabilities" in text


def _questions(feature) -> list[str]:
    questions = []
    if not feature.acceptance:
        questions.append("The request has no acceptance checks. What should be true when this is done?")
    if feature.user_facing and not feature.flag_key:
        questions.append("This is user-facing and names no LaunchDarkly flag. What flag key should default off?")
    if not questions:
        questions.append(
            "Mock mode can implement decor-room-mood on /api/capabilities. "
            "This request is outside that recipe."
        )
    return questions
