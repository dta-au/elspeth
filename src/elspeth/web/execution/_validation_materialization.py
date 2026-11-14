"""Materialization and provider-policy phases for execution validation.

Every function returns immutable evidence. The validation runner is the sole
owner of check ordering and ledger mutation.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast
from uuid import UUID

import yaml

from elspeth.contracts.blobs import AllowedMimeType, BlobCreator, BlobNotFoundError, BlobRecord, BlobStateError
from elspeth.contracts.blobs_inline import (
    BlobContentResolutionError,
    BlobInlineRef,
    BlobInlineValidationViolation,
    is_widened_blob_ref,
)
from elspeth.contracts.composer_paths import McpConfiguredRootAuthority
from elspeth.contracts.enums import CreationModality, is_llm_authored_creation_modality
from elspeth.contracts.errors import PipelineLoweringError
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.plugin_capabilities import PluginCapability
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.blobs_inline import (
    _NODE_COLLECTION_KEYS,
    BLOB_INLINE_AGGREGATE_BYTE_CAP,
    BLOB_INLINE_PER_REF_BYTE_CAP,
    _discover_blob_content_refs,
    _substitute_blob_content_refs_for_validation,
    _validate_blob_content_refs_sync,
)
from elspeth.web.composer.state import CompositionState
from elspeth.web.execution._validation_model import (
    InterpretationValidatedState,
    MaterializedYaml,
    PhaseFailure,
    PhaseReport,
    _blocked_readiness,
    _snapshot_materialized_evidence,
)
from elspeth.web.execution.preflight import resolve_runtime_yaml_paths
from elspeth.web.execution.protocol import YamlGenerator
from elspeth.web.execution.schemas import (
    CHECK_AWS_S3_ENDPOINT_URL_POLICY,
    CHECK_AWS_S3_SOURCE_POLICY,
    CHECK_BLOB_INLINE_REFS,
    CHECK_LLM_BASE_URL_POLICY,
    CHECK_LLM_RETRY_BUDGET_POLICY,
    CHECK_LLM_TRACING_POLICY,
    ValidationCheck,
    ValidationError,
)
from elspeth.web.plugin_policy.coverage import node_has_capability, source_has_capability
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId
from elspeth.web.provider_config_policy import (
    web_aws_s3_endpoint_url_policy_error,
    web_aws_s3_source_policy_error,
    web_llm_base_url_policy_error,
    web_llm_retry_budget_policy_error,
    web_llm_tracing_policy_error,
)


@dataclass(frozen=True, slots=True)
class _LLMPolicyComponent:
    """One source or transform subject to component-neutral LLM policy."""

    component_id: str
    component_type: Literal["source", "transform"]
    label: str
    options: Mapping[str, Any]

    def __post_init__(self) -> None:
        # ``options`` arrives from ``CompositionState``, which already
        # deep-freezes source/node options in its own ``__post_init__``, so in
        # practice this is a cheap re-freeze of an already-immutable mapping.
        # It is stated here anyway because the component may be constructed
        # from any Mapping and both readers — ``web_llm_base_url_policy_error``
        # and ``web_llm_tracing_policy_error`` — are policy gates whose input
        # must not be mutable behind their back. No consumer compares this
        # field by identity.
        freeze_fields(self, "options")


def _source_policy_component_id(source_name: object) -> str:
    """Return a stable, value-safe id for one possibly malformed source key."""
    if type(source_name) is not str:
        return "source:<invalid>"
    return "source" if source_name == "source" else f"source:{source_name}"


def _llm_policy_components(state: CompositionState) -> tuple[_LLMPolicyComponent, ...]:
    """Return source-first, stable components for shared LLM egress gates."""
    source_items = sorted(
        cast("Mapping[object, Any]", state.sources).items(),
        key=lambda item: (0, item[0]) if type(item[0]) is str else (1, ""),
    )
    components = [
        _LLMPolicyComponent(
            component_id=_source_policy_component_id(source_name),
            component_type="source",
            label="Source",
            options=source.options,
        )
        for source_name, source in source_items
        if source_has_capability(source, PluginCapability.LLM)
    ]
    # Every LLM-capable plugin-bearing node, not ``node_type == "transform"``
    # alone (elspeth-df8082552d). Capability resolution supplies both the
    # plugin-bearing check and the LLM subject vocabulary (elspeth-c7626ae109),
    # so neither node kinds nor the builtin plugin name are restated here.
    #
    # This is LIVE, not a latent tidy-up: ``llm`` on an AGGREGATION node
    # validates with zero composer errors (the batch-aware constraint is
    # collector-only, in ``state.py``'s ``_collector_intrinsic_errors``), so
    # before this widening a web-authored aggregation could carry an
    # attacker-supplied ``base_url`` past the egress gate and be stopped only
    # at runtime, by an unrelated check, with a runtime-shaped error instead
    # of ``llm_base_url_not_allowed``.
    #
    # ``component_type`` deliberately stays ``"transform"``: it is a closed
    # wire Literal, and the readers below branch on it to choose between
    # "LLM nodes" and "LLM sources" phrasing. Widening THAT would change a
    # wire vocabulary and a message contract, which a coverage fix has no
    # business doing. The node kind rides on ``label``, which exists only to
    # be interpolated into the human-readable ``detail`` string.
    components.extend(
        _LLMPolicyComponent(
            component_id=node.id,
            component_type="transform",
            label=node.node_type.capitalize(),
            options=node.options,
        )
        for node in state.nodes
        if node_has_capability(node, PluginCapability.LLM)
    )
    return tuple(components)


def _blob_inline_component_id(field_path: str) -> str | None:
    if field_path == "(aggregate)":
        return None
    if field_path.startswith("source."):
        return "source"
    if field_path.startswith("node:"):
        component, _separator, _rest = field_path.partition(".")
        return component[len("node:") :]
    if field_path.startswith("output:"):
        component, _separator, _rest = field_path.partition(".")
        return component[len("output:") :]
    return field_path


def _blob_inline_component_type(field_path: str) -> str | None:
    if field_path == "(aggregate)":
        return None
    if field_path.startswith("source."):
        return "source"
    if field_path.startswith("node:"):
        return "transform"
    if field_path.startswith("output:"):
        return "sink"
    return None


def _blob_inline_validation_detail(violations: list[BlobInlineValidationViolation]) -> str:
    return "; ".join(f"{violation.field_path}: {violation.category}" for violation in violations)


def _blob_inline_validation_error(violation: BlobInlineValidationViolation) -> ValidationError:
    return ValidationError(
        component_id=_blob_inline_component_id(violation.field_path),
        component_type=_blob_inline_component_type(violation.field_path),
        message=f"Inline content blob reference at {violation.field_path} is {violation.category}: {violation.detail}",
        suggestion="Verify the blob exists, is ready, is under the configured size caps, and matches the pinned sha256.",
        error_code=f"{violation.category}_inline_blob_content",
    )


_LLM_PROMPT_SURFACE_OPTION_ROOTS: Final[frozenset[str]] = frozenset({"prompt_template", "system_prompt", "model"})


def llm_prompt_surface_field(field_path: str) -> tuple[str, str] | None:
    """Return ``(node_name, option_path)`` when an inline-ref path is an ``llm`` prompt surface or model.

    The one definition of the domain every LLM-authored inline-blob refusal
    guards: ``wire_blob_inline_ref`` and the node-option authoring tools, the
    /validate materialization phase, and run admission. ``field_path`` is the
    canonical ``node:<name>.options.<key>[.<key>...]`` form
    ``_discover_blob_content_refs`` produces. The guarded options are
    ``prompt_template``, ``system_prompt`` and ``model`` (and anything beneath
    them), and ``queries`` as a whole value, a whole ``queries.<name>`` value,
    or that query's ``template``: every value that is, or would carry, a query
    template. Other options, including a query's ``input_fields``, are not
    guarded. Whether ``<name>`` is an ``llm`` node is the caller's decision,
    made from the representation it holds.
    """
    prefix, separator, option_path = field_path.partition(".options.")
    if separator == "" or not prefix.startswith("node:"):
        return None
    keys = option_path.split(".")
    root = keys[0]
    is_query_prompt = root == "queries" and (len(keys) <= 2 or keys[2] == "template")
    if root not in _LLM_PROMPT_SURFACE_OPTION_ROOTS and not is_query_prompt:
        return None
    return prefix.removeprefix("node:"), option_path


FILE_INJECT_PLUGIN: Final = "file_inject"
FILE_INJECT_CONTENT_OPTION_ROOT: Final = "files"
FILE_INJECT_DEFAULT_CONTENT_OPTION: Final = "default_content"
#: The marker the authoring tools accept, as the planner is taught to write it.
FILE_INJECT_UPLOAD_MARKER_SHAPE: Final = '{"blob_ref": "<blob_id>", "mode": "inline_content", "sha256": "<content_hash>"}'
#: The creator the upload route records. Assistant- and pipeline-created blobs
#: can also be VERBATIM, so a creation modality alone does not mean "uploaded".
USER_BLOB_CREATOR: Final[BlobCreator] = "user"

FileInjectRefusalCause = Literal["not_user_uploaded_blob", "literal_content", "default_content", "unaddressable_entry"]
#: One option-key segment of an inline-reference field path, as ``BlobInlineRef``
#: validates it. An entry whose name is not such a segment cannot be named by
#: ``wire_blob_inline_ref``, and a marker beneath it cannot be resolved.
_INLINE_REF_PATH_SEGMENT: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_ENTRY_NAME_RULE: Final = "in the composer an entry name must start with a letter or '_' and contain only letters, digits, '_' and '-'"
#: What a rename costs, for the person-facing repairs of an unaddressable entry name.
FILE_INJECT_ENTRY_RENAME_CONSEQUENCE: Final = (
    "In mode select the entry name is the value rows are matched on, so renaming it changes which rows match."
)


def is_user_uploaded_blob(*, created_by: str, creation_modality: CreationModality) -> bool:
    """Whether a blob is a file the user uploaded: created by the user and not LLM-authored.

    Creator and modality are read as a pair, as the share freeze reads them
    (``shareable_reviews.service._check_shareable_blob``): a blob the composer
    created from text in the user's message, a pipeline output and a session-fork
    copy of either all record ``VERBATIM`` without being an upload. A fork copy
    keeps its source's creator, so a copy of an untouched upload is still an
    upload. ``update_blob`` records the assistant as creator of any blob it
    rewrites, so a rewritten upload, and a fork copy of it, is not.
    """
    return created_by == USER_BLOB_CREATOR and not is_llm_authored_creation_modality(creation_modality)


@dataclass(frozen=True, slots=True)
class _RuntimePluginNode:
    """One plugin node of a runtime config dict: its name, plugin, and options as found (``None`` when absent)."""

    name: str
    plugin: str
    options: object


@trust_boundary(
    tier=3,
    source="runtime pipeline config dict generated from web/LLM-authored composition state",
    source_param="config",
    suppresses=(),
    invariant=(
        "returns every node entry that is a dict with a string name and a string plugin and skips any section or "
        "node of another shape; never raises on config and never coerces a value"
    ),
    non_raising=True,
)
def _runtime_config_plugin_nodes(config: Mapping[str, object]) -> list[_RuntimePluginNode]:
    """Every well-formed plugin node, over the sections ``_discover_blob_content_refs`` walks."""
    found: list[_RuntimePluginNode] = []
    for collection_key in _NODE_COLLECTION_KEYS:
        if collection_key not in config:
            continue
        nodes = config[collection_key]
        if type(nodes) is not list:
            continue
        for node in cast(list[object], nodes):
            if type(node) is not dict:
                continue
            node_dict = cast(dict[str, object], node)
            if "name" not in node_dict or "plugin" not in node_dict:
                continue
            name = node_dict["name"]
            plugin = node_dict["plugin"]
            if type(name) is str and type(plugin) is str:
                found.append(_RuntimePluginNode(name, plugin, node_dict["options"] if "options" in node_dict else None))
    return found


@trust_boundary(
    tier=3,
    source="runtime pipeline config dict generated from web/LLM-authored composition state",
    source_param="config",
    suppresses=(),
    invariant=(
        "returns the name-to-plugin map of every well-formed node entry and skips any section, node, name or "
        "plugin of another shape; never raises on config and never coerces a value"
    ),
    non_raising=True,
)
def runtime_config_node_plugins(config: Mapping[str, object]) -> dict[str, str]:
    """Node name to plugin for a runtime config dict."""
    return {node.name: node.plugin for node in _runtime_config_plugin_nodes(config)}


def guarded_inline_surface_field(field_path: str, *, node_plugins: Mapping[str, str]) -> tuple[str, str, str] | None:
    """Return ``(node_name, option_path, plugin)`` when an inline-ref path is in a guarded domain.

    The one definition of where a blob binding is judged, shared by the
    authoring tools, /validate and run admission:

    * on an ``llm`` node, a prompt surface or the model (``llm_prompt_surface_field``),
      which admits no LLM-authored blob (``is_llm_authored_prompt_surface_binding``);
    * on a ``file_inject`` node, anything at or beneath ``files``, which admits
      only a file the user uploaded (``file_inject_blob_refusal``): its content
      is written into row data and can reach a prompt that no review reads.

    ``node_plugins`` maps node name to plugin, built by each caller from the
    representation it holds; a node it does not name is not guarded.
    """
    prefix, separator, option_path = field_path.partition(".options.")
    if separator == "" or not prefix.startswith("node:"):
        return None
    node_name = prefix.removeprefix("node:")
    if node_name not in node_plugins:
        return None
    plugin = node_plugins[node_name]
    if plugin == "llm":
        return None if llm_prompt_surface_field(field_path) is None else (node_name, option_path, plugin)
    if plugin == FILE_INJECT_PLUGIN and option_path.split(".")[0] == FILE_INJECT_CONTENT_OPTION_ROOT:
        return node_name, option_path, plugin
    return None


def is_llm_authored_prompt_surface_binding(
    field_path: str,
    *,
    node_plugins: Mapping[str, str],
    creation_modality: CreationModality,
) -> bool:
    """Whether binding a blob of ``creation_modality`` at an ``llm`` node's ``field_path`` is refused.

    ADR-034 admits ``inline_content`` markers in prompt fields so a USER-uploaded
    prompt artifact can back an ``llm`` node. The ``llm_prompt_template`` and
    ``llm_model_choice`` reviews read those options as strings, and the run
    substitutes blob bytes afterwards, so an LLM-authored blob there would become
    the executed prompt or model with no operator review. Only user-verbatim blob
    content may stand in for that text. The rule is modality-only and covers
    ``llm`` nodes only; ``file_inject`` has its own, stricter rule
    (``file_inject_blob_refusal``).
    """
    surface = guarded_inline_surface_field(field_path, node_plugins=node_plugins)
    return surface is not None and surface[2] == "llm" and is_llm_authored_creation_modality(creation_modality)


@dataclass(frozen=True, slots=True)
class FileInjectContentRefusal:
    """One reason a ``file_inject`` node is refused in the composer.

    In the composer the only value accepted at ``files.<entry>.content`` is an
    ``inline_content`` marker for a file the user uploaded
    (``is_user_uploaded_blob``), and ``default_content`` is not accepted in any
    form: both are written into row data, can reach an LLM prompt, and have no
    review. An entry whose name an inline blob reference cannot address can
    never hold an upload, so it is refused too. A settings file run from the
    CLI is not judged by this; its author wrote it.
    """

    node_name: str
    option_path: str
    cause: FileInjectRefusalCause
    blob_id: UUID | None = None
    #: The ``files`` entry, when the refusal was found by walking the mapping.
    #: A name with a dot cannot be recovered from ``option_path``.
    entry: str | None = None

    @property
    def field_path(self) -> str:
        return f"node:{self.node_name}.options.{self.option_path}"

    @property
    def entry_name(self) -> str | None:
        """The ``files`` entry this refusal names, or ``None`` when it names no single entry."""
        if self.entry is not None:
            return self.entry
        # Without ``entry`` the path came from an inline reference, whose
        # segments hold no dot: ``files.<entry>[.<key>...]`` names ``<entry>``.
        segments = self.option_path.split(".")
        if len(segments) < 2 or segments[0] != FILE_INJECT_CONTENT_OPTION_ROOT or not segments[1]:
            return None
        return segments[1]

    @property
    def problem(self) -> str:
        """What was wrong, without the refused value."""
        if self.cause == "not_user_uploaded_blob":
            return f"blob '{self.blob_id}' is not a file the user uploaded"
        if self.cause == "literal_content":
            return "it holds literal content, not an uploaded file"
        if self.cause == "unaddressable_entry":
            return f"an uploaded file cannot be attached to an entry named {self.entry!r}: {_ENTRY_NAME_RULE}"
        return "default_content is not accepted in the composer"

    @property
    def _content_path(self) -> str:
        entry_name = self.entry_name
        return (
            f"node:{self.node_name}.options.{FILE_INJECT_CONTENT_OPTION_ROOT}."
            f"{'<entry_name>' if entry_name is None else entry_name}.content"
        )

    @property
    def repair(self) -> str:
        """The path that works for the planner, naming this node and entry."""
        if self.cause == "default_content":
            return "Use on_miss: null or on_miss: fail and handle the miss downstream."
        if self.cause == "unaddressable_entry":
            return (
                "Give the entry a name of that form, then attach the user's upload to it. In mode select the entry "
                "name must equal the row's key value, so normalise the key field upstream first."
            )
        return (
            "In the composer, file_inject content must be a file the user uploaded: "
            "(1) tell the user to save the text as a file and upload it; "
            "(2) once it is uploaded, find it with list_blobs / get_blob_metadata; "
            f"(3) attach it: for an entry that already exists, wire_blob_inline_ref at '{self._content_path}'; "
            "when the call creates the node or the entry, set that entry's content to "
            f"{FILE_INJECT_UPLOAD_MARKER_SHAPE} with the blob's id and content_hash."
        )

    @property
    def repair_for_person(self) -> str:
        """The same path, worded for a person reading a refused run or import."""
        if self.cause == "default_content":
            return "Remove default_content and use on_miss: null or on_miss: fail."
        if self.cause == "unaddressable_entry":
            return f"Rename the entry to a name of that form, then attach the uploaded file to it. {FILE_INJECT_ENTRY_RENAME_CONSEQUENCE}"
        return f"Save the text as a file, upload it in this session, then attach the upload at '{self._content_path}'."


@trust_boundary(
    tier=3,
    source="file_inject node options authored through the composer (LLM tool call, pasted YAML or persisted session state)",
    source_param="options",
    suppresses=(),
    invariant=(
        "parses each entry's content with is_widened_blob_ref into an owned WidenedBlobRefShape; returns a refusal "
        "for any non-null default_content, for every entry whose name is not an inline-reference path segment and "
        "for every content that is not an inline_content marker, and an empty tuple for every other options shape; "
        "never raises on options"
    ),
    non_raising=True,
)
def file_inject_option_refusals(node_name: str, options: Mapping[str, object]) -> tuple[FileInjectContentRefusal, ...]:
    """Refusals a ``file_inject`` node's options carry before any blob is looked up.

    Judged on the options as authored, before blob substitution: afterwards a
    marker and a literal are both strings. ``default_content`` set to anything
    but null is refused; so is an entry whose name no inline blob reference can
    address, and a ``content`` that is not an ``inline_content`` marker. Whether
    a marker's blob was uploaded by the user is ``file_inject_blob_refusal``,
    which needs the blob record.
    """
    thawed = deep_thaw(options)
    refusals: list[FileInjectContentRefusal] = []
    if FILE_INJECT_DEFAULT_CONTENT_OPTION in thawed and thawed[FILE_INJECT_DEFAULT_CONTENT_OPTION] is not None:
        refusals.append(FileInjectContentRefusal(node_name, FILE_INJECT_DEFAULT_CONTENT_OPTION, "default_content"))
    if FILE_INJECT_CONTENT_OPTION_ROOT not in thawed:
        return tuple(refusals)
    files = thawed[FILE_INJECT_CONTENT_OPTION_ROOT]
    if type(files) is not dict:
        return tuple(refusals)
    for entry_name, entry in files.items():
        if type(entry) is not dict:
            continue
        entry_path = f"{FILE_INJECT_CONTENT_OPTION_ROOT}.{entry_name}"
        if type(entry_name) is not str or _INLINE_REF_PATH_SEGMENT.fullmatch(entry_name) is None:
            refusals.append(FileInjectContentRefusal(node_name, entry_path, "unaddressable_entry", entry=str(entry_name)))
            continue
        if "content" not in entry:
            continue
        marker = is_widened_blob_ref(entry["content"])
        if marker is None or marker.mode != "inline_content":
            refusals.append(FileInjectContentRefusal(node_name, f"{entry_path}.content", "literal_content", entry=entry_name))
    return tuple(refusals)


def file_inject_blob_refusal(
    field_path: str,
    *,
    blob_id: UUID,
    node_plugins: Mapping[str, str],
    created_by: str,
    creation_modality: CreationModality,
) -> FileInjectContentRefusal | None:
    """The refusal for a blob the user did not upload, bound at or beneath a ``file_inject`` node's ``files``."""
    surface = guarded_inline_surface_field(field_path, node_plugins=node_plugins)
    if surface is None or surface[2] != FILE_INJECT_PLUGIN:
        return None
    if is_user_uploaded_blob(created_by=created_by, creation_modality=creation_modality):
        return None
    return FileInjectContentRefusal(surface[0], surface[1], "not_user_uploaded_blob", blob_id)


