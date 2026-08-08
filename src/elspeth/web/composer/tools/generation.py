"""Composer generation plane — preview, diff, explain, plugin schema, proof diagnostics."""

from __future__ import annotations

import ast
import csv
import hmac
import io
import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, TypedDict
from uuid import UUID

from opentelemetry import metrics
from sqlalchemy import Engine

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.schema import get_aggregation_contract_options, get_raw_schema_config
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.contracts.value_source import get_catalog_values
from elspeth.core.expression_parser import ExpressionEvaluationError, ExpressionParser
from elspeth.plugins.infrastructure.manager import (
    PluginNotFoundError,
    get_shared_plugin_manager,
)
from elspeth.plugins.transforms.llm.model_catalog import (
    MODEL_CATALOG_OPENROUTER,
    OPENROUTER_LITELLM_PREFIX,
    read_litellm_model_list,
)
from elspeth.web.blobs.protocol import BlobIntegrityError
from elspeth.web.catalog.protocol import PluginKind
from elspeth.web.composer._producer_resolver import ProducerEntry, ProducerResolver, is_source_producer_id, source_producer_id
from elspeth.web.composer.source_inspection import (
    SourceInspectionFacts,
    derive_extra_column_risk,
    derive_required_header_mismatch_risk,
    inspect_blob_content,
    inspect_csv_source_content,
)
from elspeth.web.composer.state import (
    CompositionState,
    NodeSpec,
    SourceSpec,
    ValidationEntryDict,
    _source_options_have_schema,
    _validate_gate_expression,
)
from elspeth.web.composer.tools._common import (
    _DATA_ERROR_KEY,
    _PLUGIN_UNAVAILABLE_EXPLANATIONS,
    ToolContext,
    ToolResult,
    _discovery_result,
    _failure_result,
    _plugin_policy_failure,
    _validate_plugin_name,
    diff_states,
)
from elspeth.web.composer.tools.blobs import (
    BlobToolRecord,
    _locked_read_ready_blob,
    _verify_blob_content_integrity,
)
from elspeth.web.composer.tools.declarations import (
    ToolDeclaration,
    ToolKind,
)
from elspeth.web.composer.tools.sessions import (
    _authoring_validation_payload,
)
from elspeth.web.interpretation_state import (
    RAW_HTML_CLEANUP_REVIEW_DRAFT,
    RAW_HTML_CLEANUP_USER_TERM,
)
from elspeth.web.plugin_policy.models import PluginUnavailableReason

_AUTHORING_VALIDATION_COUNTER = metrics.get_meter("elspeth.web.composer.tools").create_counter(
    "composer.authoring_validation.total",
    description="Total authoring (Stage 1) validation outcomes from preview_pipeline",
)


@trust_boundary(
    tier=3,
    source="CSV-source 'delimiter' option from external / LLM-authored composer source options",
    source_param="options",
    suppresses=("R1",),
    invariant="raises ValueError when 'delimiter' is present but not a single-character str; never coerces",
    test_ref="tests/unit/web/composer/test_generation_source_option_boundaries.py::test_csv_source_delimiter_rejects_non_string",
    test_fingerprint="70dcc85656bdccb86ef9d40258841bfbb2ef3123bbe836f9a7ca4266091d3716",
)
def _csv_source_delimiter(options: Mapping[str, Any]) -> str:
    raw = options.get("delimiter")
    if raw is None:
        return ","
    if type(raw) is not str:
        raise ValueError(f"csv source delimiter must be str when present; got {type(raw).__name__}")
    if len(raw) != 1:
        raise ValueError(f"csv source delimiter must be one character; got {raw!r}")
    return raw


@trust_boundary(
    tier=3,
    source="CSV-source 'skip_rows' option from external / LLM-authored composer source options",
    source_param="options",
    suppresses=("R1",),
    invariant="raises ValueError when 'skip_rows' is present but not a non-negative int; never coerces",
    test_ref="tests/unit/web/composer/test_generation_source_option_boundaries.py::test_csv_source_skip_rows_rejects_non_int",
    test_fingerprint="975c5318f2f88801e228ad4934c33851d6bf6ecb82891032a22d305ccf7b2f21",
)
def _csv_source_skip_rows(options: Mapping[str, Any]) -> int:
    raw = options.get("skip_rows")
    if raw is None:
        return 0
    if type(raw) is not int:
        raise ValueError(f"csv source skip_rows must be int when present; got {type(raw).__name__}")
    if raw < 0:
        raise ValueError(f"csv source skip_rows must be non-negative; got {raw}")
    return raw


@trust_boundary(
    tier=3,
    source="CSV-source 'columns' option from external / LLM-authored composer source options",
    source_param="options",
    suppresses=("R1", "R5"),
    invariant="raises ValueError when 'columns' is present but not a list/tuple of str; never coerces",
    test_ref="tests/unit/web/composer/test_generation_source_option_boundaries.py::test_csv_source_columns_rejects_non_sequence",
    test_fingerprint="b5bb902a03d873d14c816d002ab58e9dd8d99c5f51c2136166480bdaf6ac94e6",
)
def _csv_source_columns(options: Mapping[str, Any]) -> tuple[str, ...] | None:
    raw = options.get("columns")
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"csv source columns must be a list of strings when present; got {type(raw).__name__}")
    columns: list[str] = []
    for idx, item in enumerate(raw):
        if type(item) is not str:
            raise ValueError(f"csv source columns[{idx}] must be str; got {type(item).__name__}")
        columns.append(item)
    return tuple(columns)


@trust_boundary(
    tier=3,
    source="CSV-source 'field_mapping' option from external / LLM-authored composer source options",
    source_param="options",
    suppresses=("R5",),
    invariant="raises ValueError when 'field_mapping' is present but not a Mapping of str→str; never coerces",
    test_ref="tests/unit/web/composer/test_generation_field_mapping.py::test_csv_source_field_mapping_rejects_non_mapping",
    test_fingerprint="0b60b2320a348028e1b74cecfe0ed49335417a9844e7478f1a652eb2725e68ac",
)
def _csv_source_field_mapping(options: Mapping[str, Any]) -> dict[str, str] | None:
    raw = options["field_mapping"] if "field_mapping" in options else None
    if raw is None:
        return None
    # Accept any Mapping, not just dict: CompositionState options are deep-frozen,
    # so field_mapping arrives as a MappingProxyType. A `type(raw) is dict` check
    # wrongly rejects the frozen mapping (Tier-3 structural validation of external/
    # LLM-authored config — isinstance is the correct, subtype-aware guard here).
    if not isinstance(raw, Mapping):
        raise ValueError(f"csv source field_mapping must be a mapping when present; got {type(raw).__name__}")
    mapping: dict[str, str] = {}
    for key, value in raw.items():
        if type(key) is not str:
            raise ValueError(f"csv source field_mapping keys must be str; got {type(key).__name__}")
        if type(value) is not str:
            raise ValueError(f"csv source field_mapping[{key!r}] must be str; got {type(value).__name__}")
        mapping[key] = value
    return mapping


@trust_boundary(
    tier=3,
    source="source 'schema.required_fields' from external / LLM-authored composer source options",
    source_param="schema",
    suppresses=("R1", "R5"),
    invariant="raises ValueError when 'required_fields' is present but not a list/tuple of str; never coerces",
    test_ref="tests/unit/web/composer/test_generation_source_option_boundaries.py::test_schema_required_fields_rejects_non_sequence",
    test_fingerprint="b3c30bf7a9ac3f09269f01b4d43a3330440da5a4f2046abf025b1ab272c047ee",
)
def _schema_required_fields(schema: Mapping[str, Any]) -> tuple[str, ...]:
    raw = schema.get("required_fields")
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"schema.required_fields must be a list of strings when present; got {type(raw).__name__}")
    required: list[str] = []
    for idx, item in enumerate(raw):
        if type(item) is not str:
            raise ValueError(f"schema.required_fields[{idx}] must be str; got {type(item).__name__}")
        required.append(item)
    return tuple(required)


_EXPRESSION_GRAMMAR = """\
Expression Syntax Reference
===========================
Applies to gate conditions and value_transform expressions.

Variables:
  row      - The current row as a dict. Access fields via row['field_name'].

Field access:
  row['field_name']       Direct access (raises KeyError if missing)
  row.get('field_name')   Returns None if missing (NO default argument allowed)

Operators:
  ==, !=, <, >, <=, >=   Comparison
  is, is not              Identity (None checks only, e.g. row.get('x') is not None)
  and, or, not            Boolean logic
  in, not in              Membership test
  +, -, *, /, //, %       Arithmetic
  x if condition else y   Ternary conditional

Literals:
  Strings, numbers, and the constants True, False, None
  [..] lists, (..) tuples, {..} sets, {'k': v} dicts — for membership tests

Built-in functions (only these are allowed):
  len()       Length of a sequence or string
  abs()       Absolute value of a number
  lower()     Lowercase a string, e.g. lower(row['name'])
  upper()     Uppercase a string
  strip()     Trim whitespace (or given characters: strip(row['x'], 'z'))
  casefold()  Aggressive lowercase for caseless matching

Case folding is function-call form only — row['x'].lower() is rejected.
Type coercion functions (int, str, float, bool) are NOT available.
Types are guaranteed by the source schema — no coercion is needed in expressions.

Examples:
  row['confidence'] >= 0.85
  row['status'] == 'approved'
  row['category'] in ('A', 'B', 'C')
  row.get('optional_field') is not None
  row['score'] > 0.5 and row['status'] != 'rejected'
  len(row['name']) > 0

Forbidden:
  row.get('field', default)   Default values fabricate data — use 'is not None' test
  int(row['x'])               Type coercion — coerce at source schema instead
  row['x'] is row['y']        'is'/'is not' compare against None only
  Imports, lambdas, comprehensions, attribute access (except row.get)
"""


def get_expression_grammar() -> str:
    """Return the gate expression syntax reference."""
    return _EXPRESSION_GRAMMAR


