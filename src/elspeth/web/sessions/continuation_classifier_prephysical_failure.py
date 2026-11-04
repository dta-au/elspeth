"""Original classifier preparation failures, with logical ambiguity preserved."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4
from weakref import ReferenceType, WeakKeyDictionary, ref

from pydantic import Field, JsonValue
from sqlalchemy import Connection, select

from elspeth.contracts.composer_audit import PrivateFailureFacts
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.audit_storage import ComposerOperationFailureFacts, private_operation_failure_draft
from elspeth.web.composer.continuation import ContinuationValue
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.private_diagnostic_facts import capture_failure_facts
from elspeth.web.composer.protocol import ComposerConvergenceError
from elspeth.web.continuation_retention_memo import retention_stage_memo_scope
from elspeth.web.coordination.composer_operation_authority import (
    _read,
    _record_from_row,
    _select_failure_on_connection,
    settle_composer_operation_on_connection,
)
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkSource, RequiredWorkSubphase
from elspeth.web.sessions.composer_operation_errors import operation_failure_diagnostic_id, project_composer_operation_error
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRecord, ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    load_snapshot,
    prepare_segment_settlement_on_connection,
    put_chunk,
    read_chunk,
)
from elspeth.web.sessions.continuation_classifier_primary import (
    ClassifierPrephysicalSource,
    ClassifierPrimaryIssuer,
    read_classifier_prephysical_source_on_connection,
)
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import composition_states_table as states
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_live_phases import ContinuationLivePhases
    from elspeth.web.composer.continuation_runtime import RestoredContinuationRuntime
    from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class ClassifierPrephysicalFailure:
    """Opaque original observation. Constructing one issues no authority."""


@dataclass(frozen=True, slots=True)
class _NativeFailure:
    original: ReferenceType[ComposerConvergenceError]
    issuer: ReferenceType[ClassifierPrimaryIssuer]
    running: ComposerOperationRunning
    running_identity: int
    phase_identity: int
    runtime_identity: int
    work_identity: int
    lease_identity: int
    clock_identity: int
    selected_identity: int
    snapshot_json: str
    private_chunks: tuple[tuple[str, str, str], ...]
    facts: PrivateFailureFacts
    clock_policy_json: str
    clock_started: float
    initial_version: int
    state_json: str
    max_turns: int


_NATIVE: WeakKeyDictionary[ClassifierPrephysicalFailure, _NativeFailure] = WeakKeyDictionary()


def observe_classifier_prephysical_failure(
    phases: ContinuationLivePhases,
    runtime: RestoredContinuationRuntime,
    issuer: ClassifierPrimaryIssuer,
    original: ComposerConvergenceError,
) -> ClassifierPrephysicalFailure:
    """Called only inside the actual observer's original exception handler."""
    if sys.exception() is not original or type(original) is not ComposerConvergenceError:
        raise AuditIntegrityError("classifier failure was not observed at its original exception boundary")
    issuer.require_original_prephysical_failure(original)
    running = phases.work.running
    clock = phases.action_clock
    if (
        running is None
        or clock is None
        or phases.runtime is not runtime
        or issuer.runtime is not runtime
        or issuer.selected is not runtime.source.selected
        or issuer.running is not running
        or issuer.work is not phases.work
        or issuer.clock is not clock
        or runtime.materialization.inventory is not issuer.inventory
        or type(runtime.source.policy) is not ContinuationAdmissionPolicyV4
        or phases.classifier_prephysical_failure is not None
    ):
        raise AuditIntegrityError("classifier failure changed its original invocation owners")
    marker = ClassifierPrephysicalFailure()
    selected = runtime.source.selected
    _NATIVE[marker] = _NativeFailure(
        ref(original),
        ref(issuer),
        replace(running, claim=replace(running.claim), session_operation_context=replace(running.session_operation_context)),
        id(running),
        id(phases),
        id(runtime),
        id(phases.work),
        id(phases.lease),
        id(clock),
        id(selected),
        encode_continuation_snapshot(selected.stack),
        tuple((part.reference.codec, part.reference.digest, part.canonical) for part in selected.chunks),
        capture_failure_facts(original),
        canonical_json(clock.policy.model_dump(mode="json")),
        clock.started_monotonic,
        runtime.live.initial_version,
        canonical_json(runtime.live.state.to_dict()),
        original.max_turns,
    )
    phases.classifier_prephysical_failure = marker
    return marker


