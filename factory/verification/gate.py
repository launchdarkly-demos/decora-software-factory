from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from factory.governance.flags import audit_flag
from factory.governance.policy import flag_required
from factory.models import CommandReport, FailureClass, JobState, VerifyResult
from factory.tools.layer import ToolLayer
from factory.verification.profiles import prerequisites, selected_profiles


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
        missing = [path for path in dict.fromkeys(job.plan.files) if not tools._resolve(path).is_file()]
        if missing:
            return VerifyResult(
                passed=False,
                commands=[],
                actionable="Planned files are missing; verification was not run.\n"
                + "\n".join(_create_file_feedback(job, path) for path in missing),
                classification="test",
            )
        diff = tools.diff(job.worktree.base_sha)
        flag_key = job.feature.flag_key or job.plan.flag_key
        changed = tools.changed_files(job.worktree.base_sha)
        problems: list[str] = prerequisites(tools.policy, job.plan, changed)
        flag_problems: list[str] = []
        if flag_required(tools.policy, job):
            flag_problems.extend(
                audit_flag(diff, flag_key, must_default_off=tools.policy.flags.must_default_off)
            )
        problems.extend(flag_problems)
        commands: list[CommandReport] = []
        commands.extend(self._pytest(job, tools))
        commands.extend(self._lint_and_types(job, tools))
        for name in selected_profiles(tools.policy, job.plan, changed):
            if name not in tools.policy.verification_profiles:
                continue
            for argv in tools.policy.verification_profiles[name].commands:
                resolved = [_target_python(self.target_repo) if arg == '{python}' else arg for arg in argv]
                commands.append(tools.run(resolved))
        if not commands:
            problems.append('No verification commands executed; configure a test or verification profile.')
        profiles = selected_profiles(tools.policy, job.plan, changed)
        has_behavior_checks = bool(job.plan.test_targets or tools.policy.regression_tests) or any(
            tools.policy.verification_profiles[name].kind in {'test', 'browser', 'migration'}
            for name in profiles if name in tools.policy.verification_profiles
        )
        if not has_behavior_checks:
            problems.append('No behavioral test checks selected; static checks alone cannot approve a change.')
        failed = [item for item in commands if not item.passed]
        passed = not problems and not failed
        lines = list(problems)
        for item in failed:
            lines.append("$ " + " ".join(item.argv) + f" -> {item.exit_code}")
            if item.exit_code == 4:
                for target in re.findall(r"ERROR: file or directory not found: ([^\n]+)", item.output):
                    lines.append(_create_file_feedback(job, target.strip().split('::')[0]))
            if item.output:
                lines.append(item.output[-2000:])
        if passed:
            lines.append("All executed verification commands passed."
                         + (" The flag audit passed." if flag_required(tools.policy, job) else ""))
        classification: FailureClass = "none"
        if not passed:
            if flag_problems:
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
        targets = list(dict.fromkeys([*job.plan.test_targets, *tools.policy.regression_tests]))
        if not targets:
            return []
        missing = []
        for target in targets:
            if target.startswith('-'):
                raise ValueError('pytest targets must be repository paths, not options')
            path = target.split('::')[0]
            if not tools._resolve(path).exists():
                missing.append(path)
        if missing:
            return [CommandReport(
                argv=self._pytest_bin() + targets,
                exit_code=4,
                output="pytest was not run because targets are missing.\n"
                + "\n".join(f"ERROR: file or directory not found: {path}" for path in missing),
                passed=False,
            )]
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
        lint_targets = added
        if lint_targets:
            ruff = _bin("ruff")
            if ruff is None:
                reports.append(_missing("ruff"))
            else:
                reports.append(tools.run([*ruff, "check", "--isolated", "--select", "E,F", *lint_targets]))
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


def _create_file_feedback(job: JobState, path: str) -> str:
    assert job.plan is not None
    tasks = [task for task in job.plan.tasks if path in task.files]
    coverage = [text for task in tasks for text in [task.description, *task.acceptance]]
    if not coverage:
        coverage = [*job.plan.steps, *job.feature.acceptance] or [job.plan.summary]
    return f"Create {path} covering: " + " ".join(coverage)
