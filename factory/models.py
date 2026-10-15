from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Stage(str, Enum):
    INTAKE = "intake"
    CONTEXT = "context"
    PLAN = "plan"
    GOVERN_PLAN = "govern_plan"
    AWAIT_HUMAN = "await_human"
    BUILD = "build"
    VERIFY = "verify"
    ENSURE_FLAG = "ensure_flag"
    REVIEW = "review"
    GOVERN_SHIP = "govern_ship"
    DRAFT_PR = "draft_pr"
    DONE = "done"
    FAILED = "failed"


FailureClass = Literal["none", "test", "flag", "ambiguous", "infra"]
Risk = Literal["low", "medium", "high"]
Verdict = Literal["approve", "changes_requested", "needs_human"]


class FeatureRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    summary: str
    acceptance: list[str] = Field(default_factory=list)
    user_facing: bool = True
    flag_key: str | None = None
    flag_default: bool = False
    source: str = "file"
    issue_url: str | None = None
    hints: list[str] = Field(default_factory=list)
    change_type: str = "unspecified"
    scope: str = ""
    verification: str = ""
    rollout: str = ""
    demo_fail_first_build: bool = False


class Snippet(BaseModel):
    path: str
    text: str


class RepoContext(BaseModel):
    root: str
    top_files: list[str]
    snippets: list[Snippet]
    has_flags_module: bool
    has_capabilities_route: bool


class ImplementationPlan(BaseModel):
    summary: str
    files: list[str]
    steps: list[str]
    flag_key: str | None
    flag_default: bool
    test_targets: list[str]
    risk: Risk
    ambiguous: bool
    questions: list[str]
    recipe: str
    context_files: list[str] = Field(default_factory=list)


class FileEdit(BaseModel):
    path: str
    kind: Literal["write", "replace"]
    content: str = ""
    old: str = ""
    new: str = ""


class BuildProposal(BaseModel):
    summary: str
    commit_message: str
    edits: list[FileEdit]
    commands: list[list[str]] = Field(default_factory=list)
    flag_default: bool = False


class BuildResult(BaseModel):
    commit: str
    changed_files: list[str]
    summary: str
    attempt: int
    flag_default: bool


class CommandReport(BaseModel):
    argv: list[str]
    exit_code: int
    output: str
    passed: bool


class VerifyResult(BaseModel):
    passed: bool
    commands: list[CommandReport]
    actionable: str
    classification: FailureClass


class ReviewFinding(BaseModel):
    check: str
    passed: bool
    evidence: str


class ReviewReport(BaseModel):
    verdict: Verdict
    findings: list[ReviewFinding]
    risks: list[str]
    summary: str


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    source: str = "estimate"


class WorktreeInfo(BaseModel):
    path: str
    branch: str
    base_sha: str
    removed: bool = False
    snapshot: str | None = None
    source_url: str | None = None
    base_branch: str = "main"


class FlagAssurance(BaseModel):
    """What the factory confirmed about the LaunchDarkly flag before review."""

    mode: Literal["live", "offline"]
    required: bool
    key: str | None = None
    project_key: str | None = None
    url: str | None = None
    created: bool = False
    reused: bool = False
    production_on: bool | None = None
    production_serves_false: bool | None = None
    environments_off: bool | None = None
    detail: str = ""

    def production_off(self) -> bool:
        return self.production_on is False and self.production_serves_false is True

    def blocks_review(self) -> bool:
        if not self.required or self.mode != "live":
            return False
        return not (self.production_off() and self.environments_off is True)


class PullRequestDraft(BaseModel):
    title: str
    body_path: str
    dry_run: bool
    url: str | None = None
    branch: str | None = None


class JobEvent(BaseModel):
    ts: float
    component: str
    stage: str
    message: str
    data: dict[str, str] = Field(default_factory=dict)


class JobState(BaseModel):
    id: str
    stage: Stage
    feature: FeatureRequest
    source_sha: str | None = None
    context: RepoContext | None = None
    plan: ImplementationPlan | None = None
    builds: list[BuildResult] = Field(default_factory=list)
    verify_runs: list[VerifyResult] = Field(default_factory=list)
    review: ReviewReport | None = None
    reviews: list[ReviewReport] = Field(default_factory=list)
    launchdarkly: FlagAssurance | None = None
    question: str | None = None
    human_answers: list[str] = Field(default_factory=list)
    resume_stage: Stage | None = None
    retries: dict[str, int] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)
    worktree: WorktreeInfo | None = None
    pr: PullRequestDraft | None = None
    error: str | None = None
    paused: bool = False
    govern_feedback: str | None = None
    events: list[JobEvent] = Field(default_factory=list)
    created_at: float
    updated_at: float

    def latest_verify(self) -> VerifyResult | None:
        return self.verify_runs[-1] if self.verify_runs else None

    def latest_build(self) -> BuildResult | None:
        return self.builds[-1] if self.builds else None
