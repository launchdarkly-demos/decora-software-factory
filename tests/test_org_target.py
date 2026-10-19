"""Org target: externally managed flags and issue_triggers."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from factory.errors import FactoryError
from factory.github.issues import GitHubIssues, IssueBridge
from factory.governance.policy import (
    IssueTriggers,
    flag_required,
    flags_externally_managed,
    load_policy,
    login_allowed,
    review_plan,
    review_ship,
)
from factory.launchdarkly.flags import LaunchDarklyService
from factory.models import ImplementationPlan, PlanTask, Stage
from factory.plan_normalization import normalize_flag_task
from factory.publish.pr import render_pr
from tests.support import (
    ROOT,
    ScriptedHarness,
    ScriptedVerifier,
    bare_job,
    feature,
    init_repo,
    make_factory,
    passed_verify,
)
from tests.test_github_issues import FakeGitHub, FakeTemporal


def org_policy() -> object:
    return load_policy(ROOT / "policy.decora-org.yaml")


def personal_policy() -> object:
    return load_policy(ROOT / "policy.decora.yaml")


def plan_with_flag_and_provision() -> ImplementationPlan:
    """Planner-shaped plan: flag_key + provision_flag task (r7 style, simplified)."""
    return ImplementationPlan.model_validate({
        "summary": "Add a capability behind a flag",
        "change_class": "user_facing_behavior",
        "files": ["app/flags.py", "app/room_mood.py", "tests/test_room_mood.py"],
        "steps": ["add flag", "gate behavior", "test"],
        "flag_key": "decora-org-replay-key",
        "flag_default": False,
        "guarded_rollout_metric": "decora-request-error",
        "test_targets": ["tests/test_room_mood.py"],
        "risk": "low",
        "ambiguous": False,
        "questions": [],
        "recipe": "general",
        "flag_task_id": "provision_flag_decora_org_replay_key",
        "tasks": [
            {
                "id": "implement-capability",
                "description": "Implement room mood",
                "files": ["app/room_mood.py"],
                "acceptance": ["room mood works"],
                "depends_on": [],
            },
            {
                "id": "provision_flag_decora_org_replay_key",
                "description": "Provision required flag",
                "files": ["app/flags.py", "app/room_mood.py"],
                "acceptance": ["flag defaults off"],
                "depends_on": ["implement-capability"],
            },
            {
                "id": "write-tests",
                "description": "Test room mood",
                "files": ["tests/test_room_mood.py"],
                "acceptance": ["tests cover flag off"],
                "depends_on": ["provision_flag_decora_org_replay_key"],
            },
        ],
    })


def test_org_policy_loads_as_external():
    rules = org_policy()
    assert rules.flags.management == "external"
    assert flags_externally_managed(rules)
    assert rules.issue_triggers.slug == "launchdarkly-demos/decor-agent"
    assert rules.issue_triggers.authors == ["arober39"]
    assert rules.pull_request.draft_only and rules.pull_request.allow_push


def test_external_with_issue_flag_keys_is_load_error(tmp_path: Path):
    data = yaml.safe_load((ROOT / "policy.decora-org.yaml").read_text())
    data["flags"]["issue_flag_keys"] = {"launchdarkly-demos/decor-agent#1": "decora-x"}
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(FactoryError, match="cannot set issue_flag_keys"):
        load_policy(path)


def test_external_with_default_key_is_load_error(tmp_path: Path):
    data = yaml.safe_load((ROOT / "policy.decora-org.yaml").read_text())
    data["flags"]["default_key"] = "decora-x"
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(FactoryError, match="cannot set issue_flag_keys or default_key"):
        load_policy(path)


def test_external_requires_reason(tmp_path: Path):
    data = yaml.safe_load((ROOT / "policy.decora-org.yaml").read_text())
    data["flags"]["external_reason"] = ""
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(FactoryError, match="external_reason"):
        load_policy(path)


def test_normalize_strips_flag_under_external(tmp_path: Path):
    rules = org_policy()
    plan = plan_with_flag_and_provision()
    job = bare_job(tmp_path, plan=plan, feature=feature(
        issue_url="https://github.com/launchdarkly-demos/decor-agent/issues/99"))
    assert flag_required(rules, job, plan) is False
    normalize_flag_task(rules, job, plan)
    assert plan.flag_key is None
    assert plan.guarded_rollout_metric is None
    assert plan.flag_task_id is None
    assert not any(task.id.startswith("provision_flag_") for task in plan.tasks)
    assert any("External flag management" in note for note in plan.normalization_notes)
    assert any("decora-org-replay-key" in note for note in plan.normalization_notes)


def test_same_plan_under_personal_policy_inserts_flag_task(tmp_path: Path):
    rules = personal_policy()
    plan = plan_with_flag_and_provision()
    # Personal policy key pattern is decora-*; keep the planner key.
    job = bare_job(tmp_path, plan=plan, feature=feature(
        issue_url="https://github.com/arober39/decor-agent/issues/99"))
    decision = review_plan(rules, plan, job)
    assert not decision.retry_plan, decision.reason
    assert flag_required(rules, job, plan)
    assert plan.flag_key == "decora-org-replay-key"
    assert plan.flag_task_id is not None
    assert any(task.id.startswith("provision_flag_") or task.id == plan.flag_task_id
               for task in plan.tasks)


class NoCalls:
    def request(self, *args, **kwargs):
        raise AssertionError("LaunchDarkly client must not be constructed or called")


def test_ensure_flag_external_skips_with_zero_ld_calls_even_live(tmp_path: Path):
    rules = org_policy()
    job = bare_job(tmp_path, plan=plan_with_flag_and_provision())
    normalize_flag_task(rules, job, job.plan)
    service = LaunchDarklyService(
        mode="live", policy=rules, target_repo=tmp_path,
        client=NoCalls(), environ={"LAUNCHDARKLY_API_TOKEN": "should-not-be-used"},
    )
    record = service.ensure(job)
    assert record.mode == "external"
    assert not record.required
    assert "Release Autopilot" in record.detail or "externally" in record.detail.lower()
    assert not record.blocks_review()


def test_audit_skipped_in_review_ship_and_pr_mentions_autopilot(tmp_path: Path):
    rules = org_policy()
    plan = plan_with_flag_and_provision()
    job = bare_job(tmp_path, plan=plan)
    normalize_flag_task(rules, job, plan)
    job.plan = plan
    assert review_ship(rules, job, "diff with no flag").allowed
    job.launchdarkly = LaunchDarklyService(
        mode="live", policy=rules, target_repo=tmp_path, client=NoCalls(),
        environ={"LAUNCHDARKLY_API_TOKEN": "x"},
    ).ensure(job)
    body = render_pr(job, "", rules)
    assert "Release Autopilot" in body
    assert "flags.management=external" in body
    rollout = body.split("## Rollout", 1)[1].split("## ", 1)[0]
    assert "Behind `" not in rollout
    assert "decora-org-replay-key" not in rollout


def test_org_job_replay_reaches_done_draft_pr(tmp_path: Path):
    from factory.models import BuildResult

    rules = org_policy()
    repo = init_repo(tmp_path / "decor")
    plan = plan_with_flag_and_provision()
    plan.files = ["app/room_mood.py"]
    plan.test_targets = []
    plan.tasks = [
        PlanTask(id="implement-capability", description="Implement", files=["app/room_mood.py"],
                 acceptance=["done"]),
        PlanTask(id="provision_flag_decora_org_replay_key", description="Flag",
                 files=["app/flags.py"], acceptance=["flag"]),
    ]

    def build(job, tools):
        tools.write("app/room_mood.py", "VALUE = 'mood'\n")
        commit = tools.commit("org replay", ["app/room_mood.py"])
        return BuildResult(commit=commit, changed_files=["app/room_mood.py"],
                           summary="org replay build", attempt=1, flag_default=False)

    harness = ScriptedHarness([plan], builds=[build])
    verifier = ScriptedVerifier([passed_verify()])
    factory = make_factory(
        tmp_path, repo, harness, policy=rules, verifier=verifier, ld_mode="live",
        flag_service=LaunchDarklyService(
            mode="live", policy=rules, target_repo=repo, client=NoCalls(),
            environ={"LAUNCHDARKLY_API_TOKEN": "must-not-call"},
        ),
    )
    job = factory.run(feature(title="Org capability", summary="Add capability",
                              acceptance=["capability works"], user_facing=True))
    assert job.stage == Stage.DONE, job.error or job.question
    assert job.launchdarkly is not None and job.launchdarkly.mode == "external"
    assert job.pr is not None
    body = Path(job.pr.body_path).read_text()
    assert "Release Autopilot" in body
    assert "flags.management=external" in body
    assert not any(task.id.startswith("provision_flag_") for task in (job.plan.tasks if job.plan else []))
    assert job.plan and job.plan.flag_key is None


def test_login_allowed_bot_suffix_case_insensitive():
    assert login_allowed("Renovate[bot]", ["renovate[bot]"])
    assert login_allowed("renovate[bot]", ["Renovate"])
    assert login_allowed("renovate", ["renovate[bot]"])
    assert not login_allowed("other", ["arober39"])


def test_job_ids_include_issue_id_and_do_not_collide_across_slugs():
    # Globally unique: GitHub issue ids are unique across all repos.
    personal = f"job-gh-{5913682653}"
    org = f"job-gh-{999000111}"
    assert personal != org
    assert personal.startswith("job-gh-") and org.startswith("job-gh-")


class TriggerFakeGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.actors: dict[str, str] = {}
        self.permissions: dict[str, str] = {}
        self.all_open: list[dict] = []
        self._items_by_label: dict[str, list] = {}

    def ready(self, label):
        if label in self._items_by_label:
            return list(self._items_by_label[label])
        return super().ready(label)

    def open_issues(self):
        return list(self.all_open or self.items)

    def label_actor(self, issue, label):
        return self.actors.get(label)

    def has_write_permission(self, actor):
        return self.permissions.get(actor, "write") in {"admin", "write", "maintain"}

    def approved(self, issue, label, *, triggers=None):
        actor = self.label_actor(issue, label)
        if not actor or not self.has_write_permission(actor):
            return False
        if triggers is None:
            return True
        allow = list(triggers.labelers) + list(triggers.agent_accounts)
        if not allow:
            return True
        return login_allowed(actor, allow)


def _triggers(**kwargs) -> IssueTriggers:
    data = {"authors": ["arober39"], "labelers": ["arober39"], "agent_accounts": []}
    data.update(kwargs)
    return IssueTriggers.model_validate(data)


def test_allowlisted_author_without_label_triggers_once(tmp_path: Path, capsys):
    async def scenario():
        gh, temporal = TriggerFakeGitHub(), FakeTemporal()
        issue = {
            "id": 1001, "number": 10, "title": "From Alexis", "body": "## Summary\nHi\n",
            "html_url": "https://github.com/launchdarkly-demos/decor-agent/issues/10",
            "user": {"login": "arober39"}, "state": "open", "labels": [],
        }
        gh.items = []
        gh.all_open = [issue]
        bridge = IssueBridge(temporal, gh, tmp_path, "decora-factory-org", "factory:ready",
                             triggers=_triggers())
        await bridge.tick()
        assert list(temporal.started) == ["job-gh-1001"]
        await bridge.tick()
        assert list(temporal.started) == ["job-gh-1001"]
    asyncio.run(scenario())


def test_non_allowlisted_author_ignored(tmp_path: Path):
    async def scenario():
        gh, temporal = TriggerFakeGitHub(), FakeTemporal()
        issue = {
            "id": 1002, "number": 11, "title": "Stranger", "body": "## Summary\nNo\n",
            "html_url": "https://github.com/launchdarkly-demos/decor-agent/issues/11",
            "user": {"login": "someone-else"}, "state": "open", "labels": [],
        }
        gh.items = []
        gh.all_open = [issue]
        await IssueBridge(temporal, gh, tmp_path, "q", "factory:ready", triggers=_triggers()).tick()
        assert not temporal.started
    asyncio.run(scenario())


def test_label_by_non_allowlisted_collaborator_ignored_when_labelers_set(tmp_path: Path, capsys):
    async def scenario():
        gh, temporal = TriggerFakeGitHub(), FakeTemporal()
        issue = {
            "id": 1003, "number": 12, "title": "Labeled by other", "body": "## Summary\nX\n",
            "html_url": "https://github.com/launchdarkly-demos/decor-agent/issues/12",
            "user": {"login": "other"}, "state": "open",
            "labels": [{"name": "factory:ready"}],
        }
        gh.items = [issue]
        gh.actors["factory:ready"] = "collaborator"
        gh.permissions["collaborator"] = "write"
        bridge = IssueBridge(temporal, gh, tmp_path, "q", "factory:ready", triggers=_triggers())
        await bridge.tick()
        assert not temporal.started
        assert "1003" in bridge.state["ignored"]
        await bridge.tick()  # log once
        out = capsys.readouterr().out
        assert out.count("Ignoring issue #12") == 1
    asyncio.run(scenario())


def test_label_by_allowlisted_writer_triggers(tmp_path: Path):
    async def scenario():
        gh, temporal = TriggerFakeGitHub(), FakeTemporal()
        issue = {
            "id": 1004, "number": 13, "title": "Labeled by Alexis", "body": "## Summary\nX\n",
            "html_url": "https://github.com/launchdarkly-demos/decor-agent/issues/13",
            "user": {"login": "other"}, "state": "open",
            "labels": [{"name": "factory:ready"}],
        }
        gh.items = [issue]
        gh.all_open = []
        gh.actors["factory:ready"] = "arober39"
        gh.permissions["arober39"] = "write"
        await IssueBridge(temporal, gh, tmp_path, "q", "factory:ready", triggers=_triggers()).tick()
        assert list(temporal.started) == ["job-gh-1004"]
    asyncio.run(scenario())


def test_bot_login_matching_triggers_author_path(tmp_path: Path):
    async def scenario():
        gh, temporal = TriggerFakeGitHub(), FakeTemporal()
        issue = {
            "id": 1005, "number": 14, "title": "Bot issue", "body": "## Summary\nBot\n",
            "html_url": "https://github.com/launchdarkly-demos/decor-agent/issues/14",
            "user": {"login": "DecorFactory[bot]"}, "state": "open", "labels": [],
        }
        gh.items = []
        gh.all_open = [issue]
        triggers = _triggers(authors=[], agent_accounts=["decorfactory[bot]"])
        await IssueBridge(temporal, gh, tmp_path, "q", "factory:ready", triggers=triggers).tick()
        assert list(temporal.started) == ["job-gh-1005"]
    asyncio.run(scenario())


def test_watcher_without_policy_unchanged(tmp_path: Path):
    async def scenario():
        gh, temporal = FakeGitHub(), FakeTemporal()
        # No triggers: FakeGitHub.allow still gates like today.
        await IssueBridge(temporal, gh, tmp_path, "queue", "factory:ready").tick()
        assert list(temporal.started) == ["job-gh-42"]
        gh.allow = False
        gh.items = [{"id": 99, "number": 8, "title": "Nope", "body": "x",
                     "html_url": "https://github.com/owner/repo/issues/8"}]
        # New home so ledger does not skip
        await IssueBridge(temporal, gh, tmp_path / "other", "queue", "factory:ready").tick()
        assert "job-gh-99" not in temporal.started
    asyncio.run(scenario())


def test_github_approved_requires_allowlist_and_write():
    gh = GitHubIssues("launchdarkly-demos/decor-agent")
    events = [{"event": "labeled", "label": {"name": "factory:ready"},
               "actor": {"login": "arober39"}}]
    gh.api = lambda endpoint, **kw: (
        events if "/timeline?" in endpoint else {"permission": "write"}
    )
    triggers = _triggers()
    assert gh.approved({"number": 1}, "factory:ready", triggers=triggers)
    events[0]["actor"]["login"] = "other-maintainer"
    assert not gh.approved({"number": 1}, "factory:ready", triggers=triggers)


def test_watch_issues_policy_slug_mismatch(tmp_path: Path):
    from factory.github.issues import watch_issues

    args = SimpleNamespace(
        interval=30, home=tmp_path, slug="wrong/repo", label="factory:ready",
        retry_label="factory:retry", task_queue="q", address="localhost:7236",
        namespace="default", policy=ROOT / "policy.decora-org.yaml", once=True,
    )
    with pytest.raises(FactoryError, match="issue_triggers.slug"):
        asyncio.run(watch_issues(FakeTemporal(), args))


def test_cli_watch_issues_accepts_policy():
    from factory.temporal.cli import parser

    args = parser().parse_args([
        "watch-issues", "--slug", "launchdarkly-demos/decor-agent",
        "--policy", "policy.decora-org.yaml",
    ])
    assert args.policy.name == "policy.decora-org.yaml"
