from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from pydantic import ValidationError

from factory.agents.apply import apply_proposal, check_proposal_safety
from factory.agents.prompts import BUILDER_INSTRUCTIONS, PLANNER_INSTRUCTIONS, REVIEWER_INSTRUCTIONS
from factory.context.repo import RepositoryReader
from factory.errors import FactoryError, GovernanceError, IncompleteBuildError, InfrastructureError
from factory.governance.policy import Policy, flag_required, selected_flag_key
from factory.harness.repository_tools import repository_tools
from factory.harness.tokens import charge_provider
from factory.models import BuildProposal, ImplementationPlan, JobState, RepoContext, ReviewReport
from factory.task_queue import current_task
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
                "rollout_rules": {key: rule.model_dump() for key, rule in self.policy.rollout_rules.items()},
                "guarded_rollout": self.policy.guarded_rollout.model_dump(),
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

        task = current_task(job)
        if task is None:
            raise FactoryError("No pending Builder task")
        files = _file_excerpts(job, tools)
        payload = json.dumps(
            {
                "task": task.model_dump(),
                "limits": {"output_tokens": self.policy.builder_max_output_tokens,
                           "turns": self.policy.builder_max_turns},
                "completed_tasks": [{"id": task_id, "summary": job.task_summaries.get(task_id, "")[:600]}
                                    for task_id in [item.id for item in (job.plan.tasks if job.plan else [])
                                                    if item.id in job.completed_task_ids]],
                "files": files,
                "govern_feedback": job.govern_feedback,
                "flag_policy": self.policy.flags.model_dump(),
                "flag_context": {"key": selected_flag_key(job), "default": False,
                                 "required": flag_required(self.policy, job)},
            }
        )
        from agents import ModelSettings

        agent = Agent(
            name="Builder",
            instructions=BUILDER_INSTRUCTIONS,
            output_type=BuildProposal,
            tools=repository_tools(tools.worktree, tools.policy),
            model=self.model,
            model_settings=ModelSettings(max_tokens=self.policy.builder_max_output_tokens),
        )
        try:
            result = _run_agent(agent, payload, max_turns=self.policy.builder_max_turns,
                                timeout=self.policy.timeout_for("build"))
        except GovernanceError:
            raise
        except Exception as exc:
            raise _builder_failure(exc, self.policy.builder_max_output_tokens, self.policy.builder_max_turns) from None
        charge_provider(job, result, payload, "")
        output = result.final_output
        for response in getattr(result, "raw_responses", []):
            if getattr(response, "status", None) == "incomplete":
                raise IncompleteBuildError(_cutoff_feedback(self.policy.builder_max_output_tokens))
        try:
            proposal = output if isinstance(output, BuildProposal) else BuildProposal.model_validate(output)
        except ValueError as exc:
            raise FactoryError(_validation_feedback(exc)) from None
        check_proposal_safety(tools, proposal)
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
            "rollout_rules": {key: rule.model_dump() for key, rule in self.policy.rollout_rules.items()},
            "guarded_rollout": self.policy.guarded_rollout.model_dump(),
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
    """Refresh from disk on EVERY call, including split and correction calls.

    Each text is a contiguous verbatim source region; outlines and line numbers
    stay in metadata so anchors never accidentally include synthetic source.
    """
    task = current_task(job)
    if task is None:
        return []
    reader = RepositoryReader(tools.worktree, tools.policy)
    planned = job.plan.files if job.plan else []
    names = list(dict.fromkeys([*(path for path in planned if path in task.files), *task.files, *planned]))
    clues = " ".join([task.description, *task.acceptance, job.govern_feedback or ""])
    words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", clues))
    feedback_words = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]{2,}", job.govern_feedback or ""))
    excerpts: list[dict[str, str]] = []
    budget = tools.policy.excerpt_budget
    for rel in names:
        if budget <= 0:
            break
        allowance = min(tools.policy.whole_file_limit, budget)
        try:
            path = reader.resolve(rel)
            if not path.exists():
                deleted = rel in job.deleted_files
                excerpts.append({"path": rel, "text": "", "source_kind": "deleted" if deleted else "missing",
                                 "instruction": "Intentionally removed at current checkpoint."
                                 if deleted else "File absent at current checkpoint; use write to create it."})
                continue
            text = reader.source(rel)
        except (FactoryError, OSError):
            excerpts.append({"path": rel, "text": "", "source_kind": "unavailable",
                             "instruction": "File unavailable or denied by repository policy."})
            continue
        if len(text) <= allowance:
            excerpts.append({"path": rel, "text": text, "source_kind": "whole_file", "truncated": "false"})
            budget -= len(text)
            continue
        lines = text.splitlines(keepends=True)
        symbols = [i for i, line in enumerate(lines) if re.match(r"\s*(?:async )?(?:def|class) ", line)]
        outline = "".join(f"{i + 1}: {lines[i]}" for i in symbols)[:allowance]
        allowance -= len(outline)
        budget -= len(outline)
        ranked = sorted(range(len(lines)), key=lambda i: (
            -sum((4 if word in feedback_words else 1) for word in words
                 if re.search(r"\b" + re.escape(word) + r"\b", lines[i])), i))
        # Keep the start/end, then rank source by task and correction symbols.
        centers = [0, len(lines) - 1]
        centers += [i for i in ranked if any(word in lines[i] for word in words)][:8] + symbols
        kept: set[int] = set()
        for center in centers:
            region = set(range(max(0, center - 6), min(len(lines), center + 18))) - kept
            size = sum(len(lines[i]) for i in region)
            # A long neighboring line must not hide a small, relevant anchor.
            for farthest in sorted(region, key=lambda i: abs(i - center), reverse=True):
                if size <= allowance:
                    break
                region.remove(farthest)
                size -= len(lines[farthest])
            kept.update(region)
            allowance -= size
            budget -= size
        # Very long single lines still get a bounded exact prefix.
        if not kept:
            excerpt = text[:allowance]
            budget -= len(excerpt)
            excerpts.append({"path": rel, "text": excerpt, "source_kind": "exact_prefix",
                             "outline": outline, "truncated": "true"})
            continue
        groups: list[list[int]] = []
        for line in sorted(kept):
            if not groups or line != groups[-1][-1] + 1:
                groups.append([])
            groups[-1].append(line)
        for group_index, group in enumerate(groups):
            excerpts.append({"path": rel, "text": "".join(lines[i] for i in group),
                             "source_kind": "exact_section", "start_line": str(group[0] + 1),
                             "end_line": str(group[-1] + 1), "truncated": "true",
                             "outline": outline if group_index == 0 else "",
                             "instruction": "Current source section. Use read_file for omitted ranges."})
    return excerpts


