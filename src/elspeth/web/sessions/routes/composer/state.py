from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from typing import Annotated, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from elspeth.contracts.blobs import BlobRecord
from elspeth.contracts.blobs_inline import BlobInlineRef, ContentEncoding
from elspeth.contracts.composer_interpretation import InterpretationSurfaceOrigin
from elspeth.contracts.enums import is_llm_authored_creation_modality
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.secrets import collect_credential_field_violations
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.blobs.protocol import BlobNotFoundError, BlobServiceProtocol
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import PluginKind
from elspeth.web.compartments import compartment_ingress_record, compartment_marking_header
from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.state import CompositionState, SourceSpec
from elspeth.web.composer.tools.blobs import (
    _apply_inline_blob_marker,
    _file_inject_blob_binding_error,
    _llm_authored_prompt_surface_error,
)
from elspeth.web.composer.yaml_generator import (
    PUBLIC_EXPORT_REBIND_GUIDANCE,
    PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX,
    public_export_redaction,
    public_export_redaction_header,
)
from elspeth.web.composer.yaml_importer import (
    MAX_RUNTIME_YAML_IMPORT_CHARS,
    RuntimeYamlImportError,
    composition_state_from_runtime_yaml,
)
from elspeth.web.coordination.composer_progress_authority import (
    ComposerProgressIdentityInactive,
    ComposerProgressSessionUnavailable,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.credential_guard import require_no_credential_material_in_state
from elspeth.web.execution._validation_materialization import file_inject_state_refusals
from elspeth.web.interpretation_state import (
    InterpretationReviewCapacityError,
    InterpretationReviewIntegrityError,
    InterpretationReviewSite,
    parse_interpretation_requirements,
)
from elspeth.web.paths import (
    MAX_SESSION_RELATIVE_PATH_CHARS,
    SINK_LOCAL_PATH_OPTION_KEYS,
    SOURCE_LOCAL_PATH_OPTION_KEYS,
    SessionFolder,
    allowed_sink_directories,
    allowed_source_directories,
    managed_blob_directory,
    normalise_session_relative_path,
    resolve_data_path,
    resolve_sink_data_path,
    session_folder_path,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.secrets.ref_policy import allowed_secret_ref_fields
from elspeth.web.sessions.operation_receipts import operation_receipt_response_hash
from elspeth.web.sessions.protocol import (
    OperationReceiptResult,
    OperationReceiptSettlementConflictError,
    SessionRecord,
    StateRevertReceiptResult,
)
from elspeth.web.sessions.routes.operation_receipts import (
    OperationReceiptExpired,
    OperationReceiptLease,
    operation_receipt_lease_guard,
    raise_operation_receipt_failure,
    reserve_or_replay_operation_receipt,
)
from elspeth.web.sessions.schemas import CompositionHeadResponse
from elspeth.web.validation import reject_credential_material

from .._helpers import (
    UTC,
    UUID,
    Any,
    APIRouter,
    ComposerPreferencesResponse,
    ComposerProgressSnapshot,
    CompositionStateResponse,
    Depends,
    GraphValidationError,
    HTTPException,
    Mapping,
    PluginConfigError,
    PluginNotFoundError,
    Query,
    Request,
    RevertStateRequest,
    SessionServiceProtocol,
    SessionsTelemetry,
    UpdateComposerPreferencesRequest,
    UserIdentity,
    _composer_preferences_response,
    _get_composer_progress_registry,
    _get_session_compose_lock_registry,
    _log_last_resort_diagnostic,
    _record_composer_runtime_preflight_telemetry,
    _request_plugin_policy_context,
    _runtime_preflight_for_state,
    _state_data_from_composer_state,
    _state_from_record,
    _state_response,
    _validation_entry_responses,
    _verify_session_ownership,
    datetime,
    generate_public_yaml,
    merge_composer_meta_updates,
    record_session_completed,
    record_session_switched,
    require_pipeline_user,
    slog,
)

router = APIRouter()


class LibraryForkFacts(TypedDict):
    entry_id: str
    payload_digest: str
    published_from_session_id: str | None
    compartment_id: str
    version: int


class LibraryForkMetaUpdates(TypedDict):
    library_fork: LibraryForkFacts


class SharedImportFile(TypedDict):
    sha256: str
    blob_id: str


class SharedImportFacts(TypedDict):
    payload_digest: str
    shared_by_user_id: str
    shared_by_username: str
    shared_from_session_id: str
    shared_from_state_id: str
    files: list[SharedImportFile]


class SharedImportMetaUpdates(TypedDict):
    shared_import: SharedImportFacts


ComposerSeedMetaUpdates = LibraryForkMetaUpdates | SharedImportMetaUpdates


async def _surface_reverted_interpretation_reviews(
    service: SessionServiceProtocol,
    *,
    session_id: UUID,
    state_record: Any,
) -> None:
    """Repair review-card debt for a fresh or replayed revert result."""
    # Historical minimal service-authored checkpoints can predate populated
    # metadata and therefore cannot be promoted to CompositionState. They also
    # predate structured interpretation requirements; preserve their existing
    # revert compatibility instead of turning this additive repair into a 500.
    if state_record.metadata_ is None:
        return
    from elspeth.web.composer.interpretation_surfacing import (
        has_pending_interpretation_review_surfacing_debt,
        surface_pending_interpretation_reviews_for_state,
    )

    state = _state_from_record(state_record)
    if not await has_pending_interpretation_review_surfacing_debt(
        state,
        sessions_service=service,
        session_id=str(session_id),
        current_state_id=str(state_record.id),
    ):
        return

    # The replay joiner released the operation's session lease before this
    # post-verification repair runs, and the settling caller's receipt lease
    # guard has already closed by the time it reaches here, so the repair
    # writes hold their own short COMPOSE authority (fenced by analogy with
    # the settling attempt).
    async with await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session_id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    ) as repair_lease:
        await surface_pending_interpretation_reviews_for_state(
            state,
            sessions_service=service,
            session_id=str(session_id),
            current_state_id=str(state_record.id),
            # The revert consults no LLM: the origin names the route and the
            # LLM provenance stays empty rather than carrying a label.
            surface_origin=InterpretationSurfaceOrigin.STATE_REVERT,
            model_identifier=None,
            model_version=None,
            provider=None,
            composer_skill_hash=None,
            only_missing_evidence=True,
            session_operation_context=repair_lease.context,
        )


def _composition_plugin_policy_findings(
    state: CompositionState,
    catalog: PolicyCatalogView,
) -> list[tuple[str, str, PluginUnavailableReason]]:
    """Return closed current-policy findings without fetching plugin schemas."""
    components: list[tuple[str, PluginKind, str]] = [
        *((name, "source", source.plugin) for name, source in state.sources.items()),
        *((node.id, "transform", node.plugin) for node in state.nodes if node.plugin is not None),
        *((output.name, "sink", output.plugin) for output in state.outputs),
    ]
    findings: list[tuple[str, str, PluginUnavailableReason]] = []
    for component_id, kind, plugin_name in components:
        try:
            plugin_id = PluginId(kind, plugin_name)
        except ValueError:
            findings.append((component_id, f"{kind}:{plugin_name}", PluginUnavailableReason.NOT_INSTALLED))
            continue
        reason = catalog.unavailable_reason(plugin_id)
        if reason is not None:
            findings.append((component_id, str(plugin_id), reason))
    return findings


def _reject_imported_plugin_policy(
    state: CompositionState,
    catalog: PolicyCatalogView,
    snapshot: PluginAvailabilitySnapshot,
) -> None:
    findings = _composition_plugin_policy_findings(state, catalog)
    if not findings:
        return
    component_id, plugin_id, reason = findings[0]
    raise HTTPException(
        status_code=422,
        detail={
            "error_code": reason.value,
            "component_id": component_id,
            "plugin_id": plugin_id,
            "snapshot_fingerprint": snapshot.snapshot_hash,
        },
    )


class StateYamlRedaction(TypedDict):
    """Structured account of the public export's custody redaction.

    Mirrors the YAML header comment (``public_export_redaction_header``):
    the export is deliberately redacted — this names exactly what was
    stripped and how to re-bind it, instead of presenting the not-runnable
    YAML bare (elspeth-06f92da0d9). Names only, never blob UUIDs: the
    scrub ruling (2304d57fb, ``test_yaml_response_omits_source_blob_``
    ``identity_sidecar``) forbids blob identity beside the scrubbed YAML,
    which is why ``blob_linked_sources`` carries the sources whose
    verified blob linkage was stripped rather than the ids themselves.
    ``stripped_node_options`` names, per node id, the option paths of the
    uploaded-file markers the projection removed from that node.
    """

    stripped_source_options: dict[str, list[str]]
    stripped_node_options: dict[str, list[str]]
    stripped_output_options: dict[str, list[str]]
    blob_linked_sources: list[str]
    rebind_guidance: str


class StateYamlResponse(TypedDict):
    yaml: str
    # Omitted when the public projection stripped nothing.
    redaction: NotRequired[StateYamlRedaction]


class InlineBlobBindingRequest(BaseModel):
    """One file to re-attach as an inline-content marker at a field path."""

    model_config = ConfigDict(extra="forbid")

    blob_id: str
    encoding: ContentEncoding = "utf-8"


class SourceBlobRowsBindingRequest(BaseModel):
    """The files of one ``blob_rows`` source, in row order."""

    model_config = ConfigDict(extra="forbid")

    blob_ids: list[str] = Field(min_length=1)


class SinkPathBindingRequest(BaseModel):
    """A sink path to author below one of the target session's own folders."""

    model_config = ConfigDict(extra="forbid")

    folder: SessionFolder
    relative_path: str = Field(max_length=MAX_SESSION_RELATIVE_PATH_CHARS)

    # The bound path is written into the state after the imported YAML's
    # credential guard has run, so it needs its own.
    @field_validator("relative_path")
    @classmethod
    def _reject_credential_relative_path(cls, value: str) -> str:
        reject_credential_material(value)
        return value


class ImportStateYamlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    yaml: str = Field(min_length=1, max_length=MAX_RUNTIME_YAML_IMPORT_CHARS)
    source_blob_ids: dict[str, str] | None = None
    # Key: canonical inline-ref field path (``source:<name>.options.<f>...``,
    # ``node:<id>.options...`` or ``output:<name>.options...``).
    inline_blob_bindings: dict[str, InlineBlobBindingRequest] | None = None
    # Key: source name.
    source_blob_rows: dict[str, SourceBlobRowsBindingRequest] | None = None
    # Key: output name -> sink path option key (``path``/``file``/``persist_directory``).
    sink_path_bindings: dict[str, dict[str, SinkPathBindingRequest]] | None = None


class SeedCompositionStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: dict[str, Any]


@trust_boundary(
    tier=3,
    source="web-authored source options from a pasted/seeded composition state",
    source_param="options",
    suppresses=("R1", "R5"),
    invariant=(
        "missing or non-string path values are skipped (False); a hostile string value "
        "raises ValueError from path resolution rather than being coerced"
    ),
    test_ref="tests/unit/web/sessions/routes/composer/test_state_boundaries.py::test_source_options_reference_blob_storage_raises_on_nul_byte_path",
    test_fingerprint="b8fe8c2cfa05ee4a2ab7637e8c981a22226fee2b38657b770c649d13e96310ac",
)
def _source_options_reference_blob_storage(options: Mapping[str, Any], *, data_dir: str) -> bool:
    blob_root = managed_blob_directory(data_dir)
    for key in SOURCE_LOCAL_PATH_OPTION_KEYS:
        value = options.get(key)
        if not isinstance(value, str):
            continue
        resolved = resolve_data_path(value, data_dir)
        if resolved.is_relative_to(blob_root):
            return True
    return False


# Exporter-authored marker line (see yaml_generator's
# PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX — the prefix bytes are the
# shared authority). Source names are runtime-valid component ids, so the
# bounded \S{1,64} capture admits every legitimate name and nothing
# multi-token; anything else in the pasted text is ignored.
_REDACTED_SOURCE_MARKER_RE = re.compile(
    "^" + re.escape(PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX) + r"(\S{1,64}) stripped=",
    re.MULTILINE,
)


def _redaction_marker_source_names(yaml_text: str) -> frozenset[str]:
    """Source names the pasted document's own export marker declares redacted.

    Tier-3 parse of untrusted pasted text, scoped to the exact marker lines
    ELSPETH's public exporter emits. A document with no marker (hand-written
    YAML, hand-stripped marker) yields the empty set and the import falls
    back to the pre-existing behaviour: the strict preflight refuses the
    degraded state and it persists ``is_valid=False``.
    """
    return frozenset(_REDACTED_SOURCE_MARKER_RE.findall(yaml_text))


def _reject_redacted_sources_without_rebind(state: CompositionState, *, yaml_text: str) -> None:
    """400 the path-ABSENT redacted-export shape with re-bind guidance.

    The public export deliberately strips source path/blob linkage
    (elspeth-06f92da0d9). Re-importing that shape without ``source_blob_ids``
    used to sail through Stage-1 and strand the user with a raw Pydantic
    "path Field required" from the strict lane; the export's own marker lets
    this guard name the real remedy instead. Runs AFTER
    ``_state_with_imported_source_blobs`` and
    ``_state_with_imported_source_blob_rows`` so a supplied re-bind counts
    (a ``blob_rows`` source's re-bound ``blobs`` list included), and accepts a
    hand-re-added path option (the path guards downstream still police its
    value).
    """
    marked = _redaction_marker_source_names(yaml_text)
    if not marked:
        return
    for source_name, source in state.sources.items():
        if source_name not in marked:
            continue
        if "blob_ref" in source.options:
            continue
        if any(key in source.options for key in SOURCE_LOCAL_PATH_OPTION_KEYS):
            continue
        if source.plugin == "blob_rows" and "blobs" in source.options:
            # Re-bound through source_blob_rows.
            continue
        raise HTTPException(
            status_code=400,
            detail=(
                f"Source '{source_name}' was custody-redacted at export: its local path / blob linkage was "
                "stripped, so this YAML is not runnable as-is. Upload the source data to this session and "
                f'include source_blob_ids={{"{source_name}": "<blob id>"}} with the import, '
                "or re-add an allowed path option to the source."
            ),
        )


@trust_boundary(
    tier=3,
    source="pasted/seeded CompositionState carrying web-authored source options",
    source_param="state",
    suppresses=("R1",),
    invariant=(
        "raises HTTPException 400 for any source whose options reference managed blob storage "
        "without a blob_ref binding; blob_ref-bound and non-blob sources pass"
    ),
    test_ref="tests/unit/web/sessions/routes/composer/test_state_boundaries.py::test_reject_unbound_blob_storage_sources_raises_400_on_unbound_blob_path",
    test_fingerprint="36cb5dc337b093240d7028a644ba3088509f3b4760f6f694a78a59024afc995f",
)
def _reject_unbound_blob_storage_sources(state: CompositionState, *, data_dir: str) -> None:
    for source_name, source in state.sources.items():
        if source.options.get("blob_ref") is not None:
            continue
        if _source_options_reference_blob_storage(source.options, data_dir=data_dir):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Source '{source_name}' points at session blob storage but has no source_blob_ids entry. "
                    "Upload the blob into this session and include source_blob_ids for replay imports."
                ),
            )


