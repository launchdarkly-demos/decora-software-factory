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
def test_builder_accepts_unobserved_source_and_tool_reads(tmp_path, monkeypatch, read_source):
    import asyncio
    import json

    import agents

    from factory.models import BuildProposal, FileEdit
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    repo = init_repo(tmp_path / 'repo')
    source = '# padding for source context\n' * 500 + 'VALUE = 1\n' + '# tail padding\n' * 1000
    (repo / 'server.py').write_text(source)
    rules = policy().model_copy(update={'whole_file_limit': 12000})
    tools = ToolLayer(repo, rules)
    job = bare_job(tmp_path, plan=low_plan(files=['server.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))

    def run(agent, payload, **kwargs):
        assert all('VALUE = 1' not in item['text'] for item in json.loads(payload)['files'])
        if read_source:
            reader = next(tool for tool in agent.tools if tool.name == 'read_file')
            from agents.tool_context import ToolContext
            args = json.dumps({'path': 'server.py', 'start_line': 501, 'line_count': 1})
            ctx = ToolContext(context=None, tool_name='read_file', tool_call_id='test', tool_arguments=args)
            text = asyncio.run(reader.on_invoke_tool(ctx, args))
            assert text == 'VALUE = 1\n'
        return SimpleNamespace(raw_responses=[], final_output=BuildProposal(
            summary='Update value', commit_message='Update value', edits=[
                FileEdit(path='server.py', kind='replace', old='VALUE = 1', new='VALUE = 2')]))

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    harness = OpenAIAgentsHarness(policy=policy())
    result = harness.build(job, tools)
    assert 'server.py' in result.changed_files
    assert 'VALUE = 2\n' in tools.read('server.py')


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
    with pytest.raises(FactoryError, match='Content contains preview markers'):
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


def test_builder_payload_prioritizes_current_task_and_output_cap_is_policy(tmp_path, monkeypatch):
    import json

    import agents

    from factory.models import BuildProposal, FileEdit, PlanTask
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    plan = low_plan(files=['done.py', 'server.py', 'future.py'], tasks=[
        PlanTask(id='done', description='Done task', files=['done.py'], acceptance=['done']),
        PlanTask(id='now', description='Current task', files=['server.py'], depends_on=['done'], acceptance=['now']),
        PlanTask(id='future', description='Future task', files=['future.py'],
                 depends_on=['now'], acceptance=['future']),
    ])
    job = bare_job(tmp_path, plan=plan, completed_task_ids=['done'], task_summaries={'done': 'Completed summary'},
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))

    def run(agent, payload, **kwargs):
        data = json.loads(payload)
        assert 'plan' not in data
        assert data['task']['id'] == 'now'
        assert data['task']['files'] == ['server.py']
        assert data['completed_tasks'] == [{'id': 'done', 'summary': 'Completed summary'}]
        assert [item['path'] for item in data['files']] == ['server.py', 'done.py', 'future.py']
        assert data['limits'] == {'output_tokens': 1234, 'turns': 40}
        assert agent.model_settings.max_tokens == 1234
        return SimpleNamespace(raw_responses=[], final_output=BuildProposal(
            summary='Update value', commit_message='Update value',
            edits=[FileEdit(path='server.py', kind='write', content='VALUE = 42\n')]))

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    rules = policy().model_copy(update={'builder_max_output_tokens': 1234})
    OpenAIAgentsHarness(policy=rules).build(job, tools)
    assert tools.read('server.py') == 'VALUE = 42\n'


def test_sdk_cutoff_is_classified_as_a_split_request(tmp_path, monkeypatch):
    import agents

    from factory.errors import IncompleteBuildError
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    repo = init_repo(tmp_path / 'repo')
    job = bare_job(tmp_path, plan=low_plan(files=['server.py']))

    def run(*args, **kwargs):
        raise RuntimeError('max_output_tokens reached; incomplete response')

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    with pytest.raises(IncompleteBuildError, match='smaller part of this task now') as caught:
        OpenAIAgentsHarness(policy=policy()).build(job, ToolLayer(repo, policy()))
    assert '`task_complete=false`' in str(caught.value)
    assert 'replace snippet' not in str(caught.value)


@pytest.mark.parametrize('truncated', [False, True])
def test_invalid_complete_proposal_is_a_correction_but_provider_cutoff_is_a_split(tmp_path, monkeypatch, truncated):
    import agents

    from factory.errors import IncompleteBuildError
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    repo = init_repo(tmp_path / 'repo')
    job = bare_job(tmp_path, plan=low_plan(files=['server.py']))
    monkeypatch.setattr(agents.Runner, 'run_sync', lambda *args, **kwargs: SimpleNamespace(
        final_output={'summary': 'missing required fields'},
        raw_responses=[SimpleNamespace(status='incomplete' if truncated else 'completed')]))
    with pytest.raises(FactoryError) as caught:
        OpenAIAgentsHarness(policy=policy()).build(job, ToolLayer(repo, policy()))
    assert isinstance(caught.value, IncompleteBuildError) == truncated
    assert ('cut off' if truncated else 'Return one complete BuildProposal.') in str(caught.value)


def test_replace_accepts_anchor_in_supplied_large_file_section(tmp_path, monkeypatch):
    import json

    import agents

    from factory.models import BuildProposal, FileEdit, PlanTask
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    repo = init_repo(tmp_path / 'repo')
    source = '# filler\n' * 3000 + 'def target_symbol():\n    return 1\n' + '# tail\n' * 3000
    (repo / 'server.py').write_text(source)
    rules = policy().model_copy(update={'whole_file_limit': 12000})
    tools = ToolLayer(repo, rules)
    job = bare_job(tmp_path, plan=low_plan(files=['server.py'], tasks=[
        PlanTask(id='change', description='Update target_symbol', files=['server.py'], acceptance=['Return 2'])]),
        worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))

    def run(agent, payload, **kwargs):
        files = json.loads(payload)['files']
        anchor = 'def target_symbol():\n    return 1\n'
        assert any(anchor in item['text'] for item in files)
        assert all(item['source_kind'] == 'exact_section' for item in files)
        return SimpleNamespace(raw_responses=[], final_output=BuildProposal(
            summary='Update target', commit_message='Update target', edits=[
                FileEdit(path='server.py', kind='replace', old=anchor, new=anchor.replace('1', '2'))]))

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    OpenAIAgentsHarness(policy=policy()).build(job, tools)
    assert 'def target_symbol():\n    return 2\n' in tools.read('server.py')


def test_builder_correction_refreshes_source_and_includes_every_task_file(tmp_path, monkeypatch):
    import json

    import agents

    from factory.models import BuildProposal, FileEdit, PlanTask
    from factory.tools.layer import ToolLayer
    from tests.support import init_repo, low_plan, policy

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    repo = init_repo(tmp_path / 'repo')
    paths = [f'app/file_{i}.py' for i in range(15)]
    for path in paths:
        (repo / path).write_text('VALUE = 1\n')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=paths, tasks=[
        PlanTask(id='change', description='Change VALUE', files=paths, acceptance=['Value is 2'])]),
        worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    calls = []

    def run(agent, payload, **kwargs):
        files = json.loads(payload)['files']
        assert {item['path'] for item in files} == set(paths)
        if calls:
            assert files[0]['text'] == 'VALUE = 2\n'
        calls.append(files)
        return SimpleNamespace(raw_responses=[], final_output=BuildProposal(
            summary='Update value', commit_message='Update value', edits=[
                FileEdit(path=paths[0], kind='replace', old='VALUE = 2' if len(calls) == 2 else 'VALUE = 1',
                         new='VALUE = 3' if len(calls) == 2 else 'VALUE = 2')]))

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    harness = OpenAIAgentsHarness(policy=policy())
    harness.build(job, tools)
    job.govern_feedback = 'Correction: update VALUE again'
    harness.build(job, tools)
    assert tools.read(paths[0]) == 'VALUE = 3\n'


def test_sections_and_explicit_reads_support_source_beyond_inventory_limit(tmp_path):
    from factory.context.repo import RepositoryReader
    from factory.harness.openai_agents import _file_excerpts
    from factory.models import PlanTask
    from factory.tools.layer import ToolLayer
    from tests.support import low_plan, policy

    # An unusually long adjacent line must not crowd a named symbol out of the payload.
    source = '#' + 'padding' * 80_000 + '\ndef target_symbol():\n    return 1\n'
    (tmp_path / 'large.py').write_text(source)
    reader = RepositoryReader(tmp_path, policy())
    assert 'large.py' not in reader.paths
    assert reader.read('large.py', 2, 2) == 'def target_symbol():\n    return 1\n'
    job = bare_job(tmp_path, plan=low_plan(files=['large.py'], tasks=[
        PlanTask(id='large', description='Change target_symbol', files=['large.py'], acceptance=['Return 2'])]))
    files = _file_excerpts(job, ToolLayer(tmp_path, policy()))
    assert any('def target_symbol():\n    return 1\n' in item['text'] for item in files)
    assert sum(len(item['text']) + len(item.get('outline', '')) for item in files) <= policy().excerpt_budget
    assert all(item['text'] in source for item in files)
