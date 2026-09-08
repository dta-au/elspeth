# ADR-047: Landscape Database-Clock Authority — Custody, Liveness, Expiry and Takeover Decisions Read the Landscape Database's Clock

**Date:** 2026-09-05
**Status:** Accepted; amended 2026-09-08 for fresh lease decisions (elspeth-8f97b3403e, operator approval recorded in comment 9902). Original P4-C6 programme: elspeth-0ff11aa42e; Q1(c): elspeth-d729c26729.
**Deciders:** ELSPETH maintainer
**Review evidence:** the fail-closed gate `tests/unit/core/landscape/test_database_clock_authority.py` (4 red ids; corpus measured at p4/c `ccb98a293`: 180 distinct findings across 21 files); the Sessions-side precedent `_SessionOperationAuthorityRepository._database_now` / `_lock_fence_and_read_database_time`; ADR-030
**Tags:** landscape, coordination, multi-replica, clock, fencing, related-adr-030

## Current decision — amendment of 2026-09-08

Landscape remains the sole database clock authority for Landscape custody,
liveness and expiry. For a lease decision, acquire the required authority and
target-row locks before obtaining a fresh, aware-UTC Landscape database sample.
Reuse that sample for the decision's eligibility predicates, CAS conditions,
deadline writes and associated timestamps. No process or Sessions clock enters
the decision, and no public caller can supply its time.

