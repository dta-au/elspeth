"""Genuine ready producers → atomic first Compose → actual current owners.

Only the raw external SDK/guard transport is controlled. No direct Compose
fixture, decoded authorization, fake audit or later-phase driver is installed.
"""

import asyncio
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import uuid4

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.prompt_guard import detach_prompt_guard_evidence
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer import continuation_composition_initialization as initialization
from elspeth.web.composer import continuation_title_application as title_application
from elspeth.web.composer._compose_loop_carriers import _ComposeIterationState
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_caches import decode_continuation_caches
from elspeth.web.composer.continuation_clock import SegmentFit, admit_action
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_composition_initialization_data import capture_initial_composition
from elspeth.web.composer.continuation_evidence import decode_turn_evidence
from elspeth.web.composer.continuation_initialization import _registered
from elspeth.web.composer.continuation_iteration import decode_continuation_iteration
from elspeth.web.composer.continuation_message_builders import build_service_initial_messages
from elspeth.web.composer.continuation_message_shelf import encode_transcript_object_json
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from elspeth.web.composer.diagnostic_evidence import ComposerDiagnosticEvidence, DiagnosticPayload
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_gateway import ProviderGateway
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.transcript_blob_reader import CurrentTranscriptBlobReader
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import continuation_composition_initialization as owner
from elspeth.web.sessions._persist_payload import RedactedToolRow
from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn, ComposerOperationFenceLost
from elspeth.web.sessions.continuation import ContinuationCASConflict
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table as sessions
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.fixtures.identities import grant_test_pipeline_user
from tests.helpers.continuation_inventory import inventory_work
from tests.helpers.continuation_memo import RetentionMemoTrace
from tests.helpers.fenced_session import seed_token_policies
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted, _required_guard_settings
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.sessions.test_continuation_ingress import recovery_rows
from tests.unit.web.sessions.test_continuation_initialization import _reclaim_pretitle, bootstrap_case, publish
from tests.unit.web.sessions.test_continuation_prompt_producers import current_service
from tests.unit.web.sessions.test_continuation_title_application import paid_title, reached_or_failure
from tests.unit.web.sessions.test_continuation_title_provider import actual_title_update

operation_store = operations.operation_store


async def ready_case(store, tmp_path, monkeypatch, *, paid=False, bound_base=False, content="Original Café Session Title"):
    if paid:
        running, service, work, settled, original, sdk, calls = await paid_title(store, tmp_path, monkeypatch, content)
        ready = await title_application.apply_current_title_success(service=service, selected=settled.selected, required_work=work)
        assert ready.applied and ready.result == original.result and len(sdk) == len(calls) == 1
        return running, service, work, ready.selected, sdk
    running, completed, service, _prior, _base = await bootstrap_case(store, eligible=False, bound_base=bound_base)
    work = inventory_work(running)
    ready = await publish(store, running, completed, service, work)
    return running, service, work, ready.selected, []


async def construct(store, running, service, work, selected):
    return await initialization.initialize_continuation_composition(
        service=service,
        authority=store[2],
        selected=selected,
        required_work=work,
        provider_owner=ProviderInvocationOwner(service=store[3], required_work=work),
    )


def source_chunk(runtime, reference):
    return next(chunk for chunk in runtime.source.selected.chunks if chunk.reference == reference)


