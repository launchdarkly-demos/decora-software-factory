from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from factory.errors import GovernanceError
from factory.governance.policy import Policy
from factory.models import FeatureRequest, RepoContext, Snippet
from factory.tools.safety import check_content

SKIP_DIRS = {'.git', '.claude', '.codex', '.agents', '.aws', '.ssh', '.config', 'venv', '.venv', 'node_modules',
             '__pycache__', '.factory', 'data', 'worktrees', '.mypy_cache', '.pytest_cache',
             '.ruff_cache', 'dist', 'build', '.next', 'coverage', 'playwright-report', 'test-results'}
TEXT_SUFFIXES = {'.py', '.md', '.yml', '.yaml', '.toml', '.json', '.js', '.jsx', '.ts', '.tsx',
                 '.mjs', '.cjs', '.html', '.css', '.scss', '.vue', '.svelte', '.sql', '.txt', '.sh', '.ini', '.cfg'}
FRONTEND_SUFFIXES = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.html', '.css', '.scss', '.vue', '.svelte'}


class RepositoryReader:
    """Bounded read-only repository access, shared by discovery and agent tools."""

    def __init__(self, root: Path, policy: Policy | None = None):
        self.root = root.resolve()
        self.policy = policy or Policy()
        self.paths: list[str] = []
        for directory, dirs, files in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS
                             and not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                rel = (Path(directory) / name).relative_to(self.root).as_posix()
                try:
                    path = self.resolve(rel)
                except GovernanceError:
                    continue
                if path.suffix in TEXT_SUFFIXES or name in {'Makefile', 'Dockerfile'}:
                    if path.stat().st_size <= 512_000:
                        self.paths.append(rel)
                if len(self.paths) >= 3000:
                    return

    def resolve(self, rel: str) -> Path:
        relative = Path(rel)
        if relative.is_absolute() or '..' in relative.parts:
            raise GovernanceError('repository path must be relative and contained')
        lowered = rel.lower()
        if (any(part in SKIP_DIRS or part.startswith('.env') for part in relative.parts)
                or lowered.startswith('app/pim/') or any(word in lowered for word in ('secret', 'credential'))
                or relative.suffix in {'.pem', '.key'}):
            raise GovernanceError('repository path is excluded from agent context')
        self.policy.check_path(rel)
        path = self.root / relative
        if (any(part.is_symlink() for part in [path, *path.parents] if part.is_relative_to(self.root))
                or not path.resolve().is_relative_to(self.root)):
            raise GovernanceError('repository path escapes through a symlink')
        return path

    def source(self, rel: str) -> str:
        path = self.resolve(rel)
        if path.suffix not in TEXT_SUFFIXES and path.name not in {'Makefile', 'Dockerfile'}:
            raise GovernanceError('file is not readable source')
        with path.open(errors='replace', newline='') as stream:
            text = stream.read()
        check_content(text)
        return text

    def read(self, rel: str, start: int = 1, count: int = 160) -> str:
        if start < 1 or not 1 <= count <= 200:
            raise GovernanceError('read range must start at 1 or later and contain at most 200 lines')
        lines = self.source(rel).splitlines(keepends=True)
        return ''.join(lines[start - 1:start - 1 + count])[:16000]

    def list_files(self, pattern: str = '*') -> list[str]:
        return [path for path in self.paths if fnmatch.fnmatch(path, pattern)][:150]

    def search(self, text: str) -> list[str]:
        if not text.strip():
            return []
        hits = []
        for rel in self.paths:
            try:
                content = self.source(rel)
            except GovernanceError:
                continue
            for index, line in enumerate(content.splitlines(), 1):
                if text.lower() in line.lower():
                    hits.append(f'{rel}:{index}: {line[:240]}')
                if len(hits) >= 50:
                    return hits
        return hits


def analyze_repo(root: Path, feature: FeatureRequest, policy: Policy | None = None) -> RepoContext:
    reader = RepositoryReader(root, policy)
    words = _keywords(feature)
    scored = []
    for rel in reader.paths:
        try:
            content = reader.source(rel)
        except GovernanceError:
            continue
        score = sum(4 for word in words if word in rel.lower())
        score += sum(1 for word in words if word in content.lower())
        if rel in {'AGENTS.md', 'README.md', 'package.json', 'pyproject.toml', 'Makefile'}:
            score += 3
        scored.append((score, rel, content))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = scored[:16]
    snippets = [Snippet(path=rel, text=focused_excerpt(content, words)) for _, rel, content in chosen[:10]]
    server = next((content for _, rel, content in scored if rel == 'server.py'), '')
    return RepoContext(
        root=str(reader.root), top_files=[rel for _, rel, _ in chosen], snippets=snippets,
        has_flags_module='app/flags.py' in reader.paths, has_capabilities_route='/api/capabilities' in server,
        file_inventory=reader.paths,
        verification_profiles={name: profile.model_dump()
                               for name, profile in reader.policy.verification_profiles.items()},
    )


def _keywords(feature: FeatureRequest) -> set[str]:
    blob = ' '.join([feature.title, feature.summary, feature.scope, feature.verification,
                     *feature.acceptance, *feature.hints])
    return {word.lower() for word in re.findall(r'[A-Za-z][A-Za-z0-9_-]{3,}', blob)} - {
        'that', 'this', 'with', 'from', 'must', 'should', 'when', 'have', 'does', 'their', 'each', 'into', 'only'}


def focused_excerpt(text: str, words: set[str], limit: int = 6000) -> str:
    if len(text) <= limit:
        return text
    lines = text.splitlines()
    keep = set(range(min(15, len(lines))))
    for index, line in enumerate(lines):
        if any(word in line.lower() for word in words):
            keep.update(range(max(0, index - 3), min(len(lines), index + 12)))
        if sum(len(lines[i]) + 1 for i in keep) >= limit:
            break
    chunks = []
    prior = -1
    for index in sorted(keep):
        if index > prior + 1:
            chunks.append('... [omitted; use read_file for exact text] ...')
        chunks.append(lines[index])
        prior = index
    return '\n'.join(chunks)[:limit] + '\n... [excerpt; use read_file for more]'
