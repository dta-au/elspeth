# Cross-examination: audit integrity and trust tiers

**Position changed.** I opened with B plus a compose-owned append-only event
table. I now hold **B′**. The option letter stays B: a separate
`composer_async_operations` table and a shared canonicalisation core
parameterised by schema tag. What moved:

- The event table is downgraded to row-level forensics: keep the owner id and
  attempt on terminal rows, and add a closed `settled_by` column.
- I now add one client-minted identity per send, bound to the immutable
  `message_ingress_receipts` row by a composite FK.
- I now add leverage's transition-guard trigger and the positive predicate at
  the COMPOSE context check.

All citations were re-read on `release/0.8.1` at `6506f6a7f`. I did not
revisit the owner rulings (freeform only; 202+poll; R2; R3).

## Rebuttals, position by position

### python-architect: C

On my lens, B versus C for the codec matters less than my opening claimed,
and I withdraw the drift-hazard framing as an integrity argument. The two
families use distinct schema tags, so their hashes cannot collide across
families. Each family's hash only has to be stable within its own family and
epoch. Two independent codecs meet that requirement.

The case for sharing is ordinary maintainability. python-architect's own
addendum ("factor the shared codec shape into one generic helper") is the
same B′ that five other panellists named, so this is convergence rather than
disagreement.

One point holds under either option: the receipt hash bytes need a
golden-vector test. A deploy without an epoch bump can change in-flight
receipt hashes. `reserve_operation_receipt` compares `request_hash` on replay
(`operation_receipts.py:39-53`, `:294-353`), so a silent byte change turns a
legitimate retry into a conflict. Under C the new compose codec needs its own
golden vector as well.

python-architect's T06 point is correct and reinforces "not A": a kind branch
inside the cancel-fencing predicate has the largest blast radius of any
branch in the plan.

### leverage: B′

I accept two points and reject one.

**Accept: the positive predicate.**
`_require_session_operation_context_on_connection`
(`service.py:970-996`) admits any fence row where `released_at IS NULL AND
lease_expires_at > now`. D12 (`contract.md:82`) allows the SOL close to fail
after the terminal CAS has committed. F-B2/C2 as written fires only on
`status='running' AND cancel_requested_at IS NOT NULL` (`contract.md:46`). So
a lingering fence still admits non-audit session writes that are attributed
to a turn whose terminal payload is already sealed.

That is an ADR-046 gap. The terminal row says "this is the result", while the
session keeps changing under the same authority triple. The positive form
closes it: when a job row is bound to this triple, it must be `running` with
`cancel_requested_at IS NULL`, unless `audit_only`.

Required negative control: settle the job, fail the SOL close, then attempt a
non-audit write under the still-live triple. The write must raise.

Required positive control: the owner's audit-only `llm_calls` persist before
`request_cancelled` (`contract.md:46`) must still succeed.

**Accept: the transition-guard trigger.** My opening asserted that
"running-row binding fields never change" only through authority discipline.
leverage's trigger enforces it in the database. It forbids:

- `running`→`queued`
- any change to kind, request_hash, actor, SOL triple or started_at on a
  running row
- clearing `cancel_requested_at`

This moves the property from "every reviewer remembers" to "the database
refuses". It also belongs in `_REQUIRED_AUDIT_TRIGGERS` (`schema.py:96-110`),
so startup catches a dropped trigger.

**Reject: identity option (1).** Option (1) folds ingress dedup into the job
row and drops `message_ingress_receipts`. That weakens immutability. The
ingress table carries `trg_message_ingress_receipts_no_update` and
`no_delete` (`schema.py:84-86`, `:104-105`), and it cascades to
`chat_messages`, which is itself `no_delete`. The job row, by contrast, is
mutable through queued→running and clears `request_json` at settlement.
Choose option (2): `operation_id` is the ingress key. Ingress remains the
immutable acceptance record.

### solution-architect and tech-critic: B′ plus one client-minted id

I concede the omission. My opening never mentions `client_request_id` or
`message_ingress_receipts`. Two ids on one send would give two acceptance
records with different replay semantics (202 replay versus 409), which is
dual acceptance.

My sharpening on this lens: "job.user_message_id agrees with ingress" should
be a composite FK, not a convention. Ingress has PK
`(session_id, client_request_id)` and `UNIQUE(user_message_id)`
(`models.py:464-488`). If you add
`UNIQUE(session_id, client_request_id, user_message_id)` on ingress, the job
can carry
`FK (session_id, operation_id, user_message_id) → ingress`. The binding
between the job, the ingress record and the user row then becomes a schema
invariant.

I endorse tech-critic's rule: when the worker finds an existing ingress row
for its `operation_id` bound to a different user message, raise a Tier-1
`AuditIntegrityError`, not a 409. Admission already deduplicated, so this is
corruption, not a user retry.

This is a new owner question that sits before T02. It does not relitigate a
ruling.

### sre and tech-critic: "the event log is telemetry or ceremony"

This challenges the distinctive part of my opening, and it partly lands under
the AGENTS.md delivery posture (keep a mechanism only if it materially
improves integrity). I split the question into four parts.

- **Claim/release/reclaim history: conceded.** Queued claims have no side
  effects, so this history is transport telemetry. Structlog is enough.
