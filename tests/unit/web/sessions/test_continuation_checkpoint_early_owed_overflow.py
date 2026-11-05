"""Original paid EARLY H either completes or closes from its real capacity cut."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from threading import Event

import litellm
import pytest
from sqlalchemy import func, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.continuation_checkpoint_early_observation import observe_v4_early_checkpoint_returned_response
from elspeth.web.composer.continuation_checkpoint_early_physical import (
    checkpoint_early_detached_runtime,
    checkpoint_early_format_retry,
    checkpoint_early_physical_custody,
)
from elspeth.web.composer.continuation_checkpoint_projection import (
    CheckpointPassAccounting,
    ProjectedEarlyCheckpoint,
    project_early_checkpoint_result,
)
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError
from elspeth.web.required_work import RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions import continuation as continuation_store
from elspeth.web.sessions import continuation_checkpoint_early_owed_overflow as completion
from elspeth.web.sessions import continuation_checkpoint_early_return as early_return
from elspeth.web.sessions import continuation_checkpoint_paid_audit as paid_audit
from elspeth.web.sessions import continuation_iteration_return as iterations
from elspeth.web.sessions.continuation import load_snapshot, put_chunk
from elspeth.web.sessions.continuation_checkpoint_early_return import (
    complete_early_checkpoint_on_connection,
    read_early_complete_return_on_connection,
)
from elspeth.web.sessions.continuation_checkpoint_early_sql import EarlyCheckpointAdvanced, retain_early_complete_return_sync
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
    VerifiedCheckpointPaidOverflowTerminal,
    read_v4_checkpoint_end_owed_overflow_terminal_on_connection,
)
from elspeth.web.sessions.continuation_retained_capacity import (
    ContinuationRetainedCapacityExceeded,
    ProspectiveContinuationChunk,
    admit_retained_payload_chunks,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as ledger
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import _early_attempt, _early_case, _invoke_early
from tests.unit.web.sessions.test_continuation_checkpoint_early_physical import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_checkpoint_early_return_shapes import (
    _CLEAN,
    _admit_original_retry,
    _complete_original,
    _pass_rows,
)
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


@asynccontextmanager
async def _original_h(store, tmp_path, monkeypatch, *, malformed=False, retry=False):
    async with _early_case(store, tmp_path, monkeypatch) as (_store, owner, running, selected, primary_sdk):
        sdk = []

        if retry:

            async def first_malformed(**_request):
                sdk.append(1)
                return _reply(None)

            monkeypatch.setattr(litellm, "acompletion", first_malformed)
            async with _early_attempt(owner, selected) as first:
                with pytest.raises(_MalformedLLMResponseError):
                    await _invoke_early(first)
                completed, _returned, _projected = await _complete_original(first)
                assert completed.retry is not None
            selected = await _admit_original_retry(owner, running, completed.completed)

        async def actual_reply(**_request):
            sdk.append(1)
            return _reply(None if malformed else _CLEAN)

        monkeypatch.setattr(litellm, "acompletion", actual_reply)
        async with _early_attempt(owner, selected) as source:
            if malformed:
                with pytest.raises(_MalformedLLMResponseError):
                    await _invoke_early(source)
            else:
                await _invoke_early(source)
            custody = checkpoint_early_physical_custody(source)
            prepared, admission = custody.continuation_checkpoint_v4, custody.continuation_checkpoint_v4_admission
            assert prepared is not None and admission is not None
            returned = observe_v4_early_checkpoint_returned_response(
                source.capture.snapshot(),
                custody=custody,
                policy=owner.runtime.source.policy,
                attempt=admission.attempt,
                context=owner.runtime.bindings.session_operation_context,
                env_ref_names=owner.runtime.env_ref_names,
            )
            dispatched = await load_selected_continuation(
                authority=owner.authority,
                running=prepared.running,
                selected=admission.selected,
                required_work=owner.work,
            )
            inventory = await materialize_continuation_env_inventory(
                advisor=owner.service._advisor_checkpoint,
                authority=owner.authority,
                selected=dispatched,
                required_work=owner.work,
            )
            prior_retry = checkpoint_early_format_retry(source)
            accounting = (
                CheckpointPassAccounting(0, 1, None, None, False)
                if prior_retry is None
                else CheckpointPassAccounting(
                    0,
                    2,
                    prior_retry.marker.first_attempt_schema_valid,
                    prior_retry.child.accounting.first_attempt_accepted,
                    prior_retry.marker.format_reprompt_required,
                )
            )
            projected = await project_early_checkpoint_result(
                prepared=prepared,
                admission=admission,
                detached=checkpoint_early_detached_runtime(source),
                original=owner.runtime,
                selected=dispatched,
                inventory=inventory,
                custody=custody,
                returned=returned,
                accounting=accounting,
                physical_source=source,
            )
            assert type(projected) is ProjectedEarlyCheckpoint
            service = prepared.composer._require_sessions_service()
            owed = retain_early_complete_return_sync(service=service, prepared=prepared, admission=admission, returned=returned)
            assert len(sdk) == (2 if retry else 1) and len(primary_sdk) == 1
            yield service, source, prepared, owed, returned, projected, sdk


def _bytes(conn, running):
    return conn.execute(
        select(func.coalesce(func.sum(chunks.c.byte_length), 0)).where(
            chunks.c.session_id == str(running.claim.session_id),
            chunks.c.operation_id == running.claim.operation_id,
        )
    ).scalar_one()


def _measure_real_tail(service, prepared, owed, returned, projected, monkeypatch):
    """Execute only original SQL in a savepoint; publish no probe C/K/R."""
    before = stored_rows(service._engine)
    at_k = []
    original = iterations.retain_iteration_early_paid_completed_on_connection

    def observe_k(conn, *args, **kwargs):
        at_k.append(_bytes(conn, prepared.running))
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(iterations, "retain_iteration_early_paid_completed_on_connection", observe_k)
    measured = []
    # Ask the real admission kernel for every prefix's requirement. The added
    # virtual documents force its actual failure without changing stored policy
    # or writing probe DATA. This includes live H and the unpaid terminal floor.
    maximum = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
    payload = b'{"p":"' + b"x" * (maximum - 8) + b'"}'
    probes = tuple(ProspectiveContinuationChunk(f"capacity-probe:{index}", payload, "composer.history.v1") for index in range(16))
    probe_bytes = sum(len(item.payload) for item in probes)

    def measure_admission(conn, **kwargs):
        assert all(probe.key != item.key for probe in probes for item in kwargs["prospective"])
        with pytest.raises(ContinuationRetainedCapacityExceeded) as refused:
            admit_retained_payload_chunks(conn, **{**kwargs, "prospective": (*kwargs["prospective"], *probes)})
        measured.append((bool(at_k), refused.value.observed_bytes - probe_bytes))
        return admit_retained_payload_chunks(conn, **kwargs)

    sid = str(prepared.running.claim.session_id)
    with (
        service._session_process_locked_begin(sid) as conn,
        service._session_write_lock(conn, sid),
        service._session_composer_mutation_transaction(
            conn,
            session_id=sid,
            session_operation_context=prepared.running.session_operation_context,
            expected_kind=SessionOperationKind.COMPOSE,
            audit_only=True,
        ),
    ):
        initial = _bytes(conn, prepared.running)
        savepoint = conn.begin_nested()
        try:
            with monkeypatch.context() as probe:
                probe.setattr(continuation_store, "admit_retained_payload_chunks", measure_admission)
                probe.setattr(early_return, "admit_retained_payload_chunks", measure_admission)
                complete_early_checkpoint_on_connection(
                    conn,
                    service=service,
                    running=prepared.running,
                    prepared=prepared,
                    owed_admission=owed,
                    returned=returned,
                    projected=projected,
                )
            complete = _bytes(conn, prepared.running)
            assert len(at_k) == 1 and initial < at_k[0] < complete
        finally:
            savepoint.rollback()
        assert load_snapshot(conn, session_id=sid, operation_id=prepared.running.claim.operation_id) == owed.owed_selected
    assert stored_rows(service._engine) == before
    c_required = max(size for in_k, size in measured if not in_k) - initial
    complete_required = max(size for _in_k, size in measured) - initial
    assert c_required < complete_required
    return c_required, complete_required, at_k


def _leave_capacity(service, prepared, owed, *, available):
    """Admit unused original operation DATA without consuming the reserved H."""
    sid, operation = str(prepared.running.claim.session_id), prepared.running.claim.operation_id
    with service._session_process_locked_begin(sid) as conn, service._session_write_lock(conn, sid):
        remaining = owed.owed_hold.allowance_bytes - _bytes(conn, prepared.running) - available
        assert remaining >= 8 and available >= owed.owed_hold.reserved_bytes
        index = 0
        while remaining:
            size = min(remaining, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES)
            if 0 < remaining - size < 8:
                size -= 8
            payload = b'{"p":"' + b"x" * (size - 8) + b'"}'
            assert len(payload) == size <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
            put_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=f"original-capacity-data:{index}",
                payload=payload,
                codec="composer.history.v1",
            )
            remaining -= size
            index += 1
        assert owed.owed_hold.allowance_bytes - _bytes(conn, prepared.running) == available


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary", "malformed", "retry"),
    [("fit", False, False), ("c", False, False), ("k", False, False), ("k", True, False), ("k", True, True)],
)
async def test_original_h_exact_tail_or_actual_c_k_capacity_refusal(operation_store, tmp_path, monkeypatch, boundary, malformed, retry):
    async with _original_h(operation_store, tmp_path, monkeypatch, malformed=malformed, retry=retry) as case:
        service, _source, prepared, owed, returned, projected, sdk = case
        c_bytes, complete_bytes, at_k = _measure_real_tail(service, prepared, owed, returned, projected, monkeypatch)
        available = complete_bytes if boundary == "fit" else c_bytes - 1 if boundary == "c" else complete_bytes - 1
        _leave_capacity(service, prepared, owed, available=available)
        result = completion.complete_early_checkpoint_or_overflow_sync(
            service=service,
            prepared=prepared,
            owed=owed,
            returned=returned,
            projected=projected,
        )
        assert type(result) is completion.EarlyCheckpointCompletion and sdk == [1] * (2 if retry else 1)
        sid, operation = str(prepared.running.claim.session_id), prepared.running.claim.operation_id
        with locked_session_transaction(service._engine, sid) as conn:
            if boundary == "fit":
                assert type(result.result) is EarlyCheckpointAdvanced
                assert len(at_k) == 2
                assert load_snapshot(conn, session_id=sid, operation_id=operation) == result.result.completed
                assert len(_pass_rows(conn, sid)) == 1
                return
            verified = result.result
            assert type(verified) is VerifiedCheckpointPaidOverflowTerminal and verified.phase == "early"
            assert verified.hold_state == "consumed" and verified.source == owed.owed_hold
            assert verified.receipt.representation == "retained_tail" and verified.receipt.retryable is False
            assert verified.receipt.reason == "retained_payload_bytes" and verified.receipt.measurement == "exact"
            assert verified.receipt.observed_bytes > verified.receipt.limit_bytes == owed.owed_hold.allowance_bytes
            assert len(at_k) == (1 if boundary == "c" else 2)
            assert not _pass_rows(conn, sid)
            assert not decode_continuation_snapshot(verified.terminal.payload_json).pending_settlement_ids
            original_response = conn.execute(
                select(chunks.c.payload).where(
                    chunks.c.session_id == sid,
                    chunks.c.operation_id == operation,
                    chunks.c.chunk_key == f"value:{owed.response.reference.codec}:{owed.response.reference.digest}",
                )
            ).scalar_one()
            assert original_response == owed.response.canonical.encode("utf-8")
            cold = completion.read_v4_checkpoint_early_owed_overflow_terminal_on_connection(
                conn,
                session_id=prepared.running.claim.session_id,
                operation_id=operation,
                effect_key=owed.original.effect_key,
                names=prepared.env_ref_names,
            )
            assert cold == verified
            if malformed:
                original_call = paid_audit._closed_call

                def changed_public_projection(*args, **kwargs):
                    actual_call = original_call(*args, **kwargs)
                    return replace(actual_call, error_message="changed original public error projection")

                # Oppose DATA after the actual parser; the immutable audit row
                # remains unchanged in SQL.
                with monkeypatch.context() as opponent:
                    opponent.setattr(paid_audit, "_closed_call", changed_public_projection)
                    with pytest.raises(AuditIntegrityError, match="full original public projection"):
                        completion.read_v4_checkpoint_early_owed_overflow_terminal_on_connection(
                            conn,
                            session_id=prepared.running.claim.session_id,
                            operation_id=operation,
                            effect_key=owed.original.effect_key,
                            names=prepared.env_ref_names,
                        )
            with pytest.raises(AuditIntegrityError):
                read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
                    conn,
                    session_id=prepared.running.claim.session_id,
                    operation_id=operation,
                    effect_key=owed.original.effect_key,
                    names=prepared.env_ref_names,
                )
            assert returned.audit_call.status is (ComposerLLMCallStatus.MALFORMED_RESPONSE if malformed else ComposerLLMCallStatus.SUCCESS)
            settled = conn.execute(select(physical).where(physical.c.attempt_id == owed.original.attempt.attempt_id)).one()
            usage = conn.execute(select(ledger).where(ledger.c.entry_id == settled.ledger_entry_id)).one()
            assert (
                usage.prompt_tokens == returned.audit_call.prompt_tokens
                and usage.completion_tokens == returned.audit_call.completion_tokens
            )
        before = stored_rows(service._engine)
        with pytest.raises(AuditIntegrityError):
            completion.complete_early_checkpoint_or_overflow_sync(
                service=service,
                prepared=prepared,
                owed=owed,
                returned=returned,
                projected=projected,
            )
        assert stored_rows(service._engine) == before and sdk == [1] * (2 if retry else 1)


@pytest.mark.asyncio
async def test_original_h_rejects_foreign_projection_and_terminal_abort_restores_h(operation_store, tmp_path, monkeypatch):
    async with _original_h(operation_store, tmp_path, monkeypatch) as case:
        service, _source, prepared, owed, returned, projected, sdk = case
        before = stored_rows(service._engine)
        with pytest.raises(AuditIntegrityError, match="original detached projection"):
            completion.complete_early_checkpoint_or_overflow_sync(
                service=service,
                prepared=prepared,
                owed=owed,
                returned=returned,
                projected=replace(projected),
            )
        assert stored_rows(service._engine) == before
        with (
            service._session_process_locked_begin(str(prepared.running.claim.session_id)) as conn,
            pytest.raises(AuditIntegrityError, match="actual completion capacity refusal"),
        ):
            completion._settle_caught_overflow(
                conn,
                service=service,
                prepared=prepared,
                owed=owed,
                caught=completion._CaughtCapacityRefusal(
                    ContinuationRetainedCapacityExceeded(
                        observed_bytes=owed.owed_hold.allowance_bytes + 1,
                        limit_bytes=owed.owed_hold.allowance_bytes,
                    )
                ),
            )
        assert stored_rows(service._engine) == before
        _c_bytes, complete_bytes, _at_k = _measure_real_tail(service, prepared, owed, returned, projected, monkeypatch)
        _leave_capacity(service, prepared, owed, available=complete_bytes - 1)
        before = stored_rows(service._engine)
        actual = completion.write_checkpoint_paid_audit_cohort_on_connection
        sentinel = RuntimeError("original terminal audit storage refusal")

        def fail_after_original_cohort(*args, **kwargs):
            actual(*args, **kwargs)
            raise sentinel

        monkeypatch.setattr(completion, "write_checkpoint_paid_audit_cohort_on_connection", fail_after_original_cohort)
        with pytest.raises(RuntimeError) as observed:
            completion.complete_early_checkpoint_or_overflow_sync(
                service=service,
                prepared=prepared,
                owed=owed,
                returned=returned,
                projected=projected,
            )
        assert observed.value is sentinel and stored_rows(service._engine) == before and sdk == [1]
        with locked_session_transaction(service._engine, str(prepared.running.claim.session_id)) as conn:
            fresh = read_early_complete_return_on_connection(
                conn,
                session_id=str(prepared.running.claim.session_id),
                operation_id=prepared.running.claim.operation_id,
                selected=owed.owed_selected,
                names=prepared.env_ref_names,
            )
            assert fresh.new_hold == owed.owed_hold
            assert "elspeth.v4_checkpoint_paid_overflow_tail" not in conn.info


@pytest.mark.asyncio
async def test_original_h_commit_then_raised_delivery_preserves_commit_errors_and_cancellation(operation_store, tmp_path, monkeypatch):
    async with _original_h(operation_store, tmp_path, monkeypatch) as case:
        service, _source, prepared, owed, returned, projected, sdk = case
        _c_bytes, complete_bytes, _at_k = _measure_real_tail(service, prepared, owed, returned, projected, monkeypatch)
        _leave_capacity(service, prepared, owed, available=complete_bytes - 1)
        original = completion.complete_early_checkpoint_or_overflow_sync
        delivered = []
        failure = RuntimeError("actual committed EARLY SQL delivery failed")
        cancellation = asyncio.CancelledError("actual cancellation after EARLY commit")

        def commit_then_raise(**kwargs):
            delivered.append(original(**kwargs))
            raise BaseExceptionGroup("original delivery companions", [failure, cancellation])

        monkeypatch.setattr(completion, "complete_early_checkpoint_or_overflow_sync", commit_then_raise)
        with pytest.raises(completion.EarlyCheckpointCompletionDeliveryError) as captured:
            await completion.complete_early_checkpoint_or_overflow(
                service=service,
                prepared=prepared,
                owed=owed,
                returned=returned,
                projected=projected,
            )
        assert len(delivered) == 1 and captured.value.committed == delivered[0] and sdk == [1]
        group = captured.value.original_group
        assert group.exceptions[0].exceptions == (failure, cancellation)
        assert group.subgroup(asyncio.CancelledError).exceptions[0].exceptions == (cancellation,)
        verified = captured.value.committed.result
        assert type(verified) is VerifiedCheckpointPaidOverflowTerminal
        with locked_session_transaction(service._engine, str(prepared.running.claim.session_id)) as conn:
            transaction = conn.begin_nested()
            try:
                changed = conn.execute(
                    update(chunks)
                    .where(
                        chunks.c.session_id == str(prepared.running.claim.session_id),
                        chunks.c.operation_id == prepared.running.claim.operation_id,
                        chunks.c.codec == "composer.paid-overflow.v1",
                    )
                    .values(sha256="0" * 64)
                )
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError):
                    completion.read_v4_checkpoint_early_owed_overflow_terminal_on_connection(
                        conn,
                        session_id=prepared.running.claim.session_id,
                        operation_id=prepared.running.claim.operation_id,
                        effect_key=owed.original.effect_key,
                        names=prepared.env_ref_names,
                    )
            finally:
                transaction.rollback()
            assert (
                completion.read_v4_checkpoint_early_owed_overflow_terminal_on_connection(
                    conn,
                    session_id=prepared.running.claim.session_id,
                    operation_id=prepared.running.claim.operation_id,
                    effect_key=owed.original.effect_key,
                    names=prepared.env_ref_names,
                )
                == verified
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["none", "read_reservation", "state_projection", "read_projection"])
async def test_actual_finish_once_deferred_cancellation_keeps_original_terminal(operation_store, tmp_path, monkeypatch, failure_boundary):
    async with _original_h(operation_store, tmp_path, monkeypatch) as case:
        service, source, prepared, owed, returned, projected, sdk = case
        _c_bytes, complete_bytes, _at_k = _measure_real_tail(service, prepared, owed, returned, projected, monkeypatch)
        _leave_capacity(service, prepared, owed, available=complete_bytes - 1)
        original = completion.complete_early_checkpoint_or_overflow_sync
        loop = asyncio.get_running_loop()
        committed = asyncio.Event()
        release = Event()
        observed = []
        coordinator_type = type(source.phase_owner.phases.work.coordinator)
        original_validation = coordinator_type.validate_continuation_sql_outcome

        def validate(coordinator, *, ticket, expected_source, actual_outcome):
            if coordinator is source.phase_owner.phases.work.coordinator:
                observed.append(actual_outcome)
            return original_validation(coordinator, ticket=ticket, expected_source=expected_source, actual_outcome=actual_outcome)

        def hold_actual_commit(**kwargs):
            result = original(**kwargs)
            loop.call_soon_threadsafe(committed.set)
            assert release.wait(10), "actual committed callback was not released"
            return result

        refusal = RuntimeError(f"actual postcommit {failure_boundary} refusal")
        coordinator = source.phase_owner.phases.work.coordinator
        original_reserve = coordinator_type.reserve_pair
        original_begin = RequiredWorkTicket.begin_projection

        def reserve_pair(owner, sql_source, projection, **kwargs):
            if owner is coordinator and sql_source is RequiredWorkSource.CONTINUATION_READ_SQL and failure_boundary == "read_reservation":
                raise refusal
            return original_reserve(owner, sql_source, projection, **kwargs)

        def begin_projection(ticket):
            target = (
                RequiredWorkSource.CONTINUATION_STATE_PROJECTION
                if failure_boundary == "state_projection"
                else RequiredWorkSource.CONTINUATION_READ_PROJECTION
            )
            if failure_boundary in ("state_projection", "read_projection") and ticket.key.source is target:
                raise refusal
            return original_begin(ticket)

        monkeypatch.setattr(completion, "complete_early_checkpoint_or_overflow_sync", hold_actual_commit)
        monkeypatch.setattr(coordinator_type, "validate_continuation_sql_outcome", validate)
        monkeypatch.setattr(coordinator_type, "reserve_pair", reserve_pair)
        monkeypatch.setattr(RequiredWorkTicket, "begin_projection", begin_projection)
        task = asyncio.create_task(
            completion.complete_early_checkpoint_or_overflow(
                service=service,
                prepared=prepared,
                owed=owed,
                returned=returned,
                projected=projected,
            )
        )
        reached = asyncio.create_task(committed.wait())
        try:
            done, _pending = await asyncio.wait((task, reached), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                await task
                pytest.fail("original callback returned before the controlled commit boundary")
            task.cancel("actual EARLY delivery cancellation")
            await asyncio.sleep(0)
            release.set()
            if failure_boundary == "none":
                result = await task
            else:
                with pytest.raises(completion.EarlyCheckpointCompletionDeliveryError) as failed:
                    await task
                assert type(failed.value.committed.result) is VerifiedCheckpointPaidOverflowTerminal
                originals = failed.value.original_group.exceptions
                assert any(item is refusal for item in originals)
                deferred = next(item for item in originals if type(item) is asyncio.CancelledError)
        finally:
            release.set()
            reached.cancel()
            await asyncio.gather(reached, return_exceptions=True)
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)
        assert sdk == [1]
        if failure_boundary == "none":
            assert type(result.verified) is VerifiedCheckpointPaidOverflowTerminal
            assert len(result.deferred_cancellations) == 1
            deferred = result.deferred_cancellations[0]
        assert deferred.args == ("actual EARLY delivery cancellation",)
        assert any(any(item is deferred for item in outcome.deferred_cancellations) for outcome in observed)
