from __future__ import annotations

import shutil
import sys
from pathlib import Path

from factory.governance.flags import audit_flag
from factory.models import CommandReport, FailureClass, JobState, VerifyResult
from factory.tools.layer import ToolLayer


class VerificationGate:
    def __init__(self, target_repo: Path, home: Path) -> None:
        self.target_repo = target_repo
        self.home = home

    def run(self, job: JobState, tools: ToolLayer) -> VerifyResult:
        if job.worktree is None or job.plan is None:
            return VerifyResult(
                passed=False,
                commands=[],
                actionable="verification ran before a plan and a worktree existed",
                classification="infra",
            )
        diff = tools.diff(job.worktree.base_sha)
        flag_key = job.feature.flag_key or job.plan.flag_key
        problems: list[str] = []
        if job.feature.user_facing:
            problems.extend(
                audit_flag(diff, flag_key, must_default_off=True)
            )
        commands: list[CommandReport] = []
        commands.extend(self._pytest(job, tools))
        commands.extend(self._lint_and_types(job, tools))
        failed = [item for item in commands if not item.passed]
        passed = not problems and not failed
        lines = list(problems)
        for item in failed:
            lines.append("$ " + " ".join(item.argv) + f" -> {item.exit_code}")
            if item.output:
                lines.append(item.output[-2000:])
        if passed:
            lines.append("pytest, ruff, and the type check passed; the flag defaults off")
        classification: FailureClass = "none"
        if not passed:
            if problems:
                classification = "flag"
            elif any(Path(item.argv[0]).name == "pytest" or "pytest" in item.argv for item in failed):
                classification = "test"
            else:
                classification = "infra"
        return VerifyResult(
            passed=passed,
            commands=commands,
            actionable="\n".join(lines)[:8000],
            classification=classification,
        )

    def _pytest(self, job: JobState, tools: ToolLayer) -> list[CommandReport]:
        assert job.plan is not None
        targets = list(job.plan.test_targets)
        if (tools.worktree / "test_room_mood_api.py").exists() and "test_room_mood_api.py" not in targets:
            targets.append("test_room_mood_api.py")
        if not targets:
            return [
                CommandReport(
                    argv=["pytest"],
                    exit_code=1,
                    output="plan listed no tests",
                    passed=False,
                )
            ]
        ini = self.home / "runs" / job.id / "pytest-gate.ini"
        ini.parent.mkdir(parents=True, exist_ok=True)
        ini.write_text("[pytest]\naddopts =\n")
        argv = self._pytest_bin() + [
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(ini),
            "--rootdir",
            str(tools.worktree),
            *targets,
        ]
        return [tools.run(argv)]

    def _lint_and_types(self, job: JobState, tools: ToolLayer) -> list[CommandReport]:
        assert job.worktree is not None
        changed = tools.changed_files(job.worktree.base_sha)
        added_report = tools.run(
            ["git", "diff", "--name-only", "--diff-filter=A", job.worktree.base_sha, "HEAD"]
        )
        added = [line for line in added_report.output.splitlines() if line.endswith(".py")]
        py_changed = [path for path in changed if path.endswith(".py")]
        reports: list[CommandReport] = []
        lint_targets = added or [path for path in py_changed if path == "app/room_mood.py"]
        if lint_targets:
            ruff = _bin("ruff")
            if ruff is None:
                reports.append(_missing("ruff"))
            else:
                reports.append(tools.run([*ruff, "check", "--isolated", "--select", "E,F", *lint_targets]))
        if (tools.worktree / "app" / "room_mood.py").exists():
            mypy = _bin("mypy")
            if mypy is None:
                reports.append(_missing("mypy"))
            else:
                reports.append(
                    tools.run(
                        [
                            *mypy,
                            "--follow-imports=silent",
                            "--ignore-missing-imports",
                            "--pretty",
                            "app/room_mood.py",
                        ]
                    )
                )
        if py_changed:
            python = _target_python(self.target_repo)
            reports.append(tools.run([python, "-m", "compileall", "-q", *py_changed]))
        return reports

    def _pytest_bin(self) -> list[str]:
        for rel in ("venv/bin/pytest", ".venv/bin/pytest"):
            candidate = self.target_repo / rel
            if candidate.exists():
                return [str(candidate)]
        return [sys.executable, "-m", "pytest"]


def _bin(name: str) -> list[str] | None:
    sibling = Path(sys.executable).parent / name
    if sibling.exists():
        return [str(sibling)]
    found = shutil.which(name)
    if found:
        return [found]
    return None


def _target_python(repo: Path) -> str:
    for rel in ("venv/bin/python", ".venv/bin/python"):
        candidate = repo / rel
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _missing(name: str) -> CommandReport:
    return CommandReport(argv=[name], exit_code=127, output=f"{name} is not installed", passed=False)
