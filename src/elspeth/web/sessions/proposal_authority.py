"""Pipeline proposal audit payloads and persisted authority verification."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Connection, func, select

from elspeth.contracts.composer_audit import ComposerToolStatus, PipelineDispatchAuditPayload
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import is_lower_sha256_hex, stable_hash
from elspeth.web.compartments import is_compartment_id
from elspeth.web.composer.authority_hashing import composer_authority_hash, project_composer_authority_payload
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import (
    AbsentBase,
    PipelineProposal,
    PlannerSurface,
    PresentBase,
    ProposalBase,
    composition_content_hash,
    reviewed_anchor_hash,
)
from elspeth.web.coordination.repository import (
    _ForkCreationTransaction,
)
from elspeth.web.sessions._persist_payload import RedactedToolRow
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.guided_audit import (
    is_authentic_guided_synthetic_invocation,
)
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
    guided_operations_table,
    proposal_events_table,
)
from elspeth.web.sessions.protocol import (
    GUIDED_PROPOSAL_REBASE_REASONS,
    AuthoritativeCompositionProposal,
    AuthoritativePipelineProposal,
    CompositionProposalRecord,
    CompositionStateData,
    GuidedOperationKind,
    GuidedProposalRebaseReason,
    GuidedStateOperationCommand,
    PipelineDispatchRecovery,
    PipelineProposalPublicMetadata,
    PipelineProposalRejectionReason,
    ProposalEventRecord,
    SessionForkCreationTransaction,
    ToolCallIDMismatchError,
)
from elspeth.web.sessions.time_normalization import restore_utc

if TYPE_CHECKING:
    from elspeth.web.composer.guided.state_machine import DeferredStageIntent, GuidedSession
    from elspeth.web.sessions.service import SessionServiceImpl

_PROPOSAL_COMPOSER_PROVENANCE_FIELDS = (
    "composer_model_identifier",
    "composer_model_version",
    "composer_provider",
    "composer_skill_hash",
    "tool_arguments_hash",
)
_PIPELINE_CREATED_SCHEMA = "pipeline_proposal_created.v1"
_TOOL_PROPOSAL_CREATED_SCHEMA = "tool_proposal_created.v1"
_PIPELINE_CREATED_FIELDS = frozenset(
    {
        "schema",
        "tool_call_id",
        "tool_name",
        "status",
        "surface",
        "draft_hash",
        "base",
        "reviewed_anchor_hash",
        "repair_count",
        "skill_hash",
        "covered_deferred_intent_ids",
        "supersedes_draft_hash",
        "supersedes_proposal_id",
        "custody_result",
        "private_arguments_hash",
        "provenance_hash",
        "audit_payload_hash",
    }
)
_TOOL_PROPOSAL_CREATED_FIELDS = frozenset({"schema", "tool_call_id", "tool_name", "status"})
_PIPELINE_REJECTION_REASONS = frozenset(
    {
        "operator_rejected",
        "candidate_executor_mismatch",
        "validation_failed",
        "policy_changed",
        "base_conflict",
        "request_cancelled",
        "superseded",
        "guided_exit",
    }
)

_PIPELINE_ACCEPTED_FIELDS = frozenset(
    {
        "schema",
        "tool_call_id",
        "tool_name",
        "status",
        "outcome",
        "draft_hash",
        "committed_state_id",
        "committed_state_content_hash",
        "final_composer_metadata_hash",
        "dispatch",
    }
)

_PIPELINE_REJECTED_FIELDS = frozenset(
    {
        "schema",
        "tool_call_id",
        "tool_name",
        "status",
        "outcome",
        "reason_code",
        "draft_hash",
        "dispatch",
    }
)


class _GuidedOperationEventValues(TypedDict):
    """One immutable guided_operation_events row, built without owning DML."""

    session_id: str
    operation_id: str
    sequence: int
    event_kind: Literal["claimed", "renewed", "taken_over", "completed", "failed"]
    actor: str
    attempt: int
    prior_attempt: int | None
    lease_expires_at: datetime | None
    request_hash: str
    failure_audit_cohort: dict[str, object] | None
    occurred_at: datetime


class _PipelineCreatedEventPayload(TypedDict):
    schema: str
    tool_call_id: str
    tool_name: str
    status: str
    surface: str
    draft_hash: str
    base: dict[str, str]
    reviewed_anchor_hash: str
    repair_count: int
    skill_hash: str
    covered_deferred_intent_ids: list[str]
    supersedes_draft_hash: str | None
    supersedes_proposal_id: str | None
    custody_result: str
    private_arguments_hash: str
    provenance_hash: str
    audit_payload_hash: str


class _PipelineAcceptedEventPayload(TypedDict):
    schema: str
    tool_call_id: str
    tool_name: str
    status: str
    outcome: str
    draft_hash: str
    committed_state_id: str
    committed_state_content_hash: str
    final_composer_metadata_hash: str
    dispatch: PipelineDispatchAuditPayload


class _PipelineRejectedEventPayload(TypedDict):
    schema: str
    tool_call_id: str
    tool_name: str
    status: str
    outcome: str
    reason_code: PipelineProposalRejectionReason
    draft_hash: str
    dispatch: PipelineDispatchAuditPayload | None


class _PipelineRebasedEventPayload(TypedDict):
    """One appended, non-terminal anchor move of a still-pending proposal.

    ``from_state_id``/``to_state_id`` make the rebase chain a linked list the
    restore walks deterministically (unique successor per hop, every event
    consumed) rather than a timestamp sort, which cannot see a fork and
    breaks on same-microsecond ties. ``composition_content_hash`` is the base
    content hash that did NOT change across the move — the admission that
    keeps a rebase from laundering a graph that actually moved.

    ``reason_code`` names WHICH settlement moved the anchor. Without it the
    four carrying gestures emit byte-identical payloads modulo ids and the
    proposal's own trail cannot say what happened to it — the same
    completeness ``_pipeline_rejected_payload.reason_code`` exists to give
    its terminal sibling. It is a closed vocabulary, not the settlement's
    diagnostic origin string: two carrying sites settle the same
    ``guided_respond`` operation kind, so that string does not discriminate
    them, and it embeds an operation UUID this payload has no business
    carrying.
    """

    schema: str
    tool_call_id: str
    tool_name: str
    status: str
    outcome: str
    reason_code: GuidedProposalRebaseReason
    draft_hash: str
    from_state_id: str
    to_state_id: str
    composition_content_hash: str


def _pipeline_private_arguments_hash(arguments: Mapping[str, Any]) -> str:
    return stable_hash(
        {
            "schema": "composer.pipeline-proposal-private-arguments.v1",
            "arguments": project_composer_authority_payload(arguments),
        }
    )


def _pipeline_audit_payload_hash(
    *,
    summary: str,
    rationale: str,
    affects: Sequence[str],
    arguments_redacted_json: Mapping[str, Any],
) -> str:
    return stable_hash(
        {
            "schema": "composer.pipeline-proposal-audit-payload.v1",
            "summary": summary,
            "rationale": rationale,
            "affects": list(affects),
            "arguments_redacted_json": project_composer_authority_payload(arguments_redacted_json),
        }
    )


def _pipeline_provenance_hash(
    *,
    user_message_id: UUID | None,
    composer_model_identifier: str,
    composer_model_version: str,
    composer_provider: str,
    composer_skill_hash: str,
    tool_arguments_hash: str,
) -> str:
    return stable_hash(
        {
            "schema": "composer.pipeline-proposal-provenance.v1",
            "user_message_id": str(user_message_id) if user_message_id is not None else None,
            "composer_model_identifier": composer_model_identifier,
            "composer_model_version": composer_model_version,
            "composer_provider": composer_provider,
            "composer_skill_hash": composer_skill_hash,
            "tool_arguments_hash": tool_arguments_hash,
        }
    )


def _proposal_record_from_row(row: Any) -> CompositionProposalRecord:
    return CompositionProposalRecord(
        id=UUID(row.id),
        session_id=UUID(row.session_id),
        tool_call_id=row.tool_call_id,
        user_message_id=UUID(row.user_message_id) if row.user_message_id is not None else None,
        composer_model_identifier=row.composer_model_identifier,
        composer_model_version=row.composer_model_version,
        composer_provider=row.composer_provider,
        composer_skill_hash=row.composer_skill_hash,
        tool_arguments_hash=row.tool_arguments_hash,
        tool_name=row.tool_name,
        status=row.status,
        summary=row.summary,
        rationale=row.rationale,
        affects=tuple(row.affects),
        arguments_json=row.arguments_json,
        arguments_redacted_json=row.arguments_redacted_json,
        base_state_id=UUID(row.base_state_id) if row.base_state_id else None,
        committed_state_id=UUID(row.committed_state_id) if row.committed_state_id else None,
        audit_event_id=UUID(row.audit_event_id) if row.audit_event_id else None,
        created_at=restore_utc(row.created_at),
        updated_at=restore_utc(row.updated_at),
    )


def _proposal_event_record_from_row(row: Any) -> ProposalEventRecord:
    return ProposalEventRecord(
        id=UUID(row.id),
        session_id=UUID(row.session_id),
        proposal_id=UUID(row.proposal_id) if row.proposal_id else None,
        event_type=row.event_type,
        actor=row.actor,
        payload=row.payload,
        created_at=restore_utc(row.created_at),
    )


def _proposal_base_payload(base: AbsentBase | PresentBase) -> dict[str, str]:
    if type(base) is AbsentBase:
        return {"kind": "absent"}
    if type(base) is PresentBase:
        return {
            "kind": "present",
            "state_id": str(base.state_id),
            "composition_content_hash": base.composition_content_hash,
        }
    raise AuditIntegrityError("pipeline proposal base must be explicitly absent or present")


def _pipeline_created_payload(
    *,
    plan: PipelinePlanResult,
    user_message_id: UUID | None,
    composer_model_identifier: str,
    composer_model_version: str,
    composer_provider: str,
    summary: str,
    rationale: str,
    affects: Sequence[str],
    arguments_redacted_json: Mapping[str, Any],
    supersedes_proposal_id: UUID | None,
) -> _PipelineCreatedEventPayload:
    proposal = plan.proposal
    tool_arguments_hash = composer_authority_hash(proposal.pipeline)
    payload: _PipelineCreatedEventPayload = {
        "schema": _PIPELINE_CREATED_SCHEMA,
        "tool_call_id": plan.tool_call_id,
        "tool_name": "set_pipeline",
        "status": "pending",
        "surface": proposal.surface.value,
        "draft_hash": proposal.draft_hash,
        "base": _proposal_base_payload(proposal.base),
        "reviewed_anchor_hash": proposal.reviewed_anchor_hash,
        "repair_count": proposal.repair_count,
        "skill_hash": proposal.skill_hash,
        "covered_deferred_intent_ids": list(proposal.covered_deferred_intent_ids),
        "supersedes_draft_hash": proposal.supersedes_draft_hash,
        "supersedes_proposal_id": str(supersedes_proposal_id) if supersedes_proposal_id is not None else None,
        "custody_result": plan.custody_result,
        "private_arguments_hash": _pipeline_private_arguments_hash(proposal.pipeline),
        "audit_payload_hash": _pipeline_audit_payload_hash(
            summary=summary,
            rationale=rationale,
            affects=affects,
            arguments_redacted_json=arguments_redacted_json,
        ),
        "provenance_hash": _pipeline_provenance_hash(
            user_message_id=user_message_id,
            composer_model_identifier=composer_model_identifier,
            composer_model_version=composer_model_version,
            composer_provider=composer_provider,
            composer_skill_hash=proposal.skill_hash,
            tool_arguments_hash=tool_arguments_hash,
        ),
    }
    return payload


def _composition_state_data_content_hash(state: CompositionStateData) -> str:
    return composer_authority_hash(
        {
            "sources": state.sources,
            "nodes": state.nodes,
            "edges": state.edges,
            "outputs": state.outputs,
            "metadata": state.metadata_,
        }
    )


def _valid_compartment_ingress_metadata(value: object) -> bool:
    """Keep guided checkpoint ingress in the exact, content-free evidence shape."""
    if type(value) is not dict or set(value) != {"text_sha256", "foreign_compartment_ids"}:
        return False
    digest = value["text_sha256"]
    foreign_ids = value["foreign_compartment_ids"]
    return (
        is_lower_sha256_hex(digest)
        and type(foreign_ids) is list
        and all(type(identifier) is str and is_compartment_id(identifier) for identifier in foreign_ids)
        and foreign_ids == sorted(set(foreign_ids))
    )


def _valid_chat_ingress_inputs_metadata(value: object) -> bool:
    """Accept only ordered, distinct durable chat IDs with exact content-free evidence."""
    if type(value) is not list:
        return False
    seen: set[str] = set()
    for item in value:
        if type(item) is not dict or set(item) != {"message_id", "text_sha256", "foreign_compartment_ids"}:
            return False
        message_id = item["message_id"]
        if type(message_id) is not str:
            return False
        try:
            if str(UUID(message_id)) != message_id:
                return False
        except ValueError:
            return False
        if message_id in seen or not _valid_compartment_ingress_metadata(
            {"text_sha256": item["text_sha256"], "foreign_compartment_ids": item["foreign_compartment_ids"]}
        ):
            return False
        seen.add(message_id)
    return True


def _final_composer_metadata_hash(metadata: Mapping[str, Any] | None) -> str:
    return stable_hash(
        {
            "schema": "composer.pipeline-final-metadata.v1",
            "metadata": deep_thaw(metadata),
        }
    )


def _pipeline_accepted_payload(
    *,
    authority: AuthoritativePipelineProposal,
    state_id: str,
    state_content_hash: str,
    final_composer_metadata: Mapping[str, Any] | None,
    dispatch: PipelineDispatchAuditBinding,
) -> _PipelineAcceptedEventPayload:
    return {
        "schema": "pipeline_proposal_accepted.v1",
        "tool_call_id": authority.row.tool_call_id,
        "tool_name": "set_pipeline",
        "status": "committed",
        "outcome": "accepted",
        "draft_hash": authority.proposal.draft_hash,
        "committed_state_id": state_id,
        "committed_state_content_hash": state_content_hash,
        "final_composer_metadata_hash": _final_composer_metadata_hash(final_composer_metadata),
        "dispatch": dispatch.to_dict(),
    }


def _validated_pipeline_rejection_reason(value: object) -> PipelineProposalRejectionReason:
    if type(value) is not str or value not in _PIPELINE_REJECTION_REASONS:
        raise AuditIntegrityError("pipeline proposal rejection reason is outside the closed vocabulary")
    return cast(PipelineProposalRejectionReason, value)


_PIPELINE_REBASED_SCHEMA = "pipeline_proposal_rebased.v1"
_PIPELINE_REBASED_FIELDS = frozenset(
    {
        "schema",
        "tool_call_id",
        "tool_name",
        "status",
        "outcome",
        "reason_code",
        "draft_hash",
        "from_state_id",
        "to_state_id",
        "composition_content_hash",
    }
)


def _validated_guided_proposal_rebase_reason(value: object) -> GuidedProposalRebaseReason:
    if type(value) is not str or value not in GUIDED_PROPOSAL_REBASE_REASONS:
        raise AuditIntegrityError("pipeline proposal rebase reason is outside the closed vocabulary")
    return cast(GuidedProposalRebaseReason, value)


def _pipeline_rebased_payload(
    *,
    authority: AuthoritativePipelineProposal,
    reason: GuidedProposalRebaseReason,
    from_state_id: UUID,
    to_state_id: UUID,
    composition_content_hash_value: str,
) -> _PipelineRebasedEventPayload:
    return {
        "schema": _PIPELINE_REBASED_SCHEMA,
        "tool_call_id": authority.row.tool_call_id,
        "tool_name": "set_pipeline",
        "status": "pending",
        "outcome": "rebased",
        "reason_code": _validated_guided_proposal_rebase_reason(reason),
        "draft_hash": authority.proposal.draft_hash,
        "from_state_id": str(from_state_id),
        "to_state_id": str(to_state_id),
        "composition_content_hash": composition_content_hash_value,
    }


def _pipeline_rejected_payload(
    *,
    authority: AuthoritativePipelineProposal,
    reason: PipelineProposalRejectionReason,
    dispatch: PipelineDispatchAuditBinding | None,
) -> _PipelineRejectedEventPayload:
    reason = _validated_pipeline_rejection_reason(reason)
    # ``guided_exit`` shares the "superseded" outcome family: both are
    # invalidation-by-state-transition, not a fault ("failed") and not an
    # operator verdict on the content ("rejected"). The reason_code keeps
    # the two distinguishable in the audit trail.
    outcome = "rejected" if reason == "operator_rejected" else "superseded" if reason in ("superseded", "guided_exit") else "failed"
    return {
        "schema": "pipeline_proposal_rejected.v1",
        "tool_call_id": authority.row.tool_call_id,
        "tool_name": "set_pipeline",
        "status": "rejected",
        "outcome": outcome,
        "reason_code": reason,
        "draft_hash": authority.proposal.draft_hash,
        "dispatch": dispatch.to_dict() if dispatch is not None else None,
    }


def _persisted_pipeline_dispatch_content_hashes(
    conn: Connection,
    *,
    session_id: str,
    dispatch: PipelineDispatchAuditBinding,
) -> tuple[str, ...]:
    """Return content hashes from exact durable redacted dispatch envelopes."""
    rows = conn.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.session_id == session_id)).fetchall()
    matches: list[str] = []
    for row in rows:
        tool_calls = row.tool_calls
        if type(tool_calls) is not list:
            continue
        for envelope in tool_calls:
            try:
                recovery = _pipeline_dispatch_recovery_from_envelope(
                    envelope,
                    expected_tool_call_id=dispatch.tool_call_id,
                )
            except AuditIntegrityError as exc:
                raise AuditIntegrityError("pipeline dispatch audit evidence is malformed") from exc
            if recovery is None:
                continue
            if recovery.binding != dispatch:
                raise AuditIntegrityError("pipeline successful dispatch evidence does not match the terminal binding")
            matches.append(recovery.executor_content_hash)
    return tuple(matches)


def _pipeline_dispatch_recovery_from_envelope(
    envelope: object,
    *,
    expected_tool_call_id: str,
) -> PipelineDispatchRecovery | None:
    """Restore one successful same-call dispatch; ignore unrelated and failed attempts."""
    if type(envelope) is not dict:
        return None
    invocation = envelope["invocation"] if "invocation" in envelope else None
    if type(invocation) is not dict:
        return None
    tool_call_id = invocation["tool_call_id"] if "tool_call_id" in invocation else None
    if tool_call_id != expected_tool_call_id:
        return None
    envelope_kind = envelope["_kind"] if "_kind" in envelope else None
    if envelope_kind != "audit":
        raise AuditIntegrityError("pipeline dispatch envelope kind is malformed")
    tool_name = invocation["tool_name"] if "tool_name" in invocation else None
    if tool_name != "set_pipeline":
        raise AuditIntegrityError("pipeline dispatch call id is bound to a different tool")
    raw_status = invocation["status"] if "status" in invocation else None
    if type(raw_status) is not str:
        raise AuditIntegrityError("pipeline dispatch status is malformed")
    try:
        status = ComposerToolStatus(raw_status)
    except (TypeError, ValueError) as exc:
        raise AuditIntegrityError("pipeline dispatch status is malformed") from exc
    if status is not ComposerToolStatus.SUCCESS:
        return None
    binding = PipelineDispatchAuditBinding.from_persisted_envelope(envelope)
    result_canonical = invocation["result_canonical"] if "result_canonical" in invocation else None
    if type(result_canonical) is not str:
        raise AuditIntegrityError("pipeline dispatch result canonical is malformed")
    try:
        result_payload = json.loads(result_canonical)
    except json.JSONDecodeError as exc:
        raise AuditIntegrityError("pipeline dispatch result canonical is malformed") from exc
    if type(result_payload) is not dict:
        raise AuditIntegrityError("pipeline dispatch result payload is malformed")
    content_hash_schema = result_payload["pipeline_content_hash_schema"] if "pipeline_content_hash_schema" in result_payload else None
    if content_hash_schema != "composer.pipeline-dispatch-result.v1":
        raise AuditIntegrityError("pipeline dispatch result content schema is malformed")
    content_hash = result_payload["pipeline_content_hash"] if "pipeline_content_hash" in result_payload else None
    if not is_lower_sha256_hex(content_hash):
        raise AuditIntegrityError("pipeline dispatch result content hash is malformed")
    return PipelineDispatchRecovery(binding=binding, executor_content_hash=content_hash)


def _verify_guided_deferred_message_authority(
    conn: Connection,
    *,
    session_id: str,
    guided: Any,
) -> None:
    """Re-resolve every deferred intent's private user row under settlement lock."""

    message_ids = tuple(intent.originating_message_id for intent in guided.deferred_intents)
    if not message_ids:
        return
    rows = conn.execute(
        select(chat_messages_table.c.id, chat_messages_table.c.role, chat_messages_table.c.content)
        .where(chat_messages_table.c.session_id == session_id)
        .where(chat_messages_table.c.id.in_(message_ids))
    ).fetchall()
    rows_by_id = {row.id: row for row in rows}
    if set(rows_by_id) != set(message_ids):
        raise AuditIntegrityError("guided deferred intent message is missing or cross-session")
    for intent in guided.deferred_intents:
        row = rows_by_id[intent.originating_message_id]
        if row.role != "user":
            raise AuditIntegrityError("guided deferred intent must originate from a user message")
        if stable_hash(row.content) != intent.message_content_hash:
            raise AuditIntegrityError("guided deferred intent message content hash mismatch")