def assert_first_graph(runtime, service, running):
    frame = runtime.source.selected.stack.frames[0]
    assert type(frame) is ComposeFrame and frame.phase == "action_ready" and frame.pending_response is None
    assert frame.initial_state == frame.state and frame.slice_sequence == running.claim.segment_sequence
    assert (
        frame.composition_slice,
        frame.discovery_slice,
        frame.composition_total,
        frame.discovery_total,
        frame.advisor_calls,
        frame.repair_turns,
        frame.advisor_checkpoint_passes,
    ) == (0, 0, 0, 0, 0, 0, 0)
    assert runtime.live.state is runtime.graph.evidence.effects.initial_state is runtime.graph.evidence.effects.latest_verified_state
    assert runtime.live.llm_messages is runtime.graph.messages and runtime.live.recorder is runtime.graph.recorder
    assert runtime.live.turn_evidence is runtime.graph.evidence and runtime.graph.batch is None
    assert runtime.bindings.session_operation_context is running.session_operation_context
    assert runtime.bindings.mutation_ceiling.request_id == runtime.source.selected.stack.logical.original_user_message_id
    assert runtime.bindings.message == runtime.source.original.message
    assert runtime.bindings.completion_gates == runtime.source.original.completion_gates
    assert not runtime.live.discovery_cache and not runtime.live.runtime_preflight_cache and runtime.live.last_runtime_preflight is None
    assert not runtime.live.recorder.llm_calls and not runtime.live.recorder.invocations
    iteration = decode_continuation_iteration(source_chunk(runtime, frame.iteration), env_ref_names=runtime.env_ref_names)
    assert (
        iteration.last_validation_json is None and not iteration.producer_inputs.states_json and not iteration.producer_inputs.schema_inputs
    )
    # Current policy validation is freshly projected by the existing runtime;
    # it is not a fabricated retained tool validation/cache observation.
    _snapshot, catalog = service._policy_context.build(runtime.bindings.user_id)
    assert runtime.live.last_validation == catalog.validate_composition_state(runtime.live.state).validation
    cache = decode_continuation_caches(source_chunk(runtime, frame.caches), env_ref_names=runtime.env_ref_names)
    assert cache.capture_count == 1 and len(cache.capture_creators) == 1 and runtime.live.continuation_cache_history == cache
    retained = decode_turn_evidence(source_chunk(runtime, frame.evidence), env_ref_names=runtime.env_ref_names).snapshot
    assert retained.requires_blob_reader and retained.initial_is_latest
    reader = runtime.graph.evidence.transcript._blob_reader
    assert type(reader) is CurrentTranscriptBlobReader and reader.service is service and reader.context is running.session_operation_context
    assert reader.engine is service._session_engine and reader.data_dir == service._data_dir
    return frame


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "paid,bound_base,content",
    [
        pytest.param(False, False, None, id="nonpaid-rootless"),
        pytest.param(False, True, None, id="nonpaid-base"),
        pytest.param(True, False, "Original Café Session Title", id="paid-accepted"),
        pytest.param(True, False, None, id="paid-none"),
        pytest.param(True, False, "", id="paid-empty"),
        pytest.param(True, False, "Rejected! Title Candidate", id="paid-rejected"),
    ],
)
async def test_real_ready_publishes_complete_first_compose_once(operation_store, tmp_path, monkeypatch, paid, bound_base, content, request):
    running, service, work, ready, sdk = await ready_case(
        operation_store, tmp_path, monkeypatch, paid=paid, bound_base=bound_base, content=content
    )
    memo_trace = RetentionMemoTrace(monkeypatch, request)
    try:
        before = dict(recovery_rows(operation_store[0]))
        prior_root = decode_continuation_snapshot(ready.payload_json)
        runtime = await construct(operation_store, running, service, work, ready)
        frame = assert_first_graph(runtime, service, running)
        after = dict(recovery_rows(operation_store[0]))
        changed_tables = {jobs.name, chunks.name, checkpoints.name, events.name}
        assert before.keys() == after.keys()
        for table_name in before:
            if table_name not in changed_tables:
                assert after[table_name] == before[table_name]
        root = runtime.source.selected.stack
        assert root.cursor == prior_root.cursor + 1 and root.logical == prior_root.logical
        assert root.mutation_ceiling == prior_root.mutation_ceiling and root.admission_policy_json == prior_root.admission_policy_json
        assert root.provider_cost_policy == prior_root.provider_cost_policy and root.effect_frontier == prior_root.effect_frontier
        assert len(sdk) == int(paid) and frame.current_state_id == prior_root.logical.base_state_id
        snapshot, catalog = service._policy_context.build(runtime.bindings.user_id)
        original = runtime.source.original
        expected = build_service_initial_messages(
            service,
            chat_history=list(original.history),
            state=original.initial_state,
            user_message=original.message,
            session_id=root.logical.session_id,
            user_id=original.snapshot.actor_user_id,
            plugin_snapshot=snapshot,
            policy_catalog=catalog,
            session_operation_context=running.session_operation_context,
            env_ref_names=runtime.env_ref_names,
            schemas_loaded=frozenset(original.snapshot.initial_schema_disclosure),
        )
        expected_wire = tuple(encode_transcript_object_json(value) for value in detach_prompt_guard_evidence(expected).messages)
        actual_wire = tuple(runtime.materialization.capture_message(value)[0] for value in runtime.live.llm_messages[: len(expected)])
        assert actual_wire == expected_wire
        original_selected = operation_store[2].load_continuation_snapshot(running)
        stable = recovery_rows(operation_store[0])
        replay = await construct(operation_store, running, service, work, original_selected)
        assert_first_graph(replay, service, running)
        assert replay.source.selected == runtime.source.selected and recovery_rows(operation_store[0]) == stable
        assert replay.materialization is not runtime.materialization and replay.graph.evidence is not runtime.graph.evidence
        assert len(sdk) == int(paid)
        work.coordinator.assert_completed()
        memo_trace.finish(stages=2)
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("erase", "include-tail"))
async def test_first_compose_refuses_changed_original_excluded_prefix(operation_store, tmp_path, monkeypatch, change):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    try:
        prepared = await initialization._prepare(service=service, authority=operation_store[2], selected=ready, work=work)
        assert type(prepared) is initialization.PreparedCompositionInitialization
        live = prepared.owners.live
        excluded = live.turn_evidence.transcript._excluded
        original_excluded = tuple(excluded)
        assert 0 < len(original_excluded) < len(live.llm_messages)
        assert all(value is live.llm_messages[index] for index, value in enumerate(original_excluded))
        assert capture_initial_composition(prepared.owners) == prepared.captured
        before = recovery_rows(operation_store[0])
        try:
            if change == "erase":
                excluded.clear()
            else:
                excluded.append(live.llm_messages[len(original_excluded)])
            changed_capture = capture_initial_composition(prepared.owners)
            assert changed_capture != prepared.captured
            with pytest.raises(AuditIntegrityError, match="exact original excluded initial-message prefix"):
                await _registered(
                    work=work,
                    source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                    operation=lambda: operation_store[2].publish_composition_initialization(
                        running, composer=service, owners=prepared.owners, captured=changed_capture
                    ),
                    expected=owner.StoredContinuationSnapshot,
                )
        finally:
            excluded[:] = original_excluded
        assert recovery_rows(operation_store[0]) == before
        assert capture_initial_composition(prepared.owners) == prepared.captured
        assert len(sdk) == 0
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_ready_cannot_enter_existing_runtime_without_first_compose_publication(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, _sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    try:
        source = await initialization._read_source(service=service, authority=operation_store[2], selected=ready, work=work)
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="exactly one actual Compose frame"):
            await restore_continuation_runtime(
                service=service,
                authority=operation_store[2],
                selected=source.selected,
                inventory=source.inventory,
                required_work=work,
                provider_owner=ProviderInvocationOwner(service=operation_store[3], required_work=work),
            )
        assert recovery_rows(operation_store[0]) == before
        prepared = await initialization._prepare(service=service, authority=operation_store[2], selected=ready, work=work)
        assert type(prepared) is initialization.PreparedCompositionInitialization
        draft = prepared.owners.live
        assert type(draft) is _ComposeIterationState
        assert draft.state is draft.turn_evidence.effects.initial_state is draft.turn_evidence.effects.latest_verified_state
        assert capture_initial_composition(prepared.owners) == prepared.captured
        draft.composition_turns_used = 1
        try:
            with pytest.raises(AuditIntegrityError, match="not the actual original first composition"):
                capture_initial_composition(prepared.owners)
        finally:
            draft.composition_turns_used = 0
        assert capture_initial_composition(prepared.owners) == prepared.captured
        producer = prepared.owners.materialization
        message = prepared.owners.live.llm_messages[0]
        assert producer.capture_message(message)[1] is not None
        with pytest.raises(AuditIntegrityError, match="no actual registered current producer"):
            producer.capture_message(dict(message))
        with pytest.raises(AuditIntegrityError, match="only its actual initial-message family"):
            producer.reply_instruction()
        with pytest.raises(AuditIntegrityError, match="no existing Compose/family source"):
            producer._require_current()
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ("after_chunk", "after_append"))
async def test_initial_compose_publication_rolls_back_complete_sql_then_replays(operation_store, tmp_path, monkeypatch, stage, request):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch, paid=True)
    memo_trace = RetentionMemoTrace(monkeypatch, request)
    marker = RuntimeError("original first Compose transaction failure")
    original_put, original_append = owner.put_chunk, owner.append_complete_snapshot
    reached = []

    def fail_put(conn, **kwargs):
        original_put(conn, **kwargs)
        reached.append(stage)
        raise marker

    def fail_append(conn, **kwargs):
        original_append(conn, **kwargs)
        reached.append(stage)
        raise marker

    try:
        before = recovery_rows(operation_store[0])
        with monkeypatch.context() as patch:
            if stage == "after_chunk":
                patch.setattr(owner, "put_chunk", fail_put)
            else:
                patch.setattr(owner, "append_complete_snapshot", fail_append)
            with pytest.raises(RuntimeError) as caught:
                await construct(operation_store, running, service, work, ready)
            assert caught.value is marker and reached == [stage]
            assert recovery_rows(operation_store[0]) == before
        result = await construct(operation_store, running, service, work, ready)
        assert_first_graph(result, service, running)
        assert len(sdk) == 1
        work.coordinator.assert_completed()
        memo_trace.finish(stages=2)
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_cancelled_first_publication_joins_commit_and_read_only_replay(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch, paid=True)
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    original_append = owner.append_complete_snapshot
    task = None

    def held(conn, **kwargs):
        actual = original_append(conn, **kwargs)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=10), "test did not release its owned publication worker"
        return actual

    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, "append_complete_snapshot", held)
            task = asyncio.create_task(construct(operation_store, running, service, work, ready))
            await reached_or_failure(task, entered)
            task.cancel("controlled first Compose caller cancellation")
            release.set()
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert caught.value.args == ("controlled first Compose caller cancellation",)
        work.coordinator.assert_completed()
        committed = operation_store[2].load_continuation_snapshot(running)
        assert type(decode_continuation_snapshot(committed.payload_json).frames[0]) is ComposeFrame
        before = recovery_rows(operation_store[0])
        retry = inventory_work(running)
        result = await construct(operation_store, running, service, retry, committed)
        assert_first_graph(result, service, running)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        retry.coordinator.assert_completed()
    finally:
        release.set()
        if task is not None and not task.done():
            task.cancel("owned first Compose cleanup")
            await asyncio.gather(task, return_exceptions=True)
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("paid,cut", [(False, "ready"), (False, "compose"), (True, "ready"), (True, "compose")])
async def test_actual_new_claim_constructs_or_consumes_first_compose_without_repeating_work(
    operation_store, tmp_path, monkeypatch, paid, cut
):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch, paid=paid)
    if cut == "compose":
        await construct(operation_store, running, service, work, ready)
    work.coordinator.assert_completed()
    current, now = _reclaim_pretitle(operation_store, running)
    current_service_owner, _actors = current_service(current)
    fresh = inventory_work(now)
    try:
        selected = current[2].load_continuation_snapshot(now)
        before = recovery_rows(current[0])
        result = await construct(current, now, current_service_owner, fresh, selected)
        assert_first_graph(result, current_service_owner, now)
        assert result.bindings.session_operation_context is now.session_operation_context
        assert result.source.selected.stack.logical == decode_continuation_snapshot(ready.payload_json).logical
        if cut == "compose":
            assert recovery_rows(current[0]) == before
        policy = decode_continuation_policy(result.source.selected.stack.admission_policy_json, result.source.selected.stack.policy_hash)
        assert next_continuation_action_policy(selected=result.source.selected, policy=policy) == policy.actions.primary
        stale = inventory_work(running)
        with pytest.raises((AuditIntegrityError, SessionOperationFenceLost, ComposerOperationFenceLost)):
            await construct(operation_store, running, service, stale, ready)
        stale.coordinator.assert_completed()
        assert len(sdk) == int(paid)
        fresh.coordinator.assert_completed()
    finally:
        current[1].release(now.session_operation_context)


