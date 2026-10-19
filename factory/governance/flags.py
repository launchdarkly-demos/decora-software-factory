from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import PurePosixPath


def is_production_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (path.suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
            and not {"tests", "test", "__tests__"}.intersection(path.parts)
            and ".test." not in path.name and ".spec." not in path.name
            and not path.name.startswith("test_") and not path.name.endswith("_test.py"))


def _production_lines(diff: str) -> list[tuple[str, str]]:
    """Added production lines with target-file locations; never trust test evidence."""
    is_diff = diff.startswith("diff --git ") or "\n+++ " in diff
    path = "<source>"
    production = True
    number = 0
    lines = []
    for line in diff.splitlines():
        if is_diff:
            if line.startswith("+++ "):
                path = line[4:].removeprefix("b/")
                production = is_production_path(path)
                continue
            hunk = re.match(r"@@ -[^ ]+ \+(\d+)", line)
            if hunk:
                number = int(hunk[1]) - 1
                continue
            if line.startswith((" ", "+")):
                number += 1
            if not production or not line.startswith("+") or line.startswith("+++"):
                continue
            line = line[1:]
        else:
            number += 1
        if not line.lstrip().startswith(("#", "//", "/*", "*")):
            lines.append((f"{path}:{number}", line))
    return lines


def _evaluation_defaults(code: str, flag_key: str) -> list[tuple[int, ast.expr | None]]:
    # Diff hunks need not form a parseable module. Parse balanced call expressions
    # individually, including indented and multiline calls, without executing code.
    names = set(re.findall(r"\b(\w+)\s*(?::[^=\n]+)?=\s*[\"']" + re.escape(flag_key) + r"[\"']", code))
    evaluations = []
    for match in re.finditer(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s*\(", code):
        fragment = code[match.start():]
        depth = 0
        try:
            for token in tokenize.generate_tokens(io.StringIO(fragment).readline):
                if token.type != tokenize.OP:
                    continue
                if token.string == "(":
                    depth += 1
                elif token.string == ")":
                    depth -= 1
                    if depth == 0:
                        rows = fragment.splitlines(keepends=True)
                        expression = "".join(rows[:token.end[0] - 1]) + rows[token.end[0] - 1][:token.end[1]]
                        node = ast.parse(expression, mode="eval").body
                        if isinstance(node, ast.Call):
                            key = node.args[0] if node.args else next(
                                (kw.value for kw in node.keywords if kw.arg in {"key", "flag_key"}), None)
                            if ((isinstance(key, ast.Constant) and key.value == flag_key)
                                    or (isinstance(key, ast.Name) and key.id in names)):
                                default = next((kw.value for kw in node.keywords if kw.arg == "default"),
                                               node.args[2] if len(node.args) > 2 else None)
                                line = code.count("\n", 0, match.start())
                                if default is not None:
                                    line += default.lineno - 1
                                evaluations.append((line, default))
                        break
        except (SyntaxError, tokenize.TokenError, IndentationError):
            continue
    return evaluations


def audit_flag(diff: str, flag_key: str | None, *, must_default_off: bool) -> list[str]:
    """Deterministic LaunchDarkly check over the real diff.

    The builder's summary is not an input. A change requiring a rollout flag needs the flag
    key in the diff and a code default of off.
    """
    problems: list[str] = []
    if not flag_key:
        problems.append("change requiring a flag has no LaunchDarkly flag key")
        return problems
    records = _production_lines(diff)
    code = "\n".join(line for _, line in records)
    if flag_key not in code:
        problems.append(f"diff does not contain LaunchDarkly flag {flag_key}")
    if must_default_off:
        assignments = list(re.finditer(
            r"^[ \t]*(?:export\s+)?(?:(?:const|let)\s+)?FLAG_DEFAULT\s*=\s*"
            r"(False|false|True|true)[ \t]*;?[ \t]*(?:#.*|//.*)?$",
            code, re.MULTILINE))
        declared_off = any(match[1] in {"False", "false"} for match in assignments)
        offending = [code.count("\n", 0, match.start()) for match in assignments
                     if match[1] in {"True", "true"}]
        evaluations = _evaluation_defaults(code, flag_key)
        for line, default in evaluations:
            if isinstance(default, ast.Constant) and default.value is True:
                offending.append(line)
            elif not declared_off and not (isinstance(default, ast.Constant) and default.value is False):
                offending.append(line)
        if offending or not (declared_off or evaluations):
            locations = sorted(set(offending)) or list(range(len(records)))
            evidence = "\n".join(f"{records[i][0]}: {records[i][1]}" for i in locations)
            problems.append(
                "FLAG_DEFAULT must be False: accepted forms are (a) FLAG_DEFAULT = False or "
                "(b) every flag evaluation passes default=False or positional False; any true default fails."
                "\nOffending lines (or missing evidence):\n" + evidence)
    return problems


def verifier_requires_default_off(actionable: str) -> bool:
    needles = (
        "FLAG_DEFAULT must be False",
        "assert FLAG_DEFAULT is False",
        "assert True is False",
    )
    return any(needle in actionable for needle in needles)
