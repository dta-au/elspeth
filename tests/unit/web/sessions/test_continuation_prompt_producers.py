"""Actual selected SQL inputs and current message-producer reconstruction.

The fixture installs real bootstrap and iteration owners at the existing stored
input boundary. It does not activate a worker or replace the provider phase.
"""

import asyncio
import json
from dataclasses import FrozenInstanceError
from threading import Event
from time import monotonic

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import PROMPT_GUARD_TRUST_KEY, detach_prompt_guard_evidence
from elspeth.web.composer import continuation_prompt_producers as producers
from elspeth.web.composer._compose_loop_carriers import _AdvisorReviewState, _CallModelOutcome
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_attachment_history import empty_attachment_history, encode_attachment_history
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors, encode_recorder_snapshot
from elspeth.web.composer.continuation_bootstrap import capture_continuation_bootstrap, encode_continuation_bootstrap
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence import admit_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_graph import capture_turn_evidence
from elspeth.web.composer.continuation_iteration import (
    capture_continuation_iteration,
    capture_continuation_producer_inputs,
    encode_continuation_iteration,
)
from elspeth.web.composer.continuation_message_shelf import encode_message_shelf
from elspeth.web.composer.continuation_prompt_recipes import PluginSchemaMessageRecipe
from elspeth.web.composer.continuation_selection import SelectedContinuationState, load_selected_continuation
from elspeth.web.composer.graph_recovery import GraphRecoveryContext
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_gateway import ProviderGateway, composer_loop_tool_definitions
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.schema_disclosure import SchemaDisclosureTracker
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.config import WebSettings
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, WebPluginPolicy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, private_chunk_key, put_chunk
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages_table
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import message_ingress_receipts_table as ingress_table
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.schemas import RecomposeRequest, SendMessageRequest
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_inventory import inventory_advisor, inventory_work
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.composer.test_turn_effects import _state
from tests.unit.web.coordination import test_composer_operation_authority
from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation

operation_store = test_composer_operation_authority.operation_store
REQUEST = "Preserve café and exact context"


def current_service(store, *, secret_service=None):
    engine, _repository, _authority, sessions, _sid = store
    settings = WebSettings(
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=100,
        shareable_link_signing_key=b"\x00" * 32,
        data_dir=sessions._data_dir,
    )
    catalog = create_catalog_service()
    available = frozenset({PluginId("source", "csv")})
    policy = WebPluginPolicy.create(
        required=available, configured_optional=frozenset(), preferences=(), control_modes=(), plugin_code_identities=()
    )
    profiles = OperatorProfileRegistry(policy=policy, settings=RuntimeWebPluginConfig.from_settings(settings))
    actors = []

    def current_snapshot(actor):
        actors.append(actor)
        return PluginAvailabilitySnapshot.create(
            policy_hash=policy.policy_hash,
            principal_scope=f"local:{actor}",
            available=available,
            unavailable=(),
            selected=(),
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="current-message-test",
        )

    service = ComposerServiceImpl(
        catalog,
        settings,
        sessions_service=sessions,
        session_engine=engine,
        plugin_snapshot_factory=current_snapshot,
        operator_profile_registry=profiles,
        secret_service=secret_service,
    )
    return service, actors