@trust_boundary(
    tier=3,
    source="pasted/seeded CompositionState carrying web-authored source options",
    source_param="state",
    suppresses=("R1", "R5"),
    invariant=(
        "raises HTTPException 400 for any string source path outside the source allowlist; missing or non-string path values are skipped"
    ),
    test_ref=(
        "tests/unit/web/sessions/routes/composer/test_state_boundaries.py::test_reject_disallowed_source_paths_raises_400_outside_allowlist"
    ),
    test_fingerprint="2a73310e1ab04480348b8533732615a322b9d47cdfc3a2ca634947af3d8e4b4f",
)
def _reject_disallowed_source_paths(state: CompositionState, *, data_dir: str, session_id: str) -> None:
    allowed_dirs = allowed_source_directories(data_dir, session_id=session_id)
    for source_name, source in state.sources.items():
        for key in SOURCE_LOCAL_PATH_OPTION_KEYS:
            value = source.options.get(key)
            if not isinstance(value, str):
                continue
            resolved = resolve_data_path(value, data_dir)
            if not any(resolved.is_relative_to(directory) for directory in allowed_dirs):
                raise HTTPException(
                    status_code=400,
                    detail=f"Path traversal blocked: source '{source_name}' {key}='{value}' resolves outside allowed directories",
                )


