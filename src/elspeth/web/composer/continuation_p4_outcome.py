"""Complete historical P4 outcome DATA; never a reminted commit witness.

The original live outcome/exception/cause are untouched. A decoder returns
historical facts, not AuditOutcome or _PersistOutcome. Current use must join
an actual writer/authorized reader result, not infer a commit from these bytes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from elspeth.contracts.composer_mutation import ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer._compose_loop_carriers import _PersistOutcome
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.composer.continuation_p4_state_payload import encode_p4_state_payload
from elspeth.web.composer.continuation_prepared_p4 import (
    PreparedP4Value,
    _p4_rows_json,
    decode_prepared_p4,
    encode_prepared_p4,
    prepared_p4_members,
)
from elspeth.web.composer.continuation_tool_batch import _decode_owned, _dump_owned
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions._persist_payload import AuditOutcome, CommittedCheckpointWitness, StatePayload
from elspeth.web.sessions.content_publication import read_content_publication
from elspeth.web.sessions.protocol import (
    CompositionStateData,
    CompositionStateRecord,
    decode_stored_composition_validation_errors,
    serialize_composition_validation_errors,
)


class _HistoricalCheckpointColumns(BaseModel):
    """Exactly the original JSON column values, including their member order."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    source: dict[str, JsonValue] | None
    sources: dict[str, dict[str, JsonValue]] | None
    nodes: list[dict[str, JsonValue]] | None
    edges: list[dict[str, JsonValue]] | None
    outputs: list[dict[str, JsonValue]] | None
    metadata_: dict[str, JsonValue]
    validation_errors: list[dict[str, JsonValue]] | None
    composer_meta: dict[str, JsonValue] | None


