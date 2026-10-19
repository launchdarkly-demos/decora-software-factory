"""Bound test work before the plan reaches the Builder."""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from factory.models import ImplementationPlan, JobState, PlanTask

if TYPE_CHECKING:
    from factory.governance.policy import Policy


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


def normalize_flag_task(policy: Policy, job: JobState, plan: ImplementationPlan) -> None:
    """Give a required flag one explicit production owner, without a plan send-back."""
    from factory.governance.flags import is_production_path
    from factory.governance.policy import flag_keys, flag_required

    keys = flag_keys(job, plan)
    if not flag_required(policy, job, plan) or len(keys) != 1:
        return
    key = next(iter(keys))
    inventory = job.context.file_inventory if job.context else []
    modules = sorted(path for path in inventory if is_production_path(path)
                     and PurePosixPath(path).name in {'flags.py', 'flag.py'})
    module = modules[0] if modules else policy.flags.module
    if not plan.tasks and plan.recipe == 'general':
        return  # Governance still rejects an unstructured general plan.
    if not plan.tasks and plan.files:
        # Preserve the implicit task used by legacy saved plans and offline recipes.
        plan.tasks = [PlanTask(id='T1', description=plan.summary, files=list(plan.files),
                               acceptance=job.feature.acceptance or plan.steps or [plan.summary])]
    production = [task for task in plan.tasks if not is_test_task(task)]
    constant = re.sub(r'[^A-Za-z0-9_]', '_', key).upper() + '_FLAG'
    # Reuse a concrete existing contract even when the repository uses a different constant.
    candidates = []
    for task in production:
        acceptance = '\n'.join(task.acceptance)
        assignment = re.search(r"\b([A-Za-z_]\w*)\s*=\s*['\"]" + re.escape(key) + r"['\"]", acceptance)
        if (module in task.files and assignment and re.search(r'FLAG_DEFAULT\s*=\s*False', acceptance)
                and f'get_flag({assignment[1]}, context, default=FLAG_DEFAULT)' in acceptance):
            candidates.append(task)
    # One owner is recorded explicitly; redundant flag contracts belong to that owner.
    owner = candidates[0] if candidates else None
    for duplicate in candidates[1:]:
        assert owner is not None
        shared = [check for check in duplicate.acceptance if (
            key in check or 'FLAG_DEFAULT' in check or 'get_flag(' in check)]
        owner.acceptance.extend(check for check in shared if check not in owner.acceptance)
        duplicate.acceptance = [check for check in duplicate.acceptance if not (
            key in check or 'FLAG_DEFAULT' in check or 'get_flag(' in check)]
        duplicate.acceptance.append(f'Use the flag provided by task {owner.id}.')
    if owner is None:
        gated = list(dict.fromkeys(path for path in plan.files
                                   if is_production_path(path) and path != module))
        name = 'provision_flag_' + re.sub(r'[^a-z0-9_]+', '_', key.lower()).strip('_')
        used = {task.id for task in plan.tasks}
        base, suffix = name, 2
        while name in used:
            name = f'{base}_{suffix}'
            suffix += 1
        owner = PlanTask(id=name, description=f'Provision required flag {key} and gate new behavior.',
                         files=[module, *gated], depends_on=[task.id for task in production],
                         acceptance=[f"In {module}, define {constant} = '{key}' and FLAG_DEFAULT = False.",
                                     f'Gate new behavior with get_flag({constant}, context, default=FLAG_DEFAULT).'])
        # Production precedes tests so dependencies remain satisfiable even for interleaved plans.
        tests = [task for task in plan.tasks if is_test_task(task)]
        for task in tests:
            if owner.id not in task.depends_on:
                task.depends_on.append(owner.id)
        plan.tasks = [*production, owner, *tests]
        plan.normalization_notes.append(f'Inserted {owner.id}: {module}; gate {", ".join(gated)}.')
    plan.flag_task_id = owner.id
    if module not in plan.files:
        plan.files.append(module)
