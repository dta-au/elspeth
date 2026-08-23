"""CompositionState and supporting data models for pipeline composition.

All dataclasses are frozen with slots. Container fields (options, routes,
fork_to, branches) are deep-frozen via freeze_fields() in __post_init__.
Mutation methods return new instances — they never modify the original.

Layer: L3 (application).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from math import isfinite
from pathlib import PurePosixPath
from typing import Any, Final, Literal, NamedTuple, NotRequired, Self, TypedDict

from jinja2 import TemplateSyntaxError
from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.guarantee_propagation import compose_propagation
from elspeth.contracts.plugin_protocols import TransformProtocol
from elspeth.contracts.plugin_semantics import SemanticEdgeContract
from elspeth.contracts.schema import (
    SchemaConfig,
    get_aggregation_contract_options,
    get_raw_node_required_fields,
    get_raw_producer_guaranteed_fields,
    get_raw_schema_config,
    get_raw_sink_required_fields,
    raw_options_have_schema,
)
from elspeth.contracts.sink import (
    FAILSINK_ELIGIBLE_PLUGIN_TEXT,
    FAILSINK_ELIGIBLE_SINK_PLUGINS,
    FILE_SINK_PLUGINS,
    LOCAL_RECOVERY_SINK_PLUGINS,
)
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.contracts.union_merge import UnionTypeConflictError, merge_union_field_flags
from elspeth.contracts.wire_visible_identity import is_wire_visible_placeholder
from elspeth.core.config import (
    _MAX_NODE_NAME_LENGTH,
    _RESERVED_EDGE_LABELS,
    _VALID_NODE_NAME_RE,
    CoalesceSettings,
    TriggerConfig,
    _validate_connection_or_sink_name,
    _validate_max_length,
    _validate_node_name_chars,
    validate_sink_name,
)
from elspeth.core.dag.coalesce_merge import merge_guaranteed_fields
from elspeth.core.templates import extract_jinja2_field_usage
from elspeth.plugins.infrastructure.templates import create_sandboxed_environment, find_runtime_unbound_variables
from elspeth.plugins.sources.field_normalization import (
    describe_undeclared_row_fields,
    normalize_field_name,
    undeclared_row_fields,
)
from elspeth.plugins.transforms.field_mapper import FieldMapperConfig
from elspeth.web.composer._validation_probe import prepare_validation_probe_options
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.validation import INTERPRETATION_PLACEHOLDER_RE

NodeType = Literal["transform", "gate", "aggregation", "coalesce", "row_union", "queue"]
EdgeType = Literal["on_success", "on_error", "route_true", "route_false", "fork"]
CoalesceBranches = tuple[str, ...] | Mapping[str, str]

COMPOSER_NODE_TYPES: frozenset[str] = frozenset(("aggregation", "coalesce", "gate", "queue", "row_union", "transform"))

_DECLARED_INPUT_FIELDS_OPTION = "required_input_fields"
_MISSING_DECLARED_INPUT_FIELDS = object()
_DISCARD_ROUTE_TARGET = "discard"
_FORK_ROUTE_TARGET = "fork"
# A queue's entire runtime surface is QueueSettings, whose only field is an
# optional operator-facing description (elspeth-a5b86149d4). Nothing else may
# ride in a queue node's options.
_QUEUE_OPTION_KEYS: frozenset[str] = frozenset({"description"})
# Read from the runtime model rather than copied as literals: these values exist
# so the two surfaces cannot disagree about what an omitted coalesce field
# means, and a copied literal would drift silently the day CoalesceSettings
# changes its default — the exact drift the normalisation is here to prevent.
_COALESCE_RUNTIME_POLICY_DEFAULT: Final[str] = CoalesceSettings.model_fields["policy"].default
_COALESCE_RUNTIME_MERGE_DEFAULT: Final[str] = CoalesceSettings.model_fields["merge"].default


def _row_union_normalized_branches(node_type: str, branches: CoalesceBranches | None) -> CoalesceBranches | None:
    """Return ``row_union`` list branches as the runtime's identity mapping.

    ``branches`` is returned unchanged for every other node type, for ``None``,
    and for branches already authored as a ``Mapping`` — so this is safe to
    apply unconditionally at a construction boundary.

    A list whose entries are NOT unique is returned as the tuple it was, not
    as a mapping: a dict comprehension would silently erase the duplicate and
    with it the authoring error, so the invalid shape is preserved long enough
    for ``validate()`` to reject it. Unique lists normalize to the runtime's
    ordered identity mapping.

    This lives at module level, and takes ``node_type``/``branches`` as
    parameters rather than reading ``self``, so ``NodeSpec.__post_init__`` and
    ``NodeSpec.from_dict`` share ONE normalisation. The two were byte-identical
    copies before.
    """
    if node_type != "row_union" or branches is None or isinstance(branches, Mapping):
        return branches
    branch_tuple = tuple(branches)
    if len(branch_tuple) != len(set(branch_tuple)):
        return branch_tuple
    return {branch: branch for branch in branch_tuple}


class _ProducerEmitProfile(NamedTuple):
    """What a producer puts on its outgoing edge, for the EXTRAS direction.

    Answer of ``_producer_emit_profile``; see that function for how each field
    is derived and why the emit set differs from the producer's declared
    guarantees.

    ``emits`` are the fields this producer itself puts on the row.
    ``propagates_upstream`` says the fields definitely arriving at its own
    input survive onto that row too, and ``removes_upstream`` names the ones
    that do not — a transform consuming a column it does not forward
    (line_explode's ``source_field``, field_mapper's rename sources).
    ``removes_upstream`` is meaningful only when ``propagates_upstream``;
    a non-propagating profile carries the empty set.

    Subtracting rather than folding the removal into ``emits`` is load-bearing:
    the removal applies to names this node never sees at construction time, so
    it can only be resolved against the upstream set at the union site.
    """

    emits: frozenset[str]
    propagates_upstream: bool
    removes_upstream: frozenset[str]


def validate_composer_source_name(source_name: str) -> None:
    """Validate a composer source name against runtime settings constraints."""
    if not source_name or not source_name.strip():
        raise ValueError("source_name must be a non-empty string.")
    if source_name != source_name.lower():
        raise ValueError(f"Source name '{source_name}' must be lowercase. Suggested fix: '{source_name.lower()}'.")
    _validate_max_length(source_name, field_label="Source name", max_length=_MAX_NODE_NAME_LENGTH)
    _validate_node_name_chars(source_name, field_label="Source name")
    if source_name in _RESERVED_EDGE_LABELS:
        raise ValueError(f"Source name '{source_name}' is reserved. Reserved source/edge labels: {sorted(_RESERVED_EDGE_LABELS)}")
    if source_name.startswith("__"):
        raise ValueError(f"Source name '{source_name}' starts with '__', which is reserved for system edges")


def validate_composer_output_name(output_name: str) -> None:
    """Validate an output's routing label against runtime settings constraints."""

    if not output_name or not output_name.strip() or output_name != output_name.strip():
        raise ValueError("Output name must be a non-empty string without surrounding whitespace")
    validate_sink_name(output_name, field_label="Output name")


_NODE_TYPE_NAME_LABELS: Final[dict[str, str]] = {
    "transform": "Transform name",
    "gate": "Gate name",
    "aggregation": "Aggregation name",
    "coalesce": "Coalesce name",
    "row_union": "row_union name",
    "queue": "Queue name",
}
_LOWERCASE_ONLY_NODE_TYPES: Final[frozenset[str]] = frozenset({"queue"})


def _composer_node_id_validation_message(node_id: str, node_type: str) -> str | None:
    """Return the runtime-equivalent node-name rejection for a composer node id, if any.

    Mirrors ``core/config.py::validate_runtime_node_name`` — the rule every
    runtime node kind (Transform/Gate/Aggregation/Coalesce/row_union settings
    and the ``queues`` mapping) applies to its ``name``: non-empty, at most
    ``_MAX_NODE_NAME_LENGTH`` characters, ``_VALID_NODE_NAME_RE``, not a
    reserved edge label, no ``__`` prefix. Queue names are additionally
    lowercase-only (``ElspethSettings.validate_queue_names``). Stage 1
    mirrored these for SOURCE names only; a 60-character transform id
    validated green and died at ``settings_load`` (elspeth-2ed41f0a4a).
    Wording tracks the runtime's so a repair loop reads one message on both
    surfaces.
    """
    label = _NODE_TYPE_NAME_LABELS.get(node_type, "Node name")
    if not node_id or not node_id.strip():
        return f"{label} must not be empty"
    if node_type in _LOWERCASE_ONLY_NODE_TYPES and node_id != node_id.lower():
        return f"{label} '{node_id}' must be lowercase. Suggested fix: '{node_id.lower()}'."
    if len(node_id) > _MAX_NODE_NAME_LENGTH:
        return f"{label} exceeds max length {_MAX_NODE_NAME_LENGTH} (got {len(node_id)})"
    if not _VALID_NODE_NAME_RE.match(node_id):
        return (
            f"{label} '{node_id}' contains invalid characters. "
            "Node names must start with a letter and contain only letters, digits, underscores, and hyphens."
        )
    if node_id in _RESERVED_EDGE_LABELS:
        return f"{label} '{node_id}' is reserved. Reserved: {sorted(_RESERVED_EDGE_LABELS)}"
    if node_id.startswith("__"):
        return f"{label} '{node_id}' starts with '__', which is reserved for system edges"
    return None


def _label_message(value: str, *, field_label: str) -> str | None:
    """Return the runtime's own rejection for a connection/sink label, or ``None``.

    Calls ``core/config.py::_validate_connection_or_sink_name`` — the exact
    function every ``*Settings`` validator runs on connection, route, branch
    and sink-reference labels — and captures its ``ValueError`` text, so the
    Stage-1 message is the runtime message by construction (max 64,
    connection character class, reserved edge labels, ``__`` prefix).
    Callers own the empty-check wording, which differs per field upstream.
    """
    try:
        _validate_connection_or_sink_name(value, field_label=field_label)
    except ValueError as exc:
        return str(exc)
    return None


def _routing_label_errors(
    *,
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
    outputs: tuple[OutputSpec, ...],
) -> list[ValidationEntry]:
    """Mirror every label rule ``core/config.py`` applies at ``settings_load``.

    Stage 1 previously noticed a bad routing label only INDIRECTLY, through
    the dangling-reference rules — which go silent exactly when the bad
    label is CONSISTENT: a blank ``on_success`` feeding a blank ``input``,
    a connection named ``continue`` on both ends, an ``__``-prefixed fork
    branch that a coalesce also declares. Each validated green and died at
    ``settings_load`` (elspeth-2ed41f0a4a census, 2026-08-17). Sink names
    were unvalidated on the freeform path altogether (elspeth-88a4db09f9).

    Field-by-field the wording tracks the runtime validator that owns the
    field (empty-check text is per field there too); label rules come from
    :func:`_label_message`. Aggregation ``on_error`` and source
    ``on_validation_failure`` carry no ``config.py`` validator — the DAG
    builder resolves them — so they are deliberately absent here; the
    dangling-target rules already cover them.
    """
    found: list[ValidationEntry] = []

    def add(component: str, message: str, code: str = "connection_label_invalid") -> None:
        found.append(ValidationEntry(component, message, "high", code))

    def label(component: str, value: object, field_label: str) -> None:
        # Malformed external values (a non-string branch value from a
        # persisted payload) are owned by the intrinsic node-shape checks;
        # this rule only speaks to well-typed labels.
        if not isinstance(value, str):
            return
        message = _label_message(value, field_label=field_label)
        if message is not None:
            add(component, message)

    for source_name, source in sources.items():
        component = "source" if source_name == "source" else f"source:{source_name}"
        if not source.on_success or not source.on_success.strip():
            add(component, "Source on_success must be a connection name or sink name")
        else:
            label(component, source.on_success, "Source on_success connection name")

    for node in nodes:
        component = f"node:{node.id}"
        if node.node_type in ("transform", "aggregation", "gate"):
            kind = {"transform": "Transform", "aggregation": "Aggregation", "gate": "Gate"}[node.node_type]
            if node.input is None or not node.input.strip():
                add(component, f"{kind} input connection must not be empty")
            else:
                label(component, node.input, f"{kind} input connection name")
        if node.node_type == "transform":
            if node.on_success is not None:
                if not node.on_success.strip():
                    add(component, "on_success must be a connection name or sink name")
                else:
                    label(component, node.on_success, "Transform on_success connection name")
            if node.on_error is not None:
                if not node.on_error.strip():
                    add(component, "on_error must be a sink name or 'discard'")
                elif node.on_error != _DISCARD_ROUTE_TARGET:
                    label(component, node.on_error, "Transform on_error sink name")
        elif node.node_type == "aggregation":
            if node.on_success is not None:
                if not node.on_success.strip():
                    add(component, "on_success must be a connection name, sink name, or omitted entirely")
                else:
                    label(component, node.on_success, "Aggregation on_success connection name")
        elif node.node_type == "gate":
            for route_label, destination in (node.routes or {}).items():
                if not route_label:
                    add(component, "Route labels must not be empty")
                else:
                    label(component, route_label, "Route label")
                if destination in (_FORK_ROUTE_TARGET, _DISCARD_ROUTE_TARGET):
                    continue
                if destination == "continue":
                    add(component, "Route destination 'continue' has been removed. Use an explicit connection name or sink name.")
                    continue
                label(component, destination, f"Route destination for label '{route_label}'")
            if node.on_error is not None:
                if not node.on_error.strip():
                    add(component, "on_error must be a sink name, 'discard', or omitted")
                elif node.on_error != _DISCARD_ROUTE_TARGET:
                    label(component, node.on_error, "Gate on_error sink name")
            for branch in node.fork_to or ():
                if not branch or not branch.strip():
                    add(component, "Fork branch names must not be empty")
                else:
                    label(component, branch, "Fork branch name")
        elif node.node_type in ("coalesce", "row_union"):
            kind = "Coalesce" if node.node_type == "coalesce" else "row_union"
            raw_branches = node.branches
            # Typed ``object`` on purpose: a persisted payload can carry a
            # non-string branch value that ``NodeSpec.from_dict`` admits and
            # the intrinsic node-shape checks own; the isinstance guard below
            # keeps this rule to well-typed labels.
            items: list[tuple[object, object]]
            if isinstance(raw_branches, Mapping):
                items = list(raw_branches.items())
            elif raw_branches is not None:
                listed = [name for name in raw_branches if isinstance(name, str)]
                duplicates = sorted({name for name in listed if listed.count(name) > 1})
                if duplicates:
                    # The runtime's ``normalize_branches`` raises here before
                    # the per-branch validator runs; report once, then walk
                    # the distinct names so the same duplicate is not
                    # re-reported as a trim collision.
                    add(component, f"Duplicate branch names in list: {duplicates}")
                items = [(name, name) for name in dict.fromkeys(listed)]
            else:
                items = []
            seen_keys: set[str] = set()
            for branch_name, connection in items:
                if not isinstance(branch_name, str) or not isinstance(connection, str):
                    continue
                if not branch_name or not branch_name.strip():
                    add(component, f"{kind} branch names must not be empty")
                    continue
                if not connection or not connection.strip():
                    add(component, f"{kind} branch '{branch_name}' input connection must not be empty")
                    continue
                key = branch_name.strip()
                if key in seen_keys:
                    add(component, f"{kind} branch names collide after trimming whitespace: '{key}' is declared twice")
                seen_keys.add(key)
                label(component, key, f"{kind} branch name")
                label(component, connection.strip(), f"{kind} branch '{key}' input connection")
            if node.node_type == "coalesce":
                if node.on_success is not None:
                    if not node.on_success.strip():
                        add(component, "on_success must be a sink name or omitted entirely")
                    else:
                        label(component, node.on_success, "Coalesce on_success sink name")
            elif node.on_success is None or not node.on_success.strip():
                add(component, "row_union on_success must not be empty")
            else:
                label(component, node.on_success, "row_union on_success connection name")

    for output in outputs:
        component = f"output:{output.name}"
        try:
            validate_composer_output_name(output.name)
        except ValueError as exc:
            add(component, str(exc), "output_name_invalid")
        if not output.on_write_failure or not output.on_write_failure.strip():
            add(component, "on_write_failure must be a sink name or 'discard'")
        elif output.on_write_failure != _DISCARD_ROUTE_TARGET:
            label(component, output.on_write_failure, "Sink on_write_failure sink name")

    return found


# ``ElspethSettings`` collection caps (``Field(max_length=...)`` in
# core/config.py); the composer's node types map onto the runtime sections
# they export to.
_RUNTIME_COLLECTION_CAPS: Final[dict[str, int]] = {
    "sources": 50,
    "sinks": 50,
    "queues": 100,
    "transforms": 500,
    "gates": 100,
    "coalesce": 100,
    "row_unions": 100,
    "aggregations": 100,
}
_RUNTIME_SECTION_BY_NODE_TYPE: Final[dict[str, str]] = {
    "transform": "transforms",
    "gate": "gates",
    "aggregation": "aggregations",
    "coalesce": "coalesce",
    "row_union": "row_unions",
    "queue": "queues",
}
_RUNTIME_GATE_MAPPING_CAP: Final = 32  # GateSettings.routes / fork_to max_length


def _collection_cap_errors(
    *,
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
    outputs: tuple[OutputSpec, ...],
) -> list[ValidationEntry]:
    """Mirror the runtime's declarative collection caps.

    ``ElspethSettings.sources/sinks/...`` and ``GateSettings.routes/fork_to``
    carry ``Field(max_length=...)`` — a settings-load rejection with no raise
    site, invisible to a raise census and to Stage 1 until now. A composer
    could author a 51st sink and read ``is_valid: true``.
    """
    found: list[ValidationEntry] = []
    counts: Counter[str] = Counter({"sources": len(sources), "sinks": len(outputs)})
    for node in nodes:
        if node.node_type in _RUNTIME_SECTION_BY_NODE_TYPE:
            counts[_RUNTIME_SECTION_BY_NODE_TYPE[node.node_type]] += 1
    for section, count in counts.items():
        cap = _RUNTIME_COLLECTION_CAPS[section]
        if count > cap:
            found.append(
                ValidationEntry(
                    "pipeline",
                    f"Pipeline declares {count} {section}; the runtime accepts at most {cap}.",
                    "high",
                    "pipeline_collection_cap_exceeded",
                )
            )
    for node in nodes:
        if node.node_type != "gate":
            continue
        if node.routes is not None and len(node.routes) > _RUNTIME_GATE_MAPPING_CAP:
            found.append(
                ValidationEntry(
                    f"node:{node.id}",
                    f"Gate '{node.id}' declares {len(node.routes)} routes; the runtime accepts at most {_RUNTIME_GATE_MAPPING_CAP}.",
                    "high",
                    "pipeline_collection_cap_exceeded",
                )
            )
        if node.fork_to is not None and len(node.fork_to) > _RUNTIME_GATE_MAPPING_CAP:
            found.append(
                ValidationEntry(
                    f"node:{node.id}",
                    f"Gate '{node.id}' declares {len(node.fork_to)} fork branches; the runtime accepts at most {_RUNTIME_GATE_MAPPING_CAP}.",
                    "high",
                    "pipeline_collection_cap_exceeded",
                )
            )
    return found


def _composer_source_name_validation_message(source_name: str) -> str | None:
    """Return the runtime-equivalent source-name validation error, if any."""
    if not source_name or not source_name.strip():
        return "source_name must be a non-empty string."
    if source_name != source_name.lower():
        return f"Source name '{source_name}' must be lowercase. Suggested fix: '{source_name.lower()}'."
    if len(source_name) > _MAX_NODE_NAME_LENGTH:
        return f"Source name exceeds max length {_MAX_NODE_NAME_LENGTH} (got {len(source_name)})"
    if not _VALID_NODE_NAME_RE.match(source_name):
        return (
            f"Source name '{source_name}' contains invalid characters. "
            "Node names must start with a letter and contain only letters, digits, underscores, and hyphens."
        )
    if source_name in _RESERVED_EDGE_LABELS:
        return f"Source name '{source_name}' is reserved. Reserved source/edge labels: {sorted(_RESERVED_EDGE_LABELS)}"
    if source_name.startswith("__"):
        return f"Source name '{source_name}' starts with '__', which is reserved for system edges"
    return None


@dataclass(frozen=True, slots=True)
class PipelineMetadata:
    """Pipeline-level metadata.

    All fields are scalars or None. frozen=True is sufficient.
    """

    name: str = "Untitled Pipeline"
    description: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        """Reconstruct from a plain dict (inverse of to_dict serialisation)."""
        return cls(
            name=d["name"],
            description=d["description"],
        )


@dataclass(frozen=True, slots=True)
class SourceSpec:
    """Pipeline source configuration.

    Attributes:
        plugin: Source plugin name (e.g. "csv", "json", "dataverse").
        on_success: Named connection point for the first downstream node.
        options: Plugin-specific configuration (path, schema, etc.).
        on_validation_failure: How to handle rows that fail schema validation.
        description: Optional composer-authored one-sentence prose describing
            what this step does, rendered on the Spec tab. Informational only:
            it never participates in validation, lowering, or review hashes.
    """

    plugin: str
    on_success: str
    options: Mapping[str, Any]
    on_validation_failure: str
    description: str | None = None

    def __post_init__(self) -> None:
        freeze_fields(self, "options")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        """Reconstruct from a plain dict (inverse of to_dict serialisation).

        ``description`` defaults to None when absent so sessions persisted
        before the field existed deserialise unchanged.
        """
        return cls(
            plugin=d["plugin"],
            on_success=d["on_success"],
            options=d["options"],
            on_validation_failure=d["on_validation_failure"],
            description=d["description"] if "description" in d else None,
        )


@dataclass(frozen=True, slots=True)
class NodeSpec:
    """Transform, gate, aggregation, coalesce, row_union, or queue node.

    Attributes:
        id: Unique node identifier within the pipeline.
        node_type: One of the composer-supported node discriminators.
        plugin: Plugin name. None for structural nodes.
        input: Named connection point this node reads from.
        on_success: Named connection point for successful output. None for gates.
        on_error: Named connection point for error output. None if not diverted.
        options: Plugin-specific configuration.
        condition: Gate expression. None for non-gates.
        routes: Gate route mapping. None for non-gates.
        fork_to: Fork destinations for fork gates. None for non-fork nodes.
        branches: Branch inputs for coalesce/row_union nodes. None otherwise.
        policy: Coalesce arrival policy, defaulted to "require_all" when a
            coalesce omits it so composer state carries the policy the runtime
            will actually run. None for non-coalesce nodes.
        merge: Coalesce merge strategy, defaulted to "union" when a coalesce
            omits it so composer state carries the strategy the runtime will
            actually run. None for non-coalesce nodes.
        trigger: Aggregation batch trigger config. None for non-aggregation nodes.
        output_mode: Aggregation output mode ("passthrough" or "transform"). None for non-aggregation nodes.
        expected_output_count: Aggregation expected output count. None for non-aggregation nodes.
        timeout_seconds: Structural barrier timeout. None for other node types.
        description: Optional composer-authored one-sentence prose describing
            what this step does, rendered on the Spec tab. Informational only:
            it never participates in validation, lowering, or review hashes.
    """

    id: str
    node_type: NodeType
    plugin: str | None
    input: str
    on_success: str | None
    on_error: str | None
    options: Mapping[str, Any]
    condition: str | None
    routes: Mapping[str, str] | None
    fork_to: tuple[str, ...] | None
    branches: CoalesceBranches | None
    policy: str | None
    merge: str | None
    trigger: Mapping[str, Any] | None = None
    output_mode: str | None = None
    expected_output_count: int | None = None
    timeout_seconds: float | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        # ``CoalesceSettings`` DEFAULTS both optional coalesce fields —
        # ``merge`` to "union", ``policy`` to "require_all" (core/config.py) —
        # so a coalesce authored without them is a require_all union merge at
        # run time. Carrying None into composer state made every union rule —
        # which gates on ``merge == "union"`` — read the node as "not a union"
        # and skip it, so a type-incompatible merge validated green and died at
        # the DAG build; the same None on ``policy`` made Stage 1 REJECT a
        # pipeline the runtime accepts and runs (elspeth-deb2f5ed93) and made
        # the require_all-keyed call sites — ``merge_union_field_flags`` and
        # ``merge_guaranteed_fields`` — read the node as "not require_all".
        # Normalising HERE rather than at each gate is the point: it is the one
        # construction boundary every path routes through (``from_dict``,
        # ``upsert_node``, ``set_pipeline``, ``replace``), so a third union rule
        # added later cannot inherit the hole. This defaults the fields, it
        # never requires them — the runtime accepts both unset, and Stage 1 must
        # not be stricter than the runtime.
        if self.node_type == "coalesce" and self.merge is None:
            object.__setattr__(self, "merge", _COALESCE_RUNTIME_MERGE_DEFAULT)
        if self.node_type == "coalesce" and self.policy is None:
            object.__setattr__(self, "policy", _COALESCE_RUNTIME_POLICY_DEFAULT)
        # Do NOT extend this normalisation to ``on_error`` by analogy. The shapes
        # look identical and the remedies are inverted. ``merge`` has a runtime
        # DEFAULT to mirror (``CoalesceSettings.merge = "union"``), so defaulting
        # it here RECORDS a decision the runtime has already made. A transform's
        # ``on_error`` has NO runtime default — ``TransformSettings.on_error`` is
        # a required ``str`` (core/config.py) — so defaulting it here would
        # INVENT a routing decision the author never made, and "discard" silently
        # drops failed rows in a system whose purpose is lineage. Compare the
        # gate, whose ``on_error`` IS optional and whose documented posture for
        # omission is fail-FAST, not discard. Stage 1 already does the right
        # thing by REJECTING an unset transform ``on_error``
        # (``transform_missing_on_error``); a default here would suppress that
        # error, not complement it. Note ``from_dict`` reads
        # ``on_error=d["on_error"]`` unnormalised, so a session persisted with
        # ``on_error: null`` deserialises to None — contained, because Stage 1
        # rejects it.
        # Unconditional: the helper is a no-op for every shape that needs no
        # normalisation, and returns the value it was given.
        object.__setattr__(self, "branches", _row_union_normalized_branches(self.node_type, self.branches))
        # Mapping fields must be deep-frozen. Scalar, enum, and tuple fields
        # are already immutable and need no guard.
        freeze_fields(self, "options")
        if self.routes is not None:
            freeze_fields(self, "routes")
        if self.branches is not None:
            freeze_fields(self, "branches")
        if self.trigger is not None:
            freeze_fields(self, "trigger")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        """Reconstruct from a plain dict (inverse of to_dict serialisation).

        Optional fields (condition, routes, fork_to, branches, policy, merge,
        trigger, output_mode, expected_output_count, timeout_seconds) default to None when
        absent from the dict. fork_to is converted from list to tuple since
        to_dict() serialises tuples as lists. Coalesce branches preserve their
        list-vs-mapping semantics; row_union list branches normalize to the
        runtime's ordered identity mapping.
        """
        fork_to = d["fork_to"] if "fork_to" in d else None
        branches = d["branches"] if "branches" in d else None
        # Mapping branches are defensively copied; list branches are coerced to
        # a tuple and then routed through the SHARED row_union normalisation
        # that ``__post_init__`` also applies, so the two can never drift.
        normalized_branches: CoalesceBranches | None
        if isinstance(branches, Mapping):
            normalized_branches = dict(branches)
        elif branches is None:
            normalized_branches = None
        else:
            normalized_branches = _row_union_normalized_branches(d["node_type"], tuple(branches))
        return cls(
            id=d["id"],
            node_type=d["node_type"],
            plugin=d["plugin"],
            input=d["input"],
            on_success=d["on_success"],
            on_error=d["on_error"],
            options=d["options"],
            condition=d["condition"] if "condition" in d else None,
            routes=d["routes"] if "routes" in d else None,
            fork_to=tuple(fork_to) if fork_to is not None else None,
            branches=normalized_branches,
            policy=d["policy"] if "policy" in d else None,
            merge=d["merge"] if "merge" in d else None,
            trigger=d["trigger"] if "trigger" in d else None,
            output_mode=d["output_mode"] if "output_mode" in d else None,
            expected_output_count=d["expected_output_count"] if "expected_output_count" in d else None,
            timeout_seconds=d["timeout_seconds"] if "timeout_seconds" in d else None,
            description=d["description"] if "description" in d else None,
        )


def _coalesce_branch_names(branches: CoalesceBranches | None) -> tuple[str, ...]:
    """Return branch identities declared by a coalesce node."""
    if branches is None:
        return ()
    if isinstance(branches, Mapping):
        return tuple(branches.keys())
    return branches


def _coalesce_branch_connections(branches: CoalesceBranches | None) -> tuple[str, ...]:
    """Return input connections consumed by a coalesce node."""
    if branches is None:
        return ()
    if isinstance(branches, Mapping):
        return tuple(branches.values())
    return branches


def _coalesce_mapped_branch_connections(branches: CoalesceBranches | None) -> tuple[str, ...]:
    """Return branch connections registered as runtime consumers.

    Identity branches (``branch_name == input_connection``), including every
    list-form branch, are direct gate-to-coalesce COPY edges. Only mapped
    branches traverse the ordinary connection registry and claim a consumer.
    """
    return tuple(
        input_connection
        for branch_name, input_connection in zip(
            _coalesce_branch_names(branches),
            _coalesce_branch_connections(branches),
            strict=True,
        )
        if branch_name != input_connection
    )


def _serialize_branches(branches: CoalesceBranches) -> list[str] | dict[str, str]:
    """Serialize coalesce branches preserving list-vs-mapping semantics."""
    if isinstance(branches, Mapping):
        return dict(deep_thaw(branches))
    return list(branches)


@observation_boundary(
    tier=3,
    source="a NodeSpec.timeout_seconds value re-read from a persisted session payload via NodeSpec.from_dict",
    source_param="value",
    suppresses=("R5",),
    invariant=(
        "returns True (invalid) for any value that is not int/float, is bool, or converts to a "
        "non-finite/non-positive magnitude; float()'s OverflowError on an arbitrary-precision JSON "
        "int is caught, so this boundary never raises"
    ),
)
def _timeout_seconds_is_invalid(value: object) -> bool:
    """Return whether a structural barrier timeout violates runtime bounds.

    Persisted session payloads reach this helper through ``NodeSpec.from_dict``
    without crossing the Pydantic ``_StrictTimeoutSeconds`` tool boundary, and
    JSON has no integer ceiling — so an arbitrary-precision int can arrive
    here. ``float()`` raises ``OverflowError`` on those, which would abort
    ``validate()`` instead of producing a rejection, so the conversion is
    guarded and an unrepresentable magnitude is classified INVALID. Mirrors
    ``yaml_importer._finite_positive_timeout``. The isinstance guard above
    leaves ``int`` as the only value ``float()`` can reject, so OverflowError
    is the only reachable failure.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    try:
        normalized = float(value)
    except OverflowError:
        return True
    return not isfinite(normalized) or normalized <= 0


_PLUGINLESS_STRUCTURAL_NODE_TYPES: Final[frozenset[str]] = frozenset({"gate", "coalesce"})


def structural_node_plugin_error(node: NodeSpec) -> str | None:
    """Return the contract violation for a plugin authored onto a gate or coalesce.

    Every structural node type is wired with ``plugin=null`` — the tool schema
    says so and the YAML generator never emits a plugin for them — but only
    queues (``queue_node_contract_error``) and row_unions (their forbidden-
    fields block in ``validate()``) enforced it. A gate or coalesce could carry
    an authored token in ``plugin`` while validating clean, and later consumers
    that read ``plugin or node_type`` (the composer RGR scorer, for one) would
    classify the node by that token. The token itself is not echoed: it is
    arbitrary authored text, not closed composer vocabulary. Returns None for
    every other node type and for a plugin-less gate/coalesce.
    """
    if node.node_type not in _PLUGINLESS_STRUCTURAL_NODE_TYPES or node.plugin is None:
        return None
    return (
        f"{node.node_type} '{node.id}' does not accept a plugin: '{node.node_type}' is a built-in "
        "node_type wired with plugin=null. Re-emit the node with plugin=null."
    )


@observation_boundary(
    tier=3,
    source="NodeSpec carrying composer/LLM/user-authored options (untrusted options.description value)",
    source_param="node",
    suppresses=("R5",),
    invariant=(
        "returns an error string only for a concretely malformed queue shape; a non-string "
        "options.description is one such violation, and the function never raises"
    ),
)
def queue_node_contract_error(node: NodeSpec) -> str | None:
    """Return the intrinsic (topology-free) contract violation for a queue node.

    A queue is a structural pass-through fan-in point (elspeth-a5b86149d4). Its
    canonical shape is ``id == input``, no plugin/routing/coalesce/aggregation
    fields, implicit output under its own id (``on_success is None``), and at
    most a string ``description`` option. This helper is the SINGLE source of
    truth for that shape so state validation, the mutation tools, and YAML
    generation all reject the same malformed queues identically. It performs no
    state/topology lookup — producer/consumer/namespace checks live in
    ``validate()``. Returns None for a non-queue node or a canonical queue.
    """
    if node.node_type != "queue":
        return None
    if node.input != node.id:
        return f"Queue '{node.id}' input must equal its id."
    forbidden = {
        "plugin": node.plugin,
        "on_success": node.on_success,
        "on_error": node.on_error,
        "condition": node.condition,
        "routes": node.routes,
        "fork_to": node.fork_to,
        "branches": node.branches,
        "policy": node.policy,
        "merge": node.merge,
        "trigger": node.trigger,
        "output_mode": node.output_mode,
        "expected_output_count": node.expected_output_count,
        "timeout_seconds": node.timeout_seconds,
    }
    present = sorted(name for name, value in forbidden.items() if value is not None)
    if present:
        return f"Queue '{node.id}' does not accept field(s): {present}."
    unknown = sorted(set(node.options) - _QUEUE_OPTION_KEYS)
    if unknown:
        return f"Queue '{node.id}' contains unknown option(s): {unknown}."
    description = node.options.get("description")
    if description is not None and not isinstance(description, str):
        return f"Queue '{node.id}' options.description must be a string."
    return None


@dataclass(frozen=True, slots=True)
class EdgeSpec:
    """Connection between two nodes.

    Attributes:
        id: Unique edge identifier.
        from_node: Source node ID (or "source" for the pipeline source).
        to_node: Destination node ID or sink name.
        edge_type: One of "on_success", "on_error", "route_true", "route_false", "fork".
        label: Display label (e.g. the route key for gate edges).
    """

    id: str
    from_node: str
    to_node: str
    edge_type: EdgeType
    label: str | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        """Reconstruct from a plain dict (inverse of to_dict serialisation)."""
        return cls(
            id=d["id"],
            from_node=d["from_node"],
            to_node=d["to_node"],
            edge_type=d["edge_type"],
            label=d["label"],
        )


@dataclass(frozen=True, slots=True)
class OutputSpec:
    """Sink configuration.

    Attributes:
        name: Sink name (used as connection point in edges and routes).
        plugin: Sink plugin name (e.g. "csv", "json", "database").
        options: Plugin-specific configuration.
        on_write_failure: How to handle write failures ("discard" or a sink name).
        description: Optional composer-authored one-sentence prose describing
            what this step does, rendered on the Spec tab. Informational only:
            it never participates in validation, lowering, or review hashes.
    """

    name: str
    plugin: str
    options: Mapping[str, Any]
    on_write_failure: str
    description: str | None = None

    def __post_init__(self) -> None:
        freeze_fields(self, "options")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        """Reconstruct from a plain dict (inverse of to_dict serialisation).

        ``description`` defaults to None when absent so sessions persisted
        before the field existed deserialise unchanged.
        """
        return cls(
            name=d["name"],
            plugin=d["plugin"],
            options=d["options"],
            on_write_failure=d["on_write_failure"],
            description=d["description"] if "description" in d else None,
        )


Severity = Literal["high", "medium", "low"]


class SchemaContractDetailDict(TypedDict):
    """JSON representation of :class:`SchemaContractDetail`."""

    producer: str
    consumer: str
    missing_fields: NotRequired[list[str]]
    extra_fields: NotRequired[list[str]]


@dataclass(frozen=True, slots=True)
class SchemaContractDetail:
    """Redaction-safe structural facts for a schema-contract rejection.

    The planner's repair feedback (``_allowlisted_candidate_feedback``)
    strips raw validation messages — they can quote plugin option values or
    row content — and keys enrichment on the closed ``error_code`` alone. A
    bare ``schema_contract_violation`` is not repairable within the repair
    budget: the planner must know WHICH edge failed and WHICH fields are
    missing. This detail carries exactly those facts and nothing else:
    ``producer``/``consumer`` are pipeline component identifiers (node ids /
    ``output:<name>`` / source producer ids) and the field tuples are schema
    FIELD NAMES from validated contract config — pipeline metadata the
    session owner authored, never user row content — so forwarding them does
    not re-open the message redaction boundary.
    """

    producer: str
    consumer: str
    missing_fields: tuple[str, ...] = ()
    extra_fields: tuple[str, ...] = ()

    def to_dict(self) -> SchemaContractDetailDict:
        """Serialize to a plain dict for JSON responses."""
        result = SchemaContractDetailDict(producer=self.producer, consumer=self.consumer)
        if self.missing_fields:
            result["missing_fields"] = list(self.missing_fields)
        if self.extra_fields:
            result["extra_fields"] = list(self.extra_fields)
        return result


class RowUnionFieldSchemaDetailDict(TypedDict):
    """One validated field declaration in row_union repair facts."""

    name: str
    field_type: str
    required: bool
    nullable: bool


@dataclass(frozen=True, slots=True)
class RowUnionFieldSchemaDetail:
    """Redaction-safe field metadata from validated schema configuration."""

    name: str
    field_type: str
    required: bool
    nullable: bool

    def to_dict(self) -> RowUnionFieldSchemaDetailDict:
        return RowUnionFieldSchemaDetailDict(
            name=self.name,
            field_type=self.field_type,
            required=self.required,
            nullable=self.nullable,
        )


class RowUnionBranchSchemaDetailDict(TypedDict):
    """One row_union branch's explicit schema declaration."""

    branch: str
    mode: Literal["fixed", "flexible"]
    fields: list[RowUnionFieldSchemaDetailDict]


