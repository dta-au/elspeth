"""Runtime reads retain their one real SQL evidence result and late live guard."""

import asyncio
from dataclasses import replace
from hashlib import sha256

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_evidence as evidence_codec
from elspeth.web.composer import continuation_evidence_restore as evidence_reader
from elspeth.web.composer import continuation_prompt_producers as producers
from elspeth.web.composer import continuation_runtime as runtime_owner
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.protocol import ComposerContextUnsupported
from elspeth.web.sessions import continuation as storage
from elspeth.web.sessions import continuation_runtime as runtime_sql
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.coordination.test_composer_operation_authority import _admission_v2_authority
from tests.unit.web.coordination.test_continuation_p4_recovery_start import recovery_rows
from tests.unit.web.sessions import test_continuation_cold_restore as cold_cases
from tests.unit.web.sessions import test_continuation_runtime as runtime_cases

operation_store = runtime_cases.operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("consolidated", (True, False))
async def test_runtime_closure_reuses_member_bytes_only_above_original_sql_bound(operation_store, monkeypatch, consolidated):
    allowance = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES - (0 if consolidated else 1)
    policy = cold_cases.minimal_continuation_policy(composer_continuation_max_retained_payload_bytes=allowance)
    monkeypatch.setattr(cold_cases, "minimal_continuation_policy", lambda: policy)
    engine, repository, _authority, sessions, sid = operation_store
    authority = _admission_v2_authority(operation_store, policy=policy)
    arguments, _history, _attachments = await runtime_cases.runtime_case((engine, repository, authority, sessions, sid))
    selected, inventory = arguments["selected"], arguments["inventory"]
    running = inventory.running
    frame = selected.stack.frames[0]
    member_keys = {f"value:{ref.codec}:{ref.digest}" for ref in (frame.evidence, frame.bootstrap, frame.iteration)}
    member_reads = []
    closure_reads = []
    actual_member = evidence_reader.read_chunk
    actual_closure = evidence_reader.load_selected_private_chunks_on_connection

    def member(*args, **kwargs):
        member_reads.append(kwargs["key"])
        return actual_member(*args, **kwargs)

    def closure(*args, **kwargs):
        result = actual_closure(*args, **kwargs)
        closure_reads.append(result)
        return result

    monkeypatch.setattr(evidence_reader, "read_chunk", member)
    monkeypatch.setattr(producers, "read_chunk", member)
    monkeypatch.setattr(evidence_reader, "load_selected_private_chunks_on_connection", closure)
    monkeypatch.setattr(runtime_sql, "load_selected_private_chunks_on_connection", closure)
    try:

        def independent_read():
            with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
                inputs = producers._read_current_inputs(conn, selected=selected, inventory=inventory)
                return runtime_sql._runtime_source_from_inputs_on_connection(
                    conn, running, selected=selected, inventory=inventory, inputs=inputs
                )

        baseline = independent_read()
        # The unchanged independent path is the positive control for the same
        # member observer; a nonmember key cannot satisfy these assertions.
        assert member_keys <= set(member_reads)
        assert len(closure_reads) == 1 and closure_reads[0] == selected.chunks
        member_reads.clear()
        closure_reads.clear()
        result = arguments["authority"].read_continuation_runtime(running, selected=selected, inventory=inventory)
        assert replace(result, timing=baseline.timing) == baseline
        assert len(closure_reads) == 1 and closure_reads[0] == selected.chunks
        if consolidated:
            assert member_keys.isdisjoint(member_reads)
        else:
            assert member_keys <= set(member_reads)
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_runtime_small_allowance_refuses_oversized_actual_member_before_complete_load(operation_store, monkeypatch):
    allowance = CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES - 1
    policy = cold_cases.minimal_continuation_policy(composer_continuation_max_retained_payload_bytes=allowance)
    monkeypatch.setattr(cold_cases, "minimal_continuation_policy", lambda: policy)
    engine, repository, _authority, sessions, sid = operation_store
    authority = _admission_v2_authority(operation_store, policy=policy)
    arguments, _history, _attachments = await runtime_cases.runtime_case((engine, repository, authority, sessions, sid))
    selected, inventory = arguments["selected"], arguments["inventory"]
    running = inventory.running
    reference = selected.stack.frames[0].evidence
    key = f"value:{reference.codec}:{reference.digest}"
    sid = str(running.claim.session_id)
    actual_bounded = storage.read_bounded_retained_chunk
    actual_closure = evidence_reader.load_selected_private_chunks_on_connection
    bounded_reads = []
    complete_reads = []

    def bounded(*args, **kwargs):
        bounded_reads.append((kwargs["key"], kwargs["allowance"]))
        return actual_bounded(*args, **kwargs)

    def complete(*args, **kwargs):
        complete_reads.append(args)
        return actual_closure(*args, **kwargs)

    monkeypatch.setattr(storage, "read_bounded_retained_chunk", bounded)
    monkeypatch.setattr(evidence_reader, "load_selected_private_chunks_on_connection", complete)
    monkeypatch.setattr(runtime_sql, "load_selected_private_chunks_on_connection", complete)
    try:
        positive = arguments["authority"].read_continuation_runtime(running, selected=selected, inventory=inventory)
        assert positive.selected is selected
        assert (key, allowance) in bounded_reads and len(complete_reads) == 1
        bounded_reads.clear()
        complete_reads.clear()
        # Actual stored bytes and their SQL metadata agree. The retained root's
        # smaller reference must not permit materializing this larger payload.
        payload = ('"' + "x" * (allowance - 1) + '"').encode("utf-8")
        assert len(payload) == allowance + 1
        with locked_session_transaction(operation_store[0], sid) as conn:
            changed = conn.execute(
                update(chunks)
                .where(chunks.c.session_id == sid, chunks.c.operation_id == running.claim.operation_id, chunks.c.chunk_key == key)
                .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=len(payload))
            )
            assert changed.rowcount == 1
        with pytest.raises(AuditIntegrityError, match=r"original bound|SQL binding"):
            arguments["authority"].read_continuation_runtime(running, selected=selected, inventory=inventory)
        assert (key, allowance) in bounded_reads
        assert complete_reads == []
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_restore_final_read_copies_prior_data_with_fresh_source_admission(operation_store, monkeypatch):
    from elspeth.web.composer.continuation_runtime import restore_continuation_runtime

    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    authority = arguments["authority"]
    original_read = authority.read_continuation_runtime
    reads = []
    original_copy = evidence_codec.AdmittedTurnEvidence._copy_original_graph_for_source
    copies = []

    def copy_source(self, *args, **kwargs):
        result = original_copy(self, *args, **kwargs)
        copies.append((self, result))
        return result

    def observed_read(*args, **kwargs):
        result = original_read(*args, **kwargs)
        reads.append((kwargs.get("_prior_evidence"), result))
        return result

    monkeypatch.setattr(authority, "read_continuation_runtime", observed_read)
    monkeypatch.setattr(evidence_codec.AdmittedTurnEvidence, "_copy_original_graph_for_source", copy_source)
    try:
        restored = await restore_continuation_runtime(**arguments)
        repeated = [(prior, result) for prior, result in reads if prior is not None]
        assert len(repeated) == 1
        prior, final_source = repeated[0]
        assert prior is restored.source.admitted_evidence
        assert final_source is not restored.source
        assert final_source.admitted_evidence is not prior
        assert final_source.evidence is prior.snapshot
        assert final_source.admitted_evidence._graph_lock is not prior._graph_lock
        matching = [copied for original, copied in copies if original is prior]
        assert len(matching) == 1 and matching[0] is not None
        assert len(reads) >= 2
        with pytest.raises(AuditIntegrityError, match="runtime read requires its actual current and selected owners"):
            await asyncio.to_thread(
                original_read,
                replace(running),
                selected=arguments["selected"],
                inventory=arguments["inventory"],
                _prior_evidence=prior,
            )
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,refused", (("ledger", False), ("source", False), ("ledger", True)))
async def test_capture_prior_data_keeps_final_actual_sql_proof_and_refusal_group(operation_store, tmp_path, monkeypatch, fault, refused):
    from tests.unit.web.sessions.test_continuation_semantic_advisor import _semantic_case

    async with _semantic_case(operation_store, tmp_path, monkeypatch, end_candidate=True) as case:
        runtime = case["runtime"]
        selected, inventory = runtime.source.selected, runtime.materialization.inventory
        authority, running = runtime.materialization.authority, inventory.running
        actual_read = authority.read_continuation_runtime
        actual_capture = runtime_owner._capture_owner_data
        reads = []

        def read_source(*args, **kwargs):
            reads.append(kwargs.get("_prior_evidence"))
            return actual_read(*args, **kwargs)

        monkeypatch.setattr(authority, "read_continuation_runtime", read_source)
        positive = await runtime_owner.capture_continuation_runtime_data(runtime, selected=selected, inventory=inventory)
        assert positive.captured.selected is selected
        assert reads == [runtime.source.admitted_evidence]
        assert positive.admitted_evidence is not runtime.source.admitted_evidence
        reads.clear()
        sid = str(running.claim.session_id)
        with operation_store[0].connect() as conn:
            attempts_before = tuple(conn.execute(select(physical.c.attempt_id).where(physical.c.session_id == sid)).scalars())
        refusal = ComposerContextUnsupported("retained_chunk_bytes")
        captured = []

        def change_after_capture(*args, **kwargs):
            result = actual_capture(*args, **kwargs)
            captured.append(result)
            with locked_session_transaction(operation_store[0], sid) as conn:
                if fault == "ledger":
                    entry = conn.execute(select(usage).where(usage.c.session_id == sid)).one()
                    changed = conn.execute(
                        update(usage).where(usage.c.entry_id == entry.entry_id).values(prompt_tokens=entry.prompt_tokens + 1)
                    )
                else:
                    reference = selected.stack.frames[0].tools
                    assert reference is not None
                    assert reference not in {chunk.reference for chunk in runtime.source.admitted_evidence.dependency_chunks}
                    changed = conn.execute(
                        update(chunks)
                        .where(
                            chunks.c.session_id == sid,
                            chunks.c.operation_id == running.claim.operation_id,
                            chunks.c.chunk_key == f"value:{reference.codec}:{reference.digest}",
                        )
                        .values(sha256="0" * 64)
                    )
                assert changed.rowcount == 1
            if refused:
                raise refusal
            return result

        monkeypatch.setattr(runtime_owner, "_capture_owner_data", change_after_capture)
        if refused:
            with pytest.raises(BaseExceptionGroup, match="Retention refusal and final current-source admission") as caught:
                await runtime_owner.capture_continuation_runtime_data(runtime, selected=selected, inventory=inventory)
            assert len(caught.value.exceptions) == 2
            assert caught.value.exceptions[0] is refusal
            assert isinstance(caught.value.exceptions[1], AuditIntegrityError)
        else:
            with pytest.raises(AuditIntegrityError):
                await runtime_owner.capture_continuation_runtime_data(runtime, selected=selected, inventory=inventory)
        assert len(captured) == 1 and reads == [runtime.source.admitted_evidence]
        with operation_store[0].connect() as conn:
            after = tuple(conn.execute(select(physical.c.attempt_id).where(physical.c.session_id == sid)).scalars())
        assert after == attempts_before


