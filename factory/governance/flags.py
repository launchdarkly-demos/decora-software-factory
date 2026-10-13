from __future__ import annotations

import re


def audit_flag(diff: str, flag_key: str | None, *, must_default_off: bool) -> list[str]:
    """Deterministic LaunchDarkly check over the real diff.

    The builder's summary is not an input. A user-facing change needs the flag
    key in the diff and a code default of off.
    """
    problems: list[str] = []
    if not flag_key:
        problems.append("user-facing change has no LaunchDarkly flag key")
        return problems
    if flag_key not in diff:
        problems.append(f"diff does not contain LaunchDarkly flag {flag_key}")
    if must_default_off and (
        "FLAG_DEFAULT = False" not in diff or re.search(r"FLAG_DEFAULT\s*=\s*True", diff)
    ):
        problems.append("FLAG_DEFAULT must be False")
    return problems


def verifier_requires_default_off(actionable: str) -> bool:
    needles = (
        "FLAG_DEFAULT must be False",
        "assert FLAG_DEFAULT is False",
        "assert True is False",
    )
    return any(needle in actionable for needle in needles)
