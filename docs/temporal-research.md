# Temporal implementation research

Checked 2026-10-13 against official Temporal documentation and Decora source.

## Decora reference

The local reference branch is `durable-workflows`, commit
`88658d4162fbd9798d23236f96eedbcd647dbaa7`; `durable-execution` was not available
locally. Files were read with `git show`, without changing its checkout.

- [Decora workflow](https://github.com/arober39/decor-agent/blob/88658d4162fbd9798d23236f96eedbcd647dbaa7/app/workflow.py):
  deterministic orchestration, dataclass activity payloads, bounded activity retries,
  signals for approval/rejection, `wait_condition` for durable human waits, and a
  read-only progress query. It explicitly preserves signals received before the
  workflow reaches its wait.
- [Decora worker](https://github.com/arober39/decor-agent/blob/88658d4162fbd9798d23236f96eedbcd647dbaa7/app/worker.py):
  a separate process registers workflows and activities; a thread pool executes
  synchronous model calls. The client and worker share the data converter.
- [Decora tests](https://github.com/arober39/decor-agent/blob/88658d4162fbd9798d23236f96eedbcd647dbaa7/test_workflow.py):
  time-skipping server and mock activities exercise approvals, rejection, and early
  signals without paid model calls.

## Constraints from official sources

Workflow code must avoid filesystem, network, subprocess, environment-dependent,
and random operations. Put these in activities; use workflow-safe time and durable
waits. Keep the sandbox enabled and workflow imports small. Synchronous activities
require an executor; blocking work inside an async activity can stall the worker.
Long activities should heartbeat and set a heartbeat timeout for timely cancellation.
The SDK's `Replayer` validates compatibility against recorded histories.
[Python SDK](https://github.com/temporalio/sdk-python#workflows),
[activities](https://github.com/temporalio/sdk-python#activities),
[replay](https://github.com/temporalio/sdk-python#workflow-replay).

Signals mutate workflow state and are recorded in history. A signal call returns
when the server accepts it, not when the handler finishes. Queries are read-only
and need a polling worker. `workflow.wait_condition` supports waiting for human
input without an activity occupying a worker slot. Initialize signal storage before
running activities and consume responses explicitly; do not clear early input just
before waiting. If callers need validated acknowledgement, Updates offer validators
and a returned result.
[Message passing](https://docs.temporal.io/develop/python/workflows/message-passing).

Activities can execute more than once. Completed activity results are reused during
replay, but a worker can crash after an external effect and before reporting
completion. External idempotency keys or reconciliation are therefore necessary;
an activity wrapper alone does not provide exactly-once effects. Granular activities
reduce repeated work. Worker loss is detected through timeouts.
[Activity definition and idempotency](https://docs.temporal.io/activity-definition).

`RetryPolicy.maximum_attempts=0` means unlimited attempts. Explicitly bound attempts,
choose backoff and activity timeouts, and exclude non-retryable error types. Factory
verification failures are domain results that should enter the repair loop; invalid
credentials and policy violations should not trigger repeated paid agent runs.
The latter choices are recommendations for this repository.
[RetryPolicy API](https://python.temporal.io/temporalio.common.RetryPolicy.html).

Temporal recommends integration tests with a worker and replaceable activities.
`ActivityEnvironment` tests activity context and heartbeats. Local and time-skipping
workflow environments support actual SDK/server interaction.
[Python testing guide](https://docs.temporal.io/develop/python/best-practices/testing-suite).

## Recommended factory shape

These are implementation recommendations, rather than claims about current behavior:

1. One workflow per stable job ID; separate activities for plan, build/repair,
   verification, LaunchDarkly, review, and publication. Preserve the existing gate
   ordering and policy checks.
2. Keep SQLite/artifacts as a local projection and activity checkpoint store;
   Temporal owns durable ordering and human waits. Never read SQLite from workflow
   code. Supply immutable run configuration as input; load secrets only in workers.
3. Record stage outputs under job/stage/iteration keys. Reconcile worktree, flag,
   commit, and PR state on retry. Do not claim exactly-once model calls or arbitrary
   filesystem effects across a crash before checkpointing.
4. Carry clarification IDs in resume signals so duplicate or stale responses cannot
   satisfy a later human pause. Query exposes current stage and pending question.
5. Test stage order, failed verification/repair, human pause, early/duplicate input,
   exhausted retry, and persisted activity output reuse. Stop a worker while paused,
   signal with the worker offline, start a replacement, and assert completion without
   repeating completed stages. Replay the resulting history as a separate check.
6. Persist the development server database as well as workspace artifacts. A worker
   restart is different from discarding the Temporal server's data. Local worktrees
   mean replacement workers need access to the same filesystem; multi-host execution
   requires a further artifact/workspace design.
