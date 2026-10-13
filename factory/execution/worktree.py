from __future__ import annotations

import shutil
from pathlib import Path

from factory.errors import FactoryError
from factory.gitcmd import git
from factory.models import WorktreeInfo


class WorktreeManager:
    def __init__(self, root: Path) -> None:
        self.root = root

    def create(self, repo: Path, job_id: str) -> WorktreeInfo:
        repo = repo.resolve()
        if not (repo / ".git").exists():
            raise FactoryError(f"{repo} is not a git checkout")
        branch = f"factory/{job_id}"
        path = self.root / job_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._remove_one(repo, path, branch)
        base = git(repo, "rev-parse", "HEAD")
        if base.returncode != 0:
            raise FactoryError(base.stderr.strip() or "cannot read HEAD")
        added = git(repo, "worktree", "add", "-b", branch, str(path), "HEAD")
        if added.returncode != 0:
            detail = (added.stderr or added.stdout or "git worktree add failed").strip()
            raise FactoryError(detail)
        return WorktreeInfo(path=str(path), branch=branch, base_sha=base.stdout.strip())

    def destroy(self, repo: Path, info: WorktreeInfo) -> None:
        self._remove_one(repo.resolve(), Path(info.path), info.branch)

    def sweep(self, repo: Path) -> list[str]:
        """Remove leftover factory/job-* worktrees under this manager's root."""
        repo = repo.resolve()
        removed: list[str] = []
        listed = git(repo, "worktree", "list", "--porcelain")
        if listed.returncode != 0:
            return removed
        current_path = ""
        current_branch = ""
        records: list[tuple[str, str]] = []
        for line in listed.stdout.splitlines() + [""]:
            if line.startswith("worktree "):
                current_path = line.split(" ", 1)[1]
            elif line.startswith("branch "):
                current_branch = line.split(" ", 1)[1].removeprefix("refs/heads/")
            elif line == "":
                if current_path:
                    records.append((current_path, current_branch))
                current_path = ""
                current_branch = ""
        root = self.root.resolve()
        for path, branch in records:
            if not branch.startswith("factory/job-"):
                continue
            if not Path(path).resolve().is_relative_to(root):
                continue
            self._remove_one(repo, Path(path), branch)
            removed.append(branch)
        branches = git(repo, "branch", "--list", "factory/job-*")
        for line in branches.stdout.splitlines():
            name = line.strip().lstrip("* ").strip()
            if not name.startswith("factory/job-"):
                continue
            if name in removed:
                continue
            deleted = git(repo, "branch", "-D", name)
            if deleted.returncode == 0:
                removed.append(name)
        return removed

    def _remove_one(self, repo: Path, path: Path, branch: str) -> None:
        if path.exists():
            git(repo, "worktree", "remove", "--force", str(path))
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        git(repo, "worktree", "prune")
        git(repo, "branch", "-D", branch)
