"""Original PRIMARY preparation deadlines must not strand no-SDK effects.

Only scheduling/SDK I/O is controlled. The actual clock, prepared/begin SQL,
quota owner and receipts remain real; no witness or capability is fabricated.
"""

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer import continuation_primary as primary_source
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer
from elspeth.web.composer.prompt_guard import PromptGuardMount, PromptGuardRefused, prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationOwner
from elspeth.web.coordination.quota_authority import ProviderAttempt, _undispatched_cancellation_event_id, _usage_entry_id
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from elspeth.web.sessions import continuation_primary as primary_sql
from elspeth.web.sessions import service as sessions_source
from elspeth.web.sessions.continuation import (
    PreparedContinuationProvider,
    load_snapshot,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.helpers.continuation_app import observe_original_driver_failures
from tests.unit.web.composer.test_prompt_guard_admission import _GuardClient
from tests.unit.web.sessions.test_continuation_primary_result import primary_input
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST
from tests.unit.web.sessions.test_continuation_runtime_provider_history import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_runtime_provider_history import prepared_runtime, publish

operation_store = _operation_store


def _contains_original(error, target):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if current is target:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return False


class _OriginalPreparationExpiry:
    """Wait for the real admitted deadline, without changing any allowance."""

    def __init__(self):
        self.clocks = []
        self.triggered = False

    def capture(self, monkeypatch, policy):
        actual = ContinuationActionClock.__init__

        def initialize(clock, admitted):
            actual(clock, admitted)
            if admitted == policy:
                self.clocks.append(clock)

        monkeypatch.setattr(ContinuationActionClock, "__init__", initialize)

    def deadline(self):
        assert len(self.clocks) == 1, "the actual PRIMARY must own one original action clock"
        clock = self.clocks[0]
        return clock.started + clock.policy.preparation

    async def wait(self):
        deadline = self.deadline()
        while asyncio.get_running_loop().time() <= deadline:
            await asyncio.sleep(max(0.001, deadline - asyncio.get_running_loop().time()))
        self.triggered = True

    def wait_in_transaction(self):
        deadline = self.deadline()
        while time.monotonic() <= deadline:
            time.sleep(max(0.001, deadline - time.monotonic()))
        self.triggered = True


def _rows(engine, running):
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    fence = running.session_operation_context.fence
    with engine.connect() as conn:
        selected = load_snapshot(conn, session_id=sid, operation_id=operation)
        return {
            "effect": conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation))
            .mappings()
            .all(),
            "events": conn.execute(
                select(events).where(events.c.session_id == sid, events.c.operation_id == operation).order_by(events.c.sequence)
            )
            .mappings()
            .all(),
            "ordinals": conn.execute(select(ordinals).where(ordinals.c.session_id == sid, ordinals.c.operation_id == operation))
            .mappings()
            .all(),
            "physical": conn.execute(
                select(physical).where(
                    physical.c.session_id == sid,
                    physical.c.operation_id == fence.operation_id,
                    physical.c.operation_epoch == fence.operation_epoch,
                )
            )
            .mappings()
            .all(),
            "usage": conn.execute(select(usage).where(usage.c.session_id == sid, usage.c.source == "composer")).mappings().all(),
            "chunks": conn.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation)).mappings().all(),
            "snapshot": selected,
        }


