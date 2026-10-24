"""Primary provider effect publication on the existing continuation SQL owner.

An intent is deliberately conservative: a lost process never proves SDK entry
did not happen. Only an actual committed, settled response can be reused.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from sqlalchemy import Connection, Row, insert, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.audit_storage import private_diagnostic_drafts
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, PrivateChunkRef
from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot, decode_recorder_snapshot, encode_recorder_snapshot
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
    encode_continuation_response,
    encode_continuation_snapshot,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import append_primary_call_to_evidence
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_provider_outcomes import require_successful_response_audit
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers, supports_anthropic_prompt_cache_markers
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.coordination.quota_authority import _usage_entry_id, token_usage_entry_from_llm_call_envelope
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    ContinuationProviderBinding,
    PreparedContinuationProvider,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _owed_continuation_p4_active,
    _require_current_continuation_running,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc


@dataclass(frozen=True, slots=True)
class PrimaryProviderResult:
    snapshot: StoredContinuationSnapshot
    effect_key: str
    response: PrivateChunk
    recorder: PrivateChunk
    buffer: RecorderSnapshot
    outcome: _CallModelOutcome


@dataclass(frozen=True, slots=True)
class RetainedPrimaryInput:
    canonical: str = field(repr=False)
    messages_json: str = field(repr=False)
    tools_present: bool
    tools_json: str = field(repr=False)
    model: str
    temperature: int | float | None
    seed: int | None


def _primary_frame(stack: ContinuationFrameStack) -> ComposeFrame:
    frame = stack.frames[-1]
    if (
        len(stack.frames) != 1
        or type(frame) is not ComposeFrame
        or stack.pending_settlement_ids
        or stack.pending_owner_results
        or stack.active_action_json is not None
        or stack.active_action_source is not None
        or any(
            item.disposition in ("prepared", "dispatched", "unknown") and item.effect_id != frame.provider_effect_key
            for item in stack.effect_frontier
        )
    ):
        raise AuditIntegrityError("primary result owner requires the sole ordinary compose frame without an active child/action")
    return frame


def _owned_text(conn: Connection, running: ComposerOperationRunning, key: str) -> str:
    raw = read_chunk(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, key=key)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("primary private source is not strict UTF-8") from error


def _chunk(conn: Connection, running: ComposerOperationRunning, ref: PrivateChunkRef) -> PrivateChunk:
    row = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == str(running.claim.session_id),
            chunks.c.operation_id == running.claim.operation_id,
            chunks.c.chunk_key == f"value:{ref.codec}:{ref.digest}",
        )
    ).one_or_none()
    if row is None or row.codec != ref.codec:
        raise AuditIntegrityError("primary private source changed its SQL codec")
    text = _owned_text(conn, running, f"value:{ref.codec}:{ref.digest}")
    verify_private_chunk(ref, text)
    return PrivateChunk(ref, text)


def _input_value(canonical: str | None, digest: str) -> RetainedPrimaryInput:
    if type(canonical) is not str:
        raise AuditIntegrityError("primary effect requires its actual complete prepared input descriptor")
    value, error = parse_json_strict(canonical)
    if (
        error is not None
        or type(value) is not dict
        or set(value) != {"schema", "messages", "tools_present", "tools", "options"}
        or value["schema"] != "composer.provider-input.v1"
        or type(value["messages"]) is not list
        or type(value["tools_present"]) is not bool
        or type(value["options"]) is not dict
        or "model" not in value["options"]
        or type(value["options"]["model"]) is not str
        or {"messages", "tools", "api_key", "extra_headers", "timeout"}.intersection(value["options"])
        or canonical_json(value) != canonical
        or stable_hash(value) != digest
    ):
        raise AuditIntegrityError("primary prepared input descriptor changed its complete shape/identity")
    require_no_credential_material(value["options"], surface="composer_continuation_primary_input_options")
    options = value["options"]
    temperature = options.get("temperature")
    seed = options.get("seed")
    if (temperature is not None and type(temperature) not in (int, float)) or (seed is not None and type(seed) is not int):
        raise AuditIntegrityError("primary prepared input changed its owned sampling scalars")
    return RetainedPrimaryInput(
        canonical,
        canonical_json(value["messages"]),
        value["tools_present"],
        canonical_json(value["tools"]),
        options["model"],
        temperature,
        seed,
    )


def _wire_input(
    conn: Connection,
    running: ComposerOperationRunning,
    frame: ComposeFrame,
    value: RetainedPrimaryInput,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected, error = parse_json_strict(_chunk(conn, running, frame.messages).canonical)
    if (
        error is not None
        or type(selected) is not dict
        or set(selected) != {"messages_json", "current_indexes"}
        or type(selected["messages_json"]) is not list
        or type(selected["current_indexes"]) is not list
    ):
        raise AuditIntegrityError("primary prepared input lost its selected complete messages")
    wire: list[dict[str, Any]] = []
    for index in selected["current_indexes"]:
        if type(index) is not int or not 0 <= index < len(selected["messages_json"]):
            raise AuditIntegrityError("primary prepared input lost its actual message position")
        text = selected["messages_json"][index]
        if type(text) is not str:
            raise AuditIntegrityError("primary prepared input lost its original message wire")
        message, message_error = parse_json_strict(text)
        if message_error is not None or type(message) is not dict:
            raise AuditIntegrityError("primary prepared input lost its complete message object")
        wire.append(message)
    tools: list[dict[str, Any]] = []
    if frame.tools is not None:
        if frame.tools.codec != "composer.tools.v1":
            raise AuditIntegrityError("primary prepared input changed its actual tool codec")
        parsed, tool_error = parse_json_strict(_chunk(conn, running, frame.tools).canonical)
        if tool_error is not None or type(parsed) is not list:
            raise AuditIntegrityError("primary prepared input lost its complete tool schemas")
        for tool in parsed:
            if type(tool) is not dict:
                raise AuditIntegrityError("primary prepared input lost its complete tool schema object")
            tools.append(tool)
    if supports_anthropic_prompt_cache_markers(value.model):
        wire, marked_tools = apply_anthropic_cache_markers(wire, tools, mark_history_tail=True)
        tools = marked_tools if marked_tools is not None else tools
    if (
        value.messages_json != canonical_json(wire)
        or value.tools_present != bool(tools)
        or value.tools_json != canonical_json(tools if tools else None)
    ):
        raise AuditIntegrityError("primary prepared input differs from its exact selected wire/cache projection")
    if capture_provider_context(messages=wire, tools=tools or None).disposition != "retained":
        raise AuditIntegrityError("primary continuation requires complete safely retainable original provider input")
    return wire, tools


def _stored_descriptor(conn: Connection, running: ComposerOperationRunning, effect: Row[Any]) -> RetainedPrimaryInput:
    key = f"provider-input:{effect.input_sha256}"
    row = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == str(running.claim.session_id),
            chunks.c.operation_id == running.claim.operation_id,
            chunks.c.chunk_key == key,
        )
    ).one_or_none()
    if row is None or row.codec != "composer.provider-input.v1":
        raise AuditIntegrityError("primary prepared source changed its actual SQL input codec")
    canonical = _owned_text(conn, running, key)
    return _input_value(canonical, effect.input_sha256)


def _stored_input(
    conn: Connection, running: ComposerOperationRunning, effect: Row[Any], frame: ComposeFrame
) -> tuple[RetainedPrimaryInput, list[dict[str, Any]], list[dict[str, Any]]]:
    value = _stored_descriptor(conn, running, effect)
    wire, tools = _wire_input(conn, running, frame, value)
    return value, wire, tools


def _original_wire(value: RetainedPrimaryInput) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Actual immutable SDK input for auditing an already committed result.

    Preparation, dispatch intent and publication separately bind this input to
    their complete selected messages. A later shelf includes actual new replies
    and cannot stand in for the original paid request. This data grants no call.
    """
    messages, message_error = parse_json_strict(value.messages_json)
    tools, tool_error = parse_json_strict(value.tools_json)
    if (
        message_error is not None
        or type(messages) is not list
        or any(type(item) is not dict for item in messages)
        or tool_error is not None
        or (tools is not None and (type(tools) is not list or any(type(item) is not dict for item in tools)))
        or value.tools_present != bool(tools)
        or (not value.tools_present and tools is not None)
        or canonical_json(messages) != value.messages_json
        or canonical_json(tools) != value.tools_json
    ):
        raise AuditIntegrityError("primary original input lost its complete retained SDK wire")
    original_tools = tools if tools is not None else []
    if capture_provider_context(messages=messages, tools=original_tools or None).disposition != "retained":
        raise AuditIntegrityError("primary original input is not its safely retained complete source")
    return messages, original_tools


