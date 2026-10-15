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


def test_builder_source_is_exact_not_a_search_excerpt(tmp_path):
    from factory.harness.openai_agents import _file_excerpts
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    repo = init_repo(tmp_path / 'repo')
    source = '\n'.join(f'line_{i} = {i}' for i in range(500)) + '\n'
    (repo / 'server.py').write_text(source)
    job = bare_job(tmp_path, plan=low_plan(files=['server.py']))
    excerpts = _file_excerpts(job, ToolLayer(repo, policy()))
    assert excerpts[0]['text'] in source
    assert 'use read_file for more' not in excerpts[0]['text']


def test_invalid_later_anchor_leaves_all_files_untouched(tmp_path):
    from factory.agents.apply import apply_proposal
    from factory.models import BuildProposal, FileEdit
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    original = tools.read('server.py')
    job = bare_job(tmp_path, plan=low_plan(files=['new.py', 'server.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    proposal = BuildProposal(summary='change', commit_message='change', edits=[
        FileEdit(path='new.py', kind='write', content='x = 1\n'),
        FileEdit(path='server.py', kind='replace', old='nonexistent anchor', new='bad')])
    with pytest.raises(FactoryError):
        apply_proposal(job, tools, proposal)
    assert not (repo / 'new.py').exists()
    assert tools.read('server.py') == original


@pytest.mark.parametrize('read_source', [False, True])
def test_builder_requires_observed_source_and_accepts_tool_reads(tmp_path, monkeypatch, read_source):
    import asyncio
    import json

    import agents

    from factory.models import BuildProposal, FileEdit
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    repo = init_repo(tmp_path / 'repo')
    source = '# padding for source context\n' * 1000 + 'VALUE = 1\n'
    (repo / 'server.py').write_text(source)
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['server.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))

    def run(agent, payload, **kwargs):
        assert 'VALUE = 1' not in json.loads(payload)['files'][0]['text']
        if read_source:
            reader = next(tool for tool in agent.tools if tool.name == 'read_file')
            from agents.tool_context import ToolContext
            args = json.dumps({'path': 'server.py', 'start_line': 1001, 'line_count': 1})
            ctx = ToolContext(context=None, tool_name='read_file', tool_call_id='test', tool_arguments=args)
            text = asyncio.run(reader.on_invoke_tool(ctx, args))
            assert text == 'VALUE = 1'
        return SimpleNamespace(raw_responses=[], final_output=BuildProposal(
            summary='Update value', commit_message='Update value', edits=[
                FileEdit(path='server.py', kind='replace', old='VALUE = 1', new='VALUE = 2')]))

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    harness = OpenAIAgentsHarness(policy=policy())
    if read_source:
        result = harness.build(job, tools)
        assert 'server.py' in result.changed_files
        assert tools.read('server.py').endswith('VALUE = 2\n')
    else:
        with pytest.raises(FactoryError, match='not read in this attempt'):
            harness.build(job, tools)
        assert tools.read('server.py') == source


def test_preview_marker_is_rejected_before_any_edit(tmp_path):
    from factory.agents.apply import apply_proposal
    from factory.models import BuildProposal, FileEdit
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['new.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    proposal = BuildProposal(summary='change', commit_message='change', edits=[
        FileEdit(path='new.py', kind='write', content='... [excerpt; use read_file for more].')])
    with pytest.raises(FactoryError, match='preview markers are not source code'):
        apply_proposal(job, tools, proposal)
    assert not (repo / 'new.py').exists()


def test_builder_formats_generated_python_before_commit_and_verification(tmp_path):
    from factory.agents.apply import apply_proposal
    from factory.models import BuildProposal, FileEdit
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['test_generated.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    code = ('def test_registry():\n'
            '    home = {"home_id": "first-home", "display_name": "A long home name", '
            '"metadata": "Some descriptive home metadata for a test"}; assert home["home_id"]\n')
    result = apply_proposal(job, tools, BuildProposal(
        summary='Add registry test', commit_message='Add registry test',
        edits=[FileEdit(path='test_generated.py', kind='write', content=code)]))
    assert result.commit == tools.head_sha()
    assert tools.run(['ruff', 'check', '--isolated', '--select', 'E,F', 'test_generated.py']).passed
    assert tools.run(['pytest', '-q', 'test_generated.py']).passed
    assert tools.run(['git', 'status', '--porcelain', '--untracked-files=no']).output.strip() == ''
