"""Original family-0 provider custody for the post-P4 classifier.

The classifier keeps its actual Compose/ToolBatch source. These SQL owners
never turn it into an ordinary Compose input or an invented END candidate.
Cold results are immutable DATA and cannot issue another provider attempt.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, cast
from uuid import UUID
from weakref import WeakKeyDictionary

from pydantic import Field, ValidationError
from sqlalchemy import Connection, select, update

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _CallModelOutcome
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, ContinuationValue, ToolBatchFrame
from elspeth.web.composer.continuation_audit_buffer import RecorderSnapshot, decode_recorder_snapshot
from elspeth.web.composer.continuation_classifier_handoff import decode_classifier_runtime_preflight
from elspeth.web.composer.continuation_classifier_no_sdk import ClassifierNoSdkIssuer, ClassifierNoSdkRefusalReason
from elspeth.web.composer.continuation_classifier_owned_source import ClassifierOwnedSourceObservation
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_response,
    decode_continuation_snapshot,
    decode_retained_physical_audit_call,
    encode_continuation_response,
    encode_continuation_snapshot,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_env_inventory import (
    CurrentContinuationEnvInventory,
    require_current_env_inventory_on_connection,
)
from elspeth.web.composer.continuation_evidence import encode_turn_evidence
from elspeth.web.composer.continuation_evidence_application_data import append_primary_call_to_evidence
from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_data_on_connection
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_policy import (
    ContinuationAdmissionPolicyV2,
    ContinuationAdmissionPolicyV3,
    ContinuationAdmissionPolicyV4,
    decode_continuation_policy,
)
from elspeth.web.composer.continuation_primary_no_sdk import ContinuationRetainedTailUnbounded
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_json
from elspeth.web.composer.continuation_provider_outcomes import (
    require_successful_response_audit,
    require_v4_successful_response_audit,
)
from elspeth.web.composer.continuation_runtime import (
    CapturedContinuationRuntime,
    RestoredContinuationRuntime,
    capture_continuation_runtime_owners,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.prepared_provider_input import PreparedProviderInput
from elspeth.web.composer.provider_cost import (
    CommonProviderCostBudget,
    CommonProviderCostRequest,
    CommonProviderCostSnapshot,
    continuation_common_cost_policy,
)
from elspeth.web.composer.provider_gateway import PreparedContinuationPrimaryRequest
from elspeth.web.composer.provider_quota import ProviderInvocationFamily, ProviderInvocationOwner
from elspeth.web.composer.state import CompositionState
from elspeth.web.continuation_retention_memo import retention_stage_memo_scope
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    ContinuationProviderBinding,
    PreparedContinuationAppend,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    private_chunk_key,
    put_chunk,
    read_chunk,
    require_snapshot_cas,
)
from elspeth.web.sessions.continuation_classifier_primary_incomplete_tail import ClassifierPaidIncompleteTailPlan
from elspeth.web.sessions.continuation_classifier_primary_paid_tail import ClassifierPaidOverflowTailPlan
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowReceipt
from elspeth.web.sessions.continuation_primary import (
    _chunk_data,
    _input_value,
    _physical_source_data,
    allocate_primary_effect_on_connection,
)
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult, VerifiedSdkTerminal
from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.provider_quota import ProviderCallCustody
    from elspeth.web.composer.service import ComposerServiceImpl
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt, ClassifierNoSdkTailPlan
    from elspeth.web.sessions.service import SessionServiceImpl

type ClassifierPrimaryBranch = Literal["staged_review", "composition_last_chance"]


@dataclass(frozen=True, slots=True)
class ClassifierPrephysicalSource:
    """Reproved historical DATA; no provider or terminal permission."""

    selected: SelectedContinuationState
    effect_key: str
    creator_sequence: int
    creator_generation: int
    claim_attempt: int
    physical_operation_id: str
    physical_operation_epoch: int
    env_ref_names: frozenset[str]


def read_classifier_prephysical_source_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str, checkpoint_id: str
) -> ClassifierPrephysicalSource:
    """Prove a logical classifier source with no physical or orphan owner.

    A failed parent may have changed exactly its logical advance to unknown.
    This is a terminal DATA facet; the ordinary business-stage reader stays
    strict and cannot use this observation to dispatch.
    """
    from elspeth.contracts.json_parser import parse_json_strict
    from elspeth.web.sessions.continuation_iteration_return import _stage_source_record, read_original_classifier_stage_on_connection
    from elspeth.web.sessions.continuation_selection import load_snapshot_private_data_on_connection
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import composer_provider_terminal_holds_table as holds
    from elspeth.web.sessions.models import composer_segment_attempts_table as history
    from elspeth.web.sessions.models import quota_provider_attempts_table as physical

    sid = str(session_id)
    row = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation_id, checkpoints.c.checkpoint_id == checkpoint_id
        )
    ).one()
    stored = _historical(conn, sid, operation_id, row.cursor)
    stage = read_original_classifier_stage_on_connection(conn, session_id=session_id, operation_id=operation_id, selected=stored)
    source_record = _stage_source_record(conn, stored, sid, operation_id)
    if source_record is None:
        raise AuditIntegrityError("prephysical classifier lost its complete original stage record")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    root = decode_continuation_snapshot(stored.payload_json)
    unresolved = tuple(item.effect_id for item in root.effect_frontier if item.disposition in ("prepared", "dispatched", "unknown"))
    if unresolved != (stage.owner_effect_key,):
        raise AuditIntegrityError("prephysical classifier cannot conceal another unresolved effect")
    if parent.status not in ("running", "failed"):
        raise AuditIntegrityError("prephysical classifier has no live or failed parent")
    final = parent.status == "failed"
    actual = tuple(conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id)))
    expected = {
        (item.effect_id, "unknown" if final and item.effect_id == stage.owner_effect_key else item.disposition, item.receipt_id)
        for item in root.effect_frontier
    }
    if {(item.effect_key, item.disposition, item.receipt_key) for item in actual} != expected:
        raise AuditIntegrityError("prephysical classifier changed its complete actual effects inventory")
    creator = conn.execute(
        select(history).where(
            history.c.session_id == sid,
            history.c.operation_id == operation_id,
            history.c.sequence == stage.creator_sequence,
            history.c.generation == stage.creator_generation,
        )
    ).one()
    if (
        conn.execute(
            select(chunks.c.chunk_key).where(
                chunks.c.session_id == sid, chunks.c.operation_id == operation_id, chunks.c.chunk_key == _stage_key(stage.owner_effect_key)
            )
        ).first()
        is not None
    ):
        raise AuditIntegrityError("prephysical classifier already has its physical stage anchor")
    providers = {item.effect_key: item for item in actual if item.owner == "provider"}
    for item in conn.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation_id)):
        if item.chunk_key.startswith("classifier-primary-source:"):
            source = _read_source(conn, session_id, operation_id, item.chunk_key.removeprefix("classifier-primary-source:"))
            if source.stage_effect_key == stage.owner_effect_key or source.source_checkpoint_id == checkpoint_id:
                raise AuditIntegrityError("prephysical classifier already has an original physical source")
            if source.effect_key not in providers or providers[source.effect_key].disposition != "committed":
                raise AuditIntegrityError("prephysical classifier contains an orphan physical source")
        elif item.chunk_key.startswith("classifier-primary-stage:"):
            encoded = item.payload.decode("utf-8", errors="strict")
            value, error = parse_json_strict(encoded)
            if error is not None or type(value) is not dict or type(value.get("effect_key")) is not str:
                raise AuditIntegrityError("prephysical classifier contains a malformed stage anchor")
            linked = value["effect_key"]
            prior = _read_source(conn, session_id, operation_id, linked)
            if encoded != _event(prior) or linked not in providers or providers[linked].disposition != "committed":
                raise AuditIntegrityError("prephysical classifier contains an orphan stage anchor")
    known = {item.provider_attempt_id for item in providers.values()}
    if None in known or any(item.disposition != "committed" for item in providers.values()):
        raise AuditIntegrityError("prephysical classifier contains an unclosed original provider")
    for attempt in conn.execute(
        select(physical).where(physical.c.session_id == sid, physical.c.operation_id == creator.session_operation_id)
    ):
        if attempt.attempt_id not in known or attempt.settled_at is None or attempt.ledger_entry_id is None:
            raise AuditIntegrityError("prephysical classifier contains an orphan or unclosed physical attempt")
    for hold in conn.execute(select(holds).where(holds.c.session_id == sid, holds.c.operation_id == operation_id)):
        if (
            hold.effect_key not in providers
            or hold.state != "consumed"
            or hold.provider_attempt_id != providers[hold.effect_key].provider_attempt_id
        ):
            raise AuditIntegrityError("prephysical classifier contains an orphan or active terminal hold")
    # Reprove all original paid ledger/source links, not merely membership.
    read_continuation_cost_snapshot_on_connection(conn, root)
    selected = SelectedContinuationState(
        stored.checkpoint_id,
        root,
        load_snapshot_private_data_on_connection(conn, session_id=sid, operation_id=operation_id, selected=stored),
    )
    return ClassifierPrephysicalSource(
        selected,
        stage.owner_effect_key,
        stage.creator_sequence,
        stage.creator_generation,
        creator.claim_attempt,
        creator.session_operation_id,
        creator.session_operation_epoch,
        frozenset(source_record[0].env_ref_names),
    )


class _Source(ContinuationValue):
    schema_version: Literal["composer.classifier-primary-source.v1"] = "composer.classifier-primary-source.v1"
    session_id: UUID
    operation_id: str
    branch: ClassifierPrimaryBranch
    source_checkpoint_id: str
    source_cursor: int = Field(ge=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effect_key: str = Field(pattern=r"^provider:0:(0|[1-9][0-9]*)$")
    stage_effect_key: str = Field(min_length=1)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    creator_sequence: int = Field(gt=0)
    creator_generation: int = Field(gt=0)
    compose: ComposeFrame
    batch: ToolBatchFrame
    env_ref_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedClassifierPrimary:
    binding: ContinuationProviderBinding
    snapshot: StoredContinuationSnapshot


@dataclass(frozen=True, slots=True)
class PreparedClassifierPrimaryProvider:
    prepared: PreparedClassifierPrimary
    request: PreparedContinuationPrimaryRequest


@dataclass(frozen=True, slots=True)
class V4ClassifierProviderAdmission:
    attempt: ProviderAttempt
    dispatched: StoredContinuationSnapshot
    hold: ProviderTerminalHoldSource
    paid_tail: ClassifierPaidOverflowTailPlan
    incomplete_tail: ClassifierPaidIncompleteTailPlan
    no_sdk_tail: ClassifierNoSdkTailPlan
    effect_key: str


@dataclass(frozen=True, slots=True)
class VerifiedClassifierPaidOverflowTerminal:
    receipt: PaidOverflowReceipt
    snapshot: StoredContinuationSnapshot
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class VerifiedClassifierPaidIncompleteTerminal:
    verified: VerifiedPaidIncompleteTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = field(default=(), compare=False)


@dataclass(frozen=True, slots=True)
class DispatchedClassifierPrimary:
    """Two original checkpoints committed together by one current actor."""

    prepared: PreparedClassifierPrimary
    snapshot: StoredContinuationSnapshot


@dataclass(frozen=True, slots=True)
class ClassifierPrimaryResult:
    """Actual returned source, or a cold immutable replay without SDK authority."""

    snapshot: StoredContinuationSnapshot
    effect_key: str
    input_sha256: str
    response: PrivateChunk
    recorder: PrivateChunk
    buffer: RecorderSnapshot
    outcome: _CallModelOutcome
    source_checkpoint_id: str
    source_cursor: int
    branch: ClassifierPrimaryBranch
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = field(default=(), compare=False)

    @property
    def completion(self) -> _AdmittedLLMCompletion:
        return self.outcome.completion


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class _PublishPermit:
    """Opaque original live return; constructing a value issues no permission."""


_returned: WeakKeyDictionary[
    _PublishPermit, tuple[PreparedClassifierPrimary, StoredContinuationSnapshot, _CallModelOutcome, RecorderSnapshot]
] = WeakKeyDictionary()


def _source_key(effect_key: str) -> str:
    return f"classifier-primary-source:{effect_key}"


def _stage_key(effect_key: str) -> str:
    return f"classifier-primary-stage:{effect_key}"


def _event(source: _Source) -> str:
    return canonical_json(
        {
            "owner": "classifier_primary",
            "effect_key": source.effect_key,
            "input_sha256": source.input_sha256,
            "source_chunk_key": _source_key(source.effect_key),
            "source_sha256": stable_hash(source.model_dump(mode="json")),
        }
    )


def _historical(conn: Connection, sid: str, operation: str, cursor: int) -> StoredContinuationSnapshot:
    row = conn.execute(
        select(checkpoints).where(checkpoints.c.session_id == sid, checkpoints.c.operation_id == operation, checkpoints.c.cursor == cursor)
    ).one_or_none()
    if row is None or row.snapshot_chunk_key != f"snapshot:{row.checkpoint_id}":
        raise AuditIntegrityError("classifier PRIMARY lost its exact original checkpoint")
    raw = read_chunk(conn, session_id=sid, operation_id=operation, key=row.snapshot_chunk_key)
    root = decode_continuation_snapshot(raw.decode("utf-8", errors="strict"))
    if (root.logical.session_id, root.logical.operation_id, root.cursor) != (sid, operation, cursor):
        raise AuditIntegrityError("classifier PRIMARY changed its original logical checkpoint")
    return StoredContinuationSnapshot(encode_continuation_snapshot(root), cursor, row.checkpoint_id)


def _journal(conn: Connection, source: _Source, cursor: int, tag: str) -> None:
    rows = (
        conn.execute(
            select(events.c.payload_chunk_key).where(
                events.c.session_id == str(source.session_id),
                events.c.operation_id == source.operation_id,
                events.c.sequence == cursor,
                events.c.tag == tag,
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != 1 or read_chunk(conn, session_id=str(source.session_id), operation_id=source.operation_id, key=rows[0]).decode(
        "utf-8", errors="strict"
    ) != _event(source):
        raise AuditIntegrityError("classifier PRIMARY changed its exact original lifecycle journal")


def _read_source(conn: Connection, sid: UUID, operation: str, effect_key: str) -> _Source:
    key = _source_key(effect_key)
    row = conn.execute(
        select(chunks.c.payload, chunks.c.codec).where(
            chunks.c.session_id == str(sid), chunks.c.operation_id == operation, chunks.c.chunk_key == key
        )
    ).one_or_none()
    if row is None or row.codec != "composer.classifier-primary-source.v1":
        raise AuditIntegrityError("classifier PRIMARY lost its original prepared source")
    text = row.payload.decode("utf-8", errors="strict")
    try:
        source = _Source.model_validate_json(text, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("classifier PRIMARY source changed its complete owned shape") from error
    if canonical_json(source.model_dump(mode="json")) != text or (source.session_id, source.operation_id, source.effect_key) != (
        sid,
        operation,
        effect_key,
    ):
        raise AuditIntegrityError("classifier PRIMARY source changed its complete original identity")
    return source


def _source_selection(conn: Connection, source: _Source) -> SelectedContinuationState:
    from elspeth.web.sessions.continuation_iteration_return import (
        _require_compose_capture,
        read_original_classifier_stage_on_connection,
    )

    original = _historical(conn, str(source.session_id), source.operation_id, source.source_cursor)
    if (
        original.checkpoint_id != source.source_checkpoint_id
        or sha256(original.payload_json.encode("utf-8")).hexdigest() != source.source_sha256
    ):
        raise AuditIntegrityError("classifier PRIMARY substituted its original applied P4 source")
    stage = read_original_classifier_stage_on_connection(
        conn, session_id=source.session_id, operation_id=source.operation_id, selected=original
    )
    if stage.stage != "classify_dispatched" or (stage.creator_sequence, stage.creator_generation) != (
        source.creator_sequence,
        source.creator_generation,
    ):
        raise AuditIntegrityError("classifier PRIMARY changed its original classifier stage/creator")
    if stage.owner_effect_key != source.stage_effect_key:
        raise AuditIntegrityError("classifier PRIMARY changed its original classifier stage identity")
    anchor = read_chunk(conn, session_id=str(source.session_id), operation_id=source.operation_id, key=_stage_key(source.stage_effect_key))
    if anchor.decode("utf-8", errors="strict") != _event(source):
        raise AuditIntegrityError("classifier PRIMARY lost its one original attempt for this classifier stage")
    root = decode_continuation_snapshot(original.payload_json)
    before, batch = applied_batch_cut(root.frames)
    _require_compose_capture(before, source.compose)
    if source.batch.model_copy(update={"accumulator": batch.accumulator}) != batch:
        raise AuditIntegrityError("classifier PRIMARY changed its original applied batch fields")
    return SelectedContinuationState(
        original.checkpoint_id,
        root,
        load_snapshot_private_data_on_connection(
            conn, session_id=str(source.session_id), operation_id=source.operation_id, selected=original
        ),
    )


def _prepared_root(conn: Connection, source: _Source) -> ContinuationFrameStack:
    original = _source_selection(conn, source)
    captured = original.stack.model_copy(update={"frames": (source.compose, source.batch)})
    return decode_continuation_snapshot(
        _effect_snapshot(encode_continuation_snapshot(captured), effect_key=source.effect_key, disposition="prepared", receipt_id=None)
    ).model_copy(update={"cursor": source.source_cursor + 1})


def _chain(conn: Connection, source: _Source, *, dispatched: bool) -> StoredContinuationSnapshot:
    root = _prepared_root(conn, source)
    prepared = _historical(conn, str(source.session_id), source.operation_id, source.source_cursor + 1)
    if prepared.payload_json != encode_continuation_snapshot(root):
        raise AuditIntegrityError("classifier PRIMARY changed its original complete preparation")
    _journal(conn, source, prepared.cursor, "input_prepared")
    if not dispatched:
        return prepared
    root = decode_continuation_snapshot(
        _effect_snapshot(encode_continuation_snapshot(root), effect_key=source.effect_key, disposition="dispatched", receipt_id=None)
    ).model_copy(update={"cursor": source.source_cursor + 2})
    begun = _historical(conn, str(source.session_id), source.operation_id, source.source_cursor + 2)
    if begun.payload_json != encode_continuation_snapshot(root):
        raise AuditIntegrityError("classifier PRIMARY changed its original complete dispatch")
    _journal(conn, source, begun.cursor, "may_dispatch")
    return begun


def _prepare_classifier_primary_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    captured: CapturedContinuationRuntime,
    request: PreparedContinuationPrimaryRequest,
    branch: ClassifierPrimaryBranch,
) -> PreparedClassifierPrimary:
    from elspeth.web.sessions.continuation_iteration_return import _require_compose_capture, read_iteration_stage_on_connection

    _require_current_continuation_running(conn, running)
    current = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.stack.cursor,
    )
    if current.payload_json != encode_continuation_snapshot(selected.stack):
        raise ContinuationCASConflict("classifier PRIMARY substituted its actual selected root")
    require_current_env_inventory_on_connection(
        conn, running, inventory=inventory, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.stack.cursor
    )
    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    if type(policy) is ContinuationAdmissionPolicyV3:
        raise ContinuationRetainedTailUnbounded("classifier PRIMARY has no proven complete retained result bound")
    if type(policy) not in (ContinuationAdmissionPolicyV2, ContinuationAdmissionPolicyV4):
        raise AuditIntegrityError("classifier PRIMARY requires its admitted current cost policy")
    stage = read_iteration_stage_on_connection(conn, running, selected=current)
    if (
        stage is None
        or stage.stage != "classify_dispatched"
        or (stage.creator_sequence, stage.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
    ):
        raise AuditIntegrityError("classifier PRIMARY requires its actual dispatched classifier source")
    if (
        conn.execute(
            select(chunks.c.chunk_key).where(
                chunks.c.session_id == str(running.claim.session_id),
                chunks.c.operation_id == running.claim.operation_id,
                chunks.c.chunk_key == _stage_key(stage.owner_effect_key),
            )
        ).one_or_none()
        is not None
    ):
        raise AuditIntegrityError("classifier PRIMARY stage already has its original physical attempt; replay is forbidden")
    before, batch = applied_batch_cut(selected.stack.frames)
    if (
        type(captured) is not CapturedContinuationRuntime
        or captured.selected is not selected
        or captured.stack.model_dump(exclude={"frames"}) != selected.stack.model_dump(exclude={"frames"})
        or selected.stack.active_action_json is not None
        or selected.stack.active_action_source is not None
        or selected.stack.pending_owner_results
        or selected.stack.pending_settlement_ids
        or branch not in ("staged_review", "composition_last_chance")
        or type(request) is not PreparedContinuationPrimaryRequest
    ):
        raise AuditIntegrityError("classifier PRIMARY lost its actual pre-call owners")
    compose, captured_batch = applied_batch_cut(captured.stack.frames)
    _require_compose_capture(before, compose)
    if batch.model_dump(exclude={"accumulator"}) != captured_batch.model_dump(exclude={"accumulator"}) or (
        before.provider_effect_key,
        before.provider_input_sha256,
        before.pending_response,
        before.phase,
    ) != (compose.provider_effect_key, compose.provider_input_sha256, compose.pending_response, compose.phase):
        raise AuditIntegrityError("classifier PRIMARY replaced its actual paid P4/PRIMARY source")
    descriptor = continuation_provider_input_json(request.kwargs)
    _input_value(descriptor, request.input_sha256)
    cost_source = read_continuation_cost_snapshot_on_connection(conn, selected.stack)
    if type(cost_source) is not CommonProviderCostSnapshot or cost_source.policy != continuation_common_cost_policy(
        cast(ContinuationAdmissionPolicyV2 | ContinuationAdmissionPolicyV4, policy)
    ):
        raise AuditIntegrityError("classifier PRIMARY SQL input changed its immutable admitted cost policy")
    cost = CommonProviderCostBudget(cost_source)
    cost.validate_binding(
        session_id=selected.stack.logical.session_id, request_id=selected.stack.logical.original_user_message_id, lifecycle_version=2
    )
    admitted: CommonProviderCostRequest = {"messages": request.kwargs["messages"]}
    if "tools" in request.kwargs:
        admitted["tools"] = request.kwargs["tools"]
    if "max_tokens" in request.kwargs:
        admitted["max_tokens"] = request.kwargs["max_tokens"]
    cost.prepare_request(admitted)
    if request.kwargs.get("max_tokens") != admitted["max_tokens"]:
        raise AuditIntegrityError("classifier PRIMARY SQL input exceeds its exact admitted completion allowance")
    for chunk in captured.chunks:
        put_chunk(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
    captured_selected = SelectedContinuationState(
        selected.checkpoint_id,
        captured.stack,
        load_snapshot_private_data_on_connection(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            selected=StoredContinuationSnapshot(
                encode_continuation_snapshot(captured.stack), captured.stack.cursor, selected.checkpoint_id
            ),
        ),
    )
    evidence = read_selected_turn_evidence_graph_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        selected=captured_selected,
        names=inventory.names,
    )
    if evidence.snapshot.recorder != compose.audit_buffer:
        raise AuditIntegrityError("classifier PRIMARY changed its actual pre-call recorder/evidence")
    binding = allocate_primary_effect_on_connection(conn, running, input_sha256=request.input_sha256, action_json=None)
    source = _Source(
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        branch=branch,
        source_checkpoint_id=current.checkpoint_id,
        source_cursor=current.cursor,
        source_sha256=sha256(current.payload_json.encode("utf-8")).hexdigest(),
        effect_key=binding.effect_key,
        stage_effect_key=stage.owner_effect_key,
        input_sha256=request.input_sha256,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        compose=compose,
        batch=captured_batch,
        env_ref_names=tuple(sorted(inventory.names)),
    )
    source_chunk = encode_private_chunk(
        codec=source.schema_version, canonical=canonical_json(source.model_dump(mode="json")), env_ref_names=inventory.names
    )
    put_chunk(
        conn,
        session_id=str(source.session_id),
        operation_id=source.operation_id,
        key=_source_key(binding.effect_key),
        payload=source_chunk.canonical.encode("utf-8"),
        codec=source.schema_version,
    )
    put_chunk(
        conn,
        session_id=str(source.session_id),
        operation_id=source.operation_id,
        key=_stage_key(source.stage_effect_key),
        payload=_event(source).encode("utf-8"),
        codec="composer.classifier-primary-stage.v1",
    )
    put_chunk(
        conn,
        session_id=str(source.session_id),
        operation_id=source.operation_id,
        key=f"provider-input:{request.input_sha256}",
        payload=descriptor.encode("utf-8"),
        codec="composer.provider-input.v1",
    )
    encoded = _effect_snapshot(
        encode_continuation_snapshot(captured.stack), effect_key=binding.effect_key, disposition="prepared", receipt_id=None
    )
    stored = append_complete_snapshot(
        conn,
        session_id=str(source.session_id),
        operation_id=source.operation_id,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("input_prepared", _event(source)),),
        handoff=False,
    )
    return PreparedClassifierPrimary(binding, stored)


def _begin_classifier_primary_on_connection(
    conn: Connection,
    prepared: PreparedClassifierPrimary,
    *,
    attempt: ProviderAttempt | None = None,
    requested_model: str | None = None,
    prepared_append: PreparedContinuationAppend | None = None,
) -> StoredContinuationSnapshot:
    if type(prepared) is not PreparedClassifierPrimary:
        raise AuditIntegrityError("classifier PRIMARY dispatch requires its actual prepared owner")
    binding = prepared.binding
    _require_current_continuation_running(conn, binding.running)
    source = _read_source(conn, binding.running.claim.session_id, binding.running.claim.operation_id, binding.effect_key)
    current = require_snapshot_cas(
        conn,
        session_id=str(source.session_id),
        operation_id=source.operation_id,
        expected_checkpoint_id=prepared.snapshot.checkpoint_id,
        expected_cursor=prepared.snapshot.cursor,
    )
    if current != prepared.snapshot or _chain(conn, source, dispatched=False) != current:
        raise AuditIntegrityError("classifier PRIMARY dispatch changed its original prepared source")
    row = conn.execute(
        select(effects).where(
            effects.c.session_id == str(source.session_id),
            effects.c.operation_id == source.operation_id,
            effects.c.effect_key == source.effect_key,
        )
    ).one()
    if (
        row.disposition != "prepared"
        or row.input_sha256 != binding.input_sha256
        or row.input_sha256 != source.input_sha256
        or row.owner != "provider"
        or row.action_json is not None
        or binding.family != 0
        or binding.effect_key != f"provider:0:{binding.ordinal}"
        or (row.creator_sequence, row.creator_generation) != (source.creator_sequence, source.creator_generation)
        or (source.creator_sequence, source.creator_generation)
        != (binding.running.claim.segment_sequence, binding.running.claim.ownership_generation)
        or any(
            value is not None
            for value in (
                row.receipt_key,
                row.result_chunk_key,
                row.provider_attempt_id,
                row.physical_sequence,
                row.physical_generation,
                row.requested_model,
                row.response_finished_at,
                row.committed_state_ids_json,
                row.graph_sha256,
            )
        )
    ):
        raise AuditIntegrityError("classifier PRIMARY cannot dispatch an advanced or substituted effect")
    if (attempt is None) != (requested_model is None) or (attempt is None) != (prepared_append is None):
        raise AuditIntegrityError("classifier PRIMARY dispatch changed its physical admission shape")
    if attempt is not None and (
        type(attempt) is not ProviderAttempt
        or type(requested_model) is not str
        or not requested_model
        or type(prepared_append) is not PreparedContinuationAppend
    ):
        raise AuditIntegrityError("classifier PRIMARY dispatch requires its original physical admission")
    statement = update(effects).where(
        effects.c.session_id == str(source.session_id),
        effects.c.operation_id == source.operation_id,
        effects.c.effect_key == source.effect_key,
    )
    if attempt is None:
        statement = statement.values(disposition="dispatched")
    else:
        statement = statement.values(
            disposition="dispatched",
            provider_attempt_id=attempt.attempt_id,
            physical_sequence=binding.running.claim.segment_sequence,
            physical_generation=binding.running.claim.ownership_generation,
            requested_model=requested_model,
        )
    changed = conn.execute(statement)
    if changed.rowcount != 1:
        raise AuditIntegrityError("classifier PRIMARY dispatch lost its original prepared effect CAS")
    encoded = _effect_snapshot(current.payload_json, effect_key=source.effect_key, disposition="dispatched", receipt_id=None)
    return append_complete_snapshot(
        conn,
        session_id=str(source.session_id),
        operation_id=source.operation_id,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("may_dispatch", _event(source)),),
        handoff=False,
        prepared_append=prepared_append,
    )


def begin_classifier_primary_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    captured: CapturedContinuationRuntime,
    request: PreparedContinuationPrimaryRequest,
    branch: ClassifierPrimaryBranch,
    clock: ContinuationActionClock,
) -> DispatchedClassifierPrimary:
    """Prepare and dispatch in one transaction; never adopt an old intent.

    The caller owns the enclosing locked transaction. Both immutable journal
    checkpoints commit together, under this actual current claim, before SDK.
    """
    clock.require_preparation_time()
    prepared = _prepare_classifier_primary_on_connection(
        conn, running, selected=selected, inventory=inventory, captured=captured, request=request, branch=branch
    )
    dispatched = _begin_classifier_primary_on_connection(conn, prepared)
    clock.require_preparation_time()
    return DispatchedClassifierPrimary(prepared, dispatched)


def publish_classifier_primary_on_connection(
    conn: Connection,
    prepared: PreparedClassifierPrimary,
    *,
    selected: StoredContinuationSnapshot,
    outcome: _CallModelOutcome,
    recorder: RecorderSnapshot,
    permit: _PublishPermit,
    v4_admission: V4ClassifierProviderAdmission | None = None,
    prepared_append: PreparedContinuationAppend | None = None,
) -> ClassifierPrimaryResult:
    issued = _returned.get(permit) if type(permit) is _PublishPermit else None
    if issued is None or any(
        actual is not original for actual, original in zip((prepared, selected, outcome, recorder), issued, strict=True)
    ):
        raise AuditIntegrityError("classifier PRIMARY publication requires its original live response/recorder owner")
    if type(prepared) is not PreparedClassifierPrimary or type(outcome) is not _CallModelOutcome or type(recorder) is not RecorderSnapshot:
        raise AuditIntegrityError("classifier PRIMARY publication lost its actual nominal outcome")
    running, key = prepared.binding.running, prepared.binding.effect_key
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    source = _read_source(conn, running.claim.session_id, running.claim.operation_id, key)
    current = require_snapshot_cas(
        conn,
        session_id=str(source.session_id),
        operation_id=source.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
    )
    if current != selected or _chain(conn, source, dispatched=True) != current:
        raise AuditIntegrityError("classifier PRIMARY publication changed its original dispatched source")
    row = conn.execute(
        select(effects).where(
            effects.c.session_id == str(source.session_id), effects.c.operation_id == source.operation_id, effects.c.effect_key == key
        )
    ).one()
    call = outcome.audit_call
    v4_physical = (
        type(v4_admission) is V4ClassifierProviderAdmission
        and call is not None
        and row.provider_attempt_id == v4_admission.attempt.attempt_id == call.call_id
        and (row.physical_sequence, row.physical_generation) == (source.creator_sequence, source.creator_generation)
        and row.requested_model == call.model_requested
        and selected == v4_admission.dispatched
    )
    legacy_unbound = all(
        value is None for value in (row.provider_attempt_id, row.physical_sequence, row.physical_generation, row.requested_model)
    )
    if (
        row.disposition != "dispatched"
        or row.owner != "provider"
        or row.input_sha256 != source.input_sha256
        or call is None
        or call.call_id is None
        or row.action_json is not None
        or (row.creator_sequence, row.creator_generation) != (source.creator_sequence, source.creator_generation)
        or (source.creator_sequence, source.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        or (not v4_physical if v4_admission is not None else not legacy_unbound)
        or any(
            value is not None
            for value in (
                row.receipt_key,
                row.result_chunk_key,
                row.response_finished_at,
                row.committed_state_ids_json,
                row.graph_sha256,
            )
        )
    ):
        raise AuditIntegrityError("classifier PRIMARY publication cannot replace an unresolved original result")
    if v4_admission is not None:
        admitted_root = decode_continuation_snapshot(current.payload_json)
        admitted_policy = decode_continuation_policy(admitted_root.admission_policy_json, admitted_root.policy_hash)
        if type(admitted_policy) is not ContinuationAdmissionPolicyV4:
            raise AuditIntegrityError("classifier V4 publication changed its admitted policy")
        require_v4_successful_response_audit(
            call,
            outcome.completion.provider_metadata,
            policy=admitted_policy,
            attempt_id=call.call_id,
            started_at=call.started_at,
            finished_at=call.finished_at,
        )
    else:
        require_successful_response_audit(
            call, outcome.completion.provider_metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at
        )
    if v4_admission is None:
        _physical_source_data(conn, source.session_id, source.operation_id, row, call)
    before = decode_recorder_snapshot(_chunk_data(conn, source.session_id, source.operation_id, source.compose.audit_buffer))
    if replace(before, llm_calls=(*before.llm_calls, call)) != recorder or recorder.llm_calls[-1] is not call:
        raise AuditIntegrityError("classifier PRIMARY changed its exact original recorder prefix")
    root = decode_continuation_snapshot(current.payload_json)
    actual = SelectedContinuationState(
        current.checkpoint_id,
        root,
        load_snapshot_private_data_on_connection(
            conn, session_id=str(source.session_id), operation_id=source.operation_id, selected=current
        ),
    )
    evidence = read_selected_turn_evidence_graph_data_on_connection(
        conn, session_id=source.session_id, operation_id=source.operation_id, selected=actual, names=frozenset(source.env_ref_names)
    )
    extended = append_primary_call_to_evidence(evidence, call=call, recorder=recorder, env_ref_names=frozenset(source.env_ref_names))
    response = encode_continuation_response(outcome.completion, audit_call=call)
    retained_evidence = encode_turn_evidence(extended, env_ref_names=frozenset(source.env_ref_names))
    entry = extended.snapshot.application_entry
    if entry is None:
        raise AuditIntegrityError("classifier PRIMARY lost its original complete applied P4 graph")
    applied = encode_private_chunk(
        codec="composer.applied-p4-batch.v2", canonical=entry.graph_json, env_ref_names=frozenset(source.env_ref_names)
    )
    for chunk in (response, extended.recorder_chunk, retained_evidence, applied):
        put_chunk(
            conn,
            session_id=str(source.session_id),
            operation_id=source.operation_id,
            key=private_chunk_key(chunk),
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
    conn.execute(
        update(effects)
        .where(effects.c.session_id == str(source.session_id), effects.c.operation_id == source.operation_id, effects.c.effect_key == key)
        .values(
            disposition="committed",
            receipt_key=call.call_id,
            result_chunk_key=private_chunk_key(response),
            provider_attempt_id=call.call_id,
            physical_sequence=row.creator_sequence,
            physical_generation=row.creator_generation,
            requested_model=call.model_requested,
            response_finished_at=call.finished_at,
        )
    )
    compose = source.compose.model_copy(update={"audit_buffer": extended.recorder_chunk.reference, "evidence": retained_evidence.reference})
    encoded = _effect_snapshot(
        encode_continuation_snapshot(
            root.model_copy(update={"frames": (compose, source.batch.model_copy(update={"accumulator": applied.reference}))})
        ),
        effect_key=key,
        disposition="committed",
        receipt_id=call.call_id,
    )
    committed = append_complete_snapshot(
        conn,
        session_id=str(source.session_id),
        operation_id=source.operation_id,
        expected_checkpoint_id=current.checkpoint_id,
        expected_cursor=current.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("response_retained", _event(source)),),
        handoff=False,
        prepared_append=prepared_append,
    )
    return ClassifierPrimaryResult(
        committed,
        key,
        source.input_sha256,
        response,
        extended.recorder_chunk,
        recorder,
        outcome,
        source.source_checkpoint_id,
        source.source_cursor,
        source.branch,
    )


def read_original_classifier_primary_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    effect_key: str,
) -> ClassifierPrimaryResult:
    """Reprove the complete original paid source; never grant dispatch or retry."""
    if type(session_id) is not UUID or type(operation_id) is not str or type(effect_key) is not str:
        raise TypeError("classifier PRIMARY history requires original nominal SQL identity")
    source = _read_source(conn, session_id, operation_id, effect_key)
    dispatched = _chain(conn, source, dispatched=True)
    committed = _historical(conn, str(session_id), operation_id, source.source_cursor + 3)
    _journal(conn, source, committed.cursor, "response_retained")
    root = decode_continuation_snapshot(committed.payload_json)
    compose, batch = applied_batch_cut(root.frames)
    committed_selection = SelectedContinuationState(
        committed.checkpoint_id,
        root,
        load_snapshot_private_data_on_connection(conn, session_id=str(session_id), operation_id=operation_id, selected=committed),
    )
    committed_evidence = read_selected_turn_evidence_graph_data_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        selected=committed_selection,
        names=frozenset(source.env_ref_names),
    )
    row = conn.execute(
        select(effects).where(
            effects.c.session_id == str(session_id), effects.c.operation_id == operation_id, effects.c.effect_key == effect_key
        )
    ).one()
    if (
        row.disposition != "committed"
        or row.owner != "provider"
        or row.input_sha256 != source.input_sha256
        or type(row.result_chunk_key) is not str
        or row.action_json is not None
        or row.committed_state_ids_json is not None
        or row.graph_sha256 is not None
    ):
        raise AuditIntegrityError("classifier PRIMARY has no original committed response")
    raw = read_chunk(conn, session_id=str(session_id), operation_id=operation_id, key=row.result_chunk_key).decode("utf-8", errors="strict")
    response = encode_private_chunk(codec="composer.response.v1", canonical=raw)
    completion, call = decode_continuation_response(response), decode_retained_physical_audit_call(response)
    if (
        row.result_chunk_key != private_chunk_key(response)
        or call.call_id is None
        or row.provider_attempt_id != call.call_id
        or row.receipt_key != call.call_id
        or (row.physical_sequence, row.physical_generation) != (source.creator_sequence, source.creator_generation)
        or (row.creator_sequence, row.creator_generation) != (source.creator_sequence, source.creator_generation)
        or row.requested_model != call.model_requested
        or row.response_finished_at is None
        or restore_utc(row.response_finished_at) != call.finished_at
    ):
        raise AuditIntegrityError("classifier PRIMARY changed its complete original physical receipt")
    prepared_root = decode_continuation_snapshot(dispatched.payload_json)
    admitted_policy = decode_continuation_policy(prepared_root.admission_policy_json, prepared_root.policy_hash)
    if type(admitted_policy) is ContinuationAdmissionPolicyV4:
        require_v4_successful_response_audit(
            call,
            completion.provider_metadata,
            policy=admitted_policy,
            attempt_id=call.call_id,
            started_at=call.started_at,
            finished_at=call.finished_at,
        )
    else:
        require_successful_response_audit(
            call, completion.provider_metadata, attempt_id=call.call_id, started_at=call.started_at, finished_at=call.finished_at
        )
    _physical_source_data(conn, session_id, operation_id, row, call)
    audit = _chunk_data(conn, session_id, operation_id, compose.audit_buffer)
    buffer = decode_recorder_snapshot(audit)
    matching = tuple(item for item in buffer.llm_calls if item.call_id == call.call_id)
    if len(matching) != 1 or matching[0] != call or buffer.llm_calls[-1] is not matching[0]:
        raise AuditIntegrityError("classifier PRIMARY changed its original retained audit atom")
    call = matching[0]
    prior = decode_recorder_snapshot(_chunk_data(conn, session_id, operation_id, source.compose.audit_buffer))
    if replace(prior, llm_calls=(*prior.llm_calls, call)) != buffer:
        raise AuditIntegrityError("classifier PRIMARY cold source changed its complete recorder/P4 prefix")
    actual = SelectedContinuationState(
        dispatched.checkpoint_id,
        prepared_root,
        load_snapshot_private_data_on_connection(conn, session_id=str(session_id), operation_id=operation_id, selected=dispatched),
    )
    prior_evidence = read_selected_turn_evidence_graph_data_on_connection(
        conn, session_id=session_id, operation_id=operation_id, selected=actual, names=frozenset(source.env_ref_names)
    )
    extended = append_primary_call_to_evidence(prior_evidence, call=call, recorder=buffer, env_ref_names=frozenset(source.env_ref_names))
    entry = extended.snapshot.application_entry
    if entry is None:
        raise AuditIntegrityError("classifier PRIMARY history lost its actual complete P4 graph")
    applied = encode_private_chunk(
        codec="composer.applied-p4-batch.v2", canonical=entry.graph_json, env_ref_names=frozenset(source.env_ref_names)
    )
    if (
        batch != source.batch.model_copy(update={"accumulator": applied.reference})
        or _chunk_data(conn, session_id, operation_id, batch.accumulator) != applied
    ):
        raise AuditIntegrityError("classifier PRIMARY changed its exact retained P4 recorder graph")
    expected_compose = source.compose.model_copy(
        update={
            "audit_buffer": audit.reference,
            "evidence": encode_turn_evidence(extended, env_ref_names=frozenset(source.env_ref_names)).reference,
        }
    )
    expected = decode_continuation_snapshot(
        _effect_snapshot(
            encode_continuation_snapshot(prepared_root.model_copy(update={"frames": (expected_compose, batch)})),
            effect_key=effect_key,
            disposition="committed",
            receipt_id=call.call_id,
        )
    ).model_copy(update={"cursor": committed.cursor})
    if (
        root != expected
        or audit != extended.recorder_chunk
        or committed_evidence.source_chunk != encode_turn_evidence(extended, env_ref_names=frozenset(source.env_ref_names))
    ):
        raise AuditIntegrityError("classifier PRIMARY changed its complete original committed root")
    if type(admitted_policy) is ContinuationAdmissionPolicyV4:
        from elspeth.web.sessions.continuation_classifier_primary_complete_cold import require_v4_classifier_complete_hold_on_connection

        require_v4_classifier_complete_hold_on_connection(
            conn, source=source, dispatched=dispatched, effect=row, call=call, completion=completion
        )
    return ClassifierPrimaryResult(
        committed,
        effect_key,
        source.input_sha256,
        response,
        audit,
        buffer,
        _CallModelOutcome(completion, call),
        source.source_checkpoint_id,
        source.source_cursor,
        source.branch,
    )


@dataclass(frozen=True, slots=True, eq=False)
class ClassifierReplyDeadline:
    """Original uninvoked reply observation; only its issuing owner can verify it."""

    observation: ClassifierOwnedSourceObservation
    runtime_preflight_json: str | None


class ClassifierPrimaryIssuer:
    """One live classifier attempt, admitted afresh at each SQL/SDK boundary."""

    def __init__(
        self,
        *,
        service: ComposerServiceImpl,
        authority: ComposerAsyncOperationAuthority,
        running: ComposerOperationRunning,
        selected: SelectedContinuationState,
        inventory: CurrentContinuationEnvInventory,
        runtime: RestoredContinuationRuntime,
        required_work: RequiredWorkBinding,
        provider_owner: ProviderInvocationOwner,
        clock: ContinuationActionClock,
    ) -> None:
        from elspeth.web.composer.service import ComposerServiceImpl
        from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority

        if (
            type(service) is not ComposerServiceImpl
            or type(authority) is not ComposerAsyncOperationAuthority
            or type(running) is not ComposerOperationRunning
            or type(selected) is not SelectedContinuationState
            or type(runtime) is not RestoredContinuationRuntime
            or type(inventory) is not CurrentContinuationEnvInventory
            or type(required_work) is not RequiredWorkBinding
            or type(provider_owner) is not ProviderInvocationOwner
            or type(clock) is not ContinuationActionClock
            or required_work.running is not running
            or provider_owner.required_work is not required_work
            or runtime.source.selected is not selected
            or runtime.materialization.selected is not selected
            or runtime.materialization.inventory is not inventory
            or runtime.materialization.service is not service
            or runtime.materialization.authority is not authority
            or service._session_engine is not authority._engine
            or provider_owner.service is not service._require_sessions_service()
            or inventory.running is not running
            or runtime.bindings.provider_owner is not provider_owner
            or runtime.bindings.required_work is not required_work
            or runtime.bindings.session_operation_context is not running.session_operation_context
        ):
            raise AuditIntegrityError("classifier PRIMARY issuer crossed its actual live owners")
        policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
        if clock.policy != policy.actions.primary:
            raise AuditIntegrityError("classifier PRIMARY cannot replace its original admitted action clock")
        owner = required_work.coordinator.sql_deadline_owner
        if type(owner) is not ContinuationSQLDeadlineOwner or owner.running is not running or owner.policy != policy:
            raise AuditIntegrityError("classifier PRIMARY requires its actual original SQL deadline owner")
        self.service, self.authority, self.running, self.selected = service, authority, running, selected
        self.inventory, self.runtime, self.work, self.provider_owner, self.clock = inventory, runtime, required_work, provider_owner, clock
        self._started = False
        self._sdk_entered = False
        self._failure: BaseException | None = None
        self._result: ClassifierPrimaryResult | None = None
        self._terminal: (
            VerifiedClassifierPaidOverflowTerminal
            | VerifiedClassifierPaidIncompleteTerminal
            | ClassifierNoSdkRefusalReceipt
            | SdkTerminalResult
            | SdkTerminalFailure
            | None
        ) = None
        self._reply_deadline: float | None = None
        self._reply_deadline_observation: ClassifierReplyDeadline | None = None
        self._reply_clock = clock
        self._reply_sql_owner = owner

    def bind_reply_deadline(self, deadline: float) -> None:
        """Bind the observer's original deadline before entering the classifier."""
        owner = self.work.coordinator.sql_deadline_owner
        now = time.monotonic()
        if (
            type(deadline) is not float
            or not math.isfinite(deadline)
            or type(owner) is not ContinuationSQLDeadlineOwner
            or owner is not self._reply_sql_owner
            or self.clock is not self._reply_clock
            or owner.running is not self.running
            or self._reply_deadline is not None
            or self._started
            or deadline > min(self.runtime.bindings.deadline, self.clock.started_monotonic + self.clock.policy.total)
            or deadline > now + owner.remaining_dispatch(monotonic_now=now)
        ):
            raise AuditIntegrityError("classifier reply substituted its original active deadline")
        owner.require_action_clock(self.clock)
        self._reply_deadline = deadline

    def seal_reply_deadline(
        self, observation: ClassifierOwnedSourceObservation, *, runtime_preflight_json: str | None
    ) -> ClassifierReplyDeadline:
        """Close this never-invoked issuer at the actual elapsed reply boundary."""
        self._reply_sql_owner.require_action_clock(self._reply_clock)
        fence = self.running.session_operation_context.fence
        if (
            type(observation) is not ClassifierOwnedSourceObservation
            or observation.kind != "reply_deadline_exhausted"
            or self._reply_deadline is None
            or self.clock is not self._reply_clock
            or self.work.coordinator.sql_deadline_owner is not self._reply_sql_owner
            or observation.deadline != self._reply_deadline
            or observation.observed_monotonic is None
            or observation.observed_monotonic > time.monotonic()
            or observation.observed_monotonic < self._reply_deadline
            or observation.clock_started_monotonic != self.clock.started_monotonic
            or observation.clock_policy_json != canonical_json(self.clock.policy.model_dump(mode="json"))
            or observation.source_checkpoint_id != self.selected.checkpoint_id
            or observation.source_cursor != self.selected.stack.cursor
            or observation.state_sha256 != stable_hash(self.runtime.live.state.to_dict())
            or observation.physical_operation_id != fence.operation_id
            or observation.physical_operation_epoch != fence.operation_epoch
            or self._started
            or self._sdk_entered
            or self._failure is not None
            or self._result is not None
            or self._reply_deadline_observation is not None
        ):
            raise AuditIntegrityError("reply deadline requires its original never-invoked issuer observation")
        decode_classifier_runtime_preflight(runtime_preflight_json)
        sealed = ClassifierReplyDeadline(observation, runtime_preflight_json)
        self._reply_deadline_observation = sealed
        return sealed

    def require_reply_deadline(self, sealed: ClassifierReplyDeadline) -> ClassifierOwnedSourceObservation:
        """Verify exact live custody; decoded or copied observations grant nothing."""
        owner = self.work.coordinator.sql_deadline_owner
        if (
            type(sealed) is not ClassifierReplyDeadline
            or sealed is not self._reply_deadline_observation
            or self._started
            or self._sdk_entered
            or self._failure is not None
            or self._result is not None
            or type(owner) is not ContinuationSQLDeadlineOwner
            or owner is not self._reply_sql_owner
            or self.clock is not self._reply_clock
            or owner.running is not self.running
            or self.work.running is not self.running
            or self.provider_owner.required_work is not self.work
            or sealed.observation.deadline != self._reply_deadline
            or sealed.observation.clock_started_monotonic != self.clock.started_monotonic
            or sealed.observation.clock_policy_json != canonical_json(self.clock.policy.model_dump(mode="json"))
        ):
            raise AuditIntegrityError("reply deadline disposition lost its exact original issuer")
        owner.require_action_clock(self.clock)
        decode_classifier_runtime_preflight(sealed.runtime_preflight_json)
        return sealed.observation

    @property
    def result(self) -> ClassifierPrimaryResult | None:
        return self._result

    @property
    def terminal(
        self,
    ) -> (
        VerifiedClassifierPaidOverflowTerminal
        | VerifiedClassifierPaidIncompleteTerminal
        | ClassifierNoSdkRefusalReceipt
        | SdkTerminalResult
        | SdkTerminalFailure
        | None
    ):
        return self._terminal

    @property
    def sdk_entered(self) -> bool:
        return self._sdk_entered

    def require_original_prephysical_failure(self, original: BaseException) -> TimeoutError:
        """Identify the actual completed invocation, never authorize a terminal."""
        from elspeth.web.composer.protocol import ComposerConvergenceError

        failure = self._failure
        if (
            type(original) is not ComposerConvergenceError
            or original.budget_exhausted != "timeout"
            or type(failure) is not TimeoutError
            or original.__context__ is not failure
            or not self._started
            or self._sdk_entered
            or self._result is not None
            or self._terminal is not None
            or self._reply_deadline_observation is not None
        ):
            raise AuditIntegrityError("classifier failure has no original completed prephysical invocation")
        return failure

    async def _sql[T](self, operation: Callable[[Connection], T]) -> T:
        def transaction() -> T:
            with (
                locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn,
                retention_stage_memo_scope(),
            ):
                result = operation(conn)
            # The real transaction exited successfully. Preserve the actual
            # committed result before bridge projection can deliver cancellation;
            # a rolled-back writer candidate never becomes a committed claim.
            if isinstance(result, ClassifierPrimaryResult) and type(result) is ClassifierPrimaryResult:
                self._result = result
            return result

        sql, projection = self.work.reserve_pair(
            RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION
        )
        try:
            outcome = await run_required_sql_finish_once(sql, transaction)
        except BaseException:
            projection.complete_without_submission()
            raise
        try:
            self.work.coordinator.validate_continuation_sql_outcome(
                ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome
            )
        except BaseException:
            projection.complete_without_submission()
            raise
        failures: list[BaseException] = []
        value: T | None = None
        if type(outcome) is RequiredSQLRaised:
            projection.complete_without_submission()
            failures.append(outcome.error)
        elif type(outcome) is RequiredSQLReturned:
            projection.begin_projection()
            try:
                if type(outcome.value) not in (PreparedClassifierPrimary, DispatchedClassifierPrimary, ClassifierPrimaryResult):
                    raise AuditIntegrityError("classifier state SQL returned an unfamiliar owner")
                value = outcome.value
            except BaseException as error:
                projection.complete_owned(error)
                failures.append(error)
            else:
                projection.complete_owned()
        else:
            projection.complete_without_submission()
            raise AuditIntegrityError("classifier state SQL lost its finish-once outcome")
        for cancellation in outcome.deferred_cancellations:
            if all(cancellation is not prior for prior in failures):
                failures.append(cancellation)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("classifier state SQL and original cancellation", failures)
        if value is None:
            raise AuditIntegrityError("classifier state SQL lost its actual committed owner")
        return value

    async def invoke(
        self,
        *,
        branch: ClassifierPrimaryBranch,
        prepared_input: PreparedProviderInput,
        recorder: BufferingRecorder,
        deadline: float,
        credential_state: CompositionState | None = None,
    ) -> (
        ClassifierPrimaryResult
        | VerifiedClassifierPaidOverflowTerminal
        | VerifiedClassifierPaidIncompleteTerminal
        | ClassifierNoSdkRefusalReceipt
        | SdkTerminalResult
        | SdkTerminalFailure
    ):
        if self._reply_deadline_observation is not None:
            raise AuditIntegrityError("classifier reply deadline permanently closed its original dispatch")
        if self._started:
            if self._failure is not None:
                raise self._failure
            raise AuditIntegrityError("classifier PRIMARY cannot issue or replay a completed physical attempt")
        self._started = True
        try:
            return await self._invoke(
                branch=branch, prepared_input=prepared_input, recorder=recorder, deadline=deadline, credential_state=credential_state
            )
        except BaseException as error:
            self._failure = error
            raise

    async def _invoke(
        self,
        *,
        branch: ClassifierPrimaryBranch,
        prepared_input: PreparedProviderInput,
        recorder: BufferingRecorder,
        deadline: float,
        credential_state: CompositionState | None,
    ) -> (
        ClassifierPrimaryResult
        | VerifiedClassifierPaidOverflowTerminal
        | VerifiedClassifierPaidIncompleteTerminal
        | ClassifierNoSdkRefusalReceipt
        | SdkTerminalResult
        | SdkTerminalFailure
    ):
        policy = decode_continuation_policy(self.selected.stack.admission_policy_json, self.selected.stack.policy_hash)
        if type(policy) is ContinuationAdmissionPolicyV3:
            raise ContinuationRetainedTailUnbounded("classifier PRIMARY has no proven complete retained result bound")
        if type(policy) not in (ContinuationAdmissionPolicyV2, ContinuationAdmissionPolicyV4):
            raise AuditIntegrityError("classifier PRIMARY cannot fall back from its current admitted policy")
        if (
            type(prepared_input) is not PreparedProviderInput
            or recorder is not self.runtime.live.recorder
            or type(deadline) not in (int, float)
            or not math.isfinite(deadline)
            or branch not in ("staged_review", "composition_last_chance")
            or (branch == "staged_review" and credential_state is not None)
            or (branch == "composition_last_chance" and credential_state is not self.runtime.live.state)
            or prepared_input.genuine_user != self.runtime.bindings.message
        ):
            raise AuditIntegrityError("classifier PRIMARY changed its original branch/input/credential owner")
        owner = self.work.coordinator.sql_deadline_owner
        if type(owner) is not ContinuationSQLDeadlineOwner:
            raise AuditIntegrityError("classifier PRIMARY lost its original SQL clock")
        deadline = min(
            deadline,
            self.runtime.bindings.deadline,
            self.clock.deadline,
            time.monotonic() + owner.remaining_dispatch(monotonic_now=time.monotonic()),
        )
        self.clock.require_preparation_time()
        if time.monotonic() >= deadline:
            raise TimeoutError("classifier PRIMARY original allowance expired before preparation")
        budget = self.provider_owner.cost_budget
        if type(budget) is not CommonProviderCostBudget:
            raise AuditIntegrityError("classifier PRIMARY lost its original shared cost owner")
        captured = await capture_continuation_runtime_owners(self.runtime, selected=self.selected, inventory=self.inventory)
        request: PreparedContinuationPrimaryRequest | None = None
        no_sdk_issuer: ClassifierNoSdkIssuer | None = None
        if type(policy) is ContinuationAdmissionPolicyV4:
            request = self.service._provider_gateway.prepare_continuation_primary_from_input(prepared_input, cost_budget=budget)
            no_sdk_issuer = ClassifierNoSdkIssuer(
                running=self.running,
                work=self.work,
                selected_checkpoint_id=self.selected.checkpoint_id,
                selected_cursor=self.selected.stack.cursor,
                input_sha256=request.input_sha256,
                clock=self.clock,
            )

        def begin(conn: Connection) -> DispatchedClassifierPrimary | PreparedClassifierPrimary:
            nonlocal request
            from elspeth.web.sessions.continuation_iteration_return import read_iteration_stage_on_connection

            _require_current_continuation_running(conn, self.running)
            current = require_snapshot_cas(
                conn,
                session_id=str(self.running.claim.session_id),
                operation_id=self.running.claim.operation_id,
                expected_checkpoint_id=self.selected.checkpoint_id,
                expected_cursor=self.selected.stack.cursor,
            )
            if current.payload_json != encode_continuation_snapshot(self.selected.stack):
                raise AuditIntegrityError("classifier PRIMARY input preparation changed its actual current source")
            require_current_env_inventory_on_connection(
                conn,
                self.running,
                inventory=self.inventory,
                expected_checkpoint_id=current.checkpoint_id,
                expected_cursor=current.cursor,
            )
            stage = read_iteration_stage_on_connection(conn, self.running, selected=current)
            if stage is None or stage.stage != "classify_dispatched":
                raise AuditIntegrityError("classifier PRIMARY input has no genuine current classifier source")
            if (
                conn.execute(
                    select(chunks.c.chunk_key).where(
                        chunks.c.session_id == str(self.running.claim.session_id),
                        chunks.c.operation_id == self.running.claim.operation_id,
                        chunks.c.chunk_key == _stage_key(stage.owner_effect_key),
                    )
                ).one_or_none()
                is not None
            ):
                raise AuditIntegrityError("classifier PRIMARY stage already has its original physical attempt; replay is forbidden")
            source = read_continuation_cost_snapshot_on_connection(conn, self.selected.stack)
            if source != budget.snapshot() or source.policy != continuation_common_cost_policy(
                cast(ContinuationAdmissionPolicyV2 | ContinuationAdmissionPolicyV3 | ContinuationAdmissionPolicyV4, policy)
            ):
                raise AuditIntegrityError("classifier PRIMARY changed its immutable current cost source")
            if request is None:
                request = self.service._provider_gateway.prepare_continuation_primary_from_input(prepared_input, cost_budget=budget)
            if type(policy) is ContinuationAdmissionPolicyV4:
                if no_sdk_issuer is None:
                    raise AuditIntegrityError("V4 classifier preparation lost its original no-SDK issuer")
                prepared = _prepare_classifier_primary_on_connection(
                    conn,
                    self.running,
                    selected=self.selected,
                    inventory=self.inventory,
                    captured=captured,
                    request=request,
                    branch=branch,
                )
                no_sdk_issuer.note_prepared_candidate(prepared)
                return prepared
            return begin_classifier_primary_on_connection(
                conn,
                self.running,
                selected=self.selected,
                inventory=self.inventory,
                captured=captured,
                request=request,
                branch=branch,
                clock=self.clock,
            )

        initial = await self._sql(begin)
        if type(request) is not PreparedContinuationPrimaryRequest:
            raise AuditIntegrityError("classifier PRIMARY lost its original prepared physical request")
        if type(policy) is ContinuationAdmissionPolicyV4:
            if type(initial) is not PreparedClassifierPrimary or no_sdk_issuer is None:
                raise AuditIntegrityError("V4 classifier lost its original committed preparation")
            no_sdk_issuer.bind_prepared(initial)
            return await self._invoke_v4_prepared(
                prepared=initial,
                request=request,
                no_sdk_issuer=no_sdk_issuer,
                branch=branch,
                prepared_input=prepared_input,
                recorder=recorder,
                deadline=deadline,
                credential_state=credential_state,
            )
        if type(initial) is not DispatchedClassifierPrimary:
            raise AuditIntegrityError("classifier V2 lost its original committed dispatch")
        dispatched = initial
        prepared = dispatched.prepared
        self.clock.finish_preparation()
        remaining = min(deadline - time.monotonic(), owner.remaining_dispatch(monotonic_now=time.monotonic()))
        if remaining <= 0:
            raise TimeoutError("classifier PRIMARY original allowance expired before SDK entry")
        custody = self.provider_owner.mint(ProviderInvocationFamily.PRIMARY)
        custody.bind_continuation_primary_clock(self.clock)

        def entered() -> None:
            self._sdk_entered = True

        outcome = await self.service._provider_gateway._call_llm_outcome_with_audit(
            prepared_input.evidence.messages,
            prepared_input.tools if prepared_input.tools is not None else [],
            timeout=remaining,
            recorder=recorder,
            provider_custody=custody,
            credential_state=credential_state,
            continuation_input_sha256=request.input_sha256,
            prepared_input=prepared_input,
            on_sdk_entry=entered,
        )
        buffer = recorder.snapshot_for_continuation(drained=self.runtime.drained)
        permit = _PublishPermit()
        _returned[permit] = (prepared, dispatched.snapshot, outcome, buffer)

        def publish(conn: Connection) -> ClassifierPrimaryResult:
            result = publish_classifier_primary_on_connection(
                conn, prepared, selected=dispatched.snapshot, outcome=outcome, recorder=buffer, permit=permit
            )
            return result

        return await self.clock.run("retention", lambda: self._sql(publish))

    async def _invoke_v4_prepared(
        self,
        *,
        prepared: PreparedClassifierPrimary,
        request: PreparedContinuationPrimaryRequest,
        no_sdk_issuer: ClassifierNoSdkIssuer,
        branch: ClassifierPrimaryBranch,
        prepared_input: PreparedProviderInput,
        recorder: BufferingRecorder,
        deadline: float,
        credential_state: CompositionState | None,
    ) -> (
        ClassifierPrimaryResult
        | VerifiedClassifierPaidOverflowTerminal
        | VerifiedClassifierPaidIncompleteTerminal
        | ClassifierNoSdkRefusalReceipt
        | SdkTerminalResult
        | SdkTerminalFailure
    ):
        from elspeth.web.composer.continuation_primary import _pre_sdk_failure
        from elspeth.web.composer.provider_quota import V4PaidIncompleteCommitted, V4SdkTerminalCommitted
        from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
        from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation
        from elspeth.web.composer.provider_sdk_terminal import capture_sdk_terminal_failure
        from elspeth.web.sessions.service import SessionServiceImpl

        sessions = self.service._require_sessions_service()
        if type(sessions) is not SessionServiceImpl or no_sdk_issuer.prepared_candidate is not prepared:
            raise AuditIntegrityError("V4 classifier lost its original prepared sessions owner")
        self.clock.finish_preparation()
        custody = self.provider_owner.mint(ProviderInvocationFamily.PRIMARY)
        custody.bind_continuation_primary_clock(self.clock)
        custody.bind_classifier_no_sdk_issuer(no_sdk_issuer)
        provider = PreparedClassifierPrimaryProvider(prepared, request)

        async def finalizer(
            outcome: _CallModelOutcome, sql: RequiredWorkTicket
        ) -> RequiredSQLFinishOnce[ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal]:
            from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness

            admission = custody.continuation_classifier_v4_admission
            if type(admission) is not V4ClassifierProviderAdmission:
                raise AuditIntegrityError("V4 classifier response lost its original admitted physical/H")
            witness = outcome.completion.provider_metadata.v4_completeness
            if type(witness) is not V4ProviderResultCompleteness:
                raise AuditIntegrityError("V4 classifier response lost its actual SDK return witness")
            custody.require_v4_result_completeness(witness)
            buffer = recorder.snapshot_for_continuation(drained=self.runtime.drained)
            permit = _PublishPermit()
            _returned[permit] = (prepared, admission.dispatched, outcome, buffer)
            return await sessions.finalize_v4_continuation_classifier(
                prepared=provider,
                admission=admission,
                outcome=outcome,
                recorder=buffer,
                permit=permit,
                required_work=sql,
            )

        custody.bind_continuation_classifier_v4(provider, finalizer)

        async def incomplete_finalizer(
            incomplete: V4ProviderResultIncomplete, sql: RequiredWorkTicket
        ) -> RequiredSQLFinishOnce[VerifiedPaidIncompleteTerminal]:
            admission = custody.continuation_classifier_v4_admission
            call = custody.call
            if type(admission) is not V4ClassifierProviderAdmission or type(call) is not ComposerLLMCall:
                raise AuditIntegrityError("V4 classifier incomplete result lost its admitted physical audit/H")
            custody.require_v4_result_incomplete(incomplete)
            return await sessions.finalize_v4_continuation_classifier_incomplete(
                prepared=provider,
                admission=admission,
                incomplete=incomplete,
                call=call,
                required_work=sql,
            )

        custody.bind_v4_returned_incomplete_finalizer(incomplete_finalizer)

        async def sdk_finalizer(
            observation: V4SdkFailureObservation, sql: RequiredWorkTicket
        ) -> RequiredSQLFinishOnce[VerifiedSdkTerminal]:
            deadline_owner = self.work.coordinator.sql_deadline_owner
            if deadline_owner is not None:
                deadline_owner.begin_settlement()
            result = await sessions.finalize_v4_sdk_terminal(observation=observation, custody=custody, required_work=sql)
            self.work.coordinator.validate_provider_settlement_sql_outcome(ticket=sql, actual_outcome=result)
            return result

        custody.bind_v4_sdk_finalizer(sdk_finalizer)

        def entered() -> None:
            self._sdk_entered = True

        owner = self.work.coordinator.sql_deadline_owner
        if type(owner) is not ContinuationSQLDeadlineOwner:
            raise AuditIntegrityError("V4 classifier lost its original SQL allowance")
        remaining = min(deadline - time.monotonic(), owner.remaining_dispatch(monotonic_now=time.monotonic()))
        try:
            if remaining <= 0:
                raise TimeoutError("V4 classifier original allowance expired before SDK entry")
            outcome = await self.service._provider_gateway._call_llm_outcome_with_audit(
                prepared_input.evidence.messages,
                prepared_input.tools if prepared_input.tools is not None else [],
                timeout=remaining,
                recorder=recorder,
                provider_custody=custody,
                credential_state=credential_state,
                continuation_input_sha256=request.input_sha256,
                prepared_input=prepared_input,
                on_sdk_entry=entered,
            )
        except V4SdkTerminalCommitted as control:
            if custody.v4_sdk_terminal_result is not control.finalization:
                raise AuditIntegrityError("classifier SDK result changed original committed custody") from control
            finalized = control.finalization
            sdk_terminal = SdkTerminalResult(finalized.verified, SdkTerminalFamily.CLASSIFIER, finalized.deferred_cancellations)
            self._terminal = sdk_terminal
            return sdk_terminal
        except V4PaidIncompleteCommitted as control:
            committed = custody.require_v4_paid_incomplete_committed(control)
            admission = custody.continuation_classifier_v4_admission
            verified = committed.verified
            if (
                type(admission) is not V4ClassifierProviderAdmission
                or type(verified) is not VerifiedPaidIncompleteTerminal
                or verified.source != admission.hold
                or verified.provider_attempt_id != admission.attempt.attempt_id
            ):
                raise AuditIntegrityError("V4 classifier incomplete terminal lost its original paid source") from control
            terminal = VerifiedClassifierPaidIncompleteTerminal(verified, committed.deferred_cancellations)
            self._terminal = terminal
            return terminal
        except BaseException as error:
            exceptional = capture_sdk_terminal_failure(error, custody=custody)
            if exceptional is not None:
                self._terminal = exceptional
                return exceptional
            failure = _pre_sdk_failure(error, preparing=False)
            admission = custody.continuation_classifier_v4_admission
            if type(admission) is V4ClassifierProviderAdmission and custody.has_v4_unentered_hold() and failure is not None:
                return await self._finish_v4_no_sdk(
                    sessions=sessions,
                    custody=custody,
                    issuer=no_sdk_issuer,
                    admission=admission,
                    reason=ClassifierNoSdkRefusalReason(failure[0].value),
                    original_failure=failure[1],
                    original_cancellations=failure[2],
                )
            raise
        result = custody.continuation_classifier_v4_result
        if type(result) is ClassifierPrimaryResult:
            if result.outcome is not outcome or result.branch != branch:
                raise AuditIntegrityError("V4 classifier complete result changed original paid outcome or branch")
            settled = replace(result, deferred_cancellations=custody.continuation_classifier_v4_deferred_cancellations)
            self._result = settled
            return settled
        if type(result) is VerifiedClassifierPaidOverflowTerminal:
            admission = custody.continuation_classifier_v4_admission
            if type(admission) is not V4ClassifierProviderAdmission or result.receipt.identity.attempt_id != admission.attempt.attempt_id:
                raise AuditIntegrityError("V4 classifier terminal changed its original physical attempt")
            overflow_terminal = replace(result, deferred_cancellations=custody.continuation_classifier_v4_deferred_cancellations)
            self._terminal = overflow_terminal
            return overflow_terminal
        raise AuditIntegrityError("V4 classifier returned without its exact complete or terminal SQL result")

    async def _finish_v4_no_sdk(
        self,
        *,
        sessions: SessionServiceImpl,
        custody: ProviderCallCustody,
        issuer: ClassifierNoSdkIssuer,
        admission: V4ClassifierProviderAdmission,
        reason: ClassifierNoSdkRefusalReason,
        original_failure: BaseException,
        original_cancellations: tuple[asyncio.CancelledError, ...],
    ) -> ClassifierNoSdkRefusalReceipt:
        from elspeth.web.composer.provider_quota import ProviderCallCustody
        from elspeth.web.sessions.continuation_classifier_primary_no_sdk import (
            ClassifierNoSdkRefusalReceipt,
            read_classifier_no_sdk_refusal_on_connection,
        )
        from elspeth.web.sessions.service import SessionServiceImpl

        if type(sessions) is not SessionServiceImpl or type(custody) is not ProviderCallCustody:
            raise AuditIntegrityError("V4 classifier no-SDK disposition lost its actual physical custody")
        owner = self.work.coordinator.sql_deadline_owner
        if owner is not None:
            owner.begin_settlement()
        permit = issuer.reserve_v4_refusal(admission=admission, reason=reason, original_failure=original_failure)
        cancellations: list[asyncio.CancelledError] = []
        if type(original_failure) is asyncio.CancelledError:
            cancellations.append(original_failure)
        cancellations.extend(item for item in original_cancellations if all(item is not prior for prior in cancellations))

        async def cold() -> ClassifierNoSdkRefusalReceipt | None:
            def read() -> ClassifierNoSdkRefusalReceipt | None:
                with locked_session_transaction(self.authority._engine, str(self.running.claim.session_id)) as conn:
                    return read_classifier_no_sdk_refusal_on_connection(
                        conn,
                        session_id=self.running.claim.session_id,
                        operation_id=self.running.claim.operation_id,
                        effect_key=admission.effect_key,
                    )

            sql, projection = self.work.reserve_pair(
                RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION
            )
            observed = await run_required_sql_finish_once(sql, read)
            try:
                self.work.coordinator.validate_continuation_sql_outcome(
                    ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=observed
                )
            except BaseException:
                projection.complete_without_submission()
                raise
            if type(observed) is RequiredSQLRaised:
                projection.complete_without_submission()
                raise observed.error
            if type(observed) is not RequiredSQLReturned:
                projection.complete_without_submission()
                raise AuditIntegrityError("V4 classifier no-SDK read lost its finish-once result")
            projection.begin_projection()
            if observed.value is not None and type(observed.value) is not ClassifierNoSdkRefusalReceipt:
                error = AuditIntegrityError("V4 classifier no-SDK read returned a foreign terminal")
                projection.complete_owned(error)
                raise error
            projection.complete_owned()
            cancellations.extend(item for item in observed.deferred_cancellations if all(item is not prior for prior in cancellations))
            return observed.value

        receipt: ClassifierNoSdkRefusalReceipt | None = None
        last_failure: BaseException | None = None
        for recurrence in range(2):
            ticket = self.work.coordinator.reserve(
                RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,
                transition_ordinal=self.work.transition_ordinal,
                semantic_ordinal=self.work.semantic_ordinal,
                recurrence_ordinal=recurrence,
            )
            outcome = await sessions.cancel_v4_continuation_classifier_unentered(permit=permit, required_work=ticket)
            self.work.coordinator.validate_continuation_sql_outcome(
                ticket=ticket, expected_source=RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL, actual_outcome=outcome
            )
            cancellations.extend(item for item in outcome.deferred_cancellations if all(item is not prior for prior in cancellations))
            if type(outcome) is RequiredSQLReturned:
                if type(outcome.value) is not ClassifierNoSdkRefusalReceipt:
                    raise AuditIntegrityError("V4 classifier zero-use SQL returned a foreign terminal")
                receipt = outcome.value
            elif type(outcome) is RequiredSQLRaised:
                last_failure = outcome.error
            else:
                raise AuditIntegrityError("V4 classifier zero-use SQL lost its finish-once result")
            proved = await cold()
            if proved is not None:
                if receipt is not None and proved != receipt:
                    raise AuditIntegrityError("V4 classifier no-SDK SQL changed its exact cold terminal")
                receipt = proved
                break
        if receipt is None:
            if last_failure is not None:
                raise last_failure
            raise AuditIntegrityError("V4 classifier no-SDK refusal lost its exact terminal")
        issuer.mark_refusal_committed(permit)
        custody.note_v4_classifier_no_sdk(receipt, admission)
        terminal = replace(receipt, deferred_cancellations=tuple(cancellations))
        self._terminal = terminal
        return terminal