def prepare_primary_provider_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    input_sha256: str,
    snapshot_json: str,
    expected_checkpoint_id: str,
    expected_cursor: int,
    input_json: str | None,
) -> ContinuationProviderBinding:
    _require_current_continuation_running(conn, running)
    if _owed_continuation_p4_active(conn, running.session_operation_context) or not is_lower_sha256_hex(input_sha256):
        raise AuditIntegrityError("primary preparation requires current ordinary authority and its exact input")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    current = require_snapshot_cas(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
    )
    if current.payload_json != snapshot_json:
        raise ContinuationCASConflict("primary preparation substituted the selected complete root")
    root = decode_continuation_snapshot(snapshot_json)
    frame = _primary_frame(root)
    if frame.phase != "action_ready" or frame.pending_response is not None or frame.provider_effect_key is not None:
        raise ContinuationCASConflict("primary preparation would replace an unresolved response/effect")
    request = _input_value(input_json, input_sha256)
    _wire_input(conn, running, frame, request)
    ordinal = conn.execute(
        select(ordinals.c.next_ordinal).where(
            ordinals.c.session_id == sid,
            ordinals.c.operation_id == operation,
            ordinals.c.family == 0,
        )
    ).scalar_one_or_none()
    number = 0 if ordinal is None else ordinal
    key = f"provider:0:{number}"
    if (
        conn.execute(
            select(effects.c.effect_key).where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == key,
            )
        ).one_or_none()
        is not None
    ):
        raise ContinuationCASConflict("primary ordinal collides with an existing actual effect")
    conn.execute(
        insert(effects).values(
            session_id=sid,
            operation_id=operation,
            effect_key=key,
            input_sha256=input_sha256,
            owner="provider",
            disposition="prepared",
            creator_sequence=running.claim.segment_sequence,
            creator_generation=running.claim.ownership_generation,
            action_json=None,
        )
    )
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"provider-input:{input_sha256}",
        payload=request.canonical.encode("utf-8"),
        codec="composer.provider-input.v1",
    )
    if ordinal is None:
        conn.execute(insert(ordinals).values(session_id=sid, operation_id=operation, family=0, next_ordinal=1))
    else:
        conn.execute(
            update(ordinals)
            .where(
                ordinals.c.session_id == sid,
                ordinals.c.operation_id == operation,
                ordinals.c.family == 0,
            )
            .values(next_ordinal=number + 1)
        )
    changed = frame.model_copy(
        update={
            "phase": "awaiting_response",
            "provider_effect_key": key,
            "provider_input_sha256": input_sha256,
        }
    )
    encoded = _effect_snapshot(
        encode_continuation_snapshot(root.model_copy(update={"frames": (changed,)})),
        effect_key=key,
        disposition="prepared",
        receipt_id=None,
    )
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    conn.execute(
        update(segments)
        .where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
        )
        .values(next_action_json=policy.actions.primary.model_dump_json(), policy_sha256=root.policy_hash)
    )
    append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("input_prepared", canonical_json({"effect_key": key, "input_sha256": input_sha256})),),
        handoff=False,
    )
    return ContinuationProviderBinding(running, key, 0, number, input_sha256)


