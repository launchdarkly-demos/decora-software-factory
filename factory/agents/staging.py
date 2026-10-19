"""Parse and format the entire staged proposal before mutating the worktree."""
from __future__ import annotations

import ast
import json
import keyword
import re
import tempfile
import tomllib
from pathlib import Path

import yaml

from factory.command_output import strip_ansi
from factory.errors import FactoryError
from factory.tools.layer import ToolLayer
from factory.verification.formatting import format_python


def source_context(path: str, text: str, line: int, col: int, message: str) -> str:
    lines = text.splitlines()
    line, col = max(1, line), max(1, col)
    start = max(1, line - 10)
    end = min(len(lines), start + 20)
    result = [f'{path}:{line}:{col}: {message}']
    for number in range(start, end + 1):
        result.append(f'{number:>5} | {lines[number - 1]}')
        if number == line:
            result.append('      | ' + ' ' * (col - 1) + '^')
    if line > len(lines):
        result.extend([f'{line:>5} | ', '      | ' + ' ' * (col - 1) + '^'])
    return '\n'.join(result)


def validate_syntax(path: str, text: str) -> None:
    try:
        suffix = Path(path).suffix.lower()
        if suffix == '.py':
            ast.parse(text, filename=path)
        elif suffix == '.json':
            json.loads(text)
        elif suffix in {'.yaml', '.yml'}:
            yaml.safe_load(text)
        elif suffix == '.toml':
            tomllib.loads(text)
    except (SyntaxError, ValueError, yaml.YAMLError) as exc:
        line = getattr(exc, 'lineno', None) or 1
        col = getattr(exc, 'offset', None) or getattr(exc, 'colno', None) or 1
        message = getattr(exc, 'msg', None) or str(exc)
        mark = getattr(exc, 'problem_mark', None)
        if mark is not None:
            line, col = mark.line + 1, mark.column + 1
            message = getattr(exc, 'problem', None) or message
        # Python 3.11 TOMLDecodeError exposes its location in the message only.
        location = re.search(r'\(at line (\d+), column (\d+)\)', message)
        if location:
            line, col = map(int, location.groups())
        elif '(at end of document)' in message:
            line, col = len(text.splitlines()) + 1, 1
        raise FactoryError(source_context(path, text, line, col, message)) from None


def prepare_staged(tools: ToolLayer, staged: dict[str, str | None]) -> None:
    for path, text in staged.items():
        if text is None:
            continue
        if path.endswith('.py') and not tools._resolve(path).exists():
            for part in Path(path).with_suffix('').parts:
                if not part.isidentifier() or keyword.iskeyword(part):
                    suggested = re.sub(r'\W', '_', part)
                    if not suggested.isidentifier() or keyword.iskeyword(suggested):
                        suggested = 'module_' + suggested
                    raise FactoryError(f'{path}: invalid module or package name {part!r}; '
                                       f'suggested valid name: {suggested}.')
        validate_syntax(path, text)
    with tempfile.TemporaryDirectory(prefix='factory-staged-') as temp:
        root = Path(temp)
        paths = [path for path, text in staged.items() if text is not None and path.endswith('.py')]
        for path in paths:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(staged[path] or '')
        # Mirror in-repo configuration, including edits/deletions in this proposal.
        # Ancestor factory configuration must never enter the staging sandbox.
        configs = set()
        for path in paths:
            parent = Path(path).parent
            while True:
                for name in ('.ruff.toml', 'ruff.toml', 'pyproject.toml'):
                    rel = str(parent / name)
                    if rel in staged or tools._resolve(rel).is_file():
                        configs.add(rel)
                if parent == Path('.'):
                    break
                parent = parent.parent
        pending = list(configs)
        while pending:
            rel = pending.pop()
            text = staged.get(rel) if rel in staged else tools.read(rel)
            if text is None:
                continue
            destination = root / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text)
            data = tomllib.loads(text)
            settings = data.get('tool', {}).get('ruff', {}) if Path(rel).name == 'pyproject.toml' else data
            if extend := settings.get('extend'):
                extended = (tools.worktree / rel).parent / extend
                try:
                    extended_rel = str(extended.resolve().relative_to(tools.worktree))
                except ValueError:
                    raise FactoryError(f'{rel}: Ruff extend must stay inside the worktree.') from None
                tools._resolve(extended_rel)
                if extended_rel not in configs:
                    configs.add(extended_rel)
                    pending.append(extended_rel)
        try:
            format_python(ToolLayer(root, tools.policy), paths)
        except FactoryError as exc:
            message = strip_ansi(str(exc))
            contexts = []
            for path in paths:
                location = re.search(re.escape(path) + r':(\d+):(\d+)', message)
                line, col = map(int, location.groups()) if location else (1, 1)
                contexts.append(source_context(path, staged[path] or '', line, col, message))
            raise FactoryError('\n'.join(contexts) or message) from None
        for path in paths:
            text = (root / path).read_text()
            validate_syntax(path, text)
            staged[path] = text
