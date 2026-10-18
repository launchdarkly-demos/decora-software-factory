from __future__ import annotations

import re
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from factory.clock import Clock, SystemClock
from factory.context.repo import analyze_repo
from factory.errors import FactoryError, GovernanceError, IncompleteBuildError, InfrastructureError, SafetyError
from factory.execution.portable import PortableWorkspace
from factory.execution.worktree import WorktreeManager
from factory.governance.policy import Policy, review_plan, review_ship
from factory.harness.base import AgentHarness
from factory.launchdarkly.flags import LaunchDarklyService
from factory.models import JobEvent, JobState, Stage
from factory.observability.summary import build_summary
from factory.observability.trace import JsonLog, Tracer
from factory.orchestrator.graph import COMPONENTS, assert_legal
from factory.orchestrator.store import JobStore
from factory.publish.pr import Publisher
from factory.task_queue import current_task, missing_file_feedback, reopen_task, verification_feedback
from factory.tools.layer import ToolLayer
from factory.verification.gate import VerificationGate


@dataclass
class Transition:
    stage: Stage
    detail: str
    error: str | None = None
    resume_stage: Stage | None = None


class Factory:
    """Drives one job from intake to a draft pull request, or to a human pause."""

    def __init__(
        self,
        *,
        target_repo: Path,
        home: Path,
        policy: Policy,
        harness: AgentHarness,
        execution: WorktreeManager | None = None,
        verifier: VerificationGate | None = None,
        clock: Clock | None = None,
        keep_worktree: bool = False,
        push: bool = False,
        ld_mode: str = "offline",
        flag_service: LaunchDarklyService | None = None,
        publisher_runner=None,
        id_factory: Callable[[], str] | None = None,
        on_event: Callable[[JobEvent], None] | None = None,
    ) -> None:
        self.target_repo = target_repo.resolve()
        self.home = home
        self.policy = policy
        self.harness = harness
        self.execution = execution or WorktreeManager(home / "worktrees")
        self.verifier = verifier or VerificationGate(self.target_repo, home)
        self.clock = clock or SystemClock()
        self.keep_worktree = keep_worktree
        self.ld_mode = ld_mode
        self.flags = flag_service or LaunchDarklyService(
            mode=ld_mode,
            policy=policy,
            target_repo=self.target_repo,
        )
        self.publisher = Publisher(policy, push=push, runner=publisher_runner)
        self.id_factory = id_factory or (lambda: "job-" + uuid.uuid4().hex[:8])
        self.on_event = on_event
        self.store = JobStore(home / "factory.db")
        self.log: JsonLog | None = None
        self.tracer: Tracer | None = None
        self._handlers = {
            Stage.INTAKE: self._intake,
            Stage.CONTEXT: self._context,
            Stage.PLAN: self._plan,
            Stage.GOVERN_PLAN: self._govern_plan,
            Stage.BUILD: self._build,
            Stage.VERIFY: self._verify,
            Stage.ENSURE_FLAG: self._ensure_flag,
            Stage.REVIEW: self._review,
            Stage.GOVERN_SHIP: self._govern_ship,
            Stage.DRAFT_PR: self._draft_pr,
        }

    def run(self, feature) -> JobState:
        now = self.clock.now()
        job = JobState(
            id=self.id_factory(),
            stage=Stage.INTAKE,
            feature=feature,
            created_at=now,
            updated_at=now,
        )
        self._open_logs(job)
        self.store.save(job)
        self._drive(job)
        return job

    def resume(self, job_id: str, answer: str) -> JobState:
        job = self.store.load(job_id)
        if job.stage != Stage.AWAIT_HUMAN:
            raise FactoryError(f"{job_id} is in {job.stage.value}, not waiting for a human")
        if not answer.strip():
            raise FactoryError("an answer is required to resume")
        self.accept_answer(job, answer)
        self._drive(job)
        return job

    def accept_answer(self, job: JobState, answer: str) -> None:
        if job.stage != Stage.AWAIT_HUMAN or not answer.strip():
            raise FactoryError("a paused job and a nonempty answer are required")
        self._open_logs(job)
        job.human_answers.append(answer.strip())
        dest = job.resume_stage or Stage.PLAN
        if dest == Stage.BUILD and answer.strip().lower() not in {"approve", "approved", "ship it", "continue"}:
            dest = Stage.PLAN
        assert_legal(Stage.AWAIT_HUMAN, dest)
        job.paused = False
        job.question = None
        job.resume_stage = None
        job.stage = dest
        job.updated_at = self.clock.now()
        self._emit(job, "governance", f"human answered; resuming at {dest.value}")
        self.store.save(job)

    def status(self, job_id: str) -> JobState:
        return self.store.load(job_id)

    def _drive(self, job: JobState) -> None:
        try:
            while job.stage not in (Stage.DONE, Stage.FAILED, Stage.AWAIT_HUMAN):
                self.advance(job)
        finally:
            self._finish(job)

    def advance(self, job: JobState) -> None:
        """Execute exactly one stage; usable by the local loop or a Temporal activity."""
        self._open_logs(job)
        current = job.stage
        handler = self._handlers[current]
        assert self.tracer is not None
        with self.tracer.span(f"stage.{current.value}", COMPONENTS[current], job_id=job.id):
            transition = self._bounded(job, handler)
        self._apply(job, current, transition)
        if current == Stage.BUILD:
            job.completed_builder_calls = job.builder_calls
        self.store.save(job)

    def finish(self, job: JobState) -> None:
        self._open_logs(job)
        self._finish(job)

    def prepare_build(self, job: JobState) -> None:
        if job.worktree is None or job.worktree.removed:
            if isinstance(self.execution, PortableWorkspace):
                job.worktree = self.execution.create(self.target_repo, job.id, job.source_sha)
            else:
                job.worktree = self.execution.create(self.target_repo, job.id)
            self.store.save(job)

    def _bounded(self, job: JobState, handler: Callable[[JobState], Transition]) -> Transition:
        start = self.clock.now()
        try:
            transition = handler(job)
        except FactoryError as exc:
            transition = Transition(stage=Stage.FAILED, detail=str(exc), error=str(exc))
        limit = self.policy.timeout_for(job.stage.value)
        if job.stage != Stage.BUILD and self.clock.now() - start > limit:
            return Transition(
                stage=Stage.FAILED,
                detail=f"{job.stage.value} timed out",
                error=f"{job.stage.value} timed out",
            )
        return transition

    def _apply(self, job: JobState, current: Stage, transition: Transition) -> None:
        assert_legal(current, transition.stage)
        if transition.stage == Stage.FAILED:
            job.error = transition.error or transition.detail
        if transition.stage == Stage.AWAIT_HUMAN:
            job.paused = True
            job.question = transition.detail
            job.resume_stage = transition.resume_stage
        else:
            job.paused = False
        job.stage = transition.stage
        job.updated_at = self.clock.now()
        self._emit(
            job,
            COMPONENTS.get(transition.stage, "orchestrator"),
            transition.detail,
            **{"from": current.value},
        )

    def _intake(self, job: JobState) -> Transition:
        feature = job.feature
        if feature.source == "github_issue":
            from factory.intake.load import check_issue_target

            check_issue_target(self.target_repo, feature.issue_url)
        if not feature.id.strip() or not feature.title.strip() or not feature.summary.strip():
            return Transition(
                stage=Stage.FAILED,
                detail="feature id, title, and summary are required",
                error="feature id, title, and summary are required",
            )
        return Transition(stage=Stage.CONTEXT, detail=f"accepted {feature.id} from {feature.source}")

    def _context(self, job: JobState) -> Transition:
        context_root = self.target_repo
        if isinstance(self.execution, PortableWorkspace):
            job.source_sha = job.source_sha or self.execution.pin(self.target_repo)
            self.prepare_build(job)
            assert job.worktree is not None
            context_root = Path(job.worktree.path)
        context = analyze_repo(context_root, job.feature, self.policy)
        job.context = context
        self._write_model(job, "context.json", context)
        return Transition(
            stage=Stage.PLAN,
            detail=f"{len(context.top_files)} files considered",
        )

    def _plan(self, job: JobState) -> Transition:
        if job.context is None:
            return Transition(stage=Stage.FAILED, detail="no context", error="no context")
        plan = self.harness.plan(job, job.context)
        job.plan = plan
        job.current_task_id = None
        job.completed_task_ids.clear()
        job.task_summaries.clear()
        self._write_model(job, "implementation_plan.json", plan)
        return Transition(stage=Stage.GOVERN_PLAN, detail=f"recipe {plan.recipe} risk {plan.risk}")

    def _govern_plan(self, job: JobState) -> Transition:
        if job.plan is None:
            return Transition(stage=Stage.FAILED, detail="no plan", error="no plan")
        decision = review_plan(self.policy, job.plan, job)
        self._write_model(job, "implementation_plan.json", job.plan)
        if decision.retry_plan:
            used = job.retries.get("plan_repair", 0)
            job.govern_feedback = decision.reason
            if used < self.policy.max_retries.get("plan_repair", 2):
                job.retries["plan_repair"] = used + 1
                return Transition(stage=Stage.PLAN, detail="Repairing plan: " + decision.reason)
            return Transition(stage=Stage.AWAIT_HUMAN,
                              detail="Automatic plan repair exhausted. " + decision.reason,
                              resume_stage=Stage.PLAN)
        job.govern_feedback = None
        if decision.pause:
            resume = Stage(decision.resume_stage) if decision.resume_stage else Stage.PLAN
            return Transition(stage=Stage.AWAIT_HUMAN, detail=decision.reason, resume_stage=resume)
        if not decision.allowed:
            return Transition(stage=Stage.FAILED, detail=decision.reason, error=decision.reason)
        return Transition(stage=Stage.BUILD, detail=decision.reason)

    def _build(self, job: JobState) -> Transition:
        if job.worktree is None or job.worktree.removed:
            try:
                self.prepare_build(job)
                assert job.worktree is not None
                info = job.worktree
            except FactoryError as exc:
                return Transition(stage=Stage.AWAIT_HUMAN, detail=f"Worktree infrastructure: {exc}",
                                  resume_stage=Stage.BUILD)
            job.worktree = info
            self._emit(job, "execution", f"worktree {info.path}", branch=info.branch)
        tools = self._tools(job)
        task = current_task(job)
        if task is None:
            return Transition(stage=Stage.VERIFY, detail="all tasks checkpointed")
        if job.builder_calls >= self.policy.max_job_attempts:
            detail = f"Job reached max_job_attempts ({self.policy.max_job_attempts})."
            return Transition(stage=Stage.FAILED, detail=detail, error=detail)
        try:
            for infrastructure_attempt in range(4):
                if job.builder_calls >= self.policy.max_job_attempts:
                    detail = f"Job reached max_job_attempts ({self.policy.max_job_attempts})."
                    return Transition(stage=Stage.FAILED, detail=detail, error=detail)
                job.builder_calls += 1
                # Every provider invocation counts toward the job cap, including E18 retries.
                # Infrastructure failures consume neither task corrections nor splits.
                self.store.save(job)
                try:
                    result = self.harness.build(job, tools)
                    break
                except (InfrastructureError, TimeoutError, ConnectionError) as exc:
                    self._emit(job, "specialist_agents", f"E18 {job.id}: {type(exc).__name__}; "
                               f"infrastructure attempt {infrastructure_attempt + 1}")
                    if infrastructure_attempt == 3:
                        return Transition(stage=Stage.AWAIT_HUMAN,
                                          detail="Builder infrastructure retries exhausted; retry this call.",
                                          resume_stage=Stage.BUILD)
                    time.sleep(2 ** infrastructure_attempt)
        except SafetyError as exc:
            return Transition(stage=Stage.FAILED, detail=str(exc), error=str(exc))
        except IncompleteBuildError as exc:
            return self._task_correction(job, str(exc), split=True)
        except FactoryError as exc:
            return self._task_correction(job, str(exc))
        except (OSError, ValueError) as exc:
            return self._task_correction(job, f"Builder could not apply this task ({type(exc).__name__}). "
                                         "Check file paths and the proposal format, then correct this task.")
        except Exception as exc:
            return self._task_correction(job, f"Builder call failed: {type(exc).__name__}. "
                                         "Return one complete BuildProposal.")
        job.builds.append(result)
        job.checkpoint_shas.setdefault(task.id, []).append(result.commit)
        job.task_summaries[task.id] = result.summary[:600]
        self._write_model(job, "build.json", result)
        self._write_model(job, "implementation_plan.json", job.plan)
        if isinstance(self.execution, PortableWorkspace) and job.worktree is not None:
            self.execution.checkpoint(job.worktree)
        # Save applied parts even if a quick check fails or the task needs more calls.
        self.store.save(job)
        if result.correction:
            return self._task_correction(job, result.correction)
        if not result.task_complete:
            return self._task_correction(job, "Continue the remaining work for this task only. "
                                         "Use insert_after/insert_before with a unique old anchor, "
                                         "or append/prepend with new "
                                         "to add code to the current checkpoint. Never use replace with empty old. "
                                         "Return another smaller part with task_complete=false if necessary.",
                                         split=True)
        missing = [path for path in task.files if path not in job.deleted_files and not tools._resolve(path).is_file()]
        if missing:
            return self._task_correction(job, "\n".join(missing_file_feedback(job, path) for path in missing))
        if task.quick_test_targets:
            quick = self.verifier.run_quick(job, tools, task.quick_test_targets)
            job.quick_verify_runs.setdefault(task.id, []).append(quick)
            if not quick.passed and job.task_retries.get(task.id, 0) < self.policy.per_task_retries:
                ids = list(dict.fromkeys(re.findall(r"(?:FAILED|ERROR)\s+([^\s]+)", quick.actionable)))
                feedback = "Failing test ids: " + ", ".join(ids) + "\n"
                feedback += "\n".join(quick.actionable.splitlines()[-60:])
                return self._task_correction(job, feedback)
        if task.id not in job.completed_task_ids:
            job.completed_task_ids.append(task.id)
        job.current_task_id = None
        job.govern_feedback = None
        next_task = current_task(job)
        return Transition(stage=Stage.BUILD if next_task else Stage.VERIFY,
                          detail=f"checkpointed task {task.id}: {result.summary}")

    def _task_correction(self, job: JobState, feedback: str, *, split: bool = False) -> Transition:
        task = current_task(job)
        assert task is not None
        job.govern_feedback = feedback
        if split:
            used_splits = job.task_split_attempts.get(task.id, 0)
            job.task_split_attempts[task.id] = used_splits + 1
            if used_splits < self.policy.split_attempts:
                return Transition(stage=Stage.BUILD, detail=feedback)
            # E21: further splits become ordinary corrections after the split allowance.
        used = job.task_retries.get(task.id, 0)
        if used >= self.policy.per_task_retries:
            detail = f"Task {task.id} exhausted {self.policy.per_task_retries} retries; last error: {feedback}"
            return Transition(stage=Stage.FAILED, detail=detail, error=detail)
        job.task_retries[task.id] = used + 1
        return Transition(stage=Stage.BUILD, detail=feedback)

    def _verify(self, job: JobState) -> Transition:
        tools = self._tools(job)
        result = self.verifier.run(job, tools)
        job.verify_runs.append(result)
        self._write_model(job, "verify.json", result)
        if job.worktree is not None:
            diff_path = self._run_dir(job) / f"verify-{len(job.verify_runs)}.diff"
            diff_path.write_text(tools.diff(job.worktree.base_sha))
        if result.passed:
            return Transition(stage=Stage.ENSURE_FLAG, detail="verification passed")
        missing = [path for path in job.plan.files
                   if path not in job.deleted_files and not tools._resolve(path).is_file()] if job.plan else []
        if missing:
            feedback = missing_file_feedback(job, missing[0])
            reopen_task(job, missing[0])
        else:
            output = "\n".join(report.output for report in result.commands if not report.passed)
            owner = reopen_task(job, output or result.actionable)
            feedback = verification_feedback(owner, output or result.actionable, tools)
        return self._task_correction(job, feedback)

    def _ensure_flag(self, job: JobState) -> Transition:
        try:
            record = self.flags.ensure(job)
        except FactoryError as exc:
            text = self.flags.scrub(str(exc))
            return Transition(stage=Stage.AWAIT_HUMAN, detail="LaunchDarkly infrastructure: " + text,
                              resume_stage=Stage.ENSURE_FLAG)
        job.launchdarkly = record
        self._write_model(job, "launchdarkly.json", record)
        if record.blocks_review():
            detail = record.detail or "flag is not off in production"
            return Transition(stage=Stage.AWAIT_HUMAN, detail=detail, resume_stage=Stage.ENSURE_FLAG)
        return Transition(stage=Stage.REVIEW, detail=record.detail or "flag check recorded")

    def _review(self, job: JobState) -> Transition:
        report = self.harness.review(job, self._tools(job))
        job.review = report
        job.reviews.append(report)
        self._write_model(job, "review_report.json", report)
        if report.verdict == "approve":
            return Transition(stage=Stage.GOVERN_SHIP, detail=report.summary)
        if report.verdict == "needs_human":
            return Transition(stage=Stage.AWAIT_HUMAN, detail=report.summary, resume_stage=Stage.PLAN)
        used = job.retries.get("review", 0)
        if used >= self.policy.max_retries.get("review", 1):
            reopen_task(job, report.summary)
            return Transition(stage=Stage.AWAIT_HUMAN, detail=report.summary, resume_stage=Stage.BUILD)
        job.retries["review"] = used + 1
        reopen_task(job, report.summary)
        return Transition(stage=Stage.BUILD, detail=report.summary)

    def _govern_ship(self, job: JobState) -> Transition:
        diff = ""
        if job.worktree is not None and not job.worktree.removed:
            diff = self._tools(job).diff(job.worktree.base_sha)
        decision = review_ship(self.policy, job, diff)
        if decision.allowed:
            return Transition(stage=Stage.DRAFT_PR, detail=decision.reason)
        if decision.retry_build:
            job.retries["govern"] = job.retries.get("govern", 0) + 1
            reopen_task(job, decision.reason)
            return self._task_correction(job, decision.reason)
        return Transition(stage=Stage.FAILED, detail=decision.reason, error=decision.reason)

    def _draft_pr(self, job: JobState) -> Transition:
        diff = ""
        if job.worktree is not None and not job.worktree.removed:
            diff = self._tools(job).diff(job.worktree.base_sha)
        self._run_dir(job).joinpath("diff.patch").write_text(diff)
        try:
            job.pr = self.publisher.publish(job, diff, self._run_dir(job))
        except GovernanceError as exc:
            return Transition(stage=Stage.FAILED, detail=str(exc), error=str(exc))
        return Transition(stage=Stage.DONE, detail=job.pr.body_path)

    def _finish(self, job: JobState) -> None:
        run_dir = self._run_dir(job)
        if (
            job.worktree is not None
            and not job.worktree.removed
            and not (run_dir / "diff.patch").exists()
        ):
            try:
                (run_dir / "diff.patch").write_text(self._tools(job).diff(job.worktree.base_sha))
            except Exception:
                pass
        summary = build_summary(job)
        (run_dir / "summary.json").write_text(_json(summary))
        self._emit(
            job,
            "observability",
            f"duration_ms={summary['duration_ms']} tokens={job.usage.input_tokens}+{job.usage.output_tokens}",
        )
        terminal = job.stage in {Stage.DONE, Stage.FAILED} or sys.exc_info()[0] is not None
        if terminal and not self.keep_worktree and job.worktree is not None and not job.worktree.removed:
            try:
                self.execution.destroy(self.target_repo, job.worktree)
                job.worktree.removed = True
            except Exception as exc:
                self._emit(job, "execution", f"worktree cleanup failed: {exc}")
        self.store.save(job)

    def _tools(self, job: JobState) -> ToolLayer:
        if job.worktree is None:
            raise FactoryError("no worktree")
        return ToolLayer(Path(job.worktree.path), self.policy, target_repo=self.target_repo)

    def _run_dir(self, job: JobState) -> Path:
        path = self.home / "runs" / job.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _open_logs(self, job: JobState) -> None:
        run_dir = self._run_dir(job)
        self.log = JsonLog(run_dir / "log.jsonl")
        self.tracer = Tracer(run_dir / "trace.jsonl", self.clock)

    def _write_model(self, job: JobState, name: str, model) -> None:
        self._run_dir(job).joinpath(name).write_text(model.model_dump_json(indent=2) + "\n")

    def _emit(self, job: JobState, component: str, message: str, **data: str) -> None:
        event = JobEvent(
            ts=self.clock.now(),
            component=component,
            stage=job.stage.value,
            message=message,
            data=data,
        )
        job.events.append(event)
        job.updated_at = event.ts
        if self.log is not None:
            self.log.write(event)
        if self.on_event is not None:
            self.on_event(event)


def _json(payload: dict[str, object]) -> str:
    import json

    return json.dumps(payload, indent=2) + "\n"
