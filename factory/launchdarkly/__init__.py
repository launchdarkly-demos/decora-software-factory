"""LaunchDarkly flag assurance for factory jobs.

The builder edits the worktree. This package is what talks to LaunchDarkly.
"""

from factory.launchdarkly.flags import LaunchDarklyService

__all__ = ["LaunchDarklyService"]
