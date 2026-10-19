# Decora factory Builder spec

Status: approved, including Alexis’s #22, #23, #24, #25, #26, #27, #28 and #29 rules (Oct 18, 2026). It's based on `codex/builder-task-loop` at 989c214 (PR #17), issues #8, #9, #13, #14, #16, #18, #19 and #20, and the live runs `job-gh-5873513712` (Oct 16 to 18) and `job-gh-5913682653` (Oct 18).

"Must" is a rule. "Should" is a strong default that policy can override. All numbers are policy settings unless they're marked as constants.

---

## 1. Goal and boundaries

**Goal.** A maintainer adds `factory:ready` to a decor-agent issue. That issue becomes a draft PR that passes verification and has the required LaunchDarkly flag, created and off. No human steps are needed except the policy gates (plan review for high-risk or migration work).

**Pipeline and ownership.**

| Stage | Owner | Owns | Must not |
|---|---|---|---|
| Intake | Watcher | Turns a labeled issue into a frozen feature request and starts a job | Change a running job |
| Plan | Planner | Task list, files per task, test targets, flag key, risk, migration plan | Edit files |
| Govern plan | Governance | Risk and flag rules, the human review gate | Change intent (fixed policy flag-key normalization is deterministic) |
| **Build** | **Builder** | **Turning each task into file edits, one task at a time, until every task is checkpointed** | Run commands, touch protected paths, change the plan's intent |
| Verify | Verification gate | Full test runs with a separate retry budget, sending failures back to the owning task | Edit files |
| Flag gate | LaunchDarkly client | Making sure the flag exists in the policy project and is off in production | Turn the flag on |
| Review and govern ship | Reviewer and governance | Findings and the final policy check | Merge |
| Draft PR | Publisher | Branch, push and the PR body | Mark the PR ready or merge it |

The Builder's job ends when every plan task is checkpointed. Everything after that belongs to other stages. Those stages can hand work back to a specific task, but they never edit code themselves.

## 2. Inputs to each Builder call

Every Builder call, whether first, a split continuation or a correction, must receive:

1. **The task**: id, description, acceptance criteria, files, optional quick tests, dependencies and `rewrite_files: list[str]` (default empty; deliberate rewrites only). Test tasks should cover one behavior area and at most six test functions (planning/splitting guidance, never an apply rejection); migration and compatibility tests must be separate tasks, creating or appending to a test file. Governance deterministically splits broad test tasks by acceptance area, file and batches of at most six checks, preserving dependencies.
2. **Finished-task summaries**: id plus a summary of up to 600 characters for each checkpointed task, in order.
3. **Current file context**, read from the worktree at the latest checkpoint on every call. Never cached.
   - **Small file** (≤ `whole_file_limit`, see §13): the whole file, verbatim.
   - **Large file**: an outline (every `def`/`class` line with its line number) plus verbatim sections. Sections are ranked by symbols named in the task, the acceptance criteria and the last error, and always include the file's start and end. Each section is labeled with its line range, and the line numbers live only in the labels, never inside the text.
   - **Missing file**: marked "absent at checkpoint; create it". A **deleted** file is marked "deleted on purpose".
   - **Total size**: limited by `excerpt_budget`. When the budget is tight, the task's own files come first, in plan order.
4. **The last correction**, if any, exactly as defined in §5.
5. **Flag context**: the key, its default (off), and whether a flag is required.
6. **Limits for this call**: how many output tokens and turns it has left, so the Builder can size its answer.

Every model payload (Planner, Builder, Reviewer and structured read-tool results) must use `json.dumps(..., ensure_ascii=False)` so source characters remain verbatim.

The context is **for convenience, not a gate**. The Builder can read anything else it needs with the read tools (§3), and nothing it proposes is rejected for lack of having "seen" it.

## 3. Action set

Read actions are tools the Builder calls during its turn. Write actions are entries in its single `BuildProposal`. That proposal is applied **all-or-nothing**: either every edit applies, or none do.

### Read actions (no side effects)

| Action | Rule |
|---|---|
| `read_file(path, start_line, line_count≤200)` | Any safe path in the repo, verbatim, newlines kept |
| `list_files(glob)` | Safe paths only, at most 150 results |
| `search_code(text)` | Safe paths only; returns path:line hits |

"Safe" means it passes the path checks in §4.2. Reading files outside the task is allowed.

### Write actions

| Kind | Fields | Preconditions (checked at apply, against the real file as staged so far) |
|---|---|---|
| `create` (`write` on a missing path) | `path`, `content` | Path doesn't exist yet in the staged state |
| `write` (full replacement) | `path`, `content` | Path exists. It replaces the whole file. Allowed for any file in the plan or added automatically |
| `replace` | `path`, `old` (non-empty), `new` | `old` has exactly one match under the anchor rule below |
| `insert_after` / `insert_before` | `path`, `old` (non-empty), `new` | `old` has exactly one match under the anchor rule below; `new` goes right after or before the original span |
| `append` / `prepend` | `path`, `new` | File exists (or is being created earlier in the same proposal) |
| `delete` | `path` | File exists; the path passes safety checks |
| `move` | `path`, `new_path` | Source exists, destination doesn't; both paths pass safety checks. The destination is added to the plan automatically |

