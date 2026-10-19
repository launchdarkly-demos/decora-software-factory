from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import cast, get_args

from factory.command_output import strip_ansi
from factory.errors import FactoryError
from factory.gitcmd import git_env
from factory.models import ChangeClass, FeatureRequest

_RECOGNIZED = frozenset({
    "goal", "summary", "acceptance criteria", "acceptance", "rollout", "flag", "flag key",
    "hints", "context", "change type", "scope and constraints", "verification", "user facing",
})
_HORIZONTAL_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})\s*$")
_CHANGE_CLASS_VALUES = frozenset(get_args(ChangeClass))


def check_issue_target(repo: Path, issue_url: str | None) -> None:
    from factory.gitcmd import git

    match = re.fullmatch(r"https://github.com/([^/]+/[^/]+)/issues/[0-9]+", issue_url or "")
    origin = git(repo, "remote", "get-url", "origin")
    remote = origin.stdout.strip()
    slug = remote.removeprefix("git@github.com:").removeprefix("https://github.com/").removesuffix(".git")
    if not match or origin.returncode or match[1].lower() != slug.lower():
        raise FactoryError("GitHub issue repository does not match the worker target origin")


def load_feature(path: Path) -> FeatureRequest:
    data = json.loads(path.read_text())
    return FeatureRequest.model_validate(data)


def parse_issue_body(
    title: str, body: str, *, url: str | None = None, labels: list[str] | None = None,
) -> FeatureRequest:
    """Read a small convention from an issue body. No network."""
    sections = _parse_sections(body)
    by_key = _merge_recognized(sections)
    goal = by_key.get("goal", "").strip()
    summary_section = by_key.get("summary", "").strip()
    preamble = next((text for key, _heading, text in sections if key == "_preamble" and text), "")
    summary = goal or summary_section or preamble or title
    acceptance = _bullets(by_key.get("acceptance criteria", by_key.get("acceptance", "")))
    rollout = by_key.get("rollout", "").strip()
    flag_key, flag_default = _flag(rollout + "\n" + by_key.get("flag", "")
                                   + "\nkey: " + by_key.get("flag key", ""))
    impact = next((line.partition(":")[2].strip() for line in rollout.splitlines()
                   if line.partition(":")[0].strip().lower() == "user-facing"),
                  by_key.get("user facing", "no"))
    user_facing = _yes(impact)
    change_type = by_key.get("change type", "").strip() or "unspecified"
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
        hints=_hints(sections, preamble_used_as_summary=bool(preamble) and summary == preamble),
        labels=labels or [],
        change_class=_change_class(change_type),
        change_type=change_type,
        scope=by_key.get("scope and constraints", "").strip(),
        verification=by_key.get("verification", "").strip(),
        rollout=rollout,
    )


def load_github_issue(slug: str, number: int) -> FeatureRequest:
    """Fetch one issue with `gh`. The demo path uses a local JSON spec instead."""
    proc = subprocess.run(
        ["gh", "issue", "view", str(number), "--repo", slug, "--json", "title,body,url,labels"],
        check=False,
        capture_output=True,
        env=git_env(),
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "gh issue view failed").strip()
        raise FactoryError(strip_ansi(detail))
    payload = json.loads(strip_ansi(proc.stdout))
    return parse_issue_body(payload.get("title", ""), payload.get("body", ""), url=payload.get("url"),
                            labels=[item["name"] for item in payload.get("labels", [])])


def _parse_sections(body: str) -> list[tuple[str, str | None, str]]:
    """Ordered (key, original_heading|None, text). key is lower heading, '_preamble', or '_footer'."""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    current_key = "_preamble"
    current_heading: str | None = None
    buckets: list[tuple[str, str | None, list[str]]] = [(current_key, current_heading, [])]
    in_fence: str | None = None
    for line in body.splitlines():
        if in_fence is None:
            fence = _fence_marker(line)
            if fence is not None:
                in_fence = fence
                buckets[-1][2].append(line)
                continue
            if line.startswith("## "):
                heading = line[3:].strip()
                current_key = heading.lower()
                current_heading = heading
                buckets.append((current_key, current_heading, []))
                continue
            if _HORIZONTAL_RULE.match(line):
                current_key = "_footer"
                current_heading = None
                buckets.append((current_key, current_heading, []))
                continue
            buckets[-1][2].append(line)
            continue
        if line.startswith(in_fence):
            in_fence = None
        buckets[-1][2].append(line)
    return [(key, heading, "\n".join(lines).strip()) for key, heading, lines in buckets]


def _fence_marker(line: str) -> str | None:
    if line.startswith("```"):
        return "```"
    if line.startswith("~~~"):
        return "~~~"
    return None


def _merge_recognized(sections: list[tuple[str, str | None, str]]) -> dict[str, str]:
    merged: dict[str, list[str]] = {}
    for key, _heading, text in sections:
        if key in _RECOGNIZED and text:
            merged.setdefault(key, []).append(text)
    return {key: "\n".join(parts) for key, parts in merged.items()}


def _hints(
    sections: list[tuple[str, str | None, str]],
    *,
    preamble_used_as_summary: bool,
) -> list[str]:
    hints: list[str] = []
    for key, heading, text in sections:
        if not text:
            continue
        if key == "_preamble":
            if not preamble_used_as_summary:
                hints.append(text)
            continue
        if key == "_footer":
            hints.append(f"Footer: {text}")
            continue
        if key == "hints":
            hints.extend(_bullets(text))
            continue
        if key == "context":
            hints.append(text)
            continue
        if key in _RECOGNIZED:
            continue
        label = heading or key
        hints.append(f"{label}: {text}")
    return hints


def _change_class(text: str) -> ChangeClass | None:
    for token in re.findall(r"[A-Za-z0-9_]+", text):
        normalized = token.lower()
        if normalized in _CHANGE_CLASS_VALUES:
            return cast(ChangeClass, normalized)
    return None


def _bullets(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            item = re.sub(r"^\[[ xX]\]\s*", "", stripped[2:]).strip()
            if item:
                items.append(item)
    return items


def _flag(text: str) -> tuple[str | None, bool]:
    keys: list[str] = []
    default = False
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if name in {"key", "flag key"} and value and value != "replace-with-your-flag-key":
            keys.append(value)
        if name == "default":
            default = value.lower() in {"on", "true", "yes", "1"}
    return (", ".join(dict.fromkeys(keys)) or None), default


def _yes(text: str) -> bool:
    token = text.strip().lower()
    if token in {"no", "false", "off"}:
        return False
    return True