def _verify_guided_correction_message_authority(
    conn: Connection,
    *,
    session_id: str,
    guided: Any,
) -> None:
    """Re-resolve every private wire-correction row under settlement lock."""

    message_ids = tuple(str(reference.message_id) for reference in guided.correction_messages)
    if not message_ids:
        return
    rows = conn.execute(
        select(chat_messages_table.c.id, chat_messages_table.c.role, chat_messages_table.c.content)
        .where(chat_messages_table.c.session_id == session_id)
        .where(chat_messages_table.c.id.in_(message_ids))
    ).fetchall()
    rows_by_id = {row.id: row for row in rows}
    if set(rows_by_id) != set(message_ids):
        raise AuditIntegrityError("guided correction message is missing or cross-session")
    for reference in guided.correction_messages:
        row = rows_by_id[str(reference.message_id)]
        if row.role != "user":
            raise AuditIntegrityError("guided correction must originate from a user message")
        if stable_hash(row.content) != reference.content_hash:
            raise AuditIntegrityError("guided correction message content hash mismatch")


def _verified_guided_root_message_row(
    conn: Connection,
    *,
    service: Any,
    session_id: str,
    message_id: str,
) -> Any:
    """Read one guided root intent's authority rows on an ordinary connection.

    The three rows — the immutable completed ``guided_start`` OR
    ``guided_convert`` operation that names the message, that operation's
    result checkpoint, and the chat row itself — are read under this session's
    scope and handed to :func:`_verified_guided_root_authority`, which is the
    single judge. Returns the verified ``chat_messages`` row so callers that
    must hand the content on do not re-read it.
    """

    operations = conn.execute(
        select(guided_operations_table)
        .where(guided_operations_table.c.session_id == session_id)
        .where(guided_operations_table.c.kind.in_(("guided_start", "guided_convert")))
        .where(guided_operations_table.c.status == "completed")
        .where(guided_operations_table.c.originating_message_id == message_id)
        .where(guided_operations_table.c.result_kind == "composition_state")
    ).fetchall()
    state_row = None
    if len(operations) == 1:
        state_row = conn.execute(
            select(composition_states_table)
            .where(composition_states_table.c.session_id == session_id)
            .where(composition_states_table.c.id == operations[0].result_state_id)
        ).one_or_none()
    message_row = conn.execute(
        select(chat_messages_table).where(chat_messages_table.c.session_id == session_id).where(chat_messages_table.c.id == message_id)
    ).one_or_none()
    return _verified_guided_root_authority(
        service=service,
        session_id=session_id,
        message_id=message_id,
        message_row=message_row,
        operations=tuple(operations),
        state_row=state_row,
    )


