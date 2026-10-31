"""Terminal-only P4 from a real issued service call, through the existing writer.

These tests intentionally require the missing publication positive first. A
failure there is a prerequisite failure, not a reached corruption opposition.
No failed batch, constructed receipt, or decoded DATA supplies write authority.
"""

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_limits import CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES, CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_prepared_p4 import prepared_p4_members
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_terminal_p4_data import (
    decode_terminal_p4_input,
    decode_terminal_p4_result,
    terminal_p4_input_key,
)
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationLeaseDisposition
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error, request_cancelled_error
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationFenceLost
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot
from elspeth.web.sessions.continuation_p4 import (
    read_lost_terminal_p4_on_connection,
    read_p4_transaction_outcome_on_connection,
    terminal_service_failure,
)
from elspeth.web.sessions.continuation_p4_application_store import CommittedContinuationP4, StoredP4Application, StoredTerminalP4Application
from elspeth.web.sessions.continuation_recovery_candidates import list_recoverable_segments
from elspeth.web.sessions.continuation_tool_failure import RetainedToolFailureSource
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import sessions_table as sessions_rows
from elspeth.web.sessions.models import user_secrets_table as secret_rows
from elspeth.web.sessions.service import ComposerP4SQLCompletionUnknown, ComposerTerminalSQLCompletionUnknown
from tests.unit.web.composer._helpers import _persisted_tool_responses
from tests.unit.web.composer.test_service_failure_dispatch import _issued_review_case, _refuse_service, _review_call, _service_error
from tests.unit.web.composer.test_service_failure_publication import _exception_nodes, _finish_issued_batch
from tests.unit.web.coordination.test_continuation_p4_recovery_start import new_owners
from tests.unit.web.sessions import test_continuation_initialization as initialization_fixtures
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_p4_product import _contains_identity
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


def _registered_terminal_reference(store, monkeypatch):
    """Register the real inventory before ordinary bootstrap and authoring."""
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-default-fp-key")
    engine, _repository, _authority, _sessions, sid = store
    with engine.connect() as conn:
        session = conn.execute(select(sessions_rows).where(sessions_rows.c.id == str(sid))).one()
    user_store = UserSecretStore(engine=engine, master_key="synthetic-terminal-inventory-master-key")
    user_store.set_secret(
        "DB_URL", value="synthetic-terminal-private-value", user_id=session.user_id, auth_provider_type=session.auth_provider_type
    )
    resolver = ScopedSecretResolver(
        WebSecretService(user_store=user_store, server_store=ServerSecretStore(allowlist=())), session.auth_provider_type
    )
    actual_factory = initialization_fixtures.current_service

    def service_factory(current_store):
        assert current_store is store
        return actual_factory(current_store, secret_service=resolver)

    monkeypatch.setattr(initialization_fixtures, "current_service", service_factory)
    return session


def _readback(engine, phase):
    with locked_session_transaction(engine, str(phase.inventory.running.claim.session_id)) as conn:
        return read_p4_transaction_outcome_on_connection(conn, phase.writer_observation, env_ref_names=phase.inventory.names)


def _chunk_where(running, key):
    return (
        chunks.c.session_id == str(running.claim.session_id),
        chunks.c.operation_id == running.claim.operation_id,
        chunks.c.chunk_key == key,
    )


def _effect_where(running, key):
    return (
        effects.c.session_id == str(running.claim.session_id),
        effects.c.operation_id == running.claim.operation_id,
        effects.c.effect_key == key,
    )


async def _close_released_lease(store, owner, running):
    await owner.lease.close()
    assert owner.lease.disposition is SessionOperationLeaseDisposition.RELEASED
    expected = running.session_operation_context.fence
    with store[0].connect() as conn:
        actual = conn.execute(select(fences).where(fences.c.session_id == str(running.claim.session_id))).one()
    assert actual.operation_id == expected.operation_id and actual.operation_epoch == expected.operation_epoch
    assert actual.lease_token == expected.lease_token and actual.released_at is not None


