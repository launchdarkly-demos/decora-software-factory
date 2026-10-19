## Goal
Fix the README "Tests" section so its commands actually run the offline test suites.

## Problem
`README.md` → **Tests** says:

```bash
python test_catalog.py test_store.py test_mcp_catalog.py test_mcp_project.py test_harness.py
```

`python` runs only the first file (`test_catalog.py`) and passes the rest as arguments, so most suites never run. The list also leaves out the newer offline suites in the repo root: `test_api_project.py`, `test_board.py`, `test_events.py`, `test_flinch_gate.py`, `test_request_errors.py`. pytest isn't in `requirements.txt`, so the README should say how to get it.

## Acceptance criteria
- The README Tests section uses `python -m pytest -q` with the offline suites listed explicitly. Keep `test_agent.py` (live, needs `ANTHROPIC_API_KEY`) and `test_llm.py` out of the offline command.
- It notes `pip install pytest` (README only; don't change `requirements.txt`).
- The live-routing example (`LOG_LEVEL=WARNING python test_agent.py`) stays as it is.
- Change only `README.md`. No code, API, UI, or dependency changes.

## Change type
docs_only, low risk.

---
This is a factory test issue for the Decora software factory's org target.
