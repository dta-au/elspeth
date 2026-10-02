# Cross-examination: leverage lens

Panellist: leverage analyst (Meadows hierarchy). This is a read-only pass. Facts were **verified at
`a375d7f13`**. The brief cites `6506f6a7f`. The one commit between them, `a375d7f13`, is a gateway
timeout change and does not touch `sessions/` or `coordination/`, so the line numbers below should
match the brief's tree.

My opening position was B′: a separate table, a neutral shared request-identity codec, and the
invariants enforced by database rules, the mutation-authority gate and the COMPOSE chokepoint. The
cross-examination keeps the option. It moves three enforcement points to higher-leverage places, and it
turns my neutral stance on the identity precondition into a recommendation. So **`changed = true`**:
the option is the same, but the enforcement points and the identity recommendation are different.

## 0. The panel record has one fact wrong

The relayed summary lists codex as "Forwarded", with no position. But
`panel-2026-09-28/position-codex.md` holds a substantive position: **B with a domain-separated shared
request codec**, high confidence. Its three reasons are the takeover and `queued` state-machine
difference, full-DTO storage versus locator replay, and the different write boundary. It also offers a
conditional fallback to C (§2.4 below). The consolidated record should count codex as a **B/B′** vote.
It should not record codex as abstaining.

## 1. Rebuttals, one per differing position

### systems-thinker: B (moderate)

We agree. There is one sharpening. They propose "an explicit negative test asserting no code path can
move a running row back to a re-adoptable state". A test enforces the invariant only for the code paths
it exercises. That is an information-flow control at Level 6, and it sits below the rule it protects. My
transition-guard trigger (§3) enforces the invariant at Level 5, inside the database, against *every*
writer, including one written next year. The negative control then proves the trigger works.

I accept their point on D: extract a shared core only after a third client-minted kind actually
appears. That is also the re-test trigger tech-critic names.

### python-architect: C (high)

- **Their "optional low-risk addendum" is B′.** It factors the strict+forbid check, the
  `operation_id` exclusion and the schema-tagged `stable_hash` into one helper that both codecs call.
  B′ and C-with-the-addendum produce the same code. The only open question is whether the extraction is
  optional.
- **At the leverage level it should not be optional.** The *normalisation rule* is what the audit
  binding depends on: what counts as "the same request". Keeping two copies of that rule puts a Level-12
  duplicate on a Tier-1 binding, and the two copies will drift as the DTOs evolve.
- **"A shared file housing two lifecycles" (Divergent Change) does not apply to B′.** Under B′ the only
  thing in the neutral module is the normaliser. No lifecycle code sits there, and
  `reserve_operation_receipt` stays receipts-only.
- **What I adopt from them and from codex:** if a golden-vector test cannot show that receipt hashes
  stay byte-identical after the extraction, ship C. Durable receipt identity takes priority over removing
  the duplicate. This fallback is conditional on that proof. It is not the default.

### tech-critic: B′ (moderate)

We agree on the option. We disagreed on identity, and they are right; §2.1 records the change. One small
rebuttal: they say A "could add a nullable `result_json` column", so the full-DTO fact "only rules out
receipts' replay model, not the table". That is literally true. But a nullable column that means
something for only two of four kinds is the same kind-conditional shape as the takeover branch, just one
column wide. It is a smaller instance of the same objection, not a counter-example to it.

I accept their T05 point that a revert or fork under COMPOSE SOL between admission and start needs an
explicit ruling on whether the start refuses or records the change. It sits outside my lens. It is a
real gap, and D9 (the worker checks `state_id` → 404) only partly covers it.

### solution-architect: B′ (high)

We agree. Their identity recommendation matches the one I now make. I accept their "delete
`_track_compose_inflight` after T13 rather than extract-and-keep" as a no-old-pathways consequence.

### db-architect: B (high)

I have no rebuttal on the option. Two of their proposals are at a higher leverage point than mine, and
I adopt both (§2.2, §2.3).