def _require_publication(
    owner,
    running,
    original,
    cause,
    entered,
    sdk,
    observed,
    *,
    retained_error=None,
    expected_calls=("pending_review",),
    provider_calls=1,
    prior_sql=(),
):
    assert len(entered) == 1 and len(sdk) == provider_calls
    assert owner.failure_custody.original is original and original.__cause__ is cause
    phase = owner.p4
    assert phase is not None and phase.committed_outcome is not None, "terminal P4 publication prerequisite: real writer must commit"
    assert type(phase.physical) is RequiredSQLReturned
    assert type(phase.physical.value) is CommittedContinuationP4
    assert phase.physical.value is phase.committed_outcome
    observation = phase.writer_observation
    assert observation is not None and observation.committed
    assert phase.application is phase.committed_outcome.application is observation.application_candidate
    assert phase.committed_outcome.audit_outcome is observation.candidate
    assert phase.application_sql is None
    sql = tuple(ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
    assert len(sql) == len(prior_sql) + 1 and sql[:-1] == prior_sql
    assert all(ticket.complete for ticket in sql) and sql[-1]._finish_once_handoff is phase.physical
    if retained_error is None:
        assert observed is original, "the same service original must survive successful terminal publication"
    else:
        assert _contains_identity(observed, original) and _contains_identity(observed, retained_error)
    assert owner.failure_custody.wrapper is None
    original_runtime = owner.terminal_completion.runtime
    assert owner.terminal_completion.p4 is phase
    assert owner.runtime is phase.runtime and owner.runtime is not original_runtime
    assert owner.runtime.graph.batch is original_runtime.graph.batch
    assert (
        replace(owner.runtime, graph=owner.runtime.graph.with_drained(original_runtime.graph.drained), drained=original_runtime.drained)
        == original_runtime
    )
    assert owner.runtime.graph.batch.batch.service_failure is original
    invocation = owner.runtime.live.recorder.invocations[-1]
    assert invocation.status is ComposerToolStatus.SUCCESS
    assert invocation.error_class is invocation.private_failure is None
    assert invocation.tool_call_id == "pending_review"
    rows = _persisted_tool_responses(owner.service, str(running.claim.session_id))
    assert [row["tool_call_id"] for row in rows] == list(expected_calls)
    return phase


@asynccontextmanager
async def _terminal_case(store, tmp_path, monkeypatch, *, calls=None):
    async with _issued_review_case(store, tmp_path, monkeypatch, calls=calls) as (owner, running, sdk):
        original, cause = _service_error("admission"), RuntimeError("live terminal service cause must remain private")
        entered = _refuse_service(
            store[3], monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        observed = await _finish_issued_batch(owner)
        phase = _require_publication(
            owner,
            running,
            original,
            cause,
            entered,
            sdk,
            observed,
            expected_calls=("pending_review",) if calls is None else tuple(call[0] for call in calls),
        )
        assert await asyncio.to_thread(_readback, store[0], phase) is phase.committed_outcome.audit_outcome
        yield owner, running, sdk, original, cause, entered, phase


@pytest.mark.asyncio
async def test_terminal_p4_healthy_ordinary_neighbor_retains_portable_application(operation_store, tmp_path, monkeypatch):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        assert await _finish_issued_batch(owner) is None
        assert owner.p4 is not None and owner.p4.committed_outcome is not None
        assert type(owner.p4.application) is StoredP4Application
        assert await asyncio.to_thread(_readback, operation_store[0], owner.p4) is owner.p4.committed_outcome.audit_outcome
        with operation_store[0].connect() as conn:
            terminal = conn.execute(select(chunks).where(*_chunk_where(running, terminal_p4_input_key(owner.p4.commit.effect_key)))).all()
        assert terminal == [] and len(sdk) == 1
        assert owner.terminal_completion is None


@pytest.mark.asyncio
async def test_terminal_p4_real_service_publication_binds_complete_input_and_actual_receipt(operation_store, tmp_path, monkeypatch):
    async with _terminal_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk, original, cause, entered, phase):
        application = phase.application
        assert type(application) is not StoredP4Application
        source = decode_terminal_p4_input(application.input_document, env_ref_names=phase.inventory.names)
        result = decode_terminal_p4_result(
            application.result_document, input_document=application.input_document, env_ref_names=phase.inventory.names
        )
        assert source.prepared is not phase.prepared  # DATA decoding must not manufacture native owner identity.
        assert source.semantic_input_sha256 == result.receipt.input_sha256 == phase.commit.input_sha256
        assert source.checkpoint_id == phase.selected.checkpoint_id == phase.commit.expected_checkpoint_id
        assert source.cursor == phase.selected.stack.cursor == phase.commit.expected_cursor
        assert source.issue.tool_call_id == "pending_review"
        assert source.issued_effect_key == owner.terminal_completion.issued_effect_key
        assert result.input == application.input_document.reference
        assert result.drained == owner.runtime.drained
        with operation_store[0].connect() as conn:
            marker = conn.execute(select(chunks).where(*_chunk_where(running, terminal_p4_input_key(phase.commit.effect_key)))).one()
            effect = conn.execute(select(effects).where(*_effect_where(running, phase.commit.effect_key))).one()
            receipt = conn.execute(select(chunks).where(*_chunk_where(running, effect.result_chunk_key))).one()
        assert marker.codec == application.input_document.reference.codec
        assert marker.payload == application.input_document.canonical.encode("utf-8")
        assert marker.sha256 == application.input_document.reference.digest
        assert effect.disposition == "committed" and effect.input_sha256 == source.semantic_input_sha256
        assert receipt.codec == application.result_document.reference.codec
        assert receipt.payload == application.result_document.canonical.encode("utf-8")
        assert receipt.sha256 == application.result_document.reference.digest
        assert str(original).encode() not in marker.payload + receipt.payload
        assert str(cause).encode() not in marker.payload + receipt.payload
        assert len(entered) == len(sdk) == 1 and original.__cause__ is cause


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("missing_marker", "marker_codec", "marker_bytes", "result_bytes", "semantic_input", "issued_source"))
async def test_terminal_p4_readback_refuses_changed_sql_source_and_restores_positive(operation_store, tmp_path, monkeypatch, change):
    async with _terminal_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk, original, cause, entered, phase):
        before = stored_rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            nested = conn.begin_nested()
            try:
                marker_key = terminal_p4_input_key(phase.commit.effect_key)
                effect = conn.execute(select(effects).where(*_effect_where(running, phase.commit.effect_key))).one()
                if change == "missing_marker":
                    changed = conn.execute(delete(chunks).where(*_chunk_where(running, marker_key)))
                elif change == "marker_codec":
                    changed = conn.execute(
                        update(chunks).where(*_chunk_where(running, marker_key)).values(codec="composer.terminal-p4-result.v1")
                    )
                elif change in ("marker_bytes", "result_bytes"):
                    key = marker_key if change == "marker_bytes" else effect.result_chunk_key
                    current = conn.execute(select(chunks).where(*_chunk_where(running, key))).one()
                    changed = conn.execute(update(chunks).where(*_chunk_where(running, key)).values(payload=b" " + current.payload[1:]))
                elif change == "semantic_input":
                    changed = conn.execute(
                        update(effects).where(*_effect_where(running, phase.commit.effect_key)).values(input_sha256="0" * 64)
                    )
                else:
                    assert change == "issued_source"
                    changed = conn.execute(
                        update(effects)
                        .where(*_effect_where(running, owner.terminal_completion.issued_effect_key))
                        .values(input_sha256="0" * 64)
                    )
                assert changed.rowcount == 1, "the actual retained source mutation must be reached"
                with pytest.raises(AuditIntegrityError):
                    read_p4_transaction_outcome_on_connection(conn, phase.writer_observation, env_ref_names=phase.inventory.names)
            finally:
                nested.rollback()
            assert read_p4_transaction_outcome_on_connection(conn, phase.writer_observation, env_ref_names=phase.inventory.names) is (
                phase.committed_outcome.audit_outcome
            )
        assert stored_rows(operation_store[0]) == before
        assert owner.failure_custody.original is original and original.__cause__ is cause
        assert len(entered) == len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "before_commit", "after_commit", "unknown_readback"))
