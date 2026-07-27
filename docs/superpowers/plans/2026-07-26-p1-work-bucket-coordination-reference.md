# P1 Work-Bucket Coordination Reference

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:dispatching-parallel-agents` at the coordinator level. Use
> `superpowers:using-git-worktrees` for isolated implementation branches and
> `superpowers:subagent-driven-development` or `superpowers:executing-plans`
> within a bucket. Workers must also use the project-local Filigree and
> Loomweave workflows, plus test-driven development, systematic debugging, and
> verification-before-completion when those skills apply.

**Goal:** Resolve the live open P1 queue through independently owned work
buckets without duplicate claims, shared-checkout edits, hidden scope debt, or
unsafe integration into `release/0.7.2`.

**Architecture:** A coordinator owns live-state reconciliation, dispatch,
review, and serial integration. Each implementation bucket gets one owner and
one isolated worktree; tickets move one at a time through reproduce, test,
fix, verify, commit, comment, and close. Buckets may run concurrently only when
their active path sets and runtime authorities do not overlap.

**Tech Stack:** Filigree, Loomweave, Warpline, Git worktrees, Python 3.12/3.13,
FastAPI, SQLAlchemy, PostgreSQL/SQLite, pytest, Ruff, mypy, Docker, and the
ELSPETH web/Composer test suites.

---

## Purpose and authority

Use this mutable file as the coordination reference for a broad delegated goal.
It is not a tracker snapshot, approval receipt, release authorization, or
sealed plan. Update or delete it normally as the queue changes.

Authority order:

1. The user's current goal and explicit scope boundaries.
2. Live Filigree issue, dependency, claim, and workflow state.
3. Current repository and active-worktree state.
4. Current Loomweave ownership/navigation results and Warpline impact advice.
5. This file's bucket assignment and recorded snapshot.

Never reopen, reclaim, or reimplement a ticket solely because it appears here.
If live state disagrees with this file, follow live state and update the
coordination record.

## Recorded baseline

Recorded on 2026-07-26 from:

- branch: `release/0.7.2`
- commit: `2a3452e3452581008b5fb61b1e75ad0b8f03fb2f`
- open P1 tickets: 59
- active P1 tickets: 5
- unclaimed P1 tickets in `triage`: 54
- unclaimed implementation buckets: 13
- operator P0 blocker: `elspeth-18fe6e759e`

The AWS acceptance-controller refactor `elspeth-001692d205` and PR-integration
task `elspeth-a1e8c9d198` are closed. Do not treat either as active. The current
AWS implementation owners live under
`src/elspeth/web/_aws_ecs_acceptance/`; the facade remains at
`src/elspeth/web/aws_ecs_acceptance.py`.

## Mandatory coordinator refresh

Run this before the first dispatch, after every integration, and whenever a
claim conflict or surprising test result suggests drift:

```bash
cd /home/john/elspeth
git status --short --branch
git worktree list --porcelain
filigree session-context
filigree critical-path
filigree list --priority=1 --limit=10000 --json \
  | jq '[.items[] | select(.status_category != "done")] \
        | {count:length, tickets:map({issue_id,status,assignee,title,blocked_by})}'
```

Expected at the recorded baseline: 59 non-terminal P1 tickets. A different
count is normal drift, not a reason to force the old number.

Before answering ownership or impact questions, call Loomweave
`project_status_get`. If it reports `stale` or `stale_worktree`, complete the
`analyze_start` / `analyze_status_get` refresh loop before navigating. Use
`entity_find` or `entity_at` before reading source broadly. Use Warpline's
changed-entity, impact-radius, and reverify surfaces on the bucket diff before
closeout; advisory absence is not proof of safety.

## Delegation and isolation contract

The coordinator must:

- retain the coordinator role; do not implement bucket code in the release
  checkout while workers are active;
- dispatch at most six implementation buckets concurrently, leaving capacity
  for coordination and independent review;
- give each worker the complete bucket text, current ticket record, current
  base SHA, allowed path family, collision constraints, and expected handoff;
- never give a worker only “read this plan”; fresh-context agents need the
  relevant text in their prompt;
- allocate one bucket owner at a time; do not place multiple workers in the
  same bucket or worktree;
- review and integrate completed buckets serially against the then-current
  `release/0.7.2`; and
- refresh live tracker/worktree state before starting a replacement worker.

The repository already has an ignored `.worktrees/` directory. Before creating
any project-local worktree, verify it remains ignored:

```bash
cd /home/john/elspeth
git check-ignore -q .worktrees
```

Create bucket worktrees from the current release tip with unique names:

```bash
# Concrete example for Bucket 2; choose the matching documented bucket slug.
BUCKET_SLUG=auth-browser
BASE_SHA=$(git rev-parse release/0.7.2^{commit})
git worktree add ".worktrees/p1-${BUCKET_SLUG}" \
  -b "codex/p1-${BUCKET_SLUG}" \
  "$BASE_SHA"