@dataclass(frozen=True, slots=True)
class RowUnionBranchSchemaDetail:
    """Redaction-safe branch schema facts for planner repair."""

    branch: str
    mode: Literal["fixed", "flexible"]
    fields: tuple[RowUnionFieldSchemaDetail, ...]

    def to_dict(self) -> RowUnionBranchSchemaDetailDict:
        return RowUnionBranchSchemaDetailDict(
            branch=self.branch,
            mode=self.mode,
            fields=[field.to_dict() for field in self.fields],
        )


def _row_union_branch_schema_detail(
    branch: str,
    schema_config: SchemaConfig,
) -> RowUnionBranchSchemaDetail:
    """Project one already-proven explicit branch declaration."""
    mode = schema_config.mode
    assert mode in ("fixed", "flexible")
    assert schema_config.fields is not None
    return RowUnionBranchSchemaDetail(
        branch=branch,
        mode=mode,
        fields=tuple(
            RowUnionFieldSchemaDetail(
                name=field.name,
                field_type=field.field_type,
                required=field.required,
                nullable=field.nullable,
            )
            for field in schema_config.fields
        ),
    )


class RowUnionSchemaDetailDict(TypedDict):
    """Structured facts for a row_union schema incompatibility."""

    branches: list[RowUnionBranchSchemaDetailDict]
    conflicting_fields: list[str]


@dataclass(frozen=True, slots=True)
class RowUnionSchemaDetail:
    """Exact validated branch declarations needed to repair a row_union."""

    branches: tuple[RowUnionBranchSchemaDetail, ...]
    conflicting_fields: tuple[str, ...]

    def to_dict(self) -> RowUnionSchemaDetailDict:
        return RowUnionSchemaDetailDict(
            branches=[branch.to_dict() for branch in self.branches],
            conflicting_fields=list(self.conflicting_fields),
        )


class CoalesceUnionTypeDetailDict(TypedDict):
    """Structured facts for a union-coalesce shared-field type conflict."""

    field: str
    branch_a: str
    type_a: str
    branch_b: str
    type_b: str


@dataclass(frozen=True, slots=True)
class CoalesceUnionTypeDetail:
    """The exact conflicting declaration a union coalesce cannot merge.

    Same custody class as :class:`RowUnionSchemaDetail`: a field name and two
    branch names plus their declared types, all read from validated schema
    config — pipeline identifiers and schema field names, never user row
    content. The planner's feedback projection withholds raw validation
    messages, so without these facts the closed code names the failing NODE but
    not which FIELD conflicts. That gap is not hypothetical here: a branch can
    conflict on a field it never declared, because a plugin contributes its own
    computed output fields (``value_transform`` adds an operation target as
    ``any``), and no amount of re-reading the authored candidate reveals it.
    """

    field: str
    branch_a: str
    type_a: str
    branch_b: str
    type_b: str

    def to_dict(self) -> CoalesceUnionTypeDetailDict:
        return CoalesceUnionTypeDetailDict(
            field=self.field,
            branch_a=self.branch_a,
            type_a=self.type_a,
            branch_b=self.branch_b,
            type_b=self.type_b,
        )


class ValidationEntryDict(TypedDict):
    """JSON representation of :class:`ValidationEntry`."""

    component: str
    message: str
    severity: Severity
    error_code: NotRequired[str]
    contract: NotRequired[SchemaContractDetailDict]
    row_union_schema: NotRequired[RowUnionSchemaDetailDict]
    coalesce_union_type: NotRequired[CoalesceUnionTypeDetailDict]


@dataclass(frozen=True, slots=True)
class ValidationEntry:
    """Structured validation message with component attribution.

    Scalar fields plus an optional frozen ``contract`` detail; frozen=True
    is sufficient for immutability.
    """

    component: str
    message: str
    severity: Severity
    error_code: str | None = None
    contract: SchemaContractDetail | None = None
    row_union_schema: RowUnionSchemaDetail | None = None
    coalesce_union_type: CoalesceUnionTypeDetail | None = None
    # The ``(kind, plugin)`` this entry is ABOUT, recorded by the producer at
    # the moment it builds the failure — where the identity is known
    # authoritatively — for the in-process consumer that would otherwise have
    # to recover it by parsing ``message``. Parsing is not recoverable here:
    # these messages interpolate model-authored option values, option KEYS,
    # and the component name itself, and three successive parsers were each
    # defeated by a different one of those (elspeth-1d8fc3da83).
    #
    # Set it ONLY where the identity has already been resolved through the
    # request's policy view; a name that has not been is exactly the input
    # that makes a downstream catalog lookup raise. Absent is the safe value —
    # consumers must fail closed on ``None`` rather than fall back to parsing.
    #
    # Deliberately IN-MEMORY ONLY: absent from ``ValidationEntryDict`` and
    # from ``to_dict`` below, so no wire shape, redaction manifest, or audit
    # projection moves. Keep it that way unless a wire consumer genuinely
    # needs it.
    plugin_identity: tuple[str, str] | None = None

    def to_dict(self) -> ValidationEntryDict:
        """Serialize to a plain dict for JSON responses."""
        result = ValidationEntryDict(component=self.component, message=self.message, severity=self.severity)
        if self.error_code is not None:
            result["error_code"] = self.error_code
        if self.contract is not None:
            result["contract"] = self.contract.to_dict()
        if self.row_union_schema is not None:
            result["row_union_schema"] = self.row_union_schema.to_dict()
        if self.coalesce_union_type is not None:
            result["coalesce_union_type"] = self.coalesce_union_type.to_dict()
        return result


EdgeContractDict = TypedDict(
    "EdgeContractDict",
    {
        "from": str,
        "to": str,
        "producer_guarantees": list[str],
        "consumer_requires": list[str],
        "missing_fields": list[str],
        "satisfied": bool,
    },
)


@dataclass(frozen=True, slots=True)
class EdgeContract:
    """Schema contract check result for a single producer->consumer edge."""

    from_id: str
    to_id: str
    producer_guarantees: tuple[str, ...]
    consumer_requires: tuple[str, ...]
    missing_fields: tuple[str, ...]
    satisfied: bool

    def to_dict(self) -> EdgeContractDict:
        """Serialize to a plain dict for JSON responses."""
        return {
            "from": self.from_id,
            "to": self.to_id,
            "producer_guarantees": list(self.producer_guarantees),
            "consumer_requires": list(self.consumer_requires),
            "missing_fields": list(self.missing_fields),
            "satisfied": self.satisfied,
        }


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """Stage 1 validation result.

    errors block execution. warnings are advisory but actionable.
    suggestions are optional improvements. edge_contracts shows
    per-edge schema contract check results. semantic_contracts shows
    per-edge semantic contract check results (Phase 1: line_explode +
    web_scrape only). All are tuples for structured component
    attribution.
    """

    is_valid: bool
    errors: tuple[ValidationEntry, ...]
    warnings: tuple[ValidationEntry, ...] = ()
    suggestions: tuple[ValidationEntry, ...] = ()
    edge_contracts: tuple[EdgeContract, ...] = ()
    semantic_contracts: tuple[SemanticEdgeContract, ...] = ()


def _source_options_have_schema(options: Mapping[str, Any]) -> bool:
    """Return whether source options carry a schema under the current contract.

    Composer state can contain either the user-facing ``schema`` alias or the
    internal ``schema_config`` field name, because plugin config parsing allows
    population by either key. Read-only summaries and validation must use the
    same rule so they cannot drift.
    """
    return raw_options_have_schema(options)


def _known_batch_aware_transform_plugins() -> frozenset[str]:
    """Return transform names whose runtime config rejects declared inputs."""
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    transforms = get_shared_plugin_manager().get_transforms()
    return frozenset(cls.name for cls in transforms if cls.is_batch_aware)


def _known_batch_aware_transform_plugins_requiring_aggregation() -> frozenset[str]:
    """Return batch-aware transform names that do not support row-mode dispatch."""
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

    transforms = get_shared_plugin_manager().get_transforms()
    return frozenset(cls.name for cls in transforms if cls.is_batch_aware and not cls.supports_row_mode_when_batch_aware)


def _declared_input_fields_option(options: Mapping[str, Any]) -> object:
    """Return the raw declared-input-field option, including wrapper-shaped aggregations."""
    if _DECLARED_INPUT_FIELDS_OPTION in options:
        return options[_DECLARED_INPUT_FIELDS_OPTION]

    if "options" in options:
        nested_options = options["options"]
        if isinstance(nested_options, Mapping) and _DECLARED_INPUT_FIELDS_OPTION in nested_options:
            return nested_options[_DECLARED_INPUT_FIELDS_OPTION]

    return _MISSING_DECLARED_INPUT_FIELDS


def _batch_aware_required_input_fields_error(
    node_id: str,
    plugin_name: str | None,
    options: Mapping[str, Any],
) -> str | None:
    """Reject ADR-013 declared input fields on batch-aware transform configs."""
    if plugin_name is None or plugin_name not in _known_batch_aware_transform_plugins():
        return None

    declared_input_fields = _declared_input_fields_option(options)
    if declared_input_fields is _MISSING_DECLARED_INPUT_FIELDS or declared_input_fields in (None, [], ()):
        return None

    return (
        f"Node '{node_id}' sets required_input_fields={declared_input_fields!r}, "
        f"but transform '{plugin_name}' is batch-aware. ADR-013 declared input "
        "fields only have a non-batch pre-emission dispatch site; remove "
        "required_input_fields and express batch input requirements with "
        "schema.required_fields."
    )


def _batch_aware_placement_error(
    node_id: str,
    node_type: str,
    plugin_name: str | None,
    output_mode: str | None,
) -> str | None:
    """Reject batch-only transforms from row-mode composer placement."""
    if plugin_name is None or plugin_name not in _known_batch_aware_transform_plugins_requiring_aggregation():
        return None

    if node_type == "transform":
        message = (
            f"Node '{node_id}' uses batch-aware transform '{plugin_name}' as node_type='transform'. "
            "Batch-aware transforms require the aggregation/batch path unless the plugin explicitly supports row mode. "
            "Configure this node as node_type='aggregation' with an aggregation trigger, or use a row-level transform instead."
        )
        if plugin_name == "batch_replicate":
            message += " For batch_replicate, set output_mode: transform so replicated rows create new downstream tokens."
        return message

    if plugin_name == "batch_replicate" and node_type == "aggregation" and output_mode != "transform":
        return (
            f"Node '{node_id}' uses batch_replicate, which deaggregates a batch into new rows. "
            "Configure it as an aggregation with output_mode: transform so replicated rows create new downstream tokens."
        )

    return None


def _batch_distribution_profile_contract_options(node: NodeSpec) -> Mapping[str, Any]:
    if node.node_type != "aggregation":
        return node.options
    contract_options, _owner = get_aggregation_contract_options(node.options, owner=f"node:{node.id}")
    return contract_options


def _batch_distribution_profile_value_field_message(
    *,
    value_field: str,
    field_type: str,
) -> str:
    return (
        f"batch_distribution_profile.value_field '{value_field}' is numeric-only, "
        f"but upstream declares type {field_type} "
        "(batch_distribution_profile.value_field.numeric). "
        "Categorical distributions, barrier counts, and theme frequency should use batch_top_k "
        "with field set to the categorical column and group_by as needed, not batch_distribution_profile."
    )


def _producer_declared_field_type(
    producer_id: str,
    plugin_name: str | None,
    options: Mapping[str, Any],
    *,
    node_by_id: Mapping[str, NodeSpec],
    field_name: str,
) -> str | None:
    """Return a declared schema field type for a producer, or None when unknown."""
    is_source_producer = producer_id == "source" or producer_id.startswith("source:")
    owner = producer_id if is_source_producer else f"node:{producer_id}"
    raw_schema = get_raw_schema_config(options, owner=owner)
    if raw_schema is not None and raw_schema.fields is not None:
        for field in raw_schema.fields:
            if field.name == field_name:
                return field.field_type
        return None

    if is_source_producer:
        return None

    if producer_id not in node_by_id:
        return None
    producer_node = node_by_id[producer_id]
    if producer_node.plugin is None:
        return None
    if producer_node.node_type not in {"transform", "aggregation"}:
        return None

    try:
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        transform = get_shared_plugin_manager().create_transform(
            producer_node.plugin,
            prepare_validation_probe_options(producer_node.options),
        )
    except Exception as exc:
        if _is_config_probe_exception(exc):
            return None
        raise

    try:
        output_schema = transform._output_schema_config
        if output_schema is None or output_schema.fields is None:
            return None
        for field in output_schema.fields:
            if field.name == field_name:
                return field.field_type
        return None
    finally:
        transform.close()


# ``PluginManager._raise_if_invalid`` reports a failed plugin config as a bare
# ``ValueError`` whose message it builds as
# ``f"Invalid configuration for {label} '{name}':\n..."``. There is no type to
# match on, so this prefix is the only signal separating an expected draft
# config from a genuine engine defect. Coupled to that raise site: changing the
# message there must change these constants.
#
# The label is part of the prefix on purpose. Each probe knows which factory it
# called, so it tolerates only that factory's draft-config failure: a transform
# probe that swallowed "Invalid configuration for sink" would be swallowing an
# error it could not have produced, which is the shape of a masked defect.
_TRANSFORM_CONFIG_ERROR_PREFIX = "Invalid configuration for transform "
_SINK_CONFIG_ERROR_PREFIX = "Invalid configuration for sink "
_SOURCE_CONFIG_ERROR_PREFIX = "Invalid configuration for source "


# Repair advice for the two per-transform contract rules in
# ``_check_schema_contracts``. ``tools.generation`` imports these for its
# code-keyed catalogue rather than restating them, and
# ``test_transform_contract_advice_has_exactly_one_owner`` fails if a copy
# reappears there.
#
# The invariant is that the two surfaces do not CONTRADICT, not that they are
# identical: the message additionally carries per-target advice computed from
# the authored mapping, which a code-keyed catalogue cannot hold. So each text
# must stand alone on the path that reads it. In particular this catalogue text
# must never say "see the message" — the repair turn is the only reader of it
# and receives no message. That exact shape is a known live failure: the
# ``_WITHHELD_VALIDATION_GUIDANCE`` docstring below records
# ``plugin_options_invalid``'s fix opening with "Apply exactly what 'detail'
# names" when no detail was present, which sent live planners chasing a field
# that did not exist and burned the repair budget.
#
# Why single ownership is load-bearing rather than tidy (elspeth-920bd88299).
# The two surfaces reach the planner on DISJOINT paths, so a divergence is
# invisible to whoever writes it:
#
# * the tool-call path (``upsert_node`` / ``preview_pipeline``) returns the
#   rendered message below and never the catalogue text;
# * the one-shot planner's REPAIR TURN gets only the catalogue text, because
#   ``pipeline_planner._allowlisted_candidate_feedback`` projects
#   ``explanation``/``suggested_fix`` from the ``error_code`` alone and withholds
#   the message (a custody boundary — the message quotes authored option values).
#
# 88137581b rewrote the message here and left the catalogue on advice authored
# in 7015a561f a month earlier, so the planner alternated remedy sets depending
# on how it had learned of the error and could not converge. Neither surface's
# text was pinned by any test, which is why the drift landed green.
_TRANSFORM_DECLARED_NOT_GUARANTEED_EXPLANATION: Final[str] = (
    "A field_mapper with select_only: true declares a required output field that its own mapping cannot guarantee. "
    "The rejection's contract facts name the node and the declared-but-unguaranteed field names, which are mapping "
    "TARGETS. Because a node's `schema` block is its INPUT contract, a target name is usually absent from "
    "`schema.fields` altogether, so an instruction to remove it from the declaration has nothing to act on."
)
_TRANSFORM_DECLARED_NOT_GUARANTEED_FIX: Final[str] = (
    "Change ONLY that node, and remember `schema` is ALSO its runtime input model: under `mode: fixed` a repair "
    "must leave every field a row actually carries declared, or the row is rejected before the mapping runs. Each "
    "name in `missing_fields` is a mapping TARGET, so first look up its SOURCE in the `mapping` you authored — "
    "what repairs one kind of source is inert for another. If the source contains a dot it is a nested read that "
    "`schema.guaranteed_fields` cannot name: set `strict: true`, which is node-wide and routes any row missing ANY "
    "mapped source to on_error, and if `schema.mode` is `fixed` set it to `flexible` in the same edit — fixed mode "
    "rejects the nested read's top-level container field as an undeclared extra, so `strict: true` alone clears "
    "this error and then fails every row at input validation. Otherwise, IF you are certain the source is spelled "
    "exactly as the upstream row keys it, declare it in `schema.fields` AND name it in `schema.guaranteed_fields` "
    "AND remove the target's own entry from `schema.fields` — all three, since leaving the target declared makes "
    "the node demand the emitted name as an input it never receives. If you are not certain of that spelling, do "
    "NOT guess: a source the row is not keyed by is accepted by both of those declarations and changes nothing, "
    "returning this same error. Withdraw the guarantee instead — make the target optional by appending `?` to its "
    "declared type in `schema.fields` (do not delete the entry: a fixed/flexible schema must keep at least one "
    "field, so deleting the last one is rejected), drop it from `schema.guaranteed_fields` if named there, and if "
    "`schema.mode` is `fixed` set it to `flexible` so the key the row actually arrives under passes input "
    "validation. A consumer that needs the field then rejects at its own edge rather than here. Or repair it end "
    "to end: rename the field upstream so the row is keyed by a normalization-stable spelling, rewrite the mapping "
    "key to that SAME spelling, and then declare and guarantee it. Never rewrite the mapping key alone to make it "
    "look right: that is accepted silently and drops the column."
)
_TRANSFORM_OUTPUT_COLLISION_EXPLANATION: Final[str] = (
    "A transform declares an output field that already arrives on its input row. The engine rejects a transform that "
    "would overwrite an existing input field, so the run fails on the first row. The rejection's contract facts name "
    "the node and the colliding field names."
)
_TRANSFORM_OUTPUT_COLLISION_FIX: Final[str] = (
    "Change ONLY that node's output name, or the field upstream of it: rename this transform's output to a name the "
    "row does not already carry (for an llm transform that is `response_field`), OR rename/drop the incoming field "
    "upstream with a field_mapper before this node."
)
_PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_EXPLANATION: Final[str] = (
    "A single-prompt llm node's prompt_template reads row fields its own options.required_input_fields does not "
    "declare. That declaration IS the node's input contract: it is what edge validation checks against the upstream "
    "producer's guarantees and what the engine verifies on every row. A reference outside it is required by nothing, "
    "so no producer is obliged to supply the field, and a row that arrives without it fails the whole node at render "
    "with 'Undefined variable' — an unattributed template error rather than a named contract violation. The rejection "
    "names the node, the fields read, and the fields declared."
)
# The FIX text leads with rewrite-the-reference DELIBERATELY, and the ordering
# is the finding, not a style choice. Declaring the read name is correct only
# when the producer guarantees that exact spelling, and neither authoring layer
# can tell: ``SchemaContract.find_name`` matches a field's ``normalized_name``
# OR its ``original_name``, so ``{{ row.Name }}`` may resolve against a header
# ``Name`` while the row key is ``name``, and a ``field_mapper`` rename leaves
# an ``original_name`` no declaration can carry. ``verify_declared_required_fields``
# is a plain set difference over row keys with NO dual-name limb — measured,
# declaring the read name in either shape is ACCEPTED at config time and then
# raises ``DeclaredRequiredInputFieldsViolation`` on EVERY row. Leading with it
# would hand the planner a repair that clears this error and breaks the run.
_PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_FIX: Final[str] = (
    "Change ONLY that node. Rewrite each reference to a field the node already declares — that always applies, and a "
    "spelling the declaration does not carry works at best by accident of the producer's original header, so "
    "'correcting' the declaration to match a template typo moves the failure rather than clearing it. Add a name to "
    "options.required_input_fields ONLY if the upstream producer guarantees that exact name: declaring one it does "
    "not guarantee is accepted here and then fails every row at run time with a declared-required-fields violation. "
    'Where the rejection shows a parenthesised form, declare THAT — a bracket literal such as row["Original Header"] '
    "is not a legal declaration entry and is rejected on application. Send the full list when you patch: "
    "patch_node_options replaces the option's value, it does not append. Do not answer this by emptying "
    "required_input_fields: [] withdraws the contract for every field the node reads, including the unconditional ones."
)


def _is_plugin_config_probe_exception(exc: Exception, *, config_error_prefix: str) -> bool:
    """Return True only for expected draft/config failures from probe construction.

    The single probe-tolerance taxonomy for this module and for every other
    composer consumer (``guided.emitters``, ``_semantic_validator``), which
    reuse it rather than restating it. Composer probes construct plugins from in-progress composer/
    LLM/user-authored config, so a config, lookup, or template failure is
    ordinary external input and the caller abstains. Anything else is a
    genuine engine defect and must crash through.

    The typed exceptions are shared across plugin kinds — a missing plugin or a
    bad template is the same draft-config event whichever factory raised it.
    Only the untyped ``ValueError`` needs the per-kind prefix.
    """
    from elspeth.plugins.infrastructure.config_base import PluginConfigError
    from elspeth.plugins.infrastructure.manager import PluginNotFoundError
    from elspeth.plugins.infrastructure.templates import TemplateError
    from elspeth.plugins.infrastructure.validation import UnknownPluginTypeError

    if isinstance(exc, (PluginConfigError, PluginNotFoundError, TemplateError, UnknownPluginTypeError)):
        return True
    return type(exc) is ValueError and str(exc).startswith(config_error_prefix)


def _is_config_probe_exception(exc: Exception) -> bool:
    """Transform-probe tolerance — the established name, unchanged in behaviour."""
    return _is_plugin_config_probe_exception(exc, config_error_prefix=_TRANSFORM_CONFIG_ERROR_PREFIX)


def _is_sink_config_probe_exception(exc: Exception) -> bool:
    """Sink-probe tolerance.

    Verified against the live registry rather than assumed to match the
    transform set: a partial ``text`` config (missing ``schema``, a ``field``
    that is not an identifier, an unsupported ``encoding``) all surface as the
    prefixed ``ValueError``, and an unregistered sink name as
    ``UnknownPluginTypeError``.
    """
    return _is_plugin_config_probe_exception(exc, config_error_prefix=_SINK_CONFIG_ERROR_PREFIX)


def _is_source_config_probe_exception(exc: Exception) -> bool:
    """Source-probe tolerance."""
    return _is_plugin_config_probe_exception(exc, config_error_prefix=_SOURCE_CONFIG_ERROR_PREFIX)


def _batch_distribution_profile_value_field_entries(
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
) -> tuple[tuple[ValidationEntry, ...], tuple[ValidationEntry, ...]]:
    """Validate numeric-only batch_distribution_profile value_field contracts."""
    from elspeth.web.composer._producer_resolver import ProducerResolver

    errors: list[ValidationEntry] = []
    warnings: list[ValidationEntry] = []
    node_by_id = {node.id: node for node in nodes}
    resolver = ProducerResolver.build(
        source=None,
        sources=sources,
        nodes=nodes,
        sink_names=frozenset(),
    )
    numeric_types = {"int", "float"}

    for node in nodes:
        if node.plugin != "batch_distribution_profile":
            continue
        options = _batch_distribution_profile_contract_options(node)
        if "value_field" not in options:
            continue
        value_field = options["value_field"]
        if type(value_field) is not str or not value_field.strip():
            continue
        value_field = value_field.strip()

        producer = resolver.walk_to_real_producer(node.input)
        if producer is None:
            warnings.append(
                ValidationEntry(
                    f"node:{node.id}",
                    (
                        f"batch_distribution_profile.value_field '{value_field}' is numeric-only, "
                        "but the upstream producer is unresolved "
                        "(batch_distribution_profile.value_field.numeric). "
                        "If this field is categorical, use batch_top_k instead."
                    ),
                    "high",
                )
            )
            continue

        field_type = _producer_declared_field_type(
            producer.producer_id,
            producer.plugin_name,
            producer.options,
            node_by_id=node_by_id,
            field_name=value_field,
        )
        if field_type is None:
            warnings.append(
                ValidationEntry(
                    f"node:{node.id}",
                    (
                        f"batch_distribution_profile.value_field '{value_field}' is numeric-only, "
                        "but upstream schema is observed or does not declare the field type. "
                        "Inspect a data sample before execute "
                        "(batch_distribution_profile.value_field.numeric); "
                        "categorical distributions should use batch_top_k."
                    ),
                    "high",
                )
            )
            continue
        if field_type in numeric_types:
            continue
        errors.append(
            ValidationEntry(
                f"node:{node.id}",
                _batch_distribution_profile_value_field_message(
                    value_field=value_field,
                    field_type=field_type,
                ),
                "high",
                "batch_value_field_not_numeric",
            )
        )

    return tuple(errors), tuple(warnings)


def _runtime_connection_targets(
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
) -> set[str]:
    """Collect runtime routing targets from connection fields.

    Stage 1 validity must follow the same routing model as generate_yaml()
    and DAG build: source/node connection fields define runtime topology, while
    non-sink UI edges are advisory/editor state.
    """
    targets: set[str] = set()
    for source in sources.values():
        targets.add(source.on_success)
    for node in nodes:
        if node.node_type == "coalesce" and node.on_success is None:
            targets.add(node.id)
        elif node.on_success is not None:
            targets.add(node.on_success)
        if node.on_error is not None and node.on_error != "discard":
            targets.add(node.on_error)
        if node.routes is not None:
            targets.update(target for target in node.routes.values() if target != _DISCARD_ROUTE_TARGET)
        if node.fork_to is not None:
            targets.update(node.fork_to)
    return targets


def _runtime_consumer_connections(nodes: tuple[NodeSpec, ...]) -> set[str]:
    """Return connection names runtime can resolve to processing nodes."""
    consumers = {node.input for node in nodes if node.node_type not in ("coalesce", "row_union")}
    for node in nodes:
        if node.node_type == "coalesce" and node.branches is not None:
            consumers.update(_coalesce_branch_connections(node.branches))
        elif node.node_type == "row_union" and node.branches is not None:
            # NodeSpec.input is only the serialized adapter placeholder for a
            # row_union. Every declared branch value is a real consuming
            # binding, including identity branches.
            consumers.update(_coalesce_branch_connections(node.branches))
    return consumers


def _runtime_connection_is_downstream(
    origin: str,
    target: str,
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
) -> bool:
    """Return whether ``target`` is exclusively derived from ``origin``."""
    is_downstream, _lineage = _runtime_connection_lineage(origin, target, sources, nodes)
    return is_downstream


def _runtime_connection_lineage(
    origin: str,
    target: str,
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
) -> tuple[bool, tuple[NodeSpec, ...]]:
    """Return exclusive lineage from ``origin`` to ``target``.

    Ordinary connections have one producer (the duplicate-producer check owns
    ambiguity), so one proven predecessor establishes lineage. A queue is the
    deliberate exception: it can have many producers, and every predecessor
    must derive from the mapped fork alias. Otherwise one valid branch path
    could hide unrelated queue traffic and bypass row_union correlation.

    The lineage excludes the producer of ``origin`` itself. For a fork alias,
    this makes the originating fork gate the traversal boundary while retaining
    every node inside the branch for row_union hazard checks.
    """
    from elspeth.web.composer._producer_resolver import ProducerEntry, ProducerResolver, is_source_producer_id

    resolver = ProducerResolver.build(
        source=None,
        sources=sources,
        nodes=nodes,
        sink_names=frozenset(),
    )

    def _producer_is_compatible(
        producer: ProducerEntry,
        *,
        visiting: frozenset[str],
    ) -> tuple[bool, frozenset[str]]:
        if is_source_producer_id(producer.producer_id):
            return False, frozenset()
        producer_node = resolver.get_node(producer.producer_id)
        if producer_node is None:
            return False, frozenset()
        if producer_node.node_type in ("coalesce", "row_union"):
            dependencies = _coalesce_branch_connections(producer_node.branches)
            if not dependencies:
                return False, frozenset()
            lineage = frozenset((producer_node.id,))
            for dependency in dependencies:
                is_compatible, dependency_lineage = _connection_is_compatible(dependency, visiting=visiting)
                if not is_compatible:
                    return False, frozenset()
                lineage |= dependency_lineage
            return True, lineage
        if producer_node.node_type == "queue":
            # Queue compatibility is resolved through every registered
            # predecessor in _connection_is_compatible(), never through the
            # queue's structural input placeholder.
            return _connection_is_compatible(producer_node.id, visiting=visiting)
        is_compatible, lineage = _connection_is_compatible(producer_node.input, visiting=visiting)
        if not is_compatible:
            return False, frozenset()
        return True, lineage | {producer_node.id}

    def _connection_is_compatible(
        connection_name: str,
        *,
        visiting: frozenset[str],
    ) -> tuple[bool, frozenset[str]]:
        if connection_name == origin:
            return True, frozenset()
        if connection_name in visiting:
            return False, frozenset()
        next_visiting = visiting | {connection_name}
        producer = resolver.find_producer_for(connection_name)
        if producer is None:
            return False, frozenset()
        producer_node = resolver.get_node(producer.producer_id)
        if producer_node is not None and producer_node.node_type == "queue":
            predecessors = resolver.queue_predecessors(producer_node.id)
            if not predecessors:
                return False, frozenset()
            lineage = frozenset((producer_node.id,))
            for predecessor in predecessors:
                is_compatible, predecessor_lineage = _producer_is_compatible(predecessor, visiting=next_visiting)
                if not is_compatible:
                    return False, frozenset()
                lineage |= predecessor_lineage
            return True, lineage
        return _producer_is_compatible(producer, visiting=next_visiting)

    is_compatible, lineage_ids = _connection_is_compatible(target, visiting=frozenset())
    if not is_compatible:
        return False, ()
    return True, tuple(node for node in nodes if node.id in lineage_ids)


def _runtime_nodes_downstream_of_connection(
    connection_name: str,
    nodes: tuple[NodeSpec, ...],
) -> tuple[NodeSpec, ...]:
    """Return processing nodes reachable from a runtime connection.

    Mirrors the runtime builder's forward graph walk closely enough for the
    row_union group-indivisibility guard: all success/error/route/fork outputs
    are traversed, identity and mapped barrier inputs are both topology edges,
    and structural queue placeholders are skipped.
    """
    reachable_connections = {connection_name}
    reachable_node_ids: set[str] = set()
    ordered_nodes: list[NodeSpec] = []
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.id in reachable_node_ids or node.node_type == "queue":
                continue
            inputs = _coalesce_branch_connections(node.branches) if node.node_type in ("coalesce", "row_union") else (node.input,)
            if reachable_connections.isdisjoint(inputs):
                continue
            reachable_node_ids.add(node.id)
            ordered_nodes.append(node)
            changed = True
            if node.node_type == "coalesce" and node.on_success is None:
                reachable_connections.add(node.id)
            elif node.on_success is not None:
                reachable_connections.add(node.on_success)
            if node.on_error is not None and node.on_error != _DISCARD_ROUTE_TARGET:
                reachable_connections.add(node.on_error)
            if node.routes is not None:
                reachable_connections.update(
                    target for target in node.routes.values() if target not in (_DISCARD_ROUTE_TARGET, _FORK_ROUTE_TARGET)
                )
            if node.fork_to is not None:
                reachable_connections.update(node.fork_to)
    return tuple(ordered_nodes)


def _closer_backward_reach_connections(nodes: tuple[NodeSpec, ...], closer_node: NodeSpec) -> set[str]:
    """Connections that (transitively) reach the closer via non-DIVERT edges.

    Composer-side counterpart of core/dag/bound_regions.py's
    ``_backward_reach`` (spec §7 rule 4, F1 fix): used to widen the
    fork-branch forward-walk anchor the same way the runtime does — a
    gate's non-roster route target still counts as a legitimate branch
    start when that connection is itself backward-reachable from the
    closer (e.g. an intermediate non-fork gate re-entering the region
    before reaching the closer). Seeded from the closer's own declared
    branch connections and expanded backward through every node's
    non-DIVERT published connections (``on_error`` excluded, matching this
    module's own forward walk — deliberately NOT ``_node_published_connections``,
    which includes ``on_error``).
    """
    target_connections = set(_coalesce_branch_connections(closer_node.branches))
    seen_node_ids: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.id in seen_node_ids or node.id == closer_node.id:
                continue
            published: set[str] = set()
            if node.node_type == "coalesce" and node.on_success is None:
                published.add(node.id)
            elif node.on_success is not None:
                published.add(node.on_success)
            if node.routes is not None:
                published.update(target for target in node.routes.values() if target not in (_DISCARD_ROUTE_TARGET, _FORK_ROUTE_TARGET))
            if node.fork_to is not None:
                published.update(node.fork_to)
            if published.isdisjoint(target_connections):
                continue
            seen_node_ids.add(node.id)
            changed = True
            inputs = _coalesce_branch_connections(node.branches) if node.node_type in ("coalesce", "row_union") else (node.input,)
            target_connections.update(inputs)
    return target_connections


def _fork_branch_reaches_sink_before_closer(
    fork_branches: Sequence[str],
    closer_id: str,
    nodes: tuple[NodeSpec, ...],
    sink_names: set[str],
) -> str | None:
    """Stage-1 mirror of the SINK-inside-region forward walk (spec §7 rule 4,
    sink limb only — the backward walk and no-path limb are `abstains`, see
    generation.py's catalogue note).

    Walks non-DIVERT edges (``on_error`` excluded — pinned decision 1)
    forward from a whole-roster-bound fork's own branch connections. Does
    not expand past ``closer_id``: a bound region's only legal exit is its
    closer, so anything reached from a branch OTHER than the closer itself
    is still "inside" the region for this check, mirroring
    ``_runtime_nodes_downstream_of_connection``'s edge model but stopping at
    the closer and dropping the ``on_error`` edge it otherwise follows.
    Traverses queue nodes like any other node (review F5, 2026-08-23 fix
    round): a queue's own id IS the connection its producers publish to and
    its consumers read from, so admitting it into the walk costs nothing —
    excluding it previously left the mirror unable to pick up a sink reached
    downstream of an in-region queue in every topology shape that isn't
    already covered by a direct producer/consumer match.

    ``fork_branches`` is the caller's WIDENED seed set (roster branches plus
    any of the gate's other route targets that are backward-reachable from
    the closer — review F1's anchor fix, mirrored here since the runtime and
    Stage-1 shared the same narrow-anchor blind spot).

    Requires the closer to ALSO be reached by the same walk, or returns
    None even when a sink was hit: a branch that never reaches the closer
    at all is the "no path to closer" limb (Stage-1 abstains — the roster's
    declared VALUES having no producer at all is
    ``coalesce_branch_unreachable``/``row_union_branch_unreachable``'s job,
    already a more specific, planner-actionable diagnostic — see
    guided-incident regression `test_orphaned_coalesce_rejects_with_the_
    single_observed_code`). Firing here too would silently duplicate that
    single-code guarantee with a less specific message.
    """
    reachable_connections = set(fork_branches)
    visited_node_ids: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.id in visited_node_ids:
                continue
            inputs = _coalesce_branch_connections(node.branches) if node.node_type in ("coalesce", "row_union") else (node.input,)
            if reachable_connections.isdisjoint(inputs):
                continue
            visited_node_ids.add(node.id)
            changed = True
            if node.id == closer_id:
                continue
            if node.node_type == "coalesce" and node.on_success is None:
                reachable_connections.add(node.id)
            elif node.on_success is not None:
                reachable_connections.add(node.on_success)
            if node.routes is not None:
                reachable_connections.update(
                    target for target in node.routes.values() if target not in (_DISCARD_ROUTE_TARGET, _FORK_ROUTE_TARGET)
                )
            if node.fork_to is not None:
                reachable_connections.update(node.fork_to)
    if closer_id not in visited_node_ids:
        return None
    sink_hits = sorted(reachable_connections & sink_names)
    return sink_hits[0] if sink_hits else None


def _node_published_connections(node: NodeSpec) -> frozenset[str]:
    """Connections a node publishes rows to — the forward edges of the runtime graph walk.

    Mirrors :func:`_runtime_nodes_downstream_of_connection`'s edge model
    exactly: a coalesce without ``on_success`` publishes under its own id;
    ``on_error`` and gate ``routes`` skip the ``discard``/``fork`` keywords;
    ``fork_to`` branch names are connections. Sinks are terminal and never
    consume, so a sink-named target contributes no node edge.
    """
    published: set[str] = set()
    if node.node_type == "coalesce" and node.on_success is None:
        published.add(node.id)
    elif node.on_success is not None:
        published.add(node.on_success)
    if node.on_error is not None and node.on_error != _DISCARD_ROUTE_TARGET:
        published.add(node.on_error)
    if node.routes is not None:
        published.update(target for target in node.routes.values() if target not in (_DISCARD_ROUTE_TARGET, _FORK_ROUTE_TARGET))
    if node.fork_to is not None:
        published.update(node.fork_to)
    return frozenset(published)