def _reject_fabricated_secret_literals(
    state: CompositionState,
    *,
    secret_service: Any | None,
    user_id: str,
) -> None:
    """Reject literal credential values in pasted YAML before persistence.

    The composer's tool-call surface (``set_source``, ``patch_node_options``,
    ``set_output``) already rejects a fabricated ("typed the value directly
    instead of wiring a secret_ref") credential *before* persistence, via
    ``_credential_wiring_contract_failure`` in ``composer/tools/_common.py``:
    the mutation handler returns the unchanged state, so the literal is never
    written into CompositionState. Pasted YAML bypasses those handlers
    entirely (it reconstructs the state directly from the parsed document),
    so it has no equivalent gate -- without this check a literal credential
    would sail straight through ``_state_data_from_composer_state``'s
    ``persist_invalid`` policy: written to the DB as plaintext and echoed back
    verbatim in this response's ``sources``/``nodes``/``outputs`` fields,
    regardless of whether the state is later flagged ``is_valid=False``.

    Checking (and rejecting outright, before any persistence) here is
    stricter than the tool-call path affords today -- deliberately so, since
    this is a new, paste-facing entry point. It reuses the same field-name
    predicate and the same audit-hygiene discipline (name the field, never
    the value) as the runtime-preflight ``fabricated_secret`` check in
    ``elspeth.web.execution.validation``, so a legitimately wired
    ``{secret_ref: NAME}`` marker or declared ``${NAME}`` env-inventory
    marker is never mistaken for a fabricated literal.

    Beyond the heuristic name/suffix predicate, each component also feeds its
    plugin-specific credential fields (via ``allowed_secret_ref_fields`` --
    the database sink's whole-DSN ``url`` being the canonical case) so a
    pasted DSN with an embedded password is rejected here exactly as the
    ``set_source``/``set_output`` tool gate rejects it, rather than slipping
    past the suffix predicate and persisting plaintext.
    """
    env_ref_names: frozenset[str] = frozenset()
    if secret_service is not None:
        env_ref_names = frozenset(item.name for item in secret_service.list_refs(user_id))

    violations: dict[str, list[str]] = {}
    for source_name, source in state.sources.items():
        fields = collect_credential_field_violations(
            source.options,
            env_ref_names,
            additional_credential_fields=allowed_secret_ref_fields("source", source.plugin),
        )
        if fields:
            violations[f"source:{source_name}"] = fields
    for node in state.nodes:
        node_plugin_fields = allowed_secret_ref_fields("transform", node.plugin) if node.plugin is not None else frozenset()
        from elspeth.web.credential_guard import field_mapper_credential_options_projection

        fields = collect_credential_field_violations(
            field_mapper_credential_options_projection(node.options)
            if node.node_type == "transform" and node.plugin == "field_mapper"
            else node.options,
            env_ref_names,
            additional_credential_fields=node_plugin_fields,
        )
        if fields:
            violations[f"node:{node.id}"] = fields
    for output in state.outputs:
        fields = collect_credential_field_violations(
            output.options,
            env_ref_names,
            additional_credential_fields=allowed_secret_ref_fields("sink", output.plugin),
        )
        if fields:
            violations[f"sink:{output.name}"] = fields

    if violations:
        components = "; ".join(f"{component}: {', '.join(fields)}" for component, fields in violations.items())
        raise HTTPException(
            status_code=400,
            detail=(
                f"Pasted YAML contains a literal value in credential-bearing field(s) -- {components}. "
                "Wire each credential through the Secrets panel (produces a {secret_ref: NAME} marker) "
                "instead of pasting the value directly."
            ),
        )


async def _state_with_imported_source_blobs(
    state: CompositionState,
    *,
    source_blob_ids: Mapping[str, str] | None,
    request: Request,
    session_id: UUID,
    session_operation_context: SessionOperationContext,
) -> CompositionState:
    if not source_blob_ids:
        return state

    sources = dict(state.sources)
    requested_blobs: list[tuple[str, UUID]] = []
    for source_name, blob_id_raw in source_blob_ids.items():
        if source_name not in sources:
            raise HTTPException(status_code=400, detail=f"source_blob_ids references unknown source '{source_name}'")
        try:
            blob_id = UUID(blob_id_raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"source_blob_ids.{source_name} must be a UUID") from exc
        requested_blobs.append((source_name, blob_id))

    for source_name, blob_id in requested_blobs:
        blob = await _ready_import_blob(
            blob_id,
            request=request,
            session_id=session_id,
            session_operation_context=session_operation_context,
        )
        source = sources[source_name]
        options = dict(source.options)
        for key in SOURCE_LOCAL_PATH_OPTION_KEYS:
            if key in options:
                del options[key]
        options["path"] = blob.storage_path
        options["blob_ref"] = str(blob.id)
        sources[source_name] = SourceSpec(
            plugin=source.plugin,
            on_success=source.on_success,
            options=options,
            on_validation_failure=source.on_validation_failure,
            description=source.description,
        )

    return CompositionState(
        sources=sources,
        nodes=state.nodes,
        edges=state.edges,
        outputs=state.outputs,
        metadata=state.metadata,
        version=state.version,
    )


class _SourceBlobRowsEntry(TypedDict):
    """One ``blob_rows`` file entry, in ``_resolve_source_blobs``'s shape."""

    blob_id: str
    payload_ref: str
    filename: str
    mime_type: str
    size_bytes: int


def _parse_import_blob_id(raw: str, *, where: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{where} must be a UUID") from exc


async def _ready_import_blob(
    blob_id: UUID,
    *,
    request: Request,
    session_id: UUID,
    session_operation_context: SessionOperationContext,
) -> BlobRecord:
    """Return this session's ready blob; the one fetch-and-check every import re-bind uses.

    Ownership first so a missing blob and another session's blob stay
    indistinguishable (404). A pending or error blob has no settled bytes to
    bind (409): the export side of this round trip and the blob
    download/preview routes already refuse a non-ready blob.
    """
    blob_service = request.app.state.blob_service
    if blob_service is None:
        raise HTTPException(status_code=409, detail="Blob service unavailable for YAML import")
    try:
        blob: BlobRecord = await blob_service.get_blob(blob_id, session_operation_context=session_operation_context)
    except BlobNotFoundError:
        raise HTTPException(status_code=404, detail="Blob not found") from None
    if blob.session_id != session_id:
        raise HTTPException(status_code=404, detail="Blob not found")
    if blob.status != "ready":
        raise HTTPException(status_code=409, detail="Blob is not ready")
    return blob


def _ready_blob_content_hash(blob: BlobRecord) -> str:
    if blob.content_hash is None:
        raise AuditIntegrityError(f"Ready blob '{blob.id}' has no canonical content hash")
    return blob.content_hash


async def _state_with_imported_inline_blobs(
    state: CompositionState,
    *,
    inline_blob_bindings: Mapping[str, InlineBlobBindingRequest] | None,
    request: Request,
    session_id: UUID,
    session_operation_context: SessionOperationContext,
) -> CompositionState:
    """Re-attach inline-content file markers the public projection removed.

    Each marker is rebuilt in the canonical writer's shape
    (``wire_blob_inline_ref``) from this session's blob record, so its
    ``sha256`` is the copy's own content hash. This request field is also
    open to the ordinary pasted-YAML route, so it is a second write path for
    inline markers: it refuses everything the canonical writer refuses by
    running the writer's own checks (``_llm_authored_prompt_surface_error``
    and ``_apply_inline_blob_marker``), and additionally refuses an occupied
    leaf. The version is unchanged: this completes the imported graph, it is
    not an edit of it.
    """
    if not inline_blob_bindings:
        return state

    requested: list[tuple[str, UUID, ContentEncoding]] = [
        (field_path, _parse_import_blob_id(binding.blob_id, where=f"inline_blob_bindings.{field_path}.blob_id"), binding.encoding)
        for field_path, binding in inline_blob_bindings.items()
    ]
    for field_path, blob_id, encoding in requested:
        blob = await _ready_import_blob(
            blob_id,
            request=request,
            session_id=session_id,
            session_operation_context=session_operation_context,
        )
        content_hash = _ready_blob_content_hash(blob)
        try:
            ref = BlobInlineRef(field_path=field_path, blob_id=blob.id, sha256=content_hash, encoding=encoding)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"inline_blob_bindings path '{field_path}' is not a valid option field path "
                    "(source:<name>.options.<field>, node:<id>.options.<field> or output:<name>.options.<field>)"
                ),
            ) from None
        prompt_surface_error = _llm_authored_prompt_surface_error(
            state,
            ref.field_path,
            blob_id=blob.id,
            creation_modality=blob.creation_modality,
        )
        if prompt_surface_error is not None:
            raise HTTPException(status_code=400, detail=prompt_surface_error)
        file_inject_blob_error = _file_inject_blob_binding_error(
            state,
            ref.field_path,
            blob_id=blob.id,
            created_by=blob.created_by,
            creation_modality=blob.creation_modality,
        )
        if file_inject_blob_error is not None:
            raise HTTPException(status_code=400, detail=file_inject_blob_error)
        marker = {"blob_ref": str(blob.id), "mode": "inline_content", "sha256": content_hash}
        if encoding != "utf-8":
            marker["encoding"] = encoding
        try:
            rebound = _apply_inline_blob_marker(state, ref.field_path, marker, replace_existing=False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"inline_blob_bindings path '{field_path}': {exc}") from None
        state = replace(rebound, version=state.version)
    return state