def _verified_guided_root_authority(
    *,
    service: Any,
    session_id: str,
    message_id: str,
    message_row: Any,
    operations: tuple[Any, ...],
    state_row: Any,
) -> Any:
    """Judge one guided root intent from rows a caller's own reader supplied.

    The ordinary-connection reader (:func:`_verified_guided_root_message_row`)
    and the fork transaction's closed reader
    (``SessionForkCreationTransaction.read_parent_guided_root_authority``) both
    hand their three rows here, so the KIND, PROFILE and HASH derivation exists
    exactly once. Splitting it left the fork branch hardcoding
    ``kind="guided_start"`` and ``profile="live"`` while the ordinary branch
    read both off the checkpoint — a fork of a converted or non-``live`` parent
    then re-derived a different hash and was refused.

    ``operations`` must already be scoped to COMPLETED ``guided_start`` /
    ``guided_convert`` rows whose ``originating_message_id`` is ``message_id``
    and whose ``result_kind`` is ``composition_state``; ``state_row`` is that
    operation's result checkpoint and ``message_row`` the named chat row, each
    read under the caller's own session scoping. Returns the verified
    ``chat_messages`` row so callers that must hand the content on do not
    re-read it.

    Two facts are read off the RESULT CHECKPOINT rather than assumed:

    * the operation KIND, which selects the request DTO the hash was taken
      over (``StartGuidedRequest`` carries ``profile``, ``ConvertGuidedRequest``
      does not), and
    * for a start, the ``profile`` discriminator, recovered from the
      checkpoint's own profile constant via
      :func:`~elspeth.web.composer.guided.profile.kind_for_profile`. Hardcoding
      ``"live"`` here silently refused every rooted TUTORIAL session, which is
      why the tutorial could not carry a client goal at all.

    The kind is derived from the START checkpoint and never from the caller's
    current one: a fork resets the child to ``EMPTY_PROFILE`` and synthesises
    the child's ``guided_start`` row under a literal ``"profile": "live"``
    request hash, so the child's own start checkpoint is the consistent
    authority even when its parent was a tutorial.
    """

    from elspeth.web.composer.guided.profile import kind_for_profile
    from elspeth.web.sessions.guided_operations import guided_operation_request_hash
    from elspeth.web.sessions.schemas import ConvertGuidedRequest, StartGuidedRequest

    if len(operations) != 1:
        raise AuditIntegrityError("guided root intent has absent or ambiguous start-operation authority")
    operation = operations[0]
    if state_row is None:
        raise AuditIntegrityError("guided root intent start result state is missing")
    start_guided = state_from_record(service._row_to_state_record(state_row)).guided_session
    if start_guided is None or start_guided.root_intent_message_id != message_id:
        raise AuditIntegrityError("guided root intent differs from its live start checkpoint")
    if message_row is None or message_row.role != "user" or message_row.writer_principal != "route_user_message":
        raise AuditIntegrityError("guided root intent row failed session/role/writer custody")
    operation_kind: GuidedOperationKind = "guided_convert" if operation.kind == "guided_convert" else "guided_start"
    request: BaseModel
    if operation_kind == "guided_convert":
        request = ConvertGuidedRequest.model_validate(
            {"operation_id": operation.operation_id, "intent": message_row.content},
            strict=True,
        )
    else:
        request = StartGuidedRequest.model_validate(
            {
                "operation_id": operation.operation_id,
                "profile": kind_for_profile(start_guided.profile).value,
                "intent": message_row.content,
            },
            strict=True,
        )
    if (
        guided_operation_request_hash(
            session_id=UUID(session_id),
            kind=operation_kind,
            request=request,
        )
        != operation.request_hash
    ):
        raise AuditIntegrityError("guided root intent content no longer matches its start request hash")
    return message_row


