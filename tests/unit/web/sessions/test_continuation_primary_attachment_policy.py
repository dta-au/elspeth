"""Configured complete-original inspection through the actual PRIMARY phase."""

import json
import time
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from sqlalchemy import insert, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_attachment_application_driver, continuation_attachment_result
from elspeth.web.composer import continuation_evidence_restore as evidence_reader
from elspeth.web.composer.attachment_admission import AttachmentInspectionPolicy
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.continuation_semantic_advisor_read import execute_semantic_advisor_read
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tools import blobs
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions import continuation_attachment_preparation as preparation_source
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation_semantic_actions import install_semantic_action_on_connection, prepare_semantic_read_on_connection
from elspeth.web.sessions.continuation_semantic_advisor import retain_semantic_advisor_outcome_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.helpers.continuation_budgets import OWNER_LEASE_SECONDS
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.composer.test_continuation_semantic_advisor import _reply
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.coordination.test_continuation_p4_recovery_start import current_selected, new_owners
from tests.unit.web.sessions.test_continuation_attachment_read import _set_current_inspection_limit
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service
from tests.unit.web.sessions.test_continuation_semantic_actions import _read_answer
from tests.unit.web.sessions.test_continuation_semantic_advisor import _begin, _invoke, _semantic_case

operation_store = _operation_store


