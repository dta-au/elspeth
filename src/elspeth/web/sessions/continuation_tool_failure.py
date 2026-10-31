"""Current failed-tool DATA and exact terminal observations on canonical SQL owners."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, get_args

from sqlalchemy import Connection, select

from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.prompt_guard import PromptGuardMode, PromptGuardReason
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_batch_data import RestoredBatchData
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_continuation_snapshot
from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
from elspeth.web.composer.continuation_next_action import validate_continuation_structure_on_connection
from elspeth.web.composer.continuation_phase_transitions import (
    ToolIssueSource,
    read_tool_issue_source_on_connection,
    require_completed_tool_prefix_on_connection,
    require_same_sequence_issue_history_on_connection,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_batch import RetainedBatchFailure
from elspeth.web.composer.continuation_tool_failure import require_retained_failure_links
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.composer_operations import ComposerOperationError, ComposerOperationRecord, ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    PreparedSegmentSettlement,
    SettlementDrivingState,
    _require_current_continuation_running,
    _settlement_driving,
    load_snapshot,
    read_chunk,
)
from elspeth.web.sessions.models import chat_messages_table as messages
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_events_table as events
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_tool_failure import ContinuationToolFailureCustody
    from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce
    from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkTicket
    from elspeth.web.sessions.continuation_p4_application_store import StoredTerminalP4Application, StoredTerminalP4Preparation
    from elspeth.web.sessions.service import SessionServiceImpl


@dataclass(frozen=True, slots=True)
class RetainedToolFailureSource:
    selected: SelectedContinuationState
    data: RestoredBatchData
    evidence: PrivateChunk
    response: PrivateChunk
    invocation: ComposerToolInvocation
    issue: ToolIssueSource
    applied: bool
    audit_unavailable: bool

    @property
    def failure(self) -> RetainedBatchFailure:
        failure = self.data.batch.retained_failure
        if failure is None or failure.kind not in ("plugin_crash", "advisor_failure"):
            raise AuditIntegrityError("retained terminal source lost its actual primary failure")
        return failure

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            self.selected.checkpoint_id,
            self.evidence.reference,
            self.response.reference,
            self.issue.model_dump_json(),
            self.applied,
            self.audit_unavailable,
        )


@dataclass(frozen=True, slots=True)
class ContinuationToolFailureRead:
    source: RetainedToolFailureSource | StoredTerminalP4Preparation | StoredTerminalP4Application | None


@dataclass(frozen=True, slots=True)
class _CurrentFailureSourceSelection:
    source: RetainedToolFailureSource | None
    selected: SelectedContinuationState
    unresolved: tuple[str, ...]


def _read_tool_failure_selection_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, env_ref_names: frozenset[str]
) -> _CurrentFailureSourceSelection:
    """Fresh locked complete source proof; returned DATA cannot grant a write."""
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    from elspeth.web.sessions.continuation_selection import load_selected_private_chunks_on_connection

    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    sid, oid = str(running.claim.session_id), running.claim.operation_id
    stored = load_snapshot(conn, session_id=sid, operation_id=oid)
    selected = SelectedContinuationState(
        stored.checkpoint_id,
        decode_continuation_snapshot(stored.payload_json),
        load_selected_private_chunks_on_connection(conn, running, stored),
    )
    unresolved = validate_continuation_structure_on_connection(conn, running, selected, env_ref_names=env_ref_names)
    compose = tuple(item for item in selected.stack.frames if type(item) is ComposeFrame)
    if not compose:
        return _CurrentFailureSourceSelection(None, selected, unresolved)
    if len(compose) != 1:
        raise AuditIntegrityError("failed-tool source lost its unique Compose frame")
    admitted = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=env_ref_names)
    graph = admitted.graph
    if graph is None:
        return _CurrentFailureSourceSelection(None, selected, unresolved)
    data = graph.selected
    require_retained_failure_links(data, graph.recorder.invocations)
    failure = data.batch.retained_failure
    if failure is None or failure.kind == "advisor_timeout":
        return _CurrentFailureSourceSelection(None, selected, unresolved)
    matches = tuple(item for item in graph.recorder.invocations if item.tool_call_id == data.batch.tool_outcomes[-1].call.id)
    if len(matches) != 1:
        raise AuditIntegrityError("failed-tool source lost its unique actual recorded invocation")
    top = selected.stack.frames[-1]
    applied = data.batch.phase == "complete"
    unavailable = False
    if applied:
        from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_on_connection

        require_p4_application_journal_on_connection(conn, running, selected=selected, evidence=admitted, env_ref_names=env_ref_names)
        entry = admitted.snapshot.application_entry
        if entry is None:
            raise AuditIntegrityError("failed P4 completion lost its actual complete application")
        record = entry.applications[-1]
        response = admitted.sources.chunk(record.response)
        # The original owner result is already part of the closed application
        # admission. Read its existing typed receipt rather than inventing a
        # diagnostic from an uncommitted warm exception.
        from elspeth.web.sessions.continuation_p4 import decode_p4_receipt

        result, _outcome = decode_p4_receipt(admitted.sources.chunk(record.result).canonical, env_ref_names=env_ref_names)
        unavailable = result.unwind_audit_failed
    else:
        if type(top) is not ToolBatchFrame or data.batch.phase not in ("call_ready", "dispatch_ready"):
            from elspeth.web.sessions.service import ComposerTerminalSQLCompletionUnknown

            raise ComposerTerminalSQLCompletionUnknown("failed tool has an unresolved actual P4 physical owner")
        require_completed_tool_prefix_on_connection(conn, running, selected=selected, env_ref_names=env_ref_names)
        response = next(item for item in selected.chunks if item.reference == top.response)
    index = data.batch.next_tool_index - 1
    issue = read_tool_issue_source_on_connection(
        conn, running, effect_key=f"tool:{response.reference.digest}:{index}", env_ref_names=env_ref_names
    )
    require_same_sequence_issue_history_on_connection(conn, running, selected=selected, issue=issue)
    if issue.tool_call_id != matches[0].tool_call_id or issue.response != response.reference or issue.tool_index != index:
        raise AuditIntegrityError("failed-tool source changed its actual paid call/issue correspondence")
    refusal = failure.refusal_source
    if refusal is not None and refusal.provider_call_id is not None:
        calls = tuple(call for call in graph.recorder.llm_calls if call.call_id == refusal.provider_call_id)
        if len(calls) != 1:
            raise AuditIntegrityError("failed Advisor policy lost its actual original cost audit correlation")
    if refusal is not None and refusal.kind == "prompt_guard_refused":
        payload = refusal.outcome.response
        if not isinstance(payload, Mapping) or type(payload) not in (dict, MappingProxyType):
            raise AuditIntegrityError("failed Advisor guard lost its original refusal payload")
        guards = conn.execute(
            select(messages).where(
                messages.c.session_id == sid, messages.c.tool_calls[0]["check"]["audit_id"].as_string() == payload["audit_id"]
            )
        ).all()
        if len(guards) != 1:
            raise AuditIntegrityError("failed Advisor guard lost its actual durable audit")
        row = guards[0]
        envelopes = row.tool_calls
        if row.role != "audit" or row.writer_principal != "compose_loop" or type(envelopes) is not list or len(envelopes) != 1:
            raise AuditIntegrityError("failed Advisor guard changed its actual protected audit row")
        envelope = envelopes[0]
        if type(envelope) is not dict or set(envelope) != {"_kind", "check"} or envelope["_kind"] != "prompt_guard_audit":
            raise AuditIntegrityError("failed Advisor guard changed its complete original envelope")
        retained, error = parse_json_strict(row.content)
        if error is not None or retained != envelope:
            raise AuditIntegrityError("failed Advisor guard row lost its exact original content/envelope")
        check = envelope["check"]
        if type(check) is not dict or set(check) != {
            "schema_version",
            "audit_id",
            "operation_id",
            "operation_epoch",
            "role",
            "adapter",
            "mode",
            "policy_fingerprint",
            "request_hash",
            "disposition",
            "reason",
            "started_at",
            "duration_ms",
        }:
            raise AuditIntegrityError("failed Advisor guard lost its complete original check fields")
        if (check["operation_id"], check["operation_epoch"], check["role"]) != (
            issue.creator_operation_id,
            issue.creator_operation_epoch,
            "advisor",
        ):
            raise AuditIntegrityError("failed Advisor guard changed its actual request/creator")
        if (
            type(check["schema_version"]) is not int
            or check["schema_version"] != 1
            or any(
                type(check[field]) is not str or not check[field]
                for field in (
                    "audit_id",
                    "operation_id",
                    "role",
                    "adapter",
                    "mode",
                    "policy_fingerprint",
                    "request_hash",
                    "disposition",
                    "reason",
                    "started_at",
                )
            )
            or type(check["operation_epoch"]) is not int
            or check["operation_epoch"] < 1
            or check["mode"] not in get_args(PromptGuardMode)
            or check["reason"] not in get_args(PromptGuardReason)
            or not is_lower_sha256_hex(check["request_hash"])
            or check["audit_id"] != payload["audit_id"]
            or type(check["duration_ms"]) is not int
            or check["duration_ms"] < 0
            or (
                payload["error_type"] == "prompt_guard_unsupported_input"
                and (check["disposition"], check["reason"]) != ("unavailable", "unsupported_input")
            )
            or (payload["error_type"] == "prompt_guard_blocked" and check["disposition"] != "blocked")
            or (
                payload["error_type"] == "prompt_guard_unavailable"
                and (check["disposition"] != "unavailable" or check["reason"] == "unsupported_input")
            )
        ):
            raise AuditIntegrityError("failed Advisor guard refusal differs from its actual decision/audit facts")
        try:
            observed = datetime.fromisoformat(check["started_at"])
        except ValueError as error:
            raise AuditIntegrityError("failed Advisor guard audit lost its actual timestamp") from error
        if observed.tzinfo is None:
            raise AuditIntegrityError("failed Advisor guard audit lost its original timestamp timezone")
    return _CurrentFailureSourceSelection(
        RetainedToolFailureSource(selected, data, admitted.source_chunk, response, matches[0], issue, applied, unavailable),
        selected,
        unresolved,
    )


def read_tool_failure_source_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, env_ref_names: frozenset[str]
) -> RetainedToolFailureSource | StoredTerminalP4Preparation | StoredTerminalP4Application | None:
    from elspeth.web.sessions.continuation_p4 import read_terminal_p4_source_on_connection

    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    terminal = read_terminal_p4_source_on_connection(
        conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id, env_ref_names=env_ref_names
    )
    if terminal is not None:
        return terminal
    return _read_tool_failure_selection_on_connection(conn, running, env_ref_names=env_ref_names).source


@dataclass(slots=True)
class ContinuationFailureObservation:
    running: ComposerOperationRunning
    source: RetainedToolFailureSource | None
    selected: SelectedContinuationState
    prepared: PreparedSegmentSettlement
    draft: AuditMessageDraft
    message_id: str
    original_errors: tuple[BaseException, ...]
    env_ref_names: frozenset[str]
    candidate: ComposerOperationRecord | None = None
    committed: bool = False
    physical: list[RequiredSQLFinishOnce[ComposerOperationRecord]] = field(default_factory=list)
    deferred_cancellations: list[asyncio.CancelledError] = field(default_factory=list)
    prior: ContinuationFailureObservation | None = None
    private_sequence: int | None = None
    readback: FailureReadback | None = None


@dataclass(frozen=True, slots=True)
class FailureBundleCommitted:
    terminal: ComposerOperationRecord


@dataclass(frozen=True, slots=True)
class FailureBundleNotCommitted:
    pass


@dataclass(frozen=True, slots=True)
class FailureDrivingStateChanged:
    cancel_requested_at: datetime | None
    driving: SettlementDrivingState


@dataclass(frozen=True, slots=True)
class FailureSettledElsewhere:
    terminal: ComposerOperationRecord


type FailureReadback = FailureBundleCommitted | FailureBundleNotCommitted | FailureDrivingStateChanged | FailureSettledElsewhere


@dataclass(frozen=True, slots=True)
class CommittedContinuationFailure:
    terminal: ComposerOperationRecord
    observation: ContinuationFailureObservation


def read_failure_bundle_on_connection(conn: Connection, observation: ContinuationFailureObservation) -> FailureReadback:
    from elspeth.web.coordination.composer_operation_authority import _record_from_row, _terminal_values

    running, candidate = observation.running, observation.prepared
    candidate.append.require_original()
    if candidate.outcome.model_dump_json() != candidate.outcome_json:
        raise AuditIntegrityError("failed terminal readback changed its original selected outcome DATA")
    sid, oid = str(running.claim.session_id), running.claim.operation_id
    row = conn.execute(select(jobs).where(jobs.c.session_id == sid, jobs.c.operation_id == oid)).one()
    parent = _record_from_row(row)
    point = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid,
            checkpoints.c.operation_id == oid,
            checkpoints.c.checkpoint_id == candidate.append.checkpoint_id,
        )
    ).one_or_none()
    private = conn.execute(select(messages).where(messages.c.session_id == sid, messages.c.id == observation.message_id)).one_or_none()
    if point is None and private is None:
        orphan_snapshot = conn.execute(
            select(chunks.c.chunk_key).where(
                chunks.c.session_id == sid,
                chunks.c.operation_id == oid,
                chunks.c.chunk_key == f"snapshot:{candidate.append.checkpoint_id}",
            )
        ).first()
        actual_events = conn.execute(
            select(events).where(
                events.c.session_id == sid,
                events.c.operation_id == oid,
                events.c.sequence > candidate.append.expected_cursor,
                events.c.sequence <= candidate.append.expected_cursor + len(candidate.append.events),
            )
        ).all()
        own_events = any(
            event.tag == item.tag
            and restore_utc(event.created_at) == restore_utc(candidate.published_at)
            and read_chunk(conn, session_id=sid, operation_id=oid, key=event.payload_chunk_key) == item.payload_json.encode("utf-8")
            for event in actual_events
            for item in (candidate.append.events[event.sequence - candidate.append.expected_cursor - 1],)
        )
        if orphan_snapshot is not None or own_events:
            raise AuditIntegrityError("failed terminal readback found an incomplete actual append")
    if point is not None or private is not None:
        if point is None or private is None or parent.status != "failed":
            raise AuditIntegrityError("failed terminal readback found a partial private/terminal/checkpoint bundle")
        segment = conn.execute(
            select(segments).where(
                segments.c.session_id == sid, segments.c.operation_id == oid, segments.c.sequence == running.claim.segment_sequence
            )
        ).one_or_none()
        fence = running.session_operation_context.fence
        if (
            segment is None
            or segment.state != "settled"
            or segment.generation != running.claim.ownership_generation
            or any(value is not None for value in (segment.claim_token, segment.claim_owner_instance_id, segment.claim_expires_at))
            or (segment.session_operation_id, segment.session_operation_epoch, segment.session_operation_lease_token)
            != (fence.operation_id, fence.operation_epoch, fence.lease_token)
            or any(
                actual is None or restore_utc(actual) != expected
                for actual, expected in (
                    (segment.started_at, candidate.driving.segment_started_at),
                    (segment.cooperative_at, candidate.driving.segment_cooperative_at),
                    (segment.deadline_at, candidate.driving.segment_deadline_at),
                )
            )
        ):
            raise AuditIntegrityError("failed terminal readback lost its canonical settled segment/claim closure")
        predecessor = conn.execute(
            select(checkpoints).where(
                checkpoints.c.session_id == sid,
                checkpoints.c.operation_id == oid,
                checkpoints.c.checkpoint_id == candidate.append.expected_checkpoint_id,
            )
        ).one_or_none()
        if (
            predecessor is None
            or predecessor.cursor != candidate.append.expected_cursor
            or predecessor.snapshot_chunk_key != f"snapshot:{candidate.append.expected_checkpoint_id}"
            or read_chunk(conn, session_id=sid, operation_id=oid, key=predecessor.snapshot_chunk_key)
            != candidate.append.predecessor_json.encode("utf-8")
            or parent.attempt != running.claim.attempt
            or parent.current_segment_sequence != running.claim.segment_sequence
            or parent.ownership_generation != running.claim.ownership_generation
            or parent.checkpoint_id != candidate.append.checkpoint_id
            or parent.journal_cursor != point.cursor
            or point.kind != "transition"
            or point.snapshot_chunk_key != f"snapshot:{candidate.append.checkpoint_id}"
            or point.frame_stack_version != 1
        ):
            raise AuditIntegrityError("failed terminal readback lost its actual predecessor/current terminal relation")
        predecessor_codec = conn.execute(
            select(chunks.c.codec).where(
                chunks.c.session_id == sid, chunks.c.operation_id == oid, chunks.c.chunk_key == predecessor.snapshot_chunk_key
            )
        ).scalar_one()
        if predecessor_codec != "snapshot.v2":
            raise AuditIntegrityError("failed terminal predecessor lost its canonical codec")
        snapshot_codec = conn.execute(
            select(chunks.c.codec).where(
                chunks.c.session_id == sid, chunks.c.operation_id == oid, chunks.c.chunk_key == point.snapshot_chunk_key
            )
        ).scalar_one()
        if snapshot_codec != "snapshot.v2":
            raise AuditIntegrityError("failed terminal snapshot lost its canonical codec")
        if (
            point.cursor != candidate.append.expected_cursor + len(candidate.append.events)
            or restore_utc(point.created_at) != restore_utc(candidate.published_at)
            or read_chunk(conn, session_id=sid, operation_id=oid, key=point.snapshot_chunk_key)
            != candidate.append.snapshot_json.encode("utf-8")
            or private.role != observation.draft.role
            or private.content != observation.draft.content
            or canonical_json(private.tool_calls) != canonical_json(observation.draft.tool_calls)
            or restore_utc(private.created_at) != restore_utc(candidate.published_at)
            or private.writer_principal != "compose_loop"
            or private.composition_state_id is not None
        ):
            raise AuditIntegrityError("failed terminal readback changed its complete original retained bundle")
        if observation.private_sequence is not None and private.sequence_no != observation.private_sequence:
            raise AuditIntegrityError("failed terminal readback changed its actual reserved private sequence")
        observation.private_sequence = private.sequence_no
        terminal = _terminal_values(candidate.outcome, now=candidate.published_at, settled_by=candidate.settled_by)
        for key, expected in terminal.items():
            actual = row._mapping[key]
            if isinstance(expected, datetime):
                if actual is None or restore_utc(actual) != restore_utc(expected):
                    raise AuditIntegrityError("failed terminal readback changed its frozen terminal times")
            elif actual != expected:
                raise AuditIntegrityError("failed terminal readback changed its actual selected terminal")
        for sequence, item in enumerate(candidate.append.events, candidate.append.expected_cursor + 1):
            event = conn.execute(
                select(events).where(events.c.session_id == sid, events.c.operation_id == oid, events.c.sequence == sequence)
            ).one()
            if (
                event.tag != item.tag
                or event.payload_chunk_key != f"event:{sequence}"
                or restore_utc(event.created_at) != restore_utc(candidate.published_at)
                or read_chunk(conn, session_id=sid, operation_id=oid, key=event.payload_chunk_key) != item.payload_json.encode("utf-8")
            ):
                raise AuditIntegrityError("failed terminal readback changed its actual frozen append event")
            codec = conn.execute(
                select(chunks.c.codec).where(
                    chunks.c.session_id == sid, chunks.c.operation_id == oid, chunks.c.chunk_key == event.payload_chunk_key
                )
            ).scalar_one()
            if codec != "event.v2":
                raise AuditIntegrityError("failed terminal event lost its canonical codec")
        # Compare original immutable source owners, including the closed P4
        # dependency cohort. This is receipt verification, never a reused
        # graph/current-authority cache or an SDK/materialization replay.
        for chunk in observation.selected.chunks:
            key = f"value:{chunk.reference.codec}:{chunk.reference.digest}"
            raw = read_chunk(conn, session_id=sid, operation_id=oid, key=key)
            metadata = conn.execute(
                select(chunks.c.codec).where(chunks.c.session_id == sid, chunks.c.operation_id == oid, chunks.c.chunk_key == key)
            ).scalar_one()
            if metadata != chunk.reference.codec or len(raw) != chunk.reference.byte_length or raw != chunk.canonical.encode("utf-8"):
                raise AuditIntegrityError("failed terminal readback changed an original complete source dependency")
        history = conn.execute(
            select(attempts).where(
                attempts.c.session_id == sid,
                attempts.c.operation_id == oid,
                attempts.c.sequence == running.claim.segment_sequence,
                attempts.c.generation == running.claim.ownership_generation,
            )
        ).one()
        fence = running.session_operation_context.fence
        if (
            history.session_operation_id,
            history.session_operation_epoch,
            history.session_operation_lease_token,
            history.claim_attempt,
            history.outcome,
        ) != (fence.operation_id, fence.operation_epoch, fence.lease_token, running.claim.attempt, "settled"):
            raise AuditIntegrityError("failed terminal readback changed its actual committing physical creator")
        if history.revoked_at is None or restore_utc(history.revoked_at) != restore_utc(candidate.published_at):
            raise AuditIntegrityError("failed terminal readback lost its actual attempt revocation")
        observation.committed = True
        return FailureBundleCommitted(parent)
    if parent.status in ("completed", "failed"):
        _require_other_terminal_journal(conn, parent)
        return FailureSettledElsewhere(parent)
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == oid, segments.c.sequence == running.claim.segment_sequence
        )
    ).one()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == oid,
            attempts.c.sequence == running.claim.segment_sequence,
            attempts.c.generation == running.claim.ownership_generation,
        )
    ).one()
    driving = _settlement_driving(row, segment, history)
    if driving == candidate.driving:
        actual = _read_tool_failure_selection_on_connection(conn, running, env_ref_names=observation.env_ref_names)
        if (
            actual.selected.checkpoint_id != candidate.append.expected_checkpoint_id
            or encode_continuation_snapshot(actual.selected.stack) != candidate.append.predecessor_json
        ):
            raise AuditIntegrityError("failed terminal noncommit readback changed its original complete predecessor")
        if observation.source is not None:
            fresh = actual.source
            if fresh is None or fresh.identity != observation.source.identity:
                raise AuditIntegrityError("failed terminal noncommit readback changed its actual retained source")
        elif actual.source is not None:
            raise AuditIntegrityError("failed terminal noncommit readback acquired another completed failure source")
        return FailureBundleNotCommitted()
    return FailureDrivingStateChanged(parent.cancel_requested_at, driving)


def _require_other_terminal_journal(conn: Connection, parent: ComposerOperationRecord) -> None:
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

    sid, oid = str(parent.session_id), parent.operation_id
    point = conn.execute(
        select(checkpoints).where(
            checkpoints.c.session_id == sid, checkpoints.c.operation_id == oid, checkpoints.c.checkpoint_id == parent.checkpoint_id
        )
    ).one_or_none()
    event = conn.execute(
        select(events).where(events.c.session_id == sid, events.c.operation_id == oid, events.c.sequence == parent.journal_cursor)
    ).one_or_none()
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == sid,
            attempts.c.operation_id == oid,
            attempts.c.sequence == parent.current_segment_sequence,
            attempts.c.generation == parent.ownership_generation,
        )
    ).one_or_none()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == sid, segments.c.operation_id == oid, segments.c.sequence == parent.current_segment_sequence
        )
    ).one_or_none()
    if (
        point is None
        or event is None
        or segment is None
        or point.cursor != parent.journal_cursor
        or event.tag != "settled"
        or parent.settled_at is None
        or segment.generation != parent.ownership_generation
        or restore_utc(point.created_at) != restore_utc(event.created_at)
    ):
        raise AuditIntegrityError("separate terminal lacks its actual canonical checkpoint/attempt disposition")
    if segment.state == "settled":
        if (
            history is None
            or history.revoked_at is None
            or (history.session_operation_id, history.session_operation_epoch, history.claim_attempt)
            != (parent.session_operation_id, parent.session_operation_epoch, parent.attempt)
            or segment.claim_token is not None
            or segment.claim_owner_instance_id is not None
            or segment.claim_expires_at is not None
            or restore_utc(history.revoked_at) != restore_utc(parent.settled_at)
            or history.outcome != ("settled" if parent.settled_by == "owner_terminal" else "interrupted")
        ):
            raise AuditIntegrityError("separate terminal lost its actual started attempt closure")
    elif segment.state == "queued":
        # The canonical lost-owner writer deliberately leaves a never-started
        # segment queued. Its journal is a disposition, not a new live owner.
        if history is not None or any(value is not None for value in (segment.started_at, segment.deadline_at, segment.cooperative_at)):
            raise AuditIntegrityError("separate queued terminal invented a started attempt")
    else:
        raise AuditIntegrityError("separate terminal lost its canonical segment disposition")
    root = decode_continuation_snapshot(
        read_chunk(conn, session_id=sid, operation_id=oid, key=point.snapshot_chunk_key).decode("utf-8", errors="strict")
    )
    attribution = canonical_json(
        {
            "version": 1,
            "sequence": parent.current_segment_sequence,
            "generation": parent.ownership_generation,
            "status": parent.status,
            "failure_code": parent.failure_code,
        }
    )
    if (
        root.cursor != point.cursor
        or (root.logical.session_id, root.logical.operation_id) != (sid, oid)
        or read_chunk(conn, session_id=sid, operation_id=oid, key=event.payload_chunk_key) != attribution.encode("utf-8")
    ):
        raise AuditIntegrityError("separate terminal changed its complete canonical source/event")


async def fail_continuation_tool_operation(
    service: SessionServiceImpl,
    running: ComposerOperationRunning,
    *,
    required_work: RequiredWorkCoordinator,
    failure_projection_work: RequiredWorkTicket,
    env_ref_names: frozenset[str],
    original: BaseException | None = None,
    custody: ContinuationToolFailureCustody | None = None,
) -> CommittedContinuationFailure:
    """At most two physically joined terminal invocations; no tool/P4 replay."""
    from uuid import uuid4

    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.async_workers import run_required_sql_finish_once
    from elspeth.web.composer.audit_storage import (
        ComposerOperationFailureFacts,
        RetainedOperationFailureFacts,
        private_operation_failure_draft,
    )
    from elspeth.web.composer.continuation_tool_failure import combine_failure_objects
    from elspeth.web.composer.private_diagnostic_facts import capture_failure_facts
    from elspeth.web.coordination.composer_operation_authority import (
        _read,
        _record_from_row,
        _select_failure_on_connection,
        settle_composer_operation_on_connection,
    )
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
    from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkSource, RequiredWorkTicket
    from elspeth.web.sessions.composer_operation_errors import (
        operation_failure_diagnostic_id,
        project_composer_operation_error,
        project_retained_tool_failure,
        worker_lost_error,
    )
    from elspeth.web.sessions.continuation import prepare_segment_settlement_on_connection
    from elspeth.web.sessions.service import ComposerToolFailureSQLCompletionUnknown

    if type(required_work) is not RequiredWorkCoordinator or type(failure_projection_work) is not RequiredWorkTicket:
        raise AuditIntegrityError("V2 failed terminal requires its actual registered coordinator/projection")
    authority = required_work.authority
    if (
        authority.context is not running.session_operation_context
        or authority.durable_operation_id != running.claim.operation_id
        or authority.claim_attempt != running.claim.attempt
        or authority.proposal_id is not None
    ):
        raise AuditIntegrityError("V2 failed terminal changed its actual original physical owner")
    required_work.validate_terminal_failure_work(projection_ticket=failure_projection_work)
    timing = required_work.sql_deadline_owner
    if timing is None or timing.running is not running:
        raise AuditIntegrityError("V2 failed terminal lacks its actual original SQL deadline owner")
    timing.validate_context(running.session_operation_context, operation_id=running.claim.operation_id, attempt=running.claim.attempt)
    timing.begin_settlement()
    sid, oid = str(running.claim.session_id), running.claim.operation_id
    observation: ContinuationFailureObservation | None = None
    prepared_candidates: list[PreparedSegmentSettlement] = []
    physical_ticket: RequiredWorkTicket | None = None
    physical_outcomes: list[RequiredSQLFinishOnce[ComposerOperationRecord]] = []
    cancellations: list[asyncio.CancelledError] = []
    errors: list[BaseException] = []
    if custody is not None:
        if custody.running is not running or custody.work.coordinator is not required_work:
            raise AuditIntegrityError("V2 failed terminal replaced its native failure custodian")
        if custody.original is not None:
            errors.append(custody.original)
        errors.extend(custody.errors)
    if original is not None and not any(error is original for error in errors):
        errors.append(original)
    readback: FailureReadback | None = None

    def unknown(message: str, error: BaseException | None = None) -> ComposerToolFailureSQLCompletionUnknown:
        if error is not None and not any(error is previous for previous in errors):
            errors.append(error)
        for cancellation in cancellations:
            if not any(cancellation is previous for previous in errors):
                errors.append(cancellation)
        failure = ComposerToolFailureSQLCompletionUnknown(
            message, observation=observation, prepared_candidates=tuple(prepared_candidates), ticket=physical_ticket, custody=custody
        )
        if errors:
            failure.__cause__ = combine_failure_objects(errors[0], *errors[1:])
        return failure

    def write() -> ComposerOperationRecord:
        nonlocal observation
        with service._session_process_locked_begin(sid) as conn, service._session_write_lock(conn, sid):
            now = database_now(conn)
            service._require_session_operation_context_on_connection(
                conn,
                running.session_operation_context,
                session_id=sid,
                expected_kind=SessionOperationKind.COMPOSE,
                now=now,
                audit_only=True,
            )
            selection = _read_tool_failure_selection_on_connection(conn, running, env_ref_names=env_ref_names)
            source = selection.source
            if source is not None and not source.applied:
                raise AuditIntegrityError("failed terminal cannot bypass its actual owed P4")
            row = _read(conn, running.claim.session_id, oid)
            if row is None:
                raise AuditIntegrityError("V2 failed terminal lost its actual parent")
            current = _record_from_row(row)
            diagnostic = operation_failure_diagnostic_id(session_id=sid, operation_id=oid)
            if observation is not None:
                selected_failure = observation.prepared.outcome
                if not isinstance(selected_failure, ComposerOperationError):
                    raise AuditIntegrityError("failed terminal candidate changed its actual failed result type")
            elif selection.unresolved:
                selected_failure = worker_lost_error(request_id=current.request_id)
            elif errors:
                selected_failure = project_composer_operation_error(
                    combine_failure_objects(errors[0], *errors[1:]), request_id=current.request_id, diagnostic_id=diagnostic
                )
            elif source is not None:
                selected_failure = project_retained_tool_failure(source, request_id=current.request_id, diagnostic_id=diagnostic)
            else:
                raise AuditIntegrityError("V2 failed terminal has neither actual native nor retained failure source")
            selected_failure = _select_failure_on_connection(row, selected_failure, now=now, authoritative_failure=False)
            segment = conn.execute(
                select(segments).where(
                    segments.c.session_id == sid, segments.c.operation_id == oid, segments.c.sequence == running.claim.segment_sequence
                )
            ).one()
            history = conn.execute(
                select(attempts).where(
                    attempts.c.session_id == sid,
                    attempts.c.operation_id == oid,
                    attempts.c.sequence == running.claim.segment_sequence,
                    attempts.c.generation == running.claim.ownership_generation,
                )
            ).one()
            previous = observation
            separate_stop = (
                previous is not None
                and selected_failure.failure_code == "request_cancelled"
                and isinstance(previous.prepared.outcome, ComposerOperationError)
                and previous.prepared.outcome.failure_code != "request_cancelled"
            )
            if previous is None or separate_stop:
                if previous is not None:
                    # Only a newly durable Stop may consume slot 1 with changed
                    # publication DATA. Other driving identities stay exact.
                    driving = _settlement_driving(row, segment, history)
                    if (
                        replace(
                            driving,
                            cancel_requested_at=previous.prepared.driving.cancel_requested_at,
                            status_revision=previous.prepared.driving.status_revision,
                        )
                        != previous.prepared.driving
                        or driving.status_revision <= previous.prepared.driving.status_revision
                    ):
                        raise AuditIntegrityError("new Stop changed more than its actual cancellation observation")
                prepared = prepare_segment_settlement_on_connection(
                    conn,
                    session_id=sid,
                    operation_id=oid,
                    parent=row,
                    segment=segment,
                    history=history,
                    outcome=selected_failure,
                    settled_by="owner_terminal",
                    attempt_outcome="settled",
                )
                prepared_candidates.append(prepared)
                if source is not None:
                    secondary = tuple(
                        error for error in errors if custody is None or (error is not custody.original and error is not custody.wrapper)
                    )
                    observed_facts = None if not secondary else capture_failure_facts(combine_failure_objects(secondary[0], *secondary[1:]))
                    capture = RetainedOperationFailureFacts(
                        oid, current.request_id, running.session_operation_context, source, observed_facts
                    )
                    capture.validate_for(durable_operation_id=oid, request_id=current.request_id, context=running.session_operation_context)
                    draft = private_operation_failure_draft(capture, failure=selected_failure)
                else:
                    native = combine_failure_objects(errors[0], *errors[1:])
                    retained = None if custody is None else custody.observation.stored
                    facts = None if retained is None else retained.private_failure
                    native_capture = ComposerOperationFailureFacts(
                        oid,
                        current.request_id,
                        running.session_operation_context,
                        capture_failure_facts(native) if facts is None else facts,
                    )
                    draft = private_operation_failure_draft(native_capture, failure=selected_failure)
                observation = ContinuationFailureObservation(
                    running,
                    source,
                    selection.selected,
                    prepared,
                    draft,
                    str(uuid4()),
                    tuple(errors),
                    env_ref_names,
                    prior=previous,
                    physical=physical_outcomes,
                    deferred_cancellations=cancellations,
                )
            else:
                if (
                    selected_failure != previous.prepared.outcome
                    or _settlement_driving(row, segment, history) != previous.prepared.driving
                    or (source is None) != (previous.source is None)
                    or (source is not None and previous.source is not None and source.identity != previous.source.identity)
                ):
                    raise AuditIntegrityError("immutable failed terminal retry changed its actual bundle/source")
            candidate = observation
            if candidate is None:
                raise AuditIntegrityError("V2 terminal did not retain its prepared candidate before writing")
            active = service._write_audit_cohort_on_connection(
                conn,
                session_id=running.claim.session_id,
                drafts=(candidate.draft,),
                writer_principal="compose_loop",
                composition_state_id=None,
                session_operation_context=running.session_operation_context,
                audit_only=True,
                message_ids=(candidate.message_id,),
                created_at=candidate.prepared.published_at,
                recorded_at=candidate.prepared.published_at,
            )
            if len(active) != 1:
                raise AuditIntegrityError("failed terminal candidate did not publish its own complete private cohort")
            candidate.private_sequence = conn.execute(
                select(messages.c.sequence_no).where(messages.c.session_id == sid, messages.c.id == candidate.message_id)
            ).scalar_one()
            result = settle_composer_operation_on_connection(
                conn, running, outcome=candidate.prepared.outcome, prepared_settlement=candidate.prepared
            )
            selected_outcome = candidate.prepared.outcome
            if not isinstance(selected_outcome, ComposerOperationError):
                raise AuditIntegrityError("failed terminal writer changed its actual failed outcome type")
            expected_json = canonical_json(selected_outcome.model_dump(mode="json"))
            fence = running.session_operation_context.fence
            if (
                (
                    result.session_id,
                    result.operation_id,
                    result.attempt,
                    result.current_segment_sequence,
                    result.ownership_generation,
                    result.session_operation_id,
                    result.session_operation_epoch,
                )
                != (
                    running.claim.session_id,
                    oid,
                    running.claim.attempt,
                    running.claim.segment_sequence,
                    running.claim.ownership_generation,
                    fence.operation_id,
                    fence.operation_epoch,
                )
                or result.status != "failed"
                or result.phase != "settled"
                or result.settled_by != "owner_terminal"
                or result.failure_code != selected_outcome.failure_code
                or result.result_json != expected_json
                or result.checkpoint_id != candidate.prepared.append.checkpoint_id
                or result.journal_cursor != candidate.prepared.append.expected_cursor + len(candidate.prepared.append.events)
                or result.settled_at is None
                or restore_utc(result.settled_at) != restore_utc(candidate.prepared.published_at)
            ):
                raise AuditIntegrityError("failed terminal writer returned another actual terminal/checkpoint owner")
            candidate.candidate = result
        candidate.committed = True
        return result

    def read() -> FailureReadback:
        if observation is None:
            raise AuditIntegrityError("failed terminal has no retained prewrite candidate")
        with service._session_process_locked_begin(sid) as conn, service._session_write_lock(conn, sid):
            return read_failure_bundle_on_connection(conn, observation)

    for slot in range(2):
        try:
            timing.remaining_statement(session_id=sid)
            sql = required_work.reserve(RequiredWorkSource.TERMINAL_FAILURE_SQL, sql_attempt_ordinal=slot)
        except BaseException as error:
            failure = unknown("failed terminal exhausted/refused its original bounded invocation before submission", error)
            raise failure from failure.__cause__
        physical_ticket = sql
        try:
            actual = await run_required_sql_finish_once(sql, write)
            no_submission = required_work.validate_terminal_failure_sql_outcome(
                ticket=sql, actual_outcome=actual, expected_sql_attempt_ordinal=slot
            )
        except BaseException as error:
            failure = unknown("failed terminal has no exact joined physical handoff", error)
            raise failure from failure.__cause__
        physical_outcomes.append(actual)
        for cancellation in actual.deferred_cancellations:
            if not any(cancellation is item for item in cancellations):
                cancellations.append(cancellation)
        if type(actual) is RequiredSQLRaised and not any(actual.error is item for item in errors):
            errors.append(actual.error)
        if observation is None:
            if type(actual) is RequiredSQLRaised:
                if no_submission and slot == 0:
                    # No canonical writer entered and no publication DATA was
                    # selected. Slot 1 may make its first locked observation;
                    # this is never inferred from a timeout/unjoined Future.
                    continue
                failure = unknown("failed terminal refused before retaining its actual candidate", actual.error)
                raise failure from failure.__cause__
            raise unknown("failed terminal returned without its actual observation")
        if type(actual) is RequiredSQLReturned:
            if not observation.committed or observation.candidate is not actual.value or type(actual.value) is not ComposerOperationRecord:
                raise unknown("failed terminal returned without its actual post-outer-commit owner")
            observation.original_errors = tuple(errors)
            return CommittedContinuationFailure(actual.value, observation)
        try:
            read_sql, projection = required_work.reserve_pair(
                RequiredWorkSource.TERMINAL_WRITER_READ_SQL,
                RequiredWorkSource.TERMINAL_WRITER_READ_PROJECTION,
                transition_ordinal=0,
                semantic_ordinal=0,
            )
            observed = await run_required_sql_finish_once(read_sql, read)
            with required_work._lock, read_sql._lock:
                if read_sql._finish_once_handoff is not observed:
                    raise AuditIntegrityError("failed terminal readback replaced its actual finish-once carrier")
                required_work.validate_joined_lifecycle_sql_outcome(
                    ticket=read_sql, expected_source=RequiredWorkSource.TERMINAL_WRITER_READ_SQL, actual_outcome=observed
                )
            for cancellation in observed.deferred_cancellations:
                if not any(cancellation is item for item in observation.deferred_cancellations):
                    observation.deferred_cancellations.append(cancellation)
            if type(observed) is RequiredSQLRaised:
                projection.complete_owned(observed.error)
                raise observed.error
            if type(observed) is not RequiredSQLReturned:
                raise AuditIntegrityError("failed terminal readback has no actual nominal physical result")
            readback = observed.value
            observation.readback = readback
            projection.begin_projection()
            projection.complete_owned()
        except BaseException as error:
            failure = unknown("failed terminal commit remains unknown after actual readback", error)
            raise failure from failure.__cause__
        if type(readback) is FailureBundleCommitted:
            observation.original_errors = tuple(errors)
            return CommittedContinuationFailure(readback.terminal, observation)
        if type(readback) is FailureSettledElsewhere:
            raise unknown("failed terminal observed a separately settled disposition")
        if type(readback) is FailureDrivingStateChanged and readback.cancel_requested_at is None:
            raise unknown("failed terminal driving source changed without its separately sealed Stop")
        if slot == 1:
            break
    raise unknown("both bounded failed terminal invocation slots finished without an exact committed bundle")
