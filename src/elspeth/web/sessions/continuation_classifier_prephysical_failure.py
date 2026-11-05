"""Original classifier preparation failures, with logical ambiguity preserved."""

from __future__ import annotations

import asyncio
import math
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import TYPE_CHECKING, Literal, NoReturn
from uuid import UUID, uuid4
from weakref import ReferenceType, WeakKeyDictionary, ref

from pydantic import Field, JsonValue
from sqlalchemy import Connection, select

from elspeth.contracts.composer_audit import PrivateFailureFacts
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.async_workers import run_required_sql_finish_once
from elspeth.web.composer.continuation import ContinuationValue
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_iteration_transitions import applied_batch_cut
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4
from elspeth.web.composer.diagnostic_evidence import build_private_tool_evidence, diagnostic_evidence_identity
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
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_operation_errors import operation_failure_diagnostic_id
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
    sql_clock_identity: int
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
    timing = phases.work.coordinator.sql_deadline_owner
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
        or timing is None
        or timing.running is not running
        or original.failed_turn != runtime.live.failed_turn
    ):
        raise AuditIntegrityError("classifier failure changed its original invocation owners")
    timing.require_action_clock(clock)
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
        id(timing),
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
    timing = work.coordinator.sql_deadline_owner
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
        or phases.lease.closed
        or timing is None
        or id(timing) != saved.sql_clock_identity
        or timing.running is not running
        or clock.started_monotonic != saved.clock_started
        or canonical_json(clock.policy.model_dump(mode="json")) != saved.clock_policy_json
        or encode_continuation_snapshot(runtime.source.selected.stack) != saved.snapshot_json
        or tuple((part.reference.codec, part.reference.digest, part.canonical) for part in runtime.source.selected.chunks)
        != saved.private_chunks
        or capture_failure_facts(original) != saved.facts
        or original.max_turns != saved.max_turns
        or canonical_json(runtime.live.state.to_dict()) != saved.state_json
        or original.failed_turn != runtime.live.failed_turn
        or (None if original.partial_state is None else canonical_json(original.partial_state.to_dict()))
        != (saved.state_json if runtime.live.state.version > saved.initial_version else None)
    ):
        raise AuditIntegrityError("classifier failure substituted its original source, actor or native outcome")
    timing.require_action_clock(clock)
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
            if ticket._submission_unknown:
                raise AuditIntegrityError("classifier failure cannot close an unknown SQL submission")
            if actual is None and ticket._complete and ticket._no_submission and ticket._future is None and not ticket._submission_unknown:
                coordinator._registered(ticket, ticket.key.source)
                # A registered, completed no-submission branch owns no SQL.
                continue
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
    descriptor_json: str


@dataclass(frozen=True, slots=True)
class CommittedClassifierPrephysicalFailure:
    verified: VerifiedClassifierPrephysicalTerminal
    original: ComposerConvergenceError
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()
    original_errors: tuple[BaseException, ...] = ()

    @property
    def terminal(self) -> ComposerOperationRecord:
        return self.verified.terminal


def _key(operation_id: str) -> str:
    return "classifier-prephysical-failure:" + operation_id


