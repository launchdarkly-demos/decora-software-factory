from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, cast, get_args

import yaml
from pydantic import BaseModel, Field

from factory.errors import FactoryError, GovernanceError, SafetyError
from factory.models import ChangeClass, ImplementationPlan, JobState


class ApprovalRules(BaseModel):
    auto_approve_risk: list[str] = Field(default_factory=lambda: ["low"])
    pause_on_ambiguous: bool = True
    pause_on_risk: list[str] = Field(default_factory=lambda: ["high"])


class FlagRules(BaseModel):
    module: str = "app/flags.py"
    enabled: bool = False
    key_pattern: str = r"^decora-[a-z0-9-]+$"
    must_default_off: bool = True
    issue_flag_keys: dict[str, str] = Field(default_factory=dict)
    default_key: str | None = None


class RolloutRule(BaseModel):
    flag: Literal["required", "optional", "forbidden"]
    guarded_rollout: Literal["required", "optional", "none"] = "none"
    human_pause: bool = False
    required_for_risk: list[str] | None = None
    required_for_large_change: bool = False


def default_rollout_rules() -> dict[str, RolloutRule]:
    rules = {name: RolloutRule(flag="forbidden") for name in get_args(ChangeClass)}
    rules.update(
        user_facing_behavior=RolloutRule(flag="required", guarded_rollout="required"),
        data_migration=RolloutRule(flag="required", guarded_rollout="required", human_pause=True),
        backend_internal=RolloutRule(flag="required", required_for_risk=["medium", "high"],
                                    required_for_large_change=True),
        ui_cosmetic=RolloutRule(flag="optional"),
    )
    return rules


class GuardedRolloutRules(BaseModel):
    default_metric_key: str = ""
    allowed_metric_keys: list[str] = Field(default_factory=list)
    min_sample_size: int = Field(default=100, ge=1)
    rollback_on_regression: bool = True
    stages: list[int] = Field(default_factory=lambda: [1, 5, 25, 50, 100])


class PullRequestRules(BaseModel):
    draft_only: bool = True
    allow_push: bool = False


class LaunchDarklyRules(BaseModel):
    project_key: str = "alexis-interior-design-demo-app"
    environments: list[str] = Field(default_factory=lambda: ["test", "production"])
    production_environment: str = "production"


class VerificationProfile(BaseModel):
    description: str
    kind: str = "test"
    paths: list[str] = Field(default_factory=list)
    commands: list[list[str]] = Field(min_length=1)


class Policy(BaseModel):
    version: int = 1
    builder_max_turns: int = Field(default=40, gt=0)
    builder_max_output_tokens: int = Field(default=16000, gt=0)
    verify_retries: int = Field(default=4, ge=0)
    per_task_retries: int = Field(default=4, ge=0)
    max_job_attempts: int = Field(default=40, gt=0)
    split_attempts: int = Field(default=4, ge=0)
    whole_file_limit: int = Field(default=64000, gt=0)
    excerpt_budget: int = Field(default=160000, gt=0)
    protected_paths: list[str] = Field(default_factory=list)
    max_retries: dict[str, int] = Field(
        default_factory=lambda: {"review": 1, "govern": 1, "plan": 2, "plan_repair": 2}
    )
    stage_timeout_seconds: dict[str, float] = Field(default_factory=dict)
    regression_tests: list[str] = Field(default_factory=list)
    verification_profiles: dict[str, VerificationProfile] = Field(default_factory=dict)
    require_browser_for_frontend: bool = True
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
    rollout_rules: dict[str, RolloutRule] = Field(default_factory=default_rollout_rules)
    guarded_rollout: GuardedRolloutRules = Field(default_factory=GuardedRolloutRules)
    flags: FlagRules = Field(default_factory=FlagRules)
    launchdarkly: LaunchDarklyRules = Field(default_factory=LaunchDarklyRules)
    pull_request: PullRequestRules = Field(default_factory=PullRequestRules)

    def timeout_for(self, stage: str) -> float:
        return float(self.stage_timeout_seconds.get(stage, 600 if stage == "build" else 120))

    def check_command(self, argv: list[str]) -> None:
        if not argv:
            raise FactoryError("empty command; return commands=[] or a policy-approved test command")
        line = " ".join(argv)
        for pattern in self.deny_command_patterns:
            if pattern and pattern in line:
                raise GovernanceError(f"command denied by policy: {line}")
        name = Path(argv[0]).name
        if name not in self.allow_commands:
            raise FactoryError(f"command not allowlisted: {name}; return commands=[] and let verification run")
        # `python -c` runs arbitrary code. `pytest -c` is a config path and stays allowed.
        if name in {"python", "python3"} and len(argv) > 1 and argv[1] == "-c":
            raise GovernanceError("python -c is not allowlisted")
        if name == "git":
            sub = _git_subcommand(argv)
            if sub not in self.git_subcommands:
                raise FactoryError(f"git {sub} is not allowlisted; return commands=[] and let verification run")

    def check_path(self, rel: str) -> None:
        normalized = rel.replace("\\", "/").lstrip("/")
        if normalized == ".git" or normalized.startswith(".git/") or "/.git/" in f"/{normalized}":
            raise SafetyError(f"Safety violation: protected path on `{rel}`.")
        if any(part.startswith(".env") or part in {"policy.yaml", "policy.decora.yaml"}
               for part in Path(normalized).parts):
            raise SafetyError(f"Safety violation: protected path on `{rel}`.")
        for pattern in [*self.deny_paths, *self.protected_paths]:
            if _path_matches(pattern, normalized):
                raise SafetyError(f"Safety violation: protected path on `{rel}`.")


