"""Fenced K-7 action admission from the original paid semantic decision.

Read/review/whole-request Stop are admitted from current SQL, complete source
DATA, the actual discovery registry and an exact retained decision journal.
Execution and publication remain with their respective physical owners.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, RootTransaction

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_output import ReadContinuationAction, ReviewContinuationAction
from elspeth.web.composer.continuation import (
    ComposeFrame,
    ContinuationActionSource,
    ContinuationFrameStack,
    EndFrame,
    PrivateChunkRef,
)
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    decode_frontier_chunk,
    encode_continuation_snapshot,
    encode_frontier_chunk,
    encode_private_chunk,
)
from elspeth.web.composer.continuation_end_effects import semantic_end_effect_key
from elspeth.web.composer.continuation_end_values import EndContinuationCandidate, decode_end_candidate, encode_end_candidate
from elspeth.web.composer.continuation_progress import (
    ActionExecutionReceipt,
    canonicalize_continuation_obligations,
    continuation_action_key,
    current_action_receipts,
    record_action_receipt,
    retain_advisor_action,
)
from elspeth.web.composer.continuation_semantic_advisor_evidence import (
    SemanticAdvisorEvidenceData,
    _derive_paid_semantic_evidence_from_original_invocation,
    derive_paid_semantic_evidence,
)
from elspeth.web.composer.tools.continuation_support import continuation_tool_support
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import (
    ContinuationCASConflict,
    ContinuationEvent,
    StoredContinuationSnapshot,
    _effect_snapshot,
    _owed_continuation_p4_active,
    _require_current_continuation_running,
    append_complete_snapshot,
    put_chunk,
    read_chunk,
    require_snapshot_cas,
    retain_action_execution_receipt_on_connection,
)
from elspeth.web.sessions.continuation_owner_results import (
    ContinuationReadOwnerResult,
    re_admit_read_owner_result,
    require_read_versions_on_connection,
)
from elspeth.web.sessions.continuation_semantic_advisor import (
    SemanticAdvisorPaidDecisionData,
    SemanticAdvisorProviderOutcome,
    _advisor,
    _creator_data,
    _current,
    _effect_data,
    _historical_snapshot,
    _journal_data,
    _paid_result_data,
    read_current_paid_semantic_decision_on_connection,
)
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from elspeth.web.sessions.models import composer_continuation_events_table as events

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_semantic_invocation import SemanticAdvisorInvocationData
    from elspeth.web.sessions.continuation_checkpoint_paid_terminal import VerifiedCheckpointPaidOverflowTerminal
    from elspeth.web.sessions.continuation_primary import HistoricalPrimaryResponseData
    from elspeth.web.sessions.continuation_semantic_review_result import SemanticReviewCommitted


@dataclass(frozen=True, slots=True)
class SemanticActionAdmission:
    """Observed action DATA; the current SQL reader remains its sole issuer."""

    paid: SemanticAdvisorPaidDecisionData
    evidence: SemanticAdvisorEvidenceData
    action: ReadContinuationAction | ReviewContinuationAction | None
    action_key: str | None
    expected_root: ContinuationFrameStack
    frontier_chunk: PrivateChunk
    owner_effect_key: str | None = None
    review_candidate: EndContinuationCandidate | None = None
    review_candidate_source: PrivateChunkRef | None = None
    original_primary_response: HistoricalPrimaryResponseData | None = None


@dataclass(frozen=True, slots=True)
class _CurrentTransactionReviewProof:
    """One already re-proved installed source, valid only in its SQL transaction."""

    connection: Connection
    transaction: RootTransaction
    installed: StoredContinuationSnapshot
    effect_key: str
    admission: SemanticActionAdmission


def _require_same_review_transaction(
    conn: Connection,
    source_connection: Connection,
    source_transaction: RootTransaction,
    source_snapshot: StoredContinuationSnapshot,
    current_snapshot: StoredContinuationSnapshot,
    source_effect_key: str,
    current_effect_key: str,
) -> None:
    if (
        conn is not source_connection
        or conn.get_transaction() is not source_transaction
        or source_snapshot != current_snapshot
        or source_effect_key != current_effect_key
    ):
        raise AuditIntegrityError("semantic Review cannot reuse proof outside its original current transaction and source")


def _capture_current_review_proof(
    conn: Connection, selected: StoredContinuationSnapshot, effect_key: str, admission: SemanticActionAdmission
) -> _CurrentTransactionReviewProof:
    transaction = conn.get_transaction()
    if (
        transaction is None
        or type(admission) is not SemanticActionAdmission
        or admission.paid.selected != selected
        or admission.paid.source.effect_key != effect_key
    ):
        raise AuditIntegrityError("semantic Review cannot capture an unproved original current source")
    return _CurrentTransactionReviewProof(conn, transaction, selected, effect_key, admission)


@dataclass(frozen=True, slots=True)
class SemanticActionReceiptJournal:
    """Actual SQL effect outcome to append after an execution owner settles."""

    semantic_effect_key: str
    action_key: str
    owner_effect_key: str
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"]
    original_advisor_call_id: str


@dataclass(frozen=True, slots=True)
class SemanticReadCommitted:
    snapshot: StoredContinuationSnapshot
    original_result: ContinuationReadOwnerResult
    receipt: ActionExecutionReceipt
    owner_effect_key: str


@dataclass(frozen=True, slots=True)
class SemanticReadOriginal:
    """Historical original paid admission and committed read DATA, no writer grant."""

    admission: SemanticActionAdmission
    committed: SemanticReadCommitted


@dataclass(frozen=True, slots=True)
class SemanticPublicationProof:
    """Current whole-request Stop proof and its original completed END result."""

    admission: SemanticActionAdmission
    candidate: EndContinuationCandidate
    review_result: SemanticReviewCommitted


def _end_candidate(conn: Connection, paid: SemanticAdvisorPaidDecisionData) -> EndContinuationCandidate | None:
    frames = tuple(frame for frame in paid.paid_root.frames if type(frame) is EndFrame)
    if not frames:
        return None
    if len(frames) != 1:
        raise AuditIntegrityError("semantic action found multiple selected END candidates")
    reference = frames[0].candidate
    canonical = read_chunk(
        conn,
        session_id=str(paid.session_id),
        operation_id=paid.operation_id,
        key=f"value:{reference.codec}:{reference.digest}",
    ).decode("utf-8", errors="strict")
    return decode_end_candidate(PrivateChunk(reference, canonical))


def _admit_paid(conn: Connection, paid: SemanticAdvisorPaidDecisionData) -> SemanticActionAdmission:
    if type(paid) is not SemanticAdvisorPaidDecisionData:
        raise TypeError("semantic action requires its SQL-proved paid decision")
    evidence = derive_paid_semantic_evidence(paid, end_candidate=_end_candidate(conn, paid))
    return _admit_paid_evidence(paid, evidence)


def _admit_paid_evidence(paid: SemanticAdvisorPaidDecisionData, evidence: SemanticAdvisorEvidenceData) -> SemanticActionAdmission:
    decision = canonicalize_continuation_obligations(paid.decision, paid.invocation.frontier)
    action = decision.prescribed_action
    if action is not None and not isinstance(action, (ReadContinuationAction, ReviewContinuationAction)):
        raise AuditIntegrityError("this semantic action owner cannot install PRIMARY")
    if type(action) is ReadContinuationAction:
        arguments, error = parse_json_strict(action.arguments_json)
        if (
            error is not None
            or type(arguments) is not dict
            or action.tool_name not in evidence.permitted_read_tools
            or continuation_tool_support(action.tool_name, arguments) != "read"
        ):
            raise AuditIntegrityError("semantic read action lacks its actual read-only registry owner")
    if type(action) is ReviewContinuationAction and (
        action.candidate_ref != evidence.coverage.candidate_ref
        or action.evidence_binding != evidence.coverage.evidence_binding
        or evidence.coverage.end_candidate is None
        or evidence.coverage.candidate_source is None
        or evidence.coverage.end_candidate.kind == "recovery"
    ):
        raise AuditIntegrityError("semantic review cannot replace an original provider-authored recovery")
    if action is None and (paid.decision.decision != "STOP_CONFIRMED" or paid.decision.scope != "whole_request"):
        raise AuditIntegrityError("semantic action owner only admits an actual whole-request Stop")
    premise = paid.invocation.invocation.premise
    call_id = paid.audit_call.call_id
    if premise is None or type(call_id) is not str or not call_id:
        raise AuditIntegrityError("semantic action lost its exact original paid premise/call")
    retained = evidence.retained_evidence()
    installed_evidence_ids = {item.receipt_id for item in paid.invocation.frontier.evidence} | {
        item.receipt_id for item in paid.decision.evidence
    }
    if action is not None and not set(action.evidence_ids) <= installed_evidence_ids:
        raise AuditIntegrityError("semantic action premise was not retained in its installed frontier")
    changed, frontier_chunk = retain_advisor_action(
        paid.paid_root,
        decision,
        actual_advisor_call_id=call_id,
        frontier=paid.invocation.frontier,
        actual_source=paid.source,
        retained_evidence=retained,
        input_hash=premise.input_hash,
        policy_hash=premise.policy_hash,
        tool_manifest_hash=premise.tool_manifest_hash,
        permitted_read_tools=evidence.permitted_read_tools,
        complete_required_coverage=evidence.coverage.complete,
    )
    if action is None:
        action_key = None
    else:
        action_key = continuation_action_key(action, retained_evidence=retained)
    advisor = changed.frames[-1]
    if advisor.frame_kind != "advisor":
        raise AuditIntegrityError("semantic action lost its original Advisor frame")
    expected = changed.model_copy(update={"frames": (*changed.frames[:-1], advisor.model_copy(update={"phase": "decision_ready"}))})
    return SemanticActionAdmission(paid, evidence, action, action_key, expected, frontier_chunk)


def _admission_event(admission: SemanticActionAdmission) -> str:
    paid, coverage = admission.paid, admission.evidence.coverage
    return canonical_json(
        {
            "kind": "semantic_decision_extended",
            "effect_key": paid.source.effect_key,
            "input_sha256": paid.source.input_sha256,
            "decision_id": paid.decision.decision_id,
            "decision_sha256": paid.decision_sha256,
            "response": paid.source.response.model_dump(mode="json"),
            "action_key": admission.action_key,
            "action_json": admission.expected_root.active_action_json,
            "evidence": [item.model_dump(mode="json") for item in admission.evidence.references],
            "permitted_read_tools": sorted(admission.evidence.permitted_read_tools),
            "coverage": {
                "original_message_id": coverage.original_message_id,
                "original_length": coverage.original_length,
                "required_spans": coverage.required_spans,
                "covered_spans": coverage.covered_spans,
                "complete": coverage.complete,
                "candidate_ref": coverage.candidate_ref,
                "candidate_source": None if coverage.candidate_source is None else coverage.candidate_source.model_dump(mode="json"),
                "evidence_binding": coverage.evidence_binding,
            },
        }
    )


def read_semantic_action_admission_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticActionAdmission:
    """Admit only an uninstalled original paid decision under current CAS."""
    paid = read_current_paid_semantic_decision_on_connection(conn, running, selected=selected, effect_key=effect_key)
    return _admit_paid(conn, paid)


def install_semantic_action_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> StoredContinuationSnapshot:
    """Persist the exact admitted read/review/whole-Stop decision once."""
    admission = read_semantic_action_admission_on_connection(conn, running, selected=selected, effect_key=effect_key)
    return _install_semantic_action_admission(conn, running, selected=selected, admission=admission)


def _install_semantic_action_from_paid_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    paid: SemanticAdvisorPaidDecisionData,
) -> StoredContinuationSnapshot:
    """Join the combined current reader's actual DATA to its original owner.

    Only the lexical read-and-install owner calls this before any root write.
    Registry, coverage, END candidate and action admission remain unchanged.
    """
    admission = _admit_paid(conn, paid)
    return _install_semantic_action_admission(conn, running, selected=selected, admission=admission)


def _install_semantic_action_admission(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    admission: SemanticActionAdmission,
) -> StoredContinuationSnapshot:
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=f"value:{admission.frontier_chunk.reference.codec}:{admission.frontier_chunk.reference.digest}",
            payload=admission.frontier_chunk.canonical.encode("utf-8"),
            codec=admission.frontier_chunk.reference.codec,
        )
        return append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=encode_continuation_snapshot(admission.expected_root.model_copy(update={"cursor": selected.cursor})),
            event_batch=(ContinuationEvent("checkpoint", _admission_event(admission)),),
            handoff=False,
        )


def _original_paid_for_selected(
    conn: Connection,
    session_id: UUID,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    root: ContinuationFrameStack,
    effect_key: str,
) -> tuple[SemanticAdvisorPaidDecisionData, SemanticAdvisorInvocationData, HistoricalPrimaryResponseData | None]:
    sid, operation = str(session_id), operation_id
    actual = _paid_result_data(conn, sid, operation, selected, root, effect_key)
    result, data, paid_root = actual.result, actual.invocation, actual.paid_root
    if result.outcome is not SemanticAdvisorProviderOutcome.SUCCESS or result.decision is None or result.response is None:
        raise AuditIntegrityError("installed semantic action lost its original paid successful response")
    effect = _effect_data(conn, sid, operation, root, effect_key)
    creator = _creator_data(conn, sid, operation, effect)
    decision_json = canonical_json(result.decision.model_dump(mode="json"))
    paid = SemanticAdvisorPaidDecisionData(
        selected=selected,
        paid_root=paid_root,
        invocation=data,
        decision=result.decision,
        decision_json=decision_json,
        decision_sha256=sha256(decision_json.encode("utf-8")).hexdigest(),
        source=ContinuationActionSource(effect_key=effect_key, input_sha256=effect.input_sha256, response=result.response.reference),
        audit_call=result.audit_call,
        session_id=session_id,
        operation_id=operation,
        claim_attempt=creator.claim_attempt,
        operation_epoch=creator.session_operation_epoch,
        creator_sequence=effect.creator_sequence,
        creator_generation=effect.creator_generation,
        creator_operation_id=creator.session_operation_id,
        creator_operation_epoch=creator.session_operation_epoch,
        creator_lease_token=creator.session_operation_lease_token,
    )
    return paid, data, actual.primary_response


def _reprove_installed_snapshot(
    conn: Connection,
    session_id: UUID,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticActionAdmission:
    from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source

    root = decode_continuation_snapshot(selected.payload_json)
    paid, original_invocation, primary_response = _original_paid_for_selected(conn, session_id, operation_id, selected, root, effect_key)
    original_selected = semantic_recovery_source(conn, str(session_id), operation_id, selected).snapshot
    if type(paid) is not SemanticAdvisorPaidDecisionData:
        raise TypeError("semantic action requires its SQL-proved paid decision")
    evidence = _derive_paid_semantic_evidence_from_original_invocation(paid, original_invocation, end_candidate=_end_candidate(conn, paid))
    admission = replace(_admit_paid_evidence(paid, evidence), original_primary_response=primary_response)
    expected = admission.expected_root.model_copy(update={"cursor": selected.cursor})
    if root != expected:
        raise AuditIntegrityError("installed semantic action changed its complete original paid/admitted root")
    sid, operation = str(session_id), operation_id
    reference = admission.frontier_chunk.reference
    retained = read_chunk(conn, session_id=sid, operation_id=operation, key=f"value:{reference.codec}:{reference.digest}")
    if retained != admission.frontier_chunk.canonical.encode("utf-8"):
        raise AuditIntegrityError("installed semantic action changed its exact retained frontier")
    _journal_data(conn, sid, operation, cursor=original_selected.cursor, tag="checkpoint", payload=_admission_event(admission))
    return admission


def require_installed_semantic_action_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticActionAdmission:
    """Cold/current readback of one exact installed action before execution."""
    _current(conn, running, selected)
    return _reprove_installed_snapshot(conn, running.claim.session_id, running.claim.operation_id, selected, effect_key)


def require_semantic_read_execution_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticActionAdmission:
    """Re-enter one prepared real read effect before/after physical dispatch."""
    _current(conn, running, selected)
    return _reprove_prepared_read(conn, running, selected, effect_key)


def _reprove_prepared_read(
    conn: Connection,
    running: ComposerOperationRunning | None,
    selected: StoredContinuationSnapshot,
    effect_key: str,
    *,
    historical_identity: tuple[UUID, str] | None = None,
    effect_disposition: Literal["prepared", "committed"] = "prepared",
) -> SemanticActionAdmission:
    root = decode_continuation_snapshot(selected.payload_json)
    if running is None:
        if historical_identity is None or type(historical_identity[0]) is not UUID or type(historical_identity[1]) is not str:
            raise TypeError("historical semantic read requires its original nominal request identity")
        session_id, operation = historical_identity
        if effect_disposition != "committed":
            raise AuditIntegrityError("historical semantic read cannot grant a prepared execution")
    else:
        if historical_identity is not None:
            raise AuditIntegrityError("current semantic read cannot replace its running identity")
        session_id, operation = running.claim.session_id, running.claim.operation_id
    sid = str(session_id)
    if selected.cursor < 1:
        raise AuditIntegrityError("semantic read has no installed predecessor")
    installed = _historical_snapshot(conn, sid, operation, cursor=selected.cursor - 1)
    admission = _reprove_installed_snapshot(conn, session_id, operation, installed, effect_key)
    if type(admission.action) is not ReadContinuationAction or admission.action_key is None:
        raise AuditIntegrityError("semantic read execution lacks its original active read action")
    owner_key = f"read:{admission.action_key}"
    row = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == owner_key)
    ).one_or_none()
    if (
        row is None
        or row.owner != "read"
        or row.disposition != effect_disposition
        or row.action_json != installed_action_json(admission)
        or row.input_sha256 != _read_input_hash(admission)
        or row.provider_attempt_id is not None
    ):
        raise AuditIntegrityError("semantic read lost its original prepared effect/creator")
    creator = _creator_data(conn, sid, operation, row)
    if effect_disposition == "prepared" and (
        running is None
        or (row.creator_sequence, row.creator_generation) != (running.claim.segment_sequence, running.claim.ownership_generation)
        or row.receipt_key is not None
        or row.result_chunk_key is not None
    ):
        raise AuditIntegrityError("semantic read prepared effect lost its current creator or unsettled state")
    if effect_disposition == "committed" and (
        row.receipt_key != owner_key or type(row.result_chunk_key) is not str or creator.session_operation_epoch < 1
    ):
        raise AuditIntegrityError("semantic read committed effect lost its original creator/result")
    expected = decode_continuation_snapshot(
        _effect_snapshot(installed.payload_json, effect_key=owner_key, disposition="prepared", receipt_id=None)
    ).model_copy(update={"cursor": selected.cursor})
    frontier = decode_frontier_chunk(admission.frontier_chunk)
    prepared_frontier = encode_frontier_chunk(
        record_action_receipt(
            frontier,
            ActionExecutionReceipt(admission.action_key, owner_key, "prepared"),
            actual_owner_receipt_id=owner_key,
        )
    )
    expected = expected.model_copy(update={"semantic_frontier": prepared_frontier.reference})
    if root != expected:
        raise AuditIntegrityError("semantic read changed its complete prepared current root")
    retained = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"value:{prepared_frontier.reference.codec}:{prepared_frontier.reference.digest}",
    )
    if retained != prepared_frontier.canonical.encode("utf-8"):
        raise AuditIntegrityError("semantic read changed its actual prepared action receipt")
    _journal_data(conn, sid, operation, cursor=selected.cursor, tag="checkpoint", payload=_read_prepare_event(admission, owner_key))
    return replace(admission, owner_effect_key=owner_key)


def installed_action_json(admission: SemanticActionAdmission) -> str:
    encoded = admission.expected_root.active_action_json
    if type(encoded) is not str or not encoded:
        raise AuditIntegrityError("semantic read lost its exact installed action JSON")
    return encoded


def _read_input_hash(admission: SemanticActionAdmission) -> str:
    return stable_hash(
        {
            "domain": "composer.semantic-read-input.v1",
            "source": admission.paid.source.model_dump(mode="json"),
            "action": installed_action_json(admission),
        }
    )


def _read_prepare_event(admission: SemanticActionAdmission, owner_key: str) -> str:
    return canonical_json(
        {
            "kind": "semantic_read_prepared",
            "semantic_effect_key": admission.paid.source.effect_key,
            "owner_effect_key": owner_key,
            "action_key": admission.action_key,
            "input_sha256": _read_input_hash(admission),
            "advisor_call_id": admission.paid.audit_call.call_id,
        }
    )


def prepare_semantic_read_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> StoredContinuationSnapshot:
    """Issue one durable read effect before the real read-only dispatcher."""
    admission = require_installed_semantic_action_on_connection(conn, running, selected=selected, effect_key=effect_key)
    if type(admission.action) is not ReadContinuationAction or admission.action_key is None:
        raise AuditIntegrityError("semantic read prepare requires its installed original action")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    owner_key = f"read:{admission.action_key}"
    input_hash = _read_input_hash(admission)
    prepared = _effect_snapshot(selected.payload_json, effect_key=owner_key, disposition="prepared", receipt_id=None)
    with conn.begin_nested():
        conn.execute(
            insert(effects).values(
                session_id=sid,
                operation_id=operation,
                effect_key=owner_key,
                input_sha256=input_hash,
                owner="read",
                action_json=installed_action_json(admission),
                disposition="prepared",
                creator_sequence=running.claim.segment_sequence,
                creator_generation=running.claim.ownership_generation,
            )
        )
        prepared = retain_action_execution_receipt_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            effect_key=owner_key,
            disposition="prepared",
            snapshot_json=prepared,
        )
        return append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=prepared,
            event_batch=(ContinuationEvent("checkpoint", _read_prepare_event(admission, owner_key)),),
            handoff=False,
        )


def _read_commit_event(admission: SemanticActionAdmission, result: ContinuationReadOwnerResult, owner_key: str) -> str:
    return canonical_json(
        {
            "kind": "semantic_read_committed",
            "semantic_effect_key": admission.paid.source.effect_key,
            "owner_effect_key": owner_key,
            "action_key": admission.action_key,
            "advisor_call_id": admission.paid.audit_call.call_id,
            "result_sha256": stable_hash(result.model_dump(mode="json")),
        }
    )


def commit_semantic_read_result_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
    result: ContinuationReadOwnerResult,
) -> SemanticReadCommitted:
    """Retain the original read result, real effect and action receipt once."""
    _owed_read_current(conn, running, selected)
    admission = _reprove_prepared_read(conn, running, selected, effect_key)
    action, owner_key = admission.action, admission.owner_effect_key
    if type(action) is not ReadContinuationAction or owner_key is None or type(result) is not ContinuationReadOwnerResult:
        raise AuditIntegrityError("semantic read settlement changed its original nominal result/action")
    arguments, error = parse_json_strict(action.arguments_json)
    if (
        error is not None
        or type(arguments) is not dict
        or result.tool_name != action.tool_name
        or result.arguments_json != canonical_json(arguments)
        or result.prescribing_provider_call_id != admission.paid.audit_call.call_id
    ):
        raise AuditIntegrityError("semantic read result changed its original prescribed call")
    re_admit_read_owner_result(result, env_ref_names=admission.paid.invocation.source.env_ref_names)
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    require_read_versions_on_connection(conn, session_id=sid, result=result, env_ref_names=admission.paid.invocation.source.env_ref_names)
    chunk = encode_private_chunk(codec="composer.owner-result.v1", canonical=canonical_json(result.model_dump(mode="json")))
    chunk_key = f"value:{chunk.reference.codec}:{chunk.reference.digest}"
    with conn.begin_nested():
        put_chunk(
            conn,
            session_id=sid,
            operation_id=operation,
            key=chunk_key,
            payload=chunk.canonical.encode("utf-8"),
            codec=chunk.reference.codec,
        )
        updated = conn.execute(
            update(effects)
            .where(
                effects.c.session_id == sid,
                effects.c.operation_id == operation,
                effects.c.effect_key == owner_key,
                effects.c.owner == "read",
                effects.c.disposition == "prepared",
                effects.c.action_json == installed_action_json(admission),
            )
            .values(disposition="committed", receipt_key=owner_key, result_chunk_key=chunk_key)
        )
        if updated.rowcount != 1:
            raise AuditIntegrityError("semantic read lost its one prepared current effect")
        advanced = _effect_snapshot(selected.payload_json, effect_key=owner_key, disposition="committed", receipt_id=owner_key)
        advanced = retain_action_execution_receipt_on_connection(
            conn,
            session_id=sid,
            operation_id=operation,
            effect_key=owner_key,
            disposition="committed",
            snapshot_json=advanced,
        )
        stored = append_complete_snapshot(
            conn,
            session_id=sid,
            operation_id=operation,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
            snapshot_json=advanced,
            event_batch=(ContinuationEvent("checkpoint", _read_commit_event(admission, result, owner_key)),),
            handoff=False,
        )
        new_root = decode_continuation_snapshot(stored.payload_json)
        if new_root.semantic_frontier is None:
            raise AuditIntegrityError("semantic read result lost its actual action receipt frontier")
        reference = new_root.semantic_frontier
        frontier_json = read_chunk(conn, session_id=sid, operation_id=operation, key=f"value:{reference.codec}:{reference.digest}")
        receipt_rows = decode_frontier_chunk(PrivateChunk(reference, frontier_json.decode("utf-8", errors="strict"))).action_receipts
        matches = tuple(
            item
            for item in current_action_receipts(receipt_rows)
            if item.action_key == admission.action_key and item.owner_receipt_id == owner_key
        )
        if len(matches) != 1 or matches[0].disposition != "committed":
            raise AuditIntegrityError("semantic read result lost its actual committed action receipt")
        return SemanticReadCommitted(stored, result, matches[0], owner_key)


def _owed_read_current(conn: Connection, running: ComposerOperationRunning, selected: StoredContinuationSnapshot) -> ContinuationFrameStack:
    """Keep an issued read's original result through Stop/elapsed action time.

    The only caller then proves the exact already-prepared read effect and
    original creator before any write. This does not issue new read work.
    """
    if type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("owed semantic read finish lost its original selected root")
    _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("owed semantic read cannot borrow an active P4 finish")
    actual = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
    )
    if actual.payload_json != selected.payload_json:
        raise ContinuationCASConflict("owed semantic read changed its complete original selected root")
    root = decode_continuation_snapshot(actual.payload_json)
    if (root.logical.session_id, root.logical.operation_id) != (str(running.claim.session_id), running.claim.operation_id):
        raise AuditIntegrityError("owed semantic read changed its original logical owner")
    return root


def read_committed_semantic_read_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticReadCommitted:
    """Cold/current reproof of the original result, owner effect and receipt."""
    from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source

    root = _current(conn, running, selected)
    frame = _advisor(root)
    if frame.phase != "decision_ready" or root.active_action_json is not None:
        raise AuditIntegrityError("committed semantic read requires its actual completed Read cut")
    original = semantic_recovery_source(conn, str(running.claim.session_id), running.claim.operation_id, selected).snapshot
    return _read_original_committed_semantic_read_data(
        conn, running.claim.session_id, running.claim.operation_id, original, effect_key
    ).committed


def read_original_committed_semantic_read_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    commit_checkpoint_id: str,
    semantic_effect_key: str,
) -> SemanticReadOriginal:
    """Historical read DATA after consume or claim change, never a dispatch grant."""
    if type(session_id) is not UUID or type(operation_id) is not str or type(commit_checkpoint_id) is not str:
        raise TypeError("semantic read cold history needs its original nominal checkpoint identity")
    selected = _historical_snapshot(conn, str(session_id), operation_id, checkpoint_id=commit_checkpoint_id)
    return _read_original_committed_semantic_read_data(conn, session_id, operation_id, selected, semantic_effect_key)


def _read_original_committed_semantic_read_data(
    conn: Connection,
    session_id: UUID,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticReadOriginal:
    root = decode_continuation_snapshot(selected.payload_json)
    sid, operation = str(session_id), operation_id
    if selected.cursor < 2:
        raise AuditIntegrityError("semantic read result has no original prepared cut")
    prepared = _historical_snapshot(conn, sid, operation, cursor=selected.cursor - 1)
    admission = _reprove_prepared_read(
        conn,
        None,
        prepared,
        effect_key,
        historical_identity=(session_id, operation),
        effect_disposition="committed",
    )
    action, owner_key = admission.action, admission.owner_effect_key
    if type(action) is not ReadContinuationAction or owner_key is None or admission.action_key is None:
        raise AuditIntegrityError("semantic cold read lost its original paid action")
    row = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == owner_key)
    ).one_or_none()
    if (
        row is None
        or row.owner != "read"
        or row.disposition != "committed"
        or row.action_json != installed_action_json(admission)
        or row.input_sha256 != _read_input_hash(admission)
        or row.receipt_key != owner_key
        or type(row.result_chunk_key) is not str
    ):
        raise AuditIntegrityError("semantic cold read changed its original committed effect")
    canonical = read_chunk(conn, session_id=sid, operation_id=operation, key=row.result_chunk_key).decode("utf-8", errors="strict")
    try:
        result = ContinuationReadOwnerResult.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("semantic cold read lost its original result codec") from error
    chunk = encode_private_chunk(codec="composer.owner-result.v1", canonical=canonical_json(result.model_dump(mode="json")))
    arguments, argument_error = parse_json_strict(action.arguments_json)
    if (
        row.result_chunk_key != f"value:{chunk.reference.codec}:{chunk.reference.digest}"
        or chunk.canonical != canonical
        or argument_error is not None
        or type(arguments) is not dict
        or result.tool_name != action.tool_name
        or result.arguments_json != canonical_json(arguments)
        or result.prescribing_provider_call_id != admission.paid.audit_call.call_id
    ):
        raise AuditIntegrityError("semantic cold read changed its original prescribed result")
    re_admit_read_owner_result(result, env_ref_names=admission.paid.invocation.source.env_ref_names)
    require_read_versions_on_connection(conn, session_id=sid, result=result, env_ref_names=admission.paid.invocation.source.env_ref_names)
    prepared_root = decode_continuation_snapshot(prepared.payload_json)
    reference = prepared_root.semantic_frontier
    if reference is None:
        raise AuditIntegrityError("semantic cold read lost its prepared receipt frontier")
    frontier = decode_frontier_chunk(
        PrivateChunk(
            reference,
            read_chunk(
                conn,
                session_id=sid,
                operation_id=operation,
                key=f"value:{reference.codec}:{reference.digest}",
            ).decode("utf-8", errors="strict"),
        )
    )
    receipt = ActionExecutionReceipt(admission.action_key, owner_key, "committed")
    completed_frontier = encode_frontier_chunk(record_action_receipt(frontier, receipt, actual_owner_receipt_id=owner_key))
    expected = decode_continuation_snapshot(
        _effect_snapshot(prepared.payload_json, effect_key=owner_key, disposition="committed", receipt_id=owner_key)
    ).model_copy(
        update={
            "cursor": selected.cursor,
            "semantic_frontier": completed_frontier.reference,
            "active_action_json": None,
            "active_action_source": None,
        }
    )
    if root != expected:
        raise AuditIntegrityError("semantic cold read changed its complete committed root/frontier")
    retained_frontier = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"value:{completed_frontier.reference.codec}:{completed_frontier.reference.digest}",
    )
    if retained_frontier != completed_frontier.canonical.encode("utf-8"):
        raise AuditIntegrityError("semantic cold read lost its exact committed receipt")
    _journal_data(conn, sid, operation, cursor=selected.cursor, tag="checkpoint", payload=_read_commit_event(admission, result, owner_key))
    return SemanticReadOriginal(admission, SemanticReadCommitted(selected, result, receipt, owner_key))


def require_semantic_review_execution_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticActionAdmission:
    admission = require_installed_semantic_action_on_connection(conn, running, selected=selected, effect_key=effect_key)
    if type(admission.action) is not ReviewContinuationAction or admission.action_key is None:
        raise AuditIntegrityError("semantic review execution lacks its original active END candidate")
    return admission


def _consumed_review_from_snapshot(
    conn: Connection,
    session_id: UUID,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    effect_key: str,
    *,
    current_transaction_proof: _CurrentTransactionReviewProof | None = None,
) -> SemanticActionAdmission:
    """Reprove one immutable Advisor-to-END bridge at its exact checkpoint."""
    sid, operation = str(session_id), operation_id
    if selected.cursor < 1:
        raise AuditIntegrityError("semantic Review has no installed paid predecessor")
    installed = _historical_snapshot(conn, sid, operation, cursor=selected.cursor - 1)
    if current_transaction_proof is None:
        admission = _reprove_installed_snapshot(conn, session_id, operation, installed, effect_key)
    else:
        _require_same_review_transaction(
            conn,
            current_transaction_proof.connection,
            current_transaction_proof.transaction,
            current_transaction_proof.installed,
            installed,
            current_transaction_proof.effect_key,
            effect_key,
        )
        admission = current_transaction_proof.admission
        if type(admission) is not SemanticActionAdmission or admission.paid.selected != installed:
            raise AuditIntegrityError("semantic Review reused an unproved original installed source")
    if type(admission.action) is not ReviewContinuationAction or admission.action_key is None:
        raise AuditIntegrityError("semantic Review bridge lacks its original paid action")
    frames = admission.expected_root.frames
    if len(frames) != 3 or type(frames[0]) is not ComposeFrame or type(frames[1]) is not EndFrame:
        raise AuditIntegrityError("semantic Review bridge lost its original Compose/END/Advisor stack")
    compose, end = frames[0], frames[1]
    original = admission.evidence.coverage.end_candidate
    if (
        original is None
        or admission.evidence.coverage.candidate_source != end.candidate
        or original.publication_source is not None
        or original.publication_record_json is not None
        or end.phase != "review_needed"
    ):
        raise AuditIntegrityError("semantic Review bridge changed its unpaid original END candidate")
    candidate = original.model_copy(
        update={
            "state": compose.state,
            "evidence": compose.evidence,
            "iteration": compose.iteration,
            "semantic_source": admission.paid.source,
            "semantic_decision_json": admission.paid.decision_json,
        }
    )
    encoded = encode_end_candidate(candidate)
    expected = admission.expected_root.model_copy(
        update={
            "cursor": selected.cursor,
            "frames": (compose, end.model_copy(update={"state": compose.state, "candidate": encoded.reference})),
        }
    )
    actual = decode_continuation_snapshot(selected.payload_json)
    if actual != expected:
        raise AuditIntegrityError("semantic Review bridge changed its complete current END root")
    retained_candidate = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"value:{encoded.reference.codec}:{encoded.reference.digest}",
    )
    if retained_candidate != encoded.canonical.encode("utf-8"):
        raise AuditIntegrityError("semantic Review bridge lost its exact rebound END candidate")
    bridge_key = f"semantic-review-source:{effect_key}"
    bridge_body = canonical_json(
        {
            "schema_version": "composer.semantic-review-source.v1",
            "session_id": sid,
            "operation_id": operation,
            "action_json": installed_action_json(admission),
            "action_key": admission.action_key,
            "source": admission.paid.source.model_dump(mode="json"),
            "decision_checkpoint_id": installed.checkpoint_id,
            "decision_cursor": installed.cursor,
            "consume_cursor": selected.cursor,
            "original_candidate": end.candidate.model_dump(mode="json"),
            "candidate": encoded.reference.model_dump(mode="json"),
            "candidate_ref": admission.evidence.coverage.candidate_ref,
            "evidence_binding": admission.evidence.coverage.evidence_binding,
        }
    )
    bridge_codec = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == sid,
            chunks.c.operation_id == operation,
            chunks.c.chunk_key == bridge_key,
        )
    ).scalar_one_or_none()
    if bridge_codec != "composer.semantic-review-source.v1" or read_chunk(
        conn, session_id=sid, operation_id=operation, key=bridge_key
    ) != bridge_body.encode("utf-8"):
        raise AuditIntegrityError("semantic Review bridge lost its exact original admission record")
    bridge_event = canonical_json(
        {
            "kind": "semantic_review_ready",
            "action_json": installed_action_json(admission),
            "action_key": admission.action_key,
            "source": admission.paid.source.model_dump(mode="json"),
            "decision_checkpoint_id": installed.checkpoint_id,
            "decision_cursor": installed.cursor,
            "original_candidate": end.candidate.model_dump(mode="json"),
            "candidate": encoded.reference.model_dump(mode="json"),
            "candidate_ref": admission.evidence.coverage.candidate_ref,
            "evidence_binding": admission.evidence.coverage.evidence_binding,
            "bridge_chunk_key": bridge_key,
        }
    )
    _journal_data(conn, sid, operation, cursor=selected.cursor, tag="checkpoint", payload=bridge_event)
    return replace(admission, review_candidate=candidate, review_candidate_source=encoded.reference)


def require_consumed_semantic_review_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticActionAdmission:
    """Current proof of the exact post-consume END candidate before effect issue."""
    _current(conn, running, selected)
    return _consumed_review_from_snapshot(conn, running.claim.session_id, running.claim.operation_id, selected, effect_key)


def require_dispatched_semantic_review_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticActionAdmission:
    """Current proof after one actual END effect and dispatched action receipt."""
    _current(conn, running, selected)
    return _reprove_dispatched_review(
        conn, running.claim.session_id, running.claim.operation_id, selected, effect_key, current_running=running
    )


def read_original_dispatched_semantic_review_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    dispatch_checkpoint_id: str,
    semantic_effect_key: str,
) -> SemanticActionAdmission:
    """Historical DATA for a finished END owner; never a current writer grant."""
    selected = _historical_snapshot(conn, str(session_id), operation_id, checkpoint_id=dispatch_checkpoint_id)
    return _reprove_dispatched_review(conn, session_id, operation_id, selected, semantic_effect_key, current_running=None)


def read_original_dispatched_semantic_review_after_end_overflow_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    dispatch_checkpoint_id: str,
    semantic_effect_key: str,
    names: frozenset[str],
    terminal: VerifiedCheckpointPaidOverflowTerminal,
) -> SemanticActionAdmission:
    """Reprove Review's original D under an independently cold-verified terminal."""
    from elspeth.web.sessions.continuation_checkpoint_paid_terminal import (
        VerifiedCheckpointPaidOverflowTerminal,
        read_v4_checkpoint_end_owed_overflow_terminal_on_connection,
    )

    if (
        type(terminal) is not VerifiedCheckpointPaidOverflowTerminal
        or type(names) is not frozenset
        or terminal.source.session_id != str(session_id)
        or terminal.source.operation_id != operation_id
        or terminal.phase != "end"
        or terminal.hold_state != "consumed"
        or terminal.receipt.representation != "retained_tail"
        or read_v4_checkpoint_end_owed_overflow_terminal_on_connection(
            conn,
            session_id=session_id,
            operation_id=operation_id,
            effect_key=terminal.source.effect_key,
            names=names,
        )
        != terminal
    ):
        raise AuditIntegrityError("semantic END historical Review lacks its exact paid-overflow terminal")
    selected = _historical_snapshot(conn, str(session_id), operation_id, checkpoint_id=dispatch_checkpoint_id)
    admission = _reprove_dispatched_review(
        conn,
        session_id,
        operation_id,
        selected,
        semantic_effect_key,
        current_running=None,
        paid_overflow_terminal=terminal,
    )
    if terminal.outer_effect_key != admission.owner_effect_key:
        raise AuditIntegrityError("semantic END historical Review lacks its exact paid-overflow terminal")
    return admission


