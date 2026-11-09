"""Deterministic YAML generator -- CompositionState to ELSPETH pipeline YAML.

Pure function. Same CompositionState always produces byte-identical YAML.
Uses yaml.dump() with sort_keys=True for determinism.

Layer: L3 (application).

Trust model: state_dict comes from CompositionState.to_dict() — our own
serialization of our own frozen dataclasses. Always-present fields are read
directly. Fields to_dict() emits only when set fall in two groups: genuinely
optional ones (``fork_to``, ``trigger``, ``timeout_seconds``) are checked with
``in``, and ones the node_type makes mandatory (``condition``, ``routes``,
``branches``, ``policy``, ``merge``) are read through ``_require_node_key``,
which raises PipelineLoweringError naming the node and the field. Never use
.get() with a default — a fabricated value is a silently wrong pipeline.

Web-specific metadata keys (e.g., blob_ref for file provenance tracking)
are filtered from options before YAML generation. These are UI-layer
concerns that should not leak into engine configuration. Plugin configs
use Pydantic with extra="forbid" — unknown keys cause validation failure.

Public export/share/MCP views have one extra scrub. Blob identity and
``persist_directory`` custody carriers are recursive; source/sink storage keys
and ``bind_source`` apply only at their schema-defined locations. Arbitrary
plugin payloads (for example LLM ``lookup`` dictionaries) remain semantic data.
Runtime execution keeps private custody facts because the engine still needs
local paths after ownership checks pass.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final, Protocol, TypedDict, cast

import yaml

from elspeth.contracts.enums import OutputMode
from elspeth.contracts.errors import PipelineLoweringError
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.web.composer.state import COMPOSER_NODE_TYPES, CompositionState, queue_node_contract_error
from elspeth.web.interpretation_state import AUTHORING_METADATA_OPTION_KEYS
from elspeth.web.paths import (
    NESTED_LOCAL_PATH_OPTION_KEYS,
    SINK_LOCAL_PATH_OPTION_KEYS,
    SOURCE_LOCAL_PATH_OPTION_KEYS,
)

# Web-specific metadata keys that should NOT appear in engine YAML.
# These are UI-layer concerns for provenance tracking, not plugin config.
# Plugin configs use Pydantic with extra="forbid" — unknown keys cause errors.
_WEB_ONLY_OPTION_KEYS = frozenset({"blob_ref"}) | AUTHORING_METADATA_OPTION_KEYS
_PUBLIC_RECURSIVE_FORBIDDEN_OPTION_KEYS = _WEB_ONLY_OPTION_KEYS | frozenset(NESTED_LOCAL_PATH_OPTION_KEYS) | frozenset({"blob_id"})
_PUBLIC_STORAGE_OPTION_KEYS = frozenset(SOURCE_LOCAL_PATH_OPTION_KEYS) | frozenset(SINK_LOCAL_PATH_OPTION_KEYS)
_PUBLIC_CUSTODY_SUBTREE_KEYS = frozenset({"custody", "provider_config"})


class PublicCompositionDict(TypedDict):
    """Composition-state wire shape after recursive public projection."""

    version: int
    metadata: dict[str, str]
    sources: dict[str, dict[str, Any]]
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    outputs: list[dict[str, Any]]


@observation_boundary(
    tier=3,
    source="web-authored source options mapping (untrusted blob_ref value)",
    source_param="options",
    suppresses=("R1",),
    invariant="returns True only when blob_ref is present and non-null; absent keys yield False, never raise",
)
def _has_blob_binding(options: dict[str, Any]) -> bool:
    return options.get("blob_ref") is not None


@observation_boundary(
    tier=3,
    source="web-authored source options mapping (untrusted mode value)",
    source_param="options",
    suppresses=("R1",),
    invariant="returns True only for the exact 'bind_source' mode string; absent or mistyped mode yields False, never raises",
)
def _has_bind_source_mode(options: dict[str, Any]) -> bool:
    return options.get("mode") == "bind_source"


def _require_node_key(node: dict[str, Any], key: str, node_kind: str) -> Any:
    """Read a node field that ``node_type`` makes mandatory, or refuse to lower.

    The message repeats Stage 1's phrasing verbatim so the composer repair hints
    (tools/generation.py) match it and the authoring LLM gets the same guidance
    whichever layer caught the defect.
    """
    if key not in node:
        raise PipelineLoweringError(f"{node_kind} '{node['id']}' is missing required field '{key}'.")
    return node[key]


def _strip_web_metadata(options: dict[str, Any], *, omit_source_paths: bool = False) -> dict[str, Any]:
    """Remove web-specific metadata keys from options dict.

    Returns a shallow copy with web-only keys removed.
    """
    stripped = {k: v for k, v in options.items() if k not in _WEB_ONLY_OPTION_KEYS}
    if _has_blob_binding(options) and _has_bind_source_mode(options):
        # The guard above proves ``mode`` is present in ``options``; ``mode`` is
        # not in _WEB_ONLY_OPTION_KEYS, so it survives into ``stripped`` — pop
        # it directly without a default (a missing key here would be a bug).
        stripped.pop("mode")
    if omit_source_paths:
        for key in SOURCE_LOCAL_PATH_OPTION_KEYS:
            stripped.pop(key, None)
    return stripped


def _strip_profile_lowering_provenance(plugin: str, options: dict[str, Any]) -> dict[str, Any]:
    """Drop server-authored prompt provenance from profile-selecting llm nodes.

    ``approved_prompt_artifact_hash`` is in ``LLM_PROFILE_PRIVATE_FIELDS``, so
    the batch/CLI loader's profile-lowering pass rejects any llm component
    that both selects a ``profile`` and carries the hash
    (``ValueError('private_profile_option')``) — exported YAML could never be
    re-loaded (elspeth-b73666ac82). The web resolver strips exactly this key at
    its own lowering seam (``_PROFILE_LOWERING_METADATA_OPTION_KEYS``); the
    export mirrors that, at the same two component kinds the loader lowers
    (sources and transforms). Plain provider-config llm nodes keep the hash:
    there it is a declared, drift-validated ``LLMConfig`` field and the
    Landscape<->session-DB audit join anchor. For a re-loaded profile export,
    prompt provenance lives in the audit trail, not in plugin options
    (operator decision, 2026-08-09).
    """
    if plugin == "llm" and "profile" in options and "approved_prompt_artifact_hash" in options:
        del options["approved_prompt_artifact_hash"]
    return options


def _source_entry(source: dict[str, Any], *, omit_source_paths: bool) -> dict[str, Any]:
    """Convert a serialized SourceSpec dict into runtime YAML shape."""
    source_options = _strip_profile_lowering_provenance(
        source["plugin"],
        _strip_web_metadata(
            dict(source["options"]),
            omit_source_paths=omit_source_paths,
        ),
    )
    source_options["on_validation_failure"] = source["on_validation_failure"]
    return {
        "plugin": source["plugin"],
        "on_success": source["on_success"],
        "options": source_options,
    }


class LoweredPipelineDocument(TypedDict, total=False):
    """The runtime settings document the composer lowers to — the closed set
    of top-level sections, in the order the generator emits them. Each entry
    is a plugin-shaped mapping whose keys the per-kind lowering owns."""

    sources: dict[str, dict[str, Any]]
    queues: dict[str, dict[str, Any]]
    transforms: list[dict[str, Any]]
    gates: list[dict[str, Any]]
    row_unions: list[dict[str, Any]]
    aggregations: list[dict[str, Any]]
    coalesce: list[dict[str, Any]]
    collectors: list[dict[str, Any]]
    scopes: list[dict[str, Any]]
    sinks: dict[str, dict[str, Any]]


class _NodeKindLowering(Protocol):
    def __call__(self, doc: LoweredPipelineDocument, *, state: CompositionState, state_dict: PublicCompositionDict) -> None: ...


def _lower_queue_nodes(doc: LoweredPipelineDocument, *, state: CompositionState, state_dict: PublicCompositionDict) -> None:
    # Queues — structural pass-through fan-in points (elspeth-a5b86149d4).
    # Emitted after sources and before executable node lists so the YAML reads
    # source -> queues -> transforms -> ... Queue nodes are in COMPOSER_NODE_TYPES
    # but belong to none of the executable node lists below, so without this
    # block a queue node would be silently dropped from the export. Defend the
    # canonical shape here via the single source of truth rather than trusting
    # internal state blindly.
    queues = [node for node in state.nodes if node.node_type == "queue"]
    if queues:
        queues_doc: dict[str, Any] = {}
        for queue in queues:
            contract_error = queue_node_contract_error(queue)
            if contract_error is not None:
                raise PipelineLoweringError(contract_error)
            queue_entry: dict[str, Any] = {}
            description = queue.options["description"] if "description" in queue.options else None
            if type(description) is str:
                queue_entry["description"] = description
            queues_doc[queue.id] = queue_entry
        doc["queues"] = queues_doc


def _lower_transform_nodes(doc: LoweredPipelineDocument, *, state: CompositionState, state_dict: PublicCompositionDict) -> None:
    # Transforms — filter nodes by type, access always-present fields directly.
    transforms = [n for n in state_dict["nodes"] if n["node_type"] == "transform"]
    if transforms:
        doc["transforms"] = []
        for t in transforms:
            if t["on_error"] is None:
                raise PipelineLoweringError(
                    f"Transform '{t['id']}' has on_error=None — "
                    f"upsert_node must default this at the mutation boundary, "
                    f"not leave it for the YAML generator to fabricate"
                )
            entry: dict[str, Any] = {
                "name": t["id"],
                "plugin": t["plugin"],
                "input": t["input"],
                "on_success": t["on_success"],
                "on_error": t["on_error"],
            }
            if t["options"]:
                entry["options"] = _strip_profile_lowering_provenance(t["plugin"], _strip_web_metadata(dict(t["options"])))
            doc["transforms"].append(entry)


def _lower_gate_nodes(doc: LoweredPipelineDocument, *, state: CompositionState, state_dict: PublicCompositionDict) -> None:
    # Gates — condition and routes are conditionally present (only on gates).
    # to_dict() emits them when not None, so a Stage-1-invalid state can reach
    # here with either absent; the guarded accessor refuses to lower it.
    gates = [n for n in state_dict["nodes"] if n["node_type"] == "gate"]
    if gates:
        doc["gates"] = []
        for g in gates:
            entry = {
                "name": g["id"],
                "input": g["input"],
                "condition": _require_node_key(g, "condition", "Gate"),
                "routes": _require_node_key(g, "routes", "Gate"),
            }
            if g["on_error"] is not None:
                entry["on_error"] = g["on_error"]
            # fork_to is conditionally present — only on fork gates
            if "fork_to" in g:
                entry["fork_to"] = g["fork_to"]
            doc["gates"].append(entry)


def _lower_row_union_nodes(doc: LoweredPipelineDocument, *, state: CompositionState, state_dict: PublicCompositionDict) -> None:
    # Row unions — structural N-to-N barriers. ``input`` is a Composer-only
    # placeholder derived from the first branch connection; runtime consumes
    # only the ordered branches mapping.
    row_unions = [n for n in state_dict["nodes"] if n["node_type"] == "row_union"]
    if row_unions:
        doc["row_unions"] = []
        for row_union in row_unions:
            entry = {
                "name": row_union["id"],
                "branches": _require_node_key(row_union, "branches", "row_union"),
                "on_success": row_union["on_success"],
            }
            if "timeout_seconds" in row_union:
                entry["timeout_seconds"] = row_union["timeout_seconds"]
            doc["row_unions"].append(entry)


def _lower_aggregation_nodes(doc: LoweredPipelineDocument, *, state: CompositionState, state_dict: PublicCompositionDict) -> None:
    # Aggregations
    for node in state.nodes:
        if node.node_type == "aggregation" and (node.output_mode is None or node.output_mode in OutputMode):
            mode = OutputMode.TRANSFORM if node.output_mode is None else OutputMode(node.output_mode)
            count_error = mode.expected_output_count_error(node.expected_output_count)
            if count_error is not None:
                raise PipelineLoweringError(f"Aggregation '{node.id}': {count_error}")
    aggregations = [n for n in state_dict["nodes"] if n["node_type"] == "aggregation"]
    if aggregations:
        doc["aggregations"] = []
        for a in aggregations:
            if a["on_error"] is None:
                raise PipelineLoweringError(
                    f"Aggregation '{a['id']}' has on_error=None — "
                    f"upsert_node must default this at the mutation boundary, "
                    f"not leave it for the YAML generator to fabricate"
                )
            entry = {
                "name": a["id"],
                "plugin": a["plugin"],
                "input": a["input"],
                "on_success": a["on_success"],
                "on_error": a["on_error"],
            }
            # trigger, output_mode, expected_output_count are conditionally
            # emitted by to_dict() (only when non-None).  Use "in" checks to
            # match the to_dict() contract — a missing key is not an error
            # here; the engine treats absence as end-of-source-only flush.
            if "trigger" in a:
                entry["trigger"] = a["trigger"]
            if "output_mode" in a:
                entry["output_mode"] = a["output_mode"]
            if "expected_output_count" in a:
                entry["expected_output_count"] = a["expected_output_count"]
            if a["options"]:
                entry["options"] = _strip_web_metadata(dict(a["options"]))
            doc["aggregations"].append(entry)


def _lower_coalesce_nodes(doc: LoweredPipelineDocument, *, state: CompositionState, state_dict: PublicCompositionDict) -> None:
    # Coalesce — branches, policy, merge are conditionally present. Where the
    # runtime has a default, NodeSpec.__post_init__ already records it, so an
    # absence here proves a state_dict that never crossed that boundary. Raise
    # rather than default a second time: a second default site is exactly the
    # drift normalising at one construction boundary was meant to close.
    coalesces = [n for n in state_dict["nodes"] if n["node_type"] == "coalesce"]
    if coalesces:
        doc["coalesce"] = []
        for c in coalesces:
            if c["options"]:
                raise PipelineLoweringError(f"Coalesce '{c['id']}' does not accept options; remove them before runtime lowering.")
            entry = {
                "name": c["id"],
                "branches": _require_node_key(c, "branches", "Coalesce"),
                "policy": _require_node_key(c, "policy", "Coalesce"),
                "merge": _require_node_key(c, "merge", "Coalesce"),
            }
            if c["on_success"] is not None:
                entry["on_success"] = c["on_success"]
            if "timeout_seconds" in c:
                entry["timeout_seconds"] = c["timeout_seconds"]
            doc["coalesce"].append(entry)


def _lower_collector_nodes(doc: LoweredPipelineDocument, *, state: CompositionState, state_dict: PublicCompositionDict) -> None:
    # Collectors — EXPAND-group closers (barrier-scopes spec §3). Each
    # collector NodeSpec lowers to ONE collectors: entry plus ONE scopes:
    # entry derived from its scope binding fields; the scope's closer is the
    # collector's own name by construction, so a composer export can never
    # produce a dangling closer reference. scope_policy is REQUIRED with no
    # default and scope_name/scope_opener are mandatory for the node type, so
    # absences refuse to lower rather than fabricate (never .get with a
    # default).
    collectors = [n for n in state_dict["nodes"] if n["node_type"] == "collector"]
    if collectors:
        doc["collectors"] = []
        doc["scopes"] = []
        for c in collectors:
            if c["plugin"] is None:
                raise PipelineLoweringError(
                    f"Collector '{c['id']}' has plugin=None — collectors reuse the batch-transform "
                    f"plugin contract and cannot lower without one"
                )
            if c["on_success"] is None:
                raise PipelineLoweringError(f"Collector '{c['id']}' has on_success=None — a collector requires a flush destination")
            if c["on_error"] is not None:
                raise PipelineLoweringError(
                    f"Collector '{c['id']}' does not accept on_error — collector failures are "
                    "whole-group verdicts settled through scope policy and nesting"
                )
            entry = {
                "name": c["id"],
                "plugin": c["plugin"],
                "input": c["input"],
                "on_success": c["on_success"],
            }
            if c["options"]:
                entry["options"] = _strip_web_metadata(dict(c["options"]))
            doc["collectors"].append(entry)
            doc["scopes"].append(
                {
                    "name": _require_node_key(c, "scope_name", "Collector"),
                    "opener": _require_node_key(c, "scope_opener", "Collector"),
                    "closer": c["id"],
                    "policy": _require_node_key(c, "scope_policy", "Collector"),
                }
            )


# THE lowering table: one entry per composer node kind, in YAML section order
# (queues → transforms → gates → row_unions → aggregations → coalesce →
# collectors + scopes). Its KEYS are what the lowering drift guard in
# ``_generate_pipeline_dict`` checks against ``COMPOSER_NODE_TYPES`` (derived
# from the ``NodeType`` Literal). This operand is derived from the lowering
# CODE — a kind is "lowered" exactly when it has an entry here — never from
# ``NodeType``: a guard with both operands derived from the same authority is
# ``x != x`` (elspeth-b3117ec3ac comment 7980). So the guard now compares what
# the vocabulary declares against what lowering implements, in both
# directions: a kind added to the Literal without a lowering is refused
# (missing), and a lowering left behind by a removed kind is refused
# (obsolete) — the silent direction elspeth-11d8cb0908 recorded is closed.
_NODE_KIND_LOWERINGS: Final[Mapping[str, _NodeKindLowering]] = MappingProxyType(
    {
        "queue": _lower_queue_nodes,
        "transform": _lower_transform_nodes,
        "gate": _lower_gate_nodes,
        "row_union": _lower_row_union_nodes,
        "aggregation": _lower_aggregation_nodes,
        "coalesce": _lower_coalesce_nodes,
        "collector": _lower_collector_nodes,
    }
)


def _lowered_node_types() -> frozenset[str]:
    """The node kinds the lowering table implements — read at call time so a
    mutated table is what the guard sees."""

    return frozenset(_NODE_KIND_LOWERINGS)


def _generate_pipeline_dict(
    state: CompositionState,
    *,
    omit_source_paths: bool,
    state_dict: PublicCompositionDict | None = None,
) -> LoweredPipelineDocument:
    """Convert a CompositionState to ELSPETH's canonical pipeline dict.

    Maps CompositionState fields to the YAML structure expected by
    ELSPETH's load_settings() parser. This is the canonical analysis form
    for code that needs to walk a composition state using runtime/YAML
    section names without serializing to text first.

    Calls state.to_dict() to unwrap all frozen containers
    (MappingProxyType -> dict, tuple -> list) before building the dict.

    Args:
        state: The pipeline composition state to convert.

    Returns:
        Plain dict representing the pipeline configuration.
    """
    # Unwrap frozen containers to plain Python types (R4).
    # to_dict() recursively converts MappingProxyType -> dict,
    # tuple -> list. Without this, yaml.dump() raises RepresenterError.
    state_dict = cast(PublicCompositionDict, state.to_dict()) if state_dict is None else state_dict

    lowered = _lowered_node_types()
    if lowered != COMPOSER_NODE_TYPES:
        missing = sorted(COMPOSER_NODE_TYPES - lowered)
        obsolete = sorted(lowered - COMPOSER_NODE_TYPES)
        raise RuntimeError(f"Composer node type lowering drift: missing YAML lowering for {missing}; obsolete YAML lowering for {obsolete}")

    doc: LoweredPipelineDocument = {}

    for node in state_dict["nodes"]:
        node_type = node["node_type"]
        if node_type not in COMPOSER_NODE_TYPES:
            raise PipelineLoweringError(f"Unknown node_type '{node_type}' for node '{node['id']}'.")

    sources = state_dict["sources"]
    if sources:
        doc["sources"] = {name: _source_entry(source, omit_source_paths=omit_source_paths) for name, source in sources.items()}

    # Every composer node kind lowers through the table, in its order.
    for lowering in _NODE_KIND_LOWERINGS.values():
        lowering(doc, state=state, state_dict=state_dict)

    # Sinks — always-present fields, direct access.
    if state_dict["outputs"]:
        doc["sinks"] = {}
        for output in state_dict["outputs"]:
            sink_entry: dict[str, Any] = {
                "plugin": output["plugin"],
                "on_write_failure": output["on_write_failure"],
            }
            if output["options"]:
                sink_entry["options"] = _strip_web_metadata(dict(output["options"]))
            doc["sinks"][output["name"]] = sink_entry

    # landscape key is intentionally omitted -- URL comes from
    # WebSettings.get_landscape_url() at execution time (security fix S1).
    return doc


def generate_pipeline_dict(state: CompositionState) -> LoweredPipelineDocument:
    """Convert a CompositionState to the runtime pipeline dict."""
    return _generate_pipeline_dict(state, omit_source_paths=False)


@observation_boundary(
    tier=3,
    source="an arbitrary-depth value nested inside a source/node/output options block — planner/LLM-authored "
    "plugin option content reached through the composer tool loop, of no proven shape at any depth",
    source_param="value",
    suppresses=("R5",),
    invariant="returns the projected mapping/list or the value unchanged; a scalar, a string and any "
    "unrecognised shape are pure passthrough, never coerced and never dropped — never raises",
)
def _recursive_public_option_projection(
    value: Any,
    *,
    strip_storage_here: bool = False,
    custody_subtree: bool = False,
    strip_bind_source_mode: bool = False,
) -> Any:
    """Project options without treating arbitrary plugin data as custody."""
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for key, nested in value.items():
            if key in _PUBLIC_RECURSIVE_FORBIDDEN_OPTION_KEYS or (custody_subtree and key.endswith("_blob_id")):
                continue
            if (strip_storage_here or custody_subtree) and key in _PUBLIC_STORAGE_OPTION_KEYS:
                continue
            if strip_bind_source_mode and key == "mode" and nested == "bind_source":
                continue
            child_custody_subtree = custody_subtree or key in _PUBLIC_CUSTODY_SUBTREE_KEYS
            projected[key] = _recursive_public_option_projection(
                nested,
                custody_subtree=child_custody_subtree,
                strip_bind_source_mode=False,
            )
        return projected
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _recursive_public_option_projection(
                item,
                strip_storage_here=strip_storage_here,
                custody_subtree=custody_subtree,
                strip_bind_source_mode=strip_bind_source_mode,
            )
            for item in value
        ]
    return value


def generate_public_composition_dict(state: CompositionState) -> PublicCompositionDict:
    """Return the single recursive public projection of composer state.

    The shape remains ``CompositionState.to_dict()`` compatible so graph
    consumers retain source/node/edge/output structure. Only public facts
    survive: source/sink path carriers, nested transform persistence paths,
    blob identifiers, bind-source markers, and recursively nested authoring
    metadata are excluded.
    """
    projected = cast(PublicCompositionDict, state.to_dict())

    for source in projected["sources"].values():
        source["options"] = _recursive_public_option_projection(
            source["options"],
            strip_storage_here=True,
            strip_bind_source_mode=True,
        )
    for node in projected["nodes"]:
        node["options"] = _recursive_public_option_projection(node["options"])
    for output in projected["outputs"]:
        output["options"] = _recursive_public_option_projection(
            output["options"],
            strip_storage_here=True,
        )
    return projected


def generate_public_pipeline_dict(state: CompositionState) -> LoweredPipelineDocument:
    """Convert a CompositionState to public export/share/MCP pipeline dict."""
    public_state = generate_public_composition_dict(state)
    return _generate_pipeline_dict(
        state,
        omit_source_paths=True,
        state_dict=public_state,
    )


def generate_yaml(state: CompositionState) -> str:
    """Convert a CompositionState to deterministic ELSPETH pipeline YAML.

    The output is deterministic: same state produces byte-identical YAML.
    YAML serialization is a thin wrapper around ``generate_pipeline_dict()``
    so there is only one mapping from composer state to runtime/YAML shape.

    Args:
        state: The pipeline composition state to serialize.

    Returns:
        YAML string representing the pipeline configuration.
    """
    doc = generate_pipeline_dict(state)

    # sort_keys=False preserves insertion order: sources → queues → transforms
    # → gates → row_unions → aggregations → coalesce → collectors → scopes →
    # sinks.
    return yaml.dump(doc, default_flow_style=False, sort_keys=False)


class PublicExportRedaction(TypedDict):
    """What the public projection stripped, per component (top-level options).

    Keys are component names; values are the sorted top-level option keys the
    projection removed (storage path carriers and blob linkage). Components
    that lost nothing are absent, so an empty mapping pair means the export
    is complete as-is.
    """

    sources: dict[str, list[str]]
    outputs: dict[str, list[str]]


# Exported marker bytes: the YAML import guard
# (`web/sessions/routes/composer/state.py`) recognises these exact prefixes,
# so exporter and importer share one authority. Do not re-derive them.
PUBLIC_EXPORT_REDACTION_HEADER = "# ELSPETH public export — custody-redacted; NOT runnable as exported."
PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX = "# redacted-source: "
PUBLIC_EXPORT_REDACTED_OUTPUT_MARKER_PREFIX = "# redacted-output: "
PUBLIC_EXPORT_REBIND_GUIDANCE = (
    "Re-bind each redacted source on import by uploading its data to the target session and passing "
    'source_blob_ids={"<source name>": "<session blob id>"}; sink paths are re-authored on import.'
)

_PUBLIC_SOURCE_LINKAGE_KEYS = frozenset({"blob_ref", "blob_id"})


def public_export_redaction(state: CompositionState) -> PublicExportRedaction:
    """Account for what :func:`generate_public_yaml` strips from ``state``.

    Derived from the same key-set authorities the projection itself uses
    (``_PUBLIC_STORAGE_OPTION_KEYS`` and the blob-linkage keys), over the same
    export state, so the account cannot drift from the
    scrub (elspeth-06f92da0d9). Top-level option keys only: that is where the
    schema-defined source/sink storage carriers live; recursive scrubs of
    nested custody subtrees stay undocumented here because they carry no
    re-bindable user data.
    """
    sources: dict[str, list[str]] = {}
    for source_name, source in state.sources.items():
        stripped = sorted(key for key in source.options if key in _PUBLIC_STORAGE_OPTION_KEYS or key in _PUBLIC_SOURCE_LINKAGE_KEYS)
        if stripped:
            sources[source_name] = stripped
    outputs: dict[str, list[str]] = {}
    for output in state.outputs:
        stripped = sorted(key for key in output.options if key in _PUBLIC_STORAGE_OPTION_KEYS or key == "blob_id")
        if stripped:
            outputs[output.name] = stripped
    return {"sources": sources, "outputs": outputs}


def sources_reading_uploaded_blobs(state: CompositionState) -> tuple[str, ...]:
    """Name sources with live upload bindings in the same state used for export."""
    return tuple(
        source_name
        for source_name, source in state.sources.items()
        if any(key in source.options and source.options[key] is not None for key in _PUBLIC_SOURCE_LINKAGE_KEYS)
    )


# The marker prose uses category labels, not raw option-key names: the
# custody-egress guards over sibling consumers assert that the literal key
# tokens never appear anywhere in a serialised public artifact, and an
# explanatory comment must not weaken those greps. Exact key names travel on
# the structured route response (`StateYamlRedaction`) instead, where they are
# data rather than prose.
#
# The keys are the union of the projection's own storage-key authorities and
# the blob-linkage keys; `test_marker_labels_cover_every_stripped_key` pins
# that coverage, so a new path option fails CI instead of raising KeyError
# inside a user's export.
_MARKER_LABELS = {
    "path": "local-path",
    "file": "local-path",
    "persist_directory": "persist-directory",
    "blob_ref": "blob-linkage",
    "blob_id": "blob-linkage",
}


def _marker_labels(keys: list[str]) -> str:
    return ",".join(sorted({_MARKER_LABELS[key] for key in keys}))


def public_export_redaction_header(state: CompositionState) -> str:
    """Marker block for a public export that leaves ELSPETH as a document.

    Returns ``""`` when the projection stripped nothing. Apply this ONLY at
    the user-download boundary (``GET /{session_id}/state/yaml``) and never
    inside :func:`generate_public_yaml` — see that function for why the other
    consumers keep bare bytes. Deterministic for a given state: the account is
    derived from the same state the body is.
    """
    redaction = public_export_redaction(state)
    if not redaction["sources"] and not redaction["outputs"]:
        return ""
    lines = [PUBLIC_EXPORT_REDACTION_HEADER]
    for source_name, keys in redaction["sources"].items():
        lines.append(f"{PUBLIC_EXPORT_REDACTED_SOURCE_MARKER_PREFIX}{source_name} stripped={_marker_labels(keys)}")
    for output_name, keys in redaction["outputs"].items():
        lines.append(f"{PUBLIC_EXPORT_REDACTED_OUTPUT_MARKER_PREFIX}{output_name} stripped={_marker_labels(keys)}")
    lines.append(f"# {PUBLIC_EXPORT_REBIND_GUIDANCE}")
    return "\n".join(lines) + "\n"


def generate_public_yaml(state: CompositionState) -> str:
    """Convert a CompositionState to deterministic public export/share/MCP YAML.

    Returns the projected document and nothing else. The custody-redaction
    marker block (:func:`public_export_redaction_header`) is deliberately NOT
    applied here: this function serves four consumers with different
    contracts, and only one of them hands a user a document to keep
    (elspeth-06f92da0d9).

    * The MCP ``generate_yaml`` tool and the shareable-review snapshot each
      carry a structured composition beside the text, and both are covered by
      custody-egress guards asserting no literal blob-linkage key token
      appears anywhere in the serialised artifact — prose naming the
      ``source_blob_ids`` re-bind field trips them.
    * The share snapshot is content-addressed, so these bytes are an identity.
    * ``web/_aws_ecs_acceptance/capture.py`` feeds this output straight back
      into ``POST /state/yaml``, which makes the generator an import producer
      as well as an export producer; a marker here would be an instruction the
      importer then re-parses.

    The export route composes header + body. Every other consumer keeps the
    bare document.
    """
    doc = generate_public_pipeline_dict(state)
    return yaml.dump(doc, default_flow_style=False, sort_keys=False)


def public_projection_digest(state: CompositionState) -> str:
    """Return the content address of the exact UTF-8 public YAML projection."""
    return hashlib.sha256(generate_public_yaml(state).encode("utf-8")).hexdigest()
