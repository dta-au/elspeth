"""Characterization tests for multi-item expired-lease recovery sweeps.

These tests pin the CURRENT behavior of ``TokenSchedulerRepository.
recover_expired_leases`` against a real Tier-1 SQLite engine when the sweep
faces a POPULATION of leases rather than a single item (filigree
elspeth-0bae6d8a52):

1. A sweep by a fresh ``lease_owner`` recovers every expired lease in one
   call — exactly once each, with an attempt bump and ``work_item_id``
   rotation — and never touches a live (unexpired) lease.
2. Recovery order is the deterministic 3-key ORDER BY
   ``(ingest_sequence, step_index, work_item_id)`` — including the
   ``work_item_id`` last-resort tiebreaker for exact same-key collisions
   (the same determinism contract as ``claim_ready``, filigree
   elspeth-6cb89db535).
3. The G1 self-steal guard extends PAST lease expiry: an expired lease is
   invisible to its own holder's sweep, even while that same sweep recovers
   other owners' expired leases. Recovery therefore requires a DIFFERENT
   ``lease_owner`` — the resume-sweep path, not in-run self-recovery.

Slice-4 liveness-aware reap tests (§A.5/§C.1, design :140/221-224):

4. A registry-LIVE owner's expired item lease is REVIVED, not reaped —
   the N=1 improvement that makes long LLM calls safe against racing sweeps.
5. A registry-DEAD owner's expired item lease IS reaped under all three
   dead-owner arms: (a) absent row, (b) status='evicted'/'departed',
   (c) status='active' + stale heartbeat.
6. The stall budget arm reaps a live-heartbeat-but-wedged owner and emits
   ``worker_stalled`` in the same transaction.
7. A leader sweep recovers expired leases from multiple departed workers
   through the same registered-membership lifecycle as production.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, insert, select, update

from elspeth.contracts import NodeType
from elspeth.contracts.coordination import (
    DEFAULT_ITEM_STALL_BUDGET_SECONDS,
    DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
    CoordinationToken,
    WorkerMembershipToken,
)
from elspeth.contracts.scheduler import SchedulerEventType, TokenWorkItem, TokenWorkStatus
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.landscape.database import LandscapeDB, Tier1Engine
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    metadata,
    nodes_table,
    rows_table,
    run_coordination_events_table,
    run_coordination_table,
    run_workers_table,
    runs_table,
    scheduler_events_table,
    token_work_items_table,
    tokens_table,
)
from tests.fixtures.landscape import expire_lease, landscape_database_now

RUN_ID = "run-lease-sweep"


def _make_scheduler_engine() -> Tier1Engine:
    engine = create_engine("sqlite:///:memory:", echo=False)
    LandscapeDB._configure_sqlite(engine)
    LandscapeDB._verify_sqlite_pragmas(engine, "sqlite:///:memory:")
    metadata.create_all(engine)
    return Tier1Engine(engine)


def _row_payload_json() -> str:
    return TokenSchedulerRepository.serialize_row_payload(PipelineRow({"id": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True)))


def _insert_run_and_nodes(engine: Tier1Engine, *, now: datetime, leader_worker_id: str = "resume-sweeper") -> CoordinationToken:
    coordination = RunCoordinationRepository(engine)
    with engine.begin() as conn:
        conn.execute(
            insert(runs_table).values(
                run_id=RUN_ID,
                started_at=now,
                config_hash="config",
                settings_json="{}",
                canonical_version="v1",
                status="running",
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        )
        token = coordination.register_run_leader_on(conn, run_id=RUN_ID, worker_id=leader_worker_id, window_seconds=3600)
        for node_id, node_type, plugin in (
            ("source-a", NodeType.SOURCE, "csv"),
            ("normalize", NodeType.TRANSFORM, "identity"),
        ):
            conn.execute(
                insert(nodes_table).values(
                    run_id=RUN_ID,
                    node_id=node_id,
                    plugin_name=plugin,
                    node_type=node_type.value,
                    plugin_version="1.0",
                    determinism="deterministic",
                    config_hash="config",
                    config_json="{}",
                    registered_at=now,
                )
            )

    return token


def _insert_row_with_tokens(
    engine: Tier1Engine,
    *,
    row_id: str,
    ingest_sequence: int,
    token_ids: tuple[str, ...],
    now: datetime,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(rows_table).values(
                row_id=row_id,
                run_id=RUN_ID,
                source_node_id="source-a",
                row_index=ingest_sequence,
                source_row_index=ingest_sequence,
                ingest_sequence=ingest_sequence,
                source_data_hash=f"hash-{row_id}",
                created_at=now,
            )
        )
        for token_id in token_ids:
            conn.execute(
                insert(tokens_table).values(
                    token_id=token_id,
                    row_id=row_id,
                    run_id=RUN_ID,
                    created_at=now,
                )
            )


def _enqueue_single_token_rows(
    repo: TokenSchedulerRepository,
    engine: Tier1Engine,
    token_ids: tuple[str, ...],
    *,
    member_token: WorkerMembershipToken,
    now: datetime,
) -> dict[str, TokenWorkItem]:
    """One row + one token per entry, ingest_sequence in tuple order."""
    payload = _row_payload_json()
    items: dict[str, TokenWorkItem] = {}
    for ingest_sequence, token_id in enumerate(token_ids):
        row_id = f"row-{ingest_sequence}"
        _insert_row_with_tokens(engine, row_id=row_id, ingest_sequence=ingest_sequence, token_ids=(token_id,), now=now)
        items[token_id] = repo.enqueue_ready(
            member_token=member_token,
            token_id=token_id,
            row_id=row_id,
            node_id="normalize",
            step_index=1,
            ingest_sequence=ingest_sequence,
            row_payload_json=payload,
        )
    return items


def _work_item_states(engine: Tier1Engine) -> dict[str, dict[str, object]]:
    with engine.connect() as conn:
        return {
            row["token_id"]: dict(row)
            for row in conn.execute(
                select(
                    token_work_items_table.c.token_id,
                    token_work_items_table.c.work_item_id,
                    token_work_items_table.c.status,
                    token_work_items_table.c.attempt,
                    token_work_items_table.c.lease_owner,
                ).where(token_work_items_table.c.run_id == RUN_ID)
            ).mappings()
        }


def _recovery_events(engine: Tier1Engine) -> list[dict[str, object]]:
    """Recovery events in insertion order.

    All events of one sweep share a single ``recorded_at`` and ``event_id`` is
    a non-monotonic opaque id, so SQLite's rowid is the only durable witness
    of the sweep's per-item iteration order.
    """
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                select(scheduler_events_table)
                .where(scheduler_events_table.c.run_id == RUN_ID)
                .where(scheduler_events_table.c.event_type == SchedulerEventType.RECOVER_EXPIRED_LEASE.value)
                .order_by(scheduler_events_table.c.seq)
            ).mappings()
        ]


def test_sweep_recovers_every_expired_lease_exactly_once_and_never_live_leases() -> None:
    """A fresh-owner sweep over 3 expired + 2 live leases recovers exactly the
    3 expired items in one call — each exactly once with attempt bump and
    work_item_id rotation — leaves the live leases untouched, and is
    idempotent across repeated calls."""
    engine = _make_scheduler_engine()
    repo = TokenSchedulerRepository(engine)
    now = landscape_database_now(engine)
    leader = _insert_run_and_nodes(engine, now=now)
    worker = _admit_worker(engine, worker_id="worker-a")

    token_ids = ("token-0", "token-1", "token-2", "token-3", "token-4")
    originals = _enqueue_single_token_rows(repo, engine, token_ids, member_token=leader.membership, now=now)

    # claim_ready admits in ingest_sequence order, so the Nth claim leases
    # token-N. Tokens 0/2/4 get a 30s lease (expired at sweep time); tokens
    # 1/3 get a 3600s lease (still live at sweep time).
    expired_tokens = ("token-0", "token-2", "token-4")
    live_tokens = ("token-1", "token-3")
    lease_seconds_by_token = {"token-0": 30, "token-1": 3600, "token-2": 30, "token-3": 3600, "token-4": 30}
    for token_id in token_ids:
        claimed = repo.claim_ready(member_token=worker, lease_owner="worker-a", lease_seconds=lease_seconds_by_token[token_id])
        assert claimed is not None
        assert claimed.token_id == token_id
        if token_id in expired_tokens:
            expire_lease(engine, claimed.work_item_id)

    RunCoordinationRepository(engine).depart_worker(member_token=worker)
    assert repo.recover_expired_leases(coordination_token=leader) == 3

    states = _work_item_states(engine)
    for token_id in expired_tokens:
        assert states[token_id]["status"] == TokenWorkStatus.READY.value
        assert states[token_id]["attempt"] == 2
        assert states[token_id]["lease_owner"] is None
        assert states[token_id]["work_item_id"] != originals[token_id].work_item_id
    for token_id in live_tokens:
        assert states[token_id]["status"] == TokenWorkStatus.LEASED.value
        assert states[token_id]["attempt"] == 1
        assert states[token_id]["lease_owner"] == "worker-a"
        assert states[token_id]["work_item_id"] == originals[token_id].work_item_id

    # Exactly one recovery event per expired item, each bumping 1 -> 2.
    events = _recovery_events(engine)
    assert sorted(str(event["token_id"]) for event in events) == sorted(expired_tokens)
    assert all(event["from_attempt"] == 1 and event["to_attempt"] == 2 for event in events)
    assert all(event["caller_owner"] == "resume-sweeper" for event in events)

    # Idempotent: a second sweep finds nothing left to recover.
    assert repo.recover_expired_leases(coordination_token=leader) == 0
    assert len(_recovery_events(engine)) == 3

    # The recovered continuations are claimable in ingest order; the live
    # leases still block their own tokens.
    reclaimed: list[str] = []
    while True:
        item = repo.claim_ready(member_token=leader.membership, lease_owner="resume-sweeper", lease_seconds=300)
        if item is None:
            break
        assert item.attempt == 2
        reclaimed.append(item.token_id)
    assert reclaimed == list(expired_tokens)


def test_sweep_recovery_order_is_ingest_sequence_then_step_index_then_work_item_id() -> None:
    """Multi-item recovery walks expired leases in the deterministic 3-key
    order (ingest_sequence, step_index, work_item_id) — the work_item_id
    last-resort tiebreaker resolves exact same-key collisions, mirroring the
    claim_ready determinism contract (elspeth-6cb89db535)."""
    engine = _make_scheduler_engine()
    repo = TokenSchedulerRepository(engine)
    now = landscape_database_now(engine)
    payload = _row_payload_json()
    leader = _insert_run_and_nodes(engine, now=now)
    worker = _admit_worker(engine, worker_id="worker-a")

    # Fork-family shape: three sibling tokens on row-0 (ingest_sequence 0) —
    # token-y/token-z collide exactly on (ingest_sequence=0, step_index=1),
    # token-w trails at step_index=2 — plus token-c on row-1 (ingest_sequence 1).
    _insert_row_with_tokens(engine, row_id="row-0", ingest_sequence=0, token_ids=("token-w", "token-y", "token-z"), now=now)
    _insert_row_with_tokens(engine, row_id="row-1", ingest_sequence=1, token_ids=("token-c",), now=now)
    items: dict[str, TokenWorkItem] = {}
    for token_id, row_id, step_index, ingest_sequence in (
        ("token-w", "row-0", 2, 0),
        ("token-y", "row-0", 1, 0),
        ("token-z", "row-0", 1, 0),
        ("token-c", "row-1", 1, 1),
    ):
        items[token_id] = repo.enqueue_ready(
            member_token=leader.membership,
            token_id=token_id,
            row_id=row_id,
            node_id="normalize",
            step_index=step_index,
            ingest_sequence=ingest_sequence,
            row_payload_json=payload,
        )

    for _ in range(4):
        claimed = repo.claim_ready(member_token=worker, lease_owner="worker-a", lease_seconds=30)
        assert claimed is not None
        expire_lease(engine, claimed.work_item_id)

    RunCoordinationRepository(engine).depart_worker(member_token=worker)
    assert repo.recover_expired_leases(coordination_token=leader) == 4

    tied_pair = sorted(("token-y", "token-z"), key=lambda token_id: items[token_id].work_item_id)
    recovery_order = [event["token_id"] for event in _recovery_events(engine)]
    assert recovery_order == [*tied_pair, "token-w", "token-c"]


def test_expired_lease_is_invisible_to_its_own_holders_sweep() -> None:
    """G1 self-steal guard extends past expiry: a sweep recovers other owners'
    expired leases but NEVER the caller's own — even when the caller's lease is
    itself expired. The wedged item is recovered only when a DIFFERENT
    lease_owner (the resume-sweep path) runs the sweep."""
    engine = _make_scheduler_engine()
    repo = TokenSchedulerRepository(engine)
    now = landscape_database_now(engine)
    leader = _insert_run_and_nodes(engine, now=now, leader_worker_id="worker-a")
    worker = _admit_worker(engine, worker_id="worker-b")

    _enqueue_single_token_rows(repo, engine, ("token-0", "token-1"), member_token=leader.membership, now=now)

    # claim_ready admits in ingest order: worker-a leases token-0, worker-b
    # leases token-1. Both leases expire before the sweep.
    claimed_a = repo.claim_ready(member_token=leader.membership, lease_owner="worker-a", lease_seconds=30)
    claimed_b = repo.claim_ready(member_token=worker, lease_owner="worker-b", lease_seconds=30)
    assert claimed_a is not None and claimed_a.token_id == "token-0"
    assert claimed_b is not None and claimed_b.token_id == "token-1"
    # Exceed the stall budget so registry liveness alone cannot explain why
    # the leader's own expired item remains invisible to its recovery sweep.
    expire_lease(engine, claimed_a.work_item_id, seconds_ago=_STALL_BUDGET + 1)
    expire_lease(engine, claimed_b.work_item_id)
    RunCoordinationRepository(engine).depart_worker(member_token=worker)

    # worker-a's sweep recovers ONLY worker-b's expired lease; its own expired
    # lease stays LEASED under worker-a (invisible to its own holder).
    assert repo.recover_expired_leases(coordination_token=leader) == 1
    states = _work_item_states(engine)
    assert states["token-1"]["status"] == TokenWorkStatus.READY.value
    assert states["token-1"]["attempt"] == 2
    assert states["token-0"]["status"] == TokenWorkStatus.LEASED.value
    assert states["token-0"]["attempt"] == 1
    assert states["token-0"]["lease_owner"] == "worker-a"

    # Repeating its own sweep never reaps it.
    assert repo.recover_expired_leases(coordination_token=leader) == 0

    # An actual successor takes the expired seat and evicts the old leader.
    expired_at = landscape_database_now(engine) - timedelta(seconds=1)
    with engine.begin() as conn:
        conn.execute(
            update(run_coordination_table).where(run_coordination_table.c.run_id == RUN_ID).values(leader_heartbeat_expires_at=expired_at)
        )
    leader = RunCoordinationRepository(engine).acquire_run_leadership(run_id=RUN_ID, worker_id="resume-sweeper", window_seconds=3600)
    assert leader.leader_epoch == 2

    # A different lease_owner — the resume-sweep identity — recovers it.
    assert repo.recover_expired_leases(coordination_token=leader) == 1
    states = _work_item_states(engine)
    assert states["token-0"]["status"] == TokenWorkStatus.READY.value
    assert states["token-0"]["attempt"] == 2
    assert states["token-0"]["lease_owner"] is None

    # No recovery event was ever attributed to the lease's own holder.
    events = _recovery_events(engine)
    assert [(event["token_id"], event["caller_owner"], event["from_lease_owner"]) for event in events] == [
        ("token-1", "worker-a", "worker-b"),
        ("token-0", "resume-sweeper", "worker-a"),
    ]


# =============================================================================
# Slice-4 liveness-aware reap tests (§A.5/§C.1)
# =============================================================================

_LEASE_SECONDS = 30
_SWEEP_GRACE = DEFAULT_RUN_LIVENESS_WINDOW_SECONDS  # 80 s
_STALL_BUDGET = DEFAULT_ITEM_STALL_BUDGET_SECONDS  # 600 s


def _admit_worker(engine: Tier1Engine, *, worker_id: str) -> WorkerMembershipToken:
    return RunCoordinationRepository(engine).admit_follower(run_id=RUN_ID, worker_id=worker_id, config_hash="config", window_seconds=3600)


def _claim_and_expire(
    repo: TokenSchedulerRepository,
    engine: Tier1Engine,
    *,
    member_token: WorkerMembershipToken,
    expired_seconds_ago: float = 1.0,
) -> str:
    """Claim the first READY item and age its lease ``expired_seconds_ago`` into the database's past; return token_id."""
    item = repo.claim_ready(member_token=member_token, lease_owner=member_token.worker_id, lease_seconds=_LEASE_SECONDS)
    assert item is not None
    expire_lease(engine, item.work_item_id, seconds_ago=expired_seconds_ago)
    return item.token_id


def _coordination_events(engine: Tier1Engine, *, run_id: str, event_type: str) -> list[dict[str, object]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(run_coordination_events_table)
            .where(run_coordination_events_table.c.run_id == run_id)
            .where(run_coordination_events_table.c.event_type == event_type)
            .order_by(run_coordination_events_table.c.seq)
        ).mappings()
    return [dict(r) for r in rows]


def test_live_registered_owner_expired_lease_is_revived_not_reaped() -> None:
    """§A.5 / §C.1 — N=1 WIN: a registry-LIVE owner's expired item lease is
    left LEASED (owner_registry_dead is False) so the owner's next
    heartbeat_lease call can revive it. Long LLM calls are no longer reapable
    by a racing maintenance sweep.

    Setup: leader sweeper (token), peer worker-alive has a FRESH heartbeat.
    Item is leased under worker-alive with an expired lease_expires_at.
    Sweep by leader must return 0 (not reaped).
    """
    engine = _make_scheduler_engine()
    now = landscape_database_now(engine)
    repo = TokenSchedulerRepository(engine)

    leader_id = "leader-sweeper"
    live_owner = "worker-alive"

    # Leader mints the coordination seat and gets a fencing token.
    token = _insert_run_and_nodes(engine, now=now, leader_worker_id=leader_id)

    member = _admit_worker(engine, worker_id=live_owner)

    # Enqueue and have live_owner claim the item.
    _enqueue_single_token_rows(repo, engine, ("token-live",), member_token=token.membership, now=now)
    token_id = _claim_and_expire(repo, engine, member_token=member)

    # Leader's sweep: recover_expired_leases should NOT reap the item because
    # live_owner's heartbeat is fresh (owner_registry_dead is False) and the
    # lease has NOT passed the stall budget (only 1 s past lease_expires_at).
    reaped = repo.recover_expired_leases(
        coordination_token=token,
        grace_seconds=_SWEEP_GRACE,
        stall_budget_seconds=_STALL_BUDGET,
    )
    assert reaped == 0, "live-owner expired lease must NOT be reaped by a peer sweep"

    # Row still LEASED under live_owner.
    states = _work_item_states(engine)
    assert states[token_id]["status"] == TokenWorkStatus.LEASED.value
    assert states[token_id]["lease_owner"] == live_owner