On their "against itself" argument: they say A adds no trigger and no TablePolicy row, because the
receipts terminal trigger's `WHEN OLD.status IN ('completed','failed')` already covers compose. The
cost they leave out is the gate. Under A, `TablePolicy.permits` matches on authority alone. The primary
authority is permitted every operation
(`tests/unit/architecture/test_session_db_mutation_authority.py:41-44`). So every compose writer gains
reach over fork/revert rows, and the exact receipts pin at `:12208-12215` has to widen. Saving 30–60
lines of trigger DDL does not justify weakening the one gate that makes the receipts writers visible.

### audit-integrity: B (high)

We agree on the option. Their plan-level findings are right, and I adopt two of them (§2.3, §2.4).

One part I do not adopt as the default is the **full `composer_async_operation_events` table**. The
turn's audit evidence already lives in `chat_messages` and the `llm_calls` cohort, and F-B2/C2 persists
the cohort even on cancel (contract.md:46). Claim churn is closer to transport telemetry (sre's reading),
so the events table is a Level-6 addition whose value depends on how ADR-046 classifies it. That is
John's call. My default is the cheap structural fallback in §2.3, with the events table held back until
John rules. If John rules that claim history is custody evidence, B′ adds the table on the receipts
pattern. Nothing in that choice argues for option A.

### sre: B (moderate)

- **I rebut the signature widening.** They propose "reuse `operation_receipt_request_hash` through a
  kind-agnostic signature". That changes the receipts wrapper's public type and gives up its closed
  `OperationReceiptKind` Literal (`protocol.py:264`), which is the kind domain the receipts facility
  typechecks against. B′ instead keeps two typed wrappers, each with its own Literal and schema tag, over
  a neutral core that takes `kind: str`. Codex specifies the same arrangement.
- **I accept their pool-isolation point as a complementary lever.** Polls, scans and fork/revert all
  share the 16+16 `run_sync_in_worker` pool. That affects failure isolation more than the table choice
  does. I did not verify the pool sizes; they are listed under Information Gaps.
- **I accept their retention point** as an owner decision. It interacts with §2.4.

## 2. Points that changed my position

### 2.1 Identity: from "John decides, no recommendation" to "recommend option (2)"

In my opening I laid out two shapes: (1) the job row subsumes ingress dedup and `message_ingress_receipts`
is removed, or (2) `operation_id` *is* the ingress key. I did not choose between them. The tree settles
it. `message_ingress_receipts` is more than dedup:

- It is **immutable**, protected by `trg_message_ingress_receipts_no_update` / `no_delete` (the
  inventory rationale is at `sessions/schema.py:84-86`, the required set at `:104-105`, and the PG probe
  at `:393-395`).
- It is written only while a COMPOSE SOL is held, in the user-row path
  (`sessions/service.py:1410-1420`, which requires `SessionOperationKind.COMPOSE` and the session write
  lock).
- It is **projected into the transcript read**. `get_messages` outer-joins `client_request_id` onto
  every chat row (`sessions/service.py:4759-4764`), and the SPA matches its own sends against it.

Option (1) would therefore delete a live read path and an immutable audit binding. The owner's rule
that removing debt is not the same as deleting unfinished intent rules that out, and so does tech-critic's
immutability argument: the job row is mutable until it reaches a terminal state. **I recommend option
(2):** the job's `operation_id` is the `client_request_id`, and there is one UUID per user action.

The design also fits the no-side-effect invariant for free. Writing the ingress receipt already requires
COMPOSE SOL, and under R3 only a `running` job holds that. So the worker writes the ingress row in the
user-row transaction, and nothing is written before `running`.

The following die at cutover rather than surviving as dual acceptance:

- the route's `message_already_accepted` and `message_idempotency_conflict` 409 paths;
- the SPA's recovery path that matches `client_request_id` against the transcript.

A worker-side ingress conflict becomes a Tier-1 `AuditIntegrityError`, because admission has already
compared `request_hash`.

**Proposed at moderate-low confidence (not prototyped):** make the agreement between `job.user_message_id`
and the ingress row structural rather than a code invariant:

- Add a unique constraint on `message_ingress_receipts (session_id, client_request_id, user_message_id)`.
  It is a superset of the PK, so it adds no new restriction.
- Add a composite FK `composer_async_operations (session_id, operation_id, user_message_id)` → that
  unique constraint. Under MATCH SIMPLE, a NULL `user_message_id` (queued, or running before the user
  row exists) skips the check, and a set value must match the ingress binding exactly. Both SQLite and
  PostgreSQL skip the check when any child column is NULL, but this has to be confirmed on both dialects
  before it is relied on.
- The delete behaviour is mixed and must be stated explicitly, not inherited silently. Ingress →
  `chat_messages` is `ON DELETE CASCADE` (`models.py:473-477`), while the plan's job → `chat_messages` FK
  is RESTRICT (contract §Table). Pick one on purpose.

John still rules on the relationship. It sits outside options A–D.

### 2.2 D8 becomes a schema invariant: partial unique index on `queued` + `running` (from db-architect)

D8/R10 says admission refuses a NEW operation id while the session has a *nonterminal* job
(contract.md:78). So the owner's invariant is at most one queued-or-running job per session. The plan
enforces it with a code check inside `admit` and indexes only `one_running_per_session`. db-architect's
`uq_composer_async_operations_one_nonterminal_per_session (session_id) WHERE status IN ('queued','running')`
subsumes that index and moves an owner ruling from a Level-12 check inside one method up to a Level-5
database rule. This is higher leverage than the index I argued for, and I adopt it.

- **Reflection objection pre-empted.** This is a two-element `IN` in an index predicate, not a
  one-element `IN` in a CHECK. The tree already has the identical shape in
  `uq_runs_one_active_per_session`, with symmetric `sqlite_where`/`postgresql_where` and the
  shape-collector coverage described at `models.py:2000-2011`.
- **T04 consequence.** `admit`'s INSERT can now fail with an `IntegrityError` from this index, not only
  from the PK. The existing re-read-and-compare path for a racing insert must also map this case to
  `ComposerOperationActiveError`, carrying the *existing* row's `operation_id` and `kind` for the D8 409
  body. The session lock still catches it first, and the index is the backstop.
- **T04 simplification.** `claim_next`'s "oldest queued per session, skipping sessions with a running
  row" becomes "the one queued row per session".

### 2.3 Terminal rows keep forensic authority (from db-architect and audit-integrity)

The contract's completed and failed bundles set the claim triple NULL (contract §Table), and
`session_operation_fences` is keyed by `session_id` alone (`models.py:325-332`). Once the next operation
overwrites the fence row, a terminal job cannot tell which instance ran or reaped it. The fence table's
own design comment says "Release never nulls forensic authority" (`models.py:321-324`). **This amends the
contract's terminal bundle CHECKs.** Null only `claim_token` and `claim_expires_at`, and keep
`claim_owner_instance_id` and `attempt`. Also add audit-integrity's closed `settled_by` column
(worker / reaper / own-lapsed / inactive-session / unstarted), so the settlement path is a CHECKed fact
rather than a log line. This is the cheapest Level-5 fix and is my default. The full events table is
John's ADR-046 call (§1, audit-integrity).

### 2.4 One writing authority via connection-taking helpers (from audit-integrity)

The precedent is verified. `SessionServiceImpl.settle_fork_operation_receipt` calls
`settle_operation_receipt(conn, …)` inside its own locked transaction (`sessions/service.py:1169-1175`).
The writer gate attributes the SQL to the *helper's* authority (`SessionOperationReceiptAuthority`,
test `:458-467`), and the receipts-events policy is pinned to `operation_authorities == ()`
(`:12208-12213`).

