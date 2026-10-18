"""Task selection and file ownership shared by the machine and harnesses."""
from __future__ import annotations

import re

from factory.errors import FactoryError
from factory.models import JobState, PlanTask
from factory.tools.layer import ToolLayer


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
    # Strip the root before matching so worktrees containing spaces also work.
    if job.worktree:
        failure_output = failure_output.replace(job.worktree.path.rstrip('/') + '/', '')
    mentioned: set[str] = set()
    for line in failure_output.splitlines():
        for pattern in (r'(?:FAILED|ERROR)\s+([^\s:]+\.[A-Za-z0-9]+)',
                        r'File ["\']([^"\']+)["\'], line \d+',
                        r'(?:^|\s)([\w./-]+\.[A-Za-z0-9]+):\d+(?::|\b)'):
            mentioned.update(name.removeprefix('./') for name in re.findall(pattern, line))
    if not mentioned:
        mentioned = {name.removeprefix('./') for name in re.findall(r'[\w./-]+\.[A-Za-z0-9]+', failure_output)}
    owner = next((task for task in reversed(queue)
                  if mentioned.intersection(task.files)), queue[-1])
    job.current_task_id = owner.id
    if owner.id in job.completed_task_ids:
        job.completed_task_ids.remove(owner.id)
    affected = {owner.id}
    for task in queue[queue.index(owner) + 1:]:
        if affected.intersection(task.depends_on):
            affected.add(task.id)
            later = job.task_file_calls.get(task.id, {})
            earlier = job.task_file_calls.get(owner.id, {})
            if any(path in earlier and call > earlier[path] for path, call in later.items()):
                if task.id in job.completed_task_ids:
                    job.completed_task_ids.remove(task.id)
    job.govern_feedback = feedback
    return owner


def missing_file_feedback(job: JobState, path: str) -> str:
    assert job.plan is not None
    owners = [task for task in tasks(job) if path in task.files]
    coverage = [owners[-1].description, *owners[-1].acceptance] if owners else []
    description = " ".join(coverage or job.feature.acceptance or [job.plan.summary])
    return f'Create `{path}` covering: `{description}`.'


def verification_feedback(owner: PlanTask, output: str, tools: ToolLayer) -> str:
    """E15 includes failing IDs, the owner's traceback, and fresh source at that location."""
    from factory.agents.staging import source_context

    ids = list(dict.fromkeys(re.findall(r'(?:FAILED|ERROR)\s+([^\s]+)', output)))
    lines = output.splitlines()
    sections = []
    for index, line in enumerate(lines):
        for path in owner.files:
            if path not in line:
                continue
            location = re.search(re.escape(path) + r'(?:["\'], line |:)(\d+)', line)
            number = int(location.group(1)) if location else 1
            sections.append('\n'.join(lines[max(0, index - 5):index + 8]))
            if tools._resolve(path).is_file():
                sections.append(source_context(path, tools.read(path), number, 1, 'Current owning-file region'))
    if not sections:
        sections.append(output)
        for path in owner.files:
            if tools._resolve(path).is_file():
                sections.append(source_context(path, tools.read(path), 1, 1, 'Current owning-file region'))
    return 'Failing test ids: ' + (', '.join(ids) or 'see failure output') + '\n' + '\n'.join(sections)