```

Bootstrap every new worktree before running tests or starting a worker:

```bash
WORKTREE=/home/john/elspeth/.worktrees/p1-${BUCKET_SLUG}
cd "$WORKTREE"
env -u VIRTUAL_ENV uv sync --frozen --all-extras
test "$(env -u VIRTUAL_ENV uv run --frozen python -c \
  'import sys; print(sys.prefix)')" = "$WORKTREE/.venv"
env -u VIRTUAL_ENV uv run --frozen python -c \
  'import importlib.metadata as m; assert m.version("rfc8785") == "0.1.4"'
test "$(env -u VIRTUAL_ENV uv run --frozen which pytest)" = \
  "$WORKTREE/.venv/bin/pytest"
test "$(env -u VIRTUAL_ENV uv run --frozen which ruff)" = \
  "$WORKTREE/.venv/bin/ruff"
test "$(env -u VIRTUAL_ENV uv run --frozen which mypy)" = \
  "$WORKTREE/.venv/bin/mypy"
```

Do not run bare `uv run pytest` as the first command in a fresh worktree.
Until the frozen local environment exists, executable resolution can escape to
a user-global pytest process that cannot see mandatory locked dependencies.
`rfc8785` is a base dependency; its absence is a bootstrap failure, never an
optional-dependency explanation. Use `env -u VIRTUAL_ENV uv run --frozen
python -m pytest` after the checks above.

Do not reuse `.worktrees/deferred-platform-completion`; it belongs to active
ticket `elspeth-b5d7aa5655` on branch
`codex/deferred-platform-completion`.

Workers must not merge into, rebase, tag, push, publish, sign, or release from
their bucket worktrees unless the enclosing user goal explicitly grants that
authority. Return a reviewed local branch/commit to the coordinator.

## Ticket lifecycle inside a bucket

A bucket is an ownership and collision boundary, not permission to batch-close
all its tickets. Work one ticket at a time unless two tickets are proven to be
the same inseparable defect surface.

For each ticket:

1. Refresh the issue and comments; confirm it remains non-terminal, unclaimed,
   and unblocked.
2. Resolve current owner entities with Loomweave. Ticket line anchors describe
   the validation baseline and may have moved.
3. Atomically claim and advance the ticket. For example, the Bucket 2 worker's
   first live ticket uses:

   ```bash
   ISSUE_ID=elspeth-f2f0623573
   AGENT_NAME=codex-p1-auth-browser
   filigree start-work "$ISSUE_ID" \
     --assignee "$AGENT_NAME" \
     --actor "$AGENT_NAME" \
     --advance \
     --json
   ```

4. On `CONFLICT`, stop and tell the coordinator. Do not take over the work.
5. Reproduce the defect on current HEAD. Treat the tracker finding as evidence,
   not as permission to weaken a fail-closed rule.
6. Add the narrow failing regression, observe the expected failure, implement
   the smallest complete fix, and run focused verification.
7. Run the bucket regression set plus relevant Ruff/mypy gates. Use Warpline to
   assemble the final reverify worklist.
8. Commit only the ticket's scoped changes. Do not mix opportunistic cleanup,
   unrelated documentation, or another ticket's fix.
9. Add a Filigree comment with root cause, changed files, commit SHA, and exact
   verification.
10. Close the issue with a concrete reason only after the commit exists.
11. Refresh the bucket's remaining tickets before claiming the next one.

If work uncovers a defect required for the current ticket's correctness, fix it
or create a proper dependent issue. Do not hide it in a 14-day observation and
close known-broken work. Use observations only for genuinely incidental facts
outside the bucket's scope.

## Active claims and reserved surfaces

These tickets already have owners. Do not dispatch replacements merely because
a claim is stale; inspect live work and coordinate with the owner first.

### A1. DAG production-path corpus

- `elspeth-ef29ef6ba4` — `[DAG completeness] Build the maintained 15-scenario production-path corpus`
- recorded state: `in_progress`
- owner: `codex-dag-corpus`
- reserved surfaces: `docs/architecture/dag/`, DAG scenario-corpus tests, and
  their CI wiring
- collision: Bucket 9 may share graph fixtures or acceptance tests; coordinate
  test-file ownership before Bucket 9 edits them

### A2. Session path ownership

- `elspeth-056c26a251` — `Enforce session ownership for writable and readable local paths`
- recorded state: `verifying`
- owner: `codex-codeql-path`
- reserved surface: `src/elspeth/web/composer/tools/_common.py` and focused path-policy tests
- collision: Bucket 8 must wait until this ticket closes or hands off a stable
  commit

### A3. Release 0.7.2 preparation

- `elspeth-64c319bf4d` — `Prepare release/0.7.2 version surfaces and release gates`
- recorded state: `in_progress`
- owner: `codex-release-072`
- blocker: operator P0 `elspeth-18fe6e759e`
- reserved surfaces: version declarations, changelog/release notes, release
  gates, fingerprints, and release documentation
- boundary: bucket workers must not bump versions, regenerate trust baselines,
  tag, push, publish, sign, or bypass the operator judgment gate

### A4. Multi-replica runtime and deferred platforms

- `elspeth-b5d7aa5655` — `Make web runtime multi-replica safe before Azure Container Apps support`
- recorded state: `building`
- owner: `codex-deferred-platform`
- worktree: `.worktrees/deferred-platform-completion`
- branch: `codex/deferred-platform-completion`
- controlling plan:
  `docs/superpowers/plans/2026-07-26-finish-deferred-deployment-platforms.md`
- reserved surfaces include web coordination, sessions models/protocol/service,
  run ownership/leases, durable cancellation, progress/tickets/rate limiting,
  blob coordination, application lifecycle, deployment manifests, and
  multi-process acceptance tests
- collisions: Buckets 3, 7, 11, and 13 must wait for this program's integration
  boundary and then re-resolve their remaining defects on the merged tree

### A5. Docker runtime contract

- `elspeth-8d2bea608f` — `Docker image is unreachable and fails the documented web startup contract`
- recorded state: `fixing`
- owner: `codex-docker-runtime`
- blocker: operator P0 `elspeth-18fe6e759e`
- reserved surfaces: Docker image/start command, container filesystem/UID
  contract, Docker guide, web startup configuration, readiness, and relevant
  doctor checks
- collision: Bucket 13 must wait for this ticket's stable handoff

## Resume plan for the five recorded claims

This section supersedes the recorded-claim assumptions above whenever it is
newer than them. It was reconciled on 2026-07-26 against live Filigree state,
the local worktree list, current GitHub checks for PR #98, and the active Codex
task list.

### Resume classification

| Recorded claim | Classification | Coordinator action |
|---|---|---|
| `elspeth-ef29ef6ba4` | stale, materially unfinished program | release the old worker claim after a final handoff audit; resume through bounded child waves from a fresh release tip |
| `elspeth-056c26a251` | implementation complete; closeout proof now available | do not start a worker; verify the current CodeQL result and close |
| `elspeth-64c319bf4d` | implementation checkpoint complete; release closeout only | do not duplicate the release-prep work; wait for final integration and the separately owned P0 |
| `elspeth-b5d7aa5655` | genuinely in flight | continue the existing Codex task and worktree; never dispatch a replacement |
| `elspeth-8d2bea608f` | implementation and RC proof complete; final-candidate recheck only | do not reimplement; re-smoke the final image after platform integration and the release gate |

Filigree now also shows `elspeth-3d1d1fcb6c` as a sixth P1 work-in-progress
record. It is not a sixth independent lane: it is a child of
`elspeth-b5d7aa5655`, held by the same `codex-deferred-platform` owner, and is
implemented by Tasks 4 through 6 of the deferred-platform plan. Bucket 3 must
not claim or implement it in parallel.

The operator P0 `elspeth-18fe6e759e` is assumed to be separately in flight.
Do not reclaim, release, or execute it from this goal. Coordinate its final
authority step with the release freeze: key-free diagnosis or staging may run
earlier, but authoritative signing and fingerprint-baseline regeneration must
be revalidated against the final integrated release SHA. The platform branch
is still changing trust-relevant source and schema surfaces, so a P0 result
bound only to the pre-platform `release/0.7.2` tip is not final release proof.

### R0. Immediate reconciliation; no implementation worker

The coordinator performs these checks before dispatching a replacement for
any recorded claim:

1. Refresh all five issues and comments, the P0, the current worktree list,
   the active Codex task list, and PR #98 checks.
2. Treat a live task plus advancing branch as stronger liveness evidence than
   an old heartbeat timestamp.
3. Use compare-and-swap claim operations. Never clear an assignee or change a
   workflow state without checking the expected current holder.
4. If the P0 owner is already preparing a signing pass, tell that owner the
   final authoritative rerun must use the post-platform frozen SHA. Do not
   interrupt useful diagnosis already in progress.

### R1. Close `elspeth-056c26a251`; do not resume implementation

The CodeQL remediation commit
`964cc8c423c3ca3bb8174e4599381668810584d5` is contained in current
`release/0.7.2`. GitHub's latest instance for alert 1241 reports `fixed`, and
the PR's CodeQL/Analyze Python checks are green. The broader PR static-analysis
failure is the separate operator-owned trust-tier gate, not a recurrence of
this path-injection alert.

Closeout sequence:

1. Refresh alert 1241 and confirm its most recent PR #98 instance remains
   `fixed` on a commit containing `964cc8c423`.
2. Re-run only the focused path-helper and AST dataflow regression if current
   HEAD has changed the relevant route since the hosted analysis.
3. Add the hosted result and exact commit to the issue comment.
4. Close the bug from `verifying`; do not create a new worktree or patch.
5. Release Bucket 8 from the path-ownership wait after the close is recorded.

### R2. Continue `elspeth-b5d7aa5655` and fold in `elspeth-3d1d1fcb6c`

The active Codex task titled `Review deferred platform plan` owns this lane.
Its clean worktree is `.worktrees/deferred-platform-completion`, branch
`codex/deferred-platform-completion`, currently at
`48bc73a4ab537649307ece8de5f56a742579e8b7`. Tasks 1, 2, 2A, and 2B are
complete; Task 3 has its focused repair committed and is undergoing independent
re-review. The next implementation boundary after Task 3 GO is Task 4, the
persistent session-operation fence.

Continuation rules:

1. Do not start a new worker, branch, or worktree for either issue.
2. Let the active task finish Task 3 review, then continue the controlling plan
   at Task 4. Do not skip directly to provider artifacts.
3. Keep `elspeth-3d1d1fcb6c` inside Tasks 4 through 6: persistent operation
   authority, exhaustive mutation fencing, and run/Landscape ownership. Close
   that child only after its two-worker PostgreSQL barriers and mixed-version
   read/update/delete/run-admission proofs pass.
4. Treat the integration point after the multi-replica runtime and its full
   two-process matrix as the stable boundary for Buckets 3, 7, and 11.
5. Merge this branch serially. Refresh Filigree, Loomweave, Warpline, PR #98,
   and every deferred bucket against the new release tip before dispatching
   overlapping work.

### R3. Resume `elspeth-ef29ef6ba4` as bounded corpus waves

The bounded pre-platform recovery wave is complete at
`release/0.7.2@55727d54c6b057b77a926778deeb933208d78543`. P3
`elspeth-7dcc6554e7` and all seven B1-X/B2-X recovery children are closed. The
current schema-v2 manifest has 15 scenarios, 46 cases, 107 evidence
references, and 165 cells; 71 applicable cells remain non-pass. The augmented
recovery gate reports 593 passed, zero xfails, and three known quorum warnings.
The parent `elspeth-ef29ef6ba4` remains open.

The four B1 recovery cells and partial-terminal B2 recovery now pass. S6
fork/coalesce and S7 sequential-coalesce remain partial because the current
evidence reaches terminal publication after completed coalesces; it does not
prove a held coalesce branch or a literal between-merge seam. Do not promote
those cells from adjacency or reinterpret the completed packet as evidence for
the unavailable seam.

Resume sequence:

1. Capture the exact reviewed release tip and require
   `55727d54c6b057b77a926778deeb933208d78543` to be its ancestor.
2. Rebase the clean paused deferred-platform replay range onto that captured
   tip. Treat every pre-platform recovery claim as provisional and rerun the
   complete registered recovery gate after the rebase.
3. Use the new distributed runtime for B4-B and scenario-specific concurrency
   evidence. Do not build a parallel corpus-only lease authority.
4. Resolve the scale dependency cycle around `elspeth-cb1053fe46` before scale
   execution without weakening its acceptance contract or closing the parent.
5. Keep row-union, guided authoring, round-trip, and remaining product cells
   bound to their live capability owners; prove fail-closed rejection where a
   capability remains unsupported.
6. Add a genuine public held-branch or between-merge seam before promoting S6
   or S7 recovery, unless the requirement is deliberately revised and the
   manifest contract is updated with review.
7. Close the parent only when every applicable cell is executable and passing,
   or when a remaining cell is explicitly owned by an open capability ticket
   whose expected corpus behavior is a tested fail-closed rejection.

The corpus lane may run in parallel with the platform lane only while it avoids
multi-worker/session-coordination tests and does not edit the platform plan's
reserved deployment/runtime paths.

### R4. Park `elspeth-64c319bf4d` as release closeout

Release-preparation work through commit `736e9322251d4bbbed3ddf67a905270b2d7d7f6c`
is contained in current `release/0.7.2`, with package/version surfaces, broad
local verification, example coverage, and build artifacts already recorded.
There is no useful implementation resume before the release candidate stops
moving.

Closeout sequence after the platform branch and other intended release changes
are integrated:

1. Freeze one candidate SHA and reconcile version, changelog, schema-epoch,
   deployment-profile, documentation, site, lockfile, and build surfaces
   against the final diff.
2. Coordinate the separately owned P0's final revalidation, signing, and
   supported baseline regeneration on that exact SHA.
3. Re-run the local release gate, hosted Python 3.12/3.13, PostgreSQL/
   testcontainer contention, packaging/install, and required PR checks.
4. Record exact artifact digests and provider/container acceptance. Do not
   tag, push, publish, or merge merely to make the ticket green; those actions
   require the enclosing goal's release authority.
5. Close `elspeth-64c319bf4d` only when the release-prep acceptance is true on
   the frozen SHA. The final merge task remains separately owned by the
   deferred-platform/release closeout plan.

If the old release-prep holder is confirmed idle before this boundary, release
its claim without dispatching a replacement. Use `work_release` with
`revert_status=false` so the evidence-bearing closeout stage is preserved while
the explicit blocker remains. Reclaim atomically only when the candidate is
ready for the closeout sequence.

### R5. Reverify and close `elspeth-8d2bea608f`; do not reimplement

The runtime-contract fix `cd72367eb` is contained in current
`release/0.7.2`. Public RC2 image evidence, anonymous pulls, UID/GID 1654,
writable blob/output directories, web startup, and readiness were recorded;
the obsolete RC1 image was subsequently deleted. The platform branch now also
changes Docker frontend inputs, so the old RC2 is evidence for the fix but is
not the final candidate artifact.

Closeout sequence:

1. Wait for the platform Dockerfile changes to integrate and for the P0 gate
   to clear on the frozen release SHA.
2. Build the final all-extras and AWS/PostgreSQL variants from that SHA through
   the normal gated workflow.
3. Re-run anonymous-pull and runtime smoke checks: version, UID/GID 1654,
   writable `/app/data/blobs` and `/app/data/outputs`, explicit external bind,
   secret/config injection, SPA response, and `/api/ready == 200`.
4. Store that exact verification in `fix_verification`, transition
   `fixing -> verifying`, independently inspect the final image contract, then
   close.
5. Only reopen implementation if the final-candidate smoke test reproduces a
   real contract failure. Do not rewrite the landed runtime fix merely because
   its old claim still appears active.

If the Docker holder is confirmed idle while these gates are pending, release
the claim with `revert_status=false` and preserve the existing workflow stage.
Reclaim only for the final candidate recheck.

### Resume order at a glance

1. Close `elspeth-056c26a251` from hosted CodeQL proof.
2. Continue the already-running `elspeth-b5d7aa5655` task; keep
   `elspeth-3d1d1fcb6c` inside it.
3. In parallel, restart the DAG corpus as bounded waves, excluding the
   multi-worker case until the platform integration boundary.
4. Keep the P0 with its current external owner; coordinate its authoritative
   final pass with the frozen post-platform SHA.
5. Reclaim `elspeth-64c319bf4d` and `elspeth-8d2bea608f` only for final
   closeout/reverification after integration and the P0 gate.

## Bucket sequencing

### Cohort 1: dispatch now, maximum six workers

Choose up to six from Buckets 1, 2, 4, 6, 10, and 12. These have the cleanest
separation from current active work.

### Cohort 2: dispatch after Cohort 1 review capacity returns

- Bucket 5 — separate its merge from Bucket 4 if they share guided fixtures.
- Bucket 9 — confirm the active DAG corpus worker does not own the same tests.
- Bucket 8 — start only after active ticket `elspeth-056c26a251` lands or hands
  off a stable commit.

### Cohort 3: wait for active cross-cutting work

- Buckets 3, 7, and 11 wait for `elspeth-b5d7aa5655` to reach a stable
  integration boundary.
- Bucket 13 waits for both `elspeth-b5d7aa5655` and
  `elspeth-8d2bea608f` to reach stable integration boundaries.

After either active program lands, do not blindly execute the old ticket
description. Reproduce each remaining ticket on the merged tree; close it as
already fixed only with focused current-HEAD proof.

## Bucket 1: Wardline trust-boundary gate

**Tickets:** 1

- `elspeth-cec5c47cef` — `Wardline gate is green but inert because it recognizes zero ELSPETH trust boundaries`

**Primary surfaces:** Wardline configuration/vocabulary, trust-boundary
metadata integration, gate scripts/workflows, and a representative taint
fixture.

**Coordination:** Prefer a vocabulary/configuration integration over mass edits
to unrelated production modules. The gate must fail when resolution is inert
and must prove at least one expected crossing.

## Bucket 2: Authentication and browser perimeter

**Tickets:** 5

- `elspeth-f2f0623573` — `Passwords longer than bcrypt's 72-byte limit can collide`
- `elspeth-cbedf4007b` — `Unknown OIDC signing key does not trigger controlled JWKS refresh`
- `elspeth-6cdff5ec9b` — `Token-bearing 401 branches omit auth-failure audit events`
- `elspeth-96d7250350` — `Username trained-operator collides with trusted principal marker`
- `elspeth-9ba245797b` — `SPA responses are frameable`

