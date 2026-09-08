"""Real PostgreSQL proof for reservation lock order and conflict safety."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep

import pytest
from sqlalchemy import event, func, select, update
from sqlalchemy.engine import Connection
from tests.fixtures.landscape import leader_coordination_token, make_factory, register_test_node
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import CallType, NodeStateStatus, NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.audit import DISCARD_SINK_NAME, TokenRef
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS
from elspeth.contracts.results import ArtifactDescriptor
from elspeth.contracts.sink_effects import (
    SINK_EFFECT_PROTOCOL_VERSION,
    SinkEffectAttemptAction,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectFinalizationResult,
    SinkEffectInputKind,
    SinkEffectInspectionMode,
    SinkEffectMemberCandidate,
    SinkEffectPlan,
    SinkEffectRole,
)
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.execution.sink_effect_attempt_results import encode_sink_effect_returned_result
from elspeth.core.landscape.execution.sink_effect_finalization import SinkEffectFinalizationMember, SinkEffectFinalizeRequest
from elspeth.core.landscape.execution.sink_effect_identity import compute_pipeline_effect_identity, resolve_sink_effect_members
from elspeth.core.landscape.execution.sink_effect_lifecycle import SinkEffectAttemptRequest, SinkEffectAttemptResult
from elspeth.core.landscape.execution.sink_effect_reservation import SinkEffectReservationRequest
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import (
    artifacts_table,
    node_states_table,
    operations_table,
    sink_effect_members_table,
    sink_effects_table,
    token_outcomes_table,
)

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


@pytest.fixture(scope="module")
def postgres_db(postgres_url: str) -> Iterator[LandscapeDB]:
    db = LandscapeDB(postgres_url)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def peer_db(postgres_url: str) -> Iterator[LandscapeDB]:
    """A separate connection pool makes competing backend identities stable."""
    db = LandscapeDB(postgres_url)
    try:
        yield db
    finally:
        db.close()


@contextmanager
def _observe_fence_attempt(db: LandscapeDB) -> Iterator[tuple[threading.Event, dict[str, int]]]:
    attempted = threading.Event()
    backend: dict[str, int] = {}

    def before_cursor_execute(
        conn: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = statement.upper()
        if attempted.is_set() or not normalized.startswith("UPDATE RUN_COORDINATION "):
            return
        backend["pid"] = int(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        attempted.set()

    event.listen(db.engine, "before_cursor_execute", before_cursor_execute)
    try:
        yield attempted, backend
    finally:
        event.remove(db.engine, "before_cursor_execute", before_cursor_execute)


def _assert_backend_blocked_by(db: LandscapeDB, *, waiter: int, blocker: int) -> None:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        with db.read_only_connection() as conn:
            blocked = conn.exec_driver_sql("SELECT %s = ANY(pg_blocking_pids(%s))", (blocker, waiter)).scalar_one()
        if blocked:
            return
        sleep(0.01)
    pytest.fail(f"PostgreSQL backend {waiter} never waited on backend {blocker}")


def test_concurrent_reservation_reverse_arrival_uses_ascending_locks_and_one_effect(
    postgres_db: LandscapeDB, peer_db: LandscapeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = postgres_db
    factory = make_factory(db)
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source = register_test_node(factory.data_flow, run.run_id, "source", node_type=NodeType.SOURCE, plugin_name="source")
    sink = register_test_node(factory.data_flow, run.run_id, "sink", node_type=NodeType.SINK, plugin_name="sink")
    candidates: list[SinkEffectMemberCandidate] = []
    for ordinal in range(2):
        payload = {"ordinal": ordinal}
        _row, token = factory.data_flow.create_row_with_token(
            coordination_token=leader_coordination_token(factory, run.run_id),
            source_node_id=source,
            row_index=ordinal,
            data=payload,
            source_row_index=ordinal,
            ingest_sequence=ordinal,
        )
        factory.execution.begin_node_state(
            token_id=token.token_id,
            node_id=sink,
            member_token=leader_coordination_token(factory, run.run_id).membership,
            step_index=0,
            input_data=payload,
        )
        candidates.append(SinkEffectMemberCandidate(token_id=token.token_id, row=payload))
    members = resolve_sink_effect_members(factory, candidates)
    identity = compute_pipeline_effect_identity(
        run_id=run.run_id,
        sink_node_id=sink,
        role=SinkEffectRole.PRIMARY,
        sink_config={"name": "sink"},
        target_config={"path": "out"},
        members=members,
    )
    request = SinkEffectReservationRequest(
        run_id=run.run_id,
        sink_node_id=sink,
        role=SinkEffectRole.PRIMARY,
        input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        requested_target_hash=identity.requested_target_hash,
        members=members,
        audit_export_snapshot_id=None,
        config_hash=identity.config_hash,
        replacing_target=True,
        primary_effect_id=None,
    )
    reverse_request = replace(request, members=tuple(reversed(members)))
    first_locked = threading.Event()
    release_first = threading.Event()
    observations: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []

    def pause(pid: int, token_ids: tuple[str, ...], state_ids: tuple[str, ...]) -> None:
        observations.append((pid, token_ids, state_ids))
        if not first_locked.is_set():
            first_locked.set()
            assert release_first.wait(timeout=5)

    monkeypatch.setattr(factory.execution.sink_effects._reservation, "_after_witness_locks", pause)
    second_factory = make_factory(peer_db)
    monkeypatch.setattr(second_factory.execution.sink_effects._reservation, "_after_witness_locks", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            factory.execution.sink_effects.reserve, request, coordination_token=leader_coordination_token(factory, run.run_id)
        )
        assert first_locked.wait(timeout=5)
        second = pool.submit(
            second_factory.execution.sink_effects.reserve,
            reverse_request,
            coordination_token=leader_coordination_token(second_factory, run.run_id),
        )
        release_first.set()
        results = (first.result(timeout=10), second.result(timeout=10))

    assert len({pid for pid, _tokens, _states in observations}) == 2
    assert all(list(tokens) == sorted(tokens) and list(states) == sorted(states) for _pid, tokens, states in observations)
    assert sum(result.new_effect is not None for result in results) == 1
    with db.read_only_connection() as conn:
        assert conn.scalar(select(func.count()).select_from(sink_effects_table)) == 1


def test_finalization_vs_outcome_mutation_uses_distinct_backends_and_token_first_order(
    postgres_db: LandscapeDB,
    peer_db: LandscapeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = postgres_db
    finalizer_factory = make_factory(db)
    outcome_factory = make_factory(peer_db)
    run = finalizer_factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source = register_test_node(finalizer_factory.data_flow, run.run_id, "finalize-source", node_type=NodeType.SOURCE, plugin_name="source")
    sink = register_test_node(finalizer_factory.data_flow, run.run_id, "finalize-sink", node_type=NodeType.SINK, plugin_name="sink")
    payload = {"ordinal": 0}
    _row, token = finalizer_factory.data_flow.create_row_with_token(
        coordination_token=leader_coordination_token(finalizer_factory, run.run_id),
        source_node_id=source,
        row_index=0,
        data=payload,
        source_row_index=0,
        ingest_sequence=0,
    )
    finalizer_factory.execution.begin_node_state(
        token_id=token.token_id,
        node_id=sink,
        member_token=leader_coordination_token(finalizer_factory, run.run_id).membership,
        step_index=0,
        input_data=payload,
    )
    members = resolve_sink_effect_members(finalizer_factory, [SinkEffectMemberCandidate(token_id=token.token_id, row=payload)])
    identity = compute_pipeline_effect_identity(
        run_id=run.run_id,
        sink_node_id=sink,
        role=SinkEffectRole.PRIMARY,
        sink_config={"name": "sink"},
        target_config={"path": "finalize.jsonl"},
        members=members,
    )
    effect = finalizer_factory.execution.sink_effects.reserve(
        SinkEffectReservationRequest(
            run_id=run.run_id,
            sink_node_id=sink,
            role=SinkEffectRole.PRIMARY,
            input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            requested_target_hash=identity.requested_target_hash,
            members=members,
            audit_export_snapshot_id=None,
            config_hash=identity.config_hash,
            replacing_target=False,
            primary_effect_id=None,
        ),
        coordination_token=leader_coordination_token(finalizer_factory, run.run_id),
    ).new_effect
    assert effect is not None
    descriptor = ArtifactDescriptor(
        artifact_type="file",
        path_or_uri="file:///tmp/finalize.jsonl",
        content_hash="d" * 64,
        size_bytes=12,
    )
    claim = finalizer_factory.execution.sink_effects.claim_preparation(
        effect.effect_id,
        owner="worker-a",
        ttl=timedelta(seconds=30),
        coordination_token=leader_coordination_token(finalizer_factory, effect.run_id),
    )
    finalizer_factory.execution.sink_effects.complete_plan(
        effect.effect_id,
        SinkEffectPlan(
            effect_id=effect.effect_id,
            protocol_version=SINK_EFFECT_PROTOCOL_VERSION,
            input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            descriptor_mode=SinkEffectDescriptorMode.PRECOMPUTED,
            inspection_mode=SinkEffectInspectionMode.NO_INSPECTION_REQUIRED,
            target=descriptor.path_or_uri,
            plan_hash="a" * 64,
            payload_hash="b" * 64,
            expected_descriptor=descriptor,
            safe_evidence={"inspection_reference": "no-inspection-required:v1"},
        ),
        claim=claim,
        coordination_token=leader_coordination_token(finalizer_factory, effect.run_id),
    )
    lease = finalizer_factory.execution.sink_effects.acquire_lease(
        effect.effect_id,
        owner="worker-a",
        ttl=timedelta(seconds=30),
        coordination_token=leader_coordination_token(finalizer_factory, effect.run_id),
    )
    attempt = finalizer_factory.execution.sink_effects.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=None,
            generation=lease.generation,
            action=SinkEffectAttemptAction.COMMIT,
            call_kind=CallType.FILESYSTEM,
            request_hash="a" * 64,
        ),
        coordination_token=leader_coordination_token(finalizer_factory, effect.run_id),
    )
    finalizer_factory.execution.sink_effects.record_attempt_result(
        SinkEffectAttemptResult(
            attempt_id=attempt.attempt_id,
            evidence=encode_sink_effect_returned_result(
                SinkEffectCommitResult(
                    descriptor=descriptor,
                    evidence={"result": "exact"},
                    accepted_ordinals=(0,),
                    diverted_ordinals=(),
                )
            ),
            latency_ms=1.0,
        ),
        coordination_token=leader_coordination_token(finalizer_factory, effect.run_id),
    )
    request = SinkEffectFinalizeRequest(
        effect_id=effect.effect_id,
        lease_owner=lease.owner,
        generation=lease.generation,
        descriptor=descriptor,
        publication_performed=True,
        publication_evidence_kind="returned",
        accepted_ordinals=(0,),
        diverted_ordinals=(),
        evidence={"result": "exact"},
        members=(
            SinkEffectFinalizationMember(
                ordinal=0,
                output_data={"row": payload},
                duration_ms=1.0,
                outcome=TerminalOutcome.SUCCESS,
                path=TerminalPath.DEFAULT_FLOW,
                sink_name="sink",
            ),
        ),
        attempt_id=attempt.attempt_id,
    )
    finalizer_holds_token = threading.Event()
    release_finalizer = threading.Event()
    backend_pids: dict[str, int] = {}

    def after_token_locks(pid: int, token_ids: tuple[str, ...]) -> None:
        assert token_ids == tuple(sorted(token_ids))
        backend_pids["finalizer"] = pid
        finalizer_holds_token.set()
        assert release_finalizer.wait(timeout=5)

    original_outcome_lock = outcome_factory.data_flow.outcomes.lock_token_outcome_dependencies

    def outcome_lock(refs: tuple[TokenRef, ...], *, conn: Connection) -> None:
        backend_pids["outcome"] = int(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        original_outcome_lock(refs, conn=conn)

    monkeypatch.setattr(finalizer_factory.execution.sink_effects._finalization, "_after_token_locks", after_token_locks)
    monkeypatch.setattr(outcome_factory.data_flow.outcomes, "lock_token_outcome_dependencies", outcome_lock)

    def competing_outcome() -> str:
        return outcome_factory.data_flow.record_token_outcome_leader(
            TokenRef(token_id=token.token_id, run_id=run.run_id),
            TerminalOutcome.SUCCESS,
            TerminalPath.DEFAULT_FLOW,
            sink_name="sink",
            coordination_token=leader_coordination_token(outcome_factory, run.run_id),
        )

    # Observe the competing leader before its seat lock. It cannot approach
    # token locks until the first writer commits its entire fenced payload.
    with _observe_fence_attempt(peer_db) as (outcome_attempted, outcome_backend), ThreadPoolExecutor(max_workers=2) as pool:
        finalization = pool.submit(
            finalizer_factory.execution.sink_effects.finalize,
            request,
            coordination_token=leader_coordination_token(finalizer_factory, effect.run_id),
        )
        assert finalizer_holds_token.wait(timeout=5)
        outcome = pool.submit(competing_outcome)
        try:
            assert outcome_attempted.wait(timeout=5)
            _assert_backend_blocked_by(db, waiter=outcome_backend["pid"], blocker=backend_pids["finalizer"])
        finally:
            release_finalizer.set()
        winner = finalization.result(timeout=10)
        with pytest.raises(LandscapeRecordError):
            outcome.result(timeout=10)

    assert backend_pids["finalizer"] != backend_pids["outcome"]
    assert winner.effect.state.value == "finalized"
    with db.read_only_connection() as conn:
        assert (
            conn.scalar(select(func.count()).select_from(token_outcomes_table).where(token_outcomes_table.c.token_id == token.token_id))
            == 1
        )


def test_concurrent_disjoint_reservations_form_one_stream_predecessor_chain(
    postgres_db: LandscapeDB, peer_db: LandscapeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = postgres_db
    first_factory = make_factory(db)
    second_factory = make_factory(peer_db)
    run = first_factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source = register_test_node(
        first_factory.data_flow,
        run.run_id,
        "disjoint-source",
        node_type=NodeType.SOURCE,
        plugin_name="source",
    )
    sink = register_test_node(
        first_factory.data_flow,
        run.run_id,
        "disjoint-sink",
        node_type=NodeType.SINK,
        plugin_name="sink",
    )
    candidates: list[SinkEffectMemberCandidate] = []
    for ordinal in range(2):
        payload = {"ordinal": ordinal}
        _row, token = first_factory.data_flow.create_row_with_token(
            coordination_token=leader_coordination_token(first_factory, run.run_id),
            source_node_id=source,
            row_index=ordinal,
            data=payload,
            source_row_index=ordinal,
            ingest_sequence=ordinal,
        )
        first_factory.execution.begin_node_state(
            token_id=token.token_id,
            node_id=sink,
            member_token=leader_coordination_token(first_factory, run.run_id).membership,
            step_index=0,
            input_data=payload,
        )
        candidates.append(SinkEffectMemberCandidate(token_id=token.token_id, row=payload))
    members = resolve_sink_effect_members(first_factory, candidates)

    requests: list[SinkEffectReservationRequest] = []
    for member in members:
        dense_member = member if member.ordinal == 0 else replace(member, ordinal=0)
        identity = compute_pipeline_effect_identity(
            run_id=run.run_id,
            sink_node_id=sink,
            role=SinkEffectRole.PRIMARY,
            sink_config={"name": "sink"},
            target_config={"path": "same-output"},
            members=(dense_member,),
        )
        requests.append(
            SinkEffectReservationRequest(
                run_id=run.run_id,
                sink_node_id=sink,
                role=SinkEffectRole.PRIMARY,
                input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
                requested_target_hash=identity.requested_target_hash,
                members=(member,),
                audit_export_snapshot_id=None,
                config_hash=identity.config_hash,
                replacing_target=True,
                primary_effect_id=None,
            )
        )

    # WHAT CANNOT HAPPEN HERE, AND WHY (ADR-048). Do not reintroduce a barrier.
    #
    # This proof used to hold both reservations inside the witness lock at once
    # and assert they arrived together. That interleaving is now UNREACHABLE BY
    # DESIGN. `fenced_leader_transaction` issues the verify-and-extend UPDATE
    # against the run's single `run_coordination` seat row as the FIRST
    # statement of the payload's own IMMEDIATE transaction, and that atomicity
    # IS the safety property: split the fence from the payload and a deposed
    # leader can pass the fence and then write. An UPDATE holds its row
    # exclusive until commit, so while one leader-fenced verb is in flight for
    # a run no second one can be past the fence. Two reservations on one run
    # therefore SERIALISE, and a barrier expecting both would always time out.
    # Restoring one would assert on timing the test cannot control.
    #
    # Every property this proof is named for survives and is asserted below,
    # because none of them needs simultaneity: each witness still takes its
    # token and state locks in ascending order (checked per call), the two
    # reservations still run on two distinct PostgreSQL backends, and the
    # disjoint members still converge on one stream with a correct predecessor
    # chain whichever order the seat admits them in.
    backend_pids: set[int] = set()
    backend_guard = threading.Lock()

    def record_witness_order(pid: int, token_ids: tuple[str, ...], state_ids: tuple[str, ...]) -> None:
        assert token_ids == tuple(sorted(token_ids))
        assert state_ids == tuple(sorted(state_ids))
        with backend_guard:
            backend_pids.add(pid)

    monkeypatch.setattr(first_factory.execution.sink_effects._reservation, "_after_witness_locks", record_witness_order)
    monkeypatch.setattr(second_factory.execution.sink_effects._reservation, "_after_witness_locks", record_witness_order)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(
                first_factory.execution.sink_effects.reserve,
                requests[0],
                coordination_token=leader_coordination_token(first_factory, run.run_id),
            ),
            pool.submit(
                second_factory.execution.sink_effects.reserve,
                requests[1],
                coordination_token=leader_coordination_token(second_factory, run.run_id),
            ),
        )
        effects = tuple(future.result(timeout=10).new_effect for future in futures)

    assert len(backend_pids) == 2
    assert all(effect is not None for effect in effects)
    ordered = sorted((effect for effect in effects if effect is not None), key=lambda effect: effect.stream_sequence or 0)
    assert [effect.stream_sequence for effect in ordered] == [0, 1]
    assert ordered[0].predecessor_effect_id is None
    assert ordered[1].predecessor_effect_id == ordered[0].effect_id


def test_reservation_vs_outcome_uses_token_first_order_without_deadlock(
    postgres_db: LandscapeDB, peer_db: LandscapeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = postgres_db
    reservation_factory = make_factory(db)
    outcome_factory = make_factory(peer_db)
    run = reservation_factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source = register_test_node(
        reservation_factory.data_flow,
        run.run_id,
        "outcome-race-source",
        node_type=NodeType.SOURCE,
        plugin_name="source",
    )
    sink = register_test_node(
        reservation_factory.data_flow,
        run.run_id,
        "outcome-race-sink",
        node_type=NodeType.SINK,
        plugin_name="sink",
    )
    candidates: list[SinkEffectMemberCandidate] = []
    for ordinal in range(2):
        payload = {"ordinal": ordinal}
        _row, token = reservation_factory.data_flow.create_row_with_token(
            coordination_token=leader_coordination_token(reservation_factory, run.run_id),
            source_node_id=source,
            row_index=ordinal,
            data=payload,
            source_row_index=ordinal,
            ingest_sequence=ordinal,
        )
        reservation_factory.execution.begin_node_state(
            token_id=token.token_id,
            node_id=sink,
            member_token=leader_coordination_token(reservation_factory, run.run_id).membership,
            step_index=0,
            input_data=payload,
        )
        candidates.append(SinkEffectMemberCandidate(token_id=token.token_id, row=payload))
    members = resolve_sink_effect_members(reservation_factory, candidates)
    identity = compute_pipeline_effect_identity(
        run_id=run.run_id,
        sink_node_id=sink,
        role=SinkEffectRole.PRIMARY,
        sink_config={"name": "sink"},
        target_config={"path": "outcome-race-output"},
        members=members,
    )
    request = SinkEffectReservationRequest(
        run_id=run.run_id,
        sink_node_id=sink,
        role=SinkEffectRole.PRIMARY,
        input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
        requested_target_hash=identity.requested_target_hash,
        members=members,
        audit_export_snapshot_id=None,
        config_hash=identity.config_hash,
        replacing_target=True,
        primary_effect_id=None,
    )

    first_token_locked = threading.Event()
    release_reservation = threading.Event()
    reservation_pid: list[int] = []
    outcome_pid: list[int] = []
    complete_witnesses: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    def pause_after_first_token(pid: int, token_ids: tuple[str, ...]) -> None:
        if len(token_ids) == 1 and not first_token_locked.is_set():
            reservation_pid.append(pid)
            first_token_locked.set()
            assert release_reservation.wait(timeout=5)

    def capture_complete_witnesses(pid: int, token_ids: tuple[str, ...], state_ids: tuple[str, ...]) -> None:
        if not reservation_pid:
            reservation_pid.append(pid)
        complete_witnesses.append((token_ids, state_ids))

    original_outcome_locks = outcome_factory.data_flow.outcomes.lock_token_outcome_dependencies

    def enter_outcome_lock(refs: tuple[TokenRef, ...], *, conn: Connection) -> None:
        outcome_pid.append(int(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()))
        original_outcome_locks(refs, conn=conn)

    monkeypatch.setattr(reservation_factory.execution.sink_effects._reservation, "_after_token_lock", pause_after_first_token)
    monkeypatch.setattr(reservation_factory.execution.sink_effects._reservation, "_after_witness_locks", capture_complete_witnesses)
    monkeypatch.setattr(outcome_factory.data_flow.outcomes, "lock_token_outcome_dependencies", enter_outcome_lock)

    first_token_id = min(member.token_id for member in members)
    with _observe_fence_attempt(peer_db) as (outcome_attempted, outcome_backend), ThreadPoolExecutor(max_workers=2) as pool:
        reservation_future = pool.submit(
            reservation_factory.execution.sink_effects.reserve,
            request,
            coordination_token=leader_coordination_token(reservation_factory, run.run_id),
        )
        assert first_token_locked.wait(timeout=5)
        outcome_future = pool.submit(
            outcome_factory.data_flow.record_token_outcome_leader,
            TokenRef(token_id=first_token_id, run_id=run.run_id),
            TerminalOutcome.FAILURE,
            TerminalPath.SINK_DISCARDED,
            sink_name=DISCARD_SINK_NAME,
            error_hash="outcome-race",
            coordination_token=leader_coordination_token(outcome_factory, run.run_id),
        )
        try:
            assert outcome_attempted.wait(timeout=5)
            _assert_backend_blocked_by(db, waiter=outcome_backend["pid"], blocker=reservation_pid[0])
        finally:
            release_reservation.set()
        reservation = reservation_future.result(timeout=10)
        outcome_future.result(timeout=10)

    assert reservation.new_effect is not None
    assert reservation_pid and outcome_pid and reservation_pid[0] != outcome_pid[0]
    assert complete_witnesses == [
        (
            tuple(sorted(member.token_id for member in members)),
            tuple(sorted(state_id for state_id in request_witness_state_ids(db, run.run_id, sink))),
        )
    ]
    with db.read_only_connection() as conn:
        assert (
            conn.scalar(
                select(func.count())
                .select_from(sink_effect_members_table)
                .where(sink_effect_members_table.c.effect_id == reservation.new_effect.effect_id)
            )
            == 2
        )
        assert (
            conn.scalar(select(func.count()).select_from(token_outcomes_table).where(token_outcomes_table.c.token_id == first_token_id))
            == 1
        )


def request_witness_state_ids(db: LandscapeDB, run_id: str, sink_node_id: str) -> tuple[str, ...]:
    """Read the race's complete state set after both transactions finish."""

    with db.read_only_connection() as conn:
        return tuple(
            conn.execute(
                select(node_states_table.c.state_id).where(
                    node_states_table.c.run_id == run_id,
                    node_states_table.c.node_id == sink_node_id,
                )
            ).scalars()
        )


