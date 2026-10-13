from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from factory.errors import FactoryError
from factory.models import FeatureRequest


def load_feature(path: Path) -> FeatureRequest:
    data = json.loads(path.read_text())
    return FeatureRequest.model_validate(data)


def parse_issue_body(title: str, body: str, *, url: str | None = None) -> FeatureRequest:
    """Read a small convention from an issue body. No network."""
    sections = _sections(body)
    summary = sections.get("summary", "").strip() or body.strip().splitlines()[0] if body.strip() else title
    acceptance = _bullets(sections.get("acceptance", ""))
    flag_key, flag_default = _flag(sections.get("flag", ""))
    user_facing = _yes(sections.get("user facing", "yes"))
    slug = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-") or "issue"
    return FeatureRequest(
        id=flag_key or slug,
        title=title.strip(),
        summary=summary,
        acceptance=acceptance,
        user_facing=user_facing,
        flag_key=flag_key,
        flag_default=flag_default,
        source="github_issue",
        issue_url=url,
    )


def load_github_issue(slug: str, number: int) -> FeatureRequest:
    """Fetch one issue with `gh`. The demo path uses a local JSON spec instead."""
    proc = subprocess.run(
        ["gh", "issue", "view", str(number), "--repo", slug, "--json", "title,body,url"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "gh issue view failed").strip()
        raise FactoryError(detail)
    payload = json.loads(proc.stdout)
    return parse_issue_body(payload.get("title", ""), payload.get("body", ""), url=payload.get("url"))


def _sections(body: str) -> dict[str, str]:
    current = "summary"
    buckets: dict[str, list[str]] = {current: []}
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower()
            buckets.setdefault(current, [])
            continue
        buckets.setdefault(current, []).append(line)
    return {name: "\n".join(lines).strip() for name, lines in buckets.items()}


def _bullets(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            items.append(stripped[2:].strip())
    return items


def _flag(text: str) -> tuple[str | None, bool]:
    key = None
    default = False
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if name == "key" and value:
            key = value
        if name == "default":
            default = value.lower() in {"on", "true", "yes", "1"}
    return key, default


def _yes(text: str) -> bool:
    token = text.strip().lower()
    if token in {"no", "false", "off"}:
        return False
    return True
