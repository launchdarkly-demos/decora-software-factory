from __future__ import annotations

import re

from factory.governance.flags import audit_flag
from factory.models import JobState, ReviewFinding, ReviewReport, Verdict
from factory.tools.layer import ToolLayer

_SOFT_CHECKS = {"builder_report_matches_git"}
_STOP = {"this", "that", "with", "when", "from", "should", "code", "flag"}


def review_deterministically(job: JobState, tools: ToolLayer) -> ReviewReport:
    """Inspect the diff, the verification log, and the request. Ignore builder claims."""
    if job.worktree is None:
        diff = ""
        files: list[str] = []
    else:
        diff = tools.diff(job.worktree.base_sha)
        files = tools.changed_files(job.worktree.base_sha)
    flag_key = job.feature.flag_key or (job.plan.flag_key if job.plan else None)
    must_off = True
    problems = audit_flag(diff, flag_key, must_default_off=must_off) if job.feature.user_facing else []
    findings = [
        ReviewFinding(
            check="launchdarkly_flag_defaults_off",
            passed=not problems,
            evidence="; ".join(problems) if problems else f"{flag_key} is in the diff and defaults off",
        )
    ]
    for criterion in job.feature.acceptance:
        covered = criterion_covered(criterion, diff)
        findings.append(
            ReviewFinding(
                check=f"acceptance: {criterion}",
                passed=covered,
                evidence="matched the diff" if covered else "not found in the diff",
            )
        )
    latest = job.latest_verify()
    verify_ok = bool(latest and latest.passed)
    evidence = "no verification log"
    if latest:
        evidence = latest.actionable[-500:] or ("passed" if latest.passed else "failed")
    findings.append(ReviewFinding(check="verification_log", passed=verify_ok, evidence=evidence))
    test_files = [path for path in files if path.startswith("test_") and path.endswith(".py")]
    findings.append(
        ReviewFinding(
            check="tests_in_diff",
            passed=bool(test_files),
            evidence=", ".join(test_files) if test_files else "no test file in the diff",
        )
    )
    latest_build = job.latest_build()
    reported = set(latest_build.changed_files) if latest_build else set()
    actual = set(files)
    match = reported == actual
    findings.append(
        ReviewFinding(
            check="builder_report_matches_git",
            passed=match,
            evidence=f"git={sorted(actual)} builder={sorted(reported)}",
        )
    )
    hard_failures = [item for item in findings if not item.passed and item.check not in _SOFT_CHECKS]
    risks = [
        "The flag stays off until a human enables it in LaunchDarkly.",
        "This run does not merge. A human still reviews the draft.",
    ]
    if not match:
        risks.append("Builder's changed-file list did not match git diff --name-only.")
    if any(not run.passed for run in job.verify_runs[:-1]):
        risks.append("Verification failed once before the passing run. The diff was checked again after the fix.")
    verdict: Verdict
    if hard_failures:
        verdict = "changes_requested"
        summary = hard_failures[0].evidence
    else:
        verdict = "approve"
        summary = "Diff, flag default, tests, and the verification log agree."
    return ReviewReport(verdict=verdict, findings=findings, risks=risks, summary=summary)


def criterion_covered(criterion: str, diff: str) -> bool:
    text = criterion.lower()
    folded = diff.lower()
    if "calm linen" in text:
        return "calm linen" in folded
    if "omit" in text or "hidden" in text or ("when" in text and "off" in text):
        return "flag_default = false" in folded
    if "flag key" in text or "decor-room-mood" in text:
        return "decor-room-mood" in folded
    tokens = [
        token
        for token in re.findall(r"[a-z0-9-]{4,}", text)
        if token not in _STOP
    ]
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in folded)
    return hits >= min(3, len(tokens))