@dataclass(frozen=True)
class _InFlightEffect:
    """A leased effect with a returned commit attempt, ready to finalize."""

    run_id: str
    token_id: str
    sink_node_id: str
    effect_id: str
    operation_id: str
    lease_owner: str
    generation: int
    request: SinkEffectFinalizeRequest


def _build_in_flight_effect(factory: RecorderFactory, *, name_prefix: str, owner: str = "worker-a") -> _InFlightEffect:
    run = factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source = register_test_node(factory.data_flow, run.run_id, f"{name_prefix}-source", node_type=NodeType.SOURCE, plugin_name="source")
    sink = register_test_node(factory.data_flow, run.run_id, f"{name_prefix}-sink", node_type=NodeType.SINK, plugin_name="sink")
    payload = {"ordinal": 0}
    _row, token = factory.data_flow.create_row_with_token(
        coordination_token=leader_coordination_token(factory, run.run_id),
        source_node_id=source,
        row_index=0,
        data=payload,
        source_row_index=0,
        ingest_sequence=0,
    )
    factory.execution.begin_node_state(
        token_id=token.token_id,
        node_id=sink,
        member_token=leader_coordination_token(factory, run.run_id).membership,
        step_index=0,
        input_data=payload,
    )
    members = resolve_sink_effect_members(factory, [SinkEffectMemberCandidate(token_id=token.token_id, row=payload)])
    identity = compute_pipeline_effect_identity(
        run_id=run.run_id,
        sink_node_id=sink,
        role=SinkEffectRole.PRIMARY,
        sink_config={"name": "sink"},
        target_config={"path": f"{name_prefix}.jsonl"},
        members=members,
    )
    effect = factory.execution.sink_effects.reserve(
        SinkEffectReservationRequest(
            run_id=run.run_id,
            sink_node_id=sink,
            role=SinkEffectRole.PRIMARY,
            input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            requested_target_hash=identity.requested_target_hash,
            members=members,
            audit_export_snapshot_id=None,
            config_hash=identity.config_hash,
            replacing_target=False,
            primary_effect_id=None,
        ),
        coordination_token=leader_coordination_token(factory, run.run_id),
    ).new_effect
    assert effect is not None
    descriptor = ArtifactDescriptor(
        artifact_type="file",
        path_or_uri=f"file:///tmp/{name_prefix}.jsonl",
        content_hash="d" * 64,
        size_bytes=12,
    )
    claim = factory.execution.sink_effects.claim_preparation(
        effect.effect_id, owner=owner, ttl=timedelta(seconds=30), coordination_token=leader_coordination_token(factory, effect.run_id)
    )
    factory.execution.sink_effects.complete_plan(
        effect.effect_id,
        SinkEffectPlan(
            effect_id=effect.effect_id,
            protocol_version=SINK_EFFECT_PROTOCOL_VERSION,
            input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            descriptor_mode=SinkEffectDescriptorMode.PRECOMPUTED,
            inspection_mode=SinkEffectInspectionMode.NO_INSPECTION_REQUIRED,
            target=descriptor.path_or_uri,
            plan_hash="a" * 64,
            payload_hash="b" * 64,
            expected_descriptor=descriptor,
            safe_evidence={"inspection_reference": "no-inspection-required:v1"},
        ),
        claim=claim,
        coordination_token=leader_coordination_token(factory, effect.run_id),
    )
    lease = factory.execution.sink_effects.acquire_lease(
        effect.effect_id, owner=owner, ttl=timedelta(seconds=30), coordination_token=leader_coordination_token(factory, effect.run_id)
    )
    attempt = factory.execution.sink_effects.begin_attempt(
        SinkEffectAttemptRequest(
            effect_id=effect.effect_id,
            member_ordinal=None,
            generation=lease.generation,
            action=SinkEffectAttemptAction.COMMIT,
            call_kind=CallType.FILESYSTEM,
            request_hash="a" * 64,
        ),
        coordination_token=leader_coordination_token(factory, effect.run_id),
    )
    factory.execution.sink_effects.record_attempt_result(
        SinkEffectAttemptResult(
            attempt_id=attempt.attempt_id,
            evidence=encode_sink_effect_returned_result(
                SinkEffectCommitResult(
                    descriptor=descriptor,
                    evidence={"result": "exact"},
                    accepted_ordinals=(0,),
                    diverted_ordinals=(),
                )
            ),
            latency_ms=1.0,
        ),
        coordination_token=leader_coordination_token(factory, effect.run_id),
    )
    request = SinkEffectFinalizeRequest(
        effect_id=effect.effect_id,
        lease_owner=lease.owner,
        generation=lease.generation,
        descriptor=descriptor,
        publication_performed=True,
        publication_evidence_kind="returned",
        accepted_ordinals=(0,),
        diverted_ordinals=(),
        evidence={"result": "exact"},
        members=(
            SinkEffectFinalizationMember(
                ordinal=0,
                output_data={"row": payload},
                duration_ms=1.0,
                outcome=TerminalOutcome.SUCCESS,
                path=TerminalPath.DEFAULT_FLOW,
                sink_name="sink",
            ),
        ),
        attempt_id=attempt.attempt_id,
    )
    operations = factory.execution.get_operations_for_run(run.run_id)
    assert len(operations) == 1
    operation = operations[0]
    assert operation.sink_effect_id == effect.effect_id
    assert operation.status == "open"
    return _InFlightEffect(
        run_id=run.run_id,
        token_id=token.token_id,
        sink_node_id=sink,
        effect_id=effect.effect_id,
        operation_id=operation.operation_id,
        lease_owner=lease.owner,
        generation=lease.generation,
        request=request,
    )


