"""Resolve maintainer-owned checks from planned or actual changed paths."""
from __future__ import annotations

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
    for task in plan.tasks:
        if not task.id or task.id in seen or not set(task.depends_on).issubset(seen):
            return ['Plan tasks must have unique IDs and depend only on earlier tasks.']
        if not set(task.files).issubset(plan.files):
            return ['Task files must be included in the approved plan file list.']
        if not task.acceptance:
            return ['Each plan task needs acceptance checks.']
        seen.add(task.id)
    if plan.recipe == 'general' and not plan.tasks:
        return ['A general implementation plan must break the work into concrete tasks.']
    return []