**Primary surfaces:** `src/elspeth/web/auth/`,
`src/elspeth/web/catalog/policy_view.py`, `src/elspeth/web/app.py`, and focused
auth/security-header tests.

**Coordination:** Preserve neutral treatment of ordinary principals and the
existing auth-audit redaction contract. Do not combine browser framing,
password normalization, OIDC refresh, and audit behavior into one large patch;
keep one issue/commit cycle.

## Bucket 3: Blob lifecycle, quotas, and custody

**Tickets:** 4

- `elspeth-2ed591b6c9` — `Output recovery can terminalize a blob while retaining unaccounted files`
- `elspeth-bc6fd76505` — `Pending outputs grow at zero quota and are fully buffered before backpressure`
- `elspeth-b3feba9a7c` — `Protect every composition, review, and pending-proposal blob reference from mutation or deletion`
- `elspeth-3d1d1fcb6c` — `Serialize blob update/delete, reads, and run admission in one lock/version domain`

**Primary surfaces:** `src/elspeth/web/blobs/service.py`,
`src/elspeth/web/composer/tools/blobs.py`, blob/session repositories, and blob
quota/custody/concurrency tests.

**Coordination:** Wait for the multi-replica program. Its durable ownership and
cross-process blob work can change the correct locking and quota boundary.
Preserve fail-closed custody and byte accounting.