def original_classifier_prephysical_failure(phases: ContinuationLivePhases, original: BaseException) -> ClassifierPrephysicalFailure | None:
    """Return only the privately retained original; this getter grants no SQL."""
    marker = phases.classifier_prephysical_failure
    saved = _NATIVE.get(marker) if type(marker) is ClassifierPrephysicalFailure else None
    if saved is None or saved.phase_identity != id(phases) or saved.original() is not original:
        return None
    return marker


def _require_native(
    marker: ClassifierPrephysicalFailure, phases: ContinuationLivePhases, running: ComposerOperationRunning, work: RequiredWorkBinding
) -> tuple[_NativeFailure, ComposerConvergenceError]:
    saved = _NATIVE.get(marker) if type(marker) is ClassifierPrephysicalFailure else None
    if saved is None:
        raise AuditIntegrityError("classifier failure requires its original issued custody")
    original, issuer = saved.original(), saved.issuer()
    runtime, clock = phases.runtime, phases.action_clock
    if (
        original is None
        or issuer is None
        or runtime is None
        or clock is None
        or phases.classifier_prephysical_failure is not marker
        or (id(phases), id(running), id(runtime), id(work), id(phases.lease), id(clock), id(runtime.source.selected))
        != (
            saved.phase_identity,
            saved.running_identity,
            saved.runtime_identity,
            saved.work_identity,
            saved.lease_identity,
            saved.clock_identity,
            saved.selected_identity,
        )
        or work is not phases.work
        or running is not work.running
        or running != saved.running
        or issuer.runtime is not runtime
        or issuer.work is not work
        or issuer.clock is not clock
        or phases.lease.context is not running.session_operation_context
        or phases.cancellation_requested.is_set()
        or phases.lease.closed
        or clock.started_monotonic != saved.clock_started
        or canonical_json(clock.policy.model_dump(mode="json")) != saved.clock_policy_json
        or encode_continuation_snapshot(runtime.source.selected.stack) != saved.snapshot_json
        or tuple((part.reference.codec, part.reference.digest, part.canonical) for part in runtime.source.selected.chunks)
        != saved.private_chunks
        or capture_failure_facts(original) != saved.facts
        or original.max_turns != saved.max_turns
        or canonical_json(runtime.live.state.to_dict()) != saved.state_json
    ):
        raise AuditIntegrityError("classifier failure substituted its original source, actor or native outcome")
    issuer.require_original_prephysical_failure(original)
    return saved, original


def _require_joined_body(work: RequiredWorkBinding, original: ComposerConvergenceError) -> None:
    """The phase/watcher and every SQL producer must really have joined."""
    coordinator = work.coordinator
    receipts = coordinator.settlement_failure_receipts()
    allowed = (original, original.__context__)
    for receipt in receipts:
        if any(leaf is not allowed[0] and leaf is not allowed[1] for leaf in receipt.original_category_witnesses):
            raise AuditIntegrityError("classifier convergence cannot replace another required failure")
    for ticket in coordinator.tickets:
        if ticket.key.subphase not in (RequiredWorkSubphase.SQL_INITIAL, RequiredWorkSubphase.SQL_RETRY, RequiredWorkSubphase.SQL_READBACK):
            continue
        if ticket.pending_healthy_renewal:
            continue
        with coordinator._lock, ticket._lock:
            actual = ticket._finish_once_handoff
            if not isinstance(actual, (RequiredSQLRaised, RequiredSQLReturned)) or type(actual) not in (
                RequiredSQLRaised,
                RequiredSQLReturned,
            ):
                raise AuditIntegrityError("classifier failure has an unjoined SQL owner")
            coordinator._registered(ticket, ticket.key.source)
            coordinator._verify_sql_outcome(ticket, actual)
            if actual.deferred_cancellations:
                raise AuditIntegrityError("classifier convergence cannot consume deferred SQL cancellation")


