from __future__ import annotations

import subprocess
from pathlib import Path

from factory.errors import FactoryError, GovernanceError
from factory.gitcmd import git_env
from factory.governance.policy import Policy
from factory.models import CommandReport


class ToolLayer:
    """Allowlisted commands and path-checked edits. Cwd is always the worktree."""

    def __init__(self, worktree: Path, policy: Policy, *, target_repo: Path | None = None) -> None:
        self.worktree = worktree.resolve()
        self.policy = policy
        self.target_repo = target_repo.resolve() if target_repo else None

    def run(self, argv: list[str]) -> CommandReport:
        self.policy.check_command(argv)
        try:
            proc = subprocess.run(
                argv,
                cwd=self.worktree,
                check=False,
                capture_output=True,
                text=True,
                env=self._env(),
                timeout=self.policy.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            output = f"timed out after {self.policy.command_timeout_seconds}s\n{exc}"
            return CommandReport(argv=argv, exit_code=124, output=output, passed=False)
        output = (proc.stdout or "") + (proc.stderr or "")
        if len(output) > 20_000:
            output = output[-20_000:]
        return CommandReport(
            argv=argv,
            exit_code=proc.returncode,
            output=output,
            passed=proc.returncode == 0,
        )

    def read(self, rel: str) -> str:
        path = self._resolve(rel)
        if not path.exists():
            raise FactoryError(f"missing file: {rel}")
        return path.read_text()

    def write(self, rel: str, content: str) -> None:
        path = self._resolve(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def replace(self, rel: str, old: str, new: str) -> None:
        text = self.read(rel)
        count = text.count(old)
        if count != 1:
            raise FactoryError(f"{rel} anchor matched {count} times")
        self.write(rel, text.replace(old, new, 1))

    def commit(self, message: str, paths: list[str]) -> str:
        if not paths:
            raise FactoryError("refusing to commit with no paths")
        for rel in paths:
            self.policy.check_path(rel)
            added = self.run(["git", "add", "--", rel])
            if not added.passed:
                raise FactoryError(added.output.strip() or f"git add failed for {rel}")
        committed = self.run(["git", "commit", "-m", message])
        if not committed.passed:
            raise FactoryError(committed.output.strip() or "git commit failed")
        return self.head_sha()

    def head_sha(self) -> str:
        result = self.run(["git", "rev-parse", "HEAD"])
        if not result.passed:
            raise FactoryError(result.output.strip() or "git rev-parse failed")
        return result.output.strip()

    def diff(self, base_sha: str) -> str:
        result = self.run(["git", "diff", base_sha, "HEAD"])
        if not result.passed:
            return ""
        return result.output

    def changed_files(self, base_sha: str) -> list[str]:
        result = self.run(["git", "diff", "--name-only", base_sha, "HEAD"])
        if not result.passed:
            return []
        return [line for line in result.output.splitlines() if line.strip()]

    def _resolve(self, rel: str) -> Path:
        if rel.startswith("/") or rel.startswith("~"):
            raise GovernanceError(f"path must stay inside the worktree: {rel}")
        self.policy.check_path(rel)
        path = (self.worktree / rel).resolve()
        if not path.is_relative_to(self.worktree):
            raise GovernanceError(f"path escapes the worktree: {rel}")
        return path

    def _env(self) -> dict[str, str]:
        env = git_env()
        # Do not let git walk out of the worktree into some other repository.
        env["GIT_CEILING_DIRECTORIES"] = str(self.worktree.parent)
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTHONPATH", None)
        env["DECORA_PERSIST"] = "0"
        for key in ("LD_SDK_KEY", "LAUNCHDARKLY_SDK_KEY"):
            env.pop(key, None)
        return env
