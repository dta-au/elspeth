"""Shared toolkit for the composer-tool plane modules.

Centralises:

- ``ToolResult`` (the canonical response shape every handler returns) and the
  leaf response helpers (``_failure_result`` / ``_discovery_result`` /
  ``_mutation_result`` / ``_rejection_only_validation`` / ``_attach_post_call_hints``).
- Validation-delta and graph-repair-suggestion synthesis used by
  ``ToolResult.to_dict`` and the high-level ``diff_states`` reporter.
- The Pydantic mutation-argument validator and merge-patch helper used by every
  per-resource mutation handler.
- Base serialisation helpers for source/node/output/edge — leaves that the
  repair-suggestion generator and downstream pipeline-state serialiser both consume.
- The TypedDicts shared across pipeline-state, edge-contract, and repair payloads.

Layer: L3 (application). Imports from L0 contracts and the ``web.composer.state`` /
``web.composer.protocol`` / ``web.catalog.protocol`` / ``web.execution.schemas``
surfaces only — no sibling-plane imports.
"""

from __future__ import annotations

# Slice 4 — additional imports for shared validation/repair helpers.
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Final, NotRequired, TypedDict, cast

from pydantic import BaseModel, JsonValue
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import Engine

from elspeth.contracts.blobs_inline import is_widened_blob_ref
from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.secrets import WebSecretResolver
from elspeth.contracts.sink import FILE_SINK_PLUGINS, FILE_SINK_REPAIR_EXTENSIONS
from elspeth.core.config import TriggerConfig
from elspeth.core.secrets import (
    collect_credential_field_violations,
    collect_disallowed_secret_ref_markers,
    parse_secret_ref_marker,
    redact_secret_refs_for_validation,
)
from elspeth.engine.orchestrator.preflight import check_config_value_sources
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.validation import (
    UnknownPluginTypeError,
    get_sink_config_model,
    get_source_config_model,
    get_transform_config_model,
)
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.protocol import CatalogService, PluginKind
from elspeth.web.catalog.schemas import PluginSchemaInfo, PluginSummary
from elspeth.web.composer.plugin_policy_disclosure import (
    WEB_PROHIBITED_PLUGIN_EXPLANATION,
    ProhibitedPluginDisclosure,
    prohibited_plugin_section,
)
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import redact_source_storage_path
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    NodeType,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
    ValidationEntry,
    ValidationSummary,
    _coalesce_branch_connections,
    _coalesce_branch_names,
    _serialize_branches,
)
from elspeth.web.execution.schemas import ValidationResult
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
    SOURCE_AUTHORING_KEY,
    SOURCE_COMPONENT_ID,
    InterpretationRequirement,
    ServerStagedRequiredControlUserTerm,
    composer_pipeline_decision_user_term_error,
    parse_interpretation_requirements,
    serialize_authoring_review_options,
    source_name_from_component_id,
    strip_authoring_options,
)
from elspeth.web.paths import (
    NESTED_LOCAL_PATH_OPTION_KEYS,
    SINK_LOCAL_PATH_OPTION_KEYS,
    SOURCE_LOCAL_PATH_OPTION_KEYS,
    allowed_sink_directories,
    allowed_source_directories,
    resolve_data_path,
    resolve_sink_data_path,
)
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId, PluginUnavailableReason
from elspeth.web.provider_config_policy import web_llm_retry_budget_policy_error, web_rag_provider_config_policy_error
from elspeth.web.secrets.ref_policy import (
    allowed_secret_ref_fields,
    allowed_secret_ref_fields_text,
)
from elspeth.web.validation import (
    INTERPRETATION_PLACEHOLDER_RE,
)

_FULL_STATE_COMPONENT_ALIASES: Final[tuple[str, ...]] = ("", "full", "all", "pipeline")
_FULL_STATE_COMPONENT_ALIAS_SET: Final[frozenset[str]] = frozenset(_FULL_STATE_COMPONENT_ALIASES)
_DATA_ERROR_KEY: Final[str] = "error"
_RUNTIME_OWNED_LLM_OPTION_KEYS: Final[frozenset[str]] = frozenset({"resolved_prompt_template_hash"})
_RESOLVER_OWNED_INTERPRETATION_REQUIREMENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "status",
        "event_id",
        "accepted_value",
        "accepted_artifact_hash",
        "resolved_prompt_template_hash",
    }
)
_AUTHOR_OWNED_INTERPRETATION_REQUIREMENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "kind",
        "user_term",
        "draft",
    }
)
_CANONICAL_INTERPRETATION_REQUIREMENT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "id",
        "kind",
        "user_term",
        "draft",
        "status",
        "event_id",
        "accepted_value",
        "accepted_artifact_hash",
        "resolved_prompt_template_hash",
    }
)


def _authored_interpretation_requirement_id(
    *,
    component_id: str,
    user_term: str,
    source: bool = False,
) -> str:
    """Project one admitted author shell onto its canonical server-owned ID."""
    normalized_user_term = user_term.strip()
    return f"source_review:{normalized_user_term}" if source else f"{normalized_user_term}:{component_id}"


def _source_review_requirement_id(user_term: str) -> str:
    """Return the canonical source-review ID for one normalized review term."""
    return _authored_interpretation_requirement_id(
        component_id="source",
        user_term=user_term,
        source=True,
    )


def _prompt_template_review_requirement_id(node_id: str) -> str:
    """Return the canonical ID used by prompt-template auto-staging."""
    return f"prompt_template_review:{node_id}"


def _model_choice_review_requirement_id(node_id: str) -> str:
    """Return the canonical ID used by model-choice auto-staging."""
    return f"model_choice_review:{node_id}"


def _pending_interpretation_requirement(
    *,
    requirement_id: str,
    kind: InterpretationKind,
    user_term: str,
    draft: str,
) -> InterpretationRequirement:
    """Return a pending interpretation-review requirement row."""
    requirement: InterpretationRequirement = {
        "id": requirement_id,
        "kind": kind.value,
        "user_term": user_term,
        "status": "pending",
        "draft": draft,
        "event_id": None,
        "accepted_value": None,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }
    return requirement


def _requirement_matches_field_value(requirement: Mapping[str, Any], field_value: str) -> bool:
    """True when ``requirement``'s draft/resolved hash already matches ``field_value``.

    Polymorphic by status: pending requirements carry the raw ``draft``
    string and compare equal; resolved requirements carry the stable hash
    of the accepted value in ``resolved_prompt_template_hash`` (a
    historical field name retained across kinds — the field is the
    universal resolved-value hash, not prompt-template-specific).
    """
    status = requirement["status"] if "status" in requirement else None
    if status == "pending":
        return requirement.get("draft") == field_value
    if status != "resolved":
        return False
    return requirement.get("resolved_prompt_template_hash") == stable_hash(field_value)


def _trusted_requirement_id_for_kind(
    existing_options: Mapping[str, Any] | None,
    kind: InterpretationKind,
) -> str | None:
    """Return one unambiguous trusted current ID for an auto-staged kind."""
    if existing_options is None:
        return None
    try:
        existing_requirements = parse_interpretation_requirements(existing_options)
    except (KeyError, TypeError, ValueError):
        return None
    if existing_requirements is None:
        return None
    matching_ids = [requirement["id"] for requirement in existing_requirements if requirement["kind"] == kind.value]
    return matching_ids[0] if len(matching_ids) == 1 else None


def _options_with_pending_requirement(
    options: Mapping[str, Any],
    *,
    requirement: Mapping[str, Any],
    replace_kind: InterpretationKind | None = None,
    current_field_value: str | None = None,
    existing_options: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Append or refresh a pending requirement without mutating ``options``.

    When ``replace_kind`` matches an existing requirement and
    ``current_field_value`` already equals that requirement's draft (or
    resolved hash), the existing requirement is kept — the call is
    idempotent so re-issuing the same mutation does not churn the review
    state. Otherwise the existing requirement of ``replace_kind`` is
    replaced with the supplied one.
    """
    requirements_value = options[INTERPRETATION_REQUIREMENTS_KEY] if INTERPRETATION_REQUIREMENTS_KEY in options else None
    if requirements_value is not None and not isinstance(requirements_value, (list, tuple)):
        return dict(options)

    if replace_kind is not None:
        trusted_id = _trusted_requirement_id_for_kind(existing_options, replace_kind)
        if trusted_id is not None:
            requirement = {**requirement, "id": trusted_id}

    requirements: list[Any] = list(requirements_value or ())
    if replace_kind is not None:
        next_requirements: list[Any] = []
        replaced = False
        for existing in requirements:
            if not isinstance(existing, Mapping) or existing.get("kind", InterpretationKind.VAGUE_TERM.value) != replace_kind.value:
                next_requirements.append(existing)
                continue
            if current_field_value is not None and _requirement_matches_field_value(existing, current_field_value):
                next_requirements.append(existing)
                replaced = True
                continue
            next_requirements.append(requirement)
            replaced = True
        if replaced:
            patched = dict(options)
            patched[INTERPRETATION_REQUIREMENTS_KEY] = next_requirements
            return patched

    requirements.append(requirement)
    patched = dict(options)
    patched[INTERPRETATION_REQUIREMENTS_KEY] = requirements
    return patched


def _options_with_default_prompt_template_review(
    *,
    node_id: str,
    plugin: str | None,
    options: Mapping[str, Any],
    existing_options: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Ensure LLM-authored prompt templates carry a Class 3 review gate."""
    if plugin != "llm":
        return options
    prompt_template = options["prompt_template"] if "prompt_template" in options else None
    if not isinstance(prompt_template, str) or not prompt_template:
        return options
    requirement = _pending_interpretation_requirement(
        requirement_id=_prompt_template_review_requirement_id(node_id),
        kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
        user_term=f"llm_prompt_template:{node_id}",
        draft=prompt_template,
    )
    return _options_with_pending_requirement(
        options,
        requirement=requirement,
        replace_kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
        current_field_value=prompt_template,
        existing_options=existing_options,
    )