class _Descriptor(ContinuationValue):
    schema_version: Literal["composer.classifier-prephysical-failure.v1"] = "composer.classifier-prephysical-failure.v1"
    session_id: UUID
    operation_id: str
    source_checkpoint_id: str
    source_cursor: int = Field(ge=0)
    source_sha256: str
    effect_key: str
    sequence: int = Field(gt=0)
    generation: int = Field(gt=0)
    claim_attempt: int = Field(gt=0)
    physical_operation_id: str
    physical_operation_epoch: int
    env_ref_names: tuple[str, ...]
    private_facts: str
    max_turns: int = Field(ge=0)
    clock_policy_json: str
    clock_started: float
    state_json: str
    initial_version: int
    outcome: ComposerOperationError
    message_id: UUID
    audit_json: str
    audit_content: str
    terminal_checkpoint_id: str
    terminal_cursor: int
    terminal_snapshot_json: str
    terminal_events: tuple[tuple[str, str], ...]
    published_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedClassifierPrephysicalTerminal:
    terminal: ComposerOperationRecord
    original_source: StoredContinuationSnapshot
    effect_key: str
    private_message_id: UUID


@dataclass(frozen=True, slots=True)
class CommittedClassifierPrephysicalFailure:
    verified: VerifiedClassifierPrephysicalTerminal
    original: ComposerConvergenceError
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()

    @property
    def terminal(self) -> ComposerOperationRecord:
        return self.verified.terminal


def _key(operation_id: str) -> str:
    return "classifier-prephysical-failure:" + operation_id


def _recovery_outcome(
    conn: Connection,
    service: SessionServiceImpl,
    source: ClassifierPrephysicalSource,
    original: ComposerConvergenceError,
    state_json: str,
    initial_version: int,
    request_id: str | None,
) -> ComposerOperationError:
    from elspeth.web.composer.continuation_evidence import state_from_evidence_json
    from elspeth.web.sessions.routes._helpers import _state_from_record, _state_response

    failure = project_composer_operation_error(
        original,
        request_id=request_id,
        diagnostic_id=operation_failure_diagnostic_id(
            session_id=str(source.selected.stack.logical.session_id), operation_id=source.selected.stack.logical.operation_id
        ),
    )
    detail = failure.body.get("detail")
    if failure.error_type != "convergence" or type(detail) is not dict:
        raise AuditIntegrityError("classifier failure has no nominal convergence projection")
    public: dict[str, JsonValue] = dict(detail)
    compose, _batch = applied_batch_cut(source.selected.stack.frames)
    head = conn.execute(
        select(states).where(states.c.session_id == source.selected.stack.logical.session_id).order_by(states.c.version.desc()).limit(1)
    ).one_or_none()
    if compose.current_state_id is not None:
        if head is None or head.id != compose.current_state_id:
            raise AuditIntegrityError("classifier recovery lost its actual durable head")
        record = service._row_to_state_record(head)
        if canonical_json(_state_from_record(record).to_dict()) != state_json:
            raise AuditIntegrityError("classifier recovery state differs from its actual captured graph")
        if record.version > initial_version:
            public["partial_state"] = _state_response(record).model_dump(mode="json")
    elif head is not None or state_from_evidence_json(state_json).version > initial_version:
        raise AuditIntegrityError("classifier recovery claimed an absent durable state")
    failed = original.failed_turn
    if failed is not None:
        assistant = conn.execute(
            select(messages).where(
                messages.c.session_id == source.selected.stack.logical.session_id, messages.c.id == failed.assistant_message_id
            )
        ).one_or_none()
        if assistant is None:
            raise AuditIntegrityError("classifier recovery lost its original assistant")
        if (
            assistant.role != "assistant"
            or assistant.id != compose.persisted_assistant_message_id
            or assistant.content != compose.persisted_assistant_content
        ):
            raise AuditIntegrityError("classifier recovery changed its original assistant identity")
        responses = tuple(
            conn.execute(
                select(messages).where(
                    messages.c.session_id == source.selected.stack.logical.session_id,
                    messages.c.role == "tool",
                    messages.c.parent_message_id == assistant.id,
                )
            )
        )
        if len(assistant.tool_calls or ()) != failed.tool_calls_attempted or (
            failed.tool_responses_persisted is not None and failed.tool_responses_persisted != len(responses)
        ):
            raise AuditIntegrityError("classifier recovery changed its persisted response count")
        public["failed_turn"] = {
            "assistant_message_id": assistant.id,
            "tool_calls_attempted": failed.tool_calls_attempted,
            "tool_responses_persisted": len(responses),
            "transcript_url": None,
        }
    return failure.model_copy(update={"body": {"detail": public}})


