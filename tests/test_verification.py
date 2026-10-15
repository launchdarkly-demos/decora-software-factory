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


def test_configured_frontend_checks_run_and_failures_block_shipping(tmp_path):
    from factory.governance.policy import VerificationProfile
    from factory.models import WorktreeInfo
    repo = init_repo(tmp_path / 'frontend')
    rules = policy()
    rules.verification_profiles['ui'] = VerificationProfile(
        description='Controlled test double for browser command execution', kind='browser', paths=['web/*'],
        commands=[['pytest', '-q', 'test_browser_contract.py']])
    tools = ToolLayer(repo, rules)
    base = tools.head_sha()
    tools.write('web/page.html', '<button>Choose home</button>')
    tools.write('test_browser_contract.py', 'def test_contract():\n    assert False, "UI regression"\n')
    tools.commit('UI change', ['web/page.html', 'test_browser_contract.py'])
    job = bare_job(tmp_path, worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=base),
                   plan=_plan([]))
    failed = VerificationGate(repo, tmp_path / 'home').run(job, tools)
    assert not failed.passed
    assert any('test_browser_contract.py' in cmd.argv for cmd in failed.commands)
    assert 'UI regression' in failed.actionable
    rules.verification_profiles.clear()
    blocked = VerificationGate(repo, tmp_path / 'home').run(job, tools)
    assert not blocked.passed
    assert 'browser verification profile' in blocked.actionable
