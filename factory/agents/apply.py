from __future__ import annotations

from pathlib import Path

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
    check_proposal_safety(tools, proposal)
    if not proposal.edits:
        raise FactoryError("Builder returned no edits. Read the approved files and return concrete changes.")
    task = current_task(job)
    if task is not None:
        unrelated = [rel for edit in proposal.edits for rel in (edit.path, edit.destination) if rel
                     and rel in job.plan.files and rel not in task.files]
        if unrelated:
            raise FactoryError(f"Implement only task {task.id}; these files belong to other tasks: "
                               + ", ".join(unrelated))
    staged: dict[str, str | None] = {}
    modes: dict[str, int] = {}

    def current(rel: str) -> str:
        if rel in staged:
            value = staged[rel]
            if value is None:
                raise FactoryError(f"missing file: {rel} (deleted earlier in this proposal)")
            return value
        return tools.read(rel)

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
            staged[edit.path] = edit.content
        elif edit.kind == "delete":
            current(edit.path)  # A missing source is a correction, never a silent success.
            staged[edit.path] = None
        elif edit.kind == "move":
            destination = tools._resolve(edit.destination)
            if (edit.destination in staged and staged[edit.destination] is not None
                    or edit.destination not in staged and destination.exists()):
                raise FactoryError(f"{edit.destination}: move destination already exists; choose a new path.")
            staged[edit.destination] = current(edit.path)
            if edit.path in modes:
                modes[edit.destination] = modes[edit.path]
            elif path.exists():
                modes[edit.destination] = path.stat().st_mode
            staged[edit.path] = None
        else:
            text = current(edit.path)
            if edit.kind == "append":
                staged[edit.path] = text + edit.new
            elif edit.kind == "prepend":
                staged[edit.path] = edit.new + text
            else:
                if edit.kind == "replace" and not edit.old:
                    raise FactoryError(f"{edit.path}: replace needs a non-empty unique anchor; "
                                       "to add new code use insert_after/append/prepend")
                count = text.count(edit.old) if edit.old else 0
                if count != 1:
                    raise FactoryError(f"{edit.path} anchor matched {count} times. "
                                       "Read the current file with read_file and choose a unique exact anchor.")
                replacement = (edit.old + edit.new if edit.kind == "insert_after" else
                               edit.new + edit.old if edit.kind == "insert_before" else edit.new)
                staged[edit.path] = text.replace(edit.old, replacement, 1)
    for content in staged.values():
        if content is not None:
            check_content(content)
    # Validate every operation before touching disk or expanding the plan. Roll back
    # filesystem failures too; formatting/command failures below deliberately checkpoint.
    originals = {rel: (tools._resolve(rel).read_bytes() if tools._resolve(rel).is_file() else None)
                 for rel in staged}
    original_modes = {rel: tools._resolve(rel).stat().st_mode for rel in staged if originals[rel] is not None}
    try:
        for rel, staged_text in staged.items():
            if staged_text is None:
                tools._resolve(rel).unlink(missing_ok=True)
            else:
                tools.write(rel, staged_text)
                if rel in modes:
                    tools._resolve(rel).chmod(modes[rel])
    except OSError:
        for rel, original in originals.items():
            path = tools._resolve(rel)
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
                path.chmod(original_modes[rel])
        raise
    for rel, staged_text in staged.items():
        if rel not in job.plan.files:
            job.plan.files.append(rel)
            job.auto_added_files.append(rel)
        if task is not None and rel not in task.files:
            task.files.append(rel)
        if staged_text is None and rel not in job.deleted_files:
            job.deleted_files.append(rel)
        elif staged_text is not None and rel in job.deleted_files:
            job.deleted_files.remove(rel)
    correction = None
    try:
        format_python(tools, [rel for rel, text in staged.items() if text is not None])
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
    sha = tools.commit(proposal.commit_message,
                       [rel for rel, text in staged.items() if text is not None or originals[rel] is not None])
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
    check_content(proposal.commit_message)
    check_content(proposal.summary)
    for edit in proposal.edits:
        for rel in (edit.path, edit.destination) if edit.kind == "move" else (edit.path,):
            if not rel or Path(rel).as_posix() != rel or "\\" in rel:
                raise GovernanceError(f"use a canonical repository-relative file path: {rel!r}")
            tools._resolve(rel)
        for value in (edit.content, edit.old, edit.new):
            check_content(value)
    for command in proposal.commands:
        check_content(" ".join(command))
        tools.policy.check_command(command)
