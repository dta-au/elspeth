"""Composer blob-storage plane — session-scoped binary blob handlers.

Hosts:

- Tool handlers for blob CRUD: ``_execute_create_blob`` / ``_execute_update_blob``
  / ``_execute_delete_blob`` / ``_execute_get_blob_content`` /
  ``_handle_list_blobs`` / ``_handle_get_blob_metadata``.
- Quota state (``_BLOB_QUOTA_BYTES``).
- Storage primitives (``_prepare_blob_create`` / ``_persist_prepared_blob_create`` /
  ``_sync_get_blob`` / ``_sync_list_blobs`` / ``_check_blob_quota``).
- Blob DTOs (``BlobToolRecord`` / ``BlobCreatePayload`` / ``_PreparedBlobCreate``)
  and invariant-specific typed mutation routing.
- Tool-classification name sets and predicates live in
  ``elspeth.web.composer.tools.discovery``; the trailing comment in this file
  points to that module.

Patch-target stability: tests that bind ``_BLOB_QUOTA_BYTES`` /
``_check_blob_quota`` / ``_sync_get_blob`` by full dotted path must target this
module (``elspeth.web.composer.tools.blobs.<name>``), not the package facade —
helpers here resolve those names via their local module namespace.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Engine, func, select

from elspeth.contracts.blobs import (
    ALLOWED_MIME_TYPES,
    BlobActiveRunError,
    BlobInProgressForkError,
    BlobPendingProposalError,
)
from elspeth.contracts.blobs_inline import (
    ALLOWED_CONTENT_ENCODINGS,
    BlobInlineRef,
    ContentEncoding,
)
from elspeth.contracts.enums import CreationModality, is_llm_authored_creation_modality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.web.blobs.protocol import AllowedMimeType, BlobIntegrityError, BlobQuotaExceededError
from elspeth.web.blobs.service import (
    _BlobDeletionCoordinator,
    _BlobReplacementCoordinator,
    _guard_blob_row_literals,
    _lock_session_for_blob_quota,
    _persist_blob_content,
    content_hash,
    sanitize_filename,
)
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import (
    CreateBlobArgumentsModel,
    UpdateBlobArgumentsModel,
)
from elspeth.web.composer.state import (
    CompositionState,
)
from elspeth.web.composer.tools._common import (
    ToolContext,
    ToolResult,
    _composition_canonical_interpretation_requirement_error,
    _discovery_result,
    _failure_result,
    _mutation_result,
    _runtime_owned_llm_option_error,
)
from elspeth.web.composer.tools.declarations import (
    ToolDeclaration,
    ToolKind,
)
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.repository import SessionDerivedCustodyError
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.provider_config_policy import web_aws_s3_endpoint_url_policy_error
from elspeth.web.sessions.locking import filesystem_session_lock
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.protocol import SessionOperationAuthority

_BLOB_APPROVAL_MUTATION_OPERATION_KINDS = frozenset(
    {
        SessionOperationKind.COMPOSE,
        SessionOperationKind.PROPOSAL,
    }
)

_BLOB_DIRECT_CREATE_OPERATION_KINDS = frozenset({SessionOperationKind.COMPOSE})


class BlobToolRecord(TypedDict):
    """Closed dict shape returned by composer blob discovery helpers.

    Inline-blob provenance fields mirror the columns introduced on
    ``blobs_table``: ``creation_modality`` carries the
    closed-enum string (wire form), ``created_from_message_id`` binds to
    the originating chat message, and the five ``creating_*`` fields
    carry LLM-provenance for the three LLM-authored modalities.
    """

    id: str
    session_id: str
    filename: str
    mime_type: str
    size_bytes: int
    content_hash: str | None
    storage_path: str
    created_by: str
    source_description: str | None
    status: str
    creation_modality: str
    created_from_message_id: str | None
    creating_model_identifier: str | None
    creating_model_version: str | None
    creating_provider: str | None
    creating_composer_skill_hash: str | None
    creating_arguments_hash: str | None


class BlobCreatePayload(TypedDict):
    """Closed dict shape for the create_blob tool's success result data."""

    blob_id: str
    filename: str
    mime_type: str
    size_bytes: int
    content_hash: str


def _blob_row_to_tool_dict(row: Any) -> BlobToolRecord:
    """Serialize a validated blobs row to the tool-layer dict shape."""
    _guard_blob_row_literals(row)
    return {
        "id": row.id,
        "session_id": row.session_id,
        "filename": row.filename,
        "mime_type": row.mime_type,
        "size_bytes": row.size_bytes,
        "content_hash": row.content_hash,
        "storage_path": row.storage_path,
        "created_by": row.created_by,
        "source_description": row.source_description,
        "status": row.status,
        # Inline-blob provenance. The Tier 1 guard in
        # ``_guard_blob_row_literals`` already validated
        # ``creation_modality`` against the closed CreationModality enum.
        "creation_modality": row.creation_modality,
        "created_from_message_id": row.created_from_message_id,
        "creating_model_identifier": row.creating_model_identifier,
        "creating_model_version": row.creating_model_version,
        "creating_provider": row.creating_provider,
        "creating_composer_skill_hash": row.creating_composer_skill_hash,
        "creating_arguments_hash": row.creating_arguments_hash,
    }


def _sync_get_blob(engine: Engine, blob_id: str, session_id: str | None = None) -> BlobToolRecord | None:
    """Synchronous blob lookup for use in the tool executor thread."""
    with engine.connect() as conn:
        query = select(blobs_table).where(blobs_table.c.id == blob_id)
        if session_id is not None:
            query = query.where(blobs_table.c.session_id == session_id)
        row = conn.execute(query).first()
        if row is None:
            return None
        return _blob_row_to_tool_dict(row)


@trust_boundary(
    tier=3,
    source="LLM composer tool-call blob_id argument",
    source_param="blob_id",
    suppresses=("R5",),
    invariant="returns a repairable error message for non-string or non-UUID blob_id and None for canonical input; never raises on blob_id",
    non_raising=True,
)
def _blob_id_uuid_validation_error(blob_id: Any) -> str | None:
    """Return a repairable boundary error when ``blob_id`` is not canonical."""
    if not isinstance(blob_id, str):
        return f"blob_id must be a UUID string, got {type(blob_id).__name__}."
    try:
        UUID(blob_id)
    except ValueError:
        return (
            "blob_id is not a valid UUID. Use list_blobs or "
            "list_composer_blobs to select an uploaded blob, ask the user to "
            "upload the source file, or use create_blob for inline content "
            "before calling this tool."
        )
    return None


def _sync_get_blob_by_storage_path(
    engine: Engine,
    storage_path: str,
    session_id: str,
) -> BlobToolRecord | None:
    """Look up a blob by its canonical storage_path within a session.

    Used by guided proposal preparation to detect whether a reviewed path
    resolves to an already-uploaded blob.
    When it does, the blob_id (= blob["id"]) can be injected as ``blob_ref``
    into the reviewed source facts used by proposal custody.

    Returns None if no blob row matches the path, which is the correct
    representation for path-based sources that are not blob-backed.
    """
    with engine.connect() as conn:
        query = select(blobs_table).where(blobs_table.c.session_id == session_id).where(blobs_table.c.storage_path == storage_path)
        row = conn.execute(query).first()
        if row is None:
            return None
        return _blob_row_to_tool_dict(row)