## Bucket 4: Guided source forms and model-visible source data

**Tickets:** 4

- `elspeth-932ce704ac` — `Guided schema forms persist literal credential fields before the shared mutation guard`
- `elspeth-23a67818a9` — `Guided source selection can inspect the newest session blob instead of the selected target`
- `elspeth-836563aab4` — `Redact duplicate CSV header values in model-visible warnings`
- `elspeth-9a9e93ab5a` — `Uploaded schema and sample keys are injected into a system-role prompt`

**Primary surfaces:** `src/elspeth/web/composer/guided/stage_transitions.py`,
`source_inspection.py`, `guided/prompts.py`, and guided source/form tests.

**Coordination:** Treat uploaded headers, sample keys, and credential-bearing
fields as untrusted data. Preserve the shared mutation guard rather than adding
a weaker guided-only exception.

## Bucket 5: Guided chat transaction and turn integrity

**Tickets:** 5

- `elspeth-1e3ad83d89` — `Guided-full inline custody violates the originating-message foreign key before atomic staging`
- `elspeth-ea80e34fdc` — `Historical guided Retry can submit stale prose under a newer turn token`
- `elspeth-bbcf92ad2f` — `Step-2 discovery executes more calls than the configured per-turn security cap`
- `elspeth-9728f2d92d` — `Support plural reviewed outputs when guided chat builds sink revision context`
- `elspeth-ef92db3e16` — `Step-2 chat bypasses deployment-aware sink admission`

