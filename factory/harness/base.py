from __future__ import annotations

from typing import Protocol

from factory.models import BuildResult, ImplementationPlan, JobState, RepoContext, ReviewReport
from factory.tools.layer import ToolLayer


class AgentHarness(Protocol):
    """Specialists only reason. The tool layer applies edits and runs commands."""

    def plan(self, job: JobState, context: RepoContext) -> ImplementationPlan: ...

    def build(self, job: JobState, tools: ToolLayer) -> BuildResult: ...

    def review(self, job: JobState, tools: ToolLayer) -> ReviewReport: ...
