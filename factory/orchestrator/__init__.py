"""Deterministic orchestrator: a Python state machine with SQLite job state.

A Temporal workflow can later call the same stage handlers as activities.
The handlers do not own retries, timeouts, or permissions.
"""

from factory.orchestrator.machine import Factory

__all__ = ["Factory"]
