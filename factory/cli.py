from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from factory.errors import FactoryError
from factory.execution.worktree import WorktreeManager
from factory.governance.policy import load_policy
from factory.harness.mock import MockHarness
from factory.harness.openai_agents import OpenAIAgentsHarness
from factory.intake.load import load_feature, load_github_issue
from factory.models import JobEvent, JobState, Stage
from factory.observability.summary import build_summary
from factory.orchestrator.machine import Factory

DECORA_REPO = Path(
    "/Users/alexisroberson/Documents/Claude_Code_Projects/langgraph-projects/decor-agent"
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repo", type=Path, default=DECORA_REPO)
    common.add_argument("--home", type=Path, default=None)
    common.add_argument("--policy", type=Path, default=None)
    common.add_argument("--agents", choices=("mock", "openai"), default="mock")
    common.add_argument("--keep-worktree", action="store_true")
    common.add_argument("--push", action="store_true")

    parser = argparse.ArgumentParser(prog="factory")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", parents=[common], help="run the room-mood sample in dry-run mode")

    run = sub.add_parser("run", parents=[common], help="run a JSON feature spec")
    run.add_argument("spec", type=Path)

    issue = sub.add_parser("issue", parents=[common], help="run a GitHub issue as the feature request")
    issue.add_argument("--slug", required=True, help="GitHub owner/name")
    issue.add_argument("--number", type=int, required=True)

    resume = sub.add_parser("resume", parents=[common], help="answer a paused job and continue")
    resume.add_argument("job_id")
    resume.add_argument("--answer", required=True)

    status = sub.add_parser("status", parents=[common], help="print the stored summary for a job")
    status.add_argument("job_id")

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except FactoryError as exc:
        print(f"factory: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    root = project_root()
    home = args.home or (root / ".factory")
    policy_path = args.policy or (root / "policy.yaml")
    policy = load_policy(policy_path)
    repo = args.repo.resolve()
    if args.push and repo == DECORA_REPO.resolve():
        print("Refusing to push or open a pull request on the Decora repo.", file=sys.stderr)
        return 1
    if args.push and not policy.pull_request.allow_push:
        print("policy.yaml pull_request.allow_push is false. This factory will not push.", file=sys.stderr)
        return 1
    if args.cmd == "status":
        job = Factory(
            target_repo=repo,
            home=home,
            policy=policy,
            harness=MockHarness(),
        ).status(args.job_id)
        print(json.dumps(build_summary(job), indent=2))
        return 0 if job.stage != Stage.FAILED else 1

    harness = OpenAIAgentsHarness() if args.agents == "openai" else MockHarness()
    if args.cmd == "demo":
        before = _porcelain(repo)
        WorktreeManager(home / "worktrees").sweep(repo)
        feature = load_feature(root / "samples" / "decor-room-mood.json")
    elif args.cmd == "run":
        before = _porcelain(repo)
        feature = load_feature(args.spec)
    elif args.cmd == "issue":
        before = _porcelain(repo)
        feature = load_github_issue(args.slug, args.number)
    else:
        before = None
        feature = None

    factory = Factory(
        target_repo=repo,
        home=home,
        policy=policy,
        harness=harness,
        keep_worktree=args.keep_worktree,
        push=args.push,
        on_event=_print_event,
    )
    if args.cmd == "resume":
        job = factory.resume(args.job_id, args.answer)
    else:
        assert feature is not None
        job = factory.run(feature)
    _print_summary(job)
    if before is not None and _porcelain(repo) != before:
        print("target working tree changed; that should not happen", file=sys.stderr)
        return 1
    if args.cmd == "demo":
        leftover = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", "factory/job-*"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if leftover:
            print(f"leftover factory branches:\n{leftover}", file=sys.stderr)
            return 1
    if job.stage == Stage.FAILED:
        return 1
    return 0


def _print_event(event: JobEvent) -> None:
    origin = event.data.get("from", "")
    step = f"{origin} -> {event.stage}" if origin else event.stage
    print(f"  {event.component:<20} {step:<32} {event.message}")


def _print_summary(job: JobState) -> None:
    summary = build_summary(job)
    print()
    print(f"job {job.id}  {job.stage.value}")
    print(f"duration_ms {summary['duration_ms']}")
    tokens = summary["tokens"]
    assert isinstance(tokens, dict)
    print(
        f"tokens in={tokens['input_tokens']} out={tokens['output_tokens']} ({tokens['source']})"
    )
    print(f"retries {summary['retries'] or {}}")
    if job.pr is not None:
        print(f"pr body {job.pr.body_path}")
        if job.pr.dry_run:
            print("dry run: no branch was pushed and no pull request was opened")
        elif job.pr.url:
            print(f"pull request {job.pr.url}")
    if job.question:
        print(f"paused: {job.question}")
    if job.error:
        print(f"error: {job.error}")


def _porcelain(repo: Path) -> str:
    if not (repo / ".git").exists():
        return ""
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout
