import shutil
from pathlib import Path

import pytest

from factory.errors import FactoryError
from factory.execution.portable import PortableWorkspace
from factory.gitcmd import git
from factory.tools.layer import ToolLayer
from tests.support import init_repo, policy


def test_reconstructs_exact_commit_without_source_or_original_workspace(tmp_path):
    source = init_repo(tmp_path / 'source')
    (source / 'README.md').write_text('uncommitted user work\n')
    original = PortableWorkspace(tmp_path / 'node-a', tmp_path / 'artifacts')
    info = original.create(source, 'job-portable')
    work = Path(info.path)
    assert (work / 'README.md').read_text() == 'mini decora\n'
    tools = ToolLayer(work, policy())
    tools.write('change.py', 'FLAG_DEFAULT = False\n')
    head = tools.commit('factory change', ['change.py'])
    original.checkpoint(info)
    original.destroy(source, info)
    source.rename(tmp_path / 'source-unavailable')
    shutil.copytree(tmp_path / 'artifacts', tmp_path / 'copied-artifacts')
    replacement = PortableWorkspace(tmp_path / 'node-b', tmp_path / 'copied-artifacts')
    replacement.ensure(source, info)
    assert Path(info.path) == tmp_path / 'node-b' / 'job-portable'
    assert git(Path(info.path), 'rev-parse', 'HEAD').stdout.strip() == head
    assert (Path(info.path) / 'change.py').read_text() == 'FLAG_DEFAULT = False\n'
    assert (tmp_path / 'source-unavailable' / 'README.md').read_text() == 'uncommitted user work\n'


def test_snapshot_checksum_blocks_corrupt_restore(tmp_path):
    source = init_repo(tmp_path / 'source')
    manager = PortableWorkspace(tmp_path / 'jobs', tmp_path / 'artifacts')
    info = manager.create(source, 'job-checksum')
    manager.destroy(source, info)
    bundle = next((tmp_path / 'artifacts').glob('*.bundle'))
    bundle.write_bytes(b'corrupt')
    with pytest.raises(FactoryError, match='checksum'):
        manager.ensure(source, info)


def test_temporal_stage_reconstructs_before_verification(tmp_path):
    from factory.intake.load import load_feature
    from factory.models import Stage
    from factory.temporal.activities import StageActivities
    from tests.support import ROOT, bare_job, make_factory

    source = init_repo(tmp_path / 'source')
    home = tmp_path / 'durable'
    first = PortableWorkspace(tmp_path / 'node-a', home / 'artifacts')
    factory = make_factory(tmp_path, source, home=home, execution=first)
    job = bare_job(tmp_path, stage=Stage.INTAKE,
                   feature=load_feature(ROOT / 'samples/decor-room-mood.json'))
    job.feature.demo_fail_first_build = False
    while job.stage != Stage.BUILD:
        factory.advance(job)
    activity = StageActivities(lambda: factory, home)
    built = activity.execute({'job': job.model_dump(mode='json'), 'operation': 'step', 'sequence': 4})
    assert built['stage'] == 'verify'
    assert job.worktree is not None
    first.destroy(source, job.worktree)
    second = PortableWorkspace(tmp_path / 'node-b', home / 'artifacts')
    replacement = make_factory(tmp_path, source, home=home, execution=second)
    verified = StageActivities(lambda: replacement, home).execute(
        {'job': built, 'operation': 'step', 'sequence': 5}
    )
    assert verified['stage'] == 'ensure_flag', verified['verify_runs'][-1]['actionable']
    assert Path(verified['worktree']['path']).parent == tmp_path / 'node-b'