async def _state_with_imported_source_blob_rows(
    state: CompositionState,
    *,
    source_blob_rows: Mapping[str, SourceBlobRowsBindingRequest] | None,
    request: Request,
    session_id: UUID,
    session_operation_context: SessionOperationContext,
) -> CompositionState:
    """Re-attach a ``blob_rows`` source's file list, one entry per blob in order.

    Every entry field comes from this session's blob record, in the shape
    ``set_source_from_blobs`` writes (``_resolve_source_blobs``).
    """
    if not source_blob_rows:
        return state

    requested: list[tuple[str, list[UUID]]] = []
    for source_name, binding in source_blob_rows.items():
        if source_name not in state.sources:
            raise HTTPException(status_code=400, detail=f"source_blob_rows references unknown source '{source_name}'")
        source = state.sources[source_name]
        if source.plugin != "blob_rows":
            raise HTTPException(status_code=400, detail=f"source_blob_rows.{source_name} binds files to a source that is not blob_rows")
        if "blobs" in source.options:
            raise HTTPException(status_code=400, detail=f"source_blob_rows.{source_name}: the source already carries a blobs list")
        blob_ids = [
            _parse_import_blob_id(raw, where=f"source_blob_rows.{source_name}.blob_ids[{index}]")
            for index, raw in enumerate(binding.blob_ids)
        ]
        if len(set(blob_ids)) != len(blob_ids):
            raise HTTPException(status_code=400, detail=f"source_blob_rows.{source_name} lists the same blob more than once")
        requested.append((source_name, blob_ids))

    sources = dict(state.sources)
    for source_name, blob_ids in requested:
        entries: list[_SourceBlobRowsEntry] = []
        seen_hashes: set[str] = set()
        for blob_id in blob_ids:
            blob = await _ready_import_blob(
                blob_id,
                request=request,
                session_id=session_id,
                session_operation_context=session_operation_context,
            )
            content_hash = _ready_blob_content_hash(blob)
            if is_llm_authored_creation_modality(blob.creation_modality):
                # Parity with the canonical set_source_from_blobs writer
                # (``_resolve_source_blobs``): an LLM-authored file reaches a
                # source only through the singular flow that stages its review.
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"source_blob_rows.{source_name}: blob '{blob.id}' is LLM-authored; bind it with "
                        "set_source_from_blob so its interpretation review is staged"
                    ),
                )
            if content_hash in seen_hashes:
                raise HTTPException(
                    status_code=400,
                    detail=f"source_blob_rows.{source_name} lists two files with the same content; entries must be unique documents",
                )
            seen_hashes.add(content_hash)
            entries.append(
                {
                    "blob_id": str(blob.id),
                    "payload_ref": content_hash,
                    "filename": blob.filename,
                    "mime_type": blob.mime_type,
                    "size_bytes": blob.size_bytes,
                }
            )
        source = sources[source_name]
        sources[source_name] = replace(source, options={**deep_thaw(source.options), "blobs": entries})
    return replace(state, sources=sources)


def _state_with_imported_sink_paths(
    state: CompositionState,
    *,
    sink_path_bindings: Mapping[str, Mapping[str, SinkPathBindingRequest]] | None,
    data_dir: str,
    session_id: UUID,
) -> CompositionState:
    """Author each bound sink path below this session's own ``outputs``/``blobs`` folder.

    The shared-graph import passes the sharer's session-relative sink paths
    here; this request field is also open to the ordinary pasted-YAML route,
    so it can only ever produce a path inside the CALLER's session folders.
    ``relative_path`` is normalised and refused (400) when it is empty,
    absolute, holds ``..`` or a backslash, or resolves outside the session's
    sink directories (a symlink, say). An unknown output or option, and an
    option that already carries a value, are refused too. The path is written
    in the absolute form the same-user fork writes. The version is unchanged:
    this completes the imported graph, it is not an edit of it.
    """
    if not sink_path_bindings:
        return state

    outputs_by_name = {output.name: index for index, output in enumerate(state.outputs)}
    allowed = allowed_sink_directories(data_dir, session_id=str(session_id))
    outputs = list(state.outputs)
    for output_name, options in sink_path_bindings.items():
        if output_name not in outputs_by_name:
            raise HTTPException(status_code=400, detail=f"sink_path_bindings references unknown output '{output_name}'")
        index = outputs_by_name[output_name]
        rebuilt = deep_thaw(outputs[index].options)
        for option, binding in options.items():
            where = f"sink_path_bindings.{output_name}.{option}"
            if option not in SINK_LOCAL_PATH_OPTION_KEYS:
                raise HTTPException(
                    status_code=400,
                    detail=f"{where}: the option must be one of {', '.join(SINK_LOCAL_PATH_OPTION_KEYS)}",
                )
            if option in rebuilt and rebuilt[option] is not None:
                raise HTTPException(status_code=400, detail=f"{where}: the sink already carries a value for this option")
            try:
                relative_path = normalise_session_relative_path(binding.relative_path)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"{where}: {exc}") from None
            target = str(session_folder_path(data_dir, binding.folder, session_id=str(session_id), relative_path=relative_path))
            resolved = resolve_sink_data_path(target, data_dir, session_id=str(session_id))
            if not any(resolved.is_relative_to(directory) for directory in allowed):
                raise HTTPException(status_code=400, detail=f"{where}: the path resolves outside this session's folders")
            rebuilt[option] = target
        outputs[index] = replace(outputs[index], options=rebuilt)
    return replace(state, outputs=tuple(outputs))


