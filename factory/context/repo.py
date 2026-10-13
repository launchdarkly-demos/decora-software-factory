from __future__ import annotations

import re
from pathlib import Path

from factory.models import FeatureRequest, RepoContext, Snippet

SKIP_DIRS = {
    ".git",
    ".claude",
    "venv",
    ".venv",
    "node_modules",
    "__pycache__",
    ".factory",
    "data",
    "web",
    "worktrees",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".toml", ".json"}


def analyze_repo(root: Path, feature: FeatureRequest) -> RepoContext:
    """Score files against the request. Never reads the PIM catalog."""
    root = root.resolve()
    keywords = _keywords(feature)
    scored: list[tuple[int, str]] = []
    for path in _files(root):
        rel = path.relative_to(root).as_posix()
        if rel == "app/pim/catalog.json" or rel.startswith("app/pim/"):
            continue
        score = _score(rel, path, keywords)
        if score > 0 or rel in {"server.py", "app/flags.py"}:
            scored.append((score + (5 if rel in {"server.py", "app/flags.py"} else 0), rel))
    scored.sort(key=lambda item: (-item[0], item[1]))
    top = [rel for _, rel in scored[:8]]
    snippets: list[Snippet] = []
    for rel in top[:4]:
        text = _read_head(root / rel)
        if text:
            snippets.append(Snippet(path=rel, text=text))
    server = root / "server.py"
    flags = root / "app" / "flags.py"
    has_route = False
    if server.exists():
        has_route = "/api/capabilities" in server.read_text(errors="replace")
    return RepoContext(
        root=str(root),
        top_files=top,
        snippets=snippets,
        has_flags_module=flags.exists(),
        has_capabilities_route=has_route,
    )


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if len(rel_parts) >= 2 and rel_parts[0] == "app" and rel_parts[1] == "pim":
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > 80_000:
            continue
        found.append(path)
        if len(found) >= 400:
            break
    return found


def _keywords(feature: FeatureRequest) -> set[str]:
    blob = " ".join([feature.title, feature.summary, feature.flag_key or "", *feature.hints])
    words = {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", blob)}
    words.update({"capabilities", "flag", "server"})
    return words


def _score(rel: str, path: Path, keywords: set[str]) -> int:
    haystack = rel.lower()
    score = sum(2 for word in keywords if word in haystack)
    if score == 0 and path.suffix == ".py":
        try:
            head = path.read_text(errors="replace")[:4000].lower()
        except OSError:
            return 0
        score = sum(1 for word in keywords if word in head)
    return score


def _read_head(path: Path) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[:30])[:1500]
