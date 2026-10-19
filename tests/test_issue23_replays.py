"""#23: r2 receipts replayed with real pytest, safe fixes and local draft publication."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from factory.agents.apply import apply_proposal
from factory.command_output import plain_command, strip_ansi
from factory.errors import FactoryError
from factory.governance.policy import VerificationProfile
from factory.models import CommandReport, ImplementationPlan, VerifyResult, WorktreeInfo
from factory.tools.layer import ToolLayer
from factory.verification.commands import pytest_targets
from factory.verification.gate import VerificationGate
from tests.support import bare_job, init_repo, low_plan, policy
from tests.test_issue22_replays import TEST, assert_draft, proposal, run, write


@pytest.mark.parametrize(('command', 'targets'), [
    (['pytest', '-q', TEST], [TEST]),
    (['python', '-m', 'pytest', '--color=yes', '-c', 'foreign.ini', '--rootdir=/foreign', TEST + '::test_ok'],
     [TEST + '::test_ok']),
    (['pytest', '-k', 'something', '-p', 'plugin', '--override-ini', 'pythonpath=/foreign', TEST], [TEST]),
    (['pytest', '-q'], ['.']),
    (['pytest', '--', TEST], [TEST]),
])
def test_quick_commands_become_targets(command, targets):
    assert pytest_targets([command, command]) == targets


@pytest.mark.parametrize('command', [
    ['git', 'status'], ['ruff', 'check', '.'], ['python', '-c', 'print(1)'],
    ['bash', '-c', 'pytest'], [], ['pytest', '--unknown', TEST], ['pytest', '-c'],
])
def test_quick_commands_refused_before_edits(tmp_path, command):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['safe.txt']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    with pytest.raises(FactoryError):
        apply_proposal(job, tools, proposal([write('safe.txt', 'must not be written')], commands=[command]))
    assert not (repo / 'safe.txt').exists()
    assert tools.head_sha() == job.worktree.base_sha


def test_quick_gate_isolates_config_root_and_cwd(tmp_path, monkeypatch):
    # Nest the target under a hostile ancestor config, as with factory worktrees.
    (tmp_path / 'pyproject.toml').write_text('[tool.pytest.ini_options]\naddopts="--bad-ancestor-option"\n')
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write('app/value.py', 'VALUE = 7\n')
    tools.write(TEST, 'from app.value import VALUE\n\ndef test_ok():\n    assert VALUE == 7\n')
    job = bare_job(tmp_path, plan=low_plan(test_targets=[TEST]))
    monkeypatch.setenv('PYTEST_ADDOPTS', '--bad-env-option')
    monkeypatch.setenv('PYTHONPATH', '/foreign')
    result = VerificationGate(repo, tmp_path / 'home').run_quick(job, tools, [TEST])
    assert result.passed, result.actionable
    argv = result.commands[0].argv
    assert argv[argv.index('--rootdir') + 1] == str(repo)
    config = Path(argv[argv.index('-c') + 1])
    assert config.name == 'pytest-gate.ini' and str(repo) in config.read_text()
    assert '--color=no' in argv


def lint_setup(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['app/value.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    return repo, tools, job


def test_safe_fixes_and_format_are_in_checkpoint_and_unsafe_fix_is_not(tmp_path):
    repo, tools, job = lint_setup(tmp_path)
    tools.write('unrelated.py', 'import os\n')
    result = apply_proposal(job, tools, proposal([write('app/value.py',
        'import os\n\ndef value():\n x=1\n unused = len([])\n return x\n')]))
    text = tools.read('app/value.py')
    assert 'import os' not in text and '    x = 1' in text
    assert 'unused = len([])' in text  # F841's unsafe removal must not run.
    assert tools.run(['git', 'show', result.commit + ':app/value.py']).output == text
    assert (repo / 'unrelated.py').read_text() == 'import os\n'
    assert tools.run(['git', 'status', '--porcelain', '--', 'app/value.py']).output == ''


def test_default_lint_ignores_style_and_fixable_but_blocks_correctness(tmp_path):
    repo, tools, job = lint_setup(tmp_path)
    tools.write('app/value.py', 'import os\nLONG = "' + 'x' * 160 + '"\n')
    tools.commit('Unformatted change', ['app/value.py'])
    gate = VerificationGate(repo, tmp_path / 'home')
    reports = gate._lint_and_types(job, tools)
    assert all(report.passed for report in reports)
    assert '--isolated' in reports[0].argv and 'E9,F' in reports[0].argv
    tools.write('app/value.py', 'VALUE = missing_name\n')
    tools.commit('Undefined name', ['app/value.py'])
    reports = gate._lint_and_types(job, tools)
    assert not reports[0].passed and 'F821' in reports[0].output


@pytest.mark.parametrize('config', ['ruff.toml', '.ruff.toml', 'pyproject.toml', 'app/ruff.toml'])
def test_repo_ruff_config_controls_fixes_format_and_gate(tmp_path, config):
    repo, tools, job = lint_setup(tmp_path)
    prefix = '[tool.ruff]\n' if config == 'pyproject.toml' else ''
    lint = '[tool.ruff.lint]\n' if config == 'pyproject.toml' else '[lint]\n'
    tools.write(config, prefix + 'line-length = 100\n' + lint + 'select = ["E501"]\n')
    tools.commit('Repo Ruff config', [config])
    # Existing module modifications, not just additions, must be linted.
    tools.write('app/value.py', 'VALUE = 1\n')
    tools.commit('Existing module', ['app/value.py'])
    job.worktree.base_sha = tools.head_sha()
    result = apply_proposal(job, tools, proposal([write('app/value.py',
        'import os\nVALUE="' + 'x' * 105 + '"\n')]))
    text = tools.read('app/value.py')
    assert 'import os' in text  # F401 is disabled by repo config.
    reports = VerificationGate(repo, tmp_path / 'home')._lint_and_types(job, tools)
    assert not reports[0].passed and 'E501' in reports[0].output
    assert '--isolated' not in reports[0].argv and '--config' in reports[0].argv
    assert tools.run(['git', 'show', result.commit + ':app/value.py']).output == text


def test_plain_tools_and_reports(tmp_path, monkeypatch):
    tools = ToolLayer(init_repo(tmp_path / 'repo'), policy())
    tools.policy.command_environment['NO_COLOR'] = '0'
    observed = {}

    def runner(argv, **kwargs):
        observed.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 1, '\x1b[31mFAILED test.py::test_x\x1b[0m',
                                           '\x1b]8;;https://example.test\x1b\\link\x1b]8;;\x1b\\')

    monkeypatch.setattr('factory.tools.layer.subprocess.run', runner)
    report = tools.run(['pytest', '--color=yes', 'test.py'])
    assert observed['env']['NO_COLOR'] == '1' and observed['cwd'] == tools.worktree
    assert '--color=yes' not in report.argv and '--color=no' in report.argv
    assert report.output == 'FAILED test.py::test_xlink'
    assert CommandReport(argv=['ruff'], exit_code=1, passed=False, output='\x1b[31merror\x1b[0m').output == 'error'
    assert VerifyResult(passed=False, commands=[], classification='test',
                        actionable='\x1b[31mE15\x1b[0m').actionable == 'E15'
    assert plain_command([sys.executable, '-m', 'ruff', 'check', '--output-format', 'full', '--', 'x.py'])[-3:] == [
        '--output-format=concise', '--', 'x.py']


@pytest.mark.parametrize("full_failure", [False, True], ids=["quick-E13", "full-E15"])
def test_replay_r2(tmp_path, monkeypatch, full_failure):
    plan = ImplementationPlan.model_validate_json(
        (Path(__file__).parent / 'replays/issue_23_r2_plan.json').read_text())
    # The final receipt includes app/config.py in task 6 but omitted it from
    # plan.files. Restore that union so current plan governance can replay it.
    plan.files = list(dict.fromkeys([*plan.files, *[p for task in plan.tasks for p in task.files]]))
    ids = [task.id for task in plan.tasks]
    if full_failure:
        plan.tasks[-1].quick_test_targets = []
    original_run = subprocess.run
    observed = []

    def colored_runner(argv, **kwargs):
        result = original_run(argv, **kwargs)
        if 'pytest' in argv or 'ruff' in argv or Path(argv[0]).name in {'pytest', 'ruff'}:
            observed.append((argv, kwargs))
            # Simulate a tool/plugin disregarding NO_COLOR.
            result.stdout = '\x1b[31m' + (result.stdout or '') + '\x1b[0m'
            result.stderr = '\x1b[33m' + (result.stderr or '') + '\x1b[0m'
        return result

    monkeypatch.setattr('factory.tools.layer.subprocess.run', colored_runner)
    bad_store = ('def upsert_room(home_id=None, flag_on=False):\n'
                 '    if flag_on and not home_id:\n'
                 '        raise ValueError("home_id is required for new rooms after migration")\n'
                 '    return {"home_id": home_id}\n')
    tests = ('import os\nfrom app.store import upsert_room\n\n'
             '# ' + 'Long migration explanation ' * 8 + '\n'
             'def test_legacy_room_before_migration():\n'
             '    assert upsert_room(flag_on=True)["home_id"] is None\n')

    def check_repair(job, tools):
        assert job.current_task_id == ids[4]
        assert 'home_id is required' in job.govern_feedback
        assert '\x1b' not in job.govern_feedback
        assert 'ModuleNotFoundError' not in job.govern_feedback
        assert job.verify_retries_used == int(full_failure)
        assert ("E15" if full_failure else "E13") in job.govern_feedback
        assert 'import os' not in tools.read(TEST)
        checkpoint = job.checkpoint_shas[ids[5]][-1]
        assert tools.run(['git', 'show', checkpoint + ':' + TEST]).output == tools.read(TEST)

    calls = [
        (ids[0], proposal([write('migrations/migration_0001_room_home.py', '# Migration inventory\n')]), None),
        (ids[1], proposal([write('app/project.py', 'HOME_ID = None\n'), write('app/store.py', bad_store),
                          write(TEST, 'def test_scaffold():\n    assert True\n')]), None),
        (ids[2], proposal([write('app/flags.py', 'FLAG_DEFAULT = False\n')]), None),
        (ids[3], proposal([write('app/__init__.py', ''),
                          write('migrations/migration_0001_room_home.py',
                                '"""Idempotent migration. Recovery and rollback: restore original data."""\n')]), None),
        (ids[4], proposal([write('app/store.py', bad_store), write('tests/__init__.py', ''),
                          write('tests/conftest.py', '# No import workaround required.\n')]), None),
        (ids[5], proposal([write(TEST, tests), write('app/config.py', 'PERSIST = False\n')],
                         commands=[] if full_failure else [
                             ['pytest', '-q', '--color=yes', '-c', 'pyproject.toml', TEST]]), None),
        (ids[4], proposal([write('app/store.py', bad_store.replace('if flag_on and not home_id:',
            'if flag_on and home_id == "":'))]), check_repair),
    ]
    profile = VerificationProfile(description='Migration checks', kind='migration',
                                  commands=[['{python}', '-m', 'pytest', '-q', TEST]])
    factory, job, harness = run(tmp_path, plan, calls, approve=True,
        base_files={'app/project.py': 'HOME_ID = None\n', 'app/store.py': 'ROOMS = {}\n',
                    'docs/aaif-learning.md': 'MCP context keys stay stable.\n',
                    'mcp_servers/decor_design.py': '# MCP context entry point\n'},
        verification_profiles={'migration': profile})
    assert_draft(job)
    assert harness.seen == ids + [ids[4]]
    assert job.task_retries == ({} if full_failure else {ids[4]: 1})
    assert job.verify_retries_used == int(full_failure)
    assert factory.status(job.id).pr == job.pr
    if not full_failure:
        quick = job.quick_verify_runs[ids[5]][0]
        assert not quick.passed and quick.commands[0].exit_code == 1
    else:
        assert [result.passed for result in job.verify_runs] == [False, True]
    for results in job.quick_verify_runs.values():
        for result in results:
            argv = result.commands[0].argv
            assert Path(argv[argv.index('-c') + 1]).name == 'pytest-gate.ini'
            assert argv[argv.index('--rootdir') + 1] == job.worktree.path
    for result in [*job.verify_runs, *[r for runs in job.quick_verify_runs.values() for r in runs]]:
        assert strip_ansi(result.model_dump_json()) == result.model_dump_json()
        assert all('\x1b' not in report.output for report in result.commands)
    assert observed and all(kwargs['env']['NO_COLOR'] == '1' for _, kwargs in observed)
    assert all('--color=no' in argv for argv, _ in observed if 'pytest' in argv)
    body = Path(job.pr.body_path).read_text()
    assert 'Test evidence' in body and '\x1b' not in body
    assert json.loads(Path(job.pr.body_path).with_name('pr.json').read_text())['dry_run']


def test_staged_ruff_config_is_used_and_can_be_deleted(tmp_path):
    repo, tools, job = lint_setup(tmp_path)
    config = 'ruff.toml'
    apply_proposal(job, tools, proposal([
        write(config, '[lint]\nselect = ["E501"]\n'),
        write('app/value.py', 'import os\nVALUE=1\n'),
    ]))
    assert 'import os' in tools.read('app/value.py')
    from factory.models import FileEdit

    apply_proposal(job, tools, proposal([
        FileEdit(path=config, kind='delete'), write('app/value.py', 'import os\nVALUE=2\n')]))
    assert not (repo / config).exists() and 'import os' not in tools.read('app/value.py')


def test_nested_ruff_extend_preserves_repo_rules_in_checkpoint(tmp_path):
    _, tools, job = lint_setup(tmp_path)
    tools.write('ruff.toml', '[lint]\nselect = ["E501"]\n')
    tools.write('app/ruff.toml', 'extend = "../ruff.toml"\n')
    tools.commit('Nested repo config', ['ruff.toml', 'app/ruff.toml'])
    apply_proposal(job, tools, proposal([write('app/value.py', 'import os\nVALUE=1\n')]))
    assert 'import os' in tools.read('app/value.py')


def test_default_lint_does_not_inherit_factory_ruff_config(tmp_path):
    (tmp_path / 'ruff.toml').write_text('[lint]\nselect = ["E501"]\n')
    repo, tools, job = lint_setup(tmp_path)
    apply_proposal(job, tools, proposal([write('app/value.py', 'import os\nVALUE="' + 'x' * 140 + '"\n')]))
    assert 'import os' not in tools.read('app/value.py')
    reports = VerificationGate(repo, tmp_path / 'home')._lint_and_types(job, tools)
    assert all(report.passed for report in reports)


def test_repo_pyproject_without_ruff_settings_uses_defaults(tmp_path):
    _, tools, job = lint_setup(tmp_path)
    tools.write('pyproject.toml', '[project]\nname = "target"\nversion = "0.1.0"\n')
    tools.commit('Unrelated project config', ['pyproject.toml'])
    apply_proposal(job, tools, proposal([write('app/value.py', 'import os\nVALUE=1\n')]))
    assert 'import os' not in tools.read('app/value.py')