**Primary surfaces:** guided plan/chat routes,
`src/elspeth/web/composer/guided/protocol.py`, `guided/chat_solver.py`, and
guided transactional/integration tests.

**Coordination:** Bucket 4 and Bucket 5 may both touch shared guided fixtures.
Keep production ownership separate and serialize merges when common test
helpers overlap.

## Bucket 6: Composer model, error, and redaction boundary

**Tickets:** 5

- `elspeth-f3f669527f` — `Lexical build-intent authority can auto-commit negated requests`
- `elspeth-fcff9733e6` — `Make ToolArgumentError structurally leak-safe`
- `elspeth-416900d534` — `Model prose can forge the same marker used for trusted ELSPETH-SYSTEM notices`
- `elspeth-85ddeb201a` — `Require fail-closed response models and scrub externally derived scalars`
- `elspeth-c02119f52c` — `Scrub internal composition, local path, blob ID, and secret-inventory data from shareable-review payloads`

**Primary surfaces:** `src/elspeth/web/composer/no_tool_policy.py`,
`protocol.py`, `redaction.py`, shareable-review services, and leak-safety tests.

**Coordination:** Keep trusted attribution structural; never infer it from a
rendered marker. Make errors leak-safe by construction, not by relying on every
caller to remember redaction.

## Bucket 7: Composer concurrency, state, and audit settlement

