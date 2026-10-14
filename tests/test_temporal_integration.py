"""Real local Temporal server: durable signals, worker replacement, history replay.

Requires the Temporal CLI binary; no API keys or external services are used.
"""
import asyncio
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from factory.intake.load import load_feature
from factory.temporal.activities import StageActivities
from factory.temporal.workflow import FactoryWorkflow
from tests.support import ROOT, init_repo, make_factory

pytestmark = pytest.mark.skipif(shutil.which('temporal') is None, reason='Temporal CLI required')


async def wait_for_pause(handle):
    for _ in range(200):
        state = await handle.query(FactoryWorkflow.snapshot)
        if state['job'].get('stage') == 'await_human' and state['pause_id']:
            return state
        await asyncio.sleep(0.05)
    raise AssertionError('workflow did not pause')


def test_worker_restart_signal_and_replay(tmp_path):
    asyncio.run(_restart(tmp_path))


async def _restart(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    factory = make_factory(tmp_path, repo)
    activities = StageActivities(lambda: make_factory(tmp_path, repo), factory.home)
    environment = await WorkflowEnvironment.start_local(dev_server_existing_path=shutil.which('temporal'))
    async with environment:
        with ThreadPoolExecutor(max_workers=4) as executor:
            def worker():
                return Worker(environment.client, task_queue='factory-test', workflows=[FactoryWorkflow],
                              activities=[activities.execute], activity_executor=executor,
                              graceful_shutdown_timeout=timedelta(seconds=2))

            async with worker():
                handle = await environment.client.start_workflow(
                    FactoryWorkflow.run, load_feature(ROOT / 'samples/ambiguous.json').model_dump(mode='json'),
                    id='job-durable-test', task_queue='factory-test',
                )
                state = await wait_for_pause(handle)
                receipts = factory.home / 'temporal' / 'job-durable-test'
                before = {p.name: p.read_bytes() for p in receipts.glob('*.json')}
            # No worker is online. Temporal persists this signal until replacement starts.
            await handle.signal(FactoryWorkflow.answer, args=[state['pause_id'],
                                'flag decor-room-mood on capabilities, default off'])
            await handle.signal(FactoryWorkflow.answer, args=[state['pause_id'], 'duplicate must be ignored'])
            async with worker():
                result = await asyncio.wait_for(handle.result(), 60)
            assert result['stage'] == 'done', result.get('error')
            assert result['human_answers'] == ['flag decor-room-mood on capabilities, default off']
            assert result['worktree']['removed']
            assert all((receipts / name).read_bytes() == value for name, value in before.items())
            history = await handle.fetch_history()
            await Replayer(workflows=[FactoryWorkflow]).replay_workflow(history)


def test_activity_exhaustion_returns_failure(tmp_path):
    asyncio.run(_failure())


async def _failure():
    from temporalio import activity
    from temporalio.exceptions import ApplicationError

    attempts = 0

    @activity.defn(name='factory_stage')
    async def broken(request: dict) -> dict:
        nonlocal attempts
        attempts += 1
        raise ApplicationError('unavailable')

    environment = await WorkflowEnvironment.start_local(dev_server_existing_path=shutil.which('temporal'))
    async with environment:
        async with Worker(environment.client, task_queue='broken', workflows=[FactoryWorkflow], activities=[broken]):
            try:
                result = await environment.client.execute_workflow(
                    FactoryWorkflow.run, load_feature(ROOT / 'samples/ambiguous.json').model_dump(mode='json'),
                    id='job-failure-test', task_queue='broken',
                )
            except WorkflowFailureError:
                pytest.fail('infrastructure error should be queryable as a failed job')
            assert result['stage'] == 'failed'
            assert 'retries exhausted' in result['error']
            assert attempts == 3


def test_issue_bridge_submits_and_reports_real_workflow(tmp_path):
    asyncio.run(_issue_bridge(tmp_path))


async def _issue_bridge(tmp_path):
    from factory.gitcmd import git
    from factory.github.issues import IssueBridge
    from tests.test_github_issues import FakeGitHub

    repo = init_repo(tmp_path / 'repo')
    git(repo, 'remote', 'add', 'origin', 'git@github.com:owner/repo.git')
    factory = make_factory(tmp_path, repo)
    activities = StageActivities(lambda: make_factory(tmp_path, repo), factory.home)
    environment = await WorkflowEnvironment.start_local(dev_server_existing_path=shutil.which('temporal'))
    async with environment:
        with ThreadPoolExecutor(max_workers=4) as executor:
            async with Worker(environment.client, task_queue='issues-test', workflows=[FactoryWorkflow],
                              activities=[activities.execute], activity_executor=executor):
                gh = FakeGitHub()
                bridge = IssueBridge(environment.client, gh, tmp_path / 'bridge', 'issues-test', 'factory:ready')
                await bridge.tick()
                handle = environment.client.get_workflow_handle('job-gh-42')
                state = await wait_for_pause(handle)
                await bridge.tick()
                assert 'await_human' in gh.posts[-1][2]
                await handle.signal(FactoryWorkflow.answer, args=[state['pause_id'],
                                    'flag decor-room-mood on capabilities, default off'])
                result = await asyncio.wait_for(handle.result(), 60)
                assert result['stage'] == 'done', result.get('error')
                # The bridge continues tracking even if the trigger label is removed.
                gh.items = []
                await bridge.tick()
                assert 'local PR draft' in gh.posts[-1][2]
                assert bridge.state['jobs']['job-gh-42']['terminal']
