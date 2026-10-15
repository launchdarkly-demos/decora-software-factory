import json

import pytest

from factory.context.repo import RepositoryReader, analyze_repo
from factory.errors import GovernanceError
from factory.governance.policy import VerificationProfile, review_plan
from factory.models import BuildProposal, FileEdit, PlanTask, WorktreeInfo
from factory.verification.profiles import prerequisites, task_problems
from tests.support import bare_job, feature, init_repo, low_plan, policy


def test_frontend_discovery_and_tools_exclude_secrets_catalog_and_symlinks(tmp_path):
    (tmp_path / 'web').mkdir()
    (tmp_path / 'web/studio.tsx').write_text('export const HomeSelector = () => "home projects";')
    (tmp_path / 'web/studio.css').write_text('.home-selector { color: blue; }')
    (tmp_path / 'app/pim').mkdir(parents=True)
    (tmp_path / 'app/pim/catalog.json').write_text('{"private":"home projects"}')
    (tmp_path / 'credentials.json').write_text('private')
    (tmp_path / '.env.local').write_text('PRIVATE=1')
    (tmp_path / 'leak.ts').symlink_to(tmp_path / '.env.local')
    (tmp_path / 'node_modules').mkdir()
    (tmp_path / 'node_modules/noise.js').write_text('home projects')
    request = feature(title='Home projects', hints=['web/studio.tsx'], summary='Home selector')
    context = analyze_repo(tmp_path, request)
    assert 'web/studio.tsx' in context.top_files
    assert 'web/studio.css' in context.file_inventory
    assert 'HomeSelector' in '\n'.join(item.text for item in context.snippets)
    reader = RepositoryReader(tmp_path)
    assert reader.paths == ['web/studio.css', 'web/studio.tsx']
    assert reader.search('HomeSelector')[0].startswith('web/studio.tsx:1:')
    for path in ['../outside.py', '.env.local', 'credentials.json', 'leak.ts', 'app/pim/catalog.json']:
        with pytest.raises(GovernanceError):
            reader.read(path)


def test_request_driven_context_finds_code_after_file_header(tmp_path):
    (tmp_path / 'web').mkdir()
    (tmp_path / 'web/page.js').write_text('\n'.join(['// filler'] * 80 + ['function selectHome() {}']))
    context = analyze_repo(tmp_path, feature(title='selectHome', summary='Fix selectHome selection'))
    assert 'selectHome' in context.snippets[0].text
    assert RepositoryReader(tmp_path).read('web/page.js', 81, 1) == 'function selectHome() {}'


def test_general_plan_validates_dependencies_and_requires_real_browser_profile(tmp_path):
    plan = low_plan(recipe='general', files=['web/page.js'], test_targets=[], tasks=[
        PlanTask(id='ui', description='Implement UI', files=['web/page.js'], acceptance=['selection works'])])
    rules = policy()
    decision = review_plan(rules, plan, bare_job(tmp_path))
    assert decision.pause and decision.resume_stage == 'plan'
    assert 'browser' in decision.reason
    rules.verification_profiles['ui'] = VerificationProfile(
        description='Browser acceptance suite', kind='browser', paths=['web/*'],
        commands=[['python', '-m', 'pytest', 'tests/browser']])
    assert review_plan(rules, plan, bare_job(tmp_path)).allowed
    plan.tasks[0].depends_on = ['missing']
    assert task_problems(plan)


def test_migration_needs_plan_checks_and_human_approval(tmp_path):
    plan = low_plan(requires_migration=True)
    rules = policy()
    assert 'migration' in ' '.join(prerequisites(rules, plan, plan.files)).lower()
    plan.migration_plan = 'Idempotent legacy conversion, backup, round-trip and failure tests.'
    rules.verification_profiles['migration'] = VerificationProfile(
        description='Legacy compatibility tests', kind='migration', commands=[['pytest', 'test_migrations.py']])
    plan.verification_profiles = ['migration']
    decision = review_plan(rules, plan, bare_job(tmp_path))
    assert decision.pause and decision.resume_stage == 'build'


def test_unlisted_file_or_existing_write_rejected_before_any_edit(tmp_path):
    from factory.agents.apply import apply_proposal
    from factory.tools.layer import ToolLayer
    repo = init_repo(tmp_path / 'repo')
    tools = ToolLayer(repo, policy())
    job = bare_job(tmp_path, plan=low_plan(files=['allowed.py', 'server.py']),
                   worktree=WorktreeInfo(path=str(repo), branch='main', base_sha=tools.head_sha()))
    proposal = BuildProposal(summary='bad', commit_message='bad', edits=[
        FileEdit(path='allowed.py', kind='write', content='ok'),
        FileEdit(path='outside.py', kind='write', content='bad')])
    with pytest.raises(GovernanceError, match='outside approved'):
        apply_proposal(job, tools, proposal)
    assert not (repo / 'allowed.py').exists()
    proposal.edits[-1] = FileEdit(path='server.py', kind='write', content='overwrite')
    with pytest.raises(GovernanceError, match='Use replace'):
        apply_proposal(job, tools, proposal)
    assert not (repo / 'allowed.py').exists()


def test_preview_saves_plan_without_builder_or_target_edits(tmp_path):
    from factory.gitcmd import git
    from factory.planning import preview_plan
    from tests.support import ScriptedHarness
    repo = init_repo(tmp_path / 'repo')
    harness = ScriptedHarness([low_plan()])
    before = git(repo, 'status', '--porcelain').stdout
    output = preview_plan(repo, tmp_path / 'home', feature(), policy(), harness)
    assert json.loads((output / 'implementation_plan.json').read_text())['summary']
    assert harness.build_calls == 0
    assert git(repo, 'status', '--porcelain').stdout == before


def test_sdk_planner_has_read_only_repository_tools(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import agents

    from factory.harness.openai_agents import OpenAIAgentsHarness
    repo = init_repo(tmp_path / 'repo')
    monkeypatch.setenv('OPENAI_API_KEY', 'test-key')

    def run(agent, payload, **kwargs):
        assert {tool.name for tool in agent.tools} == {'list_files', 'search_code', 'read_file'}
        assert 'README.md' in payload
        assert 'recipe="general"' in agent.instructions
        return SimpleNamespace(final_output=low_plan(), raw_responses=[])

    monkeypatch.setattr(agents.Runner, 'run_sync', run)
    result = OpenAIAgentsHarness().plan(bare_job(tmp_path), analyze_repo(repo, feature()))
    assert result.summary


def test_rejecting_risk_approval_returns_to_planning(tmp_path):
    from factory.models import Stage
    from tests.support import make_factory
    repo = init_repo(tmp_path / 'repo')
    factory = make_factory(tmp_path, repo)
    job = bare_job(tmp_path, stage=Stage.AWAIT_HUMAN, resume_stage=Stage.BUILD,
                   plan=low_plan(risk='high'))
    factory.accept_answer(job, 'No, preserve the old data model instead')
    assert job.stage == Stage.PLAN
