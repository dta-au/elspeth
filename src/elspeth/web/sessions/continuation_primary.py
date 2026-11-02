"""Primary provider effect publication on the existing continuation SQL owner.

An intent is deliberately conservative: a lost process never proves SDK entry
did not happen. Only an actual committed, settled response can be reused.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Connection, Row, insert, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _CallModelOutcome
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
    encode_private_chunk,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_evidence import decode_turn_evidence, encode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import append_primary_call_to_evidence
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer, PrimaryNoSdkRefusalPermit, PrimaryNoSdkRefusalReason
from elspeth.web.composer.continuation_provider_outcomes import require_successful_response_audit
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.llm_response_parsing import apply_anthropic_cache_markers, supports_anthropic_prompt_cache_markers
from elspeth.web.composer.provider_context import capture_provider_context
from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness, require_retained_v4_provider_metadata_credentials
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import (
    ProviderAttempt,
    _undispatched_cancellation_event_id,
    _usage_entry_id,
    begin_provider_attempt_on_connection,
    token_usage_entry_from_llm_call_envelope,
)
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRecord, ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    ContinuationProviderBinding,
    PreparedContinuationAppend,
    PreparedContinuationProvider,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _owed_continuation_p4_active,
    _require_current_continuation_running,
    append_complete_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_paid_incomplete import PaidIncompleteReceipt
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity, PaidOverflowReceipt
from elspeth.web.sessions.continuation_paid_overflow_quota import preflight_v4_requested_model
from elspeth.web.sessions.continuation_primary_paid_incomplete_tail import PrimaryPaidIncompleteTailPlan
from elspeth.web.sessions.continuation_primary_paid_overflow_no_sdk import PrimaryNoSdkTailPlan, prepare_primary_no_sdk_tail_plan
from elspeth.web.sessions.continuation_primary_paid_overflow_tail import (
    PrimaryPaidOverflowTailPlan,
    prepare_primary_paid_overflow_tail_plan,
)
from elspeth.web.sessions.continuation_primary_terminal_reservation import primary_terminal_reservation
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    bind_provider_terminal_hold_attempt,
    reserve_provider_terminal_hold,
)
from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_primary_observation import PrimaryCompleteObservation, PrimaryPaidObservation
    from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True)
class PrimaryProviderResult:
    snapshot: StoredContinuationSnapshot
    effect_key: str
    response: PrivateChunk
    recorder: PrivateChunk
    buffer: RecorderSnapshot
    outcome: _CallModelOutcome
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class PrimaryPaidOverflowTerminal:
    receipt: PaidOverflowReceipt
    snapshot: StoredContinuationSnapshot
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class PrimaryPaidIncompleteTerminal:
    receipt: PaidIncompleteReceipt
    snapshot: StoredContinuationSnapshot
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class V4PrimaryProviderAdmission:
    attempt: ProviderAttempt
    selected: StoredContinuationSnapshot
    hold: ProviderTerminalHoldSource
    tail: PrimaryPaidOverflowTailPlan
    no_sdk_tail: PrimaryNoSdkTailPlan
    incomplete_tail: PrimaryPaidIncompleteTailPlan


def finalize_v4_primary_provider_on_connection(
    conn: Connection,
    *,
    service: SessionServiceImpl,
    prepared: PreparedContinuationProvider,
    admission: V4PrimaryProviderAdmission,
    outcome: _CallModelOutcome,
    recorder: RecorderSnapshot,
    observation: PrimaryPaidObservation,
    env_ref_names: frozenset[str],
) -> PrimaryProviderResult | PrimaryPaidOverflowTerminal:
    from elspeth.web.sessions.continuation_primary_paid_overflow_finalization import finalize_v4_primary_provider_on_connection as finalize

    return finalize(
        conn,
        service=service,
        prepared=prepared,
        admission=admission,
        outcome=outcome,
        recorder=recorder,
        observation=observation,
        env_ref_names=env_ref_names,
    )


@dataclass(frozen=True, slots=True)
class RetainedPrimaryPhaseData:
    """A proved paid response for an existing phase, never a physical receipt."""

    response: PrivateChunk
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
        or any(
            item.disposition in ("prepared", "dispatched", "unknown") and item.effect_id != frame.provider_effect_key
            for item in stack.effect_frontier
        )
    ):
        raise AuditIntegrityError("primary result owner requires the sole compose frame without an active child")
    if stack.active_action_json is not None:
        from elspeth.web.sessions.continuation import bound_action_json

        bound_action_json(stack, action_owner="primary_turn", read_tool_name=None, read_tool_arguments_json=None)
    return frame


def primary_action_wire_message(action_json: str) -> dict[str, str]:
    """Carry exact model-authored PRIMARY advice in the physical request."""
    from elspeth.web.composer.advisor_output import PrimaryContinuationAction
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action

    action = decode_prescribed_continuation_action(action_json)
    if type(action) is not PrimaryContinuationAction or canonical_json(action.model_dump(mode="json")) != action_json:
        raise AuditIntegrityError("PRIMARY wire advice differs from its canonical prescribed action")
    return {"role": "assistant", "content": action.instruction}


def _require_primary_effect_action_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    stack: ContinuationFrameStack,
    effect: Row[Any],
    current_effect: bool,
) -> None:
    """Join an exact PRIMARY effect to its original paid prescription and receipt."""
    if effect.action_json is None:
        if current_effect and stack.active_action_json is not None:
            raise AuditIntegrityError("ordinary PRIMARY effect cannot consume an unrelated prescribed action")
        return
    from elspeth.web.composer.continuation_progress import (
        continuation_action_key,
        current_action_receipts,
        decode_continuation_frontier,
        decode_prescribed_continuation_action,
    )
    from elspeth.web.sessions.continuation_semantic_advisor import read_original_prescribed_primary_source_on_connection

    primary_action_wire_message(effect.action_json)
    source = read_original_prescribed_primary_source_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        action_json=effect.action_json,
        effect_key=effect.effect_key,
    )
    if current_effect:
        active = stack.active_action_json
        if active is None:
            if effect.disposition not in ("committed", "refused") or stack.active_action_source is not None:
                raise AuditIntegrityError("unsettled prescribed PRIMARY lost its active original source")
        elif active != effect.action_json or stack.active_action_source != source:
            raise AuditIntegrityError("prescribed PRIMARY effect differs from its current original source")
    if stack.semantic_frontier is None:
        raise AuditIntegrityError("prescribed PRIMARY effect lost its semantic frontier")
    ref = stack.semantic_frontier
    canonical = read_chunk(conn, session_id=str(session_id), operation_id=operation_id, key=f"value:{ref.codec}:{ref.digest}").decode(
        "utf-8", errors="strict"
    )
    verify_private_chunk(ref, canonical)
    frontier = decode_continuation_frontier(canonical)
    action = decode_prescribed_continuation_action(effect.action_json)
    action_key = continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence})
    matching = tuple(item for item in current_action_receipts(frontier.action_receipts) if item.action_key == action_key)
    # A lost terminal may mark a dispatched physical effect unknown. The
    # action history retains the last witnessed dispatch; it is never a
    # completion claim or permission to issue the physical call again.
    witnessed_disposition = "dispatched" if effect.disposition in ("unknown", "paid_overflow", "paid_incomplete") else effect.disposition
    if len(matching) != 1 or (matching[0].owner_receipt_id, matching[0].disposition) != (
        effect.effect_key,
        witnessed_disposition,
    ):
        raise AuditIntegrityError("prescribed PRIMARY effect lost its exact execution receipt")


def _owned_text(conn: Connection, running: ComposerOperationRunning, key: str) -> str:
    return _owned_text_data(conn, running.claim.session_id, running.claim.operation_id, key)


def _owned_text_data(conn: Connection, session_id: UUID, operation_id: str, key: str) -> str:
    raw = read_chunk(conn, session_id=str(session_id), operation_id=operation_id, key=key)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("primary private source is not strict UTF-8") from error


def _chunk(conn: Connection, running: ComposerOperationRunning, ref: PrivateChunkRef) -> PrivateChunk:
    return _chunk_data(conn, running.claim.session_id, running.claim.operation_id, ref)


def _chunk_data(conn: Connection, session_id: UUID, operation_id: str, ref: PrivateChunkRef) -> PrivateChunk:
    row = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == str(session_id),
            chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == f"value:{ref.codec}:{ref.digest}",
        )
    ).one_or_none()
    if row is None or row.codec != ref.codec:
        raise AuditIntegrityError("primary private source changed its SQL codec")
    text = _owned_text_data(conn, session_id, operation_id, f"value:{ref.codec}:{ref.digest}")
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


def _wire_input_data(
    conn: Connection, session_id: UUID, operation_id: str, frame: ComposeFrame, value: RetainedPrimaryInput, action_json: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected, error = parse_json_strict(_chunk_data(conn, session_id, operation_id, frame.messages).canonical)
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
    if action_json is not None:
        wire.append(primary_action_wire_message(action_json))
    tools: list[dict[str, Any]] = []
    if frame.tools is not None:
        if frame.tools.codec != "composer.tools.v1":
            raise AuditIntegrityError("primary prepared input changed its actual tool codec")
        parsed, tool_error = parse_json_strict(_chunk_data(conn, session_id, operation_id, frame.tools).canonical)
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


def _stored_descriptor_data(conn: Connection, session_id: UUID, operation_id: str, effect: Row[Any]) -> RetainedPrimaryInput:
    key = f"provider-input:{effect.input_sha256}"
    row = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == str(session_id),
            chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == key,
        )
    ).one_or_none()
    if row is None or row.codec != "composer.provider-input.v1":
        raise AuditIntegrityError("primary prepared source changed its actual SQL input codec")
    canonical = _owned_text_data(conn, session_id, operation_id, key)
    return _input_value(canonical, effect.input_sha256)


def stored_primary_input_data_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect: Row[Any], frame: ComposeFrame
) -> tuple[RetainedPrimaryInput, list[dict[str, Any]], list[dict[str, Any]]]:
    """Original complete prepared input after the caller's SQL admission."""
    value = _stored_descriptor_data(conn, session_id, operation_id, effect)
    wire, tools = _wire_input_data(conn, session_id, operation_id, frame, value, effect.action_json)
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


