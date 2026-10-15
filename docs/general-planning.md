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

Builder edits must belong to `plan.files`. Existing files require replace edits;
write edits cannot overwrite them. These checks are not an OS sandbox: generated
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
  Issue #5 ready. The policy examples above are not enabled in `policy.decora.yaml`.
- Multi-host scheduling, stronger runtime isolation, spending budgets, preview deploys,
  automatic PR-feedback handling, and service supervision remain separate work.

Read-only function tools follow the [official OpenAI tool guide](https://developers.openai.com/api/docs/guides/tools).

## Flag decisions

Being user-facing does not by itself require a flag. The default policy requires
one for medium/high-risk changes, large changes, material changes to core UX,
or an explicit flag key. The Planner records `large_change`, `important_ux_change`
and `flag_rationale`; the Reviewer checks this assessment against the diff.
Small fixes, copy, routine visual polish, tests and documentation can ship without
introducing a flag. All changes still pass verification and review.

The `flags` section in policy.yaml and policy.decora.yaml controls these rules.
`required_for_user_facing` remains available for installations that deliberately
want the stricter old behavior, but defaults to false. Required flags still default
off and are checked in LaunchDarkly before review; changes without a flag skip that
API step. A feature flag is not a substitute for migration safety.
Restart long-running workers after changing code or policy; existing saved plans
retain their explicit flag keys and are not automatically reclassified.
