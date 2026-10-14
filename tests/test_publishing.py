import json
import subprocess

import pytest

from factory.errors import GovernanceError
from factory.models import WorktreeInfo
from factory.publish.pr import PublicationError, Publisher
from tests.support import bare_job, policy


def test_lost_create_response_reconciles_existing_draft(tmp_path):
    rules = policy()
    rules.pull_request.allow_push = True
    prs = []
    creates = []

    def runner(argv, **kwargs):
        if argv[:3] == ['gh', 'repo', 'view']:
            output = 'owner/repo\n'
        elif argv[:3] == ['gh', 'pr', 'list']:
            output = json.dumps(prs)
        elif argv[:3] == ['gh', 'pr', 'create']:
            creates.append(argv)
            prs.append({'url': 'https://github.com/owner/repo/pull/1', 'isDraft': True,
                        'state': 'OPEN', 'baseRefName': 'main'})
            return subprocess.CompletedProcess(argv, 1, '', 'response lost')
        else:
            assert argv[:2] == ['git', 'push']
            assert '--force' not in argv
            output = ''
        return subprocess.CompletedProcess(argv, 0, output, '')

    job = bare_job(tmp_path, worktree=WorktreeInfo(path=str(tmp_path), branch='codex/job-publish', base_sha='base'))
    publisher = Publisher(rules, push=True, runner=runner)
    with pytest.raises(PublicationError):
        publisher.publish(job, 'diff', tmp_path)
    result = publisher.publish(job, 'diff', tmp_path)
    assert result.url == 'https://github.com/owner/repo/pull/1'
    assert len(creates) == 1
    assert '--draft' in creates[0]
    assert '--body-file' in creates[0]


def test_closed_pr_is_not_reopened_or_pushed(tmp_path):
    rules = policy()
    rules.pull_request.allow_push = True

    def runner(argv, **kwargs):
        if argv[:3] == ['gh', 'repo', 'view']:
            return subprocess.CompletedProcess(argv, 0, 'owner/repo', '')
        assert argv[:3] == ['gh', 'pr', 'list']
        return subprocess.CompletedProcess(argv, 0, json.dumps([
            {'url': 'url', 'isDraft': True, 'state': 'CLOSED', 'baseRefName': 'main'}
        ]), '')

    job = bare_job(tmp_path, worktree=WorktreeInfo(path=str(tmp_path), branch='codex/job-closed', base_sha='base'))
    with pytest.raises(GovernanceError, match='open draft'):
        Publisher(rules, push=True, runner=runner).publish(job, 'diff', tmp_path)
