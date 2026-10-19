from __future__ import annotations

import asyncio
import fcntl
import json
import re
import shlex
import subprocess
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from factory.command_output import strip_ansi
from factory.errors import FactoryError, GovernanceError
from factory.gitcmd import git_env
from factory.intake.load import parse_issue_body
from factory.temporal.workflow import FactoryWorkflow
from factory.tools.safety import check_content


class GitHubIssues:
    def __init__(self, slug: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
            raise FactoryError("GitHub repository must be owner/name")
        self.slug = slug
        self.root = f"repos/{slug}"
        self.login: str | None = None

    def api(self, endpoint: str, *, method: str = "GET", body: dict | None = None,
            pages: bool = False, missing_ok: bool = False):
        argv = ["gh", "api", "--hostname", "github.com", endpoint, "--method", method]
        if pages:
            argv += ["--paginate", "--slurp"]
        if body is not None:
            argv += ["--input", "-"]
        result = subprocess.run(argv, input=json.dumps(body) if body is not None else None,
                                text=True, capture_output=True, timeout=60, check=False, env=git_env())
        if result.returncode and missing_ok and "HTTP 404" in result.stderr:
            return None
        if result.returncode:
            raise FactoryError("GitHub request failed; check gh authentication and repository permissions")
        if not result.stdout.strip():
            return None
        value = json.loads(strip_ansi(result.stdout))
        return [item for page in value for item in page] if pages else value

    def ready(self, label: str) -> list[dict]:
        items = self.api(f"{self.root}/issues?state=open&labels={quote(label, safe='')}&per_page=100", pages=True)
        return [item for item in items if "pull_request" not in item]

    def remove_label(self, number: int, label: str) -> None:
        self.api(f"{self.root}/issues/{number}/labels/{quote(label, safe='')}",
                 method="DELETE", missing_ok=True)

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


def status_body(job_id: str, snapshot: dict, *, address: str = "localhost:7235",
                namespace: str = "default") -> str:
    connection = "--address " + shlex.quote(address)
    if namespace != "default":
        connection += " --namespace " + shlex.quote(namespace)
    job = snapshot.get("job", {})
    stage = job.get("stage", "queued")
    lines = [f"Software factory job `{job_id}` — **{stage}**."]
    if stage == "await_human":
        lines += ["", job.get("question") or "Clarification required.", "",
                  "Answer from the factory terminal using the current pause ID:", "```bash",
                  f".venv/bin/python -m factory temporal answer {job_id} "
                  f"{connection} --pause-id {snapshot['pause_id']} --answer 'YOUR ANSWER'", "```"]
    plan = job.get("plan") or {}
    if plan.get("tasks"):
        lines += ["", "Tasks:"]
        for task in plan["tasks"]:
            done = "x" if task["id"] in job.get("completed_task_ids", []) else " "
            lines.append(f"- [{done}] {task['id']}: {task['description'][:400]}")
    if stage == "await_human":
        if plan.get("migration_plan"):
            lines += ["", "Migration/recovery: " + plan["migration_plan"][:1600]]
    if stage == "failed":
        reason = job.get("error") or "Temporal execution ended without a factory result; inspect worker logs."
        try:
            check_content(reason)
        except GovernanceError:
            reason = "Failure details contain credential-like content; inspect local job logs."
        lines += ["", "Failure: " + reason[:1600]]
    pr = job.get("pr") or {}
    if pr.get("url"):
        lines += ["", f"Draft PR: {pr['url']}", "A human must review and merge it."]
    elif stage == "done":
        lines += ["", "Completed with a local PR draft; this worker did not publish to GitHub."]
    lines += ["", "After this job finishes, a maintainer can request a retry using the configured retry label. "
              "Comments are not executed as instructions."]
    return "\n".join(lines)


class IssueBridge:
    """Single-host polling bridge. Temporal IDs deduplicate submission; disk tracks ongoing jobs."""

    def __init__(self, client, github: GitHubIssues, home: Path, queue: str, label: str, *,
                 address: str | None = None, namespace: str | None = None, retry_label: str = "factory:retry"):
        self.client, self.github, self.queue, self.label = client, github, queue, label
        if retry_label == label:
            raise FactoryError("Ready and retry labels must differ")
        self.retry_label = retry_label
        self.address = address or client.service_client.config.target_host
        self.namespace = namespace or client.namespace
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
        for label in (self.label, self.retry_label):
            for issue in self.github.ready(label):
                if issue.get("state", "open") != "open":
                    continue
                base = f"job-gh-{issue['id']}"
                related = {key: record for key, record in self.state["jobs"].items()
                           if key == base or re.fullmatch(re.escape(base) + r"-r\d+", key)}
                if label == self.label:
                    if base in related:
                        continue
                    job_id = base
                else:
                    # A provider pause resumes the same workflow/checkpoints. Other
                    # active jobs still block creating a fresh retry workflow.
                    active = [(key, record) for key, record in related.items() if not record["terminal"]]
                    if active:
                        if len(active) == 1 and self.github.approved(issue, label):
                            await self._resume_provider_pause(*active[0])
                        continue
                    if base not in related or any(record.get("remove_retry_label") for record in related.values()):
                        continue
                    attempt = max([int(key.rsplit("-r", 1)[1]) for key in related if key != base], default=0) + 1
                    job_id = f"{base}-r{attempt}"
                if not self.github.approved(issue, label):
                    continue
                # Freeze the original issue anew; later edits never change a running request.
                feature = parse_issue_body(issue["title"], issue.get("body") or "", url=issue["html_url"],
                                           labels=[item["name"] for item in issue.get("labels", [])])
                try:
                    await self.client.start_workflow(
                        FactoryWorkflow.run, feature.model_dump(mode="json"), id=job_id,
                        task_queue=self.queue, id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    pass
                record = {"number": issue["number"], "terminal": False}
                if label == self.retry_label:
                    record["remove_retry_label"] = self.retry_label
                self.state["jobs"][job_id] = record
                self.save()
        # A stalled job must not prevent updates for unrelated jobs.
        errors = []
        for job_id, record in self.state["jobs"].items():
            try:
                if not record["terminal"]:
                    await self.sync_job(job_id, record)
                if record.get("remove_retry_label"):
                    self.github.remove_label(record["number"], record["remove_retry_label"])
                    del record["remove_retry_label"]
                    self.save()
            except Exception as exc:
                errors.append(f"{job_id}: {type(exc).__name__}: {exc}")
        if errors:
            raise FactoryError(f"Could not sync {len(errors)} job(s); retry next poll: " + "; ".join(errors))

    async def _resume_provider_pause(self, job_id: str, record: dict) -> None:
        if record.get("remove_retry_label"):
            return
        handle = self.client.get_workflow_handle(job_id)
        snapshot = await handle.query(FactoryWorkflow.snapshot, rpc_timeout=timedelta(seconds=10))
        job = snapshot.get("job", {})
        if (job.get("stage") != "await_human" or not (job.get("question") or "").startswith(
                "Provider billing/credentials problem:")):
            return
        await handle.signal(FactoryWorkflow.answer, args=[snapshot["pause_id"], "continue"],
                            rpc_timeout=timedelta(seconds=10))
        record["remove_retry_label"] = self.retry_label
        self.save()

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
        body = status_body(job_id, snapshot, address=self.address, namespace=self.namespace)
        self.github.comment(record["number"], job_id, body)
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
        bridge = IssueBridge(client, GitHubIssues(args.slug), args.home, args.task_queue, args.label,
                             address=args.address, namespace=args.namespace, retry_label=args.retry_label)
        print(f"Watching {args.slug}: label={args.label}, queue={args.task_queue}", flush=True)
        while True:
            try:
                await bridge.tick()
            except Exception as exc:
                if args.once:
                    raise
                print(f"Issue sync failed ({type(exc).__name__}): {exc}; retrying next poll", flush=True)
            if args.once:
                return
            await asyncio.sleep(args.interval)