def file_inject_blob_refusals(
    refs: Iterable[BlobInlineRef],
    records_by_blob_id: Mapping[UUID, BlobRecord],
    *,
    node_plugins: Mapping[str, str],
) -> tuple[FileInjectContentRefusal, ...]:
    """``file_inject_blob_refusal`` for every resolved inline ref, in ref order.

    ``records_by_blob_id`` holds the record already read for each ref's blob,
    so /validate and both run-admission sites judge the record they resolved.
    """
    refusals: list[FileInjectContentRefusal] = []
    for ref in refs:
        record = records_by_blob_id[ref.blob_id]
        refusal = file_inject_blob_refusal(
            ref.field_path,
            blob_id=ref.blob_id,
            node_plugins=node_plugins,
            created_by=record.created_by,
            creation_modality=record.creation_modality,
        )
        if refusal is not None:
            refusals.append(refusal)
    return tuple(refusals)


def file_inject_state_refusals(state: CompositionState) -> tuple[FileInjectContentRefusal, ...]:
    """``file_inject_option_refusals`` over every ``file_inject`` node of a composition state."""
    return tuple(
        refusal
        for node in state.nodes
        if node.plugin == FILE_INJECT_PLUGIN
        for refusal in file_inject_option_refusals(node.id, node.options)
    )