def _recovery_outcome(
    conn: Connection,
    source: ClassifierPrephysicalSource,
    *,
    max_turns: int,
    state_json: str,
    initial_version: int,
    request_id: str | None,
    diagnostic_id: str | None,
) -> ComposerOperationError:
    """Reconstruct failed response DATA from the original durable source only."""
    from elspeth.web.composer.continuation_evidence import state_from_evidence_json
    from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_data_on_connection
    from elspeth.web.composer.continuation_iteration import decode_continuation_iteration, iteration_values
    from elspeth.web.composer.continuation_p4_outcome import historical_checkpoint_columns_json, historical_checkpoint_record
    from elspeth.web.sessions.composer_operation_errors import project_convergence_failure_data
    from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_data_on_connection
    from elspeth.web.sessions.routes._helpers import _state_from_record, _state_response
    from elspeth.web.sessions.state_decoder import state_record_from_row

    compose, _batch = applied_batch_cut(source.selected.stack.frames)
    state_chunk = next(part for part in source.selected.chunks if part.reference == compose.state)
    expected_state = state_from_evidence_json(state_chunk.canonical)
    if initial_version != compose.initial_version or canonical_json(expected_state.to_dict()) != state_json:
        raise AuditIntegrityError("classifier failure changed its original captured recovery state")
    if max_turns != compose.composition_slice + compose.discovery_slice + 1:
        raise AuditIntegrityError("classifier failure changed its original charged turn count")
    failure = project_convergence_failure_data(
        max_turns=max_turns, budget_exhausted="timeout", request_id=request_id, diagnostic_id=diagnostic_id
    )
    detail = failure.body.get("detail")
    if failure.error_type != "convergence" or type(detail) is not dict:
        raise AuditIntegrityError("classifier failure has no nominal convergence projection")
    public: dict[str, JsonValue] = dict(detail)
    if compose.current_state_id is not None:
        row = conn.execute(
            select(states).where(states.c.session_id == source.selected.stack.logical.session_id, states.c.id == compose.current_state_id)
        ).one_or_none()
        if row is None:
            raise AuditIntegrityError("classifier recovery lost its original durable state")
        record = state_record_from_row(row)
        admitted = read_selected_turn_evidence_graph_data_on_connection(
            conn,
            session_id=record.session_id,
            operation_id=source.selected.stack.logical.operation_id,
            selected=source.selected,
            names=source.env_ref_names,
        )
        applications = require_p4_application_journal_data_on_connection(
            conn,
            session_id=record.session_id,
            operation_id=source.selected.stack.logical.operation_id,
            selected=source.selected,
            evidence=admitted,
            env_ref_names=source.env_ref_names,
        )
        checkpoints = tuple(
            result.outcome.checkpoint
            for result in applications
            if result.outcome.checkpoint is not None and result.outcome.checkpoint.id == record.id
        )
        if len(checkpoints) != 1:
            raise AuditIntegrityError("classifier recovery lost its authenticated P4 checkpoint")
        checkpoint = checkpoints[0]
        if (
            record != historical_checkpoint_record(checkpoint, env_ref_names=source.env_ref_names)
            or historical_checkpoint_columns_json(record) != checkpoint.original_columns_json
        ):
            raise AuditIntegrityError("classifier recovery changed its complete original P4 state")
        # P4 assigns a persisted checkpoint sequence independently of the
        # local mutation revision. Prove both before comparing the complete
        # graph at the captured revision; never rewrite the persisted row.
        captured_revision = replace(_state_from_record(record), version=expected_state.version)
        if canonical_json(captured_revision.to_dict()) != state_json:
            raise AuditIntegrityError("classifier recovery state differs from its actual captured graph")
        if expected_state.version > initial_version:
            public["partial_state"] = _state_response(record).model_dump(mode="json")
    elif expected_state.version > initial_version:
        raise AuditIntegrityError("classifier recovery claimed an absent durable state")
    iteration = next(part for part in source.selected.chunks if part.reference == compose.iteration)
    failed = iteration_values(
        decode_continuation_iteration(iteration, env_ref_names=source.env_ref_names), env_ref_names=source.env_ref_names
    ).failed_turn
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
                    messages.c.parent_assistant_id == assistant.id,
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


