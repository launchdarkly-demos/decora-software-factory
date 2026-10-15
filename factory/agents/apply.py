from __future__ import annotations

from factory.errors import FactoryError, GovernanceError
from factory.models import BuildProposal, BuildResult, JobState
from factory.tools.layer import ToolLayer


def apply_proposal(job: JobState, tools: ToolLayer, proposal: BuildProposal) -> BuildResult:
    if job.worktree is None:
        raise RuntimeError("build requires a worktree")
    if job.plan is None:
        raise GovernanceError("build requires an approved plan")
    # Validate every edit before applying any; scope is enforced outside the model.
    for edit in proposal.edits:
        if edit.path not in job.plan.files:
            raise GovernanceError(f"Edit outside approved plan: {edit.path}")
        path = tools._resolve(edit.path)
        if edit.kind == "write" and path.exists():
            raise FactoryError(f"Use replace for existing file: {edit.path}")
    for edit in proposal.edits:
        if edit.kind == "write":
            tools.write(edit.path, edit.content)
        else:
            tools.replace(edit.path, edit.old, edit.new)
    for command in proposal.commands:
        report = tools.run(command)
        if not report.passed:
            raise FactoryError(report.output.strip() or "command failed")
    paths = []
    for edit in proposal.edits:
        if edit.path not in paths:
            paths.append(edit.path)
    sha = tools.commit(proposal.commit_message, paths)
    changed = tools.changed_files(job.worktree.base_sha)
    return BuildResult(
        commit=sha,
        changed_files=changed,
        summary=proposal.summary,
        attempt=len(job.builds) + 1,
        flag_default=proposal.flag_default,
    )