**When an anchor is valid, exactly.** Check at apply time against staged text. A non-empty anchor with exactly one exact match is valid. More than one exact match is E6. If there are zero exact matches, retry once with Unicode NFC normalization and folding of control/confusable characters on both sides. The folding must cover the observed arrow `→` versus mangled control `\x12`, canonical composed/decomposed characters, typographic quotes/dashes and invisible formatting characters. Accept only exactly one normalized match, map it back to the exact original span, and preserve surrounding original text (including the original anchor for insert actions). Multiple normalized matches is E6. If exact and NFC/folded matching both find zero matches, an escape-artifact fallback may run: each run of C0 controls other than tab/newline, including any trailing hex digits from a garbled escape (such as `\x19` + `2`), or literal `\uXXXX` sequences stands for exactly one non-ASCII source character. All other characters match literally. Accept only an exact single candidate span; zero matches is E5 and multiple matches is E6. Never guess between candidates. Record fallback use, path and original span in the checkpoint log. Whether the Builder read or was shown the anchor is irrelevant. Report escape-artifact fallback use exactly like NFC fallback use. Builder instructions should prefer short, ASCII-only anchors on code lines over docstrings or prose. E5 feedback must show the longest anchor prefix occurring in the file, the first divergent anchor line, and ±5 verbatim file lines around the divergence (with numbered labels). Escaped closest-line hints may be included additionally.

**Whitespace fallback.** After exact, NFC/folded and escape-artifact fallbacks find no match, normalize CRLF, trailing whitespace, indentation and internal horizontal whitespace per line. Accept only a unique span, preserve original source offsets and log it like the other fallbacks. Ambiguity remains E6. For failed replace anchors that share a nonblank source line with a small (<80 current lines) or job-created file, E5 includes the full current text and suggests a whole-file write; never apply a guessed write.

**Empty anchors.** Convert empty insert_after to append and insert_before to prepend. Convert empty replace to write of `new` only when the staged path is absent or empty; reject an existing nonempty file with full current text when small/job-created. Log every conversion in checkpoint metadata. All safety and atomicity checks still apply.

Edits run in the order they're listed, against the staged text. Edits that conflict, such as two creates of the same path, or an edit after a delete or move of the same file, are a correction.

## 4. Checks before anything is written

All checks run on the fully staged proposal **before any file is written**. If any check fails, nothing is written.

1. **Shape**: the proposal parses, every edit kind is known, the required fields are there. `task_complete` is a boolean.
2. **Path safety (fatal)**:
   - Paths are relative and normalised, with no `..`, and stay inside the worktree root.
   - No symlink anywhere along the path leads outside the root.
   - No protected path: `.env*`, `.git/`, policy files, and policy `protected_paths` such as `**/*secret*`.
3. **Content safety (fatal)**: the new content contains no credential values from the factory environment and nothing matching the secret patterns (API keys, tokens, SDK keys, private keys).
4. **Syntax (correction)**: every staged `.py` file is parsed (`ast.parse`). Planned JSON, YAML and TOML files are parsed too. On failure, the correction gives the file, line, column and message, plus about 20 lines of the staged text around the error. Formatting (`ruff format`) runs only after syntax passes, and a formatting failure is a correction with the same detail.
5. **Files added to the plan automatically (no pause, no correction)**: any path that passes checks 2 and 3 but isn't in the plan is added to the plan and recorded in `auto_added_files`, together with the task that added it. This includes `__init__.py` files for new packages. The rule covers create, write, edit and the destination of a move.
6. **Destructive edits (E9 correction)**: compare each existing file before and after the whole staged proposal, counting only removed lines matched to content at `job.worktree.base_sha`. Files absent at base and lines added earlier in this job are exempt. Reject removal of more than 50% of the base file’s lines (diff removed lines, even if replaced), except base files under 80 lines (small files are cheap to restore and often rewritten wholesale) or paths explicitly in the current task’s `rewrite_files`. Quote the head of the removed base-content region and removed/base-total counts. E9 feedback must tell the Builder to keep existing lines and change only the needed region using several smaller `replace`/`insert_*` edits; never ask it to change plan fields such as `rewrite_files`. On rejection restore touched tracked paths with `git restore --source=<last checkpoint sha>`; discard proposal-created files. No partial multi-file proposal may persist.
7. **Module names (correction)**: a new `.py` path must have valid Python names for the module and every package folder above it (no leading digits, no keywords).

Before quick or full verification, run `ruff check --fix` with safe fixes only, then `ruff format`, on changed `.py` files after syntax passes. Include these automatic edits in the task checkpoint commit. Respect the target repository's Ruff configuration; otherwise isolate Ruff from the factory/ancestor config and use correctness rules `E9` and `F` minus auto-fixable rules for the lint gate. Never gate line length without repository configuration.

Builder quick-test commands must be reduced to pytest targets; refuse other commands. All quick tests use the verification gate configuration (§8).

## 5. Errors

All tool executions set `NO_COLOR=1`; pytest uses `--color=no` and Ruff check uses `--output-format=concise`. Strip ANSI sequences from every report before logging or Builder feedback, including E10, E13 and E15.

