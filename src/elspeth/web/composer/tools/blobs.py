"""Composer blob-storage plane — session-scoped binary blob handlers.

Hosts:

- Tool handlers for blob CRUD: ``_execute_create_blob`` / ``_execute_update_blob``
  / ``_execute_delete_blob`` / ``_execute_get_blob_content`` /
  ``_handle_list_blobs`` / ``_handle_get_blob_metadata``.
- Quota policy (``_BLOB_QUOTA_BYTES``).
- Storage primitives (``_prepare_blob_create`` / ``_persist_prepared_blob_create`` /
  ``_sync_get_blob`` / ``_sync_list_blobs`` / ``_check_blob_quota``).
- Blob DTOs (``BlobToolRecord`` / ``BlobCreatePayload`` / ``_PreparedBlobCreate``).
- Shared public-service custody, recovery, and exact operation authority for
  content reads and mutations.
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
from typing import Any, Literal, TypedDict, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Engine, func, select

from elspeth.contracts.blobs import ALLOWED_MIME_TYPES, names_same_blob
from elspeth.contracts.blobs_inline import (
    ALLOWED_CONTENT_ENCODINGS,
    BlobInlineRef,
    ContentEncoding,
)
from elspeth.contracts.enums import CreationModality, is_llm_authored_creation_modality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.contracts.trust_boundary import observation_boundary, trust_boundary
from elspeth.web.blobs.protocol import (
    AllowedMimeType,
    BlobActiveRunError,
    BlobInProgressForkError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobPendingProposalError,
    BlobQuotaExceededError,
    BlobRecord,
    BlobStateError,
)
from elspeth.web.blobs.service import (
    BlobServiceImpl,
    _blob_custody_session_lock,
    _guard_blob_row_literals,
    _lock_session_for_blob_quota,
    _persist_blob_content,
    content_hash,
    sanitize_filename,
)
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import (
    CreateBlobArgumentsModel,
    GetBlobContentArgumentsModel,
    UpdateBlobArgumentsModel,
)
from elspeth.web.composer.state import (
    CompositionState,
)
from elspeth.web.composer.tools._common import (
    _BLOB_INLINE_REF_OWNERSHIP_SCHEMA_NOTE,
    _INTERPRETATION_REVIEW_FOLLOWUP,
    _RUNTIME_OWNED_LLM_OPTION_KEYS,
    _SERVER_OWNED_SOURCE_OPTION_KEYS,
    EmptyToolArgumentsModel,
    ToolContext,
    ToolResult,
    _composition_canonical_interpretation_requirement_error,
    _discovery_result,
    _failure_result,
    _mutation_result,
    _validate_mutation_arguments,
)
from elspeth.web.composer.tools.declarations import (
    ToolDeclaration,
    ToolKind,
)
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.provider_config_policy import web_aws_s3_endpoint_url_policy_error
from elspeth.web.sessions.models import (
    blobs_table,
)
from elspeth.web.sessions.protocol import SessionOperationAuthority


class BlobIdArgumentsModel(BaseModel):
    blob_id: str

    model_config = ConfigDict(extra="forbid")


class WireBlobInlineRefArgumentsModel(BaseModel):
    field_path: str
    blob_id: str
    encoding: ContentEncoding = "utf-8"

    model_config = ConfigDict(extra="forbid")


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


class BlobInlineDescriptor(TypedDict):
    """Closed file metadata exposed by the ready-blob discovery helper."""

    blob_id: str
    mime_type: str
    size_bytes: int
    content_hash: str
    filename: str


class BlobCreatePayload(TypedDict):
    """Closed dict shape for the create_blob tool's success result data.

    ``originated_in`` is the self-authorship marker (elspeth-47eba5cced):
    both producers of this payload — create_blob and a set_pipeline
    resolved inline_blob — describe a blob whose bytes came from the
    calling LLM's OWN tool arguments, and mid-turn custody rewrites excise
    those bytes from the live transcript, so without this marker a later
    get_blob_content round trip is structurally indistinguishable from
    discovering someone else's data.
    """

    blob_id: str
    filename: str
    mime_type: str
    size_bytes: int
    content_hash: str
    originated_in: Literal["this_tool_call"]


class BlobContentPayload(TypedDict):
    """Closed dict shape for the get_blob_content tool's success result data.

    ``created_by`` and ``creation_modality`` are the read-back half of the
    self-authorship marker on :class:`BlobCreatePayload` (elspeth-47eba5cced).
    ``originated_in`` can only speak for the tool call that created the blob;
    a later ``get_blob_content`` is a different call, and custody rewrites
    have by then excised the planner's own inline bytes from the live
    transcript.  Without these two fields the read-back result carried no
    origin facts at all, so a planner reading back content it fabricated
    earlier saw a discovery-shaped result and could narrate its own invention
    as something the system produced.

    Both are stored columns, not derived classifications: each is a closed
    vocabulary (``BLOB_CREATORS`` / :class:`CreationModality`) mirrored by a
    DB CHECK and re-checked on every read by ``_guard_blob_row_literals``.
    They are typed ``str`` here to match :class:`BlobToolRecord`, whose
    values that guard has already narrowed.

    Read them as a PAIR.  ``creation_modality`` alone is not an authorship
    statement: four unrelated paths write ``verbatim`` — the composer, for
    content copied out of the user's own message; ``create_blob`` behind the
    upload route; the authority facet's ``reserve_pending_output_blob`` for
    pipeline output; and ``copy_blobs_for_fork``.  ``created_by`` is what
    separates them.

    Both fields report what the row RECORDS, which is not always what
    happened: fork copy preserves ``created_by`` but resets the modality to
    ``verbatim`` and nulls the five ``creating_*`` columns, so an
    LLM-generated blob carried across a session fork records as verbatim.
    That is a defect in the fork writer, not something this read path can
    detect — and it is the reason no derived "the assistant authored this"
    flag is offered here: such a flag would confidently deny authorship of
    content the assistant really did invent.
    """

    blob_id: str
    filename: str
    mime_type: str
    content: str
    truncated: bool
    size_bytes: int
    created_by: str
    creation_modality: str


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


@observation_boundary(
    tier=3,
    source="LLM composer tool-call blob_id argument",
    source_param="blob_id",
    suppresses=("R5",),
    invariant="returns a repairable error message for non-string or non-UUID blob_id and None for canonical input; never raises on blob_id",
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
                # Paired with created_by so the inventory answers "who
                # authored these bytes" at the same depth as the read-back
                # path (get_blob_content). created_by alone cannot: the
                # composer writes created_by="assistant" both for content it
                # generated and for content copied verbatim from the user.
                "creation_modality": blob["creation_modality"],
                "status": blob["status"],
            }
            for blob in (_blob_row_to_tool_dict(row) for row in rows)
        ]


def _sync_list_ready_blob_inline_descriptors(engine: Engine, session_id: str) -> list[BlobInlineDescriptor]:
    """Return H4 visibility descriptors for ready session blobs."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(blobs_table)
            .where(blobs_table.c.session_id == session_id)
            .where(blobs_table.c.status == "ready")
            .order_by(blobs_table.c.created_at.desc())
            .limit(50)
        ).fetchall()

    descriptors: list[BlobInlineDescriptor] = []
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
    _validate_mutation_arguments(EmptyToolArgumentsModel, arguments, "list_blobs arguments")
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
    description=(
        "List uploaded/created files (blobs) in this session with metadata: each entry carries `id`, `filename`, "
        "`mime_type`, `size_bytes`, `status`, `created_by`, and `creation_modality`."
    ),
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
)


