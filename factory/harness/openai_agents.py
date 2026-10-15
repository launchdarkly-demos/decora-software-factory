from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from factory.agents.apply import apply_proposal
from factory.agents.prompts import BUILDER_INSTRUCTIONS, PLANNER_INSTRUCTIONS, REVIEWER_INSTRUCTIONS
from factory.context.repo import RepositoryReader
from factory.errors import FactoryError
from factory.governance.policy import Policy
from factory.harness.repository_tools import repository_tools
from factory.harness.tokens import charge_provider
from factory.models import BuildProposal, ImplementationPlan, JobState, RepoContext, ReviewReport
from factory.tools.layer import ToolLayer


class OpenAIAgentsHarness:
    """OpenAI Agents SDK specialists with structured outputs.

    This is the default harness. ``--agents mock`` selects the offline recipe.
    ``OPENAI_API_KEY`` comes from the environment or the gitignored project ``.env``.
    """

    def __init__(self, model: str | None = None, policy: Policy | None = None) -> None:
        self.policy = policy or Policy()
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
        from agents import Agent

        payload = json.dumps(
            {
                "feature": _feature_for_model(job),
                "context": context.model_dump(),
                "answers": job.human_answers,
                "flag_policy": self.policy.flags.model_dump(),
                "plan_validation_feedback": job.govern_feedback,
                "previous_plan": job.plan.model_dump() if job.plan else None,
            }
        )
        agent = Agent(
            name="Planner",
            instructions=PLANNER_INSTRUCTIONS,
            output_type=ImplementationPlan,
            tools=repository_tools(Path(context.root), self.policy),
            model=self.model,
        )
        try:
            result = _run_agent(agent, payload)
        except Exception as exc:
            raise FactoryError(f"Planner call failed: {exc.__class__.__name__}") from None
        charge_provider(job, result, payload, "")
        output = result.final_output
        if isinstance(output, ImplementationPlan):
            return output
        return ImplementationPlan.model_validate(output)

    def build(self, job: JobState, tools: ToolLayer):
        from agents import Agent

        latest = job.latest_verify()
        files = _file_excerpts(job, tools)
        observed = {item["path"]: [item["text"]] for item in files}
        payload = json.dumps(
            {
                "feature": _feature_for_model(job),
                "plan": job.plan.model_dump() if job.plan else None,
                "files": files,
                "verification": latest.actionable if latest else "",
                "govern_feedback": job.govern_feedback,
                "review_feedback": job.review.model_dump() if job.review else None,
                "human_answers": job.human_answers,
                "flag_policy": self.policy.flags.model_dump(),
            }
        )
        from agents import ModelSettings

        agent = Agent(
            name="Builder",
            instructions=BUILDER_INSTRUCTIONS,
            output_type=BuildProposal,
            tools=repository_tools(tools.worktree, tools.policy, observed=observed),
            model=self.model,
            model_settings=ModelSettings(max_tokens=6000),
        )
        try:
            result = _run_agent(agent, payload)
        except Exception as exc:
            detail = _builder_failure(exc)
            raise FactoryError(detail) from None
        charge_provider(job, result, payload, "")
        output = result.final_output
        proposal = output if isinstance(output, BuildProposal) else BuildProposal.model_validate(output)
        _reject_oversized_edits(proposal)
        for edit in proposal.edits:
            if edit.kind == "replace" and not any(edit.old in text for text in observed.get(edit.path, [])):
                raise FactoryError(f"{edit.path}: replacement anchor was not read in this attempt. "
                                   "Use read_file with its exact line range before proposing the replacement.")
        return apply_proposal(job, tools, proposal)

    def review(self, job: JobState, tools: ToolLayer):
        from agents import Agent
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
            "flag_policy": self.policy.flags.model_dump(),
        })
        agent = Agent(
            name="Reviewer",
            instructions=REVIEWER_INSTRUCTIONS,
            output_type=ReviewReport,
            tools=repository_tools(tools.worktree, tools.policy) if isinstance(tools, ToolLayer) else [],
            model=self.model,
        )
        try:
            result = _run_agent(agent, payload)
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
    reader = RepositoryReader(tools.worktree, tools.policy)
    excerpts: list[dict[str, str]] = []
    budget = 40_000
    for rel in dict.fromkeys(names):
        if budget <= 0 or len(excerpts) >= 12:
            break
        try:
            if rel not in reader.paths:
                continue
            text = reader.resolve(rel).read_text(errors="replace")
        except FactoryError:
            continue
        excerpt = text[:min(budget, 12000)]
        excerpts.append({"path": rel, "text": excerpt,
                         "source_kind": "exact_prefix", "truncated": str(len(excerpt) < len(text)).lower(),
                         "instruction": "Use read_file for source beyond this prefix; metadata is not code."})
        budget -= len(excerpt)
    return excerpts


def _reject_oversized_edits(proposal: BuildProposal) -> None:
    for edit in proposal.edits:
        if edit.kind == "replace" and (not edit.old or len(edit.old) > 16000 or len(edit.new) > 16000):
            raise FactoryError(f"{edit.path}: use a nonempty, focused replace anchor and read exact source first.")


def _builder_failure(exc: BaseException) -> str:
    text = str(exc)
    if "max_output_tokens" in text or "incomplete" in text:
        return "Builder output hit the token limit. Use a short replace snippet, not the whole file."
    return f"Builder call failed: {exc.__class__.__name__}"



def _run_agent(agent, payload: str):
    """Keep HTTP connection pools scoped to the activity's event loop."""
    from agents import RunConfig, Runner
    from agents.models.openai_provider import OpenAIProvider
    from openai import AsyncOpenAI

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = AsyncOpenAI()
    try:
        return Runner.run_sync(
            agent, payload, max_turns=20, run_config=RunConfig(model_provider=OpenAIProvider(openai_client=client))
        )
    finally:
        loop.run_until_complete(client.close())
        loop.close()
        asyncio.set_event_loop(None)