def _handle_get_plugin_schema(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    plugin_type = arguments["plugin_type"]
    name = arguments["name"]
    policy_error = _validate_plugin_name(context, plugin_type, name)
    if policy_error is not None:
        return _plugin_policy_failure(state, policy_error)
    try:
        schema = context.catalog.get_schema(plugin_type, name)
        return _discovery_result(state, schema)
    except (ValueError, KeyError) as exc:
        # ValueError: catalog contract for "unknown plugin/type"
        # KeyError: LLM omitted required argument (Tier 3)
        return _failure_result(state, str(exc))


_GET_PLUGIN_SCHEMA_DECLARATION = ToolDeclaration(
    name="get_plugin_schema",
    handler=_handle_get_plugin_schema,
    kind=ToolKind.DISCOVERY,
    description="Get the full configuration schema for a plugin.",
    json_schema={
        "type": "object",
        "properties": {
            "plugin_type": {
                "type": "string",
                "enum": ["source", "transform", "sink"],
                "description": "Plugin type.",
            },
            "name": {
                "type": "string",
                "description": "Plugin name (e.g. 'csv').",
            },
        },
        "required": ["plugin_type", "name"],
        "additionalProperties": False,
    },
    cacheable=True,
)


def _handle_get_expression_grammar(
    arguments: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    del context  # unused; signature uniformity with the other handlers.
    return _discovery_result(state, get_expression_grammar())


_GET_EXPRESSION_GRAMMAR_DECLARATION = ToolDeclaration(
    name="get_expression_grammar",
    handler=_handle_get_expression_grammar,
    kind=ToolKind.DISCOVERY,
    description="Get the gate expression syntax reference.",
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    cacheable=True,
)


# One suggested fix per plugin-unavailability reason. Every
# ``PluginUnavailableReason`` value is emitted as a tool ``error_code``
# (``_plugin_policy_failure`` -> ``_failure_result(..., error_code=reason.value)``),
# so the planner's redacted repair feedback can carry any of them — yet the whole
# family resolved to NOTHING through ``explain_validation_code`` until now: the
# model saw a bare token like ``credential_unavailable`` and had no way to learn
# whether to pick a different plugin, wait for an operator, or stop trying. This
# dict is TOTAL over the enum (a new reason KeyErrors here at import time rather
# than silently degrading to an unexplained code), and the *explanation* half is
# reused verbatim from ``_PLUGIN_UNAVAILABLE_EXPLANATIONS`` so the copy the model
# reads cannot drift from the copy the tool failure already carries.
_PLUGIN_UNAVAILABLE_FIXES: Final[dict[PluginUnavailableReason, str]] = {
    PluginUnavailableReason.NOT_AUTHORIZED: (
        "Pick a plugin from the live catalogue (list_sources / list_sinks / list_transforms return only usable ones) — "
        "re-emitting the same plugin cannot enable it. If the user needs this one specifically, say plainly that an "
        "operator must turn it on in this deployment's plugin policy."
    ),
    PluginUnavailableReason.NOT_INSTALLED: (
        "Pick an installed plugin from list_sources / list_sinks / list_transforms; check the name for a typo first. "
        "No change to the options can conjure a plugin this deployment does not have."
    ),
    PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING: (
        "Pick a different plugin from the live catalogue, or tell the user an operator must install the missing "
        "requirement in this deployment — the pipeline itself cannot supply it."
    ),
    PluginUnavailableReason.CREDENTIAL_MISSING: (
        "Pick a plugin whose credential this deployment already holds, or tell the user an operator must configure "
        "this plugin's credential. Never invent a literal credential value or a secret_ref name to work around it."
    ),
    PluginUnavailableReason.PROFILE_UNAVAILABLE: (
        "Pick a plugin the live catalogue offers, or tell the user an operator must configure an operator profile "
        "for this one before it can be used."
    ),
    PluginUnavailableReason.WEB_SURFACE_PROHIBITED: (
        "Use an operator-controlled connector, allowlisted ingestion job, or batch/CLI runtime instead. This refusal is "
        "categorical: no option change, no credential, and no operator setting can make this plugin usable here, so do "
        "not re-emit it."
    ),
}


_VALIDATION_ERROR_PATTERNS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        r"No source configured",
        "The pipeline has no data source. Every pipeline needs exactly one source to read input data from.",
        "Use set_source to configure a source plugin (e.g. csv, json, dataverse).",
    ),
    (
        r"No sinks configured",
        "The pipeline has no outputs. At least one sink is needed to write results.",
        "Use set_output to add an output (e.g. csv, json).",
    ),
    (
        r"references unknown node '(.+)' as from_node",
        "An edge references a node that doesn't exist in the pipeline as its source.",
        "Check the edge's from_node value. Either add the missing node with upsert_node or fix the edge with upsert_edge.",
    ),
    (
        r"references unknown node '(.+)' as to_node",
        "An edge references a node or output that doesn't exist in the pipeline as its target.",
        "Check the edge's to_node value. Either add the missing node/output or fix the edge.",
    ),
    (
        r"Duplicate node ID: '(.+)'",
        "Two nodes have the same ID. Each node must have a unique identifier.",
        "Rename one of the duplicate nodes using upsert_node with a different id.",
    ),
    (
        r"Duplicate output name: '(.+)'",
        "Two outputs have the same name. Each output must have a unique name.",
        "Rename one of the duplicate outputs using set_output with a different sink_name.",
    ),
    (
        r"Duplicate edge ID: '(.+)'",
        "Two edges have the same ID. Each edge must have a unique identifier.",
        "Remove the duplicate edge with remove_edge and re-add with a unique id.",
    ),
    (
        r"Gate '(.+)' is missing required field '(.+)'",
        "A gate node is missing a required configuration field (condition or routes).",
        "Update the gate with upsert_node, providing the missing field.",
    ),
    (
        r"Transform '(.+)' must not have '(.+)' field",
        "A transform node has a field that only gates should have (condition or routes).",
        "Update the node with upsert_node. Set node_type to 'gate' if routing is needed, or remove the field.",
    ),
    (
        r"Coalesce '(.+)' is missing required field '(.+)'",
        "A coalesce node is missing a required field (branches or policy).",
        "Update the coalesce node with upsert_node, providing the missing field.",
    ),
    (
        r"row_union_config_invalid",
        "A row_union contains plugin, options, routing, aggregation, or coalesce-only configuration that the structural barrier forbids.",
        "Keep only id, node_type='row_union', input, branches, on_success, and an optional finite positive timeout_seconds.",
    ),
    (
        r"row_union_name_invalid",
        "The row_union name does not satisfy the runtime identifier contract.",
        "Choose a name of 1-38 characters that starts with a letter and contains only letters, digits, underscores, "
        "and hyphens; do not use a reserved label or a name beginning with '__'.",
    ),
    (
        r"row_union_branches_invalid",
        "A row_union needs at least two declared fork branches.",
        "Provide an ordered branches mapping with at least two unique branch names and input connections.",
    ),
    (
        r"row_union_branch_invalid",
        "A row_union branch name or connection is empty, duplicated, or otherwise invalid.",
        "Give every branch a non-empty unique alias and a non-empty unique incoming connection.",
    ),
    (
        r"row_union_input_mismatch",
        "The row_union adapter input does not equal the first declared branch connection.",
        "Set input to the first value in the ordered branches mapping.",
    ),
    (
        r"row_union_on_success_invalid",
        "A row_union must publish released rows through a non-empty on_success connection.",
        "Set on_success to a downstream processing connection.",
    ),
    (
        r"row_union_timeout_invalid",
        "The row_union timeout is not a finite positive number.",
        "Omit timeout_seconds or set it to a finite number greater than zero.",
    ),
    (
        r"row_union_branch_alias_unreachable",
        "A row_union branch alias does not match a fork branch declared upstream.",
        "Use the upstream gate's fork_to branch names as the row_union branches keys.",
    ),
    (
        r"row_union_branch_unreachable",
        "A row_union branch connection is not produced by the corresponding fork branch.",
        "Wire each branch transform's on_success to the matching row_union branches value.",
    ),
    (
        r"row_union_branch_origin_invalid",
        "The row_union branch aliases come from more than one gate, so the reconverged rows have no single correlation origin.",
        "Pick every branches key from one gate's fork_to, or add a separate row_union for each fork.",
    ),
    (
        r"row_union_branch_not_downstream",
        "A row_union branch alias maps to a connection that is not downstream of that alias's own fork edge.",
        "Wire each branches[alias] value through processing that starts at the matching gate fork branch.",
    ),
    (
        r"row_union_branch_aggregation_invalid",
        "A transform-mode aggregation inside a fork branch loses the per-row identity required to satisfy the row_union group.",
        "Set that aggregation's output_mode to passthrough so each row keeps its identity, or move the aggregation "
        "upstream of the fork that feeds the row_union.",
    ),
    (
        r"row_union_nested_fork_invalid",
        "A nested fork inside a row_union branch replaces the enclosing branch identity, so the require-all group cannot complete.",
        "Move the nested fork before the fork that feeds the row_union, or terminate the nested branch at a sink.",
    ),
    (
        r"row_union_downstream_group_invalid",
        "A row_union release reaches a downstream early-trigger aggregation or correlated barrier that can split, "
        "drop, or duplicate members of the indivisible N-to-N group.",
        "For an aggregation, omit trigger or use trigger: {} so only end_of_source flushes, or move it upstream of "
        "the fork. For a downstream coalesce or row_union, move that barrier upstream of the fork or terminate the "
        "released group at a sink.",
    ),
    (
        r"row_union_schema_incompatible",
        "The row_union branch declarations cannot safely feed one long-format output stream. Fixed schemas must have "
        "the same complete declared shape; flexible schemas may add branch-only fields but shared fields cannot "
        "declare conflicting types.",
        "Use the row_union_schema facts: align the complete declared field sets and types for fixed branches; for "
        "flexible branches, change only the listed conflicting shared fields to compatible types. Keep disjoint "
        "flexible-only fields; they do not need to be deleted.",
    ),
    (
        r"fork_branch_multiple_barriers",
        "One fork branch is declared by two barriers (coalesce and/or row_union). A fork branch delivers its arrival to "
        "exactly one barrier, so the engine rejects the second claim at graph build time.",
        "Keep the branch on a single barrier: delete it from the other barrier's branches, or give each barrier its own "
        "fork branch name in the gate's fork_to and wire that branch to it.",
    ),
    (
        r"gate_duplicate_fork_branch",
        "A gate declares the same fork branch name more than once, so the declared branch set is ambiguous.",
        "Remove duplicate entries from the gate's fork_to list while preserving the intended unique branch order.",
    ),
    (
        r"row_union_on_success_must_be_connection",
        "A row_union release target names a sink, but the barrier may only release into downstream processing.",
        "Set row_union.on_success to a connection consumed by a downstream node, then route that node to the sink.",
    ),
    (
        r"row_union_on_success_dangling",
        "No downstream node consumes the row_union release connection.",
        "Add a downstream processing node whose input equals row_union.on_success.",
    ),
    (
        r"aggregation_missing_plugin|Aggregation '(.+)' is missing required field 'plugin'",
        "An aggregation node needs a plugin to define its aggregation behaviour.",
        "Update the aggregation with upsert_node, specifying the plugin name.",
    ),
    (
        r"coalesce_branch_unreachable|branches .* are not reachable",
        "A coalesce branches mapping names an incoming connection that no runtime routing field produces. The usual cause is "
        "the WIRING AROUND the coalesce, not the coalesce itself: a branch transform's on_success routes past the coalesce "
        "(e.g. straight to a sink), so nothing arrives under the connection name the branches value claims. The rejection's "
        "connectivity facts list each unreachable branches value and every connection the pipeline actually produces.",
        "Wire each fork branch end-to-end: the gate fork_to name is the branch transform's input; that transform's on_success "
        "must be a unique connection name (NOT a sink); the coalesce branches VALUE for that branch is exactly that connection. "
        "A branch with no transforms uses its fork branch name as the value. If a branches value names a connection nothing "
        "produces, repair that branch's transform — point its on_success at the branches value — rather than merely swapping "
        "the branches value for one of the produced_connections.",
    ),
    (
        r"node_input_not_reachable|input '(.+)' is not reachable",
        "A node's input connection point is not produced by the runtime routing fields.",
        "Set source.on_success or an upstream node's on_success/on_error/route/fork_to so it matches this node's input.",
    ),
    (
        r"Unknown .+ plugin '(.+)'",
        "The specified plugin name is not available in the catalog.",
        "Use list_sources, list_transforms, or list_sinks to see available plugins.",
    ),
    (
        r"Path violation \(S2\).*[Ss]ource",
        "The source file path is outside the allowed directories.",
        "Source paths must be under the blobs/ directory. Upload a file first or use set_source_from_blob.",
    ),
    (
        r"Path violation \(S2\).*[Ss]ink",
        "The sink output path is outside the allowed directories.",
        "Sink output paths must be under the outputs/ or blobs/ directory.",
    ),
    (
        r"Path violation \(S2\)",
        "A file path is outside the allowed directories.",
        "Source paths must be under the blobs/ directory. Sink output paths must be under the outputs/ or blobs/ directory.",
    ),
    (
        r"Invalid options for source '(.+)':",
        "The source plugin configuration is invalid. A required option may be missing or have an invalid value.",
        "Use get_pipeline_state with component='source' to see current options, then use patch_source_options to fix.",
    ),
    (
        r"Invalid options for transform '(.+)':",
        "A transform node has invalid configuration. A required option may be missing or have an invalid value.",
        "Use get_pipeline_state to see the node's current options, then use patch_node_options to fix.",
    ),
    (
        r"Invalid options for sink '(.+)':",
        "A sink output has invalid configuration. A required option may be missing (e.g. path for file-based sinks).",
        "Use get_pipeline_state to see the output's current options, then use patch_output_options to fix.",
    ),
    # Contract-family entries are ORDER-SENSITIVE: the extras (locked-input)
    # messages and the sink-edge messages both start with "Schema contract
    # violation:", so the more specific patterns must precede the generic one.
    (
        r"sink_locked_extras|Extra fields rejected by sink input contract",
        "The sink schema is locked (mode: fixed) and the upstream producer emits extra fields the sink does not accept. "
        "The rejection's contract facts name the producer, the sink, and the extra field names.",
        "Change ONLY that edge: relax the sink schema (mode: flexible), add the extra field names to the sink schema fields, "
        "or insert a field_mapper with select_only: true before the sink to drop them.",
    ),
    (
        r"locked_input_extras|Extra fields rejected by consumer input contract",
        "The consumer node's input schema is locked (mode: fixed) and the upstream producer emits extra fields it does not accept. "
        "The rejection's contract facts name the producer, the consumer, and the extra field names.",
        "Change ONLY that edge: relax the consumer schema (mode: flexible), add the extra field names to the consumer schema fields, "
        "or insert a field_mapper with select_only: true upstream to drop them.",
    ),
    (
        r"transform_contract_violation|Transform contract violation:",
        "A transform's declared output schema promises fields its own configuration cannot emit "
        "(e.g. field_mapper with select_only: true only emits its mapping targets). "
        "The rejection's contract facts name the node and the declared-but-unemitted field names.",
        "Change ONLY that node: remove the missing field names from its schema declaration, extend its mapping so they are "
        "actually produced, or set select_only: false.",
    ),
    (
        r"semantic_contract_violation|Semantic contract violation:|Semantic contract:",
        "A consumer requires a field to satisfy a semantic contract (content kind, framing, or value type) that its upstream "
        "producer does not declare or actively conflicts with. The rejection's contract facts name the producer and consumer.",
        "Call get_plugin_assistance for the consumer plugin to see which producers satisfy its semantic requirements, then "
        "change ONLY that edge: swap the producer, or route through a transform that produces the required content kind.",
    ),
    (
        r"contract_config_invalid|Invalid contract config:",
        "A schema or contract declaration failed to parse — malformed fields spec, required_fields/required_input_fields with "
        "the wrong shape (must be a list of strings), or an invalid mode.",
        "Re-emit ONLY the offending component's schema options. Field specs are single-key dicts like {'field_name': 'str'} or "
        "strings like 'field_name: str'; required_input_fields is a list of field-name strings; mode is one of observed, "
        "flexible, fixed.",
    ),
    (
        r"coalesce_schema_mode_mixed|mixed observed/explicit schemas",
        "A union coalesce has both observed and explicit branch schemas. Runtime cannot safely merge those modes because an "
        "observed branch may omit fields promised by an explicit branch.",
        "Keep the intended union row shape and make the branch schemas homogeneous: give every branch an explicit schema with "
        "compatible fields, or set every branch schema to observed.",
    ),
    (
        r"coalesce_union_type_incompatible|[Ii]ncompatible types for field",
        "A union coalesce merges its branches into one row, so a field carried by more than one branch must have the same "
        "type on each. Two branches carry the same field with different types, and there is no merged row shape that "
        "satisfies both. Two things surprise authors here. 'any' is a distinct type, NOT a wildcard: 'any' against 'int' "
        "conflicts. And a branch can carry a field it never declared, because a transform contributes its own output fields "
        "to that branch's schema — a field_mapper or value_transform writing a target adds it as 'any' when the branch "
        "schema does not declare it. So the named field may appear in neither branch's authored schema.",
        "Read the conflicting field and both branch names off the rejection rather than searching your authored schemas — "
        "the field may not appear in either. Then pick the one type the merged field should have and declare it explicitly "
        "on every branch that carries it, which also pins any type a transform would otherwise contribute as 'any'. If the "
        "branches genuinely carry different kinds of value, give them different field names instead, or convert the "
        "diverging branch to the shared type in its transform before the coalesce.",
    ),
    (
        r"sink_contract_violation|Schema contract violation: '.*' -> 'output:[^']+'",
        "A sink schema requires fields that its upstream producer does not guarantee. "
        "The rejection's contract facts name the producer, the sink, and the missing field names.",
        "Call preview_pipeline to inspect edge_contracts, then either relax the sink schema with patch_output_options or update the upstream schema with patch_source_options or patch_node_options and re-preview until the edge shows satisfied=true.",
    ),
    (
        r"schema_contract_violation|Schema contract violation:",
        "A downstream node requires fields that its upstream producer does not guarantee. "
        "The rejection's contract facts name the producer, the consumer, and the missing field names.",
        "Call preview_pipeline to inspect edge_contracts, then update the upstream schema with patch_source_options or patch_node_options and re-preview until the edge shows satisfied=true.",
    ),
    # ── Closed structural node-shape codes ──────────────────────────────────
    # The planner repair feedback strips validation messages; these codes are
    # the only signal it carries, so each must be explainable here.
    (
        r"unknown[ _]node_type",
        "The node_type is not one of the composer's node kinds: aggregation, coalesce, gate, queue, row_union, transform. There is no 'fork' node_type.",
        "Keep your current pipeline shape and change ONLY the invalid node: forking is expressed with a GATE node — "
        "node_type='gate', condition='True', routes={'true': 'fork', 'false': 'fork'}, "
        "fork_to=['branch_a', 'branch_b']; each branch node reads one branch name as its input. "
        "Use COALESCE (branches + policy + merge) for N-to-one field merging: a downstream node consumes the coalesce id. "
        "Use row_union for require_all N-to-N reconvergence that releases every original branch row: branches maps fork aliases "
        "to arriving connections, input repeats the first branch value, and on_success names downstream processing.",
    ),
    (
        r"coalesce_on_success_must_be_sink|coalesce_on_success_unknown_sink|Coalesce on_success must point to a sink|Coalesce '(.+)' on_success references unknown sink",
        "A coalesce node's on_success may only name an existing sink; merged rows otherwise flow to whichever node reads the coalesce id as its input. "
        "For coalesce_on_success_unknown_sink the rejection's 'connectivity' facts, when present, carry the offending value ('dangling_on_success') and the candidate's sink names ('declared_sinks').",
        "Either set the coalesce on_success to a sink name from outputs[] (the connectivity facts' declared_sinks) exactly, or leave on_success null and give the downstream node input='<coalesce node id>'.",
    ),
    (
        r"coalesce_missing_branches|Coalesce '(.+)' is missing required field 'branches'",
        "A coalesce node must name the branch connections it rejoins.",
        "Set branches to a mapping of fork branch name -> the connection ARRIVING at the coalesce: the branch's last transform "
        "on_success (e.g. branches={'branch_a': 'a_done', 'branch_b': 'b_done'}), or the fork branch name itself only when that "
        "branch has no transforms. Add policy='require_all' and merge='union'.",
    ),
    (
        r"transform_missing_on_success|Transform '(.+)' is missing required field 'on_success'",
        "Every transform must route its successful rows somewhere.",
        "Set the transform's on_success to the next connection or sink name; use on_error='discard' unless failed rows need a "
        "quarantine sink. A transform on a fork branch that rejoins at a coalesce must publish the connection named by that "
        "coalesce's branches value for its branch — not a sink.",
    ),
    (
        r"transform_missing_on_error|Transform '(.+)' is missing required field 'on_error'",
        "Every transform must declare where failed rows go.",
        "Set the transform's on_error — 'discard' is the simplest safe choice, or route to a quarantine sink name.",
    ),
    (
        r"gate_missing_condition|Gate '(.+)' is missing required field 'condition'",
        "A gate node needs a boolean row expression to route on.",
        "Set condition to a row expression (call get_expression_grammar for syntax); a pure fan-out gate uses condition='True'.",
    ),
    (
        r"gate_missing_routes|Gate '(.+)' is missing required field 'routes'",
        "A gate node needs a routes mapping for its condition outcomes.",
        "Set routes={'true': <connection-or-'fork'>, 'false': <connection-or-'fork'>}; use 'fork' with fork_to=[...] to fan a row out to several branches.",
    ),
    (
        r"fork_branch_no_destination|fork branch '(.+)' with no destination",
        "Every gate fork_to branch name must land somewhere: as a KEY in a coalesce 'branches' mapping, or as an exact sink name.",
        "Key the coalesce branches by the gate's fork branch names — e.g. fork_to=['branch_a','branch_b'] pairs with branches={'branch_a': '<connection arriving from that branch>', 'branch_b': '<connection>'} — where each value is the connection reaching the coalesce after any per-branch transforms (the transform's on_success).",
    ),
    (
        r"coalesce_policy_invalid|coalesce_merge_invalid",
        "Coalesce policy and merge use the engine's closed vocabularies.",
        "Set policy to one of: require_all, quorum, best_effort, first — and merge to one of: union, nested, select. For an A/B rejoin that combines branch fields into one row: policy='require_all', merge='union'.",
    ),
    (
        r"pipeline_decision_unregistered",
        "A pipeline_decision interpretation review may only use one of the registered decision kinds — novel decision terms can never be reviewed or resolved.",
        "Remove the pipeline_decision entry from the node's interpretation_requirements (record the rationale in metadata.description instead), or use an llm_prompt_template review for prompt-shaped decisions. Registered kinds: drop_raw_html_fields, web_scrape_http_identity, prompt_injection_shield_recommendation.",
    ),
    (
        r"interpretation_requirements_invalid",
        "A node's interpretation_requirements entry is malformed. interpretation_requirements is a list of review entry objects, each carrying string fields kind, user_term, and draft; the server-owned id and status fields are filled automatically.",
        'Simplest fix: omit interpretation_requirements entirely — the required LLM reviews (prompt template, model choice) are auto-staged by the server, and the prompt-injection-shield recommendation is advisory. If you must stage a review, re-emit each entry as an object {kind, user_term, draft} with non-empty string values — e.g. {"kind": "pipeline_decision", "user_term": "prompt_injection_shield_recommendation", "draft": "<recommendation text>"} — and never author id, status, or resolved review metadata.',
    ),
    (
        r"plugin_options_invalid",
        "One of the component's options failed its plugin schema. This code alone does not identify WHICH option: the rejection's 'detail' field carries the validator's own message naming the exact option and requirement — read it before hypothesising a cause.",
        "Apply exactly what 'detail' names (it usually includes a ready repair, e.g. a patch_node_options call). Only if detail is absent: call get_plugin_schema(<plugin_type>, <plugin_name>) for the exact option shapes and allowed values, fix only the offending options, and re-emit. Common traps, all equally likely: an llm node must declare options.required_input_fields matching its prompt_template's row.* references (or [] to opt out); schema options use {mode: observed} to infer types or explicit fields with mode fixed/flexible; the llm 'profile' must be one of the enum aliases from get_plugin_schema.",
    ),
    (
        r"transform_on_success_dangling|aggregation_on_success_dangling|source_on_success_dangling|is neither a sink nor a known connection",
        "An on_success destination must be an existing sink name or a connection another node reads as its input. "
        "The rejection's 'connectivity' facts, when present, name the exact mismatch in YOUR rejected candidate: "
        "'dangling_on_success' is the value that matched nothing; 'declared_sinks' and 'consumable_connections' are the only valid destinations.",
        "Re-emit with on_success set to one of the connectivity facts' declared_sinks or consumable_connections, copied exactly — "
        "for a straight source-to-sink pipeline the source's on_success must equal the outputs[].sink_name byte-for-byte. "
        "Change nothing else. Only without connectivity facts: call get_pipeline_state to list the CURRENT saved state's sink names "
        "and node input connections (note it shows the saved state, not a rejected candidate).",
    ),
    (
        r"guided_route_target_unknown",
        "A routing destination (source/node on_success, node on_error, or edge to_node) names neither a declared "
        "output, a node id, a connection another node consumes, nor 'discard'. The reviewed-output binder cannot "
        "prove what you meant — it will not guess a sink for you. The rejection's 'connectivity' facts name the "
        "exact mismatch in YOUR rejected candidate: 'dangling_references' are the values that matched nothing; "
        "'declared_sinks' and 'consumable_connections' are the only valid destinations.",
        "Re-emit with every dangling reference replaced by one of the connectivity facts' declared_sinks or "
        "consumable_connections, copied exactly — a route meant for the sink must byte-for-byte match your own "
        "outputs[].sink_name so the binder can rename it with the output. Change nothing else.",
    ),
    (
        r"guided_output_alias_collision",
        "Output aliasing is ambiguous: two outputs[] entries share one sink_name, an entry reuses another reviewed "
        "output's name, or an alias is also a node id or connection name in the same candidate. References to such "
        "a name cannot be attributed to one sink, so the reviewed-output binder rejects instead of rewriting "
        "routes onto the wrong destination. The rejection's 'connectivity' facts list the offending names as "
        "'colliding_aliases'.",
        "Re-emit with a unique sink_name per outputs[] entry, distinct from every node id and connection name, "
        "and wire each route to the matching alias. Do not reuse a name from 'colliding_aliases' for more than "
        "one purpose.",
    ),
    (
        r"guided_reviewed_name_shadowed",
        "A node id, consumed connection, or branch value in your candidate equals a reviewed sink name you were "
        "never shown. After the server restores that reviewed name, the engine resolves routing targets against "
        "sink names FIRST, so every reference meant for your node would silently deliver rows to the sink and "
        "skip the node. The rejection's 'connectivity' facts list the reserved names as 'shadowed_reviewed_names'.",
        "Re-emit with every name in 'shadowed_reviewed_names' renamed throughout your topology — the node id, its "
        "consumers' input, and every route referencing it — to a fresh name of your own. Do not reuse any "
        "shadowed name for a node, connection, or branch value. Change nothing else.",
    ),
    (
        r"gate_on_error_unknown_sink",
        "A gate's node-level on_error policy may only be 'discard' or an existing sink name. The rejection's "
        "'connectivity' facts carry the offending value as 'dangling_on_error' and the candidate's sink names as "
        "'declared_sinks'.",
        "Use upsert_node to set the gate's on_error='discard', or copy one of the connectivity facts' declared_sinks "
        "exactly. Gate evaluation-error routing is configured on the gate node; do not add an on_error edge.",
    ),
    (
        r"transform_on_error_unknown_sink|references unknown sink",
        "An on_error destination may only be 'discard' or an existing sink name. The rejection's 'connectivity' facts, when present, "
        "carry the offending value as 'dangling_on_error' and the candidate's sink names as 'declared_sinks'.",
        "Set on_error='discard', or point it at one of the connectivity facts' declared_sinks (outputs[].sink_name) exactly.",
    ),
    (
        r"gate_route_labels_mismatch|route labels don't match",
        'A boolean gate condition routes on exactly the labels "true" and "false" — both must be present even when they share a destination.',
        'Use routes={"true": <destination>, "false": <destination>}. For a pure fan-out gate: condition=\'True\', routes={"true": "fork", "false": "fork"}, fork_to=[<branch connections>]. Only string-returning conditions may use custom route labels.',
    ),
    (
        r"gate_condition_ignores_stated_threshold",
        "The instruction states a comparison (a threshold on a field), but every gate in the candidate has a constant "
        "condition. A constant condition makes no per-row decision, so the stated rule is expressed nowhere in the "
        "pipeline and every row takes the same path — with a fan-out gate that means every row is written to every "
        "destination. The rejection's 'detail' quotes the comparison from the instruction verbatim.",
        "Author the comparison as the gate's condition — e.g. condition=\"row['amount'] > 500\" — and point "
        'routes={"true": <one destination>, "false": <the other destination>} at the two DIFFERENT destinations '
        "(drop fork_to; a fork delivers to every branch). "
        "If the constant fan-out was genuinely intended — every row to every destination — re-emit the same pipeline "
        "unchanged and it will be accepted.",
    ),
    (
        r"passthrough_cannot_produce_declared_fields",
        "The candidate has no transform or aggregation nodes, so every row it writes is exactly the row the source "
        "read — but the reviewed outputs declare fields that no reviewed source declares or observes. Nothing in this "
        "pipeline can put those fields on a row. The rejection's 'detail' names them; they are also visible as "
        "outputs[].required_fields in the reviewed planner context, minus every source's observed_columns and "
        "declared_fields.",
        "Add the transform node(s) that produce the named fields — for a straight rename or copy from an existing "
        "column a field_mapper with the appropriate mapping is enough; for derived or generated values use the "
        "transform that computes them — and wire the source through them to the sink. Re-emitting the same "
        "zero-transform pipeline will be rejected again with this same code.",
    ),
    (
        r"proposal_missing_requested_transforms",
        "The revision candidate contains no transform or aggregation nodes, but the operator's revision instruction asked for processing — a bare source-to-sink pass-through would ship a pipeline that silently performs none of the requested work behind a confident name. "
        "This code only fires when a pass-through COULD satisfy the reviewed output fields; when it could not, the satisfiability rejection 'passthrough_cannot_produce_declared_fields' fires instead and re-emitting unchanged is NOT accepted there.",
        "Re-emit with the transform nodes the revision instruction requests, as a minimal delta: keep the reviewed source and sink wiring unchanged and add only the processing nodes. "
        "If a pass-through with no transforms is genuinely what the instruction calls for, re-emit the same pipeline unchanged to confirm the deliberate no-transform intent — the confirmation will be accepted.",
    ),
    (
        r"guided_correction_unchanged",
        "The candidate left the exact node or route selected by the operator unchanged. Changing a different component does not satisfy a selected-component correction.",
        "Apply the operator's correction to the selected target identified by the correction_target object in the reviewed context, preserve unrelated components, and re-emit the complete pipeline.",
    ),
    (
        r"guided_amend_contract_violation",
        "The candidate was submitted as a conservative amendment but removed, duplicated, retyped, replugged, or changed protected behavior on an existing node. The accepted proposal named by revision_authority is the predecessor; omitted fields do not grant replacement authority.",
        "Keep every existing node id, node_type, plugin, options, and control behavior unchanged. Add the requested new nodes and change only input/on_success connections needed to insert them, then re-emit the complete pipeline. If replacement or removal is genuinely intended, the operator must choose explicit replace mode instead of amend.",
    ),
    (
        r"guided_revision_unchanged",
        "The revision candidate is semantically identical to the accepted predecessor proposal, so it does not satisfy the operator's revision request. Explicit replace mode permits replacement or removal; it does not permit a no-op.",
        "Apply the requested change under revision_authority: preserve existing nodes and use only insertion rewiring in amend mode, or perform the explicitly requested replacement in replace mode, then re-emit the complete pipeline.",
    ),
    (
        r"interpretation_review_draft_malformed|cleanup review draft is malformed",
        "The cleanup node's interpretation_requirements row IS present and its user_term matches the registered decision kind, but the draft text fails marker recognition — the contract recognizes the draft only when it contains both 'raw html' and 'fingerprint'. Do NOT add another row; the fix is the draft text alone.",
        "On the existing cleanup row, replace ONLY the draft string with the canonical draft, copied verbatim without rephrasing: "
        f'"{RAW_HTML_CLEANUP_REVIEW_DRAFT}". '
        "Keep the row's user_term and every other pipeline detail unchanged, and re-emit the FULL set_pipeline call.",
    ),
    (
        r"interpretation_review_contract_unsatisfied|drops web-scrape raw field",
        "A cleanup node that drops web-scrape raw fields (field_mapper with select_only=true) must stage an interpretation review recording that decision before the pipeline is accepted. "
        "The row is recognized ONLY when user_term is the registered decision kind and the draft text names both the raw HTML and the fingerprint fields — a paraphrased draft is NOT recognized and this same code fires again.",
        # The literal minimal delta (tutorial op 18b4cee7: four generations
        # looped on this code because free-text drafts fail the contract's
        # marker recognition). Quote the registered user_term and the
        # canonical draft VERBATIM so a copy-paste repair always lands.
        "On the cleanup field_mapper node, add options.interpretation_requirements — a SIBLING of mapping, never inside it — containing exactly this entry: "
        f'{{"kind": "pipeline_decision", "user_term": "{RAW_HTML_CLEANUP_USER_TERM}", "draft": "{RAW_HTML_CLEANUP_REVIEW_DRAFT}"}}. '
        "Copy the user_term and draft strings verbatim — do not rephrase the draft. "
        "Then re-emit the FULL set_pipeline call with that requirement in place; rejected set_pipeline calls do not persist partial nodes.",
    ),
    (
        r"file_sink_write_policy_invalid|must set collision_policy explicitly|must set mode explicitly",
        "File sinks require an explicit write policy: options.mode and options.collision_policy must both be present and consistent.",
        "Set options.mode='write' with collision_policy='auto_increment' (or 'fail_if_exists'), or options.mode='append' with collision_policy='append_or_create', on every file-based outputs[] entry.",
    ),
    (
        r"no_source_configured|No source configured",
        "The pipeline has no source; every pipeline reads rows from exactly one configured source (or named sources).",
        "Include a source block in the set_pipeline call — plugin, on_success connection, and options with a schema.",
    ),
    (
        r"no_sinks_configured|No sinks configured",
        "The pipeline has no outputs; rows must land in at least one sink.",
        "Include at least one outputs[] entry — sink_name, plugin, and options (file-based sinks need a path under outputs/).",
    ),
    (
        r"source_name_invalid",
        "A named source uses an invalid name.",
        "Source names must be short lowercase identifiers; rename the source key and re-emit.",
    ),
    (
        r"reserved_node_id|Reserved node id|reserved source producer namespace",
        "The id 'source' and the 'source:<name>' namespace are reserved for pipeline sources; no node or queue may use them.",
        "Rename the node to a descriptive id (e.g. 'clean_rows') and update every routing field that referenced the old id.",
    ),
    (
        r"duplicate_node_id|Duplicate node ID",
        "Two nodes share the same id; node ids must be unique.",
        "Rename one of the nodes and update the routing fields that reference it.",
    ),
    (
        r"duplicate_output_name|Duplicate output name",
        "Two outputs share the same sink name; sink names must be unique.",
        "Rename one of the outputs and update any on_success/on_error/route that targets it.",
    ),
    (
        r"duplicate_edge_id|Duplicate edge ID",
        "Two edges share the same id.",
        "Edges are UI-only; drop the duplicate edge entry (runtime routing uses the connection fields, not edges).",
    ),
    (
        r"edge_unknown_node|references unknown node",
        "An edge names a from_node or to_node that does not exist in the pipeline.",
        "Edges are UI-only; drop the stale edge or fix its node reference — runtime routing uses the connection fields.",
    ),
    (
        r"duplicate_connection_producer|Duplicate producer for connection",
        "Two different components publish rows to the same connection name; every connection has exactly one producer.",
        "Rename one producer's on_success (or route/fork_to target) to a distinct connection name and point the intended "
        "consumer's input at it. To merge branches, use a coalesce node — not a shared connection name.",
    ),
    (
        r"duplicate_connection_consumer|Duplicate consumer for connection",
        "Two different nodes read the same connection as their input; every connection has exactly one consumer.",
        "Give each consumer its own input connection (fan out with a gate fork_to if both need the same rows).",
    ),
    (
        r"connection_sink_name_overlap|Connection names overlap with sink names|collides with a sink of the same name",
        "A connection name and a sink name are identical; connection and sink names must be disjoint so routing is unambiguous.",
        "Rename the connection (the node id or on_success value) so it no longer matches any outputs[].sink_name.",
    ),
    (
        r"gate_condition_invalid",
        "A gate's condition expression failed to parse or uses disallowed syntax.",
        "Set condition to a valid row expression (call get_expression_grammar for the syntax); a pure fan-out gate uses condition='True'.",
    ),
    (
        r"aggregation_missing_on_error|Aggregation '(.+)' is missing required field 'on_error'",
        "Every aggregation must declare where failed rows go.",
        "Set the aggregation's on_error — 'discard' is the simplest safe choice, or route to a quarantine sink name.",
    ),
    (
        r"aggregation_output_mode_invalid",
        "An aggregation's output_mode is not one of the allowed values.",
        "Set output_mode to 'passthrough' or 'transform' (or omit it for the default).",
    ),
    (
        r"batch_transform_misplaced",
        "A batch-aware plugin is configured as a row-level transform; batch plugins only run as aggregations.",
        "Re-emit the node with node_type='aggregation' and a trigger (e.g. trigger={'count': N}), or pick a row-level transform plugin.",
    ),
    (
        r"batch_required_fields_invalid",
        "A batch-aware aggregation's required_input_fields declaration is invalid for its plugin.",
        "Call get_plugin_schema('transform', <plugin>) for the exact option shapes and re-emit only the offending options.",
    ),
    (
        r"batch_value_field_not_numeric",
        "batch_distribution_profile.value_field must reference a numeric (int/float) field, but the upstream schema declares a non-numeric type.",
        "Point value_field at a numeric field, or use batch_top_k for categorical distributions.",
    ),
    (
        r"queue_config_invalid",
        "A queue node's shape is invalid — a queue's id must equal its input, with no plugin, routing, or options beyond a description.",
        "Re-emit the queue with id == input and only a description in options, or remove the queue node.",
    ),
    (
        r"queue_no_consumer",
        "A queue has no downstream consumer; a queue must feed exactly one ordinary node.",
        "Give one downstream node input='<queue id>', or remove the queue.",
    ),
    (
        r"queue_name_collision|collides with a source of the same name",
        "A queue's id collides with a source name; source and queue names must be globally unique.",
        "Rename the queue node (its id and the consumer's input) so it no longer matches any source name.",
    ),
    (
        r"aggregation_trigger_invalid|trigger is invalid",
        "An aggregation's trigger failed to parse.",
        "Set trigger to a valid shape, e.g. trigger={'count': N} to aggregate every N rows, or omit it to aggregate at end of source.",
    ),
    (
        r"web_scrape_http_identity_invalid",
        "web_scrape.http identity fields (abuse_contact, scraping_reason) carry a placeholder or undeliverable value; these ship "
        "as HTTP headers to the scraped host and must be real operator-supplied identity.",
        "Ask the operator for the deployment's abuse contact email and scraping reason, or leave the http block for the operator "
        "to fill — never invent an email or domain.",
    ),
    (
        r"prompt_template_unbound_variables|prompt render context does not define",
        "A prompt_template interpolates bare variable names the render context does not define — templates see row data only "
        "as 'row.<field>' and lookup data as 'lookup.<key>', so rendering raises 'Undefined variable' at runtime and none of "
        "the row's data reaches the model.",
        "Rewrite each bare name as '{{ row.<field> }}' using a field the upstream schema provides (e.g. '{{ text }}' becomes "
        "'{{ row.text }}'), or '{{ lookup.<key> }}' for lookup data, or remove the reference.",
    ),
    (
        r"query_template_unbound_row_fields|input_fields binds only",
        "A multi-query LLM template references 'row.<variable>' names the query never binds — each query renders with 'row' "
        "holding exactly its input_fields variables plus 'source_row', so an unbound reference raises 'Undefined variable' "
        "at runtime and that query fails for every row.",
        "Bind each missing name in that query's input_fields (template variable → row column), rename the reference to a "
        "variable the query already binds, or use '{{ row.source_row.<column> }}' for direct access to the source row.",
    ),
    # Plugin-unavailability family. Exact-code patterns only (``re.escape``, no
    # alternation): these codes are short and generic, and a loose pattern here
    # would shadow an unrelated entry above. They sit LAST so every message-shaped
    # pattern above still wins on a full validation message.
    *(
        (
            re.escape(reason.value),
            f"The pipeline names a plugin that cannot be used in this deployment: {_PLUGIN_UNAVAILABLE_EXPLANATIONS[reason]}.",
            _PLUGIN_UNAVAILABLE_FIXES[reason],
        )
        for reason in PluginUnavailableReason
    ),
)


