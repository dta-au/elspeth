# Landscape Lease Clock Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Issue Landscape lease deadlines from fresh locked decisions and refuse completion when the transaction consumes their remaining reserve.

**Architecture:** Keep family-owned deadline DML explicit. A shared read-only commit guard checks the exact deadlines issued by the outer transaction after journal serialization. Preserve leader, member, item and effect authority distinctions, returned expiries and audit consistency.

**Tech Stack:** Python, SQLAlchemy Core, PostgreSQL, SQLite, pytest and source-analysis gates.

**Prerequisites:**

- Operator approval recorded on elspeth-8f97b3403e, comment 9902, supersedes the original transaction-time ruling for locked lease decisions.
- Isolated branch `delivery/replica-aca-lease-clock`, based on `e5f5a54713d2758647a5b9831ffd667b0f841250`. Integrate into `delivery/replica-aca`; do not switch or merge the shared release checkout.
- Read `AGENTS.md`, `CONTRIBUTING.md` whole-tree gates and amended ADR-047. Use the existing virtual environment with both worktree source roots; do not reinstall dependencies.
- PostgreSQL contention tests require disposable Docker databases. Test processes must finish and their actual exit codes must be recorded.

## Contract and sequencing

For a locked decision, acquire authority and target rows in the established order, then call `read_landscape_decision_time(conn)`. Bind that single value into the associated eligibility checks, CAS, expiry and timestamps. Candidate discovery can precede locking, but cannot supply the final decision's time.

The shared API lives in `src/elspeth/core/landscape/lease_deadlines.py`:

```python
from datetime import datetime

from sqlalchemy import Connection

from elspeth.core.landscape.lease_deadlines import (
    DeadlineKey,
    DeadlineKind,
    record_issued_deadline,
)


def register_item_deadline(
    conn: Connection,
    work_item_id: str,
    expires_at: datetime,
    window_seconds: float,
) -> None:
    record_issued_deadline(
        conn,
        key=DeadlineKey(DeadlineKind.ITEM, (work_item_id,)),
        expires_at=expires_at,
        window_seconds=window_seconds,
    )
```

This example describes registration after successful deadline DML, not a new production wrapper. Other keys are LEADER `(run_id, worker_id, str(epoch))`, WORKER `(run_id, worker_id)` and SINK_EFFECT `(effect_id,)`. The leader key identifies the particular grant so an old outer context cannot renew a new grant following intentional release. Full-token SQL remains the authority proof. `forget_issued_deadline(conn, *, key)` cancels an intentionally ended obligation; `has_issued_deadline(conn, *, key)` identifies an obligation actually created by this transaction. Registration replaces the same key with its latest exact persisted expiry.

The completion threshold is `min(1 second, 10% of effective nominal window)`, where the window is the duration actually added to the sample after any dialect/TTL alignment. Validate finite positive durations and a representable reserve at the fresh clock's resolution. This is a reserve check at the last controlled boundary, not a fixed lifetime guarantee after commit. An owned `LeaseDeadlineExpiredError`, a `TimeoutError` subtype, denotes consumed reserve. It does not impersonate lost membership or database corruption.

Required order:

```text
authority admission and locks
→ fresh temporal decision and family writes
→ caller body
→ explicit continuing-seat or paired-deadline finalization
→ journal enrichment, serialization and outbox insertion
→ read-only reserve check
→ DBAPI commit
→ existing postcommit journal drain
```

The guard owns no lease-renewal callback and retries no body. Registration is tied to the root transaction, rejected inside savepoints and cleared on every exit. A raised commit-listener exception must demonstrably roll back the DBAPI transaction; SQLAlchemy bookkeeping alone is insufficient evidence.

## Task 1: Shared clock and completion guard

**Files:**

- Modify `src/elspeth/core/landscape/database_clock.py`, `database.py`, `journal.py`.
- Create `src/elspeth/core/landscape/lease_deadlines.py`.
- Create `tests/unit/core/landscape/test_lease_deadline_guard.py` and `tests/testcontainer/core/test_lease_deadline_guard_postgres.py`.
- Update interface doubles in `tests/unit/core/landscape/test_database_compatibility_guards.py` and `test_journal.py` to model real Engine/Connection behavior used by listener installation and invalidation cleanup.

1. Add failing tests for fresh versus transaction time, aware UTC, dialect/result rejection, reserve equality, replacement/cancellation, savepoint refusal, bare engines, pool reuse and journal-delay rollback.
2. Run the new tests and record the actual failures before implementation.
3. Implement the separate fresh helper: PostgreSQL `clock_timestamp()`, SQLite database `strftime` with milliseconds, both normalized to aware UTC. Retain the old transaction helper as a distinct contract.
4. Implement immutable typed obligations and the read-only late Engine commit guard. Install after the journal listener and cover bare engines accepted by `begin_write` and caller-owned connections. Prove rollback and cleanup on every supported failure path.
5. Run the new tests, existing database/journal tests, Ruff and relevant whole-tree checks. Independently review listener ordering and cleanup, then commit exact owned paths.

