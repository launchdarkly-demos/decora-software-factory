from pathlib import Path

from factory.agents.reviewer import criterion_covered
from factory.intake.load import load_feature
from factory.models import ImplementationPlan
from factory.tools.layer import ToolLayer
from factory.verification.gate import VerificationGate
from tests.support import ROOT, bare_job, init_repo, policy


def test_gate_passes_a_real_pytest_and_records_a_failure(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "decor")
    from factory.execution.worktree import WorktreeManager

    info = WorktreeManager(tmp_path / "worktrees").create(repo, "job-gate")
    tools = ToolLayer(Path(info.path), policy(), target_repo=repo)
    tools.write("test_ok.py", "def test_ok() -> None:\n    value = 1\n    assert value == 1\n")
    tools.commit("Add a passing test.", ["test_ok.py"])
    job = bare_job(
        tmp_path,
        worktree=info,
        plan=_plan(["test_ok.py"]),
        feature={"id": "gate", "title": "Gate", "summary": "Check the gate.", "user_facing": False},
    )
    result = VerificationGate(repo, tmp_path / "home").run(job, tools)
    assert result.passed, result.actionable

    tools.write("test_bad.py", "def test_bad() -> None:\n    assert False\n")
    tools.commit("Add a failing test.", ["test_bad.py"])
    job.plan = _plan(["test_bad.py"])
    failed = VerificationGate(repo, tmp_path / "home").run(job, tools)
    assert not failed.passed
    assert failed.classification == "test"
    assert "assert False" in failed.actionable
    WorktreeManager(tmp_path / "worktrees").destroy(repo, info)


def test_sample_acceptance_is_visible_in_the_diff() -> None:
    spec = load_feature(ROOT / "samples" / "decor-room-mood.json")
    diff = '\n'.join(
        [
            '+FLAG_KEY = "decor-room-mood"',
            "+FLAG_DEFAULT = False",
            '+MOOD_LINE = "calm linen"',
            '+ROOM_MOOD_FLAG = "decor-room-mood"',
        ]
    )
    for criterion in spec.acceptance:
        assert criterion_covered(criterion, diff), criterion


def _plan(targets: list[str]) -> ImplementationPlan:
    return ImplementationPlan(
        summary="gate",
        files=targets,
        steps=["test"],
        flag_key=None,
        flag_default=False,
        test_targets=targets,
        risk="low",
        ambiguous=False,
        questions=[],
        recipe="flagged_capability_field",
    )
