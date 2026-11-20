"""Custody-safe materialization for canonical ``set_pipeline`` proposals.

The preliminary candidate builder is deliberately side-effect free: it may
validate ``source.inline_blob`` using prepared bytes and a provisional path,
but it cannot publish those bytes.  This module owns the narrow transition
from that acceptable draft to exact proposal arguments containing only a
deterministic ``source.blob_id``.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import Engine

from elspeth.contracts.blobs import (
    AllowedMimeType,
    BlobRecord,
    InlineCustodyRequest,
)
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.blobs.service import (
    BlobServiceImpl,
    _normalized_inline_custody_fields,
    _persist_blob_content,
    content_hash,
    inline_custody_blob_id,
    inline_custody_storage_path,
    sanitize_filename,
)
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.sessions.protocol import SessionOperationAuthority

if TYPE_CHECKING:
    from elspeth.web.composer.tools.blobs import _PreparedBlobCreate


_AUDIT_REDACTION_MARKER = "[redacted inline content held for custody]"


def _detached_json(value: Any) -> Any:
    """Detach a decoded JSON tree without coercing or canonicalizing it."""
    if type(value) is dict:
        return {key: _detached_json(child) for key, child in value.items()}
    if type(value) is list:
        return [_detached_json(child) for child in value]
    return value


def inline_custody_audit_projection(arguments: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return an audit-safe projection before candidate validation begins.

    Malformed drafts still open a dispatch audit.  Replacing the entire
    untrusted ``content`` value up front prevents an invalid draft from
    leaking bytes through ``DispatchAudit`` before the redaction manifest can
    validate the surrounding shape.
    """
    projected_value = _detached_json(dict(arguments))
    if type(projected_value) is not dict:
        raise AuditIntegrityError("Pipeline custody audit projection must remain an object")
    projected = projected_value

    def _redact(value: Any) -> None:
        if type(value) is dict:
            for key, child in value.items():
                if key == "inline_blob":
                    value[key] = _AUDIT_REDACTION_MARKER
                else:
                    _redact(child)
        elif type(value) is list:
            for child in value:
                _redact(child)

    _redact(projected)
    return projected


