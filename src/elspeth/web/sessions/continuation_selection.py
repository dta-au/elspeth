"""Read the complete selected private DATA under the existing current SQL guard."""

from __future__ import annotations

from collections import deque

from sqlalchemy import Connection

from elspeth.contracts.bounded_serialization import utf8_size
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import (
    AdvisorFrame,
    ComposeFrame,
    ContinuationFrameStack,
    EndFrame,
    InitializeFrame,
    PrivateChunkRef,
    ToolBatchFrame,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    verify_private_chunk,
)
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    StoredContinuationSnapshot,
    _require_current_continuation_running,
    load_snapshot,
    read_private_chunk,
)


def required_private_chunks(stack: ContinuationFrameStack) -> tuple[PrivateChunkRef, ...]:
    """Closed frame walk; preserve first occurrence, refuse conflicting bounds."""
    if type(stack) is not ContinuationFrameStack:
        raise AuditIntegrityError("private selection requires its nominal complete root")
    if decode_continuation_snapshot(encode_continuation_snapshot(stack)) != stack:
        raise AuditIntegrityError("private selection changed its complete root on admission")
    refs = [item.result for item in stack.pending_owner_results]
    if stack.semantic_frontier is not None:
        refs.append(stack.semantic_frontier)
    if stack.active_action_source is not None:
        refs.append(stack.active_action_source.response)
    for frame in stack.frames:
        if type(frame) is InitializeFrame:
            if frame.bootstrap is not None:
                refs.append(frame.bootstrap)
            if frame.title_response is not None:
                refs.append(frame.title_response)
        elif type(frame) is ComposeFrame:
            refs.extend(
                (
                    frame.bootstrap,
                    frame.iteration,
                    frame.messages,
                    frame.initial_state,
                    frame.state,
                    frame.evidence,
                    frame.audit_buffer,
                    frame.transcript,
                    frame.caches,
                    frame.attachment_admissions,
                )
            )
            if frame.tools is not None:
                refs.append(frame.tools)
            if frame.pending_response is not None:
                refs.append(frame.pending_response)
        elif type(frame) is ToolBatchFrame:
            refs.extend((frame.response, frame.accumulator))
        elif type(frame) is AdvisorFrame:
            if frame.review.codec != "composer.advisor.v1" or (
                frame.response is not None and frame.response.codec != "composer.response.v1"
            ):
                raise AuditIntegrityError("Advisor frame changed its actual private source codecs")
            refs.append(frame.review)
            if frame.response is not None:
                refs.append(frame.response)
        elif type(frame) is EndFrame:
            if frame.state.codec != "composer.state.v1" or frame.candidate.codec != "composer.end.v1":
                raise AuditIntegrityError("END frame changed its actual private source codecs")
            refs.extend((frame.state, frame.candidate))
        else:
            raise AuditIntegrityError("private selection has an unsupported nominal frame")
    unique: dict[tuple[str, str], PrivateChunkRef] = {}
    for ref in refs:
        if type(ref) is not PrivateChunkRef:
            raise AuditIntegrityError("private selection lost its nominal chunk reference")
        key = (ref.codec, ref.digest)
        previous = unique.get(key)
        if previous is not None and previous != ref:
            raise AuditIntegrityError("one private chunk identity has conflicting byte bounds")
        unique[key] = ref
    return tuple(unique.values())


def nested_private_chunks(chunk: PrivateChunk) -> tuple[PrivateChunkRef, ...]:
    """Only the closed END/Advisor and application-record schemas add edges.

    Their admitted edge codecs exclude END and Advisor, so self/cross cycles
    cannot be expressed by valid source values. Never discover refs by walking
    arbitrary JSON keys or importing runtime/dispatch owners.
    """
    from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
    from elspeth.web.composer.continuation_end_values import decode_end_candidate

    if type(chunk) is not PrivateChunk:
        raise AuditIntegrityError("nested private selection requires its actual chunk")
    if not 0 <= chunk.reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES or (
        utf8_size(chunk.canonical, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) != chunk.reference.byte_length
    ):
        raise AuditIntegrityError("nested private source exceeds its complete document bound")
    if chunk.reference.codec == "composer.evidence.v2":
        from elspeth.web.composer.continuation_evidence import TurnEvidenceSnapshot

        try:
            snapshot = TurnEvidenceSnapshot.model_validate_json(chunk.canonical)
        except (ValueError, TypeError) as error:
            raise AuditIntegrityError("private evidence dependency walk lost its closed original schema") from error
        entry = snapshot.application_entry
        return (
            snapshot.recorder,
            *tuple(
                reference
                for record in (() if entry is None else entry.applications)
                for reference in (record.response, record.pending_batch, record.prepared_input, record.result)
            ),
        )
    if chunk.reference.codec == "composer.end.v1":
        candidate = decode_end_candidate(chunk)
        refs = [candidate.response, candidate.state, candidate.evidence, candidate.iteration]
        if candidate.publication_source is not None:
            refs.append(candidate.publication_source.response)
        if candidate.semantic_source is not None:
            refs.append(candidate.semantic_source.response)
        return tuple(refs)
    if chunk.reference.codec == "composer.advisor.v1":
        source = decode_continuation_advisor_invocation(chunk)
        refs = [source.state, source.evidence, source.transcript, source.iteration]
        if source.premise is not None:
            refs.append(source.premise.frontier)
        return tuple(refs)
    return ()


