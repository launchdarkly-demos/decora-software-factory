from pathlib import Path

import pytest

from factory.intake.load import load_feature
from factory.models import Stage
from factory.temporal.activities import StageActivities
from tests.support import ROOT, bare_job, init_repo, make_factory


def test_completed_activity_redelivery_does_not_repeat_effect(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    factory = make_factory(tmp_path, repo)
    activities = StageActivities(lambda: factory, factory.home)
    job = bare_job(tmp_path, stage=Stage.INTAKE)
    request = {'job': job.model_dump(mode='json'), 'sequence': 0, 'operation': 'step'}
    first = activities.execute(request)

    def should_not_run(*args):
        raise AssertionError('completed activity was repeated')

    factory.advance = should_not_run
    assert activities.execute(request) == first


def test_interrupted_build_restores_only_owned_worktree(tmp_path):
    repo = init_repo(tmp_path / 'repo')
    factory = make_factory(tmp_path, repo)
    job = bare_job(tmp_path, stage=Stage.INTAKE,
                   feature=load_feature(ROOT / 'samples/decor-room-mood.json'))
    while job.stage != Stage.BUILD:
        factory.advance(job)
    original = factory.advance

    def crash_after_commit(job):
        original(job)
        raise SystemExit('simulated worker death before receipt')

    factory.advance = crash_after_commit
    activities = StageActivities(lambda: factory, factory.home)
    request = {'job': job.model_dump(mode='json'), 'sequence': 4, 'operation': 'step'}
    with pytest.raises(SystemExit):
        activities.execute(request)
    factory.advance = original
    result = activities.execute(request)
    assert result['stage'] == 'verify'
    assert len(result['builds']) == 1
    assert 'ROOM_MOOD_FLAG' not in (repo / 'app/flags.py').read_text()
    assert Path(result['worktree']['path']).exists()
    factory.finish(factory.store.load(job.id).model_copy(update={'stage': Stage.FAILED}))