def test_takeover_vs_finalization_generation_fences_stale_finalizer(
    postgres_db: LandscapeDB, peer_db: LandscapeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Takeover paused after its effect lock beats a stale finalizer approaching
    through sorted token/state locks: the generation fence rejects the stale
    finalization, both transactions complete bounded, and no artifact or
    outcome from the loser survives (design scenario: takeover versus
    finalization/head CAS)."""
    db = postgres_db
    finalizer_factory = make_factory(db)
    takeover_factory = make_factory(peer_db)
    built = _build_in_flight_effect(finalizer_factory, name_prefix="takeover-fence")

    # Expire the lease directly so takeover is legal while the original owner
    # still believes it holds the effect — the crash-recovery race the design
    # fences with generation CAS. A sleep-based expiry would be flaky here.
    with db.engine.begin() as conn:
        conn.execute(
            update(sink_effects_table)
            .where(sink_effects_table.c.effect_id == built.effect_id)
            .values(
                lease_heartbeat_at=datetime.now(UTC) - timedelta(hours=2),
                lease_expires_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )

    # WHAT CANNOT HAPPEN HERE, AND WHY (ADR-048). Do not reintroduce the pause.
    #
    # This proof used to hold the takeover open inside its effect lock and
    # drive the finalizer into the same window, to show the generation fence
    # refusing a finalizer that had already passed its own locks. Both verbs
    # are leader-fenced, and `fenced_leader_transaction` holds the run's single
    # seat row exclusive for the whole payload transaction, so the second verb
    # cannot be past the fence while the first is in flight. Holding the
    # takeover open now blocks the finalizer BEFORE its locks, so the events
    # this test used to wait on can never be set. That is serialisation by
    # design, not a lost race.
    #
    # The property the test is named for does not need that window: a finalizer
    # carrying the pre-takeover lease owner must still be refused for stale
    # lease authority once the takeover has bumped the generation. Serialised,
    # the takeover commits first and the finalizer meets the same fence it used
    # to meet mid-flight. The per-call ascending token and state lock order,
    # the two distinct backends, and the effect row's terminal state all
    # survive untouched below.
    # This event fixes the ORDER, not an interleaving. The finalizer is
    # submitted only once the takeover is inside its fenced transaction, so the
    # takeover deterministically holds the seat first and the finalizer is
    # deterministically the one that meets the bumped generation. Without it
    # the two submissions race for the seat and the test would assert on
    # whichever happened to win. It does NOT make the two verbs overlap — the
    # fence forbids that — and nothing below depends on overlap.
    takeover_in_flight = threading.Event()
    backend_pids: dict[str, int] = {}

    def capture_takeover_backend(pid: int, effect_id: str) -> None:
        assert effect_id == built.effect_id
        backend_pids["takeover"] = pid
        takeover_in_flight.set()

    def capture_token_locks(pid: int, token_ids: tuple[str, ...]) -> None:
        assert token_ids == tuple(sorted(token_ids))
        backend_pids["finalizer"] = pid

    def check_state_lock_order(_pid: int, state_ids: tuple[str, ...]) -> None:
        assert state_ids == tuple(sorted(state_ids))

    monkeypatch.setattr(takeover_factory.execution.sink_effects._lifecycle, "_after_effect_lock", capture_takeover_backend)
    monkeypatch.setattr(finalizer_factory.execution.sink_effects._finalization, "_after_token_locks", capture_token_locks)
    monkeypatch.setattr(finalizer_factory.execution.sink_effects._finalization, "_after_state_locks", check_state_lock_order)

    takeover_token = leader_coordination_token(takeover_factory, built.run_id)
    finalizer_token = leader_coordination_token(finalizer_factory, built.run_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        takeover = pool.submit(
            takeover_factory.execution.sink_effects.takeover_expired,
            built.effect_id,
            owner="worker-b",
            ttl=timedelta(seconds=30),
            coordination_token=takeover_token,
        )
        assert takeover_in_flight.wait(timeout=5), "takeover never reached its effect lock"
        finalization = pool.submit(finalizer_factory.execution.sink_effects.finalize, built.request, coordination_token=finalizer_token)
        new_lease = takeover.result(timeout=10)
        with pytest.raises(LandscapeRecordError, match="stale lease owner"):
            finalization.result(timeout=10)

    assert new_lease.owner == "worker-b"
    assert new_lease.generation == built.generation + 1
    # Separate pools guarantee distinct server sessions even when the seat
    # admits the payloads sequentially.
    assert backend_pids.keys() == {"takeover", "finalizer"}
    assert backend_pids["takeover"] != backend_pids["finalizer"]
    with db.read_only_connection() as conn:
        effect_row = conn.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id == built.effect_id)).one()
        operation_rows = conn.execute(select(operations_table).where(operations_table.c.sink_effect_id == built.effect_id)).fetchall()
        assert effect_row.state == "in_flight"
        assert effect_row.lease_owner == "worker-b"
        assert int(effect_row.generation) == built.generation + 1
        assert len(operation_rows) == 1
        operation_row = operation_rows[0]
        assert operation_row.operation_id == built.operation_id
        assert operation_row.status == "open"
        assert operation_row.completed_at is None
        assert operation_row.duration_ms is None
        assert operation_row.output_data_hash is None
        assert operation_row.error_message is None
        assert (
            conn.scalar(select(func.count()).select_from(artifacts_table).where(artifacts_table.c.sink_effect_id == built.effect_id)) == 0
        )
        assert (
            conn.scalar(select(func.count()).select_from(token_outcomes_table).where(token_outcomes_table.c.token_id == built.token_id))
            == 0
        )
        statuses = list(conn.execute(select(node_states_table.c.status).where(node_states_table.c.token_id == built.token_id)).scalars())
        assert statuses == [NodeStateStatus.OPEN.value]

    winner_request = replace(
        built.request,
        lease_owner=new_lease.owner,
        generation=new_lease.generation,
    )
    winner = takeover_factory.execution.sink_effects.finalize(
        winner_request, coordination_token=leader_coordination_token(takeover_factory, built.run_id)
    )
    assert winner.effect.effect_id == built.effect_id
    assert winner.effect.generation == new_lease.generation
    assert winner.artifact.path_or_uri == built.request.descriptor.path_or_uri
    assert winner.artifact.content_hash == built.request.descriptor.content_hash
    assert len(winner.state_ids) == 1
    assert len(winner.outcome_ids) == 1
    with db.read_only_connection() as conn:
        effect_row = conn.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id == built.effect_id)).one()
        operation_rows = conn.execute(select(operations_table).where(operations_table.c.sink_effect_id == built.effect_id)).fetchall()
        assert effect_row.state == "finalized"
        assert effect_row.lease_owner is None
        assert int(effect_row.generation) == new_lease.generation
        assert len(operation_rows) == 1
        operation_row = operation_rows[0]
        assert operation_row.operation_id == built.operation_id
        assert operation_row.status == "completed"
        assert operation_row.completed_at is not None
        assert operation_row.duration_ms == winner_request.operation_duration_ms
        assert operation_row.error_message is None
        assert operation_row.output_data_hash == stable_hash(
            {
                "accepted_ordinals": list(winner_request.accepted_ordinals),
                "artifact_id": winner.artifact.artifact_id,
                "descriptor_hash": effect_row.result_descriptor_hash,
                "diverted_ordinals": list(winner_request.diverted_ordinals),
                "effect_id": built.effect_id,
            }
        )
        assert (
            conn.scalar(select(func.count()).select_from(artifacts_table).where(artifacts_table.c.sink_effect_id == built.effect_id)) == 1
        )
        assert (
            conn.scalar(select(func.count()).select_from(token_outcomes_table).where(token_outcomes_table.c.token_id == built.token_id))
            == 1
        )
        statuses = list(conn.execute(select(node_states_table.c.status).where(node_states_table.c.token_id == built.token_id)).scalars())
        assert statuses == [NodeStateStatus.COMPLETED.value]


def test_takeover_blocked_by_finalization_observes_finalized_effect(
    postgres_db: LandscapeDB, peer_db: LandscapeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other legal winner: a finalizer paused holding its effect locks
    commits first, and the concurrent takeover — blocked on the same effect
    row — must observe FINALIZED and be rejected instead of stealing the
    lease of a completed effect."""
    db = postgres_db
    finalizer_factory = make_factory(db)
    takeover_factory = make_factory(peer_db)
    built = _build_in_flight_effect(finalizer_factory, name_prefix="takeover-loses")

    # WHAT CANNOT HAPPEN HERE, AND WHY (ADR-048). Do not reintroduce the pause.
    #
    # This proof used to hold the finalizer inside its effect locks and drive
    # the takeover up to its own `_lock_effect` in that window, to show the
    # takeover blocking on the effect row and then observing a finalized
    # effect. Both verbs are leader-fenced, and `fenced_leader_transaction`
    # holds the run's single seat row exclusive for the whole payload
    # transaction, so the takeover cannot reach `_lock_effect` at all while the
    # finalizer is in flight. The old choreography is now a deadlock rather
    # than a race: the test waited for the takeover to approach before
    # releasing the finalizer, and the takeover cannot approach until the
    # finalizer commits.
    #
    # The property the test is named for survives without the window. A
    # takeover arriving after a finalization must observe the finalized effect
    # and be refused; serialised, that is exactly the order it meets. The
    # finalizer's ascending effect-lock order, the two distinct backends, and
    # the complete finalize outcome are all still asserted.
    backend_pids: dict[str, int] = {}

    def check_finalizer_effect_lock_order(pid: int, effect_ids: tuple[str, ...]) -> None:
        assert effect_ids == tuple(sorted(effect_ids))
        backend_pids["finalizer"] = pid

    def capture_takeover_pid(pid: int, _effect_id: str) -> None:
        backend_pids["takeover"] = pid

    lifecycle = takeover_factory.execution.sink_effects._lifecycle
    monkeypatch.setattr(finalizer_factory.execution.sink_effects._finalization, "_after_effect_locks", check_finalizer_effect_lock_order)
    monkeypatch.setattr(lifecycle, "_after_effect_lock", capture_takeover_pid)

    finalizer_token = leader_coordination_token(finalizer_factory, built.run_id)
    takeover_token = leader_coordination_token(takeover_factory, built.run_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        finalization = pool.submit(finalizer_factory.execution.sink_effects.finalize, built.request, coordination_token=finalizer_token)
        winner = finalization.result(timeout=10)
        takeover = pool.submit(
            takeover_factory.execution.sink_effects.takeover_expired,
            built.effect_id,
            owner="worker-b",
            ttl=timedelta(seconds=30),
            coordination_token=takeover_token,
        )
        with pytest.raises(LandscapeRecordError, match="finalized sink effect cannot be taken over"):
            takeover.result(timeout=10)

    assert winner.effect.state.value == "finalized"
    assert winner.effect.effect_id == built.effect_id
    assert winner.artifact.path_or_uri == built.request.descriptor.path_or_uri
    assert winner.artifact.content_hash == built.request.descriptor.content_hash
    assert len(winner.state_ids) == 1
    assert len(winner.outcome_ids) == 1
    assert backend_pids["takeover"] != backend_pids["finalizer"]
    with db.read_only_connection() as conn:
        effect_row = conn.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id == built.effect_id)).one()
        operation_rows = conn.execute(select(operations_table).where(operations_table.c.sink_effect_id == built.effect_id)).fetchall()
        assert effect_row.state == "finalized"
        assert effect_row.lease_owner is None
        assert int(effect_row.generation) == built.generation
        assert len(operation_rows) == 1
        operation_row = operation_rows[0]
        assert operation_row.operation_id == built.operation_id
        assert operation_row.status == "completed"
        assert operation_row.completed_at is not None
        assert operation_row.duration_ms == built.request.operation_duration_ms
        assert operation_row.error_message is None
        assert operation_row.output_data_hash == stable_hash(
            {
                "accepted_ordinals": list(built.request.accepted_ordinals),
                "artifact_id": winner.artifact.artifact_id,
                "descriptor_hash": effect_row.result_descriptor_hash,
                "diverted_ordinals": list(built.request.diverted_ordinals),
                "effect_id": built.effect_id,
            }
        )
        assert (
            conn.scalar(select(func.count()).select_from(artifacts_table).where(artifacts_table.c.sink_effect_id == built.effect_id)) == 1
        )