@router.get(
    "/{session_id}/composer-progress",
    response_model=ComposerProgressSnapshot,
)
async def get_composer_progress(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerProgressSnapshot:
    """Return the latest provider-safe composer progress for a session."""
    session = await _verify_session_ownership(session_id, user, request)
    registry = _get_composer_progress_registry(request)
    # The durable authority re-checks committed identity and ownership inside
    # its own snapshot, so access revoked or a session archived between the
    # checks above and that read is denied there. Answer as the next request
    # would be answered anyway: the opaque 401 the per-request token check
    # gives a revoked principal (auth/session_token.py), and the
    # non-disclosing 404 the ownership check gives. Never a server error for
    # an expected concurrent access change.
    try:
        return await registry.get_latest(str(session.id), user.user_id)
    except ComposerProgressIdentityInactive:
        raise HTTPException(status_code=401, detail="Invalid token") from None
    except ComposerProgressSessionUnavailable:
        raise HTTPException(status_code=404, detail="Session not found") from None


@router.get(
    "/{session_id}/composer/preferences",
    response_model=ComposerPreferencesResponse,
)
async def get_composer_preferences(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerPreferencesResponse:
    session = await _verify_session_ownership(session_id, user, request)
    service: SessionServiceProtocol = request.app.state.session_service
    prefs = await service.get_composer_preferences(session.id)
    return _composer_preferences_response(prefs)


@router.patch(
    "/{session_id}/composer/preferences",
    response_model=ComposerPreferencesResponse,
)
async def update_composer_preferences(
    session_id: UUID,
    body: UpdateComposerPreferencesRequest,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> ComposerPreferencesResponse:
    session = await _verify_session_ownership(session_id, user, request)
    service: SessionServiceProtocol = request.app.state.session_service
    # B2 (load-bearing): the service returns ``(prior, current)`` so
    # Phase 8b's per-session ``composer.session.switched_total``
    # counter can read ``from_mode=transition.prior.trust_mode``
    # without a route-handler read-before-write (which would open a
    # TOCTOU window — see plan §"Option not taken — read-before-write
    # from the route handler"). The PATCH response shape is unchanged;
    # we only project ``current`` into the response model.
    # Deliberately NO session-operation lease here: the preferences writer is
    # serialized on the per-session write lock, not the compose lease, so a
    # mid-plan trust downgrade can land while a compose turn is in flight
    # (elspeth-01d4c6e683; see the service docstring).
    transition = await service.update_composer_preferences(
        session.id,
        trust_mode=body.trust_mode,
        density_default=body.density_default,
        actor=f"user:{user.user_id}",
    )

    # The awaited service return is the commit boundary. Everything below
    # observes that durable transition and must not turn it into a retryable
    # request failure. Cancellation and process-control exceptions still
    # propagate because only ordinary Exception failures are downgraded.
    if transition.prior.trust_mode != transition.current.trust_mode:
        try:
            telemetry: SessionsTelemetry = request.app.state.sessions_telemetry
            record_session_switched(
                telemetry,
                from_mode=transition.prior.trust_mode,
                to_mode=transition.current.trust_mode,
            )
        except Exception as telemetry_error:
            _log_last_resort_diagnostic(
                slog.error,
                "composer_preferences_postcommit_telemetry_failed",
                session_id=str(session.id),
                exc_class=type(telemetry_error).__name__,
            )

    return _composer_preferences_response(transition.current)


@router.get("/{session_id}/state")
async def get_current_state(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> CompositionStateResponse | None:
    """Get the current (highest-version) composition state."""
    session = await _verify_session_ownership(session_id, user, request)
    service = request.app.state.session_service
    catalog, _snapshot = _request_plugin_policy_context(request, user)
    graph_read = await service.read_current_composition_graph(session.id)
    if graph_read.hydration_error is not None:
        head = graph_read.head
        slog.error(
            "composer.graph_hydration_unavailable",
            session_id=str(session.id),
            exc_class=graph_read.hydration_error,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error_type": "composition_state_unavailable",
                "detail": "The saved graph is unavailable. You can ask Composer to recover a saved draft.",
                "state_id": str(head.state_id) if head.state_id is not None else None,
                "version": head.version,
            },
        )
    state = graph_read.record
    composition = _state_from_record(state) if state is not None else None
    if state is None:
        return None
    response = _state_response(state, policy_catalog=catalog)
    # Suggestions belong to this graph version, so reload recomputes the
    # same Stage-1 advice used after composing.
    if composition is None:
        raise AuditIntegrityError("A decoded current graph lacks its composition")
    validation = await run_sync_in_worker(composition.validate)
    response.validation_warnings = _validation_entry_responses(validation.warnings)
    response.validation_suggestions = _validation_entry_responses(validation.suggestions)
    return response


@router.get("/{session_id}/state/head", response_model=CompositionHeadResponse)
async def get_composition_head(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> CompositionHeadResponse:
    """Read the owned head identity without loading potentially damaged graph data."""
    session = await _verify_session_ownership(session_id, user, request)
    service: SessionServiceProtocol = request.app.state.session_service
    head = await service.get_composition_head(session.id)
    return CompositionHeadResponse(state_id=str(head.state_id) if head.state_id is not None else None, version=head.version)


@router.get(
    "/{session_id}/state/versions",
    response_model=list[CompositionStateResponse],
)
async def get_state_versions(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[CompositionStateResponse]:
    """Get composition state versions for a session."""
    session = await _verify_session_ownership(session_id, user, request)
    service = request.app.state.session_service
    catalog, _snapshot = _request_plugin_policy_context(request, user)
    versions = await service.get_state_versions(session.id, limit=limit, offset=offset)
    # Never 409 the whole history and never omit a row: a version whose custody
    # cannot bind serves the degraded (masked, custody_unavailable-named)
    # projection beside untouched siblings, so a bindable version stays
    # reachable for restore. The single-tip GET /state keeps its named 409.
    return [_state_response(v, policy_catalog=catalog, degrade_unbindable_custody=True) for v in versions]


@router.post(
    "/{session_id}/state/revert",
    response_model=CompositionStateResponse,
)
async def revert_state(
    session_id: UUID,
    body: RevertStateRequest,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> CompositionStateResponse:
    """Revert the pipeline to a prior composition state version (R1).

    Creates a new version that is a copy of the specified prior state.
    Injects a system message recording the revert.
    """
    session = await _verify_session_ownership(session_id, user, request)
    service = request.app.state.session_service

    async def _replay(result: object) -> CompositionStateResponse:
        """Project the stored response for an already-terminal revert.

        MUST stay side-effect-free. This runs BEFORE the response-hash
        integrity check in reserve_or_replay_operation_receipt, so anything
        written here would mutate audit-primary interpretation_events under a
        projection not yet proven to match the stored response -- inserting
        new review rows and superseding existing pending ones, then failing
        verification afterwards. The surfacing debt a replayed revert may
        still owe is repaired in _repair_reverted_surfacing_debt, which runs
        only after that check.
        """
        if type(result) is not StateRevertReceiptResult:
            raise AuditIntegrityError("State revert replay has a non-state result locator")
        replay_state = await service.get_state_in_session(result.state_id, session.id)
        # Receipt replay commits to durable state only. Live policy findings
        # can change independently; GET /state reports the current policy.
        return _state_response(replay_state)

    async def _repair_reverted_surfacing_debt(result: OperationReceiptResult) -> None:
        """Repair the post-commit surfacing this revert's settlement owed.

        revert_state_for_operation_receipt terminalizes the operation in the
        same transaction that writes the reverted state, but the surfacing
        pass runs after it. An attempt that dies in between leaves the
        operation terminal, so every retry lands here -- and without this the
        reverted state keeps pending interpretation_requirements with no event
        row, so no review card renders and /execute fails closed with nothing
        the user can resolve.

        The debt is computed per site against durable evidence in ANY
        resolution status (``only_missing_evidence=True``), so this repairs
        only genuinely missing sites and writes nothing once the settling
        attempt -- or a prior replay -- covered them.

        Identity comes from the operation's own result locator, re-resolved
        here rather than closed over from _replay: after_verified receives the
        same locator replay does, not the record replay fetched.
        """
        if type(result) is not StateRevertReceiptResult:
            raise AuditIntegrityError("State revert replay has a non-state result locator")
        replay_state = await service.get_state_in_session(result.state_id, session.id)
        await _surface_reverted_interpretation_reviews(
            service,
            session_id=session.id,
            state_record=replay_state,
        )

    # ``after_verified`` rides BOTH lookups. This one is the terminal-replay
    # probe -- the very path whose settlement may have died between the
    # revert transaction and its surfacing pass -- so leaving the repair off
    # it would make the H1 repair dead on exactly the path that owes it.
    pending = await reserve_or_replay_operation_receipt(
        service=service,
        session_id=session.id,
        kind="state_revert",
        request=body,
        replay=_replay,
        after_verified=_repair_reverted_surfacing_debt,
        reserve_if_absent=False,
        takeover_expired=False,
    )
    if pending is not None and not isinstance(pending, (OperationReceiptLease, OperationReceiptExpired)):
        return pending

    compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(session.id))
    # Resolve the user-supplied target only after entering the same compose
    # exclusion domain as accept/compose. The state is immutable, so releasing
    # this lock before joining an already-active operation cannot invalidate
    # the target; the service repeats ownership validation in the write tx.
    async with compose_lock:
        try:
            target_state = await service.get_state(body.state_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="State not found") from None
        if target_state.session_id != session.id:
            raise HTTPException(status_code=404, detail="State not found")
        expected_current = await service.get_current_state(session.id)
        if expected_current is None:
            raise AuditIntegrityError("State revert session unexpectedly has no current checkpoint")

    reserved = await reserve_or_replay_operation_receipt(
        service=service,
        session_id=session.id,
        kind="state_revert",
        request=body,
        replay=_replay,
        after_verified=_repair_reverted_surfacing_debt,
    )
    if reserved is None:  # pragma: no cover - reserve_if_absent defaults true
        raise AuditIntegrityError("State revert operation was not reserved")
    if not isinstance(reserved, OperationReceiptLease):
        return reserved

    lease_guard = operation_receipt_lease_guard(service=service, lease=reserved)
    try:
        async with compose_lock:
            try:
                new_state = await service.revert_state_for_operation_receipt(
                    reserved.fence,
                    state_id=body.state_id,
                    expected_current_state_id=expected_current.id,
                    expected_current_state_version=expected_current.version,
                    actor="composer_route",
                    response_hash_factory=lambda record: operation_receipt_response_hash(_state_response(record)),
                    session_operation_context=reserved.session_operation_context,
                )
            except ValueError:
                raise HTTPException(status_code=404, detail="State not found") from None
            except OperationReceiptSettlementConflictError:
                failure = await service.fail_operation_receipt(
                    reserved.fence,
                    failure_code="stale_conflict",
                    actor="composer_route",
                    session_operation_context=reserved.session_operation_context,
                )
                raise_operation_receipt_failure(failure)
    finally:
        await lease_guard.finish_active_exception()

    await _surface_reverted_interpretation_reviews(
        service,
        session_id=session.id,
        state_record=new_state,
    )

    return _state_response(new_state)


_IMPORT_REVIEW_DEBT_TIMEOUT_DETAIL = "Review-debt check did not complete within the configured bound; import aborted."
_SEED_REVIEW_DEBT_TIMEOUT_DETAIL = "Review-debt check did not complete within the configured bound; seed aborted."


async def _review_debt_sites_off_loop(
    state: CompositionState,
    *,
    request: Request,
    timeout_detail: str,
) -> tuple[InterpretationReviewSite, ...]:
    """Run the pure review-debt check off the event loop, bounded (elspeth-e5a38115a6).

    ``unsurfaceable_pending_interpretation_review_sites`` is CPU-bound — it
    validates the composition several times — and both import routes call it
    under the session's COMPOSE lease and compose lock. Called inline, it held
    the loop for its whole duration, so one long import stalled every other
    request on the deployment and outlived its own lease. It now runs on the
    shared worker pool like the runtime preflight and under the same bound
    (``composer_runtime_preflight_timeout_seconds``: one knob for "a static
    check on the request path took too long"). The refusal is static: the
    state under check is untrusted input on both routes, so nothing from it
    reaches the detail. The worker's own exceptions propagate unchanged, so
    each caller's malformed-metadata arm still sees the classes it handles.
    """
    from elspeth.web.composer.interpretation_surfacing import unsurfaceable_pending_interpretation_review_sites

    try:
        return await asyncio.wait_for(
            run_sync_in_worker(unsurfaceable_pending_interpretation_review_sites, state),
            timeout=request.app.state.settings.composer_runtime_preflight_timeout_seconds,
        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=timeout_detail) from exc


@router.post(
    "/{session_id}/state/yaml",
    response_model=CompositionStateResponse,
)
async def import_state_yaml(
    session_id: UUID,
    body: ImportStateYamlRequest,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> CompositionStateResponse:
    """Seed a session's composition state from exported runtime YAML."""
    session = await _verify_session_ownership(session_id, user, request)
    return await seed_state_from_runtime_yaml(session=session, body=body, request=request, user=user)


async def seed_state_from_runtime_yaml(
    *,
    session: SessionRecord,
    body: ImportStateYamlRequest,
    request: Request,
    user: UserIdentity,
    composer_meta_updates: ComposerSeedMetaUpdates | None = None,
) -> CompositionStateResponse:
    """Seed through the YAML import validators, stamping exact text ingress.

    Library forks and shared-graph imports use this same path and attach
    their provenance to the first state write. No second state or alternate
    graph importer exists.
    """
    service: SessionServiceProtocol = request.app.state.session_service
    # Everything that can raise before the body runs is resolved BEFORE the
    # lease exists: the lease self-renews until closed, and only the
    # try/finally below closes it, so a raise between acquire and that try
    # would refuse every later COMPOSE writer on this session until restart.
    catalog, plugin_snapshot = _request_plugin_policy_context(request, user)
    compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(session.id))
    lease = await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    try:
        async with compose_lock:
            try:
                imported_state = composition_state_from_runtime_yaml(body.yaml)
            except RuntimeYamlImportError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            secret_service = request.app.state.scoped_secret_resolver
            env_ref_names = frozenset(item.name for item in secret_service.list_refs(str(user.user_id)))
            require_no_credential_material_in_state(
                imported_state,
                surface="composer_yaml_import",
                env_ref_names=env_ref_names,
            )
            _reject_imported_plugin_policy(imported_state, catalog, plugin_snapshot)
            imported_state = await _state_with_imported_source_blobs(
                imported_state,
                source_blob_ids=body.source_blob_ids,
                request=request,
                session_id=session.id,
                session_operation_context=lease.context,
            )
            imported_state = await _state_with_imported_inline_blobs(
                imported_state,
                inline_blob_bindings=body.inline_blob_bindings,
                request=request,
                session_id=session.id,
                session_operation_context=lease.context,
            )
            imported_state = await _state_with_imported_source_blob_rows(
                imported_state,
                source_blob_rows=body.source_blob_rows,
                request=request,
                session_id=session.id,
                session_operation_context=lease.context,
            )
            imported_state = _state_with_imported_sink_paths(
                imported_state,
                sink_path_bindings=body.sink_path_bindings,
                data_dir=str(request.app.state.settings.data_dir),
                session_id=session.id,
            )
            _reject_redacted_sources_without_rebind(imported_state, yaml_text=body.yaml)
            _reject_unbound_blob_storage_sources(
                imported_state,
                data_dir=str(request.app.state.settings.data_dir),
            )
            _reject_disallowed_source_paths(
                imported_state,
                data_dir=str(request.app.state.settings.data_dir),
                session_id=str(session.id),
            )
            _reject_fabricated_secret_literals(
                imported_state,
                secret_service=secret_service,
                user_id=str(user.user_id),
            )
            _reject_file_inject_content_not_uploaded(imported_state)
            _reject_malformed_interpretation_requirements(imported_state)
            _reject_resolved_interpretation_requirements(imported_state)
            # Import must be atomic with respect to review recoverability. Reuse
            # the generic Composer surfacer's own pure site-to-writer mapping so a
            # pending site that cannot become a consumable event is rejected before
            # the composition state is saved.
            from elspeth.web.composer.interpretation_surfacing import prepare_pending_interpretation_event_drafts_for_state

            try:
                unsurfaceable_sites = await _review_debt_sites_off_loop(
                    imported_state,
                    request=request,
                    timeout_detail=_IMPORT_REVIEW_DEBT_TIMEOUT_DETAIL,
                )
            except InterpretationReviewCapacityError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Imported YAML requires source data-contract analysis beyond the bounded work limit. "
                        "Reduce the source/required-field combination or declare explicit source schemas."
                    ),
                ) from exc
            except InterpretationReviewIntegrityError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Imported YAML contains malformed interpretation review metadata.",
                ) from exc
            except (InvariantError, KeyError, TypeError, ValueError) as exc:
                # These remain invariant failures for internally persisted state.
                # At this route the state is untrusted YAML, so reject statically
                # without echoing its field names or values.
                raise HTTPException(
                    status_code=400,
                    detail="Imported YAML contains malformed interpretation review metadata.",
                ) from exc
            if unsurfaceable_sites:
                site_labels = ", ".join(f"{site.component_id}:{site.kind.value}" for site in unsurfaceable_sites)
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Imported YAML contains pending interpretation review site(s) that cannot be surfaced: "
                        f"{site_labels}. Remove or repair the hand-written interpretation_requirements entry and re-import."
                    ),
                )

            state_data, _validation = await _state_data_from_composer_state(
                imported_state,
                settings=request.app.state.settings,
                secret_service=request.app.state.scoped_secret_resolver,
                user_id=str(user.user_id),
                session_id=session.id,
                plugin_snapshot=plugin_snapshot,
                profile_registry=request.app.state.operator_profile_registry,
                catalog=request.app.state.catalog_service,
                runtime_preflight=None,
                preflight_exception_policy="persist_invalid",
                initial_version=imported_state.version,
                telemetry_source="compose",
            )
            interpretation_drafts = prepare_pending_interpretation_event_drafts_for_state(
                imported_state,
                surface_origin=InterpretationSurfaceOrigin.YAML_IMPORT,
                model_identifier=None,
                model_version=None,
                provider=None,
                composer_skill_hash=None,
            )
            ingress = compartment_ingress_record(body.yaml, own_compartment_id=request.app.state.settings.compartment_id)
            seed_meta_updates = {"ingress": ingress} if composer_meta_updates is None else {**composer_meta_updates, "ingress": ingress}
            state_data = replace(
                state_data,
                composer_meta=merge_composer_meta_updates(state_data.composer_meta, seed_meta_updates),
            )
            response_state = await service.save_composition_state_with_interpretations(
                session.id,
                state_data,
                provenance="session_seed",
                interpretations=interpretation_drafts,
                session_operation_context=lease.context,
            )
            return _state_response(response_state, policy_catalog=catalog)
    finally:
        await lease.close()