def allocate_primary_effect_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    input_sha256: str,
    action_json: str | None,
) -> ContinuationProviderBinding:
    """Allocate one family-0 effect after its caller's exact source admission.

    Ordinary Compose and post-P4 classifier PRIMARY share this counter. Their
    separate callers must admit their complete source before allocation; this
    helper neither crops a root nor grants physical dispatch permission.
    """
    _require_current_continuation_running(conn, running)
    if not is_lower_sha256_hex(input_sha256):
        raise AuditIntegrityError("primary allocation requires its exact original input digest")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
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
            action_json=action_json,
        )
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
    return ContinuationProviderBinding(running, key, 0, number, input_sha256)


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
    action_json = None
    if root.active_action_json is not None:
        from elspeth.web.sessions.continuation_semantic_advisor import require_current_prescribed_primary_action_on_connection

        action_json = require_current_prescribed_primary_action_on_connection(conn, running, root)
    _wire_input_data(conn, running.claim.session_id, running.claim.operation_id, frame, request, action_json)
    binding = allocate_primary_effect_on_connection(conn, running, input_sha256=input_sha256, action_json=action_json)
    key = binding.effect_key
    put_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"provider-input:{input_sha256}",
        payload=request.canonical.encode("utf-8"),
        codec="composer.provider-input.v1",
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
    if action_json is not None:
        from elspeth.web.sessions.continuation import retain_action_execution_receipt_on_connection

        encoded = retain_action_execution_receipt_on_connection(
            conn, session_id=sid, operation_id=operation, effect_key=key, disposition="prepared", snapshot_json=encoded
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
    return binding


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
    return selected_primary_effect_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        checkpoint_id=checkpoint_id,
        cursor=cursor,
        effect_key=effect_key,
        input_sha256=input_sha256,
    )


