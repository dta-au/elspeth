from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from sqlalchemy import select

from elspeth.contracts.composer_interpretation import InterpretationEventRecord
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.attachment_admission import AttachmentReadAdmission
from elspeth.web.composer.continuation_attachment_history import RetainedAttachmentInspection
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.credential_guard import require_no_credential_material
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.protocol import CompositionProposalRecord, PipelineProposalPublicMetadata

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from elspeth.web.composer.state import CompositionState
    from elspeth.web.composer.tools._common import ToolResult
    from elspeth.web.composer.tools.blobs import BlobToolRecord


class PersistedComposerTurnReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["persisted_turn"] = "persisted_turn"
    effect_key: str
    input_sha256: str
    assistant_id: UUID | None
    current_state_id: UUID | None
    checkpoint_disposition: Literal["committed", "no_new_checkpoint", "empty", "unavailable"]
    creator_operation_id: str
    inserted_state_ids: tuple[UUID, ...]
    authored_state_hash: str | None
    chat_message_ids: tuple[UUID, ...]
    # None denotes the old passive metadata shape. New continuation writers
    # and readers require v2 and its full input/result, never fill old gaps.
    schema_version: Literal["composer.persisted-turn.v2"] | None = None
    prepared_p4_json: str | None = None
    audit_outcome_json: str | None = None
    unwind_audit_failed: bool = False
    invocation_ids: tuple[str, ...] = ()
    invocation_hashes: tuple[str, ...] = ()
    withheld_hashes: tuple[str, ...] = ()
    chat_message_hashes: tuple[str, ...] = ()
    rejection_ids: tuple[str, ...] = ()
    rejection_hashes: tuple[str, ...] = ()
    state_row_hashes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def actual_checkpoint_bundle(self) -> Self:
        if self.schema_version is not None and (self.prepared_p4_json is None or self.audit_outcome_json is None):
            raise ValueError("complete P4 receipt lost its actual input or writer result")
        if self.checkpoint_disposition == "unavailable":
            if (
                self.schema_version is None
                or not self.unwind_audit_failed
                or self.assistant_id is not None
                or self.current_state_id is not None
                or self.inserted_state_ids
                or self.authored_state_hash is not None
                or self.chat_message_ids
                or self.invocation_ids
                or self.invocation_hashes
                or self.withheld_hashes
                or self.chat_message_hashes
                or self.rejection_ids
                or self.rejection_hashes
                or self.state_row_hashes
            ):
                raise ValueError("unavailable P4 receipt invented committed rows")
            return self
        if self.unwind_audit_failed or self.assistant_id is None:
            raise ValueError("successful P4 receipt lost its actual assistant identity")
        if self.checkpoint_disposition == "committed":
            if not self.inserted_state_ids or self.current_state_id != self.inserted_state_ids[-1] or self.authored_state_hash is None:
                raise ValueError("P4 receipt lost its actual inserted checkpoint bundle")
        elif self.inserted_state_ids or self.authored_state_hash is not None:
            raise ValueError("P4 receipt invented a newly inserted checkpoint")
        if self.schema_version is not None:
            if self.checkpoint_disposition == "empty" and self.current_state_id is not None:
                raise ValueError("empty P4 receipt invented a saved state")
            if self.checkpoint_disposition == "no_new_checkpoint" and self.current_state_id is None:
                raise ValueError("inherited P4 receipt lost its actual saved state")
        if self.assistant_id not in self.chat_message_ids or len(set(self.chat_message_ids)) != len(self.chat_message_ids):
            raise ValueError("P4 receipt lost its actual distinct transcript rows")
        if len(set(self.invocation_ids)) != len(self.invocation_ids) or len(self.invocation_hashes) != len(self.invocation_ids):
            raise ValueError("P4 receipt lost its exact full invocation cohort")
        return self


_PROPOSAL = TypeAdapter(CompositionProposalRecord)
_PIPELINE_METADATA = TypeAdapter(PipelineProposalPublicMetadata)


