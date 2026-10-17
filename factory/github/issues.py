from __future__ import annotations

import asyncio
import fcntl
import json
import re
import subprocess
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from factory.errors import FactoryError
from factory.intake.load import parse_issue_body
from factory.temporal.workflow import FactoryWorkflow


class GitHubIssues:
    def __init__(self, slug: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
            raise FactoryError("GitHub repository must be owner/name")
        self.slug = slug
        self.root = f"repos/{slug}"
        self.login: str | None = None

    def api(self, endpoint: str, *, method: str = "GET", body: dict | None = None, pages: bool = False):
        argv = ["gh", "api", "--hostname", "github.com", endpoint, "--method", method]
        if pages:
            argv += ["--paginate", "--slurp"]
        if body is not None:
            argv += ["--input", "-"]
        result = subprocess.run(argv, input=json.dumps(body) if body is not None else None,
                                text=True, capture_output=True, timeout=60, check=False)
        if result.returncode:
            raise FactoryError("GitHub request failed; check gh authentication and repository permissions")
        value = json.loads(result.stdout)
        return [item for page in value for item in page] if pages else value

    def ready(self, label: str) -> list[dict]:
        items = self.api(f"{self.root}/issues?state=open&labels={quote(label, safe='')}&per_page=100", pages=True)
        return [item for item in items if "pull_request" not in item]

    def approved(self, issue: dict, label: str) -> bool:
        events = self.api(f"{self.root}/issues/{issue['number']}/timeline?per_page=100", pages=True)
        matching = [event for event in events if event.get("event") in {"labeled", "unlabeled"}
                    and event.get("label", {}).get("name") == label]
        if not matching or matching[-1]["event"] != "labeled":
            return False
        actor = (matching[-1].get("actor") or {}).get("login")
        if not actor:
            return False
        permission = self.api(f"{self.root}/collaborators/{quote(actor, safe='')}/permission")
        return permission.get("permission") in {"admin", "write", "maintain"}

    def comment(self, number: int, job_id: str, body: str) -> None:
        if self.login is None:
            self.login = self.api("user")["login"]
        marker = f"<!-- software-factory:{job_id} -->"
        desired = marker + "\n" + body
        comments = self.api(f"{self.root}/issues/{number}/comments?per_page=100", pages=True)
        own = [item for item in comments if item.get("user", {}).get("login") == self.login
               and item.get("body", "").startswith(marker + "\n")]
        if own:
            if own[0]["body"] != desired:
                self.api(f"{self.root}/issues/comments/{own[0]['id']}", method="PATCH", body={"body": desired})
        else:
            self.api(f"{self.root}/issues/{number}/comments", method="POST", body={"body": desired})


def status_body(job_id: str, snapshot: dict) -> str:
    job = snapshot.get("job", {})
    stage = job.get("stage", "queued")
    lines = [f"Software factory job `{job_id}` — **{stage}**."]
    if stage == "await_human":
        lines += ["", job.get("question") or "Clarification required.", "",
                  "Answer from the factory terminal using the current pause ID:", "```bash",
                  f".venv/bin/python -m factory temporal answer {job_id} "
                  f"--pause-id {snapshot['pause_id']} --answer 'YOUR ANSWER'", "```"]
        plan = job.get("plan") or {}
        if plan.get("tasks"):
            lines += ["", "Proposed work:"]
            for task in plan["tasks"][:12]:
                lines.append(f"- {task['id']}: {task['description'][:400]}")
        if plan.get("migration_plan"):
            lines += ["", "Migration/recovery: " + plan["migration_plan"][:1600]]
    if stage == "failed":
        lines += ["", "The job stopped. Inspect the local job logs before submitting a new issue."]
    pr = job.get("pr") or {}
    if pr.get("url"):
        lines += ["", f"Draft PR: {pr['url']}", "A human must review and merge it."]
    elif stage == "done":
        lines += ["", "Completed with a local PR draft; this worker did not publish to GitHub."]
    lines += ["", "Editing or relabeling this issue does not start another run. "
              "Comments are not executed as instructions."]
    return "\n".join(lines)


class IssueBridge:
    """Single-host polling bridge. Temporal IDs deduplicate submission; disk tracks ongoing jobs."""

    def __init__(self, client, github: GitHubIssues, home: Path, queue: str, label: str):
        self.client, self.github, self.queue, self.label = client, github, queue, label
        home.mkdir(parents=True, exist_ok=True)
        self.path = home / "issues.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        scope = {"slug": github.slug.lower(), "queue": queue, "namespace": client.namespace}
        if self.state and self.state["scope"] != scope:
            raise FactoryError("Bridge home belongs to a different repository, queue, or namespace")
        self.state.setdefault("scope", scope)
        self.state.setdefault("jobs", {})

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state))
        temporary.replace(self.path)

    async def tick(self):
        for issue in self.github.ready(self.label):
            job_id = f"job-gh-{issue['id']}"
            if job_id in self.state["jobs"]:
                continue
            if not self.github.approved(issue, self.label):
                continue
            # Freeze the issue text at submission; later comments/edits never change a running request.
            feature = parse_issue_body(issue["title"], issue.get("body") or "", url=issue["html_url"],
                                       labels=[item["name"] for item in issue.get("labels", [])])
            try:
                await self.client.start_workflow(
                    FactoryWorkflow.run, feature.model_dump(mode="json"), id=job_id,
                    task_queue=self.queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                pass
            self.state["jobs"][job_id] = {"number": issue["number"], "terminal": False}
            self.save()
        # A stalled job must not prevent updates for unrelated jobs.
        errors = []
        for job_id, record in self.state["jobs"].items():
            if record["terminal"]:
                continue
            try:
                await self.sync_job(job_id, record)
            except Exception as exc:
                errors.append(type(exc).__name__)
        if errors:
            raise FactoryError(f"Could not sync {len(errors)} job(s); retry next poll")

    async def sync_job(self, job_id: str, record: dict):
        # Keep following accepted jobs even after the issue is closed or its label removed.
        handle = self.client.get_workflow_handle(job_id)
        description = await handle.describe(rpc_timeout=timedelta(seconds=10))
        terminal = description.status != WorkflowExecutionStatus.RUNNING
        if description.status == WorkflowExecutionStatus.COMPLETED:
            snapshot = {"job": await handle.result()}
        elif terminal:
            snapshot = {"job": {"stage": "failed"}}
        else:
            snapshot = await handle.query(FactoryWorkflow.snapshot, rpc_timeout=timedelta(seconds=10))
        self.github.comment(record["number"], job_id, status_body(job_id, snapshot))
        # Only mark finished after the GitHub update succeeds.
        record["terminal"] = terminal
        self.save()


async def watch_issues(client, args):
    if args.interval < 10:
        raise FactoryError("Issue polling interval must be at least 10 seconds")
    args.home.mkdir(parents=True, exist_ok=True)
    with (args.home / "issues.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FactoryError("An issue bridge already owns this home") from None
        bridge = IssueBridge(client, GitHubIssues(args.slug), args.home, args.task_queue, args.label)
        print(f"Watching {args.slug}: label={args.label}, queue={args.task_queue}", flush=True)
        while True:
            try:
                await bridge.tick()
            except Exception as exc:
                if args.once:
                    raise
                print(f"Issue sync failed ({type(exc).__name__}); retrying next poll", flush=True)
            if args.once:
                return
            await asyncio.sleep(args.interval)
