from __future__ import annotations

import re
from pathlib import PurePosixPath


def _added_production_code(diff: str) -> str:
    """Exclude removed code, test fixtures, and comment-only lines from the audit."""
    is_diff = diff.startswith("diff --git ") or "\n+++ " in diff
    production = True
    lines = []
    for line in diff.splitlines():
        if is_diff:
            if line.startswith("+++ "):
                path = PurePosixPath(line[4:].removeprefix("b/"))
                production = (
                    path.suffix == ".py"
                    and "tests" not in path.parts
                    and not path.name.startswith("test_")
                    and not path.name.endswith("_test.py")
                )
                continue
            if not production or not line.startswith("+") or line.startswith("+++"):
                continue
            line = line[1:]
        if not line.lstrip().startswith("#"):
            lines.append(line)
    return "\n".join(lines)


def audit_flag(diff: str, flag_key: str | None, *, must_default_off: bool) -> list[str]:
    """Deterministic LaunchDarkly check over the real diff.

    The builder's summary is not an input. A user-facing change needs the flag
    key in the diff and a code default of off.
    """
    problems: list[str] = []
    if not flag_key:
        problems.append("user-facing change has no LaunchDarkly flag key")
        return problems
    code = _added_production_code(diff)
    if flag_key not in code:
        problems.append(f"diff does not contain LaunchDarkly flag {flag_key}")
    if must_default_off and (
        not re.search(r"^\s*FLAG_DEFAULT\s*=\s*False\b", code, re.MULTILINE)
        or re.search(r"^\s*FLAG_DEFAULT\s*=\s*True\b", code, re.MULTILINE)
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