def _reprove_dispatched_review(
    conn: Connection,
    session_id: UUID,
    operation_id: str,
    selected: StoredContinuationSnapshot,
    effect_key: str,
    *,
    current_running: ComposerOperationRunning | None,
    paid_overflow_terminal: VerifiedCheckpointPaidOverflowTerminal | None = None,
) -> SemanticActionAdmission:
    root = decode_continuation_snapshot(selected.payload_json)
    sid, operation = str(session_id), operation_id
    if selected.cursor < 2:
        raise AuditIntegrityError("semantic END dispatch has no original Review bridge")
    bridged = _historical_snapshot(conn, sid, operation, cursor=selected.cursor - 1)
    admission = _consumed_review_from_snapshot(conn, session_id, operation, bridged, effect_key)
    candidate = admission.review_candidate
    candidate_source = admission.review_candidate_source
    if candidate is None or candidate_source is None or admission.action_key is None:
        raise AuditIntegrityError("semantic END dispatch lost its actual selected candidate/action")
    owner_key = semantic_end_effect_key(candidate.response, admission.action_key)
    input_hash = stable_hash({"schema": "composer.end-review.v1", "candidate": candidate_source.digest})
    row = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == owner_key)
    ).one_or_none()
    if (
        row is None
        or row.owner != "composer_end"
        or row.disposition
        not in (
            ("dispatched",)
            if current_running is not None
            else ("unknown",)
            if paid_overflow_terminal is not None
            else ("dispatched", "committed", "refused")
        )
        or row.action_json != installed_action_json(admission)
        or row.input_sha256 != input_hash
        or row.provider_attempt_id is not None
    ):
        raise AuditIntegrityError("semantic END dispatch changed its actual bound effect/creator")
    creator = _creator_data(conn, sid, operation, row)
    if current_running is not None and (
        (row.creator_sequence, row.creator_generation)
        != (current_running.claim.segment_sequence, current_running.claim.ownership_generation)
        or any(value is not None for value in (row.receipt_key, row.result_chunk_key))
    ):
        raise AuditIntegrityError("semantic END dispatch changed its live creator/unsettled status")
    if current_running is None and creator.session_operation_epoch < 1:
        raise AuditIntegrityError("semantic END dispatch lost its original creator")
    expected = decode_continuation_snapshot(
        _effect_snapshot(bridged.payload_json, effect_key=owner_key, disposition="dispatched", receipt_id=None)
    ).model_copy(update={"cursor": selected.cursor})
    dispatched_frontier = encode_frontier_chunk(
        record_action_receipt(
            decode_frontier_chunk(admission.frontier_chunk),
            ActionExecutionReceipt(admission.action_key, owner_key, "dispatched"),
            actual_owner_receipt_id=owner_key,
        )
    )
    expected = expected.model_copy(update={"semantic_frontier": dispatched_frontier.reference})
    if root != expected:
        raise AuditIntegrityError("semantic END dispatch changed its complete current root/frontier")
    retained = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"value:{dispatched_frontier.reference.codec}:{dispatched_frontier.reference.digest}",
    )
    if retained != dispatched_frontier.canonical.encode("utf-8"):
        raise AuditIntegrityError("semantic END dispatch lost its actual action receipt")
    dispatch_event = canonical_json(
        {
            "owner": "composer_end",
            "transition": "end_review_dispatched",
            "effect_key": owner_key,
            "input_sha256": input_hash,
        }
    )
    _journal_data(conn, sid, operation, cursor=selected.cursor, tag="may_dispatch", payload=dispatch_event)
    return replace(admission, owner_effect_key=owner_key)