def _selected_effect(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    checkpoint_id: str,
    cursor: int,
    effect_key: str,
    input_sha256: str,
) -> tuple[StoredContinuationSnapshot, ContinuationFrameStack, ComposeFrame, Row[Any]]:
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    selected = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=checkpoint_id,
        expected_cursor=cursor,
    )
    stack = decode_continuation_snapshot(selected.payload_json)
    frame = _primary_frame(stack)
    if (frame.provider_effect_key, frame.provider_input_sha256) != (effect_key, input_sha256):
        raise AuditIntegrityError("primary effect differs from its actual complete selected frame")
    parts = effect_key.split(":")
    if (
        len(parts) != 3
        or parts[:2] != ["provider", "0"]
        or not parts[2].isascii()
        or not parts[2].isdigit()
        or str(int(parts[2])) != parts[2]
    ):
        raise AuditIntegrityError("primary effect has an unfamiliar physical family/ordinal")
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == str(running.claim.session_id),
            effects.c.operation_id == running.claim.operation_id,
            effects.c.effect_key == effect_key,
        )
    ).one()
    frontier = tuple(item for item in stack.effect_frontier if item.effect_id == effect_key)
    if (
        effect.owner != "provider"
        or effect.input_sha256 != input_sha256
        or effect.action_json is not None
        or len(frontier) != 1
        or (frontier[0].disposition, frontier[0].receipt_id) != (effect.disposition, effect.receipt_key)
        or effect.committed_state_ids_json is not None
        or effect.graph_sha256 is not None
    ):
        raise AuditIntegrityError("primary effect differs from its actual owner/frontier")
    return selected, stack, frame, effect