def _reject_file_inject_content_not_uploaded(state: CompositionState) -> None:
    """Refuse an imported ``file_inject`` node whose content is not an attached upload.

    In the composer a ``file_inject`` entry admits only a user-uploaded file,
    and no ``default_content`` (``file_inject_state_refusals``, the predicate
    /validate and run admission apply). Literal content in pasted YAML would
    reach row data, and from there a prompt, with no review, so it is refused
    here with what a person can do about it. An ``inline_content`` marker is
    left untouched: which blob it names is judged where the blob is resolved.
    """
    refusals = file_inject_state_refusals(state)
    if not refusals:
        return
    refusal = refusals[0]
    if refusal.cause == "default_content":
        action = "Remove default_content from the YAML and use on_miss: null or on_miss: fail, then import again."
    elif refusal.cause == "unaddressable_entry":
        action = "Rename the entry in the YAML to a name of that form and import again."
    else:
        action = (
            f"Remove '{refusal.option_path}' from the YAML and import again, upload the file in this session, then attach it to that entry."
        )
    raise HTTPException(
        status_code=400,
        detail=(
            f"Imported YAML: file_inject node '{refusal.node_name}' option '{refusal.option_path}' is refused: "
            f"{refusal.problem}. In the composer, file_inject content must be a file the user uploaded. {action}"
        ),
    )