def _install_attachment(store, service, running, blob_id, raw, limit):
    _set_current_inspection_limit(service, limit)
    sessions = service._require_sessions_service()
    assert sessions._data_dir is not None
    path = sessions._data_dir / "blobs" / str(running.claim.session_id) / f"{blob_id}_notes.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with store[0].begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=blob_id,
                session_id=str(running.claim.session_id),
                filename="notes.txt",
                mime_type="text/plain",
                size_bytes=len(raw),
                content_hash=sha256(raw).hexdigest(),
                status="ready",
                created_by="user",
                creation_modality="verbatim",
                storage_path=str(path),
                created_at=datetime.now(UTC),
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("read_attachment", "get_blob_content"))
@pytest.mark.parametrize("over_limit", (False, True))
async def test_primary_attachment_uses_configured_original_inspection_and_retains_owner(
    operation_store, tmp_path, monkeypatch, tool_name, over_limit
):
    raw = ("é" * 16).encode("utf-8")
    limit = len(raw) - int(over_limit)
    blob_id = str(uuid4())
    actual_inspect = blobs.inspect_attachment_original
    inspections = []
    actual_bind = ContinuationLivePhases._bind_retained_attachment_audit
    audit_bindings = []

    def bind(owner):
        runtime = owner.runtime
        assert runtime is not None and runtime.graph.batch is not None
        call = runtime.graph.batch.batch.current_call
        assert call is not None and call.audit is not None
        live_audit = call.audit
        retained = runtime.source.admitted_evidence.graph
        assert retained is not None
        assert runtime.graph.selected_audit is not None and retained.selected_audit is not None
        assert runtime.graph.selected_audit != retained.selected_audit
        actual_bind(owner)
        current = owner.runtime
        assert current is not None
        assert current.graph.selected_audit is retained.selected_audit
        assert current.graph.selected_dialect is retained.selected_dialect
        assert current.graph.batch is runtime.graph.batch and call.audit is live_audit
        assert current.live is runtime.live and current.materialization is runtime.materialization
        assert current.graph.messages is runtime.graph.messages and current.graph.recorder is runtime.graph.recorder
        audit_bindings.append(retained.selected_audit)

    monkeypatch.setattr(ContinuationLivePhases, "_bind_retained_attachment_audit", bind)

    def inspect(**kwargs):
        value = actual_inspect(**kwargs)
        inspections.append(value)
        return value

    monkeypatch.setattr(blobs, "inspect_attachment_original", inspect)

    def install(owner, running):
        _install_attachment(operation_store, owner.service, running, blob_id, raw, limit)

    async with _phase_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_call=("original_attachment_call", tool_name, {"blob_id": blob_id}),
        before_batch_initialization=install,
    ) as (owner, _running, primary, sdk):
        original = primary.outcome.completion.tool_batch.calls[0]
        runtime = owner.runtime
        assert runtime is not None
        await _advance(owner)
        current = owner.runtime
        assert current is not None and current.graph.batch is not None
        assert current.live is runtime.live and current.materialization is runtime.materialization
        batch = current.graph.batch.batch
        assert batch.current_call is None and batch.next_tool_index == 1
        assert batch.tool_outcomes[0].call is original
        response = batch.tool_outcomes[0].response
        assert response.success is (not over_limit)
        history = current.source.attachments
        key = f"attachment:{primary.response.reference.digest}:0"
        assert len(history.applications) == 1 and history.applications[0].effect_key == key
        if over_limit:
            assert inspections == [] and history.reads == ()
            assert response.validation.errors[0].message == (
                "The complete attachment was refused by the current inspection policy; no content was returned."
            )
        else:
            assert len(inspections) == len(history.reads) == 1
            retained = history.reads[0]
            assert retained.effect_key == key and len(retained.inspections) == 1
            inspection = retained.inspections[0]
            assert inspection.size_bytes == len(raw) and inspection.content_hash == sha256(raw).hexdigest()
            assert (
                inspection.policy_fingerprint
                == AttachmentInspectionPolicy(limit, env_ref_names=current.materialization.inventory.names).fingerprint
            )
            assert inspection.policy_fingerprint == inspections[0].policy_fingerprint
        assert len(sdk) == 1
        assert len(audit_bindings) == 1
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ("creator", "batch", "transition"))
async def test_cold_attachment_rejects_changed_original_preparation_source_before_read(operation_store, tmp_path, monkeypatch, changed):
    raw = b"Complete customer prose"
    blob_id = str(uuid4())
    original_read = continuation_attachment_result.run_attachment_result
    interruption = RuntimeError("test stops after original unissued preparation")
    reads = []

    async def interrupt(**kwargs):
        raise interruption

    async def observe(**kwargs):
        reads.append(kwargs)
        return await original_read(**kwargs)

    def install(owner, running):
        _install_attachment(operation_store, owner.service, running, blob_id, raw, len(raw))

    monkeypatch.setattr(continuation_attachment_result, "run_attachment_result", interrupt)
    async with _phase_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_call=("original_attachment_call", "read_attachment", {"blob_id": blob_id}),
        before_batch_initialization=install,
    ) as (owner, running, _primary, sdk):
        with pytest.raises(RuntimeError) as raised:
            await _advance(owner)
        assert raised.value is interruption and owner.latest_snapshot is not None
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=owner.latest_snapshot, required_work=owner.work
        )
        # Positive control uses the real immutable publication and current claim.
        assert owner.runtime is not None
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            admitted = preparation_source.read_unissued_attachment_preparation_on_connection(
                conn, running, selected=selected, inventory=owner.runtime.materialization.inventory
            )
        assert admitted.batch.current_call is not None and admitted.batch.current_call.tool_name == "read_attachment"
        actual_chunk = preparation_source.read_chunk

        def changed_event(conn, *, session_id, operation_id, key):
            encoded = actual_chunk(conn, session_id=session_id, operation_id=operation_id, key=key)
            if key != f"event:{selected.stack.cursor}":
                return encoded
            value = json.loads(encoded)
            if changed == "creator":
                value["preparation"]["creator_operation_epoch"] += 1
            elif changed == "batch":
                value["preparation"]["prepared"]["digest"] = "0" * 64
            else:
                value["transition"] = "tool_issued"
            return canonical_json(value).encode("utf-8")

        monkeypatch.setattr(preparation_source, "read_chunk", changed_event)
        monkeypatch.setattr(continuation_attachment_result, "run_attachment_result", observe)
        cold = ContinuationLivePhases(owner.service, owner.authority, owner.work, owner.provider_owner, owner.lease)
        with pytest.raises(AuditIntegrityError, match="attachment preparation"):
            await cold.advance(selected)
        assert reads == [] and len(sdk) == 1
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_attachment_prefix_keeps_next_original_tool_and_its_dispatch_allowance(operation_store, tmp_path, monkeypatch):
    raw = b"Complete customer prose"
    blob_id = str(uuid4())

    def install(owner, running):
        _install_attachment(operation_store, owner.service, running, blob_id, raw, len(raw))

    async with _phase_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_call=("original_attachment_call", "read_attachment", {"blob_id": blob_id}),
        following_tool_call=("original_following_call", "get_pipeline_state", {}),
        before_batch_initialization=install,
    ) as (owner, _running, primary, sdk):
        await _advance(owner)
        assert owner.runtime is not None and owner.runtime.graph.batch is not None
        runtime = owner.runtime
        assert runtime.graph.batch.batch.phase == "call_ready"
        policy = runtime.source.policy
        allowance = next_continuation_action_policy(selected=runtime.source.selected, policy=policy)
        expected = max(
            (policy.actions.read, policy.actions.session_sql, policy.actions.blob, policy.actions.external), key=lambda value: value.total
        )
        assert allowance == expected and allowance.total > policy.actions.session_sql.total
        await _advance(owner)
        assert owner.runtime is not None and owner.runtime.graph.batch is not None
        batch = owner.runtime.graph.batch.batch
        assert batch.phase == "dispatch_ready" and batch.next_tool_index == 2
        assert len(batch.tool_outcomes) == 2
        assert batch.tool_outcomes[0].call is primary.outcome.completion.tool_batch.calls[0]
        assert batch.tool_outcomes[1].call is primary.outcome.completion.tool_batch.calls[1]
        assert batch.tool_outcomes[1].response.success and len(sdk) == 1
        assert next_continuation_action_policy(selected=owner.runtime.source.selected, policy=policy) == expected
        await _advance(owner)
        assert owner.p4 is not None and owner.p4.application is not None
        assert owner.finished_batch is not None and len(sdk) == 1
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ("prepared", "published", "history_applied"))
async def test_primary_attachment_cold_committed_suffix_never_reads_original_again(operation_store, tmp_path, monkeypatch, cut):
    raw = b"Complete customer prose"
    blob_id = str(uuid4())
    inspected = []
    actual_inspect = blobs.inspect_attachment_original

    def inspect(**kwargs):
        inspected.append(kwargs["raw"])
        return actual_inspect(**kwargs)

    monkeypatch.setattr(blobs, "inspect_attachment_original", inspect)

    def install(owner, running):
        _install_attachment(operation_store, owner.service, running, blob_id, raw, len(raw))

    original_history = continuation_attachment_result.apply_attachment_history
    original_read = continuation_attachment_result.run_attachment_result
    original_application = continuation_attachment_application_driver.apply_current_attachment_runtime
    interruption = RuntimeError("test interrupts after actual durable attachment result")

    async def interrupt(**kwargs):
        raise interruption

    if cut == "prepared":
        monkeypatch.setattr(continuation_attachment_result, "run_attachment_result", interrupt)
    elif cut == "published":
        monkeypatch.setattr(continuation_attachment_result, "apply_attachment_history", interrupt)
    else:
        monkeypatch.setattr(continuation_attachment_application_driver, "apply_current_attachment_runtime", interrupt)
    async with _phase_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_call=("original_attachment_call", "read_attachment", {"blob_id": blob_id}),
        before_batch_initialization=install,
    ) as (owner, running, primary, sdk):
        with pytest.raises(RuntimeError) as raised:
            await _advance(owner)
        assert raised.value is interruption and inspected == ([] if cut == "prepared" else [raw])
        assert owner.latest_snapshot is not None
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=owner.latest_snapshot, required_work=owner.work
        )
        if cut == "published":
            assert owner.runtime is not None
            inventory = owner.runtime.materialization.inventory
            actual_graph = evidence_reader.read_selected_turn_evidence_graph_on_connection
            graph_reads = []

            def observe_graph(*args, **kwargs):
                graph_reads.append(kwargs["selected"])
                return actual_graph(*args, **kwargs)

            with monkeypatch.context() as observation:
                observation.setattr(evidence_reader, "read_selected_turn_evidence_graph_on_connection", observe_graph)
                with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
                    positive = evidence_reader.read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)
                    assert positive.graph is not None and graph_reads == [selected]
                    key = selected.stack.pending_owner_results[0].effect_key
                    for changed in ({"disposition": "prepared"}, {"receipt_key": "foreign-attachment-receipt"}):
                        nested = conn.begin_nested()
                        try:
                            conn.execute(
                                update(effects)
                                .where(
                                    effects.c.session_id == str(running.claim.session_id),
                                    effects.c.operation_id == running.claim.operation_id,
                                    effects.c.effect_key == key,
                                )
                                .values(**changed)
                            )
                            with pytest.raises(AuditIntegrityError):
                                evidence_reader.read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)
                            assert graph_reads == [selected]
                        finally:
                            nested.rollback()
                    evidence_reader.read_turn_evidence_on_connection(conn, running, selected=selected, inventory=inventory)
                    assert graph_reads == [selected, selected]
        monkeypatch.setattr(continuation_attachment_result, "apply_attachment_history", original_history)
        monkeypatch.setattr(continuation_attachment_result, "run_attachment_result", original_read)
        monkeypatch.setattr(continuation_attachment_application_driver, "apply_current_attachment_runtime", original_application)
        cold = ContinuationLivePhases(owner.service, owner.authority, owner.work, owner.provider_owner, owner.lease)
        await cold.advance(selected)
        assert cold.runtime is not None and cold.runtime.graph.batch is not None
        data = cold.runtime.graph.batch
        assert data.batch.next_tool_index == 1 and data.batch.current_call is None
        assert len(data.batch.tool_outcomes) == 1 and data.batch.tool_outcomes[0].response.success
        assert data.batch.tool_outcomes[0].call is data.calls[0]
        assert data.calls[0].id == primary.outcome.completion.tool_batch.calls[0].id
        assert inspected == [raw] and len(sdk) == 1
    owner.work.coordinator.assert_completed()