@pytest.mark.parametrize(
    ("owner_status", "heartbeat_fresh"),
    [
        ("absent", True),  # arm (a): no run_workers row at all
        ("evicted", True),  # arm (b): status='evicted'
        ("departed", True),  # arm (b): status='departed'
        ("active", False),  # arm (c): status='active' + stale heartbeat
    ],
    ids=["absent-row", "evicted", "departed", "active-stale-heartbeat"],
)
def test_dead_registered_owner_expired_lease_is_reaped(owner_status: str, heartbeat_fresh: bool) -> None:
    """§A.5 / §C.1 — dead-owner arms: (a) absent row, (b) non-active status,
    (c) active-but-stale heartbeat. All three are reaped by a leader sweep.
    Attempt rotation is pinned (attempt 1 → 2, work_item_id rotated for READY).
    """
    engine = _make_scheduler_engine()
    now = landscape_database_now(engine)
    repo = TokenSchedulerRepository(engine)

    leader_id = "leader-sweeper"
    dead_owner = "worker-dead"

    token = _insert_run_and_nodes(engine, now=now, leader_worker_id=leader_id)

    member = _admit_worker(engine, worker_id=dead_owner)
    _enqueue_single_token_rows(repo, engine, ("token-dead",), member_token=token.membership, now=now)
    token_id = _claim_and_expire(repo, engine, member_token=member)
    original_id = _work_item_states(engine)[token_id]["work_item_id"]

    if owner_status == "departed":
        RunCoordinationRepository(engine).depart_worker(member_token=member)
    elif owner_status == "evicted":
        with engine.begin() as conn:
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.worker_id == dead_owner)
                .values(heartbeat_expires_at=now - timedelta(seconds=_SWEEP_GRACE + 1))
            )
        assert RunCoordinationRepository(engine).evict_worker(
            token=token, target_worker_id=dead_owner, grace_seconds=_SWEEP_GRACE, window_seconds=3600
        )
        # Isolate the non-active-status arm from stale-heartbeat eligibility.
        # This clock control cannot restore the evicted membership authority.
        with engine.begin() as conn:
            conn.execute(
                update(run_workers_table)
                .where(run_workers_table.c.worker_id == dead_owner)
                .values(heartbeat_expires_at=now + timedelta(hours=1))
            )
    else:
        # Explicit registry fault/clock controls happen only AFTER a real claim.
        # The absent row exercises recovery of a corrupted registry; it is not
        # an authority bypass used to create the lease.
        with engine.begin() as conn:
            if owner_status == "absent":
                conn.execute(delete(run_workers_table).where(run_workers_table.c.worker_id == dead_owner))
            else:
                assert not heartbeat_fresh
                conn.execute(
                    update(run_workers_table)
                    .where(run_workers_table.c.worker_id == dead_owner)
                    .values(heartbeat_expires_at=now - timedelta(seconds=_SWEEP_GRACE + 1))
                )

    reaped = repo.recover_expired_leases(
        coordination_token=token,
        grace_seconds=_SWEEP_GRACE,
        stall_budget_seconds=_STALL_BUDGET,
    )
    assert reaped == 1, f"dead-owner arm={owner_status!r} must be reaped"

    states = _work_item_states(engine)
    assert states[token_id]["status"] == TokenWorkStatus.READY.value
    assert states[token_id]["attempt"] == 2
    assert states[token_id]["lease_owner"] is None

    assert states[token_id]["work_item_id"] != original_id

    # No worker_stalled event for a dead-owner reap.
    stalled_events = _coordination_events(engine, run_id=RUN_ID, event_type="worker_stalled")
    assert stalled_events == [], "dead-owner reap must NOT emit worker_stalled"