def begin_primary_provider_on_connection(conn: Connection, prepared: PreparedContinuationProvider) -> StoredContinuationSnapshot:
    if type(prepared) is not PreparedContinuationProvider or type(prepared.binding) is not ContinuationProviderBinding:
        raise AuditIntegrityError("primary dispatch requires the actual prepared owner result")
    binding, current = prepared.binding, prepared.snapshot
    running = binding.running
    _require_current_continuation_running(conn, running)
    selected, root, frame, effect = _selected_effect(
        conn,
        running,
        checkpoint_id=current.checkpoint_id,
        cursor=current.cursor,
        effect_key=binding.effect_key,
        input_sha256=binding.input_sha256,
    )
    if (
        binding.family != 0
        or binding.effect_key != f"provider:0:{binding.ordinal}"
        or frame.phase != "awaiting_response"
        or frame.pending_response is not None
        or effect.disposition != "prepared"
        or effect.provider_attempt_id is not None
        or effect.result_chunk_key is not None
        or effect.receipt_key is not None
        or effect.physical_sequence is not None
        or effect.physical_generation is not None
        or effect.requested_model is not None
        or effect.response_finished_at is not None
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
    ):
        raise ContinuationCASConflict("unresolved or foreign primary preparation cannot dispatch or retry")
    _stored_input(conn, running, effect, frame)
    conn.execute(
        update(effects)
        .where(
            effects.c.session_id == str(running.claim.session_id),
            effects.c.operation_id == running.claim.operation_id,
            effects.c.effect_key == binding.effect_key,
        )
        .values(disposition="dispatched")
    )
    encoded = _effect_snapshot(encode_continuation_snapshot(root), effect_key=binding.effect_key, disposition="dispatched", receipt_id=None)
    return append_complete_snapshot(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("may_dispatch", canonical_json({"effect_key": binding.effect_key})),),
        handoff=False,
    )


