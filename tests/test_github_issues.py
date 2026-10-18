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

    def ready(self, label):
        return self.items

    def approved(self, issue, label):
        return self.allow

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


def test_hints_are_preserved_and_failure_comment_omits_raw_logs():
    feature = parse_issue_body('Mood', '## Summary\nMood\n## Hints\n- server.py\n')
    assert feature.hints == ['server.py']
    assert 'private error' not in status_body('job-gh-42', {'job': {'stage': 'failed', 'error': 'private error'}})


@pytest.mark.parametrize('namespace', ['default', 'factory-demo'])
def test_status_commands_target_watcher_connection(namespace):
    body = status_body('job-gh-42', {'job': {'stage': 'await_human'}, 'pause_id': 1},
                       address='localhost:7236', namespace=namespace)
    assert '--address localhost:7236' in body
    assert ('--namespace factory-demo' in body) == (namespace != 'default')