def _node_topology_cycle(nodes: tuple[NodeSpec, ...]) -> tuple[str, ...] | None:
    """Return one processing-node cycle as an ordered id path, or ``None`` when acyclic.

    Mirrors ``ExecutionGraph.validate()``'s cycle rejection
    (``core/dag/graph.py``, ``nx.find_cycle``) on the composer's connection
    model. Stage 1 had NO cycle detection (elspeth-2ed41f0a4a): the per-node
    "input has a producer" check is satisfied on every node of a cycle, so
    ``t1.input=b, t1.on_success=c; t2.input=c, t2.on_success=b`` validated
    green with no warning and died at the DAG build.

    Edges are node -> node through connections: ``a -> b`` when ``a``
    publishes a connection ``b`` consumes (``input`` for ordinary nodes,
    ``branches`` values for coalesce/row_union). A structural queue is
    transparent, exactly as in the downstream walker — its producers reach its
    consumers directly. Iterative three-colour DFS; the returned path starts
    and ends on the same node so the message reads like the runtime's
    ``a -> b -> a``.
    """
    consumers_by_connection: dict[str, list[str]] = {}
    for node in nodes:
        if node.node_type == "queue":
            continue
        inputs = _coalesce_branch_connections(node.branches) if node.node_type in ("coalesce", "row_union") else (node.input,)
        for connection in inputs:
            if connection is not None:
                consumers_by_connection.setdefault(connection, []).append(node.id)
    successors: dict[str, list[str]] = {}
    for node in nodes:
        if node.node_type == "queue":
            continue
        targets: list[str] = []
        for connection in sorted(_node_published_connections(node)):
            targets.extend(consumers_by_connection.get(connection, ()))
        successors[node.id] = targets

    white, grey, black = 0, 1, 2
    colour: dict[str, int] = dict.fromkeys(successors, white)
    for root in successors:
        if colour[root] != white:
            continue
        path: list[str] = [root]
        cursors: list[int] = [0]
        colour[root] = grey
        while path:
            current = path[-1]
            index = cursors[-1]
            children = successors[current]
            if index < len(children):
                cursors[-1] = index + 1
                child = children[index]
                if colour[child] == grey:
                    start = path.index(child)
                    return (*path[start:], child)
                if colour[child] == white:
                    colour[child] = grey
                    path.append(child)
                    cursors.append(0)
                continue
            colour[current] = black
            path.pop()
            cursors.pop()
    return None


def coalesce_reachability_facts(state: CompositionState) -> dict[str, dict[str, Any]]:
    """Redaction-safe wiring facts for coalesce branch-reachability rejections.

    Maps each coalesce node id whose ``branches`` values name connections no
    runtime routing field produces to the facts a repair needs:
    ``unreachable_branches`` (branch key -> consumed connection value, exactly
    as authored — list-form branches key by the entry itself) and
    ``produced_connections`` (the membership set ``validate()``'s
    ``coalesce_branch_unreachable`` check tests, minus sink names and the
    coalesce's own published id — both pass the walk but are never a correct
    branch value, so the facts must not steer a repair toward them).

    Guided session 277fb6c4 (2026-07-22) exhausted its repair budget on four
    identical ``coalesce_branch_unreachable`` rejections: the observed
    miswiring — branch transforms publishing straight to the sink — is
    invisible from the bare code, and the planner's repair feedback strips
    raw messages. Everything here is a node id or connection name the
    session owner / planner authored — the same redaction judgment as
    ``SchemaContractDetail`` — so forwarding it through the message-stripped
    repair feedback does not re-open the redaction boundary.

    ``sink_targeting_branches`` names the lure explicitly: for each
    unreachable branch whose branch-side transform CHAIN terminates in a
    sink-publishing hop, the entry carries that transform's id, the sink it
    publishes to, and the connection the coalesce expects instead. Guided
    attempt 14 (session 04200b45) re-wired branch transforms to the
    reviewed sink three times WITH the bare facts live — the repair needs
    the exact miswired node named.
    """
    targets = _runtime_connection_targets(state.sources, state.nodes)
    sink_names = {output.name for output in state.outputs}
    transform_by_input: dict[str, NodeSpec] = {}
    for node in state.nodes:
        if node.node_type == "transform" and node.input not in transform_by_input:
            transform_by_input[node.input] = node

    def _sink_lure(branch_key: str) -> tuple[str, str] | None:
        """Follow the transform chain consuming ``branch_key`` to a sink hop."""
        connection = branch_key
        for _ in range(len(state.nodes)):
            consumer = transform_by_input.get(connection)
            if consumer is None or consumer.on_success is None:
                return None
            if consumer.on_success in sink_names:
                return consumer.id, consumer.on_success
            connection = consumer.on_success
        return None

    facts: dict[str, dict[str, Any]] = {}
    for node in state.nodes:
        if node.node_type != "coalesce" or node.branches is None:
            continue
        unreachable: dict[str, str] = {}
        sink_targeting: list[dict[str, str]] = []
        for branch_name, branch_connection in zip(
            _coalesce_branch_names(node.branches),
            _coalesce_branch_connections(node.branches),
            strict=True,
        ):
            if branch_connection in targets:
                continue
            unreachable[str(branch_name)] = branch_connection
            lure = _sink_lure(str(branch_name))
            if lure is not None:
                sink_targeting.append({"node_id": lure[0], "on_success_sink": lure[1], "expected_connection": branch_connection})
        if not unreachable:
            continue
        entry: dict[str, Any] = {
            "unreachable_branches": unreachable,
            # _FORK_ROUTE_TARGET is the reserved route keyword ("go to
            # fork_to"), not a connection — it rides the membership set but
            # must not be advertised as a wirable name.
            "produced_connections": sorted(targets - sink_names - {node.id, _FORK_ROUTE_TARGET}),
        }
        if sink_targeting:
            entry["sink_targeting_branches"] = sink_targeting
        facts[node.id] = entry
    return facts


class RouteDestinationFactDict(TypedDict):
    """Redaction-safe repair facts for one unresolved route destination."""

    dangling_on_success: NotRequired[str]
    dangling_on_error: NotRequired[str]
    declared_sinks: list[str]
    consumable_connections: NotRequired[list[str]]


def route_destination_facts(state: CompositionState) -> dict[str, RouteDestinationFactDict]:
    """Redaction-safe wiring facts for dangling routing-destination rejections.

    Maps each component whose ``on_success`` / ``on_error`` names a destination
    that ``_validate_runtime_route_destinations`` cannot resolve — keyed exactly
    as those validation entries name their component (``source`` /
    ``source:<name>`` / ``node:<id>``) — to the facts a repair needs:
    the dangling value itself, ``declared_sinks`` (the candidate's
    ``outputs[].sink_name`` set), and for on_success failures
    ``consumable_connections`` (the connections downstream nodes read as their
    input). ``on_error`` may only target a sink or — from inside a bound
    region — that region's closer (spec §7 rule 9; a closer-shaped
    ``on_error`` is accepted, never dangling, so it never reaches this
    dict). Coalesce ``on_success`` may only target sinks. Neither carries
    ``consumable_connections``.

    AWS acceptance runs 2026-07-30 (ticket elspeth-5904b1683a): the canonical
    CSV-to-JSON prompt intermittently exhausted its repair budget on
    ``source_on_success_dangling`` because the bare code names neither the
    value that dangled nor the sink it should have matched, and the static
    guidance pointed at ``get_pipeline_state`` — which reads the BASELINE
    session state (empty on a fresh compose), not the rejected candidate.
    Everything here is a sink name, node id, or connection name the planner
    itself authored in the rejected candidate — the same redaction judgment as
    :func:`coalesce_reachability_facts` — so forwarding it through the
    message-stripped repair feedback does not re-open the redaction boundary.
    """
    output_names = {output.name for output in state.outputs}
    consumer_connections = _runtime_consumer_connections(state.nodes)
    declared_sinks = sorted(output_names)
    consumable = sorted(consumer_connections)
    # Same rule-9 relax as _validate_runtime_route_destinations, kept in
    # sync: a closer-shaped on_error is no longer dangling, so it must not
    # generate a "dangling_on_error" repair fact either.
    closer_names = {node.id for node in state.nodes if node.node_type in ("coalesce", "row_union")}
    facts: dict[str, RouteDestinationFactDict] = {}

    def _merge(component: str, entry: RouteDestinationFactDict) -> None:
        if component in facts:
            facts[component].update(entry)
        else:
            facts[component] = entry

    for source_name, source in state.sources.items():
        target = source.on_success
        if target not in output_names and target not in consumer_connections:
            component = "source" if source_name == "source" else f"source:{source_name}"
            _merge(
                component,
                {
                    "dangling_on_success": target,
                    "declared_sinks": declared_sinks,
                    "consumable_connections": consumable,
                },
            )

    for node in state.nodes:
        component = f"node:{node.id}"
        if node.node_type in ("transform", "aggregation", "row_union", "gate"):
            if node.on_success is not None and node.on_success not in output_names and node.on_success not in consumer_connections:
                _merge(
                    component,
                    {
                        "dangling_on_success": node.on_success,
                        "declared_sinks": declared_sinks,
                        "consumable_connections": consumable,
                    },
                )
            node_on_error = node.on_error
            if (
                node.node_type in ("transform", "gate")
                and node_on_error is not None
                and node_on_error != "discard"
                and node_on_error not in output_names
                and node_on_error not in closer_names
            ):
                _merge(
                    component,
                    {
                        "dangling_on_error": node_on_error,
                        "declared_sinks": declared_sinks,
                    },
                )
        elif (
            node.node_type == "coalesce"
            and node.on_success is not None
            and node.on_success not in output_names
            and node.on_success not in consumer_connections
        ):
            # coalesce_on_success_unknown_sink: the destination must be a sink.
            _merge(
                component,
                {
                    "dangling_on_success": node.on_success,
                    "declared_sinks": declared_sinks,
                },
            )
    return facts


def _validate_runtime_route_destinations(
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
    outputs: tuple[OutputSpec, ...],
) -> tuple[ValidationEntry, ...]:
    """Mirror runtime DAG routing destination checks for terminal fields."""
    errors: list[ValidationEntry] = []
    output_names = {output.name for output in outputs}
    consumer_connections = _runtime_consumer_connections(nodes)
    # Spec §7 rule 9 (Task 11): on_error may target the ENCLOSING bound
    # region's closer (coalesce/row_union), not only a sink. Stage 1 does
    # not compute bound regions at all (established abstention, Task 7's
    # own parity note), so this RELAXES rather than mirrors the runtime's
    # region-membership check: any real coalesce/row_union node name is
    # accepted unconditionally, whether or not this node's chain actually
    # lies inside that closer's region. Accepting is the safe drift
    # direction — Stage 2 preview_pipeline runs the real builder and
    # rejects a genuinely out-of-region target with the runtime message;
    # rejecting a legal in-region target here instead would be
    # composer-red/runtime-green, the drift that strands the authoring
    # loop. Scope/collector closers are not in this set: NodeType has no
    # "collector"/"scope" member (Task 10 finding) — not yet
    # composer-authorable, so no scope-closer name can appear in `nodes`.
    closer_names = {node.id for node in nodes if node.node_type in ("coalesce", "row_union")}
    _err = ValidationEntry

    for source_name, source in sources.items():
        target = source.on_success
        if target not in output_names and target not in consumer_connections:
            component = "source" if source_name == "source" else f"source:{source_name}"
            message = (
                f"Source on_success '{target}' is neither a sink nor a known connection."
                if source_name == "source"
                else f"Source '{source_name}' on_success '{target}' is neither a sink nor a known connection."
            )
            errors.append(
                _err(
                    component,
                    message,
                    "high",
                    "source_on_success_dangling",
                )
            )

    # Mirror the engine's fork-branch destination rule: every gate fork_to
    # name must be a key in a correlated barrier's branches mapping (arrival
    # is tracked by FORK BRANCH NAME, not by the connection that reaches the
    # barrier) or match a sink name exactly.
    barrier_branch_names = {
        str(branch_name)
        for candidate in nodes
        if candidate.node_type in ("coalesce", "row_union") and candidate.branches is not None
        for branch_name in (candidate.branches.keys() if isinstance(candidate.branches, Mapping) else candidate.branches)
    }
    # Fourth path (spec §7 E2): a branch consumed by an ordinary downstream
    # transform/gate `input` is legal pure fan-out — no barrier claims it.
    # A SET, not a count: ambiguity (two nodes sharing the same `input`) is
    # already reported once, clearly, by the duplicate_connection_consumer
    # check below — this predicate only needs "at least one" to admit the
    # branch as having a destination.
    unbound_consumer_fed_inputs = {node.input for node in nodes if node.node_type in ("transform", "gate")}
    for node in nodes:
        if node.node_type == "gate" and node.fork_to:
            for branch in node.fork_to:
                if branch not in barrier_branch_names and branch not in output_names and branch not in unbound_consumer_fed_inputs:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Gate '{node.id}' fork branch '{branch}' has no destination: it must be a key "
                            "in some coalesce/row_union 'branches' mapping, match a sink name exactly, or be "
                            "consumed by exactly one downstream transform/gate 'input'. "
                            f"Barrier branch keys: {sorted(barrier_branch_names)}; sinks: {sorted(output_names)}. "
                            "Key barrier branches by FORK BRANCH NAME, with each value naming the connection "
                            "that arrives at the barrier after any per-branch transforms.",
                            "high",
                            "fork_branch_no_destination",
                        )
                    )

    for node in nodes:
        if node.node_type == "gate":
            if (
                node.on_error is not None
                and node.on_error != "discard"
                and node.on_error not in output_names
                and node.on_error not in closer_names
            ):
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Gate '{node.id}' on_error '{node.on_error}' references unknown sink.",
                        "high",
                        "gate_on_error_unknown_sink",
                    )
                )
            # Mirror the DAG builder's route resolution: every non-reserved
            # route destination must resolve to a sink or a consumed
            # connection, or the routed rows dead-end at graph compilation.
            for route_label, route_target in (node.routes or {}).items():
                if route_target in (_DISCARD_ROUTE_TARGET, _FORK_ROUTE_TARGET):
                    continue
                if route_target not in output_names and route_target not in consumer_connections:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Gate '{node.id}' route '{route_label}' destination '{route_target}' is neither "
                            "a sink nor a known connection.",
                            "high",
                            "gate_route_target_unknown",
                        )
                    )
            continue

        if node.node_type == "transform":
            if node.on_success is not None and node.on_success not in output_names and node.on_success not in consumer_connections:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Transform '{node.id}' on_success '{node.on_success}' is neither a sink nor a known connection.",
                        "high",
                        "transform_on_success_dangling",
                    )
                )
            if (
                node.on_error is not None
                and node.on_error != "discard"
                and node.on_error not in output_names
                and node.on_error not in closer_names
            ):
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Transform '{node.id}' on_error '{node.on_error}' references unknown sink.",
                        "high",
                        "transform_on_error_unknown_sink",
                    )
                )
            continue

        if node.node_type == "aggregation":
            if node.on_success is not None and node.on_success not in output_names and node.on_success not in consumer_connections:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Aggregation '{node.id}' on_success '{node.on_success}' is neither a sink nor a known connection.",
                        "high",
                        "aggregation_on_success_dangling",
                    )
                )
            # AggregationSettings.on_error is a sink name or 'discard'; a
            # failing batch routed to a ghost sink is a deterministic runtime
            # failure the engine only discovers when a batch actually fails.
            if node.on_error is not None and node.on_error != _DISCARD_ROUTE_TARGET and node.on_error not in output_names:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Aggregation '{node.id}' on_error '{node.on_error}' references unknown sink.",
                        "high",
                        "aggregation_on_error_unknown_sink",
                    )
                )
            continue

        if node.node_type == "coalesce":
            if node.on_success is None:
                continue
            if node.on_success in consumer_connections:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Coalesce '{node.id}' has on_success='{node.on_success}'. "
                        "Coalesce on_success must point to a sink when configured.",
                        "high",
                        "coalesce_on_success_must_be_sink",
                    )
                )
            elif node.on_success not in output_names:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Coalesce '{node.id}' on_success references unknown sink '{node.on_success}'.",
                        "high",
                        "coalesce_on_success_unknown_sink",
                    )
                )
            continue

        if node.node_type == "row_union":
            if node.on_success in output_names:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"row_union '{node.id}' on_success '{node.on_success}' names a sink. "
                        "A released group must continue on a processing connection.",
                        "high",
                        "row_union_on_success_must_be_connection",
                    )
                )
            elif node.on_success is not None and node.on_success not in consumer_connections:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"row_union '{node.id}' on_success '{node.on_success}' is not a known processing connection.",
                        "high",
                        "row_union_on_success_dangling",
                    )
                )

    return tuple(errors)


def _validate_gate_expression(condition: str) -> str | None:
    """Validate a gate condition expression at composition time.

    Returns an error message if the expression is syntactically invalid or
    contains forbidden constructs, or None if valid.

    Uses a deferred import to keep the expression-parser dependency local to
    the validation path. The import is L3→L1, which is layer-legal.
    """
    from elspeth.core.expression_parser import (
        ExpressionParser,
        ExpressionSecurityError,
        ExpressionSyntaxError,
    )

    try:
        ExpressionParser(condition)
    except ExpressionSyntaxError as e:
        return f"Invalid gate condition syntax: {e}"
    except ExpressionSecurityError as e:
        return f"Forbidden construct in gate condition: {e}"
    return None


def gate_condition_is_constant(condition: str) -> bool:
    """Return whether a gate condition is a bare literal that reads no row data.

    Diagnosis only — a constant condition is legal and is the documented
    fan-out idiom. Syntactically invalid or forbidden conditions answer False;
    ``_validate_gate_expression`` is the check that rejects those.
    """
    from elspeth.core.expression_parser import (
        ExpressionParser,
        ExpressionSecurityError,
        ExpressionSyntaxError,
    )

    try:
        return ExpressionParser(condition).is_constant_expression()
    except (ExpressionSyntaxError, ExpressionSecurityError):
        return False


def gate_route_destinations(node: NodeSpec, route_target: str) -> frozenset[str]:
    """Resolve one gate route target to the destinations it delivers rows to.

    Mirrors the engine's gate route vocabulary (``core/config.py:768-770``,
    whose composition-time counterpart is the fork-branch destination rule in
    :func:`_validate_runtime_route_destinations`): ``discard`` delivers
    nowhere, ``fork`` delivers to every ``fork_to`` branch, and any other value
    is one named destination. Callers must not re-derive this — the reserved
    keywords are not connection names.
    """
    if route_target == _DISCARD_ROUTE_TARGET:
        return frozenset()
    if route_target == _FORK_ROUTE_TARGET:
        return frozenset(node.fork_to or ())
    return frozenset({route_target})


# --- Edge/route reconciliation contract (elspeth-67b44040ee) -----------------
#
# Visual edges and scalar routing fields describe ONE runtime routing model.
# The scalar fields are the runtime authority (generate_yaml() and DAG build
# read only them); sink-targeting edges are their operator-facing mirror and
# must agree, while node-targeting on_success edges remain advisory editor
# state. The helpers below are the single source of truth for which
# (component kind, edge type, target kind) combinations can lower at all —
# shared by the mutation tools (admission) and validate() (every entry path).

ComponentKind = Literal["source", "output", "transform", "gate", "aggregation", "coalesce", "row_union", "queue"]


def composer_component_kind(
    component_id: str,
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
    outputs: tuple[OutputSpec, ...],
) -> ComponentKind | None:
    """Classify an edge endpoint. None when the id resolves to nothing yet."""
    if component_id in sources or component_id == "source":
        return "source"
    for node in nodes:
        if node.id == component_id:
            return node.node_type
    for output in outputs:
        if output.name == component_id:
            return "output"
    return None


def edge_lowering_error(edge: EdgeSpec, *, from_kind: ComponentKind | None, to_kind: ComponentKind | None) -> str | None:
    """Return why this edge cannot lower to a runtime route, or None.

    Unknown endpoints (kind None) are tolerated for the axes they blind:
    incremental authoring may reference a component before it exists, and the
    edge_unknown_node check owns unresolved ids at Stage 1. Every verdict here
    is therefore based only on facts already present in the state.
    """
    if from_kind is None or from_kind == "output":
        return None
    edge_type = edge.edge_type
    if from_kind == "source":
        if edge_type != "on_success":
            return f"Source edges must use 'on_success'; '{edge_type}' has no source runtime route."
        return None
    if from_kind == "gate":
        if edge_type == "on_success":
            # A labeled route to a connection has no dedicated EdgeType, so an
            # on_success edge into a processing node is the advisory picture
            # for it. A sink target is different: it would demand an
            # on_success scalar a gate does not have.
            if to_kind == "output":
                return f"Gate '{edge.from_node}' sink edges must use route_true, route_false, or fork."
            return None
        if edge_type == "on_error":
            return (
                f"Gate '{edge.from_node}' evaluation-error routing is node-level: use upsert_node with on_error="
                f"'{edge.to_node}' (or 'discard'). upsert_edge(edge_type='on_error') is unsupported for gates."
            )
        return None
    if edge_type in ("route_true", "route_false", "fork"):
        return f"Only gates can use '{edge_type}' edges; '{edge.from_node}' is a {from_kind}."
    if from_kind in ("transform", "aggregation"):
        if edge_type == "on_error" and to_kind is not None and to_kind != "output":
            return (
                f"{from_kind.capitalize()} '{edge.from_node}' on_error must route to a sink or 'discard'; '{edge.to_node}' is a {to_kind}."
            )
        return None
    if from_kind == "coalesce":
        if edge_type == "on_error":
            return f"Coalesce '{edge.from_node}' has no on_error route: coalesce outcomes are governed by its arrival policy."
        return None
    if from_kind == "row_union":
        if edge_type == "on_error":
            return f"row_union '{edge.from_node}' has no on_error route."
        if to_kind == "output":
            return (
                f"row_union '{edge.from_node}' on_success '{edge.to_node}' names a sink. "
                "A released group must continue on a processing connection."
            )
        return None
    # from_kind == "queue"
    if edge_type == "on_error":
        return f"Queue '{edge.from_node}' has no on_error route: queues are structural pass-through points."
    if to_kind == "output":
        return (
            f"Queue '{edge.from_node}' cannot route to sink '{edge.to_node}': queues release rows on the "
            "connection named by their id, and only processing nodes may consume it."
        )
    return None


def sink_edge_route_mismatch(
    edge: EdgeSpec,
    *,
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
) -> str | None:
    """Return why a sink-targeting edge disagrees with its scalar mirror.

    Callers must only pass edges whose to_node is a declared output and whose
    shape already passed edge_lowering_error — this checks agreement, not
    legality. Returns None when the scalar carries the route the edge draws.
    """
    if edge.from_node in sources:
        source = sources[edge.from_node]
        if source.on_success != edge.to_node:
            return f"Edge '{edge.id}' draws source on_success to sink '{edge.to_node}' but the source routes to '{source.on_success}'."
        return None
    node = next((candidate for candidate in nodes if candidate.id == edge.from_node), None)
    if node is None:
        return None
    if edge.edge_type == "on_success":
        if node.on_success != edge.to_node:
            return f"Edge '{edge.id}' draws '{node.id}' on_success to sink '{edge.to_node}' but the node routes to '{node.on_success}'."
        return None
    if edge.edge_type == "on_error":
        if node.on_error != edge.to_node:
            return f"Edge '{edge.id}' draws '{node.id}' on_error to sink '{edge.to_node}' but the node routes to '{node.on_error}'."
        return None
    if edge.edge_type in ("route_true", "route_false"):
        route_key = "true" if edge.edge_type == "route_true" else "false"
        routes = node.routes or {}
        actual = routes[route_key] if route_key in routes else None
        if actual != edge.to_node:
            return (
                f"Edge '{edge.id}' draws gate '{node.id}' route '{route_key}' to sink '{edge.to_node}' "
                f"but the gate routes it to '{actual}'."
            )
        return None
    # fork
    if edge.to_node not in (node.fork_to or ()):
        return f"Edge '{edge.id}' draws gate '{node.id}' fork branch to sink '{edge.to_node}' but fork_to does not include it."
    return None


def _validate_edge_route_contract(
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
    outputs: tuple[OutputSpec, ...],
    edges: tuple[EdgeSpec, ...],
) -> tuple[ValidationEntry, ...]:
    """Stage-1 edge/route contract: lowerability, slot uniqueness, mirror truth.

    Runs on the raw edge tuple so bulk entry paths (set_pipeline, session
    deserialization) meet the same contract as the incremental mutation tools.
    Edges with unresolved endpoints are skipped per axis — edge_unknown_node
    owns those.
    """
    errors: list[ValidationEntry] = []
    _err = ValidationEntry
    output_names = {output.name for output in outputs}

    sink_slot_claims: dict[tuple[str, str], list[str]] = {}
    fork_claims: dict[tuple[str, str], list[str]] = {}
    for edge in edges:
        from_kind = composer_component_kind(edge.from_node, sources, nodes, outputs)
        to_kind = composer_component_kind(edge.to_node, sources, nodes, outputs)
        lowering_error = edge_lowering_error(edge, from_kind=from_kind, to_kind=to_kind)
        if lowering_error is not None:
            errors.append(_err(f"edge:{edge.id}", lowering_error, "high", "edge_not_lowerable"))
            continue
        if edge.to_node not in output_names:
            continue
        if edge.edge_type == "fork":
            fork_claims.setdefault((edge.from_node, edge.to_node), []).append(edge.id)
        else:
            sink_slot_claims.setdefault((edge.from_node, edge.edge_type), []).append(edge.id)
        if from_kind is None:
            continue
        mismatch = sink_edge_route_mismatch(edge, sources=sources, nodes=nodes)
        if mismatch is not None:
            errors.append(_err(f"edge:{edge.id}", mismatch, "high", "edge_route_mismatch"))

    for (from_node, edge_type), edge_ids in sink_slot_claims.items():
        if len(edge_ids) > 1:
            errors.append(
                _err(
                    f"edge:{edge_ids[1]}",
                    f"Edges {sorted(edge_ids)} all claim the single '{edge_type}' sink route of '{from_node}'. "
                    "One routing slot has one edge: remove or retarget the duplicates.",
                    "high",
                    "edge_route_conflict",
                )
            )
    for (from_node, to_node), edge_ids in fork_claims.items():
        if len(edge_ids) > 1:
            errors.append(
                _err(
                    f"edge:{edge_ids[1]}",
                    f"Edges {sorted(edge_ids)} duplicate the fork branch from '{from_node}' to sink '{to_node}'.",
                    "high",
                    "edge_route_conflict",
                )
            )
    return tuple(errors)


def _validate_gate_route_parity(condition: str, routes: Mapping[str, str] | None) -> str | None:
    """Validate gate route labels match the condition's static return type.

    Composition-time mirror of the runtime contract
    ``GateSettings.validate_boolean_routes`` (core/config.py): a boolean-typed
    condition (comparison, and/or/not, literal ``True``/``False``) must use route
    labels exactly ``{"true", "false"}``, and a provably-numeric condition can
    never produce a route label. Without this, ``CompositionState.validate()``
    would green-light a pipeline that runtime ``GateSettings`` construction later
    rejects.

    This is a deliberate second copy of the runtime predicate built on the same
    shared ``ExpressionParser`` substrate that ``_validate_gate_expression``
    already uses (durable unification is deferred to follow-up
    elspeth-2f93076878, which moves the route-label predicates out of
    ``ExpressionParser``). It must mirror ``validate_boolean_routes`` faithfully:
    same predicates, same ``boolean … elif non_routable`` precedence.

    Returns an error message when the route labels are inconsistent with the
    condition, or None when consistent (notably for string-returning conditions,
    which are routable by any label). The caller must only invoke this after the
    syntax/security check passed, so ``ExpressionParser(condition)`` does not
    re-raise here.
    """
    from elspeth.core.expression_parser import ExpressionParser

    parser = ExpressionParser(condition)
    if parser.is_boolean_expression():
        route_labels = set(routes or {})
        expected_labels = {"true", "false"}
        if route_labels != expected_labels:
            missing = expected_labels - route_labels
            extra = route_labels - expected_labels
            msg_parts = [f"Gate has a boolean condition ({condition!r}) but route labels don't match."]
            if extra:
                msg_parts.append(f"Found labels {sorted(extra)!r} but boolean expressions evaluate to True/False, not these values.")
            if missing:
                msg_parts.append(f"Missing required labels: {sorted(missing)!r}.")
            msg_parts.append('Use routes: {"true": <destination>, "false": <destination>}')
            return " ".join(msg_parts)
    elif parser.is_provably_non_routable():
        return (
            f"Gate condition ({condition!r}) statically returns a numeric value, "
            f"which can never be a route label. Gate conditions must evaluate to a boolean "
            f'(routes "true"/"false") or to a string route label.'
        )
    return None


# RFC 2606 / RFC 6761 reserved/special-use domain labels. Emails at these
# domains are not deliverable to anyone; values at these domains in
# `web_scrape.http.abuse_contact` are fabrications that ship as HTTP headers
# to scraped third parties — a Tier-1 audit-integrity defect — regardless of
# any prose rationale ("placeholder", "internal default") the composer LLM
# attached to them. Mechanical backstop for the skill-prompt rule in
# pipeline_composer.md (web_scrape.http section).
_RFC_RESERVED_DOMAIN_LABELS: tuple[str, ...] = (
    "example.com",
    "example.org",
    "example.net",
    "example",
    "test",
    "invalid",
    "localhost",
)


@observation_boundary(
    tier=3,
    source="NodeSpec carrying web-authored web_scrape options (untrusted abuse_contact value)",
    source_param="node",
    suppresses=("R1", "R5"),
    invariant=(
        "returns a high-severity ValidationEntry only for a well-formed abuse_contact at an "
        "RFC-reserved domain; absent, mistyped, or malformed values yield None (sibling "
        "plugin-schema rules report those) and never raise"
    ),
)
def _validate_web_scrape_abuse_contact_not_reserved(node: NodeSpec) -> ValidationEntry | None:
    """Reject web_scrape.http.abuse_contact values at RFC-reserved domains.

    abuse_contact is wire-visible: it ships as an HTTP header on every
    outbound scrape request, and the receiving operator uses it to contact us.
    A reserved-domain address (`example.com`, `*.test`, etc.) is not
    deliverable and constitutes a fabricated identity on the wire.

    Returns None when the field is absent, has an unexpected type, lacks an
    `@`, or uses a real domain. Returns a high-severity ValidationEntry when
    the domain matches one of the RFC 2606/6761 reserved labels.
    """
    if node.plugin != "web_scrape":
        return None
    # node.options is statically Mapping[str, Any]; the value at "http" is
    # unstructured (Any) and may be absent or non-Mapping, so the inner
    # isinstance guard below remains.
    http = node.options.get("http")
    if not isinstance(http, Mapping):
        return None  # Plugin schema rule reports missing/malformed http block.
    abuse_contact = http.get("abuse_contact")
    if not isinstance(abuse_contact, str):
        return None
    if "@" not in abuse_contact:
        return None  # Malformed email — let the plugin schema rule report it.
    domain = abuse_contact.rsplit("@", 1)[1].strip().lower()
    for reserved in _RFC_RESERVED_DOMAIN_LABELS:
        if domain == reserved or domain.endswith("." + reserved):
            return ValidationEntry(
                component=f"node:{node.id}",
                message=(
                    f"web_scrape.http.abuse_contact has domain '{domain}' — RFC 2606/6761 reserves "
                    f"'{reserved}' for documentation/test use, so the value is not deliverable to "
                    "anyone and would ship as a fabricated identity in the HTTP header to the "
                    "scraped host. Set abuse_contact to an operator-supplied or "
                    "deployment-identity-sourced email (see the web_scrape.http rule in "
                    "pipeline_composer.md)."
                ),
                severity="high",
                error_code="web_scrape_http_identity_invalid",
            )
    return None


@observation_boundary(
    tier=3,
    source="NodeSpec carrying web-authored web_scrape options (untrusted http identity fields)",
    source_param="node",
    suppresses=("R1", "R5"),
    invariant=(
        "emits a high-severity ValidationEntry per placeholder-valued wire-visible HTTP "
        "identity field; missing or mistyped options/http/field values are skipped "
        "(no entry) and never raised on"
    ),
)
def _validate_web_scrape_http_identity_not_placeholder(node: NodeSpec) -> tuple[ValidationEntry, ...]:
    """Reject placeholder values in web_scrape's wire-visible HTTP identity fields."""
    if node.plugin != "web_scrape":
        return ()
    http = node.options.get("http")
    if not isinstance(http, Mapping):
        return ()

    errors: list[ValidationEntry] = []
    for field_name in ("abuse_contact", "scraping_reason"):
        value = http.get(field_name)
        if not isinstance(value, str):
            continue
        if not is_wire_visible_placeholder(value):
            continue
        errors.append(
            ValidationEntry(
                component=f"node:{node.id}",
                message=(
                    f"web_scrape.http.{field_name} is a placeholder value. This field ships as an HTTP "
                    "header to the scraped host, so it must be supplied by the operator or deployment "
                    "identity before the pipeline can be considered valid."
                ),
                severity="high",
                error_code="web_scrape_http_identity_invalid",
            )
        )
    return tuple(errors)


def _validate_aggregation_trigger(node_id: str, trigger: Mapping[str, Any]) -> ValidationEntry | None:
    """Validate a composer-authored aggregation trigger at the Tier-3 boundary.

    ``node.trigger`` is composer/LLM/user-authored config read back from session
    state, so a malformed ``trigger`` is recoverable external input, not an
    invariant break. We run it through the same ``TriggerConfig`` parser the
    runtime settings load uses and convert a parse failure into an explicit
    blocking ``ValidationEntry`` — rejecting the bad trigger before runtime
    settings load rather than crashing the composer.
    """
    try:
        TriggerConfig.model_validate(deep_thaw(trigger))
    except PydanticValidationError as exc:
        detail = "; ".join(str(error["msg"]) for error in exc.errors())
        return ValidationEntry(
            component=f"node:{node_id}",
            message=f"Aggregation '{node_id}' trigger is invalid: {detail}",
            severity="high",
            error_code="aggregation_trigger_invalid",
        )
    return None


# The names PromptTemplate.render actually supplies (templates.py builds the
# context as exactly {"row": ..., "lookup": ...}) plus the Jinja2 environment
# globals (range, namespace, ...) that resolve at render time. Any other
# top-level template name hits StrictUndefined and raises TemplateError live.
_PROMPT_TEMPLATE_CONTEXT_NAMES: frozenset[str] = frozenset({"row", "lookup"})
_PROMPT_TEMPLATE_GLOBAL_NAMES: frozenset[str] = frozenset(create_sandboxed_environment().globals)


@observation_boundary(
    tier=3,
    source="NodeSpec carrying a web-authored prompt_template (untrusted Jinja2 text)",
    source_param="node",
    suppresses=("R1", "R5"),
    invariant=(
        "emits high-severity ValidationEntries only when a string prompt_template parses, and only "
        "for top-level names neither supplied by the render context nor definitely assigned locally, "
        "and for row fields outside a declared non-empty required_input_fields; absent, mistyped, or "
        "unparseable templates and non-list/empty declarations yield () (sibling rules report those) "
        "and never raise"
    ),
)
def _validate_prompt_template_variable_bindings(node: NodeSpec) -> tuple[ValidationEntry, ...]:
    """Reject single-prompt templates a render path cannot satisfy.

    Two independent defects, each with its own error code because the
    catalogue is keyed on the code and one code must mean one defect:

    * ``prompt_template_unbound_variables`` — ``PromptTemplate.render``
      supplies exactly ``row`` and ``lookup`` under ``StrictUndefined``, so a
      bare ``{{ text }}`` raises ``TemplateError: Undefined variable`` at
      runtime; the model receives none of the row's data and prompt-shield
      field-scope reasoning sees an empty protected set (R2-F17 compounding
      finding).
    * ``prompt_template_undeclared_row_fields`` — a ``row.<field>`` reference
      outside a declared, non-empty ``required_input_fields``
      (elspeth-a9ba80cb0b). The composer twin of the single-prompt limb of
      ``LLMConfig._validate_template_variable_bindings``, and required rather
      than redundant: the composer's plugin probes DO construct the node and
      DO see that rejection, then swallow it through
      ``_is_config_probe_exception`` so a draft pipeline never crashes
      validation. That abstention is deliberate and test-pinned, so the rule
      has to be restated here to reach Stage 1 at all. Both surfaces serve the
      SAME repair advice — the message here and
      ``_PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_FIX``, which
      ``tools/generation.py`` imports rather than copies.

    This is a contract check, not a proof of failure: ``row`` is bound to the
    whole row, so an undeclared reference raises only when that column is in
    fact absent — which is precisely what the declaration exists to rule out.
    ``undeclared_row_fields`` owns the comparison, matching a declaration
    under either the literal or the canonical row key and dropping bracket
    literals no declaration could express.

    ``{{interpretation:<term>}}`` placeholders are masked before parsing: they
    are resolved to operator-accepted text upstream of rendering and are not
    Jinja2 variables — unmasked they are a ``TemplateSyntaxError``, which
    would silence BOTH limbs on every interpretation-carrying node.

    Returns () when prompt_template is absent, not a string, or fails to parse
    (other layers own those shapes), and for multi-query nodes: with
    ``queries`` present, each query's ``input_fields`` maps template variables
    to row columns directly (``build_template_context`` in multi_query.py), so
    bare names are the documented idiom there — the same ``queries is None``
    scoping as ``LLMConfig._validate_required_input_fields_appear_in_template``.
    """
    if node.options.get("queries") is not None:
        return ()
    template = node.options.get("prompt_template")
    if not isinstance(template, str):
        return ()
    masked = INTERPRETATION_PLACEHOLDER_RE.sub(" ", template)
    try:
        ast = create_sandboxed_environment().parse(masked)
        usage = extract_jinja2_field_usage(masked)
    except TemplateSyntaxError:
        return ()

    errors: list[ValidationEntry] = []
    unbound = sorted(find_runtime_unbound_variables(ast) - _PROMPT_TEMPLATE_CONTEXT_NAMES - _PROMPT_TEMPLATE_GLOBAL_NAMES)
    if unbound:
        names = ", ".join(f"'{name}'" for name in unbound)
        errors.append(
            ValidationEntry(
                component=f"node:{node.id}",
                message=(
                    f"prompt_template references {names}, which the prompt render context does not define — "
                    "row data is only available as 'row.<field>' and lookup data as 'lookup.<key>', so "
                    "rendering fails with 'Undefined variable' at runtime and none of the row's data "
                    "reaches the model. Rewrite each name as '{{ row.<field> }}' (matching an upstream "
                    "schema field) or '{{ lookup.<key> }}', or remove the reference."
                ),
                severity="high",
                error_code="prompt_template_unbound_variables",
            )
        )

    declared = node.options.get("required_input_fields")
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
        declared_names = tuple(name for name in declared if isinstance(name, str))
        if declared_names:
            undeclared = undeclared_row_fields(usage.fields, declared_names)
            if undeclared:
                fields = describe_undeclared_row_fields(undeclared)
                declared_display = ", ".join(f"'{name}'" for name in sorted(declared_names))
                errors.append(
                    ValidationEntry(
                        component=f"node:{node.id}",
                        message=(
                            f"prompt_template reads {fields} under 'row', which this node's "
                            f"options.required_input_fields does not declare — it declares {declared_display}. "
                            "required_input_fields is the node's input contract: it is what edge validation checks "
                            "against the upstream producer's guarantees and what the engine verifies on every row, "
                            "so a reference outside it is required by nothing and a row arriving without the field "
                            f"fails the whole node at render with 'Undefined variable'. "
                            f"{_PROMPT_TEMPLATE_UNDECLARED_ROW_FIELDS_FIX}"
                        ),
                        severity="high",
                        error_code="prompt_template_undeclared_row_fields",
                    )
                )
    return tuple(errors)


