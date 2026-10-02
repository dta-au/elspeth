# Design panel position — CODE STRUCTURE lens

**Panellist:** python-refactoring-architect
**Tree:** `release/0.8.1` @ `6506f6a7f` (verified against the working tree at read time; no
uncommitted change under `src/elspeth/web/sessions/` or `src/elspeth/web/coordination/`)
**Question:** Should the composer async job live in `session_operation_receipts`, or its own table?

## Position: **C — separate table, own codec** (as already written in `contract.md`), with one
small addendum absorbed into Task 2 rather than treated as a reason to change tables.

**Confidence: high.**

This is not a close call on the CODE STRUCTURE axis specifically. `operation_receipts.py`
(527 lines) is built for a materially different lifecycle than the one the spec requires, and
the mismatch is behavioral, not cosmetic — extending it would force a change to an existing,
safety-relevant invariant (lease takeover), not just an `if kind == ...` branch.

## What I read

- `src/elspeth/web/sessions/operation_receipts.py` (full file, 527 lines) — all seven public
  functions and every private validator.
- `src/elspeth/web/sessions/models.py:727-818` (`session_operation_receipts_table`) and
  `:825-873` (`session_operation_receipt_events_table`), plus the two CHECK-bundle strings at
  `:793-800` and `:867-872`.
- `src/elspeth/web/sessions/protocol.py:264-360` (`OperationReceiptKind` and the outcome union).
- Every caller of `session_operation_receipts_table` / the receipt functions:
  `src/elspeth/web/blobs/service.py` (lines 86, 487-530, 2286-2290),
  `src/elspeth/web/coordination/repository.py` (lines 114, 724-769, 3019-3025, 4062-4143, 5186-5188),
  `src/elspeth/web/sessions/service.py` (lines 165-175, 937-959, 1045-1235, 2188-2191, 5907-6041,
  6551-6652),
  `src/elspeth/web/sessions/routes/sessions.py`, `routes/composer/state.py`,
  `routes/operation_receipts.py`.
- `docs/specs/2026-09-16-composer-async-operations-design.md` §§1-5 in full.
- `docs/plans/2026-09-20-composer-async-operations/contract.md`: Adopted deviations, Rulings,
  Defaults D1-D16, Settings, Owned types, Table (Task 3), Authority (Task 4), Composite
  start/terminal (Tasks 5-6).

## Diagnosis: what `operation_receipts.py` actually is, structurally

`session_operation_receipts` is a **single-phase, synchronous, per-PK reservation table**. Every
function operates by exact `(session_id, operation_id)` lookup; there is no query that scans for
"the next unclaimed row." `reserve_operation_receipt` (models.py `operation_receipts.py:272-353`)
does two things atomically in one call: it admits the request *and* grants it a working lease —
the row is inserted with `status="in_progress"` on the very first write
(`operation_receipts.py:299`). **There is no admitted-but-unclaimed state in this model at all.**
The CHECK bundle at `models.py:793-800` encodes exactly three statuses
(`in_progress`/`completed`/`failed`) and ties `in_progress` to a live `lease_token` +
`lease_expires_at` pair unconditionally.

