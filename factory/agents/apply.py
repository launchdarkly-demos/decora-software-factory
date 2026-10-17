from __future__ import annotations

from factory.errors import FactoryError, GovernanceError
from factory.models import BuildProposal, BuildResult, JobState
from factory.task_queue import current_task
from factory.tools.layer import ToolLayer
from factory.tools.safety import check_content
from factory.verification.formatting import format_python


def apply_proposal(job: JobState, tools: ToolLayer, proposal: BuildProposal) -> BuildResult:
    if job.worktree is None:
        raise RuntimeError("build requires a worktree")
    if job.plan is None:
        raise GovernanceError("build requires an approved plan")
    if not proposal.edits:
        raise FactoryError("Builder returned no edits. Read the approved files and return concrete changes.")
    check_proposal_safety(tools, proposal)
    staged: dict[str, str] = {}
    for edit in proposal.edits:
        path = tools._resolve(edit.path)
        if path.exists() and not path.is_file():
            raise FactoryError(f"{edit.path}: expected a file, found a directory; choose a file path.")
        for value in (edit.old, edit.new, edit.content):
            if any(marker in value for marker in (
                "[excerpt; use read_file for more]", "[omitted; use read_file for exact text]"
            )):
                raise FactoryError(f"{edit.path}: preview markers are not source code. "
                                   "Read exact source with read_file and use a unique verbatim anchor.")
        if edit.kind == "write":
            # Path-checked full replacement is also valid for an existing file.
            staged[edit.path] = edit.content
        else:
            text = staged[edit.path] if edit.path in staged else tools.read(edit.path)
            count = text.count(edit.old) if edit.old else 0
            if count != 1:
                raise FactoryError(f"{edit.path} anchor matched {count} times. "
                                   "Read the current file with read_file and choose a unique exact anchor.")
            staged[edit.path] = text.replace(edit.old, edit.new, 1)
    for content in staged.values():
        check_content(content)
    task = current_task(job)
    for rel, text in staged.items():
        if rel not in job.plan.files:
            job.plan.files.append(rel)
            job.auto_added_files.append(rel)
        if task is not None and rel not in task.files:
            task.files.append(rel)
        tools.write(rel, text)
    correction = None
    try:
        format_python(tools, list(staged))
        for command in proposal.commands:
            report = tools.run(command)
            if not report.passed:
                raise FactoryError(report.output.strip() or "command failed")
    except GovernanceError:
        raise
    except FactoryError as exc:
        # Applied files are a checkpoint even when formatting or a quick command fails.
        # The next call repairs committed source rather than inheriting unrecorded edits.
        correction = str(exc)
    paths = []
    for edit in proposal.edits:
        if edit.path not in paths:
            paths.append(edit.path)
    sha = tools.commit(proposal.commit_message, paths)
    changed = tools.changed_files(job.worktree.base_sha)
    return BuildResult(
        task_complete=proposal.task_complete,
        correction=correction,
        commit=sha,
        changed_files=changed,
        summary=proposal.summary,
        attempt=len(job.builds) + 1,
        flag_default=proposal.flag_default,
    )


def check_proposal_safety(tools: ToolLayer, proposal: BuildProposal) -> None:
    # Validate the entire proposal before corrections or plan expansion.
    for edit in proposal.edits:
        tools._resolve(edit.path)
        for value in (edit.content, edit.old, edit.new):
            check_content(value)
    for command in proposal.commands:
        tools.policy.check_command(command)
