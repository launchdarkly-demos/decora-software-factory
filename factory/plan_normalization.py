"""Normalize task scope, flag ownership and dependency order before BUILD."""
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
    added: set[tuple[str, str]] = set()
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
                if previous:
                    added.add((part.id, previous))
                result.append(part)
                previous = name
        # Keep production files owned if this was the only task assigned to them.
        unowned = [path for path in task.files if path not in files and not any(
            path in other.files for other in plan.tasks if other.id != task.id)]
        if unowned:
            result[-1].files.extend(unowned)
        if previous:
            replacements[task.id] = previous
    # Forward references also need to point to the final split, not the removed task.
    for task in result:
        task.depends_on = list(dict.fromkeys(replacements.get(dep, dep) for dep in task.depends_on))
    plan.tasks = result
    repair_dependencies(plan, added)


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
    provision = [task for task in plan.tasks if task.id.startswith('provision_flag_')]
    owner = next(iter(provision), None)
    if owner is None:
        # A saved owner retains its identity after topological sorting moves other
        # module-touching tasks ahead of it on governance/resume.
        owner = next((task for task in production if task.id == plan.flag_task_id and module in task.files), None)
        if owner is None:
            owner = next((task for task in production if module in task.files), None)
    if owner is None:
        gated = list(dict.fromkeys(path for path in plan.files
                                   if is_production_path(path) and path != module))
        name = 'provision_flag_' + re.sub(r'[^a-z0-9_]+', '_', key.lower()).strip('_')
        owner = PlanTask(id=name, description=f'Provision required flag {key} and gate new behavior.',
                         files=[module, *gated])
        plan.tasks.append(owner)
        _note(plan, f'Inserted {owner.id}: {module}; gate {", ".join(gated)}.')
    removed = {task.id for task in provision if task is not owner}
    for duplicate in provision:
        if duplicate is owner:
            continue
        owner.files = list(dict.fromkeys([*owner.files, *duplicate.files]))
        owner.acceptance = list(dict.fromkeys([*owner.acceptance, *duplicate.acceptance]))
        owner.rewrite_files = list(dict.fromkeys([*owner.rewrite_files, *duplicate.rewrite_files]))
        owner.quick_test_targets = list(dict.fromkeys([*owner.quick_test_targets, *duplicate.quick_test_targets]))
        _note(plan, f'Merged {duplicate.id} into flag owner {owner.id}.')
    plan.tasks = [task for task in plan.tasks if task.id not in removed]
    for task in plan.tasks:
        task.depends_on = list(dict.fromkeys(owner.id if dep in removed else dep for dep in task.depends_on))
    if module not in owner.files:
        owner.files.append(module)
    # Preserve the repository/planner's constant name when it already binds the key.
    assignment = re.search(r"\b([A-Za-z_]\w*)\s*=\s*['\"]" + re.escape(key) + r"['\"]",
                           '\n'.join(owner.acceptance))
    if assignment:
        constant = assignment[1]
    for check in (f"In {module}, define {constant} = '{key}' and FLAG_DEFAULT = False.",
                  f'Gate new behavior with get_flag({constant}, context, default=FLAG_DEFAULT).'):
        if check not in owner.acceptance:
            owner.acceptance.append(check)
    plan.flag_task_id = owner.id
    if module not in plan.files:
        plan.files.append(module)
    # Cycle repair can make a production task independent of tests. Reach a fixed
    # point here so the next governance/resume cannot change the saved plan again.
    while True:
        before = [(task.id, list(task.depends_on)) for task in plan.tasks]
        test_dependent = {task.id for task in plan.tasks if is_test_task(task) and task is not owner}
        while True:
            expanded = test_dependent | {task.id for task in plan.tasks if task is not owner
                                        and any(dep in test_dependent for dep in task.depends_on)}
            if expanded == test_dependent:
                break
            test_dependent = expanded
        owner.depends_on = [task.id for task in plan.tasks if task is not owner
                            and not is_test_task(task) and task.id not in test_dependent]
        added = {(owner.id, dep) for dep in owner.depends_on}
        for task in plan.tasks:
            if task is not owner and is_test_task(task) and owner.id not in task.depends_on:
                task.depends_on.append(owner.id)
                added.add((task.id, owner.id))
        repair_dependencies(plan, added)
        if before == [(task.id, task.depends_on) for task in plan.tasks]:
            break


def _note(plan: ImplementationPlan, message: str) -> None:
    if message not in plan.normalization_notes:
        plan.normalization_notes.append(message)


def repair_dependencies(plan: ImplementationPlan, added: set[tuple[str, str]] | None = None) -> None:
    """Keep original task priority in a topological order; remove only cyclic edges."""
    tasks = {task.id: task for task in plan.tasks}
    if len(tasks) != len(plan.tasks):
        return  # Leave duplicate IDs to governance rather than guessing task identity.
    added = added or set()
    for task in plan.tasks:
        for dep in task.depends_on:
            if dep not in tasks:
                _note(plan, f'Dropped unknown dependency {task.id} -> {dep}.')
        task.depends_on = list(dict.fromkeys(dep for dep in task.depends_on if dep in tasks))

    def reaches(start: str, target: str) -> bool:
        pending, seen = [start], set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current not in seen:
                seen.add(current)
                pending.extend(tasks[current].depends_on)
        return False

    while True:
        ordered: list[PlanTask] = []
        done: set[str] = set()
        while ready := next((task for task in plan.tasks if task.id not in done
                            and all(dep in done for dep in task.depends_on)), None):
            ordered.append(ready)
            done.add(ready.id)
        if len(ordered) == len(plan.tasks):
            plan.tasks = ordered
            return
        cyclic = [(task.id, dep) for task in plan.tasks if task.id not in done
                  for dep in task.depends_on if reaches(dep, task.id)]

        def priority(edge: tuple[str, str]) -> tuple[int, str, str]:
            source, dependency = edge
            rank = (0 if edge in added else
                    1 if not is_test_task(tasks[source]) and is_test_task(tasks[dependency]) else 2)
            return rank, source, dependency

        source, dependency = min(cyclic, key=priority)
        tasks[source].depends_on.remove(dependency)
        reason = ('normalization-added' if (source, dependency) in added else
                  'production-to-test' if not is_test_task(tasks[source]) and is_test_task(tasks[dependency])
                  else 'fallback')
        _note(plan, f'Dropped cycle dependency {source} -> {dependency} ({reason}).')
