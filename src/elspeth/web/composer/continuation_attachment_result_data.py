"""Complete history-consumed read DATA; no tool application or read clearance."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_batch_data import decode_prepared_batch_data
from elspeth.web.composer.continuation_codec import PrivateChunk, encode_private_chunk, verify_private_chunk
from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result


class AttachmentHistoryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.attachment-history-batch.v1"] = "composer.attachment-history-batch.v1"
    prepared_json: str
    result_json: str


def encode_attachment_history_batch(value: AttachmentHistoryBatch, *, env_ref_names: frozenset[str]) -> PrivateChunk:
    if type(value) is not AttachmentHistoryBatch:
        raise TypeError("attachment history batch requires its actual complete owner")
    canonical = canonical_json(value.model_dump(mode="json"))
    try:
        checked = AttachmentHistoryBatch.model_validate_json(canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("attachment history batch lost its complete fields") from error
    if canonical_json(checked.model_dump(mode="json")) != canonical:
        raise AuditIntegrityError("attachment history batch changed fields under admission")
    decode_prepared_batch_data(value.prepared_json, env_ref_names=env_ref_names)
    decode_attachment_published_result(
        encode_private_chunk(codec="composer.owner-result.v1", canonical=value.result_json, env_ref_names=env_ref_names),
        env_ref_names=env_ref_names,
    )
    return encode_private_chunk(codec="composer.tool-batch.v1", canonical=canonical, env_ref_names=env_ref_names)


def decode_attachment_history_batch(chunk: PrivateChunk, *, env_ref_names: frozenset[str]) -> AttachmentHistoryBatch:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.tool-batch.v1":
        raise AuditIntegrityError("attachment history batch changed its actual outer codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        value = AttachmentHistoryBatch.model_validate_json(chunk.canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("attachment history batch lost its complete schema") from error
    if encode_attachment_history_batch(value, env_ref_names=env_ref_names) != chunk:
        raise AuditIntegrityError("attachment history batch lost fields or exact bytes")
    return value


class AttachmentPublishedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.attachment-result.v1"] = "composer.attachment-result.v1"
    effect_key: str
    input_sha256: str
    creator_sequence: int
    creator_generation: int
    creator_operation_id: str
    creator_operation_epoch: int
    result_json: str
    read_audit_json: str | None = None


def encode_attachment_published_result(value: AttachmentPublishedResult, *, env_ref_names: frozenset[str]) -> PrivateChunk:
    from elspeth.contracts.hashing import is_lower_sha256_hex
    from elspeth.web.credential_guard import require_no_credential_material

    if type(value) is not AttachmentPublishedResult:
        raise TypeError("attachment publication requires its complete DATA owner")
    # Old history-only results keep their exact bytes. Only an actual post-read
    # observation enables the later finisher; absence never implies zero work.
    encoded = canonical_json(value.model_dump(mode="json", exclude={"read_audit_json"} if value.read_audit_json is None else set()))
    try:
        checked = AttachmentPublishedResult.model_validate_json(encoded, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("attachment publication lost its complete fields") from error
    if (
        checked != value
        or not is_lower_sha256_hex(value.input_sha256)
        or (
            value.creator_sequence < 1
            or value.creator_generation < 1
            or value.creator_operation_epoch < 1
            or not value.effect_key
            or not value.creator_operation_id
        )
    ):
        raise AuditIntegrityError("attachment publication lost its historical creator/input identity")
    require_no_credential_material((value.effect_key, value.creator_operation_id), surface="attachment result locator")
    if value.read_audit_json is not None:
        from elspeth.web.composer.continuation_dispatch_audit import decode_dispatch_audit

        audit = decode_dispatch_audit(value.read_audit_json, env_ref_names=env_ref_names)
        if audit.tool_name not in ("read_attachment", "get_blob_content"):
            raise AuditIntegrityError("attachment observation changed its actual read domain")
    result = decode_read_owner_result(value.result_json, env_ref_names=env_ref_names)
    if canonical_json(result.model_dump(mode="json")) != value.result_json:
        raise AuditIntegrityError("attachment publication lost complete read bytes")
    return encode_private_chunk(codec="composer.owner-result.v1", canonical=encoded, env_ref_names=env_ref_names)


def decode_attachment_published_result(chunk: PrivateChunk, *, env_ref_names: frozenset[str]) -> AttachmentPublishedResult:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.owner-result.v1":
        raise AuditIntegrityError("attachment publication changed its actual outer codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        value = AttachmentPublishedResult.model_validate_json(chunk.canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("attachment publication lost its complete schema") from error
    if encode_attachment_published_result(value, env_ref_names=env_ref_names) != chunk:
        raise AuditIntegrityError("attachment publication lost fields or exact bytes")
    return value