def _physical_source(conn: Connection, running: ComposerOperationRunning, effect: Row[Any], call: ComposerLLMCall) -> str:
    if type(call) is not ComposerLLMCall or call.call_id is None:
        raise AuditIntegrityError("primary result requires the actual original physical audit")
    source = _stored_descriptor(conn, running, effect)
    wire, tools = _original_wire(source)
    if (
        call.model_requested != source.model
        or call.temperature != source.temperature
        or call.seed != source.seed
        or call.messages_hash != stable_hash(wire)
        or call.tools_spec_hash != (stable_hash(tools) if tools else None)
        or call.private_request != capture_provider_context(messages=wire, tools=tools or None)
    ):
        raise AuditIntegrityError("primary physical audit differs from its complete actual prepared input source")
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == str(running.claim.session_id),
            attempts.c.operation_id == running.claim.operation_id,
            attempts.c.sequence == effect.creator_sequence,
            attempts.c.generation == effect.creator_generation,
        )
    ).one()
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == call.call_id)).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == str(running.claim.session_id))).scalar_one()
    if (
        attempt is None
        or (
            attempt.session_id,
            attempt.source,
            attempt.run_id,
            attempt.operation_id,
            attempt.operation_epoch,
            attempt.lease_token,
        )
        != (
            str(running.claim.session_id),
            "composer",
            None,
            creator.session_operation_id,
            creator.session_operation_epoch,
            creator.session_operation_lease_token,
        )
        or attempt.identity_id != principal
        or attempt.settled_at is None
        or restore_utc(attempt.started_at) != call.started_at
    ):
        raise AuditIntegrityError("primary result changed its original physical creator or lacks actual settlement")
    envelope = llm_call_audit_envelope(call)
    entry = token_usage_entry_from_llm_call_envelope(envelope)
    if entry is None:
        raise AuditIntegrityError("primary result lost its actual accounting projection")
    entry_id = _usage_entry_id(session_id=attempt.session_id, source="composer", run_id=None, call_id=call.call_id)
    ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()
    if (
        ledger is None
        or attempt.ledger_entry_id != entry_id
        or (
            ledger.identity_id,
            ledger.session_id,
            ledger.source,
            ledger.run_id,
            ledger.model,
            ledger.prompt_tokens,
            ledger.completion_tokens,
            ledger.cached_prompt_tokens,
            ledger.reasoning_tokens,
            restore_utc(ledger.recorded_at),
        )
        != (
            attempt.identity_id,
            attempt.session_id,
            "composer",
            None,
            entry.model,
            entry.prompt_tokens,
            entry.completion_tokens,
            entry.cached_prompt_tokens,
            entry.reasoning_tokens,
            entry.recorded_at,
        )
    ):
        raise AuditIntegrityError("primary result changed its original complete token ledger")
    # This context is historical source data used only by the audit projector.
    # It is never supplied to a write, custody mint or provider call.
    source_context = SessionOperationContext(
        SessionOperationFence(
            attempt.session_id,
            attempt.operation_id,
            attempt.lease_token,
            attempt.operation_epoch,
        ),
        SessionOperationKind.COMPOSE,
    )
    expected = (
        canonical_json(envelope),
        *(
            canonical_json(item)
            for draft in private_diagnostic_drafts(call, context=source_context)
            if draft.tool_calls is not None
            for item in draft.tool_calls
        ),
    )
    rows = conn.execute(
        select(messages.c.tool_calls).where(
            messages.c.session_id == attempt.session_id,
            messages.c.role == "audit",
            messages.c.writer_principal == "compose_loop",
        )
    ).all()
    actual = tuple(canonical_json(item) for row in rows if row.tool_calls is not None for item in row.tool_calls)
    if any(actual.count(item) != 1 for item in expected):
        raise AuditIntegrityError("primary result lacks its exact original public/private source audit cohort")
    return entry_id