def read_classifier_prephysical_terminal_on_connection(
    conn: Connection, *, session_id: UUID, operation_id: str
) -> VerifiedClassifierPrephysicalTerminal | None:
    """Reprove the complete failed bundle; never grant live business entry."""
    row = conn.execute(
        select(chunks).where(
            chunks.c.session_id == str(session_id), chunks.c.operation_id == operation_id, chunks.c.chunk_key == _key(operation_id)
        )
    ).one_or_none()
    if row is None:
        return None
    raw = row.payload.decode("utf-8", errors="strict")
    record = _Descriptor.model_validate_json(raw, strict=True)
    if (
        row.codec != record.schema_version
        or canonical_json(record.model_dump(mode="json")) != raw
        or (record.session_id, record.operation_id) != (session_id, operation_id)
    ):
        raise AuditIntegrityError("classifier failure changed its closed terminal descriptor")
    source = read_classifier_prephysical_source_on_connection(
        conn, session_id=session_id, operation_id=operation_id, checkpoint_id=record.source_checkpoint_id
    )
    selected = source.selected
    source_json = encode_continuation_snapshot(selected.stack)
    if (
        selected.stack.cursor,
        sha256(source_json.encode()).hexdigest(),
        source.effect_key,
        source.creator_sequence,
        source.creator_generation,
        source.claim_attempt,
        source.physical_operation_id,
        source.physical_operation_epoch,
        tuple(sorted(source.env_ref_names)),
    ) != (
        record.source_cursor,
        record.source_sha256,
        record.effect_key,
        record.sequence,
        record.generation,
        record.claim_attempt,
        record.physical_operation_id,
        record.physical_operation_epoch,
        record.env_ref_names,
    ):
        raise AuditIntegrityError("classifier failure substituted its original source or creator")
    parent = _read(conn, session_id, operation_id)
    if parent is None:
        raise AuditIntegrityError("classifier failure lost its failed parent")
    terminal = _record_from_row(parent)
    current = load_snapshot(conn, session_id=str(session_id), operation_id=operation_id)
    if (
        terminal.status != "failed"
        or terminal.result_json != canonical_json(record.outcome.model_dump(mode="json"))
        or terminal.failure_code != record.outcome.failure_code
        or (current.checkpoint_id, current.cursor, current.payload_json)
        != (record.terminal_checkpoint_id, record.terminal_cursor, record.terminal_snapshot_json)
        or (terminal.checkpoint_id, terminal.journal_cursor) != (current.checkpoint_id, current.cursor)
    ):
        raise AuditIntegrityError("classifier failure changed its complete terminal identity")
    before, after = selected.stack, decode_continuation_snapshot(current.payload_json)
    expected_frontier = tuple(
        item.model_copy(update={"disposition": "unknown"}) if item.effect_id == record.effect_key else item
        for item in before.effect_frontier
    )
    if (
        after != before.model_copy(update={"cursor": current.cursor, "effect_frontier": expected_frontier})
        or current.cursor != before.cursor + 2
    ):
        raise AuditIntegrityError("classifier failure changed more than its exact unresolved terminal transition")
    observed_events = []
    for item in conn.execute(
        select(events)
        .where(events.c.session_id == str(session_id), events.c.operation_id == operation_id, events.c.sequence > before.cursor)
        .order_by(events.c.sequence)
    ):
        observed_events.append(
            (item.tag, read_chunk(conn, session_id=str(session_id), operation_id=operation_id, key=item.payload_chunk_key).decode())
        )
    if tuple(observed_events) != record.terminal_events or tuple(tag for tag, _payload in observed_events) != ("effect_unknown", "settled"):
        raise AuditIntegrityError("classifier failure changed its original terminal journal")
    message = conn.execute(
        select(messages).where(messages.c.session_id == str(session_id), messages.c.id == str(record.message_id))
    ).one_or_none()
    if (
        message is None
        or message.role != "audit"
        or message.writer_principal != "compose_loop"
        or message.content != record.audit_content
        or canonical_json(message.tool_calls) != record.audit_json
        or restore_utc(message.created_at) != restore_utc(record.published_at)
    ):
        raise AuditIntegrityError("classifier failure lost its exact private cohort")
    if terminal.settled_at is None or restore_utc(terminal.settled_at) != restore_utc(record.published_at):
        raise AuditIntegrityError("classifier failure changed its committed publication time")
    return VerifiedClassifierPrephysicalTerminal(
        terminal, StoredContinuationSnapshot(source_json, before.cursor, selected.checkpoint_id), source.effect_key, record.message_id
    )