def test_stall_budget_reaps_live_owner_and_emits_worker_stalled() -> None:
    """§A.5 :145 — stall arm: a registry-LIVE owner (fresh heartbeat) but drain
    loop is wedged. The item has been expired past stall_budget_seconds.
    The reap must succeed AND emit worker_stalled in the same transaction.
    """
    engine = _make_scheduler_engine()
    now = landscape_database_now(engine)
    repo = TokenSchedulerRepository(engine)

    leader_id = "leader-sweeper"
    live_but_wedged = "worker-wedged"

    token = _insert_run_and_nodes(engine, now=now, leader_worker_id=leader_id)

    member = _admit_worker(engine, worker_id=live_but_wedged)

    _enqueue_single_token_rows(repo, engine, ("token-stalled",), member_token=token.membership, now=now)
    # The lease has been expired for longer than stall_budget_seconds on the
    # database clock, so the stall arm triggers.
    stall_budget = 60.0  # short custom budget for the test
    token_id = _claim_and_expire(repo, engine, member_token=member, expired_seconds_ago=stall_budget + 10)

    reaped = repo.recover_expired_leases(
        coordination_token=token,
        grace_seconds=_SWEEP_GRACE,
        stall_budget_seconds=stall_budget,
    )
    assert reaped == 1, "stall-budget arm must reap the item"

    states = _work_item_states(engine)
    assert states[token_id]["status"] == TokenWorkStatus.READY.value
    assert states[token_id]["attempt"] == 2
    assert states[token_id]["lease_owner"] is None

    # worker_stalled event must be emitted for the live-but-wedged owner.
    stalled_events = _coordination_events(engine, run_id=RUN_ID, event_type="worker_stalled")
    assert len(stalled_events) == 1
    ctx = json.loads(str(stalled_events[0]["context_json"]))
    assert ctx["reason"] == "item_stall_budget"
    assert stalled_events[0]["worker_id"] == live_but_wedged
    assert stalled_events[0]["leader_epoch"] == token.leader_epoch