@pytest.mark.asyncio
async def test_runtime_source_retains_its_actual_validated_evidence_and_rejects_another_running_owner(operation_store, monkeypatch):
    arguments, _history, _attachments = await runtime_cases.runtime_case(operation_store)
    running = arguments["inventory"].running
    selected = arguments["selected"]
    original = producers.read_turn_evidence_with_current_chunks_on_connection
    original_constructor = SelectedContinuationState.__post_init__
    observed = []
    constructors = []

    def capture(*args, **kwargs):
        evidence, current_chunks = original(*args, **kwargs)
        observed.append(evidence)
        return evidence, current_chunks

    def validate_selection(self):
        constructors.append(self)
        original_constructor(self)

    monkeypatch.setattr(producers, "read_turn_evidence_with_current_chunks_on_connection", capture)
    monkeypatch.setattr(SelectedContinuationState, "__post_init__", validate_selection)
    try:
        calibrated = replace(selected)
        assert calibrated == selected and calibrated is not selected
        assert len(constructors) == 1 and constructors[0] is calibrated
        before = recovery_rows(operation_store[0])
        result = await asyncio.to_thread(
            arguments["authority"].read_continuation_runtime,
            running,
            selected=arguments["selected"],
            inventory=arguments["inventory"],
        )
        assert len(observed) == 1 and result.evidence is observed[0].snapshot
        assert result.selected is arguments["selected"]
        assert all(owner is not selected for owner in constructors)
        assert recovery_rows(operation_store[0]) == before
        another = replace(running)
        assert another == running and another is not running
        with pytest.raises(AuditIntegrityError, match="runtime read requires its actual current and selected owners"):
            await asyncio.to_thread(
                arguments["authority"].read_continuation_runtime,
                another,
                selected=arguments["selected"],
                inventory=arguments["inventory"],
            )
        assert all(owner is not selected for owner in constructors)

        # Supported changes construct a new owner and still run its real guard.
        with pytest.raises(AuditIntegrityError, match="continuation snapshot violates its owned codec"):
            replace(selected, stack=selected.stack.model_copy(update={"cursor": -1}))
        assert recovery_rows(operation_store[0]) == before
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
