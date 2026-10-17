---
name: Decora factory request
about: Propose a Decora improvement, bug fix, or maintenance task
title: ''
---

## Goal
<!-- What problem should Decora solve? Who benefits? Describe the desired result.
For a bug, include reproduction steps and actual versus expected behavior. -->

## Change type
<!-- Choose: user_facing_behavior, ui_cosmetic, backend_internal, data_migration,
refactor, docs_only, tests_only, config_ci, dependency, or unsure.
Maintainers may override with factory:class/<name>. -->

## Acceptance criteria
<!-- Add concrete, observable outcomes, one per line. Examples:
- [ ] The room title can be changed and remains changed after reload.
- [ ] An empty title shows a useful validation message.
- [ ] Existing room boards and saved selections are preserved.
Replace these examples with criteria for this request. -->
- [ ]

## Scope and constraints
<!-- What is included? What is out of scope? Add compatibility or data constraints.
The defaults below protect Decora's product behavior; identify any requested exception
for human review rather than asking the agent to bypass it. -->
- Preserve existing room, board, and project behavior outside this request.
- Do not invent catalog products, SKUs, prices, or availability.
- Preserve human approval before purchase or other spending commitments.

## Verification
<!-- How should we prove this works? Include test commands, bug reproduction steps,
API examples, or browser checks. For UI work, describe expected visible behavior
and include screenshots/mockups if helpful. Write "unsure" if you need help. -->

## Rollout
<!-- Behavioral features and migrations require a default-off flag and guarded rollout.
Cosmetic changes may use a flag. Maintenance classes are exempt. A human starts rollout.
Optionally name an EXISTING flag below to reuse it; leave blank to let planning choose. -->
Flag key:
<!-- Optional allowed LaunchDarkly metric for guarded rollout. -->
Guarded rollout metric:


## Context
<!-- Relevant paths, related issues/PRs, API routes, logs without secrets,
or design references. These are hints, not required implementation instructions. -->

<!-- Maintainer workflow:
1. Refine the goal, acceptance criteria, and scope before adding factory:ready.
2. That label authorizes agent calls and draft PR publication on the configured worker.
3. The factory plans, builds in isolation, verifies, and reviews supported work.
4. Follow the status comment; answer clarification through its local CLI command.
5. Review and test the draft PR before merging.

One issue starts one job. Edits/relabeling do not rerun it. Do not include secrets.
-->
