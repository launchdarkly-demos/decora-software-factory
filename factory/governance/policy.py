from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from factory.errors import GovernanceError
from factory.models import ImplementationPlan, JobState


class ApprovalRules(BaseModel):
    auto_approve_risk: list[str] = Field(default_factory=lambda: ["low"])
    pause_on_ambiguous: bool = True
    pause_on_risk: list[str] = Field(default_factory=lambda: ["high"])


class FlagRules(BaseModel):
    required_for_user_facing: bool = True
    must_default_off: bool = True


class PullRequestRules(BaseModel):
    draft_only: bool = True
    allow_push: bool = False


class LaunchDarklyRules(BaseModel):
    project_key: str = "decor-agent-demo-v2"
    environments: list[str] = Field(default_factory=lambda: ["test", "production"])
    production_environment: str = "production"


class Policy(BaseModel):
    version: int = 1
    max_retries: dict[str, int] = Field(
        default_factory=lambda: {"verify": 2, "review": 1, "govern": 1, "plan": 2, "build": 3}
    )
    stage_timeout_seconds: dict[str, float] = Field(default_factory=dict)
    regression_tests: list[str] = Field(default_factory=list)
    command_environment: dict[str, str] = Field(default_factory=dict)
    command_timeout_seconds: float = 180
    allow_commands: list[str] = Field(
        default_factory=lambda: ["pytest", "ruff", "mypy", "python", "python3", "git"]
    )
    deny_command_patterns: list[str] = Field(
        default_factory=lambda: ["git push", "git reset", "--no-verify", "sudo"]
    )
    git_subcommands: list[str] = Field(
        default_factory=lambda: ["status", "diff", "add", "commit", "log", "show", "rev-parse"]
    )
    deny_paths: list[str] = Field(default_factory=lambda: [".env", "app/pim/**"])
    approvals: ApprovalRules = Field(default_factory=ApprovalRules)
    flags: FlagRules = Field(default_factory=FlagRules)
    launchdarkly: LaunchDarklyRules = Field(default_factory=LaunchDarklyRules)
    pull_request: PullRequestRules = Field(default_factory=PullRequestRules)

    def timeout_for(self, stage: str) -> float:
        return float(self.stage_timeout_seconds.get(stage, 120))

    def check_command(self, argv: list[str]) -> None:
        if not argv:
            raise GovernanceError("empty command")
        line = " ".join(argv)
        for pattern in self.deny_command_patterns:
            if pattern and pattern in line:
                raise GovernanceError(f"command denied by policy: {line}")
        name = Path(argv[0]).name
        if name not in self.allow_commands:
            raise GovernanceError(f"command not allowlisted: {name}")
        # `python -c` runs arbitrary code. `pytest -c` is a config path and stays allowed.
        if name in {"python", "python3"} and len(argv) > 1 and argv[1] == "-c":
            raise GovernanceError("python -c is not allowlisted")
        if name == "git":
            sub = _git_subcommand(argv)
            if sub not in self.git_subcommands:
                raise GovernanceError(f"git {sub} is not allowlisted")

    def check_path(self, rel: str) -> None:
        normalized = rel.replace("\\", "/").lstrip("/")
        if normalized == ".git" or normalized.startswith(".git/") or "/.git/" in f"/{normalized}":
            raise GovernanceError(f"path denied: {rel}")
        for pattern in self.deny_paths:
            if _path_matches(pattern, normalized):
                raise GovernanceError(f"path denied by policy: {rel}")


class Decision(BaseModel):
    allowed: bool = False
    pause: bool = False
    retry_build: bool = False
    reason: str = ""
    resume_stage: str | None = None


def load_policy(path: Path) -> Policy:
    data = yaml.safe_load(path.read_text()) or {}
    return Policy.model_validate(data)


def review_plan(policy: Policy, plan: ImplementationPlan, job: JobState) -> Decision:
    if policy.approvals.pause_on_ambiguous and not job.feature.acceptance and not job.human_answers:
        return Decision(
            pause=True,
            reason="What behavior and acceptance checks should this change implement, and which flag should gate it?",
            resume_stage="plan",
        )
    if plan.ambiguous and policy.approvals.pause_on_ambiguous:
        question = plan.questions[0] if plan.questions else "The plan is ambiguous."
        return Decision(pause=True, reason=question, resume_stage="plan")
    if plan.risk in policy.approvals.pause_on_risk:
        return Decision(
            pause=True,
            reason=f"Risk {plan.risk} needs a human before any code is written.",
            resume_stage="build",
        )
    if (
        job.feature.user_facing
        and policy.flags.required_for_user_facing
        and not plan.flag_key
    ):
        return Decision(
            pause=True,
            reason="User-facing work needs a LaunchDarkly flag key that defaults off.",
            resume_stage="plan",
        )
    if job.feature.user_facing and policy.flags.must_default_off and plan.flag_default is True:
        return Decision(
            pause=True,
            reason="The plan turns the flag on by default. Policy requires the code default off.",
            resume_stage="plan",
        )
    return Decision(allowed=True, reason="plan is inside the auto-approve boundary")


def review_ship(policy: Policy, job: JobState, diff: str) -> Decision:
    from factory.governance.flags import audit_flag

    if job.feature.user_facing and policy.flags.required_for_user_facing:
        flag_key = job.feature.flag_key or (job.plan.flag_key if job.plan else None)
        problems = audit_flag(diff, flag_key, must_default_off=policy.flags.must_default_off)
        if problems:
            return Decision(retry_build=True, reason="; ".join(problems))
    if job.review and job.review.verdict != "approve":
        return Decision(retry_build=True, reason="Reviewer did not approve the diff.")
    if not policy.pull_request.draft_only:
        return Decision(reason="policy must keep pull requests draft-only")
    return Decision(allowed=True, reason="flag, review, and draft-only rules passed")


def _git_subcommand(argv: list[str]) -> str:
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "-c":
            index += 2
            continue
        if arg in {"-C", "--git-dir", "--work-tree"}:
            raise GovernanceError("git cannot retarget the repository")
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return ""


def _path_matches(pattern: str, rel: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return rel == prefix or rel.startswith(prefix + "/")
    if pattern.startswith("**/"):
        pattern = pattern[3:]
    if "*" in pattern:
        from fnmatch import fnmatch

        return fnmatch(rel, pattern) or any(fnmatch(part, pattern) for part in rel.split("/"))
    return rel == pattern