# The closed validation codes the planner's redacted repair feedback can carry
# (see ``pipeline_planner._allowlisted_candidate_feedback``). Every entry MUST
# resolve through :func:`explain_validation_code` — the explain tool's fallback
# advertises this list and its fuzzy route scans for these substrings, so a
# dead entry would route the model to a code that then explains nothing
# (test_closed_code_catalogue_is_fully_explainable pins the invariant).
_CLOSED_VALIDATION_ERROR_CODES: Final[tuple[str, ...]] = (
    "unknown_node_type",
    "coalesce_on_success_must_be_sink",
    "coalesce_missing_branches",
    "coalesce_policy_invalid",
    "coalesce_merge_invalid",
    "row_union_config_invalid",
    "row_union_name_invalid",
    "row_union_branches_invalid",
    "row_union_branch_invalid",
    "row_union_input_mismatch",
    "row_union_on_success_invalid",
    "row_union_timeout_invalid",
    "row_union_branch_alias_unreachable",
    "row_union_branch_unreachable",
    "row_union_branch_origin_invalid",
    "row_union_branch_not_downstream",
    "row_union_branch_aggregation_invalid",
    "row_union_nested_fork_invalid",
    "row_union_downstream_group_invalid",
    "row_union_schema_incompatible",
    "row_union_on_success_must_be_connection",
    "row_union_on_success_dangling",
    # Cross-node barrier topology: mirrors the engine's "each fork branch can
    # only join at one barrier" rule for coalesce/coalesce, coalesce/row_union
    # and row_union/row_union claims.
    "fork_branch_multiple_barriers",
    "gate_duplicate_fork_branch",
    "transform_missing_on_success",
    "transform_missing_on_error",
    "transform_on_success_dangling",
    "transform_on_error_unknown_sink",
    "gate_on_error_unknown_sink",
    "aggregation_on_success_dangling",
    "source_on_success_dangling",
    "gate_missing_condition",
    "gate_missing_routes",
    "gate_route_labels_mismatch",
    "fork_branch_no_destination",
    "pipeline_decision_unregistered",
    "interpretation_requirements_invalid",
    "plugin_options_invalid",
    # ── Schema-contract family (2026-07-22 codeless-rejection closure) ──────
    # Edge-level members carry SchemaContractDetail facts; coalesce mode
    # conflicts carry the coalesce id and branch groups in the validation row.
    "schema_contract_violation",
    "sink_contract_violation",
    "locked_input_extras",
    "sink_locked_extras",
    "transform_contract_violation",
    "semantic_contract_violation",
    "contract_config_invalid",
    "coalesce_schema_mode_mixed",
    "coalesce_union_type_incompatible",
    # ── Structural rejections (same closure sweep) ──────────────────────────
    "no_source_configured",
    "no_sinks_configured",
    "source_name_invalid",
    "reserved_node_id",
    "duplicate_node_id",
    "duplicate_output_name",
    "duplicate_edge_id",
    "edge_unknown_node",
    "duplicate_connection_producer",
    "duplicate_connection_consumer",
    "connection_sink_name_overlap",
    "gate_condition_invalid",
    "aggregation_missing_plugin",
    "aggregation_missing_on_error",
    "aggregation_output_mode_invalid",
    "batch_transform_misplaced",
    "batch_required_fields_invalid",
    "batch_value_field_not_numeric",
    "queue_config_invalid",
    "queue_no_consumer",
    "queue_name_collision",
    "coalesce_branch_unreachable",
    "node_input_not_reachable",
    "coalesce_on_success_unknown_sink",
    "aggregation_trigger_invalid",
    "web_scrape_http_identity_invalid",
    # ── Prompt-context binding guard (R2-F17 compounding; elspeth-bea314a89b) ─
    # A prompt_template interpolating names outside {row, lookup} crashes at
    # render under StrictUndefined and sends no row data to the model.
    "prompt_template_unbound_variables",
    # ── Multi-query row-binding guard (elspeth-bea314a89b follow-up) ───────
    # A multi-query template referencing row.<name> outside that query's
    # input_fields + {source_row} fails that query at render for every row.
    "query_template_unbound_row_fields",
    # ── Pre-application semantic rejections (tutorial op 1152d7e3 closure) ──
    # Previously codeless _failure_result sites: the planner saw only the
    # 'validation_error' placeholder while the actionable message was redacted.
    "interpretation_review_contract_unsatisfied",
    "interpretation_review_draft_malformed",
    "file_sink_write_policy_invalid",
    # ── Nodeless-revision guard (same closure; proposal 3cb6532e) ──────────
    # A revision candidate netting zero transform nodes drew one coded nudge
    # instead of silently shipping a passthrough with aspirational metadata.
    "proposal_missing_requested_transforms",
    # ── Guided selected-correction convergence (elspeth-43208ece4c) ───────
    # The candidate is otherwise valid but did not change the exact public
    # semantics the operator selected. Keep it inside bounded repair/hatch.
    "guided_correction_unchanged",
    # ── Guided unscoped amendment convergence (elspeth-1d97fc4b80) ───────
    # Prose revisions default to additive custody of the accepted proposal;
    # protected-change attempts and no-op candidates stay inside repair/hatch.
    "guided_amend_contract_violation",
    "guided_revision_unchanged",
    # ── Unproducible declared output fields (R2-F4, 2026-08-01) ────────────
    # Planner-loop only: pairs the reviewed guided facts (what the sources
    # carry vs what the outputs declare) with the candidate's node count.
    # No structural validator sees both.
    "passthrough_cannot_produce_declared_fields",
    # ── Stated-threshold fidelity guard (R2-F17, 2026-08-01) ───────────────
    # Planner-loop only: the instruction is the evidence, so no structural
    # validator can raise this. The shape it rejects is legal everywhere else.
    "gate_condition_ignores_stated_threshold",
    # ── Plugin-unavailability family (same sweep) ──────────────────────────
    # Every ``PluginUnavailableReason`` value is a live tool ``error_code``
    # (``_plugin_policy_failure``), so each can reach planner feedback. Derived
    # from the enum rather than transcribed: a new reason joins the catalogue
    # and the explain patterns together, or the explain-pattern generator's
    # ``_PLUGIN_UNAVAILABLE_FIXES[reason]`` lookup raises KeyError at import
    # time — totality is enforced by that lookup, not by an assert.
    *(reason.value for reason in PluginUnavailableReason),
)