def _verify_guided_root_message_authority(
    conn: Connection | SessionForkCreationTransaction,
    *,
    service: Any,
    session_id: str,
    guided: Any,
) -> None:
    """Re-derive the live root message from its immutable start operation."""

    if guided.root_intent_message_id is None:
        return
    message_id = guided.root_intent_message_id
    if type(conn) is not _ForkCreationTransaction:
        _verified_guided_root_message_row(
            cast(Connection, conn),
            service=service,
            session_id=session_id,
            message_id=message_id,
        )
        return

    # Fork staging reads the PARENT session through the fork transaction's
    # closed accessor rather than an ordinary connection, then hands the same
    # three rows to the same judge. Only the READER differs between the two
    # branches; the kind/profile/hash derivation must not, which is exactly
    # what an inline walk here got wrong.
    try:
        root_message_id = UUID(message_id)
    except ValueError as exc:
        raise AuditIntegrityError("guided root intent message id is malformed") from exc
    message_row, operations, state_row = cast(SessionForkCreationTransaction, conn).read_parent_guided_root_authority(root_message_id)
    _verified_guided_root_authority(
        service=service,
        session_id=session_id,
        message_id=message_id,
        message_row=message_row,
        operations=operations,
        state_row=state_row,
    )


def _require_exact_guided_intent_cancellation_audit(
    command: GuidedStateOperationCommand,
    existing_intent: DeferredStageIntent,
) -> None:
    cancellation_events: list[object] = []
    for invocation in command.audit_evidence.invocations:
        if invocation.tool_name != "guided_intent_cancelled":
            continue
        if not is_authentic_guided_synthetic_invocation(invocation):
            raise AuditIntegrityError("guided intent cancellation audit event is not an authentic server synthetic invocation")
        try:
            arguments = json.loads(invocation.arguments_canonical)
        except (TypeError, ValueError) as exc:
            raise AuditIntegrityError("guided intent cancellation audit payload is malformed") from exc
        if stable_hash(arguments) != invocation.arguments_hash:
            raise AuditIntegrityError("guided intent cancellation audit payload hash mismatch")
        cancellation_events.append(arguments)
    expected = {
        "intent_id": existing_intent.intent_id,
        "receiving_stage": existing_intent.receiving_stage,
        "target_stage": existing_intent.target_stage,
    }
    if cancellation_events != [expected]:
        raise AuditIntegrityError("guided intent cancellation requires one exact structural audit event")


def _verify_guided_deferred_intent_append(
    command: GuidedStateOperationCommand,
    *,
    prior_guided: GuidedSession,
    candidate_guided: GuidedSession,
) -> tuple[DeferredStageIntent, ...]:
    """Verify the K exact terminal appends the command claims, in order.

    Generalized from the single terminal append (elspeth-3a21f09f09): the
    candidate must extend the prior intents by EXACTLY the claimed ids, in
    claimed order, and every appended intent must bind the one originating
    user message by id and content hash.
    """
    claimed = command.retained_deferred_intent_ids
    appended_count = len(claimed)
    if (
        len(candidate_guided.deferred_intents) != len(prior_guided.deferred_intents) + appended_count
        or candidate_guided.deferred_intents[: len(prior_guided.deferred_intents)] != prior_guided.deferred_intents
        or tuple(intent.intent_id for intent in candidate_guided.deferred_intents[len(prior_guided.deferred_intents) :])
        != tuple(str(intent_id) for intent_id in claimed)
    ):
        raise AuditIntegrityError("retained deferred intents must be the exact claimed terminal appends in order")
    retained = candidate_guided.deferred_intents[len(prior_guided.deferred_intents) :]
    originating = command.originating_message
    if originating is None:  # pragma: no cover - command type owns this guard
        raise AuditIntegrityError("retained deferred intent lost its originating message")
    for intent in retained:
        if intent.originating_message_id != str(originating.message_id):
            raise AuditIntegrityError("retained deferred intent names the wrong originating message")
        if intent.message_content_hash != stable_hash(originating.content):
            raise AuditIntegrityError("retained deferred intent message content hash mismatch")
    return tuple(retained)


def _expected_guided_deferred_intents_after_management(
    conn: Connection,
    *,
    session_id: str,
    command: GuidedStateOperationCommand,
    prior_guided: GuidedSession,
) -> tuple[DeferredStageIntent, ...]:
    from elspeth.web.composer.guided.deferred_intents import (
        DeferredIntentCancelAction,
        DeferredIntentEditAction,
        create_deferred_stage_intent,
    )

    action = command.deferred_intent_action
    if action is None:  # pragma: no cover - command shape owns this branch
        raise AuditIntegrityError("deferred intent action sideband is missing")
    matching = [(index, intent) for index, intent in enumerate(prior_guided.deferred_intents) if intent.intent_id == action.intent_id]
    if len(matching) != 1:
        raise AuditIntegrityError("deferred intent action does not name one exact pending intent")
    intent_index, existing = matching[0]
    from elspeth.web.composer.guided.intent_management import (
        deferred_intent_management_option,
        deferred_intent_management_user_authority_matches,
    )

    if action.selection_token != deferred_intent_management_option(existing).selection_token:
        raise AuditIntegrityError("deferred intent action selection token does not bind its exact pending intent")
    originating = command.originating_message
    if originating is None or not deferred_intent_management_user_authority_matches(
        action,
        deferred_intents=prior_guided.deferred_intents,
        originating_message_content=originating.content,
    ):
        raise AuditIntegrityError("deferred intent mutation lacks matching exact action-specific user authority")
    _verify_guided_deferred_message_authority(conn, session_id=session_id, guided=prior_guided)
    if type(action) is DeferredIntentCancelAction:
        _require_exact_guided_intent_cancellation_audit(command, existing)
        replacement: tuple[DeferredStageIntent, ...] = ()
    elif type(action) is DeferredIntentEditAction:
        originating = command.originating_message
        if originating is None:  # pragma: no cover - command guards this
            raise AuditIntegrityError("deferred intent edit lost its originating message")
        replacement = (
            create_deferred_stage_intent(
                action.replacement,
                receiving_stage=existing.receiving_stage,
                intent_id=existing.intent_id,
                originating_message_id=str(originating.message_id),
                originating_message_content=originating.content,
                guided=prior_guided,
            ),
        )
    else:  # pragma: no cover - command owns the exact union
        raise AuditIntegrityError("deferred intent action type is unsupported")
    return (*prior_guided.deferred_intents[:intent_index], *replacement, *prior_guided.deferred_intents[intent_index + 1 :])