# The one name build_template_context injects beside the query's own
# input_fields variables (multi_query.py): the full source row, reachable as
# row.source_row.<column> inside a query template.
_MULTI_QUERY_IMPLICIT_ROW_NAMES: frozenset[str] = frozenset({"source_row"})


def _parse_template_names(template: str) -> tuple[frozenset[str], frozenset[str]] | None:
    """Parse a prompt into (possible context names, first-level row fields).

    ``{{interpretation:...}}`` placeholders are masked first — they resolve to
    operator-accepted text upstream of rendering and are not Jinja2 names.
    Returns None when the template does not parse; sibling rules own syntax
    errors, so callers stay silent on that shape. Dynamic row accesses
    (``row[expr]``) are unprovable at parse time and are deliberately not
    reported — only the concrete field set is returned.
    """
    masked = INTERPRETATION_PLACEHOLDER_RE.sub(" ", template)
    try:
        ast = create_sandboxed_environment().parse(masked)
        usage = extract_jinja2_field_usage(masked)
    except TemplateSyntaxError:
        return None
    return find_runtime_unbound_variables(ast), usage.fields


@observation_boundary(
    tier=3,
    source="node.options['queries'] (web-authored multi-query definitions)",
    source_param="queries",
    suppresses=("R5",),
    invariant=(
        "returns only well-formed (label, entry) pairs; malformed queries or entries are silently "
        "dropped (QueryDefinition's contract is reported by plugin schema validation, not here), and "
        "this boundary never raises"
    ),
)
def _well_formed_query_entries(queries: Any) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """Extract (label, entry) pairs from an untrusted ``queries`` option.

    Accepts the two authoring forms ``LLMConfig`` accepts (mapping keyed by
    query name, or a list of named entries) and silently drops anything
    malformed — entry shape is ``QueryDefinition``'s contract and is reported
    by plugin schema validation, not double-reported here.
    """
    if isinstance(queries, Mapping):
        return tuple((str(key), entry) for key, entry in queries.items() if isinstance(entry, Mapping))
    if isinstance(queries, Sequence) and not isinstance(queries, (str, bytes)):
        entries: list[tuple[str, Mapping[str, Any]]] = []
        for index, item in enumerate(queries):
            if not isinstance(item, Mapping):
                continue
            name = item.get("name")
            entries.append((name if isinstance(name, str) and name else f"#{index}", item))
        return tuple(entries)
    return ()


@observation_boundary(
    tier=3,
    source="NodeSpec carrying web-authored multi-query options (untrusted queries entries and Jinja2 text)",
    source_param="node",
    suppresses=("R1", "R5"),
    invariant=(
        "emits high-severity ValidationEntries only for parseable effective templates of "
        "well-formed query entries (mapping entries with a string-keyed input_fields mapping); "
        "absent, mistyped, or unparseable pieces are skipped (sibling rules report those) "
        "and never raised on"
    ),
)
def _validate_multi_query_template_variable_bindings(node: NodeSpec) -> tuple[ValidationEntry, ...]:
    """Reject multi-query templates whose interpolations can never bind at render.

    Each query renders its effective template — its ``template`` override when
    present, else the node-level ``prompt_template`` — with ``row`` bound to
    the query's synthetic context (``build_template_context`` in
    multi_query.py: the ``input_fields`` variables plus ``source_row``) and
    ``lookup``, under StrictUndefined. Two compose-time-provable defects:

    * a top-level name outside ``{row, lookup}`` + environment globals never
      binds in any mode — the single-prompt failure shape, so it reuses
      ``prompt_template_unbound_variables`` (covers the legacy positional
      ``{{ input_N }}`` idiom, which is such a bare name);
    * a ``row.<name>`` reference outside that query's ``input_fields`` keys +
      ``{source_row}`` raises ``Undefined variable`` when that query renders
      (``query_template_unbound_row_fields``).

    The node-level template is checked once for top-level names — and only
    when at least one well-formed query actually falls back to it: the shipped
    multi-query examples carry a never-rendered ``prompt_template`` beside
    all-override queries, and flagging dead text would be a false positive.
    A query whose ``template`` is present but not a string is skipped whole
    (its effective template is unknowable until the schema rejection lands).
    """
    queries = node.options.get("queries")
    if queries is None:
        return ()
    entries = _well_formed_query_entries(queries)
    if not entries:
        return ()

    node_template = node.options.get("prompt_template")
    node_parse = _parse_template_names(node_template) if isinstance(node_template, str) else None

    errors: list[ValidationEntry] = []
    node_template_in_use = False

    for label, entry in entries:
        input_fields = entry.get("input_fields")
        if not isinstance(input_fields, Mapping):
            continue
        bound = frozenset(key for key in input_fields if isinstance(key, str))
        if not bound:
            continue

        override = entry.get("template")
        if isinstance(override, str):
            parsed = _parse_template_names(override)
            source_desc = "its template override"
        elif override is None:
            if node_parse is None:
                continue
            parsed = node_parse
            source_desc = "the node-level prompt_template"
            node_template_in_use = True
        else:
            continue
        if parsed is None:
            continue
        top_level_names, row_fields = parsed

        if source_desc == "its template override":
            unbound_names = sorted(top_level_names - _PROMPT_TEMPLATE_CONTEXT_NAMES - _PROMPT_TEMPLATE_GLOBAL_NAMES)
            if unbound_names:
                names = ", ".join(f"'{name}'" for name in unbound_names)
                errors.append(
                    ValidationEntry(
                        component=f"node:{node.id}",
                        message=(
                            f"Query '{label}' template references {names}, which the multi-query render "
                            "context does not define — a query template sees only 'row' (this query's "
                            "input_fields variables plus 'row.source_row') and 'lookup', so rendering fails "
                            "with 'Undefined variable' at runtime. Rewrite each name as '{{ row.<variable> }}' "
                            "where <variable> is one of this query's input_fields keys, or bind it in "
                            "input_fields first."
                        ),
                        severity="high",
                        error_code="prompt_template_unbound_variables",
                    )
                )

        unbound_fields = sorted(row_fields - bound - _MULTI_QUERY_IMPLICIT_ROW_NAMES)
        if unbound_fields:
            fields = ", ".join(f"'{name}'" for name in unbound_fields)
            bound_names = ", ".join(f"'{name}'" for name in sorted(bound))
            errors.append(
                ValidationEntry(
                    component=f"node:{node.id}",
                    message=(
                        f"Query '{label}' renders {source_desc}, which references {fields} under 'row', "
                        f"but this query's input_fields binds only {bound_names} (plus 'source_row'). At "
                        "render the query context contains exactly its input_fields variables, so each "
                        "unbound reference fails with 'Undefined variable' and the query errors for every "
                        "row. Add the missing variables to input_fields (template variable → row column), "
                        "rename the reference to a bound variable, or use 'row.source_row.<column>' for "
                        "direct row access."
                    ),
                    severity="high",
                    error_code="query_template_unbound_row_fields",
                )
            )

    if node_template_in_use and node_parse is not None:
        unbound_names = sorted(node_parse[0] - _PROMPT_TEMPLATE_CONTEXT_NAMES - _PROMPT_TEMPLATE_GLOBAL_NAMES)
        if unbound_names:
            names = ", ".join(f"'{name}'" for name in unbound_names)
            errors.append(
                ValidationEntry(
                    component=f"node:{node.id}",
                    message=(
                        f"prompt_template references {names}, which the multi-query render context does "
                        "not define — queries without a template override render it with 'row' bound to "
                        "their input_fields variables (plus 'row.source_row') and 'lookup', so rendering "
                        "fails with 'Undefined variable' at runtime. Rewrite each name as "
                        "'{{ row.<variable> }}' with <variable> an input_fields key of every query that "
                        "uses this template, or give those queries template overrides."
                    ),
                    severity="high",
                    error_code="prompt_template_unbound_variables",
                )
            )

    return tuple(errors)


def _locked_input_field_set(options: Mapping[str, Any], owner: str) -> frozenset[str] | None:
    """Return the consumer's accepted-input field set when its input is locked.

    Mirrors ``_create_explicit_schema`` (schema_factory.py): a generated input
    Pydantic model gets ``extra="forbid"`` only when ``schema.mode == "fixed"``.
    For ``mode: flexible`` (extras allowed) and ``mode: observed`` (no field
    constraints), returns None so the membership rule short-circuits.

    The accepted set IS the declared ``schema.fields`` — that is what the
    Pydantic model whitelists. Fields enumerated only via ``required_fields``
    or ``audit_fields`` do not appear in the input model and therefore are not
    part of the accepted set.
    """
    schema_config = get_raw_schema_config(options, owner=owner)
    if schema_config is None or schema_config.mode != "fixed" or schema_config.fields is None:
        return None
    return frozenset(field.name for field in schema_config.fields)


def _consumer_locked_input_set(node: NodeSpec) -> frozenset[str] | None:
    """Return the consumer node's accepted-input set when input is locked.

    Aggregation nodes carry their contract under either flat ``options`` or a
    nested ``options.options`` wrapper; resolve via ``get_aggregation_contract_options``
    so locked-input detection uses the same alias resolution as the rest of
    the contract pipeline. The augmented owner string the helper returns is
    discarded so the caller's existing error-message wording is preserved.
    """
    owner = f"node:{node.id}"
    if node.node_type == "aggregation":
        contract_options, _ = get_aggregation_contract_options(node.options, owner=owner)
        return _locked_input_field_set(contract_options, owner=owner)
    return _locked_input_field_set(node.options, owner=owner)


def _sink_locked_input_set(output: OutputSpec) -> frozenset[str] | None:
    """Return the sink's accepted-input set when its input contract is locked."""
    return _locked_input_field_set(output.options, owner=f"output:{output.name}")


