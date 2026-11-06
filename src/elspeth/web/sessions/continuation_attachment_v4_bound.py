"""No-I/O V4 source plan for the original bounded attachment reader.

These are leaf and owner-result maxima. The value-batch SQL owner still must
reserve every duplicate occurrence in its complete history/application tail
before it issues a read. A decoded plan is never a source approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import Connection, select

from elspeth.contracts.credential_material import CREDENTIAL_DETECTOR_VERSION
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.continuation_attachment_history import RetainedAttachmentInspection
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_limits import CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.composer.continuation_tool_batch import encode_tool_result_value
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.redaction import GetBlobContentArgumentsModel, ReadAttachmentArgumentsModel
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolContext, _discovery_result, _failure_result
from elspeth.web.composer.tools._dispatch import finalize_tool_result
from elspeth.web.composer.tools.blobs import _BLOB_CONTENT_PAGE_MAX_UTF8_BYTES, _blob_row_to_tool_dict
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation_attachment_result import _selected
from elspeth.web.sessions.continuation_owner_results import ContinuationBlobVersion, ContinuationReadObservations
from elspeth.web.sessions.models import blobs_table as blobs
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import sessions_table as sessions


@dataclass(frozen=True, slots=True)
class V4AttachmentReadRefusal:
    tool_name: str
    reason: Literal["unsupported_producer_bound", "invalid_arguments", "source_not_ready", "private_document_bound"]


@dataclass(frozen=True, slots=True)
class V4AttachmentReadBound:
    session_id: str
    operation_id: str
    actor_user_id: str
    session_operation_id: str
    session_operation_epoch: int
    lease_sha256: str
    creator_sequence: int
    creator_generation: int
    claim_attempt: int
    selected_checkpoint_id: str
    selected_cursor: int
    selected_snapshot_sha256: str
    tool_name: str
    arguments_json: str
    state_json: str
    source: ContinuationBlobVersion
    names: frozenset[str]
    catalog: PolicyCatalogView
    maximum_response_bytes: int
    maximum_tool_result_bytes: int
    maximum_owner_result_bytes: int
    contract_sha256: str


class V4AttachmentReadManifest(BaseModel):
    """Complete immutable source projection hashed by the bound owner."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    session_id: str
    operation_id: str
    actor_user_id: str
    session_operation_id: str
    session_operation_epoch: int
    lease_sha256: str
    creator_sequence: int
    creator_generation: int
    claim_attempt: int
    selected_checkpoint_id: str
    selected_cursor: int
    selected_snapshot_sha256: str
    tool_name: str
    arguments_json: str
    state_json: str
    source: ContinuationBlobVersion
    names: tuple[str, ...]
    catalog_snapshot_sha256: str
    inspection_fingerprint_bytes: Literal[64]
    maximum_response_bytes: int
    maximum_tool_result_bytes: int
    maximum_owner_result_bytes: int


def _failure_messages(blob_id: str, size: int) -> tuple[str, ...]:
    """Closed returned failures of the two original content handlers.

    UUID syntax and current ready metadata are checked before this bound.
    UTF-8's three decoder failure reasons are the only variable decode text;
    using the maximum original byte offset bounds its decimal rendering.
    Integrity/fence/I/O exceptions propagate and cannot supply a ToolResult.
    """
    return (
        "The complete attachment was refused by the current inspection policy; no content was returned.",
        f"Blob '{blob_id}' not found.",
        f"Blob storage file missing for '{blob_id}'.",
        f"Blob '{blob_id}' is not readable — status is 'pending', expected 'ready'.",
        f"Blob '{blob_id}' is not readable — status is 'error', expected 'ready'.",
        "Blob content changed; restart inspection with its current content hash.",
        "Blob content offset exceeds its decoded character length.",
        "Blob page cannot fit the bounded tool response; no content was returned.",
        f"The attachment is {size} bytes, exceeding the current {CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES}-byte "
        "whole-input credential inspection limit. Neither read_attachment nor get_blob_content can disclose it "
        "under that limit; no content was returned.",
        f"The attachment's complete serialized response exceeds {CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES} UTF-8 bytes; "
        "no content was returned. Use get_blob_content for selected pages.",
        *(
            f"Blob '{blob_id}' is not valid UTF-8 text ({reason} at byte offset {size})."
            for reason in ("invalid start byte", "invalid continuation byte", "unexpected end of data")
        ),
    )