@pytest.mark.asyncio
async def test_postcommit_restore_error_keeps_original_cut_and_retry_never_recaptures(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch, paid=True)
    original_restore = initialization.restore_continuation_runtime
    marker = RuntimeError("original postcommit current-runtime projection failure")
    observed = []

    async def fail_after_actual_restore(**kwargs):
        runtime = await original_restore(**kwargs)
        observed.append(runtime.source.selected)
        raise marker

    def no_second_capture(*args, **kwargs):
        raise AssertionError("initializer replay repeated first-owner capture")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(initialization, "restore_continuation_runtime", fail_after_actual_restore)
            with pytest.raises(RuntimeError) as caught:
                await construct(operation_store, running, service, work, ready)
            assert caught.value is marker and len(observed) == 1
        work.coordinator.assert_completed()
        committed = operation_store[2].load_continuation_snapshot(running)
        assert committed.checkpoint_id == observed[0].checkpoint_id
        before = recovery_rows(operation_store[0])
        retry = inventory_work(running)
        with monkeypatch.context() as patch:
            patch.setattr(initialization, "_build_owners", no_second_capture)
            result = await construct(operation_store, running, service, retry, committed)
        assert_first_graph(result, service, running)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        retry.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_paid_ready_and_first_compose_replay_preserve_real_later_rename(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch, paid=True)
    try:
        await actual_title_update(operation_store, running, work, "Renamed Before Compose")
        result = await construct(operation_store, running, service, work, ready)
        await actual_title_update(operation_store, running, work, "Renamed After Compose")
        latest = operation_store[2].load_continuation_snapshot(running)
        before = recovery_rows(operation_store[0])
        replay = await construct(operation_store, running, service, work, latest)
        assert_first_graph(replay, service, running)
        assert replay.source.selected == result.source.selected and recovery_rows(operation_store[0]) == before
        with operation_store[0].connect() as conn:
            assert (
                conn.execute(select(sessions.c.title).where(sessions.c.id == str(operation_store[4]))).scalar_one()
                == "Renamed After Compose"
            )
        assert len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("skill", "source"))