**Tickets:** 7

- `elspeth-5269b43bca` — `Keep timed-out runtime-preflight workers admitted until they actually finish`
- `elspeth-8a56dda69e` — `Key unsaved runtime preflight single-flight by state content`
- `elspeth-01d4c6e683` — `Recheck trust mode immediately before granting auto-commit authority`
- `elspeth-90231248dc` — `Settle assistant semantics and buffered LLM/tool audit cohorts atomically and idempotently`
- `elspeth-7536e5d919` — `Preserve composition-state predecessor lineage in compose audit`
- `elspeth-45f72a949c` — `Preserve plugin crash primacy over stale-state audit rejection`
- `elspeth-b6be9e991f` — `Validate provider response shape before recording success or returning composer results`

**Primary surfaces:** `src/elspeth/web/composer/service.py`, sessions service
and message routes, preflight coordination, and compose audit/state tests.

**Coordination:** Wait for the multi-replica program because it changes session
ownership, durable progress, cancellation, and cross-process authority. After
integration, thread verified state/ownership context through the correct helper
boundary rather than loosening concurrency checks.

## Bucket 8: Composer tool admission and mutation integrity

**Tickets:** 6

- `elspeth-05cf791e68` — `Invalidate or reconcile interpretation-review authority whenever the reviewed draft changes`
- `elspeth-f34c4e838e` — `Preflight the per-batch proposal cap before creating any proposals`
- `elspeth-c40d4a0215` — `Reject duplicate provider tool-call IDs before dispatch or side effects`
- `elspeth-2ddfe3f06e` — `Validate resolver-owned interpretation metadata for every plugin and container shape`
- `elspeth-d10846bd6b` — `Validate tool arguments and path policy before guided discovery dispatch`
- `elspeth-1fb9bb787d` — `Staged-guided get_pipeline_state discovery bypasses the initial redacted state projection`

