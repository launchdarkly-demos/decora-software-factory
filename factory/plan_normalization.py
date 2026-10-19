"""Bound test work before the plan reaches the Builder."""
from __future__ import annotations

import re

from factory.models import ImplementationPlan, PlanTask


def is_test_task(task: PlanTask) -> bool:
    name = task.id.lower()
    return bool(task.files) and (all(_test_file(path) for path in task.files)
                                or 'tests' in name or name.startswith('test_'))


def _test_file(path: str) -> bool:
    from factory.governance.flags import is_production_path

    return path.endswith('.py') and not is_production_path(path)


def normalize_test_tasks(plan: ImplementationPlan) -> None:
    """Split files, broad migration/compatibility work and oversized acceptance lists.

    Acceptance checks are the smallest deterministic behavior areas available in a
    structured plan. Each generated task is bounded further in the Builder prompt.
    IDs/dependencies remain stable on repeated governance/resume.
    """
    result = []
    replacements: dict[str, str] = {}
    used_ids = {task.id for task in plan.tasks}
    for task in plan.tasks:
        task.depends_on = list(dict.fromkeys(replacements.get(dep, dep) for dep in task.depends_on))
        files = [path for path in task.files if _test_file(path)]
        broad = bool(re.search(r'migration.*compatibility|compatibility.*migration',
                               task.id + ' ' + task.description, re.I)) and not task.description.startswith(
                                   'Create or append at most six test functions for this behavior area: ')
        if not is_test_task(task) or not files or (
                len(files) <= 1 and len(task.acceptance) <= 6 and not broad):
            result.append(task)
            continue
        checks = task.acceptance or [task.description]
        # Preserve every criterion. Broad tasks get one acceptance area per call;
        # otherwise group at most six checks for each test file.
        size = 1 if broad else 6
        groups = [checks[i:i + size] for i in range(0, len(checks), size)]
        previous = None
        for path in files:
            for group in groups:
                number = len(result) + 1
                name = f'{task.id}_part_{number}'
                while name in used_ids:
                    number += 1
                    name = f'{task.id}_part_{number}'
                used_ids.add(name)
                part = task.model_copy(deep=True)
                part.id = name
                part.files = [path]  # Production repairs retain their original owners.
                part.rewrite_files = [p for p in task.rewrite_files if p == path]
                part.description = ('Create or append at most six test functions for this behavior area: '
                                    + ' '.join(group))
                part.acceptance = list(group)
                part.depends_on = [previous] if previous else list(task.depends_on)
                part.quick_test_targets = [target for target in task.quick_test_targets
                                           if target.split('::')[0] == path]
                result.append(part)
                previous = name
        # Keep production files owned if this was the only task assigned to them.
        unowned = [path for path in task.files if path not in files and not any(
            path in other.files for other in plan.tasks if other.id != task.id)]
        if unowned:
            result[-1].files.extend(unowned)
        if previous:
            replacements[task.id] = previous
    plan.tasks = result