def _extract_validator_expected_hint(error_text: str) -> str | None:
    """Pull the ``Expected ...`` span out of a validator error string.

    Pydantic and our schema-spec validators frequently emit errors like
    ``"Field spec at index 0 is a dict with 2 keys. Expected single-key
    dict like {'field_name': 'type'} or a string like 'field_name: type'."``.
    The static catalogue fix below the substring discards that hint, so
    the model only sees ``"Use get_pipeline_state ... patch_source_options"``
    — which doesn't tell it what shape to actually emit. Returning the
    ``Expected ...`` span verbatim lets the caller append it to
    ``suggested_fix`` so the model can copy the shape directly.

    The hint terminates at the next sentence boundary (``.`` followed by
    whitespace or end-of-string) so a trailing ``"Got X. Other noise."``
    doesn't get swept up.
    """
    idx = error_text.find("Expected ")
    if idx == -1:
        return None
    rest = error_text[idx:]
    end = len(rest)
    for i, ch in enumerate(rest):
        if ch == "." and (i + 1 == len(rest) or rest[i + 1].isspace()):
            end = i + 1
            break
    return rest[:end].strip()


def _augment_with_expected_hint(fix: str, error_text: str) -> str:
    """Append the validator ``Expected ...`` hint to ``fix`` when present."""
    hint = _extract_validator_expected_hint(error_text)
    if hint is None:
        return fix
    return f"{fix} {hint}"


def explain_validation_code(code: str) -> tuple[str, str] | None:
    """Resolve a closed validation ``error_code`` to ``(explanation, suggested_fix)``.

    The one-shot pipeline planner's repair feedback strips raw validation
    messages before returning a rejection to the model — a redaction boundary,
    since a raw message can quote plugin names, option values, or row content
    (see ``pipeline_planner._allowlisted_candidate_feedback``). The closed
    ``error_code`` is the only signal that survives. The "Closed structural
    node-shape codes" entries in :data:`_VALIDATION_ERROR_PATTERNS` deliberately
    embed those codes as regex alternations precisely so the *code alone*
    resolves to the same guidance the ``explain_validation_error`` tool returns
    for the full message — this accessor is what lets the planner feedback carry
    that fix (e.g. "there is no 'fork' node_type; fork with a gate") without
    re-opening the message boundary.

    Returns ``None`` when no pattern matches, so callers attach nothing rather
    than a misleading generic. The ``_augment_with_expected_hint`` span is
    intentionally NOT applied: there is no error_text to mine an ``Expected …``
    hint from — only the bare code.
    """
    if type(code) is not str or not code:
        return None
    for pattern, explanation, fix in _VALIDATION_ERROR_PATTERNS:
        if re.search(pattern, code):
            return explanation, fix
    return None


# Static text, never per-request data — the same public-constant posture as
# every catalogue entry above, so it cannot re-open the message boundary the
# planner's allowlist protects.
_WITHHELD_VALIDATION_GUIDANCE: Final[tuple[str, str]] = (
    "This error is on a component whose configuration is bound server-side for this surface. "
    "Its true component id and validator detail are deliberately withheld: the message could "
    "quote reviewed private values that are redacted from this conversation.",
    "Do not guess at the withheld configuration — you cannot see or edit it. Re-verify only the "
    "components you authored yourself: call get_plugin_schema(<plugin_type>, <plugin_name>) for "
    "the exact option shapes and allowed values, and change only fields the other errors name. "
    "If this exact rejection set repeats, a near-identical candidate cannot succeed: restructure "
    "the pipeline, or decline honestly.",
)


def explain_withheld_validation_code(code: str) -> tuple[str, str] | None:
    """Blind-mode ``(explanation, suggested_fix)`` for a withheld rejection entry.

    The planner's repair feedback withholds a rejection entry's component id
    and validator detail when the entry is about a finalizer-owned component
    (guided reviewed sources/outputs, correction-restored nodes, auto-wired
    controls — elspeth-5904b1683a). The ordinary catalogue guidance is
    dishonest in that mode: ``plugin_options_invalid``'s fix opens with
    "Apply exactly what 'detail' names" when no detail is present, which
    sent live planners chasing a field that does not exist and burned the
    repair budget on re-emissions. This accessor returns guidance that is
    honest about the blindness for EVERY code — unlike
    :func:`explain_validation_code` it never returns ``None`` for a
    well-formed code, because naming the withholding is precisely the point.
    """
    if type(code) is not str or not code:
        return None
    return _WITHHELD_VALIDATION_GUIDANCE