async def producer_case(store, *, secret_service=None, retain_ingress=True, admitted_input=None):
    engine, repository, authority, sessions, sid = store
    if admitted_input is None:
        record, _seed, _action = admit_continuation(store)
        running = start_continuation(repository, authority)
    else:
        record, running, records = admitted_input
    head = authority.load_continuation_snapshot(running)
    root = decode_continuation_snapshot(head.payload_json)
    owner = TurnEvidence(
        session_id=str(sid),
        session_operation_context=running.session_operation_context,
        request_id=root.logical.original_user_message_id,
        request=REQUEST,
        initial_state_id=None,
        initial_state=_state("Untitled Pipeline"),
        actor_user_id="alice",
    )
    messages = [{"role": "user", "content": REQUEST}]
    owner.transcript.begin_messages(messages)
    # Real nonempty evidence, not an emptied reconstruction fixture.
    owner.transcript.observe_messages(({"role": "user", "content": "Keep this observed middle amendment"},))
    recorder = BufferingRecorder()
    recorder.record_withheld_reply("repair_gate_superseded", "A retained unpublished reply")
    if admitted_input is None:
        with locked_session_transaction(engine, str(sid)) as conn:
            conn.execute(
                insert(messages_table).values(
                    id=owner.request_id,
                    session_id=str(sid),
                    role="user",
                    content=REQUEST,
                    sequence_no=1,
                    writer_principal="route_user_message",
                    created_at=database_now(conn),
                )
            )
            # The immutable receipt also references the operation's bound user row.
            # Install that existing fixture fact on this same connection first,
            # matching the real ingress owner's parent-bind-before-receipt order.
            conn.execute(
                update(jobs)
                .where(jobs.c.session_id == str(sid), jobs.c.operation_id == record.operation_id)
                .values(user_message_id=owner.request_id, ingress_phase="committed")
            )
            if retain_ingress:
                conn.execute(
                    insert(ingress_table).values(
                        session_id=str(sid),
                        operation_id=record.operation_id,
                        user_message_id=owner.request_id,
                        requested_state_id=None,
                        created_at=database_now(conn),
                    )
                )
            # Actual add_message_with_transcript returns the current operation id
            # only on its just-inserted human row. The missing-receipt case below
            # deliberately installs an invalid stored cut and must fail at read.
            records = tuple(
                chat_message_record_from_row(row, operation_id=record.operation_id if row.id == owner.request_id else None)
                for row in conn.execute(
                    select(messages_table).where(messages_table.c.session_id == str(sid)).order_by(messages_table.c.sequence_no)
                )
            )
    admitted = capture_turn_evidence(
        owner, messages, logical=root.logical, recorder=recorder, drained=RecorderDrainCursors(), env_ref_names=frozenset()
    )
    evidence = encode_turn_evidence(admitted)
    snapshot = admitted.snapshot
    state = encode_private_chunk(codec="composer.state.v1", canonical=canonical_json(owner.effects.initial_state.to_dict()))
    transcript = encode_message_shelf(snapshot.transcript)
    audit = encode_recorder_snapshot(recorder.snapshot_for_continuation(drained=RecorderDrainCursors()))
    # Cache input is replaced by the complete runtime fixture before its use.
    # Attachment history is read by current message/evidence admission already,
    # so install the real request-bound empty owner at this initial root.
    opaque = {
        "caches": encode_private_chunk(codec="composer.messages.v1", canonical=canonical_json({"unconsumed_test_input": "caches"})),
        "attachments": encode_attachment_history(
            empty_attachment_history(session_id=root.logical.session_id, operation_id=root.logical.operation_id)
        ),
    }
    service, actors = current_service(store, secret_service=secret_service)
    plugin_snapshot, _catalog = service._policy_context.build("alice")
    # Admission intentionally returns metadata without the request body. Read
    # the existing full turn-input owner rather than reconstructing a request.
    request_record = await asyncio.to_thread(authority.get_for_turn, session_id=sid, operation_id=record.operation_id)
    assert request_record is not None and request_record.request_json is not None
    request_type = SendMessageRequest if root.logical.kind == "compose_message" else RecomposeRequest
    bootstrap = capture_continuation_bootstrap(
        logical=root.logical,
        actor_user_id="alice",
        request=request_type.model_validate_json(request_record.request_json),
        records=records,
        initial_state=owner.effects.initial_state,
        recovery_context=GraphRecoveryContext(None, 0, None, 0, False, ()),
        completion_gates=None,
        retained_context=None,
        session_title="Untitled",
        initial_schema_disclosure=(),
    )
    opaque["bootstrap"] = encode_continuation_bootstrap(bootstrap)
    iteration = capture_continuation_iteration(
        producer_inputs=capture_continuation_producer_inputs((), env_ref_names=frozenset()),
        last_validation=None,
        advisor_review=_AdvisorReviewState(),
        failed_turn=None,
        anti_anchor=AntiAnchorTracker().snapshot_for_continuation(),
        disclosure=SchemaDisclosureTracker().snapshot_for_continuation(
            session_id=str(sid), catalog_hash="current-catalog", policy_hash=plugin_snapshot.policy_hash
        ),
    )
    opaque["iteration"] = encode_continuation_iteration(iteration)
    current_messages = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json(
            {"messages_json": snapshot.transcript.messages_json, "current_indexes": snapshot.transcript.current_indexes}
        ),
    )
    frame = ComposeFrame(
        phase="action_ready",
        bootstrap=opaque["bootstrap"].reference,
        iteration=opaque["iteration"].reference,
        messages=current_messages.reference,
        tools=None,
        initial_state=state.reference,
        state=state.reference,
        evidence=evidence.reference,
        audit_buffer=audit.reference,
        transcript=transcript.reference,
        caches=opaque["caches"].reference,
        attachment_admissions=opaque["attachments"].reference,
        initial_version=1,
        current_state_id=None,
        slice_sequence=running.claim.segment_sequence,
        composition_slice=0,
        discovery_slice=0,
        composition_total=0,
        discovery_total=0,
        advisor_calls=0,
        repair_turns=0,
        advisor_checkpoint_passes=0,
        mutation_success_seen=False,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        advisor_repair_context_introduced=False,
        pending_advisor_elision_indices=(),
        pending_response=None,
    )
    root = root.model_copy(update={"frames": (frame,)})
    with locked_session_transaction(engine, str(sid)) as conn:
        # Exact committed-ingress input fixture. This does not test or claim the
        # missing v2 initializer producer and does not bypass a current read guard.

        for chunk in (evidence, state, transcript, audit, current_messages, *opaque.values()):
            put_chunk(
                conn,
                session_id=str(sid),
                operation_id=record.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode("utf-8"),
                codec=chunk.reference.codec,
            )
        stored = append_complete_snapshot(
            conn,
            session_id=str(sid),
            operation_id=record.operation_id,
            expected_checkpoint_id=head.checkpoint_id,
            expected_cursor=head.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"pre-P4 evidence input"}'),),
            handoff=False,
        )
    work = inventory_work(running)
    selected = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(sessions, secret_service=secret_service), authority=authority, selected=selected, required_work=work
    )
    work.coordinator.assert_completed()
    actors.clear()
    kwargs = {
        "service": service,
        "authority": authority,
        "selected": selected,
        "inventory": inventory,
        "required_work": inventory_work(running),
    }
    return kwargs, owner, recorder, snapshot, iteration, actors