def encode_proposal_owner_result(record: CompositionProposalRecord) -> str:
    if type(record) is not CompositionProposalRecord:
        raise TypeError("proposal retention requires its actual nominal writer result")
    if not isinstance(record.arguments_json, Mapping) or not isinstance(record.arguments_redacted_json, Mapping):
        raise AuditIntegrityError("proposal retention requires argument objects")
    payload = _PROPOSAL.dump_python(
        record,
        mode="json",
        warnings="error",
        exclude={"arguments_json", "arguments_redacted_json", "pipeline_metadata"},
    )
    payload["arguments_json"] = owned_json(record.arguments_json)
    payload["arguments_redacted_json"] = owned_json(record.arguments_redacted_json)
    metadata = record.pipeline_metadata
    if metadata is None:
        payload["pipeline_metadata"] = None
    else:
        if type(metadata) is not PipelineProposalPublicMetadata or not isinstance(metadata.base, Mapping):
            raise AuditIntegrityError("proposal retention requires actual pipeline metadata with a base object")
        metadata_payload = _PIPELINE_METADATA.dump_python(metadata, mode="json", warnings="error", exclude={"base"})
        metadata_payload["base"] = owned_json(metadata.base)
        payload["pipeline_metadata"] = metadata_payload
    return canonical_json(payload)