async def test_prepared_first_capture_refuses_real_source_or_skill_change_without_writes(operation_store, tmp_path, monkeypatch, fault):
    running, service, work, ready, _sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    try:
        prepared = await initialization._prepare(service=service, authority=operation_store[2], selected=ready, work=work)
        assert type(prepared) is initialization.PreparedCompositionInitialization
        assert capture_initial_composition(prepared.owners) == prepared.captured
        if fault == "skill":
            monkeypatch.setattr(service, "_composer_skill_text", service._composer_skill_text + "\nChanged current deployment input")
        else:
            await actual_title_update(operation_store, running, work, "Changed Provider-Free Source")
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match=r"skill changed|original actor/request/base/transcript/gates/title source"):
            await _registered(
                work=work,
                source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                operation=lambda: operation_store[2].publish_composition_initialization(
                    running, composer=service, owners=prepared.owners, captured=prepared.captured
                ),
                expected=owner.StoredContinuationSnapshot,
            )
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_first_compose_refuses_coherent_forward_rewrite_without_writes(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, _sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    try:
        result = await construct(operation_store, running, service, work, ready)
        assert_first_graph(result, service, running)
        selected = operation_store[2].load_continuation_snapshot(running)
        root = decode_continuation_snapshot(selected.payload_json)
        frame = root.frames[0]
        altered = root.model_copy(update={"frames": (frame.model_copy(update={"initial_version": frame.initial_version + 1}),)})
        encoded = encode_continuation_snapshot(altered)
        assert encode_continuation_snapshot(decode_continuation_snapshot(encoded)) == encoded
        with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
            checkpoint = conn.execute(select(checkpoints).where(checkpoints.c.checkpoint_id == selected.checkpoint_id)).one()
            raw = encoded.encode("utf-8")
            changed = conn.execute(
                update(chunks)
                .where(
                    chunks.c.session_id == str(operation_store[4]),
                    chunks.c.operation_id == running.claim.operation_id,
                    chunks.c.chunk_key == checkpoint.snapshot_chunk_key,
                )
                .values(payload=raw, sha256=sha256(raw).hexdigest(), byte_length=len(raw))
            )
            assert changed.rowcount == 1
        actual = operation_store[2].load_continuation_snapshot(running)
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="complete atomic forward initialization"):
            await construct(operation_store, running, service, work, actual)
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_two_real_preparations_have_one_complete_initialization_commit(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, _sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    second = inventory_work(running)
    try:
        left = await initialization._prepare(service=service, authority=operation_store[2], selected=ready, work=work)
        right = await initialization._prepare(service=service, authority=operation_store[2], selected=ready, work=second)
        assert type(left) is type(right) is initialization.PreparedCompositionInitialization

        async def publish_one(binding, prepared):
            return await _registered(
                work=binding,
                source=RequiredWorkSource.CONTINUATION_STATE_SQL,
                operation=lambda: operation_store[2].publish_composition_initialization(
                    running, composer=service, owners=prepared.owners, captured=prepared.captured
                ),
                expected=owner.StoredContinuationSnapshot,
            )

        outcomes = await asyncio.gather(publish_one(work, left), publish_one(second, right), return_exceptions=True)
        assert sum(type(value) is owner.StoredContinuationSnapshot for value in outcomes) == 1
        assert sum(isinstance(value, ContinuationCASConflict) for value in outcomes) == 1
        selected = operation_store[2].load_continuation_snapshot(running)
        replay = inventory_work(running)
        result = await construct(operation_store, running, service, replay, selected)
        assert_first_graph(result, service, running)
        with operation_store[0].connect() as conn:
            recorded = conn.execute(
                select(events).where(events.c.session_id == str(operation_store[4]), events.c.operation_id == running.claim.operation_id)
            ).all()
            assert recorded[-1].sequence == ready.cursor + 1
        replay.coordinator.assert_completed()
        work.coordinator.assert_completed()
        second.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("paid", (False, True))
async def test_constructed_first_compose_enters_original_guarded_primary_once(operation_store, tmp_path, monkeypatch, paid):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch, paid=paid)
    try:
        runtime = await construct(operation_store, running, service, work, ready)
        assert_first_graph(runtime, service, running)
        if not paid:
            # The paid path already seeded alice through title_case; re-seeding
            # would collide on quota_policies.identity_id and would reset the
            # policy state the paid branch is asserting against.
            with operation_store[0].begin() as conn:
                grant_test_pipeline_user(conn, identity_id="alice")
                seed_token_policies(conn, identity_id="alice")
        settings, requests = _required_guard_settings(tmp_path, monkeypatch)
        service._provider_gateway = ProviderGateway(model="test/model", settings=settings, endpoint_base_url=None, endpoint_api_key=None)
        primary_calls = []

        async def completion(**kwargs):
            _assert_required_guard_admitted(
                operation_store[0],
                running.session_operation_context,
                requests,
                role="composer",
                user_prompt=runtime.bindings.message,
                expected_count=1 + int(paid),
            )
            primary_calls.append(kwargs)
            with operation_store[0].connect() as conn:
                dispatched = conn.execute(select(effects).where(effects.c.effect_key == "provider:0:0")).one()
                assert dispatched.disposition == "dispatched" and dispatched.result_chunk_key is None
            return ModelResponse(
                model="test/model",
                choices=[
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "The actual model chose the next reply.", "tool_calls": None},
                    }
                ],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            )

        monkeypatch.setattr(litellm, "acompletion", completion)
        policy = runtime.source.policy
        timing = runtime.source.timing
        assert (
            admit_action(
                policy.actions.primary,
                policy.segment,
                hard_remaining=timing.hard_remaining_seconds,
                cooperative_remaining=timing.cooperative_remaining_seconds,
                fresh=False,
            )
            is SegmentFit.ADMIT
        )
        with prompt_guard_user_scope(runtime.bindings.message):
            result = await run_continuation_primary(
                service=service,
                authority=operation_store[2],
                selected=runtime.source.selected,
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                drained=runtime.drained,
                required_work=work,
                provider_owner=runtime.bindings.provider_owner,
                timeout=5,
            )
        assert len(primary_calls) == len(runtime.live.recorder.llm_calls) == 1 and len(sdk) == int(paid)
        assert result.outcome.audit_call is runtime.live.recorder.llm_calls[0] is result.buffer.llm_calls[0]
        assert result.outcome.completion.message.content == "The actual model chose the next reply."
        with operation_store[0].connect() as conn:
            assert len(conn.execute(select(physical)).all()) == 1 + int(paid)
            assert len(conn.execute(select(usage)).all()) == 1 + int(paid)
        # A genuine PRIMARY result is now another phase, not a retry that may
        # invoke TITLE history/current family3-only source or reset initialization.
        before = recovery_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="cannot reset an advanced first composition"):
            await construct(operation_store, running, service, work, result.snapshot)
        assert recovery_rows(operation_store[0]) == before
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_cancelled_ready_cannot_publish_compose_under_owed_relief(operation_store, tmp_path, monkeypatch):
    running, service, work, ready, sdk = await ready_case(operation_store, tmp_path, monkeypatch, paid=True)
    try:
        assert not (await initialization._read_source(service=service, authority=operation_store[2], selected=ready, work=work)).initialized
        operation_store[2].request_cancel(
            session_id=operation_store[4], operation_id=running.claim.operation_id, cancelled_failure=operations.cancelled
        )
        before = recovery_rows(operation_store[0])
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await construct(operation_store, running, service, work, ready)
        assert recovery_rows(operation_store[0]) == before and len(sdk) == 1
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