def _reject_malformed_interpretation_requirements(state: CompositionState) -> None:
    """Reject hand-written ``interpretation_requirements`` rows that this
    module's own schema would refuse, BEFORE persistence (elspeth-ae5160c3cb).

    Legitimate exports never carry requirement rows (``strip_authoring_options``
    removes them at export) and the importer's auto-stagers only emit
    well-formed rows, so any malformed row here was hand-written into the
    pasted document. Sources and nodes cross the same untrusted YAML boundary;
    checking only nodes would turn a malformed source row into a later 500.
    Rejecting outright is the same
    stricter-than-the-tool-path posture as ``_reject_fabricated_secret_literals``
    for this paste-facing entry point — and it guarantees the post-persist
    surfacing pass below can parse every row without a post-persist failure
    path. Audit hygiene: name the component, never echo row content.
    """
    components = [
        *((f"Source '{source_name}'", source.options) for source_name, source in state.sources.items()),
        *((f"Node '{node.id}'", node.options) for node in state.nodes),
    ]
    for component, options in components:
        try:
            parse_interpretation_requirements(options)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{component} carries a malformed interpretation_requirements entry. "
                    "Remove the hand-written interpretation_requirements block and re-import; "
                    "the importer stages review requirements itself."
                ),
            ) from exc


def _reject_resolved_interpretation_requirements(state: CompositionState) -> None:
    """Refuse hand-written ``status: resolved`` review rows in pasted YAML.

    A resolved row is resolver-owned evidence: it records that the user
    accepted a card, citing the interpretation event that did so. Pasted YAML
    carries no such event, and the run gate (``materialize_state_for_execution``)
    trusts a coherent resolved row without consulting the events table, so
    admitting one would let the document approve its own prompt and model
    reviews and persist review evidence for events that never happened. The
    Composer tool path refuses the same "resolver-owned status 'resolved'" at
    admission; this is that refusal at the paste-facing import boundary.

    Refused rather than demoted to a pending shell: legitimate exports never
    carry requirement rows (``strip_authoring_options``), so a resolved row is
    always hand-written, and silently rewriting pasted input would hide that.
    Hand-written PENDING rows stay importable. Must run after
    ``_reject_malformed_interpretation_requirements`` so every row parses.
    Audit hygiene: name the component, never echo row content.
    """
    components = [
        *((f"Source '{source_name}'", source.options) for source_name, source in state.sources.items()),
        *((f"Node '{node.id}'", node.options) for node in state.nodes),
    ]
    for component, options in components:
        requirements = parse_interpretation_requirements(options)
        if requirements is None:
            continue
        if any(requirement["status"] != "pending" for requirement in requirements):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{component} carries a hand-written interpretation_requirements entry with status "
                    "'resolved'. Imported YAML may stage pending review requirements only; remove the "
                    "resolved entry and re-import, then resolve the review cards in this session."
                ),
            )


