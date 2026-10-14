from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from factory.dotenv import ensure_env_key, read_dotenv_key
from factory.errors import FactoryError
from factory.governance.policy import load_policy
from factory.harness.base import AgentHarness
from factory.harness.mock import MockHarness
from factory.harness.openai_agents import OpenAIAgentsHarness
from factory.intake.load import load_feature, load_github_issue
from factory.launchdarkly.token import resolve_token
from factory.models import JobEvent, JobState, Stage
from factory.observability.summary import build_summary
from factory.orchestrator.machine import Factory

DECORA_REPO = Path(
    "/Users/alexisroberson/Documents/Claude_Code_Projects/langgraph-projects/decor-agent"
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "temporal":
        from factory.temporal.cli import main as temporal_main

        return temporal_main(argv[1:])
    common = argparse.ArgumentParser(add_help=False, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    common.add_argument("--repo", type=Path, default=project_root() / ".factory" / "demo-target")
    common.add_argument("--home", type=Path, default=None)
    common.add_argument("--policy", type=Path, default=None)
    common.add_argument(
        "--agents",
        choices=("mock", "openai"),
        default="openai",
        help="openai calls the Agents SDK; mock is the offline recipe",
    )
    common.add_argument("--keep-worktree", action="store_true")
    common.add_argument("--push", action="store_true")
    common.add_argument(
        "--ld",
        choices=("auto", "live", "offline"),
        default="auto",
        help="auto uses LaunchDarkly when a token resolves; offline skips the live check",
    )

    parser = argparse.ArgumentParser(prog="factory", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(
            name,
            parents=[common],
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            help=help_text,
        )

    add("demo", "run the room-mood sample in dry-run mode")

    run = add("run", "run a JSON feature spec")
    run.add_argument("spec", type=Path)

    issue = add("issue", "run a GitHub issue as the feature request")
    issue.add_argument("--slug", required=True, help="GitHub owner/name")
    issue.add_argument("--number", type=int, required=True)

    resume = add("resume", "answer a paused job and continue")
    resume.add_argument("job_id")
    resume.add_argument("--answer", required=True)

    status = add("status", "print the stored summary for a job")
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

    harness: AgentHarness
    if args.agents == "openai":
        source = ensure_env_key(root, "OPENAI_API_KEY")
        print(f"OpenAI: live (key from {source})")
        harness = OpenAIAgentsHarness()
    else:
        print("OpenAI: mock (--agents mock)")
        harness = MockHarness()
    if not os.environ.get("LAUNCHDARKLY_API_TOKEN", "").strip():
        token = read_dotenv_key(root / ".env", "LAUNCHDARKLY_API_TOKEN")
        if token:
            os.environ["LAUNCHDARKLY_API_TOKEN"] = token
    _announce_launchdarkly(args.ld, root)
    if args.cmd == "demo":
        before = _porcelain(repo)
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
        ld_mode=args.ld,
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
            ["git", "-C", str(repo), "branch", "--list", f"factory/{job.id}"],
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
    if job.launchdarkly is not None and job.launchdarkly.url:
        print(f"flag {job.launchdarkly.url}")
    elif job.launchdarkly is not None and job.launchdarkly.required and job.launchdarkly.mode == "offline":
        print(f"flag check skipped: {job.launchdarkly.detail}")
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


def _announce_launchdarkly(mode: str, repo: Path) -> None:
    if mode == "offline":
        print("LaunchDarkly: offline (--ld offline)")
        return
    token, source = resolve_token(repo)
    if token:
        print(f"LaunchDarkly: live (token from {source})")
        return
    if mode == "live":
        print("LaunchDarkly: live (LAUNCHDARKLY_API_TOKEN is unset)")
        return
    print("LaunchDarkly: offline (LAUNCHDARKLY_API_TOKEN is unset)")


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
