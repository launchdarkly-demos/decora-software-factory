from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from factory.errors import GovernanceError
from factory.governance.policy import Policy
from factory.models import JobState, PullRequestDraft

Runner = Callable[..., subprocess.CompletedProcess[str]]


class Publisher:
    def __init__(self, policy: Policy, *, push: bool = False, runner: Runner | None = None) -> None:
        self.policy = policy
        self.push = push
        self.runner = runner or subprocess.run

    def publish(self, job: JobState, diff: str, run_dir: Path) -> PullRequestDraft:
        run_dir.mkdir(parents=True, exist_ok=True)
        title = f"[factory] {job.feature.title}"
        body = render_pr(job, diff)
        body_path = run_dir / "pr-body.md"
        body_path.write_text(body)
        branch = job.worktree.branch if job.worktree else None
        draft = PullRequestDraft(
            title=title,
            body_path=str(body_path),
            dry_run=not self.push,
            branch=branch,
        )
        if not self.push:
            (run_dir / "pr.json").write_text(draft.model_dump_json(indent=2) + "\n")
            return draft
        if not self.policy.pull_request.allow_push:
            raise GovernanceError("policy pull_request.allow_push is false")
        if not self.policy.pull_request.draft_only:
            raise GovernanceError("factory only opens draft pull requests")
        if job.worktree is None:
            raise GovernanceError("no worktree to push")
        pushed = self.runner(
            ["git", "-C", job.worktree.path, "push", "-u", "origin", job.worktree.branch],
            check=False,
            capture_output=True,
            text=True,
        )
        if pushed.returncode != 0:
            raise GovernanceError((pushed.stderr or pushed.stdout or "git push failed").strip())
        created = self.runner(
            [
                "gh",
                "pr",
                "create",
                "--draft",
                "--title",
                title,
                "--body-file",
                str(body_path),
                "--head",
                job.worktree.branch,
            ],
            cwd=job.worktree.path,
            check=False,
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            raise GovernanceError((created.stderr or created.stdout or "gh pr create failed").strip())
        draft.url = (created.stdout or "").strip() or None
        draft.dry_run = False
        (run_dir / "pr.json").write_text(draft.model_dump_json(indent=2) + "\n")
        return draft


def render_pr(job: JobState, diff: str) -> str:
    plan = job.plan
    review = job.review
    lines = [
        f"# {job.feature.title}",
        "",
        "Draft pull request. Do not merge.",
        "",
        "This body was produced by the Decora software factory. The branch was not pushed.",
        "",
        "## Summary",
        "",
        job.feature.summary,
        "",
        "## Flag",
        "",
        f"- Key: `{job.feature.flag_key or (plan.flag_key if plan else None)}`",
        "- Code default: off",
        "- A human turns the flag on in LaunchDarkly. This pull request does not.",
        "",
        "## Implementation plan",
        "",
    ]
    if plan:
        for step in plan.steps:
            lines.append(f"- {step}")
        if plan.context_files:
            lines.append("")
            lines.append("Context files the planner saw:")
            for path in plan.context_files:
                lines.append(f"- `{path}`")
    lines.extend(["", "## Test evidence", ""])
    if not job.verify_runs:
        lines.append("No verification run was recorded.")
    for index, result in enumerate(job.verify_runs, start=1):
        state = "passed" if result.passed else "failed"
        lines.append(f"### Attempt {index}: {state} ({result.classification})")
        lines.append("")
        lines.append("```text")
        lines.append(result.actionable[-2500:])
        lines.append("```")
        lines.append("")
    lines.extend(["## Review findings", ""])
    if review is None:
        lines.append("No review was recorded.")
    else:
        lines.append(f"Verdict: **{review.verdict}**.")
        lines.append("")
        lines.append(review.summary)
        lines.append("")
        for finding in review.findings:
            mark = "pass" if finding.passed else "fail"
            lines.append(f"- [{mark}] {finding.check}: {finding.evidence}")
    lines.extend(["", "## Remaining risks", ""])
    risks = review.risks if review else ["Review did not run."]
    for risk in risks:
        lines.append(f"- {risk}")
    lines.extend(["", "## Retries", ""])
    if job.retries:
        for name, count in sorted(job.retries.items()):
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Diff",
            "",
            "The patch is saved next to this file as `diff.patch`.",
            f"Diff bytes: {len(diff.encode())}.",
            "",
        ]
    )
    return "\n".join(lines)
