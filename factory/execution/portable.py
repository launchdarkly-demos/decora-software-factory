"""Reconstructable independent clones backed by content-addressed Git bundles."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path

from factory.errors import FactoryError
from factory.execution.worktree import WorktreeManager
from factory.gitcmd import git
from factory.models import WorktreeInfo


class PortableWorkspace(WorktreeManager):
    def __init__(self, root: Path, artifacts: Path, *, base_ref: str = 'HEAD', base_branch: str = 'main') -> None:
        super().__init__(root.resolve())
        self.artifacts = artifacts.resolve()
        self.base_ref = base_ref
        self.base_branch = base_branch
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def pin(self, repo: Path) -> str:
        return _git(repo, 'rev-parse', self.base_ref)

    def create(self, repo: Path, job_id: str, base_sha: str | None = None) -> WorktreeInfo:
        if not re.fullmatch(r'job-[a-zA-Z0-9-]+', job_id):
            raise FactoryError('invalid workspace job ID')
        base = base_sha or self.pin(repo)
        origin = git(repo, 'remote', 'get-url', 'origin')
        info = WorktreeInfo(
            path=str(self.root / job_id), branch=f'codex/{job_id}', base_sha=base,
            source_url=origin.stdout.strip() if origin.returncode == 0 else str(repo.resolve()),
            base_branch=self.base_branch,
        )
        # Bundle creation reads only Git objects, excluding all uncommitted changes.
        # A temporary ref in our own bare seed avoids mutating the source checkout.
        with tempfile.TemporaryDirectory(dir=self.artifacts) as temporary:
            seed = Path(temporary) / 'seed.git'
            _git(self.artifacts, 'clone', '--bare', '--shared', str(repo.resolve()), str(seed))
            _git(seed, 'update-ref', 'refs/heads/factory-base', base)
            info.snapshot = self._save_bundle(seed, info, base, initial=True)
        self.ensure(repo, info)
        return info

    def checkpoint(self, info: WorktreeInfo) -> None:
        work = self._owned(info)
        head = _git(work, 'rev-parse', 'HEAD')
        # A snapshot is committed code only. Partial edits are intentionally omitted.
        if info.snapshot:
            prior = self._manifest(info.snapshot)
            if prior['head'] == head:
                return
        info.snapshot = self._save_bundle(work, info, head, initial=False)

    def ensure(self, repo: Path, info: WorktreeInfo) -> None:
        work = self._owned(info)
        if (work / '.git').is_dir():
            if (work / '.git').is_symlink() or _git(work, 'branch', '--show-current') != info.branch:
                raise FactoryError('workspace metadata or branch is not owned by this job')
            return
        if work.exists():
            raise FactoryError('workspace exists without owned Git metadata')
        if not info.snapshot:
            raise FactoryError('workspace has no reconstructable snapshot')
        manifest = self._manifest(info.snapshot)
        base = self._manifest(manifest['base_snapshot']) if manifest['base_snapshot'] else manifest
        self.root.mkdir(parents=True, exist_ok=True)
        _git(self.root, 'clone', '--no-checkout', str(self._bundle(base)), str(work))
        _git(work, 'checkout', '-b', info.branch, info.base_sha)
        if manifest['head'] != info.base_sha:
            _git(work, 'fetch', str(self._bundle(manifest)), 'HEAD')
            _git(work, 'reset', '--hard', manifest['head'])
        _git(work, 'remote', 'set-url', 'origin', info.source_url or str(repo.resolve()))
        info.path = str(work)
        info.removed = False

    def recover(self, repo: Path, info: WorktreeInfo, head: str) -> None:
        self.ensure(repo, info)
        work = self._owned(info)
        if _git(work, 'branch', '--show-current') != info.branch:
            raise FactoryError('workspace branch does not belong to this job')
        _git(work, 'reset', '--hard', head)
        _git(work, 'clean', '-fd')

    def destroy(self, repo: Path, info: WorktreeInfo) -> None:
        work = self._owned(info)
        if work.exists():
            if not (work / '.git').is_dir() or _git(work, 'branch', '--show-current') != info.branch:
                raise FactoryError('refusing to remove an unowned workspace')
            shutil.rmtree(work)

    def _owned(self, info: WorktreeInfo) -> Path:
        job_id = info.branch.removeprefix('codex/')
        if not re.fullmatch(r'job-[a-zA-Z0-9-]+', job_id):
            raise FactoryError('invalid portable workspace branch')
        path = self.root / job_id
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise FactoryError('workspace escapes root')
        # Rebind paths from a different worker to this worker's local workspace root.
        info.path = str(path)
        return path

    def _save_bundle(self, work: Path, info: WorktreeInfo, head: str, *, initial: bool) -> str:
        name = f'{info.base_sha}-{head}'
        manifest_path = self.artifacts / f'{name}.json'
        if manifest_path.exists():
            self._manifest(name)
            return name
        with tempfile.TemporaryDirectory(dir=self.artifacts) as temporary:
            bundle = Path(temporary) / 'snapshot.bundle'
            revisions = ['refs/heads/factory-base'] if initial else [f'{info.base_sha}..HEAD']
            _git(work, 'bundle', 'create', str(bundle), *revisions)
            digest = _sha256(bundle)
            destination = self.artifacts / f'{digest}.bundle'
            bundle.replace(destination)
            manifest = {
                'base_sha': info.base_sha, 'head': head, 'bundle_sha256': digest,
                'base_snapshot': None if initial else f'{info.base_sha}-{info.base_sha}',
            }
            pending = Path(temporary) / 'manifest.json'
            pending.write_text(json.dumps(manifest))
            pending.replace(manifest_path)
        return name

    def _manifest(self, name: str) -> dict:
        if not re.fullmatch(r'[0-9a-f]{40}-[0-9a-f]{40}', name):
            raise FactoryError('invalid snapshot reference')
        manifest = json.loads((self.artifacts / f'{name}.json').read_text())
        self._bundle(manifest)
        return manifest

    def _bundle(self, manifest: dict) -> Path:
        digest = manifest['bundle_sha256']
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise FactoryError('invalid bundle digest')
        path = self.artifacts / f'{digest}.bundle'
        if _sha256(path) != digest:
            raise FactoryError('bundle checksum mismatch')
        return path


def _sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _git(repo: Path, *args: str) -> str:
    result = git(repo, *args, timeout=180)
    if result.returncode:
        raise FactoryError(f'Workspace Git operation failed: {args[0]}')
    return result.stdout.strip()