def selected_primary_effect_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    checkpoint_id: str,
    cursor: int,
    effect_key: str,
    input_sha256: str,
) -> tuple[StoredContinuationSnapshot, ContinuationFrameStack, ComposeFrame, Row[Any]]:
    """Exact selected PRIMARY DATA; never a current-custody admission."""
    selected = require_snapshot_cas(
        conn,
        session_id=str(session_id),
        operation_id=operation_id,
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
            effects.c.session_id == str(session_id),
            effects.c.operation_id == operation_id,
            effects.c.effect_key == effect_key,
        )
    ).one()
    frontier = tuple(item for item in stack.effect_frontier if item.effect_id == effect_key)
    if (
        effect.owner != "provider"
        or effect.input_sha256 != input_sha256
        or len(frontier) != 1
        or (frontier[0].disposition, frontier[0].receipt_id) != (effect.disposition, effect.receipt_key)
        or effect.committed_state_ids_json is not None
        or effect.graph_sha256 is not None
    ):
        raise AuditIntegrityError("primary effect differs from its actual owner/frontier")
    if (
        type(effect.creator_sequence) is not int
        or effect.creator_sequence < 1
        or type(effect.creator_generation) is not int
        or effect.creator_generation < 1
    ):
        raise AuditIntegrityError("primary effect lost its actual issuing attempt")
    v4 = type(decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)) is ContinuationAdmissionPolicyV4
    if effect.disposition in ("prepared", "dispatched", "unknown"):
        v4_physical = v4 and effect.disposition in ("dispatched", "unknown")
        if (
            frame.phase != "awaiting_response"
            or frame.pending_response is not None
            or effect.result_chunk_key is not None
            or effect.receipt_key is not None
            or effect.response_finished_at is not None
            or (
                v4_physical
                and (
                    type(effect.provider_attempt_id) is not str
                    or not effect.provider_attempt_id
                    or effect.physical_sequence != effect.creator_sequence
                    or effect.physical_generation != effect.creator_generation
                    or type(effect.requested_model) is not str
                    or not effect.requested_model
                )
            )
            or (
                not v4_physical
                and (
                    effect.provider_attempt_id is not None
                    or effect.physical_sequence is not None
                    or effect.physical_generation is not None
                    or effect.requested_model is not None
                )
            )
        ):
            raise AuditIntegrityError("unresolved primary carries contradictory result or physical settlement evidence")
    elif effect.disposition == "committed":
        if (
            frame.phase not in ("response_retained", "classify")
            or frame.pending_response is None
            or effect.result_chunk_key != f"value:{frame.pending_response.codec}:{frame.pending_response.digest}"
            or type(effect.receipt_key) is not str
            or not effect.receipt_key
            or effect.provider_attempt_id != effect.receipt_key
            or effect.physical_sequence != effect.creator_sequence
            or effect.physical_generation != effect.creator_generation
            or type(effect.requested_model) is not str
            or not effect.requested_model
            or effect.response_finished_at is None
        ):
            raise AuditIntegrityError("committed primary lost its complete selected response/physical settlement shape")
    elif effect.disposition == "refused":
        if (
            frame.phase != "awaiting_response"
            or frame.pending_response is not None
            or type(effect.receipt_key) is not str
            or not effect.receipt_key
            or type(effect.result_chunk_key) is not str
            or not effect.result_chunk_key.startswith("value:composer.primary-no-sdk-refusal.v1:")
            or (
                not v4
                and (
                    effect.provider_attempt_id is not None
                    or effect.physical_sequence is not None
                    or effect.physical_generation is not None
                    or effect.requested_model is not None
                    or effect.response_finished_at is not None
                )
            )
            or (
                v4
                and effect.provider_attempt_id is not None
                and (
                    type(effect.provider_attempt_id) is not str
                    or effect.physical_sequence != effect.creator_sequence
                    or effect.physical_generation != effect.creator_generation
                    or type(effect.requested_model) is not str
                    or not effect.requested_model
                    or effect.response_finished_at is not None
                )
            )
        ):
            raise AuditIntegrityError("refused primary lost its terminal no-SDK receipt shape")
    elif effect.disposition in ("paid_overflow", "paid_incomplete"):
        receipt_prefix = "paid-overflow:" if effect.disposition == "paid_overflow" else "paid-incomplete:"
        chunk_prefix = "value:composer.paid-overflow.v1:" if effect.disposition == "paid_overflow" else "value:composer.paid-incomplete.v1:"
        if (
            not v4
            or frame.phase != "awaiting_response"
            or frame.pending_response is not None
            or type(effect.receipt_key) is not str
            or not effect.receipt_key.startswith(receipt_prefix)
            or type(effect.result_chunk_key) is not str
            or not effect.result_chunk_key.startswith(chunk_prefix)
            or type(effect.provider_attempt_id) is not str
            or effect.physical_sequence != effect.creator_sequence
            or effect.physical_generation != effect.creator_generation
            or type(effect.requested_model) is not str
            or effect.response_finished_at is None
        ):
            raise AuditIntegrityError("PRIMARY paid overflow lost its incomplete original source shape")
    else:
        raise AuditIntegrityError("primary effect has an unknown durable disposition")
    _require_primary_effect_action_on_connection(
        conn, session_id=session_id, operation_id=operation_id, stack=stack, effect=effect, current_effect=True
    )
    return selected, stack, frame, effect


def begin_primary_provider_on_connection(
    conn: Connection, prepared: PreparedContinuationProvider, *, no_sdk_issuer: PrimaryNoSdkIssuer | None = None
) -> StoredContinuationSnapshot:
    if type(prepared) is not PreparedContinuationProvider or type(prepared.binding) is not ContinuationProviderBinding:
        raise AuditIntegrityError("primary dispatch requires the actual prepared owner result")
    binding, current = prepared.binding, prepared.snapshot
    if no_sdk_issuer is not None:
        if type(no_sdk_issuer) is not PrimaryNoSdkIssuer or no_sdk_issuer.prepared_candidate is not prepared:
            raise AuditIntegrityError("primary dispatch changed its original live no-SDK issuer")
        no_sdk_issuer.clock.require_preparation_time()
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
    stored_primary_input_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect=effect, frame=frame
    )
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
    if effect.action_json is not None:
        from elspeth.web.sessions.continuation import retain_action_execution_receipt_on_connection

        encoded = retain_action_execution_receipt_on_connection(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            effect_key=binding.effect_key,
            disposition="dispatched",
            snapshot_json=encoded,
        )
    result = append_complete_snapshot(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("may_dispatch", canonical_json({"effect_key": binding.effect_key})),),
        handoff=False,
    )
    if no_sdk_issuer is not None:
        no_sdk_issuer.clock.require_preparation_time()
    return result


def _v4_dispatch_action_projection(
    conn: Connection,
    *,
    root: ContinuationFrameStack,
    effect: Row[Any],
    dispatched_json: str,
    disposition: Literal["dispatched", "committed", "refused"] = "dispatched",
) -> tuple[str, PrivateChunk | None]:
    """Predict the existing action-receipt writer's exact bytes before H is reserved."""
    if effect.action_json is None:
        return dispatched_json, None
    from elspeth.web.composer.continuation_progress import (
        ActionExecutionReceipt,
        continuation_action_key,
        decode_continuation_frontier,
        decode_prescribed_continuation_action,
        encode_continuation_frontier,
        record_action_receipt,
    )

    ref = root.semantic_frontier
    if ref is None:
        raise AuditIntegrityError("V4 PRIMARY prescribed dispatch lost its original semantic frontier")
    canonical = read_chunk(
        conn,
        session_id=str(root.logical.session_id),
        operation_id=root.logical.operation_id,
        key=f"value:{ref.codec}:{ref.digest}",
    ).decode("utf-8", errors="strict")
    verify_private_chunk(ref, canonical)
    frontier = decode_continuation_frontier(canonical)
    action = decode_prescribed_continuation_action(effect.action_json)
    receipt = ActionExecutionReceipt(
        continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence}),
        effect.effect_key,
        disposition,
    )
    updated = record_action_receipt(frontier, receipt, actual_owner_receipt_id=effect.effect_key)
    chunk = encode_private_chunk(codec="composer.semantic-frontier.v1", canonical=encode_continuation_frontier(updated))
    predicted_root = decode_continuation_snapshot(dispatched_json).model_copy(update={"semantic_frontier": chunk.reference})
    if disposition in ("committed", "refused"):
        predicted_root = predicted_root.model_copy(update={"active_action_json": None, "active_action_source": None})
    predicted = encode_continuation_snapshot(predicted_root)
    return predicted, chunk