def _require_zero_use_cancellation(engine, running, rows, attempt, model):
    assert type(attempt) is ProviderAttempt
    assert len(rows["physical"]) == len(rows["usage"]) == 1
    physical_row, ledger = rows["physical"][0], rows["usage"][0]
    entry_id = _usage_entry_id(session_id=str(running.claim.session_id), source="composer", run_id=None, call_id=attempt.attempt_id)
    assert physical_row["attempt_id"] == attempt.attempt_id and physical_row["settled_at"] is not None
    assert physical_row["ledger_entry_id"] == ledger["entry_id"] == entry_id
    assert ledger["model"] == model
    assert all(ledger[key] == 0 for key in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens", "reasoning_tokens"))
    with engine.connect() as conn:
        audit = conn.execute(
            select(messages).where(
                messages.c.session_id == str(running.claim.session_id),
                messages.c.id == _undispatched_cancellation_event_id(attempt.attempt_id),
            )
        ).one()
    assert audit.role == "audit" and audit.writer_principal == "compose_loop"
    assert audit.raw_content is None and audit.tool_calls is None
    document = json.loads(audit.content)
    assert document["kind"] == "provider_attempt.cancelled_before_dispatch"
    assert document["attempt_id"] == attempt.attempt_id and document["requested_model"] == model
    assert document["source"] == "composer" and document["operation_id"] == running.session_operation_context.fence.operation_id


def _require_atomic_no_sdk_refusal(engine, authority, running, prepared, *, stop, undispatched=None, cancelled=False):
    rows = _rows(engine, running)
    assert len(rows["effect"]) == 1
    effect = rows["effect"][0]
    assert effect["effect_key"] == prepared.binding.effect_key and effect["input_sha256"] == prepared.binding.input_sha256
    assert effect["disposition"] == "refused", "a proven no-SDK expiry must durably close its exact original effect"
    assert effect["receipt_key"] and effect["result_chunk_key"]
    assert effect["response_finished_at"] is None
    if undispatched is None:
        assert all(
            effect[field] is None for field in ("provider_attempt_id", "physical_sequence", "physical_generation", "requested_model")
        )
    else:
        assert effect["provider_attempt_id"] == undispatched[0].attempt_id
        assert effect["requested_model"] == undispatched[1]
        with engine.connect() as conn:
            hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == undispatched[0].attempt_id)).one()
        assert hold.state == "consumed"
    assert (effect["creator_sequence"], effect["creator_generation"]) == (
        running.claim.segment_sequence,
        running.claim.ownership_generation,
    )
    assert len(rows["ordinals"]) == 1 and rows["ordinals"][0]["next_ordinal"] == prepared.binding.ordinal + 1
    original_input = [row for row in rows["chunks"] if row["chunk_key"] == f"provider-input:{prepared.binding.input_sha256}"]
    assert len(original_input) == 1 and original_input[0]["codec"] == "composer.provider-input.v1"
    assert sha256(original_input[0]["payload"]).hexdigest() == original_input[0]["sha256"]
    refusal = [row for row in rows["chunks"] if row["chunk_key"] == effect["result_chunk_key"]]
    assert len(refusal) == 1 and refusal[0]["codec"] == "composer.primary-no-sdk-refusal.v1"
    payload = refusal[0]["payload"]
    assert sha256(payload).hexdigest() == refusal[0]["sha256"] and len(payload) == refusal[0]["byte_length"]
    document = json.loads(payload)
    assert type(document) is dict and canonical_json(document).encode("utf-8") == payload
    if undispatched is None:
        # Before-quota expiry must not invent an admission. An actually
        # admitted cancellation instead retains its real zero-use cohort.
        assert rows["physical"] == rows["usage"] == []
        assert document["undispatched_attempt_id"] is document["undispatched_model"] is None
    else:
        attempt, model = undispatched
        _require_zero_use_cancellation(engine, running, rows, attempt, model)
        assert (document["undispatched_attempt_id"], document["undispatched_model"]) == (attempt.attempt_id, model)
    root = decode_continuation_snapshot(rows["snapshot"].payload_json)
    frontier = [item for item in root.effect_frontier if item.effect_id == effect["effect_key"]]
    assert len(frontier) == 1 and (frontier[0].disposition, frontier[0].receipt_id) == ("refused", effect["receipt_key"])
    assert len([event for event in rows["events"] if event["tag"] == "effect_refused"]) == 1
    terminal = authority.get(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
    assert terminal is not None and terminal.status == "failed" and terminal.failure_code != "worker_lost"
    assert terminal.checkpoint_id == rows["snapshot"].checkpoint_id and terminal.journal_cursor == rows["snapshot"].cursor
    assert (terminal.failure_code == "request_cancelled") is stop
    assert (terminal.cancel_requested_at is not None) is stop
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "window,stop",
    (
        pytest.param("prepared_return", False, id="after-prepared-before-admission"),
        pytest.param("prepared_return", True, id="public-stop-wins"),
    ),
)
async def test_original_preparation_expiry_closes_no_sdk_effect_atomically(operation_store, tmp_path, monkeypatch, window, stop):
    arguments, runtime, sdk = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["required_work"].running
    assert running is not None
    authority = arguments["authority"]
    expiry = _OriginalPreparationExpiry()
    expiry.capture(monkeypatch, runtime.source.policy.actions.primary)
    actual_finish = primary_source.run_required_sql_finish_once
    prepared = []

    def request_stop():
        stopped = authority.request_owner_cancel(
            session_id=running.claim.session_id,
            operation_id=running.claim.operation_id,
            actor_user_id=runtime.bindings.user_id,
            auth_provider_type="local",
        )
        assert stopped is not None and stopped.cancel_requested_at is not None

    async def finish(ticket, operation, *args, **kwargs):
        outcome = await actual_finish(ticket, operation, *args, **kwargs)
        if type(outcome) is RequiredSQLReturned and type(outcome.value) is PreparedContinuationProvider:
            prepared.append(outcome.value)
            if stop:
                request_stop()
            if window == "prepared_return":
                await expiry.wait()
        return outcome

    monkeypatch.setattr(primary_source, "run_required_sql_finish_once", finish)
    try:
        # Allow only the original expiry/cancellation; unexpected failures
        # propagate with their real traceback before durable assertions.
        with suppress(TimeoutError, asyncio.CancelledError):
            await publish(operation_store, arguments, runtime)
        assert len(prepared) == 1 and sdk == []
        assert expiry.triggered, "the intended original-clock expiry window must execute"
        rows = _require_atomic_no_sdk_refusal(operation_store[0], authority, running, prepared[0], stop=stop)
        committed_dispatches = [event for event in rows["events"] if event["tag"] == "may_dispatch"]
        assert committed_dispatches == []
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_sdk_entry_cannot_be_relabelled_as_no_sdk_from_absent_response(operation_store, tmp_path, monkeypatch):
    arguments, runtime, _sdk = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["required_work"].running
    assert running is not None
    expiry = _OriginalPreparationExpiry()
    expiry.capture(monkeypatch, runtime.source.policy.actions.primary)
    actual_mark = ProviderCallCustody.mark_sdk_entered
    marked, entered = [], []
    original = TimeoutError("actual SDK entry crossed the original preparation deadline")

    def mark(custody):
        actual_mark(custody)
        marked.append(custody)

    async def sdk(**kwargs):
        entered.append(kwargs)
        await expiry.wait()
        raise original

    monkeypatch.setattr(ProviderCallCustody, "mark_sdk_entered", mark)
    monkeypatch.setattr(litellm, "acompletion", sdk)
    try:
        from elspeth.web.sessions.continuation_sdk_terminal_result import PrimarySdkTerminal

        result = await publish(operation_store, arguments, runtime)
        assert type(result) is PrimarySdkTerminal
        assert result.receipt.sdk_entered and not result.receipt.sdk_result_observed
        assert expiry.triggered and len(marked) == len(entered) == 1
        assert result.receipt.observation == "provider_timeout"
        rows = _rows(operation_store[0], running)
        assert len(rows["physical"]) == 1
        assert len(rows["effect"]) == 1 and rows["effect"][0]["disposition"] != "refused"
        assert not [event for event in rows["events"] if event["tag"] == "effect_refused"]
        assert not [row for row in rows["chunks"] if row["codec"] == "composer.primary-no-sdk-refusal.v1"]
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_committed_preparation_deferred_cancellation_retains_no_sdk_issuer_handle(operation_store, tmp_path, monkeypatch):
    arguments, runtime, sdk = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["required_work"].running
    assert running is not None
    authority = arguments["authority"]
    actual_prepare, actual_finish = authority.prepare_provider_action, primary_source.run_required_sql_finish_once
    entered, release = threading.Event(), threading.Event()
    prepared, committed, deferred = [], [], []

    def prepare(*args, **kwargs):
        result = actual_prepare(*args, **kwargs)
        committed.append(result)
        entered.set()
        assert release.wait(timeout=20), "the controller must release the actual committed SQL outcome"
        return result

    async def finish(ticket, operation, *args, **kwargs):
        outcome = await actual_finish(ticket, operation, *args, **kwargs)
        if type(outcome) is RequiredSQLReturned:
            if type(outcome.value) is PreparedContinuationProvider:
                prepared.append(outcome.value)
            if operation == authority.prepare_provider_action:
                deferred.extend(outcome.deferred_cancellations)
        return outcome

    monkeypatch.setattr(authority, "prepare_provider_action", prepare)
    monkeypatch.setattr(primary_source, "run_required_sql_finish_once", finish)
    task = asyncio.create_task(publish(operation_store, arguments, runtime))
    try:
        async with asyncio.timeout(15):
            while not entered.is_set():
                if task.done():
                    await task
                await asyncio.sleep(0.001)
        stopped = authority.request_owner_cancel(
            session_id=running.claim.session_id,
            operation_id=running.claim.operation_id,
            actor_user_id=runtime.bindings.user_id,
            auth_provider_type="local",
        )
        assert stopped is not None and stopped.cancel_requested_at is not None
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with suppress(TimeoutError, asyncio.CancelledError):
            await task
        assert len(prepared) == len(committed) == 1 and deferred, "observe the real finish-once cancellation outcome"
        assert sdk == []
        rows = _require_atomic_no_sdk_refusal(operation_store[0], authority, running, prepared[0], stop=True)
        assert not [event for event in rows["events"] if event["tag"] == "may_dispatch"]
        arguments["required_work"].coordinator.assert_completed()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        operation_store[1].release(running.session_operation_context)


