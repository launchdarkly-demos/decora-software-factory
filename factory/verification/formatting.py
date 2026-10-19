"""Safe fixes and deterministic formatting before task checkpoints."""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from factory.errors import FactoryError
from factory.tools.layer import ToolLayer

# Ruff's auto-fixable Pyflakes rules, including fixes that require --unsafe-fixes.
# Unsafe fixes are never applied; these diagnostics do not gate an unconfigured repo.
FIXABLE_F = 'F401,F504,F522,F523,F541,F601,F602,F632,F811,F841,F901'


def ruff_config(root: Path, path: str) -> Path | None:
    directory = (root / path).parent
    while directory.is_relative_to(root):
        for name in ('.ruff.toml', 'ruff.toml', 'pyproject.toml'):
            candidate = directory / name
            if candidate.is_file():
                if name != 'pyproject.toml' or 'ruff' in tomllib.loads(candidate.read_text()).get('tool', {}):
                    return candidate
        if directory == root:
            break
        directory = directory.parent
    return None


def ruff_options(root: Path, path: str) -> list[str]:
    config = ruff_config(root, path)
    return ['--config', str(config)] if config else ['--isolated']


def format_python(tools: ToolLayer, paths: list[str]) -> None:
    targets = list(dict.fromkeys(path for path in paths if path.endswith('.py')))
    groups: dict[tuple[str, ...], list[str]] = {}
    for path in targets:
        tools._resolve(path)
        groups.setdefault(tuple(ruff_options(tools.worktree, path)), []).append(path)
    for options, files in groups.items():
        defaults = ['--select', 'E9,F'] if options == ('--isolated',) else []
        fixed = tools.run([sys.executable, '-m', 'ruff', 'check', *options, *defaults,
                           '--fix', '--no-unsafe-fixes', '--output-format=concise', '--', *files])
        # Exit 1 means remaining diagnostics: the gate owns those, not task retries.
        if fixed.exit_code not in {0, 1}:
            raise FactoryError('Python safe fixes failed before verification:\n' + fixed.output)
        report = tools.run([sys.executable, '-m', 'ruff', 'format', *options, '--', *files])
        if not report.passed:
            raise FactoryError('Python formatting failed before verification:\n' + report.output)
