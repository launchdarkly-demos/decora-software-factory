"""Create a tiny disposable Decora-shaped target inside this repo only."""
from __future__ import annotations

import subprocess
from pathlib import Path

from factory.agents.recipes import CAPABILITIES_RETURN, FLAGS_NEEDLE
from factory.gitcmd import git_env


def init_repo(path: Path, *, capabilities: bool = True) -> Path:
    path.mkdir(parents=True)
    (path / "app").mkdir()
    (path / "app" / "__init__.py").write_text("")
    (path / "app" / "flags.py").write_text(
        FLAGS_NEEDLE + "\n\ndef get_flag(key, context, default=False):\n    return default\n"
    )
    if capabilities:
        server = (
            "from app.flags import BOARD_INTAKE_FLAG, get_flag\n\n\n"
            "def build_context(key: str) -> dict:\n"
            '    return {"key": key}\n\n\n'
            'def capabilities(context_key: str = "anonymous") -> dict:\n'
            "    context = build_context(context_key)\n"
            + CAPABILITIES_RETURN
        )
    else:
        server = 'def capabilities() -> dict:\n    return {"ok": True}\n'
    (path / "server.py").write_text(server)
    (path / "README.md").write_text("mini decora\n")
    env = git_env()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, env=env, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        env=env,
        capture_output=True,
    )
    return path


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / ".factory" / "demo-target"
    if not target.exists():
        init_repo(target)
    print(f"Demo fixture: {target}")
