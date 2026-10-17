from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from factory.errors import FactoryError
from factory.execution.portable import PortableWorkspace
from factory.gitcmd import git
from factory.models import JobState, Stage
from factory.orchestrator.machine import Factory


class StageActivities:
    """One worker host and persistent home per task queue.

    Atomic receipts cover redelivery after completion. The job lock also fences
    overlapping timed-out attempts. Interrupted builds restore a known Git state
    in the owned worktree; no recovery command targets the primary checkout.
    """

    def __init__(self, factory: Callable[[], Factory], home: Path) -> None:
        self.factory = factory
        self.home = home.resolve()

    @activity.defn(name="factory_stage")
    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        job = JobState.model_validate(request["job"])
        if not re.fullmatch(r"job-[a-zA-Z0-9-]+", job.id):
            raise ApplicationError("Invalid factory job ID", non_retryable=True)
        sequence = int(request["sequence"])
        operation = request["operation"]
        if sequence < 0 or operation not in {"step", "answer", "finish"}:
            raise ApplicationError("Invalid stage request", non_retryable=True)
        directory = self.home / "temporal" / job.id
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "job.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            receipt = directory / f"{sequence}.json"
            previous = json.loads(receipt.read_text()) if receipt.exists() else None
            if previous and previous["status"] == "done":
                return previous["job"]
            try:
                factory = self.factory()
                if previous:
                    job = JobState.model_validate(previous["job"])
                    if operation == "step" and job.stage == Stage.BUILD:
                        # The stage may have committed and saved before the receipt was written.
                        # Keep its task progress and budgets instead of replaying completed work.
                        try:
                            durable = factory.store.load(job.id)
                        except FactoryError:
                            durable = job
                        if durable.completed_builder_calls > job.completed_builder_calls:
                            if isinstance(factory.execution, PortableWorkspace) and durable.worktree is not None:
                                factory.execution.ensure(factory.target_repo, durable.worktree)
                            result = durable.model_dump(mode="json")
                            _atomic_json(receipt, {"status": "done", "job": result})
                            return result
                        head = previous["head"]
                        if durable.builder_calls > job.builder_calls:
                            job = durable
                            latest = job.latest_build()
                            if latest is not None:
                                head = latest.commit
                        self._restore_build(factory, job, head)
                if (isinstance(factory.execution, PortableWorkspace) and job.worktree is not None
                        and not job.worktree.removed and operation != "finish"):
                    factory.execution.ensure(factory.target_repo, job.worktree)
                head = None
                if operation == "step" and job.stage == Stage.BUILD:
                    factory.prepare_build(job)
                    assert job.worktree is not None
                    head_result = git(Path(job.worktree.path), "rev-parse", "HEAD")
                    if head_result.returncode:
                        raise FactoryError("Cannot checkpoint build HEAD")
                    head = head_result.stdout.strip()
                _atomic_json(receipt, {"status": "started", "job": job.model_dump(mode="json"), "head": head})
                if operation == "step":
                    factory.advance(job)
                elif operation == "answer":
                    factory.accept_answer(job, str(request["answer"]))
                else:
                    factory.finish(job)
                if (isinstance(factory.execution, PortableWorkspace) and job.worktree is not None
                        and not job.worktree.removed):
                    factory.execution.checkpoint(job.worktree)
                    factory.store.save(job)
                result = job.model_dump(mode="json")
                _atomic_json(receipt, {"status": "done", "job": result})
                return result
            except Exception as exc:
                # Avoid serializing exception messages that might contain credentials.
                raise ApplicationError(f"Factory activity failed ({type(exc).__name__})") from None

    def _restore_build(self, factory: Factory, job: JobState, head: str) -> None:
        if job.worktree is None:
            raise FactoryError("Interrupted build has no worktree checkpoint")
        if isinstance(factory.execution, PortableWorkspace):
            factory.execution.recover(factory.target_repo, job.worktree, head)
            return
        path = Path(job.worktree.path).resolve()
        expected = (self.home / "worktrees" / job.id).resolve()
        if path != expected or path == factory.target_repo or not (path / ".git").is_file():
            raise FactoryError("Refusing recovery outside the owned worktree")
        branch = git(path, "branch", "--show-current")
        if branch.stdout.strip() != f"factory/{job.id}":
            raise FactoryError("Worktree branch does not belong to this job")
        for args in (("reset", "--hard", head), ("clean", "-fd")):
            if git(path, *args).returncode:
                raise FactoryError("Could not restore interrupted build checkpoint")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(payload, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
