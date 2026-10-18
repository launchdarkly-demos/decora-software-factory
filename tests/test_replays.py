"""Recorded Builder responses replayed through real Git, verification, review and publication."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.agents.apply import apply_proposal
from factory.errors import IncompleteBuildError
from factory.harness.mock import MockHarness
from factory.models import BuildProposal, ImplementationPlan, Stage
from tests.support import bare_job, feature, init_repo, make_factory, policy

REPLAYS = Path(__file__).parent / 'replays'


class ReplayHarness(MockHarness):
    def __init__(self, replay):
        self.replay = replay
        self.calls = []

    def plan(self, job, context):
        return ImplementationPlan.model_validate(self.replay['plan'])

    def build(self, job, tools):
        record = self.replay['calls'][len(self.calls)]
        assert job.current_task_id == record['task']
        assert not job.verify_runs, 'full verification must wait for the last task'
        for text in record.get('feedback', []):
            assert text in job.govern_feedback
        self.calls.append(job.current_task_id)
        if record.get('cutoff'):
            raise IncompleteBuildError('Simulated max_output_tokens cutoff; split this task into smaller parts')
        return apply_proposal(job, tools, BuildProposal.model_validate(record['proposal']))


def load_replay(number):
    return json.loads((REPLAYS / f'issue_{number}.json').read_text())


def run_replay(tmp_path, number, *, resume=False):
    replay = load_replay(number)
    repo = init_repo(tmp_path / 'repository with spaces')
    harness = ReplayHarness(replay)
    rules = policy()
    rules.flags.enabled = False
    factory = make_factory(tmp_path, repo, harness, policy=rules, keep_worktree=True)
    request = feature(id=f'issue-{number}', acceptance=['conversion'])
    if resume:
        job = bare_job(tmp_path, id=f'job-replay-{number}', stage=Stage.INTAKE, feature=request)
        while len(job.completed_task_ids) < 2:
            factory.advance(job)
        checkpoints = dict(job.checkpoint_shas)
        factory = make_factory(tmp_path, repo, harness, home=factory.home, policy=rules, keep_worktree=True)
        job = factory.status(job.id)
        assert job.checkpoint_shas == checkpoints
        for _ in range(40):
            if job.stage in {Stage.DONE, Stage.FAILED, Stage.AWAIT_HUMAN}:
                break
            factory.advance(job)
        factory.finish(job)
    else:
        job = factory.run(request)
    assert job.stage == Stage.DONE, job.error or job.question
    assert job.pr is not None and job.pr.dry_run and job.pr.url is None
    assert Path(job.pr.body_path).exists()
    pr_record = json.loads(Path(job.pr.body_path).with_name('pr.json').read_text())
    assert pr_record == job.pr.model_dump()
    assert job.review.verdict == 'approve'
    assert len(job.verify_runs) == 1 and job.verify_runs[0].passed
    assert job.verify_runs[0].commands and all(command.passed for command in job.verify_runs[0].commands)
    assert len(harness.calls) == len(replay['calls'])
    assert job.task_retries == replay['expected_retries']
    assert job.auto_added_files == replay['expected_auto_added']
    assert len(job.builds) == sum(len(shas) for shas in job.checkpoint_shas.values())
    assert len(job.completed_task_ids) == len(job.plan.tasks)
    assert job.current_task_id is None
    assert factory.status(job.id).auto_added_files == job.auto_added_files
    return job, harness


@pytest.mark.parametrize('number', [8, 9, 14, 16])
def test_issue_replay_reaches_draft_pr(tmp_path, number):
    job, harness = run_replay(tmp_path, number, resume=number == 16)
    if number == 8:
        assert (Path(job.worktree.path) / 'server.py').read_text().startswith('def conversion(')
        assert not job.task_retries
    if number == 9:
        assert harness.calls == ['T1', 'T1']
        assert len(job.checkpoint_shas['T1']) == 2
    if number == 14:
        ini = Path(job.pr.body_path).with_name('pytest-gate.ini').read_text()
        assert str(Path(job.worktree.path)) in ini
    if number == 16:
        assert harness.calls == ['T1', 'T2', 'T3', 'T3', 'T3', 'T4', 'T5']
        assert job.task_split_attempts == {'T3': 2}
        assert job.task_retries == {}
        assert len(job.checkpoint_shas['T3']) == 2


def test_auto_added_file_is_persisted_owned_and_disclosed_in_draft(tmp_path):
    job, _ = run_replay(tmp_path, 14)
    path = 'app/migrations/__init__.py'
    assert path in job.plan.files
    assert path in job.plan.tasks[0].files
    assert job.auto_added_files == [path]
    assert not job.task_retries
    body = Path(job.pr.body_path).read_text()
    assert f'## Files added to the plan automatically\n\n- `{path}`' in body


@pytest.mark.parametrize('unsafe', ['.env', '../escape.py', 'escape.py'])
def test_replay_safety_violation_is_fatal_without_partial_writes(tmp_path, unsafe):
    replay = load_replay(8)
    replay['calls'][0]['proposal']['edits'].append(dict(path=unsafe, kind='write', content='VALUE = 1\n'))

    class UnsafeHarness(ReplayHarness):
        def build(self, job, tools):
            if unsafe == 'escape.py':
                (tools.worktree / unsafe).symlink_to(tmp_path / 'outside.py')
            return super().build(job, tools)

    repo = init_repo(tmp_path / 'repo')
    factory = make_factory(tmp_path, repo, UnsafeHarness(replay), keep_worktree=True)
    job = factory.run(feature(acceptance=['conversion']))
    assert job.stage == Stage.FAILED
    assert job.pr is None and job.builder_calls == 1
    assert not job.builds and not job.auto_added_files and not job.task_retries
    assert not job.verify_runs
    assert (Path(job.worktree.path) / 'server.py').read_text() == (repo / 'server.py').read_text()
    assert not (Path(job.worktree.path) / 'tests/test_conversion.py').exists()
    assert not (tmp_path / 'outside.py').exists()
    assert not (Path(job.worktree.path).parent / 'escape.py').exists()


@pytest.mark.parametrize('failure', ['recipe', 'io', 'format', 'empty_command', 'unsupported_command'])
def test_repairable_builder_failures_correct_the_task_then_publish(tmp_path, failure):
    from factory.errors import RecipeError

    replay = load_replay(8)

    class RepairHarness(ReplayHarness):
        def build(self, job, tools):
            if job.builder_calls == 1:
                if failure == 'recipe':
                    raise RecipeError('Read the current capabilities implementation')
                if failure == 'io':
                    raise OSError('file operation failed')
                if failure == 'format':
                    raise ValueError('invalid proposal')
                proposal = BuildProposal.model_validate(replay['calls'][0]['proposal'])
                proposal.commands = [[]] if failure == 'empty_command' else [['unknown-test-runner']]
                return apply_proposal(job, tools, proposal)
            assert job.task_retries == {'T1': 1}
            assert job.govern_feedback
            return super().build(job, tools)

    repo = init_repo(tmp_path / 'repo')
    job = make_factory(tmp_path, repo, RepairHarness(replay)).run(feature(acceptance=['conversion']))
    assert job.stage == Stage.DONE, job.error
    assert job.pr is not None
    assert job.builder_calls == 2 and job.task_retries == {'T1': 1}
    assert len(job.verify_runs) == 1 and job.verify_runs[0].passed


@pytest.mark.parametrize('large_existing_file', [False, True])
def test_issue_19_sdk_replay_uses_checkpoint_sections_and_survives_max_turns(
    tmp_path, monkeypatch, large_existing_file
):
    """Replay #19 through the real SDK harness boundary, task loop and draft publisher."""
    from types import SimpleNamespace

    import agents
    from agents.exceptions import MaxTurnsExceeded

    from factory.harness.openai_agents import OpenAIAgentsHarness
    from factory.tools.layer import ToolLayer

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    replay = load_replay(19)
    repo = init_repo(tmp_path / 'repo')
    rules = policy().model_copy(update={'builder_max_turns': 7, 'whole_file_limit': 12000})
    rules.flags.enabled = False
    if large_existing_file:
        marker = '# model_update Home Room anchor\n'
        source = '# unrelated padding\n' * 2000 + marker + '# unrelated tail\n' * 2000
        assert len(source) > rules.whole_file_limit
        initial_tools = ToolLayer(repo, rules)
        initial_tools.write('app/project.py', source)
        initial_tools.commit('Existing large project module', ['app/project.py'])
        edit = replay['calls'][0]['proposal']['edits'][0]
        edit.update(kind='insert_after', old=marker, new=edit.pop('content'))

    class SDKReplayHarness(ReplayHarness):
        def build(self, job, tools):
            self.job, self.tools = job, tools
            return OpenAIAgentsHarness(policy=rules).build(job, tools)

    harness = SDKReplayHarness(replay)
    snapshots = []
    checkpoints = []

    def run(agent, payload, **kwargs):
        assert agent.name == 'Builder'
        assert kwargs['max_turns'] == 7
        data = json.loads(payload)
        record = replay['calls'][len(harness.calls)]
        assert data['task']['id'] == 'model_update'
        for text in record.get('feedback', []):
            if text == 'Split this task':
                assert 'ran out of turns' in data['govern_feedback']
            else:
                assert text in data['govern_feedback']
        harness.calls.append(data['task']['id'])
        snapshots.append(data['files'])
        checkpoints.append(harness.tools.head_sha())
        assert sum(len(item['text']) + len(item.get('outline', '')) for item in data['files']) <= rules.excerpt_budget
        for path in data['task']['files']:
            sections = [item for item in data['files'] if item['path'] == path]
            assert sections, path
            current = harness.tools.worktree / path
            if current.exists():
                text = current.read_text()
                assert all(item['text'] in text for item in sections)
                if len(text) < rules.whole_file_limit:
                    assert sections[0]['text'] == text
        if record.get('max_turns'):
            assert not harness.job.task_retries
            assert harness.job.checkpoint_shas['model_update'] == [harness.tools.head_sha()]
            raise MaxTurnsExceeded('test turn limit')
        proposal = BuildProposal.model_validate(record['proposal'])
        for edit in proposal.edits:
            if edit.kind == 'insert_after':
                sections = [item for item in data['files'] if item['path'] == edit.path]
                assert any(edit.old in item['text'] for item in sections)
                if large_existing_file:
                    assert all(item['source_kind'] == 'exact_section' for item in sections)
        return SimpleNamespace(final_output=proposal, raw_responses=[])

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    factory = make_factory(tmp_path, repo, harness, policy=rules, keep_worktree=True)
    job = factory.run(feature(id='issue-19', acceptance=['Room names may repeat; context keys stay stable']))
    assert job.stage == Stage.DONE, job.error or job.question
    assert job.pr is not None and job.pr.dry_run
    assert Path(job.pr.body_path).exists()
    assert job.task_retries == {}
    assert job.task_split_attempts == {'model_update': 2}
    assert job.builder_calls == 3
    assert len(job.checkpoint_shas['model_update']) == 2
    assert job.verify_runs[-1].passed
    assert checkpoints[1] == checkpoints[2]  # MaxTurnsExceeded made no mutation.
    assert snapshots[0] != snapshots[1]  # Partial output supplied fresh checkpoint source.