@pytest.mark.asyncio
async def test_unissued_attachment_real_fresh_claim_reprepares_without_paid_replay(operation_store, tmp_path, monkeypatch):
    raw = b"Complete customer prose"
    blob_id = str(uuid4())
    inspected = []
    actual_inspect = blobs.inspect_attachment_original
    original_read = continuation_attachment_result.run_attachment_result
    interruption = RuntimeError("test interrupts original preparation before its read effect")

    def inspect(**kwargs):
        inspected.append(kwargs["raw"])
        return actual_inspect(**kwargs)

    async def interrupt(**kwargs):
        raise interruption

    def install(owner, running):
        _install_attachment(operation_store, owner.service, running, blob_id, raw, len(raw))

    monkeypatch.setattr(blobs, "inspect_attachment_original", inspect)
    monkeypatch.setattr(continuation_attachment_result, "run_attachment_result", interrupt)
    async with _phase_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_call=("original_attachment_call", "read_attachment", {"blob_id": blob_id}),
        before_batch_initialization=install,
    ) as (owner, original, primary, sdk):
        with pytest.raises(RuntimeError) as raised:
            await _advance(owner)
        assert raised.value is interruption and inspected == []
        assert owner.runtime is not None and not owner.runtime.source.selected.stack.is_handoff_safe
        original_data = owner.runtime.graph.batch
        assert original_data is not None and original_data.batch.current_call is not None
        old_audit = original_data.batch.current_call.audit
        assert old_audit is not None
        await owner.lease.close()
        owner.work.coordinator.assert_completed()
        current = new_owners(operation_store)
        _engine, repository, authority, sessions, sid = current
        assert repository.recover_composer_segment(
            session_id=sid,
            operation_id=original.claim.operation_id,
            expected_generation=original.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        (claim,) = authority.claim_segment(limit=1)
        context = repository.start_composer_segment(
            claim, owner_instance_id=authority._owner_instance_id, lease_seconds=OWNER_LEASE_SECONDS, auth_provider_type="local"
        )
        running = ComposerOperationRunning(claim=claim, session_operation_context=context)
        work = inventory_work(running)
        lease = await ContinuationLeaseAdoption(
            repository, context, lease_seconds=OWNER_LEASE_SECONDS, required_work=work.coordinator
        ).join()
        try:
            selected, _inventory = await current_selected(current, running)
            service, _actors = current_service(current)
            _set_current_inspection_limit(service, len(raw))
            cold = ContinuationLivePhases(service, authority, work, ProviderInvocationOwner(service=sessions, required_work=work), lease)
            monkeypatch.setattr(continuation_attachment_result, "run_attachment_result", original_read)
            prepared_audits = []

            async def observe_read(**kwargs):
                prepared_audits.append(kwargs["audit"])
                return await original_read(**kwargs)

            monkeypatch.setattr(continuation_attachment_result, "run_attachment_result", observe_read)
            await cold.advance(selected)
            assert cold.runtime is not None and cold.runtime.graph.batch is not None
            batch = cold.runtime.graph.batch.batch
            assert batch.next_tool_index == 1 and batch.tool_outcomes[0].response.success
            assert batch.tool_outcomes[0].call.id == primary.outcome.completion.tool_batch.calls[0].id
            assert len(prepared_audits) == 1 and prepared_audits[0] is not old_audit
            assert prepared_audits[0].started_at >= old_audit.started_at
            assert running.session_operation_context.fence != original.session_operation_context.fence
            assert inspected == [raw] and len(sdk) == 1
        finally:
            await lease.close()
        work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ("read_attachment", "get_blob_content"))