def test_concurrent_finalization_retries_converge_on_winner_under_effect_lock(
    postgres_db: LandscapeDB, peer_db: LandscapeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Effect-linked artifact path: two crash-recovery retries of an already
    finalized effect serialize on the effect row and only then take the
    artifact FOR UPDATE — effect before artifact, never the reverse — and
    both converge on the identical winner with exactly one artifact row."""
    db = postgres_db
    setup_factory = make_factory(db)
    built = _build_in_flight_effect(setup_factory, name_prefix="retry-converge")
    first = setup_factory.execution.sink_effects.finalize(
        built.request, coordination_token=leader_coordination_token(setup_factory, built.run_id)
    )
    assert first.effect.state.value == "finalized"

    retry_a_factory = make_factory(db)
    retry_b_factory = make_factory(peer_db)

    # WHAT CANNOT HAPPEN HERE, AND WHY (ADR-048). Do not reintroduce the pause
    # or the `assert not retry_b.done()`.
    #
    # This proof used to hold retry A inside its effect locks, drive retry B up
    # to `_lock_stream_and_effects`, and assert B had not completed — showing B
    # blocked on the effect row class rather than reading a half-written
    # winner. Both retries are leader-fenced, and `fenced_leader_transaction`
    # holds the run's single seat row exclusive for the whole payload
    # transaction, so B now blocks on the SEAT before it ever reaches the
    # effect lock. B's non-completion is therefore no longer evidence about
    # the effect lock class: it is guaranteed one layer earlier, and an
    # assertion here would pass for the wrong reason.
    #
    # Convergence, which is what this proof is named for, does not need the
    # window. Two retries of an already-finalized effect must agree on one
    # winner and one artifact whichever order they run in, and that is
    # asserted in full below, together with each retry's ascending effect-lock
    # order and the two distinct backends.
    backend_pids: dict[str, int] = {}

    def check_a_effect_lock_order(pid: int, effect_ids: tuple[str, ...]) -> None:
        assert effect_ids == tuple(sorted(effect_ids))
        backend_pids["retry_a"] = pid

    def check_b_effect_lock_order(pid: int, effect_ids: tuple[str, ...]) -> None:
        assert effect_ids == tuple(sorted(effect_ids))
        backend_pids["retry_b"] = pid

    b_finalization = retry_b_factory.execution.sink_effects._finalization
    retry_a_token = leader_coordination_token(retry_a_factory, built.run_id)
    retry_b_token = leader_coordination_token(retry_b_factory, built.run_id)
    monkeypatch.setattr(retry_a_factory.execution.sink_effects._finalization, "_after_effect_locks", check_a_effect_lock_order)
    monkeypatch.setattr(b_finalization, "_after_effect_locks", check_b_effect_lock_order)

    with ThreadPoolExecutor(max_workers=2) as pool:
        retry_a = pool.submit(retry_a_factory.execution.sink_effects.finalize, built.request, coordination_token=retry_a_token)
        retry_b = pool.submit(retry_b_factory.execution.sink_effects.finalize, built.request, coordination_token=retry_b_token)
        winners: list[SinkEffectFinalizationResult] = [retry_a.result(timeout=10), retry_b.result(timeout=10)]

    assert backend_pids["retry_a"] != backend_pids["retry_b"]
    assert {winner.effect.effect_id for winner in winners} == {built.effect_id}
    assert {winner.artifact.artifact_id for winner in winners} == {first.artifact.artifact_id}
    assert all(winner.effect.state.value == "finalized" for winner in winners)
    assert all(winner.artifact.content_hash == built.request.descriptor.content_hash for winner in winners)
    with db.read_only_connection() as conn:
        assert (
            conn.scalar(select(func.count()).select_from(artifacts_table).where(artifacts_table.c.sink_effect_id == built.effect_id)) == 1
        )


def test_legacy_state_linked_artifact_outcome_contention_single_winner(
    postgres_db: LandscapeDB, peer_db: LandscapeDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy state-linked artifact path: two composed outcome writers for the
    same token follow token, state, artifact lock order; they serialize at the
    token class, exactly one outcome wins, the loser is rejected without
    deadlock, and the artifact witness row survives untouched."""
    db = postgres_db
    winner_factory = make_factory(db)
    loser_factory = make_factory(peer_db)
    run = winner_factory.run_lifecycle.begin_run(config={}, canonical_version="v1")
    source = register_test_node(
        winner_factory.data_flow, run.run_id, "legacy-artifact-source", node_type=NodeType.SOURCE, plugin_name="source"
    )
    failsink = register_test_node(
        winner_factory.data_flow, run.run_id, "legacy-artifact-failsink", node_type=NodeType.SINK, plugin_name="failsink"
    )
    payload = {"value": 1}
    _row, token = winner_factory.data_flow.create_row_with_token(
        coordination_token=leader_coordination_token(winner_factory, run.run_id),
        source_node_id=source,
        row_index=0,
        data=payload,
        source_row_index=0,
        ingest_sequence=0,
    )
    state = winner_factory.execution.begin_node_state(
        token_id=token.token_id,
        node_id=failsink,
        member_token=leader_coordination_token(winner_factory, run.run_id).membership,
        step_index=0,
        input_data=payload,
    )
    winner_factory.execution.complete_node_state(
        member_token=leader_coordination_token(winner_factory, run.run_id).membership,
        state_id=state.state_id,
        status=NodeStateStatus.COMPLETED,
        output_data={"written": True},
        duration_ms=1.0,
    )
    with fenced_leader_transaction(
        db.engine,
        token=leader_coordination_token(winner_factory, run.run_id),
        window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
        verb="test_legacy_sink_artifact",
    ) as conn:
        artifact = winner_factory.execution.artifacts.register_artifact(
            conn=conn,
            run_id=run.run_id,
            state_id=state.state_id,
            sink_node_id=failsink,
            artifact_type="test",
            path="memory://legacy/fallback-artifact",
            content_hash="ab" * 32,
            size_bytes=0,
        )

    winner_locked = threading.Event()
    release_winner = threading.Event()
    backend_pids: dict[str, int] = {}

    original_winner_lock = winner_factory.data_flow.outcomes.lock_token_outcome_dependencies

    def winner_lock(refs: tuple[TokenRef, ...], *, conn: Connection) -> None:
        original_winner_lock(refs, conn=conn)
        backend_pids["winner"] = int(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        winner_locked.set()
        assert release_winner.wait(timeout=5)

    original_loser_lock = loser_factory.data_flow.outcomes.lock_token_outcome_dependencies

    def loser_lock(refs: tuple[TokenRef, ...], *, conn: Connection) -> None:
        backend_pids["loser"] = int(conn.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
        original_loser_lock(refs, conn=conn)

    monkeypatch.setattr(winner_factory.data_flow.outcomes, "lock_token_outcome_dependencies", winner_lock)
    monkeypatch.setattr(loser_factory.data_flow.outcomes, "lock_token_outcome_dependencies", loser_lock)

    def record_fallback(factory: RecorderFactory) -> str:
        return factory.data_flow.record_token_outcome_leader(
            TokenRef(token_id=token.token_id, run_id=run.run_id),
            TerminalOutcome.TRANSIENT,
            TerminalPath.SINK_FALLBACK_TO_FAILSINK,
            sink_name="failsink",
            sink_node_id=failsink,
            artifact_id=artifact.artifact_id,
            error_hash="fallback-error",
            coordination_token=leader_coordination_token(factory, run.run_id),
        )

    with _observe_fence_attempt(peer_db) as (loser_attempted, loser_backend), ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(record_fallback, winner_factory)
        assert winner_locked.wait(timeout=5)
        loser = pool.submit(record_fallback, loser_factory)
        try:
            assert loser_attempted.wait(timeout=5)
            _assert_backend_blocked_by(db, waiter=loser_backend["pid"], blocker=backend_pids["winner"])
        finally:
            release_winner.set()
        outcome_id = winner.result(timeout=10)
        with pytest.raises(LandscapeRecordError, match="database rejected audit write"):
            loser.result(timeout=10)

    assert outcome_id
    assert backend_pids["winner"] != backend_pids["loser"]
    with db.read_only_connection() as conn:
        outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.token_id == token.token_id)).fetchall()
        assert len(outcomes) == 1
        assert outcomes[0].outcome == TerminalOutcome.TRANSIENT.value
        assert outcomes[0].path == TerminalPath.SINK_FALLBACK_TO_FAILSINK.value
        artifact_row = conn.execute(select(artifacts_table).where(artifacts_table.c.artifact_id == artifact.artifact_id)).one()
        assert artifact_row.produced_by_state_id == state.state_id
        assert artifact_row.content_hash == "ab" * 32
