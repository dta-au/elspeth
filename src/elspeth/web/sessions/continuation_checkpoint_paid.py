"""Original V4 EARLY/END checkpoint source and per-physical-attempt SQL custody.

The source preparation and dispatch cuts are separate: a detached runtime must
restore from the prepared cut before the original paid attempt is minted.
Neither a pending child nor a terminal receipt grants generic redispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from sqlalchemy import Connection, insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import ComposeFrame, EndFrame, PrivateChunkRef, ToolBatchFrame
from elspeth.web.composer.continuation_checkpoint_end_input import (
    CapturedCheckpointEndInput,
    decode_checkpoint_end_input,
    require_original_checkpoint_end_env_names,
    require_original_checkpoint_end_snapshot,
)
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_end_effects import end_effect_key
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_runtime import CapturedContinuationRuntime
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import ProviderAttempt, begin_provider_attempt_on_connection
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkRole
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationEvent,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    prepare_snapshot_append,
    private_chunk_key,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.continuation_checkpoint_paid_tail import CheckpointPaidOverflowTailPlan
from elspeth.web.sessions.continuation_checkpoint_terminal_hold import (
    CheckpointTerminalHoldPlan,
    prepare_checkpoint_terminal_hold_plan,
)
from elspeth.web.sessions.continuation_paid_overflow import CANONICAL_INTEGER_MAX, PaidOverflowIdentity
from elspeth.web.sessions.continuation_paid_overflow_quota import preflight_v4_requested_model
from elspeth.web.sessions.continuation_retained_capacity import (
    ProspectiveContinuationChunk,
    ProviderTerminalHoldSource,
    _require_active_provider_hold,
    bind_provider_terminal_hold_attempt,
    load_continuation_read_contract,
    read_bounded_retained_chunk,
    reserve_provider_terminal_hold,
)
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_continuation_provider_ordinals_table as ordinals
from elspeth.web.sessions.models import quota_provider_attempts_table as physical

if TYPE_CHECKING:
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.service import SessionServiceImpl

type CheckpointPhase = Literal["early", "end"]
type CheckpointPhaseKind = Literal["iteration_early", "iteration_end", "end_review"]


@dataclass(frozen=True, slots=True)
class CheckpointPhaseSource:
    phase: CheckpointPhase
    kind: CheckpointPhaseKind
    source: StoredContinuationSnapshot
    current: StoredContinuationSnapshot
    outer_effect_key: str
    completed_children: tuple[str, ...] = ()
    terminal: bool = False


@dataclass(frozen=True, slots=True)
class PreparedCheckpointSource:
    running: ComposerOperationRunning
    selected: SelectedContinuationState
    captured_before: CapturedContinuationRuntime = field(repr=False)
    phase: CheckpointPhase
    pass_index: int
    work: RequiredWorkBinding = field(repr=False)
    composer: ComposerServiceImpl = field(repr=False)
    authority: ComposerAsyncOperationAuthority = field(repr=False)
    end_input: CapturedCheckpointEndInput | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class CheckpointPreparedSource:
    source: StoredContinuationSnapshot
    prepared_selected: StoredContinuationSnapshot
    phase: CheckpointPhase
    kind: CheckpointPhaseKind
    outer_effect_key: str
    pass_index: int
    end_input_ref: PrivateChunkRef | None
    end_env_ref_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedCheckpointProvider:
    running: ComposerOperationRunning
    prepared_source: CheckpointPreparedSource
    work: RequiredWorkBinding = field(repr=False)
    composer: ComposerServiceImpl = field(repr=False)
    authority: ComposerAsyncOperationAuthority = field(repr=False)
    retry_ordinal: int
    request_sha256: str
    requested_model: str
    env_ref_names: frozenset[str]
    publication_binding: str | None


@dataclass(frozen=True, slots=True)
class V4CheckpointProviderAdmission:
    attempt: ProviderAttempt
    prepared_selected: StoredContinuationSnapshot
    selected: StoredContinuationSnapshot
    hold: ProviderTerminalHoldSource
    paid_tail: CheckpointPaidOverflowTailPlan
    terminal_hold: CheckpointTerminalHoldPlan
    effect_key: str
    original_source: CheckpointPreparedSource


def _effect_inventory(conn: Connection, sid: str, operation: str, selected: StoredContinuationSnapshot) -> None:
    root = decode_continuation_snapshot(selected.payload_json)
    physical = set(
        conn.execute(
            select(effects.c.effect_key, effects.c.disposition, effects.c.receipt_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation
            )
        ).tuples()
    )
    if physical != {(item.effect_id, item.disposition, item.receipt_id) for item in root.effect_frontier}:
        raise AuditIntegrityError("checkpoint phase lost its complete physical effects inventory")


def _event_text(conn: Connection, sid: str, operation: str, cursor: int) -> tuple[str, str]:
    row = conn.execute(
        select(events.c.tag, events.c.payload_chunk_key).where(
            events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == cursor
        )
    ).one_or_none()
    if row is None:
        raise AuditIntegrityError("checkpoint phase lost its actual journal event")
    return row.tag, read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key).decode("utf-8", errors="strict")


def _initial_phase_source(
    conn: Connection, *, session_id: UUID, operation_id: str, selected: StoredContinuationSnapshot
) -> CheckpointPhaseSource | None:
    from elspeth.web.composer.continuation_classifier_handoff import ClassifyPostPrimary, ClassifySourceProofStop
    from elspeth.web.sessions.continuation_iteration_return import (
        _encode_pending,
        _end_key,
        read_iteration_stage_data_on_connection,
    )

    sid = str(session_id)
    root = decode_continuation_snapshot(selected.payload_json)
    _effect_inventory(conn, sid, operation_id, selected)
    top = root.frames[-1]
    if type(top) is ToolBatchFrame:
        stage = read_iteration_stage_data_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=selected)
        if stage is None or stage.current != selected:
            return None
        phase: CheckpointPhase
        kind: CheckpointPhaseKind
        if stage.stage == "early_dispatched":
            phase, kind, key = "early", "iteration_early", stage.early_effect_key
        elif stage.stage == "end_dispatched" and isinstance(stage.pending, (ClassifyPostPrimary, ClassifySourceProofStop)):
            phase, kind, key = "end", "iteration_end", _end_key(_encode_pending(stage.pending))
        else:
            return None
    elif type(top) is EndFrame and top.phase == "review_needed" and len(root.frames) == 2:
        compose = root.frames[0]
        if type(compose) is not ComposeFrame or compose.pending_response is None:
            raise AuditIntegrityError("checkpoint END review lost its original retained PRIMARY")
        phase, kind = "end", "end_review"
        key = end_effect_key(compose.pending_response)
        if root.active_action_source is not None:
            if root.active_action_json is None:
                raise AuditIntegrityError("checkpoint END review lost its original prescribed action")
            from elspeth.web.sessions.continuation_semantic_actions import read_original_dispatched_semantic_review_on_connection

            semantic = read_original_dispatched_semantic_review_on_connection(
                conn,
                session_id=session_id,
                operation_id=operation_id,
                dispatch_checkpoint_id=selected.checkpoint_id,
                semantic_effect_key=root.active_action_source.effect_key,
            )
            if semantic.owner_effect_key is None:
                raise AuditIntegrityError("checkpoint END review lost its original semantic effect")
            key = semantic.owner_effect_key
        input_sha = stable_hash({"schema": "composer.end-review.v1", "candidate": top.candidate.digest})
        tag, text = _event_text(conn, sid, operation_id, selected.cursor)
        if tag != "may_dispatch" or text != canonical_json(
            {"owner": "composer_end", "transition": "end_review_dispatched", "effect_key": key, "input_sha256": input_sha}
        ):
            raise AuditIntegrityError("checkpoint END review lost its exact original dispatch event")
    else:
        return None
    matching = tuple(item for item in root.effect_frontier if item.effect_id == key)
    row = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == key)
    ).one_or_none()
    if (
        len(matching) != 1
        or matching[0].disposition != "dispatched"
        or matching[0].receipt_id is not None
        or row is None
        or row.disposition != "dispatched"
        or row.provider_attempt_id is not None
        or row.receipt_key is not None
        or row.result_chunk_key is not None
        or row.owner != ("composer_end" if kind == "end_review" else "composer_iteration")
    ):
        raise AuditIntegrityError("checkpoint outer phase lost its unpaid original effect")
    return CheckpointPhaseSource(phase, kind, selected, selected, key)


def checkpoint_phase_source_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, selected: StoredContinuationSnapshot
) -> CheckpointPhaseSource | None:
    """Cold phase DATA; a pending paid child never authorizes redispatch."""
    if type(session_id) is not UUID or type(operation_id) is not str or type(selected) is not StoredContinuationSnapshot:
        raise TypeError("checkpoint phase source requires exact original SQL coordinates")
    current = load_snapshot(conn, session_id=str(session_id), operation_id=operation_id)
    if current != selected:
        raise AuditIntegrityError("checkpoint phase source replaced its actual current head")
    return _initial_phase_source(conn, session_id=session_id, operation_id=operation_id, selected=selected)


def prepare_v4_checkpoint_source_on_connection(
    conn: Connection, service: SessionServiceImpl, prepared: PreparedCheckpointSource
) -> CheckpointPreparedSource:
    """Retain the actual pre-call live capture before minting any paid attempt."""
    from elspeth.web.sessions.continuation_iteration_return import _require_compose_capture
    from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection

    if (
        type(prepared) is not PreparedCheckpointSource
        or type(prepared.running) is not ComposerOperationRunning
        or type(prepared.selected) is not SelectedContinuationState
        or type(prepared.captured_before) is not CapturedContinuationRuntime
        or prepared.captured_before.selected is not prepared.selected
        or type(prepared.work) is not RequiredWorkBinding
        or prepared.work.running is not prepared.running
        or prepared.composer._require_sessions_service() is not service
        or type(prepared.pass_index) is not int
        or prepared.pass_index < 0
        or prepared.phase not in ("early", "end")
    ):
        raise AuditIntegrityError("checkpoint source preparation changed its original runtime owners")
    running = prepared.running
    _require_current_continuation_running(conn, running)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    selected = prepared.selected
    source = load_snapshot(conn, session_id=sid, operation_id=operation)
    if source.checkpoint_id != selected.checkpoint_id or source.payload_json != encode_continuation_snapshot(selected.stack):
        raise AuditIntegrityError("checkpoint preparation replaced its actual selected source")
    if load_snapshot_private_data_on_connection(conn, session_id=sid, operation_id=operation, selected=source) != selected.chunks:
        raise AuditIntegrityError("checkpoint preparation changed its actual selected private DATA")
    phase = _initial_phase_source(conn, session_id=running.claim.session_id, operation_id=operation, selected=source)
    if phase is None or phase.phase != prepared.phase:
        raise AuditIntegrityError("checkpoint preparation has no original dispatched EARLY/END phase")
    end_input = None
    end_env_ref_names: tuple[str, ...] = ()
    if phase.phase == "end":
        if type(prepared.end_input) is not CapturedCheckpointEndInput:
            raise AuditIntegrityError("END checkpoint preparation lacks its original complete gate input")
        end_input, gate = require_original_checkpoint_end_snapshot(
            prepared.end_input, phase_source=phase, captured=prepared.captured_before
        )
        end_env_ref_names = tuple(
            sorted(require_original_checkpoint_end_env_names(prepared.end_input, phase_source=phase, captured=prepared.captured_before))
        )
        if gate.pass_index != prepared.pass_index:
            raise AuditIntegrityError("END checkpoint preparation changed its original pass/retry source")
    elif prepared.end_input is not None:
        raise AuditIntegrityError("EARLY checkpoint cannot borrow END gate input")
    before = selected.stack
    captured = prepared.captured_before
    if (
        captured.stack.model_copy(update={"frames": before.frames}) != before
        or len(captured.stack.frames) != len(before.frames)
        or captured.stack.frames[1:] != before.frames[1:]
        or type(before.frames[0]) is not ComposeFrame
        or type(captured.stack.frames[0]) is not ComposeFrame
    ):
        raise AuditIntegrityError("checkpoint preparation changed its original outer phase or immutable root")
    _require_compose_capture(before.frames[0], captured.stack.frames[0])
    if before.pending_settlement_ids or before.pending_owner_results:
        raise AuditIntegrityError("checkpoint source preparation cannot borrow pending physical work")
    for chunk in captured.chunks:
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
    if end_input is not None:
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=private_chunk_key(end_input),
            payload=end_input.canonical.encode("utf-8"),
            codec=end_input.reference.codec,
        )
    captured_root_sha256 = sha256(
        encode_continuation_snapshot(captured.stack.model_copy(update={"cursor": source.cursor + 1})).encode("utf-8")
    ).hexdigest()
    transition = ContinuationEvent(
        "checkpoint",
        canonical_json(
            {
                "owner": "composer_checkpoint_source",
                "version": 1,
                "stage": "source_prepared",
                "phase": phase.phase,
                "kind": phase.kind,
                "pass_index": prepared.pass_index,
                "outer_effect_key": phase.outer_effect_key,
                "source_checkpoint_id": source.checkpoint_id,
                "source_cursor": source.cursor,
                "creator_sequence": running.claim.segment_sequence,
                "creator_generation": running.claim.ownership_generation,
                "end_input_chunk_key": private_chunk_key(end_input) if end_input is not None else None,
                "end_input_sha256": end_input.reference.digest if end_input is not None else None,
                "end_env_ref_names": end_env_ref_names,
                "captured_root_sha256": captured_root_sha256,
            }
        ),
    )
    append = prepare_snapshot_append(
        source,
        snapshot_json=encode_continuation_snapshot(captured.stack),
        events=(transition,),
        published_at=database_now(conn),
    )
    stored = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=source.checkpoint_id,
        expected_cursor=source.cursor,
        snapshot_json=append.snapshot_json,
        event_batch=append.events,
        handoff=False,
        prepared_append=append,
    )
    if sha256(stored.payload_json.encode("utf-8")).hexdigest() != captured_root_sha256:
        raise AuditIntegrityError("checkpoint source changed its bound captured root")
    return CheckpointPreparedSource(
        source,
        stored,
        phase.phase,
        phase.kind,
        phase.outer_effect_key,
        prepared.pass_index,
        end_input.reference if end_input is not None else None,
        end_env_ref_names,
    )


def _require_prepared_checkpoint_source_on_connection(conn: Connection, prepared: PreparedCheckpointProvider) -> CheckpointPreparedSource:
    if (
        type(prepared) is not PreparedCheckpointProvider
        or type(prepared.running) is not ComposerOperationRunning
        or type(prepared.prepared_source) is not CheckpointPreparedSource
        or type(prepared.work) is not RequiredWorkBinding
        # The ADVISOR owner mints a new provider-ordinal binding from the
        # original TURN coordinator. Unlike source-preparation work, that
        # minted binding deliberately has no `running` field.
        or prepared.work.running is not None
        or prepared.work.role is not RequiredWorkRole.TURN
        or prepared.work.coordinator.authority.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
        or prepared.work.coordinator.authority.context is not prepared.running.session_operation_context
        or prepared.work.coordinator.authority.durable_operation_id != prepared.running.claim.operation_id
        or prepared.work.coordinator.authority.claim_attempt != prepared.running.claim.attempt
        or type(prepared.retry_ordinal) is not int
        or prepared.retry_ordinal < 0
        or not is_lower_sha256_hex(prepared.request_sha256)
        or type(prepared.requested_model) is not str
        or type(prepared.env_ref_names) is not frozenset
        or (prepared.prepared_source.phase == "end" and prepared.env_ref_names != frozenset(prepared.prepared_source.end_env_ref_names))
        or (prepared.publication_binding is not None and not is_lower_sha256_hex(prepared.publication_binding))
    ):
        raise AuditIntegrityError("checkpoint physical admission lost its nominal original preparation")
    source = prepared.prepared_source
    running = prepared.running
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    if load_snapshot(conn, session_id=sid, operation_id=operation) != source.prepared_selected:
        raise AuditIntegrityError("checkpoint physical admission replaced its prepared current head")
    row = conn.execute(
        select(checkpoints.c.cursor, checkpoints.c.snapshot_chunk_key).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == operation,
            checkpoints.c.checkpoint_id == source.source.checkpoint_id,
        )
    ).one_or_none()
    if row is None or row.cursor != source.source.cursor:
        raise AuditIntegrityError("checkpoint preparation lost its original source checkpoint")
    original = read_chunk(conn, session_id=sid, operation_id=operation, key=row.snapshot_chunk_key).decode("utf-8", errors="strict")
    if original != source.source.payload_json or source.prepared_selected.cursor != source.source.cursor + 1:
        raise AuditIntegrityError("checkpoint preparation changed its original source bytes")
    tag, text = _event_text(conn, sid, operation, source.prepared_selected.cursor)
    expected = canonical_json(
        {
            "owner": "composer_checkpoint_source",
            "version": 1,
            "stage": "source_prepared",
            "phase": source.phase,
            "kind": source.kind,
            "pass_index": source.pass_index,
            "outer_effect_key": source.outer_effect_key,
            "source_checkpoint_id": source.source.checkpoint_id,
            "source_cursor": source.source.cursor,
            "creator_sequence": running.claim.segment_sequence,
            "creator_generation": running.claim.ownership_generation,
            "end_input_chunk_key": f"value:{source.end_input_ref.codec}:{source.end_input_ref.digest}"
            if source.end_input_ref is not None
            else None,
            "end_input_sha256": source.end_input_ref.digest if source.end_input_ref is not None else None,
            "end_env_ref_names": source.end_env_ref_names,
            "captured_root_sha256": sha256(source.prepared_selected.payload_json.encode("utf-8")).hexdigest(),
        }
    )
    if tag != "checkpoint" or text != expected:
        raise AuditIntegrityError("checkpoint preparation changed its exact source event")
    root = decode_continuation_snapshot(source.prepared_selected.payload_json)
    before = decode_continuation_snapshot(source.source.payload_json)
    if (
        root.cursor != source.prepared_selected.cursor
        or root.model_copy(update={"frames": before.frames, "cursor": before.cursor}) != before
        or source.phase not in ("early", "end")
        or source.kind not in ("iteration_early", "iteration_end", "end_review")
        or (source.kind == "iteration_early") != (source.phase == "early")
        or type(source.pass_index) is not int
        or source.pass_index < 0
        or source.outer_effect_key not in {item.effect_id for item in root.effect_frontier}
        or root.pending_settlement_ids
        or (source.phase == "end") != (source.end_input_ref is not None)
        or (source.phase == "early" and source.end_env_ref_names != ())
        or tuple(sorted(set(source.end_env_ref_names))) != source.end_env_ref_names
    ):
        raise AuditIntegrityError("checkpoint preparation changed its original outer phase root")
    if source.end_input_ref is not None:
        from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection

        reference = source.end_input_ref
        key = f"value:{reference.codec}:{reference.digest}"
        contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation)
        if type(contract.allowance) is not int:
            raise AuditIntegrityError("END checkpoint input lost its original bounded allowance")
        chunk = read_bounded_retained_chunk(conn, session_id=sid, operation_id=operation, key=key, allowance=contract.allowance)
        if (
            chunk is None
            or chunk.codec != reference.codec
            or sha256(chunk.payload).hexdigest() != reference.digest
            or len(chunk.payload) != reference.byte_length
        ):
            raise AuditIntegrityError("END checkpoint input lost its original retained binding")
        input_data = decode_checkpoint_end_input(
            PrivateChunk(reference, chunk.payload.decode("utf-8", errors="strict")), env_ref_names=frozenset(source.end_env_ref_names)
        )
        if (
            input_data.session_id != sid
            or input_data.operation_id != operation
            or input_data.original_checkpoint_id != source.source.checkpoint_id
            or input_data.original_cursor != source.source.cursor
            or input_data.outer_effect_key != source.outer_effect_key
            or input_data.pass_index != source.pass_index
            or input_data.retry_ordinal != prepared.retry_ordinal
            or input_data.captured_runtime_sha256
            != stable_hash(root.model_copy(update={"cursor": source.source.cursor}).model_dump(mode="json"))
            or input_data.runtime_chunks
            != tuple(
                value.reference
                for value in load_snapshot_private_data_on_connection(
                    conn, session_id=sid, operation_id=operation, selected=source.prepared_selected
                )
            )
        ):
            raise AuditIntegrityError("END checkpoint input changed its original gate source")
    _effect_inventory(conn, sid, operation, source.prepared_selected)
    return source


def admit_v4_checkpoint_provider_on_connection(
    conn: Connection, service: SessionServiceImpl, prepared: PreparedCheckpointProvider, *, model: str
) -> V4CheckpointProviderAdmission:
    """Mint one original physical attempt and exact H before SDK entry."""
    if (
        type(prepared) is not PreparedCheckpointProvider
        or prepared.composer._require_sessions_service() is not service
        or model != prepared.requested_model
    ):
        raise AuditIntegrityError("checkpoint admission changed its original provider/service/model")
    running = prepared.running
    _require_current_continuation_running(conn, running)
    source = _require_prepared_checkpoint_source_on_connection(conn, prepared)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    root = decode_continuation_snapshot(source.prepared_selected.payload_json)
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4 or policy.max_retained_payload_bytes >= CANONICAL_INTEGER_MAX:
        raise AuditIntegrityError("checkpoint paid hold requires the original bounded V4 policy")
    requested = preflight_v4_requested_model(model)
    if any(item.disposition == "paid_overflow" for item in root.effect_frontier):
        raise AuditIntegrityError("checkpoint paid terminal cannot re-enter the SDK")
    old = conn.execute(
        select(ordinals.c.next_ordinal).where(ordinals.c.session_id == sid, ordinals.c.operation_id == operation, ordinals.c.family == 1)
    ).scalar_one_or_none()
    ordinal = 0 if old is None else old
    key = f"provider:1:{ordinal}"
    if any(item.effect_id == key for item in root.effect_frontier):
        raise AuditIntegrityError("checkpoint child would reuse a physical provider effect")
    attempt = begin_provider_attempt_on_connection(
        conn,
        session_operation_context=running.session_operation_context,
        source="composer",
        policy=service._chargeable_admission_policy,
        run_id=None,
    )
    conn.execute(
        insert(effects).values(
            session_id=sid,
            operation_id=operation,
            effect_key=key,
            input_sha256=prepared.request_sha256,
            owner="provider",
            disposition="prepared",
            creator_sequence=running.claim.segment_sequence,
            creator_generation=running.claim.ownership_generation,
        )
    )
    if old is None:
        conn.execute(insert(ordinals).values(session_id=sid, operation_id=operation, family=1, next_ordinal=1))
    else:
        changed_ordinal = conn.execute(
            update(ordinals)
            .where(
                ordinals.c.session_id == sid,
                ordinals.c.operation_id == operation,
                ordinals.c.family == 1,
                ordinals.c.next_ordinal == ordinal,
            )
            .values(next_ordinal=ordinal + 1)
        )
        if changed_ordinal.rowcount != 1:
            raise AuditIntegrityError("checkpoint admission lost its original family-1 physical ordinal")
    dispatched_json = _effect_snapshot(
        source.prepared_selected.payload_json,
        effect_key=key,
        disposition="dispatched",
        receipt_id=None,
        add_attempt_id=attempt.attempt_id,
    )
    transition = ContinuationEvent(
        "checkpoint",
        canonical_json(
            {
                "owner": "composer_checkpoint_paid",
                "version": 1,
                "stage": "dispatched",
                "phase": source.phase,
                "kind": source.kind,
                "pass_index": source.pass_index,
                "retry_ordinal": prepared.retry_ordinal,
                "effect_key": key,
                "outer_effect_key": source.outer_effect_key,
                "attempt_id": attempt.attempt_id,
                "request_sha256": prepared.request_sha256,
                "requested_model": requested.value,
                "source_checkpoint_id": source.prepared_selected.checkpoint_id,
                "source_cursor": source.prepared_selected.cursor,
                "end_input_chunk_key": f"value:{source.end_input_ref.codec}:{source.end_input_ref.digest}"
                if source.end_input_ref is not None
                else None,
                "end_input_sha256": source.end_input_ref.digest if source.end_input_ref is not None else None,
                "end_env_ref_names": source.end_env_ref_names,
            }
        ),
    )
    published_at = database_now(conn)
    append = prepare_snapshot_append(
        source.prepared_selected, snapshot_json=dispatched_json, events=(transition,), published_at=published_at
    )
    admitted = StoredContinuationSnapshot(append.snapshot_json, source.prepared_selected.cursor + 1, append.checkpoint_id)
    fence = running.session_operation_context.fence
    identity = PaidOverflowIdentity(
        session_id=sid,
        operation_id=operation,
        effect_key=key,
        attempt_id=attempt.attempt_id,
        session_operation_id=fence.operation_id,
        session_operation_epoch=fence.operation_epoch,
        ownership_generation=running.claim.ownership_generation,
        lease_sha256=sha256(fence.lease_token.encode("utf-8")).hexdigest(),
        policy_sha256=policy.policy_hash,
        source_checkpoint_id=source.prepared_selected.checkpoint_id,
        source_cursor=source.prepared_selected.cursor,
        source_snapshot_sha256=sha256(source.prepared_selected.payload_json.encode("utf-8")).hexdigest(),
    )
    terminal_hold = prepare_checkpoint_terminal_hold_plan(
        identity=identity,
        admitted=admitted,
        phase=source.phase,
        creator_sequence=running.claim.segment_sequence,
        requested_model=requested.value,
    )
    tail = terminal_hold.paid_overflow
    hold = ProviderTerminalHoldSource(
        session_id=sid,
        operation_id=operation,
        effect_key=key,
        policy_sha256=policy.policy_hash,
        allowance_bytes=policy.max_retained_payload_bytes,
        source_checkpoint_id=source.prepared_selected.checkpoint_id,
        source_cursor=source.prepared_selected.cursor,
        source_snapshot_sha256=identity.source_snapshot_sha256,
        source_input_sha256=prepared.request_sha256,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        claim_attempt=running.claim.attempt,
        session_operation_id=fence.operation_id,
        session_operation_epoch=fence.operation_epoch,
        lease_sha256=identity.lease_sha256,
        reserved_bytes=terminal_hold.reserved_bytes,
        fallback_contract_sha256=terminal_hold.contract_sha256,
    )
    reserve_provider_terminal_hold(
        conn,
        source=hold,
        prospective=(
            ProspectiveContinuationChunk(f"event:{admitted.cursor}", transition.payload_json.encode("utf-8"), "event.v2"),
            ProspectiveContinuationChunk(f"snapshot:{admitted.checkpoint_id}", append.snapshot_json.encode("utf-8"), "snapshot.v2"),
        ),
        created_at=published_at,
    )
    changed = conn.execute(
        update(effects)
        .where(
            effects.c.session_id == sid,
            effects.c.operation_id == operation,
            effects.c.effect_key == key,
            effects.c.disposition == "prepared",
            effects.c.provider_attempt_id.is_(None),
        )
        .values(
            disposition="dispatched",
            provider_attempt_id=attempt.attempt_id,
            physical_sequence=running.claim.segment_sequence,
            physical_generation=running.claim.ownership_generation,
            requested_model=requested.value,
        )
    )
    if changed.rowcount != 1:
        raise AuditIntegrityError("checkpoint admission lost its original prepared child effect")
    bind_provider_terminal_hold_attempt(conn, source=hold, provider_attempt_id=attempt.attempt_id)
    stored = append_complete_snapshot(
        conn,
        session_id=sid,
        operation_id=operation,
        expected_checkpoint_id=source.prepared_selected.checkpoint_id,
        expected_cursor=source.prepared_selected.cursor,
        snapshot_json=append.snapshot_json,
        event_batch=append.events,
        handoff=False,
        prepared_append=append,
    )
    if stored != admitted:
        raise AuditIntegrityError("checkpoint admission changed its reserved dispatched root")
    return V4CheckpointProviderAdmission(attempt, source.prepared_selected, stored, hold, tail, terminal_hold, key, source)


def require_current_checkpoint_dispatched_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    admission: V4CheckpointProviderAdmission,
    *,
    allow_cancel: bool = False,
    allow_expired_segment: bool = False,
) -> StoredContinuationSnapshot:
    """Read pending DATA only for the original current physical writer/fence.

    This is intentionally separate from cold phase traversal. It cannot mint
    another attempt or turn an unknown child into a retry capability.
    """
    if type(running) is not ComposerOperationRunning or type(admission) is not V4CheckpointProviderAdmission:
        raise AuditIntegrityError("checkpoint pending read requires nominal original running and paid admission")
    _require_current_continuation_running(conn, running, allow_cancel=allow_cancel, allow_expired_segment=allow_expired_segment)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    source = admission.original_source
    selected = admission.selected
    if (
        type(source) is not CheckpointPreparedSource
        or source.prepared_selected != admission.prepared_selected
        or source.phase not in ("early", "end")
        or admission.effect_key != admission.hold.effect_key
        or admission.paid_tail.identity.effect_key != admission.effect_key
        or admission.paid_tail.admitted != selected
        or admission.paid_tail.phase != source.phase
        or admission.paid_tail.identity.attempt_id != admission.attempt.attempt_id
        or admission.terminal_hold.paid_overflow != admission.paid_tail
        or admission.terminal_hold.no_sdk.identity != admission.paid_tail.identity
        or admission.hold.reserved_bytes != admission.terminal_hold.reserved_bytes
        or admission.hold.fallback_contract_sha256 != admission.terminal_hold.contract_sha256
        or load_snapshot(conn, session_id=sid, operation_id=operation) != selected
    ):
        raise AuditIntegrityError("checkpoint pending read lost its original admitted source")
    root = decode_continuation_snapshot(selected.payload_json)
    before = decode_continuation_snapshot(admission.prepared_selected.payload_json)
    expected = decode_continuation_snapshot(
        _effect_snapshot(
            admission.prepared_selected.payload_json,
            effect_key=admission.effect_key,
            disposition="dispatched",
            receipt_id=None,
            add_attempt_id=admission.attempt.attempt_id,
        )
    ).model_copy(update={"cursor": selected.cursor})
    if (
        selected.cursor != admission.prepared_selected.cursor + 1
        or root != expected
        or before.pending_settlement_ids
        or root.pending_settlement_ids != (admission.attempt.attempt_id,)
    ):
        raise AuditIntegrityError("checkpoint pending read changed its complete original dispatched root")
    tag, text = _event_text(conn, sid, operation, selected.cursor)
    payload, error = parse_json_strict(text)
    if (
        tag != "checkpoint"
        or error is not None
        or type(payload) is not dict
        or set(payload)
        != {
            "owner",
            "version",
            "stage",
            "phase",
            "kind",
            "pass_index",
            "retry_ordinal",
            "effect_key",
            "outer_effect_key",
            "attempt_id",
            "request_sha256",
            "requested_model",
            "source_checkpoint_id",
            "source_cursor",
            "end_input_chunk_key",
            "end_input_sha256",
            "end_env_ref_names",
        }
        or payload["owner"] != "composer_checkpoint_paid"
        or payload["version"] != 1
        or payload["stage"] != "dispatched"
        or payload["phase"] != source.phase
        or payload["kind"] != source.kind
        or payload["pass_index"] != source.pass_index
        or type(payload["retry_ordinal"]) is not int
        or payload["retry_ordinal"] < 0
        or payload["effect_key"] != admission.effect_key
        or payload["outer_effect_key"] != source.outer_effect_key
        or payload["attempt_id"] != admission.attempt.attempt_id
        or payload["request_sha256"] != admission.hold.source_input_sha256
        or payload["source_checkpoint_id"] != admission.prepared_selected.checkpoint_id
        or payload["source_cursor"] != admission.prepared_selected.cursor
        or payload["end_input_chunk_key"]
        != (f"value:{source.end_input_ref.codec}:{source.end_input_ref.digest}" if source.end_input_ref is not None else None)
        or payload["end_input_sha256"] != (source.end_input_ref.digest if source.end_input_ref is not None else None)
        or payload["end_env_ref_names"] != list(source.end_env_ref_names)
        or text != canonical_json(payload)
    ):
        raise AuditIntegrityError("checkpoint pending read changed its original dispatch event")
    effect = conn.execute(
        select(effects).where(
            effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == admission.effect_key
        )
    ).one_or_none()
    attempt = conn.execute(select(physical).where(physical.c.attempt_id == admission.attempt.attempt_id)).one_or_none()
    fence = running.session_operation_context.fence
    if (
        effect is None
        or effect.owner != "provider"
        or effect.disposition != "dispatched"
        or effect.input_sha256 != admission.hold.source_input_sha256
        or effect.provider_attempt_id != admission.attempt.attempt_id
        or effect.requested_model != payload["requested_model"]
        or effect.receipt_key is not None
        or effect.result_chunk_key is not None
        or (effect.creator_sequence, effect.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        or (effect.physical_sequence, effect.physical_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        or attempt is None
        or attempt.session_id != sid
        or attempt.source != "composer"
        or attempt.operation_id != fence.operation_id
        or attempt.operation_epoch != fence.operation_epoch
        or attempt.lease_token != fence.lease_token
        or attempt.settled_at is not None
        or attempt.ledger_entry_id is not None
    ):
        raise AuditIntegrityError("checkpoint pending read lost its original unsettled physical attempt")
    _effect_inventory(conn, sid, operation, selected)
    contract = load_continuation_read_contract(conn, session_id=sid, operation_id=operation)
    if _require_active_provider_hold(conn, source=admission.hold, contract=contract) != admission.hold.reserved_bytes:
        raise AuditIntegrityError("checkpoint pending read lost its original active hold")
    return selected