def _sync_get_blob_by_id(
    engine: Engine,
    blob_id: str,
    session_id: str,
) -> BlobToolRecord | None:
    """Look up a blob by its UUID within a session (authoritative DB query).

    The inverse of :func:`_sync_get_blob_by_storage_path`: used to resolve a
    ``blob:<ref>`` path sentinel
    — emitted by ``build_step_1_schema_form_turn_from_resolved`` to keep the
    absolute storage_path off the wire — back to the blob's real ``storage_path``
    before the source is committed. Session-scoped so a blob ref cannot resolve
    across sessions (project/tenant isolation). Returns None if no row matches.
    """
    with engine.connect() as conn:
        query = select(blobs_table).where(blobs_table.c.session_id == session_id).where(blobs_table.c.id == blob_id)
        row = conn.execute(query).first()
        if row is None:
            return None
        return _blob_row_to_tool_dict(row)


def _sync_list_blobs(engine: Engine, session_id: str) -> list[dict[str, Any]]:
    """Synchronous blob listing for use in the tool executor thread."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(blobs_table).where(blobs_table.c.session_id == session_id).order_by(blobs_table.c.created_at.desc()).limit(50)
        ).fetchall()
        return [
            {
                "id": blob["id"],
                "filename": blob["filename"],
                "mime_type": blob["mime_type"],
                "size_bytes": blob["size_bytes"],
                "created_by": blob["created_by"],
                "status": blob["status"],
            }
            for blob in (_blob_row_to_tool_dict(row) for row in rows)
        ]


def _sync_list_ready_blob_inline_descriptors(engine: Engine, session_id: str) -> list[dict[str, Any]]:
    """Return H4 visibility descriptors for ready session blobs."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(blobs_table)
            .where(blobs_table.c.session_id == session_id)
            .where(blobs_table.c.status == "ready")
            .order_by(blobs_table.c.created_at.desc())
            .limit(50)
        ).fetchall()

    descriptors: list[dict[str, Any]] = []
    for row in rows:
        blob = _blob_row_to_tool_dict(row)
        if blob["content_hash"] is None:
            raise AuditIntegrityError(f"Ready blob '{blob['id']}' has null content_hash; cannot list for inline_content authoring")
        descriptors.append(
            {
                "blob_id": blob["id"],
                "mime_type": blob["mime_type"],
                "size_bytes": blob["size_bytes"],
                "content_hash": blob["content_hash"],
                "filename": blob["filename"],
            }
        )
    return descriptors