**Done when:** the guard refuses stale completion with no committed payload/outbox, successful transactions preserve exact expiries, and a reused connection has no stale obligation or transaction.

## Task 2: Coordination owners

**Files:**

- Modify `src/elspeth/core/landscape/run_coordination_repository.py` and `run_lifecycle_repository.py`.
- Modify `src/elspeth/engine/orchestrator/heartbeat.py` and `tests/unit/engine/orchestrator/test_run_heartbeat_thread.py`.
- Update equality fixtures in `tests/unit/core/landscape/test_run_coordination_repository.py` and `tests/testcontainer/core/test_run_coordination_release_postgres.py`, preserving strict equality refusal and unchanged-state assertions with database-derived decision instants.
- Create `tests/unit/core/landscape/test_coordination_deadline_finalization.py` and `tests/testcontainer/core/test_coordination_deadline_finalization_postgres.py`.

1. Add real server-observed seat-lock waits for takeover, export acquisition and heartbeat; a long leader body; the outer `begin_run` mint body; and follower admission crossing expiry while waiting.
2. Verify these fail for stale timing, while member-no-renewal, release-no-resurrection and body-rollback controls pass.
3. Keep the direct full-token identity-preserving first UPDATE and immediate cardinality refusal. Sample after it and renew explicitly. At successful leader-context exit, renew only the continuing registered seat with the same token and retained lock.
4. Issue registration/acquisition/leader-heartbeat seat and worker deadlines from one final sample. Handle the actual `begin_run` outer owner. Follower admission rechecks the foreign seat after waiting and never renews it. Move worker eviction decisions after their target lock.
5. Register successful writes and cancel intentional release. Classify the exact owned reserve-timeout subtype as heartbeat liveness-unknown/degraded alongside database contention; preserve fatal handling of unexpected exceptions and stop-timeout behavior. Test that no lost-membership/fatal latch is set and a later beat can recover.
6. Run focused coordination and PostgreSQL proofs, review all actual outer compositions, and commit.

**Done when:** new authority does not lose its window to admission waits/body age, release stays released, arbitrary fenced writes do not refresh membership, and competing epochs remain blocked through the leader body.

## Task 3: Scheduler claims and expiry consumers

**Files:**

- Modify `src/elspeth/core/landscape/scheduler/leases.py` and `queue.py`.
- Modify `src/elspeth/engine/scheduler_drain.py` and create `tests/unit/engine/test_scheduler_deadline_refusal.py`.
- Modify `tests/testcontainer/core/test_scheduler_lease_eviction_postgres.py` only to recognize the new earlier item-lock boundary while preserving refusal assertions.
- Update `tests/testcontainer/core/test_item_generation_fence_postgres.py` for recovery's earlier membership-row blocker; retain stale-generation refusal and full state assertions, and rerun its actual weakened-CAS mutation.
- Update equality/tied-event fixtures in `tests/unit/core/landscape/test_scheduler_lease_recovery_races.py` and `test_scheduler_events.py`; retain non-reap equality and tied-event sequence assertions.
- Create `tests/unit/core/landscape/test_scheduler_deadline_completion.py` and `tests/testcontainer/core/test_scheduler_decision_clock_postgres.py`.

1. Add failing READY, PENDING_SINK and heartbeat tests with server-observed item blockers, expiry crossing during recovery, and outer enqueue/ingest tails consuming a claim's reserve.
2. Record expected stale-deadline/recovery failures and retain stale-generation and exact snapshot controls.
3. Establish membership then target-item locks before fresh issuance. Recovery may discover candidates conservatively, then lock owners/items in stable order and re-evaluate all applicable eligibility predicates with one fresh sample.
4. Register exact successful ITEM deadlines. Keep immutable item/scalar results and event expiries equal to persisted values; refuse an overlong outer tail. Enumerate actual claim-to-clear compositions before adding cancellation elsewhere. The drain propagates the exact operational timeout without retrying a failed heartbeat, emitting a result or writing a plugin-failure disposition; it must not invent lost ownership or wrap the timeout as corruption.
5. Run focused scheduler, recovery, ingest and PostgreSQL tests, independently review lock order and generation predicates, and commit.

**Done when:** all three writers sample after their locks, recovery sees expiry crossed on its locked candidates, stale attempts still fail, and no automatic finalizer silently changes a returned item.

## Task 4: Sink-effect leases

**Files:**