@observation_boundary(
    tier=3,
    source="NodeSpecs carrying composer/LLM/user-authored options re-read from session state",
    source_param="nodes",
    suppresses=(),
    invariant=(
        "node options are never read raw: optional flags (e.g. select_only) reach rule "
        "logic only through the plugin's own config parse, which owns their defaults "
        "(elspeth-fc3cd7a86c); malformed node config surfaces as blocking ValidationEntry "
        "results, never a raise (genuine engine defects crash through via the config-probe "
        "re-raise guards)"
    ),
)
def _check_schema_contracts(
    sources: Mapping[str, SourceSpec],
    nodes: tuple[NodeSpec, ...],
    outputs: tuple[OutputSpec, ...],
) -> tuple[
    tuple[ValidationEntry, ...],
    tuple[ValidationEntry, ...],
    tuple[EdgeContract, ...],
]:
    """Validate producer/consumer schema contracts across declarative routing."""
    from elspeth.web.composer._producer_resolver import ProducerEntry, ProducerResolver, is_source_producer_id, source_producer_id

    errors: list[ValidationEntry] = []
    contract_warnings: list[ValidationEntry] = []
    edge_contracts: list[EdgeContract] = []
    parse_failed_producers: set[str] = set()
    contract_probe_failed_producers: set[str] = set()
    sink_names = {output.name for output in outputs}
    sink_names_frozen = frozenset(sink_names)
    barrier_branch_names = {
        branch_name
        for node in nodes
        if node.node_type in ("coalesce", "row_union") and node.branches is not None
        for branch_name in _coalesce_branch_names(node.branches)
    }
    internal_connection_names: set[str] = set()
    # Spec §7 rule 9 (Task 11): same carve-out as ProducerResolver.build's —
    # a closer-shaped on_error is a DIVERT edge into an EXISTING node, not a
    # claim to produce a connection under the closer's name, so it must not
    # be tracked as a duplicate-producer description candidate either.
    closer_names = {node.id for node in nodes if node.node_type in ("coalesce", "row_union")}
    source_map = sources

    _err = ValidationEntry
    _warn = ValidationEntry

    if any(node.id == "source" for node in nodes):
        errors.append(
            _err(
                "pipeline",
                "Reserved node id 'source' cannot be used in composer state because contract walk-back uses it as the source sentinel.",
                "high",
                "reserved_node_id",
            )
        )
        return tuple(errors), tuple(contract_warnings), ()

    # The resolver builds the connection -> producer map (with the
    # source-as-source-sentinel and same-node carve-out semantics), and
    # reports which connections have multiple distinct producers.
    resolver = ProducerResolver.build(
        source=None,
        sources=source_map,
        nodes=nodes,
        sink_names=sink_names_frozen,
    )
    node_by_id = {node.id: node for node in nodes}

    # Schema-specific bookkeeping: track per-connection producer
    # description, for richer duplicate-error messages. Mirror the
    # resolver's registration order so first-seen descriptions match
    # the resolver's first-seen producer.
    #
    # Direct-to-sink producers are NOT tracked here: the resolver records
    # them during the same registration walk and exposes them through
    # ``sink_producers()``. Sink-targeted connections are skipped below for
    # description purposes only, because a sink is never a duplicate-producer
    # participant — several nodes writing to one output is fan-in, not
    # contention.
    producer_desc: dict[str, str] = {}
    duplicate_descs: dict[str, list[str]] = {}

    def _record_description(connection_name: str, description: str) -> None:
        if connection_name in producer_desc:
            if connection_name not in duplicate_descs:
                duplicate_descs[connection_name] = []
            duplicate_descs[connection_name].append(description)
            return
        producer_desc[connection_name] = description
        if connection_name not in sink_names:
            internal_connection_names.add(connection_name)

    for source_name, source in source_map.items():
        if source.on_success not in sink_names:
            source_desc = f"source '{source.plugin}'" if source_name == "source" else f"source '{source_name}' ({source.plugin})"
            _record_description(source.on_success, source_desc)

    for node in nodes:
        if node.node_type == "coalesce" and node.on_success is None:
            _record_description(node.id, f"coalesce '{node.id}'")
        elif node.on_success is not None and node.on_success not in sink_names:
            _record_description(node.on_success, f"node '{node.id}' on_success")
        if (
            node.on_error is not None
            and node.on_error != "discard"
            and node.on_error not in sink_names
            and node.on_error not in closer_names
        ):
            _record_description(node.on_error, f"node '{node.id}' on_error")
        if node.routes is not None:
            for route_label, target in node.routes.items():
                if target == _DISCARD_ROUTE_TARGET:
                    continue
                if target == _FORK_ROUTE_TARGET:
                    # Reserved fork-mode keyword, not a connection. The
                    # resolver applies the same carve-out; description
                    # tracking must mirror it (see same-node carve-out
                    # below).
                    continue
                if target in sink_names:
                    continue
                # Same-node carve-out: a gate with multiple route labels
                # mapping to the same target is idempotent, not a
                # duplicate. The resolver applies the same carve-out, so
                # description tracking must mirror it to keep error
                # messages aligned.
                resolver_owner = resolver.find_producer_for(target)
                if resolver_owner is not None and resolver_owner.producer_id == node.id and target in producer_desc:
                    continue
                _record_description(target, f"gate '{node.id}' route '{route_label}'")
        if node.fork_to is not None:
            for branch_name in node.fork_to:
                if branch_name in sink_names:
                    # Fork branches that terminate at sinks behave like
                    # direct-to-sink edges from the gate: the runtime
                    # contract walks from the sink back through the gate
                    # to the gate's upstream producer (matched in
                    # _walk_producer_entry_to_real_producer's fork-vs-sink
                    # branch). The resolver records the producer entry; no
                    # description is kept, because a sink is not a
                    # duplicate-producer participant.
                    continue
                _record_description(branch_name, f"gate '{node.id}' fork '{branch_name}'")

    # Surface duplicate-producer errors using the captured descriptions.
    for connection_name in sorted(resolver.duplicate_connections):
        first_desc = producer_desc[connection_name]
        # duplicate_descs may be missing if the duplicate was suppressed
        # by the same-node route carve-out; in that case the resolver
        # would not flag a duplicate either, so this branch is purely
        # defensive against future divergence.
        second_desc = duplicate_descs[connection_name][0]
        errors.append(
            _err(
                f"connection:{connection_name}",
                f"Duplicate producer for connection '{connection_name}': {first_desc} and {second_desc}.",
                "high",
                "duplicate_connection_producer",
            )
        )

    # Queue and row_union NodeSpec.input fields are structural placeholders,
    # not consuming bindings. Coalesce/row_union identity branches are direct
    # COPY edges; only mapped branches claim their input connection.
    consumer_claims: list[tuple[str, str, str]] = [
        (node.input, node.id, f"node '{node.id}'") for node in nodes if node.node_type not in ("coalesce", "queue", "row_union")
    ]
    for node in nodes:
        if node.node_type == "coalesce":
            consumer_claims.extend(
                (connection_name, node.id, f"coalesce '{node.id}' mapped branch")
                for connection_name in _coalesce_mapped_branch_connections(node.branches)
            )
        elif node.node_type == "row_union":
            consumer_claims.extend(
                (connection_name, node.id, f"row_union '{node.id}' mapped branch")
                for connection_name in _coalesce_mapped_branch_connections(node.branches)
            )
    consumer_counts = Counter(connection_name for connection_name, _node_id, _desc in consumer_claims)
    duplicate_consumers = sorted(name for name, count in consumer_counts.items() if count > 1)
    for connection_name in duplicate_consumers:
        dup_entries = [(node_id, desc) for name, node_id, desc in consumer_claims if name == connection_name]
        first_node, first_desc = dup_entries[0]
        second_node, second_desc = dup_entries[1]
        errors.append(
            _err(
                f"connection:{connection_name}",
                f"Duplicate consumer for connection '{connection_name}': "
                f"{first_desc} ({first_node}) and {second_desc} ({second_node}). "
                "Use a gate for fan-out.",
                "high",
                "duplicate_connection_consumer",
            )
        )

    internal_connection_names.update(connection_name for connection_name, _node_id, _desc in consumer_claims)
    # Runtime fork routing resolves coalesce branch names before sink names.
    # A branch identity that also names a sink would make composer preview treat
    # the branch as direct-to-sink while execution sends it to coalesce.
    internal_connection_names.update(barrier_branch_names)
    overlap = sorted(internal_connection_names & sink_names)
    if overlap:
        errors.append(
            _err(
                "pipeline",
                f"Connection names overlap with sink names: {overlap}. Connection names and sink names must be disjoint.",
                "high",
                "connection_sink_name_overlap",
            )
        )

    if errors:
        return tuple(errors), tuple(contract_warnings), ()

    def _walk_producer_entry_to_real_producer(
        producer: ProducerEntry,
        *,
        connection_name: str,
        warnings: list[ValidationEntry],
    ) -> ProducerEntry | None:
        """Schema-specific walk-back with coalesce/fork warning emission.

        Differs from ``ProducerResolver.walk_to_real_producer`` in two ways: it
        traverses structural producers to emit skip-with-warning entries (the
        resolver returns or abstains silently), and it stops at a fan-in
        boundary whose branches it cannot resolve into one sound guarantee.
        Each fan-in kind now has a participation escape hatch — queue and
        row_union return the branch intersection, a union coalesce returns the
        builder's own merge — so the abstention is a statement about THIS
        composition rather than about the node kind.
        """
        visited_connections: set[str] = set()
        current_producer = producer
        while True:
            if is_source_producer_id(current_producer.producer_id):
                return current_producer

            producer_node = resolver.get_node(current_producer.producer_id)
            if producer_node is None:
                return None
            if producer_node.node_type == "coalesce":
                # Engine parity (elspeth-ae83a6b60c), the third sibling of the
                # queue and row_union rules below: a UNION coalesce's merged
                # guarantee is exactly what the DAG builder stamps on it, so
                # the contract check proceeds against the coalesce as producer
                # (the guarantee parsers resolve it through
                # ``_producer_entry_propagation_vote``). Abstaining here
                # unconditionally is what made every union-coalesce pipeline
                # validate green while the runtime build rejected it, with no
                # error for the authoring loop to repair against. Non-union
                # merges and a non-participating vote keep the honest "not yet
                # checked" warning — see ``_union_coalesce_merged_guarantees``
                # for why the union gate is the whole population.
                if _union_coalesce_merged_guarantees(current_producer) is not None:
                    return current_producer
                warnings.append(
                    _warn(
                        f"node:{producer_node.id}",
                        f"Contract check skipped because connection '{connection_name}' is produced by coalesce node '{producer_node.id}'; runtime validator will check this edge.",
                        "medium",
                    )
                )
                return None
            if producer_node.node_type == "queue":
                # Engine parity (83a53388a / elspeth-5a372d3267): when every
                # fan-in arm participates, the queue's effective guarantee is
                # the arm intersection and the contract check proceeds against
                # the queue as producer (the guarantee parsers resolve it
                # through ``_producer_entry_propagation_vote``). The check
                # still never compares against a single arbitrary upstream
                # (elspeth-a5b86149d4) — the intersection is the fan-in-sound
                # aggregate. When any arm abstains the vote collapses to
                # abstention: the runtime defers to per-row enforcement, and
                # the skip warning stays the honest "not yet checked" signal.
                try:
                    queue_participates, _queue_fields = _producer_entry_propagation_vote(
                        current_producer,
                        visited_fan_in_ids=frozenset(),
                    )
                except ValueError:
                    # A fan-in ARM may sit later in ``nodes`` than the consumer
                    # being checked, so this vote can be the first parse of that
                    # arm's ``options["schema"]`` — ordinary recoverable external
                    # input, not a defect in our own code. Abstain exactly as the
                    # ``_union_coalesce_merged_guarantees`` sibling already does
                    # rather than crashing /validate with an unhandled ValueError.
                    queue_participates = False
                if queue_participates:
                    return current_producer
                warnings.append(
                    _warn(
                        f"node:{producer_node.id}",
                        f"Contract check skipped because connection '{connection_name}' is produced by queue node '{producer_node.id}' with observed schema.",
                        "medium",
                    )
                )
                return None
            if producer_node.node_type == "row_union":
                # Engine parity (elspeth-41bcaa882e), sibling of the queue rule
                # above: when every branch participates, the union's effective
                # guarantee is the branch intersection and the contract check
                # proceeds against the row_union as producer (the guarantee
                # parsers resolve it through
                # ``_producer_entry_propagation_vote``). Unconditionally
                # skipping here let the composer author union-consumer
                # requirements the engine then deterministically rejected at
                # /validate (battery-2026-08-06 g08-s2/s3). When any branch
                # abstains the vote collapses to abstention: the runtime defers
                # to per-row enforcement, and the skip warning stays the honest
                # "not yet checked" signal.
                try:
                    union_participates, _union_fields = _producer_entry_propagation_vote(
                        current_producer,
                        visited_fan_in_ids=frozenset(),
                    )
                except ValueError:
                    # Same reason as the queue branch above and the
                    # ``_union_coalesce_merged_guarantees`` sibling: a branch may
                    # sit later in ``nodes``, so this can be the first parse of
                    # its schema block. Abstain rather than crash /validate.
                    union_participates = False
                if union_participates:
                    return current_producer
                warnings.append(
                    _warn(
                        f"node:{producer_node.id}",
                        f"Contract check skipped because connection '{connection_name}' is produced by "
                        f"row_union node '{producer_node.id}' with observed schema.",
                        "medium",
                    )
                )
                return None
            if producer_node.node_type != "gate":
                return current_producer
            if producer_node.fork_to is not None and connection_name not in sink_names:
                warnings.append(
                    _warn(
                        f"node:{producer_node.id}",
                        f"Contract check skipped because fork gate '{producer_node.id}' produces connection '{connection_name}'; branch-aware contract validation is out of scope for composer preview.",
                        "medium",
                    )
                )
                return None
            current_connection = producer_node.input
            if current_connection in visited_connections:
                warnings.append(
                    _warn(
                        f"connection:{connection_name}",
                        f"Contract check skipped for connection '{connection_name}' because producer walk-back encountered a routing loop.",
                        "medium",
                    )
                )
                return None
            visited_connections.add(current_connection)
            next_producer = resolver.find_producer_for(current_connection)
            if next_producer is None:
                return None
            current_producer = next_producer

    def _walk_to_real_producer(
        connection_name: str,
        *,
        warnings: list[ValidationEntry],
    ) -> ProducerEntry | None:
        producer = resolver.find_producer_for(connection_name)
        if producer is None:
            return None
        return _walk_producer_entry_to_real_producer(
            producer,
            connection_name=connection_name,
            warnings=warnings,
        )

    def _producer_owner(producer: ProducerEntry) -> str:
        return producer.producer_id if is_source_producer_id(producer.producer_id) else f"node:{producer.producer_id}"

    def _producer_label(producer: ProducerEntry) -> str:
        if producer.plugin_name is not None:
            return producer.plugin_name
        return node_by_id[producer.producer_id].node_type

    def _known_pass_through_plugins() -> frozenset[str]:
        """Lazily compute the set of pass-through plugin names from the live registry.

        Re-derived per call rather than cached at module-load — a plugin
        registered after composer module import (dynamic packs, test fixture
        ordering) was previously invisible to the fail-closed path. Cardinality
        is bounded by the annotated-transform set (short, known at startup).

        Reads ``cls.passes_through_input`` directly — no ``getattr`` defensive
        default. After the Phase A annotation, ``BaseTransform`` supplies the
        field for every transform class; a missing attribute IS a framework
        bug and must crash here loudly, not be silently coerced to ``False``.
        """
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        transforms = get_shared_plugin_manager().get_transforms()
        return frozenset(cls.name for cls in transforms if cls.passes_through_input)

    def _probe_transform_output_schema(plugin: str, options: Mapping[str, Any]) -> tuple[bool, SchemaConfig | None]:
        """Read ``plugin``'s output schema, closing the validation-only instance.

        The boolean distinguishes an expected construction failure from a
        successfully constructed plugin that abstains from an output schema.
        Genuine engine defects crash through. Once construction succeeds,
        close() owns every success, return, and inspection-exception path.
        """
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        try:
            transform = get_shared_plugin_manager().create_transform(
                plugin,
                prepare_validation_probe_options(options),
            )
        except Exception as exc:
            if not _is_config_probe_exception(exc):
                raise
            return False, None
        try:
            return True, transform._output_schema_config
        finally:
            transform.close()

    def _probe_field_mapper_config(options: Mapping[str, Any]) -> FieldMapperConfig | None:
        """Parse ``options`` through the plugin's own config class, or abstain.

        Returns None for an expected draft/config failure — the config-parse
        rules own reporting those. Genuine engine defects crash through,
        matching ``_probe_transform_output_schema``'s tolerance exactly.
        """
        try:
            return FieldMapperConfig.from_dict(
                prepare_validation_probe_options(options),
                plugin_name="field_mapper",
            )
        except Exception as exc:
            if not _is_config_probe_exception(exc):
                raise
            return None

    def _mapping_source_of(options: Mapping[str, Any], target: str) -> str:
        """Return the mapping key that emits ``target``.

        No shape guard, and no absent-key fallback, because neither state is
        reachable from the one call site. Rule C reaches this only after
        ``_probe_transform_output_schema`` returned a config, which means
        ``FieldMapperConfig`` already parsed these same options — so ``mapping``
        is a validated ``dict[str, str]``. And ``target`` always has an entry,
        because under ``select_only`` the projection that produces
        ``declared_required`` emits mapping TARGETS only, making ``missing`` a
        subset of ``mapping.values()`` by construction (a declared non-target is
        dropped by the projection, never carried into ``missing``).

        A ``KeyError``/``AttributeError`` here is therefore a framework bug and
        must crash loudly rather than degrade into advice that names no source.
        """
        mapping: Mapping[str, str] = options["mapping"]
        return next(source for source, mapped in mapping.items() if mapped == target)

    remedy_class_cache: dict[str, str] = {}

    def _mapping_source_remedy_class(source: str, target: str) -> str:
        """Memoized ``_classify_mapping_source``.

        Two plugin constructions per classification, and ``validate()`` runs on
        every composer tool call, so a many-target mapper would otherwise pay
        for each one. The verdict depends only on the SOURCE — the target is a
        free variable the probes carry through — so one entry per distinct
        source serves every target it maps to.
        """
        if source in remedy_class_cache:
            return remedy_class_cache[source]
        verdict = _classify_mapping_source(source, target)
        remedy_class_cache[source] = verdict
        return verdict

    def _classify_mapping_source(source: str, target: str) -> str:
        """Classify what would actually repair ``source`` -> ``target``.

        Asks the PLUGIN, by constructing two canonical counterfactual configs
        and reading back whether ``target`` lands in its guarantees. The message
        therefore cannot disagree with the rule it explains: both read the same
        ``_output_schema_config``. A hand-written predicate here would drift —
        the obvious spellings (``str.isidentifier``, "lowercase with no spaces")
        are both measurably wrong, admitting ``Name``/``userID``/``_id``/``a__b``
        where the plugin abstains and rejecting ``class_``/``_1``/``if_`` where
        it does not (elspeth-920bd88299, and elspeth-f262a8c678 for the
        isidentifier half).

        ``declarable`` — the source is the name the row is keyed by, so
        declaring and guaranteeing it repairs the node while KEEPING the
        downstream promise. ``strict_only`` — a nested read, which
        ``guaranteed_fields`` can never name. ``unguaranteeable`` — the plugin
        deliberately abstains (it cannot tell whether the row is keyed by the
        literal or by its normalized form), so NO declaration repairs it.
        """
        declared_options = {
            "mapping": {source: target},
            "select_only": True,
            "schema": {"mode": "fixed", "fields": [f"{source}: any"], "guaranteed_fields": [source]},
        }
        constructed, config = _probe_transform_output_schema("field_mapper", declared_options)
        if constructed and config is not None and target in (config.guaranteed_fields or ()):
            return "declarable"
        strict_options = {
            "mapping": {source: target},
            "select_only": True,
            "strict": True,
            "schema": {"mode": "fixed", "fields": [f"{target}: any"]},
        }
        constructed, config = _probe_transform_output_schema("field_mapper", strict_options)
        if constructed and config is not None and target in (config.guaranteed_fields or ()):
            return "strict_only"
        return "unguaranteeable"

    def _unguaranteed_target_remedies(options: Mapping[str, Any], missing: frozenset[str]) -> str:
        """Build Rule C's repair advice, one clause per applicable source class.

        Emits ONLY the remedies measured to work for the sources actually in
        play — measured through EXECUTION, not just through this validator.
        The rule previously offered four alternatives joined by "OR", of
        which two were inert for the shape that fires most and one was
        unauthorable in every shape — so a planner could apply a remedy, have
        the mutation ACCEPTED, and get a byte-identical error back with no
        signal that its repair had done nothing (elspeth-920bd88299). The
        successor defect was subtler: a remedy that cleared THIS validator but
        left a node whose fixed-mode INPUT model rejected the very field the
        mapping reads, so every row died in ``_run_preflight`` before
        ``process()`` — which is why the fixed-mode clauses below also
        instruct the ``schema.mode: flexible`` switch.
        """
        # Direct indexing on the same grounds as ``_mapping_source_of``: this
        # function runs only after ``_probe_transform_output_schema`` returned
        # a config, so ``FieldMapperConfig`` (whose ``schema`` and ``mode``
        # are required, defaultless fields) already parsed these options.
        schema_is_fixed = options["schema"]["mode"] == "fixed"

        # Seeded with every class so the reads below are direct indexing: this
        # dict is built and consumed here, so a ``.get`` default would be
        # covering for an absence this function itself rules out.
        by_class: dict[str, list[tuple[str, str]]] = {"declarable": [], "strict_only": [], "unguaranteeable": []}
        for target in sorted(missing):
            source = _mapping_source_of(options, target)
            by_class[_mapping_source_remedy_class(source, target)].append((target, source))

        clauses: list[str] = [
            "Those names are mapping TARGETS, and `schema` is this node's INPUT contract, so a target is usually "
            "absent from `schema.fields` altogether — removing it from the declaration then changes nothing and "
            "this same error repeats."
        ]
        for target, source in by_class["declarable"]:
            clauses.append(
                f"'{target}' is mapped from '{source}', which the row is keyed by: declare '{source}' in "
                f"`schema.fields`, name it in `schema.guaranteed_fields`, AND remove '{target}' from "
                f"`schema.fields` — all three, because leaving '{target}' declared makes this node demand the "
                f"emitted name as an input field it never receives."
            )
        for target, source in by_class["strict_only"]:
            container = source.split(".", 1)[0]
            fixed_mode_leg = (
                f" Set `schema.mode: flexible` in the same edit: `schema` is also this node's runtime input "
                f"model, and fixed mode rejects the top-level '{container}' field the nested read consumes as an "
                f"undeclared extra — with `strict: true` alone this error clears and every row then fails input "
                f"validation before the mapping runs."
                if schema_is_fixed
                else ""
            )
            clauses.append(
                f"'{target}' is mapped from the nested read '{source}', which `schema.guaranteed_fields` cannot "
                f"name: set `strict: true` instead. That is node-wide — it routes any row missing ANY mapped "
                f"source to on_error rather than emitting the row without it.{fixed_mode_leg}"
            )
        for target, source in by_class["unguaranteeable"]:
            fixed_mode_leg = (
                ", and set `schema.mode: flexible` so the key the row actually arrives under passes this node's input validation"
                if schema_is_fixed
                else ""
            )
            try:
                stable_spelling = f"'{normalize_field_name(source)}'"
            except ValueError:
                # ``ExternalHeaderError``/``ValueError`` for a literal with no
                # normalized form at all (``'!!!'``) — there is no spelling to
                # name, but the shape of the repair is still statable.
                stable_spelling = "a normalization-stable spelling"
            clauses.append(
                f"'{target}' is mapped from '{source}', which is not the name a row is keyed by, so this node "
                f"cannot promise '{target}' at all — no `schema` declaration and no `strict: true` clears this. "
                f"Do NOT rewrite the mapping key to another spelling on its own: that is accepted silently and "
                f"drops the column. Either withdraw the guarantee — make '{target}' optional by appending `?` to "
                f"its declared type in `schema.fields` (do not delete the entry: a fixed/flexible schema must "
                f"keep at least one field, so deleting the last one is rejected), drop '{target}' from "
                f"`schema.guaranteed_fields` if named there{fixed_mode_leg} — after which a consumer that "
                f"requires '{target}' rejects at its own edge; or repair it end to end — rename the field "
                f"upstream so the row is keyed by {stable_spelling}, rewrite the mapping key to that SAME "
                f"spelling, and then declare and guarantee it like any row-keyed source."
            )
        return " ".join(clauses)

    def _probe_transform_declared_output_fields(plugin: str, options: Mapping[str, Any]) -> frozenset[str]:
        """Read ``plugin``'s ``declared_output_fields``, closing the probe instance.

        Deliberately NOT served from ``_probe_transform_output_schema``:
        ``_output_schema_config.guaranteed_fields`` is a documented SUPERSET of
        ``declared_output_fields`` (the invariant asserted in the LLM transform's
        constructor), because guarantees also cover fields the transform merely
        passes through or renames from. Only ``declared_output_fields`` is the
        set the executor's collision preflight actually tests, so Rule D reads it
        directly — substituting guarantees would reject rename sources and
        pass-through fields the runtime never flags (elspeth-cfcd333f83).

        A construction failure abstains with the empty set: a draft node whose
        options do not yet build is owned by the existing config-validation
        paths, and Rule D must not turn an incomplete draft into a hard error.
        """
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        try:
            transform = get_shared_plugin_manager().create_transform(
                plugin,
                prepare_validation_probe_options(options),
            )
        except Exception as exc:
            if not _is_config_probe_exception(exc):
                raise
            return frozenset()
        try:
            return transform.declared_output_fields
        finally:
            transform.close()

    class _DeclaredInputs(NamedTuple):
        fields: frozenset[str]
        string_fields: frozenset[str]

    def _probe_transform_declared_inputs(plugin: str, options: Mapping[str, Any]) -> _DeclaredInputs:
        """Read ``plugin``'s ``declared_input_fields`` AND ``declared_string_input_fields`` in one probe.

        Both live only on a constructed instance (``keyword_filter`` sets its
        string-typed scan fields from ``fields``; the Azure document
        transforms from ``source_field``), and one construction is enough to
        read both — the lifecycle tests pin exactly one probe instance per
        site.

        Input-side twin of ``_probe_transform_declared_output_fields``. Six
        transform configs compute this as a property over their own options
        (web_scrape's ``url_field``, blob_fetch's ``url_field``,
        blob_csv_expand's ``blob_ref_field`` — which DEFAULTS to ``blob_ref``,
        so the set is non-empty even when the author wrote no option at all —
        textract's ``key_field`` plus optionally ``bucket_field``/
        ``version_field``, azure document_intelligence's ``source_field``, and
        rag's ``query_field``). None of those names reach the raw
        ``required_input_fields`` option or the ``schema:`` block, so reading
        the config surfaces alone misses every one of them; only a constructed
        instance knows (elspeth-ada5a60249).

        A construction failure abstains with the empty set, for the same reason
        the output probe does: a draft node whose options do not yet build is
        owned by the existing config-validation paths, and this rule must not
        turn an incomplete draft into a hard error.
        """
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        try:
            transform = get_shared_plugin_manager().create_transform(
                plugin,
                prepare_validation_probe_options(options),
            )
        except Exception as exc:
            if not _is_config_probe_exception(exc):
                raise
            return _DeclaredInputs(frozenset(), frozenset())
        try:
            return _DeclaredInputs(transform.declared_input_fields, transform.declared_string_input_fields)
        finally:
            transform.close()

    def _string_input_field_type_conflict(
        producer: ProducerEntry,
        node: NodeSpec,
        declared_string_input: frozenset[str],
    ) -> ValidationEntry | None:
        """Mirror ``validate_transform_string_typed_input_fields`` for a typed-source producer.

        A transform that names explicit scan fields (``keyword_filter``
        ``fields``, document-intelligence ``source_field``) fails closed on the
        first non-string value, so a producer that provably types one of them
        non-string quarantines every row. The runtime checks EVERY live
        predecessor's output schema config; Stage 1 checks the typed-SOURCE
        producer only (the same gate as ``_edge_field_type_conflict``), because
        a transform producer's runtime output config may be computed rather
        than its raw ``schema:`` block and a raw-block comparison there would
        risk a false red. That narrower reach is a documented abstention, not
        parity (elspeth-2ed41f0a4a).
        """
        try:
            producer_schema_config = get_raw_schema_config(producer.options, owner=_producer_owner(producer))
        except ValueError:
            return None
        if producer_schema_config is None or producer_schema_config.fields is None:
            return None
        mismatches = sorted(
            (field.name, field.field_type)
            for field in producer_schema_config.fields
            if field.name in declared_string_input and field.field_type not in ("str", "any")
        )
        if not mismatches:
            return None
        described = ", ".join(f"'{name}' is declared {field_type}" for name, field_type in mismatches)
        return _err(
            f"node:{node.id}",
            f"Transform '{node.plugin}' (node '{node.id}') scans input fields that must be text, but its upstream "
            f"'{producer.producer_id}' declares them non-string: {described}. Point the scan option at a text column, "
            "or declare the field as 'str' in the upstream schema if the values are genuinely text.",
            "high",
            "transform_string_input_field_type_incompatible",
        )

    def _effective_producer_vote(
        producer: ProducerEntry,
        *,
        visited_fan_in_ids: frozenset[str] = frozenset(),
    ) -> tuple[bool, frozenset[str]]:
        """Return (participates, guarantees) for preview propagation.

        Raw schema blocks are the baseline. For transform/aggregation nodes,
        prefer the plugin's computed output contract when construction succeeds;
        this keeps composer preview aligned with runtime for shape-changing
        producers like field_mapper/json_explode without turning incomplete
        draft configs into hard Stage 1 errors.

        Pass-through parity (ADR-007): for a transform whose plugin class is
        annotated ``passes_through_input=True``, the composer preview must
        mirror the runtime propagation — intersect the effective guarantees
        of upstream producers with the transform's own declared output. If
        the constructor probe fails for a *known* pass-through plugin, the
        composer fails closed (returns ``frozenset()``) so Stage 1 rejects
        the pipeline rather than silently serving a permissive preview that
        would diverge from runtime rejection.
        """
        raw_schema = get_raw_schema_config(
            producer.options,
            owner=_producer_owner(producer),
        )
        raw_guaranteed = get_raw_producer_guaranteed_fields(
            producer.plugin_name,
            producer.options,
            owner=_producer_owner(producer),
        )
        raw_participates = raw_schema is not None and raw_schema.participates_in_propagation
        if not raw_participates and raw_guaranteed:
            # Text-source heuristics can synthesize guarantees even when the
            # observed-mode schema itself abstains.
            raw_participates = True

        if is_source_producer_id(producer.producer_id):
            return raw_participates, raw_guaranteed

        producer_node = node_by_id[producer.producer_id]
        if producer_node.node_type not in {"transform", "aggregation"} or producer_node.plugin is None:
            return raw_participates, raw_guaranteed

        is_known_pass_through = producer_node.plugin in _known_pass_through_plugins()

        transform: TransformProtocol | None = None
        try:
            from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

            transform = get_shared_plugin_manager().create_transform(
                producer_node.plugin,
                prepare_validation_probe_options(producer_node.options),
            )
            is_pass_through_instance = transform.passes_through_input
            output_schema_config = transform._output_schema_config
        except Exception as exc:
            if not _is_config_probe_exception(exc):
                raise
            # Keep Stage 1 tolerant of partially configured draft nodes for
            # non-pass-through transforms — constructor-time errors must not
            # crash preview/export endpoints. For known pass-through plugins
            # we fail closed instead, because returning the raw (more permissive)
            # guarantees would let the composer accept pipelines the runtime
            # would reject.
            #
            # REDACTED: ``str(exc)`` from plugin constructors can carry
            # plugin option values (API URLs, file paths, DSN fragments,
            # occasionally secrets if an option is mis-typed into a connection
            # string), file system paths from credential-file readers, and
            # arbitrary library text routed from third-party validators. The
            # preview response surfaces these warnings directly to the
            # composer UI, where they render into an unauthenticated-
            # reachable error payload (preview is open to any logged-in
            # session owner, not just operators with secret-read grants).
            # Class name only — the triage signal ("something about this
            # plugin's config is wrong") is preserved; detailed diagnosis
            # belongs in server logs, not the UI warning list.
            if producer.producer_id not in contract_probe_failed_producers:
                contract_probe_failed_producers.add(producer.producer_id)
                if is_known_pass_through:
                    contract_warnings.append(
                        _warn(
                            f"node:{producer.producer_id}",
                            f"Computed contract probe for node '{producer.producer_id}' failed during preview "
                            f"({type(exc).__name__}); pipeline rejected "
                            f"(pass-through transform requires successful probe to mirror runtime propagation).",
                            "high",
                        )
                    )
                else:
                    contract_warnings.append(
                        _warn(
                            f"node:{producer.producer_id}",
                            f"Computed contract probe for node '{producer.producer_id}' failed during preview "
                            f"({type(exc).__name__}); falling back to raw schema declarations.",
                            "medium",
                        )
                    )
            if is_known_pass_through:
                return True, frozenset()
            return raw_participates, raw_guaranteed
        finally:
            if transform is not None:
                transform.close()

        if is_pass_through_instance:
            base = output_schema_config.get_effective_guaranteed_fields() if output_schema_config is not None else frozenset()
            inherited_participates, inherited_fields = _connection_propagation_vote(
                producer_node.input,
                visited_fan_in_ids=visited_fan_in_ids,
            )
            # ADR-009 §Clause 1: share the aggregation rule with graph.py.
            # Composer's producer-graph is single-upstream at this level
            # (coalesce absorbs fan-in via pre-computed output), so we pass a
            # one-element predecessor_guarantees list to compose_propagation.
            # An abstaining predecessor contributes None (skipped), not an
            # explicit empty set — same distinction the runtime walker makes.
            own_participates = output_schema_config.participates_in_propagation if output_schema_config is not None else raw_participates
            # Mirror walk_effective_guarantee_vote: a pass-through inherits
            # participation from its predecessors — own vote OR any upstream
            # vote. Dropping the inherited flag would let an own-abstaining
            # pass-through downstream of a participating producer read as
            # abstained here while the runtime marks it participated (and
            # validate_sink_required_fields rejects accordingly).
            return (
                own_participates or inherited_participates,
                compose_propagation(base, [inherited_fields if inherited_participates else None]),
            )

        if output_schema_config is None:
            return raw_participates, raw_guaranteed

        base = output_schema_config.get_effective_guaranteed_fields()
        return output_schema_config.participates_in_propagation, base

    def _effective_producer_guarantees(producer: ProducerEntry) -> frozenset[str]:
        """Return the producer guarantees Stage 1 should compare."""
        _participates, guarantees = _effective_producer_vote(producer)
        return guarantees

    def _known_producer_schema_config(producer: ProducerEntry) -> SchemaConfig | None:
        """Return the runtime producer schema when Composer can prove it.

        The DAG builder assigns each transform/aggregation its computed output
        ``SchemaConfig`` and falls back to the raw declaration only when the
        plugin has no computed output contract. Draft config/probe failures
        abstain: their existing validation paths own the rejection.
        """
        contract_options = producer.options
        contract_owner = _producer_owner(producer)
        if not is_source_producer_id(producer.producer_id):
            producer_node = node_by_id[producer.producer_id]
            if producer_node.node_type == "aggregation":
                try:
                    contract_options, contract_owner = get_aggregation_contract_options(
                        producer.options,
                        owner=f"node:{producer.producer_id}",
                    )
                except ValueError:
                    return None

        try:
            raw_schema = get_raw_schema_config(
                contract_options,
                owner=contract_owner,
            )
        except ValueError:
            return None

        if is_source_producer_id(producer.producer_id):
            schema_config = raw_schema
        else:
            producer_node = node_by_id[producer.producer_id]
            if producer_node.node_type not in {"transform", "aggregation"} or producer_node.plugin is None:
                return None
            constructed, computed_schema = _probe_transform_output_schema(producer_node.plugin, producer_node.options)
            if not constructed:
                return None
            schema_config = computed_schema or raw_schema

        if schema_config is None:
            return None
        return schema_config

    def _known_connection_schema_mode(
        connection_name: str,
        *,
        visited: frozenset[str] = frozenset(),
    ) -> Literal["observed", "explicit"] | None:
        """Resolve a branch connection's mode through structural producers.

        Derived from :func:`_known_connection_schema_config` rather than
        walking the producer graph a second time. The union-coalesce checks
        rely on the two answers describing the same branch set: the mode-mixed
        entry short-circuits the type check, which is only sound while
        "resolves to an explicit mode" and "resolves to a typed schema config"
        cannot disagree. Deriving makes that structural instead of a convention
        two hand-maintained traversals would have to keep in lockstep.
        """
        schema_config = _known_connection_schema_config(connection_name, visited=visited)
        if schema_config is None:
            return None
        if schema_config.is_observed:
            return "observed"
        return "explicit" if schema_config.fields is not None else None

    def _known_connection_schema_config(
        connection_name: str,
        *,
        visited: frozenset[str] = frozenset(),
    ) -> SchemaConfig | None:
        """Resolve a branch's known schema through structural producers."""
        if connection_name in visited:
            return None
        producer = resolver.find_producer_for(connection_name)
        if producer is None:
            return None
        if is_source_producer_id(producer.producer_id):
            return _known_producer_schema_config(producer)

        producer_node = node_by_id[producer.producer_id]
        if producer_node.node_type == "gate":
            return _known_connection_schema_config(
                producer_node.input,
                visited=visited | {connection_name},
            )
        if producer_node.node_type in ("queue", "row_union"):
            return SchemaConfig(mode="observed", fields=None)
        if producer_node.node_type == "coalesce":
            return None
        return _known_producer_schema_config(producer)

    # Runtime rejects a union coalesce whose known branch schemas mix observed
    # and explicit modes. Composer has enough information to mirror that rule
    # for sources, gates, queues, transforms, and aggregations. Unresolved
    # branches are omitted: one known mode plus unknowns abstains, while an
    # observed/explicit conflict already proven by known branches still rejects.
    for coalesce_node in nodes:
        if coalesce_node.node_type != "coalesce" or coalesce_node.merge != "union" or not coalesce_node.branches:
            continue
        branch_modes: dict[str, Literal["observed", "explicit"]] = {}
        for branch_name, branch_connection in zip(
            _coalesce_branch_names(coalesce_node.branches),
            _coalesce_branch_connections(coalesce_node.branches),
            strict=True,
        ):
            mode = _known_connection_schema_mode(branch_connection)
            if mode is None:
                continue
            branch_modes[branch_name] = mode

        observed_branches = sorted(branch for branch, mode in branch_modes.items() if mode == "observed")
        explicit_branches = sorted(branch for branch, mode in branch_modes.items() if mode == "explicit")
        if observed_branches and explicit_branches:
            errors.append(
                _err(
                    f"node:{coalesce_node.id}",
                    f"Coalesce '{coalesce_node.id}' has mixed observed/explicit schemas, which union merge does not allow. "
                    f"Observed branches: {observed_branches}; explicit branches: {explicit_branches}. "
                    "Ensure every branch uses an explicit schema with compatible fields, or every branch uses an observed schema.",
                    "high",
                    "coalesce_schema_mode_mixed",
                )
            )
            # Report the mode conflict alone, mirroring the runtime's ordering
            # (``merge_union_fields`` raises on mode before it builds any typed
            # field set). The tradeoff is deliberate rather than free: a node
            # carrying both defects now costs two repair round-trips. It is
            # taken because a type entry here is only conditionally real —
            # under the "make every branch observed" repair the observed
            # branches stop contributing typed fields and the conflict
            # disappears, so reporting it would send the loop after a target
            # that one of the two valid repairs deletes.
            continue

        # Runtime merges the typed branch fields through the canonical union
        # algorithm and rejects a shared field whose branches declare different
        # types. Composer resolves the same computed producer schemas, so it
        # mirrors that rule here rather than leaving the whole class to the DAG
        # build: a mutation that reports is_valid=true gives the compose loop no
        # reason to repair, which is how battery round-6 g03 handed back a
        # type-incompatible union merge believing it was done
        # (elspeth-85f3cc3022). Non-contributing branches are excluded exactly
        # as ``merge_union_fields`` excludes them.
        branch_typed_fields: dict[str, list[tuple[str, Hashable, bool, bool]]] = {}
        for branch_name, branch_connection in zip(
            _coalesce_branch_names(coalesce_node.branches),
            _coalesce_branch_connections(coalesce_node.branches),
            strict=True,
        ):
            schema_config = _known_connection_schema_config(branch_connection)
            if schema_config is None or schema_config.is_observed or schema_config.fields is None:
                continue
            branch_typed_fields[branch_name] = [
                (field.name, field.field_type, field.required, field.nullable) for field in schema_config.fields
            ]
        if len(branch_typed_fields) < 2:
            continue
        # ``require_all`` is derived from the policy alone, where the runtime
        # uses ``CoalesceSettings.has_all_branch_semantics`` — which is ALSO
        # true for a quorum whose count equals the branch count. The two cannot
        # disagree here: a composer NodeSpec has no ``quorum_count`` field
        # (``yaml_importer`` lists it unsupported) while the runtime makes it
        # mandatory for quorum, so the diverging case is unreachable from this
        # surface. Independently, the conflict raises before either this flag or
        # ``collision_policy`` is read, and the merged flags are discarded here.
        # Both hold today; the first is the one that would still hold if a
        # future caller consumed the returned flags.
        try:
            merge_union_field_flags(
                branch_typed_fields,
                require_all=coalesce_node.policy == "require_all",
                branch_order=_coalesce_branch_names(coalesce_node.branches),
            )
        except UnionTypeConflictError as conflict:
            errors.append(
                _err(
                    f"node:{coalesce_node.id}",
                    f"Coalesce '{coalesce_node.id}' receives incompatible types for field '{conflict.field}' in union merge: "
                    f"branch '{conflict.branch_a}' has {conflict.type_a!r}, branch '{conflict.branch_b}' has {conflict.type_b!r}. "
                    "Union merge requires every branch declaring a shared field to declare the same type for it.",
                    "high",
                    "coalesce_union_type_incompatible",
                    coalesce_union_type=CoalesceUnionTypeDetail(
                        field=conflict.field,
                        branch_a=conflict.branch_a,
                        type_a=str(conflict.type_a),
                        branch_b=conflict.branch_b,
                        type_b=str(conflict.type_b),
                    ),
                )
            )

    # row_union publishes every branch row unchanged into one long-format
    # stream. Exact fixed/fixed schemas need full mutual compatibility.
    # Flexible declarations allow undeclared fields, so only conflicting types
    # on fields both branches explicitly declare are provable. Observed/unknown
    # branches abstain, but cannot hide a conflict between known declarations.
    from itertools import combinations

    from elspeth.core.dag.schema_validation import row_union_schema_configs_compatible

    for row_union_node in nodes:
        if row_union_node.node_type != "row_union" or not row_union_node.branches:
            continue
        explicit_branch_schemas: list[tuple[str, SchemaConfig]] = []
        for branch_name, branch_connection in zip(
            _coalesce_branch_names(row_union_node.branches),
            _coalesce_branch_connections(row_union_node.branches),
            strict=True,
        ):
            schema_config = _known_connection_schema_config(branch_connection)
            if schema_config is None or schema_config.is_observed or schema_config.fields is None:
                continue
            explicit_branch_schemas.append((branch_name, schema_config))
        if len(explicit_branch_schemas) < 2:
            continue
        for (first_branch, first_schema), (other_branch, other_schema) in combinations(explicit_branch_schemas, 2):
            compatible, conflicting_fields, error_msg = row_union_schema_configs_compatible(first_schema, other_schema)
            if compatible:
                continue
            branch_details = tuple(
                _row_union_branch_schema_detail(branch_name, schema_config)
                for branch_name, schema_config in (
                    (first_branch, first_schema),
                    (other_branch, other_schema),
                )
            )
            errors.append(
                _err(
                    f"node:{row_union_node.id}",
                    f"row_union '{row_union_node.id}' has incompatible branch schemas for its long-format stream: "
                    f"branch '{first_branch}' and branch '{other_branch}'; "
                    f"conflicting fields {list(conflicting_fields)}. {error_msg}",
                    "high",
                    "row_union_schema_incompatible",
                    row_union_schema=RowUnionSchemaDetail(
                        branches=branch_details,
                        conflicting_fields=conflicting_fields,
                    ),
                )
            )
            break

    def _connection_propagation_vote(
        connection_name: str,
        *,
        visited_fan_in_ids: frozenset[str] = frozenset(),
    ) -> tuple[bool, frozenset[str]]:
        """Resolve a connection's propagation vote across structural nodes.

        Unlike ``_walk_to_real_producer()``, this helper is only used for
        pass-through inheritance and therefore follows structural fan-out/fan-in
        nodes instead of treating them as preview-stopping boundaries.

        Per ADR-009 §Clause 1, the aggregation rule (``compose_propagation``)
        and the participation predicate (``SchemaConfig.participates_in_propagation``)
        are shared with the runtime walker (``core/dag/guarantees.py``). The
        traversal logic remains separate — the composer walks a producer-graph
        (L3, connection-by-connection) while the runtime walks the DAG (L1,
        multi-predecessor). The two views legitimately differ; unifying
        traversal would pollute layers without eliminating duplication.
        """
        producer = resolver.find_producer_for(connection_name)
        if producer is None:
            return False, frozenset()
        return _producer_entry_propagation_vote(producer, visited_fan_in_ids=visited_fan_in_ids)

    def _producer_entry_propagation_vote(
        producer: ProducerEntry,
        *,
        visited_fan_in_ids: frozenset[str],
    ) -> tuple[bool, frozenset[str]]:
        """Structural dispatch for one producer entry's propagation vote.

        Factored out of ``_connection_propagation_vote`` because queue fan-in
        arms are several producers publishing the SAME connection (the queue
        id), so an arm's vote must be resolved per-entry, not per-connection.

        ``visited_fan_in_ids`` terminates routing loops back into a fan-in node
        — drafts are not DAG-checked at Stage 1, so a composition can route a
        barrier's own output back into one of its branches, and a revisited
        barrier votes conservative abstention instead of recursing unboundedly
        into a /validate 500. All three fan-in kinds share ONE set because node
        ids are unique across kinds, so a kind can only ever turn back its own
        revisit; the guard therefore fires on cycles alone and never abstains
        on an acyclic diamond. Sibling branches each recurse with their own
        ``| {id}`` value, so they cannot mask each other.
        """
        if is_source_producer_id(producer.producer_id):
            return _effective_producer_vote(producer, visited_fan_in_ids=visited_fan_in_ids)

        producer_node = node_by_id[producer.producer_id]
        if producer_node.node_type == "gate":
            return _connection_propagation_vote(producer_node.input, visited_fan_in_ids=visited_fan_in_ids)

        if producer_node.node_type == "queue":
            # Engine parity (83a53388a / elspeth-5a372d3267, mirrored here for
            # elspeth-3619b8774f): the queue is the sanctioned fan-in point,
            # so the walk aggregates arms with fan-in-sound semantics —
            # intersection when every arm participates, total abstention when
            # any arm abstains. ``compose_propagation``'s abstainer-skip is
            # deliberately NOT reused: it is sound only for same-row
            # pass-throughs, and queue rows arrive from exactly one arm, so
            # promoting a single arm's guarantee to the interleaved stream
            # would over-claim — the elspeth-a5b86149d4 hazard this branch
            # previously abstained over entirely.
            if producer_node.id in visited_fan_in_ids:
                return False, frozenset()
            arm_entries = resolver.queue_predecessors(producer_node.id)
            if not arm_entries:
                return False, frozenset()
            arm_votes = [
                _producer_entry_propagation_vote(arm, visited_fan_in_ids=visited_fan_in_ids | {producer_node.id}) for arm in arm_entries
            ]
            if all(arm_participates for arm_participates, _ in arm_votes):
                return True, frozenset.intersection(*[arm_fields for _, arm_fields in arm_votes])
            return False, frozenset()

        if producer_node.node_type == "row_union":
            # Engine parity (elspeth-41bcaa882e, mirroring the queue rule
            # above): row_union releases every branch payload unchanged as one
            # long-format stream, so a field is guaranteed on the stream only
            # when EVERY branch vouches for it — intersection when all
            # branches participate, total abstention when any abstains. It
            # still must not invent guarantees from a SINGLE branch:
            # released rows arrive from exactly one branch, so
            # ``compose_propagation``'s abstainer-skip would over-claim.
            if producer_node.id in visited_fan_in_ids:
                return False, frozenset()
            branch_connections = _coalesce_branch_connections(producer_node.branches)
            if not branch_connections:
                return False, frozenset()
            branch_votes = [
                _connection_propagation_vote(connection, visited_fan_in_ids=visited_fan_in_ids | {producer_node.id})
                for connection in branch_connections
            ]
            if all(branch_participates for branch_participates, _ in branch_votes):
                return True, frozenset.intersection(*[branch_fields for _, branch_fields in branch_votes])
            return False, frozenset()

        if producer_node.node_type == "coalesce":
            if not producer_node.branches or producer_node.id in visited_fan_in_ids:
                return False, frozenset()

            branch_schemas: dict[str, SchemaConfig] = {}
            for branch_name, branch_connection in zip(
                _coalesce_branch_names(producer_node.branches),
                _coalesce_branch_connections(producer_node.branches),
                strict=True,
            ):
                branch_participates, branch_guarantees = _connection_propagation_vote(
                    branch_connection,
                    visited_fan_in_ids=visited_fan_in_ids | {producer_node.id},
                )
                if not branch_participates:
                    continue
                branch_schemas[branch_name] = SchemaConfig(
                    mode="observed",
                    fields=None,
                    guaranteed_fields=tuple(sorted(branch_guarantees)),
                )

            if not branch_schemas:
                return False, frozenset()

            merged = merge_guaranteed_fields(
                branch_schemas,
                require_all=producer_node.policy == "require_all",
            )
            return True, frozenset(merged or ())

        return _effective_producer_vote(producer, visited_fan_in_ids=visited_fan_in_ids)

    def _union_coalesce_merged_guarantees(producer: ProducerEntry) -> frozenset[str] | None:
        """Return a union coalesce's merged guarantee set, or None to abstain.

        The one seam the three coalesce sites below consult, so a union
        coalesce stops being opaque to Rule A/B (elspeth-ae83a6b60c: Stage 1
        abstained at every coalesce while the runtime rejected the identical
        pipeline at build, leaving the authoring loop no error to repair).

        It computes nothing itself. The merge lives in
        ``_producer_entry_propagation_vote``'s coalesce branch, which calls the
        runtime's own ``merge_guaranteed_fields`` on propagation-walked branch
        votes — the same function, on the same shape of branch schemas, that
        the DAG builder stamps the coalesce's guarantees with
        (``core/dag/builder.py``, ``guarantee_branch_schemas``) and that
        ``validate_typed_producer_guaranteed_extras``
        (``core/dag/schema_validation.py``) then enforces. The two surfaces
        read ONE implementation instead of two mirrors free to drift.

        None means "Composer knows nothing here", and every caller keeps its
        pre-existing abstention on it. Three causes:

        * Not a union merge. ``select`` forwards one branch's raw schema and
          ``nested`` keys the merged schema BY BRANCH NAME; the vote mirrors
          neither, so treating them as union would invent top-level guarantees
          and red-line pipelines the runtime runs. ``__post_init__`` defaults
          an unset ``merge`` to "union", so this gate cannot miss a coalesce
          that merely omitted the field.
        * The vote abstained — an unresolvable branch, or a routing cycle the
          fan-in guard turned back.
        * A branch node's contract options do not parse. The Rule A/B call
          sites deliberately let a ValueError crash, because THEIR producer was
          already parsed in the same iteration and a fault there would be a
          non-determinism bug in our own code. A coalesce BRANCH is the other
          case: it may sit later in ``nodes`` than the consumer under check and
          be parsed here for the first time, which is ordinary recoverable
          external input, reported against its real owner by that node's own
          iteration. So this abstains for the same reason
          ``_arm_emit_profile`` does rather than crashing /validate.

        No extras-firewall mirror is needed for the union population:
        ``merge_union_fields`` returns observed or flexible mode and never
        fixed, so a union coalesce's merged schema always allows extras and the
        runtime's firewall skip can never exclude the edge.
        """
        producer_node = resolver.get_node(producer.producer_id)
        if producer_node is None or producer_node.node_type != "coalesce" or producer_node.merge != "union":
            return None
        try:
            participates, merged = _producer_entry_propagation_vote(producer, visited_fan_in_ids=frozenset())
        except ValueError:
            return None
        return merged if participates else None

    def _format_fields(fields: frozenset[str]) -> str:
        return ", ".join(sorted(fields)) if fields else "(none)"

    def _producer_emit_profile(producer: ProducerEntry) -> _ProducerEmitProfile:
        """Return ``(predicted emit set, propagates upstream arrivals, removals)``.

        The emit set is distinct from ``_effective_producer_guarantees``: that
        one returns the producer's *declared* output set
        (``get_effective_guaranteed_fields``, which unions ``guaranteed_fields``
        with declared-required ``fields``). Field-set membership rules need the
        *actual* emission — the set the runtime will see — which equals
        ``_output_schema_config.guaranteed_fields`` for a REDUCTIVE transform
        whose plugin computes its own emit set (``field_mapper`` renaming a
        field away, ``batch_stats``): there the declared-required fields are
        exactly the ones dropped, so using the declared set for downstream
        Rule A/B checks would invent extras the runtime never emits.

        The two sets diverging is NOT by itself an inconsistency to report.
        Rule C below is deliberately gated to ``field_mapper`` with
        ``select_only: true`` for that reason (see its comment): for an
        ADDITIVE plugin ``guaranteed_fields`` is only a LOWER BOUND on
        emission — the fields the transform itself adds, never the ones it
        forwards — so a generic divergence check would mis-attribute every
        passed-through field as missing. Which regime applies is answered by
        ``passes_through_input``, below.

        For sources (no instance) and probe-failed transforms, falls back to
        the declared set — those are the cases where we don't have a separate
        emission inference, and the declared/raw set is the best signal.

        The second element answers the extras-direction question a producer's
        own emit set cannot (elspeth-902fc354b2): does every field arriving at
        this transform's input definitely survive to its emitted rows? True
        iff the transform declares ``passes_through_input=True`` — an ADR-008
        contract the runtime cross-checks, so mis-declaration fails the run
        rather than silently skewing this walk — AND its output contract
        allows extra fields. A non-extras-allowing (``mode: fixed``) contract
        is enforced with ``extra='forbid'`` at the transform's own input
        preflight: rows either match the declared set exactly or die AT this
        node, never downstream of it — so propagation always stops here, and
        Rule A at this node's own locked input owns reporting the extras.

        Behind that firewall the EMIT set splits on the same declaration
        (elspeth-9a8367078f). ``passes_through_input=True`` is defined on
        ``BaseTransform`` as "unconditionally emits every field present on the
        input row, plus ``declared_output_fields``", so declaring it IS the
        statement that the transform is additive; one that may drop, rename or
        filter cannot declare it. For an additive producer the firewall pins
        the arriving row exactly — every declared-required field must be
        present or the row dies here, and the runtime pass-through cross-check
        guarantees it survives — so ``get_effective_guaranteed_fields()``
        (computed guarantees UNION declared-required) is a sound lower bound
        on emission, and predicting the computed set alone instead let a
        locked downstream consumer/sink pass a pipeline the runtime kills on
        row 1. For a reductive producer the plugin-computed
        ``guaranteed_fields`` stays authoritative, because there the
        declared-required fields are the dropped ones. Only when no computed
        set exists does the firewall pin the emit prediction to the declared
        effective set, which also stops the declared-set fallback composing
        upstream fields through it and re-reporting the same defect one
        consumer downstream.

        The THIRD element carries the second propagation regime
        (elspeth-15c72686f2). ``passes_through_input`` is all-or-nothing, so a
        transform that forwards the whole row except one column it consumed
        cannot declare it — line_explode drops its ``source_field``,
        json_explode its ``array_field``, field_mapper its rename sources, and
        batch_outlier_annotator drops whole ROWS rather than fields. Those
        nodes reported ``propagates_upstream=False``, which stopped this walk
        at them and hid an upstream llm's ``<response_field>_usage`` /
        ``_model`` from Rule A: the composition validated clean and every row
        died at the locked sink's per-row preflight. ``forwards_input_fields``
        is the weaker declaration those plugins CAN make, and
        ``removed_input_fields`` names what it subtracts from the propagated
        set. Same firewall gate as the pass-through arm, for the same reason:
        behind ``extra='forbid'`` the arriving row dies at THIS node, so
        nothing propagates past it.
        """
        if is_source_producer_id(producer.producer_id):
            return _ProducerEmitProfile(_effective_producer_guarantees(producer), False, frozenset())

        if producer.producer_id not in node_by_id:
            # Sources have producer_id == "source" and are not members of the
            # locally-built node_by_id map; this is expected internal control
            # flow, not a missing-key anomaly, so fall back to the declared set.
            return _ProducerEmitProfile(_effective_producer_guarantees(producer), False, frozenset())
        producer_node = node_by_id[producer.producer_id]
        if producer_node.node_type == "row_union":
            # The two directions have opposite safety polarities
            # (elspeth-9d13900064): the union's GUARANTEE is the branch
            # intersection (a field must arrive on every released row), but its
            # EMIT set is the branch union — rows from any single arm carry that
            # arm's fields, so a locked consumer forbidding one is a definite
            # runtime rejection. Now that the guarantee walk-back resolves a
            # participating row_union (elspeth-41bcaa882e) instead of abstaining
            # into the dedicated boundary path, this profile must answer with
            # the same arm-emit union that path computes.
            return _ProducerEmitProfile(_row_union_definite_emits(producer_node, visited_connections=frozenset()), False, frozenset())
        if producer_node.node_type == "coalesce":
            # Must precede the ``plugin is None`` fallback below: a coalesce has
            # no plugin, so ordering this after it would return the coalesce
            # node's own (empty) declared set and leave this branch dead code —
            # which is precisely the state that made the walk-back escape above
            # insufficient on its own. ``propagates_upstream=False`` because the
            # vote has already folded in every branch arrival; unioning the
            # coalesce's input arrivals on top would re-report one branch's
            # fields as if they arrived on every row.
            #
            # Runtime mirror: ``core/dag/builder.py`` stamps this same merge on
            # the coalesce via ``merge_guaranteed_fields``, and
            # ``validate_typed_producer_guaranteed_extras``
            # (``core/dag/schema_validation.py``) enforces it against the
            # consumer — the check this profile feeds Rule A/B.
            merged = _union_coalesce_merged_guarantees(producer)
            if merged is not None:
                return _ProducerEmitProfile(merged, False, frozenset())
        if producer_node.plugin is None:
            return _ProducerEmitProfile(_effective_producer_guarantees(producer), False, frozenset())
        if producer_node.node_type not in {"transform", "aggregation"}:
            return _ProducerEmitProfile(_effective_producer_guarantees(producer), False, frozenset())

        try:
            from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

            transform = get_shared_plugin_manager().create_transform(
                producer_node.plugin,
                prepare_validation_probe_options(producer_node.options),
            )
        except Exception as exc:
            if not _is_config_probe_exception(exc):
                raise
            return _ProducerEmitProfile(_effective_producer_guarantees(producer), False, frozenset())

        try:
            output_config = transform._output_schema_config
            passes_through = transform.passes_through_input
            # The forwarding declaration is the weaker sibling of
            # passes_through_input (elspeth-15c72686f2); a plugin that can make
            # the stronger claim never needs this one, so the two are unioned
            # rather than ordered, and `removes` is empty for a pass-through.
            forwards = passes_through or transform.forwards_input_fields
            removes = frozenset() if passes_through else transform.removed_input_fields
            if output_config is None:
                return _ProducerEmitProfile(_effective_producer_guarantees(producer), forwards, removes)
            extras_firewall = not output_config.allows_extra_fields
            propagates = forwards and not extras_firewall
            if output_config.guaranteed_fields is not None:
                if extras_firewall and passes_through:
                    # ADDITIVE behind the firewall: the computed set names only
                    # what this plugin ADDS, so it must be unioned with the
                    # declared-required fields the transform forwards.
                    return _ProducerEmitProfile(output_config.get_effective_guaranteed_fields(), False, frozenset())
                # The plugin computed its own emit set — authoritative, and the
                # only set that stays correct for REDUCTIVE transforms. For a
                # forwarding plugin it names what this node ADDS (line_explode's
                # output_field); the propagated upstream arrives on top of it.
                return _ProducerEmitProfile(frozenset(output_config.guaranteed_fields), propagates, removes)
            if extras_firewall:
                return _ProducerEmitProfile(output_config.get_effective_guaranteed_fields(), False, frozenset())
            # No computed emit set available — fall back to declared.
            return _ProducerEmitProfile(_effective_producer_guarantees(producer), propagates, removes)
        finally:
            transform.close()

    def _arm_emit_profile(producer: ProducerEntry) -> _ProducerEmitProfile:
        """Return one resolved row_union arm's predicted emit profile.

        Tier-3 parse boundary. At the Rule A/B call sites the producer's
        contract config was already parsed earlier in the same iteration, so a
        ValueError there would be a bug in our own code. An arm is different:
        it may sit later in ``nodes`` than the consumer under check, so its
        options are parsed here for the first time and a malformed declaration
        is ordinary recoverable external input. The arm's own node iteration
        reports it as ``contract_config_invalid`` against the right owner —
        re-raising here would instead crash /validate, and re-reporting here
        would duplicate the entry once per downstream consumer. An unparseable
        arm is simply knowledge Composer does not have.
        """
        try:
            return _producer_emit_profile(producer)
        except ValueError:
            return _ProducerEmitProfile(frozenset(), False, frozenset())

    def _connection_definite_emits(
        connection_name: str,
        *,
        visited_connections: frozenset[str],
    ) -> frozenset[str]:
        """Return the fields that will DEFINITELY arrive on ``connection_name``.

        The extras-direction twin of ``_connection_propagation_vote``, and
        deliberately not sharing its math: the two ask questions with opposite
        safety polarities. The presence direction asks "is every required field
        guaranteed?" and must abstain at a fan-in rather than promote one arm's
        guarantee to the whole stream. The extras direction asks "does any
        field arrive that a locked consumer forbids?" — and a field guaranteed
        by ONE arm definitely arrives on that arm's rows, so this UNIONS arm
        emit sets where ``merge_guaranteed_fields`` would intersect them.

        The result is a lower bound: an arm Composer cannot resolve contributes
        nothing. That makes it sound to raise an error ON this set but never
        sound to clear a graph WITH it, which is why the presence walk's
        abstention warning stays unconditional.

        Gates are traversed because routing changes which rows travel an edge,
        never which fields a row carries. Transforms declaring
        ``passes_through_input=True`` (runtime-verified, ADR-008) with an
        extras-allowing output contract are traversed *additively*: their own
        emits union with everything definitely arriving at their input
        (elspeth-902fc354b2 — the runtime rejection this walk predicts fires
        on the ENTIRE arriving row, not on the nearest producer's own emits).
        Transforms declaring ``forwards_input_fields`` traverse the same way
        minus their ``removed_input_fields`` (elspeth-15c72686f2) — the
        weaker declaration verified by the ADR-009 probe harness rather than
        a runtime cross-check.
        """
        if connection_name in visited_connections:
            return frozenset()
        producer = resolver.find_producer_for(connection_name)
        if producer is None:
            return frozenset()
        if is_source_producer_id(producer.producer_id):
            return _arm_emit_profile(producer)[0]

        producer_node = node_by_id[producer.producer_id]
        if producer_node.node_type == "gate":
            return _connection_definite_emits(
                producer_node.input,
                visited_connections=visited_connections | {connection_name},
            )
        if producer_node.node_type == "row_union":
            return _row_union_definite_emits(
                producer_node,
                visited_connections=visited_connections | {connection_name},
            )
        if producer_node.node_type == "coalesce":
            # A union coalesce's merged guarantee DOES definitely arrive, so it
            # must be contributed here — this is the arm that carries it across
            # an intervening extras-allowing pass-through, where the walk-back
            # and the emit profile never see the coalesce at all. Ordered before
            # the opaque arm below, which would otherwise swallow it.
            merged = _union_coalesce_merged_guarantees(producer)
            if merged is not None:
                return merged
        if producer_node.node_type in ("queue", "coalesce"):
            # Opaque to Composer preview: a queue publishes an observed schema
            # and never merges its predecessors' guarantees, and a NON-UNION
            # coalesce's merged output is strategy-specific (``select`` forwards
            # one branch's raw schema, ``nested`` keys fields by branch name).
            # Contributing nothing keeps the lower bound honest. Extending the
            # extras rule to a queue producer is a drop-in branch here, left to
            # the track that owns queue contract semantics.
            return frozenset()
        emit_set, propagates_upstream, removes_upstream = _arm_emit_profile(producer)
        if not propagates_upstream:
            return emit_set
        upstream = _connection_definite_emits(
            producer_node.input,
            visited_connections=visited_connections | {connection_name},
        )
        return emit_set | (upstream - removes_upstream)

    def _row_union_definite_emits(
        row_union_node: NodeSpec,
        *,
        visited_connections: frozenset[str],
    ) -> frozenset[str]:
        """Union every arm's definite emits; a nested row_union arm recurses."""
        branch_connections = _coalesce_branch_connections(row_union_node.branches)
        if not branch_connections:
            return frozenset()
        return frozenset().union(
            *(
                _connection_definite_emits(branch_connection, visited_connections=visited_connections)
                for branch_connection in branch_connections
            )
        )

    def _producer_entry_row_union_boundary(producer: ProducerEntry) -> tuple[NodeSpec, frozenset[str]] | None:
        """Resolve a row_union the presence walk abstained at, and what it emits.

        Returns ``(row_union node, definite emit fields)``, or None when this
        producer has no row_union behind it — including when the presence walk
        abstained at some other boundary (coalesce, queue, routing loop) that
        tells Composer nothing about emitted fields.

        A coalesce needs no sibling of this walker, which is why
        elspeth-ae83a6b60c left it alone: a PARTICIPATING union coalesce no
        longer abstains upstream of here, so it reaches Rule A/B through the
        ordinary resolved-producer path, and an abstaining one has an empty
        guarantee merge on the runtime side too — abstention there is parity,
        not a hole.
        """
        visited_connections: frozenset[str] = frozenset()
        current_producer = producer
        while True:
            if is_source_producer_id(current_producer.producer_id):
                return None
            # ``resolver.get_node`` rather than indexing ``node_by_id``: like
            # ``_walk_producer_entry_to_real_producer`` — the walker this
            # mirrors — this one is also handed ``resolver.sink_producers``
            # entries, which are registered outside the producer map and so
            # are not guaranteed to name a registered NodeSpec.
            producer_node = resolver.get_node(current_producer.producer_id)
            if producer_node is None:
                return None
            if producer_node.node_type == "row_union":
                return producer_node, _row_union_definite_emits(
                    producer_node,
                    visited_connections=visited_connections,
                )
            if producer_node.node_type != "gate" or producer_node.input in visited_connections:
                return None
            visited_connections |= {producer_node.input}
            next_producer = resolver.find_producer_for(producer_node.input)
            if next_producer is None:
                return None
            current_producer = next_producer

    def _row_union_boundary_emits(connection_name: str) -> tuple[NodeSpec, frozenset[str]] | None:
        """Connection-level entry point for ``_producer_entry_row_union_boundary``."""
        producer = resolver.find_producer_for(connection_name)
        if producer is None:
            return None
        return _producer_entry_row_union_boundary(producer)

    def _locked_input_extras_error(
        node: NodeSpec,
        *,
        producer_id: str,
        producer_label: str,
        producer_emit: frozenset[str],
        consumer_locked_input: frozenset[str],
    ) -> ValidationEntry | None:
        """Build Rule A's entry, or None when nothing extra is emitted.

        Shared by the resolved-producer path and the row_union boundary path so
        both report one wording, one error code and one fact shape.
        """
        extras = producer_emit - consumer_locked_input
        if not extras:
            return None
        # When consumer is itself a field_mapper, suggesting "insert a
        # field_mapper upstream" is degenerate — the operator is already
        # at one. The same applies to declared `fields` expansion: for
        # field_mapper, the input contract IS the declared output schema,
        # so widening fields means widening the schema declaration too.
        if node.plugin == "field_mapper":
            fix_suggestion = (
                f"Fix by adding {sorted(extras)!r} to the consumer's schema.fields, "
                f"OR by setting schema.mode: flexible on the consumer, "
                f"OR by adjusting upstream config so the extra field(s) are not emitted."
            )
        else:
            fix_suggestion = (
                "Fix by relaxing the consumer schema (mode: flexible) or by inserting a "
                "field_mapper with select_only: true to drop the extras before this consumer."
            )
        return _err(
            f"node:{node.id}",
            f"Schema contract violation: '{producer_id}' -> '{node.id}'. "
            f"Consumer ({node.plugin or node.node_type}) input is locked (mode: fixed) and accepts: "
            f"[{_format_fields(consumer_locked_input)}]. "
            f"Producer ({producer_label}) will emit: "
            f"[{_format_fields(producer_emit)}]. "
            f"Extra fields rejected by consumer input contract: [{_format_fields(extras)}]. "
            f"{fix_suggestion}",
            "high",
            "locked_input_extras",
            # Identifiers + field names only (see SchemaContractDetail).
            contract=SchemaContractDetail(
                producer=producer_id,
                consumer=node.id,
                extra_fields=tuple(sorted(extras)),
            ),
        )

    def _sink_locked_extras_error(
        output: OutputSpec,
        *,
        producer_id: str,
        producer_label: str,
        producer_emit: frozenset[str],
        sink_locked_input: frozenset[str],
    ) -> ValidationEntry | None:
        """Build Rule B's entry, or None when nothing extra is emitted."""
        extras = producer_emit - sink_locked_input
        if not extras:
            return None
        return _err(
            f"output:{output.name}",
            f"Schema contract violation: '{producer_id}' -> 'output:{output.name}'. "
            f"Sink '{output.name}' input is locked (mode: fixed) and accepts: "
            f"[{_format_fields(sink_locked_input)}]. "
            f"Producer ({producer_label}) will emit: "
            f"[{_format_fields(producer_emit)}]. "
            f"Extra fields rejected by sink input contract: [{_format_fields(extras)}]. "
            f"Fix by relaxing the sink schema (mode: flexible) or by inserting a "
            f"field_mapper with select_only: true to drop the extras before this sink.",
            "high",
            "sink_locked_extras",
            # Identifiers + field names only (see SchemaContractDetail).
            contract=SchemaContractDetail(
                producer=producer_id,
                consumer=f"output:{output.name}",
                extra_fields=tuple(sorted(extras)),
            ),
        )

    # Tier-3 contract-config parse boundary. node.options / output.options are
    # composer/LLM/user-authored config read back from session state, so a
    # malformed schema declaration is recoverable external input, not an
    # invariant break. These helpers convert the parser's ValueError into an
    # explicit (value, ValidationEntry) result the caller aggregates into the
    # validation report — the boundary surfaces the defect as a blocking entry
    # rather than crashing /validate on the first bad node.
    def _parse_node_required_fields(
        node: NodeSpec,
    ) -> tuple[frozenset[str] | None, ValidationEntry | None]:
        try:
            return (
                get_raw_node_required_fields(
                    node.options,
                    owner=f"node:{node.id}",
                    node_type=node.node_type,
                ),
                None,
            )
        except ValueError as exc:
            return None, _err(f"node:{node.id}", f"Invalid contract config: {exc}", "high", "contract_config_invalid")

    def _parse_consumer_locked_input(
        node: NodeSpec,
    ) -> tuple[frozenset[str] | None, ValidationEntry | None]:
        try:
            return _consumer_locked_input_set(node), None
        except ValueError as exc:
            return None, _err(f"node:{node.id}", f"Invalid contract config: {exc}", "high", "contract_config_invalid")

    def _parse_sink_required_fields(
        output: OutputSpec,
    ) -> tuple[frozenset[str] | None, ValidationEntry | None]:
        try:
            return (
                get_raw_sink_required_fields(output.options, owner=f"output:{output.name}"),
                None,
            )
        except ValueError as exc:
            return None, _err(f"output:{output.name}", f"Invalid contract config: {exc}", "high", "contract_config_invalid")

    def _parse_sink_locked_input(
        output: OutputSpec,
    ) -> tuple[frozenset[str] | None, ValidationEntry | None]:
        try:
            return _sink_locked_input_set(output), None
        except ValueError as exc:
            return None, _err(f"output:{output.name}", f"Invalid contract config: {exc}", "high", "contract_config_invalid")

    def _parse_producer_guarantees(
        producer: ProducerEntry,
    ) -> tuple[frozenset[str] | None, ValidationEntry | None]:
        try:
            _participates, guarantees = _producer_entry_propagation_vote(producer, visited_fan_in_ids=frozenset())
            return guarantees, None
        except ValueError as exc:
            return None, _err(_producer_owner(producer), f"Invalid contract config: {exc}", "high", "contract_config_invalid")

    def _parse_producer_vote(
        producer: ProducerEntry,
    ) -> tuple[tuple[bool, frozenset[str]] | None, ValidationEntry | None]:
        """Like ``_parse_producer_guarantees`` but preserves participation.

        The sink required-fields check needs to distinguish "abstained" from
        "participated and guarantees collapsed to empty" — the runtime twin
        (``validate_sink_required_fields``) skips abstaining producers and
        defers to per-row validation, and the composer must mirror that.

        Both parsers resolve through the STRUCTURAL vote so a walked-back
        queue entry (engine-parity fan-in, elspeth-3619b8774f) yields the arm
        intersection; for the source/transform entries the walk-back returned
        historically, the structural dispatch falls through to
        ``_effective_producer_vote`` unchanged.
        """
        try:
            return _producer_entry_propagation_vote(producer, visited_fan_in_ids=frozenset()), None
        except ValueError as exc:
            return None, _err(_producer_owner(producer), f"Invalid contract config: {exc}", "high", "contract_config_invalid")

    def _consumer_effective_required_set(node: NodeSpec) -> frozenset[str]:
        """Return the consumer's EFFECTIVE required-input fields.

        Unlike ``get_raw_node_required_fields`` (explicit ``required_fields``
        only — what runtime Phase-1 name-requirement checking consumes), this
        folds in a fixed/flexible schema's *implicitly* required declared
        fields via ``SchemaConfig.get_effective_required_fields``. Runtime
        marks those declared fields as required on the generated input Pydantic
        model, so Phase-2 type validation rejects a typed producer that does
        not guarantee one. This helper mirrors that, honouring the aggregation
        contract-options alias the rest of the contract pipeline uses.
        """
        contract_options = node.options
        contract_owner = f"node:{node.id}"
        if node.node_type == "aggregation":
            contract_options, contract_owner = get_aggregation_contract_options(node.options, owner=contract_owner)
        schema_config = get_raw_schema_config(contract_options, owner=contract_owner)
        if schema_config is None:
            return frozenset()
        return schema_config.get_effective_required_fields()

    def _parse_consumer_effective_required(
        node: NodeSpec,
    ) -> tuple[frozenset[str] | None, ValidationEntry | None]:
        try:
            return _consumer_effective_required_set(node), None
        except ValueError as exc:
            return None, _err(f"node:{node.id}", f"Invalid contract config: {exc}", "high", "contract_config_invalid")

    def _producer_is_typed_source(producer: ProducerEntry) -> bool:
        """Return whether the producer presents a TYPED (non-observed) schema.

        Mirrors the runtime Phase-2 bypass (``graph.py:1392-1403``): type
        validation fires only when the effective producer schema is a typed
        Pydantic model. A fixed/flexible *source* carries such a typed model;
        observed sources (including text-source auto-guarantee) and
        transform/gate/coalesce producers resolve to a dynamic (None) effective
        producer schema at runtime and are therefore skipped here. Gating on
        producer MODE — not guarantee-emptiness — avoids false rejection of the
        observed-source-with-auto-guaranteed-column case the runtime accepts.
        """
        if not is_source_producer_id(producer.producer_id):
            # transform/aggregation/gate/coalesce producers => dynamic effective
            # producer schema at runtime; the consumer's implicit requirement is
            # not statically enforced against them. Named sources mint
            # ``source:<name>`` producer ids, so match on the predicate, not the
            # literal "source" — else the parity check silently skips every
            # named typed source (elspeth-3332619032).
            return False
        # ABSTAIN on a malformed declaration rather than raising. This predicate
        # is a bool gate, not a reporter: the malformed block owns its rejection
        # through the lazy ``contract_config_invalid`` parsers and the eager
        # syntax sweep, both of which run in this same function. Letting the
        # ValueError escape would leave ``validate()`` — the authoring
        # validator — raising a 500 where its whole contract is to return a
        # verdict, which is the defect class tracked as elspeth-bceffeba19.
        # Safe today only by loop ordering (``_parse_producer_guarantees`` runs
        # first on the same options and ``continue``s); that invariant is
        # implicit, and this range made this predicate MORE load-bearing by
        # gating ``_edge_field_type_conflict`` on it.
        try:
            schema_config = get_raw_schema_config(producer.options, owner=_producer_owner(producer))
        except ValueError:
            return False
        return schema_config is not None and not schema_config.is_observed

    def _edge_field_type_conflict(producer: ProducerEntry, consumer: NodeSpec | OutputSpec) -> ValidationEntry | None:
        """Mirror the runtime's Phase-2 edge TYPE check on declared field specs.

        Stage 1's edge-contract accounting compares field NAMES only, so a
        producer declaring ``age: int`` into a consumer declaring ``age: str``
        validated green while the DAG build raised ``EdgeContractError``
        (elspeth-f2eb8fef9f) — a divergence needing no coalesce, no row_union
        and no special topology. The consumer may be a node OR a sink: the
        first cut covered nodes only, and ``csv(value: str) -> sink(value:
        int)`` — the registry's own Shape-10-adjacent example — stayed green
        (elspeth-2ed41f0a4a census, 2026-08-17). The runtime runs the same
        ``validate_single_edge`` on both edge kinds.

        Compares the DECLARED ``field_type`` strings directly, the way the
        union-coalesce mirror a few hundred lines above does through
        ``merge_union_field_flags`` and the way
        ``row_union_schema_configs_compatible`` does on its non-fixed branch.

        AN EARLIER VERSION RECONSTRUCTED BOTH SIDES AS PluginSchema MODELS VIA
        ``build_coalesce_schema`` AND CALLED ``check_compatibility``. That was
        wrong and produced FALSE REDS, which for a validator gating an LLM
        authoring loop is worse than the gap it closed.
        ``build_coalesce_schema`` widens a field to ``X | None`` when
        ``fd.nullable or not fd.required`` (``core/dag/schema_factory.py``),
        because a coalesce branch can lose a ``last_wins`` collision and yield
        None. The factory that actually builds ordinary source/transform
        schemas — ``plugins/infrastructure/schema_factory.py::_get_python_type``
        — widens ONLY on ``not required`` and never reads ``nullable`` at all.
        So a producer declaring ``{required: true, nullable: true}`` into a
        consumer declaring ``{required: true, nullable: false}``, both ``int``,
        reconstructed as ``int | None`` vs ``int`` and was REJECTED, while the
        real schemas are both plain ``int`` and build fine. Reusing a canonical
        function is only safe when it is canonical FOR THIS EDGE; that one is
        built for coalesce OUTPUT.

        Comparing declared type strings cannot drift that way because it
        reconstructs nothing. It is deliberately the weaker check: it abstains
        wherever either side declares ``any``, and it does not model coercion.
        Under-rejecting is the correct direction here — the runtime remains
        authoritative, and a false red misdirects the authoring loop while a
        missed one is caught downstream.

        Only the type direction is reported. Field NAMES are the surrounding
        loop's job and extras belong to the Rule A/B walkers; reporting either
        here would double-attribute one defect.

        Callers gate on ``_producer_is_typed_source``, which is the runtime's
        own Phase-2 bypass — observed sources and transform/gate/coalesce
        producers resolve to a dynamic effective producer schema at runtime and
        are skipped there, so they are skipped here too.
        """
        if isinstance(consumer, OutputSpec):
            consumer_id = f"output:{consumer.name}"
            consumer_component = consumer_id
        else:
            consumer_id = consumer.id
            consumer_component = f"node:{consumer.id}"
        try:
            producer_schema_config = get_raw_schema_config(producer.options, owner=_producer_owner(producer))
            consumer_options = consumer.options
            consumer_owner = consumer_component
            if isinstance(consumer, NodeSpec) and consumer.node_type == "aggregation":
                consumer_options, consumer_owner = get_aggregation_contract_options(consumer.options, owner=consumer_owner)
            consumer_schema_config = get_raw_schema_config(consumer_options, owner=consumer_owner)
        except ValueError:
            # Malformed declarations own their rejection through the
            # ``contract_config_invalid`` parsers; do not double-report.
            return None

        if producer_schema_config is None or consumer_schema_config is None:
            return None
        if consumer_schema_config.is_observed:
            return None
        if producer_schema_config.fields is None or consumer_schema_config.fields is None:
            return None

        producer_types = {field.name: field.field_type for field in producer_schema_config.fields}
        # ``any`` is a declared abstention on BOTH sides — the author has said
        # the type is not pinned, so no conflict is mechanically provable.
        mismatches = [
            (field.name, field.field_type, producer_types[field.name])
            for field in consumer_schema_config.fields
            if field.name in producer_types
            and field.field_type != "any"
            and producer_types[field.name] != "any"
            and producer_types[field.name] != field.field_type
        ]
        if not mismatches:
            return None
        detail = ", ".join(f"{name} (consumer expects {expected}, producer emits {actual})" for name, expected, actual in mismatches)
        return _err(
            consumer_component,
            f"Schema contract violation: '{producer.producer_id}' -> '{consumer_id}'. Incompatible field types: {detail}.",
            "high",
            "edge_field_type_incompatible",
        )

    for node in nodes:
        consumer_required, consumer_required_error = _parse_node_required_fields(node)
        if consumer_required_error is not None:
            errors.append(consumer_required_error)
            continue
        assert consumer_required is not None  # No error => resolved.

        consumer_locked_input, consumer_locked_error = _parse_consumer_locked_input(node)
        if consumer_locked_error is not None:
            errors.append(consumer_locked_error)
            continue

        consumer_effective_required, consumer_effective_error = _parse_consumer_effective_required(node)
        if consumer_effective_error is not None:
            errors.append(consumer_effective_error)
            continue
        assert consumer_effective_required is not None  # No error => resolved.

        # Projected input declarations (elspeth-ada5a60249). Read off the
        # constructed plugin because neither surface above carries them: the
        # six property-computing configs derive the field name from an ordinary
        # option, so ``required_input_fields`` is empty and the ``schema:``
        # block never names it. Probed only for transforms — sinks declare
        # their input requirements through ``get_raw_sink_required_fields``,
        # checked in the outputs loop below.
        declared_inputs = (
            _probe_transform_declared_inputs(node.plugin, node.options)
            if node.node_type == "transform" and node.plugin is not None
            else _DeclaredInputs(frozenset(), frozenset())
        )
        declared_input = declared_inputs.fields
        declared_string_input = declared_inputs.string_fields

        # ``consumer_effective_required`` folds in a fixed/flexible consumer's
        # *implicitly* required declared fields (which the explicit-only
        # ``consumer_required`` misses), so a flexible consumer — whose input is
        # NOT locked (``consumer_locked_input is None``) and whose explicit
        # ``required_fields`` is empty — still reaches producer resolution.
        # ``declared_input`` joins the same disjunction: a web_scrape with no
        # explicit contract and an unlocked input carries its requirement
        # ONLY there, and omitting it here would leave the rule permanently
        # inert.
        if (
            not consumer_required
            and consumer_locked_input is None
            and not consumer_effective_required
            and not declared_input
            and not declared_string_input
        ):
            continue

        actual_producer = _walk_to_real_producer(
            node.input,
            warnings=contract_warnings,
        )
        if actual_producer is None:
            # The presence walk abstained. That is right for the direction it
            # checks — an arm's guarantee is not the union's — but Rule A runs
            # the opposite polarity, where an arm's guarantee IS decisive
            # (elspeth-9d13900064). Re-resolve the extras direction only.
            if consumer_locked_input is not None:
                boundary = _row_union_boundary_emits(node.input)
                if boundary is not None:
                    boundary_node, boundary_emits = boundary
                    extras_error = _locked_input_extras_error(
                        node,
                        producer_id=boundary_node.id,
                        producer_label=boundary_node.node_type,
                        producer_emit=boundary_emits,
                        consumer_locked_input=consumer_locked_input,
                    )
                    if extras_error is not None:
                        errors.append(extras_error)
            continue
        if actual_producer.producer_id in parse_failed_producers:
            continue

        producer_guaranteed, producer_error = _parse_producer_guarantees(actual_producer)
        if producer_error is not None:
            errors.append(producer_error)
            parse_failed_producers.add(actual_producer.producer_id)
            continue
        assert producer_guaranteed is not None  # No error => guarantees resolved.

        producer_is_typed_source = _producer_is_typed_source(actual_producer)
        contract_required = consumer_required
        if producer_is_typed_source:
            contract_required = consumer_required | consumer_effective_required
            type_error = _edge_field_type_conflict(actual_producer, node)
            if type_error is not None:
                errors.append(type_error)
            if declared_string_input:
                string_type_error = _string_input_field_type_conflict(actual_producer, node, declared_string_input)
                if string_type_error is not None:
                    errors.append(string_type_error)

        if contract_required:
            contract_missing_fields = contract_required - producer_guaranteed
            edge_contracts.append(
                EdgeContract(
                    from_id=actual_producer.producer_id,
                    to_id=node.id,
                    producer_guarantees=tuple(sorted(producer_guaranteed)),
                    consumer_requires=tuple(sorted(contract_required)),
                    missing_fields=tuple(sorted(contract_missing_fields)),
                    satisfied=not contract_missing_fields,
                )
            )

        if consumer_required:
            missing_fields = consumer_required - producer_guaranteed
            if missing_fields:
                error_component = (
                    _producer_owner(actual_producer) if is_source_producer_id(actual_producer.producer_id) else f"node:{node.id}"
                )
                errors.append(
                    _err(
                        error_component,
                        f"Schema contract violation: '{actual_producer.producer_id}' -> '{node.id}'. "
                        f"Consumer ({node.plugin or node.node_type}) requires fields: [{_format_fields(consumer_required)}]. "
                        f"Producer ({_producer_label(actual_producer)}) guarantees: [{_format_fields(producer_guaranteed)}]. "
                        f"Missing fields: [{_format_fields(missing_fields)}].",
                        "high",
                        "schema_contract_violation",
                        # Producer/consumer ids + schema field names are
                        # pipeline identifiers from validated config, never
                        # user row content — safe for the planner's redacted
                        # repair feedback (see SchemaContractDetail).
                        contract=SchemaContractDetail(
                            producer=actual_producer.producer_id,
                            consumer=node.id,
                            missing_fields=tuple(sorted(missing_fields)),
                        ),
                    )
                )

        # Implicit-required parity: a fixed/flexible consumer schema implicitly
        # requires its declared (non-optional) fields. Runtime Phase-2 type
        # validation rejects a TYPED producer that does not guarantee one of
        # them; authoring's explicit-only ``consumer_required`` above misses
        # this. Mirror runtime by checking the consumer's *effective* required
        # set, but only against a typed (non-observed) producer — exactly the
        # producers runtime does NOT bypass (graph.py:1392-1403). Report only
        # the increment beyond the explicit set already handled above, so a
        # field is never double-reported.
        if producer_is_typed_source:
            implicit_missing = consumer_effective_required - consumer_required - producer_guaranteed
            if implicit_missing:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Schema contract violation: '{actual_producer.producer_id}' -> '{node.id}'. "
                        f"Consumer ({node.plugin or node.node_type}) requires fields: [{_format_fields(consumer_effective_required)}]. "
                        f"Producer ({_producer_label(actual_producer)}) guarantees: [{_format_fields(producer_guaranteed)}]. "
                        f"Missing fields: [{_format_fields(implicit_missing)}].",
                        "high",
                        "schema_contract_violation",
                        # Same redaction judgment as the explicit-required
                        # site above: identifiers + field names only.
                        contract=SchemaContractDetail(
                            producer=actual_producer.producer_id,
                            consumer=node.id,
                            missing_fields=tuple(sorted(implicit_missing)),
                        ),
                    )
                )

        # Projected declared-input enforcement (elspeth-ada5a60249). The
        # runtime surface is DeclaredRequiredFieldsContract.pre_emission_check,
        # which subtracts the transform's declared_input_fields from the row's
        # effective fields and raises before process() runs — so a declaration
        # the upstream cannot satisfy fails 100% of rows. Authoring previously
        # accepted it: a web_scrape whose `url_field` named a column no producer
        # emits composed clean, passed /validate, and died on row 1.
        #
        # PARTICIPATION-GATED, unlike the explicit ``consumer_required`` check
        # above, which fails closed against any producer. That asymmetry is the
        # point: an author who writes ``required_input_fields`` has made a
        # promise by hand, whereas this set is DERIVED from ordinary options
        # (blob_csv_expand's ``blob_ref_field`` defaults to ``blob_ref``, so it
        # is non-empty even when the author wrote nothing). Enforcing a derived
        # declaration against an abstaining producer would reject every
        # observed-source pipeline that names an input column — pipelines the
        # engine runs today. Those stay enforced per-row at runtime. This
        # mirrors the sink required-fields gate below and the runtime twin
        # ``validate_transform_declared_input_fields``.
        #
        # Report only the increment beyond the explicit set already handled
        # above, so a field is never double-reported — same discipline as the
        # implicit-required parity block that follows.
        if declared_input:
            # _effective_producer_guarantees(actual_producer) already succeeded
            # above (else we continued via parse_failed_producers), so this
            # producer's contract config parsed cleanly and the vote walks the
            # same paths on the same options deterministically. A ValueError
            # here would be a non-determinism bug in our own code, not a fresh
            # Tier-3 parse fault — same judgment as the Rule A site below.
            producer_participates, _producer_vote_fields = _effective_producer_vote(actual_producer)
            if producer_participates or producer_guaranteed:
                declared_missing = declared_input - consumer_required - producer_guaranteed
                if declared_missing:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Schema contract violation: '{actual_producer.producer_id}' -> '{node.id}'. "
                            f"Consumer ({node.plugin or node.node_type}) requires input fields: "
                            f"[{_format_fields(declared_input)}] (declared by its own options, not by "
                            f"`required_input_fields`). "
                            f"Producer ({_producer_label(actual_producer)}) guarantees: [{_format_fields(producer_guaranteed)}]. "
                            f"Missing fields: [{_format_fields(declared_missing)}]. "
                            f"The engine rejects every row missing one of these before the transform runs, "
                            f"so this pipeline fails on the first row. "
                            f"Fix by pointing the option that names the column (for web_scrape 'url_field', "
                            f"for blob_csv_expand 'blob_ref_field') at a field the upstream emits, OR by "
                            f"adding the field to the upstream's schema.",
                            "high",
                            # Same family and same closed repair-feedback code as
                            # the explicit-required site above: a consumer needs a
                            # field its producer does not deliver.
                            "schema_contract_violation",
                            # Identifiers + field names only, same redaction
                            # judgment as the sibling contract sites.
                            contract=SchemaContractDetail(
                                producer=actual_producer.producer_id,
                                consumer=node.id,
                                missing_fields=tuple(sorted(declared_missing)),
                            ),
                        )
                    )

        # Rule A: producer emits a field that consumer's locked input forbids.
        # The runtime check is the auto-generated input Pydantic model with
        # ``extra="forbid"`` (schema_factory.py: triggered by ``mode: fixed``);
        # composer-time we mirror the same predicate against the fields that
        # definitely ARRIVE at the consumer — the producer's *predicted emit
        # set* (not its declared set — see _producer_emit_profile) plus, for a
        # pass-through producer, everything definitely arriving at its own
        # input (elspeth-902fc354b2: the runtime model validates the entire
        # row, so fields passed through from upstream reject just as hard as
        # fields the producer minted).
        if consumer_locked_input is not None:
            # _effective_producer_guarantees(actual_producer) already succeeded
            # above (else we continued via parse_failed_producers), so this
            # producer's contract config parsed cleanly. _producer_emit_profile
            # walks the same parse paths (create_transform /
            # _effective_producer_guarantees) on the same options,
            # deterministically — any ValueError here would be a
            # non-determinism bug in our own code, not a fresh Tier-3 parse
            # fault, so it is left to crash rather than silently swallowed.
            producer_emit, producer_propagates_upstream, producer_removes_upstream = _producer_emit_profile(actual_producer)
            if producer_propagates_upstream:
                # The profile only reports propagation for transforms it
                # resolved through node_by_id, so this index cannot miss.
                producer_emit |= (
                    _connection_definite_emits(
                        node_by_id[actual_producer.producer_id].input,
                        visited_connections=frozenset({node.input}),
                    )
                    - producer_removes_upstream
                )
            extras_error = _locked_input_extras_error(
                node,
                producer_id=actual_producer.producer_id,
                producer_label=_producer_label(actual_producer),
                producer_emit=producer_emit,
                consumer_locked_input=consumer_locked_input,
            )
            if extras_error is not None:
                errors.append(extras_error)

    for output in outputs:
        sink_required, sink_required_error = _parse_sink_required_fields(output)
        if sink_required_error is not None:
            errors.append(sink_required_error)
            continue

        sink_locked_input, sink_locked_error = _parse_sink_locked_input(output)
        if sink_locked_error is not None:
            errors.append(sink_locked_error)
            continue

        if not sink_required and sink_locked_input is None:
            continue

        sink_producers = resolver.sink_producers(output.name)
        if not sink_producers:
            actual_producer = _walk_to_real_producer(
                output.name,
                warnings=contract_warnings,
            )
            sink_producers = () if actual_producer is None else (actual_producer,)

        # Rule B shares Rule A's walker and therefore shared its row_union
        # fail-open (elspeth-9d13900064). Deduplicated on the boundary node:
        # several routes from one gate converge on a sink as separate producer
        # entries, and all of them resolve back to the same row_union.
        if sink_locked_input is not None:
            seen_sink_boundaries: set[str] = set()
            for sink_producer in sink_producers:
                sink_boundary = _producer_entry_row_union_boundary(sink_producer)
                if sink_boundary is None:
                    continue
                boundary_node, boundary_emits = sink_boundary
                if boundary_node.id in seen_sink_boundaries:
                    continue
                seen_sink_boundaries.add(boundary_node.id)
                sink_extras_error = _sink_locked_extras_error(
                    output,
                    producer_id=boundary_node.id,
                    producer_label=boundary_node.node_type,
                    producer_emit=boundary_emits,
                    sink_locked_input=sink_locked_input,
                )
                if sink_extras_error is not None:
                    errors.append(sink_extras_error)

        seen_sink_contract_producers: set[str] = set()
        for sink_producer in sink_producers:
            actual_producer = _walk_producer_entry_to_real_producer(
                sink_producer,
                connection_name=output.name,
                warnings=contract_warnings,
            )
            if actual_producer is None:
                continue
            # Multiple direct routes from the same producer can converge on one
            # sink. edge_contracts has no route-label field, so emit one
            # producer->sink contract check per real upstream producer.
            if actual_producer.producer_id in seen_sink_contract_producers:
                continue
            seen_sink_contract_producers.add(actual_producer.producer_id)
            if actual_producer.producer_id in parse_failed_producers:
                continue

            producer_vote, producer_error = _parse_producer_vote(actual_producer)
            if producer_error is not None:
                errors.append(producer_error)
                parse_failed_producers.add(actual_producer.producer_id)
                continue
            assert producer_vote is not None  # No error => guarantees resolved.
            producer_participates, producer_guaranteed = producer_vote

            # Field-TYPE conflict on the producer -> sink edge, gated exactly as
            # the node-consumer call above: only a typed (fixed/flexible) SOURCE
            # presents a static schema the runtime's Phase-2 check reads; a
            # transform/gate/coalesce producer is dynamic and skipped there too.
            if _producer_is_typed_source(actual_producer):
                sink_type_error = _edge_field_type_conflict(actual_producer, output)
                if sink_type_error is not None:
                    errors.append(sink_type_error)

            # ADR-007 parity: mirror the runtime abstention clause in
            # validate_sink_required_fields (core/dag/schema_validation.py).
            # An abstaining producer — no guarantees AND no participation, e.g.
            # a select_only field_mapper with an observed schema — makes no
            # static claim; the runtime builds the pipeline and enforces the
            # sink's required fields per-row. Emitting no EdgeContract renders
            # the edge as "not yet checked" rather than asserting a
            # satisfaction verdict the composer cannot support.
            if sink_required and (producer_guaranteed or producer_participates):
                missing_fields = sink_required - producer_guaranteed
                edge_contracts.append(
                    EdgeContract(
                        from_id=actual_producer.producer_id,
                        to_id=f"output:{output.name}",
                        producer_guarantees=tuple(sorted(producer_guaranteed)),
                        consumer_requires=tuple(sorted(sink_required)),
                        missing_fields=tuple(sorted(missing_fields)),
                        satisfied=not missing_fields,
                    )
                )
                if missing_fields:
                    errors.append(
                        _err(
                            f"output:{output.name}",
                            f"Schema contract violation: '{actual_producer.producer_id}' -> 'output:{output.name}'. "
                            f"Sink '{output.name}' requires fields: [{_format_fields(sink_required)}]. "
                            f"Producer ({_producer_label(actual_producer)}) guarantees: [{_format_fields(producer_guaranteed)}]. "
                            f"Missing fields: [{_format_fields(missing_fields)}].",
                            "high",
                            "sink_contract_violation",
                            # Identifiers + field names only (see SchemaContractDetail).
                            contract=SchemaContractDetail(
                                producer=actual_producer.producer_id,
                                consumer=f"output:{output.name}",
                                missing_fields=tuple(sorted(missing_fields)),
                            ),
                        )
                    )

            # Rule B: producer emits a field that sink's locked input forbids.
            # Same predicate as Rule A but routed at the sink boundary; runtime
            # surface is the auto-generated sink Pydantic model with
            # ``extra="forbid"`` triggered by ``mode: fixed`` on the sink schema.
            if sink_locked_input is not None:
                # See Rule A above: _effective_producer_guarantees(actual_producer)
                # already succeeded for this producer in this iteration, so its
                # contract config parsed cleanly. _producer_emit_profile walks the
                # same deterministic parse paths on the same options — a ValueError
                # here would be a non-determinism bug in our own code, not a fresh
                # Tier-3 fault, so it is left to crash rather than silently
                # swallowed. Pass-through producers union in their own input's
                # definite arrivals for the same reason as Rule A
                # (elspeth-902fc354b2).
                sink_producer_emit, sink_producer_propagates_upstream, sink_producer_removes_upstream = _producer_emit_profile(
                    actual_producer
                )
                if sink_producer_propagates_upstream:
                    # The profile only reports propagation for transforms it
                    # resolved through node_by_id, so this index cannot miss.
                    sink_producer_emit |= (
                        _connection_definite_emits(
                            node_by_id[actual_producer.producer_id].input,
                            visited_connections=frozenset({output.name}),
                        )
                        - sink_producer_removes_upstream
                    )
                sink_extras_error = _sink_locked_extras_error(
                    output,
                    producer_id=actual_producer.producer_id,
                    producer_label=_producer_label(actual_producer),
                    producer_emit=sink_producer_emit,
                    sink_locked_input=sink_locked_input,
                )
                if sink_extras_error is not None:
                    errors.append(sink_extras_error)

    # Rule C: per-transform self-consistency between declared output schema
    # and the *actual* predicted emit set, scoped to plugins whose emit set
    # can be computed deterministically from config alone. Currently:
    # ``field_mapper`` with ``select_only=True`` — the actual output is
    # exactly ``mapping.values()``, so any declared output field absent from
    # mapping targets cannot be emitted.
    #
    # Why this is plugin-scoped rather than generic: ``_output_schema_config.
    # guaranteed_fields`` has plugin-specific semantics. For field_mapper it
    # IS the actual emit set (computed by ``_build_field_mapper_output_schema_config``
    # from the mapping). For additive plugins like ``line_explode``/``web_scrape``
    # it is a *lower bound* on emission (only the fields the transform itself
    # adds — passes-through input fields are not enumerated), so a generic
    # ``get_effective_guaranteed_fields() - guaranteed_fields`` check would
    # mis-attribute every passthrough field as "missing". The runtime check
    # ``verify_schema_config_mode`` only sees the actually emitted row, so it
    # does not have this disambiguation problem; we earn that ambiguity-free
    # signal composer-time only by restricting to plugins where emit is fully
    # determined by config.
    #
    # As more reductive plugins land, extend the predicate below — do NOT
    # generalize by removing the plugin gate without first lifting an
    # ``actual_emit_set`` declaration onto each plugin class.
    for node in nodes:
        if node.node_type not in {"transform", "aggregation"} or node.plugin is None:
            continue
        if node.plugin != "field_mapper":
            continue
        if node.id in parse_failed_producers:
            continue
        # Gate on the PLUGIN'S parse of select_only, not on raw-JSON
        # truthiness. The two disagree on exactly the pydantic-False strings
        # ("false"/"False"/"no"/"off"/"0"): ``bool()`` reads every non-empty
        # string as True, and the drifted gate adjudicated a mapper whose
        # parsed select_only is False under this rule's select_only-only
        # jurisdiction, asserting "with select_only: true" about a
        # configuration the node does not have (elspeth-fc3cd7a86c).
        # Constructing the CONFIG asks the single owner of that semantics
        # while still short-circuiting before plugin construction and
        # touching no private plugin instance attributes. An unparseable
        # config is the config-parse rules' to report, never Rule C's.
        node_cfg = _probe_field_mapper_config(node.options)
        if node_cfg is None:
            continue
        if not node_cfg.select_only:
            # Without select_only, field_mapper preserves input fields by
            # default and falls into the additive/loose-bound regime that we
            # cannot adjudicate without knowing the upstream emit set.
            continue
        _constructed, output_config = _probe_transform_output_schema(node.plugin, node.options)
        if output_config is None:
            continue

        predicted_emit = frozenset(output_config.guaranteed_fields or ())
        declared_required = output_config.get_effective_guaranteed_fields()
        missing = declared_required - predicted_emit
        if not missing:
            continue
        errors.append(
            _err(
                f"node:{node.id}",
                f"Transform output guarantee violation: node '{node.id}' ({node.plugin}) declares output fields "
                f"[{_format_fields(declared_required)}] (required) but with select_only: true the mapping can only "
                f"guarantee [{_format_fields(predicted_emit)}]. "
                f"Declared required output fields not guaranteed by this transform: [{_format_fields(missing)}]. "
                f"{_unguaranteed_target_remedies(node.options, missing)}",
                "high",
                "transform_declared_output_not_guaranteed",
                # Self-inconsistency: producer and consumer are the same node.
                # missing_fields are mapping TARGETS the node declares required
                # but cannot guarantee — not names the author necessarily wrote
                # in `schema.fields`, which is why the message resolves each one
                # back to its mapping source before advising anything.
                contract=SchemaContractDetail(
                    producer=node.id,
                    consumer=node.id,
                    missing_fields=tuple(sorted(missing)),
                ),
            )
        )

    # Rule D: a transform whose declared output fields collide with a field that
    # DEFINITELY arrives on its input row (elspeth-cfcd333f83). The runtime
    # surface is TransformExecutor._run_preflight, which calls
    # ``detect_field_collisions(set(input_dict.keys()), transform.declared_output_fields)``
    # and raises PluginContractViolation on the first row. Authoring previously
    # accepted this shape — a "rewrite in place" llm transform whose
    # ``response_field`` names a field the source already emits composed and
    # passed /validate, then died on row 1.
    #
    # Why this is sound rather than a guess, in the two directions that matter:
    #
    # * The output side is an IDENTITY, not an inference. ``declared_output_fields``
    #   is the very attribute the executor tests, read off a constructed instance,
    #   so no per-plugin emit modelling is needed and no plugin gate applies —
    #   unlike Rule C above, which must scope itself because it reasons about
    #   *schema* semantics that differ per plugin. Plugins that legitimately
    #   overwrite a field opt out at the source by keeping the set empty
    #   (``ValueTransform`` does exactly this, precisely so the executor's
    #   collision check does not fire), so Rule D inherits their abstention for
    #   free and cannot second-guess it.
    # * The input side is a LOWER BOUND. ``_connection_definite_emits`` reports
    #   only fields that definitely arrive, contributing nothing for arms
    #   Composer cannot resolve, and ``input_dict`` at the runtime call site is
    #   the whole arriving row (``token.row_data.to_dict()``) with no projection
    #   applied before the collision check. So every field this walk names is
    #   genuinely a key the executor will see, and the intersection of an exact
    #   set with a lower bound is a subset of the real collision set: non-empty
    #   means a guaranteed row-1 failure, never a maybe.
    #
    # Scoped to ``node_type == "transform"`` and NOT to aggregations, which Rule C
    # does include. NOT because aggregations cannot collide — ``batch_replicate``
    # and ``batch_outlier_annotator`` hand-roll the identical check in their own
    # bodies and raise the same message — but because aggregations are reductive:
    # a producer's definite arrivals describe the rows entering the batch, not the
    # row leaving it, so intersecting them with the aggregation's declared outputs
    # is unsound. Widening needs its own argument; see
    # ``validate_transform_output_field_collisions`` in core/dag/schema_validation.py
    # for the runtime-side twin of this decision (elspeth-cfcd333f83).
    for node in nodes:
        if node.node_type != "transform" or node.plugin is None:
            continue
        if node.id in parse_failed_producers:
            continue
        declared_output = _probe_transform_declared_output_fields(node.plugin, node.options)
        if not declared_output:
            continue
        # Seeded empty, NOT with ``{node.input}``: Rule A seeds its own input
        # because it resolves the producer first and unions from the producer's
        # input upward, whereas Rule D starts the walk AT this node's input.
        # Pre-seeding it would trip the visited guard on the first call and make
        # the rule silently inert.
        definite_arrivals = _connection_definite_emits(node.input, visited_connections=frozenset())
        collisions = declared_output & definite_arrivals
        if not collisions:
            continue
        errors.append(
            _err(
                f"node:{node.id}",
                f"Transform contract violation: node '{node.id}' ({node.plugin}) declares output fields "
                f"[{_format_fields(declared_output)}] but [{_format_fields(collisions)}] already arrive(s) on its input row. "
                f"The engine rejects a transform that would overwrite an existing input field, so this pipeline fails on the first row. "
                f"Fix by renaming this transform's output (for an llm transform, set `response_field` to a name the row does not "
                f"already carry, e.g. '{sorted(collisions)[0]}_result'), OR by renaming/dropping the incoming field upstream with a "
                f"field_mapper before this node.",
                "high",
                # Rule C used to share this code, on the reasoning that both are
                # per-transform contracts the node violates on its own and one
                # catalogue entry would enrich both. It does not: the catalogue
                # is keyed on the CODE, so a single entry was served to both, and
                # a Rule D rejection on an `llm` node received field_mapper
                # advice naming `mapping` and `select_only` — options that node
                # does not have. Rule C now carries
                # ``transform_declared_output_not_guaranteed``; this code stays
                # here, where the runtime parity manifest already binds it to
                # ``validate_transform_output_field_collisions``
                # (elspeth-920bd88299).
                "transform_contract_violation",
                # producer and consumer are both this node: the colliding field
                # can arrive from several arms at once (a row_union unions them),
                # so naming any single upstream producer would be arbitrary. The
                # defect is node-scoped — this transform cannot run on the rows
                # its own input delivers.
                contract=SchemaContractDetail(
                    producer=node.id,
                    consumer=node.id,
                    extra_fields=tuple(sorted(collisions)),
                ),
            )
        )

    # Eager schema-SYNTAX sweep (elspeth-33738eedb6). Every parser above is
    # LAZY: it resolves a declaration only when some contract comparison needs
    # it. So a declared schema block that nothing consumes was never parsed at
    # all, and a malformed field spec validated GREEN — `source -> sink` with a
    # plain unschema'd sink is among the most common pipeline shapes — then
    # died at plugin construction with PluginConfigError. A declared schema
    # block is authored config: its SYNTAX is checkable with no topology
    # context whatsoever, so it must not depend on who reads it.
    #
    # Deduped by OWNER against the lazy parsers above: where a contract
    # comparison already reported contract_config_invalid for an owner, the
    # sweep stays silent for that owner entirely. One owner therefore owes at
    # most one contract-config error per pass — a node carrying both a bad
    # required_input_fields and a bad schema block reports only the first
    # defect reached; the second surfaces on the following pass once the
    # first is fixed.
    schema_config_reported = {error.component for error in errors if error.error_code == "contract_config_invalid"}

    def _sweep_schema_syntax(owner: str, options: Mapping[str, Any], *, node_type: str | None = None) -> None:
        if owner in schema_config_reported:
            return
        try:
            contract_options = options
            if node_type == "aggregation":
                contract_options, _ = get_aggregation_contract_options(options, owner=owner)
            get_raw_schema_config(contract_options, owner=owner)
        except ValueError as exc:
            schema_config_reported.add(owner)
            errors.append(_err(owner, f"Invalid contract config: {exc}", "high", "contract_config_invalid"))

    for sweep_source_name, sweep_source in sources.items():
        _sweep_schema_syntax(source_producer_id(sweep_source_name), sweep_source.options)
    for sweep_node in nodes:
        _sweep_schema_syntax(f"node:{sweep_node.id}", sweep_node.options, node_type=sweep_node.node_type)
    for sweep_output in outputs:
        _sweep_schema_syntax(f"output:{sweep_output.name}", sweep_output.options)

    return tuple(errors), tuple(contract_warnings), tuple(edge_contracts)


