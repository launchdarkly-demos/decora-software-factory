"""Task selection and file ownership shared by the machine and harnesses."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from factory.errors import FactoryError
from factory.models import JobState, PlanTask
from factory.plan_normalization import is_test_task
from factory.tools.layer import ToolLayer


def tasks(job: JobState) -> list[PlanTask]:
    if job.plan is None:
        raise FactoryError('build requires a plan')
    # Old saved plans and the offline recipe are one bounded task.
    if not job.plan.tasks:
        job.plan.tasks = [PlanTask(id='T1', description=job.plan.summary,
                                   files=list(job.plan.files),
                                   acceptance=job.feature.acceptance or job.plan.steps)]
    for task in job.plan.tasks:
        job.task_original_files.setdefault(task.id, list(task.files))
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


def reopen_task(job: JobState, feedback: str, tools: ToolLayer | None = None) -> PlanTask:
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
    modules: set[str] = set()
    if tools is not None:
        # Traceback order runs from caller to the deepest frame. Prefer the last
        # non-test frame over imports; assertion-only tracebacks rely on imports.
        frames = re.findall(r'(?:File ["\']([^"\']+)["\'], line \d+|'
                            r'(?:^|\n)([\w./-]+\.py):\d+)', failure_output)
        for frame in reversed(frames):
            path = (frame[0] or frame[1]).removeprefix('./')
            if not _is_test(path) and _repo_file(tools, path):
                modules = {path}
                break
        if not modules:
            for path in sorted(mentioned):
                if _is_test(path) and _repo_file(tools, path):
                    modules.update(_imported_modules(tools, path))
    owner = _production_owner(job, modules)
    if owner is None:
        owner = next((task for task in reversed(queue)
                      if mentioned.intersection(job.task_original_files[task.id])), queue[-1])
    for path in sorted(modules):
        if path not in owner.files:
            owner.files.append(path)
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


def _is_test(path: str) -> bool:
    file = Path(path)
    return any(part in {'tests', 'test'} for part in file.parts) or (
        file.name.startswith('test_') or file.name.endswith('_test.py'))


def _repo_file(tools: ToolLayer, path: str) -> bool:
    try:
        return tools._resolve(path).is_file()
    except FactoryError:
        return False


def _imported_modules(tools: ToolLayer, path: str) -> set[str]:
    try:
        tree = ast.parse(tools.read(path))
    except (SyntaxError, FactoryError):
        return set()
    modules: set[str] = set()

    def resolve(name: str) -> None:
        if not name:
            return
        relative = name.replace('.', '/')
        for prefix in ('', 'src/'):
            for candidate in (prefix + relative + '.py', prefix + relative + '/__init__.py'):
                if not _is_test(candidate) and _repo_file(tools, candidate):
                    modules.add(candidate)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolve(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                parts = list(Path(path).parent.parts)
                if node.level > len(parts):
                    continue
                base = '.'.join([*parts[:len(parts) - node.level + 1], *([base] if base else [])])
            resolve(base)
            for alias in node.names:
                if alias.name != '*':
                    resolve('.'.join(filter(None, [base, alias.name])))
    return modules


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


def reopen_import_task(job: JobState, path: str) -> PlanTask:
    """Import checks charge the module's declared owner, else the reporting task."""
    queue = tasks(job)
    owner = _production_owner(job, {path})
    owner = owner or current_task(job) or queue[-1]
    job.current_task_id = owner.id
    if owner.id in job.completed_task_ids:
        job.completed_task_ids.remove(owner.id)
    if path not in owner.files:
        owner.files.append(path)
    return owner


def _production_owner(job: JobState, paths: set[str]) -> PlanTask | None:
    candidates = [task for task in tasks(job) if paths.intersection(job.task_original_files[task.id])]
    # Broad test tasks may list production files as context/repair scope. They
    # must not steal ownership from the task that implemented the broken module.
    production = [task for task in candidates if not is_test_task(task)]
    candidates = production or candidates
    if not candidates:
        return None
    return max(candidates, key=lambda task: (
        max((job.task_file_calls.get(task.id, {}).get(path, 0) for path in paths), default=0),
        tasks(job).index(task)))