def _options_with_default_model_choice_review(
    *,
    node_id: str,
    plugin: str | None,
    options: Mapping[str, Any],
    existing_options: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Ensure LLM-authored model choices carry a review gate.

    Algorithmic enforcement of the "every model choice must be surfaced
    to the user" contract — every state mutation that sets ``options.model``
    on an LLM node MUST stage a pending interpretation requirement of
    kind ``llm_model_choice``. Mirrors
    :func:`_options_with_default_prompt_template_review` so the composer
    cannot ship a model identifier without surfacing it for review.

    The surfacing is provider-agnostic and unconditional. Whether the
    model id is also enforced by a live catalog (currently OpenRouter
    via ``CatalogValueSource`` in
    ``elspeth.plugins.transforms.llm.providers.openrouter``) is decided
    separately by the validator at preflight time — the operator's
    directive is "reliable surfacing", so we surface for every provider
    including operator-overridden ``base_url`` (chaos servers, private
    OpenAI-compatible gateways). The catalog SHA the choice was made
    against is captured separately on the audit ``runs`` row at
    execution time (``openrouter_catalog_sha256``), not here — that
    keeps the requirement shape symmetric with ``llm_prompt_template``
    (``draft == options.<field>`` invariant) and avoids leaking
    provider-specific metadata into a provider-agnostic surface.
    """
    if plugin != "llm":
        return options
    model = options["model"] if "model" in options else None
    if not isinstance(model, str) or not model:
        return options
    requirement = _pending_interpretation_requirement(
        requirement_id=_model_choice_review_requirement_id(node_id),
        kind=InterpretationKind.LLM_MODEL_CHOICE,
        user_term=f"llm_model_choice:{node_id}",
        draft=model,
    )
    return _options_with_pending_requirement(
        options,
        requirement=requirement,
        replace_kind=InterpretationKind.LLM_MODEL_CHOICE,
        current_field_value=model,
        existing_options=existing_options,
    )


# Typographic punctuation an LLM routinely emits, mapped to its ASCII equivalent.
# web_scrape's ``http.scraping_reason`` / ``http.abuse_contact`` are sent verbatim
# as the X-Scraping-Reason / X-Abuse-Contact request headers, which must be
# ASCII-encodable (WebScrapeHTTPConfig). Folding the common typographic cases here
# lets composer-built pipelines (the first-run tutorial) round-trip; characters
# with no ASCII mapping are left untouched so the WebScrapeHTTPConfig validator
# still rejects them as a configuration error on hand-authored / YAML configs.
_TYPOGRAPHIC_TO_ASCII = {
    "\u2010": "-",  # hyphen
    "\u2011": "-",  # non-breaking hyphen
    "\u2012": "-",  # figure dash
    "\u2013": "-",  # en dash
    "\u2014": "-",  # em dash
    "\u2015": "-",  # horizontal bar
    "\u2018": "'",  # left single quotation mark
    "\u2019": "'",  # right single quotation mark / apostrophe
    "\u201a": "'",  # single low-9 quotation mark
    "\u201b": "'",  # single high-reversed-9 quotation mark
    "\u201c": '"',  # left double quotation mark
    "\u201d": '"',  # right double quotation mark
    "\u201e": '"',  # double low-9 quotation mark
    "\u201f": '"',  # double high-reversed-9 quotation mark
    "\u2032": "'",  # prime
    "\u2033": '"',  # double prime
    "\u2026": "...",  # horizontal ellipsis
    "\u00a0": " ",  # no-break space
    "\u2009": " ",  # thin space
    "\u202f": " ",  # narrow no-break space
}
_TYPOGRAPHIC_TRANSLATION = str.maketrans(_TYPOGRAPHIC_TO_ASCII)

_WIRE_VISIBLE_SCRAPE_HEADER_FIELDS = ("scraping_reason", "abuse_contact")


def _options_with_ascii_safe_scrape_headers(
    *,
    plugin: str | None,
    options: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Fold common typographic punctuation to ASCII in web_scrape header fields.

    No-op unless ``plugin == "web_scrape"`` and a header value actually changes,
    so it is safe to compose for every node. Only the wire-visible header fields
    are touched (a scrape node's prompt-like fields, and every other plugin's
    body text, are left alone). Characters with no ASCII mapping are preserved —
    the ``WebScrapeHTTPConfig`` validator rejects those as a configuration error.
    """
    if plugin != "web_scrape":
        return options
    http = options.get("http")
    if not isinstance(http, Mapping):
        return options
    folded_http: dict[str, Any] | None = None
    for header_field in _WIRE_VISIBLE_SCRAPE_HEADER_FIELDS:
        value = http.get(header_field)
        if not isinstance(value, str):
            continue
        folded = value.translate(_TYPOGRAPHIC_TRANSLATION)
        if folded != value:
            if folded_http is None:
                folded_http = dict(http)
            folded_http[header_field] = folded
    if folded_http is None:
        return options
    new_options = dict(options)
    new_options["http"] = folded_http
    return new_options


def _options_with_default_llm_reviews(
    *,
    node_id: str,
    plugin: str | None,
    options: Mapping[str, Any],
    existing_options: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Apply every default review auto-stager for an LLM node, in order.

    Composes the per-field auto-stagers (prompt template, model choice) plus the
    web_scrape wire-visible-header ASCII fold, so call sites do not have to
    remember the full set. Each individual helper is a no-op when its trigger
    condition doesn't hold (non-llm plugin, missing field, non-scrape plugin), so
    the composition is safe for non-llm nodes and for partial node options.

    Adding a new default auto-stager here is the canonical extension point —
    callers stay on the composite and acquire the new gate automatically.
    """
    staged = _options_with_default_prompt_template_review(
        node_id=node_id,
        plugin=plugin,
        options=options,
        existing_options=existing_options,
    )
    staged = _options_with_default_model_choice_review(
        node_id=node_id,
        plugin=plugin,
        options=staged,
        existing_options=existing_options,
    )
    staged = _options_with_ascii_safe_scrape_headers(plugin=plugin, options=staged)
    return staged


class _SemanticEdgeContractPayload(TypedDict):
    """Wire shape for a serialized SemanticEdgeContract.

    Mirrors composer_mcp.server._SemanticEdgeContractPayload and
    web.execution.schemas.SemanticEdgeContractResponse exactly so HTTP,
    MCP, and ToolResult surfaces stay identical modulo transport
    envelope. If a field changes here, change it in all three places.
    """

    from_id: str
    to_id: str
    consumer_plugin: str
    producer_plugin: str | None
    producer_field: str
    consumer_field: str
    outcome: str
    requirement_code: str


class _FullPipelineStateMetadataPayload(TypedDict):
    """Metadata payload nested in full get_pipeline_state responses."""

    name: str | None
    description: str | None


class _FullPipelineStateInspectionPayload(TypedDict):
    """Inspection payload documenting how a full-state alias resolved."""

    requested_component: Any
    resolved_component: str
    accepted_full_state_aliases: list[str]


class _FullPipelineStatePayload(TypedDict):
    """Full-state payload returned by get_pipeline_state."""

    sources: dict[str, dict[str, Any]]
    nodes: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    metadata: _FullPipelineStateMetadataPayload
    version: int
    inspection: _FullPipelineStateInspectionPayload


class _RepairToolCall(TypedDict):
    tool: str
    arguments: Mapping[str, object]


class _AffectedConsumer(TypedDict):
    id: str
    current_input: str
    new_input: str


class _GraphRepairSuggestion(TypedDict):
    code: str
    connection: str
    strategy: str
    reason: str
    affected_consumers: list[_AffectedConsumer]
    tool_sequence: list[_RepairToolCall]


def _semantic_contracts_payload(
    contracts: tuple[Any, ...],
) -> list[_SemanticEdgeContractPayload]:
    """Serialize a SemanticEdgeContract tuple to JSON-friendly dicts.

    Centralized so ToolResult.to_dict and _execute_preview_pipeline
    emit identical shapes — and so adding a field updates both
    surfaces in one place.

    SemanticEdgeContract intentionally has no .to_dict() of its own:
    serialization happens at consumption sites so L0 stays free of
    JSON-encoding concerns. (See composer_mcp/server.py for the same
    pattern.)
    """
    return [
        _SemanticEdgeContractPayload(
            from_id=sc.from_id,
            to_id=sc.to_id,
            consumer_plugin=sc.consumer_plugin,
            producer_plugin=sc.producer_plugin,
            producer_field=sc.producer_field,
            consumer_field=sc.consumer_field,
            outcome=sc.outcome.value,
            requirement_code=sc.requirement.requirement_code,
        )
        for sc in contracts
    ]


def _compute_validation_delta(
    before: ValidationSummary,
    after: ValidationSummary,
) -> dict[str, Any]:
    """Compute new/resolved entries between two validation states.

    Compares by (component, message) tuple since ValidationEntry
    instances are recreated on each validate() call (no stable identity).
    """
    before_errors = {(e.component, e.message) for e in before.errors}
    after_errors = {(e.component, e.message) for e in after.errors}
    before_warnings = {(w.component, w.message) for w in before.warnings}
    after_warnings = {(w.component, w.message) for w in after.warnings}

    new_errors = [e.to_dict() for e in after.errors if (e.component, e.message) not in before_errors]
    resolved_errors = [e.to_dict() for e in before.errors if (e.component, e.message) not in after_errors]
    new_warnings = [w.to_dict() for w in after.warnings if (w.component, w.message) not in before_warnings]
    resolved_warnings = [w.to_dict() for w in before.warnings if (w.component, w.message) not in after_warnings]

    return {
        "new_errors": new_errors,
        "resolved_errors": resolved_errors,
        "new_warnings": new_warnings,
        "resolved_warnings": resolved_warnings,
    }


def _repair_identifier_fragment(value: str, *, fallback: str) -> str:
    """Return a connection-safe identifier fragment for generated repair skeletons."""
    fragment = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_-")
    if not fragment:
        return fallback
    if not fragment[0].isalnum():
        return f"{fallback}_{fragment}"
    return fragment


def _unique_name(candidate: str, reserved: set[str]) -> str:
    """Return candidate or a suffixed variant that does not collide with reserved."""
    if candidate not in reserved:
        reserved.add(candidate)
        return candidate

    index = 2
    while f"{candidate}_{index}" in reserved:
        index += 1
    unique = f"{candidate}_{index}"
    reserved.add(unique)
    return unique


def _reserved_connection_names(state: CompositionState) -> set[str]:
    """Collect existing route/connection/sink names a repair branch must avoid."""
    names: set[str] = {output.name for output in state.outputs}
    for source in state.sources.values():
        names.add(source.on_success)
        if source.on_validation_failure != "discard":
            names.add(source.on_validation_failure)

    for node in state.nodes:
        names.add(node.input)
        if node.on_success is not None:
            names.add(node.on_success)
        if node.on_error is not None and node.on_error != "discard":
            names.add(node.on_error)
        if node.routes is not None:
            names.update(node.routes.values())
        if node.fork_to is not None:
            names.update(node.fork_to)
        if node.branches is not None:
            names.update(_coalesce_branch_names(node.branches))
            names.update(_coalesce_branch_connections(node.branches))
    return names


def _duplicate_consumer_repair_suggestions(
    state: CompositionState,
    validation: ValidationSummary,
) -> list[_GraphRepairSuggestion]:
    """Build copyable repair skeletons for duplicate-consumer validation failures."""
    duplicate_error_components = {
        error.component
        for error in validation.errors
        if error.component.startswith("connection:") and error.message.startswith("Duplicate consumer for connection ")
    }
    if not duplicate_error_components:
        return []

    # ``branch_alias`` is None for an ordinary ``node.input`` consumer and
    # names the branch slot for a row_union consumer. The row_union's own
    # ``input`` is only an adapter placeholder and must never be repaired as
    # though it were an independent consumption edge.
    consumers_by_connection: dict[str, list[tuple[NodeSpec, str | None]]] = {}
    for node in state.nodes:
        if node.node_type in ("coalesce", "queue", "row_union"):
            continue
        consumers_by_connection.setdefault(node.input, []).append((node, None))
    for node in state.nodes:
        if node.node_type != "row_union":
            continue
        for row_union_branch_alias, branch_connection in zip(
            _coalesce_branch_names(node.branches),
            _coalesce_branch_connections(node.branches),
            strict=True,
        ):
            if row_union_branch_alias == branch_connection:
                continue
            consumers_by_connection.setdefault(branch_connection, []).append((node, row_union_branch_alias))

    reserved_node_ids = {node.id for node in state.nodes}
    reserved_connection_names = _reserved_connection_names(state)
    suggestions: list[_GraphRepairSuggestion] = []

    for connection_name, consumer_nodes in sorted(consumers_by_connection.items()):
        if len(consumer_nodes) < 2 or f"connection:{connection_name}" not in duplicate_error_components:
            continue

        connection_fragment = _repair_identifier_fragment(connection_name, fallback="connection")
        gate_id = _unique_name(f"fork_{connection_fragment}", reserved_node_ids)
        branch_names = [
            _unique_name(
                f"{connection_fragment}_to_{_repair_identifier_fragment(binding[0].id, fallback='node')}",
                reserved_connection_names,
            )
            for binding in consumer_nodes
        ]
        gate_args: dict[str, object] = {
            "id": gate_id,
            "node_type": "gate",
            "plugin": None,
            "input": connection_name,
            "on_success": None,
            "on_error": None,
            "options": {},
            "condition": "True",
            "routes": {"true": "fork", "false": "fork"},
            "fork_to": branch_names,
            "branches": None,
            "policy": None,
            "merge": None,
            "trigger": None,
            "output_mode": None,
            "expected_output_count": None,
        }
        tool_sequence: list[_RepairToolCall] = []
        affected_consumers: list[_AffectedConsumer] = []
        # One row_union can contribute two (node, alias) bindings when two of
        # its aliases share a connection. Every patch for a node must land on
        # one running payload: re-serializing the original node per binding
        # emits two upsert_node calls for the same id, and the second reverts
        # the first. Insertion order preserves the cross-node sequence.
        patched_consumers: dict[str, dict[str, Any]] = {}
        for (node, consumer_branch_alias), branch_name in zip(consumer_nodes, branch_names, strict=True):
            patched_consumer = patched_consumers.get(node.id)
            if patched_consumer is None:
                patched_consumer = _serialize_node(node)
                patched_consumers[node.id] = patched_consumer
            if consumer_branch_alias is None:
                patched_consumer["input"] = branch_name
            else:
                patched_branches = patched_consumer["branches"]
                if not isinstance(patched_branches, dict):
                    patched_branches = dict(
                        zip(
                            _coalesce_branch_names(node.branches),
                            _coalesce_branch_connections(node.branches),
                            strict=True,
                        )
                    )
                patched_branches[consumer_branch_alias] = branch_name
                patched_consumer["branches"] = patched_branches
                # ``input`` is only the adapter placeholder for the first
                # branch connection; re-derive it from the accumulated mapping
                # so it stays consistent no matter which aliases were repaired.
                patched_consumer["input"] = next(iter(patched_branches.values()))
            affected_consumers.append(
                {
                    "id": node.id,
                    "current_input": connection_name,
                    "new_input": branch_name,
                }
            )
        tool_sequence.extend({"tool": "upsert_node", "arguments": patched_consumer} for patched_consumer in patched_consumers.values())
        tool_sequence.append({"tool": "upsert_node", "arguments": gate_args})
        tool_sequence.append({"tool": "preview_pipeline", "arguments": {}})
        suggestions.append(
            {
                "code": "duplicate_consumer_connection",
                "connection": connection_name,
                "strategy": "insert_fork_gate",
                "reason": "One connection can feed one processing node. Give each consumer a unique branch input, then insert a fork gate that consumes the shared connection and publishes those branch inputs from gate.fork_to.",
                "affected_consumers": affected_consumers,
                "tool_sequence": tool_sequence,
            }
        )

    return suggestions


def _graph_repair_suggestions(
    state: CompositionState,
    validation: ValidationSummary,
) -> list[_GraphRepairSuggestion]:
    """Return structured graph repair suggestions for validation failures."""
    return _duplicate_consumer_repair_suggestions(state, validation)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result of a tool execution.

    Attributes:
        success: Whether the operation succeeded.
        updated_state: Full state after mutation (or original if success=False).
        validation: Stage 1 validation result for the updated state.
        affected_nodes: Node IDs changed or with changed edges.
        data: Optional data payload for discovery tools.
        prior_validation: Validation from before the mutation. When set,
            to_dict() includes a ``validation_delta`` showing new and
            resolved entries so the agent can focus on what changed.
        post_call_hints: Forward-looking coaching hints from the plugin
            that was just configured. Resolved by the catalog from
            ``BaseX.get_post_call_hints`` (see
            ``contracts/plugin_assistance.py``). Advisory only — not
            part of any audit hash. ``to_dict`` emits this field
            *only when non-empty* so existing tool consumers see no
            schema change.
        plugin_schemas: Inline ``get_plugin_schema`` payloads for every
            plugin named in a validation error of the form
            ``Invalid options for <kind> '<plugin>'``. Populated only on
            failed mutations (``success=False``) for the option-shape
            tools by ``execute_tool``. Keys are ``"<kind>/<plugin>"``
            strings sorted deterministically. ``to_dict`` emits this
            field *only when non-empty*. Eliminates the second
            round-trip the LLM would otherwise burn calling
            ``get_plugin_schema`` separately after each rejection.
        applied_component: Post-finalizer projection of the components a
            successful mutation applied — the exact ``set_pipeline``
            arguments ``get_pipeline_state(component="set_pipeline_arguments")``
            serves, narrowed to what the mutation touched (see
            ``_applied_component_echo``). Populated only on successful
            incremental mutations by ``_mutation_result``; never on failures
            and never on a full replacement. ``to_dict`` emits this field
            *only when set*. Eliminates the ``get_pipeline_state`` round-trip
            the LLM would otherwise burn to see what the server stored.
    """

    success: bool
    updated_state: CompositionState
    validation: ValidationSummary
    affected_nodes: tuple[str, ...]
    data: Any = None
    prior_validation: ValidationSummary | None = None
    runtime_preflight: ValidationResult | None = None
    post_call_hints: tuple[str, ...] = ()
    plugin_schemas: Mapping[str, Mapping[str, Any]] | None = None
    applied_component: Mapping[str, Any] | None = None
    _validation_snapshot_hash: str | None = field(default=None, compare=False, repr=False)
    # True when this failure envelope deliberately withheld the pre-mutation
    # state's validate() entries (full-replacement rejections,
    # elspeth-e89e6bf47a). normalize_tool_result_validation honors it so a
    # snapshot change cannot reattach the withheld stale-state errors.
    # Private framing — never serialized by to_dict().
    _state_validation_withheld: bool = field(default=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        freeze_fields(self, "affected_nodes", "post_call_hints")
        if self.data is not None:
            freeze_fields(self, "data")
        if self.plugin_schemas is not None:
            freeze_fields(self, "plugin_schemas")
        if self.applied_component is not None:
            freeze_fields(self, "applied_component")

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for LLM tool response.

        Validation entries are serialized as structured dicts with
        component, message, and severity fields (B2 requirement).

        When prior_validation is set, includes a validation_delta with
        new_errors, resolved_errors, new_warnings, resolved_warnings to
        help the agent focus on what changed rather than re-reading the
        full validation state.
        """

        result: dict[str, Any] = {
            "success": self.success,
            "validation": {
                "is_valid": self.validation.is_valid,
                "errors": [e.to_dict() for e in self.validation.errors],
                "warnings": [e.to_dict() for e in self.validation.warnings],
                "suggestions": [e.to_dict() for e in self.validation.suggestions],
                "semantic_contracts": _semantic_contracts_payload(
                    self.validation.semantic_contracts,
                ),
                "graph_repair_suggestions": _graph_repair_suggestions(
                    self.updated_state,
                    self.validation,
                ),
            },
            "affected_nodes": list(self.affected_nodes),
            "version": self.updated_state.version,
        }
        if self.data is not None:
            result["data"] = deep_thaw(self.data)

        if self.runtime_preflight is not None:
            result["runtime_preflight"] = self.runtime_preflight.model_dump()

        if self.prior_validation is not None:
            result["validation_delta"] = _compute_validation_delta(
                self.prior_validation,
                self.validation,
            )

        if self.post_call_hints:
            result["post_call_hints"] = list(self.post_call_hints)

        if self.plugin_schemas:
            result["plugin_schemas"] = deep_thaw(self.plugin_schemas)

        if self.applied_component:
            result["applied_component"] = deep_thaw(self.applied_component)

        return result


def diff_states(
    baseline: CompositionState,
    current: CompositionState,
    *,
    baseline_validation: ValidationSummary | None = None,
    current_validation: ValidationSummary | None = None,
) -> dict[str, Any]:
    """Compare two composition states and return a structured change summary.

    Reports added, removed, and modified sources/nodes/edges/outputs, plus
    and metadata changes. Used by the diff_pipeline MCP tool (B5).

    Args:
        baseline_validation: Pre-computed validation for the baseline state.
        current_validation: Pre-computed validation for the current state.
    """
    changes: dict[str, Any] = {
        "from_version": baseline.version,
        "to_version": current.version,
        "sources_changed": False,
        "metadata_changed": False,
        "nodes": {"added": [], "removed": [], "modified": []},
        "edges": {"added": [], "removed": [], "modified": []},
        "outputs": {"added": [], "removed": [], "modified": []},
    }

    if baseline.sources != current.sources:
        changes["sources_changed"] = True
        baseline_names = set(baseline.sources)
        current_names = set(current.sources)
        changes["sources"] = {
            "added": sorted(current_names - baseline_names),
            "removed": sorted(baseline_names - current_names),
            "modified": sorted(name for name in baseline_names & current_names if baseline.sources[name] != current.sources[name]),
        }

    # Metadata
    if baseline.metadata != current.metadata:
        changes["metadata_changed"] = True

    # Nodes
    baseline_nodes = {n.id: n for n in baseline.nodes}
    current_nodes = {n.id: n for n in current.nodes}
    for nid in current_nodes:
        if nid not in baseline_nodes:
            changes["nodes"]["added"].append(nid)
        elif current_nodes[nid] != baseline_nodes[nid]:
            changes["nodes"]["modified"].append(nid)
    for nid in baseline_nodes:
        if nid not in current_nodes:
            changes["nodes"]["removed"].append(nid)

    # Edges
    baseline_edges = {e.id: e for e in baseline.edges}
    current_edges = {e.id: e for e in current.edges}
    for eid in current_edges:
        if eid not in baseline_edges:
            changes["edges"]["added"].append(eid)
        elif current_edges[eid] != baseline_edges[eid]:
            changes["edges"]["modified"].append(eid)
    for eid in baseline_edges:
        if eid not in current_edges:
            changes["edges"]["removed"].append(eid)

    # Outputs
    baseline_outputs = {o.name: o for o in baseline.outputs}
    current_outputs = {o.name: o for o in current.outputs}
    for name in current_outputs:
        if name not in baseline_outputs:
            changes["outputs"]["added"].append(name)
        elif current_outputs[name] != baseline_outputs[name]:
            changes["outputs"]["modified"].append(name)
    for name in baseline_outputs:
        if name not in current_outputs:
            changes["outputs"]["removed"].append(name)

    # Validation delta — reuse pre-computed validations when available
    if baseline_validation is None:
        baseline_validation = baseline.validate()
    if current_validation is None:
        current_validation = current.validate()
    baseline_warnings = {e.message for e in baseline_validation.warnings}
    current_warnings = {e.message for e in current_validation.warnings}
    changes["warnings_introduced"] = sorted(current_warnings - baseline_warnings)
    changes["warnings_resolved"] = sorted(baseline_warnings - current_warnings)

    # Summary stats
    total = sum(len(changes[k][action]) for k in ("nodes", "edges", "outputs") for action in ("added", "removed", "modified"))
    total += int(changes["sources_changed"]) + int(changes["metadata_changed"])
    changes["total_changes"] = total

    return changes


def _validate_mutation_arguments(model: type[BaseModel], arguments: object, argument_name: str) -> BaseModel:
    try:
        return model.model_validate(arguments)
    except PydanticValidationError as exc:
        raise ToolArgumentError(
            argument=argument_name,
            expected=f"object conforming to {model.__name__}",
            actual_type=type(exc).__name__,
        ) from exc


def _attach_post_call_hints(
    result: ToolResult,
    catalog: CatalogService,
    *,
    plugin_type: PluginKind,
    tool_name: str,
    plugin_name: str | None,
    config_snapshot: Mapping[str, object],
) -> ToolResult:
    """Resolve postscript hints from the catalog and attach them to a successful result.

    No-ops when the mutation failed (we don't second-guess validation
    errors with coaching), when ``plugin_name`` is ``None`` (gates,
    coalesces — no plugin to resolve against), or when the plugin's
    ``get_post_call_hints`` returns an empty tuple (no hint to attach,
    so emit a result that doesn't carry the optional field at all).

    See ``contracts/plugin_assistance.py`` for the discipline and
    ``ToolResult.to_dict`` for the emission rule.
    """
    if not result.success or plugin_name is None:
        return result
    hints = catalog.post_call_hints(
        plugin_type=plugin_type,
        plugin_name=plugin_name,
        tool_name=tool_name,
        config_snapshot=config_snapshot,
    )
    if not hints:
        return result
    return replace(result, post_call_hints=hints)


def _discovery_result(state: CompositionState, data: Any) -> ToolResult:
    """Build a ToolResult for a discovery (read-only) tool."""
    validation = state.validate()
    return ToolResult(
        success=True,
        updated_state=state,
        validation=validation,
        affected_nodes=(),
        data=data,
    )


def _failure_result(
    state: CompositionState,
    error_msg: str,
    *,
    error_code: str | None = None,
    with_state_validation: bool = True,
    plugin_identity: tuple[str, str] | None = None,
) -> ToolResult:
    """Build a ToolResult for a failed mutation.

    The rejection reason (``error_msg``) leads ``validation.errors`` as a
    synthetic ``ValidationEntry`` with component ``"rejected_mutation"``.

    ``with_state_validation`` decides whether ``state.validate()`` of the
    *unchanged* state follows it. Default True: for discovery tools and
    incremental mutations the standing state is what survives the rejection,
    and restricted planner surfaces rely on failure results to disclose its
    errors (see the pipeline-state disclosure tests). Full-replacement
    rejections (``set_pipeline``) pass False: there the unchanged state's
    errors are phantom repair targets — on an empty session they read
    ``no_source_configured`` / ``no_sinks_configured`` for a candidate whose
    source and sinks were configured correctly, and the raw-result surfaces
    (freeform chat tool messages, composer MCP responses) serialize
    ``ToolResult.to_dict()`` verbatim (elspeth-e89e6bf47a; tutorial session
    38e3e7f8 burned its repair budget on exactly that noise).

    ``plugin_identity`` records the ``(kind, plugin)`` this rejection is about,
    for the in-process planner consumer that attaches the plugin's contract.
    Pass it ONLY when the identity has already been resolved through the
    request's policy view — the caller knows; the message does not, and cannot
    be made to. Omitting it costs an enrichment, never correctness.
    """
    if with_state_validation:
        validation = _prepend_rejection_entry(state.validate(), error_msg, error_code=error_code, plugin_identity=plugin_identity)
    else:
        validation = _rejection_only_validation(error_msg, error_code=error_code, plugin_identity=plugin_identity)
    data = {_DATA_ERROR_KEY: error_msg}
    if error_code is not None:
        data["error_code"] = error_code
    return ToolResult(
        success=False,
        updated_state=state,
        validation=validation,
        affected_nodes=(),
        data=data,
        _state_validation_withheld=not with_state_validation,
    )


# Regex matching the option-shape failure messages emitted by
# ``_prevalidate_plugin_options`` (see ``_prevalidate_source`` /
# ``_prevalidate_transform`` / ``_prevalidate_sink``). The kind token is
# pinned to the three valid PluginKind values so an unrelated message
# containing ``Invalid options for ...`` text cannot trigger augmentation.
# The plugin name group accepts any non-apostrophe characters because
# plugin names are validated upstream.
_INVALID_OPTIONS_PLUGIN_RE: Final[re.Pattern[str]] = re.compile(
    r"Invalid options for (source|transform|sink) '([^']+)'",
)


def plugin_identities_in_option_failure(message: str) -> tuple[tuple[PluginKind, str], ...]:
    """Return the ``(kind, plugin)`` pairs one option-shape message names.

    Scans the WHOLE message, so it reports identities the validator never
    resolved. These messages interpolate model-authored text in three places —
    the details tail quotes rejected option VALUES, the secret_ref-placement
    head quotes option KEYS, and the ``set_pipeline`` attribution prefix
    quotes the component NAME, which is unvalidated for exactly the components
    that fail — and each one is enough to plant a plugin identity here. No
    reading of the message is trustworthy; that is why the in-process consumer
    now carries ``ValidationEntry.plugin_identity`` from the producer instead
    (elspeth-1d8fc3da83).

    The freeform augmentation below is this function's only caller and keeps
    the whole-message reading, its own pre-existing behaviour: changing it
    would move the schema bytes it inlines. Its exposure is real and tracked
    separately.

    Ordering is sorted and duplicates dropped.
    """
    identities = {(cast(PluginKind, match.group(1)), match.group(2)) for match in _INVALID_OPTIONS_PLUGIN_RE.finditer(message)}
    return tuple(sorted(identities))


def build_plugin_schemas_for_failure(
    result: ToolResult,
    catalog: CatalogService,
    *,
    schema_unavailable_message: Callable[[PluginSchemaInfo], str | None] | None = None,
) -> Mapping[str, Mapping[str, Any]] | None:
    """Build the ``plugin_schemas`` augmentation dict for a failed mutation.

    Scans every entry in ``result.validation.errors`` (including both the
    leading ``rejected_mutation`` entry and any state-level errors that
    follow). Each entry's ``message`` is regex-matched against
    ``_INVALID_OPTIONS_PLUGIN_RE``; every distinct ``(kind, plugin)`` pair
    is resolved through ``catalog.get_schema`` and dumped to a plain dict
    via ``PluginSchemaInfo.model_dump()`` so the payload is byte-identical
    to what the LLM would otherwise receive from a discrete
    ``get_plugin_schema`` tool call. When ``schema_unavailable_message`` is
    supplied, plugins hidden by the same availability gate as
    ``get_plugin_schema`` are omitted rather than inlining a forbidden schema.

    Returns ``None`` when the result is successful or when no error
    message matches the option-shape pattern. The caller is responsible
    for restricting the call to declarations that set
    ``augments_on_failure=True`` (gated by
    ``_registry.should_augment_with_plugin_schemas``).

    Trust tier: server-controlled response shaping. A regex match implies
    the validator already resolved the plugin in the catalog (the unknown
    -plugin path emits ``"Unknown <kind> plugin '<name>'"`` instead).
    Therefore ``catalog.get_schema`` returning ``ValueError`` here is a
    Tier-1 anomaly — propagate, do not silently omit.
    """
    if result.success:
        return None
    discovered: dict[tuple[str, str], Mapping[str, Any]] = {}
    for entry in result.validation.errors:
        for key in plugin_identities_in_option_failure(entry.message):
            kind, plugin_name = key
            if key in discovered:
                continue
            schema = catalog.get_schema(kind, plugin_name)
            if schema_unavailable_message is not None and schema_unavailable_message(schema) is not None:
                continue
            discovered[key] = schema.model_dump()
    if not discovered:
        return None
    return {f"{kind}/{plugin_name}": payload for (kind, plugin_name), payload in sorted(discovered.items())}


def _prepend_rejection_entry(
    base: ValidationSummary,
    error_msg: str,
    *,
    error_code: str | None = None,
    plugin_identity: tuple[str, str] | None = None,
) -> ValidationSummary:
    """Return a ValidationSummary with a leading rejected_mutation entry.

    Preserves all non-error fields (warnings, suggestions,
    edge_contracts, semantic_contracts) verbatim. ``is_valid`` is
    forced to False because a rejection entry is by construction a
    high-severity error.
    """
    rejection = ValidationEntry(
        component="rejected_mutation",
        message=error_msg,
        severity="high",
        error_code=error_code,
        plugin_identity=plugin_identity,
    )
    return ValidationSummary(
        is_valid=False,
        errors=(rejection, *base.errors),
        warnings=base.warnings,
        suggestions=base.suggestions,
        edge_contracts=base.edge_contracts,
        semantic_contracts=base.semantic_contracts,
    )


def _rejection_only_validation(
    error_msg: str,
    *,
    error_code: str | None = None,
    plugin_identity: tuple[str, str] | None = None,
) -> ValidationSummary:
    """Return a ValidationSummary holding only a rejected_mutation entry.

    Full-replacement rejections leave the state untouched AND replace it
    wholesale on success, so every field derived from validating that state
    (errors, warnings, suggestions, contracts) describes a state the caller
    is not editing and is withheld (elspeth-e89e6bf47a). ``is_valid`` is
    False because a rejection entry is by construction a high-severity
    error.
    """
    rejection = ValidationEntry(
        component="rejected_mutation",
        message=error_msg,
        severity="high",
        error_code=error_code,
        plugin_identity=plugin_identity,
    )
    return ValidationSummary(is_valid=False, errors=(rejection,))


# Key under which a merged rejection envelope reports the components it
# collected but did not list. Present only when the cap actually truncated,
# so a reader never has to distinguish "no overflow" from "not counted".
COMPONENTS_WITHHELD_KEY: Final[str] = "components_withheld"


def _merged_component_rejection_result(
    results: Sequence[ToolResult],
    *,
    components_withheld: int,
) -> ToolResult:
    """Merge one full-replacement candidate's per-component rejections.

    A full-replacement candidate is validated component by component; each
    failing component produces its own single-entry rejection envelope. This
    joins them into ONE envelope so a single repair turn names every defective
    component instead of the first (elspeth-4fad98a453): three defective
    components used to cost three turns, which is deterministic
    REPAIR_EXHAUSTED against the default repair budget.

    The FIRST failing component's envelope is the base — its ``data`` payload
    (including the credential-rejection repair block) and its leading entry
    stay exactly what a single-component rejection would have produced, so
    ordering is stable and no response shape moves. Later components
    contribute their rejection entries only. ``components_withheld`` records
    the components the caller's reporting cap dropped; truncation is never
    silent.
    """
    base = results[0]
    entries = tuple(entry for result in results for entry in result.validation.errors if entry.component == "rejected_mutation")
    data = base.data
    merged_data: Any = data
    if components_withheld:
        merged_data = (
            {**data, COMPONENTS_WITHHELD_KEY: components_withheld}
            if isinstance(data, Mapping)
            else {COMPONENTS_WITHHELD_KEY: components_withheld}
        )
    return replace(
        base,
        validation=ValidationSummary(is_valid=False, errors=entries),
        data=merged_data,
    )


def _mutation_result(
    new_state: CompositionState,
    affected: tuple[str, ...],
    *,
    prior_validation: ValidationSummary | None = None,
    data: Any = None,
    post_call_hints: tuple[str, ...] = (),
    full_replacement: bool = False,
) -> ToolResult:
    """Build a ToolResult for a successful mutation.

    ``post_call_hints`` is the (possibly empty) tuple returned by the
    catalog's ``post_call_hints`` method for the just-configured
    plugin. Tool handlers compute it before calling here so the hint
    surface participates in the same envelope as ``validation`` and
    ``affected_nodes``. See ``contracts/plugin_assistance.py`` for
    the discipline; ``ToolResult.to_dict`` emits the field only when
    non-empty.

    ``affected`` is also what scopes the applied-component echo, so every
    mutating tool gets it from the identifiers it already reports — node ids,
    source component ids, and sink names — with no per-tool wiring.
    ``full_replacement`` suppresses that echo for the whole-document authoring
    tools (``set_pipeline`` / ``apply_pipeline_recipe``): there every component
    is affected, so the echo would be the whole-state read it exists to
    replace, and the model already holds those bytes verbatim in the call it
    just made.
    """
    validation = new_state.validate()
    return ToolResult(
        success=True,
        updated_state=new_state,
        validation=validation,
        affected_nodes=affected,
        prior_validation=prior_validation,
        data=data,
        post_call_hints=post_call_hints,
        applied_component=None if full_replacement else _applied_component_echo(new_state, affected),
    )


def _vf_destination_note(
    state: CompositionState,
    on_vf: str,
) -> dict[str, str] | None:
    """Advisory note when on_validation_failure references an unknown output.

    Returns a dict with a ``note`` key suitable for ``ToolResult.data``,
    or ``None`` when no advisory is needed (destination is ``"discard"``
    or matches a configured output).
    """
    if on_vf == "discard":
        return None
    output_names = {o.name for o in state.outputs}
    if on_vf not in output_names:
        current = sorted(output_names) if output_names else "(none)"
        return {
            "note": (
                f"on_validation_failure='{on_vf}' does not match any configured output. "
                "Use 'discard' to drop invalid rows without routing, or "
                f"add an output named '{on_vf}' before running the pipeline. "
                f"Current outputs: {current}."
            ),
        }
    return None


def _apply_merge_patch(
    target: Mapping[str, Any],
    patch: dict[str, Any],
) -> dict[str, Any]:
    """Shallow merge-patch: overwrite or delete top-level keys in target."""
    result = dict(target)
    for key, value in patch.items():
        if value is None:
            # Delete-if-present: a None patch value removes the key. Access the
            # key directly (R9 remediation) rather than pop-with-default; the
            # membership guard preserves the silent no-op on an absent key.
            if key in result:
                del result[key]
        else:
            result[key] = value
    return result


def _serialize_source(source: SourceSpec) -> dict[str, Any]:
    """Serialize a SourceSpec to a plain dict for LLM consumption."""
    return {
        "plugin": source.plugin,
        "on_success": source.on_success,
        "options": deep_thaw(source.options),
        "on_validation_failure": source.on_validation_failure,
        "description": source.description,
    }


def _serialize_node(node: NodeSpec) -> dict[str, Any]:
    """Serialize a NodeSpec to a plain dict for LLM consumption.

    Includes all fields (even None) so the LLM sees the full schema.
    """
    return {
        "id": node.id,
        "node_type": node.node_type,
        "plugin": node.plugin,
        "input": node.input,
        "on_success": node.on_success,
        "on_error": node.on_error,
        "options": deep_thaw(node.options),
        "condition": node.condition,
        "routes": deep_thaw(node.routes) if node.routes else None,
        "fork_to": list(node.fork_to) if node.fork_to else None,
        "branches": _serialize_branches(node.branches) if node.branches else None,
        "policy": node.policy,
        "merge": node.merge,
        "trigger": deep_thaw(node.trigger) if node.trigger else None,
        "output_mode": node.output_mode,
        "expected_output_count": node.expected_output_count,
        "timeout_seconds": node.timeout_seconds,
        "description": node.description,
    }


def _serialize_output(output: OutputSpec) -> dict[str, Any]:
    """Serialize an OutputSpec to a plain dict for LLM consumption."""
    return {
        "sink_name": output.name,
        "plugin": output.plugin,
        "options": deep_thaw(output.options),
        "on_write_failure": output.on_write_failure,
        "description": output.description,
    }


def _serialize_edge(edge: EdgeSpec) -> dict[str, Any]:
    """Serialize an EdgeSpec to a plain dict for LLM consumption."""
    return {
        "id": edge.id,
        "from_node": edge.from_node,
        "to_node": edge.to_node,
        "edge_type": edge.edge_type,
        "label": edge.label,
    }


def _serialize_full_pipeline_state(state: CompositionState, *, requested_component: Any) -> _FullPipelineStatePayload:
    """Serialize the full state and expose accepted full-state spellings."""
    return {
        "sources": {name: _serialize_source(source) for name, source in state.sources.items()},
        "nodes": [_serialize_node(n) for n in state.nodes],
        "outputs": [_serialize_output(o) for o in state.outputs],
        "edges": [_serialize_edge(e) for e in state.edges],
        "metadata": {"name": state.metadata.name, "description": state.metadata.description},
        "version": state.version,
        "inspection": {
            "requested_component": requested_component,
            "resolved_component": "full",
            "accepted_full_state_aliases": list(_FULL_STATE_COMPONENT_ALIASES),
        },
    }


# Slice 4 additions — shared validation/repair helpers, file-sink collision-policy
# cluster, and source-validation policy strings. Pulled to ``_common`` so the
# per-plane files (sources/transforms/sinks/outputs/sessions) can avoid importing
# each other.

_DEFAULT_SOURCE_VALIDATION_FAILURE: Final[str] = "discard"

_SOURCE_VALIDATION_FAILURE_DESCRIPTION: Final[str] = (
    "'discard' drops rows that fail source validation. Any other value, including 'quarantine', must match a configured output/sink name."
)

_STEP_DESCRIPTION_DESCRIPTION: Final[str] = (
    "One short sentence of plain prose saying what this step does, shown to reviewers "
    "on the Spec tab. Supply it when creating the step and refresh it whenever you "
    "change what the step does. Informational only — never affects validation or execution."
)


def canonicalize_source_validation_failure(value: str | None) -> str:
    """Fold unspecified spellings of ``on_validation_failure`` into 'discard'.

    THE single owner of what an authored empty-string route means
    (elspeth-bcd7051143). ``None`` (not specified) and ``""`` (specified but
    naming no route — a sink name can never be the empty string, so "" carries
    no distinct routing intent) both canonicalize to the 'discard' default.
    Every composer seam that admits an authored ``on_validation_failure``
    routes through here: ``set_source``, ``set_source_from_blob``, both
    ``set_pipeline`` source branches, runtime-YAML import
    (``yaml_importer._source_from_runtime_entry``), and the required-control
    auto-wire projection (``required_controls._parse_source``). Before this
    owner existed the seams disagreed — ``set_pipeline`` truthiness-coerced
    "", ``set_source``/``set_source_from_blob`` passed it through to the
    engine plugin-config rejection, and the auto-wire pass refused the whole
    candidate as non-discard — an accepted-then-wedged repair defect. The
    guided surface's hard reject of "" (``guided/resolved.py``
    ``SourceResolved``) stays as an internal invariant, not a second owner:
    with boundary canonicalization "" can no longer lawfully reach it. The
    engine-side plugin-config validator
    (``plugins/infrastructure/config_base.py``) still rejects "" for
    non-composer-authored configs; composer-persisted state is always
    canonical before it gets there.
    """
    if value is None or value == "":
        return _DEFAULT_SOURCE_VALIDATION_FAILURE
    return value


def _credential_wiring_contract_failure(
    state: CompositionState,
    *,
    component_id: str,
    component_type: str,
    plugin_type: PluginKind | None = None,
    plugin_name: str | None = None,
    options: Any,
    with_state_validation: bool = True,
) -> ToolResult | None:
    """Reject literal credentials before a mutation writes them into state.

    The returned message advertises the *inline* secret_ref form first
    because that is the only path that works for new nodes:

    - ``set_pipeline`` is atomic, so a node whose options omit a required
      credential field fails pydantic validation and the whole mutation
      rolls back — meaning ``wire_secret_ref`` cannot be used to attach
      the secret post-hoc (the node never lands in state).
    - ``collect_credential_field_violations`` short-circuits on
      ``{secret_ref: NAME}`` markers and ``set_pipeline`` validates those
      deferred fields without resolving their values, so passing the marker
      inline in the node's options is the supported new-node path.

    The post-hoc ``wire_secret_ref`` sequence is still documented as
    the secondary path for nodes that already exist in state.
    """
    plugin_specific_fields = (
        allowed_secret_ref_fields(plugin_type, plugin_name) if plugin_type is not None and plugin_name is not None else frozenset()
    )
    fields = tuple(
        dict.fromkeys(
            collect_credential_field_violations(
                options,
                additional_credential_fields=plugin_specific_fields,
            )
        )
    )
    if not fields:
        return None

    credential_fields = tuple(f"{component_id}:{field}" for field in fields)
    field_list = ", ".join(credential_fields)
    repair_sequence = ("list_secret_refs", "validate_secret_ref", "wire_secret_ref")
    repair_text = "list_secret_refs -> validate_secret_ref -> wire_secret_ref"
    inline_instruction = (
        "Set `<field>: {secret_ref: NAME}` directly in the node's options "
        "when calling set_pipeline / upsert_node. (The marker is handled "
        "without resolving its value during option validation and resolved at "
        "execution time.) This "
        "rejection left pipeline state unchanged: repair by re-issuing only "
        "the rejected call with the marker substituted for the literal "
        "value — do not rebuild the pipeline from scratch. For a component "
        "already in state, patching just that component "
        "(patch_source_options / patch_node_options / patch_output_options) "
        "with the marker is the minimal correction."
    )
    post_hoc_instruction = f"Alternatively, after the node already exists in state, call {repair_text} to attach the marker post-hoc."
    error_msg = (
        f"Credential field(s) contain literal value(s): {field_list}. "
        f"Literal credential values were not stored. {inline_instruction} "
        f"{post_hoc_instruction}"
    )
    # Symmetric with _failure_result: the rejection reason leads
    # validation.errors; with_state_validation decides whether the unchanged
    # state's errors follow it (False for full-replacement set_pipeline,
    # elspeth-e89e6bf47a).
    if with_state_validation:
        validation = _prepend_rejection_entry(state.validate(), error_msg)
    else:
        validation = _rejection_only_validation(error_msg)
    return ToolResult(
        success=False,
        updated_state=state,
        validation=validation,
        affected_nodes=(),
        _state_validation_withheld=not with_state_validation,
        data={
            _DATA_ERROR_KEY: error_msg,
            "credential_fields": credential_fields,
            "components": (
                {
                    "component_id": component_id,
                    "component_type": component_type,
                    "fields": fields,
                },
            ),
            "repair": {
                "inline_form": {
                    "instruction": inline_instruction,
                    "example_options": {field: {"secret_ref": "<NAME>"} for field in fields},
                },
                "post_hoc_form": {
                    "instruction": post_hoc_instruction,
                    "tool_sequence": repair_sequence,
                },
            },
        },
    )


@dataclass(frozen=True, slots=True)
class PluginPolicyViolation:
    error_code: PluginUnavailableReason
    message: str


# Plain-language cause per unavailability reason. Each names the DISTINCT
# deployment reality (not installed vs installed-but-not-enabled vs missing
# credential ...) so the composer model can tell the user exactly why a
# capability cannot be used and what an operator would change — instead of
# echoing a bare policy code.
_PLUGIN_UNAVAILABLE_EXPLANATIONS: Final[dict[PluginUnavailableReason, str]] = {
    PluginUnavailableReason.NOT_AUTHORIZED: (
        "the plugin is installed but not turned on in this deployment's plugin policy; an operator must enable it"
    ),
    PluginUnavailableReason.NOT_INSTALLED: "no plugin with this name is installed in this deployment",
    PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING: (
        "the plugin is installed but a local requirement it depends on is missing in this deployment"
    ),
    PluginUnavailableReason.CREDENTIAL_MISSING: (
        "the plugin is turned on but the credential it needs is not configured in this deployment"
    ),
    PluginUnavailableReason.PROFILE_UNAVAILABLE: (
        "the plugin is installed but not turned on in this deployment — no operator profile is "
        "configured for it; an operator must enable one before it can be used"
    ),
    PluginUnavailableReason.WEB_SURFACE_PROHIBITED: WEB_PROHIBITED_PLUGIN_EXPLANATION,
}


def _plugin_unavailable_message(plugin_type: PluginKind, reason: PluginUnavailableReason) -> str:
    return f"{plugin_type} plugin selection is unavailable ({reason.value}): {_PLUGIN_UNAVAILABLE_EXPLANATIONS[reason]}"


def _prohibited_section(items: Sequence[PluginSummary]) -> tuple[ProhibitedPluginDisclosure, ...]:
    """Shape ``PolicyCatalogView.list_prohibited_*`` entries for chat discovery.

    Every ``item`` here already cleared ``PolicyCatalogView._prohibited`` —
    i.e. it carries ``PluginUnavailableReason.WEB_SURFACE_PROHIBITED``, the
    one closed reason this section ever names (R2-F18 / elspeth-28a695d7f4).
    Reuses the same static policy prose the attempt path
    (``_plugin_unavailable_message``) already shows on a rejected
    ``set_source`` — no new disclosure surface, just an earlier one, so a
    user naming a prohibited plugin gets the reason without first trying and
    failing.
    """
    return prohibited_plugin_section(items)


# gate/coalesce/row_union/queue are built-in node_types wired with plugin=null —
# they do not exist in the plugin registry, and answering a registry probe for
# them with "not installed" invites a false honest decline ("this deployment
# cannot merge branches"). These names are closed composer vocabulary, safe to
# echo.
_STRUCTURAL_NODE_TYPE_GUIDANCE: Final[dict[str, str]] = {
    "coalesce": (
        "'coalesce' is not a plugin — it is a built-in node_type that needs no plugin. Wire it as a "
        "node with node_type='coalesce', plugin=null, branches mapping each branch name to its "
        "incoming connection, policy (e.g. 'require_all') and merge (e.g. 'union'); downstream nodes "
        "read the coalesce node id as their input. For running several LLM assessments per row, "
        "prefer ONE llm transform with a `queries` map instead of fork/coalesce."
    ),
    "gate": (
        "'gate' is not a plugin — it is a built-in node_type that needs no plugin. Wire it as a node "
        "with node_type='gate', plugin=null, a `condition` row expression and routes={'true': ..., "
        "'false': ...}; route to 'fork' with fork_to=[...] to fan a row out to several branches. "
        "Its optional node-level `on_error` handles expression-evaluation errors: set it to 'discard' or a declared sink; "
        "omit it for fail-fast behavior. Do not represent gate on_error as an edge."
    ),
    "row_union": (
        "'row_union' is not a plugin — it is a built-in node_type that needs no plugin. Wire it as a "
        "node with node_type='row_union', plugin=null, at least two ordered `branches` mapping each "
        "fork branch alias to its incoming connection, `input` equal to the first mapped connection "
        "as a serialization placeholder, and `on_success` naming a downstream processing connection. "
        "It has fixed require_all N-to-N semantics: it waits for every branch, then releases every "
        "original row unchanged in declared branch order; an optional finite positive "
        "`timeout_seconds` is supported."
    ),
    "queue": (
        "'queue' is not a plugin — it is a built-in node_type that needs no plugin, used for fan-in: "
        "a node with node_type='queue', plugin=null whose id doubles as its connection name; upstream "
        "nodes point on_success at it and one downstream node reads it as input."
    ),
}


def _validate_plugin_name(
    context: ToolContext,
    plugin_type: PluginKind,
    name: object,
) -> PluginPolicyViolation | None:
    """Validate a new plugin selection against one request policy view."""
    if not isinstance(name, str):
        return PluginPolicyViolation(
            error_code=PluginUnavailableReason.NOT_INSTALLED,
            message=_plugin_unavailable_message(plugin_type, PluginUnavailableReason.NOT_INSTALLED),
        )
    if plugin_type == "transform" and name in _STRUCTURAL_NODE_TYPE_GUIDANCE:
        return PluginPolicyViolation(
            error_code=PluginUnavailableReason.NOT_INSTALLED,
            message=_STRUCTURAL_NODE_TYPE_GUIDANCE[name],
        )
    try:
        plugin_id = PluginId(plugin_type, name)
    except ValueError:
        return PluginPolicyViolation(
            error_code=PluginUnavailableReason.NOT_INSTALLED,
            message=_plugin_unavailable_message(plugin_type, PluginUnavailableReason.NOT_INSTALLED),
        )
    reason = context.catalog.unavailable_reason(plugin_id)
    if reason is not None:
        return PluginPolicyViolation(
            error_code=reason,
            message=_plugin_unavailable_message(plugin_type, reason),
        )
    try:
        context.catalog.get_schema(plugin_type, name)
    except (ValueError, KeyError):
        return PluginPolicyViolation(
            error_code=PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING,
            message=_plugin_unavailable_message(plugin_type, PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING),
        )
    return None


def _plugin_policy_failure(
    state: CompositionState,
    violation: PluginPolicyViolation,
    *,
    component: str | None = None,
    with_state_validation: bool = True,
) -> ToolResult:
    message = violation.message if component is None else f"{component}: {violation.message}"
    return _failure_result(
        state,
        message,
        error_code=violation.error_code.value,
        with_state_validation=with_state_validation,
    )


def _validate_aggregation_trigger(trigger: Any) -> str | None:
    """Return an error message if an aggregation trigger does not match runtime settings."""
    if trigger is None:
        return None
    try:
        TriggerConfig.model_validate(trigger)
    except PydanticValidationError as exc:
        detail = "; ".join(str(error["msg"]) for error in exc.errors())
        return f"Invalid aggregation trigger: {detail}"
    return None


def _validate_source_path(
    options: Mapping[str, Any],
    data_dir: str | None,
    *,
    session_id: str | None,
    require_data_dir: bool = False,
) -> str | None:
    """S2: Validate path/file options under the caller's blob subtree.

    Returns an error message if validation fails, None if OK.
    Uses Path.resolve() + is_relative_to() to defeat ../ traversal.
    """
    for key in SOURCE_LOCAL_PATH_OPTION_KEYS:
        if key in options:
            if data_dir is None:
                if not require_data_dir:
                    return None
                return (
                    "Path violation (S2): source path options require data_dir "
                    "for allowlist enforcement. Bind uploaded files through "
                    "set_source_from_blob or provide the dispatcher data_dir."
                )
            allowed = allowed_source_directories(data_dir, session_id=session_id)
            resolved = resolve_data_path(options[key], data_dir)
            if not any(resolved.is_relative_to(d) for d in allowed):
                return (
                    f"Path violation (S2): '{options[key]}' is outside the "
                    f"allowed directories. Source file paths "
                    f"must be under this session's {data_dir}/blobs/<session>/ subtree."
                )
    return None


def _validate_sink_path(
    options: dict[str, Any],
    data_dir: str | None,
    *,
    session_id: str | None,
) -> str | None:
    """Validate that sink path options are under allowed output directories.

    Returns an error message if validation fails, None if OK.
    Mirrors _validate_source_path but uses _allowed_sink_directories.
    Blob-directory writes are confined to the caller's own session subtree
    (elspeth-bdc17cfdb1); ``session_id=None`` fails closed to outputs only.
    """
    if data_dir is None:
        return None

    allowed = allowed_sink_directories(data_dir, session_id=session_id)

    for key in SINK_LOCAL_PATH_OPTION_KEYS:
        if key in options:
            resolved = resolve_sink_data_path(options[key], data_dir, session_id=session_id)
            if not any(resolved.is_relative_to(d) for d in allowed):
                return (
                    f"Path violation (S2): '{key}' value '{options[key]}' is outside the "
                    f"allowed directories. Sink output paths "
                    f"must be under this session's {data_dir}/outputs/<session>/ "
                    f"or {data_dir}/blobs/<session>/ subtree."
                )
    return None


def _validate_transform_provider_config_path(
    options: Mapping[str, Any],
    data_dir: str | None,
    *,
    session_id: str | None,
) -> str | None:
    """Validate nested provider_config path options are under allowed dirs.

    RAG retrieval transforms carry a local Chroma persist_directory under
    ``options["provider_config"]``. It is a read/write target like a sink, so
    it is confined to the allowed sink directories — including the per-session
    blob confinement (elspeth-bdc17cfdb1): a persist_directory pointed at
    another session's blob subtree would disclose that session's data on read
    as well as corrupt it on write. Non-RAG transforms have no provider_config
    dict and are skipped cleanly.

    Returns an error message if validation fails, None if OK.
    """
    if data_dir is None:
        return None

    provider_config = options.get("provider_config")
    if not isinstance(provider_config, Mapping):
        return None

    allowed = allowed_sink_directories(data_dir, session_id=session_id)

    for key in NESTED_LOCAL_PATH_OPTION_KEYS:
        if key not in provider_config:
            continue
        value = provider_config[key]
        # A null nested path must be skipped, not resolved — Path(None) raises.
        # Mirrors the runtime siblings (service/validation) which guard on
        # ``value is not None`` before resolving.
        if value is None:
            continue
        resolved = resolve_sink_data_path(value, data_dir, session_id=session_id)
        if not any(resolved.is_relative_to(d) for d in allowed):
            return (
                f"Path violation (S2): provider_config '{key}' value "
                f"'{value}' is outside the allowed directories. "
                f"Transform provider paths must be under this session's "
                f"{data_dir}/outputs/<session>/ or {data_dir}/blobs/<session>/ subtree."
            )
    return None


def _validate_transform_provider_config_policy(options: Mapping[str, Any], *, plugin: str | None = None) -> str | None:
    """Validate non-path web transform configuration policy constraints."""
    provider_policy_error = web_rag_provider_config_policy_error(options)
    if provider_policy_error is not None:
        return provider_policy_error
    return web_llm_retry_budget_policy_error(plugin, options)


def _prevalidate_plugin_options(
    plugin_type: PluginKind,
    plugin_name: str,
    options: dict[str, Any],
    *,
    injected_fields: dict[str, Any] | None = None,
) -> str | None:
    """Pre-validate plugin options against the plugin's config model.

    Catches missing required options (e.g., schema, operations) and
    malformed values (e.g., invalid field specs) BEFORE storing them in
    CompositionState. Returns None if valid, or a descriptive error
    message suitable for returning to the LLM agent.

    The plugin's own Pydantic config model is the authority — this
    function asks the plugin what it needs rather than hardcoding
    knowledge about individual plugins.

    Secret-ref markers (``{"secret_ref": "NAME"}``) are withheld from the
    primary config-model validation and field-level errors on those fields are
    filtered because they ARE provisioned. If a model-level validator needs to
    observe paired or conditional credential presence, validation is retried
    with the shared non-secret placeholder. Real secret values remain
    unavailable to authoring and are resolved only at execution time.

    Args:
        plugin_type: "source", "transform", or "sink".
        plugin_name: Plugin name (e.g., "csv", "value_transform").
        options: Options dict as provided by the LLM agent.
        injected_fields: Synthetic values for fields that come from
            other parts of the pipeline spec (e.g., on_validation_failure
            for sources). Merged into options for validation only —
            not stored.
    """
    secret_ref_placement_error = _secret_ref_placement_error(plugin_type, plugin_name, options)
    if secret_ref_placement_error is not None:
        return secret_ref_placement_error

    try:
        if plugin_type == "source":
            config_cls = get_source_config_model(plugin_name, options)
        elif plugin_type == "transform":
            config_cls = get_transform_config_model(plugin_name, options)
        elif plugin_type == "sink":
            config_cls = get_sink_config_model(plugin_name)
        else:
            # PluginKind is Literal["source", "transform", "sink"] — unreachable.
            raise AssertionError(f"_prevalidate_plugin_options: unexpected plugin_type={plugin_type!r}")
    except UnknownPluginTypeError:
        return f"Unknown {plugin_type} plugin '{plugin_name}'. Call list_{plugin_type}s to see available {plugin_type} plugins."
    except ValueError as exc:
        # Config model selection raised (e.g. unknown LLM provider) — surface it.
        return f"Invalid options for {plugin_type} '{plugin_name}': {exc}"

    if config_cls is None:
        return None

    # Options may contain frozen containers (MappingProxyType, tuple) from
    # CompositionState.  Thaw them so Pydantic receives plain dicts/lists.
    merged = deep_thaw(options)
    if injected_fields:
        for k, v in injected_fields.items():
            if k not in merged:
                merged[k] = v
    if plugin_type == "transform" and plugin_name == "llm":
        _mask_pending_interpretation_placeholders_for_authoring_validation(merged)

    # Withhold secret_ref markers from the primary validation pass. A
    # secret-ref'd field IS provisioned (the user called wire_secret_ref, or
    # operator-profile lowering injected the credential as a scoped marker),
    # just deferred to execution time. The canonical parser accepts both the
    # bare {"secret_ref"} form and the scoped {"secret_ref", "secret_scope"}
    # form profile lowering emits.
    placeholder_options = redact_secret_refs_for_validation(merged)
    secret_ref_keys: set[str] = set()
    for key, value in list(merged.items()):
        if parse_secret_ref_marker(value) is not None:
            secret_ref_keys.add(key)
            del merged[key]

    # Strip widened blob_ref(inline_content) markers before validation.  Like
    # secret_ref, these fields are provisioned but deferred to runtime
    # resolution; bind_source remains source-only and is deliberately not
    # stripped here.
    blob_inline_ref_keys: set[str] = set()
    for key, value in list(merged.items()):
        shape = is_widened_blob_ref(value)
        if shape is not None and shape.mode == "inline_content":
            blob_inline_ref_keys.add(key)
            del merged[key]
            del placeholder_options[key]

    try:
        config = config_cls.from_dict(merged, plugin_name=plugin_name)
    except PluginConfigError as exc:
        if not secret_ref_keys and not blob_inline_ref_keys:
            # No secret refs were stripped — report the error as-is.
            msg = exc.cause if exc.cause is not None else str(exc)
            return f"Invalid options for {plugin_type} '{plugin_name}': {msg}"

        cause = exc.__cause__
        has_model_level_error = not isinstance(cause, PydanticValidationError) or any(not error["loc"] for error in cause.errors())
        if secret_ref_keys and has_model_level_error:
            # Presence-dependent model validators cannot distinguish a withheld
            # marker from an absent credential. Retry structure-only validation
            # with the same non-secret placeholder used by export preflight.
            # Return immediately on success to preserve the established deferred
            # secret path, which intentionally does not inspect secret values or
            # advance into value-source checks that the primary pass did not reach.
            try:
                config_cls.from_dict(placeholder_options, plugin_name=plugin_name)
            except PluginConfigError as placeholder_exc:
                msg = placeholder_exc.cause if placeholder_exc.cause is not None else str(placeholder_exc)
                return f"Invalid options for {plugin_type} '{plugin_name}': {msg}"
            return None

        # Secret refs were withheld. Filter out field-level errors on those
        # fields while retaining every unrelated validation failure.
        if not isinstance(cause, PydanticValidationError):
            # ValueError path (model validators) — can't filter per-field.
            msg = exc.cause if exc.cause is not None else str(exc)
            return f"Invalid options for {plugin_type} '{plugin_name}': {msg}"

        deferred_keys = secret_ref_keys | blob_inline_ref_keys
        remaining = [e for e in cause.errors() if not (e["loc"] and e["loc"][0] in deferred_keys)]
        if not remaining:
            return None

        # Re-format only the non-secret errors.
        lines = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in remaining)
        return f"Invalid options for {plugin_type} '{plugin_name}': {lines}"

    # Construction passed type/required validation. Now enforce the config's
    # VALUE_SOURCES declarations (e.g. OpenRouter ``model`` catalog membership)
    # at authoring time — the same structured check the bundle walker runs at
    # instantiation (engine/orchestrator/preflight.py). This catches a
    # hallucinated catalog value here, with an actionable ``list_models`` hint,
    # instead of letting it slip through prevalidation. Catalog membership is a
    # value-source concern, deliberately NOT enforced in config construction.
    value_source_findings = check_config_value_sources(config, component_id=plugin_name)
    if value_source_findings:
        return f"Invalid options for {plugin_type} '{plugin_name}': " + "; ".join(f.reason for f in value_source_findings)
    return None


def _mask_pending_interpretation_placeholders_for_authoring_validation(
    options: dict[str, Any],
) -> None:
    """Allow unresolved interpretation placeholders during composer authoring.

    ``{{interpretation:<term>}}`` is a Phase 5b composer-review token, not a
    runtime Jinja variable. The LLM must be able to stage a pending LLM node
    carrying that token so ``request_interpretation_review`` can create the
    audit row and the user can resolve it. Runtime remains strict: execution
    rejects unresolved placeholders before YAML generation, and resolved
    prompts validate through the normal LLM config path.
    """

    if "resolved_prompt_template_hash" in options:
        return
    prompt_template = options.get("prompt_template")
    if not isinstance(prompt_template, str):
        return
    options["prompt_template"] = INTERPRETATION_PLACEHOLDER_RE.sub(
        "pending interpretation",
        prompt_template,
    )


def _resolver_owned_interpretation_requirement_error(
    options: Mapping[str, Any],
    *,
    tool_name: str,
    component_id: str | None = None,
    source: bool = False,
) -> str | None:
    """Validate the complete authoring shape for ``interpretation_requirements``.

    Composer input may supply only the compact unresolved shell
    ``{kind, user_term, draft}``. Identity, status, event linkage, accepted
    values, and artifact hashes are all resolver-owned even when the supplied
    value is null. Presence is therefore the authority violation; inspecting
    or reflecting the untrusted value would create a tool-error leak channel.

    This check is PLUGIN-AGNOSTIC and must guard every write path to a spec's
    ``options`` — both LLM-node options (``vague_term`` / ``llm_prompt_template``
    / ``llm_model_choice`` requirements) and SOURCE options
    (``invented_source`` requirements). The read side that decides whether an
    LLM-authored ("invented") source still needs human review
    (``interpretation_state._pending_source_sites``) trusts a self-reported
    ``status == "resolved"`` + ``accepted_artifact_hash`` match without
    consulting the events DB, so this write-boundary guard is the only real
    defence against a forged "resolved" requirement. Apply it to the
    LLM-SUPPLIED delta (full options on a create, the ``patch`` on a merge) so a
    legitimately-resolved requirement already in stored state is not re-flagged.
    """
    if INTERPRETATION_REQUIREMENTS_KEY not in options:
        return None
    requirements_value = options[INTERPRETATION_REQUIREMENTS_KEY]
    malformed_error = (
        f"{tool_name} options.{INTERPRETATION_REQUIREMENTS_KEY} must be a list of "
        "review entry objects, each carrying non-empty string fields kind, user_term, "
        "and draft. Omit the field entirely when no review is being staged; canonical "
        "review metadata is written only by resolve_interpretation_event."
    )
    if type(requirements_value) is not list:
        return malformed_error

    seen_kind_terms: set[tuple[str, str]] = set()
    seen_normalized_user_terms: set[str] = set()
    seen_projected_ids: set[str] = set()
    collision_error = (
        f"{tool_name} options.{INTERPRETATION_REQUIREMENTS_KEY} contains duplicate or "
        "colliding interpretation requirement identities. Each authored row must "
        "have a unique normalized kind/user_term and server-projected ID; remove "
        "duplicate or colliding rows and retry."
    )
    registration_error: str | None = None
    for index, requirement in enumerate(requirements_value):
        if not isinstance(requirement, Mapping):
            return malformed_error
        status = requirement["status"] if "status" in requirement else None
        if type(status) is str and status == "resolved":
            # Preserve the established repair text for the common resolved-row
            # exploit without reflecting arbitrary untrusted status values.
            return (
                f"{tool_name} options.{INTERPRETATION_REQUIREMENTS_KEY}[{index}] includes "
                "resolver-owned status 'resolved'. Composer tool input may stage pending "
                "review requirements only; resolved review metadata may only be written by "
                "resolve_interpretation_event."
            )
        resolver_owned_fields = sorted(field for field in _RESOLVER_OWNED_INTERPRETATION_REQUIREMENT_FIELDS if field in requirement)
        if resolver_owned_fields:
            field_names = ", ".join(resolver_owned_fields)
            return (
                f"{tool_name} options.{INTERPRETATION_REQUIREMENTS_KEY}[{index}] includes "
                f"resolver-owned field(s): {field_names}. Composer tool input may supply "
                "only kind, user_term, and draft; resolver-owned review metadata may only "
                "be written by resolve_interpretation_event."
            )
        if set(requirement) != _AUTHOR_OWNED_INTERPRETATION_REQUIREMENT_FIELDS:
            return malformed_error
        kind = requirement["kind"]
        user_term = requirement["user_term"]
        draft = requirement["draft"]
        server_staged_auto_wire = type(user_term) is ServerStagedRequiredControlUserTerm
        if (
            type(kind) is not str
            or not kind.strip()
            or (type(user_term) is not str and not server_staged_auto_wire)
            or not user_term.strip()
            or type(draft) is not str
            or not draft.strip()
        ):
            return malformed_error
        if user_term == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM and not server_staged_auto_wire:
            return (
                f"{tool_name} options.{INTERPRETATION_REQUIREMENTS_KEY}[{index}] uses "
                f"server-owned user_term '{REQUIRED_CONTROL_AUTO_WIRED_USER_TERM}'. "
                "Only the required-control finalizer may stage this disclosure."
            )
        try:
            parsed_kind = InterpretationKind(kind)
        except ValueError:
            return malformed_error
        if parsed_kind is InterpretationKind.PIPELINE_DECISION and not server_staged_auto_wire:
            current_registration_error = composer_pipeline_decision_user_term_error(
                user_term=user_term,
                context=f"{tool_name} options.{INTERPRETATION_REQUIREMENTS_KEY}[{index}]",
            )
            if registration_error is None:
                registration_error = current_registration_error
        normalized_user_term = user_term.strip()
        kind_term = (kind, normalized_user_term)
        if kind_term in seen_kind_terms:
            return collision_error
        seen_kind_terms.add(kind_term)
        if component_id is None and normalized_user_term in seen_normalized_user_terms:
            # Without a component ID the exact projection suffix is unknown,
            # but the projection omits kind, so equal normalized terms always
            # collide for every component.
            return collision_error
        seen_normalized_user_terms.add(normalized_user_term)
        if component_id is not None:
            projected_id = _authored_interpretation_requirement_id(
                component_id=component_id,
                user_term=user_term,
                source=source,
            )
            if projected_id in seen_projected_ids:
                return collision_error
            seen_projected_ids.add(projected_id)
    return registration_error


def _canonical_interpretation_requirement_error(
    options: Mapping[str, Any],
    *,
    tool_name: str,
) -> str | None:
    """Enforce the canonical per-component interpretation-review invariant.

    This is stage B of authoring admission. Unlike the raw compact-shell gate,
    it is unconditional: public writes, trusted proposal replay, reviewed
    sources, and internal reconciliation must all pass after canonicalization,
    merging, and automatic review staging.
    """
    if INTERPRETATION_REQUIREMENTS_KEY not in options:
        return None

    error = f"{tool_name}: interpretation_requirements_invalid: canonical interpretation requirements failed invariant validation."
    requirements_value = options[INTERPRETATION_REQUIREMENTS_KEY]
    if type(requirements_value) not in (list, tuple):
        return error

    for requirement in requirements_value:
        if not isinstance(requirement, Mapping):
            return error
        if set(requirement) != _CANONICAL_INTERPRETATION_REQUIREMENT_FIELDS:
            return error
        if type(requirement["id"]) is not str or not requirement["id"].strip():
            return error
        if type(requirement["kind"]) is not str:
            return error
        if type(requirement["user_term"]) is not str or not requirement["user_term"].strip():
            return error
        if type(requirement["status"]) is not str:
            return error
        draft = requirement["draft"]
        if draft is not None and type(draft) is not str:
            return error
        for field_name in (
            "event_id",
            "accepted_value",
            "accepted_artifact_hash",
            "resolved_prompt_template_hash",
        ):
            field_value = requirement[field_name]
            if field_value is not None and type(field_value) is not str:
                return error

    try:
        requirements = parse_interpretation_requirements(options)
    except (KeyError, TypeError, ValueError):
        return error
    if requirements is None:
        return error

    seen_ids: set[str] = set()
    seen_kind_terms: set[tuple[str, str]] = set()
    for requirement in requirements:
        requirement_id = requirement["id"]
        kind_term = (requirement["kind"], requirement["user_term"].strip())
        if requirement_id in seen_ids or kind_term in seen_kind_terms:
            return error
        seen_ids.add(requirement_id)
        seen_kind_terms.add(kind_term)

        if requirement["status"] == "pending":
            if type(requirement["draft"]) is not str or not requirement["draft"].strip():
                return error
            if (
                requirement["event_id"] is not None
                or requirement["accepted_value"] is not None
                or requirement["accepted_artifact_hash"] is not None
                or requirement["resolved_prompt_template_hash"] is not None
            ):
                return error
            continue

        if type(requirement["event_id"]) is not str or not requirement["event_id"].strip():
            return error
        if type(requirement["accepted_value"]) is not str:
            return error
        kind = InterpretationKind(requirement["kind"])
        if kind in (
            InterpretationKind.INVENTED_SOURCE,
            InterpretationKind.PIPELINE_DECISION,
        ):
            if (
                type(requirement["accepted_artifact_hash"]) is not str
                or not requirement["accepted_artifact_hash"].strip()
                or requirement["resolved_prompt_template_hash"] is not None
            ):
                return error
        elif (
            type(requirement["resolved_prompt_template_hash"]) is not str
            or not requirement["resolved_prompt_template_hash"].strip()
            or requirement["accepted_artifact_hash"] is not None
        ):
            return error

    return None


def _composition_canonical_interpretation_requirement_error(
    state: CompositionState,
    *,
    tool_name: str,
) -> str | None:
    """Apply canonical invariant B to every review-bearing component."""
    for source in state.sources.values():
        error = _canonical_interpretation_requirement_error(
            source.options,
            tool_name=tool_name,
        )
        if error is not None:
            return error
    for node in state.nodes:
        error = _canonical_interpretation_requirement_error(
            node.options,
            tool_name=tool_name,
        )
        if error is not None:
            return error
    for output in state.outputs:
        if INTERPRETATION_REQUIREMENTS_KEY in output.options:
            return f"{tool_name}: interpretation_requirements_invalid: canonical interpretation requirements failed invariant validation."
    return None


def _normalize_trusted_legacy_interpretation_requirements(
    options: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Expand trusted legacy pending rows to the exact canonical shape once.

    Only rows whose keys are a strict subset of the canonical schema and that
    already carry the five historical identity/state fields are eligible.
    Public input never reaches this helper. Any unknown field or parse failure
    is preserved unchanged so unconditional invariant B rejects it.
    """
    if INTERPRETATION_REQUIREMENTS_KEY not in options:
        return options
    requirements_value = options[INTERPRETATION_REQUIREMENTS_KEY]
    if type(requirements_value) not in (list, tuple):
        return options
    required_legacy_fields = {"id", "kind", "user_term", "draft", "status"}
    needs_normalization = False
    for requirement in requirements_value:
        if not isinstance(requirement, Mapping):
            return options
        fields = set(requirement)
        if not required_legacy_fields <= fields or not fields <= _CANONICAL_INTERPRETATION_REQUIREMENT_FIELDS:
            return options
        needs_normalization = needs_normalization or fields != _CANONICAL_INTERPRETATION_REQUIREMENT_FIELDS
    if not needs_normalization:
        return options
    try:
        requirements = parse_interpretation_requirements(options)
    except (KeyError, TypeError, ValueError):
        return options
    if requirements is None:
        return options
    normalized = dict(options)
    normalized[INTERPRETATION_REQUIREMENTS_KEY] = [dict(requirement) for requirement in requirements]
    return normalized


def _canonicalize_authored_interpretation_requirements(
    options: Mapping[str, Any],
    *,
    component_id: str,
    source: bool = False,
    existing_options: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Fill resolver-owned pending identity after authoring admission.

    Callers must run :func:`_resolver_owned_interpretation_requirement_error`
    against the untrusted delta first. This function is the trusted transition
    from the compact authoring shell to the canonical persisted pending shape;
    the resolver's later resolved output never passes back through authoring
    admission.
    """
    if INTERPRETATION_REQUIREMENTS_KEY not in options:
        return options
    requirements = options[INTERPRETATION_REQUIREMENTS_KEY]
    if type(requirements) is not list:
        raise AssertionError("interpretation requirements must be admitted before canonicalization")
    existing_ids: dict[tuple[str, str], str] = {}
    ambiguous_existing_keys: set[tuple[str, str]] = set()
    existing_requirements = (
        existing_options[INTERPRETATION_REQUIREMENTS_KEY]
        if existing_options is not None and INTERPRETATION_REQUIREMENTS_KEY in existing_options
        else None
    )
    if isinstance(existing_requirements, (list, tuple)):
        for existing in existing_requirements:
            if not isinstance(existing, Mapping):
                continue
            existing_kind = existing.get("kind")
            existing_user_term = existing.get("user_term")
            existing_id = existing.get("id")
            if type(existing_kind) is not str or not existing_kind.strip():
                continue
            if type(existing_user_term) is not str or not existing_user_term.strip():
                continue
            if type(existing_id) is not str or not existing_id.strip():
                continue
            key = (existing_kind, existing_user_term.strip())
            if key in existing_ids and existing_ids[key] != existing_id:
                ambiguous_existing_keys.add(key)
                continue
            existing_ids[key] = existing_id
    for key in ambiguous_existing_keys:
        del existing_ids[key]

    canonical_requirements: list[InterpretationRequirement] = []
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            raise AssertionError("interpretation requirement entries must be admitted before canonicalization")
        kind = requirement["kind"]
        user_term = requirement["user_term"]
        if type(kind) is not str or (type(user_term) is not str and type(user_term) is not ServerStagedRequiredControlUserTerm):
            raise AssertionError("interpretation requirement kind/user_term must be admitted before canonicalization")
        persisted_user_term = str(user_term)
        requirement_id = existing_ids.get(
            (kind, persisted_user_term.strip()),
            _authored_interpretation_requirement_id(
                component_id=component_id,
                user_term=persisted_user_term,
                source=source,
            ),
        )
        draft = requirement["draft"]
        if type(draft) is not str:
            raise AssertionError("interpretation requirement draft must be admitted before canonicalization")
        canonical_requirements.append(
            _pending_interpretation_requirement(
                requirement_id=requirement_id,
                kind=InterpretationKind(kind),
                user_term=persisted_user_term,
                draft=draft,
            )
        )
    canonical_options = dict(options)
    canonical_options[INTERPRETATION_REQUIREMENTS_KEY] = canonical_requirements
    return canonical_options


def _runtime_owned_llm_option_error(
    plugin_name: str | None,
    options: Mapping[str, Any],
    *,
    tool_name: str,
    interpretation_requirements_are_internal: bool = False,
    component_id: str | None = None,
) -> str | None:
    """Reject composer-authored writes to runtime-owned LLM audit fields.

    Two checks: (1) the LLM-only runtime-owned option keys
    (``_RUNTIME_OWNED_LLM_OPTION_KEYS``, e.g. ``resolved_prompt_template_hash``
    at the top level), gated on ``plugin_name == "llm"``; and (2) the
    plugin-agnostic resolver-owned interpretation-requirement check, which also
    guards source write paths via
    :func:`_resolver_owned_interpretation_requirement_error`.
    """
    if not interpretation_requirements_are_internal:
        interpretation_error = _resolver_owned_interpretation_requirement_error(
            options,
            tool_name=tool_name,
            component_id=component_id,
        )
        if interpretation_error is not None:
            return interpretation_error
    if plugin_name != "llm":
        return None
    supplied = sorted(key for key in _RUNTIME_OWNED_LLM_OPTION_KEYS if key in options)
    if supplied:
        field_names = ", ".join(supplied)
        return (
            f"{tool_name} options include runtime-owned LLM option(s): {field_names}. "
            "These audit-link fields may only be written by resolve_interpretation_event, "
            "not by composer tool input."
        )

    return None


def _secret_ref_placement_error(
    plugin_type: PluginKind,
    plugin_name: str,
    options: dict[str, Any],
) -> str | None:
    """Return a policy error for secret_ref markers in non-credential fields."""
    secret_ref_placement_violations = collect_disallowed_secret_ref_markers(
        options,
        additional_allowed_fields=allowed_secret_ref_fields(plugin_type, plugin_name),
    )
    if not secret_ref_placement_violations:
        return None

    violation_text = ", ".join(f"{v.field_path} -> {v.secret_name}" for v in secret_ref_placement_violations)
    allowed_text = allowed_secret_ref_fields_text(plugin_type, plugin_name)
    return (
        f"Invalid secret_ref placement for {plugin_type} '{plugin_name}': {violation_text}; "
        "only credential-bearing fields may carry secret_ref markers. "
        f"Allowed credential-bearing fields: {allowed_text}."
    )


_WEB_ONLY_SOURCE_KEYS = frozenset({"blob_ref", SOURCE_AUTHORING_KEY})


def _source_options_for_prevalidation(options: Mapping[str, Any]) -> dict[str, Any]:
    """Strip source blob-binding metadata before plugin config validation."""
    filtered = strip_authoring_options(options)
    for key in _WEB_ONLY_SOURCE_KEYS:
        if key in filtered:
            del filtered[key]
    if options.get("blob_ref") is not None and options.get("mode") == "bind_source" and "mode" in filtered:
        del filtered["mode"]
    return filtered


_WRITE_COLLISION_POLICIES = frozenset({"fail_if_exists", "auto_increment"})

_APPEND_COLLISION_POLICIES = frozenset({"append_or_create"})


_FIELD_OPTION_PLACEHOLDER = "line_text"


def _sink_required_option_keys(plugin_name: str) -> frozenset[str]:
    """Return the option keys this sink's own config model marks required.

    Read from ``model_json_schema()`` — the accessor ``PluginConfigProtocol``
    actually declares — rather than from ``model_fields``, which the protocol
    does not promise. It also returns keys in the OPTION namespace the repair
    object is written in: ``TextSinkConfig``/``DocumentSinkConfig`` name the
    attribute ``schema_config`` but the option key is ``schema``, and the JSON
    Schema carries the latter.
    """
    config_model = get_sink_config_model(plugin_name)
    if config_model is None:
        return frozenset()
    required = config_model.model_json_schema().get("required", ())
    return frozenset(key for key in required if type(key) is str)


class _FileSinkRepairOptions(TypedDict):
    """The options object a file-sink repair hint suggests.

    ``field`` is ``NotRequired`` because it is genuinely conditional: only a
    single-value sink (``text``, ``document``) requires one, and that is read
    from the sink's own config model rather than fixed here.
    """

    path: str
    schema: dict[str, str]
    mode: str
    collision_policy: str
    field: NotRequired[str]


def _file_sink_repair_options(plugin_name: str, *, path: str) -> _FileSinkRepairOptions:
    """Build repair options for a file sink that actually pass validation.

    DERIVED, not enumerated. The two gates whose rejection produces this hint
    are ``_prevalidate_sink`` (the sink's own config model) and
    ``validate_composer_file_sink_collision_policy``; a suggestion that cannot
    clear both is the authoring-surface defect this hint exists to repair, so
    it is built from what those gates actually demand:

    * ``field`` comes from the config model's own required set, so ``text`` and
      ``document`` both get it and the NEXT single-field sink inherits it for
      free. Naming the sinks instead is what silently rotted: ``document``
      shipped requiring ``field``, fell through to the generic branch that
      omitted it, and the suggested repair could not validate.
    * ``mode`` is emitted even though every file-sink config model defaults it,
      because ``validate_composer_file_sink_collision_policy`` requires it
      EXPLICITLY for every ``FILE_SINK_PLUGINS`` member — a deliberate
      operator decision (truncate vs. append), not an inferable one. The
      generic branch omitted it, so the csv and json suggestions were invalid
      on that gate too.
    """
    options: _FileSinkRepairOptions = {
        "path": path,
        "schema": {"mode": "observed"},
        "mode": "write",
        "collision_policy": "auto_increment",
    }
    if "field" in _sink_required_option_keys(plugin_name):
        options["field"] = _FIELD_OPTION_PLACEHOLDER
    return options


def _missing_output_options_repair_error(
    *,
    sink_name: str,
    plugin_name: str,
    on_write_failure: str,
    validation_error: str | None,
) -> str:
    """Return an exact output-object repair hint for omitted sink options."""
    if plugin_name in FILE_SINK_REPAIR_EXTENSIONS:
        path_fragment = _repair_identifier_fragment(sink_name, fallback="output")
        extension = FILE_SINK_REPAIR_EXTENSIONS[plugin_name]
        options = _file_sink_repair_options(
            plugin_name,
            path=f"outputs/{path_fragment}.{extension}",
        )
        repair_output = {
            "sink_name": sink_name,
            "plugin": plugin_name,
            "options": options,
            "on_write_failure": on_write_failure,
        }
        detail = f" Empty options were rejected: {validation_error}" if validation_error is not None else ""
        option_list = ", ".join(options)
        field_note = f" Replace {_FIELD_OPTION_PLACEHOLDER} with the actual selected string field." if "field" in options else ""
        return (
            f"Output '{sink_name}' is missing options. For {plugin_name} file sinks, include "
            f"an options object with {option_list}. Use this runnable output object and adjust "
            f"the path/schema if needed: {json.dumps(repair_output)}.{field_note}{detail}"
        )

    repair_output = {
        "sink_name": sink_name,
        "plugin": plugin_name,
        "options": {},
        "on_write_failure": on_write_failure,
    }
    detail = f" Empty options were rejected: {validation_error}" if validation_error is not None else ""
    return (
        f"Output '{sink_name}' is missing options. Include the sink plugin's options object. "
        f"If this sink accepts empty configuration, use: {json.dumps(repair_output)}; otherwise "
        f"call get_plugin_schema for sink '{plugin_name}' and fill the required options.{detail}"
    )


def validate_composer_file_sink_collision_policy(
    plugin_name: str,
    options: Mapping[str, Any],
    *,
    require_explicit: bool,
) -> str | None:
    """Require generated runnable file sinks to choose collision behavior."""
    if not require_explicit or plugin_name not in FILE_SINK_PLUGINS:
        return None

    if "collision_policy" not in options:
        return (
            f"File sink '{plugin_name}' must set collision_policy explicitly. "
            "Use 'fail_if_exists' to refuse a taken output path, "
            "'auto_increment' to choose a free sibling path, or "
            "'append_or_create' with mode='append'."
        )

    # mode is a safety-critical operator decision (truncate vs. append) — same
    # rationale as the collision_policy presence check above.  Mirroring
    # ``csv_sink.py:57`` / ``json_sink.py:63``'s ``Field(default="write")``
    # via ``options.get("mode", "write")`` here would silently paper over
    # every upstream null source — LLM omission, operator omission,
    # merge-patch strip, incomplete fixture — none of which is a correct
    # state for a runnable file sink at this validator's call sites.  The
    # operator-supplied options must name ``mode`` explicitly so the
    # write-vs-append branch selection below is authoritative rather than
    # inferred.  Closes I3 review finding (2026-05-24).
    if "mode" not in options:
        return (
            f"File sink '{plugin_name}' must set mode explicitly. "
            "Use 'write' to create or replace the file, or 'append' to "
            "add rows to an existing file."
        )

    mode = options["mode"]
    policy = options["collision_policy"]
    if mode == "append":
        if policy not in _APPEND_COLLISION_POLICIES:
            return f"File sink '{plugin_name}' with mode='append' must use collision_policy='append_or_create'."
    else:
        if policy not in _WRITE_COLLISION_POLICIES:
            return (
                f"File sink '{plugin_name}' with mode='write' must use "
                "collision_policy='fail_if_exists' or collision_policy='auto_increment'."
            )

    return None


def _prevalidate_source(
    plugin_name: str,
    options: Mapping[str, Any],
    on_validation_failure: str = _DEFAULT_SOURCE_VALIDATION_FAILURE,
) -> str | None:
    """Pre-validate source options, injecting on_validation_failure and filtering web-only keys."""
    filtered = _source_options_for_prevalidation(options)
    return _prevalidate_plugin_options(
        "source",
        plugin_name,
        filtered,
        injected_fields={"on_validation_failure": on_validation_failure},
    )


def _prevalidate_source_for_context(
    context: ToolContext,
    plugin_name: str,
    options: Mapping[str, Any],
    on_validation_failure: str = _DEFAULT_SOURCE_VALIDATION_FAILURE,
    *,
    source_name: str = "source",
) -> str | None:
    """Validate one candidate source through the shared profile adapter.

    Profile lowering is an in-memory validation projection only.  The caller
    persists its original authored ``options``; this helper validates the
    corresponding executable provider binding without returning or exposing
    that private projection.
    """
    if "on_validation_failure" in options and options["on_validation_failure"] != on_validation_failure:
        return (
            f"Invalid options for source '{plugin_name}': options.on_validation_failure conflicts with "
            "the source routing field on_validation_failure"
        )
    profile_options = {
        **deep_thaw(options),
        "on_validation_failure": on_validation_failure,
    }
    candidate = CompositionState(
        sources={
            source_name: SourceSpec(
                plugin=plugin_name,
                on_success="discard",
                options=profile_options,
                on_validation_failure=on_validation_failure,
            )
        },
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    try:
        profile_validation = context.catalog.validate_composition_state(candidate)
    except ValueError as exc:
        return f"Invalid options for source '{plugin_name}': {exc}"
    blocking = tuple(
        finding for finding in profile_validation.policy_findings if finding.stage in {"plugin_enablement", "operator_profile_options"}
    )
    if blocking:
        return f"Invalid options for source '{plugin_name}': {blocking[0].error_code} — {blocking[0].message}"
    executable_source = profile_validation.executable_state.sources[source_name]
    if type(executable_source.on_validation_failure) is not str or executable_source.on_validation_failure != on_validation_failure:
        return f"Invalid options for source '{plugin_name}': profile lowering changed on_validation_failure"
    executable_options = deep_thaw(executable_source.options)
    if "on_validation_failure" in executable_options:
        executable_on_validation_failure = executable_options.pop("on_validation_failure")
        if type(executable_on_validation_failure) is not str or executable_on_validation_failure != on_validation_failure:
            return f"Invalid options for source '{plugin_name}': profile lowering changed on_validation_failure"
    return _prevalidate_source(plugin_name, executable_options, on_validation_failure)


def _prevalidate_transform(plugin_name: str, options: Mapping[str, Any]) -> str | None:
    """Pre-validate transform options."""
    return _prevalidate_plugin_options("transform", plugin_name, strip_authoring_options(options))


def _prevalidate_transform_for_context(
    context: ToolContext,
    plugin_name: str,
    options: Mapping[str, Any],
) -> str | None:
    """Validate one candidate transform through the shared profile adapter."""
    candidate = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="profile_prevalidation_in",
            options={"schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="profile_prevalidation",
                node_type="transform",
                plugin=plugin_name,
                input="profile_prevalidation_in",
                on_success="profile_prevalidation_out",
                on_error="discard",
                options=options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(
            OutputSpec(
                name="profile_prevalidation_out",
                plugin="json",
                options={"schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )
    try:
        profile_validation = context.catalog.validate_composition_state(candidate)
    except ValueError as exc:
        # The profile adapter dispatches provider-specific config models
        # (llm get_config_model) BEFORE the guarded prevalidation core — a
        # Tier-3-authored unknown provider raised straight through the
        # candidate boundary (pack pressure-suite run 2 grader escape;
        # planner path degraded it to the unrepairable
        # CANDIDATE_CONSTRUCTION_ERROR, non-planner tool paths 500'd).
        # Mirror the core's own idiom: surface it as an options message.
        return f"Invalid options for transform '{plugin_name}': {exc}"
    blocking = tuple(
        finding for finding in profile_validation.policy_findings if finding.stage in {"plugin_enablement", "operator_profile_options"}
    )
    if blocking:
        # Carry the finding's own explanation — the bare error_code alone
        # (e.g. "profile_unavailable") tells neither the model nor the user
        # what is actually switched off.
        return f"Invalid options for transform '{plugin_name}': {blocking[0].error_code} — {blocking[0].message}"
    alias = options["profile"] if "profile" in options else plugin_name
    if not isinstance(alias, str):
        return f"Invalid options for transform '{plugin_name}': profile_unavailable"
    return _prevalidate_transform(plugin_name, profile_validation.executable_state.nodes[0].options)


def _prevalidate_sink(plugin_name: str, options: dict[str, Any]) -> str | None:
    """Pre-validate sink options."""
    return _prevalidate_plugin_options("sink", plugin_name, options)


# Type aliases shared by ``_dispatch`` and ``generation`` (and any plane that
# needs to talk about runtime-preflight callables or generic tool handlers).

RuntimePreflight = Callable[[CompositionState], ValidationResult]


@dataclass(frozen=True, slots=True)
class ReviewedSourceAuthority:
    """Session-bound private authority for reusing already-reviewed sources.

    This object is deliberately not a serialisable planner or event payload.
    Only the guided settlement path constructs it, and the candidate boundary
    accepts it only for the same session and an exact reviewed source record.
    """

    session_id: str
    reviewed_anchor_hash: str
    reviewed_sources: Mapping[str, Any]
    verified_blob_paths: Mapping[str, str]

    def __post_init__(self) -> None:
        if type(self.session_id) is not str or not self.session_id:
            raise TypeError("ReviewedSourceAuthority.session_id must be a non-empty exact str")
        if type(self.reviewed_anchor_hash) is not str or not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_anchor_hash):
            raise TypeError("ReviewedSourceAuthority.reviewed_anchor_hash must be a SHA-256 hash")
        if type(self.reviewed_sources) not in (dict, MappingProxyType):
            raise TypeError("ReviewedSourceAuthority.reviewed_sources must be a mapping")
        if any(type(stable_id) is not str or not stable_id for stable_id in self.reviewed_sources):
            raise TypeError("ReviewedSourceAuthority.reviewed_sources keys must be non-empty exact strings")
        if any(not isinstance(source, Mapping) for source in self.reviewed_sources.values()):
            raise TypeError("ReviewedSourceAuthority.reviewed_sources values must be mappings")
        if type(self.verified_blob_paths) not in (dict, MappingProxyType):
            raise TypeError("ReviewedSourceAuthority.verified_blob_paths must be a mapping")
        if any(
            type(locator) is not str or not locator.startswith("blob:") or type(storage_path) is not str or not storage_path
            for locator, storage_path in self.verified_blob_paths.items()
        ):
            raise TypeError("ReviewedSourceAuthority.verified_blob_paths is malformed")
        freeze_fields(self, "reviewed_sources", "verified_blob_paths")


@dataclass(frozen=True, slots=True)
class PendingCustodyBlobView:
    """One deferred inline-custody blob, resolvable before it is settled.

    Guided-full defers inline-custody finalization into the atomic staging
    settlement (elspeth-1e3ad83d89), so at custody-safe revalidation time the
    proposal's ``source.blob_id`` names a blob with no row and no storage
    file yet. This view carries the settlement-equivalent row fields plus the
    content bytes so ``_resolve_source_blob`` can resolve exactly that one
    blob (elspeth-282f392fae). Every field is derived server-side from the
    planner's own ``PipelineCustodyPreparation`` — never from tool arguments —
    and resolution requires an exact ``blob_id`` AND ``session_id`` match; any
    other reference falls through to the normal fail-closed database path.
    """

    blob_id: str
    session_id: str
    filename: str
    mime_type: str
    size_bytes: int
    content_hash: str
    storage_path: str
    source_description: str | None
    creation_modality: str
    created_from_message_id: str
    creating_model_identifier: str | None
    creating_model_version: str | None
    creating_provider: str | None
    creating_composer_skill_hash: str | None
    creating_arguments_hash: str | None
    content: bytes

    def __post_init__(self) -> None:
        if type(self.blob_id) is not str or not self.blob_id:
            raise TypeError("PendingCustodyBlobView.blob_id must be a non-empty exact string")
        if type(self.session_id) is not str or not self.session_id:
            raise TypeError("PendingCustodyBlobView.session_id must be a non-empty exact string")
        if type(self.filename) is not str or not self.filename:
            raise TypeError("PendingCustodyBlobView.filename must be a non-empty exact string")
        if type(self.mime_type) is not str or not self.mime_type:
            raise TypeError("PendingCustodyBlobView.mime_type must be a non-empty exact string")
        if type(self.content_hash) is not str or not self.content_hash:
            raise TypeError("PendingCustodyBlobView.content_hash must be a non-empty exact string")
        if type(self.storage_path) is not str or not self.storage_path:
            raise TypeError("PendingCustodyBlobView.storage_path must be a non-empty exact string")
        if type(self.creation_modality) is not str or not self.creation_modality:
            raise TypeError("PendingCustodyBlobView.creation_modality must be a non-empty exact string")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise TypeError("PendingCustodyBlobView.size_bytes must be a non-negative exact integer")
        if type(self.content) is not bytes:
            raise TypeError("PendingCustodyBlobView.content must be exact bytes")
        if len(self.content) != self.size_bytes:
            raise ValueError("PendingCustodyBlobView.size_bytes must equal len(content)")


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Immutable per-call context threaded through every ``execute_tool``
    dispatch.

    Collapsing the previously-divergent kwarg surfaces of the six sync tool
    registries (and the three hardcoded ``if tool_name == ...`` branches for
    ``preview_pipeline`` / ``diff_pipeline`` / ``set_pipeline``) into a
    single frozen dataclass means every handler takes the same shape:
    ``(arguments, state, context) -> ToolResult``. The previous per-registry
    kwarg gymnastics is reduced to "the handler reads what it needs off
    ``context``".

    Fields:
        catalog: The catalog service the tool consults for plugin metadata.
        data_dir: Base data directory enforced for S2 path allowlist checks
            on source/sink options. ``None`` when the caller is not a web
            request (legacy direct tests).
        require_data_dir_for_paths: Fail closed when a source-local path
            option appears without ``data_dir``. Enabled for audited web/LLM
            dispatches.
        session_engine: SQLAlchemy engine for the session database. Required
            for blob tools to perform synchronous lookups; ``None`` for
            non-session callers.
        session_id: Current session ID. Required for blob tools.
        secret_service: ``WebSecretResolver`` (L0 protocol from
            ``elspeth.contracts.secrets``) — the auth-scoped resolver
            surface composer tools consult.  Production wiring passes
            ``ScopedSecretResolver`` (``elspeth.web.secrets.service``),
            which binds the deployment's ``auth_provider_type`` so the
            composer plane never has to know about it.  Required for
            secret tools (``list_secret_refs`` / ``validate_secret_ref``
            / ``wire_secret_ref``); ``None`` for non-secret-aware callers.
        user_id: Current user ID. Required for secret tools.
        baseline: Baseline state for ``diff_pipeline`` comparisons.
        current_validation: Pre-computed validation of the live state, used
            by ``diff_pipeline`` so its delta is computed against the same
            ValidationSummary the caller is already holding.
        runtime_preflight: Optional callback for runtime-equivalent
            preflight, applied only to ``preview_pipeline``. Pre-computed in
            the async compose loop and injected here as a cheap synchronous
            callback so ``execute_tool`` stays synchronous.
        max_blob_storage_per_session_bytes: Configured per-session blob
            storage quota for assistant-created session artifacts. Defaults
            to ``None`` (no override) so the blob plane can fall back to its
            historical BlobServiceImpl-compatible value for direct tests
            and non-web callers.
        user_message_id: Provenance pointer for blob writes that record
            ``created_from_message_id``. Only handlers that actually persist
            a new blob row read it.
        user_message_content: Triggering user chat-message content. Blob
            writers use this to distinguish byte-identical user-authored
            content from composer-authored content.
        composer_model_identifier: Requested composer model identifier for
            LLM-authored blob provenance.
        composer_model_version: Provider-returned model/version string when
            available, falling back to the requested model.
        composer_provider: Composer LLM provider name.
        composer_skill_hash: SHA-256 hash of the composer skill markdown used
            for the request.
        tool_arguments_hash: Canonical audited hash of the tool-call
            arguments that produced an LLM-authored blob.
        reviewed_source_authority: Private session-bound reviewed source
            authority. Generic/manual callers leave this unset and therefore
            remain subject to the normal fail-closed custody checks.
        executing_proposal_id: The pending proposal this dispatch is
            executing (proposal-accept replay). The blob retention guard
            excludes exactly this proposal so an update_blob proposal can
            be accepted without being blocked by its own retention edge;
            every OTHER pending proposal still blocks.
        _interpretation_requirements_are_internal: Private server-owned
            allowance for revalidating a proposal that already crossed the
            public compact-shell admission boundary. Public tool arguments
            cannot set context fields.
    """

    catalog: PolicyCatalogView
    plugin_snapshot: PluginAvailabilitySnapshot
    data_dir: str | None = None
    require_data_dir_for_paths: bool = False
    session_engine: Engine | None = None
    session_id: str | None = None
    secret_service: WebSecretResolver | None = None
    user_id: str | None = None
    baseline: CompositionState | None = None
    current_validation: ValidationSummary | None = None
    runtime_preflight: RuntimePreflight | None = None
    max_blob_storage_per_session_bytes: int | None = None
    user_message_id: str | None = None
    user_message_content: str | None = None
    composer_model_identifier: str | None = None
    composer_model_version: str | None = None
    composer_provider: str | None = None
    composer_skill_hash: str | None = None
    tool_arguments_hash: str | None = None
    reviewed_source_authority: ReviewedSourceAuthority | None = None
    executing_proposal_id: str | None = None
    _interpretation_requirements_are_internal: bool = False
    # Private server-owned field, set ONLY by the planner's deferred
    # custody-safe revalidation (elspeth-282f392fae): the one inline-custody
    # blob this plan will settle atomically at staging. _resolve_source_blob
    # may resolve exactly this blob_id/session_id pair from the view; every
    # other blob reference keeps the fail-closed database path.
    _pending_custody: PendingCustodyBlobView | None = None


ToolHandler = Callable[
    [dict[str, Any], CompositionState, ToolContext],
    ToolResult,
]


def normalize_tool_result_validation(
    result: ToolResult,
    catalog: PolicyCatalogView,
) -> ToolResult:
    """Normalize a result through the request-scoped profile authority once.

    Handlers remain small state-transition functions and may construct their
    provisional envelope with ``CompositionState.validate()``. Candidate
    builders may call this authority before dispatch so they can make an
    honest accept/reject decision. The outward dispatch boundary calls it
    again to verify the result, but reuses validation already produced for
    the same immutable plugin-availability snapshot. A different snapshot
    always revalidates.
    """
    snapshot_hash = catalog.snapshot.snapshot_hash
    if result._validation_snapshot_hash == snapshot_hash:
        return result
    rejections = tuple(entry for entry in result.validation.errors if entry.component == "rejected_mutation")
    if rejections and result._state_validation_withheld:
        # Full-replacement rejection: updated_state is the untouched input
        # state whose validate() entries the producer deliberately withheld,
        # so re-validating it here would reattach exactly those stale-state
        # errors (elspeth-e89e6bf47a). The rejection entries are
        # snapshot-independent.
        return replace(
            result,
            validation=ValidationSummary(is_valid=False, errors=rejections),
            _validation_snapshot_hash=snapshot_hash,
        )
    shared = catalog.validate_composition_state(result.updated_state).validation
    if rejections:
        shared = replace(
            shared,
            is_valid=False,
            errors=(*rejections, *shared.errors),
        )
    return replace(result, validation=shared, _validation_snapshot_hash=snapshot_hash)


class _SetPipelineNodePayload(TypedDict):
    """Exact node shape reconstructed for a set_pipeline request."""

    id: str
    node_type: NodeType
    plugin: str | None
    input: str
    on_success: str | None
    on_error: str | None
    options: dict[str, JsonValue]
    condition: str | None
    routes: dict[str, str] | None
    fork_to: list[str] | None
    branches: list[str] | dict[str, str] | None
    policy: str | None
    merge: str | None
    trigger: dict[str, JsonValue] | None
    output_mode: str | None
    expected_output_count: int | None
    timeout_seconds: float | None
    description: str | None


def _serialize_authoring_options(options: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Strip all resolver-owned review fields from public authoring shells."""
    serialized = cast(dict[str, JsonValue], deep_thaw(serialize_authoring_review_options(options)))
    if INTERPRETATION_REQUIREMENTS_KEY in serialized:
        requirements = cast(list[dict[str, JsonValue]], serialized[INTERPRETATION_REQUIREMENTS_KEY])
        serialized[INTERPRETATION_REQUIREMENTS_KEY] = [
            {
                "kind": requirement["kind"],
                "user_term": requirement["user_term"],
                "draft": requirement["draft"],
            }
            for requirement in requirements
        ]
    return serialized


def _serialize_set_pipeline_node(node: NodeSpec) -> _SetPipelineNodePayload:
    payload = cast(_SetPipelineNodePayload, _serialize_node(node))
    payload["options"] = _serialize_authoring_options(node.options)
    return payload


_ROW_UNION_INTRINSIC_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "row_union_config_invalid",
        "row_union_branches_invalid",
        "row_union_branch_invalid",
        "row_union_input_mismatch",
        "row_union_on_success_invalid",
        "row_union_timeout_invalid",
    }
)

_MUTATION_BLOCKING_INVARIANT_CODES: Final[frozenset[str]] = _ROW_UNION_INTRINSIC_ERROR_CODES | {
    "row_union_on_success_must_be_connection",
    "node_timeout_unsupported",
    # A plugin on a gate or coalesce must never persist: upsert_node's
    # post-call hint lookup would resolve the authored token against the
    # transform catalog, and downstream readers of ``plugin or node_type``
    # would classify the node by it (state.structural_node_plugin_error).
    "structural_node_plugin_forbidden",
}


def _post_mutation_invariant_error(
    proposed_state: CompositionState,
) -> tuple[str, str] | None:
    """Return an invariant a mutation must not persist.

    Composer permits incomplete topology during incremental authoring, so a
    mutation cannot require the entire pipeline to validate. This shared
    preflight selects only intrinsic node-shape and namespace invariants whose
    persistence would make later generic mutation tools violate their own
    contracts. Callers return the original state on failure, giving the tools
    one rollback discipline without weakening ordinary validation telemetry.
    """
    for entry in proposed_state.validate().errors:
        if entry.error_code in _MUTATION_BLOCKING_INVARIANT_CODES:
            assert entry.error_code is not None
            return entry.message, entry.error_code
    return None


def _row_union_node_contract_error(
    node: NodeSpec,
    *,
    output_names: frozenset[str] = frozenset(),
) -> tuple[str, str] | None:
    """Return the first intrinsic row-union authoring failure.

    Reuse ``CompositionState.validate`` as the contract authority rather than
    maintaining a second structural validator in the tool layer. Topology
    findings (unreachable branches and a not-yet-consumed output connection)
    remain incremental-authoring telemetry. A configured sink target is
    rejected here because row_union v1 may release only to processing.
    """
    if node.node_type != "row_union":
        return None
    if node.on_success in output_names:
        return (
            (
                f"row_union '{node.id}' on_success '{node.on_success}' names a sink. "
                "A released group must continue on a processing connection."
            ),
            "row_union_on_success_must_be_connection",
        )
    probe = CompositionState(
        source=None,
        nodes=(node,),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )
    for entry in probe.validate().errors:
        if entry.component == f"node:{node.id}" and entry.error_code in _ROW_UNION_INTRINSIC_ERROR_CODES:
            assert entry.error_code is not None
            return entry.message, entry.error_code
    return None


def _serialize_set_pipeline_source(
    source: SourceSpec,
    *,
    allow_blob_binding: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    options = _serialize_authoring_options(source.options)
    blob_ref = source.options["blob_ref"] if "blob_ref" in source.options else None
    if blob_ref is not None:
        if not allow_blob_binding:
            return None, "named or multiple blob-backed sources cannot round-trip through set_pipeline v1"
        if type(blob_ref) is not str or not blob_ref.strip() or is_widened_blob_ref(blob_ref):
            return None, "the default source has a non-scalar blob identity that set_pipeline v1 cannot bind safely"
        for key in ("path", "blob_ref", "mode", SOURCE_AUTHORING_KEY):
            if key in options:
                del options[key]
        return (
            {
                "plugin": source.plugin,
                "on_success": source.on_success,
                "blob_id": blob_ref,
                "options": options,
                "on_validation_failure": source.on_validation_failure,
                "description": source.description,
            },
            None,
        )
    path_value = source.options["path"] if "path" in source.options else None
    if (
        "blob_ref" in source.options
        or SOURCE_AUTHORING_KEY in source.options
        or ("mode" in source.options and source.options["mode"] == "bind_source")
        or (type(path_value) is str and "/blobs/" in path_value)
    ):
        return None, "the blob-backed source is missing a scalar blob identity that set_pipeline v1 can bind safely"
    return (
        {
            "plugin": source.plugin,
            "on_success": source.on_success,
            "options": options,
            "on_validation_failure": source.on_validation_failure,
            "description": source.description,
        },
        None,
    )


def _serialize_set_pipeline_arguments(state: CompositionState) -> tuple[dict[str, Any] | None, str | None]:
    """Build the exact public authoring payload accepted by set_pipeline."""
    try:
        payload: dict[str, Any] = {
            "nodes": [_serialize_set_pipeline_node(node) for node in state.nodes],
            "edges": [_serialize_edge(edge) for edge in state.edges],
            "outputs": [
                {
                    **_serialize_output(output),
                    "options": _serialize_authoring_options(output.options),
                }
                for output in state.outputs
            ],
            "metadata": {
                "name": state.metadata.name,
                "description": state.metadata.description,
            },
        }
    except (KeyError, TypeError, ValueError):
        return None, "resolved authoring review state is not safely reconstructible"
    if set(state.sources) == {"source"}:
        try:
            source_payload, error = _serialize_set_pipeline_source(
                state.sources["source"],
                allow_blob_binding=True,
            )
        except (KeyError, TypeError, ValueError):
            return None, "resolved authoring review state is not safely reconstructible"
        if error is not None:
            return None, error
        assert source_payload is not None
        payload["source"] = source_payload
        return payload, None

    serialized_sources: dict[str, dict[str, Any]] = {}
    for source_name, source in state.sources.items():
        try:
            source_payload, error = _serialize_set_pipeline_source(
                source,
                allow_blob_binding=False,
            )
        except (KeyError, TypeError, ValueError):
            return None, "resolved authoring review state is not safely reconstructible"
        if error is not None:
            return None, error
        assert source_payload is not None
        serialized_sources[source_name] = source_payload
    payload["sources"] = serialized_sources
    return payload, None


# Ceiling on one applied-component echo, canonically encoded. Sized against
# the provider-payload budget family in ``planner_authoring_aids`` (8 KiB
# expression grammar, 24 KiB discovery digest, 48 KiB plugin contract) rather
# than the blob-reading caps: this is a provider response surface, not a file
# read. The echo replaces a ``get_pipeline_state`` call whose payload is the
# WHOLE state in the wider diagnostic serialization, so the cap bounds a
# pathological single response — it is not a parsimony budget.
_APPLIED_COMPONENT_ECHO_MAX_CANONICAL_BYTES: Final[int] = 16 * 1024


def _applied_component_echo(
    state: CompositionState,
    affected: tuple[str, ...],
) -> Mapping[str, Any] | None:
    """Project the components a successful mutation applied, post-finalizer.

    The echo is the exact ``set_pipeline`` arguments that
    ``get_pipeline_state(component="set_pipeline_arguments")`` already serves
    to this same surface, narrowed to the components named in ``affected``
    plus the edges on their endpoints. After a successful mutation the model
    knows THAT it worked and which errors remain, but not what the server
    stored where it transformed the input — canonicalized routes, merged
    defaults, reconciled sink-mirror edges. Echoing the applied component
    closes that gap without the model spending a ``get_pipeline_state`` turn
    reading the whole document back.

    Component scope is the natural bound: an incremental mutation touches one
    component and the edges on it. Full-replacement mutations do not echo at
    all (see ``_mutation_result``'s ``full_replacement``) — there "the applied
    component" is the entire document, which is the whole-state read this
    exists to avoid.

    Returns ``None`` when nothing resolves (a removal whose subject is gone
    from the new state), when the state cannot be represented as exact
    ``set_pipeline`` arguments, or when the canonical projection exceeds
    ``_APPLIED_COMPONENT_ECHO_MAX_CANONICAL_BYTES``. An oversized echo is
    dropped WHOLE, never truncated: half a component reads as a complete one
    and would author a wrong repair.
    """
    if not affected:
        return None
    payload, round_trip_error = _serialize_set_pipeline_arguments(state)
    if round_trip_error is not None:
        # The public authoring payload is unavailable for this state; the
        # model can still get the diagnostic view (and the exact reason) from
        # get_pipeline_state. An echo is never worth a second projection.
        return None
    assert payload is not None

    state_node_ids = {node.id for node in state.nodes}
    state_output_names = {output.name for output in state.outputs}
    source_names: list[str] = []
    node_ids: set[str] = set()
    output_names: set[str] = set()
    for component in affected:
        # Source component ids are a closed spelling ('source' / 'source:<name>'),
        # so they resolve first exactly as _execute_get_pipeline_state resolves
        # its 'source' argument ahead of node and output lookup.
        source_name = source_name_from_component_id(component)
        if source_name is not None:
            if source_name in state.sources and source_name not in source_names:
                source_names.append(source_name)
            continue
        if component in state_node_ids:
            node_ids.add(component)
        elif component in state_output_names:
            output_names.add(component)

    echo: dict[str, Any] = {}
    if "source" in payload and SOURCE_COMPONENT_ID in source_names:
        echo["source"] = payload["source"]
    if "sources" in payload:
        serialized_sources = payload["sources"]
        selected = {name: serialized_sources[name] for name in source_names if name in serialized_sources}
        if selected:
            echo["sources"] = selected
    nodes = [node for node in payload["nodes"] if node["id"] in node_ids]
    if nodes:
        echo["nodes"] = nodes
    outputs = [output for output in payload["outputs"] if output["sink_name"] in output_names]
    if outputs:
        echo["outputs"] = outputs
    # Sink-targeting edges name an OUTPUT on one endpoint, so the endpoint set
    # spans all three component kinds. These edges are the mirror the mutation
    # tools reconcile server-side (_reconcile_node_sink_mirror_edges); without
    # them the echo would restate bytes the model already authored and hide the
    # one thing it did not write.
    edge_endpoints = node_ids | output_names | set(source_names)
    edges = [edge for edge in payload["edges"] if edge["from_node"] in edge_endpoints or edge["to_node"] in edge_endpoints]
    if edges:
        echo["edges"] = edges
    if not echo:
        return None

    echo = redact_source_storage_path(echo)
    if len(canonical_json(echo).encode("utf-8")) > _APPLIED_COMPONENT_ECHO_MAX_CANONICAL_BYTES:
        return None
    return echo
