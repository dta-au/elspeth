"""Pure strict decoding of owned persisted proposal records and creation events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.authority_hashing import composer_authority_hash, project_composer_authority_payload
from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal, PresentBase
from elspeth.web.sessions.pipeline_settlement_payloads import ComposerOperationBinding
from elspeth.web.sessions.protocol import (
    AuthoritativeCompositionProposal,
    AuthoritativePipelineProposal,
    CompositionProposalRecord,
    PipelineProposalPublicMetadata,
    ProposalEventRecord,
)
from elspeth.web.sessions.time_normalization import restore_utc

_PIPELINE_CREATED_SCHEMA = "pipeline_proposal_created.v3"


_TOOL_PROPOSAL_CREATED_SCHEMA = "tool_proposal_created.v1"


_PIPELINE_CREATED_FIELDS = frozenset(
    {
        "schema",
        "tool_call_id",
        "tool_name",
        "status",
        "draft_hash",
        "base",
        "repair_count",
        "skill_hash",
        "custody_result",
        "private_arguments_hash",
        "provenance_hash",
        "audit_payload_hash",
    }
)


_TOOL_PROPOSAL_CREATED_FIELDS = frozenset({"schema", "tool_call_id", "tool_name", "status"})


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


def _restore_authoritative_pipeline_proposal(
    *,
    row: CompositionProposalRecord,
    creation_event: ProposalEventRecord,
) -> AuthoritativePipelineProposal:
    payload = deep_thaw(creation_event.payload)
    if type(payload) is not dict:
        raise AuditIntegrityError("pipeline proposal creation event payload is malformed")
    schema = payload["schema"] if "schema" in payload else None
    expected_fields = _PIPELINE_CREATED_FIELDS | {"composer_operation"} if schema == _PIPELINE_CREATED_SCHEMA else _PIPELINE_CREATED_FIELDS
    if set(payload) != expected_fields:
        raise AuditIntegrityError("pipeline proposal creation event fields are malformed")
    if schema not in ("pipeline_proposal_created.v2", _PIPELINE_CREATED_SCHEMA):
        raise AuditIntegrityError("pipeline proposal creation event schema is malformed")
    binding = None
    if schema == _PIPELINE_CREATED_SCHEMA and payload["composer_operation"] is not None:
        try:
            binding = ComposerOperationBinding.model_validate(payload["composer_operation"])
        except ValidationError as exc:
            raise AuditIntegrityError("pipeline proposal creation operation binding is malformed") from exc
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
    proposal = PipelineProposal(
        pipeline=deep_thaw(row.arguments_json),
        draft_hash=payload["draft_hash"],
        base=base,
        repair_count=payload["repair_count"],
        skill_hash=payload["skill_hash"],
    )
    expected_base_state_id = proposal.base.state_id if type(proposal.base) is PresentBase else None
    if row.base_state_id != expected_base_state_id:
        raise AuditIntegrityError("pipeline proposal row/base state binding mismatch")
    authority = AuthoritativePipelineProposal(
        row=row,
        proposal=proposal,
        creation_event_id=creation_event.id,
        custody_result=payload["custody_result"],
        composer_operation=binding,
        creation_schema=schema,
        creation_actor=creation_event.actor,
    )
    return replace(
        authority,
        row=replace(row, pipeline_metadata=_pipeline_public_metadata(authority)),
    )


def _classify_authoritative_composition_proposal(
    *,
    row: CompositionProposalRecord,
    creation_event: ProposalEventRecord,
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
        row=row,
        creation_event=creation_event,
    )
    return AuthoritativeCompositionProposal(row=pipeline.row, pipeline=pipeline)


def _pipeline_public_metadata(authority: AuthoritativePipelineProposal) -> PipelineProposalPublicMetadata:
    payload = authority.proposal
    return PipelineProposalPublicMetadata(
        draft_hash=payload.draft_hash,
        base=_proposal_base_payload(payload.base),
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
