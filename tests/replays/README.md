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
  partial response, and continuation without task retries. The test reloads the
  job after two checkpoints before completing the remaining queue.

Run these replays with `.venv/bin/python -m pytest -q tests/test_replays.py`.
