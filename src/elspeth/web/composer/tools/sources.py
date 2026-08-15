"""Composer sources plane — source-spec tool handlers and source-from-blob bridge."""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Engine, select

from elspeth.contracts.blobs import STORAGE_MIME_TYPES
from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.enums import CreationModality, is_llm_authored_creation_modality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import (
    PatchSourceOptionsArgumentsModel,
    SetSourceArgumentsModel,
    SetSourceFromBlobArgumentsModel,
    SetSourceFromBlobsArgumentsModel,
)
from elspeth.web.composer.source_inspection import (
    delimiter_for_filename,
    facts_to_dict,
    inspect_blob_content,
)
from elspeth.web.composer.state import (
    CompositionState,
    SourceSpec,
    validate_composer_source_name,
)
from elspeth.web.composer.tools._common import (
    _DEFAULT_SOURCE_VALIDATION_FAILURE,
    _SOURCE_VALIDATION_FAILURE_DESCRIPTION,
    _STEP_DESCRIPTION_DESCRIPTION,
    PendingCustodyBlobView,
    ToolContext,
    ToolResult,
    _apply_merge_patch,
    _attach_post_call_hints,
    _canonical_interpretation_requirement_error,
    _canonicalize_authored_interpretation_requirements,
    _credential_wiring_contract_failure,
    _discovery_result,
    _failure_result,
    _mutation_result,
    _options_with_pending_requirement,
    _pending_interpretation_requirement,
    _plugin_policy_failure,
    _prevalidate_source_for_context,
    _prohibited_section,
    _resolver_owned_interpretation_requirement_error,
    _source_review_requirement_id,
    _validate_plugin_name,
    _validate_source_path,
    _vf_destination_note,
    canonicalize_source_validation_failure,
)
from elspeth.web.composer.tools.blobs import (
    BlobToolRecord,
    _blob_id_uuid_validation_error,
    _blob_row_to_tool_dict,
    _locked_read_ready_blob,
    _PreparedBlobCreate,
    _sync_get_blob,
    _verify_blob_content_hash,
)
from elspeth.web.composer.tools.declarations import (
    ToolDeclaration,
    ToolKind,
)
from elspeth.web.interpretation_state import SOURCE_AUTHORING_KEY, SourceAuthoringMetadata, source_component_id
from elspeth.web.provider_config_policy import (
    web_aws_s3_endpoint_url_policy_error,
)
from elspeth.web.sessions.models import blobs_table

_INSPECT_SOURCE_MAX_BYTES = 8 * 1024
_BLOB_HASH_CHUNK_BYTES = 1024 * 1024
_INLINE_CSV_HEADER_READ_BYTES = 64 * 1024
_CSV_FIELD_MAX_CHARS = 64 * 1024
_CSV_MAX_COLUMNS = 4096
_INLINE_CSV_CANDIDATE_SCAN_LIMIT = 50


class _CsvContentBoundaryError(ValueError):
    """One redacted domain error for unsafe or malformed CSV content."""


class _CsvParseIncompleteError(_CsvContentBoundaryError):
    """The csv module could not complete a record from the supplied content
    because its input ended (e.g. an unterminated quoted field). Distinct from
    syntax and structural limit failures in ``_bounded_csv_rows``, which are
    true regardless of how much more content exists. A caller that knows its
    ``content`` is itself a bounded prefix of a larger file may treat this
    specific error as expected truncation rather than corruption.
    """


def _bounded_csv_rows(content: str) -> Iterator[list[str]]:
    """Yield CSV rows while containing parser and structural limit failures."""

    input_exhausted = False

    def _content_lines() -> Iterator[str]:
        nonlocal input_exhausted
        yield from io.StringIO(content)
        input_exhausted = True

    try:
        for row in csv.reader(_content_lines(), strict=True):
            if len(row) > _CSV_MAX_COLUMNS or any(len(cell) > _CSV_FIELD_MAX_CHARS or "\x00" in cell for cell in row):
                raise _CsvContentBoundaryError("CSV content exceeds bounded inspection limits")
            yield row
    except csv.Error as exc:
        error_type = _CsvParseIncompleteError if input_exhausted else _CsvContentBoundaryError
        raise error_type("CSV content exceeds bounded inspection limits") from exc


class InspectSourceArgumentsModel(BaseModel):
    blob_id: str

    model_config = ConfigDict(extra="forbid")