async def test_terminal_p4_transaction_boundaries_keep_real_original_and_one_physical_writer(operation_store, tmp_path, monkeypatch, fault):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        service = operation_store[3]
        original, cause = _service_error("sql"), RuntimeError("same native service cause")
        entered = _refuse_service(
            service, monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        actual_begin = type(service)._session_process_locked_begin
        physical_error = OperationalError("terminal commit delivery control", None, RuntimeError("same DBAPI original"))
        before_error, readback_error = RuntimeError("before actual terminal commit"), RuntimeError("actual readback unavailable")
        reached = []

        @contextmanager
        def begin(self, session_id):
            candidate = None
            with actual_begin(self, session_id) as conn:
                yield conn
                if self is service and owner.p4 is not None:
                    observation = owner.p4.writer_observation
                    if observation is not None and observation.application_candidate is not None and not observation.committed:
                        candidate = observation
                        reached.append(candidate)
                        if fault == "before_commit":
                            raise before_error
            if candidate is not None and fault in ("after_commit", "unknown_readback"):
                raise physical_error

        def unknown(self, observation, *, original, env_ref_names):
            assert self is service and original is physical_error
            observation.original_error = original
            error = ComposerP4SQLCompletionUnknown(observation)
            raise error from BaseExceptionGroup("actual terminal commit delivery and readback", (original, readback_error))

        monkeypatch.setattr(type(service), "_session_process_locked_begin", begin)
        if fault == "unknown_readback":
            monkeypatch.setattr(type(service), "_readback_continuation_p4_transaction", unknown)
        observed = await _finish_issued_batch(owner)
        assert len(reached) == 1, "terminal writer prerequisite: actual transaction must reach its application candidate"
        phase = owner.p4
        assert phase is not None and phase.writer_observation is reached[0]
        observation = phase.writer_observation
        assert owner.failure_custody.original is original and original.__cause__ is cause
        assert _contains_identity(observed, original) and len(entered) == len(sdk) == 1
        sql = tuple(ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
        assert len(sql) == 1 and sql[0].complete and sql[0]._finish_once_handoff is phase.physical
        if fault in ("none", "after_commit"):
            _require_publication(
                owner,
                running,
                original,
                cause,
                entered,
                sdk,
                observed,
                retained_error=physical_error if fault == "after_commit" else None,
            )
            assert await asyncio.to_thread(_readback, operation_store[0], phase) is phase.committed_outcome.audit_outcome
            if fault == "after_commit":
                assert observation.original_error is physical_error and observation.readback is not None
        else:
            assert type(phase.physical) is RequiredSQLRaised
            assert phase.publication is None and phase.application is None and not observation.committed
            if fault == "before_commit":
                assert observation.known_not_committed and _contains_identity(observed, before_error)
                await _close_released_lease(operation_store, owner, running)
                (candidate,) = list_recoverable_segments(operation_store[0], limit=10)
                retained = stored_rows(operation_store[0])
                with pytest.raises(ComposerTerminalSQLCompletionUnknown, match="no actual completion"):
                    operation_store[1].reconcile_unavailable_composer_segment(
                        candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
                    )
                assert stored_rows(operation_store[0]) == retained
                assert owner.failure_custody.original is original and original.__cause__ is cause
            else:
                assert type(phase.physical.error) is ComposerP4SQLCompletionUnknown
                assert not observation.known_not_committed
                assert _contains_identity(observed, physical_error) and _contains_identity(observed, readback_error)


@pytest.mark.asyncio
async def test_terminal_p4_stop_after_real_issue_finishes_original_service_prefix(operation_store, tmp_path, monkeypatch):
    calls = (_review_call(), ("must_not_run", "set_metadata", {"patch": {"name": "Forbidden suffix"}}))
    async with _issued_review_case(operation_store, tmp_path, monkeypatch, calls=calls) as (owner, running, sdk):
        original, cause = _service_error("admission"), RuntimeError("issued-before-Stop original cause")
        entered = _refuse_service(
            operation_store[3], monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        refuse = operation_store[3].create_pending_interpretation_event

        async def stop_then_refuse(**kwargs):
            owner.authority.request_cancel(
                session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
            )
            owner.observe_stop()
            return await refuse(**kwargs)

        monkeypatch.setattr(operation_store[3], "create_pending_interpretation_event", stop_then_refuse)
        observed = await _finish_issued_batch(owner)
        phase = _require_publication(owner, running, original, cause, entered, sdk, observed)
        assert [item.call.id for item in owner.runtime.graph.batch.batch.tool_outcomes] == ["pending_review"]
        assert await asyncio.to_thread(_readback, operation_store[0], phase) is phase.committed_outcome.audit_outcome


def _contains_fence_refusal(error):
    if isinstance(error, (ComposerOperationFenceLost, SessionOperationFenceLost)):
        return True
    if isinstance(error, BaseExceptionGroup) and any(_contains_fence_refusal(child) for child in error.exceptions):
        return True
    return error.__cause__ is not None and _contains_fence_refusal(error.__cause__)


@pytest.mark.asyncio
async def test_terminal_p4_released_issued_fence_cannot_publish_with_live_original(operation_store, tmp_path, monkeypatch):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        original, cause = _service_error("admission"), RuntimeError("released issued owner cause")
        entered = _refuse_service(
            operation_store[3], monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        refuse = operation_store[3].create_pending_interpretation_event
        released = []

        async def release_then_refuse(**kwargs):
            await _close_released_lease(operation_store, owner, running)
            released.append(stored_rows(operation_store[0]))
            return await refuse(**kwargs)

        monkeypatch.setattr(operation_store[3], "create_pending_interpretation_event", release_then_refuse)
        observed = await _finish_issued_batch(owner)
        assert len(released) == len(entered) == len(sdk) == 1
        assert owner.failure_custody.original is original and original.__cause__ is cause
        assert _contains_identity(observed, original)
        assert _contains_fence_refusal(observed), "must reach the actual lost-fence refusal, not a missing terminal API"
        assert stored_rows(operation_store[0]) == released[0]
        assert owner.p4 is None or owner.p4.committed_outcome is None


@pytest.mark.asyncio
async def test_terminal_p4_live_and_fresh_sql_readers_recognize_mode_before_ordinary_restore(operation_store, tmp_path, monkeypatch):
    async with _terminal_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk, original, cause, entered, phase):
        before = stored_rows(operation_store[0])
        current = await asyncio.to_thread(owner.authority.read_continuation_tool_failure, running, env_ref_names=phase.inventory.names)
        _engine, _repository, cold_authority, _sessions, _sid = new_owners(operation_store)
        cold = await asyncio.to_thread(cold_authority.read_continuation_tool_failure, running, env_ref_names=phase.inventory.names)
        assert current is not None and cold is not None, "terminal mode must be recognized before ordinary failed-batch restore"
        assert not isinstance(current, RetainedToolFailureSource) and type(cold) is type(current)
        assert cold == current and cold is not current
        assert stored_rows(operation_store[0]) == before
        assert len(entered) == len(sdk) == 1 and owner.failure_custody.original is original and original.__cause__ is cause


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_marker", (False, True), ids=("terminal-no-replay", "missing-terminal-marker"))
async def test_terminal_p4_lost_owner_recognition_precedes_ordinary_recovery(operation_store, tmp_path, monkeypatch, missing_marker):
    async with _terminal_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk, original, cause, entered, phase):
        await _close_released_lease(operation_store, owner, running)
        (candidate,) = list_recoverable_segments(operation_store[0], limit=10)
        before = stored_rows(operation_store[0])
        marker_key = terminal_p4_input_key(phase.commit.effect_key)
        with operation_store[0].begin() as conn:
            marker = dict(conn.execute(select(chunks).where(*_chunk_where(running, marker_key))).mappings().one())
            if missing_marker:
                assert conn.execute(delete(chunks).where(*_chunk_where(running, marker_key))).rowcount == 1
        try:
            if missing_marker:
                with pytest.raises(AuditIntegrityError):
                    operation_store[1].reconcile_unavailable_composer_segment(
                        candidate,
                        owner_instance_id=candidate.owner_instance_id,
                        lease_seconds=30,
                        auth_provider_type="local",
                    )
            else:
                retained = decode_terminal_p4_result(
                    phase.application.result_document,
                    input_document=phase.application.input_document,
                    env_ref_names=phase.inventory.names,
                )
                assert retained.receipt.unwind_audit_failed is False
                assert (
                    operation_store[1].reconcile_unavailable_composer_segment(
                        candidate,
                        owner_instance_id=candidate.owner_instance_id,
                        lease_seconds=30,
                        auth_provider_type="local",
                    )
                    == "settled"
                ), "a retained terminal service result cannot requeue the issued business call"
                terminal = owner.authority.get(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
                assert terminal is not None and terminal.status == "failed"
                assert terminal.result_json is not None
                error = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
                assert error.http_status == 500 and error.error_type == error.body["detail"]["error_type"] == "operation_failed"
                assert terminal.failure_code == error.failure_code == "operation_failed"
                assert error.body["detail"]["request_id"] == terminal.request_id
                assert error.body["detail"]["diagnostic_id"] == error.diagnostic_id
                assert (
                    operation_store[1].reconcile_unavailable_composer_segment(
                        candidate,
                        owner_instance_id=candidate.owner_instance_id,
                        lease_seconds=30,
                        auth_provider_type="local",
                    )
                    == "unchanged"
                )
        finally:
            if missing_marker:
                with operation_store[0].begin() as conn:
                    conn.execute(chunks.insert().values(**marker))
        if missing_marker:
            assert stored_rows(operation_store[0]) == before
            assert await asyncio.to_thread(_readback, operation_store[0], phase) is phase.committed_outcome.audit_outcome
            assert (
                operation_store[1].reconcile_unavailable_composer_segment(
                    candidate,
                    owner_instance_id=candidate.owner_instance_id,
                    lease_seconds=30,
                    auth_provider_type="local",
                )
                == "settled"
            ), "restoring the same marker must restore terminal settlement, not ordinary recovery"
        assert len(entered) == len(sdk) == 1 and owner.failure_custody.original is original and original.__cause__ is cause


@asynccontextmanager
async def _terminal_with_prior_effect(store, tmp_path, monkeypatch, *, history, close_before_data_inspection=False):
    earlier = ("earlier_completed", "set_metadata", {"patch": {"name": "Actual earlier completion"}})
    calls = (earlier, _review_call()) if history == "completed_tool" else (earlier,)
    assert history in ("completed_tool", "prior_application")
    async with _issued_review_case(store, tmp_path, monkeypatch, calls=calls) as (owner, running, sdk):
        prior_sql = ()
        prior_effect_key = None
        if history == "prior_application":
            assert await _finish_issued_batch(owner) is None
            prior = owner.p4
            assert prior is not None and type(prior.application) is StoredP4Application
            assert prior.application.bundle.entry.applications
            assert await asyncio.to_thread(_readback, store[0], prior) is prior.committed_outcome.audit_outcome
            prior_effect_key = prior.commit.effect_key
            prior_sql = tuple(
                ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_SQL
            )
            assert len(prior_sql) == 1 and prior_sql[0]._finish_once_handoff is prior.physical
            head = prior.application.snapshot
            root = decode_continuation_snapshot(head.payload_json)
            compose = root.frames[0]
            assert type(compose) is ComposeFrame
            assert compose.pending_response == prior.application.bundle.entry.applications[-1].response
            assert compose.provider_effect_key is not None and compose.provider_input_sha256 is not None
            with store[0].connect() as conn:
                paid = conn.execute(select(effects).where(*_effect_where(running, compose.provider_effect_key))).one()
            assert paid.owner == "provider" and paid.disposition == "committed"
            assert paid.input_sha256 == compose.provider_input_sha256
            assert paid.result_chunk_key == f"value:{compose.pending_response.codec}:{compose.pending_response.digest}"
            assert root.pending_settlement_ids == root.pending_owner_results == ()
            assert root.active_action_json is root.active_action_source is None
            # Match the existing post_p4_primary_input fixture: select the next
            # ordinary phase input after REAL P4 publication/application. This
            # is not a claim about the iteration driver, nor a fabricated P4.
            # Unlike that fixture's prebuilt input, this genuine paid PRIMARY
            # still has its consumed response locator on the Compose frame.
            # Keep the paid SQL effect and journal; only the next input's active
            # provider slots are empty, as _primary_frame/prepare_primary require.
            next_compose = compose.model_copy(
                update={
                    "phase": "action_ready",
                    "pending_response": None,
                    "provider_effect_key": None,
                    "provider_input_sha256": None,
                }
            )
            ready = root.model_copy(update={"frames": (next_compose,)})
            with locked_session_transaction(store[0], str(running.claim.session_id)) as conn:
                head = append_complete_snapshot(
                    conn,
                    session_id=str(running.claim.session_id),
                    operation_id=running.claim.operation_id,
                    expected_checkpoint_id=head.checkpoint_id,
                    expected_cursor=head.cursor,
                    snapshot_json=encode_continuation_snapshot(ready),
                    event_batch=(ContinuationEvent("checkpoint", '{"fixture":"terminal test next primary input after actual P4"}'),),
                    handoff=False,
                )
            following = ContinuationLivePhases(owner.service, owner.authority, owner.work, owner.provider_owner, owner.lease)
            selected = await load_selected_continuation(authority=owner.authority, running=running, selected=head, required_work=owner.work)
            runtime, _inventory = await following.current(selected)
            assert runtime.graph.applications is not None
            assert runtime.graph.applications.applications == prior.application.bundle.entry.applications
            call_id, name, arguments = _review_call()

            async def second_primary(**kwargs):
                sdk.append(kwargs)
                assert len(sdk) == 2, "the prior application needs exactly one new paid PRIMARY, never replay"
                return ModelResponse(
                    model="test/model",
                    choices=[
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "Review the actual existing draft.",
                                "tool_calls": [
                                    {
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": name,
                                            "arguments": canonical_json(
                                                encode_semantic_arguments(name, owner.service._planner_dialect, arguments)
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                )

            monkeypatch.setattr(litellm, "acompletion", second_primary)
            _messages, _tools, delivery, _input = owner.service._prepare_primary_delivery(
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                turn_evidence=runtime.live.turn_evidence,
                state=runtime.live.state,
                failed_turn=runtime.live.failed_turn,
            )
            with prompt_guard_user_scope(runtime.bindings.message):
                primary = await run_continuation_primary(
                    service=owner.service,
                    authority=owner.authority,
                    selected=runtime.source.selected,
                    producers=runtime.materialization,
                    messages=runtime.live.llm_messages,
                    tools=runtime.live.tools,
                    recorder=runtime.live.recorder,
                    drained=runtime.drained,
                    required_work=owner.work,
                    provider_owner=owner.provider_owner,
                    timeout=runtime.source.policy.actions.primary.total,
                )
            if delivery is not None:
                runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
            following.accept_primary(primary)
            selected = await load_selected_continuation(
                authority=owner.authority, running=running, selected=primary.snapshot, required_work=owner.work
            )
            # Observe only the already delivered actual source references. Full
            # admission/creator/prefix checks remain with the ordinary reader.
            compose_frames = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
            assert len(compose_frames) == 1
            compose = compose_frames[0]
            evidence_chunks = tuple(chunk for chunk in selected.chunks if chunk.reference == compose.evidence)
            recorder_chunks = tuple(chunk for chunk in selected.chunks if chunk.reference == compose.audit_buffer)
            assert len(evidence_chunks) == len(recorder_chunks) == 1
            assert recorder_chunks[0] == primary.recorder
            evidence_reference, recorder_reference = evidence_chunks[0].reference, recorder_chunks[0].reference
            assert evidence_reference == compose.evidence and recorder_reference == compose.audit_buffer
            e_bytes, r_bytes = evidence_reference.byte_length, recorder_reference.byte_length
            assert type(e_bytes) is type(r_bytes) is int and e_bytes > 0 and r_bytes > 0
            assert e_bytes <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
            assert e_bytes + r_bytes <= CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES
            print(
                "second_primary_actual_pair "
                + json.dumps(
                    {
                        "E": e_bytes,
                        "R": r_bytes,
                        "sum": e_bytes + r_bytes,
                        "D": CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
                        "P": CONTINUATION_EVIDENCE_PAIR_MAX_UTF8_BYTES,
                    },
                    sort_keys=True,
                )
            )
            await following.advance(selected)
            owner = following
        original, cause = _service_error("admission"), RuntimeError("service original after actual earlier effect")
        entered = _refuse_service(
            store[3], monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        observed = await _finish_issued_batch(owner)
        phase = _require_publication(
            owner,
            running,
            original,
            cause,
            entered,
            sdk,
            observed,
            expected_calls=("earlier_completed", "pending_review"),
            provider_calls=2 if history == "prior_application" else 1,
            prior_sql=prior_sql,
        )
        if close_before_data_inspection:
            # The prerequisite's extra immutable inspections do not own more
            # live work. Surface any real renewal loss at this boundary, before
            # those inspections; the tamper matrix retains its live lifetime.
            await _close_released_lease(store, owner, running)
        assert await asyncio.to_thread(_readback, store[0], phase) is phase.committed_outcome.audit_outcome
        source = decode_terminal_p4_input(phase.application.input_document, env_ref_names=phase.inventory.names)
        if history == "completed_tool":
            prior_effect_key = f"tool:{source.issue.response.digest}:0"
            assert source.issue.tool_index == 1
        else:
            assert source.issue.tool_index == 0
        assert prior_effect_key not in (phase.commit.effect_key, source.issued_effect_key)
        with store[0].connect() as conn:
            earlier_row = conn.execute(select(effects).where(*_effect_where(running, prior_effect_key))).mappings().one()
        assert earlier_row["disposition"] == "committed"
        assert earlier_row["owner"] == ("tool_dispatch" if history == "completed_tool" else "session_sql")
        assert earlier_row["receipt_key"] and earlier_row["result_chunk_key"]
        yield owner, running, sdk, original, cause, entered, phase, dict(earlier_row)


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ("completed_tool", "prior_application"))
async def test_terminal_p4_actual_earlier_effect_is_a_full_publication_prerequisite(operation_store, tmp_path, monkeypatch, history):
    async with _terminal_with_prior_effect(
        operation_store, tmp_path, monkeypatch, history=history, close_before_data_inspection=True
    ) as case:
        owner, _running, sdk, original, cause, entered, phase, _earlier = case
        before = stored_rows(operation_store[0])
        assert await asyncio.to_thread(_readback, operation_store[0], phase) is phase.committed_outcome.audit_outcome
        assert stored_rows(operation_store[0]) == before
        assert owner.failure_custody.original is original and original.__cause__ is cause
        assert len(entered) == 1 and len(sdk) == (2 if history == "prior_application" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ("completed_tool", "prior_application"))
@pytest.mark.parametrize("change", ("input", "disposition", "receipt", "owner"))
@pytest.mark.parametrize("reader", ("readback", "reaper"))
async def test_terminal_p4_prior_sql_effect_tamper_refuses_before_readback_or_recovery(
    operation_store, tmp_path, monkeypatch, history, change, reader
):
    async with _terminal_with_prior_effect(operation_store, tmp_path, monkeypatch, history=history) as case:
        owner, running, sdk, original, cause, entered, phase, earlier = case
        changed_values = {
            "input": {"input_sha256": "0" * 64},
            "disposition": {"disposition": "unknown", "receipt_key": None, "result_chunk_key": None},
            "receipt": {"receipt_key": "changed-actual-earlier-receipt"},
            "owner": {"owner": "session_sql" if history == "completed_tool" else "provider"},
        }[change]
        assert all(earlier[key] != value for key, value in changed_values.items())
        if reader == "readback":
            before = stored_rows(operation_store[0])
            with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
                savepoint = conn.begin_nested()
                try:
                    changed = conn.execute(update(effects).where(*_effect_where(running, earlier["effect_key"])).values(**changed_values))
                    assert changed.rowcount == 1
                    with pytest.raises(AuditIntegrityError):
                        read_p4_transaction_outcome_on_connection(conn, phase.writer_observation, env_ref_names=phase.inventory.names)
                finally:
                    savepoint.rollback()
                    assert read_p4_transaction_outcome_on_connection(
                        conn, phase.writer_observation, env_ref_names=phase.inventory.names
                    ) is (phase.committed_outcome.audit_outcome)
            assert stored_rows(operation_store[0]) == before
        else:
            assert reader == "reaper"
            await _close_released_lease(operation_store, owner, running)
            repository = operation_store[1]
            (candidate,) = list_recoverable_segments(operation_store[0], limit=10)
            before = stored_rows(operation_store[0])
            actual_lock = repository._locked_transaction
            changed_rows = []

            @contextmanager
            def changed_locked_transaction(session_id, **kwargs):
                # Enter the REAL repository transaction and lock. A savepoint
                # restores the mutant and any wrongly admitted settlement even
                # when the intended refusal is absent on the old implementation.
                with actual_lock(session_id, **kwargs) as conn:
                    savepoint = conn.begin_nested()
                    try:
                        changed = conn.execute(
                            update(effects).where(*_effect_where(running, earlier["effect_key"])).values(**changed_values)
                        )
                        assert changed.rowcount == 1
                        changed_rows.append(earlier["effect_key"])
                        yield conn
                    finally:
                        savepoint.rollback()

            try:
                with monkeypatch.context() as patch:
                    patch.setattr(repository, "_locked_transaction", changed_locked_transaction)
                    with pytest.raises(AuditIntegrityError):
                        repository.reconcile_unavailable_composer_segment(
                            candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
                        )
            finally:
                assert changed_rows == [earlier["effect_key"]]
                assert stored_rows(operation_store[0]) == before
                assert await asyncio.to_thread(_readback, operation_store[0], phase) is phase.committed_outcome.audit_outcome
                assert (
                    repository.reconcile_unavailable_composer_segment(
                        candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
                    )
                    == "settled"
                ), "the exact restored earlier source must permit terminal settlement"
                assert (
                    repository.reconcile_unavailable_composer_segment(
                        candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
                    )
                    == "unchanged"
                )
        assert owner.failure_custody.original is original and original.__cause__ is cause
        assert len(entered) == 1 and len(sdk) == (2 if history == "prior_application" else 1)


@asynccontextmanager
async def _actual_unavailable_terminal_case(store, tmp_path, monkeypatch, *, calls=None):
    """Origin's actual SQL-refusal setup; never construct an unavailable receipt."""
    async with _issued_review_case(store, tmp_path, monkeypatch, calls=calls) as (owner, running, sdk):
        sessions = store[3]
        original, cause = _service_error("admission"), RuntimeError("same service cause before unavailable publication")
        entered = _refuse_service(
            sessions, monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        sql_error = OperationalError("actual terminal assistant insert refused", {}, RuntimeError("actual DBAPI failure"))
        sql_cause = RuntimeError("same prior SQL cause")
        sql_error.__cause__ = sql_cause
        actual_publish = sessions.persist_continuation_compose_turn
        insertions, tickets, returns = [], [], []

        def insert(conn, **kwargs):
            assert kwargs["session_id"] == str(running.claim.session_id)
            assert kwargs["session_operation_context"] is running.session_operation_context
            assert kwargs["role"] == "assistant" and kwargs["writer_principal"] == "compose_loop"
            sessions._assert_session_write_lock_held(conn, kwargs["session_id"], caller="cold terminal unavailable prerequisite")
            insertions.append(kwargs)
            raise sql_error

        async def publish(**kwargs):
            assert kwargs["session_operation_context"] is running.session_operation_context
            tickets.append(kwargs["required_work"])
            result = await actual_publish(**kwargs)
            returns.append(result)
            return result

        monkeypatch.setattr(sessions, "_insert_chat_message", insert)
        monkeypatch.setattr(sessions, "persist_continuation_compose_turn", publish)
        observed = await _finish_issued_batch(owner)
        assert len(entered) == len(insertions) == len(tickets) == len(returns) == len(sdk) == 1
        phase = owner.p4
        assert phase is not None and phase.publication is not None and phase.committed_outcome is not None
        assert phase.physical is returns[0] and type(phase.physical) is RequiredSQLReturned
        assert type(phase.physical.value) is CommittedContinuationP4 and phase.physical.value is phase.committed_outcome
        outcome = phase.committed_outcome.audit_outcome
        assert outcome is phase.publication.audit_outcome is phase.writer_observation.candidate
        assert outcome.unwind_audit_failed and outcome.checkpoint_disposition == "unavailable"
        assert outcome.assistant_id is None and outcome.checkpoint is None and outcome.continuation_retention_failure is None
        application = phase.application
        assert type(application) is StoredTerminalP4Application
        assert application is phase.committed_outcome.application is phase.writer_observation.application_candidate
        retained = decode_terminal_p4_result(
            application.result_document, input_document=application.input_document, env_ref_names=phase.inventory.names
        )
        assert retained.receipt.unwind_audit_failed and retained.receipt.checkpoint_disposition == "unavailable"
        assert retained.receipt.chat_message_ids == retained.receipt.inserted_state_ids == retained.receipt.invocation_ids == ()
        assert phase.runtime.drained == retained.drained == owner.terminal_completion.runtime.drained
        assert not _persisted_tool_responses(owner.service, str(running.claim.session_id))
        assert phase.application_sql is None and tickets[0].complete and tickets[0]._finish_once_handoff is phase.physical
        nodes = _exception_nodes(observed)
        assert any(error is original for error in nodes) and any(error is sql_error for error in nodes)
        assert original.__cause__ is cause and sql_error.__cause__ is sql_cause
        assert any(isinstance(error, AuditIntegrityError) and error.failed_turn is phase.publication.failed_turn for error in nodes)
        projection = project_composer_operation_error(observed, request_id=running.claim.operation_id)
        assert projection.failure_code == "http_error" and projection.http_status == 500
        assert projection.error_type == projection.body["error_type"] == "audit_integrity_error"
        assert await asyncio.to_thread(_readback, store[0], phase) is outcome
        yield owner, running, sdk, original, cause, sql_error, sql_cause, phase, retained


@pytest.mark.asyncio
async def test_terminal_p4_actual_unavailable_publication_precedes_cold_projection(operation_store, tmp_path, monkeypatch):
    async with _actual_unavailable_terminal_case(operation_store, tmp_path, monkeypatch) as case:
        owner, running, sdk, original, cause, sql_error, sql_cause, phase, retained = case
        before = stored_rows(operation_store[0])
        source = await asyncio.to_thread(owner.authority.read_continuation_tool_failure, running, env_ref_names=phase.inventory.names)
        assert type(source) is StoredTerminalP4Application and source == phase.application
        assert (
            decode_terminal_p4_result(source.result_document, input_document=source.input_document, env_ref_names=phase.inventory.names)
            == retained
        )
        assert stored_rows(operation_store[0]) == before
        assert owner.failure_custody.original is original and original.__cause__ is cause and sql_error.__cause__ is sql_cause
        assert len(sdk) == 1


@pytest.mark.asyncio
async def test_terminal_p4_cold_unavailable_settlement_preserves_integrity_priority(operation_store, tmp_path, monkeypatch):
    async with _actual_unavailable_terminal_case(operation_store, tmp_path, monkeypatch) as case:
        owner, running, sdk, original, cause, sql_error, sql_cause, phase, retained = case
        _, repository, cold_authority, _, _ = new_owners(operation_store)
        source = await asyncio.to_thread(cold_authority.read_continuation_tool_failure, running, env_ref_names=phase.inventory.names)
        assert type(source) is StoredTerminalP4Application and source == phase.application
        current = cold_authority.get(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
        assert current is not None and current.cancel_requested_at is None, "Stop must not mask the unavailable classification"
        await _close_released_lease(operation_store, owner, running)
        (candidate,) = list_recoverable_segments(operation_store[0], limit=10)
        assert candidate.cancel_requested_at is None
        assert (
            repository.reconcile_unavailable_composer_segment(
                candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
            )
            == "settled"
        )
        terminal = cold_authority.get(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
        assert terminal is not None and terminal.status == "failed"
        settled_rows = stored_rows(operation_store[0])
        assert (
            repository.reconcile_unavailable_composer_segment(
                candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
            )
            == "unchanged"
        )
        assert stored_rows(operation_store[0]) == settled_rows
        assert await asyncio.to_thread(_readback, operation_store[0], phase) is phase.committed_outcome.audit_outcome
        assert (
            decode_terminal_p4_result(
                phase.application.result_document, input_document=phase.application.input_document, env_ref_names=phase.inventory.names
            )
            == retained
        )
        assert len(sdk) == 1 and owner.failure_custody.original is original
        assert original.__cause__ is cause and sql_error.__cause__ is sql_cause
        assert terminal.result_json is not None
        error = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert error.error_type == "audit_integrity_error", "cold settlement must preserve the actual unavailable audit disposition"
        assert error.body["error_type"] == "audit_integrity_error" and error.http_status == 500
        assert terminal.failure_code == error.failure_code == "http_error"
        assert error.body["request_id"] == terminal.request_id and error.diagnostic_id is None
        assert error.body["detail"] == "ELSPETH stopped before replying because it could not verify this session's audit trail."


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", (False, True), ids=("audited", "unavailable"))
async def test_terminal_p4_cold_projection_retains_actual_registered_inventory(operation_store, tmp_path, monkeypatch, unavailable):
    session = _registered_terminal_reference(operation_store, monkeypatch)
    calls = (
        ("retained_state", "set_metadata", {"patch": {"name": "Actual registered-reference state"}}),
        (
            "refused_output",
            "set_output",
            {
                "sink_name": "output",
                "plugin": "database",
                "options": {"url": "${DB_URL}", "table": "events", "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            },
        ),
        _review_call(),
    )
    producer = _actual_unavailable_terminal_case if unavailable else _terminal_case
    async with producer(operation_store, tmp_path, monkeypatch, calls=calls) as case:
        owner, running, sdk, original, cause = case[:5]
        phase = owner.p4
        assert phase is not None and phase.inventory.names == frozenset({"DB_URL"})
        application = phase.application
        assert type(application) is StoredTerminalP4Application and application.env_ref_names == phase.inventory.names
        # Keep the accepted graph change before the refused authoring and
        # service failure. The reference is original paid authoring DATA;
        # this fixture's policy does not enable the database sink.
        assert "${DB_URL}" in application.input_document.canonical
        invocations = owner.runtime.live.recorder.invocations
        assert tuple(item.tool_call_id for item in invocations) == ("retained_state", "refused_output", "pending_review")
        assert tuple(item.tool_name for item in invocations) == ("set_metadata", "set_output", "request_interpretation_review")
        assert all(item.status is ComposerToolStatus.SUCCESS for item in invocations)
        assert json.loads(invocations[0].result_canonical)["success"] is True
        refused = json.loads(invocations[1].result_canonical)
        assert refused["success"] is False and refused["validation"]["errors"][0]["error_code"] == "plugin_not_enabled"
        assert json.loads(invocations[2].result_canonical) == {"success": False, "status": "SERVICE_FAILURE", "reason": "service_failure"}
        assert invocations[0].version_after > invocations[0].version_before
        assert invocations[1].version_before == invocations[1].version_after == invocations[0].version_after
        assert invocations[2].version_before == invocations[2].version_after == invocations[1].version_after
        assert owner.runtime.live.state.metadata.name == "Actual registered-reference state"
        assert owner.runtime.live.state.outputs == ()
        assert phase.prepared is not None
        members = prepared_p4_members(phase.prepared, env_ref_names=phase.inventory.names)
        assert tuple(row.tool_call_id for row in members.redacted_rows) == ("retained_state", "refused_output", "pending_review")
        first_state = members.redacted_rows[0].composition_state_payload
        assert first_state is not None and first_state.data.metadata_["name"] == "Actual registered-reference state"
        assert all(row.composition_state_payload is None for row in members.redacted_rows[1:])
        assert members.authorings[1].tool_call_id == "refused_output"
        assert members.authorings[1].raw_arguments == canonical_json(
            encode_semantic_arguments("set_output", owner.service._planner_dialect, calls[1][2])
        )
        expected_domain = "audit_integrity_error" if unavailable else "operation_failed"
        projected = terminal_service_failure(application, request_id=running.claim.operation_id)
        assert projected.error_type == expected_domain
        with pytest.raises(CredentialMaterialRefused):
            decode_terminal_p4_result(application.result_document, input_document=application.input_document)
        secret_where = (
            secret_rows.c.user_id == session.user_id,
            secret_rows.c.auth_provider_type == session.auth_provider_type,
            secret_rows.c.name == "DB_URL",
        )
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:

            def read_source():
                return read_lost_terminal_p4_on_connection(
                    conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id
                )

            assert read_source() == application
            for change in ("missing", "foreign"):
                nested = conn.begin_nested()
                try:
                    statement = (
                        delete(secret_rows).where(*secret_where)
                        if change == "missing"
                        else update(secret_rows).where(*secret_where).values(name="FOREIGN_DB_URL")
                    )
                    assert conn.execute(statement).rowcount == 1
                    with pytest.raises(CredentialMaterialRefused):
                        read_source()
                finally:
                    nested.rollback()
                    restored = read_source()
                    assert restored == application
                    assert terminal_service_failure(restored, request_id=running.claim.operation_id) == projected
        _, repository, cold_authority, _, _ = new_owners(operation_store)
        await _close_released_lease(operation_store, owner, running)
        (candidate,) = list_recoverable_segments(operation_store[0], limit=10)
        assert candidate.cancel_requested_at is None
        assert (
            repository.reconcile_unavailable_composer_segment(
                candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
            )
            == "settled"
        )
        terminal = cold_authority.get(session_id=running.claim.session_id, operation_id=running.claim.operation_id)
        assert terminal is not None and terminal.status == "failed" and terminal.result_json is not None
        error = ComposerOperationError.model_validate_json(terminal.result_json, strict=True)
        assert error.error_type == expected_domain and error.http_status == 500
        assert terminal.failure_code == error.failure_code == ("http_error" if unavailable else "operation_failed")
        body = error.body if unavailable else error.body["detail"]
        assert body["error_type"] == expected_domain and body["request_id"] == terminal.request_id
        assert "DB_URL" not in terminal.result_json and "synthetic-terminal-private-value" not in terminal.result_json
        settled_rows = stored_rows(operation_store[0])
        assert (
            repository.reconcile_unavailable_composer_segment(
                candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
            )
            == "unchanged"
        )
        assert stored_rows(operation_store[0]) == settled_rows
        assert await asyncio.to_thread(_readback, operation_store[0], phase) is phase.committed_outcome.audit_outcome
        assert len(sdk) == 1 and owner.failure_custody.original is original and original.__cause__ is cause


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("event_role", "attempt_outcome"))
async def test_terminal_p4_settled_readback_requires_exact_original_transition(operation_store, tmp_path, monkeypatch, change):
    async with _terminal_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk, original, cause, entered, phase):
        await _close_released_lease(operation_store, owner, running)
        (candidate,) = list_recoverable_segments(operation_store[0], limit=10)
        assert candidate.cancel_requested_at is None
        assert (
            operation_store[1].reconcile_unavailable_composer_segment(
                candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
            )
            == "settled"
        )
        # The real post-settlement snapshot and unknown issued tool must remain
        # readable before any opposition. The original issued snapshot is not
        # silently substituted for this current SQL selection.
        assert await asyncio.to_thread(_readback, operation_store[0], phase) is phase.committed_outcome.audit_outcome
        before = stored_rows(operation_store[0])
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            nested = conn.begin_nested()
            try:
                if change == "event_role":
                    changed = conn.execute(
                        update(events)
                        .where(
                            events.c.session_id == str(running.claim.session_id),
                            events.c.operation_id == running.claim.operation_id,
                            events.c.sequence == phase.commit.expected_cursor + 2,
                            events.c.tag == "settled",
                        )
                        .values(tag="checkpoint")
                    )
                    diagnostic = "settlement changed its original event attribution"
                else:
                    assert change == "attempt_outcome"
                    changed = conn.execute(
                        update(attempts)
                        .where(
                            attempts.c.session_id == str(running.claim.session_id),
                            attempts.c.operation_id == running.claim.operation_id,
                            attempts.c.sequence == running.claim.segment_sequence,
                            attempts.c.generation == running.claim.ownership_generation,
                            attempts.c.outcome == "interrupted",
                        )
                        .values(outcome="settled")
                    )
                    diagnostic = "advanced without its actual same-attempt settlement"
                assert changed.rowcount == 1
                with pytest.raises(AuditIntegrityError, match=diagnostic):
                    read_p4_transaction_outcome_on_connection(conn, phase.writer_observation, env_ref_names=phase.inventory.names)
            finally:
                nested.rollback()
                assert read_p4_transaction_outcome_on_connection(conn, phase.writer_observation, env_ref_names=phase.inventory.names) is (
                    phase.committed_outcome.audit_outcome
                )
        assert stored_rows(operation_store[0]) == before
        assert (
            operation_store[1].reconcile_unavailable_composer_segment(
                candidate, owner_instance_id=candidate.owner_instance_id, lease_seconds=30, auth_provider_type="local"
            )
            == "unchanged"
        )
        assert owner.failure_custody.original is original and original.__cause__ is cause
        assert len(entered) == len(sdk) == 1