The plan should apply the same shape to compose. The T05 start composite and the T06 terminal composite
keep their transaction boundaries, as required by R2 and R3. They call
`mark_running_on_connection` / `settle_terminal_on_connection` helpers in the compose authority module.
The plan's TablePolicy then shrinks from three authorities to
`TablePolicy("composer_async_operations", "session", "ComposerAsyncOperationAuthority")` with no
operation grants. My earlier T05 pin ("no writer outside the start composite sets `status='running'`")
becomes a gate fact about **one helper symbol** rather than a review promise. This is a strict upgrade to
the pin I proposed.

## 3. Refinements to my own proposals

**Transition-guard trigger, specified so that it does not block the plan's own transitions.** The
trigger fires on UPDATE with `OLD.status = 'running'`.

It permits:
- `running → completed | failed`, including nulling `claim_token`/`claim_expires_at` and
  `request_json`, and setting `settled_at`, the result columns, `failure_code` and `settled_by`;
- setting `cancel_requested_at` from NULL, which is `request_cancel`.

It forbids:
- `running → queued`;
- changing `kind`, `request_hash`, `actor_user_id`, `claim_token` while the row stays running, the SOL
  triple, `started_at`, `deadline_at` or `claim_owner_instance_id`;
- clearing `cancel_requested_at`;
- setting `user_message_id` twice.