def _digest(text):
    """The retained-payload digest a private evidence record must carry."""
    return sha256(text.encode()).hexdigest()


async def seed_blob_history(store, *, bound=True):
    """Seed one historical blob read.

    With ``bound``, the pair is written through ``persist_compose_turn_async``,
    the production compose-loop path, so the tool row carries the real
    ``VisibleToolEvent`` envelope that binds the creator operation id the
    advisor transcript's re-read gate requires. The public ``add_message``
    cannot express that shape: ``validate_visible_audit_row`` admits a visible
    companion only on an ``audit`` row carrying exactly two envelopes, so the
    envelope reaches a ``tool`` row only through the persist path.

    ``bound=False`` keeps the plain ``add_message`` pair, which the gate must
    go on refusing with ``unavailable_parent_writer_or_operation_binding``.

    ``selected_result`` is deliberately ``None``: a retained selected result
    short-circuits to ``ordinary_visible_result_secret_redacted_no_blob_reread``
    before any reader call, and this history exists to exercise the re-read.
    """
    engine, repository, _authority, sessions_owner, sid = store
    context = repository.acquire(
        session_id=sid, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="test-owner", lease_seconds=30
    )
    text = "Historical original blob café body and REFERENCE_TAIL_CANARY"
    raw = text.encode()
    try:
        blobs = BlobServiceImpl(
            engine, Path(sessions_owner._data_dir), session_operation_authority=sessions_owner.session_operation_authority
        )
        blob = await blobs.create_blob(sid, "retained.txt", raw, "text/plain", session_operation_context=context)
        arguments = canonical_json({"blob_id": str(blob.id), "expected_content_hash": blob.content_hash, "offset_chars": 0})
        call = {"id": "history-read", "type": "function", "function": {"name": "get_blob_content", "arguments": arguments}}
        result = {
            "success": True,
            "data": {
                "blob_id": "<redacted>",
                "content": {"_type": "str", "length": len(text)},
                "content_hash": blob.content_hash,
                "size_bytes": len(raw),
                "total_chars": len(text),
                "start_char": 0,
                "end_char": len(text),
                "truncated": False,
                # The created blob's own stored provenance, which is what the
                # gate compares the recorded result against.
                "created_by": blob.created_by,
                "creation_modality": blob.creation_modality.value,
            },
        }
        serialized = canonical_json(result)
        if bound:
            visible = VisibleToolEvent.capture(
                context=context,
                request_id=None,
                tool_call_id="history-read",
                tool_name="get_blob_content",
                received_arguments=arguments,
                effective_arguments=arguments,
                selected_result=None,
                display_arguments=arguments,
                display_result=serialized,
            )
            rows = (
                RedactedToolRow(tool_call_id="history-read", content=serialized, composition_state_payload=None, visible_event=visible),
            )
            await sessions_owner.persist_compose_turn_async(
                session_id=str(sid),
                assistant_content="",
                redacted_assistant_tool_calls=(call,),
                redacted_tool_rows=rows,
                # No row advances the graph, so the refusal alternate must be
                # the same tuple object, not an equal copy.
                revoked_tool_rows=rows,
                # One private evidence record per persisted tool row, carrying
                # this operation's own authority.
                private_tool_evidence=(
                    ComposerDiagnosticEvidence(
                        session_id=str(sid),
                        operation_id=context.fence.operation_id,
                        operation_epoch=context.fence.operation_epoch,
                        tool_call_id="history-read",
                        tool_name="get_blob_content",
                        status="success",
                        evidence_id=str(uuid4()),
                        arguments=DiagnosticPayload(
                            disposition="retained", encoding="json", content=arguments, retained_sha256=_digest(arguments)
                        ),
                        authority_arguments=None,
                        result=DiagnosticPayload(
                            disposition="retained", encoding="json", content=serialized, retained_sha256=_digest(serialized)
                        ),
                        wire_arguments=DiagnosticPayload(
                            disposition="retained", encoding="wire_text", content=arguments, retained_sha256=_digest(arguments)
                        ),
                    ),
                ),
                parent_composition_state_id=None,
                expected_current_state_id=None,
                writer_principal="compose_loop",
                plugin_crash_pending=False,
                session_operation_context=context,
            )
            return blob, text
        assistant = await sessions_owner.add_message(
            sid, "assistant", "", writer_principal="compose_loop", tool_calls=[call], session_operation_context=context
        )
        await sessions_owner.add_message(
            sid,
            "tool",
            serialized,
            writer_principal="compose_loop",
            tool_call_id="history-read",
            parent_assistant_id=assistant.id,
            session_operation_context=context,
        )
        return blob, text
    finally:
        repository.release(context)


