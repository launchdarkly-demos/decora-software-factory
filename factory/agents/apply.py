from __future__ import annotations

from factory.errors import FactoryError, GovernanceError
from factory.models import BuildProposal, BuildResult, JobState
from factory.tools.layer import ToolLayer


def apply_proposal(job: JobState, tools: ToolLayer, proposal: BuildProposal) -> BuildResult:
    if job.worktree is None:
        raise RuntimeError("build requires a worktree")
    if job.plan is None:
        raise GovernanceError("build requires an approved plan")
    if not proposal.edits:
        raise FactoryError("Builder returned no edits. Read the approved files and return concrete changes.")
    # Stage edits in memory, in order. A bad later anchor must not leave partial edits.
    staged: dict[str, str] = {}
    for edit in proposal.edits:
        if edit.path not in job.plan.files:
            raise GovernanceError(f"Edit outside approved plan: {edit.path}")
        path = tools._resolve(edit.path)
        for value in (edit.old, edit.new, edit.content):
            if any(marker in value for marker in (
                "[excerpt; use read_file for more]", "[omitted; use read_file for exact text]"
            )):
                raise FactoryError(f"{edit.path}: preview markers are not source code. "
                                   "Read exact source with read_file and use a unique verbatim anchor.")
        exists = edit.path in staged or path.exists()
        if edit.kind == "write":
            if exists:
                raise FactoryError(f"Use replace for existing file: {edit.path}")
            staged[edit.path] = edit.content
        else:
            text = staged[edit.path] if edit.path in staged else tools.read(edit.path)
            count = text.count(edit.old) if edit.old else 0
            if count != 1:
                raise FactoryError(f"{edit.path} anchor matched {count} times. "
                                   "Read the current file with read_file and choose a unique exact anchor.")
            staged[edit.path] = text.replace(edit.old, edit.new, 1)
    for rel, text in staged.items():
        tools.write(rel, text)
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
