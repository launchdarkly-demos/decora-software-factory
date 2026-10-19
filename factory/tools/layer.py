from __future__ import annotations

import difflib
import os
import subprocess
import sys
from pathlib import Path

from factory.errors import FactoryError, SafetyError
from factory.gitcmd import git_env
from factory.governance.policy import Policy
from factory.models import CommandReport
from factory.tools.safety import check_content


class ToolLayer:
    """Allowlisted commands and path-checked edits. Cwd is always the worktree."""

    def __init__(self, worktree: Path, policy: Policy, *, target_repo: Path | None = None) -> None:
        self.worktree = worktree.resolve()
        self.policy = policy
        self.target_repo = target_repo.resolve() if target_repo else None

    def run(self, argv: list[str], *, output_limit: int = 20_000) -> CommandReport:
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
        except OSError as exc:
            return CommandReport(argv=argv, exit_code=127, output=f"Command unavailable: {exc.__class__.__name__}",
                                 passed=False)
        except subprocess.TimeoutExpired as exc:
            output = f"timed out after {self.policy.command_timeout_seconds}s\n{exc}"
            return CommandReport(argv=argv, exit_code=124, output=output, passed=False)
        output = (proc.stdout or "") + (proc.stderr or "")
        if len(output) > output_limit:
            output = "[output truncated]\n" + output[-output_limit:]
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
        with path.open(newline="") as stream:
            return stream.read()

    def write(self, rel: str, content: str) -> None:
        path = self._resolve(rel)
        check_content(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as stream:
            stream.write(content)

    def replace(self, rel: str, old: str, new: str) -> None:
        text = self.read(rel)
        count = text.count(old)
        if count != 1:
            raise FactoryError(_anchor_error(rel, text, old, count))
        self.write(rel, text.replace(old, new, 1))

    def commit(self, message: str, paths: list[str]) -> str:
        if not paths:
            raise FactoryError("refusing to commit with no paths")
        for rel in paths:
            self.policy.check_path(rel)
            added = self.run(["git", "add", "--", rel])
            if not added.passed:
                raise FactoryError(added.output.strip() or f"git add failed for {rel}")
        staged = self.run(["git", "diff", "--cached", "--quiet", "--", *paths])
        if staged.exit_code == 0:
            return self.head_sha()
        committed = self.run(["git", "commit", "--only", "-m", message, "--", *paths])
        if not committed.passed:
            raise FactoryError(committed.output.strip() or "git commit failed")
        return self.head_sha()

    def head_sha(self) -> str:
        result = self.run(["git", "rev-parse", "HEAD"])
        if not result.passed:
            raise FactoryError(result.output.strip() or "git rev-parse failed")
        return result.output.strip()

    def diff(self, base_sha: str) -> str:
        result = self.run(["git", "diff", "--no-ext-diff", "--no-textconv", base_sha, "HEAD"],
                          output_limit=200_000)
        if not result.passed:
            raise FactoryError("Cannot read the diff for verification and review")
        if result.output.startswith("[output truncated]"):
            raise FactoryError("Diff exceeds the 200,000-character review budget; split this change into smaller tasks")
        return result.output

    def changed_files(self, base_sha: str) -> list[str]:
        result = self.run(["git", "diff", "--no-renames", "--name-only", base_sha, "HEAD"])
        if not result.passed:
            return []
        return [line for line in result.output.splitlines() if line.strip()]

    def _resolve(self, rel: str) -> Path:
        if rel.startswith("/") or rel.startswith("~"):
            raise SafetyError(f"Safety violation: path outside the root on `{rel}`.")
        if ".." in Path(rel.replace("\\", "/")).parts:
            raise SafetyError(f"Safety violation: .. on `{rel}`.")
        self.policy.check_path(rel)
        unresolved = self.worktree / rel
        if any(part.is_symlink() for part in [unresolved, *unresolved.parents]
               if part.is_relative_to(self.worktree)):
            raise SafetyError(f"Safety violation: symlink path on `{rel}`.")
        path = unresolved.resolve()
        if not path.is_relative_to(self.worktree):
            raise SafetyError(f"Safety violation: symlink escape on `{rel}`.")
        self.policy.check_path(path.relative_to(self.worktree).as_posix())
        return path

    def _env(self) -> dict[str, str]:
        env = git_env()
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        # Do not let git walk out of the worktree into some other repository.
        env["GIT_CEILING_DIRECTORIES"] = str(self.worktree.parent)
        env.pop("PYTEST_ADDOPTS", None)
        env.pop("PYTHONPATH", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["DECORA_PERSIST"] = "0"
        env["PYTHON_DOTENV_DISABLED"] = "1"
        env.update(self.policy.command_environment)
        for key in ("LD_SDK_KEY", "LAUNCHDARKLY_SDK_KEY", "OPENAI_API_KEY", "LAUNCHDARKLY_API_TOKEN"):
            env.pop(key, None)
        return env


def _anchor_error(rel: str, text: str, old: str, count: int) -> str:
    if not old:
        return ("`replace` and `insert_*` need a non-empty unique anchor. "
                "To add code without one, use `append` or `prepend`.")
    if count:
        positions = []
        offset = 0
        for _ in range(count):
            offset = text.index(old, offset)
            positions.append(str(text.count("\n", 0, offset) + 1))
            offset += len(old)
        return (f"{rel}: anchor matched {count} times; line numbers: {', '.join(positions)}. "
                "Extend the anchor until it's unique.")
    anchor_lines = old.split("\n") or [old]
    # splitlines() silently eats controls such as \x1c and \x85; keep them visible.
    ranked = sorted(enumerate(text.split("\n"), 1), key=lambda pair: (
        -max(difflib.SequenceMatcher(None, line, pair[1]).ratio() for line in anchor_lines), pair[0]))[:3]
    nearest = "\n".join(f"{number}: {line.encode('unicode_escape').decode('ascii')}" for number, line in ranked)
    return (f"{rel}: anchor matched 0 times. Anchor (first 200 characters): {old[:200]!r}\n"
            f"Closest matching lines:\n{nearest}\nAnchors must occur exactly once in the current file.")
