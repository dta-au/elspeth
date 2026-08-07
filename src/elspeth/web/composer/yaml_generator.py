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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, TypedDict, cast

import yaml

from elspeth.contracts.errors import AuditIntegrityError, PipelineLoweringError
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.web.composer.guided.state_machine import TerminalKind
from elspeth.web.composer.guided_blob_refs import (
    GuidedReviewedBlobBinding,
    validate_guided_reviewed_blob_binding,
    validate_guided_reviewed_blob_source_mapping,
    validate_guided_reviewed_sentinel_source_mapping,
)
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
_YAML_LOWERED_NODE_TYPES = frozenset({"aggregation", "coalesce", "gate", "queue", "row_union", "transform"})


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


def _source_entry(source: dict[str, Any], *, omit_source_paths: bool) -> dict[str, Any]:
    """Convert a serialized SourceSpec dict into runtime YAML shape."""
    source_options = _strip_web_metadata(
        dict(source["options"]),
        omit_source_paths=omit_source_paths,
    )
    source_options["on_validation_failure"] = source["on_validation_failure"]
    return {
        "plugin": source["plugin"],
        "on_success": source["on_success"],
        "options": source_options,
    }


def _generate_pipeline_dict(
    state: CompositionState,
    *,
    omit_source_paths: bool,
    state_dict: PublicCompositionDict | None = None,
) -> dict[str, Any]:
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

    if COMPOSER_NODE_TYPES != _YAML_LOWERED_NODE_TYPES:
        missing = sorted(COMPOSER_NODE_TYPES - _YAML_LOWERED_NODE_TYPES)
        obsolete = sorted(_YAML_LOWERED_NODE_TYPES - COMPOSER_NODE_TYPES)
        raise RuntimeError(f"Composer node type lowering drift: missing YAML lowering for {missing}; obsolete YAML lowering for {obsolete}")

    doc: dict[str, Any] = {}

    for node in state_dict["nodes"]:
        node_type = node["node_type"]
        if node_type not in COMPOSER_NODE_TYPES:
            raise PipelineLoweringError(f"Unknown node_type '{node_type}' for node '{node['id']}'.")

    sources = state_dict["sources"]
    if sources:
        doc["sources"] = {name: _source_entry(source, omit_source_paths=omit_source_paths) for name, source in sources.items()}

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
            description = queue.options.get("description")
            if isinstance(description, str):
                queue_entry["description"] = description
            queues_doc[queue.id] = queue_entry
        doc["queues"] = queues_doc

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
                entry["options"] = _strip_web_metadata(dict(t["options"]))
            doc["transforms"].append(entry)

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

    # Aggregations
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

    # Coalesce — branches, policy, merge are conditionally present. Where the
    # runtime has a default, NodeSpec.__post_init__ already records it, so an
    # absence here proves a state_dict that never crossed that boundary. Raise
    # rather than default a second time: a second default site is exactly the
    # drift normalising at one construction boundary was meant to close.
    coalesces = [n for n in state_dict["nodes"] if n["node_type"] == "coalesce"]
    if coalesces:
        doc["coalesce"] = []
        for c in coalesces:
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


def generate_pipeline_dict(state: CompositionState) -> dict[str, Any]:
    """Convert a CompositionState to the runtime pipeline dict."""
    return _generate_pipeline_dict(state, omit_source_paths=False)


def reattach_guided_blob_refs_for_public_export(state: CompositionState) -> CompositionState:
    """Reconstitute guided blob refs before public YAML generation.

    Guided mode can commit sources with only their storage ``path`` while each
    authoritative ``blob_ref`` survives in schema-8 ``reviewed_sources``.
    Public YAML stripping keys off ``blob_ref``, so reattach each binding to a
    working copy. Private reviewed paths must exactly match the live source;
    public ``blob:<uuid>`` sentinels must match the retained ref and source name,
    after which the HTTP export boundary verifies live blob custody and the exact
    private storage path before returning the sidecar.

    An ``exited_to_freeform`` terminal retains guided history for audit and
    possible re-entry, but it is no longer current source authority. Completed
    guided sessions remain authoritative until an explicit exit records that
    lifecycle boundary.

    Direction note (elspeth-3b45cdb41e): this identity return is correct for
    EXPORT consumers only — exported YAML must not shadow a replaced freeform
    source with stale guided history. The run-admission proof needs the
    opposite failure direction; it must use
    :func:`derive_guided_blob_refs_for_admission_proof`, never this function.
    """
    guided = state.guided_session
    if guided is None or not guided.reviewed_sources:
        return state
    if guided.terminal is not None and guided.terminal.kind is TerminalKind.EXITED_TO_FREEFORM:
        return state
    return _reattach_guided_reviewed_blob_bindings(state)


@dataclass(frozen=True, slots=True)
class AdmissionProofDerivation:
    """Admission-direction proof-state derivation outcome.

    ``custody_unavailable=True`` means retained guided review custody exists
    but could not be bound to the live sources; the admission consumer must
    record a FAILED (blocking) proof check — never a pass without a proof run.
    ``proof_state`` is then the untouched input state, provided only so the
    caller has a coherent state object; it carries no derived custody.
    """

    proof_state: CompositionState
    custody_unavailable: bool


