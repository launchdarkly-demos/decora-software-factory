from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from factory.agents.staging import prepare_staged
from factory.errors import DestructiveEditError, FactoryError, GovernanceError, SafetyError
from factory.gitcmd import git
from factory.models import BuildProposal, BuildResult, JobState
from factory.task_queue import current_task
from factory.tools.anchors import anchor_span
from factory.tools.layer import ToolLayer
from factory.tools.safety import check_content, redact_secrets
from factory.verification.commands import pytest_targets

# Base files shorter than this are cheap to restore and are often rewritten wholesale.
DESTRUCTIVE_MIN_BASE_LINES = 80


def apply_proposal(job: JobState, tools: ToolLayer, proposal: BuildProposal) -> BuildResult:
    checkpoint = tools.head_sha()
    try:
        return _apply_proposal(job, tools, proposal)
    except (FactoryError, OSError, ValueError) as exc:
        if isinstance(exc, DestructiveEditError):
            restored = git(tools.worktree, "restore", "--source=" + checkpoint, "--staged", "--worktree", "--", ".")
            if restored.returncode:
                raise FactoryError("Could not restore rejected proposal checkpoint") from exc
        if tools.receipt_dir is not None:
            tools.receipt_dir.mkdir(parents=True, exist_ok=True)
            record = {"job_id": job.id, "task_id": job.current_task_id, "builder_call": job.builder_calls,
                      "edits": [{"path": redact_secrets(edit.path), "kind": edit.kind,
                                 "anchor": redact_secrets(edit.old)} for edit in proposal.edits]}
            with (tools.receipt_dir / "rejected_edits.jsonl").open("a") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        raise