def _encode_checkpoint_columns(columns: _HistoricalCheckpointColumns) -> str:
    try:
        encoded = json.dumps(owned_json(columns.model_dump(mode="python")), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        encoded.encode("utf-8", errors="strict")
    except (TypeError, ValueError) as error:
        raise AuditIntegrityError("historical checkpoint columns are not finite UTF-8 JSON") from error
    return encoded


def _decode_checkpoint_columns(encoded: str) -> _HistoricalCheckpointColumns:
    value, parse_error = parse_json_strict(encoded)
    if parse_error is not None:
        raise AuditIntegrityError("historical checkpoint columns are not complete strict JSON")
    try:
        # The strict parser alone does not refuse overflow such as 1e400.
        # Admit finite owned values before the closed model or serialization.
        columns = _HistoricalCheckpointColumns.model_validate(owned_json(value), strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("historical checkpoint columns violate their closed contract") from error
    if _encode_checkpoint_columns(columns) != encoded:
        raise AuditIntegrityError("historical checkpoint columns changed their exact ordered encoding")
    return columns


def historical_checkpoint_columns_json(record: CompositionStateRecord) -> str:
    """Preserve original owned column values, never SQL text or commit authority."""
    if type(record) is not CompositionStateRecord:
        raise TypeError("historical columns require their original owned state record")
    columns = _HistoricalCheckpointColumns.model_validate(
        {
            "source": owned_json(record.source),
            "sources": owned_json(record.sources),
            "nodes": owned_json(record.nodes),
            "edges": owned_json(record.edges),
            "outputs": owned_json(record.outputs),
            "metadata_": owned_json(record.metadata_),
            "validation_errors": owned_json(serialize_composition_validation_errors(record.validation_errors)),
            "composer_meta": owned_json(record.composer_meta),
        },
        strict=True,
    )
    return _encode_checkpoint_columns(columns)


class HistoricalCheckpointData(BaseModel):
    """Original historical row DATA; the previous nested-column shape refuses.

    The outer canonical envelope must not normalize the original raw columns
    protected by content publication. This preserves values/order/numeric types,
    not original SQL JSON whitespace or a current writer witness.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: UUID
    session_id: UUID
    version: int = Field(ge=1)
    original_columns_json: str
    is_valid: bool
    created_at: datetime
    derived_from_state_id: UUID | None
    review_projection: None
    creator_operation_id: str = Field(min_length=1)
    inserted_state_ids: tuple[UUID, ...]


class RetainedP4Outcome(BaseModel):
    """Historical output, including complete input and truthful publication."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.p4-outcome.v1"] = "composer.p4-outcome.v1"
    prepared_json: str
    current_state_id: str | None
    persisted_assistant_message_id: str | None
    persisted_assistant_content: str | None
    persisted_tool_call_turn: bool
    persisted_assistant_matches_current_dispatch: bool
    unwind_audit_failed: bool
    failed_turn_json: str
    redacted_assistant_tool_calls_json: str
    redacted_tool_rows_json: str
    checkpoint_disposition: Literal["committed", "no_new_checkpoint", "empty", "unavailable"]
    checkpoint: HistoricalCheckpointData | None
    authority_revocation_json: str | None
    # These identity facts are actual aliases in turn_audit's return, not
    # guessed from equal JSON. No independently mutable copy is reconstructed.
    published_rows_are_persist_rows: bool
    revocation_is_audit_revocation: bool
    effect_verification_failed: bool
    continuation_retention_failed: bool


_VALUE = TypeAdapter(RetainedP4Outcome)
_CHECKPOINT = TypeAdapter(HistoricalCheckpointData)
_FAILED = TypeAdapter(FailedTurnMetadata)
_REVOCATION = TypeAdapter(ComposerMutationRevocation)


def _checkpoint_data(witness: CommittedCheckpointWitness) -> HistoricalCheckpointData:
    if type(witness) is not CommittedCheckpointWitness or type(witness.state) is not CompositionStateRecord:
        raise TypeError("P4 retention requires the actual writer checkpoint")
    row = witness.state
    if row.review_projection is not None:
        raise AuditIntegrityError("P4 writer checkpoint is a raw row, not an effective review overlay")
    return HistoricalCheckpointData.model_validate(
        {
            "id": row.id,
            "session_id": row.session_id,
            "version": row.version,
            "original_columns_json": historical_checkpoint_columns_json(row),
            "is_valid": row.is_valid,
            "created_at": row.created_at,
            "derived_from_state_id": row.derived_from_state_id,
            "review_projection": None,
            "creator_operation_id": witness.creator_operation_id,
            "inserted_state_ids": witness.inserted_state_ids,
        },
        strict=True,
    )


def historical_checkpoint_record(value: HistoricalCheckpointData, *, env_ref_names: frozenset[str] = frozenset()) -> CompositionStateRecord:
    """Return only the owned historical row; never construct a commit witness."""
    if type(value) is not HistoricalCheckpointData:
        raise TypeError("historical checkpoint requires its complete data owner")
    # Validate the inner UTF-8/closed DATA before any outer serializer touches
    # a malformed string. Its ordered values never pass through canonical JSON.
    columns = _decode_checkpoint_columns(value.original_columns_json)
    value = _decode_owned(_CHECKPOINT, _dump_owned(_CHECKPOINT, value))
    if value.created_at.utcoffset() != timedelta(0):
        raise AuditIntegrityError("historical checkpoint lost its original aware UTC timestamp")
    if (
        not value.inserted_state_ids
        or value.inserted_state_ids[-1] != value.id
        or len(set(value.inserted_state_ids)) != len(value.inserted_state_ids)
    ):
        raise AuditIntegrityError("historical checkpoint lost its exact inserted revision cohort")
    errors = decode_stored_composition_validation_errors(columns.validation_errors)
    # Reuse the actual component-aware payload admission. Its normalized
    # temporary source map is used for inspection only; the row below retains
    # the original source/sources columns separately and exactly.
    data = CompositionStateData(
        source=columns.source,
        sources=columns.sources,
        nodes=columns.nodes,
        edges=columns.edges,
        outputs=columns.outputs,
        metadata_=columns.metadata_,
        is_valid=value.is_valid,
        validation_errors=errors,
        composer_meta=columns.composer_meta,
    )
    encode_p4_state_payload(
        StatePayload(data, None if value.derived_from_state_id is None else str(value.derived_from_state_id)), env_ref_names=env_ref_names
    )
    require_no_credential_material(value.creator_operation_id, surface="retained checkpoint creator", env_ref_names=env_ref_names)
    record = CompositionStateRecord(
        id=value.id,
        session_id=value.session_id,
        version=value.version,
        source=columns.source,
        sources=columns.sources,
        nodes=columns.nodes,
        edges=columns.edges,
        outputs=columns.outputs,
        metadata_=columns.metadata_,
        is_valid=value.is_valid,
        validation_errors=errors,
        created_at=value.created_at,
        derived_from_state_id=value.derived_from_state_id,
        composer_meta=columns.composer_meta,
        review_projection=None,
    )
    read_content_publication(record)
    return record


def _admit(value: RetainedP4Outcome, env_ref_names: frozenset[str]) -> RetainedP4Outcome:
    if type(value) is not RetainedP4Outcome:
        raise TypeError("P4 outcome requires its complete historical owner")
    value = _decode_owned(_VALUE, _dump_owned(_VALUE, value))
    prepared = decode_prepared_p4(value.prepared_json, env_ref_names=env_ref_names)
    members = prepared_p4_members(prepared, env_ref_names=env_ref_names)
    failed = _decode_owned(_FAILED, value.failed_turn_json)
    if failed.tool_calls_attempted < 0 or failed.tool_responses_persisted is None or failed.tool_responses_persisted < 0:
        raise AuditIntegrityError("P4 outcome lost its concrete nonnegative failed-turn counts")
    revocation = None if value.authority_revocation_json is None else _decode_owned(_REVOCATION, value.authority_revocation_json)
    if not value.published_rows_are_persist_rows or not value.revocation_is_audit_revocation:
        raise AuditIntegrityError("P4 outcome lost its actual publication/revocation aliases")
    if revocation is not None:
        require_no_credential_material(
            owned_json(_REVOCATION.dump_python(revocation, mode="json")), surface="retained P4 revocation", env_ref_names=env_ref_names
        )
        if members.mutation_ceiling is None or revocation.ceiling != members.mutation_ceiling:
            raise AuditIntegrityError("P4 revocation changed its preparing request ceiling")
    if value.redacted_assistant_tool_calls_json != prepared.redacted_calls_json:
        raise AuditIntegrityError("P4 output changed its admitted assistant call cohort")
    if failed.assistant_message_id != value.persisted_assistant_message_id or failed.tool_calls_attempted < len(members.redacted_rows):
        raise AuditIntegrityError("P4 failed-turn metadata lost its actual attempted/persisted cohort")
    if value.checkpoint_disposition == "unavailable":
        if (
            not prepared.crash_pending
            or not value.unwind_audit_failed
            or value.current_state_id is not None
            or value.persisted_assistant_message_id is not None
            or value.persisted_assistant_content is not None
            or value.persisted_tool_call_turn
            or value.persisted_assistant_matches_current_dispatch
            or value.redacted_tool_rows_json != "[]"
            or value.checkpoint is not None
            or revocation is not None
            or value.effect_verification_failed
            or failed.tool_responses_persisted != 0
        ):
            raise AuditIntegrityError("rolled-back P4 invented publication, verification or checkpoint facts")
        return value
    if members.authority_revocation is not None and revocation != members.authority_revocation:
        raise AuditIntegrityError("P4 output discarded the original revocation")
    if (
        value.continuation_retention_failed
        or value.unwind_audit_failed
        or value.persisted_assistant_message_id is None
        or value.persisted_assistant_content is None
        or not value.persisted_tool_call_turn
    ):
        raise AuditIntegrityError("successful P4 lost its actual publication pair")
    expected_rows = members.revoked_rows if revocation is not None else members.redacted_rows
    expected_content = "" if revocation is not None else prepared.assistant_content
    if (
        value.redacted_tool_rows_json != _p4_rows_json(expected_rows, env_ref_names=env_ref_names)
        or value.persisted_assistant_content != expected_content
        or failed.tool_responses_persisted != len(expected_rows)
        or value.persisted_assistant_matches_current_dispatch != (prepared.assistant_row_uses_current_dispatch and revocation is None)
    ):
        raise AuditIntegrityError("P4 output changed the actual locked publication choice")
    require_no_credential_material(
        [value.current_state_id, value.persisted_assistant_message_id], surface="retained P4 row identities", env_ref_names=env_ref_names
    )
    if value.checkpoint_disposition == "committed":
        if value.checkpoint is None or revocation is not None:
            raise AuditIntegrityError("committed P4 lost its historical checkpoint or retained a refused graph")
        record = historical_checkpoint_record(value.checkpoint, env_ref_names=env_ref_names)
        if (
            str(record.id) != value.current_state_id
            or str(record.session_id) != prepared.session_id
            or value.checkpoint.creator_operation_id != prepared.creator_operation_id
        ):
            raise AuditIntegrityError("P4 checkpoint changed its exact preparing session/creator/row")
    elif value.checkpoint is not None or value.effect_verification_failed:
        raise AuditIntegrityError("nongraph P4 invented a new checkpoint or checkpoint verification")
    elif value.checkpoint_disposition == "empty" and value.current_state_id is not None:
        raise AuditIntegrityError("empty P4 invented an inherited state")
    elif value.checkpoint_disposition == "no_new_checkpoint" and value.current_state_id is None:
        raise AuditIntegrityError("inherited P4 lost its actual state identity")
    return value


def capture_p4_outcome(
    prepared: PreparedP4Value, outcome: _PersistOutcome, *, env_ref_names: frozenset[str] = frozenset()
) -> RetainedP4Outcome:
    if type(outcome) is not _PersistOutcome or type(outcome.audit_outcome) is not AuditOutcome:
        raise TypeError("P4 capture requires the actual session-backed persistence outcome")
    audit = outcome.audit_outcome
    if type(outcome.failed_turn) is not FailedTurnMetadata:
        raise AuditIntegrityError("session-backed P4 lost its actual failed-turn projection")
    if (
        outcome.current_state_id != audit.current_state_id
        or outcome.persisted_assistant_message_id != audit.assistant_id
        or outcome.persisted_assistant_content != audit.published_assistant_content
        or outcome.unwind_audit_failed != audit.unwind_audit_failed
    ):
        raise AuditIntegrityError("P4 projection differs from its actual writer outcome")
    if audit.continuation_retention_failure is not None and outcome.effect_verification_failure is not audit.continuation_retention_failure:
        raise AuditIntegrityError("P4 projection discarded its original secondary retention failure")
    value = RetainedP4Outcome(
        prepared_json=encode_prepared_p4(prepared, env_ref_names=env_ref_names),
        current_state_id=outcome.current_state_id,
        persisted_assistant_message_id=outcome.persisted_assistant_message_id,
        persisted_assistant_content=outcome.persisted_assistant_content,
        persisted_tool_call_turn=outcome.persisted_tool_call_turn,
        persisted_assistant_matches_current_dispatch=outcome.persisted_assistant_matches_current_dispatch,
        unwind_audit_failed=outcome.unwind_audit_failed,
        failed_turn_json=_dump_owned(_FAILED, outcome.failed_turn),
        redacted_assistant_tool_calls_json=canonical_json(owned_json(outcome.redacted_assistant_tool_calls)),
        redacted_tool_rows_json=_p4_rows_json(outcome.redacted_tool_rows, env_ref_names=env_ref_names),
        checkpoint_disposition=audit.checkpoint_disposition,
        checkpoint=None if audit.checkpoint is None else _checkpoint_data(audit.checkpoint),
        authority_revocation_json=None if outcome.authority_revocation is None else _dump_owned(_REVOCATION, outcome.authority_revocation),
        published_rows_are_persist_rows=outcome.redacted_tool_rows is audit.published_tool_rows,
        revocation_is_audit_revocation=outcome.authority_revocation is audit.authority_revocation,
        effect_verification_failed=outcome.effect_verification_failure is not None and audit.continuation_retention_failure is None,
        continuation_retention_failed=audit.continuation_retention_failure is not None,
    )
    return _admit(value, env_ref_names)


def encode_p4_outcome(value: RetainedP4Outcome, *, env_ref_names: frozenset[str] = frozenset()) -> str:
    return _dump_owned(_VALUE, _admit(value, env_ref_names))


def decode_p4_outcome(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> RetainedP4Outcome:
    return _admit(_decode_owned(_VALUE, encoded), env_ref_names)


def require_same_p4_outcome(
    value: RetainedP4Outcome, *, actual: _PersistOutcome, env_ref_names: frozenset[str] = frozenset()
) -> _PersistOutcome:
    """Compare a separately obtained real owner; equality grants no authority.

    The caller must obtain actual through the existing current writer or a
    separately qualified current-authorized receipt reader. This function
    never constructs a witness, AuditOutcome, exception, callback or lease.
    """
    admitted = _admit(value, env_ref_names)
    prepared = decode_prepared_p4(admitted.prepared_json, env_ref_names=env_ref_names)
    if encode_p4_outcome(
        capture_p4_outcome(prepared, actual, env_ref_names=env_ref_names), env_ref_names=env_ref_names
    ) != encode_p4_outcome(admitted, env_ref_names=env_ref_names):
        raise AuditIntegrityError("historical P4 output differs from the actual current owner result")
    return actual


class RetainedAuditOutcome(BaseModel):
    """Actual SQL writer result as DATA, before later effect verification."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    assistant_id: str | None
    unwind_audit_failed: bool
    checkpoint_disposition: Literal["committed", "no_new_checkpoint", "empty", "unavailable"]
    checkpoint: HistoricalCheckpointData | None
    current_state_id: str | None
    authority_revocation_json: str | None
    published_tool_rows_json: str
    published_assistant_content: str | None


_AUDIT_VALUE = TypeAdapter(RetainedAuditOutcome)


def decode_audit_outcome_data(
    encoded: str, *, prepared: PreparedP4Value, env_ref_names: frozenset[str] = frozenset()
) -> RetainedAuditOutcome:
    value = _decode_owned(_AUDIT_VALUE, encoded)
    # The complete prepared owner admits source/private/display/withheld bytes
    # under their actual selected domains. Publication can select only one of
    # its two complete actual cohorts, never a generic JSON-schema waiver.
    members = prepared_p4_members(prepared, env_ref_names=env_ref_names)
    revocation = None if value.authority_revocation_json is None else _decode_owned(_REVOCATION, value.authority_revocation_json)
    if value.checkpoint_disposition == "unavailable":
        if (
            not prepared.crash_pending
            or not value.unwind_audit_failed
            or value.assistant_id is not None
            or value.checkpoint is not None
            or value.current_state_id is not None
            or revocation is not None
            or value.published_tool_rows_json != "[]"
            or value.published_assistant_content is not None
        ):
            raise AuditIntegrityError("unavailable writer outcome invented committed facts")
        return value
    if value.assistant_id is None or value.unwind_audit_failed:
        raise AuditIntegrityError("successful writer outcome lost its assistant identity")
    if revocation is not None:
        require_no_credential_material(
            owned_json(_REVOCATION.dump_python(revocation, mode="json")), surface="retained writer revocation", env_ref_names=env_ref_names
        )
        if members.mutation_ceiling is None or revocation.ceiling != members.mutation_ceiling:
            raise AuditIntegrityError("writer revocation changed its original request ceiling")
    if members.authority_revocation is not None and revocation != members.authority_revocation:
        raise AuditIntegrityError("writer outcome discarded its original revocation")
    expected_rows = members.revoked_rows if revocation is not None else members.redacted_rows
    if value.published_tool_rows_json != _p4_rows_json(expected_rows, env_ref_names=env_ref_names) or value.published_assistant_content != (
        "" if revocation is not None else prepared.assistant_content
    ):
        raise AuditIntegrityError("writer outcome changed the locked publication choice")
    require_no_credential_material(
        [value.assistant_id, value.current_state_id], surface="retained writer identities", env_ref_names=env_ref_names
    )
    if value.checkpoint_disposition == "committed":
        if value.checkpoint is None or revocation is not None:
            raise AuditIntegrityError("writer outcome lost its checkpoint or published a refused graph")
        record = historical_checkpoint_record(value.checkpoint, env_ref_names=env_ref_names)
        if (
            str(record.id) != value.current_state_id
            or str(record.session_id) != prepared.session_id
            or value.checkpoint.creator_operation_id != prepared.creator_operation_id
        ):
            raise AuditIntegrityError("writer checkpoint differs from preparing custody")
    elif value.checkpoint is not None:
        raise AuditIntegrityError("nongraph writer outcome invented a checkpoint")
    elif value.checkpoint_disposition == "empty" and value.current_state_id is not None:
        raise AuditIntegrityError("empty writer outcome invented a state")
    elif value.checkpoint_disposition == "no_new_checkpoint" and value.current_state_id is None:
        raise AuditIntegrityError("inherited writer outcome lost its state")
    return value


def encode_audit_outcome_data(outcome: AuditOutcome, *, prepared: PreparedP4Value, env_ref_names: frozenset[str] = frozenset()) -> str:
    if type(outcome) is not AuditOutcome or outcome.continuation_retention_failure is not None:
        raise TypeError("writer outcome retention requires actual AuditOutcome")
    value = RetainedAuditOutcome(
        assistant_id=outcome.assistant_id,
        unwind_audit_failed=outcome.unwind_audit_failed,
        checkpoint_disposition=outcome.checkpoint_disposition,
        checkpoint=None if outcome.checkpoint is None else _checkpoint_data(outcome.checkpoint),
        current_state_id=outcome.current_state_id,
        authority_revocation_json=None if outcome.authority_revocation is None else _dump_owned(_REVOCATION, outcome.authority_revocation),
        published_tool_rows_json=_p4_rows_json(outcome.published_tool_rows, env_ref_names=env_ref_names),
        published_assistant_content=outcome.published_assistant_content,
    )
    encoded = _dump_owned(_AUDIT_VALUE, value)
    decode_audit_outcome_data(encoded, prepared=prepared, env_ref_names=env_ref_names)
    return encoded