def _handle_list_blobs(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    session_engine = context.session_engine
    session_id = context.session_id
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    blobs = _sync_list_blobs(session_engine, session_id)
    return _discovery_result(state, blobs)


_LIST_BLOBS_DECLARATION = ToolDeclaration(
    name="list_blobs",
    handler=_handle_list_blobs,
    kind=ToolKind.BLOB_DISCOVERY,
    description="List uploaded/created files (blobs) in this session with metadata.",
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
)


def _handle_list_composer_blobs(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """List blobs using the ADR-025 composer-LLM visibility shape.

    The LLM sees only metadata needed to author a pinned inline-content
    marker. Bytes, previews, storage paths, and free-text descriptions stay
    out of the response surface.
    """
    del arguments
    session_engine = context.session_engine
    session_id = context.session_id
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    return _discovery_result(state, {"blobs": _sync_list_ready_blob_inline_descriptors(session_engine, session_id)})


_LIST_COMPOSER_BLOBS_DECLARATION = ToolDeclaration(
    name="list_composer_blobs",
    handler=_handle_list_composer_blobs,
    kind=ToolKind.BLOB_DISCOVERY,
    description=(
        "List ready blobs available for audited inline-content authoring. "
        "Returns only blob_id, mime_type, size_bytes, content_hash, and filename; never content bytes."
    ),
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
)


def _handle_get_blob_metadata(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    session_engine = context.session_engine
    session_id = context.session_id
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    blob_id_error = _blob_id_uuid_validation_error(arguments["blob_id"])
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    blob = _sync_get_blob(session_engine, arguments["blob_id"], session_id)
    if blob is None:
        return _failure_result(state, "Blob not found for this session.")
    safe_blob = {
        "id": blob["id"],
        "filename": blob["filename"],
        "mime_type": blob["mime_type"],
        "size_bytes": blob["size_bytes"],
        "content_hash": blob["content_hash"],
        "status": blob["status"],
    }
    return _discovery_result(state, safe_blob)


_GET_BLOB_METADATA_DECLARATION = ToolDeclaration(
    name="get_blob_metadata",
    handler=_handle_get_blob_metadata,
    kind=ToolKind.BLOB_DISCOVERY,
    description="Get metadata for a specific blob (file) by ID.",
    json_schema={
        "type": "object",
        "properties": {
            "blob_id": {"type": "string", "description": "Blob ID."},
        },
        "required": ["blob_id"],
        "additionalProperties": False,
    },
)


def _set_nested_option(container: dict[str, Any], keys: list[str], value: Any) -> dict[str, Any]:
    if not keys:
        raise ValueError("field_path must include at least one .options.<field> segment")
    if len(keys) == 1:
        container[keys[0]] = value
        return container
    head = keys[0]
    if head in container:
        child = container[head]
        if not isinstance(child, Mapping):
            raise ValueError(f"field_path segment {head!r} already exists and is not an object")
        nested = dict(deep_thaw(child))
    else:
        nested = {}
    container[head] = _set_nested_option(nested, keys[1:], value)
    return container


def _apply_inline_blob_marker(state: CompositionState, field_path: str, marker: dict[str, Any]) -> CompositionState:
    prefix, separator, rest = field_path.partition(".options.")
    if separator == "":
        raise ValueError("field_path must include '.options.'")
    keys = rest.split(".")

    if prefix == "source":
        source_name = "source"
    elif prefix.startswith("source:"):
        source_name = prefix.removeprefix("source:")
        if not source_name:
            raise ValueError("source:<name> field_path must include a source name")
    else:
        source_name = None

    if source_name is not None:
        source = state.sources[source_name] if source_name in state.sources else None
        if source is None:
            if source_name == "source":
                raise ValueError("Cannot wire source ref: no source has been set")
            raise ValueError(f"Source {source_name!r} not found in composition state")
        # Symmetric with the node arm below: never let a wire write land inside a
        # source's interpretation_requirements. Source review metadata
        # (INVENTED_SOURCE) may only be staged as a pending composer requirement
        # and resolved by resolve_interpretation_event — a wired ref here would
        # corrupt that structure outside the review boundary.
        if keys[0] == INTERPRETATION_REQUIREMENTS_KEY:
            raise ValueError(
                "wire_blob_inline_ref cannot write source interpretation_requirements; "
                "review metadata may only be staged as pending composer input and "
                "resolved by resolve_interpretation_event."
            )
        patched_options = _set_nested_option(dict(deep_thaw(source.options)), keys, marker)
        return state.with_named_source(source_name, replace(source, options=patched_options))

    if prefix.startswith("node:"):
        node_id = prefix.removeprefix("node:")
        new_nodes = []
        found = False
        for node in state.nodes:
            if node.id == node_id:
                if node.plugin == "llm" and keys[0] == INTERPRETATION_REQUIREMENTS_KEY:
                    raise ValueError(
                        "wire_blob_inline_ref cannot write LLM interpretation_requirements; "
                        "review metadata may only be staged as pending composer input and "
                        "resolved by resolve_interpretation_event."
                    )
                runtime_owned_error = _runtime_owned_llm_option_error(
                    node.plugin,
                    {keys[0]: marker},
                    tool_name="wire_blob_inline_ref",
                    component_id=node_id,
                )
                if runtime_owned_error is not None:
                    raise ValueError(runtime_owned_error)
                patched_options = _set_nested_option(dict(deep_thaw(node.options)), keys, marker)
                new_nodes.append(replace(node, options=patched_options))
                found = True
            else:
                new_nodes.append(node)
        if not found:
            raise ValueError(f"Node {node_id!r} not found in composition state")
        return replace(state, nodes=tuple(new_nodes), version=state.version + 1)

    if prefix.startswith("output:"):
        output_name = prefix.removeprefix("output:")
        if keys[0] == INTERPRETATION_REQUIREMENTS_KEY:
            raise ValueError(
                "wire_blob_inline_ref cannot write output interpretation_requirements; "
                "review metadata may only be staged as pending composer input and "
                "resolved by resolve_interpretation_event."
            )
        new_outputs = []
        found = False
        for output in state.outputs:
            if output.name == output_name:
                patched_options = _set_nested_option(dict(deep_thaw(output.options)), keys, marker)
                new_outputs.append(replace(output, options=patched_options))
                found = True
            else:
                new_outputs.append(output)
        if not found:
            raise ValueError(f"Output {output_name!r} not found in composition state")
        return replace(state, outputs=tuple(new_outputs), version=state.version + 1)

    raise ValueError("field_path must start with source.options, source:<name>.options, node:<id>.options, or output:<name>.options")


def _affected_component_for_inline_field_path(field_path: str) -> tuple[str, ...]:
    prefix, _, _rest = field_path.partition(".options.")
    if prefix == "source":
        return ("source",)
    if prefix.startswith("source:"):
        return (prefix.removeprefix("source:"),)
    if prefix.startswith("node:"):
        return (prefix.removeprefix("node:"),)
    if prefix.startswith("output:"):
        return (prefix.removeprefix("output:"),)
    return ()


def _inline_blob_endpoint_policy_error(state: CompositionState, field_path: str) -> str | None:
    """Return the endpoint policy result for the component changed by a blob marker."""
    prefix, _, _rest = field_path.partition(".options.")
    if prefix == "source":
        source_name = "source"
    elif prefix.startswith("source:"):
        source_name = prefix.removeprefix("source:")
    else:
        source_name = None

    if source_name is not None:
        source = state.sources[source_name]
        return web_aws_s3_endpoint_url_policy_error(source.plugin, source.options)

    if prefix.startswith("output:"):
        output_name = prefix.removeprefix("output:")
        output = next(output for output in state.outputs if output.name == output_name)
        return web_aws_s3_endpoint_url_policy_error(output.plugin, output.options)

    return None


def _execute_wire_blob_inline_ref(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Author a widened blob_ref inline-content marker into composition state."""
    session_engine = context.session_engine
    session_id = context.session_id
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")

    field_path = arguments["field_path"]
    blob_id_error = _blob_id_uuid_validation_error(arguments["blob_id"])
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    blob_id = UUID(arguments["blob_id"])

    # Tier-3 LLM tool argument. Absent ``encoding`` means utf-8 by the
    # published tool-schema contract (json_schema declares default "utf-8").
    # The ``isinstance(..., str)`` guard is load-bearing and must precede the
    # membership test: the LLM may emit a JSON array/object (Python
    # list/dict), and ``unhashable_value not in ALLOWED_CONTENT_ENCODINGS``
    # would raise TypeError out of the dispatcher rather than returning the
    # explicit failure result. The str narrowing also satisfies the
    # ContentEncoding cast below.
    encoding_value = arguments["encoding"] if "encoding" in arguments else "utf-8"
    if type(encoding_value) is not str:
        return _failure_result(state, f"encoding must be a string, got {type(encoding_value).__name__}")
    if encoding_value not in ALLOWED_CONTENT_ENCODINGS:
        return _failure_result(state, f"encoding must be one of {sorted(ALLOWED_CONTENT_ENCODINGS)}, got {encoding_value!r}")
    encoding = cast(ContentEncoding, encoding_value)

    blob = _sync_get_blob(session_engine, str(blob_id), session_id)
    if blob is None:
        return _failure_result(state, f"Blob '{blob_id}' not found.")
    if blob["status"] != "ready":
        return _failure_result(state, f"Blob '{blob_id}' is not ready (status: {blob['status']}).")
    pinned_hash = blob["content_hash"]
    if pinned_hash is None:
        raise AuditIntegrityError(f"Ready blob '{blob_id}' has null content_hash; cannot author inline_content ref")

    # Optional Tier-3 LLM tool argument; its absence honestly means "no
    # override supplied" (None), so the missing key is recorded as None
    # rather than fabricated into a value.
    sha256_override = arguments["sha256_override"] if "sha256_override" in arguments else None
    if sha256_override is not None and sha256_override != pinned_hash:
        return _failure_result(state, "sha256 override disagrees with authoritative blob content_hash; composer pins from blob metadata")

    try:
        ref = BlobInlineRef(
            field_path=field_path,
            blob_id=blob_id,
            sha256=pinned_hash,
            encoding=encoding,
        )
    except ValueError as exc:
        return _failure_result(state, f"Invalid field_path for inline blob ref: {exc}")

    marker: dict[str, Any] = {
        "blob_ref": str(blob_id),
        "mode": "inline_content",
        "sha256": pinned_hash,
    }
    if encoding != "utf-8":
        marker["encoding"] = encoding

    try:
        new_state = _apply_inline_blob_marker(state, ref.field_path, marker)
    except ValueError as exc:
        return _failure_result(state, str(exc))
    canonical_error = _composition_canonical_interpretation_requirement_error(
        new_state,
        tool_name="wire_blob_inline_ref",
    )
    if canonical_error is not None:
        return _failure_result(
            state,
            canonical_error,
            error_code="interpretation_requirements_invalid",
        )
    endpoint_policy_error = _inline_blob_endpoint_policy_error(new_state, ref.field_path)
    if endpoint_policy_error is not None:
        return _failure_result(state, endpoint_policy_error)
    return _mutation_result(new_state, _affected_component_for_inline_field_path(ref.field_path), data={"field_path": ref.field_path})


_WIRE_BLOB_INLINE_REF_DECLARATION = ToolDeclaration(
    name="wire_blob_inline_ref",
    handler=_execute_wire_blob_inline_ref,
    kind=ToolKind.BLOB_MUTATION,
    description=(
        "Author a widened blob_ref inline_content marker at a canonical field_path. "
        "Composer pins sha256 from blob metadata; callers must not pass content bytes."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "field_path": {
                "type": "string",
                "description": (
                    "Canonical path: source.options.<field>, source:<name>.options.<field>, "
                    "node:<node_id>.options.<field>, or output:<name>.options.<field>."
                ),
            },
            "blob_id": {"type": "string", "format": "uuid", "description": "Ready blob ID to wire as inline content."},
            "encoding": {
                "type": "string",
                "enum": sorted(ALLOWED_CONTENT_ENCODINGS),
                "default": "utf-8",
                "description": "Text decoder used at runtime. Defaults to utf-8.",
            },
        },
        "required": ["field_path", "blob_id"],
        "additionalProperties": False,
    },
)


_ALLOWED_BLOB_MIME_TYPES = ALLOWED_MIME_TYPES

_BLOB_QUOTA_BYTES: int = 500 * 1024 * 1024


def _resolve_blob_quota_bytes(max_blob_storage_per_session_bytes: int | None) -> int:
    return _BLOB_QUOTA_BYTES if max_blob_storage_per_session_bytes is None else max_blob_storage_per_session_bytes


@dataclass(frozen=True, slots=True)
class _PreparedBlobCreate:
    """Validated blob-create payload ready for filesystem/DB persistence.

    Provenance fields
    -----------------
    ``creation_modality`` declares how the content was produced; mirror
    enum is :class:`elspeth.contracts.enums.CreationModality`.  The five
    ``creating_*`` fields carry LLM-provenance and are populated only for
    LLM-authored modalities — the all-or-nothing invariant is enforced at
    the DB layer by ``ck_blobs_creating_llm_provenance_nullability`` in
    ``web/sessions/models.py``.  ``created_from_message_id`` binds the
    blob to the user chat message that triggered its creation; the
    composite FK on ``(created_from_message_id, session_id)`` rejects
    cross-session lineage.
    """

    blob_id: str
    filename: str
    mime_type: str
    content_bytes: bytes = field(repr=False)
    content_hash: str
    storage_path: Path
    description: Any | None
    creation_modality: CreationModality
    created_from_message_id: str | None
    creating_model_identifier: str | None
    creating_model_version: str | None
    creating_provider: str | None
    creating_composer_skill_hash: str | None
    creating_arguments_hash: str | None


@dataclass(frozen=True, slots=True)
class _BlobCreationProvenance:
    creation_modality: CreationModality
    creating_model_identifier: str | None
    creating_model_version: str | None
    creating_provider: str | None
    creating_composer_skill_hash: str | None
    creating_arguments_hash: str | None


def _verbatim_blob_creation_provenance() -> _BlobCreationProvenance:
    return _BlobCreationProvenance(
        creation_modality=CreationModality.VERBATIM,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


def _blob_provenance_message_id(user_message_id: str | None) -> str | None:
    return _blob_provenance_string(user_message_id)


def _blob_provenance_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if normalized else None


def _blob_creation_provenance(content: str, context: ToolContext) -> _BlobCreationProvenance:
    """Classify composer-created blob content and return DB provenance fields."""
    user_message_id = _blob_provenance_message_id(context.user_message_id)
    if user_message_id is not None and context.user_message_content is not None and content and content in context.user_message_content:
        return _verbatim_blob_creation_provenance()

    required = {
        "user_message_id": user_message_id,
        "composer_model_identifier": _blob_provenance_string(context.composer_model_identifier),
        "composer_model_version": _blob_provenance_string(context.composer_model_version),
        "composer_provider": _blob_provenance_string(context.composer_provider),
        "composer_skill_hash": _blob_provenance_string(context.composer_skill_hash),
        "tool_arguments_hash": _blob_provenance_string(context.tool_arguments_hash),
    }
    missing = tuple(name for name, value in required.items() if value is None)
    if missing:
        raise AuditIntegrityError(f"LLM-authored blob provenance requires complete composer context; missing: {', '.join(missing)}")

    return _BlobCreationProvenance(
        creation_modality=CreationModality.LLM_GENERATED,
        creating_model_identifier=required["composer_model_identifier"],
        creating_model_version=required["composer_model_version"],
        creating_provider=required["composer_provider"],
        creating_composer_skill_hash=required["composer_skill_hash"],
        creating_arguments_hash=required["tool_arguments_hash"],
    )


def _state_source_blob_refs(state: CompositionState) -> frozenset[str]:
    """Blob refs bound to any pipeline source root."""
    refs: set[str] = set()
    for source_name, source in state.sources.items():
        if "blob_ref" not in source.options:
            continue
        blob_ref = source.options["blob_ref"]
        if not isinstance(blob_ref, str):
            # The canonical writer sets blob_ref exclusively from
            # authoritative blob metadata as blob["id"] (a str) in
            # sources.py::_resolve_source_blob, and every caller-injection
            # path is rejected (_reject_manual_source_blob_ref, the
            # patch_source_options blob_ref guard). A present-but-non-str
            # blob_ref therefore cannot arise from any valid authoring path:
            # it is a corruption of the audited CompositionState. Silently
            # treating it as "not bound" would let _execute_update_blob mutate
            # a blob that is in fact bound to a pipeline source, defeating the
            # binding guard — so escalate rather than suppress.
            raise AuditIntegrityError(
                f"Source '{source_name}' has a non-str blob_ref ({type(blob_ref).__name__}); CompositionState integrity anomaly"
            )
        refs.add(blob_ref)
    return frozenset(refs)


def _blob_storage_path(data_dir: str, session_id: str, blob_id: str, filename: str) -> Path:
    """Compute blob storage path matching BlobServiceImpl layout.

    Pattern: {data_dir}/blobs/{session_id}/{blob_id}_{filename}
    """
    return Path(data_dir).resolve() / "blobs" / session_id / f"{blob_id}_{filename}"


def _check_blob_quota(
    conn: Any,
    session_id: str,
    additional_bytes: int,
    *,
    quota_bytes: int | None = None,
    session_locked: bool = False,
) -> str | None:
    """Check if adding bytes would exceed the session blob quota.

    Returns an error message if quota exceeded, None if OK.
    Runs inside an existing transaction for TOCTOU safety.
    """
    if not session_locked:
        _lock_session_for_blob_quota(conn, session_id)
    current_total = conn.execute(
        select(func.coalesce(func.sum(blobs_table.c.size_bytes), 0)).where(blobs_table.c.session_id == session_id)
    ).scalar()
    current_total = int(current_total)
    resolved_quota = _resolve_blob_quota_bytes(quota_bytes)
    if current_total + additional_bytes > resolved_quota:
        return f"Session blob quota exceeded: {current_total + additional_bytes} bytes would exceed {resolved_quota} byte limit."
    return None


@trust_boundary(
    tier=3,
    source="LLM-supplied create_blob-style tool arguments (filename / mime_type / content / optional description)",
    source_param="arguments",
    suppresses=("R1",),
    invariant="raises ToolArgumentError on a disallowed MIME type, unsanitizable filename, or non-UTF-8-encodable content; never coerces malformed arguments",
    test_ref="tests/integration/web/composer/test_inline_source_provenance.py::test_non_utf8_content_raises_tool_argument_error",
    test_fingerprint="0ba34e12e1e4291965b7a438789c3b877f8a9f1a2add72e9c8d1fe51628f3ab3",
)
def _prepare_blob_create(
    arguments: Mapping[str, Any],
    *,
    data_dir: str,
    session_id: str,
    creation_modality: CreationModality,
    created_from_message_id: str | None,
    creating_model_identifier: str | None = None,
    creating_model_version: str | None = None,
    creating_provider: str | None = None,
    creating_composer_skill_hash: str | None = None,
    creating_arguments_hash: str | None = None,
) -> _PreparedBlobCreate:
    """Validate a create_blob-style payload and allocate its storage path.

    Type guarantees on entry
    ------------------------
    Every reachable caller validates ``arguments`` via a Pydantic model
    BEFORE invoking this helper:

      * :func:`_execute_create_blob` — :class:`CreateBlobArgumentsModel`
        (``filename: str``, ``mime_type: str``, ``content: str`` +
        ``extra="forbid"``).
      * :func:`_execute_set_pipeline` inline-blob path — passes
        ``validated.source.inline_blob.model_dump()`` (via
        :class:`_InlineBlobModel`; same string-typed required fields
        + ``extra="forbid"``).

    The three ``isinstance(..., str)`` guards that previously sat at the
    top of this function are therefore unreachable — Pydantic rejects any
    non-string value with a structured :class:`pydantic.ValidationError`
    re-raised by the caller as :class:`ToolArgumentError` before this
    helper is invoked.  They are removed in the same commit that promotes
    ``set_pipeline`` so the dead-code surface does not linger past the
    wave that makes it dead (CLAUDE.md "No Legacy Code Policy").

    Semantic checks below this point (MIME allowlist, filename
    sanitisation, UTF-8 encodability) ARE NOT type checks — they enforce
    content-validity rules Pydantic cannot express — and remain.

    Provenance kwargs
    -----------------
    All callers MUST supply ``creation_modality`` and
    ``created_from_message_id``.  The five ``creating_*`` kwargs default
    to ``None`` and MUST be left as ``None`` for ``CreationModality.VERBATIM``;
    the three LLM-authored modalities require all five.  The DB-side
    CHECK ``ck_blobs_creating_llm_provenance_nullability`` rejects any
    other combination.  We do not duplicate the biconditional in Python
    — the constraint IS the validation, per the offensive-programming
    discipline in CLAUDE.md ("The CHECK constraint is the validation").
    """
    filename = arguments["filename"]
    mime_type = arguments["mime_type"]
    content = arguments["content"]

    if is_llm_authored_creation_modality(creation_modality) and created_from_message_id is None:
        raise AuditIntegrityError(
            "LLM-authored blob creation_modality requires created_from_message_id so the audit trail can walk back to the triggering chat message"
        )

    if mime_type not in _ALLOWED_BLOB_MIME_TYPES:
        # Tier-3 boundary: the LLM-supplied mime_type is not in the
        # operator-controlled allowlist. ToolArgumentError keeps the
        # leak-prevention discipline (no value field) — only the
        # allowlist itself appears in the LLM echo, never the rejected
        # value. Composer exception-channel discipline (CEC1) requires
        # ToolArgumentError here, not bare ValueError.
        allowed = ", ".join(sorted(_ALLOWED_BLOB_MIME_TYPES))
        raise ToolArgumentError(
            argument="mime_type",
            expected=f"one of: {allowed}",
            actual_type="str",
        )

    try:
        safe_filename = sanitize_filename(filename)
    except ValueError as exc:
        # Tier-3 boundary: filename failed sanitization (path traversal,
        # empty after strip, etc.). The underlying ValueError message
        # may echo the offending filename, so we wrap with
        # ToolArgumentError (no value field) and preserve the original
        # cause on __cause__ for auditors. CEC1 channel discipline.
        raise ToolArgumentError(
            argument="filename",
            expected="a sanitizable filename (no path separators, non-empty after stripping)",
            actual_type="str",
        ) from exc

    # UTF-8 encode guard: a Python ``str`` that contains
    # an unpaired surrogate code point (e.g. ``"\udc80"``) is a valid
    # ``str`` but is NOT encodable to UTF-8 — the underlying file write
    # would raise UnicodeEncodeError downstream and leave the audit layer
    # holding a half-written blob row.  Wrap as ToolArgumentError here
    # so the compose loop's ARG_ERROR routing handles it the same way as
    # disallowed MIME types and unsanitizable filenames (CEC1 channel).
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolArgumentError(
            argument="content",
            expected="valid UTF-8 text",
            actual_type="str (contained non-encodable character, e.g. surrogate)",
        ) from exc
    file_hash = content_hash(content_bytes)
    blob_id = str(uuid4())
    return _PreparedBlobCreate(
        blob_id=blob_id,
        filename=safe_filename,
        mime_type=mime_type,
        content_bytes=content_bytes,
        content_hash=file_hash,
        storage_path=_blob_storage_path(data_dir, session_id, blob_id, safe_filename),
        description=arguments.get("description"),
        creation_modality=creation_modality,
        created_from_message_id=created_from_message_id,
        creating_model_identifier=creating_model_identifier,
        creating_model_version=creating_model_version,
        creating_provider=creating_provider,
        creating_composer_skill_hash=creating_composer_skill_hash,
        creating_arguments_hash=creating_arguments_hash,
    )


def _persist_prepared_blob_create(
    prepared: _PreparedBlobCreate,
    *,
    session_engine: Engine,
    session_id: str,
    session_operation_authority: SessionOperationAuthority,
    session_operation_context: SessionOperationContext,
    max_blob_storage_per_session_bytes: int | None = None,
) -> str | None:
    """Persist a prepared blob through the shared blob custody primitive."""
    resolved_storage = prepared.storage_path.expanduser().resolve()
    session_dir = resolved_storage.parent
    blobs_dir = session_dir.parent
    if session_dir.name != session_id or blobs_dir.name != "blobs":
        raise AuditIntegrityError("Prepared blob storage path does not match its session custody root")
    data_dir = blobs_dir.parent
    try:
        _persist_blob_content(
            engine=session_engine,
            data_dir=data_dir,
            max_storage_per_session=_resolve_blob_quota_bytes(max_blob_storage_per_session_bytes),
            blob_id=UUID(prepared.blob_id),
            session_id=session_id,
            filename=prepared.filename,
            content=prepared.content_bytes,
            mime_type=cast(AllowedMimeType, prepared.mime_type),
            created_by="assistant",
            source_description=prepared.description,
            creation_modality=prepared.creation_modality,
            created_from_message_id=prepared.created_from_message_id,
            creating_model_identifier=prepared.creating_model_identifier,
            creating_model_version=prepared.creating_model_version,
            creating_provider=prepared.creating_provider,
            creating_composer_skill_hash=prepared.creating_composer_skill_hash,
            creating_arguments_hash=prepared.creating_arguments_hash,
            idempotent=False,
            session_operation_authority=session_operation_authority,
            session_operation_context=session_operation_context,
        )
    except BlobQuotaExceededError as exc:
        return (
            f"Session blob quota exceeded: {exc.current_bytes + len(prepared.content_bytes)} bytes "
            f"would exceed {exc.limit_bytes} byte limit."
        )
    return None


def _blob_create_payload(prepared: _PreparedBlobCreate) -> BlobCreatePayload:
    """Return the LLM/audit-safe create_blob result payload."""
    return {
        "blob_id": prepared.blob_id,
        "filename": prepared.filename,
        "mime_type": prepared.mime_type,
        "size_bytes": len(prepared.content_bytes),
        "content_hash": prepared.content_hash,
    }


def _execute_create_blob(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Create a new blob (file) in the session from inline content.

    Uses the same storage layout and safety functions as BlobServiceImpl:
    sanitize_filename() for path traversal defence, content_hash() for
    SHA-256, per-session subdirectory, and atomic quota enforcement.

    Tier-3 boundary: ``arguments`` is an LLM-supplied dict.  Validated
    via :class:`CreateBlobArgumentsModel` (the single source of truth for
    the argument schema — supersedes the deleted
    ``_TOOL_REQUIRED_PATHS["create_blob"]`` entry in ``service.py``,
    rev-3 N7 / rev-4 M1).  On :class:`pydantic.ValidationError` we
    re-raise as :class:`ToolArgumentError` so the compose loop's
    ARG_ERROR routing at ``service.py:2480`` receives the right
    exception class.

    The validated ``model_dump()`` is then fed to ``_prepare_blob_create``
    which still performs the MIME-type allowlist check and
    :func:`sanitize_filename` traversal-defence — those are semantic
    Tier-3 checks (value-based) that Pydantic's type validation cannot
    express.
    """
    session_engine = context.session_engine
    session_id = context.session_id
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    if context.data_dir is None:
        return _failure_result(state, "Blob tools require data_dir for storage.")

    try:
        validated = CreateBlobArgumentsModel.model_validate(arguments)
    except PydanticValidationError as exc:
        # The shared AllowedMimeType contract is intentionally expressed in
        # the redaction model as well as the wire schema. Preserve the
        # historical semantic-error channel for an unsupported string: callers
        # receive the safe field-specific allowlist diagnostic rather than a
        # generic model-shape failure. Non-string values remain structural
        # model errors.
        raw_mime_type = arguments.get("mime_type")
        if type(raw_mime_type) is str and any(
            tuple(error["loc"]) == ("mime_type",) and error["type"] == "literal_error" for error in exc.errors(include_input=False)
        ):
            allowed = ", ".join(sorted(_ALLOWED_BLOB_MIME_TYPES))
            raise ToolArgumentError(
                argument="mime_type",
                expected=f"one of: {allowed}",
                actual_type="str",
            ) from exc
        raise ToolArgumentError(
            argument="create_blob arguments",
            expected="object conforming to CreateBlobArgumentsModel",
            actual_type=type(exc).__name__,
        ) from exc

    # _prepare_blob_create still raises ToolArgumentError on semantic
    # Tier-3 violations (disallowed MIME type, un-sanitizable filename).
    # The Pydantic model catches type/shape violations; _prepare_blob_create
    # catches value-domain violations.  Both route via ToolArgumentError
    # to ARG_ERROR (CEC1 channel discipline).
    provenance = _blob_creation_provenance(validated.content, context)
    prepared = _prepare_blob_create(
        validated.model_dump(),
        data_dir=context.data_dir,
        session_id=session_id,
        creation_modality=provenance.creation_modality,
        created_from_message_id=context.user_message_id,
        creating_model_identifier=provenance.creating_model_identifier,
        creating_model_version=provenance.creating_model_version,
        creating_provider=provenance.creating_provider,
        creating_composer_skill_hash=provenance.creating_composer_skill_hash,
        creating_arguments_hash=provenance.creating_arguments_hash,
    )

    session_operation_authority = context.session_operation_authority
    session_operation_context = context.session_operation_context
    if session_operation_authority is None or session_operation_context is None:
        return _failure_result(state, "create_blob requires session operation authority context.")
    if (
        type(session_operation_context) is not SessionOperationContext
        or session_operation_context.operation_kind not in _BLOB_DIRECT_CREATE_OPERATION_KINDS
        or session_operation_context.fence.session_id != session_id
    ):
        return _failure_result(state, "create_blob requires exact COMPOSE authority for this session.")
    quota_error = _persist_prepared_blob_create(
        prepared,
        session_engine=session_engine,
        session_id=session_id,
        session_operation_authority=session_operation_authority,
        session_operation_context=session_operation_context,
        max_blob_storage_per_session_bytes=context.max_blob_storage_per_session_bytes,
    )
    if quota_error is not None:
        return _failure_result(state, quota_error)

    return _discovery_result(state, _blob_create_payload(prepared))


_CREATE_BLOB_DECLARATION = ToolDeclaration(
    name="create_blob",
    handler=_execute_create_blob,
    kind=ToolKind.BLOB_MUTATION,
    description=(
        "Create a new file (blob) from inline content. "
        "Use this to create seed input files (URLs, JSON, CSV snippets) "
        "mid-conversation without requiring manual upload."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Filename for the blob (e.g. 'urls.csv', 'seed.json').",
            },
            "mime_type": {
                "type": "string",
                "enum": sorted(ALLOWED_MIME_TYPES),
                "description": "MIME type of the content.",
            },
            "content": {
                "type": "string",
                "description": "The file content as a string.",
            },
            "description": {
                "type": "string",
                "description": "Optional description of the file's purpose.",
            },
        },
        "required": ["filename", "mime_type", "content"],
        "additionalProperties": False,
    },
    blob_store_only=True,
)


def _execute_update_blob(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Replace one exact ready blob under the caller's live COMPOSE fence."""
    session_engine = context.session_engine
    session_id = context.session_id
    data_dir = context.data_dir
    if session_engine is None or session_id is None or data_dir is None:
        return _failure_result(state, "Blob tools require session context and data_dir for storage.")

    try:
        validated = UpdateBlobArgumentsModel.model_validate(arguments)
    except PydanticValidationError as exc:
        raise ToolArgumentError(
            argument="update_blob arguments",
            expected="object conforming to UpdateBlobArgumentsModel",
            actual_type=type(exc).__name__,
        ) from exc

    blob_id = validated.blob_id
    blob_id_error = _blob_id_uuid_validation_error(blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    if blob_id in _state_source_blob_refs(state):
        return _failure_result(
            state,
            f"Blob '{blob_id}' is currently bound as a pipeline source; create a new blob and rebind the source instead.",
        )

    authority = context.session_operation_authority
    operation_context = context.session_operation_context
    if authority is None or operation_context is None:
        return _failure_result(state, "update_blob requires session operation authority context.")
    if (
        type(operation_context) is not SessionOperationContext
        or operation_context.operation_kind not in _BLOB_APPROVAL_MUTATION_OPERATION_KINDS
        or operation_context.fence.session_id != session_id
    ):
        return _failure_result(state, "update_blob requires exact COMPOSE or PROPOSAL authority for this session.")
    accepting_proposal_id = context.accepting_proposal_id
    if operation_context.operation_kind is SessionOperationKind.PROPOSAL:
        if type(accepting_proposal_id) is not UUID:
            return _failure_result(state, "update_blob proposal execution requires its exact accepting proposal identity.")
    elif accepting_proposal_id is not None:
        return _failure_result(state, "update_blob COMPOSE execution cannot exclude proposal retention.")

    content = validated.content
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolArgumentError(
            argument="update_blob content",
            expected="valid UTF-8 text",
            actual_type=type(exc).__name__,
        ) from exc
    file_hash = content_hash(content_bytes)
    provenance = _blob_creation_provenance(content, context)
    provenance_message_id = _blob_provenance_message_id(context.user_message_id)

    try:
        expected = authority.mutate(
            operation_context,
            lambda transaction: transaction.blobs.read_blob(blob_id=UUID(blob_id)),
        )
        if expected.status != "ready":
            return _failure_result(state, f"Blob '{blob_id}' is not ready and cannot be updated.")
        replacement = replace(
            expected,
            size_bytes=len(content_bytes),
            content_hash=file_hash,
            creation_modality=provenance.creation_modality,
            created_from_message_id=provenance_message_id,
            creating_model_identifier=provenance.creating_model_identifier,
            creating_model_version=provenance.creating_model_version,
            creating_provider=provenance.creating_provider,
            creating_composer_skill_hash=provenance.creating_composer_skill_hash,
            creating_arguments_hash=provenance.creating_arguments_hash,
        )
        committed = _BlobReplacementCoordinator(
            data_dir=Path(data_dir),
            session_operation_authority=authority,
        ).replace_blob(
            expected=expected,
            replacement=replacement,
            content=content_bytes,
            context=operation_context,
            max_storage_per_session=_resolve_blob_quota_bytes(context.max_blob_storage_per_session_bytes),
            accepting_proposal_id=accepting_proposal_id,
        )
    except BlobInProgressForkError as exc:
        return _failure_result(state, str(exc).replace("deleted", "updated"))
    except BlobPendingProposalError as exc:
        return _failure_result(state, str(exc).replace("deleted", "updated"))
    except BlobActiveRunError as exc:
        return _failure_result(state, str(exc).replace("deleted", "updated"))
    except BlobQuotaExceededError as exc:
        return _failure_result(
            state,
            f"Session blob quota exceeded: {exc.current_bytes - expected.size_bytes + len(content_bytes)} bytes "
            f"would exceed {exc.limit_bytes} byte limit.",
        )
    except SessionOperationFenceLost:
        return _failure_result(state, "update_blob lost its session operation authority before mutation.")
    except SessionDerivedCustodyError:
        return _failure_result(state, f"Blob '{blob_id}' not found.")

    return _discovery_result(
        state,
        {
            "blob_id": blob_id,
            "filename": committed.filename,
            "size_bytes": committed.size_bytes,
            "content_hash": committed.content_hash,
        },
    )


_UPDATE_BLOB_DECLARATION = ToolDeclaration(
    name="update_blob",
    handler=_execute_update_blob,
    kind=ToolKind.BLOB_MUTATION,
    description="Update the content of an existing blob (file). Overwrites the file content while preserving metadata.",
    json_schema={
        "type": "object",
        "properties": {
            "blob_id": {
                "type": "string",
                "description": "ID of the blob to update.",
            },
            "content": {
                "type": "string",
                "description": "New file content.",
            },
        },
        "required": ["blob_id", "content"],
        "additionalProperties": False,
    },
    blob_store_only=True,
)


def _execute_delete_blob(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Delete one blob through the shared durable deletion ledger."""
    session_id = context.session_id
    data_dir = context.data_dir
    if context.session_engine is None or session_id is None or data_dir is None:
        return _failure_result(state, "Blob tools require session context and data_dir for storage.")

    blob_id = arguments["blob_id"]
    blob_id_error = _blob_id_uuid_validation_error(blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)

    authority = context.session_operation_authority
    operation_context = context.session_operation_context
    if authority is None or operation_context is None:
        return _failure_result(state, "delete_blob requires session operation authority context.")
    if (
        type(operation_context) is not SessionOperationContext
        or operation_context.operation_kind not in _BLOB_APPROVAL_MUTATION_OPERATION_KINDS
        or operation_context.fence.session_id != session_id
    ):
        return _failure_result(state, "delete_blob requires exact COMPOSE or PROPOSAL authority for this session.")
    accepting_proposal_id = context.accepting_proposal_id
    if operation_context.operation_kind is SessionOperationKind.PROPOSAL:
        if type(accepting_proposal_id) is not UUID:
            return _failure_result(state, "delete_blob proposal execution requires its exact accepting proposal identity.")
    elif accepting_proposal_id is not None:
        return _failure_result(state, "delete_blob COMPOSE execution cannot exclude proposal retention.")

    blob_uuid = UUID(blob_id)
    try:
        with filesystem_session_lock(Path(data_dir), session_id):
            authority.mutate(
                operation_context,
                lambda transaction: transaction.blobs.read_blob(blob_id=blob_uuid),
            )
            _BlobDeletionCoordinator(
                data_dir=Path(data_dir),
                session_operation_authority=authority,
            ).delete_blob(
                blob_id=blob_uuid,
                context=operation_context,
                accepting_proposal_id=accepting_proposal_id,
            )
    except BlobInProgressForkError as exc:
        return _failure_result(state, str(exc))
    except BlobPendingProposalError as exc:
        return _failure_result(state, str(exc))
    except BlobActiveRunError as exc:
        return _failure_result(state, str(exc))
    except SessionOperationFenceLost:
        return _failure_result(state, "delete_blob lost its session operation authority before mutation.")
    except SessionDerivedCustodyError:
        return _failure_result(state, f"Blob '{blob_id}' not found.")

    return _discovery_result(state, {"blob_id": blob_id, "deleted": True})


_DELETE_BLOB_DECLARATION = ToolDeclaration(
    name="delete_blob",
    handler=_execute_delete_blob,
    kind=ToolKind.BLOB_MUTATION,
    description="Delete a blob (file) and its storage.",
    json_schema={
        "type": "object",
        "properties": {
            "blob_id": {
                "type": "string",
                "description": "ID of the blob to delete.",
            },
        },
        "required": ["blob_id"],
        "additionalProperties": False,
    },
    blob_store_only=True,
)


def _verify_blob_content_integrity(blob: BlobToolRecord, data: bytes) -> None:
    """Verify on-disk blob bytes match the stored content_hash.

    Tier-1 invariant: a ``ready`` blob's stored ``content_hash`` is
    enforced non-NULL by the ``ck_blobs_ready_hash`` CHECK constraint
    at write time. Reading NULL here is therefore a DB-integrity
    anomaly (someone bypassed the constraint, the row was tampered
    with, or the constraint is missing in this database). A SHA-256
    mismatch between recomputed bytes and stored hash is filesystem
    corruption, tampering, or a write-path bug.

    Both conditions ESCALATE via ``AuditIntegrityError`` /
    ``BlobIntegrityError`` rather than degrading to a soft result;
    silently passing through unverified bytes would let the audit
    trail confidently record decisions made on garbage.
    """
    _verify_blob_content_hash(blob, content_hash(data))


def _verify_blob_content_hash(blob: BlobToolRecord, actual_hash: str) -> None:
    """Verify a precomputed SHA-256 digest against a blob row."""
    blob_id = blob["id"]
    stored_hash = blob["content_hash"]
    if stored_hash is None:
        raise AuditIntegrityError(f"Tier 1: ready blob {blob_id} has NULL content_hash — DB integrity anomaly, cannot verify")
    if not hmac.compare_digest(actual_hash, stored_hash):
        raise BlobIntegrityError(blob_id, expected=stored_hash, actual=actual_hash)


def _execute_get_blob_content(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Retrieve the content of a blob for inspection.

    Mirrors the three Tier-1 guards enforced by
    ``BlobServiceImpl.read_blob_content`` so the composer read path and
    the HTTP read path apply the same invariants:

    1. **Lifecycle guard** — only ``ready`` blobs have finalised,
       trustworthy content.  ``pending`` blobs may be partial writes;
       ``error`` blobs belong to failed runs whose output is not
       authoritative.  Returned as a ``_failure_result`` so the
       compose loop can surface a helpful message to the LLM.
    2. **Integrity verification** — recompute SHA-256 of the on-disk
       bytes and compare (``hmac.compare_digest`` — constant-time) to
       the stored ``content_hash``.  A mismatch is a Tier-1 anomaly
       (our hash, our file) indicating filesystem corruption,
       tampering, or a write-path bug; it must ESCALATE via
       ``BlobIntegrityError``, not degrade to a tool-failure result.
       Implemented by ``_verify_blob_content_integrity`` (shared with
       ``_execute_inspect_source`` and ``compute_proof_diagnostics``).
    3. **Decode safety** — the MIME allowlist admits encodings other
       than UTF-8 (``text/csv`` is frequently latin-1 in the wild).
       ``UnicodeDecodeError`` is converted to a ``_failure_result``
       so the tool dispatcher is not crashed by admissible-but-
       undecodable content.

    The canonical path — ``BlobServiceImpl.read_blob_content`` — is
    async and engine-bound, so the guards are mirrored inline rather
    than shared via a common helper.  Any drift between this function
    and ``BlobServiceImpl.read_blob_content`` is caught by
    ``TestGetBlobContentGuards`` at CI time.
    """
    session_engine = context.session_engine
    session_id = context.session_id
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")

    blob_id = arguments["blob_id"]
    blob_id_error = _blob_id_uuid_validation_error(blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    blob = _sync_get_blob(session_engine, blob_id, session_id)
    if blob is None:
        return _failure_result(state, f"Blob '{blob_id}' not found.")

    # Guard 1 — lifecycle.  Pending/error blobs are not readable.
    blob_status = blob["status"]
    if blob_status != "ready":
        return _failure_result(
            state,
            f"Blob '{blob_id}' is not readable — status is '{blob_status}', expected 'ready'.",
        )

    storage_path = Path(blob["storage_path"])
    if not storage_path.exists():
        return _failure_result(state, f"Blob storage file missing for '{blob_id}'.")

    data = storage_path.read_bytes()

    # Guard 2 — integrity.  Shared helper: NULL stored_hash escalates
    # via AuditIntegrityError, mismatch via BlobIntegrityError.
    _verify_blob_content_integrity(blob, data)

    # Guard 3 — decode safety.  Non-UTF-8 bytes are a Tier-3 external
    # input condition (the operator supplied content in an encoding we
    # cannot losslessly round-trip to the LLM); surface as
    # tool-failure so the compose loop treats it as recoverable rather
    # than raising an unhandled exception out of the dispatcher.
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return _failure_result(
            state,
            f"Blob '{blob_id}' is not valid UTF-8 text ({exc.reason} at byte offset {exc.start}).",
        )

    # Truncate very large content to avoid overwhelming the LLM context
    max_chars = 50_000
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars]

    return _discovery_result(
        state,
        {
            "blob_id": blob_id,
            "filename": blob["filename"],
            "mime_type": blob["mime_type"],
            "content": content,
            "truncated": truncated,
            "size_bytes": blob["size_bytes"],
        },
    )


_GET_BLOB_CONTENT_DECLARATION = ToolDeclaration(
    name="get_blob_content",
    handler=_execute_get_blob_content,
    kind=ToolKind.BLOB_DISCOVERY,
    description="Retrieve the content of a blob (file) for inspection. Large files are truncated to 50,000 characters.",
    json_schema={
        "type": "object",
        "properties": {
            "blob_id": {
                "type": "string",
                "description": "ID of the blob to read.",
            },
        },
        "required": ["blob_id"],
        "additionalProperties": False,
    },
)


# ``_BLOB_STORE_ONLY_MUTATION_TOOL_NAMES`` and the matching predicate
# ``is_blob_store_only_mutation_tool`` are declared in
# ``elspeth.web.composer.tools.discovery``. The dispatcher carries the full
# ``ToolContext`` (including ``max_blob_storage_per_session_bytes`` and
# ``user_message_id``) to every handler, so there is no per-tool kwarg-shape
# gate to maintain at the declaration site.


TOOLS_IN_MODULE: tuple[ToolDeclaration, ...] = (
    _LIST_BLOBS_DECLARATION,
    _LIST_COMPOSER_BLOBS_DECLARATION,
    _GET_BLOB_METADATA_DECLARATION,
    _GET_BLOB_CONTENT_DECLARATION,
    _CREATE_BLOB_DECLARATION,
    _UPDATE_BLOB_DECLARATION,
    _DELETE_BLOB_DECLARATION,
    _WIRE_BLOB_INLINE_REF_DECLARATION,
)
"""Every tool declared in this module, in stable order.

``_dispatch.py`` aggregates this tuple from every plane to build the
registered-tool universe. Tests that import this module directly see the
same TOOLS_IN_MODULE that production sees; the aggregation logic lives at
the consumer site, not in a module-level side effect."""