def attachment_read_result_maxima(
    *, tool_name: str, source: ContinuationBlobVersion, pre_state: CompositionState, names: frozenset[str], catalog: PolicyCatalogView
) -> tuple[int, int]:
    """Bound actual handler response and full ToolResult, including state.

    The existing page serializer caps get_blob_content at 4096 bytes; the
    whole-attachment serializer caps read_attachment at 1 MiB. The latter is
    tightened by the original byte count: any UTF-8 original contributes at
    most six ASCII JSON bytes per original byte. All metadata/state are exact.
    Nested canonical JSON strings can at most double their bytes because
    JSON text already contains no raw control characters.
    """
    payload = {
        "blob_id": str(source.blob_id),
        "filename": source.filename,
        "mime_type": source.mime_type,
        "content": "",
        "truncated": False,
        "size_bytes": 0,
        "created_by": source.created_by,
        "creation_modality": source.creation_modality,
        "content_hash": sha256(b"").hexdigest(),
        "total_chars": 0,
        "start_char": 0,
        "end_char": 0,
        "next_offset_chars": None,
    }
    provisional = _discovery_result(pre_state, payload)
    context = ToolContext(catalog=catalog, plugin_snapshot=catalog.snapshot)
    prior = catalog.validate_composition_state(pre_state).validation
    baseline = finalize_tool_result(provisional, tool_name=tool_name, catalog=catalog, context=context, prior_validation=prior)
    normalization_growth = max(
        0,
        len(serialize_admitted_discovery_result(admit_discovery_result(tool_name, baseline)).encode("utf-8"))
        - len(serialize_admitted_discovery_result(admit_discovery_result(tool_name, provisional)).encode("utf-8")),
    )
    encoded_baseline = encode_tool_result_value(tool_name, baseline, env_ref_names=names)
    success_fixed = len(canonical_json(encoded_baseline.model_copy(update={"data_json": ""}).model_dump(mode="json")).encode("utf-8"))
    if tool_name == "get_blob_content":
        response_max = _BLOB_CONTENT_PAGE_MAX_UTF8_BYTES + normalization_growth
    elif tool_name == "read_attachment":
        response_max = (
            min(
                CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES,
                len(serialize_admitted_discovery_result(admit_discovery_result(tool_name, provisional)).encode("utf-8"))
                + 6 * source.size_bytes
                + 3 * (len(str(source.size_bytes)) - 1),
            )
            + normalization_growth
        )
    else:
        raise AuditIntegrityError("attachment result maximum requires an actually bounded content producer")
    result_max = success_fixed + 2 * response_max
    for message in _failure_messages(str(source.blob_id), source.size_bytes):
        failed = finalize_tool_result(
            _failure_result(pre_state, message), tool_name=tool_name, catalog=catalog, context=context, prior_validation=prior
        )
        response_max = max(response_max, len(canonical_json(admit_discovery_result(tool_name, failed).to_dict()).encode("utf-8")))
        result_max = max(
            result_max,
            len(canonical_json(encode_tool_result_value(tool_name, failed, env_ref_names=names).model_dump(mode="json")).encode("utf-8")),
        )
    return response_max, result_max


def _maximum_owner_result_bytes(
    *,
    session_id: str,
    tool_name: str,
    arguments_json: str,
    source: ContinuationBlobVersion,
    response_max: int,
    result_max: int,
) -> int:
    if source.content_hash is None:
        raise AuditIntegrityError("ready attachment source lost its original full content hash")
    inspection_record = RetainedAttachmentInspection(
        session_id=session_id,
        blob_id=str(source.blob_id),
        content_hash=source.content_hash,
        size_bytes=source.size_bytes,
        detector_version=CREDENTIAL_DETECTOR_VERSION,
        policy_fingerprint="0" * 64,
    )
    # Empty variable strings contribute their own quotes in this fixed shape.
    # Each actual canonical JSON string can at most double at this next layer.
    owner_fixed = len(
        canonical_json(
            {
                "kind": "read",
                "tool_name": tool_name,
                "arguments_json": arguments_json,
                "response_json": "",
                "tool_result_json": "",
                "blob_versions": [source.model_dump(mode="json")],
                "prescribing_provider_call_id": None,
                "attachment_inspections": [inspection_record.model_dump(mode="json")],
            }
        ).encode("utf-8")
    )
    return owner_fixed + 2 * response_max + 2 * result_max


