from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from factory.errors import GovernanceError
from factory.governance.policy import Policy, guarded_rollout_required, selected_flag_key
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
        body = render_pr(job, diff, self.policy)
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
        # Reconcile by stable head branch before creating anything. Activities may
        # be retried after GitHub succeeded but its response was lost.
        repo = self._run(job, ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]).strip()
        if not repo or len(repo.split("/")) != 2:
            raise GovernanceError("cannot identify publishing repository")
        existing = self._existing(job, repo)
        self._run(job, ["git", "push", "-u", "origin", f"HEAD:refs/heads/{job.worktree.branch}"])
        existing = existing or self._existing(job, repo)
        if existing:
            draft.url = existing
        else:
            draft.url = self._run(job, [
                "gh", "pr", "create", "--repo", repo, "--draft", "--title", title,
                "--body-file", str(body_path), "--head", job.worktree.branch,
                "--base", job.worktree.base_branch,
            ]).strip()
        draft.dry_run = False
        (run_dir / "pr.json").write_text(draft.model_dump_json(indent=2) + "\n")
        return draft


    def _run(self, job: JobState, argv: list[str]) -> str:
        assert job.worktree is not None
        result = self.runner(argv, cwd=job.worktree.path, check=False, capture_output=True, text=True)
        if result.returncode:
            # A transport failure can occur after a successful side effect. Let
            # Temporal retry and reconcile; never include provider output/secrets.
            raise PublicationError(f"Publication command failed: {argv[0]}")
        return result.stdout or ""

    def _existing(self, job: JobState, repo: str) -> str | None:
        assert job.worktree is not None
        items = json.loads(self._run(job, [
            "gh", "pr", "list", "--repo", repo, "--head", job.worktree.branch,
            "--state", "all", "--json", "url,isDraft,state,baseRefName",
        ]))
        if not items:
            return None
        if len(items) != 1:
            raise GovernanceError("multiple PRs exist for this factory branch")
        item = items[0]
        if not item["isDraft"] or item["state"] != "OPEN" or item["baseRefName"] != job.worktree.base_branch:
            raise GovernanceError("existing factory PR is not an open draft against the expected base")
        return str(item["url"])


class PublicationError(Exception):
    """Retryable publication transport failure; reconcile remote state on retry."""


def _flag_lines(job: JobState) -> list[str]:
    record = job.launchdarkly
    if record is not None and record.url:
        return [f"- [LaunchDarkly flag]({record.url})"]
    if record is not None and record.required and record.mode == "offline":
        return ["- Flag was not provisioned/verified in LaunchDarkly (`--ld offline` or no API token)."]
    return []


def render_pr(job: JobState, diff: str, policy: Policy | None = None) -> str:
    plan = job.plan
    review = job.review
    flag_key = selected_flag_key(job)
    if job.launchdarkly is not None and not job.launchdarkly.required:
        flag_key = None
    flag_section = (["- No feature flag required for this change."]
                    if not flag_key else [
                        f"- Behind `{flag_key}`; code default: "
                        + ("on." if (plan.flag_default if plan else job.feature.flag_default) else "off."),
                        "- Enabling the feature is a separate human release decision.",
                        *_flag_lines(job)])
    if policy and guarded_rollout_required(policy, job):
        rollout = policy.guarded_rollout
        flag_section.extend([
            f"- Guarded rollout metric: `{plan.guarded_rollout_metric if plan else ''}`.",
            f"- Stages (%): {', '.join(map(str, rollout.stages))}; minimum sample size: {rollout.min_sample_size}.",
            f"- Rollback on regression: {str(rollout.rollback_on_regression).lower()}.",
            "- Flag must remain off in production. A human starts the guarded rollout; the factory does not start it.",
        ])
    lines = [
        f"# {job.feature.title}",
        "",
        "Draft pull request. Do not merge.",
        "",
        "This draft was produced by the Decora software factory. A human must review and merge it.",
        "",
        "## Summary",
        "",
        *([f"Related issue: {job.feature.issue_url}", ""] if job.feature.issue_url else []),
        job.feature.summary,
        "",
        "## Rollout",
        "",
        *flag_section,
        *([f"- Rollout rationale: {plan.flag_rationale}"] if plan and plan.flag_rationale and flag_key else []),
        "",
        "## Implementation plan",
        "",
    ]
    if plan:
        for step in plan.steps:
            lines.append(f"- {step}")
        if plan.tasks:
            lines.extend(["", "Planned tasks:"])
            for task in plan.tasks:
                lines.append(f"- {task.id}: {task.description} (depends on: {', '.join(task.depends_on) or 'none'})")
        if plan.requires_migration:
            lines.extend(["", "Migration and recovery:", "", plan.migration_plan])
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