def publish_primary_provider_on_connection(
    conn: Connection,
    binding: ContinuationProviderBinding,
    *,
    selected: StoredContinuationSnapshot,
    outcome: _CallModelOutcome,
    recorder: RecorderSnapshot,
    env_ref_names: frozenset[str] = frozenset(),
) -> PrimaryProviderResult:
    if type(binding) is not ContinuationProviderBinding or type(outcome) is not _CallModelOutcome or type(recorder) is not RecorderSnapshot:
        raise AuditIntegrityError("primary publication lost its nominal actual owners")
    running = binding.running
    current, root, frame, effect = _selected_effect(
        conn,
        running,
        checkpoint_id=selected.checkpoint_id,
        cursor=selected.cursor,
        effect_key=binding.effect_key,
        input_sha256=binding.input_sha256,
    )
    if (
        binding.family != 0
        or binding.effect_key != f"provider:0:{binding.ordinal}"
        or effect.disposition != "dispatched"
        or frame.phase != "awaiting_response"
        or frame.pending_response is not None
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        or effect.result_chunk_key is not None
        or effect.receipt_key is not None
        or outcome.audit_call is None
        or effect.provider_attempt_id is not None
        or effect.physical_sequence is not None
        or effect.physical_generation is not None
        or effect.requested_model is not None
        or effect.response_finished_at is not None
    ):
        raise ContinuationCASConflict("primary publication cannot replace a result or its original dispatched owner")
    call = outcome.audit_call
    if call.call_id is None:
        raise AuditIntegrityError("primary publication requires its original physical identity")
    require_successful_response_audit(
        call,
        outcome.completion.provider_metadata,
        attempt_id=call.call_id,
        started_at=call.started_at,
        finished_at=call.finished_at,
    )
    # Publication still owns the original selected input. A later audit read
    # instead joins immutable paid-input DATA after the transcript has grown.
    _stored_input(conn, running, effect, frame)
    _physical_source(conn, running, effect, call)
    previous = decode_recorder_snapshot(_chunk(conn, running, frame.audit_buffer))
    if replace(previous, llm_calls=(*previous.llm_calls, call)) != recorder or not recorder.llm_calls or recorder.llm_calls[-1] is not call:
        raise AuditIntegrityError("primary publication changed its complete original recorder prefix/channels")
    response = encode_continuation_response(outcome.completion, audit_call=call)
    audit = encode_recorder_snapshot(recorder)
    evidence = decode_turn_evidence(_chunk(conn, running, frame.evidence), env_ref_names=env_ref_names)
    if evidence.recorder_json != encode_recorder_snapshot(previous).canonical:
        raise AuditIntegrityError("primary publication changed its actual evidence/recorder prefix join")
    extended_evidence = append_primary_call_to_evidence(evidence, call=call, recorder=recorder, env_ref_names=env_ref_names)
    updated_evidence = encode_turn_evidence(extended_evidence, env_ref_names=env_ref_names)
    for chunk in (response, audit, updated_evidence):
        put_chunk(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
    conn.execute(
        update(effects)
        .where(
            effects.c.session_id == str(running.claim.session_id),
            effects.c.operation_id == running.claim.operation_id,
            effects.c.effect_key == binding.effect_key,
        )
        .values(
            disposition="committed",
            receipt_key=call.call_id,
            result_chunk_key=private_chunk_key(response),
            provider_attempt_id=call.call_id,
            physical_sequence=effect.creator_sequence,
            physical_generation=effect.creator_generation,
            requested_model=call.model_requested,
            response_finished_at=call.finished_at,
        )
    )
    changed = frame.model_copy(
        update={
            "phase": "response_retained",
            "pending_response": response.reference,
            "audit_buffer": audit.reference,
            "evidence": updated_evidence.reference,
        }
    )
    encoded = _effect_snapshot(
        encode_continuation_snapshot(root.model_copy(update={"frames": (changed,)})),
        effect_key=binding.effect_key,
        disposition="committed",
        receipt_id=call.call_id,
    )
    committed = append_complete_snapshot(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
        snapshot_json=encoded,
        event_batch=(
            ContinuationEvent(
                "response_retained", canonical_json({"effect_key": binding.effect_key, "response_digest": response.reference.digest})
            ),
        ),
        handoff=False,
    )
    return PrimaryProviderResult(committed, binding.effect_key, response, audit, recorder, outcome)


def _require_selected_primary_source(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
) -> None:
    if type(selected) is not SelectedContinuationState:
        raise AuditIntegrityError("primary source read requires its actual complete nominal selection")
    selected.__post_init__()
    expected = StoredContinuationSnapshot(encode_continuation_snapshot(selected.stack), selected.stack.cursor, selected.checkpoint_id)
    actual = load_selected_private_chunks_on_connection(conn, running, expected)
    if actual != selected.chunks:
        raise ContinuationCASConflict("primary source read changed its complete selected chunk cohort")


def read_primary_provider_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    checkpoint_id: str,
    cursor: int,
    effect_key: str,
    input_sha256: str,
) -> PrimaryProviderResult:
    _require_selected_primary_source(conn, running, selected)
    if (checkpoint_id, cursor) != (selected.checkpoint_id, selected.stack.cursor):
        raise ContinuationCASConflict("primary source read changed its supplied selected head")
    current, _root, frame, effect = _selected_effect(
        conn,
        running,
        checkpoint_id=checkpoint_id,
        cursor=cursor,
        effect_key=effect_key,
        input_sha256=input_sha256,
    )
    if effect.disposition != "committed" or frame.phase != "response_retained" or frame.pending_response is None:
        raise ContinuationCASConflict("primary source is unresolved; prepared/dispatched/unknown provider actions cannot retry")
    response = _chunk(conn, running, frame.pending_response)
    if effect.result_chunk_key != private_chunk_key(response):
        raise AuditIntegrityError("primary result substituted its actual selected response")
    completion = decode_continuation_response(response)
    call = decode_retained_physical_audit_call(response)
    if (
        effect.provider_attempt_id != call.call_id
        or effect.receipt_key != call.call_id
        or (effect.physical_sequence, effect.physical_generation) != (effect.creator_sequence, effect.creator_generation)
        or effect.requested_model != call.model_requested
        or effect.response_finished_at is None
        or restore_utc(effect.response_finished_at) != call.finished_at
        or call.call_id is None
    ):
        raise AuditIntegrityError("primary result changed its actual committed physical source tuple")
    require_successful_response_audit(
        call, completion.provider_metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at
    )
    _physical_source(conn, running, effect, call)
    audit = _chunk(conn, running, frame.audit_buffer)
    snapshot = decode_recorder_snapshot(audit)
    matches = tuple(item for item in snapshot.llm_calls if item.call_id == call.call_id)
    if len(matches) != 1 or matches[0] != call:
        raise AuditIntegrityError("primary result changed its complete original retained recorder call")
    return PrimaryProviderResult(current, effect_key, response, audit, snapshot, _CallModelOutcome(completion, matches[0]))


