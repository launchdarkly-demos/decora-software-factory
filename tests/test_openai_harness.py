from types import SimpleNamespace

import pytest

from factory.errors import FactoryError
from factory.harness.openai_agents import OpenAIAgentsHarness
from factory.models import ReviewReport, WorktreeInfo
from tests.support import bare_job, failed_verify, passed_verify


class ReviewTools:
    def diff(self, base):
        return 'actual diff evidence'

    def changed_files(self, base):
        return ['server.py']


def test_sdk_reviewer_controls_verdict_and_receives_evidence(tmp_path, monkeypatch):
    import agents

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    job = bare_job(tmp_path, worktree=WorktreeInfo(path=str(tmp_path), branch='demo', base_sha='base'))
    job.verify_runs = [failed_verify('historical failure detail'), passed_verify()]
    report = ReviewReport(verdict='changes_requested', findings=[], risks=[], summary='Missing on-case test')

    def run(agent, payload, **kwargs):
        assert agent.name == 'Reviewer'
        assert agent.output_type is ReviewReport
        assert 'actual diff evidence' in payload
        assert 'historical failure detail' not in payload
        assert 'pytest passed' in payload
        assert 'demo_fail_first_build' not in payload
        return SimpleNamespace(final_output=report, raw_responses=[])

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    result = OpenAIAgentsHarness().review(job, ReviewTools())
    assert result.verdict == 'changes_requested'


def test_sdk_reviewer_failure_never_approves_or_leaks_error(tmp_path, monkeypatch):
    import agents

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    job = bare_job(tmp_path, worktree=WorktreeInfo(path=str(tmp_path), branch='demo', base_sha='base'))

    def run(*args, **kwargs):
        raise RuntimeError('sensitive provider response')

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    with pytest.raises(FactoryError, match='Reviewer call failed: RuntimeError') as caught:
        OpenAIAgentsHarness().review(job, ReviewTools())
    assert 'sensitive' not in str(caught.value)


def test_generated_commands_use_virtualenv_without_api_keys(tmp_path, monkeypatch):
    from factory.tools.layer import ToolLayer
    from tests.support import policy

    monkeypatch.setenv('OPENAI_API_KEY', 'private-openai')
    monkeypatch.setenv('LAUNCHDARKLY_API_TOKEN', 'private-ld')
    layer = ToolLayer(tmp_path, policy())
    env = layer._env()
    assert 'OPENAI_API_KEY' not in env
    assert 'LAUNCHDARKLY_API_TOKEN' not in env
    assert layer.run(['pytest', '--version']).passed


def test_build_commands_run_after_edits(tmp_path):
    from factory.agents.apply import apply_proposal
    from factory.models import BuildProposal, FileEdit
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    repo = init_repo(tmp_path / 'target')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()),
                   plan=low_plan(files=['test_new.py']))
    proposal = BuildProposal(
        summary='add test', commit_message='add test',
        edits=[FileEdit(path='test_new.py', kind='write', content='def test_new():\n    assert True\n')],
        commands=[['pytest', '-q', 'test_new.py']],
    )
    result = apply_proposal(job, tools, proposal)
    assert result.changed_files == ['test_new.py']
