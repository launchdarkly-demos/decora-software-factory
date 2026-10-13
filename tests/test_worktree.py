import subprocess
from pathlib import Path

from factory.execution.worktree import WorktreeManager
from factory.gitcmd import git_env
from tests.support import init_repo


def test_worktree_is_isolated_and_removed(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    (repo / "README.md").write_text("primary stays\n")
    subprocess.run(
        ["git", "commit", "-am", "primary edit"],
        cwd=repo,
        check=True,
        env=git_env(),
        capture_output=True,
    )
    before = _status(repo)
    manager = WorktreeManager(tmp_path / "worktrees")
    info = manager.create(repo, "job-iso")
    work = Path(info.path)
    assert work.resolve() != repo.resolve()
    (work / "app" / "room_mood.py").write_text("FLAG_DEFAULT = False\n")
    assert not (repo / "app" / "room_mood.py").exists()
    assert _status(repo) == before

    manager.destroy(repo, info)
    assert not work.exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "factory/job-*"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert branches.stdout.strip() == ""
    assert _status(repo) == before


def test_sweep_removes_a_leftover_factory_branch(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    manager = WorktreeManager(tmp_path / "worktrees")
    info = manager.create(repo, "job-sweep")
    assert Path(info.path).exists()
    removed = manager.sweep(repo)
    assert "factory/job-sweep" in removed
    assert not Path(info.path).exists()


def _status(repo: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout
