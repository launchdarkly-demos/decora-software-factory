# Builder workflow replays

`test_replays.py` loads these deterministic Planner/Builder responses into a
`MockHarness` subclass. Each replay uses an isolated Git repository and the real
proposal applier, formatter, verification gate, deterministic reviewer, and local
(dry-run) publisher. It must reach `DONE` with a persisted `pr.json` and PR body;
no network calls, pushes, or remote PR creation occur.

- `issue_8.json`: whole-file replacement of an existing approved file.
- `issue_9.json`: omitted required test, actionable coverage feedback, then repair.
- `issue_14.json`: nested migration tests importing the root package, with an
  unplanned package initializer recorded in JobState and disclosed in the PR body.
- `issue_16.json`: five dependent tasks, a simulated output cutoff, a committed
  partial response, and continuation without task retries. A separate resume replay kills a worker
  process after two checkpoints and compares recovery with an uninterrupted run.

- `issue_19.json`: `model_update` checkpoints partial Home/Room models, hits the SDK's
  `MaxTurnsExceeded`, then completes using only insert/append edits. The SDK replay
  asserts fresh source on all three calls, no task retries, two checkpoints and a
  local draft PR after real pytest verification. A second variant starts with a
  large existing module and inserts using supplied sections far beyond its prefix.
  Under #23, safe fixes remove the partial checkpoint's unused `Room` import;
  the continuation appends that import alongside the tests that use it.

Run these replays with `.venv/bin/python -m pytest -q tests/test_replays.py`.

The eleven clearly named `test_replay_01_...` through `test_replay_11_...` tests map
to §11 of `docs/builder-spec.md`. Additional cases construct responses from these
fixtures in `test_replays.py`:

- 6 (#20a): 40KB source, lowered section threshold, unique anchor outside all
  supplied sections; the mocked SDK boundary applies it on the first call.
- 7 (#20b): invalid staged Python returns E9 context/caret without writes, then
  the next call fixes it and produces a draft.
- 8: real pytest failure in task 2's file routes back to task 2, with task 3
  already done. Same-size repairs also exercise checked-hash bytecode freshness.
- 9: `.env`, traversal, symlink escape and secret content each end FAILED with
  no writes or PR, as the safety requirement specifies.
- 10: SIGKILL after two checkpoints and dirty writes, followed by a fresh worker
  redelivering the started Temporal activity; compare the final Git tree, task
  summaries and completion state with the uninterrupted equivalent.
- 11: watcher comments preserve a non-default address/namespace; retrying the
  original issue creates `-r1`, a separate status comment, a ledger receipt and
  a second completed mock run with a persisted draft PR record.

#22 adds `test_issue22_replays.py`, acceptance replays 12–16:

- 12 routes a tests-only task's assertion failures to the imported migration module's owner.
- 13 spends all three quick retries, then fixes a full-verification failure using only
  the independent verification budget. Separate cases exhaust budgets 0, 1 and 3 with E22.
- 14 replaces a mangled control anchor against an original Unicode arrow and checks
  fallback records in the checkpoint and event log. Additional cases prove NFC,
  original-span preservation for inserts, escaped E5, and atomic ambiguous rejection.
- 15 suppresses quick targets and proposal commands on partial parts; completion with
  pytest exit 5 produces a correction naming the test file.
- `replay_r1` uses `issue_22_r1_plan.json`, copied from the original
  `job-gh-5873513712-r1/implementation_plan.json` receipt. Small deterministic room,
  store and migration fixtures reproduce the six-task sequence and two migration
  defects. The high-risk gate is approved in the harness; task 6's partial no-tests
  part is checkpointed without running pytest; the completed task's failing tests
  route to task 5, whose mangled-arrow repair reaches DONE and a persisted draft record.
  LaunchDarkly is offline/disabled and publication is local; no remote side effects.

Run these with `.venv/bin/python -m pytest -q tests/test_issue22_replays.py`.

#23 adds `test_issue23_replays.py`, acceptance replay 17:

- `replay_r2` uses the six-task `issue_23_r2_plan.json` receipt with raw Builder
  pytest commands, long lines, an unused test import and simulated ANSI output.
  Real gate-isolated pytest exposes the store/home-id bug and routes it to task 5
  with budget left. Both quick E13 and full E15 repair paths reach DONE with a
  persisted local draft PR. Assertions check checkpointed safe fixes, plain feedback
  and reports, gate config/root/cwd, and retry accounting.
- Unit cases cover command refusal and target extraction, safe versus unsafe fixes,
  repository and nested Ruff configuration, changed-file lint scope, ancestor
  isolation, and color suppression/stripping.

Run these with `.venv/bin/python -m pytest -q tests/test_issue23_replays.py`.

- `replay_r4` (#25, `tests/test_issue25_replays.py`) captures the verbatim opening
  heading from the reconstructed r4 `app/store.py` (`/tmp/fac/r4`), confirmed
  against `job-gh-5873513712-r4/{summary.json,log.jsonl,diff.patch}`. A proposal
  containing `Room\x192Home` replaces the `Room→Home` span via the unique
  escape-artifact fallback, records that span, and reaches DONE with real Git,
  pytest, verification, deterministic review and persisted local draft publication.
  The fixture isolates the observed anchor failure from target-app dependencies.
  Unit regressions cover payload characters, ambiguous/atomic edits, divergence
  context and complete redacted rejected-anchor receipts.