- **Cancel actor: conceded.** `contract.md:37` states that ownership is
  re-checked on every call, and the authority methods take no actor. The
  cancel actor is therefore the row's `actor_user_id`, and a
  `cancel_requested` event carrying an actor would be redundant.
- **Settlement provenance: held.** `failure_code` (D13, `contract.md:83`)
  does not identify which path settled the row.
  - `worker_lost` can come from `settle_lost` (reaper on another instance),
    `settle_own_lapsed` (F-M3, `contract.md:49`),
    `settle_lost_inactive_session` (`contract.md:21`), or the B4-residual
    adopt defect (`contract.md:56`).
  - `request_cancelled` can come from `request_cancel` on an unclaimed
    queued row, `settle_unstarted`, the owner's terminal path, or
    `settle_lost` with `cancelled_failure` (`contract.md:19`).

  A `worker_lost` or `request_cancelled` turn has no assistant row, so the
  terminal job row is the only evidence of what happened to that user
  message. The plan nulls `claim_owner_instance_id` at settlement.
  `session_operation_fences` is keyed by `session_id` alone
  (`models.py:325-351`), so the SOL triple on the job cannot recover the
  instance once the next operation overwrites the fence.

  Because a terminal row is written exactly once and the terminal-immutable
  trigger protects it, a closed `settled_by` column on the row carries
  everything a terminal event would, without a second verifier that could
  drift. For the same reason I drop the separate `terminal_hash` event.
  `result_sha256` plus the strict-DTO re-validation on poll already serve as
  the read guard.
- **Delete guard: held as a recommendation, with sre's retention caveat.**
  The plan's only trigger is UPDATE-time (`T03.md:63`), so a terminal compose
  row can be deleted while its session lives. Receipts cannot be deleted that
  way, because their event cascade hits `no_delete` (`models.py:840-845`,
  `:1682-1690`). A `no_delete` guard that applies while the session exists
  (the chat_messages pattern) is cheap. If the owner wants terminal compose
  rows pruned, that should be an explicit ADR-046 retention ruling, not
  something the schema permits silently.

### db-architect: B

I endorse both points.

- **Keep `claim_owner_instance_id` and `attempt` on settlement.** Null only
  the `claim_token` and `claim_expires_at` fencing columns. The precedent is
  `models.py:321-324`: "Release never nulls forensic authority."
- **The partial unique index on `(session_id) WHERE status IN
  ('queued','running')`.** It turns D8 (`contract.md:78`, one nonterminal job
  per session) from an authority-method check into a schema invariant.
  Invariants the database enforces are the strongest form of Tier-1 guard.

db-architect's point that under A three compose writers would gain legal
reach over fork/revert rows is the same finding as my first opening reason.

### systems-thinker and codex: B

We agree on substance, and I endorse their sub-points.

- **systems-thinker:** the T02 parameterisation of the schema string and kind
  type, without widening `OperationReceiptKind` (`protocol.py:264`). Also the
  T04 negative test showing that no code path moves a running row back to a
  re-adoptable state. The transition-guard trigger backs that test up in the
  database.
- **codex** (the `position-codex.md` content, not the panel's "Forwarded"
  summary): the domain-separated shared primitive with two public wrappers and
  golden vectors. I also accept its fallback: if receipt byte-identity cannot
  be proved, fall back to C rather than change durable receipt identity.

## Mutation-authority point (unchanged, re-verified)

`TablePolicy.permits`
(`tests/unit/architecture/test_session_db_mutation_authority.py:36-44`)
matches on table, authority and operation, and never on `kind`. The receipts
pin covers exactly 6 writer sites, one owner and one `update` grant, and the
events table has `operation_authorities == ()` (`:12188-12216`, policy
`:190-195`). Option A would widen that pin to cover two lifecycles.

My T05/T06 proposal leaves the R2 and R3 transaction boundaries unchanged. It
puts the start-to-running CAS and the terminal CAS in connection-taking
helpers in the compose authority module, and calls them from inside the
existing composites. This follows the precedent at `service.py:1169-1175` and
the gate's attribution at `:458-467`. Only the location of the `UPDATE`
statement moves. The compose `TablePolicy` then has one authority with no
operation grants. leverage's writer-manifest pin (only the start composite
sets `status='running'`) fits on top of that.

## Final position

**B′.** The plan changes are:

- **Separate table.** A separate `composer_async_operations` table with its
  own single-authority `TablePolicy`.
- **Shared request-normalisation core.** One core, parameterised by schema
  tag, with golden vectors for both families.
- **Row-level forensics instead of an event table.**
  `claim_owner_instance_id` and `attempt` are retained at settlement, a closed
  `settled_by` column is added, and a no-delete-while-session-exists guard is
  added unless the owner rules otherwise on retention.
- **Transition-guard trigger.** Added to `_REQUIRED_AUDIT_TRIGGERS`.
- **Positive COMPOSE-context predicate.** Covers the D12 lingering-fence gap.
- **Partial unique index.** On queued/running rows, for D8.
- **One client-minted id per send.** `operation_id` is the
  `message_ingress_receipts` key, bound by a composite FK, and a worker-side
  conflict is Tier-1.

This last item needs an owner ruling before T02. A is rejected because it
makes the authority gate unable to tell fork/revert writers from compose
writers, and because it puts a takeover path next to a row that must never be
replayed.