def admit_v4_primary_provider_on_connection(
    conn: Connection,
    service: SessionServiceImpl,
    prepared: PreparedContinuationProvider,
    *,
    model: str,
    no_sdk_issuer: PrimaryNoSdkIssuer,
) -> V4PrimaryProviderAdmission:
    """Mint one physical attempt, reserve H and publish dispatch in one original cut."""
    from elspeth.web.sessions.service import SessionServiceImpl

    if type(service) is not SessionServiceImpl or type(prepared) is not PreparedContinuationProvider:
        raise TypeError("V4 PRIMARY admission requires actual sessions service and prepared source")
    if type(no_sdk_issuer) is not PrimaryNoSdkIssuer or no_sdk_issuer.prepared_candidate is not prepared:
        raise AuditIntegrityError("V4 PRIMARY admission changed its original no-SDK issuer")
    no_sdk_issuer.clock.require_preparation_time()
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
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or binding.family != 0
        or binding.effect_key != f"provider:0:{binding.ordinal}"
        or effect.disposition != "prepared"
        or frame.phase != "awaiting_response"
        or frame.pending_response is not None
        or effect.provider_attempt_id is not None
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
    ):
        raise ContinuationCASConflict("V4 PRIMARY admission lost its one prepared original effect")
    descriptor, _wire, _tools = stored_primary_input_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect=effect, frame=frame
    )
    requested_model = preflight_v4_requested_model(model)
    if descriptor.model != requested_model.value:
        raise AuditIntegrityError("V4 PRIMARY admission model differs from its prepared physical request")
    attempt = begin_provider_attempt_on_connection(
        conn,
        session_operation_context=running.session_operation_context,
        source="composer",
        policy=service._chargeable_admission_policy,
        run_id=None,
    )
    dispatched_json = _effect_snapshot(
        encode_continuation_snapshot(root), effect_key=binding.effect_key, disposition="dispatched", receipt_id=None
    )
    dispatched_json, action_chunk = _v4_dispatch_action_projection(conn, root=root, effect=effect, dispatched_json=dispatched_json)
    transition = ContinuationEvent("may_dispatch", canonical_json({"effect_key": binding.effect_key}))
    published_at = database_now(conn)
    append = prepare_snapshot_append(selected, snapshot_json=dispatched_json, events=(transition,), published_at=published_at)
    admitted = StoredContinuationSnapshot(append.snapshot_json, selected.cursor + 1, append.checkpoint_id)
    context = running.session_operation_context
    fence = context.fence
    identity = PaidOverflowIdentity(
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        effect_key=binding.effect_key,
        attempt_id=attempt.attempt_id,
        session_operation_id=fence.operation_id,
        session_operation_epoch=fence.operation_epoch,
        ownership_generation=running.claim.ownership_generation,
        lease_sha256=sha256(fence.lease_token.encode("utf-8")).hexdigest(),
        policy_sha256=policy.policy_hash,
        source_checkpoint_id=selected.checkpoint_id,
        source_cursor=selected.cursor,
        source_snapshot_sha256=sha256(selected.payload_json.encode("utf-8")).hexdigest(),
    )
    tail = prepare_primary_paid_overflow_tail_plan(identity=identity, admitted=admitted, creator_sequence=running.claim.segment_sequence)
    refused_projection = _effect_snapshot(
        admitted.payload_json,
        effect_key=binding.effect_key,
        disposition="refused",
        receipt_id="00000000-0000-4000-8000-000000000000",
    )
    _refused_json, no_sdk_action_chunk = _v4_dispatch_action_projection(
        conn,
        root=decode_continuation_snapshot(admitted.payload_json),
        effect=effect,
        dispatched_json=refused_projection,
        disposition="refused",
    )
    no_sdk_tail = prepare_primary_no_sdk_tail_plan(
        admitted=admitted,
        attempt_id=attempt.attempt_id,
        requested_model=requested_model.value,
        input_sha256=effect.input_sha256,
        effect_key=binding.effect_key,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        claim_attempt=running.claim.attempt,
        operation_epoch=fence.operation_epoch,
        action_chunk=no_sdk_action_chunk,
    )
    incomplete_tail, reserved_bytes, fallback_contract_sha256 = primary_terminal_reservation(tail, no_sdk_tail)
    hold = ProviderTerminalHoldSource(
        session_id=identity.session_id,
        operation_id=identity.operation_id,
        effect_key=identity.effect_key,
        policy_sha256=identity.policy_sha256,
        allowance_bytes=policy.max_retained_payload_bytes,
        source_checkpoint_id=identity.source_checkpoint_id,
        source_cursor=identity.source_cursor,
        source_snapshot_sha256=identity.source_snapshot_sha256,
        source_input_sha256=effect.input_sha256,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        claim_attempt=running.claim.attempt,
        session_operation_id=fence.operation_id,
        session_operation_epoch=fence.operation_epoch,
        lease_sha256=identity.lease_sha256,
        reserved_bytes=reserved_bytes,
        fallback_contract_sha256=fallback_contract_sha256,
    )
    prospective = (
        *(
            (
                ProspectiveContinuationChunk(
                    private_chunk_key(action_chunk), action_chunk.canonical.encode("utf-8"), action_chunk.reference.codec
                ),
            )
            if action_chunk is not None
            else ()
        ),
        ProspectiveContinuationChunk(f"event:{admitted.cursor}", transition.payload_json.encode("utf-8"), "event.v2"),
        ProspectiveContinuationChunk(f"snapshot:{admitted.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
    )
    reserve_provider_terminal_hold(conn, source=hold, prospective=prospective, created_at=published_at)
    changed = conn.execute(
        update(effects)
        .where(
            effects.c.session_id == identity.session_id,
            effects.c.operation_id == identity.operation_id,
            effects.c.effect_key == identity.effect_key,
            effects.c.disposition == "prepared",
            effects.c.provider_attempt_id.is_(None),
        )
        .values(
            disposition="dispatched",
            provider_attempt_id=attempt.attempt_id,
            physical_sequence=running.claim.segment_sequence,
            physical_generation=running.claim.ownership_generation,
            requested_model=requested_model.value,
        )
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("V4 PRIMARY dispatch lost its original prepared effect CAS")
    bind_provider_terminal_hold_attempt(conn, source=hold, provider_attempt_id=attempt.attempt_id)
    if action_chunk is not None:
        from elspeth.web.sessions.continuation import retain_action_execution_receipt_on_connection

        actual_json = retain_action_execution_receipt_on_connection(
            conn,
            session_id=identity.session_id,
            operation_id=identity.operation_id,
            effect_key=identity.effect_key,
            disposition="dispatched",
            snapshot_json=_effect_snapshot(
                encode_continuation_snapshot(root), effect_key=binding.effect_key, disposition="dispatched", receipt_id=None
            ),
        )
        if actual_json != dispatched_json:
            raise AuditIntegrityError("V4 PRIMARY prescribed dispatch changed its reserved action bytes")
    stored = append_complete_snapshot(
        conn,
        session_id=identity.session_id,
        operation_id=identity.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=append.snapshot_json,
        event_batch=append.events,
        handoff=False,
        prepared_append=append,
    )
    if stored != admitted:
        raise AuditIntegrityError("V4 PRIMARY dispatch changed its reserved complete snapshot")
    no_sdk_issuer.clock.require_preparation_time()
    no_sdk_issuer.note_dispatched_candidate(admitted)
    return V4PrimaryProviderAdmission(attempt, admitted, hold, tail, no_sdk_tail, incomplete_tail)


class _PrimaryNoSdkRefusalData(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    version: Literal[1]
    session_id: str
    operation_id: str
    effect_key: str
    input_sha256: str
    creator_sequence: int
    creator_generation: int
    claim_attempt: int
    operation_epoch: int
    source_checkpoint_id: str
    source_cursor: int
    source_disposition: Literal["prepared", "dispatched"]
    receipt_key: str
    reason: Literal[
        "preparation_expired",
        "pre_sdk_deadline",
        "chargeable_refused",
        "cost_refused",
        "storage_failure",
        "integrity_refused",
        "cancelled_before_sdk",
        "retained_tail_unbounded",
    ]
    public_error_json: str
    undispatched_attempt_id: str | None
    undispatched_model: str | None


@dataclass(frozen=True, slots=True)
class PrimaryNoSdkRefusalReceipt:
    terminal: ComposerOperationRecord
    refused: StoredContinuationSnapshot
    effect_key: str
    receipt_key: str
    result_chunk_key: str
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = field(default=(), compare=False)


def refuse_v4_primary_unentered_on_connection(
    conn: Connection,
    *,
    prepared: PreparedContinuationProvider,
    admission: V4PrimaryProviderAdmission,
    permit: PrimaryNoSdkRefusalPermit,
    append_audit_event: Callable[[str, str, datetime], None],
) -> PrimaryNoSdkRefusalReceipt:
    from elspeth.web.sessions.continuation_primary_paid_overflow_refusal import refuse_v4_primary_unentered_on_connection as refuse

    return refuse(
        conn,
        prepared=prepared,
        admission=admission,
        permit=permit,
        append_audit_event=append_audit_event,
    )


def _primary_no_sdk_event(data: _PrimaryNoSdkRefusalData, result_chunk_key: str) -> str:
    return canonical_json(
        {
            "version": 1,
            "effect_key": data.effect_key,
            "input_sha256": data.input_sha256,
            "receipt_key": data.receipt_key,
            "result_chunk_key": result_chunk_key,
            "source_disposition": data.source_disposition,
        }
    )


def refuse_primary_no_sdk_on_connection(conn: Connection, permit: PrimaryNoSdkRefusalPermit) -> PrimaryNoSdkRefusalReceipt:
    """One fenced transaction for the proved unentered effect and failed terminal."""
    from elspeth.web.coordination.composer_operation_authority import (
        _read,
        _select_failure_on_connection,
        settle_composer_operation_on_connection,
    )
    from elspeth.web.sessions.composer_operation_errors import deadline_expired_error, project_composer_operation_error

    if type(permit) is not PrimaryNoSdkRefusalPermit:
        raise AuditIntegrityError("PRIMARY no-SDK writer requires its actual live permit")
    issuer = permit.issuer
    issuer.require_refusal_permit(permit)
    running = issuer.running
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    source = permit.dispatched if permit.dispatched is not None else permit.prepared.snapshot
    disposition: Literal["prepared", "dispatched"] = "dispatched" if permit.dispatched is not None else "prepared"
    selected, root, frame, effect = _selected_effect(
        conn,
        running,
        checkpoint_id=source.checkpoint_id,
        cursor=source.cursor,
        effect_key=permit.prepared.binding.effect_key,
        input_sha256=permit.prepared.binding.input_sha256,
    )
    if (
        selected != source
        or effect.disposition != disposition
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
    ):
        raise AuditIntegrityError("PRIMARY no-SDK writer lost its exact prepared or dispatched source")
    stored_primary_input_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect=effect, frame=frame
    )
    parent = _read(conn, running.claim.session_id, running.claim.operation_id)
    if parent is None or parent.status != "running":
        raise AuditIntegrityError("PRIMARY no-SDK writer lost its current terminal owner")
    if permit.reason in (PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED, PrimaryNoSdkRefusalReason.PRE_SDK_DEADLINE):
        observed_allowance = (
            issuer.clock.policy.preparation if permit.reason is PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED else issuer.clock.policy.total
        )
        original_error = deadline_expired_error(request_id=parent.request_id, timeout_seconds=observed_allowance)
    else:
        original_error = project_composer_operation_error(permit.original_failure, request_id=parent.request_id)
    selected_error = _select_failure_on_connection(parent, original_error, authoritative_failure=False)
    data = _PrimaryNoSdkRefusalData(
        version=1,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        effect_key=effect.effect_key,
        input_sha256=effect.input_sha256,
        creator_sequence=effect.creator_sequence,
        creator_generation=effect.creator_generation,
        claim_attempt=running.claim.attempt,
        operation_epoch=running.session_operation_context.fence.operation_epoch,
        source_checkpoint_id=source.checkpoint_id,
        source_cursor=source.cursor,
        source_disposition=disposition,
        receipt_key=permit.receipt_key,
        reason=permit.reason.value,
        public_error_json=canonical_json(selected_error.model_dump(mode="json")),
        undispatched_attempt_id=permit.undispatched_attempt_id,
        undispatched_model=permit.undispatched_model,
    )
    chunk = encode_private_chunk(codec="composer.primary-no-sdk-refusal.v1", canonical=canonical_json(data.model_dump(mode="json")))
    result_chunk_key = private_chunk_key(chunk)
    put_chunk(
        conn,
        session_id=data.session_id,
        operation_id=data.operation_id,
        key=result_chunk_key,
        payload=chunk.canonical.encode("utf-8"),
        codec=chunk.reference.codec,
    )
    changed = conn.execute(
        update(effects)
        .where(
            effects.c.session_id == data.session_id,
            effects.c.operation_id == data.operation_id,
            effects.c.effect_key == data.effect_key,
            effects.c.disposition == disposition,
            effects.c.receipt_key.is_(None),
            effects.c.result_chunk_key.is_(None),
            effects.c.provider_attempt_id.is_(None),
        )
        .values(disposition="refused", receipt_key=data.receipt_key, result_chunk_key=result_chunk_key)
    )
    if changed.rowcount != 1:
        raise ContinuationCASConflict("PRIMARY no-SDK refusal lost its exact unresolved effect")
    encoded = _effect_snapshot(
        encode_continuation_snapshot(root), effect_key=data.effect_key, disposition="refused", receipt_id=data.receipt_key
    )
    if effect.action_json is not None:
        from elspeth.web.sessions.continuation import retain_action_execution_receipt_on_connection

        encoded = retain_action_execution_receipt_on_connection(
            conn,
            session_id=data.session_id,
            operation_id=data.operation_id,
            effect_key=data.effect_key,
            disposition="refused",
            snapshot_json=encoded,
        )
    refused = append_complete_snapshot(
        conn,
        session_id=data.session_id,
        operation_id=data.operation_id,
        expected_checkpoint_id=source.checkpoint_id,
        expected_cursor=source.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("effect_refused", _primary_no_sdk_event(data, result_chunk_key)),),
        handoff=False,
    )
    terminal = settle_composer_operation_on_connection(conn, running, outcome=selected_error, authoritative_failure=True)
    if terminal.status != "failed" or terminal.journal_cursor != refused.cursor + 1:
        raise AuditIntegrityError("PRIMARY no-SDK terminal did not atomically follow its refused effect")
    return PrimaryNoSdkRefusalReceipt(terminal, refused, data.effect_key, data.receipt_key, result_chunk_key)


def read_primary_no_sdk_refusal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str
) -> PrimaryNoSdkRefusalReceipt | None:
    """Cold terminal proof; a missing receipt never implies an unentered SDK."""
    from elspeth.web.coordination.composer_operation_authority import _record_from_row

    sid = str(session_id)
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one_or_none()
    if parent is None or parent.status != "failed":
        return None
    terminal = _record_from_row(parent)
    stored = require_snapshot_cas(
        conn,
        session_id=sid,
        operation_id=operation_id,
        expected_checkpoint_id=terminal.checkpoint_id,
        expected_cursor=terminal.journal_cursor,
    )
    root = decode_continuation_snapshot(stored.payload_json)
    frame = _primary_frame(root)
    if frame.provider_effect_key != effect_key or frame.provider_input_sha256 is None:
        return None
    _selected, _root, _frame, effect = selected_primary_effect_data_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        checkpoint_id=stored.checkpoint_id,
        cursor=stored.cursor,
        effect_key=effect_key,
        input_sha256=frame.provider_input_sha256,
    )
    if effect.disposition != "refused":
        return None
    if (
        type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is ContinuationAdmissionPolicyV4
        and effect.provider_attempt_id is not None
    ):
        from elspeth.web.sessions.continuation_primary_paid_overflow_refusal_terminal import read_v4_primary_no_sdk_refusal_on_connection

        return read_v4_primary_no_sdk_refusal_on_connection(conn, session_id=session_id, operation_id=operation_id, effect_key=effect_key)
    chunk_row = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == sid,
            chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == effect.result_chunk_key,
        )
    ).one_or_none()
    if chunk_row is None or chunk_row.codec != "composer.primary-no-sdk-refusal.v1":
        raise AuditIntegrityError("PRIMARY no-SDK receipt has no exact closed result chunk")
    try:
        canonical = read_chunk(conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key).decode(
            "utf-8", errors="strict"
        )
        chunk = encode_private_chunk(codec=chunk_row.codec, canonical=canonical)
        data = _PrimaryNoSdkRefusalData.model_validate_json(canonical, strict=True)
        failure = ComposerOperationError.model_validate_json(data.public_error_json, strict=True)
    except (ValueError, ValidationError) as error:
        raise AuditIntegrityError("PRIMARY no-SDK receipt violates its closed result codec") from error
    if (
        private_chunk_key(chunk) != effect.result_chunk_key
        or canonical_json(data.model_dump(mode="json")) != canonical
        or canonical_json(failure.model_dump(mode="json")) != data.public_error_json
        or terminal.result_json != data.public_error_json
        or terminal.failure_code != failure.failure_code
        or terminal.status != "failed"
        or terminal.settled_by != "owner_terminal"
        or (data.session_id, data.operation_id, data.effect_key, data.input_sha256) != (sid, operation_id, effect_key, effect.input_sha256)
        or (data.creator_sequence, data.creator_generation) != (effect.creator_sequence, effect.creator_generation)
        or (data.claim_attempt, data.operation_epoch) != (terminal.attempt, terminal.session_operation_epoch)
        or data.receipt_key != effect.receipt_key
        or (data.undispatched_attempt_id is None) != (data.undispatched_model is None)
        or terminal.journal_cursor != data.source_cursor + 2
    ):
        raise AuditIntegrityError("PRIMARY no-SDK receipt contradicts its terminal/effect identity")
    source_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == data.source_checkpoint_id,
            checkpoints.c.cursor == data.source_cursor,
        )
    ).one_or_none()
    refusal_row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.cursor == data.source_cursor + 1,
        )
    ).one_or_none()
    refusal_event = conn.execute(
        select(events).where(
            events.c.session_id == sid,
            events.c.operation_id == operation_id,
            events.c.sequence == data.source_cursor + 1,
        )
    ).one_or_none()
    settled_event = conn.execute(
        select(events).where(
            events.c.session_id == sid,
            events.c.operation_id == operation_id,
            events.c.sequence == terminal.journal_cursor,
        )
    ).one_or_none()
    if source_row is None or refusal_row is None or refusal_event is None or settled_event is None:
        raise AuditIntegrityError("PRIMARY no-SDK receipt lost its exact source/refusal/terminal journal")
    try:
        source_stack = decode_continuation_snapshot(
            read_chunk(conn, session_id=sid, operation_id=operation_id, key=source_row.snapshot_chunk_key).decode("utf-8", errors="strict")
        )
        refused_stack = decode_continuation_snapshot(
            read_chunk(conn, session_id=sid, operation_id=operation_id, key=refusal_row.snapshot_chunk_key).decode("utf-8", errors="strict")
        )
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("PRIMARY no-SDK journal lost strict snapshot bytes") from error
    source_frontier = tuple(item for item in source_stack.effect_frontier if item.effect_id == effect_key)
    expected_root = source_stack.model_copy(update={"effect_frontier": root.effect_frontier, "cursor": root.cursor})
    if effect.action_json is not None:
        from elspeth.web.composer.continuation_progress import (
            ActionExecutionReceipt,
            continuation_action_key,
            decode_continuation_frontier,
            decode_prescribed_continuation_action,
            encode_continuation_frontier,
            record_action_receipt,
        )

        if source_stack.semantic_frontier is None:
            raise AuditIntegrityError("PRIMARY no-SDK source lost its original semantic frontier")
        semantic_ref = source_stack.semantic_frontier
        semantic_canonical = read_chunk(
            conn, session_id=sid, operation_id=operation_id, key=f"value:{semantic_ref.codec}:{semantic_ref.digest}"
        ).decode("utf-8", errors="strict")
        verify_private_chunk(semantic_ref, semantic_canonical)
        original_frontier = decode_continuation_frontier(semantic_canonical)
        action = decode_prescribed_continuation_action(effect.action_json)
        action_key = continuation_action_key(action, retained_evidence={item.receipt_id: item for item in original_frontier.evidence})
        updated_frontier = record_action_receipt(
            original_frontier,
            ActionExecutionReceipt(action_key, effect_key, "refused"),
            actual_owner_receipt_id=effect_key,
        )
        expected_ref = encode_private_chunk(
            codec="composer.semantic-frontier.v1", canonical=encode_continuation_frontier(updated_frontier)
        ).reference
        expected_root = expected_root.model_copy(
            update={"semantic_frontier": expected_ref, "active_action_json": None, "active_action_source": None}
        )
    if (
        source_row.kind != "transition"
        or refusal_row.kind != "transition"
        or source_stack.cursor != data.source_cursor
        or len(source_frontier) != 1
        or (source_frontier[0].disposition, source_frontier[0].receipt_id) != (data.source_disposition, None)
        or type(source_stack.frames[-1]) is not ComposeFrame
        or source_stack.frames[-1].provider_effect_key != effect_key
        or source_stack.frames[-1].provider_input_sha256 != effect.input_sha256
        or refused_stack.cursor != data.source_cursor + 1
        or refused_stack.effect_frontier != root.effect_frontier
        or refused_stack.semantic_frontier != root.semantic_frontier
        or refused_stack.active_action_json != root.active_action_json
        or refused_stack.active_action_source != root.active_action_source
        or expected_root != root
        or refusal_event.tag != "effect_refused"
        or read_chunk(conn, session_id=sid, operation_id=operation_id, key=refusal_event.payload_chunk_key)
        != _primary_no_sdk_event(data, effect.result_chunk_key).encode("utf-8")
        or settled_event.tag != "settled"
        or read_chunk(conn, session_id=sid, operation_id=operation_id, key=settled_event.payload_chunk_key)
        != canonical_json(
            {
                "version": 1,
                "sequence": data.creator_sequence,
                "generation": data.creator_generation,
                "status": terminal.status,
                "failure_code": terminal.failure_code,
            }
        ).encode("utf-8")
    ):
        raise AuditIntegrityError("PRIMARY no-SDK journal does not prove one refused source")
    stored_primary_input_data_on_connection(conn, session_id=session_id, operation_id=operation_id, effect=effect, frame=frame)
    if data.undispatched_attempt_id is not None:
        attempt = conn.execute(select(physical).where(physical.c.attempt_id == data.undispatched_attempt_id)).one_or_none()
        owner = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == sid)).scalar_one_or_none()
        entry_id = _usage_entry_id(session_id=sid, source="composer", run_id=None, call_id=data.undispatched_attempt_id)
        ledger = conn.execute(select(usage).where(usage.c.entry_id == entry_id)).one_or_none()
        audit = conn.execute(
            select(messages).where(messages.c.id == _undispatched_cancellation_event_id(data.undispatched_attempt_id))
        ).one_or_none()
        if (
            attempt is None
            or attempt.session_id != sid
            or attempt.source != "composer"
            or attempt.run_id is not None
            or attempt.operation_id != terminal.session_operation_id
            or attempt.operation_epoch != terminal.session_operation_epoch
            or attempt.identity_id != owner
            or attempt.settled_at is None
            or attempt.ledger_entry_id != entry_id
            or ledger is None
            or ledger.identity_id != attempt.identity_id
            or ledger.session_id != sid
            or ledger.source != "composer"
            or ledger.run_id is not None
            or ledger.model != data.undispatched_model
            or any(
                value != 0
                for value in (
                    ledger.prompt_tokens,
                    ledger.completion_tokens,
                    ledger.cached_prompt_tokens,
                    ledger.reasoning_tokens,
                )
            )
            or audit is None
            or audit.session_id != sid
            or audit.role != "audit"
            or audit.writer_principal != "compose_loop"
            or audit.raw_content is not None
            or audit.tool_calls is not None
            or audit.composition_state_id is not None
            or audit.tool_call_id is not None
            or audit.parent_assistant_id is not None
            or restore_utc(audit.created_at) != restore_utc(ledger.recorded_at)
            or restore_utc(audit.created_at) != restore_utc(attempt.settled_at)
        ):
            raise AuditIntegrityError("PRIMARY no-SDK admitted attempt lacks its exact zero-use cancellation cohort")
        if audit.content != canonical_json(
            {
                "kind": "provider_attempt.cancelled_before_dispatch",
                "attempt_id": data.undispatched_attempt_id,
                "source": "composer",
                "run_id": None,
                "requested_model": data.undispatched_model,
                "operation_id": terminal.session_operation_id,
                "operation_epoch": terminal.session_operation_epoch,
                "lease_token_sha256": sha256(attempt.lease_token.encode("utf-8")).hexdigest(),
            }
        ):
            raise AuditIntegrityError("PRIMARY no-SDK admitted attempt changed its cancellation audit")
    return PrimaryNoSdkRefusalReceipt(
        terminal,
        StoredContinuationSnapshot(encode_continuation_snapshot(refused_stack), refusal_row.cursor, refusal_row.checkpoint_id),
        effect_key,
        effect.receipt_key,
        effect.result_chunk_key,
    )