def _verify_guided_deferred_intent_mutation(
    conn: Connection,
    *,
    session_id: str,
    command: GuidedStateOperationCommand,
    prior_guided: GuidedSession,
    candidate_guided: GuidedSession,
) -> tuple[DeferredStageIntent, ...] | None:
    """Verify the exact append/cancel/edit sideband against both checkpoints."""

    from elspeth.web.composer.guided.deferred_intents import DeferredIntentCancelAction

    cancellation_invocations = tuple(
        invocation for invocation in command.audit_evidence.invocations if invocation.tool_name == "guided_intent_cancelled"
    )
    is_cancel = type(command.deferred_intent_action) is DeferredIntentCancelAction
    if is_cancel != bool(cancellation_invocations):
        raise AuditIntegrityError("guided intent cancellation audit must exist if and only if the typed action is cancel")

    if not command.retained_deferred_intent_ids and command.deferred_intent_action is None:
        if candidate_guided.deferred_intents != prior_guided.deferred_intents:
            raise AuditIntegrityError("deferred intent mutation requires an explicit typed sideband")
        return None
    if command.retained_deferred_intent_ids:
        return _verify_guided_deferred_intent_append(command, prior_guided=prior_guided, candidate_guided=candidate_guided)
    expected = _expected_guided_deferred_intents_after_management(
        conn,
        session_id=session_id,
        command=command,
        prior_guided=prior_guided,
    )
    if candidate_guided.deferred_intents != expected:
        raise AuditIntegrityError("deferred intent action candidate differs from its exact typed mutation")
    return None


def _normalize_optional_provenance_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if normalized else None


def _normalize_proposal_composer_provenance(
    *,
    composer_model_identifier: str | None,
    composer_model_version: str | None,
    composer_provider: str | None,
    composer_skill_hash: str | None,
    tool_arguments_hash: str | None,
) -> dict[str, str | None]:
    raw = {
        "composer_model_identifier": composer_model_identifier,
        "composer_model_version": composer_model_version,
        "composer_provider": composer_provider,
        "composer_skill_hash": composer_skill_hash,
        "tool_arguments_hash": tool_arguments_hash,
    }
    normalized = {name: _normalize_optional_provenance_text(value) for name, value in raw.items()}
    if any(value is not None for value in raw.values()):
        missing = tuple(name for name in _PROPOSAL_COMPOSER_PROVENANCE_FIELDS if normalized[name] is None)
        if missing:
            raise AuditIntegrityError(
                "composer provenance for composition proposals requires all fields to be non-blank "
                f"when any are supplied; missing: {', '.join(missing)}"
            )
    return normalized


def _assert_state_in_session(
    conn: Connection,
    *,
    state_id: str,
    expected_session_id: str,
    caller: str,
) -> None:
    """Offensive guard: composition state must belong to the expected session.

    Catches cross-session reference bugs at the service boundary, before
    they hit the DB-level composite FK. Produces a diagnostic naming the
    caller, the state, and the session mismatch — something a generic
    ``IntegrityError`` cannot.

    Raises ``RuntimeError`` because a cross-session reference is a bug
    in caller code, not invalid user input. The audit trail records the
    attempted violation through the standard exception path.

    Contrast with ``revert_state_for_guided_operation``, which raises
    ``ValueError`` for an equivalent-looking cross-session check on
    purpose: that method receives the state_id from the HTTP body and
    must map an unknown / non-owned state to 404 rather than 500. The
    exception type is load-bearing and encodes whether the caller
    (RuntimeError) or the user (ValueError) is wrong.
    """
    state_session_id = conn.execute(select(composition_states_table.c.session_id).where(composition_states_table.c.id == state_id)).scalar()
    if state_session_id is None:
        raise RuntimeError(f"{caller}: composition_state_id={state_id!r} does not exist (expected in session={expected_session_id!r})")
    if state_session_id != expected_session_id:
        raise RuntimeError(
            f"{caller}: composition_state_id={state_id!r} belongs to session "
            f"{state_session_id!r}, not {expected_session_id!r} — cross-session "
            f"reference is a contract violation"
        )


def _assert_parent_assistant_message(
    conn: Connection,
    *,
    parent_assistant_id: str,
    session_id: str,
    caller: str,
) -> None:
    """Offensive guard for tool rows.

    The composite FK on ``(parent_assistant_id, session_id)`` proves
    same-session existence at the DB layer, but SQL CHECK constraints
    cannot portably inspect the referenced row's ``role`` column.
    Service writers must therefore reject tool rows whose parent id
    exists in the same session but does not belong to an assistant
    message — otherwise a tool row could legally point at a user or
    system message and the audit trail would record a false parent
    relationship.

    Raises ``RuntimeError`` because a wrong-role parent reference is a
    bug in caller code, not invalid user input. The exception type
    mirrors ``_assert_state_in_session`` above and is load-bearing:
    it routes to a 500 in the route layer, not a 4xx for the user.
    """
    role = conn.execute(
        select(chat_messages_table.c.role).where(
            chat_messages_table.c.id == parent_assistant_id,
            chat_messages_table.c.session_id == session_id,
        )
    ).scalar_one_or_none()
    if role != "assistant":
        raise RuntimeError(
            f"{caller}: parent_assistant_id={parent_assistant_id!r} must reference "
            f"an assistant message in session={session_id!r}; got role={role!r}"
        )


def _assert_assistant_row_has_audit_content(
    *,
    content: str,
    raw_content: str | None,
    tool_calls: Any,
    caller: str,
) -> None:
    """Reject assistant rows that carry no auditable model output."""
    if content.strip():
        return
    if raw_content is not None and raw_content.strip():
        return
    if tool_calls:
        return
    raise AuditIntegrityError(f"{caller}: refusing to persist empty assistant audit row with no raw_content and no tool_calls")


def _validate_tool_call_id_set_equality(
    *,
    redacted_assistant_tool_calls: tuple[Mapping[str, Any], ...],
    redacted_tool_rows: tuple[RedactedToolRow, ...],
) -> None:
    """Raise ``ToolCallIDMismatchError`` if the assistant's
    ``tool_calls`` IDs and the tool rows' ``tool_call_id`` values are
    not the same unique set.

    Four failure axes — any of them raises:

    - ``missing``: assistant ID with no tool row
    - ``extra``: tool row with no assistant ID
    - ``duplicates_in_assistant``: ID twice in tool_calls
    - ``duplicates_in_rows``: ID twice in tool rows

    All four are reported simultaneously so the diagnostic shows the
    full picture in one shot. The empty-empty case is valid.

    Pure function of caller arguments; called BEFORE
    ``_engine.begin()`` (pre-lock, pre-transaction) so a contract
    violation cannot leave a half-written audit trail behind.
    """
    assistant_ids: list[str] = [
        # ``id`` key is contractually present (OpenAI/LiteLLM tool-call
        # shape requires it). If it's missing, that's an upstream
        # framework bug, not data we should defend against.
        tc["id"]
        for tc in redacted_assistant_tool_calls
    ]
    row_ids: list[str] = [row.tool_call_id for row in redacted_tool_rows]

    assistant_set = set(assistant_ids)
    row_set = set(row_ids)
    missing = frozenset(assistant_set - row_set)
    extra = frozenset(row_set - assistant_set)
    duplicates_in_assistant = frozenset(i for i in assistant_set if assistant_ids.count(i) > 1)
    duplicates_in_rows = frozenset(i for i in row_set if row_ids.count(i) > 1)

    if missing or extra or duplicates_in_assistant or duplicates_in_rows:
        raise ToolCallIDMismatchError(
            missing=missing,
            extra=extra,
            duplicates_in_assistant=duplicates_in_assistant,
            duplicates_in_rows=duplicates_in_rows,
        )