The compose job needs a fundamentally different shape, per spec §1 and contract.md's Table
section: a **two-phase** lifecycle — `queued` (admitted, no side effects allowed, freely
reclaimable by a cluster-wide `claim_next` scan) and `running` (claimed, bound to the session's
`SessionOperationLease`, **never** taken over — spec §1: "An expired `running` row is never taken
over for another provider attempt"). That second clause is the load-bearing fact: receipts'
`reserve_operation_receipt` *does* take over an expired `in_progress` lease and returns
`OperationReceiptTakenOver` (`operation_receipts.py:321-353`) precisely so a second attempt can
retry fork/revert work. For compose, doing that on a `running` row means replaying a provider
call — the exact thing Decision 1 forbids ("A worker must never run a second provider turn merely
to repair a missing transport result"). Reusing `reserve_operation_receipt` for compose would
therefore require *disabling* its takeover behavior conditionally on `kind`, inside a function
whose current callers (fork/revert) depend on takeover working. That is not a kind-branch for
convenience; it is two callers needing opposite behavior from the same state machine — the
definition of a lifecycle that should not be merged.

A second structural mismatch: receipts stores **locators only** (`result_state_id`,
`result_session_id`, UUIDs) and has no request payload and no cancel marker at all — the brief's
own measured facts confirm this ("no request JSON, no full result JSON, no cancel marker, no
actor column on the row, no queued state"). The compose job must store the **full**
`MessageWithStateResponse` (spec §1: "A single proposal locator cannot represent that response")
plus a bounded `request_json` while live, plus `cancel_requested_at`, plus a three-column
`session_operation_*` fence binding distinct from its own `claim_token`/`claim_expires_at`. Every
one of `_validate_row`'s branches (`operation_receipts.py:93-148`), the status-bundle CHECK
(`models.py:793-800`), and the kind/locator CHECK (`models.py:801-807`) would need to be rewritten
from scratch to admit these columns — not extended with an `OR` arm, because the existing arms
assert *absence* of columns (`result_state_id IS NULL`, etc.) that compose needs present in
different combinations per status. `contract.md`'s own status-bundle prose for
`composer_async_operations` (queued/running/completed/failed, four arms, request_json and
claim-triple and SOL-triple all varying independently) has no structural overlap with the
three-arm bundle at `models.py:793-800` beyond "it's a CHECK constraint on a status column."

Third: the compose authority's cluster-wide claim scan (`claim_next` with `FOR UPDATE SKIP
LOCKED` on PostgreSQL, contract.md Task 4) has **zero counterpart** in `operation_receipts.py`.
Nothing in the file queries for "the next reservable row" — every function is handed an exact
`(session_id, operation_id)` by its caller. So even the *authority* layer (contract.md's
`ComposerAsyncOperationAuthority`: `claim_next`, `renew_claim`, `release_claim`,
`settle_unstarted`, `list_expired_running`, `settle_lost`, `list_expired_queued`,
`settle_lost_inactive_session`, `settle_own_lapsed`, `count_nonterminal`) would be **net-new
code** under option A — none of it exists to "reuse" in `operation_receipts.py`. Placing it in
the same module next to fork/revert's single-lease functions would be Divergent Change: one file
serving two unrelated write-lifecycles, changing for unrelated reasons.

## Answering the brief's direct questions

**"How much of receipts' code would compose actually reuse under A or D, versus bypass or
special-case by kind?"** Functionally, almost none of the *live* machinery
(`reserve`/`renew`/`bind`/`settle`, the takeover branch, the events table and its sequence/
terminal-hash validation) is reusable as-is; all of it would need to be bypassed for compose
(different state count, different takeover rule, different result shape, no companion event
table planned for compose at all — contract.md's Task 3 names no
`composer_async_operation_events` table). What *is* structurally identical in shape (not in
code) is the codec pattern: a strict+`extra=forbid` DTO check, `operation_id` excluded from the
hash, a schema-version string, `stable_hash(...)`. That pattern is ~15 lines and contract.md
already independently arrived at "same shape... never imports" for it (Owned types section,
`composer_operation_request_hash`). That is the one place B's instinct ("reuse the codec") has
real merit — but as a shared *shape*, not a shared *function*, because the receipt codec's schema
string (`session-operation-receipt-request.v1`) is itself an audit-bound constant: existing
persisted `request_hash` values were computed against it, and reusing the function literally
would either bind compose rows to the receipt schema string (semantically wrong — a reader of
`session_operation_receipt`-schema hashes should be able to assume they came from a fork/revert
request) or force parameterizing the schema string and kind-literal through the receipt module,
which just re-derives a generic helper — see below.

**"Would A force kind-branches into a mode-neutral module (a smell), and would D be
behaviour-preserving or a rearchitecture of a just-shipped module?"** Yes to the first: A doesn't
just add branches, it needs the takeover branch to *invert* for one kind, which is the sharpest
form of the smell — a shared function whose contract depends on `kind`. On the second: `D` (a
generalized "client-minted session operation core") is not behaviour-preserving, because there is
no single core to extract that both consumers' *current, real* behaviour maps onto without a
config knob for "how many phases," "is takeover allowed," "is there a companion event table," and
"is the result a locator or a JSON blob." Parameterizing over all four collapses back into the
same kind-conditional shape, one layer removed — this is Speculative Extraction against a
"second implementation" that turns out not to share a lifecycle, only a rough family resemblance
(both use a client-minted UUID and both hash a strict request). `operation_receipts.py` was
introduced by `7001600fe` on this same branch; rearchitecting it now, days later, to host or
share machinery with a lifecycle it was never designed for is a rearchitecture of a just-shipped
module for a reuse payoff of roughly one codec's worth of code.

**"What exactly is shareable without coupling lifecycles?"** The codec *shape* (strict-DTO check
→ excluded-field dump → schema-tagged `stable_hash`), and nothing else with confidence. If the
panel wants that captured as actual shared code rather than convention, the safe move is a
generic helper in `contracts/hashing.py` or a new tiny module —
`_strict_request_hash(*, schema: str, session_id: UUID, kind: str, request: BaseModel,
exclude: set[str])` — that both `operation_receipt_request_hash` and
`composer_operation_request_hash` call. That is a same-day, low-risk micro-extraction (Task 2
scope, not a table decision) and does not argue for a shared table, a shared authority class, or
a shared events table.

## Consequences for T02–T06 under my position (C)

- **T02** (owned types + codecs): unaffected as currently contracted.
  `composer_operation_request_hash` stays its own function with its own schema string
  (`composer-operation-request.v1`); optionally factor the shared codec shape per the paragraph
  above — genuinely optional, does not gate anything else.
- **T03** (table, CHECKs, indexes, trigger, TablePolicy): unaffected. `composer_async_operations`
  is a new table with its own `TablePolicy("composer_async_operations", ...)`
  (`ComposerAsyncOperationAuthority`), independent of `session_operation_receipts`'s policy. No
  rewrite of the receipts CHECK bundles, no epoch collision to manage beyond the plan's own T15/T17
  epoch bump.
- **T04** (authority): unaffected — a genuinely new module (`claim_next`/SKIP LOCKED/reaper
  methods have no existing counterpart to inherit from `operation_receipts.py`).
- **T05** (composite start, R3): unaffected — `start_composer_async_operation` is new on
  `repository.py`; its only real coupling to existing code is `_advance_exclusive_fence_on_connection`
  (shared with `acquire()`, already planned as a refactor of `acquire`'s own body, not of receipts).
- **T06** (composite terminal, R2 + the F-B2/C2 cancel-fencing predicate): unaffected — the
  predicate is already scoped to `composer_async_operations` rows specifically
  (`_require_session_operation_context_on_connection` gains a check keyed to *that* table's fence
  triple). Under option A this predicate would instead need to distinguish compose rows from
  fork/revert rows inside the shared receipts table by `kind`, which is exactly the same
  kind-branch risk as above, now inside a security/audit-fencing predicate — higher blast radius
  if wrong than the takeover branch.

Under option A, all five tasks instead carry a widening cost: T02's hash function and schema
string are Tier-1 audit-bound and can't be silently repointed; T03's CHECK bundle needs to be
rewritten (not extended) to admit `queued`/`running` and four new column groups; T04's entire
scan/claim surface is net-new code regardless of where it lives, so "reuse" buys nothing there;
and every one of the nine existing call sites in `blobs/service.py`, `coordination/repository.py`,
and `service.py` that reads `session_operation_receipts_table.c.status` would need re-audit for a
newly four-valued status column, even though most already filter by `kind == 'session_fork'` and
so would not misfire on data — the risk is status-literal drift (`"in_progress"` vs `"running"`)
across files that don't currently need to reason about a queued state at all.

## Strongest argument against my own position

If a second async-job kind is ever added beyond `compose_message`/`compose_recompose` — say, a
third long-running composer action with the *same* two-phase queue/claim/running shape — then two
near-identical tables (`session_operation_receipts` and `composer_async_operations`) sitting side
by side with zero shared code, other than a codec pattern, is a real duplication cost, and at that
point option D's generalized core stops being speculative (the "wait for the second
implementation" rule would be satisfied by a second *matching* implementation, not by
fork/revert which doesn't match). But that is a future contingency, not a fact about the two
lifecycles in front of the panel today, and per the anti-patterns table in my own operating
brief: "Don't extract a class because it might grow." If the operator wants to hedge against that
future now, the correct hedge is naming the extraction point in a comment on
`ComposerAsyncOperationAuthority` (a "if a third two-phase kind appears, factor claim_next/
renew_claim/settle_* out of both authorities" note), not building the abstraction today for one
real consumer.

## Fact I found to be wrong / needing correction

None of the brief's measured facts about `operation_receipts.py` or the receipts tables were
wrong against what I read. One fact worth sharpening: the brief says compose needs "a `running`
row that is never taken over (no provider replay)" — I traced this precisely to
`reserve_operation_receipt`'s takeover branch (`operation_receipts.py:314-353`,
`OperationReceiptTakenOver`) as the concrete function that would need to behave oppositely for
compose kinds; that specific contradiction (not just "the shapes differ") is, in my view, the
single strongest piece of evidence for keeping the tables separate, and I'd want it in the
panel's consolidated record even if another panellist's domain (DB/queueing) makes the case more
completely.

## Risk Assessment

- **Low** risk in recommending C: it is what `contract.md` already specifies in full (Tasks 2-17
  already written against it), so this position changes nothing about the in-flight plan.
- **Medium** risk sits in the tiny codec-shape extraction I floated as an addendum to T02: if
  done carelessly it could accidentally let a future caller pass a receipt kind through the
  compose schema string or vice versa. If the panel or operator wants it, it should carry an
  explicit test asserting the two schema-string constants remain distinct.
- The one place I could not fully verify from static reading alone: whether any *other* in-flight
  branch work (composer-wires campaign, per MEMORY) touches `operation_receipts.py` concurrently
  in a way that would change this analysis before it lands. I read only the tree at `6506f6a7f`.

## Information Gaps

- I did not read the DB/queueing-specific literature on `FOR UPDATE SKIP LOCKED` performance
  under the existing connection-pool sizing — that is squarely the database-architect lens, not
  mine, and I have not duplicated it here.
- I did not evaluate the systems-thinking angle (operational/on-call cost of two tables vs one)
  — deferred to that panellist.

## Caveats

- This position is scoped strictly to CODE STRUCTURE / reuse, per my assigned lens. It does not
  re-litigate R2/R3 or the freeform-only scope ruling, both of which I treated as fixed inputs.
- "Own codec" in my recommended position C is the contract.md status quo; the only change I'd
  suggest is optionally factoring the shared *shape* of the two codecs into one generic helper,
  which is a same-day Task 2 detail, not a reason to revisit the table decision.
