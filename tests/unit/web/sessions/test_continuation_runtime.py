"""Actual current runtime restore/capture with complete closed owner chunks.

The ordinary fixture upgrades the existing real ingress/message owner input.
The recovery fixture uses an actual guarded provider publication, then the
installed committed-result recovery selector. Neither activates a full driver.
"""

import asyncio
import time
from dataclasses import replace
from hashlib import sha256
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PROMPT_GUARD_TRUST_KEY
from elspeth.web.composer import continuation_runtime as runtime_owner
from elspeth.web.composer.attachment_admission import AttachmentInspectionPolicy, inspect_attachment_original
from elspeth.web.composer.continuation_attachment_history import (
    append_attachment_inspections,
    decode_attachment_history,
    empty_attachment_history,
    encode_attachment_history,
)
from elspeth.web.composer.continuation_caches import (
    capture_continuation_caches,
    decode_continuation_caches,
    encode_continuation_caches,
)
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_iteration import decode_continuation_iteration, encode_continuation_iteration
from elspeth.web.composer.continuation_runtime import (
    build_runtime_tool_batch_context,
    capture_continuation_runtime_owners,
    restore_continuation_runtime,
)
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.discovery_cache import CachedDiscoveryPayload, make_cache_key
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tools.generation import EXPRESSION_GRAMMAR_RESPONSE_CONTRACT
from elspeth.web.execution.runtime_preflight import RuntimePreflightKey
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import sessions_table
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_continuation_caches import verdict
from tests.unit.web.composer.test_continuation_prepared_batch_data import capture, prepared_cut
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.coordination.test_continuation_p4_recovery_start import current_selected, new_owners, recovery_rows
from tests.unit.web.sessions.test_continuation_current_evidence import guarded_case
from tests.unit.web.sessions.test_continuation_primary_result import publish_actual
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service

operation_store = operations.operation_store


async def runtime_case(store, *, provider_input=None):
    if provider_input is None:
        kwargs, _materialization, _shelf, _recorder = await guarded_case(store)
    else:
        kwargs, result = provider_input
        work = inventory_work(kwargs["inventory"].running)
        selected = await load_selected_continuation(
            authority=kwargs["authority"], running=work.running, selected=result.snapshot, required_work=work
        )
        inventory = await materialize_continuation_env_inventory(
            advisor=inventory_advisor(store[3]), authority=kwargs["authority"], selected=selected, required_work=work
        )
        work.coordinator.assert_completed()
        kwargs = {**kwargs, "selected": selected, "inventory": inventory}
    running = kwargs["inventory"].running
    selected = kwargs["selected"]
    frame = selected.stack.frames[0]
    service = kwargs["service"]
    policy_snapshot, _catalog = service._policy_context.build("alice")
    result = verdict()
    historical_cache = capture_continuation_caches(
        discovery={
            make_cache_key("get_expression_grammar", {}): CachedDiscoveryPayload(
                True, (), None, EXPRESSION_GRAMMAR_RESPONSE_CONTRACT.admit({"grammar": "Retained grammar history"})
            )
        },
        preflight={
            RuntimePreflightKey(
                "retained-runtime", 1, "retained-state", "retained-settings", True, running.session_operation_context
            ): result
        },
        last_preflight=result,
        creator=running.session_operation_context,
    )
    cache_chunk = encode_continuation_caches(historical_cache)
    # An actual original-byte detector produces this historical receipt. It
    # grants no filesystem clearance, source delivery or later prompt trust.
    raw = b"Retained complete original inspection"
    admission = inspect_attachment_original(
        session_id=str(store[4]),
        blob_id=str(uuid4()),
        verified_hash=sha256(raw).hexdigest(),
        raw=raw,
        policy=AttachmentInspectionPolicy(4096),
    )
    attachment_history = append_attachment_inspections(
        empty_attachment_history(session_id=str(store[4]), operation_id=running.claim.operation_id),
        effect_key="runtime-fixture:actual-original-inspection",
        inspections=(admission,),
    )
    attachments = encode_attachment_history(attachment_history)
    tools = encode_private_chunk(
        codec="composer.tools.v1", canonical=canonical_json(composer_loop_tool_definitions(service._planner_dialect))
    )
    old_iteration = next(chunk for chunk in selected.chunks if chunk.reference == frame.iteration)
    iteration = decode_continuation_iteration(old_iteration)
    iteration = iteration.model_copy(
        update={
            "disclosure": service._schema_disclosure.snapshot_for_continuation(
                session_id=str(store[4]), catalog_hash=policy_snapshot.snapshot_hash, policy_hash=policy_snapshot.policy_hash
            )
        }
    )
    iteration_chunk = encode_continuation_iteration(iteration)
    changed = frame.model_copy(
        update={
            "caches": cache_chunk.reference,
            "attachment_admissions": attachments.reference,
            "tools": tools.reference,
            "iteration": iteration_chunk.reference,
            "composition_slice": 2,
            "composition_total": 7,
            "discovery_slice": 3,
            "discovery_total": 11,
            "advisor_calls": 4,
            "repair_turns": 1,
            "advisor_checkpoint_passes": 2,
            "mutation_success_seen": True,
        }
    )
    root = selected.stack.model_copy(update={"frames": (changed, *selected.stack.frames[1:])})
    with locked_session_transaction(store[0], str(store[4])) as conn:
        for chunk in (cache_chunk, attachments, tools, iteration_chunk):
            put_chunk(
                conn,
                session_id=str(store[4]),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        stored = append_complete_snapshot(
            conn,
            session_id=str(store[4]),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"complete closed runtime owner input"}'),),
            handoff=False,
        )
    kwargs["required_work"].coordinator.assert_completed()
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=kwargs["authority"], running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint, authority=kwargs["authority"], selected=selected, required_work=work
    )
    arguments = {
        "service": service,
        "authority": kwargs["authority"],
        "selected": selected,
        "inventory": inventory,
        "required_work": work,
        "provider_owner": ProviderInvocationOwner(service=store[3], required_work=work),
    }
    return arguments, historical_cache, attachment_history