def _failure_draft(record: _Descriptor, *, request_id: str | None) -> AuditMessageDraft:
    """Bind original facts and invocation DATA in the existing private envelope."""
    correlation = "operation:" + record.operation_id
    evidence = build_private_tool_evidence(
        session_id=str(record.session_id),
        operation_id=record.physical_operation_id,
        operation_epoch=record.physical_operation_epoch,
        evidence_id=diagnostic_evidence_identity("operation-failure", correlation),
        tool_call_id=correlation,
        tool_name="composer_failure",
        status="operation_failure",
        arguments_canonical=canonical_json(
            {
                "durable_operation_id": record.operation_id,
                "request_id": request_id,
                "diagnostic_id": record.outcome.diagnostic_id,
                "failure_code": record.outcome.failure_code,
                "correlation_kind": "internal_operation",
                "classifier_prephysical": {
                    "source_checkpoint_id": record.source_checkpoint_id,
                    "source_sha256": record.source_sha256,
                    "effect_key": record.effect_key,
                    "max_turns": record.max_turns,
                    "clock_policy_json": record.clock_policy_json,
                    "clock_started": record.clock_started,
                    "state_sha256": sha256(record.state_json.encode()).hexdigest(),
                    "initial_version": record.initial_version,
                },
            }
        ),
        result_canonical=PrivateFailureFacts(record.private_facts).canonical,
    )
    return AuditMessageDraft(
        role="audit",
        content="Private credential-filtered operation failure facts",
        tool_calls=(evidence.to_envelope(),),
        inherit_composition_state=False,
    )


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
    raw = read_chunk(conn, session_id=str(session_id), operation_id=operation_id, key=_key(operation_id)).decode("utf-8", errors="strict")
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
    from elspeth.web.composer.continuation_policy import decode_continuation_policy

    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    if (
        type(policy) is not ContinuationAdmissionPolicyV4
        or record.clock_policy_json != canonical_json(policy.actions.primary.model_dump(mode="json"))
        or not math.isfinite(record.clock_started)
    ):
        raise AuditIntegrityError("classifier failure changed its original action policy")
    projected = _recovery_outcome(
        conn,
        source,
        max_turns=record.max_turns,
        state_json=record.state_json,
        initial_version=record.initial_version,
        request_id=parent.request_id,
        diagnostic_id=operation_failure_diagnostic_id(session_id=str(session_id), operation_id=operation_id),
    )
    if _select_failure_on_connection(parent, projected, authoritative_failure=False) != record.outcome:
        raise AuditIntegrityError("classifier failure changed its original semantic projection")
    draft = _failure_draft(record, request_id=parent.request_id)
    if record.audit_content != draft.content or record.audit_json != canonical_json(draft.tool_calls):
        raise AuditIntegrityError("classifier failure changed its original private facts or observation")
    current = load_snapshot(conn, session_id=str(session_id), operation_id=operation_id)
    if (
        terminal.status != "failed"
        or terminal.result_json != canonical_json(record.outcome.model_dump(mode="json"))
        or terminal.failure_code != record.outcome.failure_code
        or parent.result_sha256 != sha256(canonical_json(record.outcome.model_dump(mode="json")).encode()).hexdigest()
        or (current.checkpoint_id, current.cursor, current.payload_json)
        != (record.terminal_checkpoint_id, record.terminal_cursor, record.terminal_snapshot_json)
        or (terminal.checkpoint_id, terminal.journal_cursor) != (current.checkpoint_id, current.cursor)
        or (
            terminal.current_segment_sequence,
            terminal.ownership_generation,
            terminal.attempt,
            terminal.session_operation_id,
            terminal.session_operation_epoch,
        )
        != (record.sequence, record.generation, record.claim_attempt, record.physical_operation_id, record.physical_operation_epoch)
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
    expected_events = (
        ("effect_unknown", canonical_json({"version": 1, "effect_key": record.effect_key})),
        (
            "settled",
            canonical_json(
                {
                    "version": 1,
                    "sequence": record.sequence,
                    "generation": record.generation,
                    "status": "failed",
                    "failure_code": record.outcome.failure_code,
                }
            ),
        ),
    )
    if tuple(observed_events) != record.terminal_events or record.terminal_events != expected_events:
        raise AuditIntegrityError("classifier failure changed its original terminal journal")
    message = conn.execute(
        select(messages).where(messages.c.session_id == str(session_id), messages.c.id == str(record.message_id))
    ).one_or_none()
    if (
        message is None
        or message.role != "audit"
        or message.writer_principal != "compose_loop"
        or message.content != record.audit_content
        or message.tool_call_id is not None
        or message.parent_assistant_id is not None
        or message.composition_state_id is not None
        or canonical_json(message.tool_calls) != record.audit_json
        or restore_utc(message.created_at) != restore_utc(record.published_at)
    ):
        raise AuditIntegrityError("classifier failure lost its exact private cohort")
    if terminal.settled_at is None or restore_utc(terminal.settled_at) != restore_utc(record.published_at):
        raise AuditIntegrityError("classifier failure changed its committed publication time")
    return VerifiedClassifierPrephysicalTerminal(
        terminal, StoredContinuationSnapshot(source_json, before.cursor, selected.checkpoint_id), source.effect_key, record.message_id, raw
    )


def _require_candidate(verified: VerifiedClassifierPrephysicalTerminal, candidate: _Descriptor) -> None:
    if type(verified) is not VerifiedClassifierPrephysicalTerminal or verified.descriptor_json != canonical_json(
        candidate.model_dump(mode="json")
    ):
        raise AuditIntegrityError("classifier failure readback belongs to another prepared terminal candidate")


def _originals(*errors: BaseException) -> tuple[BaseException, ...]:
    result: list[BaseException] = []

    def append(error: BaseException) -> None:
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                append(child)
        elif all(error is not previous for previous in result):
            result.append(error)

    for error in errors:
        append(error)
    return tuple(result)


def _raise_originals(*errors: BaseException) -> NoReturn:
    originals = _originals(*errors)
    if len(originals) == 1:
        raise originals[0]
    raise BaseExceptionGroup("classifier original required outcomes", list(originals))


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
    if timing is None or timing.running is not running:
        raise AuditIntegrityError("classifier failure lost its original bounded settlement owner")
    if timing.drain_signal.started_at is not None:
        from elspeth.web.sessions.composer_operations import COMPOSER_SHUTDOWN

        raise asyncio.CancelledError(COMPOSER_SHUTDOWN)
    timing.begin_settlement()
    candidate: _Descriptor | None = None

    def write() -> VerifiedClassifierPrephysicalTerminal:
        nonlocal candidate
        with (
            service._session_process_locked_begin(str(running.claim.session_id)) as conn,
            service._session_write_lock(conn, str(running.claim.session_id)),
            retention_stage_memo_scope(),
        ):
            _require_native(custody, phases, running, work)
            if timing.drain_signal.started_at is not None:
                from elspeth.web.sessions.composer_operations import COMPOSER_SHUTDOWN

                raise asyncio.CancelledError(COMPOSER_SHUTDOWN)
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
            if phases.cancellation_requested.is_set() and parent.cancel_requested_at is None:
                raise AuditIntegrityError("classifier failure cannot consume an undelivered cancellation")
            compose, _batch = applied_batch_cut(source.selected.stack.frames)
            head_id = conn.execute(
                select(states.c.id).where(states.c.session_id == str(running.claim.session_id)).order_by(states.c.version.desc()).limit(1)
            ).scalar_one_or_none()
            if head_id != compose.current_state_id:
                raise AuditIntegrityError("classifier failure changed its actual durable recovery head")
            failure = _recovery_outcome(
                conn,
                source,
                max_turns=saved.max_turns,
                state_json=saved.state_json,
                initial_version=saved.initial_version,
                request_id=parent.request_id,
                diagnostic_id=operation_failure_diagnostic_id(
                    session_id=str(running.claim.session_id), operation_id=running.claim.operation_id
                ),
            )
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
                audit_json="",
                audit_content="",
                terminal_checkpoint_id=prepared.append.checkpoint_id,
                terminal_cursor=current.cursor + len(prepared.append.events),
                terminal_snapshot_json=prepared.append.snapshot_json,
                terminal_events=tuple((event.tag, event.payload_json) for event in prepared.append.events),
                published_at=prepared.published_at,
            )
            draft = _failure_draft(candidate, request_id=parent.request_id)
            candidate = candidate.model_copy(update={"audit_json": canonical_json(draft.tool_calls), "audit_content": draft.content})
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
            _require_candidate(verified, candidate)
        return verified

    sql = work.coordinator.reserve(RequiredWorkSource.TERMINAL_FAILURE_SQL, sql_attempt_ordinal=0)
    try:
        outcome = await run_required_sql_finish_once(sql, write)
        work.coordinator.validate_terminal_failure_sql_outcome(ticket=sql, actual_outcome=outcome, expected_sql_attempt_ordinal=0)
    except BaseException as failure:
        _raise_originals(original, failure)
    if type(outcome) is RequiredSQLReturned:
        if candidate is None:
            raise AuditIntegrityError("classifier failure returned without its original prepared candidate")
        _require_candidate(outcome.value, candidate)
        return CommittedClassifierPrephysicalFailure(outcome.value, original, outcome.deferred_cancellations)
    if type(outcome) is not RequiredSQLRaised:
        raise AuditIntegrityError("classifier failed terminal lost its original SQL handoff")
    errors: list[BaseException] = [outcome.error]
    cancellations = list(outcome.deferred_cancellations)
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
        try:
            observed = await run_required_sql_finish_once(read_sql, read)
            cancellations.extend(item for item in observed.deferred_cancellations if all(item is not earlier for earlier in cancellations))
            work.coordinator.validate_joined_lifecycle_sql_outcome(
                ticket=read_sql, expected_source=RequiredWorkSource.TERMINAL_WRITER_READ_SQL, actual_outcome=observed
            )
            if type(observed) is RequiredSQLRaised:
                errors.append(observed.error)
                projection.complete_owned(observed.error)
            elif type(observed) is RequiredSQLReturned:
                projection.begin_projection()
                if observed.value is not None:
                    _require_candidate(observed.value, candidate)
                    projection.complete_owned()
                    return CommittedClassifierPrephysicalFailure(observed.value, original, tuple(cancellations), _originals(*errors))
                projection.complete_owned()
            else:
                raise AuditIntegrityError("classifier failure readback lost its original SQL carrier")
        except BaseException as error:
            errors.append(error)
            if not projection.complete:
                try:
                    projection.complete_owned(error)
                except BaseException as cleanup:
                    errors.append(cleanup)
    if candidate is not None:
        errors.append(ComposerTerminalSQLCompletionUnknown("classifier failed terminal has no exact committed readback"))
    _raise_originals(original, *errors, *cancellations)
