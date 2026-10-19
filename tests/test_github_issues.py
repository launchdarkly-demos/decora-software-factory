import asyncio
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

from factory.errors import FactoryError
from factory.github.issues import GitHubIssues, IssueBridge, status_body
from factory.intake.load import check_issue_target, parse_issue_body
from tests.support import init_repo


class FakeGitHub(GitHubIssues):
    def __init__(self):
        super().__init__('owner/repo')
        self.items = [{'id': 42, 'number': 7, 'title': 'Mood', 'body': '## Summary\nMood\n',
                       'html_url': 'https://github.com/owner/repo/issues/7'}]
        self.allow = True
        self.posts = []
        self.fail_comment = False
        self.retry_items = []
        self.removed = []
        self.fail_remove = False

    def ready(self, label):
        return self.items if label == "factory:ready" else self.retry_items

    def remove_label(self, number, label):
        if self.fail_remove:
            raise FactoryError('label removal failed')
        self.removed.append((number, label))
        self.retry_items = []

    def approved(self, issue, label, *, triggers=None):
        return self.allow

    def open_issues(self):
        return list(self.items)

    def label_actor(self, issue, label):
        return 'alice' if self.allow else None

    def has_write_permission(self, actor):
        return True

    def comment(self, number, job_id, body):
        if self.fail_comment:
            raise FactoryError('transport failed')
        self.posts.append((number, job_id, body))


class FakeTemporal:
    namespace = 'default'
    service_client = SimpleNamespace(config=SimpleNamespace(target_host='localhost:7236'))

    def __init__(self):
        self.started = {}
        self.status = WorkflowExecutionStatus.RUNNING
        self.snapshot = {'job': {'stage': 'await_human', 'question': 'Which field?'}, 'pause_id': 2}

    async def start_workflow(self, workflow, feature, **kwargs):
        key = kwargs['id']
        if key in self.started:
            raise WorkflowAlreadyStartedError(key, 'FactoryWorkflow')
        self.started[key] = feature

    def get_workflow_handle(self, job_id):
        return self

    async def describe(self, **kwargs):
        return SimpleNamespace(status=self.status)

    async def query(self, *args, **kwargs):
        return self.snapshot

    async def result(self):
        return {'stage': 'done', 'pr': {'url': 'https://github.com/owner/repo/pull/8'}}


def test_restart_deduplicates_and_tracks_after_label_removed(tmp_path):
    async def scenario():
        gh, temporal = FakeGitHub(), FakeTemporal()
        first = IssueBridge(temporal, gh, tmp_path, 'queue', 'factory:ready')
        await first.tick()
        assert '--pause-id 2' in gh.posts[-1][2]
        assert '--address localhost:7236' in gh.posts[-1][2]
        # Simulate a lost local receipt after Temporal accepted submission.
        first.path.unlink()
        restored = IssueBridge(temporal, gh, tmp_path, 'queue', 'factory:ready')
        await restored.tick()
        assert len(temporal.started) == 1
        gh.items = []
        temporal.status = WorkflowExecutionStatus.COMPLETED
        gh.fail_comment = True
        with pytest.raises(FactoryError):
            await restored.tick()
        assert not restored.state['jobs']['job-gh-42']['terminal']
        gh.fail_comment = False
        await restored.tick()
        assert '/pull/8' in gh.posts[-1][2]
        assert restored.state['jobs']['job-gh-42']['terminal']
    asyncio.run(scenario())


def test_unapproved_issue_does_not_start(tmp_path):
    async def scenario():
        gh, temporal = FakeGitHub(), FakeTemporal()
        gh.allow = False
        await IssueBridge(temporal, gh, tmp_path, 'queue', 'factory:ready').tick()
        assert not temporal.started
    asyncio.run(scenario())


def test_label_actor_requires_write_permission():
    gh = GitHubIssues('owner/repo')
    events = [{'event': 'labeled', 'label': {'name': 'factory:ready'}, 'actor': {'login': 'alice'}}]
    gh.api = lambda endpoint, **kw: events if '/timeline?' in endpoint else {'permission': 'read'}
    assert not gh.approved({'number': 1}, 'factory:ready')
    gh.api = lambda endpoint, **kw: events if '/timeline?' in endpoint else {'permission': 'write'}
    assert gh.approved({'number': 1}, 'factory:ready')
    events.append({'event': 'unlabeled', 'label': {'name': 'factory:ready'}})
    assert not gh.approved({'number': 1}, 'factory:ready')