@dataclass(frozen=True, slots=True)
class _PipelineProposalRebaseHop:
    """One appended anchor move, parsed from its immutable event payload."""

    from_state_id: UUID
    to_state_id: UUID
    composition_content_hash: str


def _pipeline_proposal_rebase_hop(
    payload: object,
    *,
    tool_call_id: str,
    draft_hash: str,
) -> _PipelineProposalRebaseHop:
    """Parse one ``proposal.rebased`` payload and bind it to its proposal."""

    thawed = deep_thaw(payload)
    if type(thawed) is not dict or set(thawed) != _PIPELINE_REBASED_FIELDS:
        raise AuditIntegrityError("pipeline proposal rebase event fields are malformed")
    if thawed["schema"] != _PIPELINE_REBASED_SCHEMA or thawed["outcome"] != "rebased":
        raise AuditIntegrityError("pipeline proposal rebase event schema is malformed")
    if thawed["tool_name"] != "set_pipeline" or thawed["status"] != "pending":
        raise AuditIntegrityError("pipeline proposal rebase event status/tool is malformed")
    _validated_guided_proposal_rebase_reason(thawed["reason_code"])
    if thawed["tool_call_id"] != tool_call_id or thawed["draft_hash"] != draft_hash:
        raise AuditIntegrityError("pipeline proposal rebase event is bound to a different proposal")
    raw_content_hash = thawed["composition_content_hash"]
    if not is_lower_sha256_hex(raw_content_hash):
        raise AuditIntegrityError("pipeline proposal rebase event content hash is malformed")
    hop_ids: list[UUID] = []
    for field_name in ("from_state_id", "to_state_id"):
        raw_state_id = thawed[field_name]
        if type(raw_state_id) is not str:
            raise AuditIntegrityError("pipeline proposal rebase event state id is malformed")
        try:
            hop_state_id = UUID(raw_state_id)
        except ValueError as exc:
            raise AuditIntegrityError("pipeline proposal rebase event state id is malformed") from exc
        if str(hop_state_id) != raw_state_id:
            raise AuditIntegrityError("pipeline proposal rebase event state id is not canonical")
        hop_ids.append(hop_state_id)
    if hop_ids[0] == hop_ids[1]:
        raise AuditIntegrityError("pipeline proposal rebase event does not move the anchor")
    return _PipelineProposalRebaseHop(
        from_state_id=hop_ids[0],
        to_state_id=hop_ids[1],
        composition_content_hash=raw_content_hash,
    )


def _effective_pipeline_proposal_base(
    rows: Sequence[Any],
    *,
    creation_base: ProposalBase,
    tool_call_id: str,
    draft_hash: str,
) -> ProposalBase:
    """Return the creation base moved forward by the appended rebase chain.

    ``composition_proposals.base_state_id`` is lifecycle-managed
    (elspeth-ed67eb9d0d): a guided settlement that carries a still-pending
    proposal across the checkpoint it writes moves the proposal's anchor
    there and appends one ``proposal.rebased`` event recording the hop. The
    creation event keeps its original base forever, so the current anchor is
    DERIVED here rather than trusted from the mutable column — the column is
    then checked against this derivation by the caller.

    ``rows`` are that proposal's ``proposal.rebased`` events, read by the
    caller through whichever reader it holds — an ordinary connection or the
    fork transaction's closed accessor. The derivation is deliberately not a
    reader itself: keeping the WALK here and the READ at the call site is what
    lets the fork-transaction path share this one derivation instead of
    substituting a narrower rule of its own.

    The chain is walked as a linked list (each hop's ``from_state_id``
    naming the previous ``to_state_id``), never sorted by ``created_at``:
    same-microsecond ties are possible and a timestamp sort cannot see a
    fork. Every event must be consumed by the walk, so a fork or a dangling
    hop is detectable corruption instead of a silently shortened chain.
    """

    if not rows:
        return creation_base
    if type(creation_base) is not PresentBase:
        raise AuditIntegrityError("pipeline proposal rebase chain has no present creation base")
    successors: dict[UUID, _PipelineProposalRebaseHop] = {}
    for event_row in rows:
        hop = _pipeline_proposal_rebase_hop(event_row.payload, tool_call_id=tool_call_id, draft_hash=draft_hash)
        if hop.from_state_id in successors:
            raise AuditIntegrityError("pipeline proposal rebase chain forks at one base")
        successors[hop.from_state_id] = hop
    base = creation_base
    walked = 0
    while base.state_id in successors:
        hop = successors[base.state_id]
        if hop.composition_content_hash != base.composition_content_hash:
            raise AuditIntegrityError("pipeline proposal rebase chain changed the base content binding")
        base = PresentBase(state_id=hop.to_state_id, composition_content_hash=base.composition_content_hash)
        walked += 1
        if walked > len(successors):
            # A cycle inside the reachable chain would otherwise loop
            # forever; an unreachable cycle is caught by the walk-length
            # equality below.
            raise AuditIntegrityError("pipeline proposal rebase chain is cyclic")
    if walked != len(successors):
        raise AuditIntegrityError("pipeline proposal rebase chain is not one append-only walk from the creation base")
    return base