def _physical_source(conn: Connection, running: ComposerOperationRunning, effect: Row[Any], call: ComposerLLMCall) -> str:
    return _physical_source_data(conn, running.claim.session_id, running.claim.operation_id, effect, call)


def _require_primary_response_audit(stack: ContinuationFrameStack, completion: _AdmittedLLMCompletion, call: ComposerLLMCall) -> None:
    """Compare diagnostics only after the caller has proved the original source."""
    if call.call_id is None:
        raise AuditIntegrityError("PRIMARY response lost its original call identity")
    policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    if type(policy) is ContinuationAdmissionPolicyV4:
        from elspeth.web.composer.continuation_provider_outcomes import require_v4_successful_response_audit

        require_retained_v4_provider_metadata_credentials(completion, surface="composer_primary_response")
        require_v4_successful_response_audit(
            call,
            completion.provider_metadata,
            policy=policy,
            attempt_id=call.call_id,
            started_at=call.started_at,
            finished_at=call.finished_at,
        )
    else:
        require_successful_response_audit(
            call,
            completion.provider_metadata,
            attempt_id=call.call_id,
            started_at=call.started_at,
            finished_at=call.finished_at,
        )


def _require_v4_complete_hold_consumed_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    stack: ContinuationFrameStack,
    effect: Row[Any],
    call: ComposerLLMCall,
) -> None:
    if type(decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)) is not ContinuationAdmissionPolicyV4:
        return
    from elspeth.web.sessions.continuation_primary_paid_overflow_terminal import _stored_hold_source
    from elspeth.web.sessions.continuation_retained_capacity import _require_hold_fields, load_continuation_read_contract

    source = _stored_hold_source(conn, session_id=str(session_id), operation_id=operation_id, effect_key=effect.effect_key)
    if source is None:
        raise AuditIntegrityError("V4 PRIMARY cold complete result lost its original H")
    contract = load_continuation_read_contract(conn, session_id=str(session_id), operation_id=operation_id)
    hold = _require_hold_fields(conn, source=source, contract=contract)
    if (
        source.policy_sha256 != stack.policy_hash
        or source.source_input_sha256 != effect.input_sha256
        or hold.state != "consumed"
        or hold.provider_attempt_id != call.call_id
    ):
        raise AuditIntegrityError("V4 PRIMARY cold complete result changed its consumed original H")