def read_committed_primary_response_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    reference: PrivateChunkRef,
) -> PrivateChunk:
    """Exact historical paid response under ordinary complete current admission.

    The selected recipe supplies a complete response reference, never a latest
    ordinal or recorder position. It must still have its exact committed effect
    frontier, original input and actual physical audit/accounting source.
    """
    _require_current_continuation_running(conn, running)
    _require_selected_primary_source(conn, running, selected)
    if type(reference) is not PrivateChunkRef or reference.codec != "composer.response.v1":
        raise AuditIntegrityError("historical primary response lost its actual source codec")
    key = f"value:{reference.codec}:{reference.digest}"
    found = conn.execute(
        select(effects).where(
            effects.c.session_id == str(running.claim.session_id),
            effects.c.operation_id == running.claim.operation_id,
            effects.c.owner == "provider",
            effects.c.result_chunk_key == key,
        )
    ).all()
    if len(found) != 1:
        raise AuditIntegrityError("historical primary response has no unique exact provider effect")
    effect = found[0]
    parts = effect.effect_key.split(":")
    if (
        len(parts) != 3
        or parts[:2] != ["provider", "0"]
        or not parts[2].isascii()
        or not parts[2].isdigit()
        or str(int(parts[2])) != parts[2]
    ):
        raise AuditIntegrityError("historical primary response changed its actual family/ordinal")
    frontier = tuple(item for item in selected.stack.effect_frontier if item.effect_id == effect.effect_key)
    if (
        effect.disposition != "committed"
        or effect.action_json is not None
        or type(effect.creator_sequence) is not int
        or effect.creator_sequence < 1
        or type(effect.creator_generation) is not int
        or effect.creator_generation < 1
        or effect.committed_state_ids_json is not None
        or effect.graph_sha256 is not None
        or len(frontier) != 1
        or (frontier[0].disposition, frontier[0].receipt_id) != ("committed", effect.receipt_key)
    ):
        raise AuditIntegrityError("historical primary response lost its exact committed current frontier")
    response = _chunk(conn, running, reference)
    completion = decode_continuation_response(response)
    call = decode_retained_physical_audit_call(response)
    if (
        call.call_id is None
        or effect.provider_attempt_id != call.call_id
        or effect.receipt_key != call.call_id
        or (effect.physical_sequence, effect.physical_generation) != (effect.creator_sequence, effect.creator_generation)
        or effect.requested_model != call.model_requested
        or effect.response_finished_at is None
        or restore_utc(effect.response_finished_at) != call.finished_at
    ):
        raise AuditIntegrityError("historical primary response changed its original complete physical tuple")
    require_successful_response_audit(
        call, completion.provider_metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at
    )
    _physical_source(conn, running, effect, call)
    return response
