from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.worker import Worker

from factory.dotenv import ensure_env_key
from factory.errors import FactoryError
from factory.execution.portable import PortableWorkspace
from factory.governance.policy import load_policy
from factory.harness.mock import MockHarness
from factory.harness.openai_agents import OpenAIAgentsHarness
from factory.intake.load import load_feature
from factory.models import JobState
from factory.observability.summary import build_summary
from factory.orchestrator.machine import Factory
from factory.temporal.activities import StageActivities
from factory.temporal.workflow import FactoryWorkflow

ROOT = Path(__file__).resolve().parents[2]


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--address", default="localhost:7235")
    common.add_argument("--namespace", default="default")
    common.add_argument("--task-queue", default="software-factory")
    result = argparse.ArgumentParser(prog="factory temporal")
    sub = result.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker", parents=[common], help="run a durable worker on this host")
    worker.add_argument("--repo", type=Path, default=ROOT / ".factory" / "demo-target")
    worker.add_argument("--home", type=Path, default=ROOT / ".factory")
    worker.add_argument("--policy", type=Path, default=ROOT / "policy.yaml")
    worker.add_argument("--agents", choices=["openai", "mock"], default="openai")
    worker.add_argument("--ld", choices=["auto", "live", "offline"], default="auto")
    worker.add_argument("--keep-worktree", action="store_true")
    worker.add_argument("--push", action="store_true", help="publish draft PRs if policy allows")
    worker.add_argument("--workspace-root", type=Path, default=None)
    worker.add_argument("--base-ref", default="HEAD")
    worker.add_argument("--base-branch", default="main")
    start = sub.add_parser("start", parents=[common], help="submit a feature; returns immediately")
    start.add_argument("spec", type=Path)
    start.add_argument("--id", default=None)
    issues = sub.add_parser("watch-issues", parents=[common], help="submit maintainer-labeled GitHub issues")
    issues.add_argument("--slug", required=True, help="GitHub owner/name matching the worker target")
    issues.add_argument("--label", default="factory:ready")
    issues.add_argument("--home", type=Path, default=ROOT / ".factory" / "github")
    issues.add_argument("--interval", type=float, default=30)
    issues.add_argument("--once", action="store_true")
    for name in ("status", "wait", "answer"):
        command = sub.add_parser(name, parents=[common])
        command.add_argument("job_id")
        if name == "answer":
            command.add_argument("--answer", required=True)
            command.add_argument("--pause-id", type=int, required=True)
    return result


def main(argv: list[str]) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(dispatch(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"Temporal command failed ({type(exc).__name__}); check server/worker configuration.")
        return 1


async def dispatch(args: argparse.Namespace) -> int:
    client = await Client.connect(args.address, namespace=args.namespace)
    if args.command == "watch-issues":
        from factory.github.issues import watch_issues

        await watch_issues(client, args)
        return 0
    if args.command == "worker":
        await run_worker(client, args)
        return 0
    if args.command == "start":
        feature = load_feature(args.spec)
        job_id = args.id or "job-" + uuid.uuid4().hex[:12]
        import re

        if not re.fullmatch(r"job-[a-zA-Z0-9-]+", job_id):
            raise FactoryError("job ID must start with job- and contain only letters, numbers, or hyphens")
        await client.start_workflow(
            FactoryWorkflow.run,
            feature.model_dump(mode="json"),
            id=job_id,
            task_queue=args.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
        print(f"Started {job_id}")
        print(f"Status: factory temporal status {job_id} --address {args.address}")
        return 0
    handle = client.get_workflow_handle(args.job_id)
    if args.command == "wait":
        job = JobState.model_validate(await handle.result())
        print(json.dumps(build_summary(job), indent=2))
        return 1 if job.stage.value == "failed" else 0
    if args.command == "answer":
        if not args.answer.strip():
            raise FactoryError("answer must not be empty")
        await handle.signal(FactoryWorkflow.answer, args=[args.pause_id, args.answer])
        print("Answer queued; the workflow accepts it only for the matching clarification round.")
    else:
        state = await handle.query(FactoryWorkflow.snapshot)
        print(json.dumps(state, indent=2))
    return 0


async def run_worker(client: Client, args: argparse.Namespace) -> None:
    if args.agents == "openai":
        ensure_env_key(ROOT, "OPENAI_API_KEY")
    policy = load_policy(args.policy)
    repo, home = args.repo.resolve(), args.home.resolve()
    if not (repo / ".git").exists() and not any((home / "artifacts").glob("*.json")):
        raise FactoryError("worker needs a source repository or existing recovery artifacts")

    def create_factory() -> Factory:
        return Factory(
            target_repo=repo, home=home, policy=policy,
            harness=OpenAIAgentsHarness(policy=policy) if args.agents == "openai" else MockHarness(),
            ld_mode=args.ld, keep_worktree=args.keep_worktree, push=args.push,
            execution=PortableWorkspace(args.workspace_root or home / "workspaces", home / "artifacts",
                                        base_ref=args.base_ref, base_branch=args.base_branch),
        )

    activities = StageActivities(create_factory, home)
    print(f"Worker: {args.task_queue}; repo={repo}; agents={args.agents}; ld={args.ld}", flush=True)
    with ThreadPoolExecutor(max_workers=4) as executor:
        async with Worker(
            client, task_queue=args.task_queue, workflows=[FactoryWorkflow], activities=[activities.execute],
            activity_executor=executor, max_concurrent_activities=4,
            graceful_shutdown_timeout=timedelta(seconds=20),
        ):
            await asyncio.Event().wait()