def test_leader_sweep_recovers_expired_claims_from_multiple_departed_workers() -> None:
    """Registered peers claim before departure; leader recovery rotates all three."""
    engine = _make_scheduler_engine()
    repo = TokenSchedulerRepository(engine)
    now = landscape_database_now(engine)
    leader = _insert_run_and_nodes(engine, now=now)

    token_ids = ("token-u0", "token-u1", "token-u2")
    _enqueue_single_token_rows(repo, engine, token_ids, member_token=leader.membership, now=now)

    for token_id, owner in zip(token_ids, ("owner-a", "owner-b", "owner-c"), strict=True):
        member = _admit_worker(engine, worker_id=owner)
        item = repo.claim_ready(member_token=member, lease_owner=owner, lease_seconds=_LEASE_SECONDS)
        assert item is not None and item.token_id == token_id
        expire_lease(engine, item.work_item_id)
        RunCoordinationRepository(engine).depart_worker(member_token=member)

    reaped = repo.recover_expired_leases(coordination_token=leader)
    assert reaped == 3

    states = _work_item_states(engine)
    for token_id in token_ids:
        assert states[token_id]["status"] == TokenWorkStatus.READY.value
        assert states[token_id]["attempt"] == 2
        assert states[token_id]["lease_owner"] is None
    assert [event["token_id"] for event in _recovery_events(engine)] == list(token_ids)
