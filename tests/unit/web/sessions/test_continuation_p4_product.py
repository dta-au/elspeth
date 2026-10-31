"""Actual source ownership and one-transaction P4 failure boundaries."""

import json
from contextlib import contextmanager
from hashlib import sha256

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, verify_private_chunk
from elspeth.web.composer.continuation_evidence import (
    admit_turn_evidence,
    decode_turn_evidence,
    encode_turn_evidence,
    require_admitted_turn_evidence,
)
from elspeth.web.composer.continuation_evidence_application_values import P4ApplicationSources
from elspeth.web.composer.continuation_runtime import _registered_read
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.credential_guard import CredentialMaterialRefused
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_p4_application_store import CommittedContinuationP4
from elspeth.web.sessions.service import ComposerP4SQLCompletionUnknown
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import stored_rows

operation_store = _operation_store


async def _current_admitted(owner, running):
    stored = await _registered_read(owner.work, StoredContinuationSnapshot, lambda: owner.authority.load_continuation_snapshot(running))
    selected = await load_selected_continuation(authority=owner.authority, running=running, selected=stored, required_work=owner.work)
    runtime, inventory = await owner.current(selected)
    compose = selected.stack.frames[0]
    assert type(compose) is ComposeFrame
    source = next(chunk for chunk in selected.chunks if chunk.reference == compose.evidence)
    admitted = decode_turn_evidence(source, env_ref_names=inventory.names, source_chunks=selected.chunks)
    return admitted, source, selected, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "source", "env", "nested_graph", "nested_snapshot", "second_transfer", "independent_cold"))
async def test_actual_admitted_source_and_once_transfer_keep_original_ownership(operation_store, tmp_path, monkeypatch, change):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        admitted, source, selected, runtime = await _current_admitted(owner, running)
        graph = admitted.graph
        assert graph is not None
        before = stored_rows(operation_store[0])
        assert admitted.source_chunk is source
        assert encode_turn_evidence(admitted, env_ref_names=runtime.env_ref_names) is source
        assert encode_turn_evidence(admitted, env_ref_names=runtime.env_ref_names, mandatory_retention=True) is source
        if change == "source":
            snapshot = admitted.snapshot.model_copy(update={"registration_revision": admitted.snapshot.registration_revision + 1})
            following = admit_turn_evidence(snapshot, env_ref_names=runtime.env_ref_names, source_chunks=selected.chunks)
            assert following is not admitted and following.source_chunk != source and following.graph is not graph
            assert admitted.snapshot.registration_revision + 1 == following.snapshot.registration_revision
        elif change == "env":
            with pytest.raises(AuditIntegrityError, match="credential environment"):
                require_admitted_turn_evidence(admitted, env_ref_names=runtime.env_ref_names | {"DIAGNOSTIC_CHANGED_POLICY"})
        elif change == "nested_graph":
            graph.selected.batch.stop_issued_batch = not graph.selected.batch.stop_issued_batch
            with pytest.raises(AuditIntegrityError):
                admitted.take_graph()
            # Source serialization is still the original immutable DATA. It
            # neither captures the mutation nor grants it current authority.
            assert encode_turn_evidence(admitted, env_ref_names=runtime.env_ref_names) is source
        elif change == "nested_snapshot":
            admitted.snapshot.effects.__dict__["initial_authored_state_hash"] = "0" * 64
            with pytest.raises(AuditIntegrityError, match="snapshot changed"):
                admitted.take_graph()
        elif change == "second_transfer":
            assert admitted.take_graph() is graph
            with pytest.raises(AuditIntegrityError, match="transferred twice"):
                admitted.take_graph()
            with pytest.raises(AuditIntegrityError, match="already transferred"):
                _ = admitted.graph
        elif change == "independent_cold":
            other = decode_turn_evidence(source, env_ref_names=runtime.env_ref_names, source_chunks=selected.chunks)
            other_graph = other.graph
            assert other_graph is not None and other_graph is not graph
            assert other_graph.selected.batch is not graph.selected.batch
            assert admitted.take_graph() is graph
            graph.selected.batch.stop_issued_batch = not graph.selected.batch.stop_issued_batch
            assert other.take_graph() is other_graph
            assert other_graph.selected.batch.stop_issued_batch is not graph.selected.batch.stop_issued_batch
        else:
            assert change == "none" and admitted.take_graph() is graph
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