def _handle_list_composer_blobs(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """List blobs using the ADR-034 composer-LLM visibility shape.

    The LLM sees only metadata needed to author a pinned inline-content
    marker. Bytes, previews, storage paths, and free-text descriptions stay
    out of the response surface.
    """
    _validate_mutation_arguments(EmptyToolArgumentsModel, arguments, "list_composer_blobs arguments")
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
        "Returns a `blobs` list whose entries carry only `blob_id`, `mime_type`, `size_bytes`, `content_hash`, "
        "and `filename`; never content bytes."
    ),
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
)


def _handle_get_blob_metadata(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    validated = _validate_mutation_arguments(BlobIdArgumentsModel, arguments, "get_blob_metadata arguments")
    session_engine = context.session_engine
    session_id = context.session_id
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    blob_id_error = _blob_id_uuid_validation_error(validated.blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    blob = _sync_get_blob(session_engine, validated.blob_id, session_id)
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
    description="Get metadata for a specific blob (file) by ID: `id`, `filename`, `mime_type`, `size_bytes`, `content_hash`, and `status`.",
    json_schema={
        "type": "object",
        "properties": {
            "blob_id": {"type": "string", "description": "Blob ID."},
        },
        "required": ["blob_id"],
        "additionalProperties": False,
    },
)


@trust_boundary(
    tier=3,
    source="existing component options content (web/LLM-authored, thawed from persisted session state) navigated by an LLM-supplied field_path",
    source_param="container",
    suppresses=("R5",),
    invariant=(
        "raises ValueError when a field_path segment collides with an existing non-object value or "
        "when field_path carries no segment; never coerces an existing value into an object"
    ),
    test_ref="tests/unit/web/composer/test_blob_inline_tools.py::test_set_nested_option_rejects_non_object_segment_collision",
    test_fingerprint="f737be9d00e5cfa17240cfb4dda84f2dbf1ef46dd5be557429ef27613b2877d2",
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
        if keys[0] in _SERVER_OWNED_SOURCE_OPTION_KEYS:
            field_name = keys[0]
            raise ValueError(
                f"wire_blob_inline_ref cannot write server/resolver-owned source option root '{field_name}'. "
                "Bind source blobs with set_source_from_blob or set_source_from_blobs; "
                "ELSPETH stamps source_authoring and canonical blob metadata from session records."
            )
        # Symmetric with the node arm below: never let a wire write land inside a
        # source's interpretation_requirements. Source review metadata
        # (INVENTED_SOURCE) may only be staged as a pending composer requirement;
        # a wired ref here would corrupt that structure outside the review boundary.
        if keys[0] == INTERPRETATION_REQUIREMENTS_KEY:
            raise ValueError(
                "wire_blob_inline_ref cannot write source interpretation_requirements; "
                "stage an authorable pending source review with set_source or patch_source_options. "
                f"{_INTERPRETATION_REVIEW_FOLLOWUP}"
            )
        patched_options = _set_nested_option(dict(deep_thaw(source.options)), keys, marker)
        return state.with_named_source(source_name, replace(source, options=patched_options))

    if prefix.startswith("node:"):
        node_id = prefix.removeprefix("node:")
        new_nodes = []
        found = False
        for node in state.nodes:
            if node.id == node_id:
                if node.node_type not in ("transform", "aggregation", "collector") or node.plugin is None:
                    raise ValueError(
                        "Inline blob references can only be wired into source, transform, aggregation, collector, or output plugin options."
                    )
                if keys[0] == INTERPRETATION_REQUIREMENTS_KEY:
                    raise ValueError(
                        "wire_blob_inline_ref cannot write node interpretation_requirements; "
                        "stage an authorable pending node review with upsert_node or patch_node_options. "
                        f"{_INTERPRETATION_REVIEW_FOLLOWUP}"
                    )
                if node.plugin == "llm" and keys[0] in _RUNTIME_OWNED_LLM_OPTION_KEYS:
                    field_name = keys[0]
                    raise ValueError(
                        "wire_blob_inline_ref field_path targets runtime-owned top-level LLM option "
                        f"'{field_name}'. This field_path cannot be wired. For an author-owned LLM option "
                        "edit use patch_node_options, or upsert_node for a full node edit, without the runtime hash; "
                        "ELSPETH re-derives it during review reconciliation or execution."
                    )
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
                "outputs do not own review metadata. Stage an authorable pending review on its source or node. "
                f"{_INTERPRETATION_REVIEW_FOLLOWUP}"
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
        # Sources keep their prefix where nodes and outputs drop theirs: the
        # affected-component vocabulary is bare ids for nodes/outputs but
        # ``source_component_id(name)`` — i.e. "source:<name>" — for sources,
        # which is what every other source-mutating site reports. Stripping it
        # here made the bare name collide with a node id of the same name.
        return (prefix,)
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
    validated = _validate_mutation_arguments(WireBlobInlineRefArgumentsModel, arguments, "wire_blob_inline_ref arguments")
    session_engine = context.session_engine
    session_id = context.session_id
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")

    field_path = validated.field_path
    blob_id_error = _blob_id_uuid_validation_error(validated.blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    blob_id = UUID(validated.blob_id)

    # The argument model validates ContentEncoding and defaults absence to utf-8.
    encoding = validated.encoding

    blob = _sync_get_blob(session_engine, str(blob_id), session_id)
    if blob is None:
        return _failure_result(state, f"Blob '{blob_id}' not found.")
    if blob["status"] != "ready":
        return _failure_result(state, f"Blob '{blob_id}' is not ready (status: {blob['status']}).")
    pinned_hash = blob["content_hash"]
    if pinned_hash is None:
        raise AuditIntegrityError(f"Ready blob '{blob_id}' has null content_hash; cannot author inline_content ref")

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
        "Composer pins sha256 from blob metadata; callers must not pass content bytes. "
        "Returns the `field_path` that was wired."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "field_path": {
                "type": "string",
                "description": (
                    "Canonical path: source.options.<field>, source:<name>.options.<field>, "
                    "node:<node_id>.options.<field> for a transform, aggregation, or collector, "
                    "or output:<name>.options.<field>." + _BLOB_INLINE_REF_OWNERSHIP_SCHEMA_NOTE
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


@trust_boundary(
    tier=3,
    source="one component's frozen options tree (web/LLM-authored content retained through CompositionState freezing)",
    source_param="options",
    suppresses=("R5",),
    invariant=(
        "returns True only on a blob_ref / blob_id / *_blob_id value, or a blob:<uuid> path / file "
        "sentinel, that is UUID-identical to blob_id (any hex case), or a path / file value equal to "
        "storage_path, found by full structural traversal (mappings and list/tuple at any depth); raises "
        "AuditIntegrityError on a present-but-non-str blob_ref / blob_id / *_blob_id (audited-state "
        "corruption) rather than treating the blob as unbound"
    ),
    test_ref="tests/unit/web/composer/test_blob_inline_tools.py::test_state_options_reference_blob_crashes_on_non_str_blob_ref",
    test_fingerprint="cc4ab30745da588422649670128e04e05a2b19ad065a5750801cfc3a86e56a3b",
)
def _state_options_reference_blob(
    options: Mapping[str, Any],
    blob_id: str,
    storage_path: str,
    *,
    owner: str,
) -> bool:
    """Recursively inspect one component's options for references to a blob.

    Recognizes the UNION of the vocabularies the other blob walkers use
    (``guided/stage_transitions._option_blob_ids``,
    ``web/blobs/service._option_value_references_blob``,
    ``web/coordination/repository._option_value_references_blob``,
    ``yaml_generator``'s public-YAML strip list) — no single one of them
    knows the whole set: ``blob_ref`` values (top-level source bindings and
    nested inline-content markers), ``blob_id`` and any ``*_blob_id``
    custody key, and ``path``/``file`` values equal to either the blob's
    canonical ``storage_path`` or its ``blob:<uuid>`` sentinel.  Id values
    are compared by UUID identity (``names_same_blob``), because the binding
    path accepts either hex case.  Traversal is full: every nested mapping
    and every list/tuple element at any depth is inspected, so a binding
    inside a list of lists is seen.  Frozen state options are Mapping/tuple
    shaped, hence the structural checks here rather than the exact
    ``dict``/``list`` checks the DB-side walker uses.

    Before elspeth-4f3cd4155b this walker knew only ``blob_ref``/``path``/
    ``file`` and descended one level into sequences, and only into mapping
    elements — while its docstring claimed the coverage above. A blob bound
    through ``blob_id`` vocabulary, or through a mapping two sequence levels
    down, read as UNBOUND and became updatable/deletable under an accepted
    composition. This function is the SOLE retention guard over the live
    authored state on both update and delete; the adjacent
    ``_composition_references_blob`` only runs when an active run exists.

    A present-but-non-str ``blob_ref`` / ``blob_id`` / ``*_blob_id`` cannot
    arise from any valid authoring path (the canonical writers always record
    ``blob["id"]`` as a string); it is a corruption of the audited
    CompositionState.  Silently treating it as "not bound" would let
    update/delete mutate a blob that is in fact bound, defeating the guard —
    so escalate rather than suppress.
    """
    for key, value in options.items():
        if isinstance(key, str) and (key == "blob_ref" or key == "blob_id" or key.endswith("_blob_id")):
            if value is None:
                continue
            if not isinstance(value, str):
                raise AuditIntegrityError(f"{owner} has a non-str {key} ({type(value).__name__}); CompositionState integrity anomaly")
            if names_same_blob(value, blob_id):
                return True
        elif key in ("path", "file") and isinstance(value, str):
            if value == storage_path:
                return True
            if value.startswith("blob:") and names_same_blob(value.removeprefix("blob:"), blob_id):
                return True
        else:
            # Descend into the value whatever its shape. Mappings recurse so
            # key vocabulary applies at every depth; list/tuple elements are
            # each pushed in turn, so ``[[{"blob_ref": ...}]]`` is seen.
            # Scalars never reference a blob on their own: a bare string equal
            # to the storage path is only a binding under a ``path``/``file``
            # key, which is the DB-side walker's rule too.
            pending: list[object] = [value]
            while pending:
                item = pending.pop()
                if isinstance(item, Mapping):
                    if _state_options_reference_blob(item, blob_id, storage_path, owner=owner):
                        return True
                elif isinstance(item, (list, tuple)):
                    pending.extend(item)
    return False


def _state_references_blob(state: CompositionState, blob_id: str, storage_path: str) -> bool:
    """Whether the current composition references a blob anywhere.

    Walks every source, node, and output option tree — the same coverage
    ``_composition_references_blob`` applies to an active run's persisted
    pipeline dict, applied here to the live authored state so update/delete
    cannot invalidate an accepted composition through a nested reference the
    old top-level-source check never saw (elspeth-b3feba9a7c).
    """
    for source_name, source in state.sources.items():
        if _state_options_reference_blob(source.options, blob_id, storage_path, owner=f"Source '{source_name}'"):
            return True
    for node in state.nodes:
        if _state_options_reference_blob(node.options, blob_id, storage_path, owner=f"Node '{node.id}'"):
            return True
    for output in state.outputs:
        if _state_options_reference_blob(output.options, blob_id, storage_path, owner=f"Output '{output.name}'"):
            return True
    return False


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
    wave that makes it dead (CONTRIBUTING.md §Code Standards).

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
    — the constraint IS the validation, per the engine-patterns-reference
    skill §Offensive Programming Examples.
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


def _require_blob_tool_authority(context: ToolContext) -> tuple[SessionOperationAuthority, SessionOperationContext]:
    operation = context.session_operation_context
    authority = context.session_operation_authority
    if type(operation) is not SessionOperationContext or authority is None:
        raise AuditIntegrityError("Blob effects require the actual session operation context and authority")
    if operation.fence.session_id != context.session_id:
        raise AuditIntegrityError("Blob effect operation context does not own the session")
    authority.compare_and_swap(operation)
    return authority, operation


def _persist_prepared_blob_create(
    prepared: _PreparedBlobCreate,
    *,
    session_engine: Engine,
    session_id: str,
    max_blob_storage_per_session_bytes: int | None = None,
    session_operation_context: SessionOperationContext | None = None,
    session_operation_authority: SessionOperationAuthority | None = None,
) -> str | None:
    """Persist a prepared blob through the shared blob custody primitive."""
    resolved_storage = prepared.storage_path.expanduser().resolve()
    session_dir = resolved_storage.parent
    blobs_dir = session_dir.parent
    if session_dir.name != session_id or blobs_dir.name != "blobs":
        raise AuditIntegrityError("Prepared blob storage path does not match its session custody root")
    data_dir = blobs_dir.parent
    if type(session_operation_context) is not SessionOperationContext or session_operation_authority is None:
        raise AuditIntegrityError("Prepared blob creation requires the actual operation context and authority")
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
            session_operation_context=session_operation_context,
            session_operation_authority=session_operation_authority,
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
        "originated_in": "this_tool_call",
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
        raw_mime_type = arguments["mime_type"] if "mime_type" in arguments else None
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

    quota_error = _persist_prepared_blob_create(
        prepared,
        session_engine=session_engine,
        session_id=session_id,
        max_blob_storage_per_session_bytes=context.max_blob_storage_per_session_bytes,
        session_operation_context=context.session_operation_context,
        session_operation_authority=context.session_operation_authority,
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
        "mid-conversation without requiring manual upload. Returns the new blob's `blob_id`, `filename`, `mime_type`, "
        "`content_hash`, `size_bytes`, and `originated_in` (`this_tool_call`: the blob was authored by "
        "this call, not uploaded)."
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
    """Replace content through the shared, fenced durable replacement driver."""
    from elspeth.web.blobs.replacement import BlobReplacementCoordinator
    from elspeth.web.coordination.repository import SessionDerivedCustodyError

    if context.session_engine is None or context.session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    if context.data_dir is None:
        return _failure_result(state, "Blob tools require data_dir for storage.")
    try:
        validated = UpdateBlobArgumentsModel.model_validate(arguments)
    except PydanticValidationError as exc:
        raise ToolArgumentError(
            argument="update_blob arguments",
            expected="object conforming to UpdateBlobArgumentsModel",
            actual_type=type(exc).__name__,
        ) from exc
    blob_id_error = _blob_id_uuid_validation_error(validated.blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    try:
        content_bytes = validated.content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolArgumentError(
            argument="update_blob content",
            expected="valid UTF-8 text",
            actual_type=type(exc).__name__,
        ) from exc
    authority, operation = _require_blob_tool_authority(context)
    provenance = _blob_creation_provenance(validated.content, context)
    driver = BlobReplacementCoordinator(
        engine=context.session_engine,
        data_dir=Path(context.data_dir),
        session_operation_authority=authority,
    )
    # Repair any interrupted version before pinning the metadata snapshot.
    driver.reconcile(context=operation)
    try:
        expected = authority.mutate(
            operation,
            lambda transaction: transaction.blobs.read_blob(blob_id=UUID(validated.blob_id)),
        )
        if _state_references_blob(state, validated.blob_id, expected.storage_path):
            return _failure_result(
                state,
                f"Blob '{validated.blob_id}' is referenced by the current composition and cannot be updated; unbind it first.",
            )
        replacement = replace(
            expected,
            size_bytes=len(content_bytes),
            content_hash=content_hash(content_bytes),
            creation_modality=provenance.creation_modality,
            created_from_message_id=_blob_provenance_message_id(context.user_message_id),
            creating_model_identifier=provenance.creating_model_identifier,
            creating_model_version=provenance.creating_model_version,
            creating_provider=provenance.creating_provider,
            creating_composer_skill_hash=provenance.creating_composer_skill_hash,
            creating_arguments_hash=provenance.creating_arguments_hash,
        )
        result = driver.replace_blob(
            expected=expected,
            replacement=replacement,
            content=content_bytes,
            context=operation,
            max_storage_per_session=_resolve_blob_quota_bytes(context.max_blob_storage_per_session_bytes),
            accepting_proposal_id=UUID(context.executing_proposal_id) if context.executing_proposal_id is not None else None,
        )
    except SessionDerivedCustodyError:
        return _failure_result(state, f"Blob '{validated.blob_id}' not found.")
    except (BlobActiveRunError, BlobPendingProposalError, BlobInProgressForkError, BlobQuotaExceededError, BlobStateError) as exc:
        return _failure_result(state, str(exc))
    return _discovery_result(
        state,
        {
            "blob_id": str(result.id),
            "filename": result.filename,
            "mime_type": result.mime_type,
            "size_bytes": result.size_bytes,
            "content_hash": result.content_hash,
        },
    )


_UPDATE_BLOB_DECLARATION = ToolDeclaration(
    name="update_blob",
    handler=_execute_update_blob,
    kind=ToolKind.BLOB_MUTATION,
    description=(
        "Update the content of an existing blob (file). Overwrites the file content while preserving metadata. "
        "Returns `blob_id`, `filename`, `mime_type`, and the new `content_hash` and `size_bytes`."
    ),
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
    """Delete through the same custody ledger as the authenticated API."""
    validated = _validate_mutation_arguments(BlobIdArgumentsModel, arguments, "delete_blob arguments")
    from elspeth.web.coordination.repository import SessionDerivedCustodyError

    if context.session_engine is None or context.session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    if context.data_dir is None:
        return _failure_result(state, "Blob tools require data_dir for storage.")
    blob_id = validated.blob_id
    blob_id_error = _blob_id_uuid_validation_error(blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    authority, operation = _require_blob_tool_authority(context)
    service = BlobServiceImpl(
        context.session_engine,
        Path(context.data_dir),
        session_operation_authority=authority,
    )
    with _blob_custody_session_lock(context.session_engine, context.session_id):
        service._reconcile_blob_deletions_locked(operation, exclude_blob_id=UUID(blob_id))
        try:
            record = authority.mutate(
                operation,
                lambda transaction: transaction.blobs.read_blob(blob_id=UUID(blob_id)),
            )
        except SessionDerivedCustodyError:
            # The shared driver distinguishes an absent row from a durable
            # committed deletion whose filesystem cleanup still needs retry.
            record = None
        if record is not None and _state_references_blob(state, blob_id, record.storage_path):
            return _failure_result(
                state,
                f"Blob '{blob_id}' is referenced by the current composition and cannot be deleted; unbind it first.",
            )
        try:
            service._delete_blob_with_ledger(
                blob_id=UUID(blob_id),
                context=operation,
                accepting_proposal_id=UUID(context.executing_proposal_id) if context.executing_proposal_id is not None else None,
            )
        except (BlobNotFoundError, BlobActiveRunError, BlobPendingProposalError, BlobInProgressForkError) as exc:
            return _failure_result(state, str(exc))
    return _discovery_result(state, {"blob_id": blob_id, "deleted": True})


_DELETE_BLOB_DECLARATION = ToolDeclaration(
    name="delete_blob",
    handler=_execute_delete_blob,
    kind=ToolKind.BLOB_MUTATION,
    description="Delete a blob (file) and its storage. Returns the deleted `blob_id` and `deleted`: true.",
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


def _blob_record_to_tool_dict(record: BlobRecord) -> BlobToolRecord:
    return {
        "id": str(record.id),
        "session_id": str(record.session_id),
        "filename": record.filename,
        "mime_type": record.mime_type,
        "size_bytes": record.size_bytes,
        "content_hash": record.content_hash,
        "storage_path": record.storage_path,
        "created_by": record.created_by,
        "source_description": record.source_description,
        "status": record.status,
        "creation_modality": record.creation_modality.value,
        "created_from_message_id": record.created_from_message_id,
        "creating_model_identifier": record.creating_model_identifier,
        "creating_model_version": record.creating_model_version,
        "creating_provider": record.creating_provider,
        "creating_composer_skill_hash": record.creating_composer_skill_hash,
        "creating_arguments_hash": record.creating_arguments_hash,
    }


def _locked_read_ready_blob(
    session_engine: Engine,
    session_id: str,
    blob_id: str,
    *,
    data_dir: str | None,
    session_operation_context: SessionOperationContext | None,
    session_operation_authority: SessionOperationAuthority | None,
) -> tuple[BlobToolRecord | None, bytes | None]:
    """Observe one version through the API's exact custody and recovery path."""
    if data_dir is None:
        raise AuditIntegrityError("Blob content reads require the service storage root")
    if type(session_operation_context) is not SessionOperationContext or session_operation_authority is None:
        raise AuditIntegrityError("Blob reads require the actual session operation context and authority")
    if session_operation_context.fence.session_id != session_id:
        raise AuditIntegrityError("Blob read context does not match the requested store")
    authority, operation = session_operation_authority, session_operation_context
    service = BlobServiceImpl(
        session_engine,
        Path(data_dir),
        session_operation_authority=authority,
    )
    try:
        with service._locked_blob_row_for_read(blob_id, operation) as row:
            record = _blob_record_to_tool_dict(row)
            if record["status"] != "ready":
                return record, None
            try:
                data = Path(row.storage_path).read_bytes()
            except FileNotFoundError:
                return record, None
            _verify_blob_content_integrity(record, data)
            return record, data
    except BlobNotFoundError:
        return None, None


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

    validated = _validate_mutation_arguments(GetBlobContentArgumentsModel, arguments, "get_blob_content arguments")
    blob_id = validated.blob_id
    blob_id_error = _blob_id_uuid_validation_error(blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    # Guards 1 + 2 (lifecycle, integrity) run inside the same-session
    # custody lock so the row and the bytes are one version — a read
    # racing update/delete must block rather than pair the old committed
    # hash with freshly-swapped bytes (elspeth-3d1d1fcb6c).
    blob, data = _locked_read_ready_blob(
        session_engine,
        session_id,
        blob_id,
        data_dir=context.data_dir,
        session_operation_context=context.session_operation_context,
        session_operation_authority=context.session_operation_authority,
    )
    if blob is None:
        return _failure_result(state, f"Blob '{blob_id}' not found.")

    blob_status = blob["status"]
    if blob_status != "ready":
        return _failure_result(
            state,
            f"Blob '{blob_id}' is not readable — status is '{blob_status}', expected 'ready'.",
        )

    if data is None:
        return _failure_result(state, f"Blob storage file missing for '{blob_id}'.")

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

    payload: BlobContentPayload = {
        "blob_id": blob_id,
        "filename": blob["filename"],
        "mime_type": blob["mime_type"],
        "content": content,
        "truncated": truncated,
        "size_bytes": blob["size_bytes"],
        # Origin facts, read straight off the row. See BlobContentPayload:
        # they are the only within-result signal that distinguishes reading
        # back self-authored content from discovering an operator's file.
        "created_by": blob["created_by"],
        "creation_modality": blob["creation_modality"],
    }
    return _discovery_result(state, payload)


_GET_BLOB_CONTENT_DECLARATION = ToolDeclaration(
    name="get_blob_content",
    handler=_execute_get_blob_content,
    kind=ToolKind.BLOB_DISCOVERY,
    description=(
        "Retrieve a blob for inspection: `blob_id`, `filename`, `mime_type`, and UTF-8 decoded `content`. "
        "Large files are truncated to 50,000 characters "
        "(`truncated` is true when so; `size_bytes` is the full size). "
        "The result also carries the blob's recorded origin — `created_by` (user, assistant, or pipeline) and "
        "`creation_modality` — so content the assistant generated earlier is not mistaken for a discovered file."
    ),
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
