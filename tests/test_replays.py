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


def test_replay_01_issue_8_whole_file_write(tmp_path):
    job, _ = run_replay(tmp_path, 8)
    assert (Path(job.worktree.path) / 'server.py').read_text().startswith('def conversion(')
    assert not job.task_retries


def test_replay_02_issue_9_missing_planned_test(tmp_path):
    job, harness = run_replay(tmp_path, 9)
    assert harness.calls == ['T1', 'T1']
    assert len(job.checkpoint_shas['T1']) == 2


def test_replay_03_issue_14_nested_tests_and_automatic_package(tmp_path):
    job, _ = run_replay(tmp_path, 14)
    ini = Path(job.pr.body_path).with_name('pytest-gate.ini').read_text()
    assert str(Path(job.worktree.path)) in ini
    assert 'app/migrations/__init__.py' in Path(job.pr.body_path).read_text()


def test_replay_04_issue_16_five_tasks_cutoff_without_retries(tmp_path):
    job, harness = run_replay(tmp_path, 16)
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


@pytest.mark.parametrize('unsafe', ['.env', '../escape.py', 'escape.py', 'secret'])
def test_replay_09_safety_is_fatal_without_partial_writes(tmp_path, unsafe):
    replay = load_replay(8)
    content = 'VALUE = 1\n' if unsafe != 'secret' else 'KEY = \"ghp_1234567890123456789012345\"\n'
    replay['calls'][0]['proposal']['edits'].append(
        dict(path=unsafe + '.py' if unsafe == 'secret' else unsafe, kind='write', content=content))

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
def test_replay_05_issue_19_insert_append_and_max_turns(
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
            # #23 safe fixes remove unused imports even at partial checkpoints.
            # The append-only continuation must import Room when it starts using it.
            assert 'import Room' not in harness.tools.read('tests/test_models.py')
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


def assert_draft_done(job):
    assert job.stage == Stage.DONE, job.error or job.question
    assert job.pr is not None and job.pr.dry_run
    assert json.loads(Path(job.pr.body_path).with_name('pr.json').read_text()) == job.pr.model_dump()
    assert job.verify_runs[-1].passed


def test_replay_06_issue_20a_40kb_unseen_anchor_applies_first_time(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import agents

    from factory.harness.openai_agents import OpenAIAgentsHarness
    from factory.tools.layer import ToolLayer

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    replay = load_replay(8)
    repo = init_repo(tmp_path / 'repo')
    # Lower the policy threshold deliberately: a 40KB file otherwise fits whole.
    rules = policy().model_copy(update={'whole_file_limit': 12000, 'excerpt_budget': 16000})
    rules.flags.enabled = False
    anchor = '# unique interior anchor\n'
    source = '# padding\n' * 2000 + anchor + '# padding\n' * 2000
    assert 40000 <= len(source) < 41000
    tools = ToolLayer(repo, rules)
    tools.write('server.py', source)
    tools.commit('Large source fixture', ['server.py'])
    replay['calls'][0]['proposal']['edits'][0] = dict(
        path='server.py', kind='replace', old=anchor, new='def conversion(value):\n    return value\n')

    class SDKHarness(ReplayHarness):
        def build(self, job, tools):
            return OpenAIAgentsHarness(policy=rules).build(job, tools)

    payloads = []
    def run(agent, payload, **kwargs):
        data = json.loads(payload)
        payloads.append(data)
        sections = [item for item in data['files'] if item['path'] == 'server.py']
        assert sections and all(item['source_kind'] == 'exact_section' for item in sections)
        assert all(anchor not in item['text'] for item in sections)
        return SimpleNamespace(final_output=BuildProposal.model_validate(replay['calls'][0]['proposal']),
                               raw_responses=[])
    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    job = make_factory(tmp_path, repo, SDKHarness(replay), policy=rules).run(feature(acceptance=['conversion']))
    assert_draft_done(job)
    assert len(payloads) == 1 and job.builder_calls == 1 and not job.task_retries


def test_replay_07_issue_20b_syntax_e9_context_then_fixed(tmp_path):
    replay = load_replay(8)

    class SyntaxHarness(ReplayHarness):
        def build(self, job, tools):
            if job.builder_calls == 1:
                proposal = BuildProposal.model_validate(replay['calls'][0]['proposal'])
                proposal.edits[0].content = '# staged context\n' * 12 + 'def conversion(:\n    return 1\n'
                return apply_proposal(job, tools, proposal)
            assert 'server.py:13:' in job.govern_feedback
            assert '# staged context' in job.govern_feedback and '^' in job.govern_feedback
            assert not job.builds and not job.verify_runs
            assert 'def conversion(:' not in tools.read('server.py')
            return super().build(job, tools)

    repo = init_repo(tmp_path / 'repo')
    job = make_factory(tmp_path, repo, SyntaxHarness(replay)).run(feature(acceptance=['conversion']))
    assert_draft_done(job)
    assert job.builder_calls == 2 and job.task_retries == {'T1': 1}


def test_replay_08_verification_routes_to_task_2_file(tmp_path):
    replay = load_replay(16)
    # Use three tasks with distinct files so the last task cannot own T2's failure.
    replay['plan']['tasks'] = [replay['plan']['tasks'][i] for i in [0, 1, 4]]
    replay['plan']['tasks'][-1]['depends_on'] = ['T2']
    replay['plan']['tasks'][-1]['files'].remove('tests/test_conversion.py')
    replay['plan']['tasks'][1]['files'].append('tests/test_conversion.py')
    replay['plan']['files'] = ['app/part_1.py', 'app/part_2.py', 'app/part_5.py', 'tests/test_conversion.py']
    replay['calls'] = [replay['calls'][i] for i in [0, 1, 6]]
    replay['calls'][1]['proposal']['edits'][0]['content'] = 'VALUE = 0\n'
    test_edit = replay['calls'][2]['proposal']['edits'].pop()
    test_edit['content'] = 'from app.part_2 import VALUE\n\ndef test_conversion():\n    assert VALUE == 2\n'
    replay['calls'][1]['proposal']['edits'].append(test_edit)
    repair = dict(task='T2', proposal=dict(summary='Repair T2', commit_message='Repair T2',
                  edits=[dict(path='app/part_2.py', kind='write', content='VALUE = 2\n')]))
    replay['calls'].append(repair)

    class RoutingHarness(ReplayHarness):
        def build(self, job, tools):
            record = replay['calls'][len(self.calls)]
            assert job.current_task_id == record['task']
            if job.verify_runs:
                assert len(self.calls) == 3 and job.current_task_id == 'T2'
                assert 'app/part_2.py' in job.govern_feedback or 'test_conversion' in job.govern_feedback
            self.calls.append(job.current_task_id)
            return apply_proposal(job, tools, BuildProposal.model_validate(record['proposal']))

    repo = init_repo(tmp_path / 'repo')
    rules = policy()
    rules.flags.enabled = False
    harness = RoutingHarness(replay)
    job = make_factory(tmp_path, repo, harness, policy=rules).run(feature(acceptance=['conversion']))
    assert_draft_done(job)
    assert harness.calls == ['T1', 'T2', 'T5', 'T2']
    assert job.task_retries == {}
    assert job.verify_retries_used == 1
    assert job.verification_task_routes == {'T2': 1}
    assert [run.passed for run in job.verify_runs] == [False, True]


class CheckpointWorkerHarness(ReplayHarness):
    def __init__(self, replay, killed):
        super().__init__(replay)
        self.killed = killed

    def build(self, job, tools):
        import os
        import signal

        records = [record for record in self.replay['calls'] if 'proposal' in record]
        index = len(job.builds)
        if index == 2 and not self.killed.exists():
            tools.write('app/part_1.py', 'DIRTY = True\n')
            tools.write('uncommitted.txt', 'discard me')
            self.killed.touch()
            os.kill(os.getpid(), signal.SIGKILL)
        if index == 2:
            assert tools.read('app/part_1.py') == 'VALUE = 1\n'
            assert not (tools.worktree / 'uncommitted.txt').exists()
        record = records[index]
        assert job.current_task_id == record['task']
        return apply_proposal(job, tools, BuildProposal.model_validate(record['proposal']))


def execute_checkpoint_worker(request, root, repo, replay, killed):
    from factory.temporal.activities import StageActivities

    rules = policy()
    rules.flags.enabled = False
    factory = make_factory(root, repo, CheckpointWorkerHarness(replay, killed), policy=rules, keep_worktree=True)
    StageActivities(lambda: factory, factory.home).execute(request)


def test_replay_10_worker_killed_between_checkpoints_resumes_identically(tmp_path):
    import multiprocessing
    import signal

    from factory.gitcmd import git
    from factory.models import JobState
    from factory.temporal.activities import StageActivities

    replay = load_replay(16)
    # A stateless worker chooses output from durable progress, never process memory.
    replay['plan']['tasks'][2]['acceptance'] = ['Conversion preserves input and is repeatable']
    killed = tmp_path / 'killed'

    repo = init_repo(tmp_path / 'repo')
    rules = policy()
    rules.flags.enabled = False
    def create_factory():
        return make_factory(tmp_path, repo, CheckpointWorkerHarness(replay, killed), policy=rules, keep_worktree=True)
    factory = create_factory()
    activities = StageActivities(create_factory, factory.home)
    job = bare_job(tmp_path, stage=Stage.INTAKE, feature=feature(acceptance=['conversion']))
    sequence = 0
    while len(job.builds) < 2:
        job = JobState.model_validate(activities.execute(dict(
            job=job.model_dump(mode='json'), sequence=sequence, operation='step')))
        sequence += 1
    request = dict(job=job.model_dump(mode='json'), sequence=sequence, operation='step')
    worker = multiprocessing.get_context('spawn').Process(
        target=execute_checkpoint_worker, args=(request, tmp_path, repo, replay, killed))
    worker.start()
    worker.join(timeout=30)
    assert worker.exitcode == -signal.SIGKILL
    # A fresh worker redelivers the same activity using its started receipt.
    activities = StageActivities(create_factory, factory.home)
    job = JobState.model_validate(activities.execute(request))
    sequence += 1
    while job.stage not in {Stage.DONE, Stage.FAILED, Stage.AWAIT_HUMAN}:
        job = JobState.model_validate(activities.execute(dict(
            job=job.model_dump(mode='json'), sequence=sequence, operation='step')))
        sequence += 1
    assert_draft_done(job)
    baseline_root = tmp_path / 'baseline'
    baseline_repo = init_repo(baseline_root / 'repo')
    baseline = make_factory(baseline_root, baseline_repo, CheckpointWorkerHarness(replay, killed),
                            policy=rules, keep_worktree=True).run(feature(acceptance=['conversion']))
    assert_draft_done(baseline)
    assert git(Path(job.worktree.path), 'rev-parse', 'HEAD^{tree}').stdout == git(
        Path(baseline.worktree.path), 'rev-parse', 'HEAD^{tree}').stdout
    assert job.task_split_attempts == baseline.task_split_attempts
    assert job.task_summaries == baseline.task_summaries
    assert job.completed_task_ids == baseline.completed_task_ids
    assert job.task_retries == baseline.task_retries == {}


def test_replay_11_watcher_nondefault_address_and_retry_r1_reaches_draft(tmp_path):
    import asyncio

    from temporalio.client import WorkflowExecutionStatus

    from factory.github.issues import IssueBridge
    from tests.test_github_issues import FakeGitHub, FakeTemporal

    class ReplayTemporal(FakeTemporal):
        def __init__(self):
            super().__init__()
            self.jobs = {}
            self.active = None

        async def start_workflow(self, workflow, request, **kwargs):
            await super().start_workflow(workflow, request, **kwargs)
            job_id = kwargs['id']
            root = tmp_path / job_id
            repo = init_repo(root / 'repo')
            rules = policy()
            rules.flags.enabled = False
            job = make_factory(root, repo, ReplayHarness(load_replay(8)), policy=rules).run(
                feature(id=job_id, acceptance=['conversion']))
            assert_draft_done(job)
            self.jobs[job_id] = job

        def get_workflow_handle(self, job_id):
            self.active = job_id
            return self

        async def result(self):
            return self.jobs[self.active].model_dump(mode='json')

    async def scenario():
        gh, temporal = FakeGitHub(), ReplayTemporal()
        bridge = IssueBridge(temporal, gh, tmp_path / 'bridge', 'queue', 'factory:ready',
                             address='factory.example:7444', namespace='custom')
        await bridge.tick()
        assert '--address factory.example:7444 --namespace custom' in gh.posts[-1][2]
        temporal.status = WorkflowExecutionStatus.COMPLETED
        await bridge.tick()
        gh.retry_items = [gh.items[0]]
        await bridge.tick()
        assert set(temporal.jobs) == {'job-gh-42', 'job-gh-42-r1'}
        assert {post[1] for post in gh.posts} == {'job-gh-42', 'job-gh-42-r1'}
        assert gh.removed == [(7, 'factory:retry')]
        restored = IssueBridge(temporal, gh, bridge.path.parent, 'queue', 'factory:ready')
        assert restored.state['jobs']['job-gh-42-r1']['terminal']
    asyncio.run(scenario())