**Primary surfaces:** Composer tool dispatch/common helpers, transforms,
`tool_batch.py`, `pipeline_planner.py`, and tool admission/proposal tests.

**Coordination:** Wait for `elspeth-056c26a251` because both work streams touch
`tools/_common.py` and path-policy contracts. Validate before side effects and
preflight whole batches before committing any member.

## Bucket 9: Topology, routing, and coalesce correctness

**Tickets:** 4

- `elspeth-3f4e63900f` — `Include queue-to-coalesce edges in queue consumer accounting`
- `elspeth-67b44040ee` — `Reconcile visual edges and runtime routes atomically and reject every unsupported route shape`
- `elspeth-5d40dee1ad` — `Traverse coalesce mapping values in fanout guard`
- `elspeth-572c642dbf` — `Reviewed-output rebinding silently corrupts topology for ambiguous or unproven aliases`

**Primary surfaces:** Composer state/transforms/guided planning,
`src/elspeth/web/execution/fanout_guard.py`, and graph/coalesce tests.

**Coordination:** Check the active DAG corpus worker's test ownership before
editing scenario fixtures. Preserve a single canonical topology and reject
unsupported route shapes before runtime.

## Bucket 10: Persistence, canonical recovery, and MCP compare-and-swap

**Tickets:** 3

- `elspeth-0ccb1e5f25` — `Reviewed-source custody performs synchronous DB work on the event loop outside its preparation deadline`
- `elspeth-ffc9a2de59` — `Persisted pipeline audit recovery accepts non-canonical and duplicate-key JSON bytes`
- `elspeth-593876ec38` — `Use compare-and-swap for MCP composition-session saves`

**Primary surfaces:** `src/elspeth/web/composer/pipeline_commit.py`,
`src/elspeth/composer_mcp/session.py`, and persistence/canonical/CAS tests.

**Coordination:** Preserve canonical bytes and optimistic concurrency. Do not
paper over event-loop blocking by extending deadlines.

## Bucket 11: Runtime launch, diagnostics, and accounting

**Tickets:** 3

- `elspeth-a3e0a2b7ea` — `Pin tutorial execution to the state revision readiness approved`
- `elspeth-1d24bb0d96` — `Treat missing expected Landscape storage as audit unavailable`
- `elspeth-d5578ccd98` — `Validate and isolate corrupt token outcomes per run before declaring accounting closed`

**Primary surfaces:** tutorial launch/readiness and
`src/elspeth/web/execution/diagnostics.py` and `accounting.py`, with runtime
evidence/accounting tests.

**Coordination:** Wait for the multi-replica program because run ownership and
takeover semantics can change the correct launch and diagnostic authority.
Missing evidence must remain unavailable, never silently clean.

## Bucket 12: AWS acceptance policy and evidence

**Tickets:** 4

