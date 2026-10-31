"""Current business application of one actually settled successful TITLE.

Historical response/accounting is DATA. Only the actual current live writer
may change title/phase; ready replay verifies the actual transition and never
reapplies a title over a later legitimate rename.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.continuation import ContinuationValue, InitializeFrame, PrivateChunkRef
from elspeth.web.composer.continuation_clock import SegmentFit, admit_action
from elspeth.web.composer.continuation_codec import PrivateChunk, decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_title_values import DecodedTitleProviderResult
from elspeth.web.sessions._auto_title import _admit_title_candidate, _RejectedTitle
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    append_complete_snapshot,
    load_snapshot,
    read_chunk,
)
from elspeth.web.sessions.continuation_bootstrap_source import _pretitle_checkpoint_at_cursor, pretitle_recovery_origin_data_on_connection
from elspeth.web.sessions.continuation_title_provider import (
    _require_audit_cohort_data,
    _timing,
    read_title_result_data_on_connection,
    require_title_creator,
)
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import sessions_table as sessions

if TYPE_CHECKING:
    from elspeth.web.sessions.service import SessionServiceImpl


class TitleApplicationDecision(ContinuationValue):
    disposition: Literal["accepted", "empty_response", "rejected"]
    candidate: str | None
    rejection_class: str | None


class TitleApplicationEvent(ContinuationValue):
    schema_version: Literal["composer.title-application.v1"] = "composer.title-application.v1"
    owner: Literal["composer_title_application"] = "composer_title_application"
    predecessor_checkpoint_id: str = Field(min_length=1)
    predecessor_cursor: int = Field(ge=0)
    result: PrivateChunkRef
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    effect_key: Literal["provider:3:0"] = "provider:3:0"
    attempt_id: str = Field(min_length=1)
    ledger_entry_id: str = Field(min_length=1)
    creator_sequence: int = Field(ge=1)
    creator_generation: int = Field(ge=1)
    creator_operation_id: str = Field(min_length=1)
    creator_operation_epoch: int = Field(ge=1)
    decision: TitleApplicationDecision


def title_application_decision(result: DecodedTitleProviderResult) -> TitleApplicationDecision:
    if type(result) is not DecodedTitleProviderResult:
        raise AuditIntegrityError("TITLE application requires its complete original decoded SUCCESS")
    content = result.completion.message.content
    if content is None:
        return TitleApplicationDecision(disposition="empty_response", candidate=None, rejection_class=None)
    candidate = _admit_title_candidate(content)
    if isinstance(candidate, _RejectedTitle):
        return TitleApplicationDecision(disposition="rejected", candidate=None, rejection_class=candidate.rejection_class)
    return TitleApplicationDecision(disposition="accepted", candidate=candidate, rejection_class=None)


def _frame(selected: StoredContinuationSnapshot) -> InitializeFrame:
    root = decode_continuation_snapshot(selected.payload_json)
    frame = root.frames[0]
    if (
        len(root.frames) != 1
        or type(frame) is not InitializeFrame
        or frame.ingress_phase not in ("title_pending", "ready")
        or frame.bootstrap is None
        or frame.ingress_receipt_json is None
        or frame.title_attempt_id is None
        or frame.title_response is None
        or frame.title_response.codec != "composer.title-provider-result.v1"
        or root.pending_settlement_ids
        or root.pending_owner_results
        or root.semantic_frontier is not None
        or root.active_action_json is not None
        or root.active_action_source is not None
        or len(root.effect_frontier) != 1
        or root.effect_frontier[0].effect_id != "provider:3:0"
        or root.effect_frontier[0].disposition != "committed"
        or root.effect_frontier[0].receipt_id != f"title:{frame.title_response.digest}"
    ):
        raise AuditIntegrityError("TITLE application lost its complete settled-success cut")
    if frame.ingress_phase == "title_pending":
        if frame.provider_effect_key != "provider:3:0" or frame.provider_input_sha256 is None:
            raise AuditIntegrityError("TITLE application lost its original pending provider locator")
    elif frame.provider_effect_key is not None or frame.provider_input_sha256 is not None:
        raise AuditIntegrityError("TITLE ready application retained a dispatch locator")
    return frame


def require_current_title_application(conn: Connection, running: ComposerOperationRunning, selected: StoredContinuationSnapshot) -> None:
    if type(running) is not ComposerOperationRunning or type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("TITLE application requires actual nominal running/selected owners")
    # Business authority deliberately excludes owed-accounting cancel/expiry relief.
    _require_current_continuation_running(conn, running)
    if load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id) != selected:
        raise ContinuationCASConflict("TITLE application changed its full current root CAS")
    _frame(selected)


@dataclass(frozen=True, slots=True)
class CurrentTitleApplicationNames:
    running: ComposerOperationRunning
    selected: StoredContinuationSnapshot
    names: frozenset[str]

    def require(self, conn: Connection) -> None:
        if type(self.names) is not frozenset or any(type(name) is not str for name in self.names):
            raise AuditIntegrityError("TITLE application lost its actual current names producer")
        require_current_title_application(conn, self.running, self.selected)


@dataclass(frozen=True, slots=True)
class CurrentTitleApplication:
    selected: StoredContinuationSnapshot
    result: PrivateChunk
    decision: TitleApplicationDecision
    inventory: CurrentTitleApplicationNames
    applied: bool
    event: TitleApplicationEvent | None


@dataclass(frozen=True, slots=True)
class TitleApplicationRequest:
    running: ComposerOperationRunning
    selected: StoredContinuationSnapshot
    inventory: CurrentTitleApplicationNames
    result: PrivateChunk
    decision: TitleApplicationDecision


def _settled_source(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
    names: frozenset[str],
    generation: int | None = None,
) -> tuple[PrivateChunk, DecodedTitleProviderResult]:
    # No current authority is inferred here. Callers first admit the actual
    # current root and prove the complete historical forward transition.
    principal = conn.execute(select(sessions.c.archived_at).where(sessions.c.id == str(running.claim.session_id))).one()
    if principal.archived_at is not None:
        raise AuditIntegrityError("TITLE source changed its original logical request/actor/eligibility")
    return _settled_source_data(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        sequence=running.claim.segment_sequence,
        generation=running.claim.ownership_generation if generation is None else generation,
        selected=selected,
        names=names,
    )


def _settled_source_data(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    sequence: int | None,
    generation: int | None,
    selected: StoredContinuationSnapshot,
    names: frozenset[str],
) -> tuple[PrivateChunk, DecodedTitleProviderResult]:
    chunk, result, settled = read_title_result_data_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        sequence=sequence,
        generation=generation,
        selected=selected,
        names=names,
    )
    if not settled or decode_continuation_snapshot(selected.payload_json).pending_settlement_ids:
        raise AuditIntegrityError("TITLE application cannot consume unsettled accounting")
    _require_audit_cohort_data(conn, session_id, result.audit_call, result.audit_drafts, settled=True)
    return chunk, result


def _ledger_id(conn: Connection, result: DecodedTitleProviderResult) -> str:
    value = conn.execute(select(physical.c.ledger_entry_id).where(physical.c.attempt_id == result.audit_call.call_id)).scalar_one()
    if type(value) is not str or not value:
        raise AuditIntegrityError("TITLE application lost its actual settled ledger link")
    return value


def _following(selected: StoredContinuationSnapshot) -> str:
    root = decode_continuation_snapshot(selected.payload_json)
    frame = _frame(selected)
    return encode_continuation_snapshot(
        root.model_copy(
            update={
                "frames": (frame.model_copy(update={"ingress_phase": "ready", "provider_effect_key": None, "provider_input_sha256": None}),)
            }
        )
    )


def read_title_application_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, inventory: CurrentTitleApplicationNames
) -> CurrentTitleApplication:
    if type(inventory) is not CurrentTitleApplicationNames or inventory.running is not running:
        raise AuditIntegrityError("TITLE application changed its actual names/current owner")
    inventory.require(conn)
    selected = inventory.selected
    if _frame(selected).ingress_phase == "title_pending":
        chunk, result = _settled_source(conn, running, selected, inventory.names)
        return CurrentTitleApplication(selected, chunk, title_application_decision(result), inventory, False, None)
    chunk, decision, event = read_applied_title_ready_data_on_connection(
        conn, running, selected=selected, names=inventory.names, generation=running.claim.ownership_generation
    )
    return CurrentTitleApplication(selected, chunk, decision, inventory, True, event)


def read_applied_title_ready_data_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    names: frozenset[str],
    generation: int | None,
) -> tuple[PrivateChunk, TitleApplicationDecision, TitleApplicationEvent]:
    """Historical DATA only; the caller separately proves its full current root.

    The original current reader above retains its actual inventory/current gate.
    Initial Compose replay may consume this exact historical ready predecessor
    while the complete physical/effect cohort is still TITLE-only.
    """
    principal = conn.execute(select(sessions.c.archived_at).where(sessions.c.id == str(running.claim.session_id))).one()
    if principal.archived_at is not None:
        raise AuditIntegrityError("TITLE source changed its original logical request/actor/eligibility")
    return read_applied_title_ready_immutable_data_on_connection(
        conn,
        session_id=running.claim.session_id,
        operation_id=running.claim.operation_id,
        sequence=running.claim.segment_sequence,
        generation=generation,
        selected=selected,
        names=names,
    )


def read_applied_title_ready_immutable_data_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    sequence: int | None,
    generation: int | None,
    selected: StoredContinuationSnapshot,
    names: frozenset[str],
) -> tuple[PrivateChunk, TitleApplicationDecision, TitleApplicationEvent]:
    """Actual ready event and paid source; no current business grant is issued."""
    if _frame(selected).ingress_phase != "ready":
        raise AuditIntegrityError("historical TITLE application has no actual ready predecessor")
    origin, generation = pretitle_recovery_origin_data_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        sequence=sequence,
        generation=generation,
        selected=selected,
    )
    sid, operation = str(session_id), operation_id
    row = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == operation, events.c.sequence == origin.cursor)
    ).one_or_none()
    if row is None or row.tag != "checkpoint":
        raise AuditIntegrityError("TITLE ready lost its actual application event")
    try:
        text = read_chunk(conn, session_id=sid, operation_id=operation, key=row.payload_chunk_key).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise AuditIntegrityError("TITLE application event is not strict UTF-8") from error
    value, parse_error = parse_json_strict(text)
    if parse_error is not None or type(value) is not dict:
        raise AuditIntegrityError("TITLE application event is not complete owned JSON")
    try:
        event = TitleApplicationEvent.model_validate_json(text, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("TITLE application event violates its owned shape") from error
    if (
        text != canonical_json(event.model_dump(mode="json"))
        or event.predecessor_cursor + 1 != origin.cursor
        or event.creator_sequence != sequence
        or event.creator_generation != generation
    ):
        raise AuditIntegrityError("TITLE ready changed its complete application creator/event")
    previous = _pretitle_checkpoint_at_cursor(conn, session_id=sid, operation_id=operation, cursor=event.predecessor_cursor)
    if previous.checkpoint_id != event.predecessor_checkpoint_id or _frame(previous).ingress_phase != "title_pending":
        raise AuditIntegrityError("TITLE ready lost its actual settled predecessor")
    expected = decode_continuation_snapshot(_following(previous)).model_copy(update={"cursor": origin.cursor})
    if encode_continuation_snapshot(expected) != origin.payload_json:
        raise AuditIntegrityError("TITLE ready changed its complete atomic forward transition")
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == operation,
            attempts.c.sequence == event.creator_sequence,
            attempts.c.generation == event.creator_generation,
        )
    ).one_or_none()
    if creator is None or (creator.session_operation_id, creator.session_operation_epoch) != (
        event.creator_operation_id,
        event.creator_operation_epoch,
    ):
        raise AuditIntegrityError("TITLE ready lost its original application writer source")
    chunk, result = _settled_source_data(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        sequence=sequence,
        generation=generation,
        selected=previous,
        names=names,
    )
    decision = title_application_decision(result)
    if (
        event.result != chunk.reference
        or event.input_sha256 != result.snapshot.input_sha256
        or event.attempt_id != result.audit_call.call_id
        or event.ledger_entry_id != _ledger_id(conn, result)
        or event.decision != decision
    ):
        raise AuditIntegrityError("TITLE ready differs from its exact original paid result/decision/ledger")
    # No permanent equality with sessions.title: a later legitimate rename is
    # preserved on replay. The original event proves the prior atomic write.
    return chunk, decision, event


@dataclass(slots=True)
class AdmittedTitleApplication:
    connection: Connection
    transaction: object
    context: SessionOperationContext
    request: TitleApplicationRequest
    current: CurrentTitleApplication
    original_title: str
    active: bool = True

    def require(self, conn: Connection, context: SessionOperationContext) -> None:
        if (
            not self.active
            or conn is not self.connection
            or conn.get_transaction() is not self.transaction
            or context is not self.context
            or self.request.running.session_operation_context is not context
        ):
            raise AuditIntegrityError("TITLE application admission escaped its current writer transaction")
        actual = read_title_application_on_connection(conn, self.request.running, inventory=self.request.inventory)
        if actual != self.current:
            raise AuditIntegrityError("TITLE application changed its admitted complete current source")


def admit_title_application_on_connection(
    conn: Connection, service: SessionServiceImpl, context: SessionOperationContext, request: TitleApplicationRequest
) -> AdmittedTitleApplication:
    from elspeth.web.sessions.service import SessionServiceImpl

    if (
        type(service) is not SessionServiceImpl
        or conn.engine is not service._engine
        or type(request) is not TitleApplicationRequest
        or type(request.running) is not ComposerOperationRunning
        or request.running.session_operation_context is not context
        or type(request.inventory) is not CurrentTitleApplicationNames
        or request.inventory.selected != request.selected
    ):
        raise AuditIntegrityError("TITLE application requires its exact current sessions/request writer")
    actual = read_title_application_on_connection(conn, request.running, inventory=request.inventory)
    if actual.result != request.result or actual.decision != request.decision:
        raise AuditIntegrityError("TITLE application changed its full actual result or candidate disposition")
    if not actual.applied:
        root = decode_continuation_snapshot(actual.selected.payload_json)
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        timing = _timing(conn, request.running, actual.selected)
        if (
            admit_action(
                policy.actions.session_sql,
                policy.segment,
                hard_remaining=timing.hard_remaining_seconds,
                cooperative_remaining=timing.cooperative_remaining_seconds,
                fresh=False,
            )
            is not SegmentFit.ADMIT
        ):
            raise AuditIntegrityError("TITLE application must yield before new local publication")
    title = conn.execute(select(sessions.c.title).where(sessions.c.id == str(request.running.claim.session_id))).scalar_one()
    return AdmittedTitleApplication(conn, conn.get_transaction(), context, request, actual, title)


def complete_title_application_on_connection(
    conn: Connection, context: SessionOperationContext, admitted: AdmittedTitleApplication
) -> StoredContinuationSnapshot:
    if type(admitted) is not AdmittedTitleApplication:
        raise AuditIntegrityError("TITLE application requires its actual nominal admitted source")
    admitted.require(conn, context)
    if admitted.current.applied:
        return admitted.current.selected
    running, selected = admitted.request.running, admitted.current.selected
    title = conn.execute(select(sessions.c.title).where(sessions.c.id == str(running.claim.session_id))).scalar_one()
    expected_title = admitted.current.decision.candidate
    if title != (admitted.original_title if expected_title is None else expected_title):
        raise AuditIntegrityError("TITLE ready differs from its actual original title-write/no-change result")
    chunk, result = _settled_source(conn, running, selected, admitted.request.inventory.names)
    sequence, generation = require_title_creator(running)
    call_id = result.audit_call.call_id
    if call_id is None:
        raise AuditIntegrityError("TITLE application lost its original paid call identity")
    event = TitleApplicationEvent(
        predecessor_checkpoint_id=selected.checkpoint_id,
        predecessor_cursor=selected.cursor,
        result=chunk.reference,
        input_sha256=result.snapshot.input_sha256,
        attempt_id=call_id,
        ledger_entry_id=_ledger_id(conn, result),
        creator_sequence=sequence,
        creator_generation=generation,
        creator_operation_id=context.fence.operation_id,
        creator_operation_epoch=context.fence.operation_epoch,
        decision=admitted.current.decision,
    )
    return append_complete_snapshot(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
        snapshot_json=_following(selected),
        event_batch=(ContinuationEvent("checkpoint", canonical_json(event.model_dump(mode="json"))),),
        handoff=False,
    )