@pytest.mark.asyncio
async def test_initial_capture_retains_actual_reader_and_rechecks_historical_blob_access(operation_store, tmp_path, monkeypatch):
    blob, text = await seed_blob_history(operation_store)
    running, service, work, ready, _sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    released = False
    try:
        runtime = await construct(operation_store, running, service, work, ready)
        assert_first_graph(runtime, service, running)
        transcript = runtime.graph.evidence.transcript
        snapshot = await asyncio.to_thread(transcript.snapshot)
        assert text in snapshot.provider_json and "historical_provider_delivery_not_reconstructed" in snapshot.provider_json
        reader = transcript._blob_reader
        with pytest.raises(AuditIntegrityError, match="actual service/store/current context"):
            replace(reader, data_dir=reader.data_dir + "-foreign")
        changed = replace(
            reader,
            context=replace(reader.context, fence=replace(reader.context.fence, operation_epoch=reader.context.fence.operation_epoch + 1)),
        )
        with pytest.raises(AuditIntegrityError, match="another evidence physical owner"):
            changed.require_for_evidence(runtime.graph.evidence)
        Path(blob.storage_path).unlink()
        missing = await asyncio.to_thread(transcript.snapshot)
        assert text not in missing.provider_json and "unavailable_" in missing.provider_json
        work.coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)
        released = True
        with pytest.raises(SessionOperationFenceLost):
            await asyncio.to_thread(reader, str(blob.id))
    finally:
        if not released:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_initial_capture_refuses_history_whose_tool_row_binds_no_creator_operation(operation_store, tmp_path, monkeypatch):
    """Unbound history keeps failing closed, and no blob reader is reached.

    A plain ``add_message`` pair writes a history production never writes: the
    tool row carries no visible-tool-event envelope, so nothing binds a creator
    operation id. Refusing it is the guard's contract, not a defect.
    """
    blob, text = await seed_blob_history(operation_store, bound=False)
    running, service, work, ready, _sdk = await ready_case(operation_store, tmp_path, monkeypatch)
    # Remove the body first: a reader that was reached would report a
    # read-side failure instead, so the binding label proves the gate fired
    # ahead of any reader call rather than merely alongside it.
    Path(blob.storage_path).unlink()
    try:
        runtime = await construct(operation_store, running, service, work, ready)
        assert_first_graph(runtime, service, running)
        transcript = runtime.graph.evidence.transcript

        snapshot = await asyncio.to_thread(transcript.snapshot)

        assert "unavailable_parent_writer_or_operation_binding" in snapshot.provider_json
        assert text not in snapshot.provider_json
        work.coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