def detached(messages):
    return detach_prompt_guard_evidence(messages).messages


async def save_produced_shelf(store, kwargs, snapshot, iteration, transcript, messages, owner, *, transform_shelf=None, recorder=None):
    """Install actual capture outputs using the existing locked root/CAS owner."""
    engine, _repository, authority, sessions, sid = store
    shelf = transcript.snapshot_for_continuation(messages, materialization=owner)
    if transform_shelf is not None:
        shelf = transform_shelf(shelf)
    audit = encode_recorder_snapshot(recorder.snapshot_for_continuation(drained=RecorderDrainCursors())) if recorder is not None else None
    changed = snapshot.model_copy(update={"transcript": shelf, **({"recorder": audit.reference} if audit is not None else {})})
    evidence = encode_turn_evidence(
        admit_turn_evidence(
            changed,
            env_ref_names=kwargs["inventory"].names,
            source_chunks=((audit,) if audit is not None else ()) + kwargs["selected"].chunks,
        ),
        env_ref_names=kwargs["inventory"].names,
    )
    transcript_chunk = encode_message_shelf(shelf)
    message_chunk = encode_private_chunk(
        codec="composer.messages.v1",
        canonical=canonical_json({"messages_json": shelf.messages_json, "current_indexes": shelf.current_indexes}),
    )
    iteration = iteration.model_copy(update={"producer_inputs": owner.snapshot_inputs()})
    iteration_chunk = encode_continuation_iteration(iteration)
    selected = kwargs["selected"]
    (frame,) = selected.stack.frames
    root = selected.stack.model_copy(
        update={
            "frames": (
                frame.model_copy(
                    update={
                        "evidence": evidence.reference,
                        "transcript": transcript_chunk.reference,
                        "messages": message_chunk.reference,
                        "iteration": iteration_chunk.reference,
                        **({"audit_buffer": audit.reference} if audit is not None else {}),
                    }
                ),
            )
        }
    )
    running = kwargs["inventory"].running
    with locked_session_transaction(engine, str(sid)) as conn:
        for chunk in (evidence, transcript_chunk, message_chunk, iteration_chunk, *((audit,) if audit is not None else ())):
            put_chunk(
                conn,
                session_id=str(sid),
                operation_id=running.claim.operation_id,
                key=private_chunk_key(chunk),
                payload=chunk.canonical.encode(),
                codec=chunk.reference.codec,
            )
        stored = append_complete_snapshot(
            conn,
            session_id=str(sid),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(root),
            event_batch=(ContinuationEvent("checkpoint", '{"test_scope":"actual producer capture"}'),),
            handoff=False,
        )
    work = inventory_work(running)
    current = await load_selected_continuation(authority=authority, running=running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=inventory_advisor(sessions), authority=authority, selected=current, required_work=work
    )
    work.coordinator.assert_completed()
    return {**kwargs, "selected": current, "inventory": inventory, "required_work": inventory_work(running)}, shelf