@asynccontextmanager
async def _actual_expired_no_sdk_receipt(operation_store, tmp_path, monkeypatch):
    arguments, runtime, sdk = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["required_work"].running
    assert running is not None
    expiry = _OriginalPreparationExpiry()
    expiry.capture(monkeypatch, runtime.source.policy.actions.primary)
    actual_finish = primary_source.run_required_sql_finish_once
    prepared = []

    async def finish(ticket, operation, *args, **kwargs):
        outcome = await actual_finish(ticket, operation, *args, **kwargs)
        if type(outcome) is RequiredSQLReturned and type(outcome.value) is PreparedContinuationProvider:
            prepared.append(outcome.value)
            await expiry.wait()
        return outcome

    monkeypatch.setattr(primary_source, "run_required_sql_finish_once", finish)
    try:
        receipt = await publish(operation_store, arguments, runtime)
        assert type(receipt) is primary_sql.PrimaryNoSdkRefusalReceipt
        assert expiry.triggered and len(prepared) == 1 and sdk == []
        _require_atomic_no_sdk_refusal(operation_store[0], arguments["authority"], running, prepared[0], stop=False)
        arguments["required_work"].coordinator.assert_completed()
        yield arguments, running, prepared[0], receipt
    finally:
        operation_store[1].release(running.session_operation_context)