async def install_capture(store, arguments, captured):
    running = arguments["inventory"].running
    with locked_session_transaction(store[0], str(store[4])) as conn:
        # Actual complete owner-value transaction boundary. It does not dispatch
        # work, turn inspection history into clearance or advance a phase.
        for chunk in captured.chunks:
            put_chunk(
                conn,
                session_id=str(store[4]),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        stored = append_complete_snapshot(
            conn,
            session_id=str(store[4]),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=captured.selected.checkpoint_id,
            expected_cursor=captured.selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(captured.stack),
            event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"actual runtime capture owner values"}'),),
            handoff=False,
        )
    selected = await load_selected_continuation(
        authority=arguments["authority"], running=running, selected=stored, required_work=arguments["required_work"]
    )
    inventory = await materialize_continuation_env_inventory(
        advisor=arguments["service"]._advisor_checkpoint,
        authority=arguments["authority"],
        selected=selected,
        required_work=arguments["required_work"],
    )
    return {**arguments, "selected": selected, "inventory": inventory}


@pytest.mark.asyncio
async def test_current_runtime_restores_complete_history_and_one_shared_graph_then_captures(operation_store, monkeypatch):
    arguments, history, attachments = await runtime_case(operation_store)
    running = arguments["inventory"].running
    observed = []
    original = runtime_owner._restore_live

    def delayed(**kwargs):
        observed.append(kwargs["monotonic_before_read"])
        time.sleep(0.025)
        return original(**kwargs)

    monkeypatch.setattr(runtime_owner, "_restore_live", delayed)
    try:
        runtime = await restore_continuation_runtime(**arguments)
        assert runtime.live.turn_evidence is runtime.graph.evidence
        assert runtime.live.recorder is runtime.graph.recorder
        assert runtime.live.llm_messages is runtime.graph.messages
        assert runtime.live.llm_messages[-1] is runtime.live.llm_messages[-3]
        assert runtime.live.llm_messages[-2] is not runtime.live.llm_messages[-3]
        assert any(PROMPT_GUARD_TRUST_KEY in message for message in runtime.live.llm_messages)
        assert runtime.live.discovery_cache == {} and runtime.live.runtime_preflight_cache == {}
        assert runtime.live.last_runtime_preflight is None
        assert runtime.live.continuation_cache_history == history and history.last_preflight_json is not None
        assert runtime.attachments == attachments and len(attachments.reads) == 1
        assert runtime.bindings.deadline <= observed[0] + runtime.source.timing.hard_remaining_seconds
        assert runtime.bindings.actor == "composer-web:user-alice"
        assert runtime.bindings.session_operation_context is running.session_operation_context
        assert runtime.bindings.completion_gates == runtime.source.original.completion_gates
        assert runtime.bindings.mutation_ceiling == arguments["selected"].stack.mutation_ceiling.materialize()
        assert (
            runtime.live.composition_turns_used,
            runtime.live.discovery_turns_used,
            runtime.live.advisor_calls_used,
            runtime.live.repair_turns_used,
            runtime.live.advisor_checkpoint_passes_used,
        ) == (2, 3, 4, 1, 2)
        runtime.live.composition_turns_used += 1
        runtime.live.discovery_turns_used += 2
        runtime.live.recorder.record_withheld_reply("repair_gate_superseded", "Actual later runtime record")
        captured = await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        frame = captured.stack.frames[0]
        assert (frame.composition_slice, frame.composition_total, frame.discovery_slice, frame.discovery_total) == (3, 8, 5, 13)
        assert frame.attachment_admissions == arguments["selected"].stack.frames[0].attachment_admissions
        assert captured.stack.effect_frontier == arguments["selected"].stack.effect_frontier
        assert captured.stack.mutation_ceiling == arguments["selected"].stack.mutation_ceiling
        cache = decode_continuation_caches(next(chunk for chunk in captured.chunks if chunk.reference == frame.caches))
        assert cache.capture_count == 2 and cache.discovery == history.discovery and cache.preflight == history.preflight
        assert cache.previous_last_preflight_json == (history.last_preflight_json,) and cache.last_preflight_json is None
        following = await install_capture(operation_store, arguments, captured)
        restored = await restore_continuation_runtime(**following)
        assert restored.live.recorder.withheld_replies[-1].content == "Actual later runtime record"
        assert restored.live.continuation_cache_history == cache
        assert restored.live.discovery_cache == {} and restored.live.runtime_preflight_cache == {}
        assert restored.live.last_runtime_preflight is None and restored.attachments == attachments
        # A different actual owner may advance inspection history between live
        # phase captures. Keep that selected ref, rather than the older decode.
        raw = b"A later completed original inspection"
        later_inspection = inspect_attachment_original(
            session_id=str(operation_store[4]),
            blob_id=str(uuid4()),
            verified_hash=sha256(raw).hexdigest(),
            raw=raw,
            policy=AttachmentInspectionPolicy(4096),
        )
        advanced = append_attachment_inspections(
            attachments, effect_key="runtime-fixture:later-original-inspection", inspections=(later_inspection,)
        )
        advanced_chunk = encode_attachment_history(advanced)
        latest_root = following["selected"].stack.model_copy(
            update={
                "frames": (following["selected"].stack.frames[0].model_copy(update={"attachment_admissions": advanced_chunk.reference}),)
            }
        )
        history_source = runtime_owner.CapturedContinuationRuntime(
            following["selected"], latest_root, runtime_owner._selected_chunks(latest_root, following["selected"].chunks, (advanced_chunk,))
        )
        following = await install_capture(operation_store, following, history_source)
        second = await capture_continuation_runtime_owners(runtime, selected=following["selected"], inventory=following["inventory"])
        second_frame = second.stack.frames[0]
        assert (
            second_frame.composition_slice,
            second_frame.composition_total,
            second_frame.discovery_slice,
            second_frame.discovery_total,
        ) == (3, 8, 5, 13)
        assert second_frame.attachment_admissions == advanced_chunk.reference != frame.attachment_admissions
        selected_history = next(chunk for chunk in second.chunks if chunk.reference == second_frame.attachment_admissions)
        assert (
            decode_attachment_history(selected_history, session_id=str(operation_store[4]), operation_id=running.claim.operation_id)
            == advanced
        )
        again = decode_continuation_caches(next(chunk for chunk in second.chunks if chunk.reference == second_frame.caches))
        assert again.capture_count == 3 and again.discovery == cache.discovery and again.preflight == cache.preflight
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ("auto_commit", "explicit_approve"))
async def test_runtime_context_rechecks_current_preference_without_renewing_original_ceiling(operation_store, initial):
    with operation_store[0].begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(operation_store[4])).values(trust_mode=initial))
    arguments, _history, _attachments = await runtime_case(operation_store)
    running = arguments["inventory"].running
    try:
        runtime = await restore_continuation_runtime(**arguments)
        first = await build_runtime_tool_batch_context(runtime, cancellation_requested=asyncio.Event())
        assert first.review_required is (initial == "explicit_approve") and first.mutation_ceiling.initial_trust_mode == initial
        assert first.recorder is runtime.graph.recorder and first.turn_evidence is runtime.graph.evidence
        assert (
            first.discovery_cache is runtime.live.discovery_cache and first.runtime_preflight_cache is runtime.live.runtime_preflight_cache
        )
        following = "explicit_approve" if initial == "auto_commit" else "auto_commit"
        await operation_store[3].update_composer_preferences(
            operation_store[4], trust_mode=following, density_default=first.turn_preferences.density_default, actor="alice"
        )
        second = await build_runtime_tool_batch_context(runtime, cancellation_requested=asyncio.Event())
        assert second.review_required is True and second.mutation_ceiling is first.mutation_ceiling
        assert second.deadline == first.deadline == runtime.bindings.deadline
        assert second.session_operation_context is running.session_operation_context
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("selection", "cache_sql", "counter"))
async def test_runtime_refuses_source_drift_and_counter_reset_after_actual_positive(operation_store, change):
    arguments, _history, _attachments = await runtime_case(operation_store)
    running = arguments["inventory"].running
    try:
        runtime = await restore_continuation_runtime(**arguments)
        if change == "selection":
            original = arguments["selected"]
            changed = replace(
                original,
                stack=original.stack.model_copy(
                    update={
                        "logical": original.stack.logical.model_copy(update={"request_hash": "same-checkpoint-valid-different-request"})
                    }
                ),
            )
            before = recovery_rows(operation_store[0])
            with pytest.raises(AuditIntegrityError):
                await restore_continuation_runtime(**{**arguments, "selected": changed})
        elif change == "cache_sql":
            frame = arguments["selected"].stack.frames[0]
            payload = b"\xff"
            with operation_store[0].begin() as conn:
                result = conn.execute(
                    update(chunks)
                    .where(
                        chunks.c.session_id == str(operation_store[4]),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == f"value:{frame.caches.codec}:{frame.caches.digest}",
                    )
                    .values(payload=payload, sha256=sha256(payload).hexdigest(), byte_length=1)
                )
                assert result.rowcount == 1
            before = recovery_rows(operation_store[0])
            with pytest.raises(AuditIntegrityError):
                await restore_continuation_runtime(**arguments)
        else:
            runtime.live.composition_turns_used -= 1
            before = recovery_rows(operation_store[0])
            with pytest.raises(AuditIntegrityError, match="cannot reset"):
                await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        assert recovery_rows(operation_store[0]) == before
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_real_committed_provider_cut_recovers_into_fresh_current_runtime(operation_store, tmp_path, monkeypatch):
    kwargs, _produced, _recorder, sdk, result = await publish_actual(operation_store, tmp_path, monkeypatch)
    assert result is not None and len(sdk) == 1
    arguments, old_cache, attachments = await runtime_case(operation_store, provider_input=(kwargs, result))
    original = arguments["inventory"].running
    restored = await restore_continuation_runtime(**arguments)
    arguments["required_work"].coordinator.assert_completed()
    operation_store[1].release(original.session_operation_context)
    current = new_owners(operation_store)
    _engine, repository, authority, sessions, sid = current
    assert (
        repository.recover_composer_segment(
            session_id=sid,
            operation_id=original.claim.operation_id,
            expected_generation=original.claim.ownership_generation,
            actor_user_id="alice",
            auth_provider_type="local",
        )
        is True
    )
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim, owner_instance_id=authority._owner_instance_id, lease_seconds=30, auth_provider_type="local"
    )
    running = ComposerOperationRunning(claim=claim, session_operation_context=context)
    try:
        selected, inventory = await current_selected(current, running)
        service, _actors = current_service(current)
        work = inventory_work(running)
        fresh = await restore_continuation_runtime(
            service=service,
            authority=authority,
            selected=selected,
            inventory=inventory,
            required_work=work,
            provider_owner=ProviderInvocationOwner(service=sessions, required_work=work),
        )
        assert fresh.bindings.session_operation_context is context and context.fence != original.session_operation_context.fence
        assert fresh.source.timing.generation == running.claim.ownership_generation
        assert fresh.live.continuation_cache_history == old_cache and fresh.attachments == attachments
        assert fresh.live.discovery_cache == {} and fresh.live.runtime_preflight_cache == {} and fresh.live.last_runtime_preflight is None
        assert fresh.bindings.mutation_ceiling == restored.bindings.mutation_ceiling
        assert fresh.live.composition_turns_used == restored.live.composition_turns_used == 2
        assert fresh.live.discovery_turns_used == restored.live.discovery_turns_used == 3
        assert fresh.live.recorder.llm_calls == restored.live.recorder.llm_calls and len(sdk) == 1
        assert fresh.source.evidence.physical_operation_id == restored.source.evidence.physical_operation_id
        assert fresh.graph.evidence.operation_id == context.fence.operation_id
        work.coordinator.assert_completed()
    finally:
        repository.release(context)


