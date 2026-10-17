from __future__ import annotations

from factory.models import JobState


def build_summary(job: JobState) -> dict[str, object]:
    return {
        "job_id": job.id,
        "stage": job.stage.value,
        "paused": job.paused,
        "question": job.question,
        "error": job.error,
        "duration_ms": max(0, int((job.updated_at - job.created_at) * 1000)),
        "tokens": job.usage.model_dump(),
        "retries": dict(job.retries),
        "task_retries": dict(job.task_retries),
        "task_split_attempts": dict(job.task_split_attempts),
        "builder_calls": job.builder_calls,
        "current_task_id": job.current_task_id,
        "completed_task_ids": list(job.completed_task_ids),
        "checkpoint_shas": dict(job.checkpoint_shas),
        "auto_added_files": list(job.auto_added_files),
        "commits": [item.commit for item in job.builds],
        "launchdarkly": job.launchdarkly.model_dump() if job.launchdarkly else None,
        "pr": job.pr.model_dump() if job.pr else None,
    }
