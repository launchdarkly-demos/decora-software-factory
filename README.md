# Decora software factory

A feature request goes in. A draft pull request comes out, or the job pauses when a human has to decide.

The factory builds changes for Decora. The default demo uses a disposable Decora-shaped fixture in `.factory/demo-target`, without accessing any external checkout. It does not furnish rooms. It does not merge. It analyzes the repo, plans the change, implements it in an isolated git worktree, runs a deterministic gate, reviews the diff it actually produced, and writes a draft pull request for a person.

`make demo` uses the OpenAI Agents SDK with `gpt-4.1` (override with `FACTORY_OPENAI_MODEL`). `OPENAI_API_KEY` is read from the environment or from this repo's gitignored `.env`. The factory prints where the key came from, not the key. `--agents mock` runs the scripted specialists instead.

```bash
make demo
```

## Architecture

```mermaid
flowchart TD
  intake[1 Intake]
  orch[2 Orchestrator]
  harness[3 Agent harness]
  agents[4 Planner Builder Reviewer]
  context[5 Context]
  tools[6 Tool layer]
  worktree[7 Worktree]
  verify[8 Verification]
  policy[9 Governance]
  observe[10 Observability]
  draft[Draft PR body]

  intake --> orch
  orch --> context
  context --> agents
  harness --> agents
  orch --> policy
  policy -->|ambiguous or high risk| human[Human pause]
  human --> orch
  policy -->|allowed| worktree
  agents --> tools
  tools --> worktree
  worktree --> verify
  verify -->|failed| agents
  verify -->|passed| flag[Ensure flag off]
  flag -->|off, or offline| agents
  agents -->|changes requested| agents
  agents -->|approved| policy
  policy --> draft
  orch --> observe
```

The stage machine is a graph. Verification and review both send work back to the builder. Ambiguity stops for a human instead of guessing.

```mermaid
stateDiagram-v2
  [*] --> Intake
  Intake --> Context
  Context --> Plan
  Plan --> GovernPlan
  GovernPlan --> AwaitHuman: ambiguous or high risk
  GovernPlan --> Build: inside policy
  AwaitHuman --> Plan: clarified
  AwaitHuman --> Build: approved
  Build --> Verify
  Verify --> Build: tests or flag audit failed
  Verify --> EnsureFlag: passed
  EnsureFlag --> Review: off in production, or offline
  EnsureFlag --> Failed: live flag is missing or on
  Review --> Build: changes requested
  Review --> GovernShip: approved
  GovernShip --> Build: flag missing or default on
  GovernShip --> DraftPR: policy passes
  DraftPR --> Done
  Verify --> Failed: retries exhausted
```

| # | Component | Where | What it does |
| --- | --- | --- | --- |
| 1 | Intake | `factory/intake/` | JSON spec or a GitHub issue body |
| 2 | Orchestrator | `factory/orchestrator/` | Python state machine, SQLite job state, retries, timeouts |
| 3 | Agent harness | `factory/harness/` | Mock offline, or OpenAI Agents SDK structured outputs |
| 4 | Specialist agents | `factory/agents/` | Planner, Builder, Reviewer |
| 5 | Context | `factory/context/` | Read-only repo scan. Skips `app/pim/` |
| 6 | Tool layer | `factory/tools/` | Allowlisted commands and path-checked edits |
| 7 | Execution | `factory/execution/` | Git worktree outside the primary checkout |
| 8 | Verification | `factory/verification/` | pytest, ruff, mypy, syntax compile, flag audit |
| 9 | Governance | `factory/governance/`, `policy.yaml` | Permissions and human-approval pauses |
| 10 | Observability | `factory/observability/` | JSON logs, OpenTelemetry-ready spans, run summary |

Agents return structured data. The orchestrator decides the next stage. The tool layer is what actually writes files and runs commands. A later Temporal workflow can call the same handlers as activities; the handlers do not own the control flow.

## Demo

From this directory:

```bash
make demo
```

That command:

1. Loads `samples/decor-room-mood.json`.
2. Creates a git worktree of the local demo fixture. Its flag evaluator is a stub returning the supplied default; generated tests exercise on/off behavior.
3. Plans a dark capability: `GET /api/capabilities` gains `room_mood` only when LaunchDarkly flag `decor-room-mood` is on. The code default is off.
4. The OpenAI builder is told to ship `FLAG_DEFAULT = False`. If pytest or the flag audit fails, that log goes back to the builder. `--agents mock` still ships the default on once, then repairs it, so the retry is visible without an API call.
5. After verification passes, the factory creates or reuses LaunchDarkly flag `decor-room-mood` and checks that production is off before the reviewer runs. `make demo` does this when `LAUNCHDARKLY_API_TOKEN` resolves. `make demo-offline` skips the live check.
6. The reviewer reads `git diff`, the verification log, and the acceptance lines. It does not trust the builder's summary.
7. Writes a draft pull request body under `.factory/runs/<job-id>/` and deletes the worktree and the `factory/job-*` branch. The body includes the LaunchDarkly URL when the live check ran.