class Decision(BaseModel):
    allowed: bool = False
    pause: bool = False
    retry_build: bool = False
    retry_plan: bool = False
    reason: str = ""
    resume_stage: str | None = None


def load_policy(path: Path) -> Policy:
    data = yaml.safe_load(path.read_text()) or {}
    return Policy.model_validate(data)


def change_class(job: JobState, plan: ImplementationPlan) -> ChangeClass | None:
    labels = [label.removeprefix("factory:class/") for label in job.feature.labels
              if label.startswith("factory:class/")]
    overrides = set(labels + ([job.feature.change_class] if job.feature.change_class else []))
    if overrides:
        return (cast(ChangeClass, next(iter(overrides)))
                if len(overrides) == 1 and overrides <= set(get_args(ChangeClass)) else None)
    if plan.requires_migration:
        return "data_migration"
    if plan.change_class:
        return plan.change_class
    if plan.files and all(path.startswith("docs/") or path.endswith(".md") for path in plan.files):
        return "docs_only"
    if plan.files and all(path.startswith("tests/") for path in plan.files):
        return "tests_only"
    return None


def flag_keys(job: JobState, plan: ImplementationPlan | None = None) -> set[str]:
    plan = plan or job.plan
    if job.fixed_flag_key:
        return {job.fixed_flag_key}
    values = [job.feature.flag_key, plan.flag_key if plan else None]
    return {key for value in values if value for key in re.split(r"[\s,;]+", value.strip())
            if key and key != "replace-with-your-flag-key"}


def fixed_flag_key(policy: Policy, job: JobState) -> str | None:
    """Prefer an exact URL, canonical owner/repo#number, then a numeric issue key."""
    url = job.feature.issue_url or ''
    match = re.search(r'github\.com/([^/]+/[^/]+)/issues/(\d+)', url)
    aliases = [url.rstrip('/')]
    if match:
        aliases.extend([f'{match[1]}#{match[2]}', match[2]])
    aliases.append(job.feature.id)
    for alias in aliases:
        if alias in policy.flags.issue_flag_keys:
            return policy.flags.issue_flag_keys[alias]
    return policy.flags.default_key


def selected_flag_key(job: JobState) -> str | None:
    keys = flag_keys(job)
    return next(iter(keys)) if len(keys) == 1 else None


def flag_required(policy: Policy, job: JobState, plan: ImplementationPlan | None = None) -> bool:
    """Whether this job needs flag provisioning and audit, including an optional opt-in."""
    plan = plan or job.plan
    if not policy.flags.enabled or plan is None:
        return False
    rule = policy.rollout_rules.get(change_class(job, plan) or "")
    if rule is None or rule.flag == "forbidden":
        return False
    required = rule.flag == "required"
    if rule.required_for_risk is not None:
        required = (plan.risk in rule.required_for_risk
                    or (rule.required_for_large_change and plan.large_change))
    return required or bool(flag_keys(job, plan))


def guarded_rollout_required(policy: Policy, job: JobState) -> bool:
    if not policy.flags.enabled or job.plan is None:
        return False
    rule = policy.rollout_rules.get(change_class(job, job.plan) or "")
    return bool(rule and rule.guarded_rollout == "required")


