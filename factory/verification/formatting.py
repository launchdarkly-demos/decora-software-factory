"""Deterministic formatting of generated Python before it is committed/tested."""
from __future__ import annotations

import re
import sys

from factory.errors import FactoryError
from factory.tools.layer import ToolLayer


def format_python(tools: ToolLayer, paths: list[str]) -> None:
    targets = list(dict.fromkeys(path for path in paths if path.endswith('.py')))
    if not targets:
        return
    for path in targets:
        tools._resolve(path)
    report = tools.run([sys.executable, '-m', 'ruff', 'format', '--isolated', '--', *targets])
    if not report.passed:
        message = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', report.output)
        raise FactoryError('Python formatting failed before verification:\n' + message)