@pytest.mark.asyncio
async def test_initial_and_fixed_actual_producers_restore_order_aliases_and_current_policy(operation_store):
    kwargs, evidence, _recorder, snapshot, iteration, actors = await producer_case(operation_store)
    produced = await producers.load_current_message_producers(**kwargs, initial=True)
    owner = produced.producers
    assert type(produced.messages) is tuple
    assert all(owner.capture_message(message)[1] is not None for message in produced.messages)
    with pytest.raises(FrozenInstanceError):
        produced.messages = ()
    initial = list(produced.messages)
    current_snapshot, catalog = kwargs["service"]._policy_context.build("alice")
    warm = await asyncio.to_thread(
        kwargs["service"]._build_messages,
        chat_history=list(owner.inputs.values.history),
        state=owner.inputs.values.initial_state,
        user_message=REQUEST,
        session_id=str(operation_store[4]),
        user_id="alice",
        plugin_snapshot=current_snapshot,
        policy_catalog=catalog,
        session_operation_context=kwargs["inventory"].running.session_operation_context,
    )
    assert detached(initial) == detached(warm)
    assert any(PROMPT_GUARD_TRUST_KEY in message for message in initial)
    assert actors and set(actors) == {"alice"}
    recovery = owner.recovery_instruction()
    reply = owner.reply_instruction()
    # The same object in current/excluded/observed remains the same object after
    # capture. Equal but distinct plain messages remain distinct shelf entries.
    distinct = {"role": "user", "content": "equal prose"}
    equal = dict(distinct)
    transcript = evidence.transcript
    transcript.begin_messages(initial)
    transcript.observe_messages((recovery, reply, distinct, equal))
    messages = [*initial, recovery, reply, distinct, equal, distinct]
    next_kwargs, shelf = await save_produced_shelf(operation_store, kwargs, snapshot, iteration, transcript, messages, owner)
    assert shelf.current_indexes[-1] == shelf.current_indexes[-3]
    assert shelf.current_indexes[-2] != shelf.current_indexes[-3]
    cold = await producers.load_current_message_producers(**next_kwargs, retained_shelf=shelf)
    assert type(cold.messages) is tuple
    assert cold.messages[-1] is cold.messages[-3] and cold.messages[-2] is not cold.messages[-3]
    assert tuple(cold.producers.capture_message(message) for message in cold.messages) == tuple(
        (shelf.messages_json[index], shelf.producer_recipes[index]) for index in shelf.current_indexes
    )
    assert detached(cold.messages) == detached(messages)
    # The transcript owner reconstructs the complete current/excluded/observed
    # partition from the interned shelf, preserving actual reference identities.
    restored, current = await asyncio.to_thread(
        AdvisorTranscriptOwner.for_continuation,
        shelf,
        session_id=str(operation_store[4]),
        actor_user_id="alice",
        request_id=next_kwargs["selected"].stack.logical.original_user_message_id,
        request=REQUEST,
        materialization=cold.producers,
    )
    assert current[-1] is current[-3] and current[-2] is not current[-3]
    assert any(current[0] is item for item in restored._excluded)
    assert any(current[-3] is item.message for item in restored._observed)
    assert restored.snapshot_for_continuation(current, materialization=cold.producers) == shelf
    kwargs["required_work"].coordinator.assert_completed()
    next_kwargs["required_work"].coordinator.assert_completed()