def _contains_identity(error, expected):
    if error is expected:
        return True
    if isinstance(error, BaseExceptionGroup) and any(_contains_identity(child, expected) for child in error.exceptions):
        return True
    return error.__cause__ is not None and _contains_identity(error.__cause__, expected)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ("none", "missing", "length_conflict", "same_length_corruption", "sole_wrong_digest", "changed_policy_credential", "role")
)
async def test_actual_completed_application_requires_closed_original_dependencies(operation_store, tmp_path, monkeypatch, change):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        await owner.finish_owed_prefix(owner.runtime.source.selected)
        phase = owner.p4
        assert phase is not None and phase.application is not None
        actual = phase.application.bundle.evidence_after
        entry = actual.snapshot.application_entry
        assert entry is not None and len(entry.applications) == 1
        source = actual.source_chunk
        chunks = actual.sources.chunks
        record = entry.applications[0]
        before = stored_rows(operation_store[0])
        # The new wire has four locators and its one inline identity graph;
        # none of the four original documents is embedded again in the record.
        assert set(record.model_dump()) == {
            "checkpoint_id",
            "cursor",
            "effect_key",
            "input_sha256",
            "receipt_id",
            "creator_sequence",
            "creator_generation",
            "creator_operation_id",
            "creator_operation_epoch",
            "response",
            "pending_batch",
            "prepared_input",
            "result",
            "verification",
        }
        if change == "none":
            cold = decode_turn_evidence(source, env_ref_names=actual.env_ref_names, source_chunks=(actual.recorder_chunk, *chunks))
            assert encode_turn_evidence(cold, env_ref_names=actual.env_ref_names) is source
            assert all(cold.sources.chunk(chunk.reference) is chunk for chunk in chunks)
            assert cold.graph is not actual.graph
        elif change == "missing":
            missing = actual.sources.chunk(record.response)
            with pytest.raises(AuditIntegrityError, match="lost its original selected dependency"):
                decode_turn_evidence(
                    source,
                    env_ref_names=actual.env_ref_names,
                    source_chunks=(actual.recorder_chunk, *(c for c in chunks if c is not missing)),
                )
        elif change == "length_conflict":
            original = actual.sources.chunk(record.response)
            conflict = PrivateChunk(
                original.reference.model_copy(update={"byte_length": original.reference.byte_length + 1}), original.canonical
            )
            with pytest.raises(AuditIntegrityError, match="conflicting original bytes or lengths"):
                P4ApplicationSources(entry, (*chunks, conflict))
        elif change == "same_length_corruption":
            original = actual.sources.chunk(record.response)
            corrupted = PrivateChunk(original.reference, original.canonical[:-1] + " ")
            assert len(corrupted.canonical.encode("utf-8")) == original.reference.byte_length
            with pytest.raises(AuditIntegrityError, match="conflicting original bytes or lengths"):
                P4ApplicationSources(entry, (*chunks, corrupted))
        elif change == "sole_wrong_digest":
            positive = decode_turn_evidence(source, env_ref_names=actual.env_ref_names, source_chunks=(actual.recorder_chunk, *chunks))
            assert positive.source_chunk is source
            original = actual.sources.chunk(record.response)
            canonical = original.canonical.replace('"composer.response.v1"', '"composer.response.v0"', 1)
            assert canonical != original.canonical and canonical_json(json.loads(canonical)) == canonical
            assert len(canonical.encode("utf-8")) == original.reference.byte_length
            corrupted = PrivateChunk(original.reference, canonical)
            opposed = tuple(corrupted if chunk is original else chunk for chunk in chunks)
            assert sum(chunk.reference == original.reference for chunk in opposed) == 1
            with pytest.raises(AuditIntegrityError, match="private chunk bytes differ from their binding"):
                decode_turn_evidence(source, env_ref_names=actual.env_ref_names, source_chunks=(actual.recorder_chunk, *opposed))
        elif change == "changed_policy_credential":
            positive = decode_turn_evidence(source, env_ref_names=actual.env_ref_names, source_chunks=(actual.recorder_chunk, *chunks))
            assert positive.source_chunk is source
            original = actual.sources.chunk(record.response)
            payload = json.loads(original.canonical)
            payload["content"] = "sk-" + "syntheticcredential" * 3
            canonical = canonical_json(payload)
            encoded = canonical.encode("utf-8")
            hostile = PrivateChunk(
                original.reference.model_copy(update={"digest": sha256(encoded).hexdigest(), "byte_length": len(encoded)}), canonical
            )
            # Valid canonical bytes and a matching locator reach the resolved
            # leaf's credential guard. This is an opposed source, not a stored
            # business result or a source granted by its reference alone.
            verify_private_chunk(hostile.reference, hostile.canonical)
            replaced = record.model_copy(update={"response": hostile.reference})
            wrong = actual.snapshot.model_copy(update={"application_entry": entry.model_copy(update={"applications": (replaced,)})})
            opposed = tuple(hostile if chunk is original else chunk for chunk in chunks)
            changed_policy = actual.env_ref_names | {"P4_CHANGED_CREDENTIAL_POLICY"}
            with pytest.raises(CredentialMaterialRefused) as caught:
                admit_turn_evidence(wrong, env_ref_names=changed_policy, source_chunks=(actual.recorder_chunk, *opposed))
            assert caught.value.surface == "portable application original sources"
            assert caught.value.finding.category == "token_shape"
        else:
            assert change == "role"
            replaced = record.model_copy(update={"response": record.result})
            wrong = actual.snapshot.model_copy(update={"application_entry": entry.model_copy(update={"applications": (replaced,)})})
            with pytest.raises(AuditIntegrityError):
                admit_turn_evidence(wrong, env_ref_names=actual.env_ref_names, source_chunks=(actual.recorder_chunk, *chunks))
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "before_commit", "after_commit", "unknown_readback", "projection"))
async def test_actual_p4_one_physical_outcome_keeps_atomic_and_uncertain_boundaries(operation_store, tmp_path, monkeypatch, fault):
    from elspeth.web.composer import continuation_live_p4_application
    from elspeth.web.sessions import continuation_p4_application_store

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        service = owner.service._require_sessions_service()
        actual_begin = type(service)._session_process_locked_begin
        actual_apply = continuation_p4_application_store.apply_committing_p4_application_on_connection
        actual_project = continuation_live_p4_application.retain_portable_application_on_live_evidence
        physical_error = OperationalError("actual commit-exit control", None, RuntimeError("original DBAPI outcome"))
        before_error = RuntimeError("actual application before commit control")
        projection_error = RuntimeError("actual warm projection after commit control")
        readback_error = RuntimeError("actual readback unavailable control")
        applied, exits, projected = [], [], []

        def apply(conn, commit, *, env_ref_names):
            value = actual_apply(conn, commit, env_ref_names=env_ref_names)
            applied.append(value)
            if fault == "before_commit":
                raise before_error
            return value

        @contextmanager
        def begin(self, session_id):
            candidate = None
            with actual_begin(self, session_id) as conn:
                yield conn
                if self is service and owner.p4 is not None:
                    observation = owner.p4.writer_observation
                    if observation is not None and observation.application_candidate is not None and not observation.committed:
                        candidate = observation
            # Inject only AFTER the real outer transaction exited. A failure
            # here is not a known rollback and cannot erase either durable row.
            if candidate is not None:
                exits.append(candidate)
                if fault in ("after_commit", "unknown_readback") and len(exits) == 1:
                    raise physical_error

        def project(**kwargs):
            value = actual_project(**kwargs)
            projected.append(value)
            if fault == "projection":
                raise projection_error
            return value

        def unknown(self, observation, *, original, env_ref_names):
            assert self is service and original is physical_error
            observation.original_error = original
            error = ComposerP4SQLCompletionUnknown(observation)
            raise error from BaseExceptionGroup("actual transaction and unavailable readback", (original, readback_error))

        monkeypatch.setattr(continuation_p4_application_store, "apply_committing_p4_application_on_connection", apply)
        monkeypatch.setattr(type(service), "_session_process_locked_begin", begin)
        monkeypatch.setattr(continuation_live_p4_application, "retain_portable_application_on_live_evidence", project)
        if fault == "unknown_readback":
            monkeypatch.setattr(type(service), "_readback_continuation_p4_transaction", unknown)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        if fault == "none":
            await owner.finish_owed_prefix(owner.runtime.source.selected)
        else:
            with pytest.raises(BaseException) as caught:
                await owner.finish_owed_prefix(owner.runtime.source.selected)
            if fault == "before_commit":
                assert _contains_identity(caught.value, before_error)
            elif fault == "projection":
                assert _contains_identity(caught.value, projection_error)
            else:
                assert _contains_identity(caught.value, physical_error)
                if fault == "unknown_readback":
                    assert _contains_identity(caught.value, readback_error)
        phase = owner.p4
        assert phase is not None and phase.writer_observation is not None
        observation = phase.writer_observation
        sql = tuple(ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
        projection = tuple(
            ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
        )
        assert len(sql) == len(projection) == 1
        assert phase.physical is sql[0]._finish_once_handoff and sql[0].complete and projection[0].complete
        assert phase.application_sql is None and len(applied) == 1 and len(sdk) == 1
        current = await _registered_read(
            owner.work, StoredContinuationSnapshot, lambda: owner.authority.load_continuation_snapshot(running)
        )
        root = decode_continuation_snapshot(current.payload_json)
        if fault == "before_commit":
            assert type(phase.physical) is RequiredSQLRaised
            assert observation.known_not_committed and not observation.committed
            assert current.checkpoint_id == observation.commit.expected_checkpoint_id
            assert phase.publication is None and phase.application is None and not projected
        elif fault == "unknown_readback":
            assert type(phase.physical) is RequiredSQLRaised and not observation.committed
            assert type(phase.physical.error) is ComposerP4SQLCompletionUnknown
            assert phase.physical.error.observation is observation and _contains_identity(caught.value, phase.physical.error)
            assert observation.candidate is not None and observation.application_candidate is applied[0]
            assert current == applied[0].snapshot and not root.pending_owner_results
            assert phase.publication is None and phase.application is None and not projected
        else:
            physical = phase.physical
            assert type(physical) is RequiredSQLReturned and type(physical.value) is CommittedContinuationP4
            assert physical.value.audit_outcome is observation.candidate
            assert physical.value.application is observation.application_candidate is applied[0]
            assert observation.committed and current == applied[0].snapshot and not root.pending_owner_results
            assert phase.publication is not None and phase.publication.audit_outcome is physical.value.audit_outcome
            if fault == "projection":
                assert phase.application is None and projection[0].errors == (projection_error,)
            else:
                assert phase.application is applied[0] and len(projected) == 1
            if fault == "after_commit":
                assert observation.original_error is physical_error and observation.readback is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("retention_fails", (False, True))
async def test_actual_unavailable_unwind_keeps_original_failure_and_one_physical_owner(
    operation_store, tmp_path, monkeypatch, retention_fails
):
    from elspeth.web.composer import tool_batch
    from elspeth.web.sessions import continuation_p4_application_store

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        plugin_error = RuntimeError("actual dispatched plugin fault")
        audit_error = OperationalError("actual chat insertion fault", None, RuntimeError("original DBAPI audit fault"))
        retention_error = RuntimeError("actual unavailable application fault")
        executed, applied = [], []
        actual_apply = continuation_p4_application_store.apply_committing_p4_application_on_connection

        def tool(*args, **kwargs):
            executed.append((args[0], args[1]))
            raise plugin_error

        def insert(*args, **kwargs):
            raise audit_error

        def apply(conn, commit, *, env_ref_names):
            value = actual_apply(conn, commit, env_ref_names=env_ref_names)
            applied.append(value)
            if retention_fails:
                raise retention_error
            return value

        monkeypatch.setattr(tool_batch, "execute_tool", tool)
        await _advance(owner)
        batch = owner.runtime.graph.batch.batch
        assert (
            owner.failure_custody is not None
            and owner.failure_custody.wrapper is not None
            and owner.failure_custody.wrapper.original_exc is plugin_error
            and batch.plugin_crash is None
        )
        assert (
            owner.failure_custody.original is plugin_error
            and batch.plugin_crash_cause is batch.advisor_failure is None
            and len(executed) == 1
        )
        service = owner.service._require_sessions_service()
        monkeypatch.setattr(service, "_insert_chat_message", insert)
        monkeypatch.setattr(continuation_p4_application_store, "apply_committing_p4_application_on_connection", apply)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        with pytest.raises(BaseException) as caught:
            await owner.finish_owed_prefix(owner.runtime.source.selected)
        assert _contains_identity(caught.value, audit_error)
        phase = owner.p4
        assert phase is not None and phase.writer_observation is not None and phase.physical is not None
        observation = phase.writer_observation
        sql = tuple(ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_SQL)
        projection = tuple(
            ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
        )
        assert len(sql) == len(projection) == 1 and sql[0].complete and projection[0].complete
        assert phase.physical is sql[0]._finish_once_handoff and phase.application_sql is None
        assert len(applied) == len(executed) == len(sdk) == 1
        current = await _registered_read(
            owner.work, StoredContinuationSnapshot, lambda: owner.authority.load_continuation_snapshot(running)
        )
        if retention_fails:
            assert _contains_identity(caught.value, retention_error)
            assert type(phase.physical) is RequiredSQLRaised and observation.known_not_committed and not observation.committed
            assert current.checkpoint_id == observation.commit.expected_checkpoint_id
            assert phase.publication is None and phase.application is None
        else:
            assert type(phase.physical) is RequiredSQLReturned and type(phase.physical.value) is CommittedContinuationP4
            assert observation.committed and phase.application is applied[0] and current == applied[0].snapshot
            outcome = phase.physical.value.audit_outcome
            assert outcome.unwind_audit_failed and outcome.checkpoint is None and outcome.assistant_id is None
            assert outcome.continuation_retention_failure is None
            assert phase.publication is not None and phase.publication.audit_outcome is outcome
            assert phase.application.bundle.result.receipt.unwind_audit_failed


@pytest.mark.asyncio
async def test_actual_committed_p4_readback_survives_terminal_progress_without_reminting_write_authority(
    operation_store, tmp_path, monkeypatch
):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        await owner.finish_owed_prefix(owner.runtime.source.selected)
        phase = owner.p4
        assert phase is not None and phase.committed_outcome is not None and phase.writer_observation is not None
        observation = phase.writer_observation
        actual = phase.committed_outcome.audit_outcome
        terminal = owner.authority.settle_continuation_owner(
            running, outcome=request_cancelled_error(request_id=None), env_ref_names=phase.inventory.names
        )
        assert terminal.failure_code == "request_cancelled"
        before = stored_rows(operation_store[0])
        original = RuntimeError("late actual completion observer")
        service = owner.service._require_sessions_service()
        returned = await _registered_read(
            owner.work,
            type(actual),
            lambda: service._readback_continuation_p4_transaction(observation, original=original, env_ref_names=phase.inventory.names),
        )
        assert returned is actual and observation.committed and observation.readback is not None
        assert observation.original_error is original and phase.application is phase.committed_outcome.application
        with pytest.raises(ComposerOperationFenceLost):
            owner.authority.load_continuation_snapshot(running)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