def _cold_no_sdk_receipt(conn, running, effect_key):
    return primary_sql.read_primary_no_sdk_refusal_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        effect_key=effect_key,
    )


@pytest.mark.asyncio
async def test_actual_no_sdk_receipt_round_trips_through_cold_sql_owner(operation_store, tmp_path, monkeypatch):
    async with _actual_expired_no_sdk_receipt(operation_store, tmp_path, monkeypatch) as (_arguments, running, prepared, receipt):
        # This owner takes only durable identity; no live issuer or permit is
        # supplied to prove the receipt from a fresh SQL connection.
        with operation_store[0].connect() as conn:
            cold = _cold_no_sdk_receipt(conn, running, prepared.binding.effect_key)
        assert type(cold) is primary_sql.PrimaryNoSdkRefusalReceipt
        assert cold == receipt and cold.refused.cursor + 1 == cold.terminal.journal_cursor


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("forged-receipt-identity", "partial-closed-document"))
async def test_cold_no_sdk_owner_refuses_forged_or_partial_receipt(operation_store, tmp_path, monkeypatch, corruption):
    async with _actual_expired_no_sdk_receipt(operation_store, tmp_path, monkeypatch) as (_arguments, running, prepared, receipt):
        with operation_store[0].connect() as conn:
            assert _cold_no_sdk_receipt(conn, running, prepared.binding.effect_key) == receipt
            payload = conn.execute(
                select(chunks.c.payload).where(
                    chunks.c.session_id == str(running.claim.session_id),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == receipt.result_chunk_key,
                )
            ).scalar_one()
        document = json.loads(payload)
        if corruption == "forged-receipt-identity":
            document["receipt_key"] = "foreign-no-sdk-receipt"
            expected = "terminal/effect identity"
        else:
            del document["source_disposition"]
            expected = "closed result codec"
        forged = encode_private_chunk(codec="composer.primary-no-sdk-refusal.v1", canonical=canonical_json(document))
        forged_key = private_chunk_key(forged)
        with operation_store[0].connect() as conn:
            transaction = conn.begin()
            try:
                # Keep actual payload bytes, UTF8 length and content address
                # internally correct: the opponent is semantic/closed-shape
                # forgery, rather than a trivial stale digest.
                put_chunk(
                    conn,
                    session_id=str(running.claim.session_id),
                    operation_id=running.claim.operation_id,
                    key=forged_key,
                    payload=forged.canonical.encode("utf-8"),
                    codec=forged.reference.codec,
                )
                changed = conn.execute(
                    update(effects)
                    .where(
                        effects.c.session_id == str(running.claim.session_id),
                        effects.c.operation_id == running.claim.operation_id,
                        effects.c.effect_key == prepared.binding.effect_key,
                    )
                    .values(result_chunk_key=forged_key)
                )
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError, match=expected):
                    _cold_no_sdk_receipt(conn, running, prepared.binding.effect_key)
            finally:
                transaction.rollback()
        with operation_store[0].connect() as conn:
            assert _cold_no_sdk_receipt(conn, running, prepared.binding.effect_key) == receipt


