"""#25: replay r4's damaged anchor without a provider or target-app dependencies."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.agents.apply import apply_proposal
from factory.agents.prompts import BUILDER_INSTRUCTIONS
from factory.context.repo import analyze_repo
from factory.errors import FactoryError, SafetyError
from factory.harness.openai_agents import OpenAIAgentsHarness
from factory.harness.repository_tools import repository_tools
from factory.models import FileEdit, PlanTask, WorktreeInfo
from factory.tools.anchors import anchor_span
from factory.tools.layer import ToolLayer, _anchor_error
from tests.support import approved, bare_job, init_repo, low_plan, policy
from tests.test_issue22_replays import assert_draft, proposal, run, write

# Verbatim opening lines of /tmp/fac/r4/app/store.py, confirmed against the r4
# summary/log/diff. The small module isolates the anchor failure from app imports.
R4_HEADER = ('"""\nProject store. Memory by default; the server can persist rooms to a JSON file.\n\n'
             'Room→Home Migration Robustness')
R4_ANCHOR = R4_HEADER.replace('→', '\x192')


def test_replay_r4(tmp_path):
    module, test = 'app/store.py', 'tests/test_store.py'
    plan = low_plan(change_class='backend_internal', files=[module, test], test_targets=[test], tasks=[
        PlanTask(id='require_home_on_all_future_rooms', description='Repair migration documentation', files=[module]),
        PlanTask(id='test_migration', description='Verify repaired store', files=[test],
                 depends_on=['require_home_on_all_future_rooms']),
    ])
    factory, job, _ = run(tmp_path, plan, [
        ('require_home_on_all_future_rooms', proposal([FileEdit(
            path=module, kind='replace', old=R4_ANCHOR, new=R4_HEADER + '\nSafe recovery documented.')]), None),
        ('test_migration', proposal([write(test,
            'from app import store\n\ndef test_store():\n'
            '    assert "Safe recovery documented." in store.__doc__\n'
            '    assert "Room→Home" in store.__doc__\n')]), None),
    ], base_files={module: R4_HEADER + '\n"""\n'})
    assert_draft(job)
    assert job.builder_calls == 2 and not job.task_retries
    assert job.builds[0].anchor_fallbacks == [
        f'Anchor fallback used: app/store.py original span [0:{len(R4_HEADER)}]']
    assert any('Anchor fallback used' in event.message for event in job.events)
    assert factory.status(job.id).builds[0].anchor_fallbacks
    tools = ToolLayer(Path(job.worktree.path), policy())
    assert '\x19' not in tools.read(module)
    assert 'Anchor fallback used' in tools.run(['git', 'log', '--format=%B']).output


@pytest.mark.parametrize('artifact', ['\x192', r'\u2192', '\x01\x02', r'\u2192\u2014'])
@pytest.mark.parametrize('kind', ['replace', 'insert_before', 'insert_after'])
def test_escape_artifact_unique_span_and_ambiguity(tmp_path, artifact, kind):
    text = 'prefix\nRoom→Home\nsuffix\n'
    old = 'Room' + artifact + 'Home'
    start, end, fallback = anchor_span('notes.txt', text, old)
    assert fallback and text[start:end] == 'Room→Home'
    with pytest.raises(FactoryError, match='escape-artifact anchor matched 2 times; line numbers: 2, 4'):
        anchor_span('notes.txt', text + 'Room⇒Home\n', old)
    with pytest.raises(FactoryError, match='anchor matched 0 times'):
        anchor_span('notes.txt', 'Room->Home', old)
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write('notes.txt', text)
    tools.commit('Unicode fixture', ['notes.txt'])
    job = bare_job(tmp_path, plan=low_plan(files=['notes.txt']), worktree=WorktreeInfo(
        path=str(repo), branch='main', base_sha=tools.head_sha()))
    edit = FileEdit(path='notes.txt', kind=kind, old=old, new='NEW')
    with pytest.raises(FactoryError, match='escape-artifact anchor matched 2 times'):
        apply_proposal(job, tools, proposal([edit, write('other.txt', 'Room→Home\nRoom⇒Home'),
            FileEdit(path='other.txt', kind='replace', old=old, new='bad')]))
    assert tools.read('notes.txt') == text and not (repo / 'other.txt').exists()
    result = apply_proposal(job, tools, proposal([edit]))
    replacement = {'replace': 'NEW', 'insert_before': 'NEWRoom→Home', 'insert_after': 'Room→HomeNEW'}[kind]
    assert tools.read('notes.txt') == 'prefix\n' + replacement + '\nsuffix\n'
    assert result.anchor_fallbacks
    assert anchor_span('notes.txt', old + '\nRoom→Home', old) == (0, len(old), False)