@pytest.mark.asyncio
async def test_runtime_current_prepared_batch_is_the_same_complete_evidence_graph(operation_store, monkeypatch):
    from elspeth.contracts.composer_mutation import ComposerMutationRevocation
    from elspeth.web.composer.continuation import ToolBatchFrame
    from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot
    from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data
    from elspeth.web.composer.continuation_codec import encode_continuation_response
    from elspeth.web.composer.continuation_evidence import encode_turn_evidence
    from elspeth.web.composer.continuation_evidence_graph import capture_turn_evidence
    from tests.unit.web.composer.test_continuation_message_family_builders import completion_with_native

    arguments, _history, _attachments = await runtime_case(operation_store)
    running = arguments["inventory"].running
    try:
        original = await restore_continuation_runtime(**arguments)
        # Actual prepared DATA boundary with complete selected response and the
        # same request/actor/creator. No provider execution is inferred here.
        with monkeypatch.context() as clock:
            batch, accumulator, calls, dialect = prepared_cut(clock, dialect=arguments["service"]._planner_dialect)
            batch.current_call.audit = replace(batch.current_call.audit, actor="composer-web:user-alice")
            batch.current_call.original_creator_id = running.session_operation_context.fence.operation_id
            batch.current_call.original_creator_epoch = running.session_operation_context.fence.operation_epoch
            batch.pre_state_id = None
            accumulator.authority_revocation = ComposerMutationRevocation(
                arguments["selected"].stack.mutation_ceiling.materialize(), "explicit_approve"
            )
            prepared_json = capture((batch, accumulator, calls, dialect))
        data = decode_prepared_batch_data(prepared_json)
        for authoring in data.batch.private_authorings_by_call.values():
            original.live.recorder.record_private_tool_authoring(authoring)
        snapshot = capture_turn_evidence(
            original.live.turn_evidence,
            original.live.llm_messages,
            logical=arguments["selected"].stack.logical,
            recorder=original.live.recorder,
            drained=original.drained,
            env_ref_names=arguments["inventory"].names,
            data=data,
            materialization=original.materialization,
        )
        evidence = encode_turn_evidence(snapshot)
        audit = encode_recorder_snapshot(original.live.recorder.snapshot_for_continuation(drained=original.drained))
        prepared = encode_private_chunk(codec="composer.tool-batch.v1", canonical=prepared_json)
        response = encode_continuation_response(completion_with_native(calls=data.calls))
        state = encode_private_chunk(codec="composer.state.v1", canonical=canonical_json(data.batch.state.to_dict()))
        compose = (
            arguments["selected"]
            .stack.frames[0]
            .model_copy(
                update={
                    "phase": "response_retained",
                    "pending_response": response.reference,
                    "state": state.reference,
                    "evidence": evidence.reference,
                    "audit_buffer": audit.reference,
                }
            )
        )
        top = ToolBatchFrame(
            phase="tool_ready",
            response=response.reference,
            accumulator=prepared.reference,
            next_tool_index=data.batch.next_tool_index,
            settled_effect_ids=(),
        )
        root = arguments["selected"].stack.model_copy(update={"frames": (compose, top)})
        projected = runtime_owner.CapturedContinuationRuntime(
            arguments["selected"],
            root,
            runtime_owner._selected_chunks(root, arguments["selected"].chunks, (evidence, audit, prepared, response, state)),
        )
        current = await install_capture(operation_store, arguments, projected)
        restored = await restore_continuation_runtime(**current)
        shared = restored.graph.batch
        assert shared is not None and shared.batch.current_call.audit is None
        assert restored.graph.selected_audit is not None and restored.graph.selected_dialect is dialect
        assert restored.live.state is shared.batch.state is shared.accumulator.state
        assert shared.batch.current_call.pre_state is restored.live.state
        assert shared.batch.tool_outcomes[0].response is shared.batch.tool_outcomes[1].response
        retained_recorder = restored.live.recorder.snapshot_for_continuation(drained=restored.drained)
        for authoring in shared.batch.private_authorings_by_call.values():
            assert any(authoring is item for item in retained_recorder.private_authorings)
        assert shared.accumulator.authority_revocation == data.accumulator.authority_revocation
        assert shared.accumulator.advisor_calls_used == 2 and shared.batch.advisor_calls_used == 4
        assert restored.live.last_runtime_preflight is None and restored.live.runtime_preflight_cache == {}
        captured = await capture_continuation_runtime_owners(restored, selected=current["selected"], inventory=current["inventory"])
        selected_batch = next(chunk for chunk in captured.chunks if chunk.reference == captured.stack.frames[-1].accumulator)
        assert selected_batch.canonical == prepared_json and selected_batch.reference.codec == "composer.tool-batch.v1"
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ("error", "started_cancellation"))
async def test_runtime_retains_original_worker_failure_and_joins_started_cancellation(operation_store, monkeypatch, outcome):
    arguments, _history, _attachments = await runtime_case(operation_store)
    running = arguments["inventory"].running
    try:
        await restore_continuation_runtime(**arguments)
        before = recovery_rows(operation_store[0])
        actual = arguments["authority"].read_continuation_runtime
        original = ValueError("actual runtime source failure")
        entered, release = Event(), Event()
        completed = []

        def controlled(*args, **kwargs):
            if outcome == "error":
                raise original
            source = actual(*args, **kwargs)
            completed.append(source)
            entered.set()
            assert release.wait(5)
            return source

        monkeypatch.setattr(arguments["authority"], "read_continuation_runtime", controlled)
        if outcome == "error":
            with pytest.raises(ValueError) as caught:
                await restore_continuation_runtime(**arguments)
            assert caught.value is original
        else:
            task = asyncio.create_task(restore_continuation_runtime(**arguments))
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel("actual started runtime cancellation")
                await asyncio.sleep(0)
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError, match="actual started runtime cancellation"):
                await task
            assert len(completed) == 1
        assert recovery_rows(operation_store[0]) == before
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