Each error belongs to one of three classes:
- **Correction**: the same task is retried with the feedback, using 1 of `per_task_retries`, except failures reported by full verification (E14/E15), which use the independent job-level `verify_retries`.
- **Split**: the same task continues from its checkpoint, using 1 of `split_attempts`. A split doesn't cost a retry.
- **Fatal**: the job ends.

| # | Error | Class | Feedback (exact content) |
|---|---|---|---|
| E1 | Output cut off (token limit / `incomplete`) | Split | "Your response was cut off at N tokens. Return a smaller part of this task now with `task_complete=false`; the rest follows in the next call. Prefer insert/append over rewriting whole files." For test tasks, request at most six tests per call using append. Reopened/correction calls use the same split allowance; preserve IncompleteBuildError through wrappers. |
| E2 | Turn limit hit (`MaxTurnsExceeded`) | Split | "You ran out of turns (N). The current file text is included; return the next part of the task directly, using at most K reads." |
| E3 | Proposal malformed or schema invalid | Correction | Validation errors (field and reason), plus "Return one complete BuildProposal." |
| E4 | Empty proposal / no edits without `task_complete=true` | Correction | "No edits returned. Make concrete edits for task T, or set `task_complete=true` if the task is already done at this checkpoint." |
| E5 | Anchor matches 0 times after the fallbacks | Correction | File, the first 200 characters of the anchor, longest occurring anchor prefix, first divergent anchor line, and ±5 verbatim file lines around the divergence; optional escaped closest-line hints. "Anchors must occur exactly once in the current file." |
| E6 | Anchor matches more than once | Correction | File, number of matches, and line numbers of each. "Extend the anchor until it's unique." |
| E7 | Empty replace anchor on an existing nonempty file | Correction | Use a unique anchor or explicit whole-file write; empty inserts and empty/missing-file replacements convert under §3 without a retry. |
| E8 | Conflicting edits (create of an existing path, edit after delete, move onto an existing path) | Correction | Both edits and why they conflict |
| E9 | Syntax/parse error, destructive proposal (§4), or checkpoint import failure (§8) | Correction | File:line:col, message, about 20 lines of staged context with a caret |
| E10 | Formatter failure | Correction | The formatter's message with ANSI codes stripped, plus the E9 context |
| E11 | Invalid module or package name | Correction | Path and a suggested valid name |
| E12 | Placeholder markers in content (`[excerpt…]`, `[omitted…]`) | Correction | "Content contains preview markers; read the exact source." |
| E13 | Quick-test failure after `task_complete=true` | Correction | Failing test ids and the last 60 lines of output, routed to the module-under-test owner (§8). Exit 5 is a correction naming the target file. If the owning task has no retries left, record the result as advisory and move on |
| E14 | Planned file still missing when all tasks report done | Correction to the task that owns the file | "Create `<file>` covering: `<task description and acceptance>`." |
| E15 | Full verification failure | Correction to the module-under-test owner (§8), charged only to `verify_retries` | The failing test ids, relevant traceback, and current text of the routed modules; never consume `per_task_retries` |
| E16 | Protected path, `..`, symlink escape, path outside the root | **Fatal** | "Safety violation: `<rule>` on `<path>`." |
| E17 | Secret in content | **Fatal** | "Safety violation: credential-like content in `<path>`." The value is never echoed |
| E18 | Model/network error (5xx, timeout, connection), HTTP 429 / RateLimitError with transient codes such as rate_limit_exceeded | Infrastructure: up to 3 retries; honor retry-after / x-ratelimit-reset-* with exponential backoff plus jitter capped at 60s; injectable sleep. Never charge task retries or splits. Exhaustion pauses at the current stage | Safe provider code logged |
| E19 | Task retries exhausted | **Fatal** for the job | "Task T exhausted N retries; last error: …" |
| E20 | Job attempt cap reached | **Fatal** | "Job reached max_job_attempts (N)." |
| E21 | Split allowance exhausted | Continue with smaller-scope prompts, charged only to `max_job_attempts`; never a task retry | Request one small region or at most one test per call, using append/insert |
| E22 | Full-verification retry budget exhausted | **Fatal** | "E22: Job exhausted verify_retries (N); last verification failure: …" |

