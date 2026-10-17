"""Resolve maintainer-owned checks from planned or actual changed paths."""
from __future__ import annotations

import keyword
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from factory.governance.policy import Policy
    from factory.models import ImplementationPlan

FRONTEND_SUFFIXES = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.html', '.css', '.scss', '.vue', '.svelte'}


def selected_profiles(policy: Policy, plan: ImplementationPlan, paths: list[str]) -> list[str]:
    names = list(plan.verification_profiles)
    for name, profile in policy.verification_profiles.items():
        if any(fnmatch(path, pattern) for path in paths for pattern in profile.paths):
            names.append(name)
    return list(dict.fromkeys(names))


def prerequisites(policy: Policy, plan: ImplementationPlan, paths: list[str]) -> list[str]:
    names = selected_profiles(policy, plan, paths)
    errors = [f'Unknown verification profile: {name}' for name in names if name not in policy.verification_profiles]
    kinds = {policy.verification_profiles[name].kind for name in names if name in policy.verification_profiles}
    frontend = any(PurePosixPath(path).suffix in FRONTEND_SUFFIXES
                   and not any(part in {'tests', '__tests__', 'e2e'} for part in PurePosixPath(path).parts)
                   and '.test.' not in path and '.spec.' not in path for path in paths)
    if frontend and policy.require_browser_for_frontend and 'browser' not in kinds:
        errors.append('Frontend changes require a configured browser verification profile before building.')
    if plan.requires_migration and not plan.migration_plan.strip():
        errors.append('Stored-data changes require a migration and recovery plan.')
    if plan.requires_migration and 'migration' not in kinds:
        errors.append('Stored-data changes require a configured migration verification profile.')
    for name in names:
        if name not in policy.verification_profiles:
            continue
        for argv in policy.verification_profiles[name].commands:
            try:
                policy.check_command(['python' if arg == '{python}' else arg for arg in argv])
            except Exception:
                errors.append(f'Profile {name} has a command not permitted by policy.')
    return errors


def task_problems(plan: ImplementationPlan) -> list[str]:
    seen: set[str] = set()
    errors: list[str] = []
    for path in plan.files:
        module = PurePosixPath(path)
        if module.suffix == '.py':
            parts = [module.stem, *module.parts[:-1]]
            if any(not part.isidentifier() or keyword.iskeyword(part) for part in parts):
                errors.append(f'Python module and package names must have valid identifiers: {path}')
    for task in plan.tasks:
        if not task.id or task.id in seen:
            errors.append(f"Task ID must be nonempty and unique: {task.id!r}.")
        missing_dependencies = set(task.depends_on) - seen
        if missing_dependencies:
            errors.append(f"Task {task.id} depends on tasks not listed earlier: "
                          + ", ".join(sorted(missing_dependencies)))
        missing_files = set(task.files) - set(plan.files)
        if missing_files:
            errors.append(f"Task {task.id} files missing from plan.files: " + ", ".join(sorted(missing_files)))
        if not task.acceptance:
            errors.append(f"Task {task.id} needs acceptance checks.")
        seen.add(task.id)
    if plan.tasks:
        unowned = set(plan.files) - {path for task in plan.tasks for path in task.files}
        if unowned:
            errors.append('Assign every planned file to a task: ' + ', '.join(sorted(unowned)))
    if plan.recipe == 'general' and not plan.tasks:
        errors.append('A general implementation plan must break the work into concrete tasks.')
    return errors