def _apply_proposal(job: JobState, tools: ToolLayer, proposal: BuildProposal) -> BuildResult:
    if job.worktree is None:
        raise RuntimeError("build requires a worktree")
    if job.plan is None:
        raise FactoryError("build requires an approved plan")
    check_proposal_safety(tools, proposal)
    task = current_task(job)
    if not proposal.edits and not proposal.task_complete:
        task_id = task.id if task else "T"
        raise FactoryError(f"No edits returned. Make concrete edits for task {task_id}, or set "
                           "`task_complete=true` if the task is already done at this checkpoint.")
    staged: dict[str, str | None] = {}
    modes: dict[str, int] = {}
    anchor_fallbacks: list[str] = []
    previous_edits: dict[str, object] = {}

    def current(rel: str) -> str:
        if rel in staged:
            value = staged[rel]
            if value is None:
                raise FactoryError(f"Conflicting edits: {previous_edits.get(rel)} followed by {edit}; "
                                   f"missing file: {rel} (deleted or moved earlier in this proposal)")
            return value
        return tools.read(rel)

    for edit in proposal.edits:
        path = tools._resolve(edit.path)
        if path.exists() and not path.is_file():
            raise FactoryError(f"{edit.path}: expected a file, found a directory; choose a file path.")
        for value in (edit.old, edit.new, edit.content):
            if re.search(r"\[(?:excerpt|omitted)[^\]]*\]", value, re.IGNORECASE):
                raise FactoryError("Content contains preview markers; read the exact source.")
        if edit.kind in {"write", "create"}:
            exists = staged[edit.path] is not None if edit.path in staged else path.exists()
            if edit.kind == "create" and exists:
                raise FactoryError(f"Conflicting edits: {previous_edits.get(edit.path, 'checkpoint file')} "
                                   f"and {edit}; create of an existing path.")
            if edit.path in staged and staged[edit.path] is None:
                raise FactoryError(f"Conflicting edits: {previous_edits.get(edit.path)} and {edit}; "
                                   "source was deleted or moved earlier.")
            staged[edit.path] = edit.content
        elif edit.kind == "delete":
            current(edit.path)  # A missing source is a correction, never a silent success.
            staged[edit.path] = None
        elif edit.kind == "move":
            destination = tools._resolve(edit.destination)
            if (edit.destination in staged and staged[edit.destination] is not None
                    or edit.destination not in staged and destination.exists()):
                raise FactoryError(f"Conflicting edits: {previous_edits.get(edit.destination, 'checkpoint file')} "
                                   f"and {edit}; {edit.destination}: move destination already exists.")
            staged[edit.destination] = current(edit.path)
            if edit.path in modes:
                modes[edit.destination] = modes[edit.path]
            elif path.exists():
                modes[edit.destination] = path.stat().st_mode
            staged[edit.path] = None
        else:
            kind: str = edit.kind
            if kind in {"replace", "insert_after", "insert_before"} and not edit.old:
                exists = staged[edit.path] is not None if edit.path in staged else path.exists()
                if edit.path in staged and staged[edit.path] is None:
                    current(edit.path)  # Preserve E8 for edits following a delete/move.
                text = current(edit.path) if exists else ""
                if kind == "replace" and text:
                    raise FactoryError(f"{edit.path}: empty replace anchor on a nonempty file; "
                                       "use a unique anchor or explicit whole-file write."
                                       + _whole_file_feedback(tools, job, edit.path, text))
                converted = {"insert_after": "append", "insert_before": "prepend", "replace": "write"}[kind]
                anchor_fallbacks.append(f"Empty-anchor conversion: {edit.path} {kind} -> {converted}")
                if converted == "write":
                    staged[edit.path] = edit.new
                    previous_edits[edit.path] = edit
                    continue
                kind = converted
            text = current(edit.path)
            if kind == "append":
                staged[edit.path] = text + edit.new
            elif kind == "prepend":
                staged[edit.path] = edit.new + text
            else:
                try:
                    start, end, fallback = anchor_span(edit.path, text, edit.old)
                except FactoryError as exc:
                    # Only missing (not ambiguous) replace anchors receive whole-file guidance.
                    shares_line = any(line.strip() and line.strip() in text
                                      for line in edit.old.splitlines())
                    if kind == "replace" and "matched 0 times" in str(exc) and shares_line:
                        raise FactoryError(str(exc) + _whole_file_feedback(tools, job, edit.path, text)) from None
                    raise
                original_anchor = text[start:end]
                replacement = (original_anchor + edit.new if edit.kind == "insert_after" else
                               edit.new + original_anchor if edit.kind == "insert_before" else edit.new)
                staged[edit.path] = text[:start] + replacement + text[end:]
                if fallback:
                    anchor_fallbacks.append(f"Anchor fallback used: {edit.path} original span [{start}:{end}]")
        previous_edits[edit.path] = edit
        if edit.kind == "move":
            previous_edits[edit.destination] = edit
    for rel, content in staged.items():
        if content is not None:
            _check_content_at(content, rel)
    check_destructive_edits(tools, staged, task.rewrite_files if task else [], job.worktree.base_sha)
    prepare_staged(tools, staged)
    check_destructive_edits(tools, staged, task.rewrite_files if task else [], job.worktree.base_sha)
    for rel, content in staged.items():
        if content is not None:
            _check_content_at(content, rel)
    # Validate every operation before touching disk or expanding the plan. Roll back
    # filesystem failures too. All parsing and formatting has already passed.
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
            job.auto_added_file_tasks[rel] = task.id if task else "T1"
        if task is not None:
            job.task_file_calls.setdefault(task.id, {})[rel] = job.builder_calls
        if task is not None and rel not in task.files:
            task.files.append(rel)
            job.task_original_files[task.id].append(rel)
        if staged_text is None and rel not in job.deleted_files:
            job.deleted_files.append(rel)
        elif staged_text is not None and rel in job.deleted_files:
            job.deleted_files.remove(rel)
    paths = [rel for rel, text in staged.items() if text is not None or originals[rel] is not None]
    task_id = task.id if task else "T1"
    part = len(job.checkpoint_shas.get(task_id, [])) + 1
    message = f"checkpoint {task_id}/{part}\n\n{proposal.commit_message}"
    if anchor_fallbacks:
        message += "\n\n" + "\n".join(anchor_fallbacks)
    sha = tools.commit(message, paths) if paths else tools.head_sha()
    changed = tools.changed_files(job.worktree.base_sha)
    return BuildResult(
        task_complete=proposal.task_complete,
        anchor_fallbacks=anchor_fallbacks,
        quick_test_commands=proposal.commands,
        commit=sha,
        changed_files=changed,
        summary=proposal.summary,
        attempt=len(job.builds) + 1,
        flag_default=proposal.flag_default,
    )


