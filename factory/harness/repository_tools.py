"""Read-only SDK tools; no shell or mutation capability is exposed here."""
from __future__ import annotations

import json
from pathlib import Path

from factory.context.repo import RepositoryReader
from factory.errors import FactoryError
from factory.governance.policy import Policy


def repository_tools(root: Path, policy: Policy, *, observed: dict[str, list[str]] | None = None):
    from agents import function_tool

    reader = RepositoryReader(root, policy)
    calls = 0
    remaining = 100_000

    def bounded(operation):
        nonlocal calls, remaining
        calls += 1
        if calls > 30 or remaining <= 0:
            return 'Repository inspection budget exhausted. Return a plan or ask for clarification.'
        try:
            value = operation()
            result = value if isinstance(value, str) else json.dumps(value)
        except (FactoryError, OSError):
            return 'File unavailable or denied by repository policy.'
        result = result[:min(16000, remaining)]
        remaining -= len(result)
        return result

    @function_tool
    def list_files(pattern: str = '*') -> str:
        """List safe repo paths matching a glob, including outside task files; at most 150 results."""
        return bounded(lambda: reader.list_files(pattern))

    @function_tool
    def search_code(text: str) -> str:
        """Search safe repo source, including outside task files; returns paths and line numbers."""
        return bounded(lambda: reader.search(text))

    @function_tool
    def read_file(path: str, start_line: int = 1, line_count: int = 160) -> str:
        """Read exact current source anywhere in the repo (safe paths only), preserving newlines.

        Use 1-based start_line and at most 200 lines per call."""
        succeeded = False

        def read():
            nonlocal succeeded
            text = reader.read(path, start_line, line_count)
            succeeded = True
            return text

        result = bounded(read)
        if succeeded and observed is not None:
            observed.setdefault(path, []).append(result)
        return result

    return [list_files, search_code, read_file]