def prepare_v4_attachment_read_bound_on_connection(
    conn: Connection,
    *,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    inventory: CurrentContinuationEnvInventory,
    tool_name: str,
    arguments_json: str,
    pre_state: CompositionState,
    catalog: PolicyCatalogView | None = None,
) -> V4AttachmentReadBound | V4AttachmentReadRefusal:
    """Current SQL metadata only; no effect issue, file access or source grant."""
    current, names = _selected(conn, running, selected, inventory)
    policy = decode_continuation_policy(selected.stack.admission_policy_json, selected.stack.policy_hash)
    if type(policy) is not ContinuationAdmissionPolicyV4 or type(pre_state) is not CompositionState:
        raise AuditIntegrityError("V4 attachment bound requires its current owned policy/state")
    if type(catalog) is not PolicyCatalogView or tool_name not in ("get_blob_content", "read_attachment"):
        # In particular, inspect_source's registry codecs and fact grammar do
        # not yet establish a closed complete-output bound from its 8KiB input.
        return V4AttachmentReadRefusal(tool_name, "unsupported_producer_bound")
    try:
        validated: GetBlobContentArgumentsModel | ReadAttachmentArgumentsModel
        if tool_name == "get_blob_content":
            validated = GetBlobContentArgumentsModel.model_validate_json(arguments_json, strict=True)
        else:
            validated = ReadAttachmentArgumentsModel.model_validate_json(arguments_json, strict=True)
        if canonical_json(validated.model_dump(mode="json", exclude_unset=True)) != arguments_json:
            return V4AttachmentReadRefusal(tool_name, "invalid_arguments")
        blob_id = UUID(validated.blob_id)
        if str(blob_id) != validated.blob_id:
            return V4AttachmentReadRefusal(tool_name, "invalid_arguments")
    except (ValueError, ValidationError):
        return V4AttachmentReadRefusal(tool_name, "invalid_arguments")
    sid, operation = str(running.claim.session_id), running.claim.operation_id
    actor = conn.execute(select(jobs.c.actor_user_id).where(jobs.c.session_id == sid, jobs.c.operation_id == operation)).scalar_one()
    principal = conn.execute(select(sessions.c.user_id, sessions.c.archived_at).where(sessions.c.id == sid)).one()
    if actor != principal.user_id or principal.archived_at is not None:
        raise AuditIntegrityError("V4 attachment bound changed its original current actor")
    row = conn.execute(select(blobs).where(blobs.c.session_id == sid, blobs.c.id == str(blob_id))).one_or_none()
    if row is None or row.status != "ready" or row.content_hash is None:
        return V4AttachmentReadRefusal(tool_name, "source_not_ready")
    observations = ContinuationReadObservations()
    observations.observe_blob(_blob_row_to_tool_dict(row))
    source = observations.versions[0]
    if source.content_hash is None:
        raise AuditIntegrityError("ready attachment source lost its original full content hash")
    response_max, result_max = attachment_read_result_maxima(
        tool_name=tool_name, source=source, pre_state=pre_state, names=names, catalog=catalog
    )
    owner_max = _maximum_owner_result_bytes(
        session_id=sid,
        tool_name=tool_name,
        arguments_json=arguments_json,
        source=source,
        response_max=response_max,
        result_max=result_max,
    )
    if max(response_max, result_max, owner_max) > CONTINUATION_PRIVATE_DOCUMENT_MAX_UTF8_BYTES:
        return V4AttachmentReadRefusal(tool_name, "private_document_bound")
    fence = running.session_operation_context.fence
    lease_hash = sha256(fence.lease_token.encode("utf-8")).hexdigest()
    snapshot_hash = sha256(encode_continuation_snapshot(selected.stack).encode("utf-8")).hexdigest()
    manifest = V4AttachmentReadManifest(
        session_id=sid,
        operation_id=operation,
        actor_user_id=actor,
        session_operation_id=fence.operation_id,
        session_operation_epoch=fence.operation_epoch,
        lease_sha256=lease_hash,
        creator_sequence=running.claim.segment_sequence,
        creator_generation=running.claim.ownership_generation,
        claim_attempt=running.claim.attempt,
        selected_checkpoint_id=current.checkpoint_id,
        selected_cursor=current.cursor,
        selected_snapshot_sha256=snapshot_hash,
        tool_name=tool_name,
        arguments_json=arguments_json,
        state_json=canonical_json(pre_state.to_dict()),
        source=source,
        names=tuple(sorted(names)),
        catalog_snapshot_sha256=catalog.snapshot.snapshot_hash,
        inspection_fingerprint_bytes=64,
        maximum_response_bytes=response_max,
        maximum_tool_result_bytes=result_max,
        maximum_owner_result_bytes=owner_max,
    )
    return V4AttachmentReadBound(
        sid,
        operation,
        actor,
        fence.operation_id,
        fence.operation_epoch,
        lease_hash,
        running.claim.segment_sequence,
        running.claim.ownership_generation,
        running.claim.attempt,
        current.checkpoint_id,
        current.cursor,
        snapshot_hash,
        tool_name,
        arguments_json,
        canonical_json(pre_state.to_dict()),
        source,
        names,
        catalog,
        response_max,
        result_max,
        owner_max,
        sha256(canonical_json(manifest.model_dump(mode="json")).encode("utf-8")).hexdigest(),
    )


