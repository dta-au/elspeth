# Composer cache handover — 8 November 2026

## Resume here

The user requested a quota-conscious stop: let the already-running acceptance
gates finish and retain outcomes, but start no additional investigations,
optimisation experiments, profiling repeats or broad reruns. Do not merge or
deploy. This documentation checkpoint preserves work for a later session.

Implemented application checkpoint: `ad773c656ff6009bf13e7916e1f249076e8da9b2`,
on remote branch `codex/composer-cache-final`. This handover commit adds only
notes and an inert draft patch; it does not integrate that draft. The running
worktree remains clean and pinned to the application checkpoint. The separate
handover checkout prevents documentation changes invalidating its frozen gate.

## Essential work versus optional coding

1. **Essential acceptance:** collect terminal default/backend and PostgreSQL
   results, including both global authority proofs. If failed, record exact
   failures and the smallest focused next step; do not launch more work in this
   quota-limited session. No new production defect is established by the three
   earlier caller timeouts: they passed unchanged in isolation.
2. **Cold catalog bootstrap:** source draft is complete enough for qualification,
   but untested and not integrated. This is the one substantial remaining code
   draft. It needs focused backend/frontend tests, type/lint/build checks,
   applicable inventories and real browser qualification, then review/integration.
   Expected one snapshot instead of four is a source-derived expectation, not
   a measured result. No reliable coding-hour estimate is established.
3. **Request/candidate reuse:** design only, not a required rewrite to preserve
   the implemented cache. A small repeated UTF-8 encoding can potentially be
   removed locally; broader reuse requires ownership/domain proof. Do not remove
   the independent candidate/executor construction comparison or SDK isolation.
4. Other open performance items below are follow-up measurements or explicit
   design blockers, not secretly completed fixes. Socket continuation remains
   lower priority and has no implementation here.

## Durable draft and design transfer

The [cold-catalog patch](composer-resume-drafts/catalog-bootstrap.patch) contains
all 15 modified/new files from local branch `codex/composer-catalog-bootstrap`,
based exactly on `ad773c656`. Reconstruction in a private Git index matched
all draft file bytes; see [transfer receipt](composer-resume-drafts/catalog-bootstrap-transfer.json).
It adds a single authenticated bootstrap response and atomic frontend loading,
while retaining fresh warm policy checks and existing endpoints. Static review
corrections are incorporated; **no application tests/imports/builds ran**.
See [draft notes](composer-resume-drafts/catalog-bootstrap-status.md).

To resume, create a separate worktree at the application checkpoint, run
`git apply --check` on the saved patch, then apply it there. Do not apply it to
an active frozen test checkout. The original isolated worktree remains at
`.claude/worktrees/composer-catalog-bootstrap`; the patch means it need not be
available in the next container. This patch is not an approved production change.

Designs are preserved here:
- [Provider request preparation](composer-resume-drafts/provider-prepared-request-disposition.md).
  Capture, guard and transport have different hash/content domains; retain SDK
  mutable-copy isolation, credential checks and per-attempt receipts.
- [Candidate integrity](composer-resume-drafts/prepared-pipeline-candidate-contract-design.md).
  Reusing the first ToolResult makes independent comparison circular. Sharing
  pure immutable parsed inputs while retaining both builds is a possible later
  direction; no implementation or benefit is established.

## Exact-checkpoint measurements

Main: `3b28d537be98234da8135c418bf185a998c18b68`.
Feature: `ad773c656ff6009bf13e7916e1f249076e8da9b2`.
Six functional qualifiers and three alternating 20-turn pairs completed with
strict workload, source, runtime and durable-review parity: **120 healthy turns**.
Small graph/discovery workload; fresh process/SQLite DB per run; Python 3.12.14;
controlled SDK responses with zero provider delay and exactly three calls/turn;
10 composition rounds, 24 discovery rounds, 240-second budget, rate 20/minute.
No paid calls. Model-cost-map fetches fell back to the local map at startup.
This compares the full changeset, not an isolated causal cache effect.

Aggregate 20-turn CPU seconds, paired:
main `[64.686509, 64.368762, 62.571142]`;
feature `[49.485913, 49.165359, 49.595596]`.
Reductions: 23.50%, 23.62%, 20.74%; summed reduction **22.64%**.
Turn-20 medians: CPU 4.177 to 3.584 seconds; SQL statements 491 to 485;
RSS 662.168 to 675.945 MiB. **No memory improvement or live-provider latency
claim.** Feature turn-1/20 CPU is 1.830/3.584 seconds, SQL 474/485, RSS
443.867/675.945 MiB. See [all 20 turns](composer-resume-drafts/exact-checkpoint-profile.md).

