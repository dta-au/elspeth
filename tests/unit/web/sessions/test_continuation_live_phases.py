"""Actual V2 paid response → tool → owed Stop prefix source controls."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any

import litellm
import pytest
from litellm import ModelResponse
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import ToolBatchFrame
from elspeth.web.composer.continuation_clock import freeze_observation
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import (
    ContinuationEnvSelection,
    CurrentContinuationEnvInventory,
    _read_current_env_names,
)
from elspeth.web.composer.continuation_finish_reads import ContinuationFinishRead
from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases, _require_runtime_source_refresh
from elspeth.web.composer.continuation_primary import run_continuation_primary
from elspeth.web.composer.continuation_runtime import restore_continuation_runtime
from elspeth.web.composer.continuation_selection import load_selected_continuation
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_quota import ProviderInvocationOwner
from elspeth.web.composer.tools.wire_projection import encode_semantic_arguments
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import ContinuationLeaseAdoption
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_primary_result import _assert_required_guard_admitted, initialized_primary_input, stored_rows

operation_store = _operation_store


@asynccontextmanager
async def _phase_case(store, tmp_path, monkeypatch, *, tool_call: tuple[str, str, dict[str, Any]] | None = None):
    runtime, work, requests = await initialized_primary_input(store, tmp_path, monkeypatch)
    service, authority = runtime.materialization.service, store[2]
    running = work.running
    assert running is not None
    provider = runtime.bindings.provider_owner
    assert type(provider) is ProviderInvocationOwner
    adoption = ContinuationLeaseAdoption(store[1], running.session_operation_context, lease_seconds=30, required_work=work.coordinator)
    lease = await adoption.join()
    sdk_calls = []
    try:
        owner = ContinuationLivePhases(service, authority, work, provider, lease)
        owner.bind_initial(runtime)

        async def completion(**sdk_kwargs):
            _assert_required_guard_admitted(
                store[0], running.session_operation_context, requests, role="composer", user_prompt=runtime.bindings.message
            )
            sdk_calls.append(sdk_kwargs)
            tool_calls = (
                [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "get_pipeline_state",
                            "arguments": canonical_json(encode_semantic_arguments("get_pipeline_state", service._planner_dialect, {})),
                        },
                    }
                    for call_id in ("first_read", "second_read")
                ]
                if tool_call is None
                else [
                    {
                        "id": tool_call[0],
                        "type": "function",
                        "function": {
                            "name": tool_call[1],
                            "arguments": canonical_json(encode_semantic_arguments(tool_call[1], service._planner_dialect, tool_call[2])),
                        },
                    }
                ]
            )
            return ModelResponse(
                model="test/model",
                choices=[
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "Inspecting the existing state.",
                            "tool_calls": tool_calls,
                        },
                    }
                ],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            )

        monkeypatch.setattr(litellm, "acompletion", completion)
        _messages, _tools, delivery, _delivery_input = service._prepare_primary_delivery(
            messages=runtime.live.llm_messages,
            tools=runtime.live.tools,
            recorder=runtime.live.recorder,
            turn_evidence=runtime.live.turn_evidence,
            state=runtime.live.state,
            failed_turn=runtime.live.failed_turn,
        )
        with prompt_guard_user_scope(runtime.bindings.message):
            primary = await run_continuation_primary(
                service=service,
                authority=authority,
                selected=runtime.source.selected,
                producers=runtime.materialization,
                messages=runtime.live.llm_messages,
                tools=runtime.live.tools,
                recorder=runtime.live.recorder,
                drained=runtime.drained,
                required_work=work,
                provider_owner=provider,
                timeout=5,
            )
        if delivery is not None:
            runtime.live.turn_evidence.confirm_delivery(delivery, runtime.live.recorder.llm_calls)
        owner.accept_primary(primary)
        selected = await load_selected_continuation(authority=authority, running=running, selected=primary.snapshot, required_work=work)
        await service.advance_continuation_phase(owner=owner, authority=authority, selected=selected, work=work, provider_owner=provider)
        assert owner.runtime.bindings is runtime.bindings and owner.runtime.live is runtime.live
        assert owner.runtime.graph.evidence is runtime.graph.evidence and owner.runtime.graph.recorder is runtime.graph.recorder
        assert owner.runtime.graph.messages is runtime.graph.messages and owner.runtime.materialization is runtime.materialization
        yield owner, running, primary, sdk_calls
    finally:
        await lease.close()


async def _advance(owner):
    assert owner.runtime is not None
    selected = owner.runtime.source.selected
    await owner.service.advance_continuation_phase(
        owner=owner, authority=owner.authority, selected=selected, work=owner.work, provider_owner=owner.provider_owner
    )


async def _finish_inventory(owner, running):
    selected = owner.runtime.source.selected
    names = await _read_current_env_names(owner.service._advisor_checkpoint, selected.stack.logical.session_id)
    return selected, CurrentContinuationEnvInventory(
        running,
        ContinuationEnvSelection(selected.checkpoint_id, selected.stack.cursor, encode_continuation_snapshot(selected.stack)),
        names,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ("none", "selected", "bootstrap", "sequence", "generation", "watermark", "hard_extension", "cooperative_extension", "backwards"),
)
async def test_current_sql_refresh_preserves_source_and_absolute_clocks(operation_store, tmp_path, monkeypatch, change):
    runtime, work, _requests = await initialized_primary_input(operation_store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None
    try:
        source = runtime.source
        actual = operation_store[2].read_continuation_runtime(
            running, selected=source.selected, inventory=runtime.materialization.inventory
        )
        before = stored_rows(operation_store[0])
        live_messages = runtime.live.llm_messages
        live_graph = runtime.graph
        _require_runtime_source_refresh(source, actual)
        assert actual.timing.observed_at >= source.timing.observed_at
        if change == "none":
            assert runtime.live.llm_messages is live_messages and runtime.graph is live_graph
            assert stored_rows(operation_store[0]) == before
            return
        if change == "selected":
            changed = replace(actual, selected=replace(actual.selected, checkpoint_id="00000000-0000-0000-0000-000000000000"))
        elif change == "bootstrap":
            changed = replace(actual, bootstrap=actual.bootstrap.model_copy(update={"actor_user_id": "foreign-actor"}))
        else:
            timing = actual.timing
            if change == "sequence":
                timing = replace(timing, sequence=timing.sequence + 1)
            elif change == "generation":
                timing = replace(timing, generation=timing.generation + 1)
            elif change == "watermark":
                timing = replace(timing, watermark_seconds=timing.watermark_seconds + 1)
            elif change == "hard_extension":
                timing = replace(timing, hard_remaining_seconds=timing.hard_remaining_seconds + 1)
            elif change == "cooperative_extension":
                timing = replace(
                    timing,
                    observed_at=timing.observed_at + timedelta(seconds=1),
                    hard_remaining_seconds=timing.hard_remaining_seconds - 1,
                )
            else:
                assert change == "backwards"
                timing = replace(timing, observed_at=source.timing.observed_at - timedelta(seconds=1))
            changed = replace(actual, timing=timing)
        with pytest.raises(AuditIntegrityError, match="live phase refresh"):
            _require_runtime_source_refresh(source, changed)
        assert runtime.source is source and runtime.live.llm_messages is live_messages and runtime.graph is live_graph
        assert stored_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_sql_source_accepts_strictly_elapsed_microsecond_observation(operation_store, tmp_path, monkeypatch):
    runtime, work, _requests = await initialized_primary_input(operation_store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None
    try:
        source = runtime.source
        actual = operation_store[2].read_continuation_runtime(
            running, selected=source.selected, inventory=runtime.materialization.inventory
        )
        before = stored_rows(operation_store[0])
        bindings, live, graph, materialization = runtime.bindings, runtime.live, runtime.graph, runtime.materialization
        _require_runtime_source_refresh(source, actual)
        timing = actual.timing
        hard_at = timing.observed_at + timedelta(seconds=timing.hard_remaining_seconds)
        cooperative_at = timing.observed_at + timedelta(seconds=timing.cooperative_remaining_seconds)
        assert hard_at == source.timing.observed_at + timedelta(seconds=source.timing.hard_remaining_seconds)
        assert cooperative_at == source.timing.observed_at + timedelta(seconds=source.timing.cooperative_remaining_seconds)
        # SQLite's real reads may share one whole-second observation. Retain
        # that actual SQL positive, then deterministically advance only owned
        # observation DATA using the same original database deadlines.
        observed_at = timing.observed_at + timedelta(seconds=1, microseconds=137)
        assert observed_at < cooperative_at < hard_at
        later = replace(
            actual,
            timing=freeze_observation(
                sequence=timing.sequence,
                generation=timing.generation,
                now=observed_at,
                deadline=hard_at,
                cooperative=cooperative_at,
                watermark=timing.watermark_seconds,
            ),
        )
        assert later.timing.observed_at > timing.observed_at and later.timing.observed_at.microsecond != timing.observed_at.microsecond
        assert later.timing.hard_remaining_seconds < timing.hard_remaining_seconds
        assert later.timing.cooperative_remaining_seconds < timing.cooperative_remaining_seconds
        assert later.timing.observed_at + timedelta(seconds=later.timing.hard_remaining_seconds) == hard_at
        assert later.timing.observed_at + timedelta(seconds=later.timing.cooperative_remaining_seconds) == cooperative_at
        assert later != actual and replace(later, timing=timing) == actual
        _require_runtime_source_refresh(actual, later)
        assert runtime.source is source and runtime.bindings is bindings and runtime.live is live
        assert runtime.graph is graph and runtime.materialization is materialization
        assert stored_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_current_sql_source_refuses_hard_absolute_extension_with_nonincreasing_remaining(operation_store, tmp_path, monkeypatch):
    runtime, work, _requests = await initialized_primary_input(operation_store, tmp_path, monkeypatch)
    running = work.running
    assert running is not None
    try:
        actual = operation_store[2].read_continuation_runtime(
            running, selected=runtime.source.selected, inventory=runtime.materialization.inventory
        )
        _require_runtime_source_refresh(runtime.source, actual)
        before = stored_rows(operation_store[0])
        timing = actual.timing
        hard_at = timing.observed_at + timedelta(seconds=timing.hard_remaining_seconds)
        cooperative_at = timing.observed_at + timedelta(seconds=timing.cooperative_remaining_seconds)
        observed_at = timing.observed_at + timedelta(seconds=2, microseconds=271)
        assert observed_at < cooperative_at < hard_at
        changed = replace(
            actual,
            timing=freeze_observation(
                sequence=timing.sequence,
                generation=timing.generation,
                now=observed_at,
                deadline=hard_at + timedelta(seconds=1),
                cooperative=cooperative_at,
                watermark=timing.watermark_seconds,
            ),
        )
        assert replace(changed, timing=timing) == actual
        assert changed.timing.observed_at > timing.observed_at
        assert 0 < changed.timing.hard_remaining_seconds <= timing.hard_remaining_seconds
        assert 0 < changed.timing.cooperative_remaining_seconds <= timing.cooperative_remaining_seconds
        assert changed.timing.observed_at + timedelta(seconds=changed.timing.cooperative_remaining_seconds) == cooperative_at
        assert changed.timing.observed_at + timedelta(seconds=changed.timing.hard_remaining_seconds) == hard_at + timedelta(seconds=1)
        with pytest.raises(AuditIntegrityError, match="extended an original absolute deadline"):
            _require_runtime_source_refresh(actual, changed)
        assert stored_rows(operation_store[0]) == before
    finally:
        operation_store[1].release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_tool_issue_keeps_prepared_data_pure_and_reproves_completed_prefix(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data, encode_restored_prepared_batch_data
    from elspeth.web.composer.continuation_phase_transitions import read_tool_issue_source_on_connection
    from elspeth.web.sessions.continuation import read_chunk
    from elspeth.web.sessions.locking import locked_session_transaction

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        await _advance(owner)
        selected = owner.runtime.source.selected
        inventory = owner.runtime.materialization.inventory
        before = stored_rows(operation_store[0])
        current = owner.authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
        assert current.selected is selected
        key = f"tool:{primary.response.reference.digest}:0"
        with locked_session_transaction(operation_store[0], str(running.claim.session_id)) as conn:
            issue = read_tool_issue_source_on_connection(conn, running, effect_key=key, env_ref_names=inventory.names)
            encoded = read_chunk(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                key=f"value:{issue.prepared.codec}:{issue.prepared.digest}",
            ).decode("utf-8", errors="strict")
        prepared = decode_prepared_batch_data(encoded, env_ref_names=inventory.names)
        call = prepared.batch.current_call
        assert call is not None and call.effect_key is None and call.tool_call.id == issue.tool_call_id
        assert encode_restored_prepared_batch_data(prepared, env_ref_names=inventory.names) == encoded
        # The SQL issue proof is separate from pure prepared DATA. The same
        # protected codec must still reject a later owner field injected here.
        call.effect_key = key
        with pytest.raises(AuditIntegrityError, match="later owner result or receipt"):
            encode_restored_prepared_batch_data(prepared, env_ref_names=inventory.names)
        assert owner.runtime.graph.batch.calls is primary.outcome.completion.tool_batch.calls
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("missing_effect", "receipt", "issued_input", "creator", "event_cursor", "call"))
async def test_actual_next_tool_refuses_missing_or_counterfeit_completed_prefix(operation_store, tmp_path, monkeypatch, change):
    import json

    from sqlalchemy import delete, update

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        await _advance(owner)
        selected = owner.runtime.source.selected
        inventory = owner.runtime.materialization.inventory
        owner.authority.read_continuation_runtime(running, selected=selected, inventory=inventory)
        with operation_store[0].begin() as conn:
            row = conn.execute(select(effects).where(effects.c.owner == "tool_dispatch")).one()
            where = (
                effects.c.session_id == str(running.claim.session_id),
                effects.c.operation_id == running.claim.operation_id,
                effects.c.effect_key == row.effect_key,
            )
            if change == "missing_effect":
                conn.execute(delete(effects).where(*where))
            elif change == "receipt":
                conn.execute(update(effects).where(*where).values(receipt_key="tool-result:" + "0" * 64))
            elif change == "issued_input":
                conn.execute(update(effects).where(*where).values(input_sha256="0" * 64))
            else:
                descriptor = json.loads(row.action_json)
                if change == "creator":
                    descriptor["creator_operation_epoch"] += 1
                elif change == "event_cursor":
                    descriptor["cursor"] += 1
                else:
                    assert change == "call"
                    descriptor["tool_call_id"] += "_foreign"
                conn.execute(update(effects).where(*where).values(action_json=canonical_json(descriptor)))
        malformed = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError):
            await _advance(owner)
        assert stored_rows(operation_store[0]) == malformed and len(sdk) == 1
        assert owner.runtime.source.selected is selected
        assert owner.runtime.graph.batch.calls is primary.outcome.completion.tool_batch.calls


@pytest.mark.asyncio
async def test_actual_v2_tool_phase_preserves_live_paid_objects_and_commits_the_issued_result(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer import tool_batch
    from elspeth.web.composer.continuation_message_families import family_message_wire
    from elspeth.web.composer.continuation_message_family_recipes import ToolResultMessageRecipe
    from elspeth.web.composer.continuation_prompt_producers import CurrentContinuationMessageProducers

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        produced = []
        registered = []
        actual_factory = tool_batch.tool_result_message
        actual_register = CurrentContinuationMessageProducers.register_tool_result_message

        def factory(**kwargs):
            message = actual_factory(**kwargs)
            produced.append((message, kwargs["result"], kwargs["serialized"]))
            return message

        def register(self, **kwargs):
            assert produced and kwargs["message"] is produced[-1][0]
            assert kwargs["result"] is produced[-1][1] and kwargs["serialized"] is produced[-1][2]
            message = actual_register(self, **kwargs)
            assert message is kwargs["message"]
            registered.append(message)
            return message

        monkeypatch.setattr(tool_batch, "tool_result_message", factory)
        monkeypatch.setattr(CurrentContinuationMessageProducers, "register_tool_result_message", register)
        original = owner.runtime.graph.batch
        assert original.calls is primary.outcome.completion.tool_batch.calls
        await _advance(owner)
        assert len(sdk) == 1 and owner.primary is primary
        assert owner.runtime.graph.batch is original and original.batch.next_tool_index == 1
        assert original.batch.tool_outcomes[0].call is original.calls[0]
        assert owner.business_stage is not None and owner.business_stage.returned and owner.business_stage.observed
        with operation_store[0].connect() as conn:
            row = conn.execute(select(effects).where(effects.c.owner == "tool_dispatch")).one()
        assert row.disposition == "committed" and row.result_chunk_key is not None
        assert row.creator_sequence == running.claim.segment_sequence and row.creator_generation == running.claim.ownership_generation
        assert type(owner.runtime.source.selected.stack.frames[-1]) is ToolBatchFrame
        assert len(produced) == len(registered) == 1
        message, _actual_result, serialized = produced[0]
        assert message is registered[0] and message["content"] is serialized
        assert any(item is message for item in owner.runtime.live.llm_messages)
        wire, recipe = owner.runtime.materialization.capture_message(message)
        assert type(recipe) is ToolResultMessageRecipe
        assert (recipe.response, recipe.tool_index, recipe.tool_call_id) == (primary.response.reference, 0, "first_read")
        before = stored_rows(operation_store[0])
        restored = await restore_continuation_runtime(
            service=owner.service,
            authority=owner.authority,
            selected=owner.runtime.source.selected,
            inventory=owner.runtime.materialization.inventory,
            required_work=owner.work,
            provider_owner=owner.provider_owner,
        )
        restored_message = next(item for item in restored.live.llm_messages if item.get("tool_call_id") == "first_read")
        assert restored_message is not message and family_message_wire(restored_message) == wire
        assert restored.materialization.capture_message(restored_message)[1] == recipe
        assert restored.graph.batch.batch.tool_outcomes[0].call is restored.graph.batch.calls[0]
        assert owner.runtime.graph.batch.calls is primary.outcome.completion.tool_batch.calls
        assert message is registered[0] and message["content"] is serialized
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    (
        "equal_copy",
        "body_identity",
        "body_changed",
        "partition_tuple",
        "partition_structure",
        "partition_trust",
        "partition_inspection",
        "missing_partition",
    ),
)
async def test_actual_tool_result_capture_refuses_changed_live_provenance(operation_store, tmp_path, monkeypatch, change):
    from elspeth.contracts.prompt_guard import PROMPT_GUARD_CONTENT_PARTS_KEY, PromptGuardContentPart, PromptGuardTrust
    from elspeth.web.composer.continuation_message_family_recipes import ToolResultMessageRecipe

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        await _advance(owner)
        materialization = owner.runtime.materialization
        message = next(item for item in owner.runtime.live.llm_messages if item.get("tool_call_id") == "first_read")
        original = materialization.capture_message(message)
        assert type(original[1]) is ToolResultMessageRecipe
        before = stored_rows(operation_store[0])
        content, parts = message["content"], message[PROMPT_GUARD_CONTENT_PARTS_KEY]
        facts = tuple((part, part.text, part.trust, part.inspection_text) for part in parts)
        assert parts and all(part.trust is None for part in parts)
        target = message
        try:
            if change == "equal_copy":
                target = dict(message)
                assert target == message and target is not message
            elif change == "body_identity":
                replacement = content.encode("utf-8").decode("utf-8")
                assert replacement == content and replacement is not content
                message["content"] = replacement
            elif change == "body_changed":
                message["content"] = content + " "
                message[PROMPT_GUARD_CONTENT_PARTS_KEY] = (PromptGuardContentPart(message["content"]),)
            elif change == "partition_tuple":
                replacement = tuple(part for part in parts)
                assert replacement == parts and replacement is not parts
                message[PROMPT_GUARD_CONTENT_PARTS_KEY] = replacement
            elif change == "partition_structure":
                # The real fixture may produce one complete JSON unit. Add an
                # empty untrusted unit: complete wire stays equal while the
                # ordered producer partition structure changes.
                replacement = (PromptGuardContentPart(""), *parts)
                message[PROMPT_GUARD_CONTENT_PARTS_KEY] = replacement
            elif change == "partition_trust":
                # Deliberate frozen-object fault: the tuple and part identities
                # stay unchanged, so the recorded immutable values must decide.
                object.__setattr__(parts[0], "trust", PromptGuardTrust.REFERENCE)
            elif change == "partition_inspection":
                object.__setattr__(parts[0], "inspection_text", "Changed detector view")
            else:
                assert change == "missing_partition"
                message.pop(PROMPT_GUARD_CONTENT_PARTS_KEY)
            match = "guarded/native message" if change == "equal_copy" else "tool result capture"
            with pytest.raises(AuditIntegrityError, match=match):
                materialization.capture_message(target)
        finally:
            message["content"] = content
            message[PROMPT_GUARD_CONTENT_PARTS_KEY] = parts
            for part, text, trust, inspection in facts:
                object.__setattr__(part, "text", text)
                object.__setattr__(part, "trust", trust)
                object.__setattr__(part, "inspection_text", inspection)
        assert materialization.capture_message(message) == original
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ("response", "index", "call", "wire"))
async def test_actual_tool_result_restore_refuses_foreign_selector_or_body(operation_store, tmp_path, monkeypatch, change):
    from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies, family_message_wire

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        await _advance(owner)
        runtime = owner.runtime
        message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == "first_read")
        wire, recipe = runtime.materialization.capture_message(message)
        families = CurrentContinuationMessageFamilies(
            owner.service, owner.authority, runtime.source.selected, runtime.materialization.inventory
        )
        before = stored_rows(operation_store[0])
        restored = families.restore_messages(((recipe, wire),))
        assert len(restored) == 1 and family_message_wire(restored[0]) == wire
        if change == "response":
            altered = recipe.model_copy(update={"response": recipe.response.model_copy(update={"digest": "0" * 64})})
        elif change == "index":
            altered = recipe.model_copy(update={"tool_index": 1})
        elif change == "call":
            altered = recipe.model_copy(update={"tool_call_id": "second_read"})
        else:
            assert change == "wire"
            altered = recipe
            wire = wire + " "
        with pytest.raises(AuditIntegrityError):
            families.restore_messages(((altered, wire),))
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        assert runtime.materialization.capture_message(message)[1] == recipe


@pytest.mark.asyncio
async def test_private_tool_result_capture_does_not_admit_a_rolled_back_completion(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer import continuation_phase_transitions
    from elspeth.web.composer.continuation_message_families import CurrentContinuationMessageFamilies
    from elspeth.web.composer.continuation_message_family_recipes import ToolResultMessageRecipe

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, _running, _primary, sdk):
        actual_append = continuation_phase_transitions.append_complete_snapshot
        attempted = []
        failure = RuntimeError("opposed tool completion rollback after actual snapshot append")

        def append(conn, **kwargs):
            retained = actual_append(conn, **kwargs)
            if '"transition":"tool_completed"' in kwargs["event_batch"][0].payload_json:
                attempted.append(retained)
                raise failure
            return retained

        monkeypatch.setattr(continuation_phase_transitions, "append_complete_snapshot", append)
        with pytest.raises(RuntimeError) as caught:
            await _advance(owner)
        assert caught.value is failure and len(attempted) == 1
        runtime = owner.runtime
        message = next(item for item in runtime.live.llm_messages if item.get("tool_call_id") == "first_read")
        wire, recipe = runtime.materialization.capture_message(message)
        assert type(recipe) is ToolResultMessageRecipe
        selected = runtime.source.selected
        assert selected.stack.frames[-1].phase == "effect_reconciling"
        with operation_store[0].connect() as conn:
            row = conn.execute(select(effects).where(effects.c.owner == "tool_dispatch")).one()
        assert row.disposition == "dispatched" and row.receipt_key is None and row.result_chunk_key is None
        before = stored_rows(operation_store[0])
        families = CurrentContinuationMessageFamilies(owner.service, owner.authority, selected, runtime.materialization.inventory)
        with pytest.raises(AuditIntegrityError):
            families.restore_messages(((recipe, wire),))
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1
        assert runtime.materialization.capture_message(message) == (wire, recipe)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", (False, True))
async def test_actual_phase_batch_context_preserves_normal_and_owed_read_owners(operation_store, tmp_path, monkeypatch, stop):
    from elspeth.web.composer.continuation_runtime import build_runtime_tool_batch_context

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        await _advance(owner)
        original = owner.runtime
        assert owner.latest_snapshot is not None
        selected = await load_selected_continuation(
            authority=owner.authority, running=running, selected=owner.latest_snapshot, required_work=owner.work
        )
        assert selected == original.source.selected and selected is not original.source.selected
        if stop:
            owner.authority.request_cancel(
                session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
            )
            owner.observe_stop()
        phase = await owner._current_phase_read(selected)
        runtime = phase.runtime
        assert runtime.source.selected is selected and phase.selected is selected
        assert runtime.bindings is original.bindings and runtime.live is original.live and runtime.graph is original.graph
        assert runtime.materialization is original.materialization and runtime.live.recorder is original.live.recorder
        assert runtime.graph.batch.calls is primary.outcome.completion.tool_batch.calls
        assert runtime.materialization.finish_read is None  # Actual warm owner, including after Stop.
        if stop:
            assert type(phase.finish_read) is ContinuationFinishRead
            assert phase.finish_read.selected is selected and phase.finish_read.inventory is phase.inventory
            assert phase.inventory is not runtime.materialization.inventory
        else:
            assert phase.finish_read is None
        before = stored_rows(operation_store[0])
        reads = []
        normal_read = type(owner.authority).read_continuation_runtime
        owed_read = ContinuationFinishRead.read_runtime

        def normal(self, current, **kwargs):
            reads.append(("normal", current, kwargs["selected"], kwargs["inventory"]))
            return normal_read(self, current, **kwargs)

        def owed(self, authority):
            reads.append(("owed", self.running, self.selected, self.inventory))
            assert self is phase.finish_read
            return owed_read(self, authority)

        monkeypatch.setattr(type(owner.authority), "read_continuation_runtime", normal)
        monkeypatch.setattr(ContinuationFinishRead, "read_runtime", owed)
        context = await build_runtime_tool_batch_context(runtime, cancellation_requested=owner.cancellation_requested, phase_read=phase)
        assert reads == [("owed" if stop else "normal", running, selected, phase.inventory)] * 2
        assert all(row[1] is running and row[2] is selected and row[3] is phase.inventory for row in reads)
        assert context.recorder is runtime.live.recorder and context.message_producers is runtime.materialization
        assert context.turn_evidence is runtime.live.turn_evidence and context.provider_owner is runtime.bindings.provider_owner
        assert context.deadline == original.bindings.deadline and context.cancellation_requested is owner.cancellation_requested
        assert runtime.bindings is original.bindings and runtime.live.llm_messages is original.live.llm_messages
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1 and owner.p4 is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    (
        "selected",
        "inventory",
        "runtime",
        "running",
        "physical_fence",
        "generation",
        "sequence",
        "foreign_facet",
        "no_stop",
        "no_owed",
        "missing_prefix",
    ),
)
async def test_actual_phase_batch_context_refuses_foreign_or_unearned_finish_read(operation_store, tmp_path, monkeypatch, change):
    from sqlalchemy import delete

    from elspeth.web.composer.continuation_runtime import build_runtime_tool_batch_context

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        if change != "no_owed":
            await _advance(owner)
        selected = owner.runtime.source.selected
        phase = await owner._current_phase_read(selected)
        await build_runtime_tool_batch_context(phase.runtime, cancellation_requested=owner.cancellation_requested, phase_read=phase)
        if change != "no_stop":
            owner.authority.request_cancel(
                session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
            )
            owner.observe_stop()
        if change in ("no_stop", "no_owed"):
            phase = replace(phase, finish_read=ContinuationFinishRead(running, selected, phase.inventory))
        else:
            phase = await owner._current_phase_read(selected)
            await build_runtime_tool_batch_context(phase.runtime, cancellation_requested=owner.cancellation_requested, phase_read=phase)
        runtime = phase.runtime
        facet = phase.finish_read
        assert type(facet) is ContinuationFinishRead
        if change == "selected":
            copied = replace(selected)
            assert copied == selected and copied is not selected
            phase = replace(phase, finish_read=replace(facet, selected=copied))
        elif change == "inventory":
            copied = replace(phase.inventory)
            assert copied == phase.inventory and copied is not phase.inventory
            phase = replace(phase, finish_read=replace(facet, inventory=copied))
        elif change == "runtime":
            phase = replace(phase, runtime=replace(runtime))
        elif change in ("running", "physical_fence", "generation", "sequence"):
            if change == "physical_fence":
                copied = replace(
                    running,
                    session_operation_context=replace(
                        running.session_operation_context,
                        fence=replace(
                            running.session_operation_context.fence,
                            operation_epoch=running.session_operation_context.fence.operation_epoch + 1,
                        ),
                    ),
                )
            elif change in ("generation", "sequence"):
                claim = replace(
                    running.claim,
                    **{
                        "ownership_generation" if change == "generation" else "segment_sequence": (
                            running.claim.ownership_generation if change == "generation" else running.claim.segment_sequence
                        )
                        + 1
                    },
                )
                copied = replace(running, claim=claim)
            else:
                copied = replace(running)
                assert copied == running and copied is not running
            inventory = replace(phase.inventory, running=copied)
            phase = replace(phase, inventory=inventory, finish_read=replace(facet, running=copied, inventory=inventory))
        elif change == "foreign_facet":
            phase = replace(phase, finish_read=object())
        elif change == "missing_prefix":
            with operation_store[0].begin() as conn:
                conn.execute(delete(effects).where(effects.c.owner == "tool_dispatch"))
        else:
            assert change in ("no_stop", "no_owed")
        before = stored_rows(operation_store[0])
        expected = {
            "selected": "actual selected/current physical owners",
            "inventory": "actual selected/current physical owners",
            "runtime": "actual phase read cohort",
            "running": "actual current required-work owners",
            "physical_fence": "actual current required-work owners",
            "generation": "actual current required-work owners",
            "sequence": "sequence/request",
            "foreign_facet": "foreign owed-read facet",
            "no_stop": "no actual durable Stop",
            "no_owed": "completed issued-tool prefix",
            "missing_prefix": None,
        }[change]
        with pytest.raises(AuditIntegrityError, match=expected):
            await build_runtime_tool_batch_context(runtime, cancellation_requested=owner.cancellation_requested, phase_read=phase)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1 and owner.p4 is None


@pytest.mark.asyncio
async def test_actual_phase_batch_context_rechecks_cut_after_preferences(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_runtime import build_runtime_tool_batch_context
    from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationEvent, append_complete_snapshot

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        phase = await owner._current_phase_read(owner.runtime.source.selected)
        assert type(phase.finish_read) is ContinuationFinishRead
        reads, advanced = [], []
        actual_read = ContinuationFinishRead.read_runtime
        sessions = owner.service._require_sessions_service()
        actual_preferences = type(sessions).get_composer_preferences

        def read(self, authority):
            assert self is phase.finish_read
            reads.append(self)
            return actual_read(self, authority)

        async def preferences(self, session_id):
            assert self is sessions and len(reads) == 1
            result = await actual_preferences(self, session_id)
            with operation_store[0].begin() as conn:
                current = append_complete_snapshot(
                    conn,
                    session_id=str(running.claim.session_id),
                    operation_id=running.claim.operation_id,
                    expected_checkpoint_id=phase.selected.checkpoint_id,
                    expected_cursor=phase.selected.stack.cursor,
                    snapshot_json=encode_continuation_snapshot(phase.selected.stack),
                    event_batch=(ContinuationEvent("checkpoint", canonical_json({"opposed": "head changed during preferences"})),),
                    handoff=False,
                )
            assert current.checkpoint_id != phase.selected.checkpoint_id and current.cursor == phase.selected.stack.cursor + 1
            advanced.append(stored_rows(operation_store[0]))
            return result

        monkeypatch.setattr(ContinuationFinishRead, "read_runtime", read)
        monkeypatch.setattr(type(sessions), "get_composer_preferences", preferences)
        with pytest.raises(ContinuationCASConflict):
            await build_runtime_tool_batch_context(phase.runtime, cancellation_requested=owner.cancellation_requested, phase_read=phase)
        assert len(reads) == 2 and len(advanced) == 1
        assert stored_rows(operation_store[0]) == advanced[0] and len(sdk) == 1 and owner.p4 is None


@pytest.mark.asyncio
async def test_default_context_and_next_tool_keep_stop_dispatch_refusal(operation_store, tmp_path, monkeypatch):
    from elspeth.web.composer.continuation_runtime import build_runtime_tool_batch_context
    from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        await _advance(owner)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        phase = await owner._current_phase_read(owner.runtime.source.selected)
        await build_runtime_tool_batch_context(phase.runtime, cancellation_requested=owner.cancellation_requested, phase_read=phase)
        before = stored_rows(operation_store[0])
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await build_runtime_tool_batch_context(phase.runtime, cancellation_requested=owner.cancellation_requested)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await owner._tool_context(phase.runtime)
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            await _advance(owner)
        with operation_store[0].connect() as conn:
            issued = conn.execute(select(effects.c.effect_key).where(effects.c.owner == "tool_dispatch")).scalars().all()
        assert issued == [f"tool:{primary.response.reference.digest}:0"]
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1 and owner.p4 is None


@pytest.mark.asyncio
async def test_real_stop_reads_and_publishes_only_the_actual_completed_prefix(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        await _advance(owner)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        owner.observe_stop()
        selected, inventory = await _finish_inventory(owner, running)
        facet = ContinuationFinishRead(running, selected, inventory)
        before = stored_rows(operation_store[0])
        source = facet.read_runtime(owner.authority)
        assert source.selected is selected and stored_rows(operation_store[0]) == before
        await owner.finish_owed_prefix(selected)
        assert owner.p4 is not None and owner.p4.publication is not None and owner.p4.application is not None
        assert owner.primary is primary and len(sdk) == 1
        assert [item.call.id for item in owner.runtime.graph.batch.batch.tool_outcomes] == ["first_read"]
        terminal = owner.authority.settle_continuation_owner(
            running, outcome=request_cancelled_error(request_id=None), env_ref_names=inventory.names
        )
        assert terminal.failure_code == "request_cancelled" and terminal.cancel_requested_at is not None
        with operation_store[0].connect() as conn:
            issued = conn.execute(select(effects.c.effect_key).where(effects.c.owner == "tool_dispatch")).scalars().all()
        assert issued == [f"tool:{primary.response.reference.digest}:0"]


@pytest.mark.asyncio
async def test_stop_without_issued_or_completed_work_cannot_use_the_finish_facet(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        selected, inventory = await _finish_inventory(owner, running)
        before = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="completed issued-tool prefix"):
            ContinuationFinishRead(running, selected, inventory).read_runtime(owner.authority)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ("physical_fence", "generation", "sequence"))
async def test_owed_stop_restore_refuses_a_stale_owner_or_sequence_after_actual_positive(operation_store, tmp_path, monkeypatch, foreign):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, _primary, sdk):
        await _advance(owner)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        selected, inventory = await _finish_inventory(owner, running)
        assert ContinuationFinishRead(running, selected, inventory).read_runtime(owner.authority).selected is selected
        if foreign == "physical_fence":
            stale = replace(
                running,
                session_operation_context=replace(
                    running.session_operation_context,
                    fence=replace(
                        running.session_operation_context.fence, operation_epoch=running.session_operation_context.fence.operation_epoch + 1
                    ),
                ),
            )
        else:
            key = "ownership_generation" if foreign == "generation" else "segment_sequence"
            claim = replace(
                running.claim,
                **{key: (running.claim.ownership_generation if foreign == "generation" else running.claim.segment_sequence) + 1},
            )
            stale = replace(running, claim=claim)
        foreign_inventory = replace(inventory, running=stale)
        before = stored_rows(operation_store[0])
        with pytest.raises((AuditIntegrityError, ComposerOperationFenceLost, SessionOperationFenceLost)):
            ContinuationFinishRead(stale, selected, foreign_inventory).read_runtime(owner.authority)
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
async def test_cold_stop_restores_the_actual_prefix_without_repeat_paid_work(operation_store, tmp_path, monkeypatch):
    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        await _advance(owner)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        selected, inventory = await _finish_inventory(owner, running)
        facet = ContinuationFinishRead(running, selected, inventory)
        before = stored_rows(operation_store[0])
        warm_calls = owner.runtime.graph.batch.calls
        restored = await restore_continuation_runtime(
            service=owner.service,
            authority=owner.authority,
            selected=selected,
            inventory=inventory,
            required_work=owner.work,
            provider_owner=owner.provider_owner,
            finish_read=facet,
        )
        assert restored.materialization.finish_read is facet
        assert restored.graph.batch is not owner.runtime.graph.batch
        assert restored.graph.batch.batch.tool_outcomes[0].call is restored.graph.batch.calls[0]
        assert owner.runtime.graph.batch.calls is warm_calls is primary.outcome.completion.tool_batch.calls
        assert restored.live.current_state_id == owner.runtime.live.current_state_id
        assert stored_rows(operation_store[0]) == before and len(sdk) == 1


@pytest.mark.asyncio
async def test_owed_stop_read_refuses_contradictory_tool_result_after_actual_positive(operation_store, tmp_path, monkeypatch):
    from sqlalchemy import update

    from elspeth.web.sessions.continuation import private_chunk_key

    async with _phase_case(operation_store, tmp_path, monkeypatch) as (owner, running, primary, sdk):
        await _advance(owner)
        owner.authority.request_cancel(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id, cancelled_failure=request_cancelled_error
        )
        selected, inventory = await _finish_inventory(owner, running)
        facet = ContinuationFinishRead(running, selected, inventory)
        assert facet.read_runtime(owner.authority).selected is selected
        with operation_store[0].begin() as conn:
            conn.execute(
                update(effects).where(effects.c.owner == "tool_dispatch").values(result_chunk_key=private_chunk_key(primary.response))
            )
        malformed = stored_rows(operation_store[0])
        with pytest.raises(AuditIntegrityError, match="coupled source codec"):
            facet.read_runtime(owner.authority)
        assert stored_rows(operation_store[0]) == malformed and len(sdk) == 1
