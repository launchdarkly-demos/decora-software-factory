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
        "commits": [item.commit for item in job.builds],
        "pr": job.pr.model_dump() if job.pr else None,
    }
