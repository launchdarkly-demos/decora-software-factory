"""Offline regressions for the r6 provider failure and missing flag ownership."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory.agents.apply import apply_proposal
from factory.errors import InfrastructureError, ProviderBlockedError
from factory.github.issues import status_body
from factory.governance.policy import load_policy, review_plan
from factory.harness.openai_agents import OpenAIAgentsHarness, _builder_failure
from factory.models import BuildProposal, FileEdit, ImplementationPlan, RepoContext, Stage
from factory.plan_normalization import is_test_task, normalize_flag_task
from factory.publish.pr import render_pr
from tests.support import (
    ROOT,
    ScriptedHarness,
    ScriptedVerifier,
    bare_job,
    feature,
    init_repo,
    low_plan,
    make_factory,
    passed_verify,
    policy,
)


class RateLimitError(Exception):
    status_code = 429

    def __init__(self, code='rate_limit_exceeded', headers=None):
        self.code = code
        self.response = SimpleNamespace(headers=headers or {})


def test_transient_rate_limit_is_infrastructure():
    error = _builder_failure(RateLimitError(headers={'retry-after': '12'}))
    assert isinstance(error, InfrastructureError)
    assert error.retry_after == 12


KEY = 'decora-room-home-migration'


def r6_plan():
    plan = ImplementationPlan.model_validate_json((ROOT / 'tests/replays/issue_27_28_r6_plan.json').read_text())
    # The final receipt includes the human-added task. Replay the pre-send-back gap.
    removed = 'provision_mandatory_flag_and_gate_migration'
    plan.tasks = [task for task in plan.tasks if task.id != removed]
    for task in plan.tasks:
        task.depends_on = [dep for dep in task.depends_on if dep != removed]
    return plan


def test_replay_r6_normalizes_before_tests_and_persists_receipt(tmp_path):
    plan = r6_plan()
    rules = load_policy(ROOT / 'policy.decora.yaml')
    repo = init_repo(tmp_path / 'repo')
    job = bare_job(tmp_path, stage=Stage.GOVERN_PLAN, plan=plan,
                   feature=feature(issue_url='https://github.com/arober39/decor-agent/issues/8'),
                   context=RepoContext(root=str(repo), top_files=[], snippets=[], has_flags_module=True,
                                       has_capabilities_route=False, file_inventory=plan.context_files))
    factory = make_factory(tmp_path, repo, policy=rules)
    factory.advance(job)
    assert job.stage == Stage.AWAIT_HUMAN and job.resume_stage == Stage.BUILD  # Existing migration approval only.
    owner = next(task for task in plan.tasks if task.id == plan.flag_task_id)
    assert owner.id == 'provision_flag_decora_room_home_migration'
    index = plan.tasks.index(owner)
    assert all(not is_test_task(task) for task in plan.tasks[:index])
    assert all(is_test_task(task) for task in plan.tasks[index + 1:])
    assert 'app/flags.py' in owner.files and 'app/migrations/migration_0001_room_home.py' in owner.files
    assert f"DECORA_ROOM_HOME_MIGRATION_FLAG = '{KEY}'" in ' '.join(owner.acceptance)
    assert 'FLAG_DEFAULT = False' in ' '.join(owner.acceptance)
    assert 'get_flag(DECORA_ROOM_HOME_MIGRATION_FLAG, context, default=FLAG_DEFAULT)' in ' '.join(owner.acceptance)
    assert any(owner.id in event.message for event in job.events)
    receipt = json.loads((factory.home / 'runs' / job.id / 'implementation_plan.json').read_text())
    assert receipt['flag_task_id'] == owner.id and receipt['normalization_notes']
    assert owner.id in render_pr(job, '')
    before = plan.model_dump()
    review_plan(rules, plan, job)
    assert plan.model_dump() == before


@pytest.mark.parametrize('blocked', [False, True], ids=['transient_then_success', 'quota_continue'])
def test_replay_r6_provider_errors_keep_checkpoints_and_task_budgets(tmp_path, blocked):
    repo = init_repo(tmp_path / 'repo')
    plan = r6_plan()
    rules = load_policy(ROOT / 'policy.decora.yaml')
    plan.context_files = []
    # Profile execution is independently covered; this replay exercises orchestration.
    for task in plan.tasks:
        task.quick_test_targets = []
    failures, saved, delays = [], {}, []

    def build(job, tools):
        task = next(task for task in job.plan.tasks if task.id == job.current_task_id)
        if task.id == 'test_migration_and_legacy_compatibility_part_4':
            if not saved:
                job.task_retries['introduce_home_and_update_room_model'] = 3
                saved.update({key: list(value) for key, value in job.checkpoint_shas.items()})
            if len(failures) < (1 if blocked else 3):
                failures.append(task.id)
                raise _builder_failure(RateLimitError('insufficient_quota' if blocked else 'rate_limit_exceeded',
                                                     {'retry-after': '7'}))
        edits = []
        for path in task.files:
            if path == 'app/flags.py':
                content = tools.read(path)
                if KEY not in content:
                    content += f"\nDECORA_ROOM_HOME_MIGRATION_FLAG = '{KEY}'\nFLAG_DEFAULT = False\n"
            elif is_test_task(task) or path.startswith('tests/'):
                content = 'def test_replay():\n    assert True\n'
            elif path == 'app/migrations/migration_0001_room_home.py' and task.id == job.plan.flag_task_id:
                content = ('from app.flags import DECORA_ROOM_HOME_MIGRATION_FLAG, FLAG_DEFAULT, get_flag\n'
                           'def migrate(context):\n'
                           '    return get_flag(DECORA_ROOM_HOME_MIGRATION_FLAG, context, default=FLAG_DEFAULT)\n')
            else:
                content = f'# checkpoint {task.id}\nVALUE = 1\n'
            edits.append(FileEdit(path=path, kind='write', content=content))
        return apply_proposal(job, tools, BuildProposal(summary=task.id, commit_message=task.id, edits=edits))

    factory = make_factory(tmp_path, repo, ScriptedHarness([plan], builds=[build]), policy=rules,
                           verifier=ScriptedVerifier([passed_verify()]), keep_worktree=True, sleep=delays.append)
    job = factory.run(feature(issue_url=None))
    assert job.stage == Stage.AWAIT_HUMAN and job.resume_stage == Stage.BUILD
    job = factory.resume(job.id, 'approve')
    if blocked:
        assert job.stage == Stage.AWAIT_HUMAN and job.resume_stage == Stage.BUILD
        assert job.checkpoint_shas == saved and len(saved) >= 4
        assert job.question == ('Provider billing/credentials problem: insufficient_quota. Fix the OpenAI account, '
                                'then answer continue (or add factory:retry).')
        assert 'insufficient_quota' in status_body(job.id, {'job': job.model_dump(mode='json'), 'pause_id': 1})
        assert not delays
        job = factory.resume(job.id, 'continue')
    assert job.stage == Stage.DONE, job.error or job.question
    assert job.task_retries == {'introduce_home_and_update_room_model': 3}
    assert job.task_split_attempts == {}
    assert all(job.checkpoint_shas[key] == value for key, value in saved.items())
    assert len(delays) == (0 if blocked else 3)
    assert all(7 <= delay <= 8 for delay in delays)
    assert Path(job.pr.body_path).exists()


@pytest.mark.parametrize('headers,expected', [
    ({'Retry-After': '12'}, 12), ({'retry-after': '0.5'}, .5),
    ({'x-ratelimit-reset-tokens': '1m2.5s'}, 62.5), ({'x-ratelimit-reset-requests': '250ms'}, .25),
    ({'retry-after': 'bad', 'x-ratelimit-reset-tokens': '2s'}, 2),
    ({'retry-after': 'NaN'}, None), ({'retry-after': '-1'}, 0),
    ({'retry-after': 'Thu, 01 Jan 1970 00:00:20 GMT'}, 10),
    ({'x-ratelimit-reset-requests': '1000000020'}, 1000000010),
])
def test_retry_after_parsing(monkeypatch, headers, expected):
    monkeypatch.setattr('factory.harness.openai_agents.time.time', lambda: 10)
    assert _builder_failure(RateLimitError(headers=headers)).retry_after == expected


@pytest.mark.parametrize('code', ['insufficient_quota', 'credit_balance_exhausted', 'billing_hard_limit_reached'])
@pytest.mark.parametrize('source', ['code', 'body', 'json'])
def test_blocked_code_sources_are_safe(code, source):
    error = RateLimitError(code=None)
    if source == 'code':
        error.code = code
    elif source == 'body':
        error.body = {'error': {'code': code, 'message': 'secret body must not be logged'}}
    else:
        error.response.json = lambda: {'error': {'code': code, 'message': 'secret response'}}
    result = _builder_failure(error)
    assert isinstance(result, ProviderBlockedError) and result.code == code
    assert 'secret' not in str(result)


@pytest.mark.parametrize('status', [401, 403])
def test_authentication_pauses(status):
    error = RuntimeError('secret exception text')
    error.status_code = status
    error.body = {'code': 'invalid_api_key'}
    result = _builder_failure(error)
    assert isinstance(result, ProviderBlockedError) and result.code == 'invalid_api_key'
    assert 'secret exception' not in str(result)


@pytest.mark.parametrize('stage', [Stage.PLAN, Stage.REVIEW])
@pytest.mark.parametrize('blocked', [False, True])
def test_planner_reviewer_pause_at_current_stage(tmp_path, monkeypatch, stage, blocked):
    import agents

    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')
    calls, delays = [], []

    def runner(*args, **kwargs):
        calls.append(1)
        raise RateLimitError('insufficient_quota' if blocked else 'rate_limit_exceeded', {'retry-after': '100'})

    monkeypatch.setattr(agents.Runner, 'run_sync', runner)
    repo = init_repo(tmp_path / 'repo')
    factory = make_factory(tmp_path, repo, OpenAIAgentsHarness(), sleep=delays.append)
    job = bare_job(tmp_path, stage=stage, plan=low_plan())
    if stage == Stage.PLAN:
        job.context = RepoContext(root=str(repo), top_files=[], snippets=[], has_flags_module=True,
                                  has_capabilities_route=False)
    else:
        factory.prepare_build(job)
    factory.advance(job)
    assert job.stage == Stage.AWAIT_HUMAN and job.resume_stage == stage
    assert len(calls) == (1 if blocked else 4)
    assert delays == ([] if blocked else [60., 60., 60.])
    assert not job.task_retries and not job.task_split_attempts
    factory.accept_answer(job, 'continue')
    assert job.stage == stage


def test_normalization_reuses_owner_collapses_duplicate_contracts_and_module_selection(tmp_path):
    rules = policy()
    rules.flags.module = 'lib/flag.py'
    job = bare_job(tmp_path, plan=low_plan())
    plan = job.plan
    normalize_flag_task(rules, job, plan)
    assert plan.tasks[-1].files[0] == 'lib/flag.py'
    original = plan.tasks[-1]
    duplicate = original.model_copy(deep=True)
    duplicate.id = original.id + '_2'
    plan.tasks.append(duplicate)
    normalize_flag_task(rules, job, plan)
    assert plan.flag_task_id == original.id
    assert duplicate.id not in {task.id for task in plan.tasks}
    assert any(f'Merged {duplicate.id}' in note for note in plan.normalization_notes)


def test_credential_shaped_code_is_redacted(monkeypatch):
    secret = 'sk-proj-' + 'x' * 32
    monkeypatch.setenv('OPENAI_API_KEY', secret)
    error = _builder_failure(RateLimitError(secret))
    assert isinstance(error, InfrastructureError)
    assert secret not in str(error) and error.code == 'rate_limit_exceeded'


def test_real_openai_rate_limit_and_auth_errors():
    from openai import AuthenticationError, RateLimitError

    response = SimpleNamespace(status_code=429, request=object(), headers={'retry-after': '2'})
    error = _builder_failure(RateLimitError('private provider message', response=response,
                                          body={'code': 'rate_limit_exceeded'}))
    assert isinstance(error, InfrastructureError) and error.retry_after == 2
    blocked = _builder_failure(RateLimitError('private billing message', response=response,
                                            body={'error': {'code': 'insufficient_quota'}}))
    assert isinstance(blocked, ProviderBlockedError)
    response = SimpleNamespace(status_code=401, request=object(), headers={})
    assert isinstance(_builder_failure(AuthenticationError('private auth message', response=response,
                                                           body={'code': 'invalid_api_key'})), ProviderBlockedError)


def test_build_transient_exhaustion_pauses_without_spending_task_budget(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    delays = []

    def build(job, tools):
        raise _builder_failure(RateLimitError())

    factory = make_factory(tmp_path, repo, ScriptedHarness([low_plan()], builds=[build]), sleep=delays.append)
    job = bare_job(tmp_path, stage=Stage.BUILD, plan=low_plan())
    factory.advance(job)
    assert job.stage == Stage.AWAIT_HUMAN and job.resume_stage == Stage.BUILD
    assert 'rate_limit_exceeded' in job.question
    assert not job.task_retries and not job.task_split_attempts
    assert job.builder_calls == 4 and len(delays) == 3


def test_retry_label_resumes_provider_pause_without_new_workflow(tmp_path):
    import asyncio

    from factory.github.issues import IssueBridge
    from tests.test_github_issues import FakeGitHub, FakeTemporal

    async def scenario():
        github, temporal = FakeGitHub(), FakeTemporal()
        signals = []

        async def signal(*args, **kwargs):
            signals.append(kwargs['args'])

        temporal.signal = signal
        temporal.snapshot = {'job': {'stage': 'await_human', 'resume_stage': 'build',
                                     'question': str(ProviderBlockedError('insufficient_quota'))}, 'pause_id': 3}
        bridge = IssueBridge(temporal, github, tmp_path, 'queue', 'factory:ready')
        await bridge.tick()
        github.retry_items = list(github.items)
        github.allow = False
        await bridge.tick()
        assert not signals
        github.allow = True
        await bridge.tick()
        assert signals == [[3, 'continue']]
        assert list(temporal.started) == ['job-gh-42']
        assert github.removed == [(7, 'factory:retry')]
        assert 'insufficient_quota' in github.posts[-1][2]
        await bridge.tick()
        assert signals == [[3, 'continue']]

    asyncio.run(scenario())


@pytest.mark.parametrize('inventory,configured,expected', [
    (['tests/flags.py', 'lib/flag.py'], 'custom/flags.py', 'lib/flag.py'),
    ([], 'custom/flags.py', 'custom/flags.py'),
    ([], 'app/flags.py', 'app/flags.py'),
])
def test_flag_module_selection_and_required_only(tmp_path, inventory, configured, expected):
    rules = policy()
    rules.flags.module = configured
    job = bare_job(tmp_path, plan=low_plan(), context=RepoContext(
        root=str(tmp_path), top_files=[], snippets=[], has_flags_module=False,
        has_capabilities_route=False, file_inventory=inventory))
    original = job.plan.model_dump()
    rules.flags.enabled = False
    normalize_flag_task(rules, job, job.plan)
    assert job.plan.model_dump() == original
    rules.flags.enabled = True
    normalize_flag_task(rules, job, job.plan)
    assert job.plan.tasks[-1].files[0] == expected


def test_request_quota_limit_is_transient():
    assert isinstance(_builder_failure(RateLimitError('requests_quota_exceeded')), InfrastructureError)