@trust_boundary(
    tier=3,
    source="runtime pipeline config dict generated from web/LLM-authored composition state",
    source_param="config",
    suppresses=(),
    invariant=(
        "returns file_inject_option_refusals for every well-formed file_inject node entry and skips any section "
        "or node of another shape, which settings validation refuses; never raises on config"
    ),
    non_raising=True,
)
def file_inject_runtime_config_refusals(config: Mapping[str, object]) -> tuple[FileInjectContentRefusal, ...]:
    """``file_inject_option_refusals`` over every ``file_inject`` node of a runtime config dict.

    Call it before blob substitution. A ``file_inject`` node whose options are
    not a mapping is left to settings validation, which refuses it.
    """
    refusals: list[FileInjectContentRefusal] = []
    for node in _runtime_config_plugin_nodes(config):
        if node.plugin == FILE_INJECT_PLUGIN and type(node.options) is dict:
            refusals.extend(file_inject_option_refusals(node.name, cast(dict[str, object], node.options)))
    return tuple(refusals)


def file_inject_admission_message(refusals: Collection[FileInjectContentRefusal]) -> str:
    """The run-admission refusal text: each refused path, what was wrong, and what a person does about it."""
    refused = " ".join(f"{refusal.field_path}: {refusal.problem}. {refusal.repair_for_person}" for refusal in refusals)
    return f"In the composer a file_inject entry admits only a file the user uploaded, and no default_content. {refused}"