@pytest.mark.asyncio
async def test_committed_no_sdk_refusal_lost_ack_is_read_back_without_rewrite(operation_store, tmp_path, monkeypatch):
    actual_mark = PrimaryNoSdkIssuer.mark_refusal_committed
    actual_read = primary_sql.read_primary_no_sdk_refusal_on_connection
    marks, committed_readbacks = [], []
    original = OperationalError("no-sdk committed acknowledgement unavailable", {}, RuntimeError("injected lost acknowledgement"))

    def mark(issuer, permit):
        marks.append(permit)
        if len(marks) == 1:
            # The real authority calls this only after its transaction commits.
            # Leave the actual issuer unmarked and require its cold readback.
            raise original
        return actual_mark(issuer, permit)

    def read(conn, **kwargs):
        receipt = actual_read(conn, **kwargs)
        if receipt is not None:
            committed_readbacks.append(receipt)
        return receipt

    monkeypatch.setattr(PrimaryNoSdkIssuer, "mark_refusal_committed", mark)
    monkeypatch.setattr(primary_sql, "read_primary_no_sdk_refusal_on_connection", read)
    async with _actual_expired_no_sdk_receipt(operation_store, tmp_path, monkeypatch) as (_arguments, running, prepared, receipt):
        assert len(marks) == 2 and marks[0] is marks[1]
        assert marks[0].prepared is prepared
        assert committed_readbacks == [receipt]
        rows = _rows(operation_store[0], running)
        assert len([event for event in rows["events"] if event["tag"] == "effect_refused"]) == 1
        assert len([event for event in rows["events"] if event["tag"] == "settled"]) == 1
        with operation_store[0].connect() as conn:
            assert _cold_no_sdk_receipt(conn, running, prepared.binding.effect_key) == receipt


@asynccontextmanager
async def _actual_admitted_primary(operation_store, tmp_path, monkeypatch, *, disposition):
    arguments, runtime, sdk = await prepared_runtime(operation_store, tmp_path, monkeypatch)
    running = arguments["required_work"].running
    assert running is not None
    admitted, release_admission = asyncio.Event(), asyncio.Event()
    settled, release_settlement = asyncio.Event(), threading.Event()
    prepared, admissions, originals, deferred, writes = [], [], [], [], []
    actual_admit = ProviderCallCustody.admit
    actual_cancel = sessions_source.SessionServiceImpl.cancel_v4_continuation_primary_unentered
    actual_writer = primary_sql.refuse_v4_primary_unentered_on_connection
    actual_transaction = sessions_source.SessionServiceImpl._session_process_locked_begin
    actual_finish = primary_source.run_required_sql_finish_once
    loop = asyncio.get_running_loop()
    original = OperationalError("composite disposition unavailable", {}, RuntimeError("injected disposition failure"))
    faults = []

    async def admit(custody, *, model):
        await actual_admit(custody, model=model)
        assert custody.context is running.session_operation_context
        assert custody.continuation_primary_v4_admission is not None
        admissions.append((custody.attempt, model))
        admitted.set()
        try:
            await release_admission.wait()
        except asyncio.CancelledError as error:
            originals.append(error)
            raise

    def writer(conn, **values):
        assert values["admission"].attempt.attempt_id == admissions[0][0].attempt_id
        writes.append(values["permit"])
        if disposition == "failed-before-write":
            faults.append(original)
            raise original
        result = actual_writer(conn, **values)
        if disposition == "settled":
            loop.call_soon_threadsafe(settled.set)
            assert release_settlement.wait(timeout=20)
        return result

    @contextmanager
    def transaction(service, *args, **kwargs):
        before = len(writes)
        with actual_transaction(service, *args, **kwargs) as conn:
            yield conn
        if disposition == "unknown-after-commit" and len(writes) > before and not faults:
            faults.append(original)
            raise original

    async def cancel(service, **values):
        outcome = await actual_cancel(service, **values)
        deferred.extend(outcome.deferred_cancellations)
        return outcome

    async def finish(ticket, operation, *args, **kwargs):
        outcome = await actual_finish(ticket, operation, *args, **kwargs)
        if type(outcome) is RequiredSQLReturned and type(outcome.value) is PreparedContinuationProvider:
            prepared.append(outcome.value)
        return outcome

    monkeypatch.setattr(ProviderCallCustody, "admit", admit)
    monkeypatch.setattr(primary_sql, "refuse_v4_primary_unentered_on_connection", writer)
    monkeypatch.setattr(sessions_source.SessionServiceImpl, "_session_process_locked_begin", transaction)
    monkeypatch.setattr(sessions_source.SessionServiceImpl, "cancel_v4_continuation_primary_unentered", cancel)
    monkeypatch.setattr(primary_source, "run_required_sql_finish_once", finish)
    task = asyncio.create_task(publish(operation_store, arguments, runtime))
    try:
        async with asyncio.timeout(10):
            while not admitted.is_set():
                if task.done():
                    await task
                await asyncio.sleep(0.001)
        assert len(admissions) == len(prepared) == 1 and sdk == []
        yield {
            "arguments": arguments,
            "actor_user_id": runtime.bindings.user_id,
            "env_ref_names": runtime.env_ref_names,
            "running": running,
            "task": task,
            "prepared": prepared[0],
            "admission": admissions[0],
            "originals": originals,
            "deferred": deferred,
            "settled": settled,
            "release_settlement": release_settlement,
            "faults": faults,
            "failure": original,
            "sdk": sdk,
            "writes": writes,
        }
    finally:
        release_admission.set()
        release_settlement.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", (False, True), ids=("caller-cancellation", "public-stop-wins"))