def require_semantic_whole_stop_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
    effect_key: str,
) -> SemanticActionAdmission:
    admission = require_installed_semantic_action_on_connection(conn, running, selected=selected, effect_key=effect_key)
    if admission.action is not None or admission.action_key is not None or not admission.evidence.coverage.complete:
        raise AuditIntegrityError("semantic whole Stop lost its exact closed original request")
    return admission


def read_original_semantic_review_for_advisor_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    original_candidate: EndContinuationCandidate,
) -> SemanticReviewCommitted:
    """Historical DATA locating the one actually completed clean or blocked Review.

    The candidate is an input to reproof, never a grant. The original installed
    Review journal, paid action, END result effect and completed candidate must
    all agree before the result can be used by a later whole Stop.
    """
    if type(session_id) is not UUID or type(operation_id) is not str or type(original_candidate) is not EndContinuationCandidate:
        raise TypeError("semantic Stop needs its original nominal reviewed candidate identity")
    source, decision_json = original_candidate.semantic_source, original_candidate.semantic_decision_json
    if source is None or decision_json is None:
        raise AuditIntegrityError("semantic Stop candidate lacks its original paid Review source")
    from elspeth.web.sessions.continuation_semantic_review_result import locate_original_semantic_review_result_on_connection

    review = locate_original_semantic_review_result_on_connection(
        conn,
        session_id=session_id,
        operation_id=operation_id,
        semantic_effect_key=source.effect_key,
        response_digest=original_candidate.response.digest,
    )
    if review.original_source != source or review.original_decision_json != decision_json:
        raise AuditIntegrityError("semantic Stop changed its original paid Review source or decision")
    owner_key = semantic_end_effect_key(original_candidate.response, review.action_key)
    completed_root = decode_continuation_snapshot(review.completed.payload_json)
    if len(completed_root.frames) != 2:
        raise AuditIntegrityError("semantic Stop lost its original completed Review END")
    completed_end = completed_root.frames[1]
    if (
        type(completed_end) is not EndFrame
        or completed_end.candidate != encode_end_candidate(original_candidate).reference
        or review.owner_effect_key != owner_key
        or review.semantic_effect_key != source.effect_key
    ):
        raise AuditIntegrityError("semantic Stop changed its one original completed Review result")
    return review