def decode_proposal_owner_result(encoded: str) -> CompositionProposalRecord:
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("retained proposal result is not strict JSON")
    try:
        result = _PROPOSAL.validate_json(canonical_json(value), strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("retained proposal result violates its owned codec") from exc
    if encode_proposal_owner_result(result) != canonical_json(value):
        raise AuditIntegrityError("retained proposal result lost fields during decode")
    return result


_INTERPRETATION = TypeAdapter(InterpretationEventRecord)


def encode_interpretation_owner_result(record: InterpretationEventRecord) -> str:
    if type(record) is not InterpretationEventRecord:
        raise TypeError("interpretation retention requires its actual nominal writer result")
    return canonical_json(_INTERPRETATION.dump_python(record, mode="json", warnings="error"))


def decode_interpretation_owner_result(encoded: str) -> InterpretationEventRecord:
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("retained interpretation result is not strict JSON")
    try:
        result = _INTERPRETATION.validate_json(canonical_json(value), strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("retained interpretation result violates its owned codec") from exc
    if encode_interpretation_owner_result(result) != canonical_json(value):
        raise AuditIntegrityError("retained interpretation result lost fields during decode")
    return result


class ContinuationBlobVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    blob_id: UUID
    content_hash: str | None
    size_bytes: int = Field(ge=0)
    filename: str
    mime_type: str
    status: Literal["pending", "ready", "error"]
    created_by: Literal["user", "assistant", "pipeline"]
    creation_modality: str


class ContinuationReadObservations:
    __slots__ = ("_inspections", "_versions")

    def __init__(self) -> None:
        self._versions: dict[UUID, ContinuationBlobVersion] = {}
        self._inspections: list[AttachmentReadAdmission] = []

    def observe_blob(self, record: BlobToolRecord) -> None:
        from elspeth.contracts.blobs import BLOB_STATUSES
        from elspeth.contracts.enums import CreationModality

        value = ContinuationBlobVersion.model_validate(
            {
                "blob_id": UUID(record["id"]),
                "content_hash": record["content_hash"],
                "size_bytes": record["size_bytes"],
                "filename": record["filename"],
                "mime_type": record["mime_type"],
                "status": record["status"],
                "created_by": record["created_by"],
                "creation_modality": record["creation_modality"],
            }
        )
        if value.status not in BLOB_STATUSES or value.creation_modality not in CreationModality:
            raise AuditIntegrityError("read observation contains an invalid recorded blob vocabulary")
        if value.blob_id in self._versions and self._versions[value.blob_id] != value:
            raise AuditIntegrityError("one read observed two versions of the same blob")
        self._versions[value.blob_id] = value

    def observe_attachment(self, inspection: AttachmentReadAdmission) -> None:
        if type(inspection) is not AttachmentReadAdmission:
            raise TypeError("attachment observation requires the actual inspection result")
        observed = self._versions.get(UUID(inspection.blob_id))
        if (
            observed is None
            or observed.status != "ready"
            or (observed.content_hash, observed.size_bytes) != (inspection.content_hash, inspection.size_bytes)
        ):
            raise AuditIntegrityError("attachment inspection differs from its actual observed blob")
        self._inspections.append(inspection)

    @property
    def attachment_inspections(self) -> tuple[AttachmentReadAdmission, ...]:
        return tuple(self._inspections)

    @property
    def versions(self) -> tuple[ContinuationBlobVersion, ...]:
        return tuple(self._versions[key] for key in sorted(self._versions, key=str))


class ContinuationReadOwnerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["read"] = "read"
    tool_name: str
    arguments_json: str
    response_json: str
    tool_result_json: str
    blob_versions: tuple[ContinuationBlobVersion, ...]
    prescribing_provider_call_id: str | None = None
    attachment_inspections: tuple[RetainedAttachmentInspection, ...] = ()


def re_admit_read_owner_result(
    result: ContinuationReadOwnerResult,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    from elspeth.contracts.blobs import BLOB_STATUSES
    from elspeth.contracts.enums import CreationModality
    from elspeth.web.composer.continuation_tool_batch import decode_tool_result_json, encode_tool_result_value
    from elspeth.web.composer.discovery_response import admit_discovery_result
    from elspeth.web.composer.tools._registry import response_contract_for
    from elspeth.web.composer.tools.continuation_support import continuation_tool_support

    arguments, argument_error = parse_json_strict(result.arguments_json)
    envelope, response_error = parse_json_strict(result.response_json)
    if (
        argument_error is not None
        or type(arguments) is not dict
        or response_error is not None
        or type(envelope) is not dict
        or type(envelope.get("success")) is not bool
    ):
        raise AuditIntegrityError("retained read has an invalid arguments/response envelope")
    if continuation_tool_support(result.tool_name, arguments) != "read":
        raise AuditIntegrityError("retained read no longer has a supported actual read owner")
    contract = response_contract_for(result.tool_name)
    if contract is None:
        raise AuditIntegrityError("retained read no longer has its selected response contract")
    if envelope["success"]:
        if "data" not in envelope:
            raise AuditIntegrityError("retained successful read has no actual data")
        admitted = contract.admit(envelope["data"])
        if canonical_json(admitted.to_wire()) != canonical_json(envelope["data"]):
            raise AuditIntegrityError("retained read changed under current selected response admission")
    elif "data" in envelope:
        raise AuditIntegrityError("retained failed read invented data")
    nominal = decode_tool_result_json(result.tool_result_json, env_ref_names=env_ref_names)
    if nominal.affected_nodes:
        raise AuditIntegrityError("retained read contains an authored graph effect")
    if (
        canonical_json(encode_tool_result_value(result.tool_name, nominal, env_ref_names=env_ref_names).model_dump(mode="json"))
        != result.tool_result_json
    ):
        raise AuditIntegrityError("retained read changed its complete selected nominal result")
    if canonical_json(admit_discovery_result(result.tool_name, nominal).to_dict()) != result.response_json:
        raise AuditIntegrityError("retained read envelope differs from its actual complete nominal result")
    for observed in result.blob_versions:
        if observed.status not in BLOB_STATUSES or observed.creation_modality not in CreationModality:
            raise AuditIntegrityError("retained read contains an invalid recorded blob vocabulary")
    if len({item.blob_id for item in result.blob_versions}) != len(result.blob_versions):
        raise AuditIntegrityError("retained read duplicated a blob observation")
    if result.tool_name in {"read_attachment", "get_blob_content"} and envelope["success"] and not result.attachment_inspections:
        raise AuditIntegrityError("retained attachment success lost its complete-original inspection")
    if result.attachment_inspections:
        if result.tool_name not in {"read_attachment", "get_blob_content"}:
            raise AuditIntegrityError("attachment observations belong to another selected tool")
        for inspection in result.attachment_inspections:
            if type(inspection) is not RetainedAttachmentInspection:
                raise AuditIntegrityError("retained attachment inspection lost its nominal owner")
            try:
                checked_inspection = RetainedAttachmentInspection.model_validate_json(canonical_json(inspection.model_dump(mode="json")))
            except ValidationError as error:
                raise AuditIntegrityError("retained attachment inspection violates its owned fields") from error
            if checked_inspection != inspection:
                raise AuditIntegrityError("retained attachment inspection changed under complete admission")
            require_no_credential_material(inspection.model_dump(mode="json"), surface="retained attachment inspection")
            matching = tuple(item for item in result.blob_versions if str(item.blob_id) == inspection.blob_id)
            if (
                len(matching) != 1
                or matching[0].status != "ready"
                or (matching[0].content_hash, matching[0].size_bytes) != (inspection.content_hash, inspection.size_bytes)
            ):
                raise AuditIntegrityError("retained attachment inspection differs from its blob version")
            if arguments.get("blob_id") != inspection.blob_id:
                raise AuditIntegrityError("retained attachment inspection belongs to another selected call")
        if envelope["success"]:
            data = envelope["data"]
            if type(data) is not dict:
                raise AuditIntegrityError("retained attachment success lost its actual content object")
            if any(
                (item.blob_id, item.content_hash, item.size_bytes)
                != (data.get("blob_id"), data.get("content_hash"), data.get("size_bytes"))
                for item in result.attachment_inspections
            ):
                raise AuditIntegrityError("retained attachment response differs from its inspected original")
    return envelope


def decode_read_owner_result(encoded: str, *, env_ref_names: frozenset[str] = frozenset()) -> ContinuationReadOwnerResult:
    parsed, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("retained read is not strict JSON")
    try:
        result = ContinuationReadOwnerResult.model_validate_json(canonical_json(parsed))
    except ValidationError as exc:
        raise AuditIntegrityError("retained read violates its owned codec") from exc
    if canonical_json(result.model_dump(mode="json")) != canonical_json(parsed):
        raise AuditIntegrityError("retained read lost fields during decode")
    re_admit_read_owner_result(result, env_ref_names=env_ref_names)
    return result


def build_read_owner_result(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    result: ToolResult,
    pre_state: CompositionState,
    observations: ContinuationReadObservations,
    env_ref_names: frozenset[str] = frozenset(),
) -> ContinuationReadOwnerResult:
    from elspeth.web.composer.continuation_tool_batch import encode_tool_result_value
    from elspeth.web.composer.discovery_response import admit_discovery_result
    from elspeth.web.composer.tools._common import ToolResult
    from elspeth.web.composer.tools.continuation_support import continuation_tool_support

    if (
        type(result) is not ToolResult
        or result.updated_state is not pre_state
        or result.affected_nodes
        or continuation_tool_support(tool_name, arguments) != "read"
        or type(observations) is not ContinuationReadObservations
    ):
        raise AuditIntegrityError("read owner did not receive the actual nonmutating handler result")
    admitted = admit_discovery_result(tool_name, result)
    value = ContinuationReadOwnerResult(
        tool_name=tool_name,
        arguments_json=canonical_json(arguments),
        response_json=canonical_json(admitted.to_dict()),
        blob_versions=observations.versions,
        attachment_inspections=tuple(RetainedAttachmentInspection.from_actual(item) for item in observations.attachment_inspections),
        tool_result_json=canonical_json(encode_tool_result_value(tool_name, result, env_ref_names=env_ref_names).model_dump(mode="json")),
    )
    re_admit_read_owner_result(value, env_ref_names=env_ref_names)
    return value


def require_read_versions_on_connection(
    conn: Connection,
    *,
    session_id: str,
    result: ContinuationReadOwnerResult,
    env_ref_names: frozenset[str] = frozenset(),
) -> None:
    from elspeth.web.blobs.service import _guard_blob_row_literals

    re_admit_read_owner_result(result, env_ref_names=env_ref_names)
    if any(item.session_id != session_id for item in result.attachment_inspections):
        raise AuditIntegrityError("retained attachment inspection belongs to another session")
    for observed in result.blob_versions:
        row = conn.execute(
            select(blobs_table).where(
                blobs_table.c.session_id == session_id,
                blobs_table.c.id == str(observed.blob_id),
            )
        ).one_or_none()
        if row is None:
            raise AuditIntegrityError("retained blob observation is no longer present in this session")
        _guard_blob_row_literals(row)
        actual = ContinuationBlobVersion(
            blob_id=UUID(row.id),
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            filename=row.filename,
            mime_type=row.mime_type,
            status=row.status,
            created_by=row.created_by,
            creation_modality=row.creation_modality,
        )
        if actual != observed:
            raise AuditIntegrityError("retained blob observation differs from its current immutable version")