**Provider blocks (#27).** Inspect exc.code, exc.body and response JSON for the error code without logging secrets. HTTP 429 insufficient_quota, credit_balance_exhausted or billing codes, and 401/403 authentication errors, immediately raise ProviderBlockedError and pause at AWAIT_HUMAN, never FAILED. Preserve checkpoints and resume_stage = the current BUILD/PLAN/REVIEW stage. Question: `Provider billing/credentials problem: <code>. Fix the OpenAI account, then answer continue (or add factory:retry).` The GitHub watcher status comment includes the provider error code. Planner and Reviewer use the same classification and infrastructure retry/pause rules.

Nothing apart from E16, E17, E19, E20 and E22 may end a job. E19 still applies to ordinary Builder corrections; a full-verification failure itself cannot cause E19.

## Safeguard calibration (r1-r7 sweep)

- E9 applies only to base-commit files with at least 80 base lines and only removed base content; job-created files and job-added lines rewrite freely (§4).
- Anchors use unique whitespace fallback and actionable whole-file feedback; empty inserts convert without correction (§3).
- Output/turn cutoffs always split, even after the split allowance; smaller continuations consume only the hard job-call cap (40). Task and verification retry defaults are 4.
- A retry with a strictly shrinking failing-test set or decreasing failing count is uncharged, at most three consecutive times per owning task and budget (quick/task vs full verification). Persist the preceding failure signature and streak. No identified failures means no progress credit. A charged correction or success resets the streak. Unrelated failures cannot qualify solely by IDs; sets must be a strict subset (or an explicit pytest summary count must decrease).
- Flag audit accepts typed declarations, any constant bound to False used as the evaluation default, literal keyword/positional False and LD variation(key, ctx, False). True or unknown evaluation defaults still fail, even alongside a False declaration.
- Six tests is planning guidance; apply never counts/rejects test functions. Production-caused test failures route to production owners. Safe Ruff fixes and format run before checkpoint imports, quick tests and full verification; unfixable errors remain corrections.
- Transient provider backoff spends no task retries/splits; quota/auth pauses preserve checkpoints. Quick commands remain pytest-only because Builder commands are untrusted; safe extra files are auto-added. Plan repair stays bounded and pauses for intent/structure problems after deterministic normalization.
- E16/E17, protected/escaping/deny paths, default-off flags, Builder git push/reset prohibitions, no pushes to main and the hard max_job_attempts cap are unchanged.

## 6. Budgets

| Budget | Setting | Default today | Proposed |
|---|---|---|---|
| Output tokens per Builder call | `builder_max_output_tokens` | 6000 | **16000** (gpt-4.1 allows 32k of output; fewer splits) |
| Turns per Builder call | `builder_max_turns` | 40 | 40 |
| Full-verification retries per job | `verify_retries` | 3 | 4 |
| Retries per task | `per_task_retries` | 3 | 4 |
| Splits per task | `split_attempts` | 3 | 4 |
| Builder calls per job | `max_job_attempts` | 30 | 40 |
| Build stage time limit | `stage_timeout_seconds.build` | 180 s | **600 s per call** (a 40-turn call can take longer than 180 s) |
| Whole-file limit / excerpt budget | `whole_file_limit` / `excerpt_budget` (currently constants) | 12 KB / 40 KB | **64 KB / 160 KB** (§13) |

**Cost and time per run** (assuming gpt-4.1 list pricing of about $2 per million input tokens and $8 per million output tokens; check current pricing before relying on these numbers):

- **Measured:** `job-gh-5913682653` used 9 Builder calls, 310k input and 14k output tokens, about 16 minutes. That's roughly $0.73, about 34k input tokens per call.
- **Estimated full run of 5 tasks (about 12 to 18 Builder calls):**
  - 0.5 to 0.9M input and 40 to 80k output tokens, about $1.50 to $2.50.
  - Planner and reviewer add about $0.20.
  - With the proposed 160 KB budget, input grows about 1.5 to 2×, about $2.50 to $4.00 per run.
  - Time: 25 to 45 minutes of building plus verification, not counting human gates.
- **Hard ceiling:** `max_job_attempts` × (input budget + output limit), about $15 at the proposed defaults. Exceeding it is impossible by construction.

## 7. Checkpoints and resume

- After a proposal applies, the factory commits only that proposal's paths (`checkpoint <task>[/<part>]`). It records the commit hash under the task, the `task_complete` flag and the summary.
- A task is **done** only when `task_complete=true` and the checkpoint is committed.
- **Partial continuations** add checkpoints under the same task and consume only `max_job_attempts`; reserve `split_attempts` for E1/E2 failures, including corrections of reopened tasks.
- **JobState** keeps: `current_task_id`, `completed_task_ids`, `checkpoint_shas`, `task_retries`, `verify_retries_used`, `verification_task_routes`, `task_split_attempts`, `builder_calls`, `auto_added_files`, `deleted_files` and `task_summaries`.
- **Resume after a crash, worker restart or Temporal replay:** reset the worktree to the latest checkpoint commit, rebuild the file context from disk, and continue from `current_task_id`. Uncommitted edits are thrown away.
- A correction or split always starts from the latest checkpoint, never from a half-applied proposal, because apply is all-or-nothing.

Every rejected proposal must append a receipt to the run’s `rejected_edits.jsonl`, retaining each edit’s path, kind and full anchor after secret redaction, including safety rejections. Receipts must never contain environment credential values or credential-pattern matches.

## 8. Verification, routing failures back, the flag gate and the PR

**Per-checkpoint imports.** After every task checkpoint, including partial parts and applied corrections, import every changed production Python module with the target interpreter, cwd=worktree and the verification environment. Use `python -c` with `importlib.import_module` per module; exclude tests and map standalone scripts by path with importlib file loading. An import failure reopens the task whose files include that module (otherwise current task), retains the checkpoint and traceback, and consumes a task correction, never `verify_retries`. Collection/import tracebacks route to the innermost production frame’s owner before test-file ownership.

**Required flag task (#28).** When flag_required(policy, job, plan), normalization ensures exactly one production task owns the flag. Use an existing production flags.py/flag.py from the repository, otherwise policy flags.module (default app/flags.py). Acceptance requires defining `<KEY_CONST> = '<flag_key>'` and `FLAG_DEFAULT = False` there, and gating new behavior with `get_flag(<KEY_CONST>, context, default=FLAG_DEFAULT)`. If absent, insert `provision_flag_<sanitized key>` after production tasks and before test tasks, with the flag module and gated production files. Log the insertion and include it in the plan receipt and PR body. Governance verifies ownership before BUILD; no human send-back for this repair.

- **#28 follow-up (r7):** Choose the existing `provision_flag_*` owner, else the first production task containing the flag module, else insert one; complete its canonical constant/default/gating acceptance and module scope, merge/remove other provision tasks and repoint dependencies. The owner depends only on production tasks without transitive test dependencies; every test depends on it. After flag and test normalization, drop unknown dependencies and stably topologically sort; break cycles by dropping normalization-added edges first, then production-to-test edges, logging each repair. Repeated normalization must yield the same plan.
- **#29 plan consistency:** Intersect task files (including echoed flag-owner gated files) with `plan.files` before normalization. Drop tasks left with no files and repoint their dependents transitively to their own dependencies. Log dropped files, removed tasks and dependency repairs in `normalization_notes`; only the required flag module may be added to both plan and owner scope.

**Fixed flag keys.** `flags.issue_flag_keys` maps canonical `owner/repo#number`, numeric issue strings, or full GitHub issue URLs to keys; `default_key` is an optional fallback. Example: `{"arober39/decor-agent#8": "decora-room-home-migration"}`. The planner payload declares the fixed key mandatory when the job requires a flag. Governance sets/overrides `plan.flag_key` deterministically and logs old/new values; a different planner key is replaced without a repair round.

**Quick tests.**
- Run only after a checkpoint reporting `task_complete=true`, on that task's `quick_test_targets`. Never run on partial parts, including proposal commands that are quick tests.
- Every quick test runs through `VerificationGate.run_quick`, using generated `pytest-gate.ini`, `--rootdir <worktree>` and cwd = worktree. Builder-supplied pytest commands contribute targets only; discard pytest options (including config/root overrides), and refuse non-pytest commands.
- Route failures to the module-under-test owner below (E13), using that owner's `per_task_retries`. Exit 5 (no tests collected) is a correction to the test-file owner naming the target file; there is no failing test from which to infer a module defect.
- If the owning task's retries are spent, record the failure as advisory and continue to full verification.

**Full verification.**
- Run after every task is done; repeat after routed fixes until it passes or the independent job-level `verify_retries` budget is exhausted (default 4 retries after the initial run).
- Every planned file must exist (E14).
- Safe Ruff fixes and formatting of changed Python files are already committed in the task checkpoint before verification. Gate changed Python files using repository Ruff config when present; otherwise use isolated correctness rules `E9` and `F` minus auto-fixable rules. No line-length gate without repository config.
- Run `pytest` on plan targets using `pytest-gate.ini` with the worktree root on `pythonpath`; then configured linters. Exit 4 names the missing path.
- Each failed full verification reopens the owning task with E15 and consumes one job-level verification retry, without changing that task's `per_task_retries`. Persist the used count and routing history by task. After all allowed retries fail, E22 ends the job. Ordinary Builder corrections and the job call cap retain their own budgets.

**Default-off flag evidence.** When policy requires default-off, accept either (a) `FLAG_DEFAULT = False` in production code, or (b) every evaluation of the selected flag key, literal or via a constant bound to it, supplies a literal false default (`default=False` or positional `False`). Any true default fails, even alongside false evidence. Failure messages must name both accepted forms and the offending source lines. Builder and Planner prompts must state the convention: define `FLAG_KEY` and `FLAG_DEFAULT = False` next to the key and evaluate with `default=FLAG_DEFAULT`.

**Sending a failure back (quick or full verification).**
Flag-audit verification failures bypass module-under-test routing: select the plan task owning the flag module, or the production file containing the selected key. Never select a tests-only task for a flag-audit failure. Preserve original ownership and the normal reopening/dependent-task rules below.

1. Collect failing test files and repo traceback frames. Prefer the deepest non-test repo frame (closest to the failure). Also resolve repo modules imported by the failing test file, including relative imports and imports of symbols/submodules. Ignore external modules and unrelated command lines.
2. For a candidate production module (deepest frame takes precedence over imports), prefer production tasks over broad test tasks that also list the file, then prefer the task that most recently edited it; break ties by the **last** owner in plan order. If no module owner is found, fall back to the previous rule: last task owning a mentioned traceback/test path, else last task in the plan.
3. Add the identified repo modules to the fixing task's scope and supply their current text in its next context. Preserve original ownership when adding repair scope so later routing stays stable.
4. Mark the owner not done. For a quick-test failure from another task, preserve the reporting task's completed checkpoint; the module owner does the repair. Re-check dependent tasks only when they touched the owner's files later.
5. Run full verification once the queue is all done again. Quick-test corrections use the owning task's retries; full-verification corrections use only the job verification budget.

**Flag gate.**
- It runs only after verification passes.
- If the change class requires a flag, the flag `<plan.flag_key>` must exist in project `launchdarkly.project_key` (currently `alexis-interior-design-demo-app`).
- It's created if missing, as a boolean that is off in every environment, and production must read as off.
- Infrastructure errors pause the job (await_human) with the error text scrubbed of secrets. The factory never turns a flag on or starts a rollout.

**PR body** (draft only). Sections, in this order:
- Title
- "Draft pull request. Do not merge."
- Summary with the issue link
- **Rollout**: flag key, default off, link, rationale, and the guarded-rollout stages when policy requires them
- Implementation plan and tasks
- Migration and recovery, when required
- **Files added to the plan automatically**, each with the task that added it
- Test evidence for each verification attempt
- Review findings
- Remaining risks
- Retries (per task, splits and job calls)
- Diff summary

## 9. Watcher lifecycle and rerun rules

**Start.**
- Command: `factory temporal watch-issues --address <addr> --slug <owner/repo> --task-queue <q> --home <dir>`.
- One watcher per home, enforced by `issues.lock`. The state's scope (repo, queue, namespace) must match.

**Intake** (polls every ≥10 s, default 30 s).
1. It finds open issues with the label `factory:ready`.
2. The most recent "labeled" event for that label must come from someone with write, maintain or admin access.
3. Job ID: `job-gh-<issue.id>`.
4. Temporal rejects duplicate IDs, and the ledger (`issues.json`) remembers every job it has accepted.

**Status.**
- There's one comment per job, marked `<!-- software-factory:<job> -->` and edited in place.
- Every command it prints must include the watcher's real `--address`, and `--namespace` when it isn't the default (#18).
- The comment shows the stage, the gate text with its pause ID, the task list, a link to the PR when there is one, and the failure reason on failure.

**After the job ends.** The ledger marks the job finished. Editing, relabeling or reopening the issue never starts another run.

**Reruns.**
- Today: open a new issue that links the old one and add the label (the documented path).
- Spec: add a supported rerun. A maintainer adds the `factory:retry` label to the original issue, and the watcher starts `job-gh-<issue.id>-r<n>`, records it in the ledger, posts a new status comment and removes the label.
- For an active provider billing/credentials pause (#27), an authorized `factory:retry` label signals `continue` with the current pause ID to the same workflow and removes the label; it preserves checkpoints and does not start a fresh run. Other active jobs still block reruns.
- The ledger must never be edited by hand.

**Outages.** If GitHub or the watcher goes down, jobs keep running in Temporal. A failed sync is retried on the next poll. An empty `FactoryError` log line isn't acceptable; logs must say which job failed and why.

## 10. Past failures mapped to rules

| Failure | What broke | Rule that prevents it |
|---|---|---|
| r1–r7 sweep | Progress spent retries; cutoffs became corrections; empty inserts and job stubs were blocked | Calibration section; §3–§6; replay_safeguard suite |
| #8 | Whole-file write to an existing file was fatal | §3 `write` allowed for files in the plan; §4.5 |
| #9 | Builder skipped a required new test; pytest ran on a missing path | §8 planned files must exist (E14); exit code 4 names the path |
| #13 | Missing-file corrections used verify retries; multi-task plans ran out of budget early | §5 per-task budgets; §8 full verification only once every task is done |
| #14 (a) | Nested tests couldn't import `app` | §8 `pythonpath` set to the worktree root |
| #14 (b) | Out-of-plan `__init__.py` was fatal | §4.5 files added automatically |
| #14 (c) | `0001_room_home.py` isn't a valid module name | §4.6 (E11) |
| #16 | "Finish everything at once" overflowed the 6000-token limit; wrong advice | §2 one task per call; E1 split; §6 16k limit |
| #19 (a) | Follow-up calls couldn't add code | §3 insert/append/prepend; E7 |
| #19 (b) | `MaxTurnsExceeded` used up a retry | E2 is a split |
| #19 (c) | No file text in follow-up calls | §2.3 context on every call |
| #20 (a) | Valid anchors in an 18.8 KB file rejected as "not read" | §3 anchor validated once, at apply |
| #20 (b) | Syntax errors found late, without context | §4.4 parse before writing (E9) |
| #18 | Watcher comment's command reached the wrong server | §9 commands include the real address |
| #22 / `job-gh-5873513712-r1` | Tests-only task exhausted quick retries for migration defects; full verify hit E19; arrow anchor mangled to control; partial quick test exit 5 | §3 normalized unique fallback and escaped E5; §5 E13/E15/E22; §6 independent verify budget; §8 module routing and completion-only quick tests |
| #23 / `job-gh-5873513712-r2` | Raw quick pytest inherited factory config; style lint exhausted verify retries; ANSI leaked into feedback | §4/§8 gate-only quick tests, safe fixes + format in checkpoint, correctness lint unless repo config; §5 plain tool output |
| #24 / `job-gh-5873513712-r3` | Default-off evaluations rejected; flag repairs routed to tests-only task three times, ending at E22 | §8 both evidence forms, prompt convention and flag-owner routing |
| Ops | Live runs blocked by manual job IDs, port and address mix-ups, project key changes | §9 rerun via label; §11 live definition of done |
| #25 / `job-gh-5873513712-r4` | Escaped non-ASCII payloads garbled `Room→Home` into `Room\x192Home`; multi-line feedback hid divergence and full rejected anchors were lost | §2 verbatim payloads; §3 unique escape-artifact fallback and ASCII code anchors; §5 divergence feedback; §7 redacted rejected-edit receipts |
| #26 / `job-gh-5873513712-r5` | `app/store.py` lost its first 238 lines (the `-1,241 +1,3` hunk); collection errors went to tests; reopened test cutoff spent retries; flag key drift | §4 destructive guard and checkpoint restore; §8 per-checkpoint imports and innermost production owner; §2 bounded test normalization; E1 split preservation; §8 fixed policy key |
| #27 / `job-gh-5873513712-r6` | Three RateLimitErrors spent task retries and FAILED after five checkpoints | §5 provider classification, header-aware backoff and current-stage pause preserving checkpoints |
| #28 / `job-gh-5873513712-r6` | Required key existed without a production flag task; human plan send-back needed | §8 deterministic required flag task before tests, governance check and receipts |
| #29 / `job-gh-5873513712-r7` | Guard rejected rewrites of a same-job stub; E9 asked Builder to change plan fields; stale task scope survived plan revisions | §4 base-content-only guard and actionable E9; §8 plan-file consistency; §11 replay_r7 |

## 11. Acceptance criteria

**Replay suite** (mock harness, in CI, each must reach `DONE` with a draft PR record):

1. **#8**: a whole-file write to an existing planned file.
2. **#9**: first call skips a planned test, which is completed later.
3. **#14**: a nested `tests/migrations/` test, plus an `app/migrations/__init__.py` that gets added automatically and listed in the PR.
4. **#16**: a 5-task plan where one call is cut off and the split finishes it, with 0 retries spent.
5. **#19**: a split continuation adds code with insert/append only, and a `MaxTurnsExceeded` costs no retry.
6. **#20 (a)**: a replace in a 40 KB file whose anchor is outside the supplied sections but occurs exactly once applies first time.
7. **#20 (b)**: a syntax error gets E9 with context and is fixed on the next call.
8. **Routing**: a full-verification failure in task 2's file goes back to task 2, not to the last task.
9. **Safety**: `.env`, `..`, a symlink escape and a secret in content each end the job (E16/E17), and nothing is written.
10. **Resume**: kill the worker between two checkpoints, and the job resumes from the last checkpoint with an identical end result.
11. **Watcher**: a comment generated with a non-default address includes `--address`, and a `factory:retry` label starts `-r1`.

12. **#22 routing**: tests-only task fails on an earlier task's imported module; route back to that owner with fresh module scope/context.
13. **#22 budgets**: quick retries exhausted, then full verification still reopens the owner without charging task retries; a successful fix reaches DONE. Also prove E22 at exhaustion.
14. **#22 anchors**: mangled control/Unicode anchor uses a unique normalized original span, logs fallback, and reaches DONE; ambiguous fallback rejects atomically and E5 escapes closest lines.
15. **#22 timing**: a partial part with no collected tests runs no quick tests; completion with exit 5 produces a correction naming the file, then reaches DONE.
16. **`replay_r1`**: reproduce `job-gh-5873513712-r1` end to end with its six-task plan, tests-only task 6 exposing task 5 migration defects, arrow/control anchor and partial no-tests part; reach DONE with a persisted draft PR record.

17. **`replay_r2` (#23)**: reproduce `job-gh-5873513712-r2` end to end in the mock harness with raw Builder pytest commands, long lines, an unused test import and ANSI-colored output. Quick tests expose and route the actual store/home-id bug with budget left; safe fixes are checkpointed, feedback is plain, and the run reaches DONE with a persisted draft PR record. Unit tests cover command refusal/target extraction, gate isolation/cwd, safe fixes/config handling, lint scope and color suppression/stripping.

**#24 / replay_r3.** Replay `get_flag(KEY, ctx, default=False)` with a bound key constant through the real audit and verification to DONE with a persisted draft PR. Unit regressions cover true and mixed defaults, constant-bound keys, accepted-form/offending-line diagnostics, and flag-owner routing instead of a tests-only task.

**#29 / replay_r7.** Task A creates a 35-line stub absent at base; task B replaces all of it and reaches DONE with a persisted draft PR. Base-file removal >50% still rejects atomically with checkpoint restore and actionable E9. Regressions exempt earlier job additions in existing files and prove stale plan files are removed from tasks/flag owners, empty tasks are removed with transitive dependency repointing, and normalization notes and idempotence are preserved.

**Live definition of done** (decor-agent, real model, `--ld live --push`):
- A maintainer labels an issue with a high-risk migration plan (the #8/#12 room-home migration). The watcher starts the job without anyone typing a job ID.
- After the human approves the plan, the job reaches a **draft PR on arober39/decor-agent** with no further human steps, and:
  - full verification passes on the plan's targets;
  - the flag `decora-room-home-migration` exists in `alexis-interior-design-demo-app`, off in production (checked through the LaunchDarkly API);
  - the PR body has every §8 section, including the automatically added files;
  - total Builder calls ≤ `max_job_attempts`, and cost within the §6 estimate.
- This must happen on **two consecutive live runs**, with the second on a different issue, so one lucky run doesn't count as done.

**#25 / replay_r4.** A Builder proposal anchored on `"""\nProject store. Memory by default; the server can persist rooms to a JSON file.\n\nRoom\x192Home Migration Robustness` against `Room→Home` must apply via the escape-artifact fallback and reach DONE through the mock harness. Regressions must cover verbatim non-ASCII model payloads, ambiguous fallback rejection, longest-prefix/divergent-line/±5 verbatim feedback, and full rejected-edit receipts with secret redaction.

## 12. Change list status (baseline 989c214)

Items 1–13 describe the baseline implementation changes already delivered on this branch; remaining audit limitations are tracked in [builder-spec-traceability.md](builder-spec-traceability.md).

1. **Anchor pre-check:** remove the "anchor was not read in this attempt" check in `harness/openai_agents.py` and rely on the apply-time check (§3). Add the closest-lines detail to E5 and E6.
2. **Parse first:** run `ast.parse` (and parse planned JSON, YAML and TOML) on every staged file before writing in `agents/apply.py`, giving E9 with context. Strip ANSI codes from formatter output (E10).
3. **Size settings in policy:** move `WHOLE_FILE_THRESHOLD` and `EXCERPT_BUDGET` into policy as `whole_file_limit: 64000` and `excerpt_budget: 160000`. Label section line ranges in metadata (already done) and put the task's files first.
4. **Budgets:** raise `builder_max_output_tokens` to 16000, `split_attempts` to 4 and `max_job_attempts` to 30. Make the build stage time limit apply per Builder call, at 600 s.
5. **`task_complete=true` with no edits:** allow it (E4), for tasks already satisfied at their checkpoint.
6. **Quick tests:** make a quick-test failure a correction while the task still has retries (E13). Today it's always advisory.
7. **Routing:** pick the **last** task that owns the failing file, and re-check dependent tasks only if they touched it later (§8).
8. **Model errors:** handle E18 retries with backoff, not charged to the task.
9. **PR body:** add the adding task's id to each automatically added file.
10. **Watcher rerun label:** add `factory:retry` (job ID suffix `-r<n>`, ledger entry, new comment, label removed afterwards).
11. **Watcher logs:** sync errors log the job id and the error message, not just the type name.
12. **Replays:** add replays 6, 7, 8, 10 and 11 from §11; replays 1 to 5 and 9 exist already.
13. **Docs:** replace `docs/general-planning.md`'s Builder section with a pointer to this spec.

14. **#22 implemented and replayed:** module-under-test routing and repair scope/context; independent `verify_retries: 3` with E22; unique normalized anchor fallback with checkpoint logging and escaped E5; completion-only quick tests and named exit-5 corrections. Replays 12–16 reach DONE with local draft PR records; E22 exhaustion and ambiguous-anchor atomic rejection have explicit regression coverage.

## 13. Recommendation: large-file handling

**Change it: raise the whole-file limit, and keep sections only for genuinely large files.**

- **Context is cheap, guessing is expensive.** gpt-4.1 has a context window of about 1M tokens. The current 12 KB limit (about 3k tokens) sent only sections of a 19 KB `store.py`, and that cost a whole run (#20). Sending the whole 19 KB file costs about 5k input tokens, roughly $0.01 per call.
- **Proposed:** whole files up to **64 KB** each (about 16k tokens) and **160 KB** per call in total (about 40k tokens). That covers every file in decor-agent today; its largest are 15 to 25 KB. Input cost per run rises by about $0.50 to $1.50.
- **Above 64 KB:** outline plus ranked sections, as today. Because of §3, a section only helps the Builder; it's never a requirement for an edit to be accepted.
- **Don't** raise output limits to fit whole-file rewrites. Use insert/replace/append for existing files, so output stays small even when the input is large.
- **Revisit** if the factory moves to a model with a smaller context window. The limits are policy settings, so that's a configuration change, not a code change.

**#27/#28 / replay_r6.** Replay the r6 plan without flag ownership: insert provision_flag before test tasks. Three transient RateLimitErrors then success reach DONE with unchanged task_retries. insufficient_quota pauses at AWAIT_HUMAN with resume_stage BUILD and checkpoints retained; answer continue resumes to DONE. Unit coverage includes safe code extraction, retry-after/reset headers and 401 authentication, PLAN/REVIEW pauses and exhausted transient retries.

**#26 / replay_r5.** Reject and restore a replace truncating `app/store.py` to three lines, preserving other proposal files; charge a broken production import to its owning task with no verify retry; reopen a test task, truncate its correction and use split allowance; repair and reach DONE with a persisted draft PR. Replay the r5 plan’s wrong flag key and prove deterministic policy override. Unit coverage includes threshold/exemption/explicit rewrite, import mapping/environment, traceback routing, normalization/dependencies and split exhaustion.

## §11 supplemental safeguard sweep replays

`tests/test_safeguard_sweep_replays.py` replays r6 empty-anchor insert, r5 cutoff beyond split allowance, r1 shrinking failures, r7 stub replacement, whitespace uniqueness, flag equivalents and true-default refusal, safe formatting, >6 tests and unchanged safety/provider behavior. Original r1–r7 receipts remain the evidence; tests use deterministic Git-backed proposals and failure sequences.
