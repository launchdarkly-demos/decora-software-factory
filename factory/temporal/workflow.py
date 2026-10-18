from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn
class FactoryWorkflow:
    """Only orchestration here: I/O and model calls belong to activities."""

    def __init__(self) -> None:
        self.job: dict[str, Any] = {}
        self.sequence = 0
        self.answers: dict[int, str] = {}
        self.pause_id = 0
        self.activity_error: str | None = None

    @workflow.signal
    def answer(self, pause_id: int, text: str) -> None:
        # A signal sent while the worker is offline is replayed after restart.
        # Round IDs prevent a duplicate answer from approving a later pause.
        if text.strip() and pause_id == self.pause_id and pause_id not in self.answers:
            self.answers[pause_id] = text.strip()

    @workflow.query
    def snapshot(self) -> dict[str, Any]:
        return {"job": self.job, "pause_id": self.pause_id, "activity_error": self.activity_error}

    async def _activity(self, operation: str, answer: str = "") -> None:
        self.job = await workflow.execute_activity(
            "factory_stage",
            {"job": self.job, "sequence": self.sequence, "operation": operation, "answer": answer},
            result_type=dict[str, Any],
            # One build activity includes a 600s call and up to three E18 retries,
            # plus checkpoint/quick-test overhead. The model call enforces its own limit.
            start_to_close_timeout=timedelta(seconds=4 * 600 + 2 * 180),
            schedule_to_close_timeout=timedelta(hours=3),
            retry_policy=RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3),
        )
        self.sequence += 1

    @workflow.run
    async def run(self, feature: dict[str, Any]) -> dict[str, Any]:
        now = workflow.now().timestamp()
        self.job = {
            "id": workflow.info().workflow_id,
            "feature": feature,
            "stage": "intake",
            "created_at": now,
            "updated_at": now,
        }
        try:
            while self.job["stage"] not in {"done", "failed"}:
                if self.job["stage"] == "await_human":
                    self.pause_id += 1
                    await workflow.wait_condition(lambda: self.pause_id in self.answers)
                    await self._activity("answer", self.answers.pop(self.pause_id))
                else:
                    await self._activity("step")
            await self._activity("finish")
        except ActivityError:
            # Keep the worktree for inspection after an infrastructure failure.
            # Do not expose provider exception text in history/status output.
            self.activity_error = "Activity retries exhausted; inspect worker logs and retained job artifacts."
            self.job["stage"] = "failed"
            self.job["error"] = self.activity_error
        return self.job