`read_landscape_decision_time(conn)` in
`src/elspeth/core/landscape/database_clock.py` provides the fresh sample. On
PostgreSQL it executes a separate `SELECT clock_timestamp()` after locking;
on SQLite it reads database time with millisecond resolution after the write
lock is acquired. PostgreSQL's transaction-start `CURRENT_TIMESTAMP` is not
fresh after a wait. An inline `clock_timestamp()` in a contended UPDATE also
does not establish post-lock sampling: the target expression can be evaluated
before the row lock is obtained. The old `read_landscape_transaction_time`
helper retains its separately named transaction-time semantics; it cannot
stand in for the fresh sample of a locked lease decision. The dialects'
timestamp semantics are documented by [PostgreSQL](https://www.postgresql.org/docs/16/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT)
and [SQLite](https://www.sqlite.org/lang_datefunc.html).

The leader identity-and-epoch fence remains the first authority operation,
with a direct full-token conditional UPDATE and immediate cardinality-one
refusal. An identity-preserving UPDATE establishes the lock before the fresh
deadline renewal. The lock remains held through commit or rollback. Expiry is
not added to this admission predicate. A pure membership fence checks active
worker identity and does not issue or renew a heartbeat. Item and sink-effect
operations retain their owner, generation, status and strict-expiry rules.

The transaction owner must finalize only the continuing deadlines that its
transaction explicitly issues or renews, after its body and while the required
locks remain held, or refuse completion when its selected reserve has been
consumed. The implementation uses explicit family-owned issuance and renewal:

- A successful leader-fenced body renews its still-owned seat at its normal
  completion boundary. Intentional release cancels that obligation; the
  completion path must not recreate the seat.
- Registration, acquisition and leader heartbeat issue paired seat and worker
  deadlines from one final sample. Ordinary leader-fenced writes renew only
  the seat. Follower admission rechecks the foreign seat's liveness and cannot
  renew that seat.
- Scheduler and sink-effect writers lock the target, sample once, issue their
  deadline and register the exact persisted expiry. Their immutable return
  values and audit events retain that exact expiry. A long remaining body is
  refused rather than silently changing those published values.
- Connection-accepting helpers register obligations on the caller's outer
  transaction. Returning from a helper is not a commit; the helper neither
  commits nor retries the caller's body. Registration inside a savepoint is
  rejected until nested rollback semantics are explicitly supported.

A shared read-only commit guard checks registered deadlines after journal
enrichment, serialization and outbox insertion, immediately before the DBAPI
commit. Family renewal DML occurs before journal serialization. The guard does
not discover leases, rewrite deadlines or retry work. State is scoped to the
outer transaction and cleared on every exit, including rollback and failed
commit, so pooled connections cannot retain another transaction's obligations.

For effective nominal duration `W` (the duration actually added after any
dialect or TTL alignment), the implementation requires a remaining reserve of
`min(1 second, 0.1 × W)` at that check. This is a completion threshold chosen
to reject consumed tails while supporting short test and configured leases;
it is not a measured bound on production commit latency. Consumed reserve
raises an owned timeout and rolls back the transaction, without claiming lost
membership, corruption or permission to replay an external effect. An uncertain
commit or a delayed postcommit journal drain must not cause automatic replay.

The configured duration is nominal from the final issuance sample. The check
establishes remaining life at the check, not an unconditional lifetime after
an unbounded commit or delayed client receipt. A minimum-at-commit guarantee
would additionally require a bound on the remaining commit delay and database
clock movement. Subsequent protected operations still revalidate authority.
Equality is required within each locked decision, not between every clock read
throughout an arbitrarily long transaction. Earlier conservative candidate
discovery may use an earlier sample, but the locked decision revalidates the
relevant eligibility conditions.

This amendment supersedes the original transaction-time/equality ruling,
the inline first-fence deadline requirement, the fence's SQLite formatting
exception, and corresponding consequences and clock pins below. Database
custody, aware UTC, separate Landscape/Sessions domains, forensic-clock
freedom, and the prohibition on caller-clock injection remain in force.
The implementation and verification plan is
[Landscape lease clocks](../../plans/2026-09-08-landscape-lease-clock.md).

### Evidence and limits

The original PostgreSQL proof held a vacant export seat for 82 seconds and
observed an 80-second lease already expired after acquisition returned. That
measures post-return residual life, not an exact commit timestamp or deployed
contention frequency. The independent follow-up reproduced pre-lock evaluation
of an inline fresh clock expression. These establish the defect and the need
for explicit ordering without requiring a production measurement campaign.

Leader-fenced database bodies hold the seat lock through completion, so expiry
alone cannot install another epoch during that body. This does not prove the
safety of every external-effect protocol. Journaling can extend the transaction
through precommit work; its postcommit drain occurs after the original seat lock
is released. The ACA web opening path does not enable that journal.

Required proofs cover server-observed lock waits, expiry crossed while waiting,
long leader and caller-owned bodies, intentional release, unchanged member
liveness, exact returned/event/persisted expiries, journal-delay rollback,
connection reuse and the distinction between transaction and decision time.
Static gates must prove clock provenance and operation ordering, and retain
their adversarial controls. The full default and serial PostgreSQL suites run
on the integrated implementation before closure.

## Original decision record — superseded where stated above

The remaining sections preserve the original C6 rationale and implementation
record. Their counts and future-tense instructions describe that historical
programme; they are not the current inventory or execution instructions.

## Original context

ADR-030 made the Landscape database the arbiter of run leadership, worker
liveness, scheduler leases, sink-effect leases and checkpoint fencing. Every
one of those decisions compares a stored deadline (`leader_heartbeat_expires_at`,
`heartbeat_expires_at`, `lease_expires_at`, `available_at`, `barrier_blocked_at`)
against "now". Today "now" is a **process clock**: either the caller passes
`now: datetime` into the repository verb (89 caller-clock forwardings and 23
caller-clock authority sites in the corpus), or the repository calls
`elspeth.core.landscape._helpers.now()` / `datetime.now(UTC)` itself (11
process-clock authority sites), and the first leader fence
(`verify_and_extend_leader_fence`) takes `now` as a parameter and writes
`now + timedelta(seconds=window_seconds)` as the seat's new expiry.

With one process per Landscape that is harmless: there is exactly one clock.
Release 0.8.0's goal (Phase 6/6b, Azure Container Apps, replicas > 1) puts
**two or more process clocks against one Landscape**. A replica whose clock
runs ahead extends its own seat further into the future than its peers
believe; a replica whose clock runs behind judges a live seat expired and
takes it over; a heartbeat written by one replica is read by another with a
different idea of "now". Nothing in the schema, the fence or the CAS
predicates can detect this, because every predicate is `stored_deadline < now`
with `now` supplied by whichever process is asking. This is the unmitigated
risk row the Q1(c) ruling names ("two process clocks meeting one Landscape").
The Sessions database already closed the same hole in the web tier: its
authority reads `SELECT CURRENT_TIMESTAMP` (SQLite) / `clock_timestamp()`
(PostgreSQL) once per locked transaction and derives every lease deadline and
CAS predicate from that value. The two databases are deliberately separate
clock domains, and the gate forbids either clock crossing into the other.

The gate is a scanner, not a policy document. Its four red ids pin:

1. `test_landscape_authority_has_no_process_or_caller_clock_ingress` — no
   authority verb takes or forwards a caller clock, no authority reads a
   process clock, no clock is injected; 146 findings today.
2. `test_every_clock_sensitive_decision_uses_landscape_database_time` — every
   deadline write and every deadline comparison on the four authority tables
   derives from database time; 34 decision sites today.
3. `test_first_leader_fence_statement_uses_database_time_and_full_token` — the
   first fence's exact shape (below).
4. `test_divergent_sessions_and_landscape_clocks_never_cross_production_fence`
   — behavioural: with the Sessions clock pinned to 2040-01-01, every
   Landscape family's written deadline lands within ±1 s of the Landscape
   database's own `CURRENT_TIMESTAMP`, and a takeover cannot be won with the
   Sessions clock.

Corpus by file (180 findings, `p4/c` at `ccb98a293`):

| file | findings |
|---|---|
| `core/landscape/run_coordination_repository.py` | 36 |
| `core/landscape/execution/sink_effect_lifecycle.py` | 35 |
| `core/landscape/scheduler/leases.py` | 28 |
| `core/landscape/scheduler_repository.py` | 23 |
| `core/landscape/scheduler/dispositions.py` | 14 |
| `core/landscape/scheduler/barrier.py` | 11 |
| `engine/orchestrator/resume.py` | 5 |
| `core/landscape/run_lifecycle_repository.py` | 5 |
| `engine/orchestrator/run_lifecycle.py` | 4 |
| `core/landscape/execution/sink_effect_finalization.py` | 4 |
| `engine/orchestrator/follower.py` | 3 |
| `core/landscape/scheduler/queue.py`, `scheduler/group_losses.py` | 2 each |
| `engine/orchestrator/join_admission.py`, `engine/orchestrator/heartbeat.py`, `core/landscape/scheduler/fencing.py`, `core/landscape/execution/source_completion_recovery.py`, `core/landscape/execution_repository.py`, `core/landscape/data_flow/tokens.py`, `core/checkpoint/recovery.py`, `core/checkpoint/manager.py` | 1 each |

By kind: caller-clock-forwarding 89, missing-database-time 34,
caller-clock-authority 23, process-clock-authority 11,
unresolved-clock-provenance 9, forensic-clock-authority 9,
caller-clock-positional-forwarding 2, injected-process-clock 1,
dynamic-authority-raw-sql 1, dynamic-authority-mapping 1.

Constraints the design must respect:

- The gate's first-fence contract is exact: `verify_and_extend_leader_fence(conn, *, token, window_seconds, verb)`
  with no clock-named parameter, whose first statement is one
  `conn.execute(update(run_coordination_table).where(<the three full-token predicates>).values(...))`,
  whose `leader_heartbeat_expires_at` value is one of
  `func.current_timestamp() + window_seconds`,
  `func.current_timestamp() + timedelta(seconds=window_seconds)`,
  `func.datetime(func.current_timestamp(), f"+{window_seconds} seconds")`, or
  an `IfExp` whose both arms are such expressions; followed by
  `if result.rowcount != 1: raise RunLeadershipLostError(...)`. Helper calls
  inside that statement are rejected as effectful, so the dialect switch must
  be written inline.
- `core/landscape` and `core/checkpoint` may not import anything from
  `elspeth.web.coordination` or `elspeth.web.sessions` (transitively). The
  Sessions helper cannot be reused; it must be re-implemented in the
  Landscape domain.
- Forensic timestamps (`created_at`, `occurred_at`, `recorded_at`,
  `timestamp`, `forensic_timestamp`) may keep the process clock. They may not
  be compared with an authority deadline or forwarded into an authority verb.
- Injected clocks (`now_fn=`, `clock=`) into authority code are rejected by the
  gate. `RunHeartbeatThread.now_fn` survives only for the thread's own wait
  scheduling and forensic `recorded_at`.
- No `xfail`, no `--deselect`, no assertion loosening (Phase 4 doctrine).

## Original decision

**Every custody, liveness, expiry, takeover and stale-owner decision against the
Landscape derives its "now" from the Landscape database, read inside the same
transaction that makes the decision. No authority verb takes, forwards, or
injects a clock. Tests control time by writing deadlines relative to database
time, never by passing time.**

Concretely:

1. **One read-once helper, in the Landscape domain.**
   `elspeth.core.landscape.database_clock.read_landscape_transaction_time(conn) -> datetime`
   executes `SELECT CURRENT_TIMESTAMP` on both dialects (SQLite: UTC text at
   1 s resolution; PostgreSQL: transaction start time at µs resolution),
   parses the result, and returns an aware UTC `datetime`. The returned value
   is threaded to the deadline writes and comparisons of the decision. It
   raises `NotImplementedError` for any other dialect and never imports from
   `elspeth.web`. The original locks-before-read requirement does not establish
   clock freshness on PostgreSQL: some callers read before locking, and even
   a read after locking still returns transaction-start time. SQLite's
   `BEGIN IMMEDIATE` acquires its write lock before the clock query.

   `CURRENT_TIMESTAMP` (not `clock_timestamp()`) is chosen on PostgreSQL so
   that the in-SQL fence expression (item 2) and the read-once value are the
   **same instant** within one transaction (ruled 2026-09-05, comment 9425
   on elspeth-0ff11aa42e). The Sessions authority uses `clock_timestamp()`;
   the two domains are separate and the difference is deliberate. SQLite
   Landscape writes use `BEGIN IMMEDIATE`; PostgreSQL uses its own transaction
   and row-lock semantics. Both require internally consistent decisions.

   **Timezone contract.** The helper returns an *aware UTC* `datetime` on
   both dialects: PostgreSQL `CURRENT_TIMESTAMP` is `timestamptz` and is
   converted with `astimezone(UTC)` (never `replace`), SQLite returns naive
   UTC text and is parsed then given `tzinfo=UTC`. This is the same
   contract the consolidated web helper carries
   (`src/elspeth/web/coordination/database_clock.py`, identity branch
   `df17f22e5`), and it is a measured defect class, not a nicety: the three
   web-side `_database_now` copies it replaces were not identical — on an
   aware non-UTC value (psycopg with a non-UTC session timezone) one
   converted and two returned `+10:00` unchanged, so every "UTC" log line
   and date bucket lied by the session offset while comparisons still
   agreed. The pin tests in C6.0 (Notes, below) cover an aware non-UTC
   input and a non-UTC session timezone.

   **Erratum, 2026-09-08: expiry predicates and deadline writes have different
   timing requirements.** PostgreSQL's `CURRENT_TIMESTAMP` is the transaction's
   start, even when queried after a lock wait. An early `now` delays the truth
   of `stored_deadline < now`; it does not make that expiry predicate fire
   early. Conversely, it can over-report "still live", including in
   `admit_follower` and the `seat_live` heartbeat projection. These verbs use
   multiple statements; their transaction age is not merely one round trip.

   The previous explanation incorrectly extended that conservative-predicate
   argument to deadline writes. A deadline of `transaction_start + window`
   has remaining life `window - transaction_age` at commit. Lock waits and
   work performed by the transaction body both consume this life. The default
   80-second window equals the documented sizing minimum, so the implementation
   cannot promise that full minimum remains after commit. A PostgreSQL proof
   observed an 80-second export lease already expired after an 82-second seat
   lock wait (`elspeth-8f97b3403e`; evidence also recorded in comment 9894 on
   `elspeth-0ff11aa42e`). This is a liveness defect, not evidence that a lock
   wait of that duration occurs in deployed workloads.

   Leader-fenced database mutations retain the seat-row lock through their
   transaction's commit or rollback. Takeover waits for that lock; expiry
   alone cannot install another epoch during the fenced body. Long bodies can
   nevertheless delay heartbeat and takeover and leave an expired deadline
   when they commit. External effects outside that transaction need their own
   effect-protocol evidence; the seat lock alone does not establish their
   safety. This erratum corrects the rationale without changing the selected
   clock. A deadline-freshness contract and its implementation remain subject
   to a separate architectural decision.

2. **The first fence writes database time in SQL.**
   `verify_and_extend_leader_fence(conn, *, token, window_seconds, verb)` loses
   `now`. Its values clause becomes, inline:

   ```python
   leader_heartbeat_expires_at=(
       func.datetime(func.current_timestamp(), f"+{window_seconds} seconds")
       if conn.dialect.name == "sqlite"
       else func.current_timestamp() + timedelta(seconds=window_seconds)
   ),
   updated_at=func.current_timestamp(),
   ```

   `fenced_leader_transaction(engine, *, token, window_seconds, verb)` loses
   `now`; its `fence_refusal` event keeps a forensic `recorded_at` from the
   process clock (allowed: it is recorded, never compared). The eleven
   `fenced_leader_transaction(...)` call sites (checkpoint manager, tokens,
   source-completion recovery, run coordination, run lifecycle ×2, barrier,
   dispositions ×2, fencing, group_losses ×2, queue) drop their `now=`.

3. **Every other authority verb loses its clock parameter and reads once.**
   Each verb in `_CLOCK_AUTHORITY_VERBS` and each `_SENSITIVE_SYMBOLS` entry
   opens (or receives) its transaction, calls `landscape_database_now(conn)`
   exactly once, and uses that value for `expires = database_now + timedelta(...)`,
   for `where(table.c.<deadline> < database_now)` predicates, and for
   post-fetch Python comparisons (`_utc(expires) >= database_now`). The
   grace-threshold forms (`now - timedelta(seconds=grace)`) become
   `database_now - timedelta(...)`. Verbs that today decide **outside** a
   transaction (for example `live_leader`, `worker_heartbeat`'s seat-liveness
   projection after the transaction closes) move the comparison inside, or
   carry the transaction's `database_now` out with the row. Barrier's dynamic
   raw SQL (`complete_barrier`) binds `database_now` as a parameter rather
   than interpolating any clock.

4. **Bound parameters, not in-SQL expressions, everywhere except the fence.**
   Outside the first fence, deadlines are written and compared as bound
   Python `datetime` values derived from `database_now`. This keeps every
   stored value in SQLAlchemy's own SQLite storage format
   (`YYYY-MM-DD HH:MM:SS.ffffff`) so text comparison stays well-ordered, and it
   keeps the churn to "replace `now` with `database_now`" at each site. The
   fence's SQLite `datetime()` text (`YYYY-MM-DD HH:MM:SS`, no fraction) is the
   single exception the gate forces; SQLAlchemy parses both forms, and the
   only artefact is that a fence-written expiry compares as expired within
   the final second of its window (text `"…:05"` sorts before `"…:05.000000"`).
   That is inside the gate's ±1 s tolerance and inside every liveness window
   (≥ 10 s) in the tree; it is documented on the helper and pinned by a test.

5. **Forwarders in the engine stop minting time.** `engine/orchestrator/*`
   (`resume`, `run_lifecycle`, `follower`, `join_admission`, `heartbeat`) and
   `core/checkpoint/*` stop passing `now=datetime.now(UTC)`; they call the
   verb without a clock. `RunHeartbeatThread` keeps `now_fn` for its own wait
   loop and for `record_heartbeat_degraded(recorded_at=...)`, which is
   forensic; `worker_heartbeat(worker_id=, window_seconds=)` carries no clock.
   `engine/executors/sink_effects.py`'s `clock` (poll budget for waiting on a
   foreign lease) is a process-local wait budget, not a custody decision, and
   is outside this ADR; the custody decision it waits on
   (`SinkEffectLifecycle.acquire_lease` / `takeover_expired`) moves to
   database time.

6. **Tests control time through the database, not through arguments.**
   `tests/fixtures/landscape.py` gains `landscape_database_now(db)`,
   `expire_leader_seat` (already present — re-based from `datetime.now(UTC)`
   onto database time), `expire_worker`, `expire_work_item_lease`,
   `expire_sink_effect_lease`, `set_available_at`, and
   `assert_deadline_within(deadline, seconds, before, after)` (the ±1 s window
   the gate's divergent-clock test already uses). A test that passed
   `now=AFTER_EXPIRY` to provoke a takeover writes the seat's expiry into the
   past and calls the verb; a test that asserted `expires_at == NOW + WINDOW`
   asserts the window instead. `MockClock` stays for engine poll budgets.

7. **The clock domains never cross, and the Landscape clock is a distinct
   authority.** Nothing under `core/landscape` or `core/checkpoint` imports
   the Sessions authority or the consolidated web helper
   `elspeth.web.coordination.database_clock` — the two clocks share a
   *contract* (aware UTC, read once per transaction, transaction time), not
   *code*; nothing passes a Sessions `database_now` into a Landscape verb or
   vice versa; the web tier's adapters call each authority with its own
   transaction and no time. The proof is already a gate, not prose:
   `_sessions_import_violations` in the clock-authority test fails any
   module under `core/landscape` or `core/checkpoint` that imports
   `elspeth.web.coordination` or `elspeth.web.sessions` directly, via
   `importlib`, or transitively, and the `cross-database-clock-comparison` /
   `cross-database-clock-forwarding` scanner kinds fail any comparison or
   forwarding between a Sessions-derived and a Landscape-derived time. Both
   are exercised by gate id 1. Consistency between the two helpers is proven
   by the shared pin tests (aware-UTC on both dialects, same value across two
   calls in one transaction) run against each helper, not by importing one
   from the other.

What this ADR does **not** do:

- It does not change any schema column or add a clock table.
- It does not touch forensic timestamps on `runs`, `nodes`, `tokens`,
  `node_states`, `calls`, `operations`, `batches`, `artifacts`, `journal`,
  `auth_audit` — those remain process-clock `created_at`/`recorded_at`
  facts.
- It does not introduce a `LandscapeClock` abstraction or any injection
  seam. The absence of a seam is the safety property.
- It does not add a `--deselect`; the four ids go green by implementation.

## Original consequences

### Positive Consequences

- Replicas with divergent wall clocks cannot extend, expire, or take over a
  seat or lease on the strength of their own clock; the database's single
  clock decides, and a skewed replica is simply early or late by its own
  skew, never authoritative.
- The first fence, the seat CAS, worker liveness, scheduler leases and
  sink-effect leases all become internally consistent within one
  transaction on PostgreSQL (`CURRENT_TIMESTAMP` is transaction time).
- The `now: datetime` parameter — the largest single source of caller-side
  "fail-open" freedom in the coordination API (a caller can pass any time) —
  disappears from the contract.
- The four red gate ids close by construction, and the gate keeps future
  ingress out.

### Negative Consequences

- SQLite `CURRENT_TIMESTAMP` has one-second resolution. Tests that asserted
  strict monotonic ordering of two deadlines written within the same second
  must assert `>=`, and exact-equality deadline assertions become window
  assertions. Liveness windows in the tree are ≥ 10 s, so production
  semantics do not change.
- Every authority verb takes one extra round trip (`SELECT CURRENT_TIMESTAMP`)
  inside its transaction. On SQLite in-process this is microseconds; on
  PostgreSQL it is one statement per write transaction that already holds a
  row lock.
- The test surface is large: 344 single-line `verb(..., now=...)` sites in 38
  test files were measured at `ccb98a293` (multi-line calls raise this toward
  the plan's ~560), plus the fixture helpers. This is mechanical but wide,
  and it is why the programme lands per family with its tests.
- `heartbeat.py`'s `RunHeartbeatThread` loses the ability to steer the
  *stored* deadline through `now_fn` in tests; those tests assert against
  database time windows instead.

### Neutral Consequences

- `elspeth.core.landscape._helpers.now()` stays, for forensic timestamps.
- `updated_at` on the coordination seat becomes database time from the fence
  and `database_now` elsewhere; it is forensic but harmlessly consistent.

## Original alternatives considered

### Alternative 1: Keep the caller clock and require replicas to run NTP

**Description:** Leave `now: datetime` in place and make clock discipline a
deployment requirement.

**Rejected because:** it leaves the fail-open freedom in the API (any caller
can pass any time), it cannot be verified by the gate or by the audit trail,
and the Sessions tier already rejected it for the same reason. Operational
clock discipline is not a substitute for the database being the arbiter.

### Alternative 2: In-SQL `func.current_timestamp()` expressions at every site

**Description:** Rewrite every predicate and values clause as SQL expressions
(`where(c.expires_at < func.current_timestamp())`).

**Rejected because:** on SQLite the deadline columns are text in SQLAlchemy's
microsecond format and `CURRENT_TIMESTAMP` is fraction-less text, so every
comparison would carry the boundary-second artefact rather than only the one
fence the gate forces into that shape; the Python-side comparisons after
fetch (`_utc(row.expires_at) >= now`) would still need a value; and the churn
per site is larger. The read-once helper is the Sessions precedent and the
gate's own positive control (`database_now = read_landscape_transaction_time(conn)`).

### Alternative 3: A `LandscapeClock` collaborator injected into repositories

**Description:** Abstract time behind an injected object so tests can
substitute a fake.

**Rejected because:** the injection seam is exactly the mechanism by which a
process clock reaches a decision; the gate flags injected clocks
(`injected-process-clock`) for that reason, and a fake that tests substitute
proves nothing about the production clock's provenance.

### Alternative 4: Ticketed `--deselect` of the four ids with a sunset

**Description:** Q1's second option.

**Rejected because:** ruled out — Q1(c) is EXECUTE (elspeth-d729c26729):
the risk is first exercised by the replicas > 1 goal the release exists for.

## Related Decisions

- ADR-030: Multi-Worker Deployment Shape — the coordination substrate whose
  clock this ADR fixes.
- ADR-026: Durable Token Scheduler — the lease/available_at vocabulary.
- ADR-032: Validate by Trust Domain — the "no injection seam" posture.

## References

- Gate: `tests/unit/core/landscape/test_database_clock_authority.py`
  (`_CLOCK_AUTHORITY_VERBS`, `_SENSITIVE_SYMBOLS`, `_first_fence_contract_violations`,
  `_deadline_expression_is_database_authoritative`).
- Sessions precedent: `src/elspeth/web/coordination/repository.py`
  `_database_now`, `_lock_fence_and_read_database_time`, `mutate`.
- Plan: `docs/plans/2026-09-04-release-0.8.0-phase4-burn-down-plan.md` §4 Lane C, §5 C6, §7 Q1.
- Tickets: elspeth-0ff11aa42e (C6), elspeth-d729c26729 (Q1(c) ruling).

## Notes — threading plan (implementation order, one visible commit per family)

Each commit: `mypy src/ elspeth-lints/src/` at 0, ruff, the four gate ids
rerun at `-n 0` (finding counts may only fall; the set is diffed), the
family's test files rerun, one named mutation with cp-roundtrip restore, and
the whole-tree AST gates when a helper is added. The behavioural oracle for
every family is `test_divergent_sessions_and_landscape_clocks_never_cross_production_fence`,
whose per-family arm goes green as that family lands.

| # | commit | production files | corpus | tests to re-express | mutation |
|---|---|---|---|---|---|
| C6.0 | `landscape_database_now(conn)` in `core/landscape/database.py`; dialect parse; docstring on the fence-text artefact | `core/landscape/database.py` | 0 (new) | pin tests: (i) aware non-UTC input (`+10:00`) → aware UTC result, and a PostgreSQL session with `SET TIME ZONE` non-UTC → aware UTC result; (ii) two calls inside one PostgreSQL transaction return the SAME value and differ from `clock_timestamp()` (the property the fence relies on); (iii) SQLite round-trip is aware UTC; (iv) unknown dialect raises; (v) fence-text artefact: a fence-written expiry and a `.ffffff` bound value for the same instant differ by < 1 s, and every production liveness/lease window constant is ≥ 10 s; (vi) positive control: the scanner classifies the helper as a database clock. PostgreSQL cases run under testcontainer (tests/testcontainer/core — the sessions-schema fixture defect elspeth-d0e62aea41 does not apply) | return `datetime.now(UTC)` → scanner classifies as process clock (gate id 1 grows); drop `astimezone(UTC)` → pin (i) fails |
| C6.1 | first fence + `fenced_leader_transaction` without `now`; 11 forwarders drop `now=` | `run_coordination_repository.py:244-320`, `checkpoint/manager.py`, `data_flow/tokens.py`, `execution/source_completion_recovery.py`, `run_lifecycle_repository.py`, `scheduler/{barrier,dispositions,fencing,group_losses,queue}.py` | ~15 | `test_leader_fence_stale_token.py`, `test_coordination_fence_constructs.py`, `test_run_coordination_repository.py` (fence cases), checkpoint tests | reintroduce `now` on the verifier → gate id 3 fails; write `now + timedelta` → `caller-or-process-deadline` |
| C6.2 | leadership + worker family | `run_coordination_repository.py` (register/acquire/release/live_leader/worker_heartbeat/admit_follower/depart/evict/dead_non_leader_workers/_insert_worker_row), `run_lifecycle_repository.py`, `checkpoint/recovery.py`, `engine/orchestrator/{resume,run_lifecycle,follower,join_admission,heartbeat}.py` | ~55 | `test_run_coordination_repository.py` (85 sites), `test_run_coordination_liveness.py`, `test_join_run_admission.py`, `test_evict_worker_housekeeping.py`, `test_run_coordination_release_postgres.py`, `e2e/recovery/*`, `tests/fixtures/landscape.py` (`expire_leader_seat`, `leader_coordination_token`) | in `_acquire_run_leadership_on` compare against `datetime.now(UTC)` → gate ids 1/2 and the divergent test's `takeover accepted Sessions clock` arm fail |
| C6.3 | scheduler family | `scheduler/leases.py`, `scheduler_repository.py`, `scheduler/dispositions.py`, `scheduler/barrier.py` (raw SQL binds `database_now`), `scheduler/queue.py`, `scheduler/group_losses.py`, `scheduler/fencing.py`, `data_flow/tokens.py` | ~80 | `test_scheduler_events.py`, `test_scheduler_lease_recovery_races.py`, `test_scheduler_lease_eviction_postgres.py`, `test_scheduler_repository_complete_barrier.py`, `test_lease_recovery_sweep.py`, `test_multi_source_foundation.py`, `test_count_ready_in_set.py`, property state machine | in `claim_ready` compare `available_at <= now()` → gate id 2 `scheduler` family fails |
| C6.4 | sink-effect family | `execution/sink_effect_lifecycle.py`, `execution/sink_effect_finalization.py`, `execution/source_completion_recovery.py`, `execution_repository.py` | ~40 | sink-effect lifecycle/finalization tests, `engine/test_sink_effect_lease_wait.py` (wait budgets keep `MockClock`), executor tests | in `acquire_lease` write `now() + ttl` → gate id 2 `effect` family fails |
| C6.5 | closure | — | 0 | all four gate ids green at `-n 0`; full `pytest tests/` on a frozen tree (hub GO); kwsweep rerun (no caller may omit a default-less kw-only parameter introduced here — none is; the change is deletion of `now`) | — |

Estimate: 60–90 h as planned (C6.2 and C6.3 carry most of the test surface).
C6 starts only after C1 → C2 → C4 land (lane C's critical path) and after
the hub replies to this design; the ADR moves to **Accepted** on C6.5.

**C6.5 closed at the 0.8.0 release (2026-09-07): C6.0–C6.4 landed, the gate's
four ids are green at `-n 0`, and the programme ticket elspeth-0ff11aa42e
closed 2026-09-06. This ADR is Accepted; the plan above is retained as the
implementation record.**

Original rulings (2026-09-05, hub comment 9425 on elspeth-0ff11aa42e; superseded
for fresh lease decisions by the 2026-09-08 amendment above; no merge-writer
veto):

1. `CURRENT_TIMESTAMP` (transaction time) on PostgreSQL — **confirmed**; the
   in-SQL fence write and the read-once value being the same instant is the
   property that matters. The Sessions clock stays on `clock_timestamp()`;
   the domains are distinct authorities that never cross (item 7 carries the
   import-boundary proof).
2. `updated_at` on `run_coordination` / `run_workers` → database time —
   **yes**.
3. The SQLite fraction-less `datetime()` fence text — **accepted as
   documented**; no normalising second `UPDATE` (the gate would rightly flag
   the extra effect); pin test (v) above.
4. Added requirements: the aware-UTC contract with its pin tests (item 1),
   the conservative-direction argument with its named exception (item 1),
   and the same-value-across-two-calls pin (C6.0 (ii)).

Typing note for the implementation (hook rule since `c63bed73a`): the
contracts-check hook refuses a new `dict[str, Any]` return type, so any
receipt or probe result C6 introduces (for example the fixture helpers'
"before/after" windows) is a frozen dataclass or a `TypedDict`, never a
dict.