def test_comment_reconciles_lost_response_and_ignores_other_authors():
    gh = GitHubIssues('owner/repo')
    gh.login = 'factory-user'
    comments = [{'id': 1, 'user': {'login': 'someone-else'},
                 'body': '<!-- software-factory:job-gh-42 -->\nspoof'}]
    writes = []

    def api(endpoint, *, method='GET', body=None, pages=False):
        if method == 'GET':
            return comments
        writes.append(method)
        if method == 'POST':
            comments.append({'id': 2, 'user': {'login': gh.login}, 'body': body['body']})
            raise FactoryError('response lost')
        comments[-1]['body'] = body['body']
    gh.api = api
    with pytest.raises(FactoryError):
        gh.comment(7, 'job-gh-42', 'queued')
    gh.comment(7, 'job-gh-42', 'queued')
    gh.comment(7, 'job-gh-42', 'done')
    assert writes == ['POST', 'PATCH']
    assert comments[0]['body'].endswith('spoof')


def test_issue_cannot_target_another_repository(tmp_path):
    from factory.gitcmd import git
    repo = init_repo(tmp_path / 'repo')
    git(repo, 'remote', 'add', 'origin', 'git@github.com:owner/repo.git')
    check_issue_target(repo, 'https://github.com/owner/repo/issues/7')
    with pytest.raises(FactoryError, match='does not match'):
        check_issue_target(repo, 'https://github.com/other/repo/issues/7')


def test_hints_are_preserved_and_failure_comment_includes_safe_reason():
    feature = parse_issue_body('Mood', '## Summary\nMood\n## Hints\n- server.py\n')
    assert feature.hints == ['server.py']
    assert 'verification failed' in status_body(
        'job-gh-42', {'job': {'stage': 'failed', 'error': 'verification failed'}})


@pytest.mark.parametrize('namespace', ['default', 'factory-demo'])
def test_status_commands_target_watcher_connection(namespace):
    body = status_body('job-gh-42', {'job': {'stage': 'await_human'}, 'pause_id': 1},
                       address='localhost:7236', namespace=namespace)
    assert '--address localhost:7236' in body
    assert ('--namespace factory-demo' in body) == (namespace != 'default')


def test_sync_error_identifies_job_and_message_and_next_poll_recovers(tmp_path):
    async def scenario():
        gh, temporal = FakeGitHub(), FakeTemporal()
        bridge = IssueBridge(temporal, gh, tmp_path, 'queue', 'factory:ready')
        gh.fail_comment = True
        with pytest.raises(FactoryError) as caught:
            await bridge.tick()
        assert 'job-gh-42' in str(caught.value)
        assert 'FactoryError' in str(caught.value)
        # Preserve the underlying reason, beyond its exception class.
        async def unavailable(job_id, record):
            raise FactoryError('GitHub temporarily unavailable')
        original = bridge.sync_job
        bridge.sync_job = unavailable
        with pytest.raises(FactoryError, match='job-gh-42: FactoryError: GitHub temporarily unavailable'):
            await bridge.tick()
        bridge.sync_job = original
        gh.fail_comment = False
        await bridge.tick()
        assert gh.posts
    asyncio.run(scenario())


@pytest.mark.parametrize('retry_label', ['factory:retry', 'rerun/custom'])
def test_retry_requires_terminal_original_and_recovers_cleanup_without_duplicate(tmp_path, retry_label):
    async def scenario():
        gh, temporal = FakeGitHub(), FakeTemporal()
        bridge = IssueBridge(temporal, gh, tmp_path, 'queue', 'factory:ready', retry_label=retry_label)
        issue = gh.items[0]
        gh.retry_items = [issue]
        await bridge.tick()
        assert list(temporal.started) == ['job-gh-42']
        temporal.status = WorkflowExecutionStatus.COMPLETED
        await bridge.tick()  # The original becomes terminal.
        gh.allow = False
        await bridge.tick()
        assert len(temporal.started) == 1
        gh.allow = True
        issue['state'] = 'closed'
        await bridge.tick()
        assert len(temporal.started) == 1
        issue['state'] = 'open'
        gh.items = []  # The ready label is not required for a retry.
        gh.fail_comment = True
        with pytest.raises(FactoryError):
            await bridge.tick()
        assert list(temporal.started) == ['job-gh-42', 'job-gh-42-r1']
        assert not gh.removed
        gh.fail_comment = False
        gh.fail_remove = True
        with pytest.raises(FactoryError, match='label removal failed'):
            await bridge.tick()
        assert bridge.state['jobs']['job-gh-42-r1']['terminal']
        bridge = IssueBridge(temporal, gh, tmp_path, 'queue', 'factory:ready', retry_label=retry_label)
        gh.fail_remove = False
        await bridge.tick()
        assert len(temporal.started) == 2
        assert gh.removed == [(7, retry_label)]
        assert any(post[1] == 'job-gh-42-r1' for post in gh.posts)
        gh.retry_items = [issue]
        await bridge.tick()
        assert list(temporal.started)[-1] == 'job-gh-42-r2'
    asyncio.run(scenario())