Nothing is pushed. `policy.yaml` has `pull_request.allow_push: false`. The CLI also refuses `--push` when the target is the Decora checkout.

Other entry points:

```bash
make test
make demo-mock
python -m factory demo --agents mock --ld offline --repo /path/to/decor-agent
python -m factory run samples/decor-room-mood.json --repo /path/to/decor-agent
python -m factory run samples/ambiguous.json --repo /path/to/decor-agent
python -m factory resume <job-id> --answer "flag decor-room-mood on capabilities, default off"
python -m factory status <job-id>
python -m factory issue --slug owner/name --number 12
```

`samples/ambiguous.json` stops before any code is written and asks for acceptance checks and a flag key.

Artifacts for a run:

- `implementation_plan.json`
- `review_report.json`
- `verify-1.diff` (the failing build) and `diff.patch` (what would ship)
- `pr-body.md`
- `summary.json` with duration, token counts, and retries
- `log.jsonl` and `trace.jsonl`

## LaunchDarkly

Every new user-facing behavior is wrapped in a flag whose code default is off. The verifier and the ship gate both read the diff. A builder summary that claims the flag exists is not evidence.

When a token is available, passing verification is not enough to reach the reviewer. The factory calls the LaunchDarkly REST API and:

- creates boolean flag `decor-room-mood` in project `decor-agent-demo-v2` if it does not exist
- reuses it if it does
- keeps targeting off, serving the false variation, in `test` and `production`
- tags it `software-factory` and `job-<id>`
- writes a description that points at `.factory/runs/<job-id>`
- puts `https://app.launchdarkly.com/projects/decor-agent-demo-v2/flags/<key>/targeting?env=production` in the pull request body

The reviewer does not run unless that production check passes. Turning the flag on stays a human action.

Set the token in the environment, or in this factory repo’s gitignored `.env`. The factory reads `LAUNCHDARKLY_API_TOKEN` and does not print it.

```bash
export LAUNCHDARKLY_API_TOKEN="..."   # preferred; do not commit this
make demo                              # auto: live when the token resolves
make demo-offline                      # skip LaunchDarkly
python -m factory demo --repo /path/to/decor-agent --ld live
```

`auto` also reads `LAUNCHDARKLY_API_TOKEN` from this factory repo’s `.env` when the environment variable is unset. `--ld live` fails the job if the token is missing or production is not off. `--ld offline` still runs the demo and says the live check was skipped.

## Policy

`policy.yaml` is the approval boundary:

- Allowlisted commands: pytest, ruff, mypy, python, git status/diff/add/commit/log/show/rev-parse
- Denied: push, reset, rebase, `--no-verify`, secret paths, `app/pim/**`
- Auto-approve only `low` risk plans
- Pause on an ambiguous plan or a `high` risk plan
- Pull requests stay drafts

The execution environment may create and delete `factory/job-*` worktrees. The agent tool layer cannot.

## Tests

```bash
make test
```

Orchestrator tests cover the verify loop, exhausted retries, human pause and resume, SQLite reload, review rejection, timeouts, a denied push, a missing flag, and the room-mood worktree path.

## What is stubbed

- **Demo target.** A tiny Decora-shaped Python fixture, not the full Decora application. Planner, Builder, and Reviewer all call the OpenAI Agents SDK by default; the Reviewer decides its verdict from the diff and verification evidence. The deterministic verification and governance gates remain mandatory.
- **Temporal.** The state machine and SQLite job row are the swap point. Temporal is not running.
- **GitHub publish.** `gh pr create --draft` is implemented and untested against a real remote. Policy keeps it off. The demo writes the body locally.
- **Flag deletion.** The factory creates and reuses flags. It does not archive or delete them.
- **Mock recipes.** `--agents mock` implements one change: `decor-room-mood` on `/api/capabilities`. Any other request pauses for a human.
- **Docker.** `docker compose up --build` runs the factory test suite. It does not run the Decora demo.

## Layout

```
policy.yaml                 governance
samples/                    feature requests
factory/intake/             1
factory/orchestrator/       2
factory/harness/            3
factory/agents/             4
factory/context/            5
factory/tools/              6
factory/execution/          7
factory/verification/       8
factory/governance/         9
factory/launchdarkly/       flag create and the production-off check
factory/observability/      10
```

For real-agent runs without an LD token:

```bash
make test
make demo DEMO_ARGS="--ld offline"
make demo-pause DEMO_ARGS="--ld offline"
```

The SDK structured-output integration follows the [official Agent definitions guide](https://developers.openai.com/api/docs/guides/agents/define-agents).
