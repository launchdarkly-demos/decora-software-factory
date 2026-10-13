from __future__ import annotations

import json
import os

from factory.agents.apply import apply_proposal
from factory.agents.prompts import BUILDER_INSTRUCTIONS, PLANNER_INSTRUCTIONS, REVIEWER_INSTRUCTIONS
from factory.agents.reviewer import review_deterministically
from factory.errors import FactoryError
from factory.harness.tokens import charge_provider
from factory.models import BuildProposal, ImplementationPlan, JobState, RepoContext
from factory.tools.layer import ToolLayer


class OpenAIAgentsHarness:
    """OpenAI Agents SDK specialists with structured outputs.

    The demo does not use this. The account behind OPENAI_API_KEY has no quota.
    Select it with --agents openai after installing the openai extra.
    """

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.environ.get("FACTORY_OPENAI_MODEL", "gpt-4.1-mini")
        try:
            import agents  # noqa: F401
        except ImportError as exc:
            raise FactoryError(
                "OpenAI agent mode needs the openai-agents package. "
                "Install with: pip install 'software-factory[openai]'. "
                "The demo uses --agents mock."
            ) from exc

    def plan(self, job: JobState, context: RepoContext) -> ImplementationPlan:
        from agents import Agent, Runner

        payload = json.dumps(
            {
                "feature": job.feature.model_dump(),
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
        result = Runner.run_sync(agent, payload)
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
                "feature": job.feature.model_dump(),
                "plan": job.plan.model_dump() if job.plan else None,
                "verification": latest.actionable if latest else "",
                "govern_feedback": job.govern_feedback,
            }
        )
        agent = Agent(
            name="Builder",
            instructions=BUILDER_INSTRUCTIONS,
            output_type=BuildProposal,
            model=self.model,
        )
        result = Runner.run_sync(agent, payload)
        charge_provider(job, result, payload, "")
        output = result.final_output
        proposal = output if isinstance(output, BuildProposal) else BuildProposal.model_validate(output)
        return apply_proposal(job, tools, proposal)

    def review(self, job: JobState, tools: ToolLayer):
        from agents import Agent, Runner
        from pydantic import BaseModel, Field

        report = review_deterministically(job, tools)

        class Narrative(BaseModel):
            summary: str
            extra_risks: list[str] = Field(default_factory=list)

        try:
            agent = Agent(
                name="Reviewer",
                instructions=REVIEWER_INSTRUCTIONS,
                output_type=Narrative,
                model=self.model,
            )
            payload = report.model_dump_json()
            result = Runner.run_sync(agent, payload)
            charge_provider(job, result, payload, report.summary)
            narrative = result.final_output
            if not isinstance(narrative, Narrative):
                narrative = Narrative.model_validate(narrative)
            if report.verdict == "approve" and narrative.summary:
                report.summary = narrative.summary
            report.risks.extend(narrative.extra_risks)
        except Exception as exc:
            report.risks.append(f"Prose review skipped: {exc.__class__.__name__}")
        return report
