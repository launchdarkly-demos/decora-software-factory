from __future__ import annotations

import subprocess
from pathlib import Path

from factory.agents.recipes import CAPABILITIES_RETURN, FLAGS_NEEDLE
from factory.gitcmd import git_env
from factory.governance.policy import Policy, load_policy
from factory.harness.mock import MockHarness
from factory.models import (
    BuildResult,
    FeatureRequest,
    ImplementationPlan,
    JobState,
    ReviewReport,
    Stage,
    VerifyResult,
)
from factory.orchestrator.machine import Factory

ROOT = Path(__file__).resolve().parents[1]


def policy() -> Policy:
    rules = load_policy(ROOT / "policy.yaml")
    # Existing flag-mode tests exercise the retained opt-in integration explicitly.
    rules.flags.enabled = True
    rules.flags.required_for_risk = ["medium", "high"]
    rules.flags.required_for_large_changes = True
    rules.flags.required_for_important_ux = True
    return rules


def init_repo(path: Path, *, capabilities: bool = True) -> Path:
    path.mkdir(parents=True)
    (path / "app").mkdir()
    (path / "app" / "__init__.py").write_text("")
    (path / "app" / "flags.py").write_text(
        FLAGS_NEEDLE + "\n\ndef get_flag(key, context, default=False):\n    return default\n"
    )
    if capabilities:
        server = (
            "from app.flags import BOARD_INTAKE_FLAG, get_flag\n\n\n"
            "def build_context(key: str) -> dict:\n"
            '    return {"key": key}\n\n\n'
            'def capabilities(context_key: str = "anonymous") -> dict:\n'
            "    context = build_context(context_key)\n"
            + CAPABILITIES_RETURN
        )
    else:
        server = 'def capabilities() -> dict:\n    return {"ok": True}\n'
    (path / "server.py").write_text(server)
    (path / "README.md").write_text("mini decora\n")
    env = git_env()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, env=env, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True, env=env, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=path,
        check=True,
        env=env,
        capture_output=True,
    )
    return path


def feature(**overrides) -> FeatureRequest:
    data = {
        "id": "sample",
        "title": "Sample",
        "summary": "Do the sample.",
        "acceptance": ["the sample holds"],
        "user_facing": False,
        "flag_key": None,
    }
    data.update(overrides)
    return FeatureRequest.model_validate(data)


def low_plan(**overrides) -> ImplementationPlan:
    data = {
        "summary": "Add the change behind a flag",
        "files": ["app/room_mood.py"],
        "steps": ["add the flag"],
        "flag_key": "decor-room-mood",
        "flag_default": False,
        "test_targets": ["test_room_mood_flag.py"],
        "risk": "low",
        "ambiguous": False,
        "questions": [],
        "recipe": "flagged_capability_field",
    }
    data.update(overrides)
    return ImplementationPlan.model_validate(data)


def passed_verify() -> VerifyResult:
    return VerifyResult(passed=True, commands=[], actionable="pytest passed", classification="none")


def failed_verify(text: str = "assert failed", classification: str = "test") -> VerifyResult:
    return VerifyResult(passed=False, commands=[], actionable=text, classification=classification)


def approved() -> ReviewReport:
    return ReviewReport(verdict="approve", findings=[], risks=[], summary="ok")


class ScriptedHarness:
    def __init__(self, plans, builds=None, reviews=None) -> None:
        self.plans = list(plans)
        self.builds = list(builds or [])
        self.reviews = list(reviews or [])
        self.build_calls = 0

    def plan(self, job, context):
        return _take(self.plans)

    def build(self, job, tools):
        self.build_calls += 1
        if self.builds:
            item = _take(self.builds)
            if callable(item):
                return item(job, tools)
            return item
        commit = "deadbeef"
        if job.plan and job.plan.flag_key:
            tools.write("app/room_mood.py",
                        f'FLAG_KEY = {job.plan.flag_key!r}\nFLAG_DEFAULT = False\n'
                        f'# Build attempt {self.build_calls}\n')
            commit = tools.commit("Scripted flag fixture", ["app/room_mood.py"])
        return BuildResult(
            commit=commit,
            changed_files=["app/room_mood.py"],
            summary="scripted build",
            attempt=self.build_calls,
            flag_default=False,
        )

    def review(self, job, tools):
        if not self.reviews:
            return approved()
        return _take(self.reviews)


class ScriptedVerifier:
    def __init__(self, results) -> None:
        self.results = list(results)

    def run(self, job, tools):
        return _take(self.results)


def make_factory(tmp_path: Path, repo: Path, harness=None, **kwargs) -> Factory:
    home = kwargs.pop("home", None) or (tmp_path / "home")
    return Factory(
        target_repo=repo,
        home=home,
        policy=kwargs.pop("policy", None) or policy(),
        harness=harness or MockHarness(),
        **kwargs,
    )


def bare_job(tmp_path: Path, **overrides) -> JobState:
    now = 10.0
    data = {
        "id": "job-test",
        "stage": Stage.VERIFY,
        "feature": feature(),
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return JobState.model_validate(data)


def _take(items):
    if not items:
        raise AssertionError("scripted double ran out of values")
    if len(items) == 1:
        return items[0]
    return items.pop(0)
