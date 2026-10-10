"""Pipeline proposal audit payloads and persisted authority verification."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, TypedDict, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, func, select

from elspeth.contracts.composer_audit import ComposerToolStatus, PipelineDispatchAuditPayload
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import canonical_json, is_lower_sha256_hex, stable_hash
from elspeth.web.compartments import is_compartment_id
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
from elspeth.web.composer.pipeline_planner import PipelinePlanResult
from elspeth.web.composer.pipeline_proposal import (
    composition_content_hash,
)
from elspeth.web.coordination.repository import (
    _ForkCreationTransaction,
)
from elspeth.web.sessions._persist_payload import RedactedToolRow
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    chat_messages_table,
    composition_states_table,
    proposal_events_table,
)
from elspeth.web.sessions.pipeline_settlement_payloads import PipelineAcceptedEvidence
from elspeth.web.sessions.proposal_decoder import (
    _PIPELINE_CREATED_FIELDS as _PIPELINE_CREATED_FIELDS,
)
from elspeth.web.sessions.proposal_decoder import (
    _PIPELINE_CREATED_SCHEMA as _PIPELINE_CREATED_SCHEMA,
)
from elspeth.web.sessions.proposal_decoder import (
    _TOOL_PROPOSAL_CREATED_FIELDS as _TOOL_PROPOSAL_CREATED_FIELDS,
)
from elspeth.web.sessions.proposal_decoder import (
    _TOOL_PROPOSAL_CREATED_SCHEMA as _TOOL_PROPOSAL_CREATED_SCHEMA,
)
from elspeth.web.sessions.proposal_decoder import (
    _classify_authoritative_composition_proposal as _classify_authoritative_composition_proposal,
)
from elspeth.web.sessions.proposal_decoder import (
    _pipeline_audit_payload_hash as _pipeline_audit_payload_hash,
)
from elspeth.web.sessions.proposal_decoder import (
    _pipeline_private_arguments_hash as _pipeline_private_arguments_hash,
)
from elspeth.web.sessions.proposal_decoder import (
    _pipeline_provenance_hash as _pipeline_provenance_hash,
)
from elspeth.web.sessions.proposal_decoder import (
    _pipeline_public_metadata as _pipeline_public_metadata,
)
from elspeth.web.sessions.proposal_decoder import (
    _proposal_base_payload as _proposal_base_payload,
)
from elspeth.web.sessions.proposal_decoder import (
    _proposal_event_record_from_row as _proposal_event_record_from_row,
)
from elspeth.web.sessions.proposal_decoder import (
    _proposal_record_from_row as _proposal_record_from_row,
)
from elspeth.web.sessions.proposal_decoder import (
    _restore_authoritative_pipeline_proposal as _restore_authoritative_pipeline_proposal,
)
from elspeth.web.sessions.protocol import (
    AuthoritativePipelineProposal,
    CompositionStateData,
    PipelineDispatchRecovery,
    PipelineProposalRejectionReason,
    SessionForkCreationTransaction,
    ToolCallIDMismatchError,
)

if TYPE_CHECKING:
    from elspeth.web.sessions.service import SessionServiceImpl

_PROPOSAL_COMPOSER_PROVENANCE_FIELDS = (
    "composer_model_identifier",
    "composer_model_version",
    "composer_provider",
    "composer_skill_hash",
    "tool_arguments_hash",
)


_PIPELINE_REJECTION_REASONS = frozenset(
    {
        "operator_rejected",
        "candidate_executor_mismatch",
        "validation_failed",
        "policy_changed",
        "base_conflict",
        "request_cancelled",
        "superseded",
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


class _PipelineCreatedEventPayload(TypedDict):
    schema: str
    tool_call_id: str
    tool_name: str
    status: str
    draft_hash: str
    base: dict[str, str]
    repair_count: int
    skill_hash: str
    custody_result: str
    private_arguments_hash: str
    provenance_hash: str
    audit_payload_hash: str
    composer_operation: dict[str, str | int] | None


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
) -> _PipelineCreatedEventPayload:
    proposal = plan.proposal
    tool_arguments_hash = composer_authority_hash(proposal.pipeline)
    payload: _PipelineCreatedEventPayload = {
        "schema": _PIPELINE_CREATED_SCHEMA,
        "composer_operation": None,
        "tool_call_id": plan.tool_call_id,
        "tool_name": "set_pipeline",
        "status": "pending",
        "draft_hash": proposal.draft_hash,
        "base": _proposal_base_payload(proposal.base),
        "repair_count": proposal.repair_count,
        "skill_hash": proposal.skill_hash,
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
    """Keep ingress metadata in the exact, content-free evidence shape."""
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


def _pipeline_rejected_payload(
    *,
    authority: AuthoritativePipelineProposal,
    reason: PipelineProposalRejectionReason,
    dispatch: PipelineDispatchAuditBinding | None,
) -> _PipelineRejectedEventPayload:
    reason = _validated_pipeline_rejection_reason(reason)
    outcome = "rejected" if reason == "operator_rejected" else "superseded" if reason == "superseded" else "failed"
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

    Contrast with ``revert_state_for_operation_receipt``, which raises
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
    if type(payload) is not dict:
        raise AuditIntegrityError("committed pipeline proposal terminal payload is malformed")
    accepted_v2 = None
    if payload.get("schema") == "pipeline_proposal_accepted.v2":
        try:
            accepted_v2 = PipelineAcceptedEvidence.model_validate_json(canonical_json(payload))
        except ValidationError as exc:
            raise AuditIntegrityError("committed pipeline proposal v2 evidence is malformed") from exc
    elif set(payload) != _PIPELINE_ACCEPTED_FIELDS:
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
    compared_payload = {key: payload[key] for key in _PIPELINE_ACCEPTED_FIELDS}
    if accepted_v2 is not None:
        compared_payload["schema"] = "pipeline_proposal_accepted.v1"
        if accepted_v2.creation_composer_operation != authority.composer_operation:
            raise AuditIntegrityError("committed pipeline proposal creation operation mismatch")
    if compared_payload != expected:
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