def test_divergence_feedback_prefix_line_and_verbatim_window():
    lines = [f'line {i} →' for i in range(1, 20)]
    lines[7:10] = ['def upsert_room(', '    context_key: str,', '    home_id: str,']
    text = '\n'.join(lines)
    old = 'def upsert_room(\n    context_key: str,\n    name: str,'
    message = _anchor_error('app/store.py', text, old, 0)
    prefix = 'def upsert_room(\n    context_key: str,\n    '
    assert f'Longest occurring anchor prefix ({len(prefix)} characters):\n{prefix}\n' in message
    assert 'First divergent anchor line 3:\n    name: str,' in message
    assert 'divergence (line 10, ±5)' in message
    window = message.split('±5):\n')[1].split('\nClosest matching lines:')[0]
    assert window == '\n'.join(f'{i + 1}: {lines[i]}' for i in range(4, 15))
    assert '→' in window
    no_prefix = _anchor_error('empty.txt', '', 'missing', 0)
    assert 'prefix (0 characters)' in no_prefix and 'First divergent anchor line 1:' in no_prefix


@pytest.mark.parametrize('rejection', ['anchor', 'safety'])
@pytest.mark.parametrize('via_sdk', [False, True])
def test_rejected_edits_receipt_full_anchor_and_redaction(tmp_path, monkeypatch, rejection, via_sdk):
    repo = init_repo(tmp_path / 'repo')
    receipts = tmp_path / 'receipts'
    tools = ToolLayer(repo, policy(), receipt_dir=receipts)
    job = bare_job(tmp_path, plan=low_plan(files=['server.py']), worktree=WorktreeInfo(
        path=str(repo), branch='main', base_sha=tools.head_sha()))
    original = tools.read('server.py')
    anchor = 'Room→Home\n' + 'full anchor preserved\n' * 30
    if rejection == 'safety':
        monkeypatch.setenv('CUSTOM_API_KEY', 'environment-credential-value')
        anchor += ('environment-credential-value\nsk-' + 'a' * 25 +
                   '\n-----BEGIN PRIVATE KEY-----\nprivate-key-body\n-----END PRIVATE KEY-----')
    edit = FileEdit(path='server.py', kind='replace', old=anchor, new='value')
    rejected = proposal([edit, write('unwritten.txt', 'never written')])
    if via_sdk:
        import agents

        monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
        monkeypatch.setattr(agents.Runner, 'run_sync', lambda *args, **kwargs:
                            SimpleNamespace(raw_responses=[], final_output=rejected))
    for _ in range(2):
        with pytest.raises(SafetyError if rejection == 'safety' else FactoryError):
            if via_sdk:
                OpenAIAgentsHarness().build(job, tools)
            else:
                apply_proposal(job, tools, rejected)
    raw = (receipts / 'rejected_edits.jsonl').read_text()
    records = [json.loads(line) for line in raw.splitlines()]
    assert len(records) == 2
    saved = records[0]['edits'][0]
    assert saved['path'] == 'server.py' and saved['kind'] == 'replace'
    assert saved['anchor'].startswith('Room→Home\n' + 'full anchor preserved\n' * 30)
    if rejection == 'safety':
        assert 'environment-credential-value' not in raw and 'sk-' + 'a' * 25 not in raw
        assert 'private-key-body' not in raw
        assert saved['anchor'].endswith('[redacted]\n[redacted]\n[redacted]')
    else:
        assert saved['anchor'] == anchor
    assert tools.read('server.py') == original and not (repo / 'unwritten.txt').exists()


def test_all_model_payloads_preserve_non_ascii(tmp_path, monkeypatch):
    import agents

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    tools.write('server.py', '# Room→Home café\n')
    tools.commit('Unicode source', ['server.py'])
    job = bare_job(tmp_path, feature=bare_job(tmp_path).feature.model_copy(update={'summary': 'Room→Home café'}),
                   plan=low_plan(files=['server.py']), worktree=WorktreeInfo(
                       path=str(repo), branch='main', base_sha=tools.head_sha()))
    seen = []

    def runner(agent, payload, **kwargs):
        assert 'Room→Home café' in payload
        assert r'\u2192' not in payload and r'\u00e9' not in payload
        seen.append(agent.name)
        outputs = {'Planner': job.plan, 'Builder': proposal(), 'Reviewer': approved()}
        return SimpleNamespace(raw_responses=[], final_output=outputs[agent.name])

    monkeypatch.setattr(agents.Runner, 'run_sync', runner)
    harness = OpenAIAgentsHarness(policy=policy())
    harness.plan(job, analyze_repo(repo, job.feature, policy()))
    harness.build(job, tools)
    harness.review(job, tools)
    assert seen == ['Planner', 'Builder', 'Reviewer']
    assert 'ASCII-only verbatim anchors on code lines' in BUILDER_INSTRUCTIONS


def test_structured_read_tool_result_preserves_non_ascii(tmp_path):
    from agents.tool_context import ToolContext

    (tmp_path / 'café.py').write_text('# Room→Home\n')
    search = next(tool for tool in repository_tools(tmp_path, policy()) if tool.name == 'search_code')
    args = json.dumps({'text': 'Room'})
    ctx = ToolContext(context=None, tool_name='search_code', tool_call_id='test', tool_arguments=args)
    result = asyncio.run(search.on_invoke_tool(ctx, args))
    assert 'café.py' in result and 'Room→Home' in result
    assert r'\u00e9' not in result and r'\u2192' not in result