def _restore_authoritative_pipeline_proposal(
    *,
    conn: Connection | SessionForkCreationTransaction,
    row: CompositionProposalRecord,
    creation_event: ProposalEventRecord,
    reviewed_facts: Mapping[str, Any] | None,
) -> AuthoritativePipelineProposal:
    payload = deep_thaw(creation_event.payload)
    if type(payload) is not dict:
        raise AuditIntegrityError("pipeline proposal creation event payload is malformed")
    if set(payload) != _PIPELINE_CREATED_FIELDS:
        raise AuditIntegrityError("pipeline proposal creation event fields are malformed")
    if payload["schema"] != _PIPELINE_CREATED_SCHEMA:
        raise AuditIntegrityError("pipeline proposal creation event schema is malformed")
    if creation_event.session_id != row.session_id or creation_event.proposal_id != row.id:
        raise AuditIntegrityError("pipeline proposal creation event ownership mismatch")
    if payload["tool_call_id"] != row.tool_call_id or payload["tool_name"] != row.tool_name:
        raise AuditIntegrityError("pipeline proposal creation event tool binding mismatch")
    if row.tool_name != "set_pipeline" or payload["status"] != "pending":
        raise AuditIntegrityError("pipeline proposal creation event status/tool is malformed")
    if payload["custody_result"] not in {"not_required", "ready"}:
        raise AuditIntegrityError("pipeline proposal custody result is malformed")

    private_hash = _pipeline_private_arguments_hash(row.arguments_json)
    if payload["private_arguments_hash"] != private_hash:
        raise AuditIntegrityError("pipeline proposal private arguments binding mismatch")
    if payload["audit_payload_hash"] != _pipeline_audit_payload_hash(
        summary=row.summary,
        rationale=row.rationale,
        affects=row.affects,
        arguments_redacted_json=row.arguments_redacted_json,
    ):
        raise AuditIntegrityError("pipeline proposal audit payload binding mismatch")
    if None in {
        row.composer_model_identifier,
        row.composer_model_version,
        row.composer_provider,
        row.composer_skill_hash,
        row.tool_arguments_hash,
    }:
        raise AuditIntegrityError("pipeline proposal composer provenance is incomplete")
    assert row.composer_model_identifier is not None
    assert row.composer_model_version is not None
    assert row.composer_provider is not None
    assert row.composer_skill_hash is not None
    assert row.tool_arguments_hash is not None
    expected_provenance_hash = _pipeline_provenance_hash(
        user_message_id=row.user_message_id,
        composer_model_identifier=row.composer_model_identifier,
        composer_model_version=row.composer_model_version,
        composer_provider=row.composer_provider,
        composer_skill_hash=row.composer_skill_hash,
        tool_arguments_hash=row.tool_arguments_hash,
    )
    if payload["provenance_hash"] != expected_provenance_hash:
        raise AuditIntegrityError("pipeline proposal provenance binding mismatch")
    if row.tool_arguments_hash != composer_authority_hash(row.arguments_json):
        raise AuditIntegrityError("pipeline proposal row arguments hash mismatch")
    if row.composer_skill_hash != payload["skill_hash"]:
        raise AuditIntegrityError("pipeline proposal skill provenance mismatch")
    raw_base = payload["base"]
    if type(raw_base) is not dict:
        raise AuditIntegrityError("pipeline proposal base metadata is malformed")
    raw_base_kind = raw_base["kind"] if "kind" in raw_base else None
    if raw_base_kind == "absent" and set(raw_base) == {"kind"}:
        base: AbsentBase | PresentBase = AbsentBase()
    elif raw_base_kind == "present" and set(raw_base) == {"kind", "state_id", "composition_content_hash"}:
        raw_state_id = raw_base["state_id"]
        if type(raw_state_id) is not str:
            raise AuditIntegrityError("pipeline proposal base state id is malformed")
        try:
            state_id = UUID(raw_state_id)
        except ValueError as exc:
            raise AuditIntegrityError("pipeline proposal base state id is malformed") from exc
        if str(state_id) != raw_state_id:
            raise AuditIntegrityError("pipeline proposal base state id is not canonical")
        base = PresentBase(state_id=state_id, composition_content_hash=raw_base["composition_content_hash"])
    else:
        raise AuditIntegrityError("pipeline proposal base metadata is malformed")
    raw_surface = payload["surface"]
    if type(raw_surface) is not str:
        raise AuditIntegrityError("pipeline proposal surface is malformed")
    try:
        surface = PlannerSurface(raw_surface)
    except ValueError as exc:
        raise AuditIntegrityError("pipeline proposal surface is malformed") from exc
    covered_ids = payload["covered_deferred_intent_ids"]
    if type(covered_ids) is not list or any(type(value) is not str for value in covered_ids):
        raise AuditIntegrityError("pipeline proposal covered intent ids are malformed")
    proposal = PipelineProposal(
        pipeline=deep_thaw(row.arguments_json),
        draft_hash=payload["draft_hash"],
        base=base,
        reviewed_anchor_hash=payload["reviewed_anchor_hash"],
        surface=surface,
        repair_count=payload["repair_count"],
        skill_hash=payload["skill_hash"],
        covered_deferred_intent_ids=tuple(covered_ids),
        supersedes_draft_hash=payload["supersedes_draft_hash"],
    )
    if reviewed_facts is not None and proposal.reviewed_anchor_hash != reviewed_anchor_hash(reviewed_facts):
        raise AuditIntegrityError("pipeline proposal reviewed anchor does not match current server facts")
    # ``proposal.base`` is the immutable reviewed identity — it is hashed
    # into ``draft_hash``, so it never moves. The proposal's ANCHOR is that
    # base moved forward by every appended rebase hop, and the mutable row
    # column is checked against that derivation rather than being trusted
    # as the source of truth (elspeth-ed67eb9d0d).
    # Only the READER of the rebase hops differs by connection kind; the
    # derivation below is the same one on both paths.
    if type(conn) is _ForkCreationTransaction:
        rebase_rows: Sequence[Any] = cast(SessionForkCreationTransaction, conn).read_parent_proposal_rebase_events(row.id)
    else:
        rebase_rows = (
            cast(Connection, conn)
            .execute(
                select(proposal_events_table)
                .where(proposal_events_table.c.session_id == str(row.session_id))
                .where(proposal_events_table.c.proposal_id == str(row.id))
                .where(proposal_events_table.c.event_type == "proposal.rebased")
            )
            .fetchall()
        )
    current_base = _effective_pipeline_proposal_base(
        rebase_rows,
        creation_base=proposal.base,
        tool_call_id=row.tool_call_id,
        draft_hash=proposal.draft_hash,
    )
    expected_base_state_id = current_base.state_id if type(current_base) is PresentBase else None
    if row.base_state_id != expected_base_state_id:
        raise AuditIntegrityError("pipeline proposal row/base state binding mismatch")
    supersedes_raw = payload["supersedes_proposal_id"]
    if supersedes_raw is not None:
        if type(supersedes_raw) is not str:
            raise AuditIntegrityError("pipeline proposal supersedes id is malformed")
        try:
            supersedes_proposal_id = UUID(supersedes_raw)
        except ValueError as exc:
            raise AuditIntegrityError("pipeline proposal supersedes id is malformed") from exc
        if str(supersedes_proposal_id) != supersedes_raw:
            raise AuditIntegrityError("pipeline proposal supersedes id is not canonical")
    else:
        supersedes_proposal_id = None
    if (supersedes_proposal_id is None) != (proposal.supersedes_draft_hash is None):
        raise AuditIntegrityError("pipeline proposal supersedes id/draft binding is incomplete")
    if supersedes_proposal_id is not None:
        if type(conn) is _ForkCreationTransaction:
            transaction = cast(SessionForkCreationTransaction, conn)
            referenced_row = transaction.read_parent_proposal(supersedes_proposal_id)
            referenced_events = transaction.read_parent_proposal_creation_events(supersedes_proposal_id)
        else:
            connection = cast(Connection, conn)
            referenced_row = connection.execute(
                select(composition_proposals_table)
                .where(composition_proposals_table.c.session_id == str(row.session_id))
                .where(composition_proposals_table.c.id == str(supersedes_proposal_id))
            ).one_or_none()
            referenced_events = tuple(
                connection.execute(
                    select(proposal_events_table)
                    .where(proposal_events_table.c.session_id == str(row.session_id))
                    .where(proposal_events_table.c.proposal_id == str(supersedes_proposal_id))
                    .where(proposal_events_table.c.event_type == "proposal.created")
                ).fetchall()
            )
        if referenced_row is None:
            raise AuditIntegrityError("pipeline proposal supersedes target is missing or cross-session")
        if len(referenced_events) != 1:
            raise AuditIntegrityError("pipeline proposal supersedes target creation authority is malformed")
        referenced_payload = referenced_events[0].payload
        referenced_schema = referenced_payload["schema"] if type(referenced_payload) is dict and "schema" in referenced_payload else None
        referenced_draft_hash = (
            referenced_payload["draft_hash"] if type(referenced_payload) is dict and "draft_hash" in referenced_payload else None
        )
        if (
            type(referenced_payload) is not dict
            or referenced_schema != _PIPELINE_CREATED_SCHEMA
            or referenced_draft_hash != proposal.supersedes_draft_hash
        ):
            raise AuditIntegrityError("pipeline proposal supersedes target draft binding mismatch")
    authority = AuthoritativePipelineProposal(
        row=row,
        proposal=proposal,
        creation_event_id=creation_event.id,
        custody_result=payload["custody_result"],
        supersedes_proposal_id=supersedes_proposal_id,
        current_base=current_base,
    )
    return replace(
        authority,
        row=replace(row, pipeline_metadata=_pipeline_public_metadata(authority)),
    )


def _classify_authoritative_composition_proposal(
    *,
    conn: Connection,
    row: CompositionProposalRecord,
    creation_event: ProposalEventRecord,
    reviewed_facts: Mapping[str, Any] | None,
) -> AuthoritativeCompositionProposal:
    """Accept only one of the two closed current proposal event schemas."""
    # ``ProposalEventRecord.payload`` is typed ``Mapping[str, Any]`` and frozen
    # by the record's own ``__post_init__`` — a first-party audit-event value
    # whose authorship a DB round-trip does not demote. Read it directly; a
    # corrupted non-mapping crashes on the key-set dispatch below instead of
    # being defensively revalidated here.
    payload = creation_event.payload
    if type(payload) not in (dict, MappingProxyType):
        raise AuditIntegrityError("proposal creation event payload must be a mapping")
    if set(payload) == _TOOL_PROPOSAL_CREATED_FIELDS:
        expected = {
            "schema": _TOOL_PROPOSAL_CREATED_SCHEMA,
            "tool_call_id": row.tool_call_id,
            "tool_name": row.tool_name,
            "status": "pending",
        }
        if payload != expected or creation_event.session_id != row.session_id or creation_event.proposal_id != row.id:
            raise AuditIntegrityError("tool proposal creation event binding is malformed")
        return AuthoritativeCompositionProposal(row=row, pipeline=None)
    pipeline = _restore_authoritative_pipeline_proposal(
        conn=conn,
        row=row,
        creation_event=creation_event,
        reviewed_facts=reviewed_facts,
    )
    return AuthoritativeCompositionProposal(row=pipeline.row, pipeline=pipeline)