async def dispatch_schema_twice(kwargs, owner, recorder, *, store, tmp_path, monkeypatch):
    service = kwargs["service"]
    snapshot, catalog = service._policy_context.build("alice")
    state = owner.inputs.values.initial_state
    calls = tuple(
        {
            "id": identity,
            "name": "get_plugin_schema",
            "arguments": encode_semantic_arguments("get_plugin_schema", service._planner_dialect, {"plugin_type": "source", "name": "csv"}),
        }
        for identity in ("schema-first", "schema-cache")
    )
    settings, requests = _required_guard_settings(tmp_path, monkeypatch)
    service._provider_gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
    sdk_calls = []

    async def complete(**values):
        _assert_required_guard_admitted(
            store[0], kwargs["inventory"].running.session_operation_context, requests, role="composer", user_prompt=REQUEST
        )
        assert not sdk_calls, "The schema component supplies one actual primary SDK reply"
        sdk_calls.append(values)
        return ModelResponse(
            model="test/model",
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call["id"],
                                "type": "function",
                                "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])},
                            }
                            for call in calls
                        ],
                    },
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", complete)
    provider = ProviderInvocationOwner(service=store[3], required_work=kwargs["required_work"])
    deadline = monotonic() + 30
    with prompt_guard_user_scope(REQUEST):
        outcome = await service._call_llm_outcome_before_deadline(
            owner.initial_messages(),
            composer_loop_tool_definitions(service._planner_dialect),
            state,
            state.version,
            deadline,
            recorder,
            composition_turns_used=0,
            discovery_turns_used=0,
            failed_turn=None,
            provider_owner=provider,
        )
    assert type(outcome) is _CallModelOutcome
    assert len(sdk_calls) == 1 and outcome.audit_call is recorder.llm_calls[-1]
    original_call = outcome.audit_call
    assert original_call is not None and original_call.status.name == "SUCCESS"
    with store[0].connect() as conn:
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == original_call.call_id)).one()
        ledger = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
    assert attempt.settled_at is not None and attempt.ledger_entry_id == ledger.entry_id
    assert attempt.source == ledger.source == "composer"
    assert attempt.identity_id == ledger.identity_id == "alice"
    assert attempt.session_id == ledger.session_id == str(store[4])
    assert attempt.operation_id == kwargs["inventory"].running.session_operation_context.fence.operation_id
    assert ledger.prompt_tokens == original_call.prompt_tokens == 7
    assert ledger.completion_tokens == original_call.completion_tokens == 3
    messages = []
    cache = {}
    result, advisor_used = await service._dispatch_tool_batch(
        call_model=outcome,
        mutation_ceiling=kwargs["selected"].stack.mutation_ceiling.materialize(),
        state=state,
        last_validation=None,
        last_runtime_preflight=None,
        llm_messages=messages,
        recorder=recorder,
        anti_anchor=AntiAnchorTracker(),
        discovery_cache=cache,
        runtime_preflight_cache={},
        session_id=owner.inputs.bootstrap.logical.session_id,
        session_operation_context=kwargs["inventory"].running.session_operation_context,
        user_id="alice",
        user_message_id=owner.inputs.bootstrap.logical.original_user_message_id,
        user_message_content=REQUEST,
        current_state_id=None,
        actor="planner",
        initial_version=state.version,
        deadline=deadline,
        progress=None,
        session_scope=owner.inputs.bootstrap.logical.session_id,
        advisor_calls_used=0,
        cancellation_requested=asyncio.Event(),
        plugin_snapshot=snapshot,
        policy_catalog=catalog,
        composition_turns_used=0,
        discovery_turns_used=0,
        failed_turn=None,
        message_producers=owner,
        provider_owner=provider,
    )
    assert result.state is state and result.turn_has_discovery and not result.turn_has_mutation
    assert advisor_used == 0 and result.plugin_crash is None
    assert len(cache) == 1 and len(result.tool_outcomes) == 2
    return result, messages