def _cutoff_feedback(tokens: int) -> str:
    return (f"Your response was cut off at {tokens} tokens. Return a smaller part of this task now with "
            "`task_complete=false`; the rest follows in the next call. "
            "Prefer insert/append over rewriting whole files.")


def _validation_feedback(exc: ValueError) -> str:
    if isinstance(exc, ValidationError):
        reasons = [".".join(map(str, item["loc"])) + ": " + item["msg"]
                   for item in exc.errors(include_input=False, include_url=False)]
        return "\n".join([*reasons, "Return one complete BuildProposal."])
    return "Proposal schema invalid. Return one complete BuildProposal."


def _builder_failure(exc: BaseException, tokens: int = 16000, turns: int = 40) -> FactoryError:
    text = str(exc).lower()
    if exc.__class__.__name__ == "MaxTurnsExceeded":
        return IncompleteBuildError(f"You ran out of turns ({turns}). The current file text is included; "
                                    "return the next part of the task directly, using at most 3 reads.")
    cutoff_markers = ("max_output_tokens", "max_tokens", "incomplete", "token limit", "finish_reason='length'")
    if any(marker in text for marker in cutoff_markers):
        return IncompleteBuildError(_cutoff_feedback(tokens))
    if isinstance(exc, ValidationError):
        return FactoryError(_validation_feedback(exc))
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", 0) or 0
    transport_markers = ("timeout", "connection", "connecterror", "networkerror", "readerror", "writeerror",
                         "protocolerror", "internalserver")
    if isinstance(exc, (TimeoutError, ConnectionError)) or status >= 500 or any(
        marker in type(exc).__name__.lower() for marker in transport_markers
    ):
        return InfrastructureError(f"Builder infrastructure error: {type(exc).__name__}")
    return FactoryError(f"Builder call failed: {exc.__class__.__name__}. Return one complete BuildProposal.")



def _run_agent(agent, payload: str, *, max_turns: int = 20, timeout: float | None = None):
    """Keep HTTP connection pools scoped to the activity's event loop."""
    from agents import RunConfig, Runner
    from agents.models.openai_provider import OpenAIProvider
    from openai import AsyncOpenAI

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = AsyncOpenAI(max_retries=0, timeout=timeout) if timeout is not None else AsyncOpenAI()
    try:
        def cancel_call():
            for task in asyncio.all_tasks(loop):
                task.cancel()

        timer = loop.call_later(timeout, cancel_call) if timeout is not None else None
        try:
            return Runner.run_sync(
                agent, payload, max_turns=max_turns,
                run_config=RunConfig(model_provider=OpenAIProvider(openai_client=client)))
        except asyncio.CancelledError:
            raise TimeoutError("Builder call timed out") from None
        finally:
            if timer is not None:
                timer.cancel()
    finally:
        loop.run_until_complete(client.close())
        loop.close()
        asyncio.set_event_loop(None)
