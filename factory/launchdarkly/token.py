"""Resolve a LaunchDarkly API token without ever logging it."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from factory.dotenv import read_dotenv_key

TOKEN_ENV = "LAUNCHDARKLY_API_TOKEN"


def resolve_token(
    target_repo: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[str | None, str]:
    """Return ``(token, source)``.

    Source is ``env``, ``target .env``, or ``missing``. Callers may print the
    source. They must not print the token.
    """
    envmap = os.environ if environ is None else environ
    from_env = envmap.get(TOKEN_ENV, "").strip()
    if from_env:
        return from_env, "env"
    found = read_dotenv_key(target_repo / ".env", TOKEN_ENV)
    if found:
        return found, "target .env"
    return None, "missing"


def scrub(text: str, secret: str | None) -> str:
    if not secret or secret not in text:
        return text
    return text.replace(secret, "[redacted]")