def read_original_semantic_review_for_stop_on_connection(
    conn: Connection,
    *,
    session_id: UUID,
    operation_id: str,
    original_candidate: EndContinuationCandidate,
) -> SemanticReviewCommitted:
    """Only a clean original Review with binding may back a whole Stop."""
    review = read_original_semantic_review_for_advisor_on_connection(
        conn, session_id=session_id, operation_id=operation_id, original_candidate=original_candidate
    )
    if review.outcome != "clean" or review.publication_binding is None:
        raise AuditIntegrityError("semantic whole Stop requires its original clean bound Review")
    return review


def require_semantic_publication_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    selected: StoredContinuationSnapshot,
) -> SemanticPublicationProof | None:
    """Reprove the consumed paid Stop at the current final SQL cut.

    An ordinary result without an actual semantic provider source returns None.
    A prior semantic attempt without this exact paid Stop bridge cannot publish.
    """
    from elspeth.web.sessions.continuation_semantic_recovery import semantic_recovery_source

    if type(selected) is not StoredContinuationSnapshot:
        raise AuditIntegrityError("semantic terminal publication lost its original selected snapshot")
    _require_current_continuation_running(conn, running, allow_cancel=False, allow_expired_segment=True)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("semantic terminal publication cannot borrow an active P4 finish")
    actual = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
    )
    if actual.payload_json != selected.payload_json:
        raise ContinuationCASConflict("semantic terminal publication changed its complete selected root")
    root = decode_continuation_snapshot(actual.payload_json)
    if (root.logical.session_id, root.logical.operation_id) != (str(running.claim.session_id), running.claim.operation_id):
        raise AuditIntegrityError("semantic terminal publication changed its current logical owner")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    semantic_effect = conn.execute(
        select(effects.c.effect_key)
        .where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key.like("provider:1:%"))
        .limit(1)
    ).scalar_one_or_none()
    semantic_input = conn.execute(
        select(chunks.c.chunk_key)
        .where(chunks.c.session_id == sid, chunks.c.operation_id == operation, chunks.c.chunk_key.like("semantic-provider-input:%"))
        .limit(1)
    ).scalar_one_or_none()
    has_semantic_history = semantic_effect is not None or semantic_input is not None
    if not has_semantic_history:
        checkpoint_keys = tuple(
            conn.execute(
                select(events.c.payload_chunk_key).where(
                    events.c.session_id == sid, events.c.operation_id == operation, events.c.tag == "checkpoint"
                )
            ).scalars()
        )
        semantic_kinds = frozenset(
            {
                "semantic_decision",
                "semantic_decision_extended",
                "semantic_primary_ready",
                "semantic_read_prepared",
                "semantic_read_committed",
                "semantic_review_ready",
                "semantic_whole_stop_ready",
                "semantic_current_graph",
            }
        )
        for key in checkpoint_keys:
            payload = read_chunk(conn, session_id=sid, operation_id=operation, key=key).decode("utf-8", errors="strict")
            event, error = parse_json_strict(payload)
            if error is not None or type(event) is not dict or canonical_json(event) != payload:
                raise AuditIntegrityError("semantic publication found a malformed original checkpoint")
            if event.get("kind") in semantic_kinds:
                has_semantic_history = True
                break
    if not has_semantic_history:
        if root.active_action_json is not None or root.active_action_source is not None:
            raise AuditIntegrityError("semantic publication lost its actual prescribed action source")
        if len(root.frames) != 2 or type(root.frames[-1]) is not EndFrame:
            return None
    if len(root.frames) != 2:
        raise AuditIntegrityError("semantic publication lost its current Compose/END pair")
    compose, end = root.frames
    if type(compose) is not ComposeFrame or type(end) is not EndFrame:
        raise AuditIntegrityError("semantic publication lost its current Compose/END pair")
    if has_semantic_history and end.phase != "publication_ready":
        raise AuditIntegrityError("semantic publication lacks its actual END publication phase")
    candidate_canonical = read_chunk(
        conn,
        session_id=sid,
        operation_id=operation,
        key=f"value:{end.candidate.codec}:{end.candidate.digest}",
    ).decode("utf-8", errors="strict")
    candidate = decode_end_candidate(PrivateChunk(end.candidate, candidate_canonical))
    if candidate.semantic_source is None:
        if has_semantic_history:
            raise AuditIntegrityError("semantic publication omitted its original paid Stop source")
        return None
    if not has_semantic_history or root.semantic_frontier is None or selected.cursor < 1:
        raise AuditIntegrityError("semantic publication invented a paid Stop source")
    if (
        root.active_action_json is not None
        or root.active_action_source is not None
        or root.pending_settlement_ids
        or root.pending_owner_results
        or any(item.disposition in ("prepared", "dispatched", "unknown") for item in root.effect_frontier)
    ):
        raise AuditIntegrityError("semantic publication has outstanding action or physical custody")
    physical_effects = tuple(
        conn.execute(
            select(effects.c.effect_key, effects.c.disposition, effects.c.receipt_key).where(
                effects.c.session_id == sid, effects.c.operation_id == operation
            )
        ).all()
    )
    if {(row.effect_key, row.disposition, row.receipt_key) for row in physical_effects} != {
        (item.effect_id, item.disposition, item.receipt_id) for item in root.effect_frontier
    }:
        raise AuditIntegrityError("semantic publication found an off-frontier or changed physical effect")
    original_selected = semantic_recovery_source(conn, sid, operation, selected).snapshot
    installed = _historical_snapshot(conn, sid, operation, cursor=original_selected.cursor - 1)
    admission = _reprove_installed_snapshot(conn, running.claim.session_id, operation, installed, candidate.semantic_source.effect_key)
    if (
        admission.action is not None
        or admission.action_key is not None
        or not admission.evidence.coverage.complete
        or admission.paid.decision.decision != "STOP_CONFIRMED"
        or admission.paid.decision.scope != "whole_request"
        or candidate.semantic_source != admission.paid.source
        or candidate.semantic_decision_json != admission.paid.decision_json
    ):
        raise AuditIntegrityError("semantic publication lost its complete original paid whole Stop")
    frames = admission.expected_root.frames
    if len(frames) != 3:
        raise AuditIntegrityError("semantic publication lost its original END result carrier")
    prior_compose, prior_end = frames[0], frames[1]
    if type(prior_compose) is not ComposeFrame or type(prior_end) is not EndFrame:
        raise AuditIntegrityError("semantic publication lost its original END result carrier")
    original = admission.evidence.coverage.end_candidate
    if (
        original is None
        or admission.evidence.coverage.candidate_source != prior_end.candidate
        or prior_end.phase != "publication_ready"
        or prior_end.publication_binding is None
        or (original.state, original.evidence, original.iteration)
        != (
            admission.paid.invocation.source.state.reference,
            admission.paid.invocation.source.evidence.reference,
            admission.paid.invocation.source.iteration.reference,
        )
    ):
        raise AuditIntegrityError("semantic publication changed its original reviewed END candidate")
    review = read_original_semantic_review_for_stop_on_connection(
        conn, session_id=running.claim.session_id, operation_id=operation, original_candidate=original
    )
    review_key = review.owner_effect_key
    reviewed_root = decode_continuation_snapshot(review.completed.payload_json)
    if len(reviewed_root.frames) != 2:
        raise AuditIntegrityError("semantic publication lost its original clean END review result")
    reviewed_end = reviewed_root.frames[1]
    if (
        type(reviewed_end) is not EndFrame
        or reviewed_end.candidate != prior_end.candidate
        or reviewed_end.publication_binding != prior_end.publication_binding
        or review.completed.cursor >= installed.cursor
        or review.publication_binding is None
        or review.publication_binding != prior_end.publication_binding
    ):
        raise AuditIntegrityError("semantic publication lost its original clean END review result")
    review_row = conn.execute(
        select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation, effects.c.effect_key == review_key)
    ).one_or_none()
    if (
        review_row is None
        or review_row.owner != "composer_end"
        or review_row.disposition != "committed"
        or review_row.receipt_key != review_key
        or type(review_row.result_chunk_key) is not str
    ):
        raise AuditIntegrityError("semantic publication lost its committed END result effect")
    rebound = original.model_copy(
        update={
            "state": compose.state,
            "evidence": compose.evidence,
            "iteration": compose.iteration,
            "semantic_source": admission.paid.source,
            "semantic_decision_json": admission.paid.decision_json,
        }
    )
    encoded = encode_end_candidate(rebound)
    expected = admission.expected_root.model_copy(
        update={
            "cursor": selected.cursor,
            "frames": (
                prior_compose,
                prior_end.model_copy(update={"state": compose.state, "candidate": encoded.reference}),
            ),
        }
    )
    if root != expected or candidate != rebound or end.candidate != encoded.reference or compose != prior_compose:
        raise AuditIntegrityError("semantic publication changed its complete consumed Stop root")
    retained_candidate = read_chunk(
        conn, session_id=sid, operation_id=operation, key=f"value:{encoded.reference.codec}:{encoded.reference.digest}"
    )
    if retained_candidate != encoded.canonical.encode("utf-8"):
        raise AuditIntegrityError("semantic publication lost its exact rebound END candidate")
    bridge_key = f"semantic-whole-stop-source:{admission.paid.source.effect_key}"
    bridge_body = canonical_json(
        {
            "schema_version": "composer.semantic-whole-stop-source.v1",
            "session_id": sid,
            "operation_id": operation,
            "decision_checkpoint_id": installed.checkpoint_id,
            "decision_cursor": installed.cursor,
            "consume_cursor": original_selected.cursor,
            "source": admission.paid.source.model_dump(mode="json"),
            "decision_json": admission.paid.decision_json,
            "original_candidate": prior_end.candidate.model_dump(mode="json"),
            "candidate": encoded.reference.model_dump(mode="json"),
            "review_effect_key": review_key,
            "review_result_chunk_key": review_row.result_chunk_key,
            "review_checkpoint_id": review.completed.checkpoint_id,
            "review_cursor": review.completed.cursor,
            "publication_binding": review.publication_binding,
        }
    )
    codec = conn.execute(
        select(chunks.c.codec).where(
            chunks.c.session_id == sid,
            chunks.c.operation_id == operation,
            chunks.c.chunk_key == bridge_key,
        )
    ).scalar_one_or_none()
    if codec != "composer.semantic-whole-stop-source.v1" or read_chunk(
        conn, session_id=sid, operation_id=operation, key=bridge_key
    ) != bridge_body.encode("utf-8"):
        raise AuditIntegrityError("semantic publication lost its immutable paid Stop bridge")
    bridge_event = canonical_json(
        {
            "kind": "semantic_whole_stop_ready",
            "decision_checkpoint_id": installed.checkpoint_id,
            "decision_cursor": installed.cursor,
            "source": admission.paid.source.model_dump(mode="json"),
            "decision_json": admission.paid.decision_json,
            "original_candidate": prior_end.candidate.model_dump(mode="json"),
            "candidate": encoded.reference.model_dump(mode="json"),
            "review_effect_key": review_key,
            "review_result_chunk_key": review_row.result_chunk_key,
            "review_checkpoint_id": review.completed.checkpoint_id,
            "review_cursor": review.completed.cursor,
            "publication_binding": review.publication_binding,
            "bridge_chunk_key": bridge_key,
        }
    )
    _journal_data(conn, sid, operation, cursor=original_selected.cursor, tag="checkpoint", payload=bridge_event)
    return SemanticPublicationProof(admission, candidate, review)
