# Running a second target (personal vs org)

The factory can serve two checkouts at once against the same Temporal server. Each
target has its own policy, task queue, and home directories so jobs never cross.

## Side by side

| | Personal | Org |
| --- | --- | --- |
| Repo | `arober39/decor-agent` | `launchdarkly-demos/decor-agent` |
| Checkout | (your personal clone) | `~/Documents/decora-org/decor-agent` |
| Policy | `policy.decora.yaml` | `policy.decora-org.yaml` |
| Flags | Factory plans, audits, provisions (`flags.management: factory`) | Externally managed (`flags.management: external`; Release Autopilot) |
| Task queue | `decora-factory` | `decora-factory-org` |
| Worker home | `.factory/decora` | `.factory/decora-org` |
| Watcher home | `.factory/decora-issues` (or similar) | `.factory/github-org` |
| Draft PRs | yes (`--push`) | yes (`draft_only` + `allow_push`) |

Do not disturb a live personal worker/watcher when starting the org pair.

## `flags.management`

- **`factory` (default):** current behavior — required flags, audits, `ensure_flag`, LaunchDarkly API.
- **`external`:** another system owns flags and guarded releases. The factory never plans,
  inserts, audits, or provisions flags (no LD client, even with `--ld live`). Requires
  `flags.external_reason`. Cannot combine with `issue_flag_keys` / `default_key`.
- **`flags.enabled: false`:** no flags at all (different from external: nothing expects
  product flags; external means flags may exist but are not factory-owned).

## Org worker and watcher (Temporal on localhost:7236)

From this factory repo:

```bash
.venv/bin/python -m factory temporal worker \
  --address localhost:7236 \
  --repo ~/Documents/decora-org/decor-agent \
  --home "$PWD/.factory/decora-org" \
  --task-queue decora-factory-org \
  --policy policy.decora-org.yaml \
  --base-branch main \
  --base-ref 26abb8f9f00bfde367d0878b702b94d615a106c3 \
  --push
```

```bash
.venv/bin/python -m factory temporal watch-issues \
  --address localhost:7236 \
  --slug launchdarkly-demos/decor-agent \
  --task-queue decora-factory-org \
  --home "$PWD/.factory/github-org" \
  --policy policy.decora-org.yaml
```

`--policy` on the watcher enables `issue_triggers` (allowlisted authors can open a job
without a label; labelers must be allowlisted **and** have write access). Without
`--policy`, intake stays label + collaborator-write only.

## Auth

The `gh` token must be SSO-authorized for `launchdarkly-demos`:

- https://github.com/orgs/launchdarkly-demos/sso
- or `gh auth refresh -h github.com -s repo,read:org` then authorize the org

Git push uses the org checkout's `origin` remote.