def inline_custody_manifest_redaction_input(arguments: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Restore a schema-valid shell solely for manifest redaction.

    Dispatch auditing removes the whole untrusted ``inline_blob`` value before
    validation. The manifest still needs a structurally valid value so it can
    redact other sensitive pipeline fields; this shell contains no user bytes
    and is projected back to the whole-value marker before persistence.
    """
    restored = _detached_json(dict(arguments))
    if type(restored) is not dict:
        raise AuditIntegrityError("Pipeline custody manifest input must remain an object")

    def _restore(value: Any) -> None:
        if type(value) is dict:
            for key, child in value.items():
                if key == "inline_blob" and child == _AUDIT_REDACTION_MARKER:
                    value[key] = {
                        "filename": "redacted-inline-content",
                        "mime_type": "text/plain",
                        "content": _AUDIT_REDACTION_MARKER,
                    }
                else:
                    _restore(child)
        elif type(value) is list:
            for child in value:
                _restore(child)

    _restore(restored)
    return cast(dict[str, JsonValue], restored)


def _contains_inline_blob(value: Any) -> bool:
    if type(value) is dict:
        return "inline_blob" in value or any(_contains_inline_blob(child) for child in value.values())
    if type(value) is list:
        return any(_contains_inline_blob(child) for child in value)
    return False


@dataclass(frozen=True, slots=True)
class PipelineCustodyPreparation:
    """Pure result produced before custody performs filesystem or DB I/O.

    ``max_storage_per_session`` is the storage ceiling the plan authorized
    this preparation under. Finalization — whether mid-plan or deferred
    into a later staging settlement — refuses to enforce any other value:
    the plan-time and settle-time quotas are plumbed from independent
    sources (planner custody config vs route settings read), and without
    this recorded ceiling nothing would trip if they diverged.
    """

    arguments: Mapping[str, Any]
    request: InlineCustodyRequest
    blob_id: UUID
    max_storage_per_session: int
    additional_requests: tuple[InlineCustodyRequest, ...] = ()

    def __post_init__(self) -> None:
        if type(self.max_storage_per_session) is not int or self.max_storage_per_session <= 0:
            raise AuditIntegrityError("Pipeline custody max_storage_per_session must be a positive exact integer")
        freeze_fields(self, "arguments")


def _validate_prepared_inline_blob(
    inline_blob: Mapping[str, Any],
    prepared: _PreparedBlobCreate,
) -> None:
    """Fail closed unless candidate bytes exactly match removed arguments."""
    inline_content = inline_blob["content"] if "content" in inline_blob else None
    inline_mime_type = inline_blob["mime_type"] if "mime_type" in inline_blob else None
    inline_filename = inline_blob["filename"] if "filename" in inline_blob else None
    inline_description = inline_blob["description"] if "description" in inline_blob else None
    if type(inline_content) is not str:
        raise AuditIntegrityError("Pipeline custody inline content must be a string")
    if type(inline_mime_type) is not str:
        raise AuditIntegrityError("Pipeline custody inline MIME type must be a string")
    if type(inline_filename) is not str:
        raise AuditIntegrityError("Pipeline custody inline filename must be a string")
    if inline_description is not None and type(inline_description) is not str:
        raise AuditIntegrityError("Pipeline custody inline description must be a string or null")
    try:
        inline_bytes = inline_content.encode("utf-8")
        inline_safe_filename = sanitize_filename(inline_filename)
    except (UnicodeEncodeError, ValueError) as exc:
        raise AuditIntegrityError("Pipeline custody inline candidate metadata is invalid") from exc

    prepared_hash = content_hash(prepared.content_bytes)
    if type(prepared.content_hash) is not str or not hmac.compare_digest(prepared.content_hash, prepared_hash):
        raise AuditIntegrityError("Pipeline custody prepared content hash does not match prepared bytes")
    if not hmac.compare_digest(inline_bytes, prepared.content_bytes):
        raise AuditIntegrityError("Pipeline custody prepared bytes do not match inline arguments")
    if inline_mime_type != prepared.mime_type:
        raise AuditIntegrityError("Pipeline custody prepared MIME type does not match inline arguments")
    if inline_safe_filename != prepared.filename:
        raise AuditIntegrityError("Pipeline custody prepared filename does not match inline arguments")
    if inline_description != prepared.description:
        raise AuditIntegrityError("Pipeline custody prepared description does not match inline arguments")


def prompt_call_arguments_hash(arguments: Mapping[str, JsonValue], prepared_source: _PreparedBlobCreate | None) -> str:
    """Hash original call arguments with a stable stand-in for source bytes.

    Direct source creation assigns a fresh ID on each call. Prompt provenance
    therefore uses the source's declared file metadata and content hash.
    """
    skeleton = _detached_json(dict(arguments))
    if prepared_source is not None:
        source = skeleton.get("source")
        if type(source) is not dict:
            raise AuditIntegrityError("Pipeline custody requires canonical source arguments")
        inline_blob = source.get("inline_blob")
        if type(inline_blob) is not dict:
            raise AuditIntegrityError("Pipeline custody requires canonical source.inline_blob arguments")
        _validate_prepared_inline_blob(inline_blob, prepared_source)
        # Preserve every declared field of the original call. Only the source
        # bytes are replaced; source creation itself keeps its own identity.
        del inline_blob["content"]
        inline_blob["content_hash"] = prepared_source.content_hash
    return composer_authority_hash(skeleton)


def prepare_pipeline_custody(
    arguments: Mapping[str, Any],
    prepared: _PreparedBlobCreate | None,
    *,
    session_id: str,
    max_storage_per_session: int,
    prompt_blobs: tuple[tuple[str, _PreparedBlobCreate], ...] = (),
    prompt_arguments_hash: str | None = None,
) -> PipelineCustodyPreparation:
    """Replace inline source and prompt content with deterministic blob ids.

    ``creating_arguments_hash`` is intentionally excluded from the UUID5
    identity.  The safe arguments must contain the UUID before their hash can
    be computed, so the request is first used only to derive that UUID and is
    then rebound to the hash of the final, custody-safe arguments.

    ``max_storage_per_session`` is stamped onto the preparation as the
    ceiling this plan authorized; finalization refuses any other value.
    """
    try:
        session_uuid = UUID(session_id)
    except (TypeError, ValueError) as exc:
        raise AuditIntegrityError("Pipeline custody session_id must be a canonical UUID string") from exc
    if str(session_uuid) != session_id:
        raise AuditIntegrityError("Pipeline custody session_id must be a canonical UUID string")

    safe_arguments = _detached_json(dict(arguments))
    if prepared is None and not prompt_blobs:
        raise AuditIntegrityError("Pipeline custody requires at least one prepared file")

    def _request(item: _PreparedBlobCreate) -> InlineCustodyRequest:
        if type(item.description) not in {str, type(None)}:
            raise AuditIntegrityError("Prepared inline custody description must be str or None")
        return InlineCustodyRequest(
            session_id=session_uuid,
            filename=item.filename,
            content=item.content_bytes,
            mime_type=cast(AllowedMimeType, item.mime_type),
            source_description=item.description,
            creation_modality=item.creation_modality,
            created_from_message_id=item.created_from_message_id or "",
            creating_model_identifier=item.creating_model_identifier,
            creating_model_version=item.creating_model_version,
            creating_provider=item.creating_provider,
            creating_composer_skill_hash=item.creating_composer_skill_hash,
            creating_arguments_hash=item.creating_arguments_hash,
        )

    requests: list[InlineCustodyRequest] = []
    prompt_markers: list[dict[str, str]] = []
    if prepared is not None:
        source = safe_arguments["source"] if "source" in safe_arguments else None
        if type(source) is not dict:
            raise AuditIntegrityError("Pipeline custody requires canonical set_pipeline source arguments")
        inline_blob = source["inline_blob"] if "inline_blob" in source else None
        if type(inline_blob) is not dict:
            raise AuditIntegrityError("Pipeline custody requires canonical source.inline_blob arguments")
        _validate_prepared_inline_blob(inline_blob, prepared)
        if "blob_id" in source:
            raise AuditIntegrityError("Pipeline custody refuses source arguments containing both blob_id and inline_blob")
        options = source["options"] if "options" in source else None
        if options is not None and type(options) is not dict:
            raise AuditIntegrityError("Pipeline custody requires canonical source.options arguments")
        if type(options) is dict and "blob_ref" in options:
            raise AuditIntegrityError("Pipeline custody refuses caller-authored source.options.blob_ref")
        source_request = _request(prepared)
        requests.append(source_request)
        del source["inline_blob"]
        source["blob_id"] = str(inline_custody_blob_id(source_request))

    for path, item in prompt_blobs:
        prefix, separator, option_path = path.partition(".options.")
        if separator == "" or not prefix.startswith("node:"):
            raise AuditIntegrityError("Pipeline custody prompt path is invalid")
        node_id = prefix.removeprefix("node:")
        nodes = safe_arguments.get("nodes")
        if type(nodes) is not list:
            raise AuditIntegrityError("Pipeline custody requires node arguments")
        matches = [node for node in nodes if type(node) is dict and node.get("id") == node_id]
        if len(matches) != 1:
            raise AuditIntegrityError("Pipeline custody prompt node is ambiguous")
        options = matches[0].get("options")
        if type(options) is not dict:
            raise AuditIntegrityError("Pipeline custody prompt options are invalid")
        keys = option_path.split(".")
        container = options
        if len(keys) == 3 and keys[0] == "queries" and keys[2] == "template":
            queries = options.get("queries")
            if type(queries) is list:
                matches_query = [q for index, q in enumerate(queries) if type(q) is dict and (q.get("name") or str(index)) == keys[1]]
                if len(matches_query) != 1:
                    raise AuditIntegrityError("Pipeline custody prompt query is ambiguous")
                container = matches_query[0]
            elif type(queries) is dict and type(queries.get(keys[1])) is dict:
                container = queries[keys[1]]
            else:
                raise AuditIntegrityError("Pipeline custody prompt query is missing")
        elif len(keys) != 1 or keys[0] not in {"prompt_template", "system_prompt"}:
            raise AuditIntegrityError("Pipeline custody prompt field is invalid")
        member = container.get(keys[-1])
        if type(member) is not dict or type(member.get("inline_blob")) is not dict:
            raise AuditIntegrityError("Pipeline custody prompt inline_blob is missing")
        _validate_prepared_inline_blob(member["inline_blob"], item)
        prompt_request = _request(item)
        requests.append(prompt_request)
        marker = {
            "blob_ref": str(inline_custody_blob_id(prompt_request)),
            "mode": "inline_content",
            "sha256": item.content_hash,
        }
        container[keys[-1]] = marker
        prompt_markers.append(marker)
    if _contains_inline_blob(safe_arguments):
        raise AuditIntegrityError("Pipeline custody rewrite left an inline_blob member in proposal arguments")
    identity_hash = prompt_arguments_hash or composer_authority_hash(safe_arguments)
    source_count = 1 if prepared is not None else 0
    for index, marker in enumerate(prompt_markers):
        request = replace(requests[source_count + index], identity_arguments_hash=identity_hash)
        requests[source_count + index] = request
        marker["blob_ref"] = str(inline_custody_blob_id(request))
    safe_arguments_hash = composer_authority_hash(safe_arguments)
    rebound: list[InlineCustodyRequest] = []
    for request in requests:
        blob_id = inline_custody_blob_id(request)
        final_arguments_hash = (
            (prompt_arguments_hash or safe_arguments_hash) if request.creation_modality is not CreationModality.VERBATIM else None
        )
        final_request = replace(request, creating_arguments_hash=final_arguments_hash)
        if inline_custody_blob_id(final_request) != blob_id:
            raise AuditIntegrityError("Pipeline custody identity changed after safe argument hashing")
        rebound.append(final_request)
    return PipelineCustodyPreparation(
        arguments=safe_arguments,
        request=rebound[0],
        blob_id=inline_custody_blob_id(rebound[0]),
        max_storage_per_session=max_storage_per_session,
        additional_requests=tuple(rebound[1:]),
    )


async def finalize_pipeline_custody(
    preparation: PipelineCustodyPreparation,
    *,
    engine: Engine,
    data_dir: str | Path,
    max_storage_per_session: int,
    session_operation_context: SessionOperationContext | None = None,
    session_operation_authority: SessionOperationAuthority | None = None,
) -> BlobRecord:
    """Materialize a prepared inline source through the shared blob service."""
    if type(session_operation_context) is not SessionOperationContext:
        raise TypeError("pipeline custody requires an exact SessionOperationContext")
    if session_operation_context.fence.session_id != str(preparation.request.session_id):
        raise AuditIntegrityError("Pipeline custody operation targets a different session")
    if session_operation_authority is None:
        raise AuditIntegrityError("Pipeline custody requires the session operation authority")
    if max_storage_per_session != preparation.max_storage_per_session:
        raise AuditIntegrityError("Pipeline custody enforcement ceiling diverges from the plan-time ceiling")
    service = BlobServiceImpl(
        engine,
        Path(data_dir),
        max_storage_per_session=max_storage_per_session,
        session_operation_authority=session_operation_authority,
    )
    records: list[BlobRecord] = []
    for request in (preparation.request, *preparation.additional_requests):
        record = await service.reserve_inline_custody(request, session_operation_context=session_operation_context)
        blob_id = inline_custody_blob_id(request)
        if record.id != blob_id:
            raise AuditIntegrityError("Inline custody returned a blob id different from the prepared proposal")
        await run_sync_in_worker(
            verify_finalized_pipeline_custody,
            replace(preparation, request=request, blob_id=blob_id, additional_requests=()),
            record,
            data_dir=data_dir,
        )
        records.append(record)
    return records[0]


def finalize_pipeline_custody_sync(
    preparation: PipelineCustodyPreparation,
    *,
    engine: Engine,
    data_dir: str | Path,
    max_storage_per_session: int,
    session_operation_context: SessionOperationContext | None,
    session_operation_authority: SessionOperationAuthority | None,
) -> BlobRecord:
    """Settle deterministic custody from the synchronous direct tool handler."""
    if type(session_operation_context) is not SessionOperationContext or session_operation_authority is None:
        raise AuditIntegrityError("Pipeline custody requires the actual session operation context and authority")
    if session_operation_context.fence.session_id != str(preparation.request.session_id):
        raise AuditIntegrityError("Pipeline custody operation targets a different session")
    if max_storage_per_session != preparation.max_storage_per_session:
        raise AuditIntegrityError("Pipeline custody enforcement ceiling diverges from the plan-time ceiling")
    records: list[BlobRecord] = []
    for request in (preparation.request, *preparation.additional_requests):
        fields = _normalized_inline_custody_fields(request)
        blob_id = inline_custody_blob_id(request)
        record = _persist_blob_content(
            engine=engine,
            data_dir=Path(data_dir),
            max_storage_per_session=max_storage_per_session,
            blob_id=blob_id,
            session_id=request.session_id,
            filename=fields["filename"],
            content=request.content,
            mime_type=fields["mime_type"],
            created_by="assistant",
            source_description=fields["source_description"],
            creation_modality=request.creation_modality,
            created_from_message_id=fields["created_from_message_id"],
            creating_model_identifier=fields["creating_model_identifier"],
            creating_model_version=fields["creating_model_version"],
            creating_provider=fields["creating_provider"],
            creating_composer_skill_hash=fields["creating_composer_skill_hash"],
            creating_arguments_hash=fields["creating_arguments_hash"],
            idempotent=True,
            session_operation_context=session_operation_context,
            session_operation_authority=session_operation_authority,
        )
        if type(record) is not BlobRecord:
            raise AuditIntegrityError("Fenced pipeline custody persistence returned a raw row")
        verify_finalized_pipeline_custody(
            replace(preparation, request=request, blob_id=blob_id, additional_requests=()), record, data_dir=data_dir
        )
        records.append(record)
    return records[0]


def verify_finalized_pipeline_custody(
    preparation: PipelineCustodyPreparation,
    record: BlobRecord,
    *,
    data_dir: str | Path,
) -> None:
    """Verify publication returned the exact row the preparation promised.

    Agreeing on the blob id shows only that the write was addressed correctly;
    it cannot show that the persisted row and the file on disk carry the bytes
    and provenance this plan authorized. Every field is re-derived through the
    SAME normalization and path formula the settlement insert uses, then
    compared on exact runtime type as well as value — a ``size_bytes`` of
    ``8.0`` equals ``8`` and must still not settle.

    Two deliberate narrowings:

    ``created_at`` is checked for shape only. The column is
    ``DateTime(timezone=True)`` and the write stamps an aware value, but the
    SQLite round-trip returns it naive, so awareness is not a property this
    storage layer can promise on every backend.

    The symbolic-link guard tests the settled file itself, not its ancestors:
    ``inline_custody_storage_path`` already resolves ``data_dir``, so a link
    planted at the blob path is the reachable case, while walking the operator's
    configured parents would reject legitimate deployments.
    """
    if type(preparation) is not PipelineCustodyPreparation:
        raise TypeError("preparation must be an exact PipelineCustodyPreparation")
    if type(record) is not BlobRecord:
        raise TypeError("record must be an exact BlobRecord")
    request = preparation.request
    fields = _normalized_inline_custody_fields(request)
    storage_path = inline_custody_storage_path(
        Path(data_dir),
        session_id=fields["session_id"],
        blob_id=str(preparation.blob_id),
        filename=fields["filename"],
    )
    settled: tuple[tuple[str, object, object], ...] = (
        ("id", record.id, preparation.blob_id),
        ("session_id", record.session_id, request.session_id),
        ("filename", record.filename, fields["filename"]),
        ("mime_type", record.mime_type, fields["mime_type"]),
        ("size_bytes", record.size_bytes, fields["size_bytes"]),
        ("content_hash", record.content_hash, fields["content_hash"]),
        ("storage_path", record.storage_path, str(storage_path)),
        ("created_by", record.created_by, "assistant"),
        ("source_description", record.source_description, fields["source_description"]),
        ("status", record.status, "ready"),
        ("creation_modality", record.creation_modality, fields["creation_modality"]),
        ("created_from_message_id", record.created_from_message_id, fields["created_from_message_id"]),
        ("creating_model_identifier", record.creating_model_identifier, fields["creating_model_identifier"]),
        ("creating_model_version", record.creating_model_version, fields["creating_model_version"]),
        ("creating_provider", record.creating_provider, fields["creating_provider"]),
        ("creating_composer_skill_hash", record.creating_composer_skill_hash, fields["creating_composer_skill_hash"]),
        ("creating_arguments_hash", record.creating_arguments_hash, fields["creating_arguments_hash"]),
    )
    for field_name, actual, expected in settled:
        if type(actual) is not type(expected) or actual != expected:
            raise AuditIntegrityError(f"Inline custody finalization returned mismatched {field_name}")
    if type(record.created_at) is not datetime:
        raise AuditIntegrityError("Inline custody finalization returned mismatched created_at")
    if storage_path.is_symlink():
        raise AuditIntegrityError("Inline custody finalization returned a symbolic-link storage path")
    if not storage_path.is_file():
        raise AuditIntegrityError("Inline custody finalization returned a missing storage file")
    stored_bytes = storage_path.read_bytes()
    if not hmac.compare_digest(stored_bytes, request.content):
        raise AuditIntegrityError("Inline custody finalization returned mismatched storage bytes")