def _physical_source_data(conn: Connection, session_id: UUID, operation_id: str, effect: Row[Any], call: ComposerLLMCall) -> str:
    if type(call) is not ComposerLLMCall or call.call_id is None:
        raise AuditIntegrityError("primary result requires the actual original physical audit")
    source = _stored_descriptor_data(conn, session_id, operation_id, effect)
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
            attempts.c.session_id == str(session_id),
            attempts.c.operation_id == operation_id,
            attempts.c.sequence == effect.creator_sequence,
            attempts.c.generation == effect.creator_generation,
        )
    ).one()
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == call.call_id)).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == str(session_id))).scalar_one()
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
            str(session_id),
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
    complete_observation: PrimaryCompleteObservation | None = None,
    prepared_append: PreparedContinuationAppend | None = None,
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
    v4 = type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is ContinuationAdmissionPolicyV4
    if v4:
        from elspeth.web.composer.continuation_primary_observation import PrimaryCompleteObservation

        if (
            type(complete_observation) is not PrimaryCompleteObservation
            or complete_observation.outcome is not outcome
            or type(prepared_append) is not PreparedContinuationAppend
        ):
            raise AuditIntegrityError("V4 PRIMARY complete publication lost its exact observed result batch")
    elif complete_observation is not None or prepared_append is not None:
        raise AuditIntegrityError("legacy PRIMARY publication cannot consume V4 complete batch credit")
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
        or (
            v4
            and (
                effect.provider_attempt_id != outcome.audit_call.call_id
                or effect.physical_sequence != effect.creator_sequence
                or effect.physical_generation != effect.creator_generation
                or effect.requested_model != outcome.audit_call.model_requested
            )
        )
        or (
            not v4
            and (
                effect.provider_attempt_id is not None
                or effect.physical_sequence is not None
                or effect.physical_generation is not None
                or effect.requested_model is not None
            )
        )
        or effect.response_finished_at is not None
    ):
        raise ContinuationCASConflict("primary publication cannot replace a result or its original dispatched owner")
    call = outcome.audit_call
    if call.call_id is None:
        raise AuditIntegrityError("primary publication requires its original physical identity")
    if v4:
        metadata = outcome.completion.provider_metadata
        witness = metadata.v4_completeness
        if type(witness) is not V4ProviderResultCompleteness or witness.sdk_return.attempt_id != call.call_id:
            raise AuditIntegrityError("V4 PRIMARY publication lost its original complete SDK return")
        witness.require_result(metadata, content=outcome.completion.message.content, tool_calls=outcome.completion.tool_batch.calls)
    # Publication still owns the original selected input. A later audit read
    # instead joins immutable paid-input DATA after the transcript has grown.
    stored_primary_input_data_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, effect=effect, frame=frame
    )
    _physical_source(conn, running, effect, call)
    _require_primary_response_audit(root, outcome.completion, call)
    previous = decode_recorder_snapshot(_chunk(conn, running, frame.audit_buffer))
    if replace(previous, llm_calls=(*previous.llm_calls, call)) != recorder or not recorder.llm_calls or recorder.llm_calls[-1] is not call:
        raise AuditIntegrityError("primary publication changed its complete original recorder prefix/channels")
    response = encode_continuation_response(outcome.completion, audit_call=call)
    from elspeth.web.sessions.continuation_selection import load_private_chunks

    evidence = decode_turn_evidence(
        _chunk(conn, running, frame.evidence),
        env_ref_names=env_ref_names,
        source_chunks=load_private_chunks(
            conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, stack=root
        ),
    )
    if evidence.snapshot.recorder != frame.audit_buffer or evidence.recorder_chunk != encode_recorder_snapshot(previous):
        raise AuditIntegrityError("primary publication changed its actual evidence/recorder prefix join")
    extended_evidence = append_primary_call_to_evidence(evidence, call=call, recorder=recorder, env_ref_names=env_ref_names)
    audit = extended_evidence.recorder_chunk
    updated_evidence = encode_turn_evidence(extended_evidence, env_ref_names=env_ref_names)
    if complete_observation is not None and (
        complete_observation.response != response
        or complete_observation.recorder != audit
        or complete_observation.evidence != updated_evidence
    ):
        raise AuditIntegrityError("V4 PRIMARY publication changed its original observed complete bytes")
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
    if effect.action_json is not None:
        from elspeth.web.sessions.continuation import retain_action_execution_receipt_on_connection

        encoded = retain_action_execution_receipt_on_connection(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            effect_key=binding.effect_key,
            disposition="committed",
            snapshot_json=encoded,
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
        prepared_append=prepared_append,
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
    _physical_source(conn, running, effect, call)
    _require_v4_complete_hold_consumed_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        stack=_root,
        effect=effect,
        call=call,
    )
    _require_primary_response_audit(_root, completion, call)
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
    return _read_committed_primary_response_data_on_connection(conn, running, selected=selected, reference=reference)