@pytest.mark.asyncio
async def test_real_fresh_and_cache_schema_paths_register_full_current_rebuild(operation_store, tmp_path, monkeypatch):
    with operation_store[0].begin() as conn:
        grant_test_pipeline_user(conn, identity_id="alice")
    kwargs, _evidence, recorder, snapshot, iteration, _actors = await producer_case(operation_store)
    produced = await producers.load_current_message_producers(**kwargs)
    result, messages = await dispatch_schema_twice(
        kwargs, produced.producers, recorder, store=operation_store, tmp_path=tmp_path, monkeypatch=monkeypatch
    )
    tools = [message for message in messages if message["role"] == "tool"]
    assert [entry.cache_hit for entry in result.private_invocations] == [False, True]
    retained = produced.producers.snapshot_inputs()
    assert len(retained.schema_inputs) == 2 and retained.schema_inputs[0].state_index == retained.schema_inputs[1].state_index
    assert [produced.producers.capture_message(message)[1].input_index for message in tools] == [0, 1]
    transcript = AdvisorTranscriptOwner(request=REQUEST)
    transcript.observe_messages(tools)
    next_kwargs, shelf = await save_produced_shelf(
        operation_store, kwargs, snapshot, iteration, transcript, tools, produced.producers, recorder=recorder
    )
    cold = await producers.load_current_message_producers(**next_kwargs, retained_shelf=shelf)
    assert detached(cold.messages) == detached(tools)
    assert all(isinstance(recipe, PluginSchemaMessageRecipe) for recipe in shelf.producer_recipes)
    assert cold.producers.snapshot_inputs() == retained
    kwargs["required_work"].coordinator.assert_completed()
    next_kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_changed_root_and_unregistered_guarded_wire_refuse_after_normal_factory(operation_store):
    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(operation_store)
    actual = await producers.load_current_message_producers(**kwargs, initial=True)
    assert actual.messages
    forged = dict(actual.messages[0])
    with pytest.raises(AuditIntegrityError, match="no actual registered"):
        actual.producers.capture_message(forged)
    changed = dict(actual.messages[0])
    actual.messages[0]["content"] += " changed after actual producer"
    with pytest.raises(AuditIntegrityError, match="changed its complete"):
        actual.producers.capture_message(actual.messages[0])
    actual.messages[0].update(changed)
    selected = kwargs["selected"]
    root = selected.stack.model_copy(update={"logical": selected.stack.logical.model_copy(update={"request_hash": "f" * 64})})
    foreign = SelectedContinuationState(selected.checkpoint_id, root, selected.chunks)
    with pytest.raises(AuditIntegrityError, match="complete selected root"):
        await producers.load_current_message_producers(**{**kwargs, "selected": foreign})
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_changed_original_row_prefix_refuses_before_producing_instruction(operation_store):
    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(operation_store)
    assert (await producers.load_current_message_producers(**kwargs, initial=True)).messages
    # The complete original raw channel is checked independently of protected
    # content; the actual mutable column changes without disabling a trigger.
    with operation_store[0].begin() as conn:
        conn.execute(
            update(messages_table)
            .where(messages_table.c.id == kwargs["selected"].stack.logical.original_user_message_id)
            .values(raw_content="different original raw channel")
        )
    with pytest.raises(AuditIntegrityError, match="complete original ordered transcript prefix"):
        await producers.load_current_message_producers(**kwargs, initial=True)
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_current_cancellation_during_actual_initial_builder_refuses_projection(operation_store, monkeypatch):
    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(operation_store)
    assert (await producers.load_current_message_producers(**kwargs, initial=True)).messages
    actual = producers.build_service_initial_messages

    def cancel_after_builder(*args, **values):
        result = actual(*args, **values)
        with operation_store[0].begin() as conn:
            conn.execute(
                update(jobs)
                .where(jobs.c.operation_id == kwargs["inventory"].running.claim.operation_id)
                .values(cancel_requested_at=database_now(conn))
            )
        return result

    monkeypatch.setattr(producers, "build_service_initial_messages", cancel_after_builder)
    with pytest.raises(ComposerOperationCancelledDuringTurn):
        await producers.load_current_message_producers(**kwargs, initial=True)
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_started_current_read_cancellation_retains_original_singleton(operation_store, monkeypatch):
    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(operation_store)
    assert (await producers.load_current_message_producers(**kwargs)).producers
    entered, release = Event(), Event()
    actual = producers._produce_current_messages

    def held(**values):
        result = actual(**values)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(producers, "_produce_current_messages", held)
    task = asyncio.create_task(producers.load_current_message_producers(**kwargs))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel("current message cancellation")
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError, match="current message cancellation"):
        await task
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("wire", "current_skill"))
async def test_cold_rebuild_requires_complete_wire_and_actual_current_skill_after_positive(operation_store, change):
    import json

    from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json

    kwargs, evidence, _recorder, snapshot, iteration, _actors = await producer_case(operation_store)
    initial = await producers.load_current_message_producers(**kwargs, initial=True)
    messages = list(initial.messages)
    assert messages and any(PROMPT_GUARD_TRUST_KEY in item for item in messages)

    def alter(shelf):
        wire = json.loads(shelf.messages_json[0])
        wire["content"] += " forged retained instruction"
        return shelf.model_copy(update={"messages_json": (encode_transcript_object_json(wire), *shelf.messages_json[1:])})

    next_kwargs, shelf = await save_produced_shelf(
        operation_store,
        kwargs,
        snapshot,
        iteration,
        evidence.transcript,
        messages,
        initial.producers,
        transform_shelf=alter if change == "wire" else None,
    )
    if change == "current_skill":
        positive = await producers.load_current_message_producers(**next_kwargs, retained_shelf=shelf)
        assert detached(positive.messages) == detached(messages)
        kwargs["service"]._composer_skill_text += "\nCurrent deployment instruction changed."
    with pytest.raises(AuditIntegrityError, match="complete retained ordered wire"):
        await producers.load_current_message_producers(**next_kwargs, retained_shelf=shelf)
    kwargs["required_work"].coordinator.assert_completed()
    next_kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_admission_wait_keeps_exact_original_cancellation_without_message_work(operation_store, monkeypatch):
    from elspeth.web import async_workers

    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(operation_store)
    assert (await producers.load_current_message_producers(**kwargs)).producers
    entered = asyncio.Event()
    originals = []
    calls = []
    admission = async_workers._acquire_admission
    actual = producers._produce_current_messages

    async def observe_wait():
        entered.set()
        try:
            await admission()
        except asyncio.CancelledError as error:
            originals.append(error)
            raise

    def observe_read(**values):
        calls.append(True)
        return actual(**values)

    monkeypatch.setattr(async_workers, "ADMISSION_CAPACITY", 0)
    monkeypatch.setattr(async_workers, "ADMISSION_WAIT_SECONDS", 30.0)
    monkeypatch.setattr(async_workers, "_acquire_admission", observe_wait)
    monkeypatch.setattr(producers, "_produce_current_messages", observe_read)
    task = asyncio.create_task(producers.load_current_message_producers(**kwargs))
    try:
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        task.cancel("message producer admission cancellation")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert len(originals) == 1 and caught.value is originals[0]
    assert calls == []
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_live_registration_context_is_same_current_session_actor_and_object(operation_store):
    from dataclasses import replace

    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(operation_store)
    current = await producers.load_current_message_producers(**kwargs)
    context = kwargs["inventory"].running.session_operation_context
    expected = {"session_id": str(operation_store[4]), "actor_user_id": "alice", "context": context}
    current.producers.require_context(**expected)
    for replacement in ({"session_id": "another-session"}, {"actor_user_id": "another-actor"}, {"context": replace(context)}):
        with pytest.raises(AuditIntegrityError, match="actual current context"):
            current.producers.require_context(**{**expected, **replacement})
    kwargs["required_work"].coordinator.assert_completed()


@pytest.mark.asyncio
async def test_missing_actual_ingress_receipt_refuses_after_valid_complete_selected_input(operation_store):
    kwargs, _evidence, _recorder, _snapshot, _iteration, _actors = await producer_case(operation_store)
    assert (await producers.load_current_message_producers(**kwargs, initial=True)).messages
    engine, repository, authority, sessions, _sid = operation_store
    another = repository.create_session_with_initial_fence(
        user_id="alice", title="Missing ingress receipt", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
    )
    invalid, *_rest = await producer_case((engine, repository, authority, sessions, another.id), retain_ingress=False)
    with pytest.raises(AuditIntegrityError, match="immutable same-session ingress receipt"):
        await producers.load_current_message_producers(**invalid, initial=True)
    kwargs["required_work"].coordinator.assert_completed()
    invalid["required_work"].coordinator.assert_completed()