def check_proposal_safety(tools: ToolLayer, proposal: BuildProposal) -> None:
    # Validate the entire proposal before corrections or plan expansion.
    _check_content_at(proposal.commit_message, "<proposal>")
    _check_content_at(proposal.summary, "<proposal>")
    for index, edit in enumerate(proposal.edits):
        if not edit.path or edit.kind == "move" and not edit.destination:
            field = "path" if not edit.path else "destination"
            raise FactoryError(f"edits.{index}.{field}: required non-empty field. "
                               "Return one complete BuildProposal.")
        required = (["content"] if edit.kind in {"create", "write"} else
                    ["new"] if edit.kind in {"replace", "insert_before", "insert_after", "append", "prepend"} else [])
        for field in required:
            if field not in edit.model_fields_set:
                raise FactoryError(f"edits.{index}.{field}: required field. Return one complete BuildProposal.")
        for rel in (edit.path, edit.destination) if edit.kind == "move" else (edit.path,):
            if not rel or Path(rel).as_posix() != rel or "\\" in rel:
                raise SafetyError(f"Safety violation: noncanonical path on `{rel}`.")
            tools._resolve(rel)
        for value in (edit.content, edit.old, edit.new):
            _check_content_at(value, edit.path)
    pytest_targets(proposal.commands)
    for command in proposal.commands:
        _check_content_at(" ".join(command), "<proposal>")
        try:
            tools.policy.check_command(command)
        except GovernanceError as exc:
            raise FactoryError(str(exc)) from None


def _check_content_at(content: str, path: str) -> None:
    try:
        check_content(content)
    except GovernanceError:
        raise SafetyError(f"Safety violation: credential-like content in `{path}`.") from None


def check_destructive_edits(
    tools: ToolLayer, staged: dict[str, str | None], rewrite_files: list[str], base_sha: str,
) -> None:
    """Count proposal removals only where checkpoint lines match the job base."""
    for path, after in staged.items():
        if path in rewrite_files or not tools._resolve(path).is_file():
            continue
        base = git(tools.worktree, 'show', f'{base_sha}:{path}')
        if base.returncode:
            # Missing paths are exempt; other Git failures must not disable the guard.
            exists = git(tools.worktree, 'cat-file', '-e', base_sha + '^{commit}')
            listing = git(tools.worktree, 'ls-tree', base_sha, '--', path)
            if exists.returncode or listing.returncode or listing.stdout.strip():
                raise FactoryError(f'Cannot read base content for {path}')
            continue
        base_lines = base.stdout.splitlines()
        if len(base_lines) < DESTRUCTIVE_MIN_BASE_LINES:
            continue
        before = tools.read(path).splitlines()
        base_indices = {index for block in difflib.SequenceMatcher(
            None, base_lines, before, autojunk=False).get_matching_blocks()
            for index in range(block.b, block.b + block.size)}
        removed = [(start, end) for kind, start, end, _, _ in
                   difflib.SequenceMatcher(None, before, (after or '').splitlines(), autojunk=False).get_opcodes()
                   if kind in {'delete', 'replace'}]
        removed_base = [index for start, end in removed for index in range(start, end) if index in base_indices]
        count = len(removed_base)
        if count > len(base_lines) * 0.5:
            start = removed_base[0]
            head = '\n'.join(f'{index + 1:>5} | {before[index]}' for index in removed_base[:20])
            raise DestructiveEditError(
                f'E9: {path}:{start + 1}:1: destructive edit removes {count}/{len(base_lines)} lines '
                f'({count / len(base_lines):.0%}) of base content; keep existing lines and change only the needed '
                f'region using several smaller replace/insert_* edits. Head of removed region:\n{head}')


def _whole_file_feedback(tools: ToolLayer, job: JobState, path: str, text: str) -> str:
    assert job.worktree is not None
    base = git(tools.worktree, 'ls-tree', job.worktree.base_sha, '--', path)
    job_created = base.returncode == 0 and not base.stdout.strip()
    if len(text.splitlines()) < DESTRUCTIVE_MIN_BASE_LINES or job_created:
        return f"\nUse an explicit whole-file write after reviewing the full current file ({path}):\n{text}"
    return ''