@pytest.mark.parametrize("over_limit", (False, True))
async def test_paid_semantic_read_has_the_same_configured_attachment_boundary(
    operation_store, tmp_path, monkeypatch, tool_name, over_limit
):
    raw = ("é" * 16).encode("utf-8")
    limit = len(raw) - int(over_limit)
    blob_id = str(uuid4())
    async with _semantic_case(operation_store, tmp_path, monkeypatch, timed_read=True) as case:
        runtime, running = case["runtime"], case["running"]
        service = runtime.materialization.service
        _install_attachment(operation_store, service, running, blob_id, raw, limit)
        physical = []

        async def completion(**kwargs):
            physical.append(kwargs)
            answer = _read_answer(case["request"])
            answer["prescribed_action"].update(tool_name=tool_name, arguments_json=canonical_json({"blob_id": blob_id}))
            return _reply(canonical_json(answer))

        monkeypatch.setattr(litellm, "acompletion", completion)
        issuer, prepared, dispatched = _begin(operation_store, case)
        observed = await _invoke(case, issuer)
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            retained = retain_semantic_advisor_outcome_on_connection(
                conn, prepared, selected=dispatched, outcome=observed, recorder=case["recorder"], issuer=issuer
            )
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            installed = install_semantic_action_on_connection(
                conn, running, selected=retained.snapshot, effect_key=prepared.binding.effect_key
            )
            selected = prepare_semantic_read_on_connection(conn, running, selected=installed, effect_key=prepared.binding.effect_key)
        deadline = case["sql_deadline"]
        assert deadline is not None
        deadline.begin_action(deadline.policy.actions.read, started_at=time.monotonic())
        actual = await execute_semantic_advisor_read(
            service=service,
            authority=runtime.materialization.authority,
            running=running,
            selected=selected,
            effect_key=prepared.binding.effect_key,
            runtime=runtime,
            required_work=case["work"],
        )
        assert actual.result.success is (not over_limit)
        original = actual.committed.original_result
        assert original.prescribing_provider_call_id is not None
        if over_limit:
            assert original.attachment_inspections == ()
            assert actual.result.validation.errors[0].message == (
                "The complete attachment was refused by the current inspection policy; no content was returned."
            )
        else:
            assert len(original.attachment_inspections) == 1
            inspection = original.attachment_inspections[0]
            assert inspection.content_hash == sha256(raw).hexdigest() and inspection.size_bytes == len(raw)
            assert inspection.policy_fingerprint == AttachmentInspectionPolicy(limit, env_ref_names=runtime.env_ref_names).fingerprint
        assert len(physical) == 1
        case["work"].coordinator.assert_completed()
