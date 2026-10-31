"""Real guarded primary, actual B2 preparation/read/finish and atomic successor.

Only the external SDK/guard transport is controlled. The initial prepared phase
checkpoint uses the actual produced complete runtime values; no Running,
invocation, read receipt or current authorization is fabricated.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer import continuation_attachment_application_driver as driver
from elspeth.web.composer import continuation_attachment_finisher as finisher
from elspeth.web.composer import continuation_runtime as runtime_owner
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_application_graph import admit_application_recorder_source
from elspeth.web.composer.continuation_attachment_application_driver import (
    apply_current_attachment_runtime,
    read_current_applied_attachment,
)
from elspeth.web.composer.continuation_attachment_result import apply_attachment_history, read_attachment_result, run_attachment_result
from elspeth.web.composer.continuation_audit_buffer import RecorderDrainCursors
from elspeth.web.composer.continuation_batch_data import RestoredBatchData, RestoredPreparedBatchData, encode_restored_prepared_batch_data
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot, encode_private_chunk
from elspeth.web.composer.continuation_dispatch_audit import capture_dispatch_audit
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_evidence import admit_turn_evidence, decode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import require_evidence_application_links
from elspeth.web.composer.continuation_evidence_graph import RestoredTurnEvidence
from elspeth.web.composer.continuation_message_shelf import decode_message_shelf
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_prompt_producers import load_current_message_producers
from elspeth.web.composer.continuation_runtime import (
    build_runtime_tool_batch_context,
    capture_continuation_runtime_owners,
    restore_continuation_runtime,
)
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tool_batch import (
    BatchAccumulator,
    ContinuationBatchState,
    ContinuationCallState,
    _initialize_continuation_batch_live,
    _prepare_continuation_call_live,
)
from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
)
from elspeth.web.sessions.continuation_runtime import read_runtime_source_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.helpers.continuation_budgets import OWNER_LEASE_SECONDS
from tests.helpers.continuation_inventory import inventory_work
from tests.unit.web.composer.test_explicit_provider_custody import _assert_required_guard_admitted
from tests.unit.web.coordination import test_composer_operation_authority as operations
from tests.unit.web.coordination.test_continuation_p4_recovery_start import (
    current_selected,
    new_owners,
    recovery_rows,
    recovery_rows_on_connection,
)
from tests.unit.web.sessions.test_continuation_primary_result import post_p4_primary_input, primary_input
from tests.unit.web.sessions.test_continuation_prompt_producers import REQUEST, current_service
from tests.unit.web.sessions.test_continuation_runtime import install_capture, runtime_case

operation_store = operations.operation_store


async def _latest(arguments):
    work, authority = arguments["required_work"], arguments["authority"]
    stored = await asyncio.to_thread(authority.load_continuation_snapshot, work.running)
    selected = await load_selected_continuation(authority=authority, running=work.running, selected=stored, required_work=work)
    inventory = await materialize_continuation_env_inventory(
        advisor=arguments["service"]._advisor_checkpoint, authority=authority, selected=selected, required_work=work
    )
    return {**arguments, "selected": selected, "inventory": inventory}


def _read_args(arguments):
    return {key: arguments[key] for key in ("authority", "selected", "inventory", "required_work")}


def _runtime_sql_no_write(engine, arguments, *, error=None):
    """The actual cold-runtime SQL boundary, observed on its own connection."""
    authority, inventory, work = arguments["authority"], arguments["inventory"], arguments["required_work"]
    running = runtime_owner._work_owners(authority, inventory, work)
    assert authority._engine is engine
    with locked_session_transaction(engine, str(running.claim.session_id)) as conn:
        if conn.dialect.name == "postgresql":
            assert conn.get_isolation_level() == "READ COMMITTED"
        before = recovery_rows_on_connection(conn)
        if error is None:
            actual = read_runtime_source_on_connection(conn, running, selected=arguments["selected"], inventory=inventory)
            assert actual.selected is arguments["selected"]
        else:
            # Catch inside this transaction: unexpected reader writes remain
            # visible to the second snapshot instead of disappearing on rollback.
            with pytest.raises(AuditIntegrityError, match=error):
                read_runtime_source_on_connection(conn, running, selected=arguments["selected"], inventory=inventory)
        assert recovery_rows_on_connection(conn) == before
    return True


async def _registered_runtime_sql_no_write(engine, arguments, *, error=None):
    assert await runtime_owner._registered_read(
        arguments["required_work"], bool, lambda: _runtime_sql_no_write(engine, arguments, error=error)
    )


async def application_case(
    store, tmp_path, monkeypatch, *, tool="read_attachment", failed=False, post_p4=False, native=False, following_native=False
):
    if post_p4:
        kwargs, produced, recorder, requests, drained = await post_p4_primary_input(store, tmp_path, monkeypatch)
    else:
        kwargs, produced, recorder, requests = await primary_input(store, tmp_path, monkeypatch)
        drained = RecorderDrainCursors()
    running = kwargs["inventory"].running
    renewed = await asyncio.to_thread(store[1].renew, running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS)
    assert renewed is running.session_operation_context
    # Explicit ordinary phase-input fixture: select the actual service/dialect
    # tool wire before the real SDK action. Preserve every other root/value,
    # including an existing P4 journal, recorder, state and budget counters.
    original_selected = kwargs["selected"]
    original_producers = produced.producers
    compose = original_selected.stack.frames[0]
    assert type(compose) is ComposeFrame and compose.phase == "action_ready"
    tools = composer_loop_tool_definitions(kwargs["service"]._planner_dialect)
    tools_chunk = encode_private_chunk(codec="composer.tools.v1", canonical=canonical_json(tools), env_ref_names=kwargs["inventory"].names)
    tool_root = original_selected.stack.model_copy(
        update={"frames": (compose.model_copy(update={"tools": tools_chunk.reference}), *original_selected.stack.frames[1:])}
    )
    with locked_session_transaction(store[0], str(store[4])) as conn:
        put_chunk(
            conn,
            session_id=str(store[4]),
            operation_id=running.claim.operation_id,
            key=private_chunk_key(tools_chunk),
            payload=tools_chunk.canonical.encode("utf-8"),
            codec=tools_chunk.reference.codec,
        )
        tool_snapshot = append_complete_snapshot(
            conn,
            session_id=str(store[4]),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=original_selected.checkpoint_id,
            expected_cursor=original_selected.stack.cursor,
            snapshot_json=encode_continuation_snapshot(tool_root),
            event_batch=(ContinuationEvent("checkpoint", '{"fixture":"actual current service tool input before SDK"}'),),
            handoff=False,
        )
    kwargs = await _latest(kwargs)
    assert kwargs["selected"].checkpoint_id == tool_snapshot.checkpoint_id
    assert encode_continuation_snapshot(kwargs["selected"].stack) == encode_continuation_snapshot(
        tool_root.model_copy(update={"cursor": tool_snapshot.cursor})
    )
    shelf = decode_message_shelf(next(chunk for chunk in kwargs["selected"].chunks if chunk.reference == compose.transcript))
    produced = await load_current_message_producers(
        service=kwargs["service"],
        authority=kwargs["authority"],
        selected=kwargs["selected"],
        inventory=kwargs["inventory"],
        required_work=kwargs["required_work"],
        retained_shelf=shelf,
    )
    assert produced.messages is not None and produced.producers is not original_producers
    assert produced.producers.selected is kwargs["selected"]
    assert original_producers.selected is original_selected
    raw = b"Complete ordinary customer document with retained original bytes."
    blob_id = str(uuid4())
    path = store[3]._data_dir / "blobs" / str(store[4]) / f"{blob_id}_application.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with store[0].begin() as conn:
        conn.execute(
            insert(blobs_table).values(
                id=blob_id,
                session_id=str(store[4]),
                filename="application.txt",
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
    arguments = {"blob_id": str(uuid4()) if failed else blob_id}
    if tool == "get_blob_content":
        arguments.update(offset_chars=1, expected_content_hash=sha256(raw).hexdigest())
    from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments

    sdk = []

    async def completion(**sdk_arguments):
        _assert_required_guard_admitted(store[0], running.session_operation_context, requests, role="composer", user_prompt=REQUEST)
        sdk.append(sdk_arguments)
        with store[0].connect() as conn:
            actual = conn.execute(select(effects).where(effects.c.owner == "provider", effects.c.disposition == "dispatched")).one()
        assert actual.result_chunk_key is None
        return ModelResponse(
            model="test/model",
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "Read the selected user document.",
                        **(
                            {
                                "thinking_blocks": [
                                    {
                                        "type": "thinking",
                                        "thinking": "Actual controlled native read reasoning",
                                        "signature": "native-read-signature",
                                    }
                                ]
                            }
                            if native
                            else {}
                        ),
                        "tool_calls": [
                            {
                                "id": "actual-attachment-application",
                                "type": "function",
                                "function": {"name": tool, "arguments": canonical_json(arguments)},
                            }
                        ]
                        + (
                            [
                                {
                                    "id": "actual-native-after-attachment",
                                    "type": "function",
                                    "function": {
                                        "name": "get_pipeline_state",
                                        "arguments": canonical_json(
                                            encode_semantic_arguments("get_pipeline_state", kwargs["service"]._planner_dialect, {})
                                        ),
                                    },
                                }
                            ]
                            if following_native
                            else []
                        ),
                    },
                }
            ],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        )

    monkeypatch.setattr(litellm, "acompletion", completion)
    with prompt_guard_user_scope(REQUEST):
        published = await run_continuation_primary(
            service=kwargs["service"],
            authority=kwargs["authority"],
            selected=kwargs["selected"],
            producers=produced.producers,
            messages=list(produced.messages),
            tools=tools,
            recorder=recorder,
            drained=drained,
            required_work=kwargs["required_work"],
            provider_owner=ProviderInvocationOwner(service=store[3], required_work=kwargs["required_work"]),
            timeout=5,
        )
    assert len(sdk) == 1 and published.outcome.audit_call is recorder.llm_calls[-1]
    renewed = await asyncio.to_thread(store[1].renew, running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS)
    assert renewed is running.session_operation_context
    current, _caches, _history = await runtime_case(store, provider_input=(kwargs, published))
    renewed = await asyncio.to_thread(store[1].renew, running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS)
    assert renewed is running.session_operation_context
    runtime = await restore_continuation_runtime(**current)
    ctx = await build_runtime_tool_batch_context(runtime, cancellation_requested=asyncio.Event())
    live = runtime.live
    # The phase-input fixture applies the actual successful primary's delivery
    # using its complete current transcript/tool hashes and original audit. This
    # exercises the real registration producer; it does not forge a receipt or
    # claim the future primary-completion driver is installed.
    delivery = live.turn_evidence.prepare_delivery(
        live.llm_messages, previous_calls=live.recorder.llm_calls[:-1], tools=live.tools, scope="primary"
    )
    assert live.turn_evidence.confirm_delivery(delivery, live.recorder.llm_calls)
    assert live.turn_evidence.current_call_id("primary") == published.outcome.audit_call.call_id
    batch = ContinuationBatchState(
        live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used, entry_state=live.state
    )
    accumulator = BatchAccumulator(live.state, live.last_validation, live.last_runtime_preflight, live.advisor_calls_used)
    await _initialize_continuation_batch_live(
        call_model=published.outcome, ctx=ctx, acc=accumulator, llm_messages=live.llm_messages, batch=batch
    )
    call = ContinuationCallState(0, published.outcome.completion.tool_batch.calls[0], live.state, live.state.version)
    batch.current_call, batch.phase = call, "call_active"
    assert (
        await _prepare_continuation_call_live(call_model=published.outcome, ctx=ctx, llm_messages=live.llm_messages, batch=batch, call=call)
        == "prepared"
    )
    assert (
        await asyncio.to_thread(store[1].renew, running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS)
        is running.session_operation_context
    )
    original_audit = call.audit
    assert original_audit is not None
    # The actual initializer/argument admission registered this same object.
    assert batch.assistant_message_index is not None
    actual_message = live.llm_messages[batch.assistant_message_index]
    _wire, actual_recipe = runtime.materialization.capture_message(actual_message)
    assert actual_recipe is not None and actual_recipe.kind == ("native_thinking" if native else "tool_call_arguments")
    assert actual_recipe.response == published.response.reference
    assert actual_recipe.argument_indexes == (0,)
    data = RestoredBatchData(batch, accumulator, published.outcome.completion.tool_batch.calls)
    runtime = replace(
        runtime,
        graph=runtime.graph.with_batch(data).with_selected_dispatch(capture_dispatch_audit(original_audit), ctx.tool_contract_dialect),
    )
    captured = await capture_continuation_runtime_owners(runtime, selected=current["selected"], inventory=current["inventory"])
    compose = captured.stack.frames[0]
    snapshot = decode_turn_evidence(
        next(chunk for chunk in captured.chunks if chunk.reference == compose.evidence),
        env_ref_names=current["inventory"].names,
        source_chunks=captured.chunks,
    )
    graph = require_evidence_application_links(
        snapshot.snapshot,
        env_ref_names=current["inventory"].names,
        recorder_source=admit_application_recorder_source(snapshot.recorder_chunk, env_ref_names=current["inventory"].names),
        sources=snapshot.sources,
    )
    assert graph is not None and graph.selected_audit is not None and graph.selected_dialect is not None
    prepared = encode_private_chunk(
        codec="composer.tool-batch.v1",
        canonical=encode_restored_prepared_batch_data(
            RestoredPreparedBatchData(
                graph.selected.batch, graph.selected.accumulator, graph.selected.calls, graph.selected_audit, graph.selected_dialect
            ),
            env_ref_names=current["inventory"].names,
        ),
    )
    top = ToolBatchFrame(
        phase="tool_ready", response=published.response.reference, accumulator=prepared.reference, next_tool_index=0, settled_effect_ids=()
    )
    root = captured.stack.model_copy(update={"frames": (compose, top)})
    phase_input = runtime_owner.CapturedContinuationRuntime(
        current["selected"], root, runtime_owner._selected_chunks(root, captured.chunks, (prepared, published.response))
    )
    current = await install_capture(store, current, phase_input)
    runtime = replace(
        runtime,
        graph=RestoredTurnEvidence(
            runtime.graph.evidence,
            runtime.graph.messages,
            runtime.graph.recorder,
            runtime.graph.drained,
            runtime.graph.batch,
            runtime.graph.applications,
            graph.selected_audit,
            runtime.graph.selected_dialect,
        ),
    )
    selected_owners = RestoredTurnEvidence(
        runtime.graph.evidence,
        runtime.graph.messages,
        runtime.graph.recorder,
        runtime.graph.drained,
        runtime.graph.batch,
        runtime.graph.applications,
        graph.selected_audit,
        graph.selected_dialect,
    )
    cleared = selected_owners.without_selected_dispatch()
    assert cleared is not selected_owners and cleared.selected_audit is None and cleared.selected_dialect is None
    assert selected_owners.selected_audit is graph.selected_audit and selected_owners.selected_dialect is graph.selected_dialect
    assert cleared.evidence is selected_owners.evidence and cleared.messages is selected_owners.messages
    assert cleared.recorder is selected_owners.recorder and cleared.drained is selected_owners.drained
    assert cleared.batch is selected_owners.batch and cleared.applications is selected_owners.applications
    reselected = cleared.with_selected_dispatch(graph.selected_audit, graph.selected_dialect)
    assert reselected is not cleared and reselected == selected_owners
    assert cleared.selected_audit is None and cleared.selected_dialect is None
    assert reselected.selected_audit is graph.selected_audit and reselected.selected_dialect is graph.selected_dialect
    assert reselected.evidence is cleared.evidence and reselected.messages is cleared.messages
    assert reselected.recorder is cleared.recorder and reselected.drained is cleared.drained
    assert reselected.batch is cleared.batch and reselected.applications is cleared.applications
    await run_attachment_result(**_read_args(current), service=current["service"], audit=original_audit)
    assert (
        await asyncio.to_thread(store[1].renew, running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS)
        is running.session_operation_context
    )
    current = await _latest(current)
    await apply_attachment_history(**_read_args(current))
    current = await _latest(current)
    source = await read_attachment_result(**_read_args(current))
    assert source.history_applied and source.read_audit is not None and source.result.success is not failed
    renewed = await asyncio.to_thread(store[1].renew, running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS)
    assert renewed is running.session_operation_context
    return current, runtime, ctx, published.outcome, source, original_audit, sdk, path


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ("read_attachment", "get_blob_content"))
@pytest.mark.parametrize("failed", (False, True))
async def test_actual_attachment_finisher_commits_one_original_invocation_and_cold_replays_without_read(
    operation_store, tmp_path, monkeypatch, tool, failed
):
    arguments, runtime, ctx, call_model, source, audit, sdk, path = await application_case(
        operation_store, tmp_path, monkeypatch, tool=tool, failed=failed
    )
    running = arguments["inventory"].running
    original_batch, original_acc = runtime.graph.batch.batch, runtime.graph.batch.accumulator
    original_prestate = original_batch.state
    before_invocations = runtime.live.recorder.invocations
    lifecycle_owns_context = False
    try:
        result = await apply_current_attachment_runtime(
            runtime=runtime,
            selected=arguments["selected"],
            inventory=arguments["inventory"],
            ctx=ctx,
            call_model=call_model,
            effect_key=source.effect_key,
        )
        assert (
            await asyncio.to_thread(operation_store[1].renew, running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS)
            is running.session_operation_context
        )
        assert result.snapshot.cursor == arguments["selected"].stack.cursor + 1
        assert runtime.graph.batch.batch is original_batch and runtime.graph.batch.accumulator is original_acc
        assert original_batch.state is original_prestate is original_acc.state
        assert original_batch.next_tool_index == 1 and original_batch.current_call is None
        assert original_batch.phase == "dispatch_ready" and len(original_batch.tool_outcomes) == 1
        actual = runtime.live.recorder.invocations
        assert actual[:-1] == before_invocations and len(actual) == len(before_invocations) + 1
        assert actual[-1].started_at == audit.started_at and actual[-1].latency_ms >= source.read_audit.elapsed_active_ns // 1_000_000
        assert actual[-1].private_authoring is original_batch.private_authorings_by_call[call_model.completion.tool_batch.calls[0].id]
        assert original_batch.tool_outcomes[0].response.updated_state is original_prestate
        assert len(sdk) == 1 and not actual[-1].cache_hit
        registered = runtime.live.turn_evidence._pages
        if failed:
            assert call_model.completion.tool_batch.calls[0].id not in registered
        else:
            page = registered[call_model.completion.tool_batch.calls[0].id]
            assert page.producer_call_id == call_model.audit_call.call_id
            assert page.tool_call_id == actual[-1].tool_call_id
            assert not runtime.live.turn_evidence._historical_pages
        latest = await _latest(arguments)
        path.unlink()

        async def forbidden(**_arguments):
            raise AssertionError("already-applied attachment must never finish or read again")

        monkeypatch.setattr(driver, "finish_current_attachment_result", forbidden)
        before = recovery_rows(operation_store[0])
        replay = await apply_current_attachment_runtime(
            runtime=runtime,
            selected=latest["selected"],
            inventory=latest["inventory"],
            ctx=ctx,
            call_model=call_model,
            effect_key=source.effect_key,
        )
        assert replay.application == result.application and recovery_rows(operation_store[0]) == before
        assert (
            await asyncio.to_thread(operation_store[1].renew, running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS)
            is running.session_operation_context
        )
        # The exact no-write replay pair above is complete. Transfer this same
        # still-current context to the real lifetime before the final cold read.
        # adopt owns failure cleanup from this point, including before return.
        lifecycle_owns_context = True
        async with await SessionOperationLease.adopt(
            operation_store[1],
            running.session_operation_context,
            lease_seconds=OWNER_LEASE_SECONDS,
            required_work=arguments["required_work"].coordinator,
        ) as lease:
            assert lease.context is running.session_operation_context
            assert lease.required_work is arguments["required_work"].coordinator
            cold = await lease.create_task(restore_continuation_runtime(**latest), name="attachment-final-positive-cold-restore")
            assert cold.graph.batch.batch.next_tool_index == 1
            assert cold.graph.batch.batch.tool_outcomes[0].response.updated_state is cold.graph.batch.batch.state
            assert cold.live.recorder.invocations == actual
            assert cold.live.continuation_cache_history.capture_count > runtime.live.continuation_cache_history.capture_count
        assert lease.disposition is SessionOperationLeaseDisposition.RELEASED
        # close joins renewal and its required SQL before this final assertion.
        arguments["required_work"].coordinator.assert_completed()
    finally:
        if not lifecycle_owns_context:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ("before", "after"))
@pytest.mark.parametrize("handoff", ("release_then_recover", "live_handover"))
async def test_attachment_application_real_release_fresh_claim_preserves_original_read_and_receipt(
    operation_store, tmp_path, monkeypatch, point, handoff
):
    arguments, runtime, ctx, call_model, source, _audit, sdk, _path = await application_case(operation_store, tmp_path, monkeypatch)
    original = arguments["inventory"].running
    # Actual original bytes are gone after successful publication. The real
    # retained success and original observation must survive a fresh claim;
    # invoking the filesystem handler again cannot manufacture that success.
    _path.unlink()
    applied = None
    if point == "after":
        applied = await apply_current_attachment_runtime(
            runtime=runtime,
            selected=arguments["selected"],
            inventory=arguments["inventory"],
            ctx=ctx,
            call_model=call_model,
            effect_key=source.effect_key,
        )
        assert (
            await asyncio.to_thread(operation_store[1].renew, original.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS)
            is original.session_operation_context
        )
    arguments["required_work"].coordinator.assert_completed()
    if handoff == "live_handover":
        # The live owner hands over in one transaction (design §3.4 B). The
        # landed attachment owner must read it exactly as it reads a recovery.
        receipt = operation_store[1].suspend_composer_segment(original)
        assert (receipt.previous_generation, receipt.generation) == (
            original.claim.ownership_generation,
            original.claim.ownership_generation + 1,
        )
        current = new_owners(operation_store)
        _engine, repository, authority, sessions, sid = current
    else:
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
        claim, owner_instance_id=authority._owner_instance_id, lease_seconds=OWNER_LEASE_SECONDS, auth_provider_type="local"
    )
    running = ComposerOperationRunning(claim=claim, session_operation_context=context)
    lifecycle_owns_context = False
    try:
        work = inventory_work(running)
        # This is the genuine newly started claim, not a private pause/restart
        # of the original context. Its positive scope has no no-write pair.
        lifecycle_owns_context = True
        async with await SessionOperationLease.adopt(
            repository, context, lease_seconds=OWNER_LEASE_SECONDS, required_work=work.coordinator
        ) as lease:
            assert lease.context is context and lease.required_work is work.coordinator
            selected, inventory = await lease.create_task(current_selected(current, running), name="attachment-fresh-current-selection")
            service, _actors = current_service(current)
            cold = await lease.create_task(
                restore_continuation_runtime(
                    service=service,
                    authority=authority,
                    selected=selected,
                    inventory=inventory,
                    required_work=work,
                    provider_owner=ProviderInvocationOwner(service=sessions, required_work=work),
                ),
                name="attachment-fresh-positive-cold-restore",
            )
            assert context.fence != original.session_operation_context.fence
            if point == "before":
                assert cold.graph.batch.batch.current_call.audit is None
                newctx = await lease.create_task(
                    build_runtime_tool_batch_context(cold, cancellation_requested=asyncio.Event()), name="attachment-fresh-tool-context"
                )
                applied = await lease.create_task(
                    apply_current_attachment_runtime(
                        runtime=cold,
                        selected=selected,
                        inventory=inventory,
                        ctx=newctx,
                        call_model=call_model,
                        effect_key=source.effect_key,
                    ),
                    name="attachment-fresh-application",
                )
                assert applied.application.application_operation_id == context.fence.operation_id
            else:
                replay = await lease.create_task(
                    read_current_applied_attachment(runtime=cold, selected=selected, inventory=inventory, effect_key=source.effect_key),
                    name="attachment-fresh-applied-read",
                )
                assert replay.application == applied.application
                assert replay.application.application_operation_id == original.session_operation_context.fence.operation_id
            assert len(sdk) == 1 and len(cold.live.recorder.invocations) == len(runtime.live.recorder.invocations) + (point == "before")
        assert lease.disposition is SessionOperationLeaseDisposition.RELEASED
        work.coordinator.assert_completed()
    finally:
        if not lifecycle_owns_context:
            repository.release(context)


async def _complete_prior_p4_with_live_lease(operation_store, arguments, runtime, ctx, call_model, source):
    """Close the actual positive phase before any new-claim corruption oracle."""
    running = arguments["inventory"].running
    work = arguments["required_work"]
    lifecycle_owns_context = False
    try:
        prior = runtime.graph.applications
        assert prior is not None and prior.applications
        # No SQL before/after no-write pair spans this renewing lifetime.
        # Adoption owns original-context cleanup even when its await fails.
        lifecycle_owns_context = True
        async with await SessionOperationLease.adopt(
            operation_store[1], running.session_operation_context, lease_seconds=OWNER_LEASE_SECONDS, required_work=work.coordinator
        ) as lease:
            assert lease.context is running.session_operation_context and lease.required_work is work.coordinator
            active = await lease.create_task(restore_continuation_runtime(**arguments), name="attachment-prior-p4-active-positive")
            assert active.graph.applications.applications == prior.applications
            result = await lease.create_task(
                apply_current_attachment_runtime(
                    runtime=runtime,
                    selected=arguments["selected"],
                    inventory=arguments["inventory"],
                    ctx=ctx,
                    call_model=call_model,
                    effect_key=source.effect_key,
                ),
                name="attachment-prior-p4-application",
            )
            latest = await lease.create_task(_latest(arguments), name="attachment-prior-p4-latest")
            cold = await lease.create_task(restore_continuation_runtime(**latest), name="attachment-prior-p4-completed-positive")
            assert cold.graph.applications.applications == prior.applications
            assert cold.graph.applications.graph_json != prior.graph_json
            assert cold.graph.batch.batch.next_tool_index == 1 and result.application.effect_key == source.effect_key
        assert lease.disposition is SessionOperationLeaseDisposition.RELEASED
        work.coordinator.assert_completed()
        # These are historical values only. A later current read must mint a
        # genuine new claim/context and read the current selected root again.
        return prior, result, latest, cold
    finally:
        if not lifecycle_owns_context:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("none", "graph", "receipt", "source"))
async def test_prior_real_p4_journal_survives_active_and_applied_attachment_and_keeps_all_old_sql_proofs(
    operation_store, tmp_path, monkeypatch, change
):
    """Whole cold positives, then same-claim SQL-boundary corruption oracles."""
    arguments, runtime, ctx, call_model, source, _audit, _sdk, _path = await application_case(
        operation_store, tmp_path, monkeypatch, post_p4=True
    )
    running = arguments["inventory"].running
    if change == "none":
        await _complete_prior_p4_with_live_lease(operation_store, arguments, runtime, ctx, call_model, source)
        return
    lifecycle_owns_context = False
    try:
        prior = runtime.graph.applications
        assert prior is not None and prior.applications
        # The lease owns this same context through every old positive phase.
        # Its real heartbeat keeps running during each later SQL transaction.
        lifecycle_owns_context = True
        async with await SessionOperationLease.adopt(
            operation_store[1],
            running.session_operation_context,
            lease_seconds=OWNER_LEASE_SECONDS,
            required_work=arguments["required_work"].coordinator,
        ) as lease:
            assert lease.context is running.session_operation_context
            assert lease.required_work is arguments["required_work"].coordinator
            active = await lease.create_task(restore_continuation_runtime(**arguments), name="same-claim-prior-active-cold")
            assert active.graph.applications.applications == prior.applications
            result = await lease.create_task(
                apply_current_attachment_runtime(
                    runtime=runtime,
                    selected=arguments["selected"],
                    inventory=arguments["inventory"],
                    ctx=ctx,
                    call_model=call_model,
                    effect_key=source.effect_key,
                ),
                name="same-claim-prior-application",
            )
            latest = await lease.create_task(_latest(arguments), name="same-claim-prior-current")
            cold = await lease.create_task(restore_continuation_runtime(**latest), name="same-claim-prior-completed-cold")
            assert cold.graph.applications.applications == prior.applications
            assert cold.graph.applications.graph_json != prior.graph_json
            assert cold.graph.batch.batch.next_tool_index == 1 and result.application.effect_key == source.effect_key
            await lease.create_task(_registered_runtime_sql_no_write(operation_store[0], latest), name="same-claim-prior-sql-positive")
            if change == "graph":
                selected = latest["selected"]
                top = selected.stack.frames[-1]
                changed = replace(
                    selected,
                    stack=selected.stack.model_copy(
                        update={"frames": (selected.stack.frames[0], top.model_copy(update={"next_tool_index": top.next_tool_index + 1}))}
                    ),
                )
                latest = {**latest, "selected": changed}
                message = "cold evidence changed the complete selected root"
            else:
                record = prior.applications[0]
                with locked_session_transaction(operation_store[0], str(operation_store[4])) as conn:
                    if change == "receipt":
                        changed = conn.execute(
                            update(effects)
                            .where(
                                effects.c.session_id == str(operation_store[4]),
                                effects.c.operation_id == running.claim.operation_id,
                                effects.c.effect_key == record.effect_key,
                            )
                            .values(receipt_key="missing-original-p4-receipt")
                        )
                        message = "application journal lost its actual committed effect"
                    else:
                        raw = b'{"coherently_changed_original_p4_source":true}'
                        changed = conn.execute(
                            update(chunks)
                            .where(
                                chunks.c.session_id == str(operation_store[4]),
                                chunks.c.operation_id == running.claim.operation_id,
                                chunks.c.chunk_key == f"value:{record.result.codec}:{record.result.digest}",
                            )
                            .values(payload=raw, byte_length=len(raw), sha256=sha256(raw).hexdigest())
                        )
                        message = "private chunk bytes differ from their binding"
                    assert changed.rowcount == 1
            # Scope is explicit: this negative exercises the real registered
            # SQL reader, not the whole asynchronous cold materialization.
            await lease.create_task(
                _registered_runtime_sql_no_write(operation_store[0], latest, error=message), name="same-claim-prior-sql-negative"
            )
        assert lease.disposition is SessionOperationLeaseDisposition.RELEASED
        arguments["required_work"].coordinator.assert_completed()
    finally:
        if not lifecycle_owns_context:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_attachment_application_rolls_back_every_chunk_and_event_preserving_original_failure(operation_store, tmp_path, monkeypatch):
    from elspeth.web.sessions import continuation_attachment_application as sql_owner

    arguments, runtime, ctx, call_model, source, _audit, _sdk, _path = await application_case(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    before = recovery_rows(operation_store[0])
    original = ValueError("actual attachment successor write failure")
    observed = []

    def fail_after_chunks(*args, **kwargs):
        observed.append(kwargs)
        raise original

    monkeypatch.setattr(sql_owner, "append_complete_snapshot", fail_after_chunks)
    try:
        with pytest.raises(ValueError) as caught:
            await apply_current_attachment_runtime(
                runtime=runtime,
                selected=arguments["selected"],
                inventory=arguments["inventory"],
                ctx=ctx,
                call_model=call_model,
                effect_key=source.effect_key,
            )
        assert caught.value is original and len(observed) == 1
        assert recovery_rows(operation_store[0]) == before
        # The original live invocation remains real; no reconstructed exception
        # or pretend rollback of in-memory work is offered as a retry owner.
        assert runtime.graph.batch.batch.next_tool_index == 1 and len(runtime.live.recorder.invocations) == 1
        assert (await read_attachment_result(**_read_args(arguments))).history_applied
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("none", "native_wrong_owner", "read_wrong_owner", "dual_source"))
async def test_actual_mixed_attachment_and_native_prefix_keeps_both_original_source_proofs(operation_store, tmp_path, monkeypatch, fault):
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases

    arguments, runtime, ctx, call_model, source, _audit, sdk, _path = await application_case(
        operation_store, tmp_path, monkeypatch, following_native=True
    )
    running = arguments["inventory"].running
    owns_lease = False
    try:
        await apply_current_attachment_runtime(
            runtime=runtime,
            selected=arguments["selected"],
            inventory=arguments["inventory"],
            ctx=ctx,
            call_model=call_model,
            effect_key=source.effect_key,
        )
        latest = await _latest(arguments)
        owns_lease = True
        async with await SessionOperationLease.adopt(
            operation_store[1],
            running.session_operation_context,
            lease_seconds=OWNER_LEASE_SECONDS,
            required_work=arguments["required_work"].coordinator,
        ) as lease:
            cold = await lease.create_task(restore_continuation_runtime(**latest), name="mixed-prefix-original-attachment-cold")
            assert cold.graph.batch is not None and cold.graph.batch.batch.next_tool_index == 1
            owner = ContinuationLivePhases(
                arguments["service"], arguments["authority"], arguments["required_work"], cold.bindings.provider_owner, lease
            )
            owner.bind_initial(cold)
            await lease.create_task(
                owner.service.advance_continuation_phase(
                    owner=owner,
                    authority=owner.authority,
                    selected=latest["selected"],
                    work=owner.work,
                    provider_owner=owner.provider_owner,
                ),
                name="mixed-prefix-actual-native-completion",
            )
            assert owner.runtime is not None and owner.runtime.graph.batch is not None
            data = owner.runtime.graph.batch
            assert data.batch.next_tool_index == 2 and len(data.batch.tool_outcomes) == 2 and data.batch.current_call is None
            latest = await _latest(latest)
            await lease.create_task(_registered_runtime_sql_no_write(operation_store[0], latest), name="mixed-prefix-positive-source-read")
            response = latest["selected"].stack.frames[-1].response
            native_key, read_key = f"tool:{response.digest}:1", f"attachment:{response.digest}:0"
            with operation_store[0].begin() as conn:
                native_row = conn.execute(
                    select(effects).where(
                        effects.c.session_id == str(operation_store[4]),
                        effects.c.operation_id == running.claim.operation_id,
                        effects.c.effect_key == native_key,
                    )
                ).one()
                read_row = conn.execute(
                    select(effects).where(
                        effects.c.session_id == str(operation_store[4]),
                        effects.c.operation_id == running.claim.operation_id,
                        effects.c.effect_key == read_key,
                    )
                ).one()
                assert native_row.owner == "tool_dispatch" and read_row.owner == "read"
                assert native_row.disposition == read_row.disposition == "committed"
                if fault == "native_wrong_owner":
                    conn.execute(
                        update(effects)
                        .where(
                            effects.c.session_id == native_row.session_id,
                            effects.c.operation_id == native_row.operation_id,
                            effects.c.effect_key == native_key,
                        )
                        .values(owner="read")
                    )
                elif fault == "read_wrong_owner":
                    conn.execute(
                        update(effects)
                        .where(
                            effects.c.session_id == read_row.session_id,
                            effects.c.operation_id == read_row.operation_id,
                            effects.c.effect_key == read_key,
                        )
                        .values(owner="tool_dispatch")
                    )
                elif fault == "dual_source":
                    conn.execute(insert(effects).values(**{**dict(native_row._mapping), "effect_key": f"tool:{response.digest}:0"}))
            if fault != "none":
                expected = {
                    "native_wrong_owner": "runtime native prefix lost its actual issued source",
                    "read_wrong_owner": "attachment effect lost its actual selected read owner/frontier",
                    "dual_source": "runtime prefix has missing or dual completed source ownership",
                }[fault]
                await lease.create_task(
                    _registered_runtime_sql_no_write(operation_store[0], latest, error=expected), name="mixed-prefix-source-opposition"
                )
            assert len(sdk) == 1
        arguments["required_work"].coordinator.assert_completed()
    finally:
        if not owns_lease:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ("before", "after"))
async def test_historical_attachment_requires_own_original_audit_after_actual_recorder_progress(
    operation_store, tmp_path, monkeypatch, role
):
    from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot

    arguments, runtime, ctx, call_model, source, _audit, sdk, _path = await application_case(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    owns_lease = False
    try:
        applied = await apply_current_attachment_runtime(
            runtime=runtime,
            selected=arguments["selected"],
            inventory=arguments["inventory"],
            ctx=ctx,
            call_model=call_model,
            effect_key=source.effect_key,
        )
        latest = await _latest(arguments)
        owns_lease = True
        async with await SessionOperationLease.adopt(
            operation_store[1],
            running.session_operation_context,
            lease_seconds=OWNER_LEASE_SECONDS,
            required_work=arguments["required_work"].coordinator,
        ) as lease:
            cold = await lease.create_task(restore_continuation_runtime(**latest), name="historical-audit-original-positive")
            cold.live.recorder.record_withheld_reply("planner_prose_unadmitted", "Actual later private recorder observation")
            captured = await lease.create_task(
                capture_continuation_runtime_owners(cold, selected=latest["selected"], inventory=latest["inventory"]),
                name="historical-audit-later-real-capture",
            )
            latest = await install_capture(operation_store, latest, captured)
            await lease.create_task(
                _registered_runtime_sql_no_write(operation_store[0], latest), name="historical-audit-after-progress-positive"
            )
            compose = latest["selected"].stack.frames[0]
            actual = next(chunk for chunk in latest["selected"].chunks if chunk.reference == compose.audit_buffer)
            original_fields = TurnEvidenceSnapshot.model_validate_json(
                applied.application.before_evidence_json if role == "before" else applied.application.after_evidence_json
            )
            original = original_fields.recorder
            assert original != actual.reference and len(sdk) == 1
            # Deliberate original-row substitution: current audit remains valid,
            # but cannot stand in for the historical role's original dependency.
            with operation_store[0].begin() as conn:
                changed = conn.execute(
                    update(chunks)
                    .where(
                        chunks.c.session_id == str(operation_store[4]),
                        chunks.c.operation_id == running.claim.operation_id,
                        chunks.c.chunk_key == f"value:{original.codec}:{original.digest}",
                    )
                    .values(payload=actual.canonical.encode(), byte_length=actual.reference.byte_length, sha256=actual.reference.digest)
                )
                assert changed.rowcount == 1
            await lease.create_task(
                _registered_runtime_sql_no_write(operation_store[0], latest, error="SQL binding mismatch"),
                name="historical-audit-original-source-opposition",
            )
            assert len(sdk) == 1
        arguments["required_work"].coordinator.assert_completed()
    finally:
        if not owns_lease:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("effect", "version"))
async def test_attachment_finisher_refuses_before_invocation_when_current_selector_or_blob_changed(
    operation_store, tmp_path, monkeypatch, change
):
    arguments, runtime, ctx, call_model, source, _audit, _sdk, _path = await application_case(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        assert source.history_applied and source.retained.blob_versions
        effect_key = source.effect_key
        if change == "effect":
            effect_key += ":wrong"
        else:
            observed = source.retained.blob_versions[0]
            with operation_store[0].begin() as conn:
                result = conn.execute(
                    update(blobs_table)
                    .where(blobs_table.c.id == str(observed.blob_id), blobs_table.c.session_id == str(operation_store[4]))
                    .values(filename="changed-after-publication.txt")
                )
                assert result.rowcount == 1
        before = recovery_rows(operation_store[0])
        previous = runtime.live.recorder.invocations
        with pytest.raises(AuditIntegrityError):
            await apply_current_attachment_runtime(
                runtime=runtime,
                selected=arguments["selected"],
                inventory=arguments["inventory"],
                ctx=ctx,
                call_model=call_model,
                effect_key=effect_key,
            )
        assert runtime.live.recorder.invocations == previous and runtime.graph.batch.batch.current_call is not None
        assert recovery_rows(operation_store[0]) == before
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("receipt", "message", "iteration", "cache", "creator"))
async def test_complete_attachment_application_data_refuses_one_coherent_changed_owner_after_positive(
    operation_store, tmp_path, monkeypatch, change
):
    from elspeth.contracts.json_parser import parse_json_strict
    from elspeth.web.composer.continuation_attachment_application import admit_attachment_application
    from elspeth.web.composer.continuation_evidence import encode_turn_evidence
    from elspeth.web.composer.continuation_tool_receipts import decode_retained_tool_receipt, encode_retained_tool_receipt

    arguments, runtime, ctx, call_model, source, _audit, _sdk, _path = await application_case(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    names = arguments["inventory"].names
    lifecycle_owns_context = False
    try:
        # The actual SQL application is live work. Finish and release it before
        # the subsequent pure DATA mutation/refusal checks begin.
        lifecycle_owns_context = True
        async with await SessionOperationLease.adopt(
            operation_store[1],
            running.session_operation_context,
            lease_seconds=OWNER_LEASE_SECONDS,
            required_work=arguments["required_work"].coordinator,
        ) as lease:
            assert lease.context is running.session_operation_context
            assert lease.required_work is arguments["required_work"].coordinator
            stored = await lease.create_task(
                apply_current_attachment_runtime(
                    runtime=runtime,
                    selected=arguments["selected"],
                    inventory=arguments["inventory"],
                    ctx=ctx,
                    call_model=call_model,
                    effect_key=source.effect_key,
                ),
                name="attachment-data-positive-application",
            )
        assert lease.disposition is SessionOperationLeaseDisposition.RELEASED
        arguments["required_work"].coordinator.assert_completed()
        value = stored.application
        from elspeth.web.composer.continuation_audit_buffer import encode_recorder_snapshot

        # The actual live completion has not advanced again. Retain its exact
        # source beside the original selected before-source for this DATA check.
        actual_after_audit = encode_recorder_snapshot(runtime.live.recorder.snapshot_for_continuation(drained=runtime.drained))
        original_sources = (*arguments["selected"].chunks, actual_after_audit)
        admit_attachment_application(value, env_ref_names=names, source_chunks=original_sources)
        if change == "receipt":
            receipt = decode_retained_tool_receipt(value.receipt_json, env_ref_names=names)
            # A valid receipt for another effect still cannot replace this one.
            changed = receipt.model_copy(update={"effect_key": receipt.effect_key + ":other"})
            value = value.model_copy(update={"receipt_json": encode_retained_tool_receipt(changed, env_ref_names=names)})
        elif change == "message":
            evidence = decode_turn_evidence(
                encode_private_chunk(codec="composer.evidence.v2", canonical=value.after_evidence_json, env_ref_names=names),
                env_ref_names=names,
                source_chunks=original_sources,
            )
            shelf = evidence.snapshot.transcript
            last = shelf.current_indexes[-1]
            changed_messages = list(shelf.messages_json)
            wire, error = parse_json_strict(changed_messages[last])
            assert error is None and type(wire) is dict
            wire["content"] = "changed ordinary retained tool prose"
            changed_messages[last] = canonical_json(wire)
            changed = evidence.snapshot.model_copy(
                update={"transcript": shelf.model_copy(update={"messages_json": tuple(changed_messages)})}
            )
            fresh = admit_turn_evidence(changed, env_ref_names=names, source_chunks=evidence.dependency_chunks)
            value = value.model_copy(update={"after_evidence_json": encode_turn_evidence(fresh, env_ref_names=names).canonical})
        elif change == "iteration":
            raw, error = parse_json_strict(value.after_iteration_json)
            assert error is None and type(raw) is dict
            # Complete actual owner field, not an unknown-key rejection.
            raw["anti_anchor"] = {"failures": [["read_attachment", "a" * 64]], "categories": [None]}
            value = value.model_copy(update={"after_iteration_json": canonical_json(raw)})
        elif change == "cache":
            value = value.model_copy(update={"after_caches_json": value.before_caches_json})
        else:
            value = value.model_copy(update={"application_operation_id": "another-ordinary-creator"})
        with pytest.raises((AuditIntegrityError, ValueError)):
            admit_attachment_application(value, env_ref_names=names, source_chunks=original_sources)
    finally:
        if not lifecycle_owns_context:
            operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_concurrent_successor_cas_consumes_one_completed_projection(operation_store, tmp_path, monkeypatch):
    import threading

    from elspeth.web.sessions.continuation_attachment_application import StoredAttachmentApplication

    arguments, runtime, ctx, call_model, source, _audit, _sdk, _path = await application_case(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    try:
        completed = await finisher.finish_current_attachment_result(
            **_read_args(arguments), service=arguments["service"], ctx=ctx, call_model=call_model, restored=runtime.graph
        )
        captured = await capture_continuation_runtime_owners(runtime, selected=arguments["selected"], inventory=arguments["inventory"])
        barrier = threading.Barrier(2)

        def actual_writer():
            barrier.wait(timeout=10)
            return arguments["authority"].apply_attachment_application(
                running, selected=arguments["selected"], inventory=arguments["inventory"], projection=completed, captured=captured
            )

        # This is the actual locked synchronous facade race, not concurrent B2
        # mutation of one live graph or a fabricated second invocation.
        values = await asyncio.gather(asyncio.to_thread(actual_writer), asyncio.to_thread(actual_writer), return_exceptions=True)
        successes = [value for value in values if type(value) is StoredAttachmentApplication]
        failures = [value for value in values if isinstance(value, BaseException)]
        assert len(successes) == 1 and len(failures) == 1
        assert isinstance(failures[0], (AuditIntegrityError, ContinuationCASConflict))
        assert successes[0].snapshot.cursor == arguments["selected"].stack.cursor + 1
        assert len(runtime.live.recorder.invocations) == 1
        latest = await _latest(arguments)
        replay = await read_current_applied_attachment(
            runtime=runtime, selected=latest["selected"], inventory=latest["inventory"], effect_key=source.effect_key
        )
        assert replay.application == successes[0].application
        before = recovery_rows(operation_store[0])
        with pytest.raises((AuditIntegrityError, ContinuationCASConflict)):
            await asyncio.to_thread(
                arguments["authority"].apply_attachment_application,
                running,
                selected=arguments["selected"],
                inventory=arguments["inventory"],
                projection=completed,
                captured=captured,
            )
        assert recovery_rows(operation_store[0]) == before
    finally:
        arguments["required_work"].coordinator.assert_completed()
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ("stale", "foreign"))
async def test_warm_attachment_current_history_refuses_stale_or_foreign_selection_before_finish(
    operation_store, tmp_path, monkeypatch, selection
):
    arguments, runtime, ctx, call_model, source, _audit, sdk, _path = await application_case(operation_store, tmp_path, monkeypatch)
    running = arguments["inventory"].running
    original_graph, original_live = runtime.graph, runtime.live
    before = recovery_rows(operation_store[0])
    selected = runtime.source.selected if selection == "stale" else replace(arguments["selected"], checkpoint_id=str(uuid4()))
    assert selected.checkpoint_id != arguments["selected"].checkpoint_id

    async def forbidden(**_arguments):
        raise AssertionError("invalid current history cannot reach the B2 finisher")

    monkeypatch.setattr(driver, "finish_current_attachment_result", forbidden)
    try:
        with pytest.raises(AuditIntegrityError):
            await apply_current_attachment_runtime(
                runtime=runtime,
                selected=selected,
                inventory=arguments["inventory"],
                ctx=ctx,
                call_model=call_model,
                effect_key=source.effect_key,
            )
        assert runtime.graph is original_graph and runtime.live is original_live
        assert len(sdk) == 1 and recovery_rows(operation_store[0]) == before
        arguments["required_work"].coordinator.assert_completed()
    finally:
        operation_store[1].release(running.session_operation_context)