def _bound_manifest(bound: V4AttachmentReadBound) -> V4AttachmentReadManifest:
    return V4AttachmentReadManifest(
        session_id=bound.session_id,
        operation_id=bound.operation_id,
        actor_user_id=bound.actor_user_id,
        session_operation_id=bound.session_operation_id,
        session_operation_epoch=bound.session_operation_epoch,
        lease_sha256=bound.lease_sha256,
        creator_sequence=bound.creator_sequence,
        creator_generation=bound.creator_generation,
        claim_attempt=bound.claim_attempt,
        selected_checkpoint_id=bound.selected_checkpoint_id,
        selected_cursor=bound.selected_cursor,
        selected_snapshot_sha256=bound.selected_snapshot_sha256,
        tool_name=bound.tool_name,
        arguments_json=bound.arguments_json,
        state_json=bound.state_json,
        source=bound.source,
        names=tuple(sorted(bound.names)),
        catalog_snapshot_sha256=bound.catalog.snapshot.snapshot_hash,
        inspection_fingerprint_bytes=64,
        maximum_response_bytes=bound.maximum_response_bytes,
        maximum_tool_result_bytes=bound.maximum_tool_result_bytes,
        maximum_owner_result_bytes=bound.maximum_owner_result_bytes,
    )


def require_v4_attachment_read_bound(bound: V4AttachmentReadBound, *, pre_state: CompositionState) -> None:
    """Rederive the complete leaf contract; this does not grant a file read."""
    if type(bound) is not V4AttachmentReadBound or type(pre_state) is not CompositionState or type(bound.catalog) is not PolicyCatalogView:
        raise TypeError("V4 attachment bound integrity requires owned source/state/catalog")
    if canonical_json(pre_state.to_dict()) != bound.state_json:
        raise AuditIntegrityError("V4 attachment bound changed its original state contract")
    response_max, result_max = attachment_read_result_maxima(
        tool_name=bound.tool_name, source=bound.source, pre_state=pre_state, names=bound.names, catalog=bound.catalog
    )
    owner_max = _maximum_owner_result_bytes(
        session_id=bound.session_id,
        tool_name=bound.tool_name,
        arguments_json=bound.arguments_json,
        source=bound.source,
        response_max=response_max,
        result_max=result_max,
    )
    if (response_max, result_max, owner_max) != (
        bound.maximum_response_bytes,
        bound.maximum_tool_result_bytes,
        bound.maximum_owner_result_bytes,
    ):
        raise AuditIntegrityError("V4 attachment bound changed its original complete result maxima")
    if sha256(canonical_json(_bound_manifest(bound).model_dump(mode="json")).encode("utf-8")).hexdigest() != bound.contract_sha256:
        raise AuditIntegrityError("V4 attachment bound changed its original source contract")