The trigger fires on UPDATE only, so the queued → running transition (from `OLD.status = 'queued'`) is
unaffected. A negative control goes with it: a test writer that reclaims a running row must go red on
both dialects. The trigger is **not prototyped**; see Information Gaps.

**Terminal-row DELETE gap (from audit-integrity), verified.** `TablePolicy.permits` returns True for any
operation by the primary authority (`test_session_db_mutation_authority.py:41-44`). The only explicit
delete denial in the receipts pin is for the secondary authority (`:12214-12215`). So a `delete` written
inside `ComposerAsyncOperationAuthority` would pass the gate, and the plan's table has only a terminal
*UPDATE* trigger (contract §Table). A `BEFORE DELETE … WHEN EXISTS (sessions WHERE id = OLD.session_id)`
trigger follows the receipts-events pattern (`models.py:1682-1690`). It closes the gap while still
letting the D7 session cascade through.

Tradeoff: this closes off pruning of settled rows without a schema change, which is sre's retention
concern. I recommend defaulting to deny and flag it for John rather than decide it.

**The positive chokepoint predicate (T06) stands.** No panellist addressed it. The chokepoint at
`sessions/service.py:970-996` validates only the fence row. Under D12 (contract.md:82), a live SOL can
outlast a terminal job, so the negative F-B2/C2 predicate would still admit a late write. The positive
form requires that any job bound to this fence triple is `running` with `cancel_requested_at IS NULL`,
unless the write is `audit_only`. With §2.2 in place, at most one such row can exist per session, which
makes the lookup a single-row probe.

## 4. Final position

**B′, amended.** Recommendations:

1. A separate `composer_async_operations` table with its own single-authority TablePolicy
   (connection-taking helpers).
2. A neutral request normaliser with typed per-facility wrappers and distinct schema tags. Receipt
   hashes must be proven byte-identical by golden vector, or the plan falls back to C.
3. The invariants sit at the highest available leverage points:
   - D8 as a partial unique index on `queued` + `running`;
   - "never taken over" as a running-row transition-guard trigger, plus the absence of any lease column
     on running rows;
   - "no side effect before running" and "cancel races completion" through the positive predicate at
     the COMPOSE chokepoint `service.py:970`;
   - the full DTO through the completed-bundle CHECK;
   - custody through terminal rows that keep `claim_owner_instance_id`, `attempt` and `settled_by`, and
     cannot be deleted while their session lives.
4. Precondition outside A–D, for John: recommend that `operation_id` **is** the existing
   `client_request_id`, with `message_ingress_receipts` kept as the immutable user-row binding written by
   the running worker. The 409 ingress paths and the SPA transcript-match recovery are deleted at
   cutover.
