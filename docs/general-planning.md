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
characters per specialist run. Planner/reviewer calls allow 20 SDK turns; Builder
calls use `builder_max_turns` (default 40). Symlinks, traversal,
secret/config directories, `.env*`, credentials, generated/dependency trees, and the
PIM catalog are excluded. Maintainer deny-path policy also applies.

Builder edits focus on the current task. Safe in-repo extra files (including package
`__init__.py`) are added to the plan automatically and listed separately in the draft PR.
Existing files accept focused replacements, anchored insertions, append/prepend, or path-checked whole-file writes.
Files can also be deleted or moved to a safe new destination.
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

## Builder

The approved [Builder spec](builder-spec.md) is the source of truth for Builder
inputs, edits, safety checks, correction and split handling, budgets, checkpoints,
verification, watcher retries, and acceptance replays. See the
[traceability audit](builder-spec-traceability.md) for implementation and coverage.

## Editing toolkit audit (#18 / #19)

Added anchor-free and anchored additions, file deletion, and file moves. Existing
whole-file writes cover file copying after a safe read; empty replacement text
already supports deleting a unique block. Ordered edits support several changes to
one file in a single proposal. There is no need to spend tool turns reading files
already included in the payload. No arbitrary shell or unrestricted filesystem
access is exposed to the Builder. Binary editing and standalone permission changes
remain outside this text-edit toolkit.
