"""Planning preview: read the target and save artifacts, never build or publish."""
from __future__ import annotations

import time
import uuid
from pathlib import Path

from factory.context.repo import analyze_repo
from factory.governance.policy import Policy, review_plan
from factory.harness.base import AgentHarness
from factory.models import FeatureRequest, JobState, Stage


def preview_plan(repo: Path, home: Path, feature: FeatureRequest, policy: Policy, harness: AgentHarness) -> Path:
    from factory.intake.load import check_issue_target

    if feature.source == 'github_issue':
        check_issue_target(repo, feature.issue_url)
    job = JobState(id='job-plan-' + uuid.uuid4().hex[:12], stage=Stage.PLAN, feature=feature,
                   created_at=time.time(), updated_at=time.time())
    job.context = analyze_repo(repo, feature, policy)
    job.plan = harness.plan(job, job.context)
    decision = review_plan(policy, job.plan, job)
    output = home / 'plans' / job.id
    output.mkdir(parents=True)
    (output / 'context.json').write_text(job.context.model_dump_json(indent=2))
    (output / 'implementation_plan.json').write_text(job.plan.model_dump_json(indent=2))
    (output / 'decision.json').write_text(decision.model_dump_json(indent=2))
    (output / 'usage.json').write_text(job.usage.model_dump_json(indent=2))
    print(f'Plan saved: {output / "implementation_plan.json"}')
    print(f'Tasks: {len(job.plan.tasks)}; risk: {job.plan.risk}; migration: {job.plan.requires_migration}')
    print(f'Build readiness: {decision.reason}')
    print('Preview only: no edits, workflow submission, flag changes, or PR publication.')
    return output
