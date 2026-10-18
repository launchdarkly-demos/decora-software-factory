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
