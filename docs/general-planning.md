# General planning and frontend context

The real-agent path no longer chooses a scripted capability recipe. Planner,
Builder, and Reviewer use bounded read-only SDK tools to list files, search source,
and read exact line ranges. Mock mode retains the room-mood recipe for offline tests.

## Preview before authorizing a build

```bash
.venv/bin/python -m factory plan \
  --slug arober39/decor-agent --number 5 \
  --repo /Users/alexisroberson/Documents/Claude_Code_Projects/langgraph-projects/decor-agent \
  --policy policy.decora.yaml
```

Or use `--spec path/to/request.json` instead of `--slug`/`--number`. The preview
reads the current checkout, including uncommitted source, and saves context,
implementation plan, governance decision, and usage under `.factory/plans/`.
It does not edit the target, submit a workflow, create a flag, or publish a PR.
A preview is not a build approval; a later workflow plans against its own pinned clone.

Plans include ordered tasks with IDs, earlier-task dependencies, editable files,
acceptance checks, selected verification profiles, risks, assumptions, and a migration
plan when needed. Dependencies and editable-file scope are validated outside the model.
Migration work and risks outside the auto-approve policy still require human review.
Missing verification profiles cause a planning pause rather than silently skipped checks.

## Context and inspection

Discovery includes JS/TS/JSX/TSX, HTML, CSS, Vue/Svelte, SQL, Python, manifests and docs,
including `web/`. Relevant sections are chosen using the request rather than fixed
room-mood anchors. Models can inspect up to 30 read-only tool calls / 100,000 returned
characters per specialist run, bounded further by 20 SDK turns. Symlinks, traversal,
secret/config directories, `.env*`, credentials, generated/dependency trees, and the
PIM catalog are excluded. Maintainer deny-path policy also applies.

Builder edits focus on the current task. Safe in-repo extra files (including package
`__init__.py`) are added to the plan automatically and listed separately in the draft PR.
Existing files accept either focused replacements or path-checked whole-file writes.
These checks are not an OS sandbox: generated
tests and configured commands still execute on the worker host.

## Configure verification once per target stack

The Python gate runs planned pytest targets and required regression tests, lints new
Python files, and compiles changed Python files. Explicit profile commands add broader
lint/type/build/browser checks. Static checks alone do not count as behavioral tests.
Profile selection comes from the plan plus every profile whose `paths` match actual
changed files. A model cannot supply the profile's commands.

Example policy additions (use actual commands and test paths for your target):

```yaml
verification_profiles:
  studio-browser:
    description: Browser interaction and room isolation tests
    kind: browser
    paths: ['web/*']
    commands:
      - ['{python}', '-m', 'pytest', 'tests/browser']
  migration:
    description: Legacy fixtures, repeat migration, failure recovery, and round trips
    kind: migration
    paths: ['migrations/*']
    commands:
      - ['{python}', '-m', 'pytest', 'tests/migrations']
  typecheck:
    description: Python type checks
    kind: type
    paths: ['app/*.py']
    commands:
      - ['mypy', '--follow-imports=silent', '--ignore-missing-imports', 'app']
```

`{python}` resolves to the target's Python environment when available. Commands run
in the isolated clone; install required test tooling in the worker environment first.
Commands must also be permitted by `allow_commands` and other command policy. JS
projects can use exact configured npm/node commands after those executables are allowed.
Browser profiles should run a real browser suite (for example pytest + Playwright),
with server setup/teardown owned by test fixtures. Labeling an ordinary unit test
profile `browser` does not make it browser coverage; profile definitions are trusted
maintainer configuration.

A frontend change needs a selected browser profile. A declared migration needs a
migration/recovery plan and migration profile, followed by human approval. Known
Python and JS/TS default-off declarations are audited, but the static flag audit is
still a convention check; behavioral flag tests and review remain necessary.

## What this upgrade does and does not complete

- General request planning, frontend discovery, on-demand source inspection, task
  dependency validation, edit-scope enforcement and configurable verification now exist.
- Tasks describe ordered work inside one job/PR. They are not independently scheduled
  child jobs or automatically split into multiple PRs. Large plans may still require
  narrower issues and human planning review.
- Decora's browser suite and migration test suite for home projects still need to be
  implemented/provisioned. This change deliberately does not claim they exist or mark
  Issue #5 verified. `policy.decora.yaml` now enables the browser and migration
  test contracts described below; configuring commands does not supply their tests.
- Multi-host scheduling, stronger runtime isolation, spending budgets, preview deploys,
  automatic PR-feedback handling, and service supervision remain separate work.