async def fail_classifier_prephysical_operation(
    service: SessionServiceImpl,
    running: ComposerOperationRunning,
    *,
    work: RequiredWorkBinding,
    phases: ContinuationLivePhases,
    custody: ClassifierPrephysicalFailure,
) -> CommittedClassifierPrephysicalFailure:
    """One source-admitted atomic writer; an uncertain result gets readback only."""
    from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

    saved, original = _require_native(custody, phases, running, work)
    _require_joined_body(work, original)
    timing = work.coordinator.sql_deadline_owner
    if timing is None or timing.running is not running or timing.drain_signal.started_at is not None:
        raise AuditIntegrityError("classifier failure lost its original bounded settlement owner")
    timing.begin_settlement()
    candidate: _Descriptor | None = None

    def write() -> VerifiedClassifierPrephysicalTerminal:
        nonlocal candidate
        with locked_session_transaction(service._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
            _require_native(custody, phases, running, work)
            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            runtime = phases.runtime
            if (
                runtime is None
                or current.checkpoint_id != runtime.source.selected.checkpoint_id
                or current.payload_json != saved.snapshot_json
            ):
                raise AuditIntegrityError("classifier failure changed its current full source")
            source = read_classifier_prephysical_source_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, checkpoint_id=current.checkpoint_id
            )
            if (
                source.creator_sequence,
                source.creator_generation,
                source.claim_attempt,
                source.physical_operation_id,
                source.physical_operation_epoch,
            ) != (
                running.claim.segment_sequence,
                running.claim.ownership_generation,
                running.claim.attempt,
                running.session_operation_context.fence.operation_id,
                running.session_operation_context.fence.operation_epoch,
            ):
                raise AuditIntegrityError("classifier failure changed its exact original execution actor")
            parent = _read(conn, running.claim.session_id, running.claim.operation_id)
            if parent is None:
                raise AuditIntegrityError("classifier failure lost its original parent")
            failure = _recovery_outcome(conn, service, source, original, saved.state_json, saved.initial_version, parent.request_id)
            failure = _select_failure_on_connection(parent, failure, authoritative_failure=False)
            segment = conn.execute(
                select(segments).where(
                    segments.c.session_id == str(running.claim.session_id),
                    segments.c.operation_id == running.claim.operation_id,
                    segments.c.sequence == running.claim.segment_sequence,
                )
            ).one()
            history = conn.execute(
                select(attempts).where(
                    attempts.c.session_id == str(running.claim.session_id),
                    attempts.c.operation_id == running.claim.operation_id,
                    attempts.c.sequence == running.claim.segment_sequence,
                    attempts.c.generation == running.claim.ownership_generation,
                )
            ).one()
            prepared = prepare_segment_settlement_on_connection(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                parent=parent,
                segment=segment,
                history=history,
                outcome=failure,
                settled_by="owner_terminal",
                attempt_outcome="settled",
            )
            draft = private_operation_failure_draft(
                ComposerOperationFailureFacts(
                    running.claim.operation_id, parent.request_id, running.session_operation_context, saved.facts
                ),
                failure=failure,
            )
            candidate = _Descriptor(
                session_id=running.claim.session_id,
                operation_id=running.claim.operation_id,
                source_checkpoint_id=current.checkpoint_id,
                source_cursor=current.cursor,
                source_sha256=sha256(current.payload_json.encode()).hexdigest(),
                effect_key=source.effect_key,
                sequence=source.creator_sequence,
                generation=source.creator_generation,
                claim_attempt=source.claim_attempt,
                physical_operation_id=source.physical_operation_id,
                physical_operation_epoch=source.physical_operation_epoch,
                env_ref_names=tuple(sorted(source.env_ref_names)),
                private_facts=saved.facts.canonical,
                max_turns=saved.max_turns,
                clock_policy_json=saved.clock_policy_json,
                clock_started=saved.clock_started,
                state_json=saved.state_json,
                initial_version=saved.initial_version,
                outcome=failure,
                message_id=uuid4(),
                audit_json=canonical_json(draft.tool_calls),
                audit_content=draft.content,
                terminal_checkpoint_id=prepared.append.checkpoint_id,
                terminal_cursor=current.cursor + len(prepared.append.events),
                terminal_snapshot_json=prepared.append.snapshot_json,
                terminal_events=tuple((event.tag, event.payload_json) for event in prepared.append.events),
                published_at=prepared.published_at,
            )
            encoded = canonical_json(candidate.model_dump(mode="json"))
            require_no_credential_material(
                encoded, surface="classifier_prephysical_failure", env_ref_names=source.env_ref_names, max_string_bytes=1_048_576
            )
            put_chunk(
                conn,
                session_id=str(running.claim.session_id),
                operation_id=running.claim.operation_id,
                key=_key(running.claim.operation_id),
                payload=encoded.encode(),
                codec=candidate.schema_version,
            )
            active = service._write_audit_cohort_on_connection(
                conn,
                session_id=running.claim.session_id,
                drafts=(draft,),
                writer_principal="compose_loop",
                composition_state_id=None,
                session_operation_context=running.session_operation_context,
                audit_only=True,
                message_ids=(str(candidate.message_id),),
                created_at=prepared.published_at,
                recorded_at=prepared.published_at,
            )
            if len(active) != 1:
                raise AuditIntegrityError("classifier failure did not publish exactly its private cohort")
            terminal = settle_composer_operation_on_connection(conn, running, outcome=failure, prepared_settlement=prepared)
            verified = read_classifier_prephysical_terminal_on_connection(
                conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id
            )
            if verified is None or verified.terminal != terminal:
                raise AuditIntegrityError("classifier failure writer lost its exact terminal bundle")
        return verified

    sql = work.coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_SQL, sql_attempt_ordinal=0)
    outcome = await run_required_sql_finish_once(sql, write)
    work.coordinator.validate_terminal_failure_sql_outcome(ticket=sql, actual_outcome=outcome, expected_sql_attempt_ordinal=0)
    if type(outcome) is RequiredSQLReturned:
        return CommittedClassifierPrephysicalFailure(outcome.value, original, outcome.deferred_cancellations)
    if type(outcome) is not RequiredSQLRaised:
        raise AuditIntegrityError("classifier failed terminal lost its original SQL handoff")
    if candidate is not None:

        def read() -> VerifiedClassifierPrephysicalTerminal | None:
            with locked_session_transaction(service._engine, str(running.claim.session_id)) as conn, retention_stage_memo_scope():
                return read_classifier_prephysical_terminal_on_connection(
                    conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id
                )

        read_sql, projection = work.coordinator.reserve_pair(
            RequiredWorkSource.TERMINAL_WRITER_READ_SQL,
            RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION,
            transition_ordinal=0,
            semantic_ordinal=0,
        )
        observed = await run_required_sql_finish_once(read_sql, read)
        work.coordinator.validate_joined_lifecycle_sql_outcome(
            ticket=read_sql, expected_source=RequiredWorkSource.TERMINAL_WRITER_READ_SQL, actual_outcome=observed
        )
        if type(observed) is RequiredSQLReturned and observed.value is not None:
            projection.begin_projection()
            projection.complete_owned()
            # A real postcommit SQL failure stays native, even after readback.
            raise BaseExceptionGroup("classifier failed terminal committed with original SQL failure", [original, outcome.error])
        projection.complete_owned(observed.error if type(observed) is RequiredSQLRaised else outcome.error)
    failure = ComposerTerminalSQLCompletionUnknown("classifier failed terminal has no exact committed readback")
    raise failure from BaseExceptionGroup("classifier original and terminal SQL failure", [original, outcome.error])