async def test_admitted_no_sdk_group_keeps_both_original_cancellations_and_zero_use_cohort(operation_store, tmp_path, monkeypatch, stop):
    async with _actual_admitted_primary(operation_store, tmp_path, monkeypatch, disposition="settled") as case:
        arguments, running, task = case["arguments"], case["running"], case["task"]
        if stop:
            stopped = arguments["authority"].request_owner_cancel(
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                actor_user_id=case["actor_user_id"],
                auth_provider_type="local",
            )
            assert stopped is not None and stopped.cancel_requested_at is not None
        assert task.cancel("original-admitted-cancellation")
        await asyncio.wait_for(case["settled"].wait(), timeout=10)
        assert not task.done()
        assert task.cancel("original-settlement-cancellation")
        await asyncio.sleep(0)
        case["release_settlement"].set()
        receipt = await asyncio.wait_for(asyncio.shield(task), timeout=10)
        assert type(receipt) is primary_sql.PrimaryNoSdkRefusalReceipt
        assert len(case["originals"]) == len(case["deferred"]) == 1
        first, second = case["originals"][0], case["deferred"][0]
        assert first is not second and task.cancelling() == 2
        assert receipt.deferred_cancellations == (first, second)
        assert len(case["writes"]) == 1
        assert case["sdk"] == [] and case["faults"] == []
        _require_atomic_no_sdk_refusal(
            operation_store[0],
            arguments["authority"],
            running,
            case["prepared"],
            stop=stop,
            undispatched=case["admission"],
            cancelled=True,
        )
        with operation_store[0].connect() as conn:
            assert _cold_no_sdk_receipt(conn, running, case["prepared"].binding.effect_key) == receipt
        arguments["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ("failed-before-write", "unknown-after-commit"))
async def test_composite_no_sdk_failure_refuses_or_recovers_actual_committed_receipt(operation_store, tmp_path, monkeypatch, disposition):
    async with _actual_admitted_primary(operation_store, tmp_path, monkeypatch, disposition=disposition) as case:
        task = case["task"]
        assert task.cancel("original-unsettled-cancellation")
        if disposition == "failed-before-write":
            with pytest.raises(OperationalError) as caught:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            assert caught.value is case["failure"]
            assert len(case["writes"]) == len(case["faults"]) == 2
            rows = _rows(operation_store[0], case["running"])
            assert len(rows["effect"]) == len(rows["physical"]) == 1
            effect = rows["effect"][0]
            assert effect["disposition"] == "dispatched" and effect["receipt_key"] is effect["result_chunk_key"] is None
            assert rows["physical"][0]["settled_at"] is None and rows["usage"] == []
            assert not [event for event in rows["events"] if event["tag"] in ("effect_refused", "settled")]
            with operation_store[0].connect() as conn:
                hold = conn.execute(select(holds).where(holds.c.provider_attempt_id == case["admission"][0].attempt_id)).one()
                assert hold.state == "active"
                assert _cold_no_sdk_receipt(conn, case["running"], case["prepared"].binding.effect_key) is None
        else:
            receipt = await asyncio.wait_for(asyncio.shield(task), timeout=10)
            assert type(receipt) is primary_sql.PrimaryNoSdkRefusalReceipt
            assert len(case["writes"]) == len(case["faults"]) == 1
            assert receipt.deferred_cancellations == tuple(case["originals"])
            _require_atomic_no_sdk_refusal(
                operation_store[0],
                case["arguments"]["authority"],
                case["running"],
                case["prepared"],
                stop=False,
                undispatched=case["admission"],
                cancelled=True,
            )
            with operation_store[0].connect() as conn:
                assert _cold_no_sdk_receipt(conn, case["running"], case["prepared"].binding.effect_key) == receipt
        assert len(case["originals"]) == 1 and case["sdk"] == []
        assert all(error is case["failure"] for error in case["faults"])
        case["arguments"]["required_work"].coordinator.assert_completed()


async def _run_v4_guard_case(kwargs, produced, recorder, store):
    work = kwargs["required_work"]
    with prompt_guard_user_scope(REQUEST):
        return await primary_source.run_continuation_primary(
            service=kwargs["service"],
            authority=kwargs["authority"],
            selected=kwargs["selected"],
            producers=produced.producers,
            messages=list(produced.messages),
            tools=[],
            recorder=recorder,
            drained=RecorderDrainCursors(),
            required_work=work,
            provider_owner=ProviderInvocationOwner(service=store[3], required_work=work),
            timeout=5,
        )


@pytest.mark.asyncio
async def test_v4_guard_refusal_closes_pre_h_with_original_audit_and_no_physical_attempt(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    running = kwargs["inventory"].running
    assert running is not None
    guard = _GuardClient(PromptGuardDecision("blocked", "policy_blocked"))
    kwargs["service"]._provider_gateway._prompt_guard_mount = PromptGuardMount(guard)
    sdk = []

    async def completion(**_kwargs):
        sdk.append(1)
        raise AssertionError("guarded PRIMARY entered SDK")

    monkeypatch.setattr(litellm, "acompletion", completion)
    receipt = await _run_v4_guard_case(kwargs, produced, recorder, store)
    assert type(receipt) is primary_sql.PrimaryNoSdkRefusalReceipt
    assert len(guard.requests) == 1 and sdk == []
    rows = _rows(store[0], running)
    assert len(rows["effect"]) == 1 and rows["effect"][0]["disposition"] == "refused"
    assert rows["physical"] == rows["usage"] == []
    with store[0].connect() as conn:
        assert conn.execute(select(holds).where(holds.c.session_id == str(running.claim.session_id))).all() == []
        assert _cold_no_sdk_receipt(conn, running, rows["effect"][0]["effect_key"]) == receipt
        audits = conn.execute(
            select(messages).where(messages.c.session_id == str(running.claim.session_id), messages.c.role == "audit")
        ).all()
    guard_checks = [item["check"] for row in audits for item in row.tool_calls or () if item.get("_kind") == "prompt_guard_audit"]
    assert len(guard_checks) == 1
    assert (guard_checks[0]["role"], guard_checks[0]["disposition"], guard_checks[0]["reason"]) == ("composer", "blocked", "policy_blocked")
    terminal = receipt.terminal
    assert terminal.status == "failed" and terminal.failure_code == "http_error"
    public = json.loads(terminal.result_json)
    assert public["http_status"] == 422
    assert public["body"]["detail"]["error_type"] == "prompt_guard_blocked"
    assert public["body"]["detail"]["audit_id"] == guard_checks[0]["audit_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse", (True, False), ids=("handled-integrity", "successful-sdk"))
async def test_original_observer_preserves_actual_v4_integrity_refusal_after_admission(operation_store, tmp_path, monkeypatch, refuse):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    running = kwargs["inventory"].running
    original_error = AuditIntegrityError("PRIVATE_V4_NO_SDK_DIAGNOSTIC_MARKER")
    original_dispatch, original_reserve = ProviderCallCustody.require_cost_dispatch, PrimaryNoSdkIssuer.reserve_v4_refusal
    permits, reached, sdk = [], [], []

    def after_admission(custody):
        if custody.continuation_primary_v4_admission is not None:
            reached.append(custody.continuation_primary_v4_admission)
            if refuse:
                raise original_error
        return original_dispatch(custody)

    def reserve(issuer, **values):
        permit = original_reserve(issuer, **values)
        permits.append(permit)
        return permit

    async def completion(**_kwargs):
        sdk.append(1)
        assert not refuse, "original no-SDK refusal entered SDK"
        return litellm.ModelResponse(
            model="test/model",
            choices=[
                {"finish_reason": "stop", "message": {"role": "assistant", "content": "Original successful response.", "tool_calls": None}}
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "cost": 0.01},
        )

    monkeypatch.setattr(PrimaryNoSdkIssuer, "reserve_v4_refusal", reserve)
    diagnostics = observe_original_driver_failures(monkeypatch, running.claim.operation_id, session_id=running.claim.session_id)
    monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", after_admission)
    monkeypatch.setattr(litellm, "acompletion", completion)
    receipt = await _run_v4_guard_case(kwargs, produced, recorder, store)
    observed = [event for event in diagnostics if event["boundary"] == "primary_no_sdk_refusal"]
    if not refuse:
        assert type(receipt) is primary_source.PrimaryProviderResult
        assert reached and len(sdk) == 1 and permits == [] and observed == []
        rows = _rows(store[0], running)
        assert len(rows["effect"]) == len(rows["physical"]) == len(rows["usage"]) == 1
        assert rows["effect"][0]["disposition"] == "committed" and rows["physical"][0]["settled_at"] is not None
        assert rows["usage"][0]["prompt_tokens"] == 7 and rows["usage"][0]["completion_tokens"] == 3
        kwargs["required_work"].coordinator.assert_completed()
        return
    assert type(receipt) is primary_sql.PrimaryNoSdkRefusalReceipt
    assert len(reached) == len(permits) == 1 and permits[0].original_failure is original_error and sdk == []
    assert len(observed) == 1
    assert (observed[0]["event"], observed[0]["method"], observed[0]["reason"]) == (
        "reserved",
        "reserve_v4_refusal",
        "integrity_refused",
    )
    assert observed[0]["effect_key"] == receipt.effect_key == permits[0].prepared.binding.effect_key
    original = observed[0]["original"]
    assert original["type"] == "AuditIntegrityError" and original["first_line"] == "[redacted]" and original["frames"]
    assert original["line_sha256"] == sha256(original_error.args[0].encode()).hexdigest()
    assert "PRIVATE_V4_NO_SDK_DIAGNOSTIC_MARKER" not in json.dumps(diagnostics)
    rows = _rows(store[0], running)
    assert len(rows["effect"]) == len(rows["physical"]) == len(rows["usage"]) == 1
    assert rows["effect"][0]["disposition"] == "refused" and rows["physical"][0]["settled_at"] is not None
    assert rows["usage"][0]["prompt_tokens"] == rows["usage"][0]["completion_tokens"] == 0
    with store[0].connect() as conn:
        assert _cold_no_sdk_receipt(conn, running, receipt.effect_key) == receipt
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_v4_admitted_h_cannot_relabel_later_guard_error_as_no_sdk_refusal(operation_store, tmp_path, monkeypatch):
    kwargs, produced, recorder, _requests = await primary_input(operation_store, tmp_path, monkeypatch)
    store = operation_store
    running = kwargs["inventory"].running
    assert running is not None
    forged = PromptGuardRefused(decision=PromptGuardDecision("blocked", "policy_blocked"), audit_id=str(uuid4()))
    original = ProviderCallCustody.require_cost_dispatch
    original_reserve = PrimaryNoSdkIssuer.reserve_v4_refusal
    reservation_errors = []
    sdk = []

    def reserve(issuer, **kwargs):
        try:
            return original_reserve(issuer, **kwargs)
        except BaseException as error:
            reservation_errors.append(error)
            raise

    monkeypatch.setattr(PrimaryNoSdkIssuer, "reserve_v4_refusal", reserve)
    diagnostics = observe_original_driver_failures(monkeypatch, running.claim.operation_id, session_id=running.claim.session_id)

    def after_admission(custody):
        if custody.continuation_primary_v4_admission is not None:
            raise forged
        return original(custody)

    async def completion(**_kwargs):
        sdk.append(1)
        raise AssertionError("post-H guard error entered SDK")

    monkeypatch.setattr(ProviderCallCustody, "require_cost_dispatch", after_admission)
    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(AuditIntegrityError, match="V4 PRIMARY no-SDK refusal lacks proved unentered admitted custody") as raised:
        await _run_v4_guard_case(kwargs, produced, recorder, store)
    assert reservation_errors == [raised.value]
    assert not [event for event in diagnostics if event["boundary"] == "primary_no_sdk_refusal"]
    rows = _rows(store[0], running)
    assert sdk == [] and len(rows["effect"]) == len(rows["physical"]) == 1
    assert rows["effect"][0]["disposition"] == "dispatched"
    assert rows["physical"][0]["settled_at"] is None and rows["usage"] == []
    assert not [row for row in rows["chunks"] if row["codec"] == "composer.primary-no-sdk-refusal.v1"]
    with store[0].connect() as conn:
        assert _cold_no_sdk_receipt(conn, running, rows["effect"][0]["effect_key"]) is None