def read_committed_primary_response_for_settlement_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    reference: PrivateChunkRef,
) -> PrivateChunk:
    """Read an existing paid result while retaining an issued effect after Stop.

    This facet grants no dispatch or business mutation. Its caller still proves
    the exact pre-Stop issue before writing any owed retention.
    """
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    return _read_committed_primary_response_data_on_connection(conn, running, selected=selected, reference=reference)


def read_retained_primary_phase_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    reference: PrivateChunkRef,
    env_ref_names: frozenset[str],
) -> RetainedPrimaryPhaseData:
    """Read actual historical response DATA joined to the complete current graph."""
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection

    response = read_committed_primary_response_for_settlement_on_connection(conn, running, selected=selected, reference=reference)
    compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
    if len(compose) != 1 or compose[0].pending_response != reference:
        raise AuditIntegrityError("retained phase changed its actual selected primary response")
    admitted = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=env_ref_names)
    call = decode_retained_physical_audit_call(response)
    recorder = decode_recorder_snapshot(admitted.recorder_chunk)
    matching = tuple(item for item in recorder.llm_calls if item.call_id == call.call_id)
    if len(matching) != 1 or matching[0] != call:
        raise AuditIntegrityError("retained phase lost its actual paid call in the complete current recorder")
    return RetainedPrimaryPhaseData(response, _CallModelOutcome(decode_continuation_response(response), matching[0]))