def review_plan(policy: Policy, plan: ImplementationPlan, job: JobState) -> Decision:
    from factory.plan_normalization import normalize_flag_task, normalize_test_tasks
    from factory.verification.profiles import prerequisites, task_problems

    normalize_test_tasks(plan)
    job.fixed_flag_key = None

    if policy.approvals.pause_on_ambiguous and not job.feature.acceptance and not job.human_answers:
        return Decision(
            pause=True,
            reason="What behavior and acceptance checks should this change implement?",
            resume_stage="plan",
        )
    classification = change_class(job, plan)
    if classification is None or classification not in policy.rollout_rules:
        return Decision(pause=True, reason="Which change_class applies to this change? Choose "
                        + ", ".join(get_args(ChangeClass)) + ". Resolve conflicting class overrides.",
                        resume_stage="plan")
    plan.change_class = classification
    if flag_required(policy, job, plan) and (fixed := fixed_flag_key(policy, job)):
        job.fixed_flag_key = fixed
        plan.flag_key = fixed
    keys = flag_keys(job, plan)
    if policy.flags.enabled:
        if len(keys) > 1:
            return Decision(pause=True, retry_plan=True, reason="Use one flag key per job; reuse the issue's key.",
                            resume_stage="plan")
        if any(not re.fullmatch(policy.flags.key_pattern, key) for key in keys):
            return Decision(pause=True, retry_plan=True, reason="Flag key must match " + policy.flags.key_pattern,
                            resume_stage="plan")
        rule = policy.rollout_rules[classification]
        if rule.guarded_rollout == "required" or plan.guarded_rollout_metric:
            metric = plan.guarded_rollout_metric or policy.guarded_rollout.default_metric_key
            if not metric:
                return Decision(pause=True, reason="Which allowed metric should guard this rollout? "
                                "Configure default_metric_key or name a guarded_rollout_metric in the plan.",
                                resume_stage="plan")
            if metric not in policy.guarded_rollout.allowed_metric_keys:
                return Decision(pause=True, retry_plan=True,
                                reason="Guarded rollout metric must be in allowed_metric_keys.",
                                resume_stage="plan")
            plan.guarded_rollout_metric = metric
    normalize_flag_task(policy, job, plan)
    repairs = task_problems(plan)
    if flag_required(policy, job, plan) and keys and not any(
            task.id == plan.flag_task_id for task in plan.tasks):
        repairs.append("Required flag must have a production task owner before BUILD.")
    if job.context and job.context.file_inventory:
        missing = set(plan.context_files) - set(job.context.file_inventory)
        if missing:
            repairs.append("Plan cites context files absent from the repository: " + ", ".join(sorted(missing))
                           + ". Inspect the inventory and read existing paths; do not invent context files.")
    for name in plan.verification_profiles:
        if name not in policy.verification_profiles:
            repairs.append(f"Unknown verification profile {name!r}; select only configured profile names.")
    if plan.requires_migration and not plan.migration_plan.strip():
        repairs.append("Supply the required migration and recovery plan.")
    blockers = prerequisites(policy, plan, plan.files)
    for kind in ("browser", "migration"):
        candidates = [name for name, profile in policy.verification_profiles.items() if profile.kind == kind]
        if candidates and any(f"configured {kind} verification profile" in error for error in blockers):
            repairs.append(f"Select a configured {kind} profile in verification_profiles: " + ", ".join(candidates))
    if repairs:
        return Decision(pause=True, retry_plan=True, reason="\n".join(repairs), resume_stage="plan")
    if blockers:
        return Decision(pause=True, reason=" ".join(blockers), resume_stage="plan")
    if plan.ambiguous and policy.approvals.pause_on_ambiguous:
        question = plan.questions[0] if plan.questions else "The plan is ambiguous."
        return Decision(pause=True, reason=question, resume_stage="plan")
    if (
        flag_required(policy, job, plan)
        and not keys
    ):
        return Decision(
            pause=True,
            retry_plan=True,
            reason="This change class needs one default-off LaunchDarkly flag key.",
            resume_stage="plan",
        )
    if flag_required(policy, job, plan) and policy.flags.must_default_off and plan.flag_default is True:
        return Decision(
            pause=True,
            reason="The plan turns the flag on by default. Policy requires the code default off.",
            resume_stage="plan",
        )
    if (plan.requires_migration or policy.rollout_rules[classification].human_pause
            or plan.risk in policy.approvals.pause_on_risk):
        return Decision(
            pause=True,
            reason=f"Risk {plan.risk} or data migration needs human review of the saved implementation plan. "
                   "Reply 'approve' to build; other feedback goes back to planning.",
            resume_stage="build",
        )
    return Decision(allowed=True, reason="plan is inside the auto-approve boundary")


def review_ship(policy: Policy, job: JobState, diff: str) -> Decision:
    from factory.governance.flags import audit_flag

    if flag_required(policy, job):
        flag_key = selected_flag_key(job)
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