def _handle_list_sources(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    return _discovery_result(
        state,
        {
            "available": context.catalog.list_sources(),
            "prohibited": _prohibited_section(context.catalog.list_prohibited_sources()),
        },
    )


_LIST_SOURCES_DECLARATION = ToolDeclaration(
    name="list_sources",
    handler=_handle_list_sources,
    kind=ToolKind.DISCOVERY,
    description=(
        "List available source plugins with name and summary. The result's "
        "`prohibited` array names any source categorically banned from the web "
        "authoring surface by security policy, with its closed reason and "
        "explanation — cite it when a user asks why a specific plugin is unavailable."
    ),
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    cacheable=True,
)


def _handle_set_source(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    result = _execute_set_source(arguments, state, context)
    if not result.success:
        return result
    source_name = SetSourceArgumentsModel.model_validate(arguments).source_name
    if source_name not in result.updated_state.sources:
        return result
    source = result.updated_state.sources[source_name]
    return _attach_post_call_hints(
        result,
        context.catalog,
        plugin_type="source",
        tool_name="set_source",
        plugin_name=source.plugin,
        config_snapshot=source.options,
    )


_SET_SOURCE_DECLARATION = ToolDeclaration(
    name="set_source",
    handler=_handle_set_source,
    kind=ToolKind.MUTATION,
    description="Set or replace a named pipeline source.",
    json_schema={
        "type": "object",
        "properties": {
            "source_name": {
                "type": "string",
                "description": "Stable source root name. Defaults to 'source' for legacy single-source pipelines.",
            },
            "plugin": {"type": "string", "description": "Source plugin name."},
            "on_success": {
                "type": "string",
                "description": (
                    "Connection-name string this source PUBLISHES. Some downstream consumer "
                    "(transform 'input' or output 'sink_name') MUST equal this value — the "
                    "runtime matches strings, not graph topology."
                ),
            },
            "options": {"type": "object", "description": "Plugin-specific config."},
            "on_validation_failure": {
                "type": "string",
                "description": _SOURCE_VALIDATION_FAILURE_DESCRIPTION,
            },
            "description": {
                "type": ["string", "null"],
                "description": _STEP_DESCRIPTION_DESCRIPTION,
            },
        },
        "required": ["plugin", "on_success", "options", "on_validation_failure"],
        "additionalProperties": False,
    },
    augments_on_failure=True,
)


_MIME_TO_SOURCE: dict[str, tuple[str, dict[str, str]]] = {
    "text/csv": ("csv", {}),
    "application/json": ("json", {}),
    "application/x-jsonlines": ("json", {"format": "jsonl"}),
    "application/jsonl": ("json", {"format": "jsonl"}),
    "text/jsonl": ("json", {"format": "jsonl"}),
    "text/plain": ("text", {}),
}


def _delimiter_extra_for_csv_blob(
    plugin: str,
    filename: str,
    caller_options: Mapping[str, Any],
) -> dict[str, str]:
    """Derive a filename-implied csv delimiter for a blob-backed source.

    ``_MIME_TO_SOURCE`` keys only on MIME type, so a ``.tsv`` file uploaded as
    ``text/csv`` binds the ``csv`` plugin with no delimiter and parses as one
    column at runtime. ``inspect_blob_content`` already reports the tab via
    :func:`delimiter_for_filename`; reuse that single rule so inspection and
    binding agree. Inject only a fixed tab and only when (a) the bound plugin is
    ``csv``, (b) the filename means tab, and (c) the caller/LLM did not already
    supply a delimiter (never clobber an explicit one).
    """
    if plugin != "csv" or "delimiter" in caller_options:
        return {}
    delimiter = delimiter_for_filename(filename)
    return {"delimiter": delimiter} if delimiter is not None else {}


class SourceBlobPayload(TypedDict):
    """LLM/audit-safe source blob metadata for set_pipeline/set_source_from_blob."""

    blob_id: str
    filename: str
    mime_type: str
    size_bytes: int
    content_hash: str | None


@dataclass(frozen=True, slots=True)
class _ResolvedSourceBlob:
    plugin: str
    options: Mapping[str, Any]
    payload: SourceBlobPayload
    creation_modality: CreationModality

    def __post_init__(self) -> None:
        # ``options`` is the resolved-source pipeline-options mapping; it
        # may carry nested dicts/lists from the composer YAML and is
        # mutable through the attribute reference without a freeze guard,
        # defeating ``frozen=True``. ``payload`` is a SourceBlobPayload
        # TypedDict whose declared fields are all scalars (str / int /
        # str | None) — the dict itself is a container so we deep-freeze
        # both for symmetry rather than relying on caller discipline to
        # keep payload scalar-only forever.
        freeze_fields(self, "options", "payload")


def _source_blob_payload(blob: BlobToolRecord) -> SourceBlobPayload:
    """Return source-blob metadata without leaking storage_path."""
    return {
        "blob_id": blob["id"],
        "filename": blob["filename"],
        "mime_type": blob["mime_type"],
        "size_bytes": blob["size_bytes"],
        "content_hash": blob["content_hash"],
    }


def _source_authoring_options(
    creation_modality: CreationModality,
    content_hash_value: str | None,
) -> dict[str, SourceAuthoringMetadata]:
    """Return source authoring metadata for LLM-authored blob-backed sources."""
    if not is_llm_authored_creation_modality(creation_modality):
        return {}
    if content_hash_value is None:
        raise AuditIntegrityError("LLM-authored blob-backed source requires non-null content_hash for source authoring metadata")
    return {
        SOURCE_AUTHORING_KEY: {
            "modality": creation_modality.value,
            "content_hash": content_hash_value,
            "review_event_id": None,
            "resolved_kind": None,
        }
    }


def _source_blob_review_user_term(*, mime_type: str, content: str) -> str:
    """Choose the stable Class 2 review term for an LLM-authored source blob."""
    if mime_type != "text/csv":
        return "inline_source_data"
    try:
        header = next(_bounded_csv_rows(content), ())
    except _CsvContentBoundaryError:
        return "inline_source_data"
    if len(header) == 1 and header[0].strip().lower() == "url":
        return "inline_source_url_list"
    return "inline_source_data"


def _options_with_source_blob_review(
    options: Mapping[str, Any],
    *,
    mime_type: str,
    content: str,
    existing_options: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Ensure LLM-authored blob-backed sources carry a Class 2 review gate."""
    if SOURCE_AUTHORING_KEY not in options:
        return options
    user_term = _source_blob_review_user_term(mime_type=mime_type, content=content)
    requirement = _pending_interpretation_requirement(
        requirement_id=_source_review_requirement_id(user_term),
        kind=InterpretationKind.INVENTED_SOURCE,
        user_term=user_term,
        draft=content,
    )
    return _options_with_pending_requirement(
        options,
        requirement=requirement,
        replace_kind=InterpretationKind.INVENTED_SOURCE,
        existing_options=existing_options,
    )


def _manual_source_authoring_error(*, tool_name: str) -> str:
    """Return the source-options error for caller-supplied source_authoring."""
    return (
        f"{tool_name} must not be called with '{SOURCE_AUTHORING_KEY}' in source.options. "
        "source_authoring is reserved for ELSPETH's blob provenance writer and is stamped only when "
        "a source is bound through set_source_from_blob, source.blob_id, or source.inline_blob."
    )


def _reject_manual_source_authoring(
    options: Mapping[str, Any],
    *,
    tool_name: str = "set_source_from_blob",
) -> str | None:
    """Reject caller-supplied source_authoring outside provenance stamping."""
    if SOURCE_AUTHORING_KEY not in options:
        return None
    return _manual_source_authoring_error(tool_name=tool_name)


def _reject_manual_source_blobs(
    options: Mapping[str, Any],
    *,
    tool_name: str,
) -> str | None:
    """Reject a caller-supplied plural ``blobs`` list outside the resolver.

    The plural blob_rows binding is resolver-owned (elspeth-0c6a343921):
    ``set_source_from_blobs`` resolves every entry field from the session's
    authoritative blob records and refuses LLM-authored blobs. A generic
    writer carrying ``blobs`` would author that custody by assertion, so
    the key is reserved everywhere except resolver output (run admission
    independently re-verifies modality and entry facts as the backstop).
    """
    if "blobs" not in options:
        return None
    return (
        f"{tool_name} must not be called with 'blobs' in source options. "
        "The plural blob binding is resolved from session blob records by set_source_from_blobs; "
        "bind or rebind blobs through that tool instead of authoring the list directly."
    )


def _source_component_id(source_name: str) -> str:
    """Return the legacy/default or named source component identifier."""
    return source_component_id(source_name)


def _validate_source_name_argument(source_name: str) -> None:
    """Raise ARG_ERROR for invalid LLM-supplied source names."""
    try:
        validate_composer_source_name(source_name)
    except ValueError as exc:
        raise ToolArgumentError(
            argument="source_name",
            expected="a valid composer source name",
            actual_type="str",
        ) from exc


def _pending_custody_tool_record(pending: PendingCustodyBlobView) -> BlobToolRecord:
    """Project the deferred-custody view as the ready row it will settle to.

    Field-for-field the row :func:`persist_inline_custody_blob_on_connection`
    inserts at the atomic staging settlement (elspeth-282f392fae): the view
    is built by the same normalization and storage-path derivation, so this
    projection and the settled row cannot disagree.
    """
    return {
        "id": pending.blob_id,
        "session_id": pending.session_id,
        "filename": pending.filename,
        "mime_type": pending.mime_type,
        "size_bytes": pending.size_bytes,
        "content_hash": pending.content_hash,
        "storage_path": pending.storage_path,
        "created_by": "assistant",
        "source_description": pending.source_description,
        "status": "ready",
        "creation_modality": pending.creation_modality,
        "created_from_message_id": pending.created_from_message_id,
        "creating_model_identifier": pending.creating_model_identifier,
        "creating_model_version": pending.creating_model_version,
        "creating_provider": pending.creating_provider,
        "creating_composer_skill_hash": pending.creating_composer_skill_hash,
        "creating_arguments_hash": pending.creating_arguments_hash,
    }


def _resolve_source_blob(
    *,
    blob_id: str,
    explicit_plugin: str | None,
    caller_options: Mapping[str, Any],
    on_validation_failure: str,
    state: CompositionState,
    context: ToolContext,
    session_engine: Engine | None,
    session_id: str | None,
    tool_name: str = "set_source_from_blob",
    source_name: str = "source",
    existing_options: Mapping[str, Any] | None = None,
) -> _ResolvedSourceBlob | ToolResult:
    """Resolve an existing ready blob into authoritative source options."""
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    blob_id_error = _blob_id_uuid_validation_error(blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    manual_authoring_error = _reject_manual_source_authoring(caller_options, tool_name=tool_name)
    if manual_authoring_error is not None:
        return _failure_result(state, manual_authoring_error)
    blob = _sync_get_blob(session_engine, blob_id, session_id)
    # Deferred inline custody (elspeth-282f392fae): the planner's custody-safe
    # revalidation runs BEFORE the atomic staging settlement materializes the
    # blob, so the one blob this plan will settle is resolvable from the
    # server-derived view on the context. Exact blob_id AND session_id match
    # only; any other reference keeps the fail-closed database verdict, and a
    # row that already exists (idempotent replay) stays authoritative.
    pending_content: bytes | None = None
    pending = context._pending_custody
    if blob is None and pending is not None and pending.blob_id == blob_id and pending.session_id == session_id:
        blob = _pending_custody_tool_record(pending)
        pending_content = pending.content
    if blob is None:
        return _failure_result(state, f"Blob '{blob_id}' not found.")

    if blob["status"] != "ready":
        return _failure_result(state, f"Blob is not ready (status: {blob['status']}).")

    mime_extra: dict[str, str] = {}
    if explicit_plugin:
        plugin = explicit_plugin
    elif blob["mime_type"] not in _MIME_TO_SOURCE:
        return _failure_result(
            state,
            f"Cannot infer source plugin for MIME type '{blob['mime_type']}'. Please specify the 'plugin' parameter explicitly.",
        )
    else:
        plugin, mime_extra = _MIME_TO_SOURCE[blob["mime_type"]]

    creation_modality = CreationModality(blob["creation_modality"])
    merged_options: Mapping[str, Any] = {
        **caller_options,
        **mime_extra,
        **_delimiter_extra_for_csv_blob(plugin, blob["filename"], caller_options),
        "path": blob["storage_path"],
        "blob_ref": blob["id"],
        "mode": "bind_source",
        **_source_authoring_options(creation_modality, blob["content_hash"]),
    }
    if SOURCE_AUTHORING_KEY in merged_options:
        # Row + bytes must be observed as ONE version under the session
        # custody lock: an unlocked read racing update_blob's in-transaction
        # file swap pairs the stale committed hash with the new bytes and
        # escalates a false BlobIntegrityError (elspeth-3d1d1fcb6c).
        # A deferred-custody resolution has no row or file to lock yet; the
        # view IS the single version (content and hash derived together
        # server-side), so it satisfies the same one-version guarantee.
        fresh_blob: BlobToolRecord | None
        data: bytes | None
        if pending_content is not None:
            fresh_blob, data = blob, pending_content
        else:
            fresh_blob, data = _locked_read_ready_blob(session_engine, session_id, blob_id)
        if fresh_blob is None:
            return _failure_result(state, f"Blob '{blob_id}' not found.")
        if fresh_blob["status"] != "ready":
            return _failure_result(state, f"Blob is not ready (status: {fresh_blob['status']}).")
        if data is None:
            return _failure_result(state, f"Blob storage file missing for '{blob_id}'.")
        if fresh_blob["content_hash"] != blob["content_hash"]:
            # A full update committed between the metadata fetch that pinned
            # ``merged_options`` (content_hash / authoring metadata) and this
            # locked read.  Binding would author a source whose pinned hash
            # disagrees with its reviewed content — fail recoverably instead.
            return _failure_result(
                state,
                f"Blob '{blob_id}' content changed while binding the source; retry the tool call.",
            )
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            return _failure_result(
                state,
                f"Blob '{blob_id}' is not valid UTF-8 text ({exc.reason} at byte offset {exc.start}).",
            )
        merged_options = _options_with_source_blob_review(
            merged_options,
            mime_type=blob["mime_type"],
            content=content,
            existing_options=existing_options,
        )
    canonical_error = _canonical_interpretation_requirement_error(
        merged_options,
        tool_name=tool_name,
    )
    if canonical_error is not None:
        return _failure_result(
            state,
            canonical_error,
            error_code="interpretation_requirements_invalid",
        )
    try:
        context.catalog.get_schema("source", plugin)
    except (ValueError, KeyError) as exc:
        return _failure_result(state, f"Unknown source plugin '{plugin}': {exc}")
    prevalidation_error = _prevalidate_source_for_context(
        context,
        plugin,
        merged_options,
        on_validation_failure,
        source_name=source_name,
    )
    if prevalidation_error is not None:
        return _failure_result(state, prevalidation_error)

    return _ResolvedSourceBlob(
        plugin=plugin,
        options=merged_options,
        payload=_source_blob_payload(blob),
        creation_modality=creation_modality,
    )


def _manual_source_blob_ref_error(*, tool_name: str, inline_blob_supported: bool = False) -> str:
    """Return the source-options error for tools that reject manual blob_ref."""
    if inline_blob_supported:
        bind_path = "set_source_from_blob, source.blob_id, or source.inline_blob"
    else:
        bind_path = "set_source_from_blob"
    return (
        f"Use {bind_path} to bind a blob to the source. "
        f"{tool_name} must not be called with 'blob_ref' in source.options "
        "because it cannot enforce that 'path' equals the blob's canonical storage_path."
    )


def _reject_manual_source_blob_ref(
    options: Mapping[str, Any],
    *,
    tool_name: str,
    inline_blob_supported: bool = False,
) -> str | None:
    """Reject caller-supplied blob_ref outside authoritative blob-binding tools."""
    if "blob_ref" not in options:
        return None
    return _manual_source_blob_ref_error(tool_name=tool_name, inline_blob_supported=inline_blob_supported)


def _execute_set_source(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Set or replace the pipeline source.

    Tier-3 boundary: ``args`` is an LLM-supplied dict.  Validated via the
    Pydantic redaction-bearing model :class:`SetSourceArgumentsModel` (the
    single source of truth for the argument schema — supersedes the
    deleted ``_TOOL_REQUIRED_PATHS["set_source"]`` entry in ``service.py``,
    rev-3 N7 / rev-4 M1).

    On :class:`pydantic.ValidationError` the handler re-raises as
    :class:`ToolArgumentError` so the compose loop's ARG_ERROR routing at
    ``service.py:2480`` receives the right exception class.  A bare
    ``ValidationError`` would escape into the catch-all
    (``ComposerPluginCrashError`` → HTTP 500) — wrong disposition for
    Tier-3 input.  Pattern: ``tools.py:2668, 2761, 2767, 2773, 2787, 2801``.
    """
    try:
        validated = SetSourceArgumentsModel.model_validate(args)
    except PydanticValidationError as exc:
        raise ToolArgumentError(
            argument="set_source arguments",
            expected="object conforming to SetSourceArgumentsModel",
            actual_type=type(exc).__name__,
        ) from exc

    plugin = validated.plugin
    options: Mapping[str, Any] = validated.options
    source_name = validated.source_name
    _validate_source_name_argument(source_name)

    # Stage A validates the untrusted compact shell before any plugin work.
    review_metadata_error = _resolver_owned_interpretation_requirement_error(
        options,
        tool_name="set_source",
        component_id=_source_component_id(source_name),
        source=True,
    )
    if review_metadata_error is not None:
        return _failure_result(
            state,
            review_metadata_error,
            error_code="interpretation_requirements_invalid",
        )
    options = _canonicalize_authored_interpretation_requirements(
        options,
        component_id=_source_component_id(source_name),
        source=True,
        existing_options=state.sources[source_name].options if source_name in state.sources else None,
    )
    canonical_error = _canonical_interpretation_requirement_error(
        options,
        tool_name="set_source",
    )
    if canonical_error is not None:
        return _failure_result(
            state,
            canonical_error,
            error_code="interpretation_requirements_invalid",
        )

    endpoint_policy_error = web_aws_s3_endpoint_url_policy_error(plugin, options)
    if endpoint_policy_error is not None:
        return _failure_result(state, endpoint_policy_error)

    # Validate plugin exists in catalog
    plugin_error = _validate_plugin_name(context, "source", plugin)
    if plugin_error is not None:
        return _plugin_policy_failure(state, plugin_error)

    # Reject manual blob_ref injection.  The canonical write path for a
    # blob-backed source is set_source_from_blob, which forces the path to
    # the blob's authoritative storage_path.  set_source with a hand-crafted
    # blob_ref + path lets the caller persist a path that disagrees with the
    # blob's canonical storage_path, breaking runtime resolution and
    # composer/runtime agreement.
    manual_blob_ref_error = _reject_manual_source_blob_ref(options, tool_name="set_source")
    if manual_blob_ref_error is not None:
        return _failure_result(state, manual_blob_ref_error)
    manual_authoring_error = _reject_manual_source_authoring(options, tool_name="set_source")
    if manual_authoring_error is not None:
        return _failure_result(state, manual_authoring_error)
    manual_blobs_error = _reject_manual_source_blobs(options, tool_name="set_source")
    if manual_blobs_error is not None:
        return _failure_result(state, manual_blobs_error)
    credential_error = _credential_wiring_contract_failure(
        state,
        component_id=_source_component_id(source_name),
        component_type="source",
        plugin_type="source",
        plugin_name=plugin,
        options=options,
    )
    if credential_error is not None:
        return credential_error

    # S2: Validate source path allowlist
    path_error = _validate_source_path(
        options,
        context.data_dir,
        session_id=context.session_id,
        require_data_dir=context.require_data_dir_for_paths,
    )
    if path_error is not None:
        return _failure_result(state, path_error)

    # "" means no route and canonicalizes to 'discard' — one shared owner
    # (elspeth-bcd7051143), so this seam agrees with set_pipeline and the
    # auto-wire pass instead of deferring "" to the engine's plugin-config
    # rejection.
    on_vf = canonicalize_source_validation_failure(validated.on_validation_failure)
    prevalidation_error = _prevalidate_source_for_context(
        context,
        plugin,
        options,
        on_vf,
        source_name=source_name,
    )
    if prevalidation_error is not None:
        return _failure_result(state, prevalidation_error)

    source = SourceSpec(
        plugin=plugin,
        on_success=validated.on_success,
        options=options,
        on_validation_failure=on_vf,
        description=validated.description,
    )
    new_state = state.with_named_source(source_name, source)
    affected = (_source_component_id(source_name),)
    return _mutation_result(new_state, affected, data=_vf_destination_note(new_state, on_vf))


def _execute_set_source_from_blob(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Bind the pipeline source to an existing blob.

    Tier-3 boundary: ``arguments`` is an LLM-supplied dict.  Validated
    via :class:`SetSourceFromBlobArgumentsModel` (the single source of
    truth for the argument schema — supersedes the deleted
    ``_TOOL_REQUIRED_PATHS["set_source_from_blob"]`` entry in
    ``service.py``, rev-3 N7 / rev-4 M1).  On
    :class:`pydantic.ValidationError` we re-raise as
    :class:`ToolArgumentError` so the compose loop's ARG_ERROR routing
    at ``service.py:2480`` receives the right exception class.

    The prior in-handler ``isinstance(caller_options, dict)`` guard at
    this site is superseded by the Pydantic model's ``options: dict[str,
    Any]`` validation: a non-dict (or missing-required-fields) input now
    raises a structured ValidationError that the handler re-raises as
    ToolArgumentError before any blob-lookup work is done.

    Optional-field semantics (mirrors the JSON schema's `required`):
      * ``options`` defaults to ``{}`` (matches the prior
        ``arguments.get("options", {})``).
      * ``plugin`` and ``on_validation_failure`` remain ``str | None``
        so the handler can distinguish operator-omitted from
        operator-specified.  ``on_validation_failure`` (both ``None``
        and the unroutable ``""`` spelling) canonicalizes to "discard"
        via ``canonicalize_source_validation_failure`` at the seam
        below — the single owner shared by every source-authoring seam
        (elspeth-bcd7051143).
    """
    try:
        validated = SetSourceFromBlobArgumentsModel.model_validate(arguments)
    except PydanticValidationError as exc:
        raise ToolArgumentError(
            argument="set_source_from_blob arguments",
            expected="object conforming to SetSourceFromBlobArgumentsModel",
            actual_type=type(exc).__name__,
        ) from exc

    source_name = validated.source_name
    _validate_source_name_argument(source_name)

    # Caller options merge into the bound source's options, so a forged
    # "resolved" INVENTED_SOURCE requirement here would bypass human review even
    # though the blob path also re-stamps a pending requirement — guard at the
    # boundary rather than relying on that downstream overwrite.
    review_metadata_error = _resolver_owned_interpretation_requirement_error(
        validated.options,
        tool_name="set_source_from_blob",
        component_id=_source_component_id(source_name),
        source=True,
    )
    if review_metadata_error is not None:
        return _failure_result(
            state,
            review_metadata_error,
            error_code="interpretation_requirements_invalid",
        )
    caller_options = _canonicalize_authored_interpretation_requirements(
        validated.options,
        component_id=_source_component_id(source_name),
        source=True,
        existing_options=state.sources[source_name].options if source_name in state.sources else None,
    )
    endpoint_policy_error = web_aws_s3_endpoint_url_policy_error(validated.plugin, caller_options)
    if endpoint_policy_error is not None:
        return _failure_result(state, endpoint_policy_error)

    # None and "" both mean 'discard' — one shared owner (elspeth-bcd7051143).
    # The previous ``is not None`` seam preserved "" while set_pipeline
    # coerced it, so the same authored value diverged by tool.
    on_vf = canonicalize_source_validation_failure(validated.on_validation_failure)
    resolved = _resolve_source_blob(
        blob_id=validated.blob_id,
        explicit_plugin=validated.plugin,
        caller_options=caller_options,
        on_validation_failure=on_vf,
        state=state,
        context=context,
        session_engine=context.session_engine,
        session_id=context.session_id,
        tool_name="set_source_from_blob",
        source_name=source_name,
        existing_options=state.sources[source_name].options if source_name in state.sources else None,
    )
    if isinstance(resolved, ToolResult):
        return resolved

    endpoint_policy_error = web_aws_s3_endpoint_url_policy_error(resolved.plugin, resolved.options)
    if endpoint_policy_error is not None:
        return _failure_result(state, endpoint_policy_error)
    plugin_error = _validate_plugin_name(context, "source", resolved.plugin)
    if plugin_error is not None:
        return _plugin_policy_failure(state, plugin_error)

    source = SourceSpec(
        plugin=resolved.plugin,
        on_success=validated.on_success,
        options=resolved.options,
        on_validation_failure=on_vf,
    )
    new_state = state.with_named_source(source_name, source)
    data = _vf_destination_note(new_state, on_vf) or {}
    return _mutation_result(new_state, (_source_component_id(source_name),), data={**data, "source_blob": resolved.payload})


def _resolve_source_blobs(
    *,
    blob_ids: Sequence[str],
    caller_options: Mapping[str, Any],
    on_validation_failure: str,
    state: CompositionState,
    context: ToolContext,
    session_engine: Engine | None,
    session_id: str | None,
    source_name: str,
) -> tuple[Mapping[str, Any], tuple[SourceBlobPayload, ...]] | ToolResult:
    """Resolve a bounded blob-id list into authoritative blob_rows options.

    The PLURAL binding (elspeth-0c6a343921): every persisted ``blobs`` entry
    field comes from the session's authoritative blob record, never from LLM
    assertions, and the WHOLE list is validated before any composition
    mutation — one malformed, missing, duplicated, foreign, non-ready, or
    inconsistent member fails the complete call. Binary blobs are never
    UTF-8 decoded here; no content is read at all (run admission re-resolves
    the records and web execution stages the bytes for the payload store).
    """
    if session_engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    manual_authoring_error = _reject_manual_source_authoring(caller_options, tool_name="set_source_from_blobs")
    if manual_authoring_error is not None:
        return _failure_result(state, manual_authoring_error)
    if "blobs" in caller_options:
        return _failure_result(
            state,
            "set_source_from_blobs resolves the 'blobs' list from session records; do not author or edit it directly.",
        )
    seen_ids: set[str] = set()
    for blob_id in blob_ids:
        blob_id_error = _blob_id_uuid_validation_error(blob_id)
        if blob_id_error is not None:
            return _failure_result(state, blob_id_error)
        normalized = blob_id.lower()
        if normalized in seen_ids:
            return _failure_result(state, f"Duplicate blob id '{blob_id}' in blob_ids.")
        seen_ids.add(normalized)

    entries: list[dict[str, Any]] = []
    payloads: list[SourceBlobPayload] = []
    seen_hashes: dict[str, str] = {}
    for blob_id in blob_ids:
        blob = _sync_get_blob(session_engine, blob_id, session_id)
        if blob is None:
            return _failure_result(state, f"Blob '{blob_id}' not found.")
        if blob["status"] != "ready":
            return _failure_result(state, f"Blob '{blob_id}' is not ready (status: {blob['status']}).")
        content_hash_value = blob["content_hash"]
        if content_hash_value is None:
            return _failure_result(state, f"Blob '{blob_id}' has no canonical content hash.")
        if blob["mime_type"] not in STORAGE_MIME_TYPES:
            return _failure_result(state, f"Blob '{blob_id}' has unsupported MIME type '{blob['mime_type']}'.")
        creation_modality = CreationModality(blob["creation_modality"])
        if is_llm_authored_creation_modality(creation_modality):
            # Fail-closed v1 boundary (recorded design deviation): the
            # singular set_source_from_blob path owns the LLM-authored
            # review custody (it stamps source_authoring metadata and a
            # pending review over the decoded content). The plural path
            # serves the authenticated upload/paste flow, whose blobs are
            # always user-verbatim; admitting LLM-authored blobs here
            # without an equivalent review shape would bypass human review.
            return _failure_result(
                state,
                f"Blob '{blob_id}' is LLM-authored; bind it through set_source_from_blob so its interpretation review is staged.",
            )
        if content_hash_value in seen_hashes:
            return _failure_result(
                state,
                f"Blob '{blob_id}' has the same content as blob '{seen_hashes[content_hash_value]}'; entries must be unique documents.",
            )
        seen_hashes[content_hash_value] = blob["id"]
        entries.append(
            {
                "blob_id": blob["id"],
                "payload_ref": content_hash_value,
                "filename": blob["filename"],
                "mime_type": blob["mime_type"],
                "size_bytes": blob["size_bytes"],
            }
        )
        payloads.append(_source_blob_payload(blob))

    merged_options: dict[str, Any] = {**caller_options, "blobs": entries}
    if "schema" not in merged_options and "schema_config" not in merged_options:
        merged_options["schema"] = {"mode": "observed"}
    canonical_error = _canonical_interpretation_requirement_error(merged_options, tool_name="set_source_from_blobs")
    if canonical_error is not None:
        return _failure_result(state, canonical_error, error_code="interpretation_requirements_invalid")
    try:
        context.catalog.get_schema("source", "blob_rows")
    except (ValueError, KeyError) as exc:
        return _failure_result(state, f"Unknown source plugin 'blob_rows': {exc}")
    prevalidation_error = _prevalidate_source_for_context(
        context,
        "blob_rows",
        merged_options,
        on_validation_failure,
        source_name=source_name,
    )
    if prevalidation_error is not None:
        return _failure_result(state, prevalidation_error)
    return merged_options, tuple(payloads)


def _execute_set_source_from_blobs(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Bind one or more existing blobs to a blob_rows source, atomically.

    Tier-3 boundary: ``arguments`` is an LLM-supplied dict, validated via
    :class:`SetSourceFromBlobsArgumentsModel`; ValidationError re-raises as
    ToolArgumentError for the compose loop's ARG_ERROR routing (same
    discipline as the singular handler above).
    """
    try:
        validated = SetSourceFromBlobsArgumentsModel.model_validate(arguments)
    except PydanticValidationError as exc:
        raise ToolArgumentError(
            argument="set_source_from_blobs arguments",
            expected="object conforming to SetSourceFromBlobsArgumentsModel",
            actual_type=type(exc).__name__,
        ) from exc

    source_name = validated.source_name
    _validate_source_name_argument(source_name)

    review_metadata_error = _resolver_owned_interpretation_requirement_error(
        validated.options,
        tool_name="set_source_from_blobs",
        component_id=_source_component_id(source_name),
        source=True,
    )
    if review_metadata_error is not None:
        return _failure_result(state, review_metadata_error, error_code="interpretation_requirements_invalid")
    caller_options = _canonicalize_authored_interpretation_requirements(
        validated.options,
        component_id=_source_component_id(source_name),
        source=True,
        existing_options=state.sources[source_name].options if source_name in state.sources else None,
    )
    on_vf = canonicalize_source_validation_failure(validated.on_validation_failure)
    resolved = _resolve_source_blobs(
        blob_ids=validated.blob_ids,
        caller_options=caller_options,
        on_validation_failure=on_vf,
        state=state,
        context=context,
        session_engine=context.session_engine,
        session_id=context.session_id,
        source_name=source_name,
    )
    if isinstance(resolved, ToolResult):
        return resolved
    merged_options, payloads = resolved

    plugin_error = _validate_plugin_name(context, "source", "blob_rows")
    if plugin_error is not None:
        return _plugin_policy_failure(state, plugin_error)

    source = SourceSpec(
        plugin="blob_rows",
        on_success=validated.on_success,
        options=merged_options,
        on_validation_failure=on_vf,
    )
    new_state = state.with_named_source(source_name, source)
    data = _vf_destination_note(new_state, on_vf) or {}
    return _mutation_result(
        new_state,
        (_source_component_id(source_name),),
        data={**data, "source_blobs": list(payloads)},
    )


_SET_SOURCE_FROM_BLOBS_DECLARATION = ToolDeclaration(
    name="set_source_from_blobs",
    handler=_execute_set_source_from_blobs,
    kind=ToolKind.BLOB_MUTATION,
    description=(
        "Wire one or more READY session blobs as a blob_rows source: one blob becomes one row "
        "carrying its payload hash and bounded metadata, in the given order. Every entry field "
        "is resolved from the session's authoritative records — never pass document content. "
        "Use this for pasted/uploaded binary documents (jpeg/png/pdf) feeding "
        "aws_textract_inline_analysis; mixed formats need one homogeneous source per format."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "blob_ids": {
                "type": "array",
                "items": {"type": "string", "description": "Ready session blob ID."},
                "minItems": 1,
                "maxItems": 1000,
                "description": "Blob IDs in authoring order (becomes source-row order). No duplicates.",
            },
            "source_name": {
                "type": "string",
                "description": "Source root name to bind. Defaults to 'source' for legacy single-source pipelines.",
            },
            "on_success": {
                "type": "string",
                "description": (
                    "Connection-name string the source PUBLISHES. Some downstream consumer "
                    "(node 'input' or output 'sink_name') MUST equal this value."
                ),
                "examples": ["documents"],
            },
            "on_validation_failure": {
                "type": "string",
                "description": _SOURCE_VALIDATION_FAILURE_DESCRIPTION,
                "default": _DEFAULT_SOURCE_VALIDATION_FAILURE,
            },
            "options": {
                "type": "object",
                "description": ("Optional blob_rows config (e.g. schema). The 'blobs' list is resolver-owned and must not appear here."),
            },
        },
        "required": ["blob_ids", "on_success"],
        "additionalProperties": False,
    },
    blob_store_only=False,
    augments_on_failure=True,
)


_SET_SOURCE_FROM_BLOB_DECLARATION = ToolDeclaration(
    name="set_source_from_blob",
    handler=_execute_set_source_from_blob,
    kind=ToolKind.BLOB_MUTATION,
    description=(
        "Wire a blob as the pipeline source. Resolves the blob's storage path "
        "internally and infers the source plugin from its MIME type. "
        "Use 'options' for plugin-specific config (e.g., 'column' and 'schema' for text sources)."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "blob_id": {"type": "string", "description": "Blob ID to use as source."},
            "source_name": {
                "type": "string",
                "description": "Source root name to bind. Defaults to 'source' for legacy single-source pipelines.",
            },
            "plugin": {
                "type": "string",
                "description": "Source plugin override (e.g. 'csv'). Inferred from MIME type if omitted.",
            },
            "on_success": {
                "type": "string",
                "description": (
                    "Connection-name string the source PUBLISHES. Some downstream consumer "
                    "(node 'input' or output 'sink_name') MUST equal this value. Despite the "
                    "field name, this is NOT a node id — connections match by string, not by "
                    "topology."
                ),
                "examples": ["raw_url_rows", "csv_rows", "fetched_text"],
            },
            "on_validation_failure": {
                "type": "string",
                "description": _SOURCE_VALIDATION_FAILURE_DESCRIPTION,
                "default": _DEFAULT_SOURCE_VALIDATION_FAILURE,
            },
            "options": {
                "type": "object",
                "description": (
                    "Plugin-specific config (merged with blob path). Required fields vary by plugin: "
                    "text sources need 'column' (output field name) and 'schema' (e.g., {mode: 'observed'})."
                ),
            },
        },
        "required": ["blob_id", "on_success"],
        "additionalProperties": False,
    },
    blob_store_only=False,
    augments_on_failure=True,
)


def _first_nonempty_csv_row(content: str) -> tuple[str, ...] | None:
    """Return the first non-empty CSV row, if any."""
    for row in _bounded_csv_rows(content):
        if any(cell.strip() for cell in row):
            return tuple(row)
    return None


def _first_nonempty_csv_row_from_path(path: Path) -> tuple[str, ...] | None:
    """Return a candidate CSV header from a bounded file prefix.

    Reads one byte past ``_INLINE_CSV_HEADER_READ_BYTES`` solely to tell apart
    two situations that both start out looking like a parse failure:

    * The file genuinely ends within the window and the parser still can't
      complete a record — real corruption, which the caller escalates.
    * The file continues past the window and its first record (typically a
      long quoted cell) simply straddles the boundary, so the truncated
      prefix ends mid-record. This is expected truncation of an otherwise
      well-formed CSV, not corruption, so the header is reported as
      undeterminable (``None``) rather than as a parse failure.

    Structural limit violations (oversized cell/row, embedded NUL) are never
    swallowed here regardless of truncation: a cell that already exceeds the
    limit within the visible prefix only gets longer with more data, so it is
    a genuine finding either way.

    The same window-edge-truncation reasoning applies to decoding: a
    multi-byte UTF-8 character straddling the window boundary is not
    corruption either, so the trailing partial sequence is decoded with
    ``final=not truncated_by_window`` — an incremental decoder buffers (and
    silently drops) a dangling partial sequence at the end of input rather
    than raising, but still raises immediately on bytes that are invalid
    UTF-8 regardless of what follows, and still raises on any incomplete
    sequence when the file's real EOF was reached (``final=True``).
    """
    with path.open("rb") as handle:
        raw = handle.read(_INLINE_CSV_HEADER_READ_BYTES + 1)
    truncated_by_window = len(raw) > _INLINE_CSV_HEADER_READ_BYTES
    content = raw[:_INLINE_CSV_HEADER_READ_BYTES]
    decoder = codecs.getincrementaldecoder("utf-8")()
    text = decoder.decode(content, final=not truncated_by_window)
    try:
        return _first_nonempty_csv_row(text)
    except _CsvParseIncompleteError:
        if truncated_by_window:
            return None
        raise


def _is_header_only_csv(content: str) -> tuple[str, ...] | None:
    """Return the sole CSV row when content is header-only, otherwise None."""
    header: tuple[str, ...] | None = None
    for row in _bounded_csv_rows(content):
        if not any(cell.strip() for cell in row):
            continue
        if header is not None:
            return None
        header = tuple(row)
    return header


def _read_inspection_prefix_and_hash(path: Path) -> tuple[bytes, str]:
    """Read the bounded inspection prefix while hashing the full file."""
    digest = hashlib.sha256()
    prefix = bytearray()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_BLOB_HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            remaining = _INSPECT_SOURCE_MAX_BYTES - len(prefix)
            if remaining > 0:
                prefix.extend(chunk[:remaining])
    return bytes(prefix), digest.hexdigest()


def _header_only_inline_csv_conflict(
    prepared: _PreparedBlobCreate,
    *,
    session_engine: Engine,
    session_id: str,
) -> str | None:
    """Reject schema-only CSV blobs when a matching uploaded CSV is ready."""
    if prepared.mime_type != "text/csv":
        return None
    try:
        header = _is_header_only_csv(prepared.content_bytes.decode("utf-8"))
    except _CsvContentBoundaryError:
        return "Refusing inline CSV because it exceeds bounded CSV inspection limits."
    if header is None:
        return None

    with session_engine.connect() as conn:
        rows = conn.execute(
            select(blobs_table)
            .where(
                blobs_table.c.session_id == session_id,
                blobs_table.c.mime_type == "text/csv",
                blobs_table.c.status == "ready",
                blobs_table.c.created_by == "user",
                blobs_table.c.size_bytes > len(prepared.content_bytes),
            )
            .limit(_INLINE_CSV_CANDIDATE_SCAN_LIMIT + 1)
        ).fetchall()

    if len(rows) > _INLINE_CSV_CANDIDATE_SCAN_LIMIT:
        return (
            "Refusing header-only inline CSV because the ready uploaded CSV candidate scan limit was exceeded. "
            "Bind the intended uploaded file with source.blob_id or call list_blobs then set_source_from_blob."
        )

    matches: list[BlobToolRecord] = []
    for row in rows:
        blob = _blob_row_to_tool_dict(row)
        try:
            candidate_header = _first_nonempty_csv_row_from_path(Path(blob["storage_path"]))
        except UnicodeDecodeError as exc:
            raise AuditIntegrityError(
                f"Ready uploaded blob '{blob['id']}' storage_path could not be decoded during set_pipeline inline CSV custody check"
            ) from exc
        except OSError as exc:
            raise AuditIntegrityError(
                f"Ready uploaded blob '{blob['id']}' storage_path could not be read during set_pipeline inline CSV custody check"
            ) from exc
        except _CsvContentBoundaryError as exc:
            raise AuditIntegrityError(
                f"Ready uploaded blob '{blob['id']}' could not be parsed within bounded CSV inspection limits"
            ) from exc
        if candidate_header == header:
            matches.append(blob)

    if not matches:
        return None

    choices = ", ".join(f"{blob['filename']} ({blob['id']}, {blob['size_bytes']} bytes)" for blob in matches)
    return (
        "Refusing header-only inline CSV for set_pipeline because ready uploaded CSV blob(s) "
        f"with matching headers already exist in this session: {choices}. "
        "Bind the uploaded file with source.blob_id or call list_blobs then set_source_from_blob."
    )


def _execute_inspect_source(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Inspect a blob-backed source and return bounded structural facts.

    Mirrors the lifecycle and integrity guards of ``_execute_get_blob_content``
    (only ``ready`` blobs are readable; SHA-256 verified; UnicodeDecodeError
    surfaced as tool-failure) but returns ``SourceInspectionFacts`` rather
    than raw content. Reads at most 8 KiB and parses at most 100 rows.

    Never returns raw row content — only summary facts (headers, inferred
    types, URL candidates, warnings, redacted identity).
    """
    if context.session_engine is None or context.session_id is None:
        return _failure_result(state, "Blob tools require session context.")

    try:
        validated = InspectSourceArgumentsModel.model_validate(arguments)
    except PydanticValidationError as exc:
        raise ToolArgumentError(
            argument="inspect_source arguments",
            expected="object conforming to InspectSourceArgumentsModel",
            actual_type=type(exc).__name__,
        ) from exc

    blob_id = validated.blob_id
    blob_id_error = _blob_id_uuid_validation_error(blob_id)
    if blob_id_error is not None:
        return _failure_result(state, blob_id_error)
    blob = _sync_get_blob(context.session_engine, blob_id, context.session_id)
    if blob is None:
        return _failure_result(state, f"Blob '{blob_id}' not found.")

    blob_status = blob["status"]
    if blob_status != "ready":
        return _failure_result(
            state,
            f"Blob '{blob_id}' is not readable — status is '{blob_status}', expected 'ready'.",
        )

    storage_path = Path(blob["storage_path"])
    if not storage_path.exists():
        return _failure_result(state, f"Blob storage file missing for '{blob_id}'.")

    data, actual_hash = _read_inspection_prefix_and_hash(storage_path)
    _verify_blob_content_hash(blob, actual_hash)

    facts = inspect_blob_content(
        content=data,
        filename=blob["filename"],
        mime_type=blob["mime_type"],
        blob_id=UUID(blob_id),
        content_hash=blob["content_hash"],
        total_size_bytes=blob["size_bytes"],
    )
    return _discovery_result(state, facts_to_dict(facts))


_INSPECT_SOURCE_DECLARATION = ToolDeclaration(
    name="inspect_source",
    handler=_execute_inspect_source,
    kind=ToolKind.BLOB_DISCOVERY,
    description=(
        "Return bounded structural facts about a blob-backed source: source kind, observed "
        "headers, sample row count, inferred scalar types per column, URL candidates, and "
        "warnings. Reads at most 8 KiB of the blob and parses at most 100 rows. Use this "
        "before declaring a fixed CSV/JSON schema — observed headers and inferred types "
        "tell you which fields the source actually contains and what numeric coercion is "
        "needed before any gate or value_transform numeric op. Never returns raw row "
        "content; only summary facts."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "blob_id": {
                "type": "string",
                "description": "ID of the blob to inspect.",
            },
        },
        "required": ["blob_id"],
        "additionalProperties": False,
    },
)


def _execute_patch_source_options(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Apply a merge-patch to the current source options.

    Tier-3 boundary: ``args`` is an LLM-supplied dict.  Validated via the
    Pydantic redaction-bearing model :class:`PatchSourceOptionsArgumentsModel`
    (the single source of truth for the argument schema — supersedes the
    deleted ``_TOOL_REQUIRED_PATHS["patch_source_options"]`` entry in
    ``service.py``, rev-3 N7 / rev-4 M1).

    On :class:`pydantic.ValidationError` the handler re-raises as
    :class:`ToolArgumentError` so the compose loop's ARG_ERROR routing at
    ``service.py:2480`` receives the right exception class.  A bare
    ``ValidationError`` would escape into the catch-all
    (``ComposerPluginCrashError`` → HTTP 500) — wrong disposition for
    Tier-3 input.
    """
    try:
        validated = PatchSourceOptionsArgumentsModel.model_validate(args)
    except PydanticValidationError as exc:
        raise ToolArgumentError(
            argument="patch_source_options arguments",
            expected="object conforming to PatchSourceOptionsArgumentsModel",
            actual_type=type(exc).__name__,
        ) from exc
    source_name = validated.source_name
    if source_name not in state.sources:
        return _failure_result(state, f"No source named '{source_name}' configured to patch.")
    current_source = state.sources[source_name]
    patch: Mapping[str, Any] = validated.patch

    manual_authoring_error = _reject_manual_source_authoring(patch, tool_name="patch_source_options")
    if manual_authoring_error is not None:
        return _failure_result(state, manual_authoring_error)
    manual_blobs_error = _reject_manual_source_blobs(patch, tool_name="patch_source_options")
    if manual_blobs_error is not None:
        return _failure_result(state, manual_blobs_error)
    # Check the LLM-supplied PATCH delta (not the merged result): a patch that
    # carries a forged "resolved" INVENTED_SOURCE requirement is the live review
    # bypass vector. Checking the delta — mirroring patch_node_options — leaves a
    # legitimately-resolved requirement already in stored options untouched.
    review_metadata_error = _resolver_owned_interpretation_requirement_error(
        patch,
        tool_name="patch_source_options",
        component_id=_source_component_id(source_name),
        source=True,
    )
    if review_metadata_error is not None:
        return _failure_result(
            state,
            review_metadata_error,
            error_code="interpretation_requirements_invalid",
        )
    patch = _canonicalize_authored_interpretation_requirements(
        patch,
        component_id=_source_component_id(source_name),
        source=True,
        existing_options=current_source.options,
    )
    if "blob_ref" in patch:
        return _failure_result(
            state,
            "Cannot patch 'blob_ref' on a source. Re-bind via set_source_from_blob "
            "(or call clear_source first) to change the underlying blob.",
        )

    # Lock the (path, blob_ref) pair on blob-backed sources.  Once
    # set_source_from_blob has bound a source to a blob, the path is the
    # blob's canonical storage_path and is not patchable: any divergence
    # breaks runtime path resolution and composer/runtime agreement.
    # Replace the binding via a fresh set_source_from_blob (or
    # clear_source) instead of patching it.
    if "blob_ref" in current_source.options:
        forbidden_keys = {"path"} & patch.keys()
        if forbidden_keys:
            return _failure_result(
                state,
                "Cannot patch "
                f"{sorted(forbidden_keys)} on a blob-backed source. "
                "The 'path' is bound to the referenced blob's canonical "
                "storage_path. Re-bind via set_source_from_blob (or call "
                "clear_source first) to change the underlying blob.",
            )

    new_options = _apply_merge_patch(current_source.options, dict(patch))
    canonical_error = _canonical_interpretation_requirement_error(
        new_options,
        tool_name="patch_source_options",
    )
    if canonical_error is not None:
        return _failure_result(
            state,
            canonical_error,
            error_code="interpretation_requirements_invalid",
        )
    endpoint_policy_error = web_aws_s3_endpoint_url_policy_error(current_source.plugin, new_options)
    if endpoint_policy_error is not None:
        return _failure_result(state, endpoint_policy_error)
    credential_error = _credential_wiring_contract_failure(
        state,
        component_id=_source_component_id(source_name),
        component_type="source",
        plugin_type="source",
        plugin_name=current_source.plugin,
        options=new_options,
    )
    if credential_error is not None:
        return credential_error

    # S2: Validate patched source paths against allowlist
    path_error = _validate_source_path(
        new_options,
        context.data_dir,
        session_id=context.session_id,
        require_data_dir=context.require_data_dir_for_paths,
    )
    if path_error is not None:
        return _failure_result(state, path_error)

    # Pre-validate patched options against config model
    prevalidation_error = _prevalidate_source_for_context(
        context,
        current_source.plugin,
        new_options,
        current_source.on_validation_failure,
        source_name=source_name,
    )
    if prevalidation_error is not None:
        return _failure_result(state, prevalidation_error)

    new_source = replace(current_source, options=new_options)
    new_state = state.with_named_source(source_name, new_source)
    return _mutation_result(new_state, (_source_component_id(source_name),))


def _handle_patch_source_options(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    result = _execute_patch_source_options(arguments, state, context)
    if not result.success:
        return result
    source_name = PatchSourceOptionsArgumentsModel.model_validate(arguments).source_name
    if source_name not in result.updated_state.sources:
        return result
    source = result.updated_state.sources[source_name]
    return _attach_post_call_hints(
        result,
        context.catalog,
        plugin_type="source",
        tool_name="patch_source_options",
        plugin_name=source.plugin,
        config_snapshot=source.options,
    )


_PATCH_SOURCE_OPTIONS_DECLARATION = ToolDeclaration(
    name="patch_source_options",
    handler=_handle_patch_source_options,
    kind=ToolKind.MUTATION,
    description="Apply a shallow merge-patch to a named source's options. "
    "Keys in the patch overwrite existing keys. "
    "Keys set to null are deleted. Missing keys are unchanged.",
    json_schema={
        "type": "object",
        "properties": {
            "source_name": {
                "type": "string",
                "description": "Source root name to patch. Defaults to 'source'.",
            },
            "patch": {
                "type": "object",
                "description": "Merge-patch to apply to source options.",
            },
        },
        "required": ["patch"],
        "additionalProperties": False,
    },
    augments_on_failure=True,
)


def _execute_clear_source(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Remove the pipeline source."""
    del context  # unused; signature uniformity with the other handlers.
    extra_keys = set(args) - {"source_name"}
    if extra_keys:
        raise ToolArgumentError(
            argument="clear_source arguments",
            expected="only the optional 'source_name' key",
            actual_type="unexpected extra keys",
        )
    source_name = args["source_name"] if "source_name" in args else "source"
    if type(source_name) is not str or not source_name:
        raise ToolArgumentError(
            argument="source_name",
            expected="a non-empty string",
            actual_type=type(source_name).__name__,
        )
    new_state = state.without_named_source(source_name)
    if new_state is None:
        return _failure_result(state, f"No source named '{source_name}' configured to clear.")
    return _mutation_result(new_state, (_source_component_id(source_name),))


def _handle_clear_source(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    return _execute_clear_source(arguments, state, context)


_CLEAR_SOURCE_DECLARATION = ToolDeclaration(
    name="clear_source",
    handler=_handle_clear_source,
    kind=ToolKind.MUTATION,
    description="Remove a named source from the pipeline composition state.",
    json_schema={
        "type": "object",
        "properties": {
            "source_name": {
                "type": "string",
                "description": "Source root name to clear. Defaults to 'source'.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
)


TOOLS_IN_MODULE: tuple[ToolDeclaration, ...] = (
    _LIST_SOURCES_DECLARATION,
    _SET_SOURCE_DECLARATION,
    _PATCH_SOURCE_OPTIONS_DECLARATION,
    _CLEAR_SOURCE_DECLARATION,
    _SET_SOURCE_FROM_BLOB_DECLARATION,
    _SET_SOURCE_FROM_BLOBS_DECLARATION,
    _INSPECT_SOURCE_DECLARATION,
)
"""Every tool declared in this module, in stable order.

``_dispatch.py`` aggregates this tuple alongside every other plane's
TOOLS_IN_MODULE to build the registered-tool universe."""