def _pipeline_public_metadata(authority: AuthoritativePipelineProposal) -> PipelineProposalPublicMetadata:
    payload = authority.proposal
    return PipelineProposalPublicMetadata(
        surface=payload.surface.value,
        draft_hash=payload.draft_hash,
        base=_proposal_base_payload(payload.base),
        reviewed_anchor_hash=payload.reviewed_anchor_hash,
        repair_count=payload.repair_count,
        skill_hash=payload.skill_hash,
        audit_payload_hash=_pipeline_audit_payload_hash(
            summary=authority.row.summary,
            rationale=authority.row.rationale,
            affects=authority.row.affects,
            arguments_redacted_json=authority.row.arguments_redacted_json,
        ),
        custody_result=authority.custody_result,
    )


def _verify_committed_pipeline_authority(
    conn: Connection,
    *,
    service: SessionServiceImpl,
    authority: AuthoritativePipelineProposal,
) -> None:
    """Verify the complete already-committed outcome used by HTTP retries."""
    sid = str(authority.row.session_id)
    pid = str(authority.row.id)
    terminal_rows = conn.execute(
        select(proposal_events_table)
        .where(proposal_events_table.c.session_id == sid)
        .where(proposal_events_table.c.proposal_id == pid)
        .where(proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")))
    ).fetchall()
    if len(terminal_rows) != 1 or terminal_rows[0].event_type != "proposal.accepted":
        raise AuditIntegrityError("committed pipeline proposal must have one accepted terminal event")
    terminal = _proposal_event_record_from_row(terminal_rows[0])
    payload = deep_thaw(terminal.payload)
    if type(payload) is not dict or set(payload) != _PIPELINE_ACCEPTED_FIELDS:
        raise AuditIntegrityError("committed pipeline proposal terminal payload is malformed")
    row = authority.row
    if row.audit_event_id != terminal.id:
        raise AuditIntegrityError("committed pipeline proposal terminal event pointer is malformed")
    if row.committed_state_id is None:
        raise AuditIntegrityError("committed pipeline proposal is missing committed state")
    dispatch_payload = payload["dispatch"]
    if type(dispatch_payload) is not dict or set(dispatch_payload) != {
        "tool_call_id",
        "tool_name",
        "status",
        "arguments_hash",
        "result_hash",
    }:
        raise AuditIntegrityError("committed pipeline proposal dispatch binding is malformed")
    try:
        dispatch = PipelineDispatchAuditBinding(
            tool_call_id=dispatch_payload["tool_call_id"],
            tool_name=dispatch_payload["tool_name"],
            status=ComposerToolStatus(dispatch_payload["status"]),
            arguments_hash=dispatch_payload["arguments_hash"],
            result_hash=dispatch_payload["result_hash"],
        )
    except (TypeError, ValueError) as exc:
        raise AuditIntegrityError("committed pipeline proposal dispatch binding is malformed") from exc
    state_row = conn.execute(
        select(composition_states_table)
        .where(composition_states_table.c.session_id == sid)
        .where(composition_states_table.c.id == str(row.committed_state_id))
    ).one_or_none()
    if state_row is None:
        raise AuditIntegrityError("committed pipeline proposal state is missing")
    state_record = service._row_to_state_record(state_row)
    state_hash = composition_content_hash(state_from_record(state_record))
    expected = _pipeline_accepted_payload(
        authority=authority,
        state_id=str(state_record.id),
        state_content_hash=state_hash,
        final_composer_metadata=state_record.composer_meta,
        dispatch=dispatch,
    )
    if payload != expected:
        raise AuditIntegrityError("committed pipeline proposal exact retry binding mismatch")
    if _persisted_pipeline_dispatch_content_hashes(conn, session_id=sid, dispatch=dispatch) != (state_hash,):
        raise AuditIntegrityError("committed pipeline proposal dispatch audit is missing or duplicated")


def _verify_rejected_pipeline_authority(
    conn: Connection,
    *,
    authority: AuthoritativePipelineProposal,
) -> None:
    """Verify the closed terminal authority for a canonical rejection."""
    sid = str(authority.row.session_id)
    pid = str(authority.row.id)
    terminal_rows = conn.execute(
        select(proposal_events_table)
        .where(proposal_events_table.c.session_id == sid)
        .where(proposal_events_table.c.proposal_id == pid)
        .where(proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")))
    ).fetchall()
    if len(terminal_rows) != 1 or terminal_rows[0].event_type != "proposal.rejected":
        raise AuditIntegrityError("rejected pipeline proposal must have one rejected terminal event")
    terminal = _proposal_event_record_from_row(terminal_rows[0])
    row = authority.row
    if row.audit_event_id != terminal.id or row.committed_state_id is not None:
        raise AuditIntegrityError("rejected pipeline proposal row terminal binding is malformed")
    payload = deep_thaw(terminal.payload)
    if type(payload) is not dict or set(payload) != _PIPELINE_REJECTED_FIELDS:
        raise AuditIntegrityError("rejected pipeline proposal terminal payload is malformed")
    reason = _validated_pipeline_rejection_reason(payload["reason_code"])
    dispatch_payload = payload["dispatch"]
    if dispatch_payload is None:
        dispatch = None
    elif type(dispatch_payload) is dict and set(dispatch_payload) == {
        "tool_call_id",
        "tool_name",
        "status",
        "arguments_hash",
        "result_hash",
    }:
        try:
            dispatch = PipelineDispatchAuditBinding(
                tool_call_id=dispatch_payload["tool_call_id"],
                tool_name=dispatch_payload["tool_name"],
                status=ComposerToolStatus(dispatch_payload["status"]),
                arguments_hash=dispatch_payload["arguments_hash"],
                result_hash=dispatch_payload["result_hash"],
            )
        except (TypeError, ValueError) as exc:
            raise AuditIntegrityError("rejected pipeline proposal dispatch binding is malformed") from exc
    else:
        raise AuditIntegrityError("rejected pipeline proposal dispatch binding is malformed")
    if reason == "candidate_executor_mismatch" and dispatch is None:
        raise AuditIntegrityError("rejected pipeline proposal mismatch outcome is missing dispatch evidence")
    expected = _pipeline_rejected_payload(authority=authority, reason=reason, dispatch=dispatch)
    if payload != expected:
        raise AuditIntegrityError("rejected pipeline proposal exact terminal binding mismatch")
    if dispatch is not None and len(_persisted_pipeline_dispatch_content_hashes(conn, session_id=sid, dispatch=dispatch)) != 1:
        raise AuditIntegrityError("rejected pipeline proposal dispatch audit is missing or duplicated")


def _verify_pipeline_lifecycle_authority(
    conn: Connection | SessionForkCreationTransaction,
    *,
    service: SessionServiceImpl,
    authority: AuthoritativePipelineProposal,
) -> None:
    """Verify the complete canonical lifecycle before exposing or mutating it."""
    is_fork_transaction = type(conn) is _ForkCreationTransaction
    if authority.row.status == "committed":
        if is_fork_transaction:
            raise AuditIntegrityError("fork source proposal must remain pending")
        _verify_committed_pipeline_authority(cast(Connection, conn), service=service, authority=authority)
        return
    if authority.row.status == "rejected":
        if is_fork_transaction:
            raise AuditIntegrityError("fork source proposal must remain pending")
        _verify_rejected_pipeline_authority(cast(Connection, conn), authority=authority)
        return
    if authority.row.status != "pending":
        raise AuditIntegrityError("pipeline proposal lifecycle status is malformed")
    sid = str(authority.row.session_id)
    pid = str(authority.row.id)
    if is_fork_transaction:
        terminal_count = cast(SessionForkCreationTransaction, conn).count_parent_proposal_terminal_events(authority.row.id)
    else:
        terminal_count = (
            cast(Connection, conn)
            .execute(
                select(func.count(proposal_events_table.c.id))
                .select_from(proposal_events_table)
                .where(proposal_events_table.c.session_id == sid)
                .where(proposal_events_table.c.proposal_id == pid)
                .where(proposal_events_table.c.event_type.in_(("proposal.accepted", "proposal.rejected")))
            )
            .scalar_one()
        )
    if terminal_count != 0:
        raise AuditIntegrityError("pending pipeline proposal must not have a terminal event")
    if authority.row.audit_event_id != authority.creation_event_id or authority.row.committed_state_id is not None:
        raise AuditIntegrityError("pending pipeline proposal row authority binding is malformed")