5. Open owner calls: whether to add the full job events table (ADR-046), whether terminal rows can
   ever be pruned (retention), and whether a head change between admission and start is refused or
   recorded (tech-critic, T05).

## Confidence Assessment

**Overall: Moderate-high** that amended B′ beats A, C and a full D. Each enforcement proposal carries
its own confidence below.

| Finding | Confidence | Basis |
|---|---|---|
| Codex holds a B position, not "Forwarded" | High | `position-codex.md` lines 1-3 read in-session |
| Ingress receipt is immutable, needs COMPOSE SOL, and is projected into the transcript | High | `schema.py:84-86,104-105`; `service.py:1410-1420, 4759-4764` |
| Option (2) identity beats option (1) | Moderate-high | Above evidence plus owner doctrine; John rules |
| D8 is "one nonterminal per session" and the partial-index precedent exists | High | contract.md:78; `models.py:2005-2011` |
| The terminal bundle loses instance identity | High | contract §Table bundles; `models.py:321-332` |
| The one-authority helper precedent and gate attribution | High | `service.py:1169-1175`; test `:458-467`, `:12208-12213` |
| The primary authority may delete under the gate | High | test `:41-44` |
| Composite MATCH SIMPLE FK job → ingress | Low-moderate | Reasoned from documented NULL-FK semantics; not prototyped |
| The transition-guard trigger is feasible on both dialects | Moderate | By analogy with the existing terminal-immutable and no-delete triggers; not prototyped |
| The positive chokepoint predicate closes the D12 window | Moderate | `service.py:970-996`, contract.md:46,82; not run |

## Information Gaps

- I did not prototype the transition-guard trigger or the no-delete trigger on SQLite or PostgreSQL. I
  also did not measure whether the PG reflection probe (`test_schema_probe_postgres.py`) and
  `_REQUIRED_AUDIT_TRIGGERS` need new arms for them. A failure here would move the invariant back to a
  Level-6 test.
- I did not test composite-FK NULL-skip behaviour or the mixed CASCADE/RESTRICT interaction. That
  includes db-architect's concern that a PostgreSQL session delete may fail on a RESTRICT FK during
  cascade; only SQLite was measured, by them.
- I did not verify sre's `async_workers.py` pool sizes (16+16).
- I did not read the design intent in `docs/plans/2026-09-27-composer-system-boundary-repair.md` for
  whether ingress was meant to outlive an async cutover. A grep for async, 202 and operation_id there
  found nothing.
- I did not re-measure the F-B2/C2 claim that every composer write passes `service.py:970`.
- How ADR-046 classifies job claim history (custody evidence or telemetry) is an owner call.

## Caveats & Required Follow-ups

1. **Before T02:** John rules on identity (recommended: `operation_id` ≡ `client_request_id`). Rebase
   the contract DTO section onto `schemas.py` as it is now.
2. **T02:** the golden-vector byte-identity test for receipt hashes comes *first*. If it cannot be
   shown, ship C and do not touch receipts.
3. **T03 contract amendments:**
   - the partial unique index on nonterminal rows, replacing `one_running_per_session`;
   - terminal bundles that keep `claim_owner_instance_id` and `attempt`, plus `settled_by`;
   - the transition-guard trigger and the no-delete-while-session-lives trigger, each with a negative
     control that must go red on both dialects;
   - epoch 72.
4. **T04–T06:** a single-authority TablePolicy through connection-taking helpers; `admit` maps an
   index `IntegrityError` to `ComposerOperationActiveError`; the positive chokepoint predicate, with a
   negative control that writes under a lingering SOL after the terminal CAS.
5. **For John:** the events table (ADR-046), the retention of terminal rows (which interacts with the
   no-delete trigger), and T05's head-change ruling.
6. This analysis does not cover capacity, throughput, the reaper's timing, or frontend custody.
