"""Execution environment: isolated git worktrees. Agents never touch the primary checkout."""

from factory.execution.worktree import WorktreeManager

__all__ = ["WorktreeManager"]