- Modify `src/elspeth/core/landscape/execution/sink_effect_lifecycle.py` and `sink_effect_finalization.py`.
- Create `tests/unit/core/landscape/test_sink_effect_deadline_completion.py` and `tests/testcontainer/core/test_sink_effect_decision_clock_postgres.py`.
- Create `tests/unit/engine/test_sink_effect_deadline_propagation.py` and update the precision-dependent assertion in `tests/unit/engine/test_sink_effect_lease_wait.py` while preserving lease-wait and ownership checks.

1. Add failing target-lock delay and expiry-crossing tests for preparation, acquisition, heartbeat, takeover and finalization.
2. Verify the failures expose stale timing while owner/generation controls remain intact.
3. Use one fresh post-lock sample for each decision. Register only successful issuance/renewal, and preserve RESERVED revival versus strict IN_FLIGHT refusal. Include paired expiry consumers in these files.
4. Keep `SinkEffectLease.expires_at`, persisted expiry and relevant event payloads equal. Cancel an intentionally ended obligation only where it can exist in the same transaction. Review timeout consumers in `src/elspeth/engine/scheduler_drain.py` and `src/elspeth/engine/executors/sink_effects.py`; intentional propagation can be correct, but the new timeout must not silently authorize external replay.
5. Run focused effect and PostgreSQL tests, independently review protocol semantics, and commit.

**Done when:** lock waits do not consume freshly issued deadlines, expired IN_FLIGHT operations refuse, and returned/audited authority matches committed data.

## Task 5: Enforcement and integrated verification

**Files:**

- Modify `tests/unit/core/landscape/test_database_clock_authority.py` and `tests/unit/architecture/test_web_landscape_mutation_fencing.py`.
- Update measured inventories in these gates from the final production source; modify `scripts/fencing_inventory.py` only if its reporting requires an explicit new category.
- Amend `docs/architecture/adr/047-landscape-database-clock-authority.md`.

1. Add adversarial cases before accepting new shapes: forged/shadowed helper, pre-lock sample, inline fresh UPDATE, wrong connection/subject, reused discovery sample, skipped finalization and improper membership renewal.
2. Separate first authority admission, fresh locked decision and normal-success completion proofs. Preserve existing process/Sessions ingress, mapping, executemany, raw SQL, alias and wrapper controls.
3. Integrate the shared and family commits in the isolated clock worktree. Recompute exact DML/caller/subordinate/clock inventories from this source and review every changed identity; do not merely expand accepted function names.
4. Complete independent specification review followed by code-quality review. Exercise meaningful actual mutants for admission, post-lock sampling, reserve guard and intentional release where static controls alone do not prove behavior.
5. Integrate into `delivery/replica-aca`, freeze a clean HEAD, then run the full default and serial PostgreSQL suites, Ruff/format, mypy, key-free lint comparison and affected install/frontend checks. Reuse prior evidence only with verified unchanged inputs and report that binding explicitly.
6. Re-query Git and tracker state, record precise completed evidence on elspeth-8f97b3403e, and close only after integrated verification. No push, shared release merge, cloud operation or signing-key work is part of this plan.

**Done when:** the integrated production paths, permanent enforcement and independent review all support the amended contract, and required completed checks have recorded exit codes. The deliberate global signing gate remains a separately reported condition.

## Commands and evidence

Run from the relevant worktree, using its resolved absolute path without committing a user-specific path:

```bash
cd "$(git rev-parse --show-toplevel)" && \
  export PYTHONPATH="$PWD/src:$PWD/elspeth-lints/src"
.venv/bin/python -c 'import elspeth, elspeth_lints; print(elspeth.__file__, elspeth_lints.__file__)'
.venv/bin/python -m pytest tests/unit/core/landscape/test_lease_deadline_guard.py -n 0 > /tmp/lease-clock-guard-unit.log 2>&1
lease_test_exit=$?
printf '%s\n' "$lease_test_exit" > /tmp/lease-clock-guard-unit.exit
```

Substitute the exact task test module for focused runs. PostgreSQL cases require `-m testcontainer -n 0`. Each lane uses its own log names. A failing RED run must fail at the intended assertion, not because a fixture or dependency is broken. GREEN requires process completion, exit 0 and inspection of the result.

Integrated commands, each with its own output and exit capture:

```bash
.venv/bin/python -m pytest tests/
.venv/bin/python -m pytest tests/ -m testcontainer -n 0
.venv/bin/ruff check src tests elspeth-lints
.venv/bin/ruff format --check src tests elspeth-lints
.venv/bin/mypy src/ elspeth-lints/src/
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/elspeth-lints check --rules all --root src/elspeth
```

The key-free gate's known red corpus must be compared with the frozen base; exit 1 alone neither identifies a regression nor authorizes clearing signatures. Preserve unrelated dirty worktrees and the ignored scratch review/artifacts throughout.
