# GitHub issue workflow

The factory polls GitHub from the same host as its Temporal worker. No public webhook
endpoint or GitHub Actions self-hosted runner is required. Issue text is frozen at
submission; comments are not executed. GitHub authentication comes from `gh`.

## Prepare the target repository

Install `.github/ISSUE_TEMPLATE/factory-task.md` from this repo in the target repo's
default branch. The template deliberately does not apply a trigger label itself.
Create a `factory:ready` label. A maintainer with write/admin access applies it after
reviewing the request. The bridge checks the most recent label event and the actor's
current repository permission; triage/read access alone does not authorize execution.
The worker refuses issue requests whose GitHub repository differs from its origin.

## Run on this machine

From `/Users/alexisroberson/Documents/the-software-factory`, start the Temporal server
if it is not already running:

```bash
make temporal-server
```

In another terminal, run the real worker (do not mix different worker configurations
on the same task queue):

```bash
.venv/bin/python -m factory temporal worker \
  --repo /Users/alexisroberson/Documents/Claude_Code_Projects/langgraph-projects/decor-agent \
  --home "$PWD/.factory/decora" --task-queue decora-factory \
  --policy policy.decora.yaml --base-ref main --base-branch main --ld live --push
```

`main` is the local source branch. Fetch/update it deliberately before starting a new
batch; the worker does not automatically update the source checkout. Each job pins
its source commit and uses an independent clone. The existing `.env` supplies the
OpenAI and LaunchDarkly credentials. Publishing uses the worker's GitHub access.

In a third terminal, start the issue bridge:

```bash
.venv/bin/python -m factory temporal watch-issues \
  --slug arober39/decor-agent --task-queue decora-factory \
  --home "$PWD/.factory/decora-issues"
```

Add `--once` to reconcile once and exit. Default polling is every 30 seconds. Keep
the bridge home on persistent storage; one bridge process owns it at a time. Use a
separate home and queue for another target. The bridge uses github.com and the same
Temporal address/namespace options as the other commands.

## Submit and review

1. Open a GitHub issue using **Decora factory request**. Describe the goal, acceptance
   criteria, scope, verification and rollout needs. New feature flags and guarded rollout are deferred.
2. A maintainer adds `factory:ready`. This authorizes model calls and, for a worker
   started with `--push`, a draft PR. The issue must still be open at intake.
3. The bridge creates one `job-gh-<GitHub issue ID>` workflow. A single status comment
   is updated as stages change and eventually links to the draft PR.
4. If clarification is needed, the comment supplies the `temporal answer` command
   and current pause ID. Answers currently come from the trusted local CLI, not comments.
5. Review/test the PR and merge manually. Deployment is a separate human decision.

Removing the label or closing the issue stops new intake but **does not cancel a job
already accepted**. The bridge continues reporting accepted jobs. Editing or relabeling
the issue does not rerun it. To request a deliberate retry, create a new issue linking
the old one. Do not delete bridge state: Temporal prevents duplicate IDs while its
history is retained, and the bridge ledger remembers completed issues beyond that.

If the bridge or GitHub is unavailable, execution continues in Temporal. Restart the
bridge with the same home to reconcile status. Failed comment updates are retried;
completed-job comments are reconciled before the ledger marks them finished. Retain
Temporal history and bridge state together. This is single-host operation, not a
multi-host distributed lock implementation.

## Current scope and remaining work

| Area | Current behavior | Remaining work |
| --- | --- | --- |
| Issue intake | Maintainer label, deduplicated Temporal submission, status/PR comment | GitHub-based clarification, explicit rerun/cancel controls, PR-review feedback loop |
| Planning | General SDK Planner with ordered tasks, dependencies, migration plans and read-only tools | Independently scheduled sub-jobs/PRs; broader end-to-end acceptance cases |
| Context | Frontend/backend discovery and bounded file/search tools | Larger-repository scaling and richer semantic navigation |
| Verification | Required profiles, selected pytest regressions, new Python lint and compile | Provision Decora browser/migration suites; required CI and broader coverage |
| Isolation | Independent clone, path/command policy; shared host and target virtualenv | Per-job sandbox/container with constrained credentials, filesystem, network and resources; command allowlists are not a security sandbox |
| Publishing | Draft PR, stable branch reconciliation, issue link | Required CI/branch protection, preview deployments, revision loop for reviewer comments |
| Durability | Local Temporal database, receipts and Git bundles | Supervised startup/restarts, backups and tested restore, alerts; production Temporal configuration |
| Cost and control | Bounded retries/timeouts and reported tokens | Per-job spending budgets, queue limits, cancellation and operational monitoring |
| Deployment/release | Human reviews and merges; no automated deployment | Preview workflow, human release decision and rollout/rollback procedure |

Kubernetes is not required to close these gaps. First make the single-host workflow
reliable and support the classes of Decora tasks you actually want it to perform.

## Request format

The **Decora factory request** template starts with the product goal, change type,
acceptance criteria, scope, verification, rollout, and context. The intake parser
preserves scope, verification, and rollout text for the agents; context becomes
file-discovery hints. Markdown checkboxes are accepted and instructional HTML
comments are ignored. Existing Summary/Acceptance/Flag/Hints issues still work.

A blank template has no acceptance criteria and therefore pauses. Choosing
`feature flag: no` records rollout intent. User-facing alone does not require a flag.
Feature flags and guarded rollout are currently disabled by policy, including for
older issues that specify a flag key. Risk review and verification remain independent. `user-facing: unsure` is
conservatively treated as user-facing. This format supports proposing broader
work without claiming the current Planner can automatically implement it.

## Demo: request to reviewed pull request

A feature request describes a desired product improvement. A GitHub issue is the
tracked record containing that request (or a bug report or maintenance task).
The factory parses the issue into its internal `FeatureRequest` model; there is
no separate feature-request submission to create.

For a live demo, create one small, supported request using **Decora factory request**
in the repository watched by the issue bridge. Show the acceptance criteria, then
apply `factory:ready` after maintainer review. Follow the workflow status through
verification and the resulting draft PR. Rehearse with the configured verification
profiles before going live. The multiple-home-projects request remains a larger
follow-up until browser and migration verification are configured.

Generated PRs include a short **Rollout** note. With flags disabled, they report no
feature flag requirement, even when a prior saved request contains a flag key.
Detailed run evidence stays in artifacts. New flags and guarded rollout are deferred.
