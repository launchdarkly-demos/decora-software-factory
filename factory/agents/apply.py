from __future__ import annotations

from pathlib import Path

from factory.errors import FactoryError, GovernanceError, PlanScopeError
from factory.models import BuildProposal, BuildResult, JobState
from factory.tools.layer import ToolLayer
from factory.verification.formatting import format_python


def apply_proposal(job: JobState, tools: ToolLayer, proposal: BuildProposal) -> BuildResult:
    if job.worktree is None:
        raise RuntimeError("build requires a worktree")
    if job.plan is None:
        raise GovernanceError("build requires an approved plan")
    if not proposal.edits:
        raise FactoryError("Builder returned no edits. Read the approved files and return concrete changes.")
    # Validate every path before scope corrections, so unsafe edits remain fatal.
    for edit in proposal.edits:
        if ".." in Path(edit.path).parts:
            raise GovernanceError(f"path traversal is not allowed: {edit.path}")
        path = tools._resolve(edit.path)
        tools.policy.check_path(path.relative_to(tools.worktree).as_posix())
    # New package directories may need initializers at each new ancestor level.
    # Save these approvals in the plan so later Builder attempts can edit them too.
    initializers: set[str] = set()
    for rel in job.plan.files:
        if Path(rel).suffix != ".py":
            continue
        parent = Path(rel).parent
        while parent != Path(".") and not tools._resolve(parent.as_posix()).exists():
            initializer = (parent / "__init__.py").as_posix()
            tools._resolve(initializer)
            initializers.add(initializer)
            parent = parent.parent
    added = sorted(initializers - set(job.plan.files))
    job.auto_approved_initializers.extend(added)
    job.plan.files.extend(added)
    for edit in proposal.edits:
        if edit.path not in job.plan.files:
            raise PlanScopeError(
                f"Edit outside approved plan: {edit.path}. Use only these approved files: "
                + ", ".join(job.plan.files)
            )
    # Stage edits in memory, in order. A bad later anchor must not leave partial edits.
    staged: dict[str, str] = {}
    for edit in proposal.edits:
        path = tools._resolve(edit.path)
        for value in (edit.old, edit.new, edit.content):
            if any(marker in value for marker in (
                "[excerpt; use read_file for more]", "[omitted; use read_file for exact text]"
            )):
                raise FactoryError(f"{edit.path}: preview markers are not source code. "
                                   "Read exact source with read_file and use a unique verbatim anchor.")
        if edit.kind == "write":
            # Full replacements are safe within the approved, path-checked scope.
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
    format_python(tools, list(staged))
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