def _read_committed_primary_response_data_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    reference: PrivateChunkRef,
) -> PrivateChunk:
    _require_selected_primary_source(conn, running, selected)
    return read_committed_primary_response_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        selected=selected,
        reference=reference,
    )


def read_committed_primary_response_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    selected: SelectedContinuationState,
    reference: PrivateChunkRef,
) -> PrivateChunk:
    """Immutable paid-response DATA after an independent locked SQL proof."""
    from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection

    expected = require_snapshot_cas(
        conn,
        session_id=str(session_id),
        operation_id=operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.stack.cursor,
    )
    if (
        expected.payload_json != encode_continuation_snapshot(selected.stack)
        or load_snapshot_private_data_on_connection(conn, session_id=str(session_id), operation_id=operation_id, selected=expected)
        != selected.chunks
    ):
        raise ContinuationCASConflict("primary source read changed its complete selected chunk cohort")
    return _verify_committed_primary_response_data_on_connection(
        conn, session_id=session_id, operation_id=operation_id, selected=selected, reference=reference
    )


def read_original_committed_primary_response_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    source_checkpoint_id: str,
    source_cursor: int,
    response: PrivateChunkRef,
) -> PrivateChunk:
    """Prove immutable PRIMARY paid DATA from one original checkpoint.

    This grants no current-head, effect-write, provider or replay authority.
    The caller separately proves why this historical checkpoint is its source.
    """
    from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection

    sid = str(session_id)
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation_id,
            checkpoints.c.checkpoint_id == source_checkpoint_id,
            checkpoints.c.cursor == source_cursor,
        )
    ).one_or_none()
    if (
        row is None
        or row.kind not in ("transition", "handoff")
        or row.frame_stack_version != 1
        or row.snapshot_chunk_key != f"snapshot:{source_checkpoint_id}"
    ):
        raise AuditIntegrityError("historical PRIMARY source lost its exact original checkpoint")
    snapshot_json = read_chunk(conn, session_id=sid, operation_id=operation_id, key=row.snapshot_chunk_key).decode("utf-8", errors="strict")
    stack = decode_continuation_snapshot(snapshot_json)
    decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    if stack.cursor != source_cursor or (stack.logical.session_id, stack.logical.operation_id) != (sid, operation_id):
        raise AuditIntegrityError("historical PRIMARY source changed its original root identity")
    stored = StoredContinuationSnapshot(snapshot_json, source_cursor, source_checkpoint_id)
    selected = SelectedContinuationState(
        source_checkpoint_id,
        stack,
        load_snapshot_private_data_on_connection(conn, session_id=sid, operation_id=operation_id, selected=stored),
    )
    if all(chunk.reference != response for chunk in selected.chunks):
        raise AuditIntegrityError("historical PRIMARY response is absent from its original complete private closure")
    return _verify_committed_primary_response_data_on_connection(
        conn, session_id=session_id, operation_id=operation_id, selected=selected, reference=response
    )


def _verify_committed_primary_response_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    selected: SelectedContinuationState,
    reference: PrivateChunkRef,
) -> PrivateChunk:
    if type(reference) is not PrivateChunkRef or reference.codec != "composer.response.v1":
        raise AuditIntegrityError("historical primary response lost its actual source codec")
    key = f"value:{reference.codec}:{reference.digest}"
    found = conn.execute(
        select(effects).where(
            effects.c.session_id == str(session_id),
            effects.c.operation_id == operation_id,
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
    _require_primary_effect_action_on_connection(
        conn, session_id=session_id, operation_id=operation_id, stack=selected.stack, effect=effect, current_effect=False
    )
    response = _chunk_data(conn, session_id, operation_id, reference)
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
    _physical_source_data(conn, session_id, operation_id, effect, call)
    _require_v4_complete_hold_consumed_on_connection(
        conn, session_id=session_id, operation_id=operation_id, stack=selected.stack, effect=effect, call=call
    )
    _require_primary_response_audit(selected.stack, completion, call)
    return response