@router.post(
    "/{session_id}/state/e2e-seed",
    response_model=CompositionStateResponse,
    include_in_schema=False,
)
async def seed_state_for_e2e(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> CompositionStateResponse:
    """Test-only state seed endpoint for Playwright-managed E2E runs."""
    if not request.app.state.settings.e2e_state_seed_enabled:
        raise HTTPException(status_code=404, detail="Not found")

    session = await _verify_session_ownership(session_id, user, request)
    catalog, plugin_snapshot = _request_plugin_policy_context(request, user)
    service: SessionServiceProtocol = request.app.state.session_service
    # Body parse and lock lookup precede the self-renewing lease for the same
    # reason as import_state_yaml: only the try/finally below closes it.
    try:
        body = SeedCompositionStateRequest.model_validate(await request.json())
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid seed request JSON") from exc
    compose_lock = await _get_session_compose_lock_registry(request).get_lock(str(session.id))
    lease = await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    try:
        async with compose_lock:
            try:
                seeded_state = CompositionState.from_dict(body.state)
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid composition state JSON") from exc
            _reject_imported_plugin_policy(seeded_state, catalog, plugin_snapshot)

            _reject_unbound_blob_storage_sources(
                seeded_state,
                data_dir=str(request.app.state.settings.data_dir),
            )
            _reject_disallowed_source_paths(
                seeded_state,
                data_dir=str(request.app.state.settings.data_dir),
                session_id=str(session.id),
            )
            _reject_fabricated_secret_literals(
                seeded_state,
                secret_service=request.app.state.scoped_secret_resolver,
                user_id=str(user.user_id),
            )
            _reject_file_inject_content_not_uploaded(seeded_state)
            _reject_malformed_interpretation_requirements(seeded_state)
            from elspeth.web.composer.interpretation_surfacing import prepare_pending_interpretation_event_drafts_for_state

            try:
                unsurfaceable_sites = await _review_debt_sites_off_loop(
                    seeded_state,
                    request=request,
                    timeout_detail=_SEED_REVIEW_DEBT_TIMEOUT_DETAIL,
                )
            except (InvariantError, KeyError, TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid composition state JSON") from exc
            if unsurfaceable_sites:
                raise HTTPException(status_code=400, detail="Composition state contains review debt that cannot be surfaced")

            state_data, _validation = await _state_data_from_composer_state(
                seeded_state,
                settings=request.app.state.settings,
                secret_service=request.app.state.scoped_secret_resolver,
                user_id=str(user.user_id),
                session_id=session.id,
                plugin_snapshot=plugin_snapshot,
                profile_registry=request.app.state.operator_profile_registry,
                catalog=request.app.state.catalog_service,
                runtime_preflight=None,
                preflight_exception_policy="persist_invalid",
                initial_version=seeded_state.version,
                telemetry_source="state_seed",
            )
            interpretation_drafts = prepare_pending_interpretation_event_drafts_for_state(
                seeded_state,
                surface_origin=InterpretationSurfaceOrigin.E2E_SEED,
                model_identifier=None,
                model_version=None,
                provider=None,
                composer_skill_hash=None,
            )
            state_record = await service.save_composition_state_with_interpretations(
                session.id,
                state_data,
                provenance="session_seed",
                interpretations=interpretation_drafts,
                session_operation_context=lease.context,
            )
            return _state_response(state_record, policy_catalog=catalog)
    finally:
        await lease.close()


async def _require_yaml_export_preflight(
    state: CompositionState,
    *,
    request: Request,
    session_id: UUID,
    plugin_snapshot: PluginAvailabilitySnapshot,
) -> None:
    """Keep the existing export preflight for currently available states."""
    try:
        runtime_validation = await _runtime_preflight_for_state(
            state,
            settings=request.app.state.settings,
            secret_service=None,
            user_id=None,
            session_id=session_id,
            plugin_snapshot=plugin_snapshot,
            profile_registry=request.app.state.operator_profile_registry,
            catalog=request.app.state.catalog_service,
        )
    except (
        TimeoutError,
        OSError,
        PluginConfigError,
        PluginNotFoundError,
        GraphValidationError,
    ) as exc:
        _record_composer_runtime_preflight_telemetry(
            "exception",
            source="yaml_export",
            exception_class=type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail="Runtime preflight could not complete; YAML export aborted.",
        ) from exc
    _record_composer_runtime_preflight_telemetry(
        "passed" if runtime_validation.is_valid else "failed",
        source="yaml_export",
    )
    if not runtime_validation.is_valid:
        raise HTTPException(
            status_code=409,
            detail="Current composition state failed runtime preflight. Fix validation errors before exporting YAML.",
        )


async def _verified_yaml_export_blob_ids(
    state: CompositionState,
    *,
    request: Request,
    session_id: UUID,
    session_operation_context: SessionOperationContext,
) -> dict[str, str]:
    """Verify every public-export blob sidecar entry against live custody."""
    source_blob_ids: dict[str, str] = {}
    parsed_blob_ids: list[tuple[SourceSpec, UUID]] = []
    for source_name, source in state.sources.items():
        if "blob_ref" not in source.options:
            continue
        blob_ref = source.options["blob_ref"]
        if type(blob_ref) is not str:
            raise AuditIntegrityError("YAML export source blob_ref must be a canonical UUID")
        try:
            blob_id = UUID(blob_ref)
        except ValueError as exc:
            raise AuditIntegrityError("YAML export source blob_ref must be a canonical UUID") from exc
        if str(blob_id) != blob_ref:
            raise AuditIntegrityError("YAML export source blob_ref must be a canonical UUID")
        source_blob_ids[source_name] = blob_ref
        parsed_blob_ids.append((source, blob_id))

    if not parsed_blob_ids:
        return source_blob_ids
    blob_service: BlobServiceProtocol = request.app.state.blob_service
    for source, blob_id in parsed_blob_ids:
        try:
            blob = await blob_service.get_blob(blob_id, session_operation_context=session_operation_context)
        except BlobNotFoundError:
            raise AuditIntegrityError("YAML export blob custody verification failed") from None
        # ``BlobServiceProtocol`` is structural, so an implementation could
        # hand back anything; ``BlobRecord`` is an owned, unsubclassed frozen
        # dataclass, so exact-type is the closed check (ADR-032) and it fails
        # closed — anything else aborts the export.
        if type(blob) is not BlobRecord:
            raise TypeError("BlobServiceProtocol.get_blob() must return BlobRecord")
        source_paths = {
            value for key in SOURCE_LOCAL_PATH_OPTION_KEYS if key in source.options and type(value := source.options[key]) is str
        }
        if (
            blob.id != blob_id
            or blob.session_id != session_id
            or blob.status != "ready"
            or type(storage_path := blob.storage_path) is not str
            or storage_path not in source_paths
        ):
            raise AuditIntegrityError("YAML export blob custody verification failed")
    return source_blob_ids


@router.get("/{session_id}/state/yaml")
async def get_state_yaml(
    session_id: UUID,
    request: Request,
    user: Annotated[UserIdentity, Depends(require_pipeline_user)],
) -> StateYamlResponse:
    """Get YAML representation of the current composition state (M1).

    Runs runtime preflight on the exact CompositionState reconstructed
    from the persisted record, then generates deterministic public YAML via
    generate_public_yaml() against that same snapshot. YAML export preflight
    deliberately does not receive the scoped secret resolver: export
    serializes secret_ref markers, and a rejected preflight response must
    not expose resolved secret values through plugin validation prose.
    The two operations see the same Python object — there is no re-fetch
    between preflight and serialization, so a state that passes the gate
    is byte-identical to the state that gets serialized.
    """
    session = await _verify_session_ownership(session_id, user, request)
    service: SessionServiceProtocol = request.app.state.session_service
    # The export records a composer-completion event, so this is a writer:
    # it takes COMPOSE authority and answers 409 while another compose is
    # live (elspeth-bf52d495a2, option A).
    lease = await SessionOperationLease.acquire(
        service.session_operation_authority,
        session_id=session.id,
        operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id=service.session_operation_owner_instance_id,
        lease_seconds=service.session_operation_lease_seconds,
    )
    try:
        state_record = await service.get_current_state(session.id)
        if state_record is None:
            raise HTTPException(status_code=404, detail="No composition state exists")
        state = _state_from_record(state_record)
        policy_catalog, plugin_snapshot = _request_plugin_policy_context(request, user)
        # Historical states must remain exportable in their authored, public form
        # even when a component is no longer enabled.  Do not instantiate or lower
        # such a component merely to serialize it for repair elsewhere.
        if not _composition_plugin_policy_findings(state, policy_catalog):
            await _require_yaml_export_preflight(
                state,
                request=request,
                session_id=session.id,
                plugin_snapshot=plugin_snapshot,
            )
        source_blob_ids = await _verified_yaml_export_blob_ids(
            state,
            request=request,
            session_id=session.id,
            session_operation_context=lease.context,
        )
        # elspeth-06f92da0d9: this route is the one consumer that hands the user a
        # document to keep, so it is where the deliberate custody redaction stops
        # being invisible — header first, then the bare projection. The marker
        # lives here rather than in ``generate_public_yaml`` because the MCP,
        # share, and acceptance-import consumers of that function must keep bare
        # bytes (see its docstring).
        compartment_header = compartment_marking_header(request.app.state.settings.compartment_id)
        yaml_str = compartment_header + public_export_redaction_header(state) + generate_public_yaml(state)

        # Audit-first and fence-first: a failed or stale COMPOSE authority
        # returns no YAML and emits no completion telemetry.
        service.session_operation_authority.mutate(
            lease.context,
            lambda transaction: transaction.composer_completion.record_yaml_export(
                composition_state_id=state_record.id,
                actor=str(user.user_id),
                created_at=datetime.now(UTC),
            ),
        )

        # Phase 8 Sub-task 7c (telemetry-backfill: phase-6).
        # composer.session.completed_total — fires AFTER the audit
        # fenced audit transaction has committed. If that write raises,
        # control never reaches this line and the counter
        # stays at zero and the superset invariant (counter aggregates
        # over committed audit rows) is structurally enforced.
        record_session_completed(
            request.app.state.sessions_telemetry,
            completion_verb="export_yaml",
        )

        response: StateYamlResponse = {"yaml": yaml_str}
        export_redaction = public_export_redaction(state)
        if export_redaction["sources"] or export_redaction["nodes"] or export_redaction["outputs"]:
            response["redaction"] = {
                "stripped_source_options": export_redaction["sources"],
                "stripped_node_options": export_redaction["nodes"],
                "stripped_output_options": export_redaction["outputs"],
                # Names from the custody-verified sidecar map; the UUID values
                # stay server-side (scrub ruling 2304d57fb).
                "blob_linked_sources": sorted(source_blob_ids),
                "rebind_guidance": PUBLIC_EXPORT_REBIND_GUIDANCE,
            }
        return response
    finally:
        await lease.close()
