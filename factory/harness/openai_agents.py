from __future__ import annotations

import json
import os

from factory.agents.apply import apply_proposal
from factory.agents.prompts import BUILDER_INSTRUCTIONS, PLANNER_INSTRUCTIONS, REVIEWER_INSTRUCTIONS
from factory.errors import FactoryError
from factory.harness.tokens import charge_provider
from factory.models import BuildProposal, ImplementationPlan, JobState, RepoContext, ReviewReport
from factory.tools.layer import ToolLayer


class OpenAIAgentsHarness:
    """OpenAI Agents SDK specialists with structured outputs.

    This is the default harness. ``--agents mock`` selects the offline recipe.
    ``OPENAI_API_KEY`` comes from the environment or the gitignored project ``.env``.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("FACTORY_OPENAI_MODEL", "gpt-4.1")
        try:
            import agents  # noqa: F401
        except ImportError as exc:
            raise FactoryError(
                "OpenAI agent mode needs the openai-agents package. "
                "Install with: pip install 'software-factory[openai]'. "
                "Pass --agents mock to run without it."
            ) from exc

    def plan(self, job: JobState, context: RepoContext) -> ImplementationPlan:
        from agents import Agent, Runner

        payload = json.dumps(
            {
                "feature": _feature_for_model(job),
                "context": context.model_dump(),
                "answers": job.human_answers,
            }
        )
        agent = Agent(
            name="Planner",
            instructions=PLANNER_INSTRUCTIONS,
            output_type=ImplementationPlan,
            model=self.model,
        )
        try:
            result = Runner.run_sync(agent, payload)
        except Exception as exc:
            raise FactoryError(f"Planner call failed: {exc.__class__.__name__}") from None
        charge_provider(job, result, payload, "")
        output = result.final_output
        if isinstance(output, ImplementationPlan):
            return output
        return ImplementationPlan.model_validate(output)

    def build(self, job: JobState, tools: ToolLayer):
        from agents import Agent, Runner

        latest = job.latest_verify()
        payload = json.dumps(
            {
                "feature": _feature_for_model(job),
                "plan": job.plan.model_dump() if job.plan else None,
                "files": _file_excerpts(job, tools),
                "verification": latest.actionable if latest else "",
                "govern_feedback": job.govern_feedback,
            }
        )
        from agents import ModelSettings

        agent = Agent(
            name="Builder",
            instructions=BUILDER_INSTRUCTIONS,
            output_type=BuildProposal,
            model=self.model,
            model_settings=ModelSettings(max_tokens=6000),
        )
        try:
            result = Runner.run_sync(agent, payload)
        except Exception as exc:
            detail = _builder_failure(exc)
            raise FactoryError(detail) from None
        charge_provider(job, result, payload, "")
        output = result.final_output
        proposal = output if isinstance(output, BuildProposal) else BuildProposal.model_validate(output)
        _reject_oversized_edits(proposal)
        return apply_proposal(job, tools, proposal)

    def review(self, job: JobState, tools: ToolLayer):
        from agents import Agent, Runner
        if job.worktree is None:
            raise FactoryError("review requires a worktree")
        latest = job.latest_verify()
        payload = json.dumps({
            "feature": _feature_for_model(job),
            "plan": job.plan.model_dump() if job.plan else None,
            "diff": tools.diff(job.worktree.base_sha),
            "changed_files": tools.changed_files(job.worktree.base_sha),
            "verification": latest.model_dump() if latest else None,
            "previous_failed_attempts": sum(not run.passed for run in job.verify_runs[:-1]),
            "launchdarkly": job.launchdarkly.model_dump() if job.launchdarkly else None,
        })
        agent = Agent(
            name="Reviewer",
            instructions=REVIEWER_INSTRUCTIONS,
            output_type=ReviewReport,
            model=self.model,
        )
        try:
            result = Runner.run_sync(agent, payload)
        except Exception as exc:
            raise FactoryError(f"Reviewer call failed: {exc.__class__.__name__}") from None
        charge_provider(job, result, payload, "")
        output = result.final_output
        return output if isinstance(output, ReviewReport) else ReviewReport.model_validate(output)


def _feature_for_model(job: JobState) -> dict[str, object]:
    data = job.feature.model_dump()
    data.pop("demo_fail_first_build", None)
    return data


def _file_excerpts(job: JobState, tools: ToolLayer) -> list[dict[str, str]]:
    names: list[str] = []
    if job.plan is not None:
        names.extend(job.plan.files)
        names.extend(job.plan.context_files)
    for extra in ("app/flags.py", "server.py"):
        if extra not in names:
            names.append(extra)
    excerpts: list[dict[str, str]] = []
    seen: set[str] = set()
    for rel in names:
        if rel in seen or len(excerpts) >= 6:
            continue
        seen.add(rel)
        try:
            text = tools.read(rel)
        except FactoryError:
            continue
        excerpts.append({"path": rel, "text": _focused_excerpt(rel, text)})
    return excerpts


def _focused_excerpt(rel: str, text: str) -> str:
    needles = _WINDOWS.get(rel, ())
    if needles:
        window = _window(text, needles)
        if window:
            return window
    if len(text) > 2500:
        return text[:2500] + "\n# ... truncated\n"
    return text


def _window(text: str, needles: tuple[str, ...]) -> str:
    lines = text.splitlines()
    keep: set[int] = set()
    for index, line in enumerate(lines):
        if any(needle in line for needle in needles):
            start = max(0, index - 8)
            end = min(len(lines), index + 18)
            keep.update(range(start, end))
    if not keep:
        return ""
    chunks: list[str] = []
    previous = -2
    for index in sorted(keep):
        if index != previous + 1 and chunks:
            chunks.append("# ...")
        chunks.append(lines[index])
        previous = index
    return "\n".join(chunks)


def _reject_oversized_edits(proposal: BuildProposal) -> None:
    for edit in proposal.edits:
        if edit.kind == "write" and edit.path in {"server.py", "app/flags.py"}:
            raise FactoryError(f"Do not rewrite {edit.path}. Use a short replace around the existing lines.")
        if edit.kind == "replace" and (len(edit.old) > 1200 or len(edit.new) > 1200):
            raise FactoryError(f"{edit.path} replace is too large. Keep the anchor to a few lines.")


def _builder_failure(exc: BaseException) -> str:
    text = str(exc)
    if "max_output_tokens" in text or "incomplete" in text:
        return "Builder output hit the token limit. Use a short replace snippet, not the whole file."
    return f"Builder call failed: {exc.__class__.__name__}"


_WINDOWS = {
    "app/flags.py": ("BOARD_INTAKE_FLAG", "def get_flag"),
    "server.py": ("def capabilities", "from app.flags import", "sample_board"),
}