@dataclass(frozen=True, slots=True, init=False)
class CompositionState:
    """Immutable, versioned snapshot of a pipeline under construction.

    Every edit produces a new instance with incremented version.
    All container fields are deep-frozen via freeze_fields().

    Attributes:
        sources: Named source roots keyed by stable composer/audit-visible name.
        nodes: Ordered tuple of transform, gate, aggregation, coalesce,
            row_union, and queue nodes.
        edges: Connections between nodes.
        outputs: Sink configurations.
        metadata: Pipeline name and description.
        version: Monotonically increasing per session, starting at 1.
        guided_session: Optional guided-mode session pointer. None for freeform
            sessions; set to GuidedSession.initial() at session-create time for
            guided sessions (spec §5.2).
    """

    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    outputs: tuple[OutputSpec, ...]
    metadata: PipelineMetadata
    version: int
    guided_session: GuidedSession | None = None
    sources: Mapping[str, SourceSpec] = field(default_factory=dict)
    # Write-once memo slot for ``pipeline_proposal.composition_content_hash``:
    # the hash serializes the whole state, and preflight identity keys rebuild
    # it several times per composer turn. Content is immutable per instance,
    # and every mutation constructor (``replace``/``with_*``) re-runs
    # ``__init__``, which resets the memo — so a stale hash can never survive
    # onto a modified copy. Excluded from comparison and repr: it is derived
    # state, not composition content.
    _content_hash_memo: str | None = field(default=None, init=False, compare=False, repr=False)

    def __init__(
        self,
        *,
        nodes: tuple[NodeSpec, ...],
        edges: tuple[EdgeSpec, ...],
        outputs: tuple[OutputSpec, ...],
        metadata: PipelineMetadata,
        version: int,
        guided_session: GuidedSession | None = None,
        sources: Mapping[str, SourceSpec] | None = None,
        source: SourceSpec | None = None,
    ) -> None:
        if version < 1:
            raise ValueError(f"CompositionState.version must be >= 1, got {version}")
        if source is not None and sources:
            raise ValueError("CompositionState accepts either source or sources, not both")
        source_map = {"source": source} if source is not None else dict(sources or {})
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "outputs", outputs)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "guided_session", guided_session)
        object.__setattr__(self, "sources", source_map)
        object.__setattr__(self, "_content_hash_memo", None)
        freeze_fields(self, "sources")

    # --- Mutation methods ---

    def with_source(self, source: SourceSpec) -> CompositionState:
        """Return new state with the default named source, version incremented."""
        return self.with_named_source("source", source)

    def without_source(self) -> CompositionState:
        """Return new state with all sources removed, version incremented."""
        return replace(self, sources={}, version=self.version + 1)

    def with_named_source(self, source_name: str, source: SourceSpec) -> CompositionState:
        """Add or replace a named source root. Version incremented."""
        validate_composer_source_name(source_name)
        sources = dict(self.sources)
        sources[source_name] = source
        return replace(self, sources=sources, version=self.version + 1)

    def without_named_source(self, source_name: str) -> CompositionState | None:
        """Remove one named source. Returns None if the source is not found."""
        if source_name not in self.sources:
            return None
        sources = dict(self.sources)
        del sources[source_name]
        return replace(self, sources=sources, version=self.version + 1)

    def with_node(self, node: NodeSpec) -> CompositionState:
        """Add or replace a node (matched by id). Version incremented."""
        existing_ids = [n.id for n in self.nodes]
        if node.id in existing_ids:
            # Replace at original position to preserve order
            idx = existing_ids.index(node.id)
            node_list = list(self.nodes)
            node_list[idx] = node
            nodes = tuple(node_list)
        else:
            # Append new node
            nodes = (*self.nodes, node)
        return replace(self, nodes=nodes, version=self.version + 1)

    def without_node(self, node_id: str) -> CompositionState | None:
        """Remove node by id. Returns None if node not found."""
        if not any(n.id == node_id for n in self.nodes):
            return None
        nodes = tuple(n for n in self.nodes if n.id != node_id)
        # Also remove edges referencing this node
        edges = tuple(e for e in self.edges if e.from_node != node_id and e.to_node != node_id)
        return replace(self, nodes=nodes, edges=edges, version=self.version + 1)

    def with_edge(self, edge: EdgeSpec) -> CompositionState:
        """Add or replace an edge (matched by id). Version incremented."""
        existing_ids = [e.id for e in self.edges]
        if edge.id in existing_ids:
            idx = existing_ids.index(edge.id)
            edge_list = list(self.edges)
            edge_list[idx] = edge
            edges = tuple(edge_list)
        else:
            edges = (*self.edges, edge)
        return replace(self, edges=edges, version=self.version + 1)

    def without_edge(self, edge_id: str) -> CompositionState | None:
        """Remove edge by id. Returns None if edge not found."""
        if not any(e.id == edge_id for e in self.edges):
            return None
        edges = tuple(e for e in self.edges if e.id != edge_id)
        return replace(self, edges=edges, version=self.version + 1)

    def with_output(self, output: OutputSpec) -> CompositionState:
        """Add or replace an output (matched by name). Version incremented."""
        existing_names = [o.name for o in self.outputs]
        if output.name in existing_names:
            idx = existing_names.index(output.name)
            output_list = list(self.outputs)
            output_list[idx] = output
            outputs = tuple(output_list)
        else:
            outputs = (*self.outputs, output)
        return replace(self, outputs=outputs, version=self.version + 1)

    def without_output(self, output_name: str) -> CompositionState | None:
        """Remove output by name. Returns None if output not found."""
        if not any(o.name == output_name for o in self.outputs):
            return None
        outputs = tuple(o for o in self.outputs if o.name != output_name)
        return replace(self, outputs=outputs, version=self.version + 1)

    def with_metadata(self, patch: dict[str, Any]) -> CompositionState:
        """Update metadata fields from partial dict. Version incremented."""
        current = self.metadata
        name = patch["name"] if "name" in patch else current.name
        description = patch["description"] if "description" in patch else current.description
        new_meta = PipelineMetadata(
            name=name,
            description=description,
        )
        return replace(self, metadata=new_meta, version=self.version + 1)

    # --- Serialization ---

    def to_dict(self) -> dict[str, Any]:
        """Recursively unwrap frozen containers to plain Python types.

        Converts MappingProxyType -> dict, tuple -> list recursively.
        The result is suitable for yaml.dump() and JSON serialization.
        """

        result: dict[str, Any] = {
            "version": self.version,
            "metadata": {
                "name": self.metadata.name,
                "description": self.metadata.description,
            },
            "sources": {},
            "nodes": [],
            "edges": [],
            "outputs": [],
        }

        for source_name, source in self.sources.items():
            source_dict: dict[str, Any] = {
                "plugin": source.plugin,
                "on_success": source.on_success,
                "options": deep_thaw(source.options),
                "on_validation_failure": source.on_validation_failure,
            }
            # Optional fields serialise only when present so states authored
            # before ``description`` existed keep byte-identical dicts (and
            # therefore stable composition_content_hash values).
            if source.description is not None:
                source_dict["description"] = source.description
            result["sources"][source_name] = source_dict

        for node in self.nodes:
            node_dict: dict[str, Any] = {
                "id": node.id,
                "node_type": node.node_type,
                "plugin": node.plugin,
                "input": node.input,
                "on_success": node.on_success,
                "on_error": node.on_error,
                "options": deep_thaw(node.options),
            }
            if node.condition is not None:
                node_dict["condition"] = node.condition
            if node.routes is not None:
                node_dict["routes"] = deep_thaw(node.routes)
            if node.fork_to is not None:
                node_dict["fork_to"] = list(node.fork_to)
            if node.branches is not None:
                node_dict["branches"] = _serialize_branches(node.branches)
            if node.policy is not None:
                node_dict["policy"] = node.policy
            if node.merge is not None:
                node_dict["merge"] = node.merge
            if node.trigger is not None:
                node_dict["trigger"] = deep_thaw(node.trigger)
            if node.output_mode is not None:
                node_dict["output_mode"] = node.output_mode
            if node.expected_output_count is not None:
                node_dict["expected_output_count"] = node.expected_output_count
            if node.timeout_seconds is not None:
                node_dict["timeout_seconds"] = node.timeout_seconds
            if node.description is not None:
                node_dict["description"] = node.description
            result["nodes"].append(node_dict)

        for edge in self.edges:
            result["edges"].append(
                {
                    "id": edge.id,
                    "from_node": edge.from_node,
                    "to_node": edge.to_node,
                    "edge_type": edge.edge_type,
                    "label": edge.label,
                }
            )

        for output in self.outputs:
            output_dict: dict[str, Any] = {
                "name": output.name,
                "plugin": output.plugin,
                "options": deep_thaw(output.options),
                "on_write_failure": output.on_write_failure,
            }
            if output.description is not None:
                output_dict["description"] = output.description
            result["outputs"].append(output_dict)

        return result

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        """Reconstruct from a plain dict (inverse of to_dict serialisation).

        Calls from_dict() on each nested Spec type. This is the only way
        to construct CompositionState from deserialised JSON (Spec AC #18).

        The round-trip is CONTENT-exact, not identity-exact, in both directions,
        and callers that need either property must say which:

        * ``from_dict(to_dict(state)) == state`` fails whenever ``state`` carries
          a ``guided_session`` — ``to_dict`` never emits it and this never
          restores it. It is carried on the ``composer_meta`` side channel
          instead (``sessions/converters.py``).
        * ``to_dict(from_dict(payload)) == payload`` fails for any payload the
          spec constructors normalise (coalesce ``merge``/``policy`` defaults,
          ``row_union`` list branches), for any key not declared by the spec
          (silently dropped), and for an optional written out as an explicit
          null (``to_dict`` encodes absence by omission).

        The second direction is load-bearing for persisted authorities, whose
        bytes are hash-bound and therefore cannot be re-normalised in place; see
        ``pipeline_proposal.restore_owned_composition_state_authority``
        (elspeth-da00e1c1cb).
        """
        raw_sources = d["sources"] if "sources" in d and d["sources"] is not None else {}
        if not raw_sources and "source" in d and d["source"] is not None:
            raw_sources = {"source": d["source"]}
        sources = {name: SourceSpec.from_dict(source) for name, source in raw_sources.items()}
        return cls(
            sources=sources,
            nodes=tuple(NodeSpec.from_dict(n) for n in d["nodes"]),
            edges=tuple(EdgeSpec.from_dict(e) for e in d["edges"]),
            outputs=tuple(OutputSpec.from_dict(o) for o in d["outputs"]),
            metadata=PipelineMetadata.from_dict(d["metadata"]),
            version=d["version"],
        )

    # --- Validation ---

    def validate(self) -> ValidationSummary:
        """Run Stage 1 composition-time validation.

        Pure function of the current state — no DAG build or session mutation.
        Returns ValidationSummary with is_valid and human-readable errors.
        """
        errors: list[ValidationEntry] = []
        _err = ValidationEntry  # local alias for brevity
        invalid_row_union_branch_nodes: set[str] = set()

        # 1. Source exists
        if not self.sources:
            errors.append(_err("source", "No source configured.", "high", "no_source_configured"))
        for source_name in self.sources:
            source_name_error = _composer_source_name_validation_message(source_name)
            if source_name_error is not None:
                component = "source" if source_name == "source" else f"source:{source_name}"
                errors.append(_err(component, source_name_error, "high", "source_name_invalid"))

        # 1b. Every routing label the runtime settings model validates
        # (elspeth-2ed41f0a4a): connection/route/branch labels and sink names.
        errors.extend(_routing_label_errors(sources=self.sources, nodes=self.nodes, outputs=self.outputs))

        # 1c. Collection caps — ``ElspethSettings``/``GateSettings`` declare
        # them as pydantic ``Field(max_length=...)`` constraints, which reject
        # at settings load with no raise site of their own (elspeth-2ed41f0a4a).
        errors.extend(_collection_cap_errors(sources=self.sources, nodes=self.nodes, outputs=self.outputs))

        # 2. At least one output
        if not self.outputs:
            errors.append(_err("pipeline", "No sinks configured.", "high", "no_sinks_configured"))

        # 3. Edge references valid
        node_ids = {n.id for n in self.nodes}
        output_names = {o.name for o in self.outputs}
        valid_from = node_ids | set(self.sources) | {"source"}
        valid_to = node_ids | output_names
        for edge in self.edges:
            if edge.from_node not in valid_from:
                errors.append(
                    _err(
                        f"edge:{edge.id}",
                        f"Edge '{edge.id}' references unknown node '{edge.from_node}' as from_node.",
                        "high",
                        "edge_unknown_node",
                    )
                )
            if edge.to_node not in valid_to:
                errors.append(
                    _err(
                        f"edge:{edge.id}",
                        f"Edge '{edge.id}' references unknown node '{edge.to_node}' as to_node.",
                        "high",
                        "edge_unknown_node",
                    )
                )

        # 4. Node IDs unique and runtime-valid
        seen_node_ids: set[str] = set()
        # The runtime requires ONE namespace across every node kind, sources
        # and sinks (ElspethSettings.validate_globally_unique_node_names);
        # Stage 1 checked node-vs-node and queue-vs-source only
        # (elspeth-2ed41f0a4a census, 2026-08-17).
        source_names = frozenset(self.sources)
        for node in self.nodes:
            if node.id in seen_node_ids:
                errors.append(_err(f"node:{node.id}", f"Duplicate node ID: '{node.id}'.", "high", "duplicate_node_id"))
            elif node.id in output_names or (node.id in source_names and node.node_type != "queue"):
                other = "sink" if node.id in output_names else "source"
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Node name '{node.id}' is used by both {node.node_type} and {other}. All node names must be "
                        "unique across transforms, gates, aggregations, coalesce nodes, row_union nodes, sources, queues, and sinks.",
                        "high",
                        "node_id_collides_with_source_or_sink",
                    )
                )
            node_id_message = _composer_node_id_validation_message(node.id, node.node_type)
            if node_id_message is not None:
                errors.append(_err(f"node:{node.id}", node_id_message, "high", "node_id_invalid"))
            if node.id == "source" or node.id.startswith("source:"):
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Reserved node id '{node.id}' cannot use the source producer namespace.",
                        "high",
                        "reserved_node_id",
                    )
                )
            seen_node_ids.add(node.id)

        # 5. Output names unique
        seen_output_names: set[str] = set()
        for output in self.outputs:
            if output.name in seen_output_names:
                errors.append(_err(f"output:{output.name}", f"Duplicate output name: '{output.name}'.", "high", "duplicate_output_name"))
            seen_output_names.add(output.name)

        # 6. Edge IDs unique
        seen_edge_ids: set[str] = set()
        for edge in self.edges:
            if edge.id in seen_edge_ids:
                errors.append(_err(f"edge:{edge.id}", f"Duplicate edge ID: '{edge.id}'.", "high", "duplicate_edge_id"))
            seen_edge_ids.add(edge.id)

        # 7. Node type field consistency
        for node in self.nodes:
            if node.node_type not in COMPOSER_NODE_TYPES:
                expected = ", ".join(sorted(COMPOSER_NODE_TYPES))
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Node '{node.id}' has unknown node_type '{node.node_type}'. Expected one of: {expected}.",
                        "high",
                        "unknown_node_type",
                    )
                )
                continue

            # Authored pipeline_decision reviews must use a registered decision
            # term: the resolve-side artifact-hash registry raises on unknown
            # terms, so a novel term mints an unresolvable review event and
            # wedges the session at the run gate.
            from elspeth.web.interpretation_state import (
                REGISTERED_PIPELINE_DECISION_USER_TERMS,
                composer_pipeline_decision_user_term_error,
            )

            authored_requirements = node.options.get("interpretation_requirements")
            if isinstance(authored_requirements, (list, tuple)):
                for requirement in authored_requirements:
                    if not isinstance(requirement, Mapping):
                        continue
                    if requirement.get("kind") != "pipeline_decision":
                        continue
                    term = requirement.get("user_term")
                    if not isinstance(term, str) or term.strip() not in REGISTERED_PIPELINE_DECISION_USER_TERMS:
                        repair = (
                            composer_pipeline_decision_user_term_error(
                                user_term=term,
                                context=f"Node {node.id!r}",
                            )
                            or "The pipeline_decision user_term is not registered."
                            if isinstance(term, str)
                            else "The pipeline_decision user_term must be a registered string."
                        )
                        errors.append(
                            _err(
                                f"node:{node.id}",
                                repair,
                                "high",
                                "pipeline_decision_unregistered",
                            )
                        )

            batch_placement_error = _batch_aware_placement_error(node.id, node.node_type, node.plugin, node.output_mode)
            if batch_placement_error is not None:
                errors.append(_err(f"node:{node.id}", batch_placement_error, "high", "batch_transform_misplaced"))

            batch_required_error = _batch_aware_required_input_fields_error(node.id, node.plugin, node.options)
            if batch_required_error is not None:
                errors.append(_err(f"node:{node.id}", batch_required_error, "high", "batch_required_fields_invalid"))

            abuse_contact_error = _validate_web_scrape_abuse_contact_not_reserved(node)
            if abuse_contact_error is not None:
                errors.append(abuse_contact_error)
            errors.extend(_validate_web_scrape_http_identity_not_placeholder(node))

            errors.extend(_validate_prompt_template_variable_bindings(node))
            errors.extend(_validate_multi_query_template_variable_bindings(node))

            # ``timeout_seconds`` is a top-level structural-barrier field.
            # Queue rejects it through queue_node_contract_error below so every
            # queue consumer shares the same canonical-shape guard.
            if node.timeout_seconds is not None and node.node_type not in ("coalesce", "row_union", "queue"):
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Node '{node.id}' of type '{node.node_type}' does not accept top-level timeout_seconds; "
                        "only coalesce and row_union nodes accept that field.",
                        "high",
                        "node_timeout_unsupported",
                    )
                )

            structural_plugin_error = structural_node_plugin_error(node)
            if structural_plugin_error is not None:
                errors.append(_err(f"node:{node.id}", structural_plugin_error, "high", "structural_node_plugin_forbidden"))

            if node.node_type == "gate":
                if node.condition is None:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Gate '{node.id}' is missing required field 'condition'.",
                            "high",
                            "gate_missing_condition",
                        )
                    )
                else:
                    # Validate expression content — defense-in-depth catches
                    # malformed conditions from any entry path (including
                    # session deserialization).
                    expr_error = _validate_gate_expression(node.condition)
                    if expr_error is not None:
                        errors.append(_err(f"node:{node.id}", f"Gate '{node.id}': {expr_error}", "high", "gate_condition_invalid"))
                    elif node.routes is not None:
                        # Route-label / condition-return-type parity — mirror of
                        # GateSettings.validate_boolean_routes so the composer does
                        # not green-light a pipeline runtime config later rejects.
                        parity_error = _validate_gate_route_parity(node.condition, node.routes)
                        if parity_error is not None:
                            errors.append(
                                _err(f"node:{node.id}", f"Gate '{node.id}': {parity_error}", "high", "gate_route_labels_mismatch")
                            )
                if node.routes is None:
                    errors.append(
                        _err(f"node:{node.id}", f"Gate '{node.id}' is missing required field 'routes'.", "high", "gate_missing_routes")
                    )
                elif not node.routes:
                    # GateSettings.validate_routes requires at least one entry;
                    # an empty mapping is a deterministic runtime rejection.
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Gate '{node.id}' routes must have at least one entry.",
                            "high",
                            "gate_routes_empty",
                        )
                    )
                else:
                    # Mirror GateSettings.validate_fork_consistency: 'fork'
                    # route destinations and fork_to require each other.
                    has_fork_route = any(target == _FORK_ROUTE_TARGET for target in node.routes.values())
                    if has_fork_route and not node.fork_to:
                        errors.append(
                            _err(
                                f"node:{node.id}",
                                f"Gate '{node.id}' routes to 'fork' but fork_to is missing: fork_to is required "
                                "when any route destination is 'fork'.",
                                "high",
                                "gate_fork_route_without_fork_to",
                            )
                        )
                    if node.fork_to and not has_fork_route:
                        errors.append(
                            _err(
                                f"node:{node.id}",
                                f"Gate '{node.id}' declares fork_to but no route destination is 'fork': "
                                "fork_to is only valid when a route destination is 'fork'.",
                                "high",
                                "gate_fork_to_without_fork_route",
                            )
                        )
                    # An EMPTY fork_to is not "no fork" — the runtime settings
                    # model rejects it (GateSettings.validate_fork_to_labels)
                    # and, before that guard existed, it crashed the DAG
                    # builder (elspeth-2ed41f0a4a). Both consistency checks
                    # above read ``()`` as falsy, so this needs its own arm.
                    if node.fork_to is not None and len(node.fork_to) == 0:
                        errors.append(
                            _err(
                                f"node:{node.id}",
                                f"Gate '{node.id}' fork_to must not be an empty list; omit it (or use null) for no fork.",
                                "high",
                                "gate_fork_to_empty",
                            )
                        )
            elif node.node_type == "transform":
                # Negative constraints — transforms must not have gate fields
                if node.condition is not None:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Transform '{node.id}' must not have 'condition' field.",
                            "high",
                            "transform_unexpected_condition",
                        )
                    )
                if node.routes is not None:
                    errors.append(
                        _err(
                            f"node:{node.id}", f"Transform '{node.id}' must not have 'routes' field.", "high", "transform_unexpected_routes"
                        )
                    )
                # Positive constraints — engine requires these as non-empty strings
                # (TransformSettings.plugin, .on_success, .on_error in config.py
                #  — field validators call .strip() and reject empty/blank)
                if not node.plugin:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Transform '{node.id}' is missing required field 'plugin'.",
                            "high",
                            "transform_missing_plugin",
                        )
                    )
                if not node.on_success or not node.on_success.strip():
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Transform '{node.id}' is missing required field 'on_success'.",
                            "high",
                            "transform_missing_on_success",
                        )
                    )
                if not node.on_error or not node.on_error.strip():
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Transform '{node.id}' is missing required field 'on_error'.",
                            "high",
                            "transform_missing_on_error",
                        )
                    )
            elif node.node_type == "coalesce":
                if node.branches is None:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}' is missing required field 'branches'.",
                            "high",
                            "coalesce_missing_branches",
                        )
                    )
                elif len(dict.fromkeys(_coalesce_branch_names(node.branches))) < 2:
                    # ``CoalesceSettings.branches`` is ``Field(min_length=2)`` —
                    # a DECLARATIVE constraint, so no raise site names it and
                    # the AST census could not see it; found by probe
                    # (elspeth-2ed41f0a4a, 2026-08-17). A one-branch coalesce
                    # merges nothing and the runtime refuses it at settings load.
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}' requires at least two branches.",
                            "high",
                            "coalesce_branches_invalid",
                        )
                    )
                # Mirror the engine's closed vocabularies (core/config.py
                # CoalesceSettings) at composition time: a committed value
                # outside them passes composer validation but fails engine
                # pre-run validation — valid-but-not-runnable. An UNSET policy
                # is not one of those values: ``__post_init__`` has already
                # normalised it to the runtime's own default.
                #
                # A value INSIDE the runtime vocabulary can still be unrunnable
                # as authored (elspeth-2ed41f0a4a census, 2026-08-17). The
                # runtime couples ``quorum`` to ``quorum_count`` and ``select``
                # to ``select_branch`` (``CoalesceSettings.validate_policy_requirements``
                # / ``validate_merge_requirements``); ``NodeSpec`` carries
                # neither field and the YAML importer lists both as
                # unsupported, so those two menu items can NEVER run from this
                # surface and are rejected outright with the reason. ``best_effort``
                # is coupled to ``timeout_seconds``, which NodeSpec CAN carry, so
                # that one is a plain coupling check.
                if node.policy == "quorum":
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}' policy 'quorum' requires quorum_count, which the composer cannot author. "
                            "Use require_all, best_effort (with timeout_seconds), or first.",
                            "high",
                            "coalesce_policy_quorum_unsupported",
                        )
                    )
                elif node.policy not in ("require_all", "best_effort", "first"):
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}' policy {node.policy!r} is not a valid policy. "
                            "Valid values: require_all, best_effort, first.",
                            "high",
                            "coalesce_policy_invalid",
                        )
                    )
                if node.policy == "best_effort" and node.timeout_seconds is None:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}': best_effort policy requires timeout_seconds.",
                            "high",
                            "coalesce_best_effort_requires_timeout",
                        )
                    )
                if node.merge == "select":
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}' merge 'select' requires select_branch, which the composer cannot author. "
                            "Use union or nested.",
                            "high",
                            "coalesce_merge_select_unsupported",
                        )
                    )
                elif node.merge is not None and node.merge not in ("union", "nested"):
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}' merge {node.merge!r} is not a valid merge mode. Valid values: union, nested.",
                            "high",
                            "coalesce_merge_invalid",
                        )
                    )
                if node.timeout_seconds is not None and _timeout_seconds_is_invalid(node.timeout_seconds):
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}' timeout_seconds must be a finite positive number or None.",
                            "high",
                            "coalesce_timeout_invalid",
                        )
                    )
            elif node.node_type == "row_union":
                try:
                    if not node.id or not node.id.strip():
                        raise ValueError("row_union name must not be empty")
                    row_union_name = node.id
                    _validate_max_length(
                        row_union_name,
                        field_label="row_union name",
                        max_length=_MAX_NODE_NAME_LENGTH,
                    )
                    _validate_node_name_chars(row_union_name, field_label="row_union name")
                    if row_union_name in _RESERVED_EDGE_LABELS:
                        raise ValueError(f"row_union name '{row_union_name}' is reserved. Reserved: {sorted(_RESERVED_EDGE_LABELS)}")
                    if row_union_name.startswith("__"):
                        raise ValueError(f"row_union name '{row_union_name}' starts with '__', which is reserved for system edges")
                except ValueError as exc:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            str(exc),
                            "high",
                            "row_union_name_invalid",
                        )
                    )

                forbidden = {
                    "plugin": node.plugin,
                    "on_error": node.on_error,
                    "condition": node.condition,
                    "routes": node.routes,
                    "fork_to": node.fork_to,
                    "policy": node.policy,
                    "merge": node.merge,
                    "trigger": node.trigger,
                    "output_mode": node.output_mode,
                    "expected_output_count": node.expected_output_count,
                }
                present = sorted(name for name, value in forbidden.items() if value is not None)
                if node.options:
                    present.append("options")
                if present:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"row_union '{node.id}' does not accept field(s): {sorted(present)}.",
                            "high",
                            "row_union_config_invalid",
                        )
                    )

                branch_names = _coalesce_branch_names(node.branches)
                branch_connections = _coalesce_branch_connections(node.branches)
                if len(branch_names) < 2:
                    invalid_row_union_branch_nodes.add(node.id)
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"row_union '{node.id}' requires at least two ordered branches.",
                            "high",
                            "row_union_branches_invalid",
                        )
                    )
                elif any(branch_name in branch_names[:index] for index, branch_name in enumerate(branch_names)):
                    invalid_row_union_branch_nodes.add(node.id)
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"row_union '{node.id}' branch aliases must be unique.",
                            "high",
                            "row_union_branches_invalid",
                        )
                    )

                for branch_name, connection_name in zip(branch_names, branch_connections, strict=True):
                    for value, field_label in (
                        (branch_name, "branch name"),
                        (connection_name, f"branch '{branch_name}' input connection"),
                    ):
                        try:
                            if type(value) is not str or not value.strip():
                                raise ValueError(f"row_union {field_label} must be a non-empty string")
                            _validate_connection_or_sink_name(
                                value,
                                field_label=f"row_union {field_label}",
                            )
                        except ValueError as exc:
                            invalid_row_union_branch_nodes.add(node.id)
                            errors.append(
                                _err(
                                    f"node:{node.id}",
                                    str(exc),
                                    "high",
                                    "row_union_branch_invalid",
                                )
                            )

                if branch_connections and node.input != branch_connections[0]:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"row_union '{node.id}' input must equal its first branch connection "
                            f"'{branch_connections[0]}'; input is only a serialization placeholder.",
                            "high",
                            "row_union_input_mismatch",
                        )
                    )

                if type(node.on_success) is not str or not node.on_success.strip():
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"row_union '{node.id}' requires a non-empty on_success processing connection.",
                            "high",
                            "row_union_on_success_invalid",
                        )
                    )
                else:
                    try:
                        _validate_connection_or_sink_name(
                            node.on_success,
                            field_label="row_union on_success connection name",
                        )
                    except ValueError as exc:
                        errors.append(
                            _err(
                                f"node:{node.id}",
                                str(exc),
                                "high",
                                "row_union_on_success_invalid",
                            )
                        )

                if node.timeout_seconds is not None and _timeout_seconds_is_invalid(node.timeout_seconds):
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"row_union '{node.id}' timeout_seconds must be a finite positive number or None.",
                            "high",
                            "row_union_timeout_invalid",
                        )
                    )
            elif node.node_type == "aggregation":
                if not node.plugin:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Aggregation '{node.id}' is missing required field 'plugin'.",
                            "high",
                            "aggregation_missing_plugin",
                        )
                    )
                # Engine requires on_error as non-empty string
                # (AggregationSettings.on_error in config.py)
                if not node.on_error or not node.on_error.strip():
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Aggregation '{node.id}' is missing required field 'on_error'.",
                            "high",
                            "aggregation_missing_on_error",
                        )
                    )
                # Runtime treats a missing/empty trigger as end-of-source-only.
                # If early triggers are present, validate them through the same
                # TriggerConfig parser used by settings load.
                if node.trigger is not None:
                    trigger_error = _validate_aggregation_trigger(node.id, node.trigger)
                    if trigger_error is not None:
                        errors.append(trigger_error)
                # output_mode must be a valid OutputMode value when present
                if node.output_mode is not None and node.output_mode not in ("passthrough", "transform"):
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Aggregation '{node.id}' output_mode must be 'passthrough' or 'transform', got '{node.output_mode}'.",
                            "high",
                            "aggregation_output_mode_invalid",
                        )
                    )
            elif node.node_type == "queue":
                # Intrinsic (topology-free) queue shape: id == input, no
                # plugin/routing, description-only options (elspeth-a5b86149d4).
                # Producer/consumer/namespace checks run in the queue-structure
                # block after connection completeness.
                queue_error = queue_node_contract_error(node)
                if queue_error is not None:
                    errors.append(_err(f"node:{node.id}", queue_error, "high", "queue_config_invalid"))

        errors.extend(_validate_runtime_route_destinations(self.sources, self.nodes, self.outputs))
        errors.extend(_validate_edge_route_contract(self.sources, self.nodes, self.outputs, self.edges))

        # 8. Connection completeness
        runtime_connections = _runtime_connection_targets(self.sources, self.nodes)
        for candidate in self.nodes:
            if candidate.node_type != "gate" or candidate.fork_to is None:
                continue
            duplicate_branches = sorted(branch for branch, count in Counter(candidate.fork_to).items() if count > 1)
            if duplicate_branches:
                errors.append(
                    _err(
                        f"node:{candidate.id}",
                        f"Gate '{candidate.id}' has duplicate fork branches: {duplicate_branches}. Each fork branch name must be unique.",
                        "high",
                        "gate_duplicate_fork_branch",
                    )
                )
        gate_fork_branches_by_id = {
            candidate.id: frozenset(candidate.fork_to)
            for candidate in self.nodes
            if candidate.node_type == "gate" and candidate.fork_to is not None
        }
        gate_fork_branches = {branch for branches in gate_fork_branches_by_id.values() for branch in branches}

        # Fork branch names are GLOBALLY unique across gates (builder.py
        # "Fork branch '{}' is declared by multiple gates"). The
        # duplicate-producer accounting catches most of this incidentally, but
        # it deliberately skips branch names that are also SINK names — so two
        # gates each forking to ['main', 'other'] over declared sinks validated
        # green (elspeth-2ed41f0a4a census, 2026-08-17). Check the rule
        # directly, in gate declaration order, so the report matches the
        # runtime's "'first' and 'second'".
        fork_branch_owner: dict[str, str] = {}
        for candidate in self.nodes:
            if candidate.node_type != "gate" or candidate.fork_to is None:
                continue
            for branch in dict.fromkeys(candidate.fork_to):
                owner = fork_branch_owner.get(branch)
                if owner is not None and owner != candidate.id:
                    errors.append(
                        _err(
                            f"node:{candidate.id}",
                            f"Fork branch '{branch}' is declared by multiple gates: '{owner}' and '{candidate.id}'. "
                            "Fork branch names must be globally unique across all gates.",
                            "high",
                            "fork_branch_declared_by_multiple_gates",
                        )
                    )
                    continue
                fork_branch_owner[branch] = candidate.id

        # Mirror the engine's one-barrier-per-fork-branch rule. The DAG builder
        # raises GraphValidationError when a branch name is claimed twice —
        # by two coalesces, by a coalesce and a row_union, or by two row_unions
        # ("Each fork branch can only join at one barrier"): the branch's
        # arrival is delivered to exactly one barrier's pending map, so a
        # second claimant has no runtime meaning. The engine compares raw
        # branch NAMES before any reachability reasoning, so this check is
        # unconditional too — a dual claim is invalid whether or not the
        # aliases resolve to a gate fork_to. This is a cross-node TOPOLOGY
        # finding, so it carries its own code rather than either barrier's
        # intrinsic node-shape code: completing one barrier from an unrelated
        # node must not roll that node's mutation back.
        barrier_claimants: dict[str, list[str]] = {}
        for node in self.nodes:
            if node.node_type not in ("coalesce", "row_union"):
                continue
            # dict.fromkeys dedupes within a single barrier: a repeated alias
            # inside one node is that node's own intrinsic branches error.
            for branch_alias in dict.fromkeys(_coalesce_branch_names(node.branches)):
                barrier_claimants.setdefault(branch_alias, []).append(node.id)
        for branch_alias, claimants in barrier_claimants.items():
            if len(claimants) < 2:
                continue
            errors.append(
                _err(
                    f"node:{claimants[1]}",
                    f"Fork branch '{branch_alias}' is claimed by more than one barrier: {claimants}. "
                    "Each fork branch may join at exactly one coalesce or row_union. "
                    "Drop the branch from every barrier but one, or fork a distinct branch name per barrier.",
                    "high",
                    "fork_branch_multiple_barriers",
                )
            )

        # Stage-1 mirror of the DAG builder's whole-roster fork closure rule
        # (core/dag/builder.py "WHOLE-ROSTER FORK CLOSURE", spec §7 rule 2 /
        # ruling 23): a fork gate is either fully bound — every fork_to
        # branch closes at the SAME barrier, roster-equal — or fully unbound
        # (pure fan-out to sinks). Reuses gate_fork_branches_by_id and
        # barrier_claimants computed above; a branch claimed by >1 barrier is
        # already reported by fork_branch_multiple_barriers above, so this
        # picks the first claimant and does not duplicate that finding.
        barrier_node_by_id = {node.id: node for node in self.nodes if node.node_type in ("coalesce", "row_union")}
        fork_closure_sink_names = {output.name for output in self.outputs}
        # Fourth path (spec §7 E2): a branch consumed by an ordinary
        # downstream transform/gate is unbound (pure fan-out), same as a
        # direct sink match — mirrors builder.py's WHOLE-ROSTER FORK CLOSURE,
        # which classifies unbound identically regardless of WHY it is
        # unbound (sink match vs. consumer-fed).
        fork_closure_consumer_fed_inputs = {node.input for node in self.nodes if node.node_type in ("transform", "gate")}
        # closer_gate_rosters accumulates every fork gate that contributes a
        # branch to a given closer, checked in one closer-centric pass below
        # rather than per-gate — mirrors core/dag/builder.py's rule-2
        # restructuring (maintainer ruling 2026-08-23): a closer whose
        # roster is produced by more than one gate can never equal any
        # single contributing gate's own fork_to, so a per-gate compare
        # would let a multi-gate roster whose union happens to equal the
        # declared set slip through as "legal".
        closer_gate_rosters: dict[str, dict[str, list[str]]] = {}
        for gate_id, fork_branches in gate_fork_branches_by_id.items():
            closer_labels: dict[str, str] = {}
            unbound_branches: list[str] = []
            for branch in fork_branches:
                if branch in barrier_claimants and barrier_claimants[branch]:
                    branch_claimants = barrier_claimants[branch]
                    first_claimant = branch_claimants[0]
                    claimant_node = barrier_node_by_id[first_claimant] if first_claimant in barrier_node_by_id else None
                    claimant_kind = claimant_node.node_type if claimant_node is not None else "coalesce"
                    closer_labels[branch] = f"{claimant_kind}:{first_claimant}"
                elif branch in fork_closure_sink_names or branch in fork_closure_consumer_fed_inputs:
                    unbound_branches.append(branch)
                # Neither a barrier alias, a sink name, nor a downstream
                # consumer: an undeclared destination, owned by the
                # runtime-connection/edge-route checks elsewhere in this
                # method. Skip it here rather than report a misleading
                # mixed-closure/roster finding on top.
            if closer_labels and unbound_branches:
                errors.append(
                    _err(
                        f"node:{gate_id}",
                        f"Fork gate '{gate_id}' has mixed closure: branches {sorted(closer_labels)} close at a "
                        f"barrier while branches {sorted(unbound_branches)} go direct to a sink or an ordinary "
                        "consumer. A fork is either "
                        "fully bound — every declared branch flows to the fork's single closer — or fully "
                        "unbound (pure fan-out). Route every branch to the closer, or none (spec §7 rule 2).",
                        "high",
                        "fork_mixed_closure_invalid",
                    )
                )
                continue
            distinct_closers = sorted(set(closer_labels.values()))
            if len(distinct_closers) > 1:
                errors.append(
                    _err(
                        f"node:{gate_id}",
                        f"Fork gate '{gate_id}' closes at multiple barriers: {distinct_closers}. "
                        "A fork closes entirely at ONE closer (spec §7 rule 2). Split into nested forks — an "
                        "outer pure fan-out whose branches each contain their own fork→closer pair.",
                        "high",
                        "fork_multiple_closers_invalid",
                    )
                )
                continue
            if len(distinct_closers) == 1:
                closer_label = distinct_closers[0]
                if closer_label not in closer_gate_rosters:
                    closer_gate_rosters[closer_label] = {}
                closer_gate_rosters[closer_label][gate_id] = sorted(fork_branches)

        # Roster equality, checked once per CLOSER across every contributing
        # gate. "One gate, no orphans" is the only legal shape: mixed
        # closure and multi-closer splits are already ruled out above, so a
        # single contributing gate's own roster is always a subset of the
        # closer's declared roster — legality collapses to that one case.
        for closer_label, gate_rosters in closer_gate_rosters.items():
            closer_kind, _, closer_id = closer_label.partition(":")
            closer_node = barrier_node_by_id[closer_id] if closer_id in barrier_node_by_id else None
            declared = set(_coalesce_branch_names(closer_node.branches)) if closer_node is not None else set()
            produced: set[str] = set()
            for roster in gate_rosters.values():
                produced.update(roster)
            orphaned = sorted(declared - produced)
            if len(gate_rosters) == 1 and not orphaned:
                # Whole-roster fork closure holds — this pair is a candidate
                # bound region. Stage-1 mirror of spec §7 rule 4's sink-inside
                # limb only (backward walk + no-path limb `abstains`; see
                # generation.py's parity note): a plain in-region routing
                # gate can still leak straight to a sink without ever
                # forking, exactly the shape rule 2 above cannot see.
                if closer_node is not None:
                    _gate_id, roster = next(iter(gate_rosters.items()))
                    # F1 anchor fix (review, 2026-08-23): widen the seed set
                    # with any of the gate's OWN non-fork route targets that
                    # are themselves backward-reachable from the closer — the
                    # roster-only seed missed an intermediate non-fork gate
                    # re-entering the region before the closer, the same
                    # blind spot the runtime walk had.
                    widened_roster = list(roster)
                    gate_node = next((n for n in self.nodes if n.id == _gate_id), None)
                    if gate_node is not None and gate_node.routes is not None:
                        backward_reach = _closer_backward_reach_connections(self.nodes, closer_node)
                        for route_target in gate_node.routes.values():
                            if route_target in (_DISCARD_ROUTE_TARGET, _FORK_ROUTE_TARGET, *roster):
                                continue
                            if route_target in backward_reach and route_target not in widened_roster:
                                widened_roster.append(route_target)
                    sink_hit = _fork_branch_reaches_sink_before_closer(widened_roster, closer_id, self.nodes, fork_closure_sink_names)
                    if sink_hit is not None:
                        errors.append(
                            _err(
                                f"node:{closer_id}",
                                f"A path inside bound group '{closer_id}' (fork branches {roster}) reaches sink "
                                f"'{sink_hit}' before the group's closer. No token may leave a bound region except "
                                f"through its closer — sinks inside a bound region are rejected flat (spec §7 rule 4). "
                                f"Route the in-region chain to '{closer_id}' and move the sink after it.",
                                "high",
                                "bound_region_sink_inside",
                            )
                        )
                continue
            closer_word = "Coalesce" if closer_kind == "coalesce" else "row_union"
            gate_summary = "; ".join(f"'{name}' declares {roster}" for name, roster in sorted(gate_rosters.items()))
            orphan_clause = f"; no gate produces {orphaned}" if orphaned else ""
            errors.append(
                _err(
                    f"node:{closer_id}",
                    f"{closer_word} '{closer_id}' roster mismatch: closer declares {sorted(declared)}, "
                    f"drawn from {len(gate_rosters)} fork gate(s): {gate_summary}{orphan_clause}. Whole-roster "
                    f"closure requires the closer's branches to come from exactly ONE gate's fork_to, with the "
                    f"rosters exactly equal (spec §7 rule 2).",
                    "high",
                    "fork_roster_mismatch",
                )
            )

        for node in self.nodes:
            if node.node_type == "coalesce":
                missing_branches = sorted(
                    branch for branch in _coalesce_branch_connections(node.branches) if branch not in runtime_connections
                )
                if missing_branches:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}' branches {missing_branches} are not reachable from any runtime connection.",
                            "high",
                            "coalesce_branch_unreachable",
                        )
                    )
                # Branch ALIASES (the mapping keys) must each be a gate
                # fork_to name — the row_union twin of this rule already
                # existed (``row_union_branch_alias_unreachable``); the
                # coalesce side checked only the VALUES against runtime
                # connections, so a coalesce declaring a branch no gate forks
                # validated green and died at the DAG build ("Coalesce '{}'
                # declares branch '{}', but no gate produces this branch";
                # elspeth-2ed41f0a4a census, 2026-08-17).
                missing_aliases = sorted(
                    branch for branch in dict.fromkeys(_coalesce_branch_names(node.branches)) if branch not in gate_fork_branches
                )
                if missing_aliases:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"Coalesce '{node.id}' declares branches {missing_aliases} that no gate produces via fork_to. "
                            "Branches must be listed in a gate's fork_to.",
                            "high",
                            "coalesce_branch_alias_unreachable",
                        )
                    )
                # Spec §7 rule 6 (ruling 25): aggregators are banned inside
                # EVERY bound region, both output modes — flat, unlike the
                # row_union twin below (`row_union_branch_aggregation_invalid`),
                # which only flags output_mode: transform (that check targets
                # a NARROWER, transform-mode-specific hazard: a single
                # buffered parent's flush colliding on one row_id). This
                # check stays beside it, keyed to coalesce-bound branches —
                # scope/collector regions are not yet composer-authorable
                # (Task 12), so they are not covered here either.
                if not missing_branches and not missing_aliases:
                    coalesce_branch_aliases = _coalesce_branch_names(node.branches)
                    coalesce_branch_connections = _coalesce_branch_connections(node.branches)
                    coalesce_branch_aggregations: dict[str, tuple[NodeSpec, str]] = {}
                    for branch_alias, branch_connection in zip(coalesce_branch_aliases, coalesce_branch_connections, strict=True):
                        is_downstream, lineage = _runtime_connection_lineage(branch_alias, branch_connection, self.sources, self.nodes)
                        if not is_downstream:
                            continue
                        for ancestor in lineage:
                            if ancestor.node_type == "aggregation" and ancestor.id not in coalesce_branch_aggregations:
                                coalesce_branch_aggregations[ancestor.id] = (ancestor, branch_alias)
                    for aggregation, branch_alias in coalesce_branch_aggregations.values():
                        errors.append(
                            _err(
                                f"node:{node.id}",
                                f"Aggregation '{aggregation.id}' is inside fork branch '{branch_alias}' that feeds "
                                f"coalesce '{node.id}'. Aggregators are banned inside all bound regions (spec §7 "
                                "rule 6, ruling 25): a batch flush consumes members the group's roster must "
                                f"account for. Move '{aggregation.id}' before the fork or after "
                                f"'{node.id}''s release; for an in-region N->M batch, use a scoped multi-row "
                                "transform closed by a collector.",
                                "high",
                                "bound_region_aggregation_invalid",
                            )
                        )
                continue
            if node.node_type == "row_union":
                # Intrinsic validation owns malformed external branch values.
                # Do not pass them into topology set/sort/walk operations,
                # which assume runtime-valid strings.
                if node.id in invalid_row_union_branch_nodes:
                    continue
                branch_aliases = _coalesce_branch_names(node.branches)
                branch_connections = _coalesce_branch_connections(node.branches)
                missing_aliases = sorted(branch for branch in branch_aliases if branch not in gate_fork_branches)
                if missing_aliases:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"row_union '{node.id}' branch aliases {missing_aliases} are not produced by any gate fork_to.",
                            "high",
                            "row_union_branch_alias_unreachable",
                        )
                    )
                missing_branches = sorted(branch for branch in branch_connections if branch not in runtime_connections)
                if missing_branches:
                    errors.append(
                        _err(
                            f"node:{node.id}",
                            f"row_union '{node.id}' branch connections {missing_branches} are not reachable from any runtime connection.",
                            "high",
                            "row_union_branch_unreachable",
                        )
                    )
                # Downstream-lineage checks. These are topology findings like
                # their unreachable siblings above, so they carry their own
                # codes: sharing the intrinsic ``row_union_branch_invalid``
                # code put them in the tool layer's mutation-blocking
                # preflight, where completing the topology from an unrelated
                # node rolled that node's mutation back with an error naming
                # the mis-wired row_union.
                #
                # The correlation-origin check that used to gate this section
                # (aliases must share one common gate fork_to) is deleted:
                # the Stage-1 mirror of core/dag/builder.py's WHOLE-ROSTER
                # FORK CLOSURE rule (fork_roster_mismatch, above) provably
                # pre-empts it for every shape it caught — a closer whose
                # roster spans more than one gate's fork_to can never equal
                # any single contributing gate's own roster (maintainer
                # ruling 2026-08-23; dead code deleted per prerelease
                # no-dead-code doctrine, mirroring the builder-side deletion
                # of the same analysis). Lineage/aggregation/nested-fork
                # checks below don't depend on a single common origin, so
                # they now run unconditionally once aliases/connections are
                # reachable.
                if not missing_aliases and not missing_branches:
                    branch_lineages: list[tuple[str, tuple[NodeSpec, ...]]] = []
                    lineage_is_valid = True
                    for branch_alias, branch_connection in zip(branch_aliases, branch_connections, strict=True):
                        is_downstream, lineage = _runtime_connection_lineage(
                            branch_alias,
                            branch_connection,
                            self.sources,
                            self.nodes,
                        )
                        if is_downstream:
                            branch_lineages.append((branch_alias, lineage))
                            continue
                        lineage_is_valid = False
                        errors.append(
                            _err(
                                f"node:{node.id}",
                                f"row_union '{node.id}' branch alias '{branch_alias}' maps to input connection "
                                f"'{branch_connection}', which is not downstream of that alias's fork edge. "
                                "Wire each branches[alias] value through processing that starts at the same gate fork branch.",
                                "high",
                                "row_union_branch_not_downstream",
                            )
                        )
                    if lineage_is_valid:
                        branch_aggregations: dict[str, tuple[NodeSpec, str]] = {}
                        nested_forks: dict[str, tuple[NodeSpec, str]] = {}
                        # Spec §7 rule 6 (ruling 25): unlike branch_aggregations
                        # above (transform-mode only — a narrower, DIFFERENT
                        # hazard: single-buffered-parent identity collision),
                        # ruling 25 bans aggregators inside every bound region
                        # regardless of output_mode. Collected separately so a
                        # transform-mode node can be reported by BOTH checks —
                        # both rejections are correct for that node — with this
                        # loop's own errors.append() running strictly AFTER
                        # branch_aggregations' below, so the twin's more
                        # specific message stays FIRST in the errors list for
                        # the overlap case (pinned by
                        # test_transform_mode_overlap_reports_the_specific_twin_message_first).
                        row_union_branch_any_mode_aggregations: dict[str, tuple[NodeSpec, str]] = {}
                        for branch_alias, lineage in branch_lineages:
                            for ancestor in lineage:
                                if ancestor.node_type == "aggregation" and ancestor.output_mode in (None, "transform"):
                                    branch_aggregations.setdefault(ancestor.id, (ancestor, branch_alias))
                                if ancestor.node_type == "aggregation" and ancestor.id not in row_union_branch_any_mode_aggregations:
                                    row_union_branch_any_mode_aggregations[ancestor.id] = (ancestor, branch_alias)
                                if ancestor.node_type == "gate" and ancestor.fork_to:
                                    nested_forks.setdefault(ancestor.id, (ancestor, branch_alias))
                        for aggregation, branch_alias in branch_aggregations.values():
                            errors.append(
                                _err(
                                    f"node:{node.id}",
                                    f"Aggregation '{aggregation.id}' is inside fork branch '{branch_alias}' that feeds "
                                    f"row_union '{node.id}' and uses output_mode 'transform' (the default). "
                                    "A transform-mode flush emits its rows from a single buffered parent token, "
                                    "so every emitted row carries that parent's row_id and the union group can never "
                                    f"be satisfied. Move '{aggregation.id}' upstream of the originating fork, or "
                                    "downstream of its release — aggregators are banned inside every bound region "
                                    "regardless of output_mode (spec §7 rule 6).",
                                    "high",
                                    "row_union_branch_aggregation_invalid",
                                )
                            )
                        for aggregation, branch_alias in row_union_branch_any_mode_aggregations.values():
                            errors.append(
                                _err(
                                    f"node:{node.id}",
                                    f"Aggregation '{aggregation.id}' is inside fork branch '{branch_alias}' that feeds "
                                    f"row_union '{node.id}'. Aggregators are banned inside all bound regions (spec §7 "
                                    "rule 6, ruling 25): a batch flush consumes members the group's roster must "
                                    f"account for. Move '{aggregation.id}' before the fork or after "
                                    f"'{node.id}''s release; for an in-region N->M batch, use a scoped multi-row "
                                    "transform closed by a collector.",
                                    "high",
                                    "bound_region_aggregation_invalid",
                                )
                            )
                        for nested_fork, branch_alias in nested_forks.values():
                            errors.append(
                                _err(
                                    f"node:{node.id}",
                                    f"Fork gate '{nested_fork.id}' is nested inside fork branch '{branch_alias}' that "
                                    f"feeds row_union '{node.id}'. A nested fork replaces the enclosing branch identity, "
                                    "so the union group can never be satisfied. Move the nested fork before the fork "
                                    f"that produces '{branch_alias}', or terminate that branch at a sink.",
                                    "high",
                                    "row_union_nested_fork_invalid",
                                )
                            )
                    for downstream in _runtime_nodes_downstream_of_connection(node.on_success or "", self.nodes):
                        if downstream.node_type in ("coalesce", "row_union"):
                            errors.append(
                                _err(
                                    f"node:{node.id}",
                                    f"{downstream.node_type} '{downstream.id}' is downstream of row_union '{node.id}' "
                                    "with no intervening sink. row_union releases an indivisible N-to-N group, "
                                    "and a correlated barrier cannot safely consume multiple tokens sharing one row_id. "
                                    "Move the downstream barrier upstream of the fork or terminate the released group at a sink.",
                                    "high",
                                    "row_union_downstream_group_invalid",
                                )
                            )
                            break
                        if downstream.node_type != "aggregation" or downstream.trigger is None:
                            continue
                        try:
                            trigger = TriggerConfig.model_validate(deep_thaw(downstream.trigger))
                        except PydanticValidationError:
                            # The aggregation's intrinsic trigger validator
                            # owns malformed external input.
                            continue
                        if trigger.has_count or trigger.has_timeout or trigger.has_condition:
                            errors.append(
                                _err(
                                    f"node:{node.id}",
                                    f"Aggregation '{downstream.id}' is downstream of row_union '{node.id}' "
                                    "but declares a count/timeout/condition trigger. Such triggers can fire "
                                    "between variants of one source row, splitting an indivisible union group. "
                                    "Use the implicit end_of_source trigger or move the aggregation upstream of the fork.",
                                    "high",
                                    "row_union_downstream_group_invalid",
                                )
                            )
                            break
                continue

            if node.input not in runtime_connections:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Node '{node.id}' input '{node.input}' is not reachable from any runtime connection "
                        "(source.on_success, node.on_success/on_error, routes, or fork_to).",
                        "high",
                        "node_input_not_reachable",
                    )
                )

        # Cycles (elspeth-2ed41f0a4a). Every node of a cycle passes the
        # per-node reachability check above, so this is a whole-graph rule.
        cycle = _node_topology_cycle(self.nodes)
        if cycle is not None:
            errors.append(
                _err(
                    f"node:{cycle[0]}",
                    f"Pipeline contains a cycle: {' -> '.join(cycle)}. Rows would loop forever; "
                    "route the last node's on_success/routes to a sink or a downstream connection instead.",
                    "high",
                    "pipeline_cycle",
                )
            )

        # Structural queue topology (elspeth-a5b86149d4). At-least-one-producer
        # is covered by the input-reachability check above (a queue's input is
        # its id, which its producers publish to); more-than-one ordinary
        # consumer is covered by the duplicate-consumer check. Here we require
        # exactly one runtime downstream consumer (reject zero) and a name disjoint from
        # the source keys and the reserved source producer namespace, mirroring
        # the runtime's global source/queue name uniqueness. Sink-name disjoint-
        # ness rides the existing connection/sink overlap check via the queue's
        # consumer claim.
        sink_output_names = {output.name for output in self.outputs}
        for node in self.nodes:
            if node.node_type != "queue":
                continue
            downstream_consumers = [
                n.id for n in self.nodes if n.node_type not in ("coalesce", "queue", "row_union") and n.input == node.id
            ]
            downstream_consumers.extend(
                n.id for n in self.nodes if n.node_type == "coalesce" and node.id in _coalesce_mapped_branch_connections(n.branches)
            )
            downstream_consumers.extend(
                n.id for n in self.nodes if n.node_type == "row_union" and node.id in _coalesce_mapped_branch_connections(n.branches)
            )
            if not downstream_consumers:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Queue '{node.id}' has no downstream consumer; a queue must feed exactly one runtime node.",
                        "high",
                        "queue_no_consumer",
                    )
                )
            if node.id in self.sources:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Queue '{node.id}' collides with a source of the same name; source and queue names must be globally unique.",
                        "high",
                        "queue_name_collision",
                    )
                )
            if node.id == "source" or node.id.startswith("source:"):
                # Mirrors _producer_resolver.is_source_producer_id — a queue may
                # not shadow the reserved source producer namespace.
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Queue '{node.id}' uses the reserved source producer namespace ('source' / 'source:<name>').",
                        "high",
                        "reserved_node_id",
                    )
                )
            if node.id in sink_output_names:
                errors.append(
                    _err(
                        f"node:{node.id}",
                        f"Queue '{node.id}' collides with a sink of the same name; connection and sink names must be disjoint.",
                        "high",
                        "connection_sink_name_overlap",
                    )
                )

        # Generic semantic-contract check.
        from elspeth.web.composer._semantic_validator import validate_semantic_contracts

        semantic_errors, semantic_warnings, semantic_contracts = validate_semantic_contracts(self)
        errors.extend(semantic_errors)

        numeric_contract_errors, numeric_contract_warnings = _batch_distribution_profile_value_field_entries(self.sources, self.nodes)
        errors.extend(numeric_contract_errors)

        # --- Warnings (advisory, non-blocking) ---
        warnings: list[ValidationEntry] = []
        _warn = ValidationEntry
        warnings.extend(numeric_contract_warnings)
        warnings.extend(semantic_warnings)
        from elspeth.web.interpretation_state import prompt_shield_recommendation_warning_pairs

        for component, message in prompt_shield_recommendation_warning_pairs(self):
            warnings.append(_warn(component, message, "medium"))

        # Build connection-field targets (wiring that doesn't require edges)
        connection_targets = _runtime_connection_targets(self.sources, self.nodes)

        # W1: Output has no runtime routing reference (on_success / on_error / routes)
        # Edges are UI-only — generate_yaml() uses only connection fields,
        # so an edge to a sink without a matching connection field is a
        # false positive for reachability.
        #
        # Also count implicit engine-level routes: on_validation_failure
        # and on_write_failure route data to outputs without explicit
        # connection fields.
        implicit_targets: set[str] = set()
        for source in self.sources.values():
            if source.on_validation_failure != "discard":
                implicit_targets.add(source.on_validation_failure)
        for output in self.outputs:
            if output.on_write_failure != "discard":
                implicit_targets.add(output.on_write_failure)
        for output in self.outputs:
            if output.name not in connection_targets and output.name not in implicit_targets:
                warnings.append(
                    _warn(
                        f"output:{output.name}",
                        f"Output '{output.name}' is not referenced by any on_success, on_error, or route — it will never receive data.",
                        "medium",
                    )
                )

        # W2: Source on_success target doesn't match any node input or output name
        node_inputs = _runtime_consumer_connections(self.nodes)
        for source_name, source in self.sources.items():
            source_on_success = source.on_success
            if source_on_success not in node_inputs and source_on_success not in output_names:
                component = "source" if source_name == "source" else f"source:{source_name}"
                message = (
                    f"Source on_success '{source_on_success}' does not match any node input or output — data may not flow."
                    if source_name == "source"
                    else f"Source '{source_name}' on_success '{source_on_success}' does not match any node input or output — data may not flow."
                )
                warnings.append(
                    _warn(
                        component,
                        message,
                        "medium",
                    )
                )

        # W3: Node has no outgoing edges and no connection-field targets
        edge_sources = {e.from_node for e in self.edges}
        for node in self.nodes:
            has_edge_out = node.id in edge_sources
            has_connection_out = (
                node.on_success is not None or node.on_error is not None or (node.routes is not None and len(node.routes) > 0)
            )
            if not has_edge_out and not has_connection_out:
                warnings.append(
                    _warn(
                        f"node:{node.id}",
                        f"Node '{node.id}' has no outgoing edges — its output is not connected to any downstream node or sink.",
                        "medium",
                    )
                )

        # W4: Sink plugin/filename extension mismatch
        _plugin_exts: dict[str, set[str]] = {
            "csv": {".csv"},
            "json": {".json", ".jsonl"},
            "jsonl": {".jsonl"},
        }
        for output in self.outputs:
            if "path" not in output.options:
                continue
            path_val = output.options["path"]
            if type(path_val) is not str:
                continue

            ext = PurePosixPath(path_val).suffix.lower()
            if output.plugin not in _plugin_exts:
                continue
            accepted = _plugin_exts[output.plugin]
            if ext and ext not in accepted:
                warnings.append(
                    _warn(
                        f"output:{output.name}",
                        f"Output '{output.name}' uses plugin '{output.plugin}' but filename extension suggests a different format.",
                        "low",
                    )
                )

        # W5: Transform/aggregation node has empty or incomplete options
        # These plugins require configuration to do anything useful.
        _plugins_requiring_config: dict[str, tuple[str, str]] = {
            "value_transform": ("operations", "no operations defined — nothing will be computed"),
            "type_coerce": ("conversions", "no conversions defined — no types will be changed"),
            "llm": ("prompt_template", "no prompt_template defined — nothing will be sent to the model"),
            "field_mapper": ("mapping", "no mapping defined — no fields will be renamed"),
            "truncate": ("fields", "no fields specified — nothing will be truncated"),
            "keyword_filter": ("keywords", "no keywords defined — all rows will pass through"),
            "web_scrape": ("url_field", "no url_field specified — cannot determine which field contains URLs"),
            "json_explode": ("field", "no field specified — cannot determine which field to explode"),
        }
        for node in self.nodes:
            if node.plugin in _plugins_requiring_config:
                required_key, reason = _plugins_requiring_config[node.plugin]
                if not node.options or required_key not in node.options:
                    warnings.append(
                        _warn(
                            f"node:{node.id}",
                            f"Transform '{node.id}' ({node.plugin}) appears incomplete: {reason}.",
                            "medium",
                        )
                    )
                # Also check for empty list/dict/tuple values (lists are frozen to tuples)
                elif node.options[required_key] in ([], (), {}, None, ""):
                    warnings.append(
                        _warn(
                            f"node:{node.id}",
                            f"Transform '{node.id}' ({node.plugin}) has empty '{required_key}': {reason}.",
                            "medium",
                        )
                    )

        # W6: File sink missing required path
        for output in self.outputs:
            if output.plugin in FILE_SINK_PLUGINS:
                if not output.options or "path" not in output.options:
                    warnings.append(
                        _warn(
                            f"output:{output.name}",
                            f"Output '{output.name}' ({output.plugin}) has no path configured — cannot write to file.",
                            "medium",
                        )
                    )
                elif not output.options["path"]:
                    warnings.append(
                        _warn(
                            f"output:{output.name}",
                            f"Output '{output.name}' ({output.plugin}) has empty path — cannot write to file.",
                            "medium",
                        )
                    )

        # Failsink (on_write_failure) reference validation. Mirrors
        # engine/orchestrator/validation.py validate_sink_failsink_destinations,
        # where every one of these rules raises RouteValidationError at
        # pipeline initialization — deterministic runtime-fatal routes are
        # Stage-1 ERRORS, not advisory warnings (elspeth-eb4127fb49).
        _failsink_eligible = FAILSINK_ELIGIBLE_SINK_PLUGINS
        output_name_set = {o.name for o in self.outputs}
        output_by_name = {o.name: o for o in self.outputs}
        for output in self.outputs:
            dest = output.on_write_failure
            if dest == "discard":
                continue
            # Rule 2: must reference an existing output
            if dest not in output_name_set:
                errors.append(
                    _err(
                        f"output:{output.name}",
                        f"Output '{output.name}' on_write_failure references '{dest}' which is not a configured output.",
                        "high",
                        "failsink_unknown_output",
                    )
                )
                continue  # Skip dependent checks
            # Rule 3: no self-reference
            if dest == output.name:
                errors.append(
                    _err(
                        f"output:{output.name}",
                        f"Output '{output.name}' on_write_failure references itself — a sink cannot be its own failsink.",
                        "high",
                        "failsink_self_reference",
                    )
                )
                continue
            # Rule 4: target must use an eligible file plugin
            target = output_by_name[dest]
            if target.plugin not in _failsink_eligible:
                errors.append(
                    _err(
                        f"output:{output.name}",
                        f"Output '{output.name}' on_write_failure references '{dest}' (plugin='{target.plugin}'), but failsinks must use {FAILSINK_ELIGIBLE_PLUGIN_TEXT}.",
                        "high",
                        "failsink_ineligible_plugin",
                    )
                )
            # Rule 5: no chains — target must use 'discard'
            if target.on_write_failure != "discard":
                errors.append(
                    _err(
                        f"output:{output.name}",
                        f"Output '{output.name}' on_write_failure references '{dest}', but '{dest}' has on_write_failure='{target.on_write_failure}' — failsink targets must use 'discard' (no chains).",
                        "high",
                        "failsink_chain",
                    )
                )

        # Source on_validation_failure (quarantine) reference validation.
        # Mirrors validate_source_quarantine_destination, which raises
        # RouteValidationError at pipeline initialization — same promotion
        # rationale as the failsink rules above.
        for source_name, source in self.sources.items():
            vf_dest = source.on_validation_failure
            if vf_dest != "discard" and vf_dest not in output_name_set:
                component = "source" if source_name == "source" else f"source:{source_name}"
                if source_name == "source":
                    message = (
                        f"Source on_validation_failure references '{vf_dest}' which is not a configured output — "
                        "validation failures will cause a pipeline build error."
                    )
                else:
                    message = (
                        f"Source '{source_name}' on_validation_failure references '{vf_dest}' which is not a configured output — "
                        "validation failures will cause a pipeline build error."
                    )
                errors.append(
                    _err(
                        component,
                        message,
                        "high",
                        "quarantine_unknown_output",
                    )
                )

        # --- Suggestions (optional improvements) ---
        suggestions: list[ValidationEntry] = []
        _sug = ValidationEntry

        # S1: No error routing
        has_gate = any(n.node_type == "gate" for n in self.nodes)
        has_error_routing = any(e.edge_type == "on_error" for e in self.edges) or any(n.on_error is not None for n in self.nodes)
        if not has_gate and not has_error_routing and self.nodes:
            suggestions.append(
                _sug("pipeline", "Consider adding error routing — rows that fail transforms currently have no explicit destination.", "low")
            )

        # S2: Single output to external sink — suggest a local fallback
        # Local file sinks don't benefit from a backup:
        # if the filesystem is failing, a second file will fail too.
        # External sinks (database, azure_blob, dataverse, http) benefit from a
        # local recovery file when the external system is unavailable.
        if len(self.outputs) == 1:
            output = self.outputs[0]
            if output.plugin not in LOCAL_RECOVERY_SINK_PLUGINS:
                suggestions.append(
                    _sug(
                        "pipeline",
                        f"Single external output ('{output.plugin}'). Consider adding a local file output for recovery if the external system is unavailable.",
                        "low",
                    )
                )

        # S3: Source has no schema under the current composer/plugin config contract
        for source_name, source in self.sources.items():
            has_schema = _source_options_have_schema(source.options)
            if not has_schema:
                component = "source" if source_name == "source" else f"source:{source_name}"
                message = (
                    "Source has no explicit schema. Downstream field references depend on runtime column names."
                    if source_name == "source"
                    else f"Source '{source_name}' has no explicit schema. Downstream field references depend on runtime column names."
                )
                suggestions.append(_sug(component, message, "low"))

        # 9. Schema contract validation
        contract_errors, contract_warnings, edge_contracts = _check_schema_contracts(self.sources, self.nodes, self.outputs)
        errors.extend(contract_errors)
        warnings.extend(contract_warnings)

        return ValidationSummary(
            is_valid=len(errors) == 0,
            errors=tuple(errors),
            warnings=tuple(warnings),
            suggestions=tuple(suggestions),
            edge_contracts=edge_contracts,
            semantic_contracts=semantic_contracts,
        )