def _execute_explain_validation_error(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Explain a validation error with human-readable diagnosis and fix."""
    validation = context.catalog.validate_composition_state(state).validation
    error_text = args["error_text"]
    for pattern, explanation, fix in _VALIDATION_ERROR_PATTERNS:
        if re.search(pattern, error_text):
            return ToolResult(
                success=True,
                updated_state=state,
                validation=validation,
                affected_nodes=(),
                data={
                    "error_text": error_text,
                    "explanation": explanation,
                    "suggested_fix": _augment_with_expected_hint(fix, error_text),
                },
            )
    # Fuzzy route: live planners pass junk like "ValidationError" or
    # "node:X ValidationError validation_error" instead of the message or the
    # bare code. A closed code buried in noise (any case) still resolves —
    # the direct pattern pass above is case-sensitive, so this catches e.g.
    # an upper-cased code echoed from a log line.
    lowered = error_text.lower()
    for code in _CLOSED_VALIDATION_ERROR_CODES:
        if code in lowered:
            guidance = explain_validation_code(code)
            if guidance is None:  # pragma: no cover — catalogue invariant is test-pinned
                continue
            explanation, fix = guidance
            return ToolResult(
                success=True,
                updated_state=state,
                validation=validation,
                affected_nodes=(),
                data={
                    "error_text": error_text,
                    "error_code": code,
                    "explanation": explanation,
                    "suggested_fix": _augment_with_expected_hint(fix, error_text),
                },
            )
    # No match at all — teach usage instead of an unhelpful generic. The codes
    # are a public static catalogue, so listing them leaks nothing.
    return ToolResult(
        success=True,
        updated_state=state,
        validation=validation,
        affected_nodes=(),
        data={
            "error_text": error_text,
            "explanation": "The text does not match any known validation message or closed error_code.",
            "suggested_fix": _augment_with_expected_hint(
                "Call explain_validation_error with the exact error_code string from the rejection "
                "feedback's validation.errors[].error_code. Closed codes: " + ", ".join(_CLOSED_VALIDATION_ERROR_CODES) + ".",
                error_text,
            ),
            "known_codes": list(_CLOSED_VALIDATION_ERROR_CODES),
        },
    )


_EXPLAIN_VALIDATION_ERROR_DECLARATION = ToolDeclaration(
    name="explain_validation_error",
    handler=_execute_explain_validation_error,
    kind=ToolKind.DISCOVERY,
    description="Get a human-readable explanation of a validation error "
    "with suggested fixes. Pass the exact error text from a validation result.",
    json_schema={
        "type": "object",
        "properties": {
            "error_text": {
                "type": "string",
                "description": "The validation error message to explain.",
            },
        },
        "required": ["error_text"],
        "additionalProperties": False,
    },
    cacheable=True,
)


def _serialize_plugin_assistance_example(
    example: Any,
) -> dict[str, Any]:
    """Serialize a PluginAssistanceExample for LLM consumption.

    Mirrors the serialize-at-the-boundary pattern used by
    ``_semantic_edge_contract_to_payload`` (composer_mcp/server.py) and
    ``serialize_semantic_contracts`` (web/execution/_semantic_helpers.py):
    L0 contract types intentionally have no ``.to_dict()``; the rendering
    site owns the JSON shape so contracts stay free of encoding concerns.
    """
    return {
        "title": example.title,
        "before": deep_thaw(example.before) if example.before is not None else None,
        "after": deep_thaw(example.after) if example.after is not None else None,
    }


def _execute_get_plugin_assistance(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Return plugin-owned guidance for a source, transform, or sink.

    Dual-use by ``issue_code``:

    * ``issue_code is None`` (or absent) — discovery-time guidance. The
      plugin returns a one-line ``summary`` and ``composer_hints``
      (same surface that list_* and get_plugin_schema already carry).
    * ``issue_code is not None`` — failure-time guidance. The
      semantic validator emits ``requirement_code`` values like
      ``line_explode.source_field.line_framed_text``; the agent echoes
      that code in to retrieve ``suggested_fixes`` + example
      before/after configs.

    When the plugin has no assistance to publish, returns success with
    a "no assistance published" payload (summary=None, empty lists)
    rather than failing — the absence is itself a useful signal.

    Unknown plugin name or invalid plugin_type surfaces here as a tool
    failure with the original message so the agent can correct the call.
    """
    plugin_type_raw = args["plugin_type"]
    plugin_name = args["plugin_name"]
    # ``args`` is LLM tool-call arguments (Tier 3). ``plugin_type``/``plugin_name``
    # are required (json_schema ``required``) so direct subscript lets a KeyError
    # surface an LLM contract violation; ``issue_code`` is optional (discovery vs
    # failure mode), so its absence is recorded honestly as ``None`` via the
    # membership form rather than a defensive ``.get``.
    issue_code = args["issue_code"] if "issue_code" in args else None

    if plugin_type_raw not in ("source", "transform", "sink"):
        return _failure_result(
            state,
            f"Unknown plugin_type: {plugin_type_raw!r}. Must be one of: 'source', 'transform', 'sink'.",
        )
    plugin_type: PluginKind = plugin_type_raw

    policy_error = _validate_plugin_name(context, plugin_type, plugin_name)
    if policy_error is not None:
        return _plugin_policy_failure(state, policy_error)
    try:
        context.catalog.get_schema(plugin_type, plugin_name)
    except (ValueError, KeyError) as exc:
        return _failure_result(state, str(exc))

    manager = get_shared_plugin_manager()
    try:
        if plugin_type == "source":
            plugin_cls: Any = manager.get_source_by_name(plugin_name)
        elif plugin_type == "transform":
            plugin_cls = manager.get_transform_by_name(plugin_name)
        else:
            plugin_cls = manager.get_sink_by_name(plugin_name)
    except PluginNotFoundError as exc:
        return _failure_result(state, str(exc))

    assistance = plugin_cls.get_agent_assistance(issue_code=issue_code)
    if assistance is not None:
        assistance = context.catalog.project_agent_assistance(plugin_type, plugin_name, assistance)

    if assistance is None:
        payload: dict[str, Any] = {
            "plugin_type": plugin_type,
            "plugin_name": plugin_name,
            "issue_code": issue_code,
            "summary": None,
            "suggested_fixes": [],
            "examples": [],
            "composer_hints": [],
        }
        return _discovery_result(state, payload)

    payload = {
        "plugin_type": plugin_type,
        "plugin_name": assistance.plugin_name,
        "issue_code": assistance.issue_code,
        "summary": assistance.summary,
        "suggested_fixes": list(assistance.suggested_fixes),
        "examples": [_serialize_plugin_assistance_example(ex) for ex in assistance.examples],
        "composer_hints": list(assistance.composer_hints),
    }
    return _discovery_result(state, payload)


_GET_PLUGIN_ASSISTANCE_DECLARATION = ToolDeclaration(
    name="get_plugin_assistance",
    handler=_execute_get_plugin_assistance,
    kind=ToolKind.DISCOVERY,
    description=(
        "Retrieve plugin-owned guidance for a source, transform, or sink. "
        "Two modes by ``issue_code``:\n"
        "  * Omit ``issue_code`` (or pass null) to get discovery-time guidance "
        "    — a summary of the plugin and composer_hints. (The same hints "
        "    are also carried on list_sources / list_transforms / list_sinks / "
        "    get_plugin_schema responses; this tool is the explicit path.)\n"
        "  * Pass an ``issue_code`` (validators emit these as requirement_code "
        "    on semantic_contracts entries) to get failure-time guidance — "
        "    summary, suggested_fixes, and example before/after configurations."
    ),
    json_schema={
        "type": "object",
        "properties": {
            "plugin_type": {
                "type": "string",
                "enum": ["source", "transform", "sink"],
                "description": "Plugin family. 'source', 'transform', or 'sink'.",
            },
            "plugin_name": {
                "type": "string",
                "description": "Plugin name (e.g. 'csv', 'web_scrape', 'database').",
            },
            "issue_code": {
                "type": ["string", "null"],
                "description": (
                    "Optional. Stable issue identifier owned by the plugin "
                    "for failure-time guidance. Omit or pass null for "
                    "discovery-time guidance."
                ),
            },
        },
        "required": ["plugin_type", "plugin_name"],
        "additionalProperties": False,
    },
    cacheable=True,
)


def _execute_get_audit_info(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Return constant facts about the Landscape audit trail.

    Audit is mandatory (`LandscapeSettings` rejects `enabled=false` at
    config validation time) and the backend URL is operator-managed via
    `WebSettings.get_landscape_url()` — security fix S1, see
    `web/composer/yaml_generator.py:179`. Letting the composer set the
    audit backend would let a user prompt redirect the audit trail to an
    attacker DB, disable encryption, or split audit across stores.

    The returned payload is a constant — no `WebSettings` access — so
    operator-internal config (URL, backend type, encryption-key env var)
    never reaches the LLM context. The model paraphrases `summary`; it
    does not need the URL itself.
    """
    del context  # unused; signature uniformity with the other handlers.
    payload = {
        "enabled": True,
        "composer_modifiable": False,
        "summary": (
            "Landscape audit is mandatory and always on for every pipeline run. "
            "The audit backend (database type, location, encryption) is configured "
            "by the operator at deploy time and is intentionally NOT addressable "
            "from the composer — letting the composer set it would be a security "
            "regression (audit-DSN injection, encryption bypass, audit split-brain). "
            "When a user asks for 'audit logging', 'SQLite audit', or similar: "
            "acknowledge that audit is already enabled for every run, do NOT add a "
            "sink shape for it, and do NOT silently remove an audit-shaped node by "
            "treating it as 'unconnected'. To inspect past runs, point the user at "
            "the Landscape MCP forensic tools."
        ),
        "audit_export_summary": (
            "A separate optional feature ('landscape.export') can copy each run's "
            "audit data to an additional sink for offline review. This is also "
            "operator-configured and is not currently composer-controllable. If a "
            "user asks for 'export the audit data to a file', explain that this is "
            "an operator-side configuration and is not part of the pipeline the "
            "composer is building."
        ),
    }
    return _discovery_result(state, payload)


_GET_AUDIT_INFO_DECLARATION = ToolDeclaration(
    name="get_audit_info",
    handler=_execute_get_audit_info,
    kind=ToolKind.DISCOVERY,
    description=(
        "Return facts about ELSPETH's Landscape audit trail. Call this BEFORE "
        "answering any user question that mentions audit logging, audit "
        "database, SQLite/Postgres audit, audit backend, audit export, "
        "Landscape, or 'how do I record what the pipeline did'. Audit is "
        "mandatory and operator-managed; the composer cannot configure the "
        "backend (security boundary — see yaml_generator.py:179, fix S1). "
        "Returns enabled status, composer_modifiable flag, and a canonical "
        "summary to paraphrase. Does NOT return the audit URL/path/DSN — "
        "that is operator-internal and intentionally not surfaced to the LLM."
    ),
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    cacheable=True,
)


def _execute_list_models(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """List available LLM model identifiers.

    Without a provider filter, returns provider names and model counts
    to avoid dumping hundreds of entries. With a provider filter,
    returns matching model IDs capped at ``limit``.

    For ``provider="openrouter/"`` the slugs are read from the live
    OpenRouter catalog primed at boot (or the bundled litellm fallback
    when the primer hasn't run / is offline) via
    :func:`get_catalog_values` against :data:`MODEL_CATALOG_OPENROUTER` —
    the same catalog accessor the value-source compliance walker
    consults at preflight. Sharing one source of truth between the
    composer's discovery surface and the validator closes the
    "composer recommended a model the validator then rejects" loop
    that previously surfaced as ``ValueSourceValidationError`` on
    tutorial runs. Non-OpenRouter providers continue to be served from
    the bundled litellm list because no per-provider live primer exists
    yet.
    """
    del context  # unused; signature uniformity with the other handlers.
    all_models: list[str] = list(read_litellm_model_list())

    # ``args`` is LLM tool-call arguments (Tier 3). Both ``provider`` and
    # ``limit`` are optional (json_schema ``required: []``). ``provider``
    # absence is recorded honestly as ``None`` (membership form). ``limit``
    # absence resolves to the documented default of 50 (the json_schema
    # description states "default 50") — a meaning-preserving substitution, not
    # fabrication. The scalar type checks use ``type() is`` so a bool ``limit``
    # (``isinstance(True, int)`` is True) is correctly rejected at the boundary.
    provider = args["provider"] if "provider" in args else None
    limit = args["limit"] if "limit" in args else 50
    if type(limit) is not int or limit < 1:
        limit = 50

    if provider is not None and type(provider) is str:
        normalised = provider.rstrip("/")
        if normalised == OPENROUTER_LITELLM_PREFIX.rstrip("/"):
            # Live-catalog read returns un-prefixed slugs already (e.g.
            # ``anthropic/claude-sonnet-4.5``) — exactly the form the
            # OpenRouterLLMProvider config field ``model`` expects and
            # the value-source walker compares against. No prefix strip
            # needed; the litellm fallback path inside
            # ``get_catalog_values`` already strips ``openrouter/`` from
            # its bundled list.
            filtered = sorted(get_catalog_values(MODEL_CATALOG_OPENROUTER))
        elif provider == "":
            # Empty string means "models without a provider prefix"
            filtered = [m for m in all_models if "/" not in m]
        else:
            filtered = [m for m in all_models if m.startswith(provider)]
        truncated = len(filtered) > limit
        data: dict[str, Any] = {
            "models": filtered[:limit],
            "count": len(filtered),
            "truncated": truncated,
        }
    else:
        # Group by provider prefix to avoid token waste. ``providers`` is our
        # own freshly-constructed accumulator dict, not a Tier-3 boundary, so
        # the offensive-programming rule rejects ``providers.get(prefix, 0)``
        # as a defensive read on data we wrote. Initialize the slot
        # explicitly before increment instead.
        providers: dict[str, int] = {}
        for m in all_models:
            prefix = m.split("/", 1)[0] if "/" in m else ""
            if prefix not in providers:
                providers[prefix] = 0
            providers[prefix] += 1
        # Replace the bundled-litellm openrouter count with the live
        # catalog size so the unfiltered summary advertises the same
        # numbers a follow-up ``provider="openrouter/"`` filter will
        # return. The litellm bundled list and the live OpenRouter
        # catalog drift apart as Anthropic / OpenAI rotate model slugs;
        # advertising the bundled count would invite the composer to
        # request a "known" model that the validator then rejects.
        openrouter_key = OPENROUTER_LITELLM_PREFIX.rstrip("/")
        live_openrouter = get_catalog_values(MODEL_CATALOG_OPENROUTER)
        if openrouter_key in providers:
            bundled = providers[openrouter_key]
            providers[openrouter_key] = len(live_openrouter)
            total_models = len(all_models) - bundled + len(live_openrouter)
        else:
            providers[openrouter_key] = len(live_openrouter)
            total_models = len(all_models) + len(live_openrouter)
        data = {
            "providers": providers,
            "total_models": total_models,
            "hint": "Use provider parameter to list models for a specific provider. An empty string key means models without a provider prefix.",
        }

    return _discovery_result(state, data)


_LIST_MODELS_DECLARATION = ToolDeclaration(
    name="list_models",
    handler=_execute_list_models,
    kind=ToolKind.DISCOVERY,
    description="List available LLM model identifiers. Without a provider "
    "filter, returns provider names and counts. With a provider filter, "
    "returns matching model IDs (capped at limit). For provider='openrouter/' "
    "the returned slugs are normalised to OpenRouter's HTTP API form "
    "(without the litellm-internal 'openrouter/' routing prefix) — these "
    "are the values to put directly in `model:`.",
    json_schema={
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Provider prefix to filter by (e.g. 'openrouter/', 'azure/'). "
                "Omit to get a provider summary instead of individual models.",
            },
            "limit": {
                "type": "integer",
                "description": "Max models to return (default 50).",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
    cacheable=True,
)


_BLOCKING_DIAGNOSTIC_CODES: Final[frozenset[str]] = frozenset(
    {
        "aggregation_numeric_value_field_type_mismatch_against_source_schema",
        "csv_duplicate_headers",
        "csv_fixed_schema_omits_observed_columns",
        "csv_source_blob_header_mismatch",
        "csv_source_field_resolution_error",
        "gate_expression_preview_memory_exhaustion",
        "gate_expression_type_mismatch_against_source_schema",
        "gate_expression_unbounded_string_amplification",
        "text_source_url_without_web_scrape",
        "source_inspection_failed",
    }
)
_MAX_PROOF_BLOB_SOURCES: Final[int] = 256


@dataclass(frozen=True, slots=True)
class ResolvedProofBlob:
    """One custody-resolved blob and optional verified bounded content."""

    metadata: BlobToolRecord
    verified_prefix: bytes | None = None
    verified_content_hash: str | None = None
    total_size_bytes: int | None = None

    def __post_init__(self) -> None:
        supplied = (
            self.verified_prefix is not None,
            self.verified_content_hash is not None,
            self.total_size_bytes is not None,
        )
        if any(supplied) and not all(supplied):
            raise TypeError("verified proof bytes, content hash, and total size must be supplied together")
        if not any(supplied):
            return
        if type(self.verified_prefix) is not bytes or len(self.verified_prefix) > 8 * 1024:
            raise TypeError("verified proof prefix must be exact bytes bounded to 8192 bytes")
        if type(self.verified_content_hash) is not str:
            raise TypeError("verified proof content hash must be an exact string")
        if type(self.total_size_bytes) is not int or self.total_size_bytes < len(self.verified_prefix):
            raise TypeError("verified proof total size must be an exact int no smaller than the prefix")
        stored_hash = self.metadata["content_hash"]
        if stored_hash is None:
            raise AuditIntegrityError("ready proof blob has null content_hash")
        if not hmac.compare_digest(stored_hash, self.verified_content_hash):
            raise BlobIntegrityError(
                self.metadata["id"],
                expected=stored_hash,
                actual=self.verified_content_hash,
            )
        if self.metadata["size_bytes"] != self.total_size_bytes:
            raise AuditIntegrityError("verified proof blob size differs from its custody metadata")


@dataclass(frozen=True, slots=True)
class UnresolvedClaimedProofBlob:
    """Nominal marker that a reviewed sentinel's custody claim failed."""


class _BlockingDiagnosticPayload(TypedDict):
    code: str
    severity: Literal["blocking"]
    message: str
    suggested_repair: str
    evidence_locator: Mapping[str, Any]


def _blocking_diagnostic(
    *,
    code: str,
    message: str,
    suggested_repair: str,
    evidence_locator: Mapping[str, Any],
) -> _BlockingDiagnosticPayload:
    """Construct a blocking diagnostic dict and assert the code is registered.

    Offensive: a contributor who adds a new blocker without registering it in
    ``_BLOCKING_DIAGNOSTIC_CODES`` (and the matching skill-markdown vocabulary)
    crashes immediately rather than shipping an unrecognised code into the
    audit trail and the LLM's repair-message context.
    """
    if code not in _BLOCKING_DIAGNOSTIC_CODES:
        raise AssertionError(
            f"blocking diagnostic code {code!r} is not registered in "
            f"_BLOCKING_DIAGNOSTIC_CODES. Add it there (and to the skill-"
            f"markdown vocabulary the composer LLM consumes) before emitting "
            f"a blocking diagnostic with this code."
        )
    return {
        "code": code,
        "severity": "blocking",
        "message": message,
        "suggested_repair": suggested_repair,
        "evidence_locator": evidence_locator,
    }


def _csv_source_field_resolution_error_diagnostic(
    *,
    blob_id: object,
    facts: SourceInspectionFacts,
    exc: ValueError,
) -> _BlockingDiagnosticPayload:
    return _blocking_diagnostic(
        code="csv_source_field_resolution_error",
        message=(
            "CSV source header resolution failed before proof diagnostics could compare "
            "declared fields to observed headers. CSVSource would reject this shape at "
            "runtime, so preview_pipeline is blocking it for repair. The raw resolver "
            "error text and observed header values are withheld: header-resolution "
            "failure means a headerless or malformed CSV can make the first data row "
            "look like headers, so those values are treated as potential row content."
        ),
        suggested_repair=(
            "Rename colliding or invalid CSV headers, correct field_mapping keys and "
            "values to match normalized source headers, or declare explicit `columns` "
            "for headerless input, then re-run preview_pipeline."
        ),
        evidence_locator={
            "source": "blob",
            "blob_id": str(blob_id),
            "error_class": type(exc).__name__,
            "observed_header_count": len(facts.observed_headers or ()),
            "observed_headers_redacted": True,
        },
    )


def _csv_source_schema_config_error_diagnostic(
    *,
    blob_id: object,
    facts: SourceInspectionFacts,
    exc: ValueError,
) -> _BlockingDiagnosticPayload:
    """Distinct repair guidance for a ``schema.*``-block parse failure.

    Shares the registered ``csv_source_field_resolution_error`` code (the
    composer LLM's repair vocabulary and the skill markdown are keyed on that
    code; adding a new code would silently half-wire the LLM contract), but the
    repair text points at the ``schema.*`` knob rather than at header /
    field_mapping / columns resolution. A failure from ``get_raw_schema_config``
    is a malformed *schema declaration* (bad ``schema.mode``, a malformed field
    spec, a non-bool required flag), NOT a header-resolution problem — so the
    generic field-resolution repair text would misdirect the operator and the
    LLM. The raw ``{exc}`` is NOT interpolated: it can quote observed header /
    field values that, under a failed header resolution, may be row content; the
    exception class is surfaced structurally instead and the repair text points
    at the ``schema.*`` knob.
    """
    return _blocking_diagnostic(
        code="csv_source_field_resolution_error",
        message=(
            "CSV source schema declaration failed to parse before proof diagnostics "
            "could compare declared fields to observed headers. CSVSource would reject "
            "this schema at runtime, so preview_pipeline is blocking it for repair. The "
            "raw parser error text and observed header values are withheld (they may "
            "carry row content for a headerless or malformed CSV)."
        ),
        suggested_repair=(
            "Correct the source `schema` block: `schema.mode` must be one of "
            "'fixed'/'flexible'/'observed', each entry in `schema.fields` must be a "
            "valid field spec (a 'name: type' string or a mapping with a str name and a "
            "bool required flag), then re-run preview_pipeline."
        ),
        evidence_locator={
            "source": "blob",
            "blob_id": str(blob_id),
            "error_class": type(exc).__name__,
            "observed_header_count": len(facts.observed_headers or ()),
            "observed_headers_redacted": True,
        },
    )


def _source_schema_mode(source: SourceSpec) -> str | None:
    # ``source.options`` is composer/user-authored config re-read from persisted
    # session state — Tier-3 origin (we authored the container, they authored the
    # values), so absence is recorded as ``None`` and shape is validated, never
    # coerced. Membership form (not ``.get``) makes the honest absence→None
    # explicit at the boundary.
    schema = source.options["schema"] if "schema" in source.options else None
    if not isinstance(schema, Mapping):
        return None
    mode = schema["mode"] if "mode" in schema else None
    if type(mode) is not str:
        return None
    return mode.strip().lower()


def _sample_csv_rows(content: bytes, *, filename: str, max_rows: int = 100) -> tuple[dict[str, str], ...]:
    text = content[: 8 * 1024].decode("utf-8", errors="replace")
    delimiter = "\t" if filename.lower().endswith(".tsv") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows: list[dict[str, str]] = []
    for index, row in enumerate(reader):
        if index >= max_rows:
            break
        rows.append({key: value for key, value in row.items() if type(key) is str and value is not None})
    return tuple(rows)


def _row_fields_referenced_by_condition(condition: str) -> tuple[str, ...]:
    tree = ast.parse(condition, mode="eval")
    fields: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "row"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            fields.append(node.slice.value)
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "row"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            fields.append(node.args[0].value)
    return tuple(dict.fromkeys(fields))


def _gate_expression_type_diagnostics_for_observed_csv(
    state: CompositionState,
    source_name: str,
    source: SourceSpec,
    *,
    blob_id: str,
    filename: str,
    content: bytes,
) -> list[Mapping[str, Any]]:
    """Evaluate provably field-preserving source-fed gates against sampled rows.

    Observed CSV sources emit raw strings because there are no declared field
    types to coerce against. A gate such as ``row['amount'] >= 1000`` is
    syntactically valid but fails at runtime when the evaluator compares
    ``str`` with ``int``. This preview proof step uses the same expression
    evaluator against bounded raw rows and reports the type mismatch without
    surfacing row values.
    """
    if _source_schema_mode(source) != "observed":
        return []

    rows = _sample_csv_rows(content, filename=filename)
    if not rows:
        return []

    diagnostics: list[Mapping[str, Any]] = []
    gate_nodes = (node for node in state.nodes if node.node_type == "gate" and node.condition is not None)
    for node in gate_nodes:
        condition = node.condition
        if condition is None:
            continue
        if _validate_gate_expression(condition) is not None:
            continue
        fields = _row_fields_referenced_by_condition(condition)
        if not fields or not all(
            _source_field_reaches_connection_without_type_change(
                state,
                node.input,
                source_name=source_name,
                field_name=field_name,
            )
            for field_name in fields
        ):
            continue
        parser = ExpressionParser(condition)
        field = fields[0] if fields else None
        if parser.has_string_amplification_risk():
            # THREAT-001: str repetition / printf-formatting over observed
            # (string-typed) fields allocates output linear in the literal,
            # per sampled row, inside the web process. Diagnose IN PLACE OF
            # evaluating — never evaluate, never silently skip.
            diagnostics.append(
                _blocking_diagnostic(
                    code="gate_expression_unbounded_string_amplification",
                    message=(
                        f"Gate '{node.id}' condition {condition!r} multiplies or %-formats a field that is "
                        "string-typed under this observed CSV source. String repetition and formatting "
                        "allocate output proportional to their inputs, so this expression is not evaluated "
                        "at preview. If the field is meant to be numeric, declare it in the source schema; "
                        "if string repetition/formatting is intended, it is not a routable gate condition."
                    ),
                    suggested_repair=(
                        "Patch the source schema to declare the field with an explicit numeric type, for "
                        "example schema.mode='fixed' or 'flexible' with schema.fields including "
                        f"{field + ': int' if field is not None else '<field>: int'}, then re-run preview_pipeline."
                    ),
                    evidence_locator={
                        "source": "blob",
                        "blob_id": str(blob_id),
                        "node_id": node.id,
                        "field": field,
                        "fields": list(fields),
                        "source_schema_mode": "observed",
                    },
                )
            )
            continue
        for row_index, row in enumerate(rows):
            try:
                parser.evaluate(row)
            except ExpressionEvaluationError:
                diagnostics.append(
                    _blocking_diagnostic(
                        code="gate_expression_type_mismatch_against_source_schema",
                        message=(
                            f"Gate '{node.id}' condition {condition!r} fails against sampled observed CSV "
                            "rows before runtime. Observed CSV source values are strings unless the "
                            "source schema declares explicit field types. (The raw evaluation error is "
                            "withheld: it can quote sampled row values and the observed field/key set.)"
                        ),
                        suggested_repair=(
                            "Patch the source schema to declare the compared field with an explicit numeric "
                            "type, for example schema.mode='fixed' or 'flexible' with schema.fields including "
                            f"{field + ': int' if field is not None else '<field>: int'}, then re-run preview_pipeline."
                        ),
                        evidence_locator={
                            "source": "blob",
                            "blob_id": str(blob_id),
                            "node_id": node.id,
                            "field": field,
                            "fields": list(fields),
                            "sample_row_index": row_index,
                            "source_schema_mode": "observed",
                        },
                    )
                )
                break
            except MemoryError:
                # Belt-and-braces behind the amplification predicate: a
                # resource event must not receive the schema-typing repair.
                # By the time this fires the process is already degraded, so
                # the diagnostic is best-effort — the predicate above is the
                # actual bound.
                diagnostics.append(
                    _blocking_diagnostic(
                        code="gate_expression_preview_memory_exhaustion",
                        message=(
                            f"Gate '{node.id}' condition {condition!r} exhausted memory while being "
                            "evaluated against sampled observed CSV rows at preview. This is a resource "
                            "failure, not a field-typing mismatch."
                        ),
                        suggested_repair=(
                            "Simplify the gate condition so it does not build large intermediate values, then re-run preview_pipeline."
                        ),
                        evidence_locator={
                            "source": "blob",
                            "blob_id": str(blob_id),
                            "node_id": node.id,
                            "field": field,
                            "fields": list(fields),
                            "sample_row_index": row_index,
                            "source_schema_mode": "observed",
                        },
                    )
                )
                break
    return diagnostics


_NUMERIC_VALUE_FIELD_AGGREGATION_PLUGINS: Final[frozenset[str]] = frozenset(
    {
        "batch_distribution_profile",
        "batch_outlier_annotator",
        "batch_stats",
        "batch_threshold_summary",
    }
)


def _value_transform_preserves_field(node: NodeSpec, field_name: str) -> bool:
    # ``node.options`` is composer/user-authored config re-read from persisted
    # session state — Tier-3 origin. Membership form (not ``.get``) records the
    # honest absence→None at the boundary; the subtype guards below remain
    # because frozen config arrives as MappingProxyType/tuple subtypes.
    operations = node.options["operations"] if "operations" in node.options else None
    if not isinstance(operations, (list, tuple)):
        return False
    for operation in operations:
        if not isinstance(operation, Mapping):
            return False
        target = operation["target"] if "target" in operation else None
        if target == field_name:
            return False
    return True


def _source_field_reaches_connection_without_type_change(
    state: CompositionState,
    connection_name: str,
    *,
    source_name: str,
    field_name: str,
) -> bool:
    """Return True when a source field flows to a connection unchanged.

    This intentionally recognises only field-preserving nodes. Declared queues
    traverse their predecessor set source-by-source; unrelated fan-in never
    stands in for the target source. Unknown transforms may coerce, overwrite,
    delete, or synthesize the field, so the proof step abstains instead of
    emitting a false positive.
    """
    resolver = ProducerResolver.build(
        source=None,
        sources=state.sources,
        nodes=state.nodes,
        sink_names=frozenset(output.name for output in state.outputs),
    )
    target_source_id = source_producer_id(source_name)

    def _producer_preserves_field(
        producer: ProducerEntry,
        *,
        visiting: frozenset[str],
    ) -> bool:
        if is_source_producer_id(producer.producer_id):
            return producer.producer_id == target_source_id
        if producer.producer_id in visiting:
            return False
        node = resolver.get_node(producer.producer_id)
        if node is None:
            return False
        next_visiting = visiting | {producer.producer_id}
        if node.node_type == "queue":
            return any(
                _producer_preserves_field(predecessor, visiting=next_visiting) for predecessor in resolver.queue_predecessors(node.id)
            )
        if node.node_type == "gate":
            return _connection_preserves_field(node.input, visiting=next_visiting)
        if node.plugin == "value_transform" and _value_transform_preserves_field(node, field_name):
            return _connection_preserves_field(node.input, visiting=next_visiting)
        if node.plugin == "passthrough":
            return _connection_preserves_field(node.input, visiting=next_visiting)
        return False

    def _connection_preserves_field(
        current: str,
        *,
        visiting: frozenset[str],
    ) -> bool:
        producer = resolver.find_producer_for(current)
        if producer is None:
            return False
        return _producer_preserves_field(producer, visiting=visiting)

    return _connection_preserves_field(connection_name, visiting=frozenset())


def _numeric_aggregation_diagnostics_for_observed_csv(
    state: CompositionState,
    source_name: str,
    source: SourceSpec,
    *,
    blob_id: str,
    inferred_types: Mapping[str, str] | None,
    observed_headers: tuple[str, ...] | None,
) -> list[Mapping[str, Any]]:
    """Block observed CSV strings before numeric aggregation runtime failures."""
    if _source_schema_mode(source) != "observed" or observed_headers is None:
        return []

    observed_header_set = set(observed_headers)
    diagnostics: list[Mapping[str, Any]] = []
    for node in state.nodes:
        if node.node_type != "aggregation" or node.plugin not in _NUMERIC_VALUE_FIELD_AGGREGATION_PLUGINS:
            continue
        options, _owner = get_aggregation_contract_options(node.options, owner=f"node:{node.id}")
        # ``options`` is the external-origin (composer/user-authored) node
        # options Mapping returned by the contract helper — Tier-3. Membership
        # form records honest absence→None; the following ``type() is`` check is
        # the boundary validation of the scalar value.
        value_field = options["value_field"] if "value_field" in options else None
        if type(value_field) is not str or not value_field.strip():
            continue
        value_field = value_field.strip()
        if value_field not in observed_header_set:
            continue
        if not _source_field_reaches_connection_without_type_change(
            state,
            node.input,
            source_name=source_name,
            field_name=value_field,
        ):
            continue

        # ``inferred_types`` is our own Tier-2 derived inspection output. When
        # present it is built by ``_inspect_csv`` as ``{h: ... for h in
        # headers}`` against the SAME ``headers`` tuple it returns as
        # ``observed_headers`` — i.e. it carries exactly one entry per observed
        # header, never omitting one (``_merge_types`` always yields a type).
        # The whole map can legitimately be absent (None) for an empty/parse-
        # error blob, so that absence is handled explicitly. But ``value_field``
        # was already confirmed present in ``observed_header_set`` above, so a
        # non-None map is GUARANTEED to contain it: a missing key would be a
        # Tier-2 invariant break in our own inspection output, which must crash
        # (subscript), not be silently coerced to None by ``.get()``.
        inferred_type = inferred_types[value_field] if inferred_types is not None else None
        diagnostics.append(
            _blocking_diagnostic(
                code="aggregation_numeric_value_field_type_mismatch_against_source_schema",
                message=(
                    f"Aggregation '{node.id}' ({node.plugin}) uses numeric value_field '{value_field}', "
                    "but it is flowing from an observed CSV source. Observed CSV source values are strings "
                    "unless the source schema declares explicit field types or an upstream type_coerce node "
                    "converts the field before aggregation."
                ),
                suggested_repair=(
                    "Patch the source schema to declare the aggregated field with an explicit numeric type "
                    f"(for example {value_field}: float), or insert a type_coerce node upstream of the aggregation "
                    "with a conversions entry targeting the numeric type. A type_coerce (or value_transform) node's "
                    f"own schema: block declares what ARRIVES at the node — here {value_field} arrives as str — never "
                    "the transformed result; the coerced type belongs to the node's output contract and is derived "
                    "automatically from its conversions. Declaring the post-coercion type on the node's own schema is "
                    "an unsatisfiable input contract and is rejected at the edge. If the field is categorical and you "
                    "want counts/frequencies, use batch_top_k instead of a numeric aggregation."
                ),
                evidence_locator={
                    "source": "blob",
                    "blob_id": str(blob_id),
                    "node_id": node.id,
                    "plugin": node.plugin,
                    "field": value_field,
                    "observed_type": "str",
                    "inferred_sample_type": inferred_type or "unknown",
                    "source_runtime_type": "str",
                    "source_schema_mode": "observed",
                },
            )
        )

    return diagnostics


def _compute_proof_diagnostics_for_source(
    state: CompositionState,
    *,
    source_name: str,
    source: SourceSpec,
    blob_id: object,
    blob_resolver: Callable[[str], ResolvedProofBlob | UnresolvedClaimedProofBlob | None],
) -> list[Mapping[str, Any]]:
    """Compute machine-readable proof diagnostics for one blob-backed source.

    Promotes ``preview_pipeline`` from a "state validates" check into a
    "state is plausibly runnable against observed input" proof. Returns a
    machine-readable list of diagnostics — each entry has::

        {
            "code": "csv_fixed_schema_omits_observed_columns",
            "severity": "blocking" | "warning" | "info",
            "message": "human-readable description",
            "suggested_repair": "tool/options the LLM should call",
            "evidence_locator": {"source": "...", "node_id": "...", ...},
        }

    Diagnostics surfaced:

      * ``csv_fixed_schema_omits_observed_columns`` — fixed CSV schema +
        on_validation_failure=discard + at least one observed column
        absent from declared fields. The combination silently discards
        every row, which is the #1 historical convergence-failure mode.
      * ``csv_source_blob_header_mismatch`` — CSV source with required
        declared fields but no overlap with the parsed blob header. This
        catches headerless CSV/list inputs before the first row is consumed
        as a header and every data row is discarded.
      * ``csv_source_field_resolution_error`` — CSVSource-style header
        normalization or field_mapping resolution failed before schema
        comparison could proceed.
      * ``text_source_url_without_web_scrape`` — text source whose blob
        content is a single URL but no web_scrape node downstream. The
        URL string itself reaches sinks instead of the URL's content.
      * ``gate_expression_type_mismatch_against_source_schema`` — observed
        CSV source values are still strings, and a gate reached through a
        provably field-preserving path fails against sampled rows before runtime.
      * ``aggregation_numeric_value_field_type_mismatch_against_source_schema`` —
        observed CSV strings flow unchanged into a numeric aggregation
        ``value_field`` before runtime can reject the batch.
      * ``source_inspection_warning`` — every warning surfaced by
        ``inspect_blob_content`` is mirrored here at ``info`` severity
        so the model sees them in the same array as blocking issues.

    Bounded I/O: exactly one attempted blob resolution per call, bounded by
    ``inspect_blob_content``'s 8 KiB / 100 row caps.
    """
    diagnostics: list[Mapping[str, Any]] = []

    resolved_blob = blob_resolver(str(blob_id))
    if isinstance(resolved_blob, UnresolvedClaimedProofBlob):
        return [
            _blocking_diagnostic(
                code="source_inspection_failed",
                message=(
                    "A guided reviewed source claims blob custody, but the live blob is not an exact ready, session-owned path match."
                ),
                suggested_repair="Re-select or re-upload the source blob, then confirm the guided wiring again.",
                evidence_locator={"source": "blob", "blob_id": str(blob_id)},
            )
        ]
    blob = None if resolved_blob is None else resolved_blob.metadata
    # ``blob`` is a BlobToolRecord (TypedDict produced by
    # ``_blob_row_to_tool_dict`` from a validated blobs row). Direct
    # subscript access is mandatory — a missing key is a Tier-1
    # contract violation in our own dict shape, not external data.
    if blob is None or blob["status"] != "ready":
        return diagnostics
    assert resolved_blob is not None

    if resolved_blob.verified_prefix is None:
        storage_path = Path(blob["storage_path"])
        if not storage_path.exists():
            diagnostics.append(
                _blocking_diagnostic(
                    code="source_inspection_failed",
                    message=(f"Source blob '{blob_id}' storage file is missing — pipeline cannot run until the blob is re-uploaded."),
                    suggested_repair="create_blob with the original content and re-wire via set_source_from_blob",
                    evidence_locator={"source": "blob", "blob_id": str(blob_id)},
                )
            )
            return diagnostics

        # Existing Composer-engine behavior: read and verify the complete local
        # file. Authoritative validate/execute callers instead supply the
        # BlobService's already-verified 8 KiB prefix to avoid full-file memory
        # use and a metadata/path TOCTOU seam.
        content = storage_path.read_bytes()
        _verify_blob_content_integrity(blob, content)
        total_size_bytes = len(content)
    else:
        content = resolved_blob.verified_prefix
        assert resolved_blob.total_size_bytes is not None
        total_size_bytes = resolved_blob.total_size_bytes

    facts = inspect_blob_content(
        content=content,
        filename=blob["filename"],
        mime_type=blob["mime_type"],
        content_hash=blob["content_hash"],
        total_size_bytes=total_size_bytes,
    )
    if source.plugin == "csv":
        # ``source.options`` is composer/user-authored config re-read from
        # persisted session state — Tier-3 origin (see the long note on the
        # ``get_raw_schema_config`` catch below). ``_csv_source_delimiter`` /
        # ``_csv_source_skip_rows`` / ``_csv_source_columns`` each re-validate a
        # persisted external-origin option and raise a raw ``ValueError`` on a
        # malformed value (non-single-char delimiter, non-int/negative skip_rows,
        # non-sequence/non-str columns). ``compute_proof_diagnostics`` is called
        # UNWRAPPED from ``_execute_preview_pipeline`` and the forced-repair gate,
        # and the tool dispatcher only catches ``ToolArgumentError`` — so an
        # unhandled ``ValueError`` here would escape and crash the preview_pipeline
        # tool over recoverable bad external input. Wrap the whole inspect call,
        # turn the failure into a per-blob blocking diagnostic (quarantine-
        # equivalent), and early-return: every downstream diagnostic section
        # (schema-mismatch, gate/aggregation type checks, text-URL, inspection
        # warnings) depends on ``facts``, which we could not compute, so there is
        # no independent diagnostic left to produce for this blob. The early
        # return also makes the later ``_csv_source_columns(source.options)`` call
        # safe-by-construction — reaching it means ``columns`` already parsed here.
        try:
            facts = inspect_csv_source_content(
                content=content,
                filename=blob["filename"],
                mime_type=blob["mime_type"],
                delimiter=_csv_source_delimiter(source.options),
                skip_rows=_csv_source_skip_rows(source.options),
                columns=_csv_source_columns(source.options),
                content_hash=blob["content_hash"],
            )
        except ValueError as exc:
            diagnostics.append(
                _csv_source_field_resolution_error_diagnostic(
                    blob_id=blob_id,
                    facts=facts,
                    exc=exc,
                )
            )
            return diagnostics

    # 1. Fixed CSV schema omits observed columns + discard => silent all-row drop.
    if facts.source_kind in {"csv", "json", "jsonl"}:
        # ``source.options`` is composer/user-authored configuration re-read
        # from persisted session state — Tier-3 origin, not Tier-1. We authored
        # the container (``SourceSpec`` / the options Mapping) and the schema
        # that validated it at load time, but the *values* were authored by the
        # operator or the composer LLM. Persistence does not promote that to
        # Tier-1: the store is mutable (hand-edits, stale restore) and the
        # validating contract can drift between write and read, so re-reading it
        # here is a FRESH Tier-3 boundary, not a Tier-1 invariant check. A
        # ``ValueError`` from ``get_raw_schema_config`` is therefore recoverable
        # bad external-origin input — exactly like a malformed source row — and
        # is caught and turned into a per-blob blocking diagnostic
        # (quarantine-equivalent: record what we found, skip the dependent
        # schema-mismatch analysis for this blob) rather than being allowed to
        # escape and crash the preview_pipeline tool. This mirrors the
        # ``_csv_source_field_mapping`` try/except below. The typed
        # ``SchemaConfig`` it returns replaces the prior raw-dict probing of
        # mode/fields; declared field specs are bridged back into the spec form
        # ``derive_*_risk`` consumes via ``to_dict()`` so the typed field
        # names/required flags flow through unchanged.
        try:
            schema_config = get_raw_schema_config(source.options, owner=f"source:{source.plugin}")
        except ValueError as exc:
            diagnostics.append(
                _csv_source_schema_config_error_diagnostic(
                    blob_id=blob_id,
                    facts=facts,
                    exc=exc,
                )
            )
            # Parse failed and the diagnostic is the record. Fall through with a
            # ``None`` schema_config so the existing ``is not None`` guard skips
            # the schema-mismatch analysis for this blob, mirroring how
            # ``field_resolution_failed`` short-circuits dependent work below.
            schema_config = None
        if schema_config is not None and schema_config.mode in {"fixed", "flexible"}:
            declared: tuple[Mapping[str, Any], ...] = tuple(schema_config.to_dict()["fields"] or ())
            headerless_columns = source.plugin == "csv" and _csv_source_columns(source.options) is not None
            field_mapping: dict[str, str] | None = None
            field_resolution_failed = False
            if source.plugin == "csv" and not headerless_columns:
                try:
                    field_mapping = _csv_source_field_mapping(source.options)
                except ValueError as exc:
                    diagnostics.append(
                        _csv_source_field_resolution_error_diagnostic(
                            blob_id=blob_id,
                            facts=facts,
                            exc=exc,
                        )
                    )
                    field_resolution_failed = True

            missing_declared: tuple[str, ...] = ()
            if not field_resolution_failed and not headerless_columns and facts.source_kind == "csv":
                try:
                    missing_declared = derive_required_header_mismatch_risk(
                        facts,
                        declared,
                        explicit_required_fields=schema_config.required_fields or (),
                        field_mapping=field_mapping,
                    )
                except ValueError as exc:
                    diagnostics.append(
                        _csv_source_field_resolution_error_diagnostic(
                            blob_id=blob_id,
                            facts=facts,
                            exc=exc,
                        )
                    )
                    field_resolution_failed = True
            if missing_declared and source.on_validation_failure == "discard":
                observed_header_count = len(facts.observed_headers or ())
                diagnostics.append(
                    _blocking_diagnostic(
                        code="csv_source_blob_header_mismatch",
                        message=(
                            f"CSV source declares required field(s) {list(missing_declared)} "
                            f"but the bound blob's parsed header has {observed_header_count} column(s) "
                            "with no overlapping field names. Header values are redacted because "
                            "headerless CSV input can make the first data row look like headers. "
                            "Every row will fail validation; "
                            "with on_validation_failure='discard', the run will terminate empty. "
                            "Either prepend a header row containing the declared field names or set "
                            "source options.columns to those names for headerless CSV input."
                        ),
                        suggested_repair=(
                            "For headered CSV, update the blob so line 1 contains the declared "
                            "schema field names. For headerless CSV, patch_source_options with "
                            "`columns` set to the declared field names, then re-run preview_pipeline. "
                            "See pipeline_composer.md rule 10."
                        ),
                        evidence_locator={
                            "source": "blob",
                            "blob_id": str(blob_id),
                            "declared_required_fields": list(missing_declared),
                            "observed_header_count": observed_header_count,
                            "observed_headers_redacted": True,
                            "source_plugin": source.plugin,
                        },
                    )
                )
            elif schema_config.mode == "fixed" and not field_resolution_failed:
                missing: tuple[str, ...] = ()
                if not headerless_columns:
                    try:
                        missing = derive_extra_column_risk(
                            facts,
                            declared,
                            field_mapping=field_mapping if source.plugin == "csv" else None,
                        )
                    except ValueError as exc:
                        diagnostics.append(
                            _csv_source_field_resolution_error_diagnostic(
                                blob_id=blob_id,
                                facts=facts,
                                exc=exc,
                            )
                        )
                    if missing and source.on_validation_failure == "discard":
                        diagnostics.append(
                            _blocking_diagnostic(
                                code="csv_fixed_schema_omits_observed_columns",
                                message=(
                                    f"Source schema is mode=fixed but {len(missing)} observed column(s) "
                                    "are not declared in schema.fields. Combined with "
                                    "on_validation_failure='discard', every row will be dropped because "
                                    "each contains an undeclared column. (Observed and missing column "
                                    "values are withheld: an observed-mode or headerless CSV can make a "
                                    "data row look like column headers.)"
                                ),
                                suggested_repair=(
                                    "patch_source_options with schema.mode='flexible' to accept extra "
                                    "columns, OR add the missing columns to schema.fields, OR set "
                                    "on_validation_failure to a configured output for inspection."
                                ),
                                evidence_locator={
                                    "source": "blob",
                                    "blob_id": str(blob_id),
                                    "missing_column_count": len(missing),
                                    "observed_column_count": len(facts.observed_headers or ()),
                                    "observed_columns_redacted": True,
                                },
                            )
                        )

    # 2. Observed CSV + numeric gate predicate => preview/runtime agreement gap.
    if facts.source_kind == "csv":
        diagnostics.extend(
            _gate_expression_type_diagnostics_for_observed_csv(
                state,
                source_name,
                source,
                blob_id=str(blob_id),
                filename=blob["filename"],
                content=content,
            )
        )
        diagnostics.extend(
            _numeric_aggregation_diagnostics_for_observed_csv(
                state,
                source_name,
                source,
                blob_id=str(blob_id),
                inferred_types=facts.inferred_types,
                observed_headers=facts.observed_headers,
            )
        )

    # 3. Text source containing a single URL but no web_scrape downstream.
    if facts.source_kind == "text" and facts.url_candidates:
        node_plugins = {(n.plugin or "").lower() for n in state.nodes}
        if "web_scrape" not in node_plugins:
            diagnostics.append(
                _blocking_diagnostic(
                    code="text_source_url_without_web_scrape",
                    message=(
                        f"Source blob contains URL(s) {list(facts.url_candidates)} but no "
                        "web_scrape transform is wired downstream. The URL string itself will "
                        "flow to sinks, not the URL's content."
                    ),
                    suggested_repair=(
                        "upsert_node({node_type: 'transform', plugin: 'web_scrape', "
                        "input: <source on_success>, options: {url_field: '<column>'}}) and route "
                        "the source on_success to it."
                    ),
                    evidence_locator={
                        "source": "blob",
                        "blob_id": str(blob_id),
                        "url_candidates": list(facts.url_candidates),
                    },
                )
            )

    # 4. Surface inspection warnings as info-severity diagnostics so the model
    #    sees them in the same array as blocking issues. These are *advisory*
    #    only — the model can ignore them if the operator's intent justifies.
    #
    #    Exception: ``csv_duplicate_headers`` is promoted to blocking. Duplicate
    #    headers cause silent column collapse in csv.DictReader (last-write-
    #    wins) and similar libraries, fabricating a single column from multiple
    #    source columns. That is a Tier-1 audit-integrity violation — the
    #    audit trail would silently contain data that "looks single-column"
    #    when the source had two — and must force the repair loop, not pass
    #    through as advisory. A genuine header row must be corrected and
    #    re-uploaded. Explicit unique ``columns`` are a valid alternative only
    #    when the repeated first row is data from a genuinely headerless file.
    for warning in facts.warnings:
        if warning.startswith("csv_duplicate_headers:"):
            # Do not relay the warning verbatim. ``facts`` comes from bounded
            # source bytes, and a malformed/headerless CSV can make its first
            # data row look like headers. Recompute only structural facts at
            # this final model-visible boundary so even a future inspection-
            # warning regression cannot expose raw header/cell values here.
            observed_headers = facts.observed_headers or ()
            header_counts = Counter(observed_headers)
            duplicate_values = {name for name, count in header_counts.items() if count > 1}
            duplicate_positions = [index for index, name in enumerate(observed_headers, start=1) if name in duplicate_values]
            diagnostics.append(
                _blocking_diagnostic(
                    code="csv_duplicate_headers",
                    message=(
                        f"CSV source has {len(duplicate_values)} duplicate header value "
                        f"class(es) across {len(duplicate_positions)} duplicate column "
                        f"position(s) out of {len(observed_headers)} total columns. Header "
                        "values are redacted because malformed or headerless CSV input can "
                        "make row content look like headers. Downstream consumers may "
                        "silently collapse these columns."
                    ),
                    suggested_repair=(
                        "For a genuine header row, correct the source file so every "
                        "physical header is unique, then re-upload and re-bind the "
                        "replacement blob. If the file is genuinely headerless and "
                        "the repeated first row is data rather than headers, declare "
                        "explicit unique `columns` in the source options, then re-run "
                        "preview_pipeline."
                    ),
                    evidence_locator={
                        "source": "blob",
                        "blob_id": str(blob_id),
                        "observed_header_count": len(observed_headers),
                        "duplicate_header_class_count": len(duplicate_values),
                        "duplicate_header_column_count": len(duplicate_positions),
                        "duplicate_header_positions": duplicate_positions,
                        "header_values_redacted": True,
                    },
                )
            )
            continue
        diagnostics.append(
            {
                "code": "source_inspection_warning",
                "severity": "info",
                "message": warning,
                "suggested_repair": None,
                "evidence_locator": {"source": "blob", "blob_id": str(blob_id)},
            }
        )

    return diagnostics


def _attribute_proof_diagnostic_to_source(
    diagnostic: Mapping[str, Any],
    *,
    source_name: str,
) -> Mapping[str, Any]:
    """Attach the authored source identity to one detector-owned diagnostic."""
    evidence = diagnostic["evidence_locator"]
    if not isinstance(evidence, Mapping):
        raise RuntimeError("proof diagnostic evidence_locator must be a mapping")
    return {
        **diagnostic,
        "evidence_locator": {**evidence, "source_name": source_name},
    }


def compute_proof_diagnostics(
    state: CompositionState,
    *,
    session_engine: Engine | None = None,
    session_id: str | None = None,
    blob_resolver: Callable[[str], ResolvedProofBlob | UnresolvedClaimedProofBlob | None] | None = None,
) -> list[Mapping[str, Any]]:
    """Inspect every blob-backed source within the authored-source proof cap.

    Each source gets an independent custody resolution and bounded 8 KiB
    inspection. Pipelines above the explicit cap block deterministically rather
    than silently marking a partially inspected topology as proof-passed.
    Composer callers supply ``session_engine``/``session_id``; authoritative
    execution supplies the BlobService-backed ``blob_resolver``.
    """
    if session_id is None:
        return []
    effective_resolver = blob_resolver
    if effective_resolver is None:
        if session_engine is None:
            return []

        def _resolve_from_composer_store(resolved_blob_id: str) -> ResolvedProofBlob | None:
            # Row + bytes observed as ONE version under the session custody
            # lock — an unlocked read racing update_blob's in-transaction
            # file swap would escalate a false BlobIntegrityError
            # (elspeth-3d1d1fcb6c).
            metadata, content = _locked_read_ready_blob(session_engine, session_id, resolved_blob_id)
            if metadata is None:
                return None
            if content is None:
                # Not ready, or storage file missing — metadata-only
                # resolution; downstream emits the matching diagnostic.
                return ResolvedProofBlob(metadata=metadata)
            stored_hash = metadata["content_hash"]
            if stored_hash is None:
                raise AuditIntegrityError("ready proof blob has null content_hash")
            return ResolvedProofBlob(
                metadata=metadata,
                verified_prefix=content[: 8 * 1024],
                verified_content_hash=stored_hash,
                total_size_bytes=len(content),
            )

        effective_resolver = _resolve_from_composer_store

    uncached_resolver = effective_resolver
    resolved_by_blob_id: dict[tuple[str, str], ResolvedProofBlob | UnresolvedClaimedProofBlob | None] = {}

    def _memoized_resolver(resolved_blob_id: str) -> ResolvedProofBlob | UnresolvedClaimedProofBlob | None:
        try:
            parsed_blob_id = UUID(resolved_blob_id)
        except ValueError:
            cache_key = ("raw", resolved_blob_id)
        else:
            canonical_blob_id = str(parsed_blob_id)
            cache_key = (
                "uuid" if canonical_blob_id == resolved_blob_id else "noncanonical",
                canonical_blob_id if canonical_blob_id == resolved_blob_id else resolved_blob_id,
            )
        if cache_key not in resolved_by_blob_id:
            resolved_by_blob_id[cache_key] = uncached_resolver(resolved_blob_id)
        return resolved_by_blob_id[cache_key]

    blob_sources: list[tuple[str, SourceSpec, object]] = []
    for source_name, source in state.sources.items():
        blob_id = source.options["blob_ref"] if "blob_ref" in source.options else None
        if blob_id is not None:
            blob_sources.append((source_name, source, blob_id))

    if len(blob_sources) > _MAX_PROOF_BLOB_SOURCES:
        return [
            _blocking_diagnostic(
                code="source_inspection_failed",
                message=(
                    f"Pipeline has {len(blob_sources)} blob-backed sources, exceeding the bounded proof "
                    f"limit of {_MAX_PROOF_BLOB_SOURCES}; no partial proof was admitted."
                ),
                suggested_repair=(
                    f"Reduce the pipeline to at most {_MAX_PROOF_BLOB_SOURCES} blob-backed sources or partition "
                    "it into separately validated pipelines, then re-run preview_pipeline."
                ),
                evidence_locator={
                    "source": "pipeline",
                    "blob_source_count": len(blob_sources),
                    "max_blob_sources": _MAX_PROOF_BLOB_SOURCES,
                },
            )
        ]

    diagnostics: list[Mapping[str, Any]] = []
    for source_name, source, blob_id in blob_sources:
        source_diagnostics = _compute_proof_diagnostics_for_source(
            state,
            source_name=source_name,
            source=source,
            blob_id=blob_id,
            blob_resolver=_memoized_resolver,
        )
        diagnostics.extend(_attribute_proof_diagnostic_to_source(diagnostic, source_name=source_name) for diagnostic in source_diagnostics)
    return diagnostics


def _runtime_preflight_not_run_error() -> ValidationEntryDict:
    """Build the marker naming an un-run Stage 2 in a preview's error channel.

    A fresh dict per call: the entry is handed to the caller inside
    ``data["errors"]``, so a shared module constant would let one consumer's
    mutation leak into every later preview.

    Deliberately NOT registered in ``_CLOSED_VALIDATION_ERROR_CODES`` — that
    tuple is a claim about the codes the planner's redacted repair feedback
    can carry, and this marker is not on that path. Its own message therefore
    has to carry the repair.
    """
    return ValidationEntryDict(
        component="pipeline",
        message=(
            "The runtime preflight stage did not run for this preview, so this pipeline has not been "
            "checked against the real engine at all (path allowlist, plugin instantiation, graph "
            "structure, schema compatibility, secret refs, policy gates). is_valid is false because no "
            "verdict on those checks exists, not because one of them failed. This is a server wiring "
            "omission, not a defect in the pipeline: do not rewrite the pipeline to try to clear it — "
            "report that preview_pipeline returned runtime_preflight_not_run."
        ),
        severity="high",
        error_code="runtime_preflight_not_run",
    )


def _execute_preview_pipeline(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Preview pipeline configuration — dry-run validation with source summary.

    Returns ``authoring_validation`` (Stage 1), ``runtime_preflight``
    (Stage 2 from the caller-supplied callback), and ``proof_diagnostics``
    (Stage 3 — operator-input-aware proof against the observed source
    blob). The presence of any blocking ``proof_diagnostics`` entry means
    ``is_valid=False`` even when authoring + runtime checks pass.

    An un-run stage never rides the success side: with no runtime callback
    wired, ``is_valid`` is false and ``errors`` carries an explicit
    ``runtime_preflight_not_run`` entry naming the stage that did not run.
    """
    validation = context.catalog.validate_composition_state(state).validation
    _AUTHORING_VALIDATION_COUNTER.add(
        1,
        {"outcome": "valid" if validation.is_valid else "invalid"},
    )
    authoring_payload = _authoring_validation_payload(state, validation)
    runtime_result = context.runtime_preflight(state) if context.runtime_preflight is not None else None

    proof_diagnostics = compute_proof_diagnostics(
        state,
        session_engine=context.session_engine,
        session_id=context.session_id,
    )
    has_blocking_proof = any(d["severity"] == "blocking" for d in proof_diagnostics)

    is_valid = validation.is_valid
    summary_errors = authoring_payload["errors"]
    if runtime_result is not None:
        is_valid = is_valid and runtime_result.is_valid
    else:
        # Both live callers wire Stage 2 unconditionally for preview — the
        # operator channel precomputes it per call (tool_batch.py) and the
        # stdio MCP server wires it at construction (d53fbee91) — so this
        # branch is a pure wiring-omission tripwire. An un-run stage must not
        # ride the success side of the conjunct: fail closed and name the
        # stage, because ``runtime_preflight: null`` alone cannot tell a
        # caller "not computed" apart from "nothing to report".
        is_valid = False
        # A fresh list, never an in-place append: this list object IS
        # ``authoring_payload["errors"]``, which the summary also embeds as
        # ``authoring_validation`` — Stage 1's own report must stay Stage-1-true.
        summary_errors = [*summary_errors, _runtime_preflight_not_run_error()]
    if has_blocking_proof:
        is_valid = False

    summary: dict[str, Any] = {
        "is_valid": is_valid,
        "errors": summary_errors,
        "warnings": authoring_payload["warnings"],
        "suggestions": authoring_payload["suggestions"],
        "edge_contracts": authoring_payload["edge_contracts"],
        "semantic_contracts": authoring_payload["semantic_contracts"],
        "graph_repair_suggestions": authoring_payload["graph_repair_suggestions"],
        "authoring_validation": authoring_payload,
        "runtime_preflight": runtime_result.model_dump() if runtime_result is not None else None,
        "proof_diagnostics": proof_diagnostics,
        "sources": {
            name: {
                "plugin": source.plugin,
                "on_success": source.on_success,
                "has_schema_config": _source_options_have_schema(source.options),
            }
            for name, source in state.sources.items()
        },
        "node_count": len(state.nodes),
        "output_count": len(state.outputs),
        "nodes": [{"id": n.id, "node_type": n.node_type, "plugin": n.plugin} for n in state.nodes],
        "outputs": [{"name": o.name, "plugin": o.plugin} for o in state.outputs],
    }

    return ToolResult(
        success=True,
        updated_state=state,
        validation=validation,
        affected_nodes=(),
        data=summary,
        runtime_preflight=runtime_result,
    )


_PREVIEW_PIPELINE_DECLARATION = ToolDeclaration(
    name="preview_pipeline",
    handler=_execute_preview_pipeline,
    kind=ToolKind.DISCOVERY,
    description="Preview the current pipeline configuration — returns "
    "validation status, source summary, and node/output overview "
    "without executing. Use this to confirm the pipeline is set up "
    "correctly before running.",
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    cacheable=False,
)


def _execute_diff_pipeline(
    args: dict[str, Any],
    state: CompositionState,
    context: ToolContext,
) -> ToolResult:
    """Compute a diff/change summary against a baseline state.

    The baseline is passed explicitly by the MCP server or web composer
    via ``context.baseline``. If no baseline is available, returns a
    notice instead.

    Pre-computed current-state validation is threaded through
    ``context.current_validation`` so this handler avoids redundant
    recomputation when the caller already has the live ValidationSummary
    in hand.
    """
    baseline = context.baseline
    current_validation = context.current_validation
    if baseline is None:
        return _discovery_result(
            state,
            {
                _DATA_ERROR_KEY: "No baseline available. Load or create a session first.",
                "current_version": state.version,
            },
        )

    baseline_validation = context.catalog.validate_composition_state(baseline).validation
    changes = diff_states(
        baseline,
        state,
        baseline_validation=baseline_validation,
        current_validation=current_validation,
    )
    return _discovery_result(state, changes)


_DIFF_PIPELINE_DECLARATION = ToolDeclaration(
    name="diff_pipeline",
    handler=_execute_diff_pipeline,
    kind=ToolKind.DISCOVERY,
    description="Show what changed since the session was loaded or created. "
    "Returns added, removed, and modified nodes/edges/outputs, "
    "plus warnings introduced or resolved.",
    json_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    cacheable=False,
)


TOOLS_IN_MODULE: tuple[ToolDeclaration, ...] = (
    _GET_PLUGIN_SCHEMA_DECLARATION,
    _GET_EXPRESSION_GRAMMAR_DECLARATION,
    _EXPLAIN_VALIDATION_ERROR_DECLARATION,
    _GET_PLUGIN_ASSISTANCE_DECLARATION,
    _GET_AUDIT_INFO_DECLARATION,
    _LIST_MODELS_DECLARATION,
    _PREVIEW_PIPELINE_DECLARATION,
    _DIFF_PIPELINE_DECLARATION,
)
"""Every tool declared in this module, in stable order.

``_dispatch.py`` aggregates this tuple alongside every other plane's
TOOLS_IN_MODULE to build the registered-tool universe."""