Read-only function tools follow the [official OpenAI tool guide](https://developers.openai.com/api/docs/guides/tools).

## Flag decisions

Feature flags and guarded rollout are deferred. Both shipped policies set
`flags.enabled: false`, which overrides all automatic rules and stored flag keys.
Planning, building and review receive this policy; flag audits and LaunchDarkly
provisioning are skipped, even with `--ld live`. No LaunchDarkly token is needed.
Existing application flags must be preserved. Tests, migration recovery and human
risk approval remain mandatory. The integration code remains available for a later
explicit re-enablement; it is not active in the current factory workflow.

Restart workers after updating policy. A paused flag-key request needs one replan
answer with its current pause ID; a valid medium/high-risk plan may then pause for
risk approval. These are distinct decisions.

## Decora home-project verification contract

`policy.decora.yaml` selects `studio-browser` for Studio changes and `migration`
for storage/model changes. The Planner must also select `migration` whenever
`requires_migration` is true, including migrations outside those path patterns.
The implementation must create `tests/browser` and `tests/migrations` in the
isolated Decora checkout. Missing suites, no collected tests, missing browser tooling,
or failed checks block verification. Do not use skipped tests as acceptance evidence.

Provision pytest-playwright and its Chromium browser in the target Python environment
before running the browser profile. The gate uses Decora's `venv/bin/python`, then
`.venv/bin/python`, falling back to the factory interpreter. Browser test fixtures
must own app startup/teardown and temporary storage; no manually running Decora
instance should be required. Tests must not depend on paid model calls or live data.

After a policy change, stop and restart the worker using the same `--home`, queue,
and `--policy policy.decora.yaml`. For an already paused job, use its current pause ID
and answer with replanning instructions to select these profiles and implement their
suites. Do not answer `approve` to bypass a missing verification prerequisite.
A migration plan still pauses separately for human review before building.

## Automatic plan repair

Before asking a human to approve a plan, governance collects task-file omissions,
invalid task dependencies/IDs, missing task acceptance, nonexistent context paths,
and missing/unknown selections of configured verification profiles. The Planner
receives the complete defect list and previous plan and gets up to
`max_retries.plan_repair` automatic correction attempts (default: 2 per job).
No task files are silently added to approved scope, and existing paths must still
be grounded in repository inventory/tools.

If repair is exhausted, the job pauses with the remaining defects instead of failing
or silently building. Missing infrastructure, product clarification, a missing flag
key, and approval of valid migration/high-risk plans still require human input.
This improves structural consistency, not proof of semantic correctness: generated
plans and changes still need acceptance tests and review.

Restart the worker to load this change. An existing paused planning job can be sent
one reply requesting replanning with the automatic validator; subsequent structural
corrections happen internally. Use its current pause ID and do not approve a build
until it reaches the separate risk-approval pause.

## Builder source grounding

Builder context contains exact contiguous source prefixes. Truncation instructions
are separate metadata rather than synthetic code lines. For replacements outside
that prefix, the Builder must read the relevant range through `read_file` during
that attempt. Anchors absent from supplied/read source are rejected with corrective
feedback. Each retry rebuilds the source payload from the current checkout.

The edit applier rejects preview markers and empty proposals as corrections. Protected
paths, traversal, symlink escapes, outside-repo paths and secrets are fatal safety errors.
It checks the entire proposal for safety and simulates the sequence of edits
before writing any file, so a bad later anchor does not leave earlier edits applied.
This preflight protects against validation failures; it is not a filesystem transaction
for disk errors or a rollback of commands run after successful edits.

After applying a proposal, the factory formats its edited Python files with the
worker's Ruff formatter before running proposal commands and committing. Formatting
uses isolated defaults and stays inside the approved edit scope. This prevents
fixable layout errors from consuming model repair attempts. Formatting failures
return to the current task as corrections after recording the applied source in a
checkpoint. The normal lint and behavioral verification gates still run, since
formatting alone cannot fix every lint error or establish correctness.

## Builder task queue and retry policy

`plan.tasks` runs in dependency order. Each task owns files and acceptance checks,
with optional `quick_test_targets` containing pytest paths or selectors. Plan tasks
must fit one Builder response; use importable Python module names and include
`__init__.py` for new packages. Nested test directories are supported: the generated
pytest config sets `pythonpath` to the absolute workspace root.

Each Builder call receives one task, its source prefixes, and short completed-task
summaries. Each accepted proposal is formatted and committed, then recorded in
`JobState.checkpoint_shas`. Completed task IDs, current task ID, summaries, auto-added
files, retry/split counts and total call count are persisted. Temporal recovery reuses
completed stages and restores interrupted work to the latest saved checkpoint;
portable workers also save a reconstructable bundle at each checkpoint.

Missing task files produce `Create <file> covering: <task description and acceptance>`
feedback before tests run. Quick-test failures return to the same task. Once all tasks
complete, one full verification pass runs. A failed full pass reopens the owner of a
file named in the failure output (or the last task), then runs full verification again
after repair. Completed unrelated tasks are not replayed. Flag assurance, review and
ship governance retain their existing order and policy.

Policy controls:

```yaml
builder_max_output_tokens: 6000
per_task_retries: 3
max_job_attempts: 25
split_attempts: 3
```

`per_task_retries` counts corrections after the initial call, shared across apply,
missing-file, quick-test and full-verification failures. Cut-off output asks for a
smaller part of the same task. Valid partial proposals set `task_complete=false` and
are checkpointed. The first `split_attempts` continuations per task do not consume
its retry budget; later ones do. Every Builder call, including interrupted calls,
counts toward `max_job_attempts`. Review and governance keep their separate caps
under `max_retries`; legacy build/verify/missing-file counters are no longer used.