def _file_inject_validation_error(refusal: FileInjectContentRefusal) -> ValidationError:
    return ValidationError(
        component_id=refusal.node_name,
        component_type="transform",
        message=f"file_inject node '{refusal.node_name}' option '{refusal.option_path}' is refused: {refusal.problem}",
        suggestion=refusal.repair,
        error_code="file_inject_content_not_uploaded",
    )


def _file_inject_validation_failure(
    refusals: Collection[FileInjectContentRefusal], interpretation: InterpretationValidatedState
) -> PhaseFailure:
    detail = "; ".join(f"{refusal.field_path}: {refusal.cause}" for refusal in refusals)
    return PhaseFailure(
        passed_checks=(),
        failed_check=ValidationCheck(name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None),
        errors=tuple(_file_inject_validation_error(refusal) for refusal in refusals),
        readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
        semantic_contracts=interpretation.authored.semantic_contracts,
    )


def _llm_authored_prompt_surface_refs(
    refs: list[BlobInlineRef],
    records_by_blob_id: Mapping[UUID, BlobRecord],
    *,
    node_plugins: Mapping[str, str],
) -> list[BlobInlineRef]:
    """Inline refs that bind an LLM-authored blob into an ``llm`` prompt surface or model.

    Called only after the metadata validation found no violation, so every ref
    is well formed and its blob resolved: ``records_by_blob_id`` holds the
    record that validation read for each one, and no metadata is read again.
    """
    refused: list[BlobInlineRef] = []
    for ref in refs:
        record = records_by_blob_id[ref.blob_id]
        if is_llm_authored_prompt_surface_binding(
            ref.field_path,
            node_plugins=node_plugins,
            creation_modality=record.creation_modality,
        ):
            refused.append(ref)
    return refused