A separate qualified synthetic SQLite reader pilot passed 24 controls. With
ten 4096-byte noise rows and two committed proposals, fullscan/unbatched was
8 SELECTs and 91,466 returned JSON-text bytes; exact projection/batching was
7 SELECTs and 6,046 bytes. These are not wire bytes or timing measurements.
The larger factorized matrix is unrun. Earlier profiling receipts remain valid
for their recorded snapshots; do not relabel them as this exact checkpoint.

## Gate status at handover publication

Canonical run in `.claude/worktrees/composer-cache-final/work/pushed-checkpoint-gate/20261108T230134Z-composer-cache-final-2154979`:
- Ruff exit 0; mypy exit 0; contracts exit 0; source freeze healthy.
- Keyless trust-tier scan exit 1, nonfatal baseline corpus: 5,459 findings.
  Exact delta vs the preceding 5,460 corpus removes only the stale ADI source-hash
  finding. Vs the earlier 5,458 corpus, only the reviewed authored endpoint type
  check is added. No signed allowlist clearance is claimed.
- Default/backend pytest: **RUNNING**, two workers; no terminal count yet.
- PostgreSQL testcontainer: **QUEUED**, serial; no terminal result yet.
- Both actual global authority proofs remain pending until their explicit results.

The gate records stage exits, JUnit XML, freeze state and `.done` in that run
folder. Astra is only supervising to terminal outcomes, then stopping. Preserve
those receipts; they are local, not bundled into this documentation push.
The original completed broad run remains failed evidence: default 71,191 passed,
53 failed, 267 skipped, 1 xfailed; PostgreSQL 1,179 passed, 3 failed, 1 skipped.
Focused reconciliation has 51 of those default failures passing and two global
proofs pending. Subsequent scoped results include 69 probe/ADI controls, six PG
ack controls, 12 recovery cases and eight hash/provenance cases passing.
Caller regression was 850 passed/3 failed; all three passed unchanged serially.
None of this retroactively turns a failed or unfinished broad run green.

## Remaining bugs and main-profile findings

Implemented/focused-qualified: session/discovery cache and lifecycle; catalog
serialization/ownership controls; exact dispatch projection and history batching;
shared review preparation/record reuse/early term filtering; policy-only probes;
state/authority/digest reuse; readiness snapshot reuse; same-attempt schema and
Advisor section reuse; bounded manifest AST lookup; authoritative input-digest
reuse; combined recovery-envelope parsing; ADI endpoint defaults; fork-to-sink
edges; document/ADI inspector display and heading; catalog notice offset; Advisor
timeout wording. Hidden review-line-cap and request-rejected taxonomy were
already fixed in current source. Broad acceptance is still pending.

Still open, with explicit next steps:
- Cold bootstrap: qualify the transferred draft before integration.
- Independent candidate build and full provider request reuse: design constraints
  above; do not remove integrity or isolation checks to claim completion.
- Private-audit JSON scans: two index proposals narrowed valid storage inputs and
  were rejected. Cohort reads are already batched. A safe no-schema alternative
  needs measured lookup/transaction evidence and storage-domain controls.
- Cross-admission manifest and application-fingerprint reuse: no independent,
  complete deployment/reload invalidation identity established; retain fresh reads.
- Output/aggregation digest reuse: mutable output ownership remains unproven;
  only authoritative input-digest reuse is implemented.
- Broader factorized proposal/history/bootstrap/validation recount and final
  allocation attribution remain unmeasured. No additional experiments now.
- Historical five-second runtime-preflight timeout: slow stage unknown. Ignored
  diagnostic observer has seven passing synthetic controls; real profiler adapter
  remains unqualified (including capture/import/binding ordering). No cause claim.
- Staging gateway namespace: operational repair was reported, but actual
  deployment replacement/connectivity behavior was not verified here.
- Prompt Shields: original full guard input was not retained; no proven false
  positive or bypass fix. Safe provenance remains future work.
- Shared whole-turn deadline can still expire on upstream latency. No final-review
  headroom reservation or socket-continuation implementation is included.
- Complex earlier “Bug Rep” attachment remains unavailable; request its full text.
- Original source-session export's untracked contents remain unverified.

See [the detailed inventory](composer-follow-up-investigations.md) for every
main-profile finding and its historical qualifications; this handover supersedes
stale pending wording there. Raw databases, private captures, credentials, browser
artifacts, local diagnostic tools and the exact common comparison adapter/raw
receipts are **not transferred by this push**. General profiling scripts and
these safe summaries are tracked. No deployment, merge or guard-policy change.