@pytest.mark.parametrize('permission', ['write', 'maintain', 'admin', 'read', 'triage'])
def test_retry_uses_latest_label_actor(permission):
    gh = GitHubIssues('owner/repo')
    events = [
        {'event': 'labeled', 'label': {'name': 'factory:retry'}, 'actor': {'login': 'old-maintainer'}},
        {'event': 'labeled', 'label': {'name': 'factory:retry'}, 'actor': {'login': 'latest'}},
    ]
    def api(endpoint, **kwargs):
        if '/timeline?' in endpoint:
            return events
        assert '/latest/permission' in endpoint
        return {'permission': permission}
    gh.api = api
    assert gh.approved({'number': 7}, 'factory:retry') == (permission in {'write', 'maintain', 'admin'})


def test_retry_cli_default_and_override():
    from factory.temporal.cli import parser

    assert parser().parse_args(['watch-issues', '--slug', 'owner/repo']).retry_label == 'factory:retry'
    args = parser().parse_args(['watch-issues', '--slug', 'owner/repo', '--retry-label', 'custom'])
    assert args.retry_label == 'custom'


def test_status_tasks_at_build_and_failure_reason_redacts_credentials(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sensitive-provider-value')
    job = {'stage': 'build', 'plan': {'tasks': [{'id': 'T1', 'description': 'Build it'}]},
           'completed_task_ids': ['T1']}
    assert '- [x] T1: Build it' in status_body('job-gh-42', {'job': job})
    job.update(stage='failed', error='Provider failed with sensitive-provider-value')
    body = status_body('job-gh-42', {'job': job})
    assert 'Failure:' in body and 'sensitive-provider-value' not in body


def test_retry_without_original_never_starts(tmp_path):
    async def scenario():
        gh, temporal = FakeGitHub(), FakeTemporal()
        gh.retry_items, gh.items = gh.items, []
        await IssueBridge(temporal, gh, tmp_path, 'queue', 'factory:ready').tick()
        assert not temporal.started and not gh.removed
    asyncio.run(scenario())


def test_watcher_scope_lock_and_minimum_interval(tmp_path):
    import fcntl

    from factory.github.issues import watch_issues

    gh, temporal = FakeGitHub(), FakeTemporal()
    bridge = IssueBridge(temporal, gh, tmp_path, 'queue', 'factory:ready')
    bridge.save()
    with pytest.raises(FactoryError, match='different repository, queue, or namespace'):
        IssueBridge(temporal, gh, tmp_path, 'another-queue', 'factory:ready')
    args = SimpleNamespace(interval=9, home=tmp_path)
    with pytest.raises(FactoryError, match='at least 10'):
        asyncio.run(watch_issues(temporal, args))
    args.interval = 30
    with (tmp_path / 'issues.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(FactoryError, match='already owns'):
            asyncio.run(watch_issues(temporal, args))


def test_label_delete_encodes_name_and_tolerates_already_removed(monkeypatch):
    gh = GitHubIssues('owner/repo')
    endpoints = []
    def run(argv, **kwargs):
        endpoints.append(argv)
        return SimpleNamespace(returncode=1, stderr='HTTP 404: Not Found', stdout='')
    monkeypatch.setattr('factory.github.issues.subprocess.run', run)
    gh.remove_label(7, 'retry/custom')
    assert 'repos/owner/repo/issues/7/labels/retry%2Fcustom' in endpoints[0]
    monkeypatch.setattr('factory.github.issues.subprocess.run', lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout='', stderr=''))
    gh.remove_label(7, 'retry/custom')
