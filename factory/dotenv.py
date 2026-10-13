"""Read single keys from a gitignored .env file. Never log the values."""

from __future__ import annotations

import os
from collections.abc import MutableMapping
from pathlib import Path

from factory.errors import FactoryError


def read_dotenv_key(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].strip()
        name, sep, raw = stripped.partition("=")
        if sep != "=" or name.strip() != key:
            continue
        value = raw.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        value = value.strip()
        if value:
            return value
    return None


def ensure_env_key(
    root: Path,
    key: str,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> str:
    """Return ``env`` or ``project .env``.

    When ``key`` is missing from the environment, copy it from ``root / .env``.
    The value is not included in the return value.
    """
    envmap = os.environ if environ is None else environ
    if envmap.get(key, "").strip():
        return "env"
    found = read_dotenv_key(root / ".env", key)
    if not found:
        raise FactoryError(f"{key} is unset. Add it to .env in this repo, or pass --agents mock.")
    envmap[key] = found
    return "project .env"
