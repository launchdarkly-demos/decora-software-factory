from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Identity for commits this process creates. Does not touch global git config."""
    env = dict(base or os.environ)
    env["GIT_AUTHOR_NAME"] = "Decora Factory"
    env["GIT_AUTHOR_EMAIL"] = "factory@decora.local"
    env["GIT_COMMITTER_NAME"] = "Decora Factory"
    env["GIT_COMMITTER_EMAIL"] = "factory@decora.local"
    # Local process config only. Hooks still run; --no-verify is denied.
    env["GIT_CONFIG_COUNT"] = "3"
    env["GIT_CONFIG_KEY_0"] = "user.name"
    env["GIT_CONFIG_VALUE_0"] = "Decora Factory"
    env["GIT_CONFIG_KEY_1"] = "user.email"
    env["GIT_CONFIG_VALUE_1"] = "factory@decora.local"
    env["GIT_CONFIG_KEY_2"] = "commit.gpgsign"
    env["GIT_CONFIG_VALUE_2"] = "false"
    return env


def git(repo: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Privileged git for the execution environment. Agents do not call this."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        env=git_env(),
        timeout=timeout,
    )
