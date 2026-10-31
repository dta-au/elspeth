"""U-35b: the yield at a real handoff-safe cut (design §2.2, §2.3).

The cut is the completed attachment application (``tool_ready``), reached by
the real owners. A yield writes a handoff checkpoint, queues exactly one
successor at ``(sequence + 1, generation + 1)`` and releases the fence, all in
one transaction.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_attachment_application_driver import apply_current_attachment_runtime
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_handoff import ContinuationYieldEvent
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost
from elspeth.web.sessions.continuation import load_snapshot, read_chunk
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.models import session_operation_fences_table as fences
from tests.unit.web.coordination.test_composer_continuation_claim_start import start_segment
from tests.unit.web.coordination.test_composer_operation_authority import _admission_rows
from tests.unit.web.sessions.test_continuation_attachment_application import _latest, application_case
from tests.unit.web.sessions.test_continuation_attachment_application import operation_store as _store

operation_store = _store


async def _handoff_safe(store, tmp_path, monkeypatch):
    arguments, runtime, ctx, call_model, source, _audit, _sdk, _path = await application_case(store, tmp_path, monkeypatch)
    await apply_current_attachment_runtime(
        runtime=runtime,
        selected=arguments["selected"],
        inventory=arguments["inventory"],
        ctx=ctx,
        call_model=call_model,
        effect_key=source.effect_key,
    )
    latest = await _latest(arguments)
    assert latest["selected"].stack.is_handoff_safe
    arguments["required_work"].coordinator.assert_completed()
    running = arguments["inventory"].running
    engine, _repository, _authority, _service, sid = store
    with engine.connect() as conn:
        head = conn.execute(
            select(states.c.id).where(states.c.session_id == str(sid)).order_by(states.c.version.desc()).limit(1)
        ).scalar_one_or_none()
        snapshot = load_snapshot(conn, session_id=str(sid), operation_id=running.claim.operation_id)
    yield_args = {
        "expected_head": None if head is None else type(running.claim.session_id)(head),
        "expected_checkpoint_id": snapshot.checkpoint_id,
        "expected_cursor": snapshot.cursor,
        "snapshot_json": snapshot.payload_json,
        "reason": "quantum",
    }
    return running, yield_args


@pytest.mark.asyncio
async def test_yield_commits_handoff_successor_and_releases_fence(operation_store, tmp_path, monkeypatch):
    engine, repository, authority, _service, sid = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    sequence, generation = running.claim.segment_sequence, running.claim.ownership_generation

    receipt = repository.commit_checkpoint_and_successor(running, **yield_args)

    assert (receipt.predecessor_sequence, receipt.predecessor_generation) == (sequence, generation)
    assert (receipt.successor_sequence, receipt.successor_generation) == (sequence + 1, generation + 1)
    with engine.connect() as conn:
        predecessor = conn.execute(select(segments).where(segments.c.sequence == sequence)).one()
        successor = conn.execute(select(segments).where(segments.c.sequence == sequence + 1)).one()
        attempt = conn.execute(select(attempts).where(attempts.c.sequence == sequence, attempts.c.generation == generation)).one()
        parent = conn.execute(select(jobs)).one()
        fence = conn.execute(select(fences).where(fences.c.session_id == str(sid))).one()
        checkpoint = conn.execute(select(checkpoints).where(checkpoints.c.checkpoint_id == receipt.checkpoint_id)).one()
        event = conn.execute(select(events).where(events.c.sequence == receipt.cursor)).one()
        payload = read_chunk(conn, session_id=str(sid), operation_id=running.claim.operation_id, key=event.payload_chunk_key)
        head = decode_continuation_snapshot(load_snapshot(conn, session_id=str(sid), operation_id=running.claim.operation_id).payload_json)
    assert (predecessor.state, predecessor.rollover_reason, predecessor.claim_token) == ("yielded", "quantum", None)
    assert attempt.outcome == "yielded" and attempt.revoked_at is not None
    assert (successor.state, successor.generation, successor.predecessor_sequence) == ("queued", generation + 1, sequence)
    assert successor.checkpoint_id == receipt.checkpoint_id
    assert (parent.status, parent.phase) == ("running", "continuation_queued")
    assert (parent.current_segment_sequence, parent.ownership_generation) == (sequence + 1, generation + 1)
    assert fence.released_at is not None
    assert checkpoint.kind == "handoff" and event.tag == "yielded"
    assert ContinuationYieldEvent.model_validate_json(payload.decode(), strict=True).reason == "quantum"
    # The successor's technical slice starts; request-wide totals survive.
    assert head.frames[0].slice_sequence == sequence + 1
    assert (head.frames[0].composition_slice, head.frames[0].discovery_slice) == (0, 0)

    repository.verify_yield(receipt)
    assert repository.read_yield_outcome(running) == receipt
    # Claimable at commit; a peer start does not make the read-back fail.
    (claim,) = authority.claim_segment(limit=1)
    assert (claim.segment_sequence, claim.ownership_generation) == (sequence + 1, generation + 1)
    start_segment(repository, claim, owner_instance_id=authority._owner_instance_id)
    repository.verify_yield(receipt)


@pytest.mark.asyncio
async def test_duplicate_yield_refuses_without_a_second_successor(operation_store, tmp_path, monkeypatch):
    engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    repository.commit_checkpoint_and_successor(running, **yield_args)
    before = _admission_rows(engine)
    with pytest.raises((ComposerOperationFenceLost, AuditIntegrityError)):
        repository.commit_checkpoint_and_successor(running, **yield_args)
    assert _admission_rows(engine) == before


@pytest.mark.asyncio
async def test_duplicate_yield_body_refuses_at_the_revoked_attempt(operation_store, tmp_path, monkeypatch):
    from sqlalchemy import event

    from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions import continuation as module
    from elspeth.web.sessions.locking import locked_session_transaction

    engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    repository.commit_checkpoint_and_successor(running, **yield_args)
    before = _admission_rows(engine)
    with engine.connect() as conn:
        current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
    # Use the actual current CAS, so the append cannot mask step 7. The
    # predecessor's genuine pre-yield payload still has its original slice;
    # the current payload has already rolled and cannot be rolled again for
    # that predecessor. Only its cursor advances to the actual current cut.
    predecessor = decode_continuation_snapshot(yield_args["snapshot_json"]).model_copy(update={"cursor": current.cursor})
    replay_args = {
        **yield_args,
        "expected_checkpoint_id": current.checkpoint_id,
        "expected_cursor": current.cursor,
        "snapshot_json": encode_continuation_snapshot(predecessor),
    }
    initial_proofs = []
    attempt_rowcounts = []

    def pass_initial_proof(_conn, context, *, allow_cancel, allow_expired_segment):
        assert context is running.session_operation_context
        initial_proofs.append((allow_cancel, allow_expired_segment))
        return True

    def observe_attempt_update(_conn, cursor, _statement, _parameters, execution, _executemany):
        compiled = execution.compiled
        if compiled is not None and compiled.isupdate and compiled.statement.table.name == attempts.name:
            attempt_rowcounts.append(cursor.rowcount)

    # Design §6.1/F12 explicitly isolates the body from the earlier live-owner
    # proof. Decode, slice roll, append and retained selection all remain real.
    monkeypatch.setattr(module, "require_continuation_mutation", pass_initial_proof)
    event.listen(engine, "after_cursor_execute", observe_attempt_update)
    try:
        with pytest.raises(ComposerOperationFenceLost) as caught, locked_session_transaction(engine, str(running.claim.session_id)) as conn:
            module.yield_segment_on_connection(conn, running, **replay_args, now=database_now(conn))
    finally:
        event.remove(engine, "after_cursor_execute", observe_attempt_update)
    assert initial_proofs == [(False, True)]
    # The revoked-at predicate itself refuses. A later segment/CAS/PK refusal
    # cannot substitute: omitting that predicate makes this rowcount one.
    assert attempt_rowcounts == [0]
    assert (caught.value.session_id, caught.value.operation_id, caught.value.attempt) == (
        running.claim.session_id,
        running.claim.operation_id,
        running.claim.attempt,
    )
    assert _admission_rows(engine) == before


@pytest.mark.asyncio
async def test_stop_wins_over_a_yield(operation_store, tmp_path, monkeypatch):
    engine, repository, authority, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    authority.request_cancel(
        session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
    )
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationCancelledDuringTurn):
        repository.commit_checkpoint_and_successor(running, **yield_args)
    assert _admission_rows(engine) == before
    assert repository.read_yield_outcome(running) is None


@pytest.mark.asyncio
async def test_yield_refuses_when_the_composition_head_moved(operation_store, tmp_path, monkeypatch):
    from uuid import uuid4

    engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError, match="composition head changed"):
        repository.commit_checkpoint_and_successor(running, **{**yield_args, "expected_head": uuid4()})
    assert _admission_rows(engine) == before


def _stale(running, field, yield_args):
    from dataclasses import replace

    fence = running.session_operation_context.fence
    if field in ("lease_token", "operation_epoch"):
        value = fence.lease_token + "x" if field == "lease_token" else fence.operation_epoch + 7
        context = replace(running.session_operation_context, fence=replace(fence, **{field: value}))
        return replace(running, session_operation_context=context), yield_args
    if field == "claim_token":
        return replace(running, claim=replace(running.claim, claim_token=running.claim.claim_token + "x")), yield_args
    if field == "attempt":
        return replace(running, claim=replace(running.claim, attempt=running.claim.attempt + 1)), yield_args
    if field == "generation":
        return replace(running, claim=replace(running.claim, ownership_generation=running.claim.ownership_generation + 1)), yield_args
    if field == "checkpoint":
        return running, {**yield_args, "expected_checkpoint_id": "another-checkpoint"}
    return running, {**yield_args, "expected_cursor": yield_args["expected_cursor"] + 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("lease_token", "operation_epoch", "claim_token", "attempt", "generation", "checkpoint", "cursor"))
async def test_stale_owner_cannot_yield_and_writes_nothing(operation_store, tmp_path, monkeypatch, field):
    from elspeth.web.coordination.contracts import SessionOperationFenceLost
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions import continuation as module
    from elspeth.web.sessions.continuation import ContinuationCASConflict
    from elspeth.web.sessions.locking import locked_session_transaction

    engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    stale, args = _stale(running, field, yield_args)
    refused = ContinuationCASConflict if field in ("checkpoint", "cursor") else ComposerOperationFenceLost
    before = _admission_rows(engine)
    # End to end, the first guard that sees the stale element refuses (design §2.6).
    with pytest.raises((ComposerOperationFenceLost, SessionOperationFenceLost, ContinuationCASConflict)):
        repository.commit_checkpoint_and_successor(stale, **args)
    assert _admission_rows(engine) == before
    # Targeted: with the live-attempt proof passed, the body's own predicates refuse (F12).
    monkeypatch.setattr(module, "require_continuation_mutation", lambda *_args, **_kwargs: True)
    with pytest.raises(refused), locked_session_transaction(engine, str(running.claim.session_id)) as conn:
        module.yield_segment_on_connection(conn, stale, **args, now=database_now(conn))
    assert _admission_rows(engine) == before


@pytest.mark.asyncio
async def test_yield_after_the_segment_hard_deadline_is_admitted(operation_store, tmp_path, monkeypatch):
    from datetime import timedelta

    from sqlalchemy import update

    from elspeth.web.coordination.database_clock import database_now

    engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    with engine.begin() as conn:
        past = database_now(conn) - timedelta(seconds=5)
        conn.execute(
            update(segments)
            .where(segments.c.sequence == running.claim.segment_sequence)
            .values(started_at=past - timedelta(seconds=20), cooperative_at=past - timedelta(seconds=10), deadline_at=past)
        )
    receipt = repository.commit_checkpoint_and_successor(running, **yield_args)
    assert receipt.successor_sequence == running.claim.segment_sequence + 1


def _unsafe_snapshot(snapshot_json, case):
    from elspeth.web.composer.continuation import EffectFrontierEntry
    from elspeth.web.composer.continuation_codec import encode_continuation_snapshot

    stack = decode_continuation_snapshot(snapshot_json)
    if case == "dispatched_effect":
        extra = EffectFrontierEntry(effect_id="tool:9", disposition="dispatched", receipt_id=None)
        stack = stack.model_copy(update={"effect_frontier": (*stack.effect_frontier, extra)})
    elif case == "pending_settlement":
        stack = stack.model_copy(update={"pending_settlement_ids": ("attempt-9",)})
    else:
        top = stack.frames[-1]
        stack = stack.model_copy(update={"frames": (*stack.frames[:-1], top.model_copy(update={"phase": "effect_reconciling"}))})
    assert not stack.is_handoff_safe
    return encode_continuation_snapshot(stack)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("dispatched_effect", "pending_settlement", "live_top_frame", "dispatched_effect_row"))
async def test_unsafe_cut_refuses_before_any_write(operation_store, tmp_path, monkeypatch, case):
    from sqlalchemy import update

    from elspeth.web.sessions.models import composer_continuation_effects_table as effects

    engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    if case == "dispatched_effect_row":
        # The frame flag is clean but an effect row is still dispatched (design §2.2 step 2).
        with engine.begin() as conn:
            assert conn.execute(update(effects).values(disposition="dispatched")).rowcount >= 1
        message = "unresolved dispatched effect"
    else:
        yield_args = {**yield_args, "snapshot_json": _unsafe_snapshot(yield_args["snapshot_json"], case)}
        message = "live or unsettled work"
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError, match=message):
        repository.commit_checkpoint_and_successor(running, **yield_args)
    assert _admission_rows(engine) == before


_STEP_STATEMENTS = {
    # (statement prefix, occurrence): the write that completes each step (design §2.2).
    5: ("INSERT INTO composer_continuation_checkpoints", 1),
    7: ("UPDATE composer_segment_attempts", 1),
    8: ("UPDATE composer_operation_segments", 1),
    9: ("INSERT INTO composer_operation_segments", 1),
    10: ("UPDATE composer_async_operations", 2),
    11: ("UPDATE session_operation_fences", 1),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("step", (5, 6, 7, 8, 9, 10, 11))
async def test_a_write_failure_after_each_step_rolls_the_whole_yield_back(operation_store, tmp_path, monkeypatch, step):
    from sqlalchemy import event

    from elspeth.web.sessions import continuation as module

    engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    before = _admission_rows(engine)

    class Injected(RuntimeError):
        pass

    if step == 6:
        original = module._retained_successor_action

        def failing(*args, **kwargs):
            original(*args, **kwargs)
            raise Injected

        monkeypatch.setattr(module, "_retained_successor_action", failing)
        listener = None
    else:
        prefix, occurrence = _STEP_STATEMENTS[step]
        seen = []

        def listener(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith(prefix.upper()):
                seen.append(statement)
                if len(seen) == occurrence:
                    raise Injected

        event.listen(engine, "after_cursor_execute", listener)
    try:
        with pytest.raises(Injected):
            repository.commit_checkpoint_and_successor(running, **yield_args)
    finally:
        if listener is not None:
            event.remove(engine, "after_cursor_execute", listener)
    assert _admission_rows(engine) == before
    monkeypatch.undo()
    # Nothing was half-applied: the same owner can still yield.
    receipt = repository.commit_checkpoint_and_successor(running, **yield_args)
    assert receipt.successor_sequence == running.claim.segment_sequence + 1


@pytest.mark.asyncio
async def test_read_yield_outcome_distinguishes_committed_from_uncommitted(operation_store, tmp_path, monkeypatch):
    _engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    assert repository.read_yield_outcome(running) is None
    receipt = repository.commit_checkpoint_and_successor(running, **yield_args)
    assert repository.read_yield_outcome(running) == receipt
    assert repository.read_yield_outcome(running).lease_validity_remaining is None


@pytest.mark.asyncio
async def test_read_yield_outcome_refuses_after_a_peer_recovered_the_owner(operation_store, tmp_path, monkeypatch):
    _engine, repository, _authority, _service, sid = operation_store
    running, _yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    # The yield never committed; the owner's lease ended and a peer recovered it.
    repository.release(running.session_operation_context)
    assert repository.recover_composer_segment(
        session_id=sid,
        operation_id=running.claim.operation_id,
        expected_generation=running.claim.ownership_generation,
        actor_user_id="alice",
        auth_provider_type="local",
    )
    with pytest.raises(AuditIntegrityError, match="revoked by another writer"):
        repository.read_yield_outcome(running)


@pytest.mark.asyncio
async def test_yield_readback_tolerates_a_recovered_successor(operation_store, tmp_path, monkeypatch):
    engine, repository, authority, _service, sid = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    receipt = repository.commit_checkpoint_and_successor(running, **yield_args)
    (claim,) = authority.claim_segment(limit=1)
    successor = start_segment(repository, claim, owner_instance_id=authority._owner_instance_id)
    repository.release(successor)
    assert repository.recover_composer_segment(
        session_id=sid,
        operation_id=running.claim.operation_id,
        expected_generation=claim.ownership_generation,
        actor_user_id="alice",
        auth_provider_type="local",
    )
    with engine.connect() as conn:
        row = conn.execute(select(segments).where(segments.c.sequence == receipt.successor_sequence)).one()
    # The successor moved past the generation the yield queued; read-back still holds.
    assert row.generation == receipt.successor_generation + 1
    repository.verify_yield(receipt)
    assert repository.read_yield_outcome(running) == receipt


@pytest.mark.asyncio
async def test_v2_saved_revisions_fold_across_yielded_sequences(operation_store, tmp_path, monkeypatch):
    from uuid import UUID

    from elspeth.web.coordination.composer_operation_authority import settle_composer_operation_on_connection
    from elspeth.web.sessions.composer_operation_errors import worker_lost_error
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.graph_recovery import checkpoint_data, empty_recovery_state
    from elspeth.web.sessions.locking import locked_session_transaction

    engine, repository, authority, service, sid = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    first = await service.save_composition_state(
        sid, checkpoint_data(empty_recovery_state()), provenance="session_seed", session_operation_context=running.session_operation_context
    )
    receipt = repository.commit_checkpoint_and_successor(running, **{**yield_args, "expected_head": UUID(str(first.id))})
    (claim,) = authority.claim_segment(limit=1)
    successor = ComposerOperationRunning(
        claim=claim, session_operation_context=start_segment(repository, claim, owner_instance_id=authority._owner_instance_id)
    )
    assert claim.segment_sequence == receipt.successor_sequence
    latest = await service.save_composition_state(
        sid,
        checkpoint_data(empty_recovery_state()),
        provenance="session_seed",
        session_operation_context=successor.session_operation_context,
    )
    with locked_session_transaction(engine, str(sid)) as conn:
        settle_composer_operation_on_connection(conn, successor, outcome=worker_lost_error(request_id=claim.operation_id))
    fences = {running.session_operation_context.fence.operation_id, successor.session_operation_context.fence.operation_id}
    with engine.connect() as conn:
        owned = conn.execute(select(states.c.id).where(states.c.creator_operation_id.in_(fences))).all()
        first_sequence = conn.execute(
            select(states.c.id).where(states.c.creator_operation_id == running.session_operation_context.fence.operation_id)
        ).all()
    assert first_sequence, "the first sequence must own saves for this case to discriminate"

    (outcome,), _cursor = authority.list_failure_outcomes(session_id=sid, actor_user_id="alice")

    assert outcome.saved_revisions.count == len(owned)
    assert outcome.saved_revisions.latest_state_id == latest.id


@pytest.mark.asyncio
async def test_a_cas_refused_yield_retries_under_the_same_lease(operation_store, tmp_path, monkeypatch):
    from uuid import uuid4

    from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition
    from elspeth.web.required_work import RequiredWorkSource
    from elspeth.web.sessions.continuation import ContinuationCASConflict
    from tests.unit.web.coordination.test_composer_continuation_handover import _adopted

    _engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    lease, coordinator = await _adopted(operation_store, running)
    async with lease:
        with pytest.raises(ContinuationCASConflict):
            await lease.consume_continuation_handoff(
                lambda: repository.commit_checkpoint_and_successor(running, **{**yield_args, "expected_head": uuid4()})
            )
        assert lease.disposition is SessionOperationLeaseDisposition.ACTIVE
        receipt = await lease.consume_continuation_handoff(lambda: repository.commit_checkpoint_and_successor(running, **yield_args))
    assert lease.disposition is SessionOperationLeaseDisposition.CONSUMED
    assert receipt.successor_sequence == running.claim.segment_sequence + 1
    handoffs = [ticket for ticket in coordinator.tickets if ticket.key.source is RequiredWorkSource.CONTINUATION_HANDOFF_SQL]
    assert len(handoffs) == 2
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_the_successor_runtime_read_accepts_the_rolled_slice(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning
    from elspeth.web.sessions.continuation_runtime import read_runtime_source_on_connection
    from elspeth.web.sessions.locking import locked_session_transaction
    from tests.unit.web.coordination.test_continuation_p4_recovery_start import current_selected

    engine, repository, authority, _service, sid = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    repository.commit_checkpoint_and_successor(running, **yield_args)
    (claim,) = authority.claim_segment(limit=1)
    successor = ComposerOperationRunning(
        claim=claim, session_operation_context=start_segment(repository, claim, owner_instance_id=authority._owner_instance_id)
    )
    selected, inventory = await current_selected(operation_store, successor)
    with locked_session_transaction(engine, str(sid)) as conn:
        source = read_runtime_source_on_connection(conn, successor, selected=selected, inventory=inventory)
    assert source.selected is selected


def _poll_claims_from_process(url: str, owner: str, ready, transaction_open, polled_before_commit):
    import time

    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.engine import create_session_engine

    engine = create_session_engine(url)
    authority = ComposerAsyncOperationAuthority(engine, owner_instance_id=owner, claim_lease_seconds=30)
    claims: list[tuple[int, int]] = []
    try:
        ready.set()
        assert transaction_open.wait(timeout=30), "the owner never reached the yield commit barrier"
        # The owner cannot commit until this actual database poll has returned.
        # Uncommitted yield writes must expose neither a claimable predecessor
        # nor a claimable successor to this independent process.
        before_commit_rows = _admission_rows(engine)
        before_commit = [(claim.segment_sequence, claim.ownership_generation) for claim in authority.claim_segment(limit=1)]
        polled_before_commit.set()
        deadline = time.monotonic() + 10
        settle_until = None
        while time.monotonic() < deadline and (settle_until is None or time.monotonic() < settle_until):
            for claim in authority.claim_segment(limit=1):
                claims.append((claim.segment_sequence, claim.ownership_generation))
                settle_until = time.monotonic() + 1.0
            time.sleep(0.05)
        return before_commit_rows, before_commit, claims
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("release_fence", (True, False), ids=("fence_released", "release_omitted_control"))
async def test_two_processes_race_a_yield_and_a_successor_claim(operation_store, tmp_path, monkeypatch, release_fence):
    import asyncio
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    from sqlalchemy import event

    from elspeth.web.coordination.database_clock import database_now

    engine, repository, *_ = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    before_yield = _admission_rows(engine)
    assert before_yield["composer_operation_segments"]
    if not release_fence:
        # Design §6.1's control: without the in-transaction fence release, the
        # same peer cannot claim the committed successor under the live fence.
        monkeypatch.setattr(type(repository), "_release_fence_on_connection", lambda *_args, **_kwargs: None)
    url = engine.url.render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        ready, transaction_open, polled_before_commit = manager.Event(), manager.Event(), manager.Event()

        def before_commit(_conn):
            # SQLAlchemy emits this event before the DBAPI commit. All yield
            # writes are still uncommitted while the peer performs its poll.
            transaction_open.set()
            assert polled_before_commit.wait(timeout=30), "the peer never polled during the open yield transaction"

        with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
            peer = pool.submit(_poll_claims_from_process, url, "peer-owner", ready, transaction_open, polled_before_commit)
            assert ready.wait(timeout=30), "the peer did not initialize its independent authority"
            event.listen(engine, "commit", before_commit)
            try:
                receipt = repository.commit_checkpoint_and_successor(running, **yield_args)
            finally:
                event.remove(engine, "commit", before_commit)
                transaction_open.set()
            uncommitted_rows, uncommitted_claims, claims = await asyncio.wait_for(asyncio.wrap_future(peer), timeout=45)
    # The peer saw the complete prior committed bundle, never a partial handoff.
    assert uncommitted_rows == before_yield
    assert uncommitted_claims == []
    with engine.connect() as conn:
        successor = conn.execute(select(segments).where(segments.c.sequence == receipt.successor_sequence)).one()
        fence = conn.execute(select(fences).where(fences.c.session_id == str(running.claim.session_id))).one()
        fence_still_live = conn.execute(
            select(fences.c.lease_expires_at > database_now(conn)).where(fences.c.session_id == str(running.claim.session_id))
        ).scalar_one()
    assert successor.state == "queued"
    if release_fence:
        # Exactly one claim, of the successor, and never of the predecessor.
        assert claims == [(receipt.successor_sequence, receipt.successor_generation)]
        assert successor.claim_owner_instance_id == "peer-owner"
        assert fence.released_at is not None
    else:
        assert claims == []
        assert successor.claim_token is None
        assert fence.released_at is None
        assert fence_still_live


@pytest.mark.asyncio
async def test_yield_and_handover_transaction_latency_is_recorded(operation_store, tmp_path, monkeypatch, record_property):
    import time
    from statistics import quantiles

    from elspeth.web.sessions.composer_operations import ComposerOperationRunning

    engine, repository, authority, _service, sid = operation_store
    running, yield_args = await _handoff_safe(operation_store, tmp_path, monkeypatch)
    timings: dict[str, list[float]] = {"yield": [], "handover": []}
    for kind in ("yield", "handover"):
        for _ in range(50):
            started = time.perf_counter()
            if kind == "yield":
                repository.commit_checkpoint_and_successor(running, **yield_args)
            else:
                repository.suspend_composer_segment(running)
            timings[kind].append(time.perf_counter() - started)
            (claim,) = authority.claim_segment(limit=1)
            running = ComposerOperationRunning(
                claim=claim, session_operation_context=start_segment(repository, claim, owner_instance_id=authority._owner_instance_id)
            )
            with engine.connect() as conn:
                head = load_snapshot(conn, session_id=str(sid), operation_id=running.claim.operation_id)
            yield_args = {
                **yield_args,
                "expected_checkpoint_id": head.checkpoint_id,
                "expected_cursor": head.cursor,
                "snapshot_json": head.payload_json,
            }
    for kind, values in timings.items():
        cuts = quantiles(values, n=100)
        record_property(f"{kind}_p50_seconds", round(cuts[49], 4))
        record_property(f"{kind}_p99_seconds", round(cuts[98], 4))
        # Half of Y_budget (design §3.5); this records, it does not prove the drain bound (F11).
        assert cuts[98] < 1.0, (kind, cuts[49], cuts[98])
