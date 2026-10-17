from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from factory.models import BuildResult, ReviewReport, Stage
from factory.orchestrator.graph import LEGAL
from tests.support import (
    ROOT,
    ScriptedHarness,
    ScriptedVerifier,
    failed_verify,
    feature,
    init_repo,
    low_plan,
    make_factory,
    passed_verify,
    policy,
)


def test_graph_sends_verification_failures_back_to_the_builder() -> None:
    assert Stage.BUILD in LEGAL[Stage.VERIFY]
    assert Stage.ENSURE_FLAG in LEGAL[Stage.VERIFY]
    assert Stage.REVIEW not in LEGAL[Stage.VERIFY]
    assert Stage.REVIEW in LEGAL[Stage.ENSURE_FLAG]
    assert Stage.FAILED in LEGAL[Stage.ENSURE_FLAG]
    assert Stage.DRAFT_PR not in LEGAL[Stage.VERIFY]


def test_verify_failure_returns_to_the_builder_then_passes(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    harness = ScriptedHarness([low_plan()])
    verifier = ScriptedVerifier([failed_verify(), passed_verify()])
    job = make_factory(tmp_path, repo, harness, verifier=verifier).run(feature())

    assert job.stage == Stage.DONE
    assert job.retries["verify"] == 1
    assert len(job.builds) == 2
    assert job.pr is not None and job.pr.dry_run
    assert job.pr.url is None


def test_missing_file_correction_preserves_budget_for_a_real_verify_failure(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo")
    rules = policy()
    rules.max_retries["verify"] = 1
    verifier = ScriptedVerifier([
        failed_verify("Create planned test file", "missing_files"),
        failed_verify("AssertionError: implementation needs correction"),
        passed_verify(),
    ])
    job = make_factory(tmp_path, repo, ScriptedHarness([low_plan()]),
                       policy=rules, verifier=verifier).run(feature())

    assert job.stage == Stage.DONE, job.error
    assert job.retries == {"missing_files": 1, "verify": 1}
    assert len(job.builds) == 3


def test_missing_planned_files_return_to_builder_before_verification(tmp_path: Path) -> None:
    from factory.agents.apply import apply_proposal
    from factory.models import BuildProposal, FileEdit, PlanTask, WorktreeInfo
    from tests.support import bare_job

    repo = init_repo(tmp_path / "repo")
    test_path = "tests/test_legacy.py"
    fixture_path = "tests/fixtures/legacy.json"
    # This synthetic helper/tests change does not migrate stored data or expose behavior.
    # Low-risk backend_internal work is flag-optional under the change-class policy.
    plan = low_plan(files=["app/migration.py", test_path, fixture_path],
                    test_targets=[test_path], flag_key=None, change_class="backend_internal")
    plan.tasks = [PlanTask(
        id="migration-tests", description="Test legacy data preservation.",
        files=[test_path, fixture_path], acceptance=["Legacy room context keys survive."],
    )]

    def incomplete_build(job, tools):
        return apply_proposal(job, tools, BuildProposal(
            summary="Implementation only", commit_message="Add migration",
            edits=[FileEdit(path="app/migration.py", kind="write", content="VALUE = 1\n")],
        ))

    def corrected_build(job, tools):
        feedback = job.latest_verify()
        assert feedback is not None and not feedback.passed
        assert feedback.commands == []
        for path in [test_path, fixture_path]:
            assert f"Create {path} covering" in feedback.actionable
        assert "Legacy room context keys survive" in feedback.actionable
        return apply_proposal(job, tools, BuildProposal(
            summary="Add required tests and fixture", commit_message="Test legacy preservation",
            edits=[
                FileEdit(path=fixture_path, kind="write", content='{"context_key": "room-1"}\n'),
                FileEdit(path=test_path, kind="write", content=(
                    "import json\nfrom pathlib import Path\n\n\n"
                    "def test_legacy_context_key():\n"
                    "    fixture = Path(__file__).parent / 'fixtures' / 'legacy.json'\n"
                    "    assert json.loads(fixture.read_text())['context_key'] == 'room-1'\n"
                )),
            ],
        ))

    harness = ScriptedHarness([], builds=[incomplete_build, corrected_build])
    factory = make_factory(tmp_path, repo, harness)
    tools = factory._tools(bare_job(
        tmp_path, worktree=WorktreeInfo(path=str(repo), branch="main", base_sha="HEAD")))
    job = bare_job(tmp_path, stage=Stage.BUILD, plan=plan,
                   worktree=WorktreeInfo(path=str(repo), branch="main", base_sha=tools.head_sha()))
    assert factory._build(job).stage == Stage.VERIFY
    assert factory._verify(job).stage == Stage.BUILD
    assert job.error is None
    assert job.retries.get("verify", 0) == 0
    assert job.retries["missing_files"] == 1
    assert factory._build(job).stage == Stage.VERIFY
    assert factory._verify(job).stage == Stage.ENSURE_FLAG, job.latest_verify().actionable
    assert job.latest_verify().passed
    assert job.latest_verify().commands
    assert job.retries == {"missing_files": 1}


def test_multitask_build_finishes_before_full_verification(tmp_path: Path) -> None:
    from factory.agents.apply import apply_proposal
    from factory.models import BuildProposal, FileEdit, PlanTask

    repo = init_repo(tmp_path / "repo")
    paths = ["README.md", "app/migration.py", "tests/fixtures/legacy.json", "tests/test_legacy.py"]
    # A pure dict-copy helper and synthetic fixture need no stored-data migration.
    plan = low_plan(files=paths, test_targets=[paths[-1]], flag_key=None,
                    change_class="backend_internal")
    plan.tasks = [
        PlanTask(id=f"T{i}", description=f"Complete {path}", files=[path],
                 depends_on=[f"T{i - 1}"] if i > 1 else [], acceptance=[f"{path} is complete"])
        for i, path in enumerate(paths, 1)
    ]
    edits = [
        FileEdit(path=paths[0], kind="replace", old="mini decora", new="Migration plan inspected"),
        FileEdit(path=paths[1], kind="write", content="def migrate(data):\n    return dict(data)\n"),
        FileEdit(path=paths[2], kind="write", content='{"context_key": "room-1"}\n'),
        FileEdit(path=paths[3], kind="write", content=(
            "import json\nfrom pathlib import Path\n\nfrom app.migration import migrate\n\n\n"
            "def test_legacy():\n"
            "    fixture = Path(__file__).parent / 'fixtures' / 'legacy.json'\n"
            "    assert migrate(json.loads(fixture.read_text()))['context_key'] == 'room-1'\n"
        )),
    ]

    def build_next_task(job, tools):
        attempt = len(job.builds)
        if attempt:
            assert job.latest_verify().commands == []
            assert job.retries.get("verify", 0) == 0
        return apply_proposal(job, tools, BuildProposal(
            summary=f"Complete T{attempt + 1}", commit_message=f"Complete T{attempt + 1}",
            edits=[edits[attempt]],
        ))

    rules = policy()
    rules.max_retries["verify"] = 0
    # YAML policies may omit missing_files: the default allowance must still apply.
    rules.max_retries.pop("missing_files", None)
    harness = ScriptedHarness([plan], builds=[build_next_task])
    job = make_factory(tmp_path, repo, harness, policy=rules).run(feature())

    assert job.stage == Stage.DONE, job.error or job.question
    assert harness.build_calls == 4
    assert job.retries == {"missing_files": 3}
    assert all(run.commands == [] for run in job.verify_runs[:-1])
    assert job.latest_verify().passed
    assert job.latest_verify().commands
    assert job.pr is not None and job.pr.dry_run


@pytest.mark.parametrize("cap", [None, 0, 1, 4])
def test_missing_file_corrections_have_an_independent_cap(tmp_path: Path, cap: int | None) -> None:
    from factory.models import WorktreeInfo
    from tests.support import bare_job

    repo = init_repo(tmp_path / "repo")
    factory = make_factory(tmp_path, repo)
    if cap is None:
        factory.policy.max_retries.pop("missing_files", None)
    else:
        factory.policy.max_retries["missing_files"] = cap
    limit = 3 if cap is None else cap
    job = bare_job(tmp_path, plan=low_plan(files=["missing.py"]),
                   worktree=WorktreeInfo(path=str(repo), branch="main", base_sha="HEAD"))
    job.retries["verify"] = factory.policy.max_retries["verify"]
    for used in range(1, limit + 1):
        assert factory._verify(job).stage == Stage.BUILD
        assert job.retries["missing_files"] == used
    exhausted = factory._verify(job)
    assert exhausted.stage == Stage.FAILED
    assert "missing planned files correction retries exhausted" in exhausted.error
    assert "missing.py" in exhausted.error
    assert job.retries.get("missing_files", 0) == limit
    assert job.retries["verify"] == factory.policy.max_retries["verify"]


def test_verify_retries_exhausted_fails_without_a_pr(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    verifier = ScriptedVerifier([failed_verify()])
    job = make_factory(tmp_path, repo, ScriptedHarness([low_plan()]), verifier=verifier).run(feature())

    assert job.stage == Stage.FAILED
    assert job.pr is None
    assert "retries exhausted" in (job.error or "")
    assert len(job.verify_runs) == 3


def test_ambiguous_verification_pauses_for_a_human(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    verifier = ScriptedVerifier([failed_verify("which route should this wrap?", "ambiguous")])
    job = make_factory(tmp_path, repo, ScriptedHarness([low_plan()]), verifier=verifier).run(feature())

    assert job.stage == Stage.AWAIT_HUMAN
    assert job.question is not None
    assert "which route" in job.question
    assert job.resume_stage == Stage.PLAN


def test_high_risk_plan_pauses_and_resume_builds(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    harness = ScriptedHarness([low_plan(risk="high")])
    factory = make_factory(tmp_path, repo, harness, verifier=ScriptedVerifier([passed_verify()]))
    paused = factory.run(feature())

    assert paused.stage == Stage.AWAIT_HUMAN
    assert paused.question is not None
    assert "Risk high" in paused.question

    finished = factory.resume(paused.id, "ship it")
    assert finished.stage == Stage.DONE
    assert finished.pr is not None


def test_job_reloads_from_sqlite_across_factories(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    home = tmp_path / "home"
    harness = ScriptedHarness(
        [
            low_plan(ambiguous=True, questions=["What flag key should default off?"], risk="high"),
            low_plan(),
        ]
    )
    first = make_factory(
        tmp_path,
        repo,
        harness,
        home=home,
        verifier=ScriptedVerifier([passed_verify()]),
    )
    paused = first.run(feature())
    assert paused.stage == Stage.AWAIT_HUMAN

    second = make_factory(
        tmp_path,
        repo,
        harness,
        home=home,
        verifier=ScriptedVerifier([passed_verify()]),
    )
    reloaded = second.status(paused.id)
    assert reloaded.stage == Stage.AWAIT_HUMAN
    assert reloaded.question == paused.question

    finished = second.resume(paused.id, "continue")
    assert finished.stage == Stage.DONE


def test_review_changes_requested_loops_to_the_builder(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    harness = ScriptedHarness(
        [low_plan()],
        reviews=[
            ReviewReport(verdict="changes_requested", findings=[], risks=[], summary="fix the flag"),
            ReviewReport(verdict="approve", findings=[], risks=[], summary="ok"),
        ],
    )
    job = make_factory(tmp_path, repo, harness, verifier=ScriptedVerifier([passed_verify()])).run(feature())

    assert job.stage == Stage.DONE
    assert job.retries["review"] == 1
    assert len(job.builds) == 2


def test_stage_timeout_fails_the_job(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    clock = _JumpClock()

    class SlowHarness(ScriptedHarness):
        def build(self, job, tools):
            clock.advance(500)
            return super().build(job, tools)

    rules = policy_with_build_timeout(10)
    job = make_factory(
        tmp_path,
        repo,
        SlowHarness([low_plan()]),
        verifier=ScriptedVerifier([passed_verify()]),
        clock=clock,
        policy=rules,
    ).run(feature())

    assert job.stage == Stage.FAILED
    assert job.error is not None
    assert "timed out" in job.error
    assert job.pr is None


def test_unexpected_build_error_removes_the_worktree(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")

    class Boom(ScriptedHarness):
        def build(self, job, tools):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        make_factory(tmp_path, repo, Boom([low_plan()])).run(feature())

    assert list((tmp_path / "home" / "worktrees").glob("job-*")) == []
    leftover = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", "factory/job-*"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert leftover == ""


def test_bad_edit_returns_to_the_builder(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")

    class Flaky(ScriptedHarness):
        def build(self, job, tools):
            self.build_calls += 1
            if self.build_calls == 1:
                from factory.errors import FactoryError

                raise FactoryError("app/flags.py anchor matched 0 times")
            return super().build(job, tools)

    job = make_factory(
        tmp_path,
        repo,
        Flaky([low_plan()]),
        verifier=ScriptedVerifier([passed_verify()]),
    ).run(feature())

    assert job.stage == Stage.DONE
    assert job.retries["build"] == 1
    assert job.pr is not None


def test_push_command_fails_closed(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")

    class PushHarness(ScriptedHarness):
        def build(self, job, tools):
            tools.run(["git", "push", "origin", "HEAD"])
            raise AssertionError("push should have been denied")

    job = make_factory(tmp_path, repo, PushHarness([low_plan()])).run(feature())
    assert job.stage == Stage.FAILED
    assert job.pr is None
    assert job.error is not None
    assert "denied" in job.error


def test_missing_flag_never_reaches_a_pull_request(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")

    calls = {"n": 0}

    def write_note(job, tools):
        calls["n"] += 1
        tools.write("app/note.py", f"VALUE = {calls['n']}\n")
        tools.write("test_note.py", "def test_note() -> None:\n    assert True\n")
        sha = tools.commit("Add a note without a flag.", ["app/note.py", "test_note.py"])
        return BuildResult(
            commit=sha,
            changed_files=tools.changed_files(job.worktree.base_sha),
            summary="added a note",
            attempt=len(job.builds) + 1,
            flag_default=False,
        )

    harness = ScriptedHarness(
        [low_plan()],
        builds=[write_note],
        reviews=[ReviewReport(verdict="approve", findings=[], risks=[], summary="looks fine")],
    )
    job = make_factory(tmp_path, repo, harness, verifier=ScriptedVerifier([passed_verify()])).run(
        feature(
            user_facing=True,
            flag_key="decor-room-mood",
            acceptance=["The flag key decor-room-mood defaults off in code"],
        )
    )

    assert job.stage == Stage.FAILED
    assert job.pr is None
    assert "FLAG_DEFAULT must be False" in (job.error or "")
    assert not (repo / "app" / "note.py").exists()


def test_room_mood_demo_loop_on_a_worktree(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    spec = ROOT / "samples" / "decor-room-mood.json"
    from factory.intake.load import load_feature

    job = make_factory(tmp_path, repo).run(load_feature(spec))
    diff = (tmp_path / "home" / "runs" / job.id / "diff.patch").read_text()
    first = (tmp_path / "home" / "runs" / job.id / "verify-1.diff").read_text()
    body = Path(job.pr.body_path).read_text() if job.pr else ""

    assert job.stage == Stage.DONE
    assert job.retries["verify"] == 1
    assert len(job.builds) == 2
    assert "FLAG_DEFAULT = True" in first
    assert "FLAG_DEFAULT = False" in diff
    assert "FLAG_DEFAULT = True" not in diff
    assert "Draft pull request" in body
    assert "Flag was not provisioned/verified" in body
    assert job.launchdarkly is not None and job.launchdarkly.mode == "offline"
    assert job.pr is not None and job.pr.dry_run and job.pr.url is None
    assert job.usage.input_tokens > 0
    assert job.review is not None and job.review.verdict == "approve"
    assert not (repo / "app" / "room_mood.py").exists()
    assert (tmp_path / "home" / "runs" / job.id / "summary.json").exists()
    assert (tmp_path / "home" / "runs" / job.id / "trace.jsonl").exists()
    assert (tmp_path / "home" / "runs" / job.id / "implementation_plan.json").exists()
    assert (tmp_path / "home" / "runs" / job.id / "review_report.json").exists()


def test_missing_capabilities_anchor_pauses(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor", capabilities=False)
    from factory.intake.load import load_feature

    job = make_factory(tmp_path, repo).run(load_feature(ROOT / "samples" / "decor-room-mood.json"))
    assert job.stage == Stage.AWAIT_HUMAN
    assert job.question is not None
    assert "capabilities" in job.question
    assert not (repo / "app" / "room_mood.py").exists()


def test_blank_request_fails_at_intake(tmp_path: Path) -> None:
    job = make_factory(tmp_path, tmp_path).run(feature(title="  "))
    assert job.stage == Stage.FAILED
    assert "title" in (job.error or "")


def test_cli_refuses_to_push(capsys) -> None:
    from factory.cli import main

    code = main(["demo", "--push"])
    assert code == 1
    assert "will not push" in capsys.readouterr().err


class _JumpClock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def now(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def policy_with_build_timeout(seconds: float):
    rules = policy()
    limits = dict(rules.stage_timeout_seconds)
    limits["build"] = seconds
    return rules.model_copy(update={"stage_timeout_seconds": limits})


@pytest.mark.parametrize('exhausted', [False, True])
def test_existing_file_write_advances_to_verify_without_retry(tmp_path, exhausted):
    from factory.agents.apply import apply_proposal
    from factory.models import BuildProposal, FileEdit, WorktreeInfo
    from factory.tools.layer import ToolLayer
    from tests.support import bare_job

    repo = init_repo(tmp_path / 'repo')
    content = 'def capabilities():\n    return {"ok": True}\n'

    class WholeFileWrite(ScriptedHarness):
        def build(self, job, tools):
            return apply_proposal(job, tools, BuildProposal(
                summary='Update existing file', commit_message='Update server',
                edits=[FileEdit(path='server.py', kind='write', content=content)]))

    factory = make_factory(tmp_path, repo, WholeFileWrite([]))
    base = ToolLayer(repo, factory.policy).head_sha()
    job = bare_job(tmp_path, stage=Stage.BUILD, plan=low_plan(files=['server.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=base))
    if exhausted:
        job.retries['build'] = factory.policy.max_retries['build']
    retries = dict(job.retries)
    result = factory._build(job)
    assert result.stage == Stage.VERIFY
    assert result.error is None
    assert job.retries == retries
    assert job.govern_feedback is None
    assert (repo / 'server.py').read_text() == content
    assert len(job.builds) == 1
    assert job.builds[0].changed_files == ['server.py']
