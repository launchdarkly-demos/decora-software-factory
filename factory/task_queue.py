"""Task selection and file ownership shared by the machine and harnesses."""
from __future__ import annotations

import re

from factory.errors import FactoryError
from factory.models import JobState, PlanTask


def tasks(job: JobState) -> list[PlanTask]:
    if job.plan is None:
        raise FactoryError('build requires a plan')
    # Old saved plans and the offline recipe are one bounded task.
    if not job.plan.tasks:
        job.plan.tasks = [PlanTask(id='T1', description=job.plan.summary,
                                   files=list(job.plan.files),
                                   acceptance=job.feature.acceptance or job.plan.steps)]
    return job.plan.tasks


def current_task(job: JobState) -> PlanTask | None:
    queue = tasks(job)
    if job.current_task_id:
        return next(task for task in queue if task.id == job.current_task_id)
    done = set(job.completed_task_ids)
    for task in queue:
        if task.id not in done and set(task.depends_on) <= done:
            job.current_task_id = task.id
            return task
    if len(done) != len(queue):
        raise FactoryError('Task dependencies cannot be satisfied')
    return None


def reopen_task(job: JobState, feedback: str) -> PlanTask:
    queue = tasks(job)
    # Prefer paths in failure output, including pytest's path::test_name notation.
    failure_output = '\n'.join(line for line in feedback.splitlines() if not line.startswith('$ '))
    mentioned = set(re.findall(r'[\w./-]+\.[A-Za-z0-9]+', failure_output))
    owner = next((task for task in reversed(queue) if mentioned.intersection(task.files)), queue[-1])
    job.current_task_id = owner.id
    if owner.id in job.completed_task_ids:
        job.completed_task_ids.remove(owner.id)
    job.govern_feedback = feedback
    return owner


def missing_file_feedback(job: JobState, path: str) -> str:
    assert job.plan is not None
    owners = [task for task in tasks(job) if path in task.files]
    coverage = [text for task in owners for text in [task.description, *task.acceptance]]
    return f'Create {path} covering: ' + ' '.join(coverage or job.feature.acceptance or [job.plan.summary])
