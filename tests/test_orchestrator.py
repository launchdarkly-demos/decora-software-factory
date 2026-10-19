from __future__ import annotations

import subprocess
from pathlib import Path

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
    assert job.verify_retries_used == 1
    assert len(job.builds) == 2
    assert job.pr is not None and job.pr.dry_run
    assert job.pr.url is None


def test_verify_retries_exhausted_fails_without_a_pr(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    verifier = ScriptedVerifier([failed_verify()])
    job = make_factory(tmp_path, repo, ScriptedHarness([low_plan()]), verifier=verifier).run(feature())

    assert job.stage == Stage.FAILED
    assert job.pr is None
    assert "E22: Job exhausted verify_retries (3)" in (job.error or "")
    assert len(job.verify_runs) == 4


def test_ambiguous_verification_returns_to_builder(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    verifier = ScriptedVerifier([failed_verify("which route should this wrap?", "ambiguous"), passed_verify()])
    job = make_factory(tmp_path, repo, ScriptedHarness([low_plan()]), verifier=verifier).run(feature())

    assert job.stage == Stage.DONE
    assert job.task_retries == {}
    assert job.verify_retries_used == 1
    assert len(job.verify_runs) == 2


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


def test_build_timeout_retries_infrastructure_then_pauses(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("factory.orchestrator.machine.time.sleep", lambda _: None)
    repo = init_repo(tmp_path / "decor")
    clock = _JumpClock()

    class SlowHarness(ScriptedHarness):
        def build(self, job, tools):
            clock.advance(500)
            raise TimeoutError("call timeout")

    rules = policy_with_build_timeout(10)
    job = make_factory(
        tmp_path,
        repo,
        SlowHarness([low_plan()]),
        verifier=ScriptedVerifier([passed_verify()]),
        clock=clock,
        policy=rules,
    ).run(feature())

    assert job.stage == Stage.AWAIT_HUMAN
    assert job.task_retries == {}
    assert job.builder_calls == 4
    assert "infrastructure retries exhausted" in job.question
    assert job.pr is None


def test_unexpected_build_error_uses_task_budget_and_removes_the_worktree(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")

    class Boom(ScriptedHarness):
        def build(self, job, tools):
            raise RuntimeError("boom")

    job = make_factory(tmp_path, repo, Boom([low_plan()])).run(feature())
    assert job.stage == Stage.FAILED
    assert "exhausted 3 retries" in job.error
    assert "RuntimeError" in job.error

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
    assert job.task_retries["T1"] == 1
    assert job.verify_retries_used == 0
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
        [low_plan(files=['app/note.py', 'test_note.py'])],
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
    assert job.verify_retries_used == 1
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


def test_missing_capabilities_anchor_uses_bounded_task_corrections(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor", capabilities=False)
    from factory.intake.load import load_feature

    job = make_factory(tmp_path, repo).run(load_feature(ROOT / "samples" / "decor-room-mood.json"))
    assert job.stage == Stage.FAILED
    assert job.task_retries == {"T1": 3}
    assert "capabilities" in job.error
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


def test_existing_file_write_is_applied(tmp_path):
    from factory.agents.apply import apply_proposal
    from factory.models import BuildProposal, FileEdit, WorktreeInfo
    from tests.support import bare_job

    repo = init_repo(tmp_path / 'repo')

    class FullWrite(ScriptedHarness):
        def build(self, job, tools):
            return apply_proposal(job, tools, BuildProposal(
                summary='Update existing file', commit_message='Update server',
                edits=[FileEdit(path='server.py', kind='write', content='VALUE = 42\n')]))

    factory = make_factory(tmp_path, repo, FullWrite([]))
    job = bare_job(tmp_path, stage=Stage.BUILD, plan=low_plan(files=['server.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha='HEAD'))
    result = factory._build(job)
    assert result.stage == Stage.VERIFY
    assert job.task_retries == {}
    assert (repo / 'server.py').read_text() == 'VALUE = 42\n'
    assert job.checkpoint_shas['T1'] == [job.builds[-1].commit]