def derive_guided_blob_refs_for_admission_proof(state: CompositionState) -> AdmissionProofDerivation:
    """Derive the source-proof state for run admission, failing closed.

    Export and admission consume the same reviewed-source history with
    OPPOSITE safety directions (epic elspeth-c1b8b26d32).
    :func:`reattach_guided_blob_refs_for_public_export` keeps its
    ``EXITED_TO_FREEFORM`` identity return — exited history is no longer
    authoring authority and must not shadow a replaced freeform source in an
    export. The admission proof must NOT inherit that skip: for admission the
    retained review history is still proof custody, and skipping it recorded a
    fabricated passing ``proof_diagnostics`` check for exactly the pipeline
    guided confirmation had just blocked (elspeth-3b45cdb41e).

    Non-exited states derive byte-identically to the export path, including
    letting :class:`AuditIntegrityError` propagate. Exited states run the same
    strict binding; when the diverged freeform state can no longer bind the
    retained custody (renamed/removed sources, re-pointed carriers, or a
    conflicting live ``blob_ref``), the derivation reports
    ``custody_unavailable=True`` so admission blocks instead of admitting an
    unproven source.

    Never mutates ``state``; the derived custody exists only for the proof
    computation and is not persisted.
    """
    guided = state.guided_session
    if guided is None or not guided.reviewed_sources:
        return AdmissionProofDerivation(proof_state=state, custody_unavailable=False)
    if guided.terminal is not None and guided.terminal.kind is TerminalKind.EXITED_TO_FREEFORM:
        try:
            derived = _reattach_guided_reviewed_blob_bindings(state)
        except AuditIntegrityError:
            return AdmissionProofDerivation(proof_state=state, custody_unavailable=True)
        return AdmissionProofDerivation(proof_state=derived, custody_unavailable=False)
    return AdmissionProofDerivation(
        proof_state=_reattach_guided_reviewed_blob_bindings(state),
        custody_unavailable=False,
    )


def _reattach_guided_reviewed_blob_bindings(state: CompositionState) -> CompositionState:
    """Bind retained reviewed-source custody onto a working copy of ``state``.

    Shared binding body for both directions above; callers own the terminal
    gating. Raises :class:`AuditIntegrityError` when the retained history and
    the live sources disagree.
    """
    guided = state.guided_session
    assert guided is not None  # callers gate on a populated guided session

    reviewed_bindings: list[tuple[str, frozenset[str], str]] = []
    sentinel_bindings: list[tuple[str, GuidedReviewedBlobBinding]] = []
    reviewed_names: set[str] = set()
    for snapshot in guided.reviewed_sources.values():
        source_name = snapshot.name
        if source_name in reviewed_names:
            raise AuditIntegrityError("guided reviewed source names must be unique")
        reviewed_names.add(source_name)
        binding = validate_guided_reviewed_blob_binding(snapshot.options)
        if binding is None:
            continue
        if binding.is_sentinel:
            sentinel_bindings.append((source_name, binding))
            continue
        reviewed_bindings.append((source_name, binding.paths, binding.blob_ref))

    if not reviewed_bindings and not sentinel_bindings:
        return state
    validate_guided_reviewed_blob_source_mapping(
        [(name, paths) for name, paths, _blob_ref in reviewed_bindings],
        {name: source.options for name, source in state.sources.items()},
    )
    all_reviewed_paths = frozenset(path for _name, paths, _blob_ref in reviewed_bindings for path in paths)
    reattached = dict(state.sources)
    changed = False
    for source_name, source in state.sources.items():
        live_reviewed_paths = {
            value for key in SOURCE_LOCAL_PATH_OPTION_KEYS if type(value := source.options.get(key)) is str and value in all_reviewed_paths
        }
        if not live_reviewed_paths:
            continue
        candidates = [
            (paths, blob_ref)
            for reviewed_name, paths, blob_ref in reviewed_bindings
            if reviewed_name == source_name and live_reviewed_paths <= paths
        ]
        if len(candidates) != 1:
            raise AuditIntegrityError("guided blob source mapping is inconsistent")
        _reviewed_paths, blob_ref = candidates[0]
        options = source.options
        if "blob_ref" in options:
            if options["blob_ref"] != blob_ref:
                raise AuditIntegrityError("guided blob source mapping is inconsistent")
            continue
        merged = dict(options)
        merged["blob_ref"] = blob_ref
        reattached[source_name] = replace(source, options=merged)
        changed = True

    live_source_options = {name: source.options for name, source in state.sources.items()}
    for source_name, binding in sentinel_bindings:
        validate_guided_reviewed_sentinel_source_mapping(
            binding,
            source_name=source_name,
            live_source_options=live_source_options,
        )
        sentinel_source = state.sources[source_name]
        options = sentinel_source.options
        if "blob_ref" in options:
            continue
        merged = dict(options)
        merged["blob_ref"] = binding.blob_ref
        reattached[source_name] = replace(sentinel_source, options=merged)
        changed = True

    return replace(state, sources=reattached) if changed else state


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
    export_state = reattach_guided_blob_refs_for_public_export(state)
    projected = cast(PublicCompositionDict, export_state.to_dict())

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


def generate_public_pipeline_dict(state: CompositionState) -> dict[str, Any]:
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
    # → gates → row_unions → aggregations → coalesce → sinks.
    return yaml.dump(doc, default_flow_style=False, sort_keys=False)


def generate_public_yaml(state: CompositionState) -> str:
    """Convert a CompositionState to deterministic public export/share/MCP YAML."""
    doc = generate_public_pipeline_dict(state)
    return yaml.dump(doc, default_flow_style=False, sort_keys=False)