- `elspeth-217facea3a` — `Positive evidence and retained-evidence checkpoint can be orphaned`
- `elspeth-cf5c8f5a5d` — `Task-definition policy ignores every non-target container`
- `elspeth-2c1942fe90` — `Terraform approval authenticates receipt aggregate rather than plan digest`
- `elspeth-cf83a56eb5` — `Successful dynamic receipt retries conflict with their fixed identity`

**Current implementation owners:**

- retained evidence/orphaning:
  `src/elspeth/web/_aws_ecs_acceptance/manifest.py`,
  `manifest_schema.py`, `orphan_sweep.py`, and `receipt_store.py`
- task policy: `src/elspeth/web/_aws_ecs_acceptance/task_definition.py`
- approvals: `src/elspeth/web/_aws_ecs_acceptance/approvals.py`
- receipt identity: `src/elspeth/web/_aws_ecs_acceptance/receipt_store.py` and
  `receipt_contracts.py`
- primary tests: `tests/unit/web/aws_ecs_acceptance/`

**Coordination:** The old monolith line numbers in Filigree are historical
anchors. Fix the private owner while preserving the facade's public re-export
identity and dependency-direction tests. Work one issue/commit cycle even
though the regression suite is shared.

## Bucket 13: Deployment configuration hardening

**Tickets:** 3

- `elspeth-a59a83f081` — `Reject plaintext PostgreSQL transport in AWS ECS doctor and startup`
- `elspeth-67ac64e95e` — `Reject shared-writable data and blob trust roots`
- `elspeth-3e48b1ff4f` — `Reject uniform-byte production JWT secret keys`

**Primary surfaces:** `src/elspeth/web/deployment_contract.py`, `doctor.py`,
`config.py`, startup/readiness contracts, and deployment/config tests.

**Coordination:** Wait for both the multi-replica and Docker active programs.
Preserve the SQLite single-process path while keeping production PostgreSQL,
filesystem ownership, and key-strength checks fail closed.

## Worker handoff contract

Every worker returns:

- bucket and ticket ID;
- final status: `DONE`, `DONE_WITH_CONCERNS`, `NEEDS_CONTEXT`, or `BLOCKED`;
- worktree, branch, base SHA, and commit SHA;
- root cause and why the fix is complete;
- exact files changed;
- failing regression observed before implementation;
- exact focused and bucket-level verification with results;
- Warpline reverify worklist and any explicit unavailable enrichment;
- current Filigree status and the comment/close action taken;
- overlap with any active or completed bucket; and
- residual concerns that affect integration.

The coordinator must run spec-compliance review before code-quality review.
Reviewers must inspect the live diff and tests, not rely on the implementer's
summary. The implementer fixes review findings and the same reviewer rechecks
them before approval.

## Serial integration contract

Integrate one approved bucket branch at a time:

1. Refresh `release/0.7.2`, live Filigree state, and active worktrees.
2. Confirm the bucket branch contains only its declared commits and paths.
3. Reconcile it with the current release tip in an isolated integration
   worktree; do not let the worker merge directly into the release checkout.
4. Run the bucket regressions and the Warpline-derived reverify worklist.
5. Run targeted Ruff formatting/checking and mypy on touched Python surfaces.
6. Integrate only on green evidence.
7. Refresh the P1 inventory and this file's active/remaining sections.

After all buckets and active claims have landed, run the repository's full
release verification required by the live release task. Do not claim the
release is complete while `elspeth-18fe6e759e` remains operator-blocked.

## Stop and escalation conditions

Stop the affected bucket and return to the coordinator when:

- atomic claim returns `CONFLICT`;
- an active owner is editing the same production or test surface;
- the ticket requires changing a public contract beyond its acceptance scope;
- reproduction contradicts the ticket or current code already fixes it;
- a baseline test fails before the ticket change;
- the fix would weaken security, custody, audit, canonicalization, ownership,
  or fail-closed behavior;
- credentials, operator signatures, release publication, or new external
  authority are required; or
- the task expands into the active multi-replica, Docker, release, or DAG
  program without an explicit handoff.

For an operator policy or credential decision, create/escalate the proper P0
blocker, record the dependency, and continue with a different independent
bucket. Do not code around the missing authority.

## Completion criteria

The broad delegated goal is technically complete only when:

- every ticket listed here is terminal or has an explicit live blocker and
  owner action;
- every current open P1 discovered during execution is either incorporated
  into a bucket or explicitly excluded by the user's goal;
- no bucket branches or worktrees contain unintegrated required fixes;
- every integrated fix has ticket-scoped regression evidence and independent
  review;
- cross-bucket integration and full release verification are green;
- live Filigree counts and remaining operator actions are reported exactly;
  and
- this reference is updated or removed so it does not masquerade as current
  after the queue is resolved.