def _llm_authored_prompt_surface_error(ref: BlobInlineRef) -> ValidationError:
    return ValidationError(
        component_id=_blob_inline_component_id(ref.field_path),
        component_type=_blob_inline_component_type(ref.field_path),
        message=(
            f"Inline content blob reference at {ref.field_path} is llm_authored: blob {ref.blob_id} was written by "
            "the composer, and an llm node's prompt template, system prompt, query template or model admits only "
            "user-uploaded blob content, because the prompt-template and model-choice reviews cover those fields"
        ),
        suggestion=(
            "Write the text directly into the option with patch_node_options or upsert_node so its review is "
            "staged, or bind a user-uploaded blob instead."
        ),
        error_code="llm_authored_inline_blob_content",
    )


def materialize_validation_yaml(
    interpretation: InterpretationValidatedState,
    *,
    yaml_generator: YamlGenerator,
    data_dir: Path,
    session_id: str | None,
    mcp_root_authority: McpConfiguredRootAuthority | None = None,
    blob_get_metadata: Callable[[UUID], BlobRecord | None] | None,
    load_yaml: Callable[[str], object],
    blob_get_content: Callable[[UUID], tuple[BlobRecord, bytes]] | None = None,
) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Generate the exact runtime YAML and validate inline-blob metadata.

    A state shape the generator refuses to lower (``PipelineLoweringError``) is an
    authoring defect reachable from any rehydrated session, so it becomes a red
    verdict here rather than a 500. Note the failure lands on
    ``CHECK_BLOB_INLINE_REFS``: the ledger requires a failure to occupy the next
    canonical core check, and materialization's canonical slot is that one — so
    the check NAME is positional, and ``error_code`` is what distinguishes a
    state-shape refusal from an actual blob problem.

    Only that typed error converts. ``resolve_runtime_yaml_paths`` and the
    non-dict check below stay outside the guard, and anything else the generator
    raises is a bug in code ELSPETH owns, which must keep escaping.
    """
    try:
        pipeline_yaml = yaml_generator.generate_yaml(interpretation.materialized_state)
    except PipelineLoweringError as exc:
        detail = f"Pipeline state cannot be materialized to runtime YAML: {exc}"
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_BLOB_INLINE_REFS,
                passed=False,
                detail=detail,
                affected_nodes=(),
                outcome_code=None,
            ),
            errors=(
                ValidationError(
                    component_id=None,
                    component_type=None,
                    message=detail,
                    suggestion="Repair the named node so the field is set — patch it with upsert_node/patch_node_options, or remove the node if it is no longer part of the pipeline.",
                    error_code="state_shape_materialization",
                ),
            ),
            readiness=_blocked_readiness(code="state_shape_materialization", detail=detail),
            semantic_contracts=interpretation.authored.semantic_contracts,
        )
    pipeline_yaml = resolve_runtime_yaml_paths(pipeline_yaml, str(data_dir), session_id=session_id, mcp_root_authority=mcp_root_authority)

    # Judged on the state, before any blob is substituted: after substitution a
    # literal and an attached upload are both strings. Run admission applies the
    # same function to the runtime config.
    file_inject_refusals = file_inject_state_refusals(interpretation.materialized_state)
    if file_inject_refusals:
        return _file_inject_validation_failure(file_inject_refusals, interpretation)

    if "blob_ref" in pipeline_yaml and "inline_content" in pipeline_yaml:
        loaded = load_yaml(pipeline_yaml)
        if type(loaded) is not dict:
            raise TypeError(f"generate_yaml() produced non-dict YAML (got {type(loaded).__name__}) — this is a bug in the YAML generator")
        config_dict = cast(dict[str, object], loaded)
        try:
            refs = _discover_blob_content_refs(config_dict)
        except BlobContentResolutionError as exc:
            malformed = [
                BlobInlineValidationViolation(category="malformed", field_path=field_path, detail=reason)
                for field_path, reason in exc.malformed
            ]
            detail = _blob_inline_validation_detail(malformed)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in malformed),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        if not refs:
            # The YAML substring check also matches literal prompt/table text.
            # Only discovered markers require authorized blob readers.
            return PhaseReport(
                artifact=MaterializedYaml(
                    authored=interpretation.authored,
                    materialized_state=interpretation.materialized_state,
                    pipeline_yaml=pipeline_yaml,
                ),
                checks=(
                    ValidationCheck(
                        name=CHECK_BLOB_INLINE_REFS,
                        passed=True,
                        detail="No inline-content blob references found",
                        affected_nodes=(),
                        outcome_code=None,
                    ),
                ),
            )
        if blob_get_metadata is None:
            unavailable = [
                BlobInlineValidationViolation(
                    category="not_ready", field_path=ref.field_path, detail="authorized blob metadata read is unavailable"
                )
                for ref in refs
            ]
            detail = _blob_inline_validation_detail(unavailable)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in unavailable),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        # The modality refusal below reuses the records this validation reads,
        # so each ref's metadata is fetched once and both checks judge one record.
        resolved_records: dict[UUID, BlobRecord] = {}

        def _recorded_blob_metadata(blob_id: UUID) -> BlobRecord | None:
            if blob_id in resolved_records:
                return resolved_records[blob_id]
            record = blob_get_metadata(blob_id)
            if record is not None:
                resolved_records[blob_id] = record
            return record

        blob_violations = _validate_blob_content_refs_sync(
            _recorded_blob_metadata,
            config_dict,
            per_ref_byte_cap=BLOB_INLINE_PER_REF_BYTE_CAP,
            aggregate_byte_cap=BLOB_INLINE_AGGREGATE_BYTE_CAP,
        )
        if blob_violations:
            detail = _blob_inline_validation_detail(blob_violations)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS,
                    passed=False,
                    detail=detail,
                    affected_nodes=(),
                    outcome_code=None,
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in blob_violations),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        # Readiness parity with run admission (InlineBlobPromptSurfaceAdmissionError):
        # the same predicates refuse an LLM-authored blob in an llm prompt surface
        # or model, and a blob the user did not upload in a file_inject node's
        # files, here, so /validate is not ready for exactly the pipelines whose
        # run would be refused after creation. Node names and plugins are read from the runtime config the
        # refs were discovered in, as run admission reads them.
        node_plugins = runtime_config_node_plugins(config_dict)
        file_inject_blob_refs = file_inject_blob_refusals(refs, resolved_records, node_plugins=node_plugins)
        if file_inject_blob_refs:
            return _file_inject_validation_failure(file_inject_blob_refs, interpretation)
        llm_authored_refs = _llm_authored_prompt_surface_refs(refs, resolved_records, node_plugins=node_plugins)
        if llm_authored_refs:
            detail = "; ".join(f"{ref.field_path}: llm_authored" for ref in llm_authored_refs)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS,
                    passed=False,
                    detail=detail,
                    affected_nodes=(),
                    outcome_code=None,
                ),
                errors=tuple(_llm_authored_prompt_surface_error(ref) for ref in llm_authored_refs),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        if blob_get_content is None:
            unavailable = [
                BlobInlineValidationViolation(
                    category="not_ready", field_path=ref.field_path, detail="authorized blob content read is unavailable"
                )
                for ref in refs
            ]
            detail = _blob_inline_validation_detail(unavailable)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in unavailable),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        fetched: dict[BlobInlineRef, bytes] = {}
        content_by_blob_id: dict[UUID, bytes] = {}
        content_failure_by_blob_id: dict[UUID, BlobInlineValidationViolation] = {}
        content_violations: list[BlobInlineValidationViolation] = []
        actual_total_bytes = 0
        for ref in refs:
            if ref.blob_id in content_failure_by_blob_id:
                previous_failure = content_failure_by_blob_id[ref.blob_id]
                content_violations.append(
                    BlobInlineValidationViolation(
                        category=previous_failure.category, field_path=ref.field_path, detail=previous_failure.detail
                    )
                )
                continue
            if ref.blob_id not in content_by_blob_id:
                try:
                    content_record, content = blob_get_content(ref.blob_id)
                except BlobNotFoundError:
                    failure = BlobInlineValidationViolation(category="missing", field_path=ref.field_path, detail="blob not found")
                    content_failure_by_blob_id[ref.blob_id] = failure
                    content_violations.append(failure)
                    continue
                except BlobStateError:
                    failure = BlobInlineValidationViolation(category="not_ready", field_path=ref.field_path, detail="blob is not ready")
                    content_failure_by_blob_id[ref.blob_id] = failure
                    content_violations.append(failure)
                    continue
                metadata_record = resolved_records[ref.blob_id]
                if (
                    content_record.id != metadata_record.id
                    or content_record.session_id != metadata_record.session_id
                    or content_record.status != "ready"
                    or content_record.content_hash != metadata_record.content_hash
                    or content_record.mime_type != metadata_record.mime_type
                    or content_record.size_bytes != metadata_record.size_bytes
                    or len(content) != metadata_record.size_bytes
                    or content_record.creation_modality != metadata_record.creation_modality
                ):
                    failure = BlobInlineValidationViolation(
                        category="not_ready", field_path=ref.field_path, detail="blob changed during validation"
                    )
                    content_failure_by_blob_id[ref.blob_id] = failure
                    content_violations.append(failure)
                    continue
                content_by_blob_id[ref.blob_id] = content
            content = content_by_blob_id[ref.blob_id]
            if len(content) > BLOB_INLINE_PER_REF_BYTE_CAP:
                content_violations.append(
                    BlobInlineValidationViolation(
                        category="oversized",
                        field_path=ref.field_path,
                        detail=f"{len(content)} bytes exceeds per-ref cap {BLOB_INLINE_PER_REF_BYTE_CAP}",
                    )
                )
            actual_total_bytes += len(content)
            fetched[ref] = content
        if actual_total_bytes > BLOB_INLINE_AGGREGATE_BYTE_CAP:
            content_violations.append(
                BlobInlineValidationViolation(
                    category="oversized",
                    field_path="(aggregate)",
                    detail=f"total resolved bytes {actual_total_bytes} exceeds aggregate cap {BLOB_INLINE_AGGREGATE_BYTE_CAP}",
                )
            )
        if content_violations:
            detail = _blob_inline_validation_detail(content_violations)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in content_violations),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        try:
            resolved_config = _substitute_blob_content_refs_for_validation(
                config_dict,
                fetched,
                refs=refs,
                blob_metadata={
                    record.id: (cast(AllowedMimeType, record.mime_type), len(content_by_blob_id[record.id]))
                    for record in resolved_records.values()
                },
            )
        except BlobContentResolutionError as exc:
            decode_violations = [
                BlobInlineValidationViolation(category="malformed", field_path=field_path, detail=f"cannot decode as {encoding}")
                for field_path, encoding in exc.undecodable
            ]
            detail = _blob_inline_validation_detail(decode_violations)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_BLOB_INLINE_REFS, passed=False, detail=detail, affected_nodes=(), outcome_code=None
                ),
                errors=tuple(_blob_inline_validation_error(violation) for violation in decode_violations),
                readiness=_blocked_readiness(code="blob_inline_refs", detail=detail),
                semantic_contracts=interpretation.authored.semantic_contracts,
            )
        check_detail = "All inline-content blob references and bytes are valid"
        pipeline_yaml = yaml.dump(resolved_config, default_flow_style=False)
    else:
        check_detail = "No inline-content blob references found"

    return PhaseReport(
        artifact=MaterializedYaml(
            authored=interpretation.authored,
            materialized_state=interpretation.materialized_state,
            pipeline_yaml=pipeline_yaml,
        ),
        checks=(
            ValidationCheck(
                name=CHECK_BLOB_INLINE_REFS,
                passed=True,
                detail=check_detail,
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_llm_retry_budget_policy(materialized: MaterializedYaml) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject unsafe sequential multi-query LLM retry budgets.

    Subject set is every LLM-capable plugin-bearing node, regardless of node
    kind (elspeth-df8082552d, elspeth-c7626ae109). Note this gate carries its
    OWN node walk — it is not fed by
    ``_llm_policy_components`` despite gating the same plugin, so widening
    that helper alone would have left this site behind.
    """
    for node in materialized.authored.policy.state.nodes:
        if not node_has_capability(node, PluginCapability.LLM):
            continue
        policy_error = web_llm_retry_budget_policy_error(node.options)
        if policy_error is None:
            continue
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_LLM_RETRY_BUDGET_POLICY,
                passed=False,
                detail=f"{node.node_type.capitalize()} '{node.id}' uses disallowed sequential multi-query LLM retry budget",
                affected_nodes=(node.id,),
                outcome_code=None,
            ),
            errors=(
                ValidationError(
                    component_id=node.id,
                    component_type="transform",
                    message=policy_error,
                    suggestion="Set max_capacity_retry_seconds to a small positive value or configure pool_size > 1 for pooled retry handling.",
                    error_code=None,
                ),
            ),
            readiness=_blocked_readiness(
                code=CHECK_LLM_RETRY_BUDGET_POLICY,
                detail=f"{node.node_type} {node.id} uses an unsafe sequential multi-query LLM retry budget",
                component_id=node.id,
                component_type="transform",
            ),
            semantic_contracts=materialized.authored.semantic_contracts,
        )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_LLM_RETRY_BUDGET_POLICY,
                passed=True,
                detail="No unsafe web-authored sequential multi-query LLM retry budget",
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_llm_base_url_policy(materialized: MaterializedYaml) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject web-authored OpenRouter base URL overrides."""
    # Profile lowering can inject a validated operator endpoint. This author
    # policy must inspect the retained original options, not that binding.
    for component in _llm_policy_components(materialized.authored.policy.authored_state):
        policy_error = web_llm_base_url_policy_error(component.options)
        if policy_error is None:
            continue
        policy_message = policy_error if component.component_type == "transform" else policy_error.replace("LLM nodes", "LLM sources")
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_LLM_BASE_URL_POLICY,
                passed=False,
                detail=f"{component.label} '{component.component_id}' overrides OpenRouter base_url in a web-authored pipeline",
                affected_nodes=(component.component_id,),
                outcome_code=None,
            ),
            errors=(
                ValidationError(
                    component_id=component.component_id,
                    component_type=component.component_type,
                    message=policy_message,
                    suggestion="Remove the base_url option to use the canonical OpenRouter endpoint.",
                    error_code="llm_base_url_not_allowed",
                ),
            ),
            readiness=_blocked_readiness(
                code=CHECK_LLM_BASE_URL_POLICY,
                detail=f"{component.component_type} {component.component_id} overrides OpenRouter base_url in a web-authored pipeline",
                component_id=component.component_id,
                component_type=component.component_type,
            ),
            semantic_contracts=materialized.authored.semantic_contracts,
        )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_LLM_BASE_URL_POLICY,
                passed=True,
                detail="No web-authored OpenRouter base_url override",
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_llm_tracing_policy(materialized: MaterializedYaml) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject author-controlled LLM tracing configuration."""
    for component in _llm_policy_components(materialized.authored.policy.state):
        policy_error = web_llm_tracing_policy_error(component.options)
        if policy_error is None:
            continue
        policy_message = policy_error if component.component_type == "transform" else policy_error.replace("LLM nodes", "LLM sources")
        return PhaseFailure(
            passed_checks=(),
            failed_check=ValidationCheck(
                name=CHECK_LLM_TRACING_POLICY,
                passed=False,
                detail=f"{component.label} '{component.component_id}' configures tracing in a web-authored pipeline",
                affected_nodes=(component.component_id,),
                outcome_code=None,
            ),
            errors=(
                ValidationError(
                    component_id=component.component_id,
                    component_type=component.component_type,
                    message=policy_message,
                    suggestion="Remove tracing; tracing destinations and credentials are operator-controlled.",
                    error_code="llm_tracing_not_allowed",
                ),
            ),
            readiness=_blocked_readiness(
                code=CHECK_LLM_TRACING_POLICY,
                detail=f"{component.component_type} {component.component_id} configures tracing in a web-authored pipeline",
                component_id=component.component_id,
                component_type=component.component_type,
            ),
            semantic_contracts=materialized.authored.semantic_contracts,
        )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_LLM_TRACING_POLICY,
                passed=True,
                detail="No web-authored LLM tracing configuration",
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_aws_s3_endpoint_url_policy(
    materialized: MaterializedYaml,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Reject author-controlled AWS S3 endpoints for web principals."""
    policy_state = materialized.authored.policy.state
    if not plugin_snapshot.is_trained_operator:
        for source_name, source in policy_state.sources.items():
            policy_error = web_aws_s3_endpoint_url_policy_error(source.plugin, source.options)
            if policy_error is None:
                continue
            source_component = _source_policy_component_id(source_name)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                    passed=False,
                    detail=f"Source '{source_name}' sets aws_s3 endpoint_url in a web-authored pipeline",
                    affected_nodes=(source_component,),
                    outcome_code=None,
                ),
                errors=(
                    ValidationError(
                        component_id=source_component,
                        component_type="source",
                        message=policy_error,
                        suggestion="Remove endpoint_url and use operator-controlled AWS configuration.",
                        error_code="aws_s3_endpoint_url_not_allowed",
                    ),
                ),
                readiness=_blocked_readiness(
                    code=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                    detail=f"source {source_component} sets aws_s3 endpoint_url in a web-authored pipeline",
                    component_id=source_component,
                    component_type="source",
                ),
                semantic_contracts=materialized.authored.semantic_contracts,
            )

        for output in policy_state.outputs:
            policy_error = web_aws_s3_endpoint_url_policy_error(output.plugin, output.options)
            if policy_error is None:
                continue
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                    passed=False,
                    detail=f"Sink '{output.name}' sets aws_s3 endpoint_url in a web-authored pipeline",
                    affected_nodes=(output.name,),
                    outcome_code=None,
                ),
                errors=(
                    ValidationError(
                        component_id=output.name,
                        component_type="sink",
                        message=policy_error,
                        suggestion="Remove endpoint_url and use operator-controlled AWS configuration.",
                        error_code="aws_s3_endpoint_url_not_allowed",
                    ),
                ),
                readiness=_blocked_readiness(
                    code=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                    detail=f"sink {output.name} sets aws_s3 endpoint_url in a web-authored pipeline",
                    component_id=output.name,
                    component_type="sink",
                ),
                semantic_contracts=materialized.authored.semantic_contracts,
            )

    detail = (
        "No web-authored aws_s3 endpoint_url override"
        if not plugin_snapshot.is_trained_operator
        else "Local trained-operator validation is exempt from the web aws_s3 endpoint_url policy"
    )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_AWS_S3_ENDPOINT_URL_POLICY,
                passed=True,
                detail=detail,
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


def validate_aws_s3_source_policy(
    materialized: MaterializedYaml,
    *,
    plugin_snapshot: PluginAvailabilitySnapshot,
) -> PhaseReport[MaterializedYaml] | PhaseFailure:
    """Require profile-derived authority for every Web-authored S3 source."""
    profiled_source = False
    if not plugin_snapshot.is_trained_operator:
        source_id = PluginId("source", "aws_s3")
        operator_profile_available = source_id in plugin_snapshot.available
        for source_name, source in materialized.authored.policy.state.sources.items():
            policy_error = web_aws_s3_source_policy_error(
                source.plugin,
                operator_profile_available=operator_profile_available,
            )
            if policy_error is None:
                profiled_source = profiled_source or source.plugin == "aws_s3"
                continue
            source_component = _source_policy_component_id(source_name)
            return PhaseFailure(
                passed_checks=(),
                failed_check=ValidationCheck(
                    name=CHECK_AWS_S3_SOURCE_POLICY,
                    passed=False,
                    detail=f"Source '{source_name}' uses aws_s3 in a web-authored pipeline",
                    affected_nodes=(source_component,),
                    outcome_code=None,
                ),
                errors=(
                    ValidationError(
                        component_id=source_component,
                        component_type="source",
                        message=policy_error,
                        suggestion="Ask an operator to configure an S3 source profile, or use batch/CLI for raw S3 options.",
                        error_code="aws_s3_source_profile_required",
                    ),
                ),
                readiness=_blocked_readiness(
                    code=CHECK_AWS_S3_SOURCE_POLICY,
                    detail=f"source {source_component} uses aws_s3 in a web-authored pipeline",
                    component_id=source_component,
                    component_type="source",
                ),
                semantic_contracts=materialized.authored.semantic_contracts,
            )

    detail = (
        (
            "Web-authored aws_s3 source is bound through an available operator profile"
            if profiled_source
            else "Web-authored pipeline does not use an aws_s3 source"
        )
        if not plugin_snapshot.is_trained_operator
        else "Local trained-operator validation is exempt from the web aws_s3 source policy"
    )
    return PhaseReport(
        artifact=_snapshot_materialized_evidence(materialized),
        checks=(
            ValidationCheck(
                name=CHECK_AWS_S3_SOURCE_POLICY,
                passed=True,
                detail=detail,
                affected_nodes=(),
                outcome_code=None,
            ),
        ),
    )


__all__ = [
    "materialize_validation_yaml",
    "validate_aws_s3_endpoint_url_policy",
    "validate_aws_s3_source_policy",
    "validate_llm_base_url_policy",
    "validate_llm_retry_budget_policy",
    "validate_llm_tracing_policy",
]