def require_private_chunk_closure(stack: ContinuationFrameStack, values: tuple[PrivateChunk, ...]) -> None:
    """Admit the exact breadth-first first-occurrence cohort and every bound byte.

    This is DATA completeness, not current read or historical receipt authority.
    Leaves retain their existing semantic decoder/current-reader obligations.
    """
    if type(values) is not tuple:
        raise AuditIntegrityError("selected DATA lost its exact private chunk tuple")
    pending = deque(required_private_chunks(stack))
    seen: dict[tuple[str, str], PrivateChunkRef] = {}
    position = 0
    while pending:
        reference = pending.popleft()
        if not 0 <= reference.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise AuditIntegrityError("selected private source exceeds its complete document bound")
        key = reference.codec, reference.digest
        previous = seen.get(key)
        if previous is not None:
            if previous != reference:
                raise AuditIntegrityError("nested private reference changed its exact retained byte bound")
            continue
        if position >= len(values):
            raise AuditIntegrityError("selected DATA lost its complete ordered chunk cohort")
        chunk = values[position]
        if type(chunk) is not PrivateChunk or chunk.reference != reference:
            raise AuditIntegrityError("selected DATA substituted its ordered chunk reference")
        if utf8_size(chunk.canonical, limit=CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES) != reference.byte_length:
            raise AuditIntegrityError("selected DATA changed its bounded original bytes")
        verify_private_chunk(reference, chunk.canonical)
        seen[key] = reference
        position += 1
        pending.extend(nested_private_chunks(chunk))
    if position != len(values):
        raise AuditIntegrityError("selected DATA added an unselected or repeated private chunk")


def load_selected_private_chunks_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: StoredContinuationSnapshot,
) -> tuple[PrivateChunk, ...]:
    if type(running) is not ComposerOperationRunning or type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("private selection requires nominal running and stored owners")
    # Same reconciliation read boundary as load_continuation_snapshot: this does
    # not waive the actual session lease or six-field current claim/history join.
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    current = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    if current != selected:
        raise ContinuationCASConflict("private chunk read selected an obsolete snapshot")
    return load_snapshot_private_data_on_connection(conn, session_id=sid, operation_id=operation_id, selected=current)


def load_snapshot_private_data_on_connection(
    conn: Connection,
    *,
    session_id: str,
    operation_id: str,
    selected: StoredContinuationSnapshot,
) -> tuple[PrivateChunk, ...]:
    """Read complete bound DATA after the caller proves its actual SQL admission.

    This grants no current operation, provider allowance or mutation authority.
    Start/recovery callers supply their selected snapshot inside the same locked
    transaction as the original actor, claim and head checks.
    """
    if type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("private DATA requires its nominal stored snapshot")
    sid = session_id
    current = selected
    stack = decode_continuation_snapshot(current.payload_json)
    if (stack.logical.session_id, stack.logical.operation_id) != (sid, operation_id):
        raise AuditIntegrityError("private selection changed its logical operation")
    return load_private_chunks(conn, session_id=sid, operation_id=operation_id, stack=stack)


def load_private_chunks(conn: Connection, *, session_id: str, operation_id: str, stack: ContinuationFrameStack) -> tuple[PrivateChunk, ...]:
    """Complete immutable DATA closure after the actual caller's SQL admission.

    Selected read/start/recovery and future END transactions retain their own
    current claim, actor and root-CAS obligations. This function grants none.
    """
    if type(stack) is not ContinuationFrameStack or (stack.logical.session_id, stack.logical.operation_id) != (session_id, operation_id):
        raise AuditIntegrityError("private closure changed its nominal logical operation")
    sid = session_id
    loaded: dict[tuple[str, str], PrivateChunk] = {}
    pending = deque(required_private_chunks(stack))
    while pending:
        ref = pending.popleft()
        if not 0 <= ref.byte_length <= CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
            raise AuditIntegrityError("selected private source exceeds its complete document bound")
        identity = ref.codec, ref.digest
        previous = loaded.get(identity)
        if previous is not None:
            if previous.reference != ref:
                raise AuditIntegrityError("nested private reference changed its exact retained byte bound")
            continue
        payload = read_private_chunk(conn, session_id=sid, operation_id=operation_id, reference=ref)
        try:
            canonical = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise AuditIntegrityError("selected private chunk is not strict UTF-8") from error
        verify_private_chunk(ref, canonical)
        actual = PrivateChunk(ref, canonical)
        loaded[identity] = actual
        pending.extend(nested_private_chunks(actual))
    return tuple(loaded.values())
