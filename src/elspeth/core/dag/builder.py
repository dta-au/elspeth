"""DAG construction from plugin instances.

Extracts the graph-building logic from ExecutionGraph.from_plugin_instances()
into a module-level function. The classmethod facade on ExecutionGraph delegates
here via lazy import to avoid circular dependencies.

Dependency: models.py (leaf) — no import of graph.py at module level.
"""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from elspeth.contracts import RouteDestination, RoutingMode, error_edge_label
from elspeth.contracts.enums import NodeType, OutputMode
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.schema import SchemaConfig, get_raw_schema_config
from elspeth.contracts.types import (
    AggregationName,
    BranchName,
    CoalesceName,
    CollectorName,
    GateName,
    NodeID,
    RowUnionName,
    SinkName,
)
from elspeth.core.canonical import canonical_json
from elspeth.core.dag.bound_regions import compute_bound_regions, derive_escalation_fixpoint_bound, validate_sese_regions
from elspeth.core.dag.coalesce_merge import merge_coalesce_schema
from elspeth.core.dag.group_bindings import build_group_binding_registry
from elspeth.core.dag.guarantees import walk_effective_guarantee_vote
from elspeth.core.dag.models import (
    _NODE_ID_MAX_LENGTH,
    BranchInfo,
    GraphValidationError,
    _GateEntry,
    _suggest_similar,
)

if TYPE_CHECKING:
    from elspeth.contracts import SinkProtocol, SourceProtocol, TransformProtocol
    from elspeth.core.config import (
        AggregationSettings,
        CoalesceSettings,
        CollectorSettings,
        GateSettings,
        QueueSettings,
        RowUnionSettings,
        ScopeSettings,
        SourceSettings,
    )
    from elspeth.core.dag.graph import ExecutionGraph
    from elspeth.core.dag.models import GraphValidationWarning, NodeConfig
    from elspeth.core.dag.wiring import WiredTransform


@dataclass(frozen=True, slots=True)
class _CoalesceBranchSpec:
    branch_name: BranchName
    coalesce_name: CoalesceName
    coalesce_node_id: NodeID
    input_connection: str
    uses_transform_chain: bool


@dataclass(frozen=True, slots=True)
class _CoalesceBranchPlan:
    branch_name: BranchName
    coalesce_name: CoalesceName
    coalesce_node_id: NodeID
    gate_name: GateName
    gate_node_id: NodeID
    input_connection: str
    uses_transform_chain: bool

    @classmethod
    def from_spec(cls, spec: _CoalesceBranchSpec, *, gate_name: GateName, gate_node_id: NodeID) -> _CoalesceBranchPlan:
        return cls(
            branch_name=spec.branch_name,
            coalesce_name=spec.coalesce_name,
            coalesce_node_id=spec.coalesce_node_id,
            gate_name=gate_name,
            gate_node_id=gate_node_id,
            input_connection=spec.input_connection,
            uses_transform_chain=spec.uses_transform_chain,
        )

    def to_branch_info(self) -> BranchInfo:
        return BranchInfo(
            coalesce_name=self.coalesce_name,
            gate_node_id=self.gate_node_id,
            input_connection=self.input_connection,
            uses_transform_chain=self.uses_transform_chain,
        )


@dataclass(frozen=True, slots=True)
class _CoalescePlan:
    name: CoalesceName
    node_id: NodeID
    branches: tuple[_CoalesceBranchSpec, ...]


@dataclass(frozen=True, slots=True)
class _RowUnionBranchSpec:
    branch_name: BranchName
    row_union_name: RowUnionName
    row_union_node_id: NodeID
    input_connection: str
    uses_transform_chain: bool


def _validate_output_schema_contract(transform: Any) -> None:
    """Validate consistency between declared_output_fields and _output_schema_config.

    Two-directional check:
    1. Forward: declared_output_fields non-empty → _output_schema_config must exist.
    2. Containment: declared_output_fields ⊆ guaranteed_fields when both are set.

    Raises FrameworkBugError on any contract violation.
    """
    declared = transform.declared_output_fields
    config = transform._output_schema_config

    # Forward: declares fields but no schema contract → silent DAG validation gap.
    if declared and config is None:
        raise FrameworkBugError(
            f"Transform {transform.name!r} declares output fields "
            f"{sorted(declared)} but provides no "
            f"_output_schema_config for DAG contract validation. "
            f"Call self._output_schema_config = self._build_output_schema_config(schema_config) "
            f"in __init__ after setting declared_output_fields."
        )

    # Containment: declared fields must appear in effective guaranteed fields.
    # Uses get_effective_guaranteed_fields() rather than raw guaranteed_fields
    # to include implicit guarantees from fixed/flexible mode declared fields.
    # Without this, collision detection checks fields that the DAG contract
    # doesn't guarantee — downstream required_fields validation has a blind spot.
    if declared and config is not None and config.declares_guaranteed_fields:
        effective = config.get_effective_guaranteed_fields()
        missing = set(declared) - effective
        if missing:
            raise FrameworkBugError(
                f"Transform {transform.name!r} declares output fields "
                f"{sorted(missing)} not present in effective guaranteed fields "
                f"{sorted(effective)}. "
                f"declared_output_fields must be a subset of guaranteed_fields."
            )


def _parse_contract_schema_config(
    config: Mapping[str, Any],
    *,
    owner: str,
    component_id: str,
    component_type: str,
) -> SchemaConfig | None:
    """Parse a node schema config using the shared raw-option rules."""
    try:
        return get_raw_schema_config(config, owner=owner)
    except ValueError as exc:
        raise GraphValidationError(
            f"Invalid schema config: {exc}",
            component_id=component_id,
            component_type=component_type,
        ) from exc


def build_execution_graph(
    cls: type[ExecutionGraph],
    *,
    sources: Mapping[str, SourceProtocol],
    source_settings_map: Mapping[str, SourceSettings],
    transforms: Sequence[WiredTransform] = (),
    sinks: Mapping[str, SinkProtocol] | None = None,
    aggregations: Mapping[str, tuple[TransformProtocol, AggregationSettings]] | None = None,
    gates: Sequence[GateSettings] = (),
    coalesce_settings: Sequence[CoalesceSettings] | None = None,
    queues: Mapping[str, QueueSettings] | None = None,
    row_union_settings: Sequence[RowUnionSettings] | None = None,
    collectors: Mapping[str, tuple[TransformProtocol, CollectorSettings]] | None = None,
    scope_settings: Sequence[ScopeSettings] | None = None,
    max_bound_region_depth: int = 5,
) -> ExecutionGraph:
    """Build an ExecutionGraph from plugin instances.

    Called by ExecutionGraph.from_plugin_instances() facade. See that method
    for full documentation of parameters and semantics.

    Per ADR-025 §2 the source surface is plural-only — callers pass
    ``sources`` and ``source_settings_map`` keyed by source name. The
    pre-ADR singular ``source=`` / ``source_settings=`` keyword shim
    and its ``legacy_single_source_invocation`` branch are deleted.
    """
    if not sources:
        raise GraphValidationError("ExecutionGraph requires at least one source")
    if not sinks:
        raise GraphValidationError("ExecutionGraph requires at least one sink")
    if aggregations is None:
        aggregations = {}
    if collectors is None:
        collectors = {}
    if set(sources) != set(source_settings_map):
        raise GraphValidationError(
            f"Source plugin names and source settings names must match. plugins={sorted(sources)}, settings={sorted(source_settings_map)}"
        )

    queue_settings = queues or {}
    graph = cls()

    def node_id(prefix: str, name: str, config: NodeConfig, sequence: int | None = None) -> NodeID:
        """Generate deterministic node ID based on plugin type and config.

        Node IDs must be deterministic for checkpoint/resume compatibility.
        If a pipeline is checkpointed and later resumed, the node IDs must
        be identical so checkpoint state can be restored correctly.

        For nodes that can appear multiple times with identical configs
        (transforms, aggregations), include sequence number to ensure uniqueness.

        Args:
            prefix: Node type prefix (source_, transform_, sink_, etc.)
            name: Plugin name
            config: Plugin configuration dict
            sequence: Optional sequence number for duplicate configs (transforms, aggregations)

        Returns:
            Deterministic node ID
        """
        # Create stable hash of config using RFC 8785 canonical JSON
        # CRITICAL: Must use canonical_json() not json.dumps() for true determinism
        # (floats, nested dicts, datetime serialization must be consistent)
        config_str = canonical_json(config)
        config_hash = hashlib.sha256(config_str.encode()).hexdigest()[:12]  # 48 bits

        # Include sequence number for nodes that can have duplicates
        if sequence is not None:
            generated = f"{prefix}_{name}_{config_hash}_{sequence}"
        else:
            generated = f"{prefix}_{name}_{config_hash}"

        if len(generated) > _NODE_ID_MAX_LENGTH:
            raise GraphValidationError(
                f"Generated node_id exceeds {_NODE_ID_MAX_LENGTH} characters: "
                f"'{generated}' (length={len(generated)}). "
                "Use shorter transform/gate/aggregation/source/sink names.",
                component_id=name,
                component_type=prefix,
            )

        return NodeID(generated)

    def _best_schema_config(nid: NodeID) -> SchemaConfig:
        """Get SchemaConfig from a node.

        All nodes have output_schema_config populated at construction time
        (sources, transforms, aggregations from config; gates and coalesce
        from upstream inheritance via _assign_schema).
        """
        info = graph.get_node_info(nid)
        if info.output_schema_config is None:
            raise FrameworkBugError(
                f"Node '{nid}' has no output_schema_config. "
                "All producer nodes must have output_schema_config populated "
                "at construction time."
            )
        return info.output_schema_config

    def _assign_schema(target_nid: NodeID, schema: SchemaConfig) -> None:
        """Set output_schema_config on a pass-through node (gate or coalesce).

        Pass-through nodes don't have their own schema — they inherit from
        upstream producers. This sets the typed SchemaConfig so all consumers
        can read it directly without fallback chains.
        """
        graph.set_node_output_schema(target_nid, schema)

    def _sink_name_set() -> set[str]:
        return {str(name) for name in sink_ids}

    # Add sources. Per ADR-025 §2 source node identity always includes the
    # configured source name in the config hash so two instances of the same
    # plugin remain distinct DAG roots and audit records. There is no
    # singular checkpoint-identity reservation; the prior "source" literal
    # name shortcut is gone with the legacy facade.
    source_ids: dict[str, NodeID] = {}
    for source_name, source_instance in sources.items():
        source_config = source_instance.config
        # Prefer the plugin-computed output contract over re-parsing the raw
        # options dict — the source-side mirror of the transform path below.
        # Sources that rewrite their schema at construction (the LLM source's
        # guaranteed-field augmentation) must feed the augmented contract into
        # graph validation (elspeth-db98d3f660). SourceProtocol owns this
        # field; absence is a broken source contract and must fail loudly.
        source_schema_config: SchemaConfig | None = source_instance._output_schema_config
        if source_schema_config is None:
            source_schema_config = _parse_contract_schema_config(
                source_config,
                owner=f"source:{source_name}",
                component_id=source_name,
                component_type="source",
            )
        source_node_config = dict(source_config)
        source_node_config["source_name"] = source_name
        source_id = node_id("source", source_name, source_node_config)
        source_ids[source_name] = source_id
        graph.add_node(
            source_id,
            node_type=NodeType.SOURCE,
            plugin_name=source_instance.name,
            config=source_node_config,
            output_schema=source_instance.output_schema,  # SourceProtocol requires this
            output_schema_config=source_schema_config,
        )

    # Add sinks
    sink_ids: dict[SinkName, NodeID] = {}
    for sink_name, sink in sinks.items():
        sink_config = sink.config
        sid = node_id("sink", sink_name, sink_config)
        sink_ids[SinkName(sink_name)] = sid
        sink_schema_config = _parse_contract_schema_config(
            sink_config,
            owner=f"sink:{sink_name}",
            component_id=sink_name,
            component_type="sink",
        )
        graph.add_node(
            sid,
            node_type=NodeType.SINK,
            plugin_name=sink.name,
            config=sink_config,
            input_schema=sink.input_schema,  # SinkProtocol requires this
            output_schema_config=sink_schema_config,
            declared_required_fields=sink.declared_required_fields,
        )

    graph.set_sink_id_map(sink_ids)

    # Build declared scheduling queues. V1 queue semantics are pass-through
    # coordination only: queues do not merge fields or synthesize guarantees
    # of their own, so their schema contract is deliberately observed. The
    # effective guarantee at a queue is computed by the propagation walk
    # (walk_effective_guarantee_vote), which intersects the arms feeding it
    # and abstains entirely if any arm abstains (elspeth-5a372d3267).
    queue_ids: dict[str, NodeID] = {}
    observed_queue_schema = SchemaConfig(mode="observed", fields=None)
    for queue_name, queue_config in queue_settings.items():
        queue_node_config: NodeConfig = {"name": queue_name}
        if queue_config.description is not None:
            queue_node_config["description"] = queue_config.description
        qid = node_id("queue", queue_name, queue_node_config)
        queue_ids[queue_name] = qid
        graph.add_node(
            qid,
            node_type=NodeType.QUEUE,
            plugin_name=f"queue:{queue_name}",
            config=queue_node_config,
            output_schema_config=observed_queue_schema,
        )

    # Build transforms
    transform_ids_by_name: dict[str, NodeID] = {}
    transform_ids_by_seq: dict[int, NodeID] = {}
    gate_entries: list[_GateEntry] = []
    gate_route_connections: list[tuple[NodeID, str, str]] = []

    for seq, wired in enumerate(transforms):
        transform = wired.plugin
        transform_config = transform.config
        tid = node_id("transform", wired.settings.name, transform_config)
        transform_ids_by_name[wired.settings.name] = tid
        transform_ids_by_seq[seq] = tid

        node_config = dict(transform_config)
        node_type = NodeType.TRANSFORM

        # Validate output schema contract — crash if transform declares output
        # fields but provides no DAG contract.
        _validate_output_schema_contract(transform)
        output_schema_config = transform._output_schema_config

        # Shape-preserving transforms don't compute _output_schema_config.
        # Parse the raw schema config so every node has a typed schema.
        if output_schema_config is None:
            output_schema_config = _parse_contract_schema_config(
                transform_config,
                owner=f"transform:{wired.settings.name}",
                component_id=wired.settings.name,
                component_type="transform",
            )

        # This is the only site that projects declared_input_fields; the
        # aggregation loop below deliberately does not. Aggregation wiring
        # rejects any transform with is_batch_aware=False (runtime_factory), and
        # _initialize_declared_input_fields raises FrameworkBugError when a
        # batch-aware transform declares input fields, so the excluded space is
        # empty by construction rather than an unhandled case.
        graph.add_node(
            tid,
            node_type=node_type,
            plugin_name=transform.name,
            config=node_config,
            input_schema=transform.input_schema,  # TransformProtocol requires this
            output_schema=transform.output_schema,  # TransformProtocol requires this
            output_schema_config=output_schema_config,
            declared_output_fields=transform.declared_output_fields,
            declared_input_fields=transform.declared_input_fields,
            declared_string_input_fields=transform.declared_string_input_fields,
            passes_through_input=transform.passes_through_input,
            forwards_input_fields=transform.forwards_input_fields,
            removed_input_fields=transform.removed_input_fields,
        )

    graph.set_transform_id_map(transform_ids_by_seq)

    # Build aggregations
    aggregation_ids: dict[AggregationName, NodeID] = {}
    for agg_name, (transform, agg_config) in aggregations.items():
        transform_config = transform.config
        # Use "input_schema" (not "schema") so add_node() doesn't auto-populate
        # output_schema_config. Aggregations have dynamic output by design —
        # BatchStats produces count/sum/mean, not the input fields. The key is
        # preserved for audit/hashing but doesn't trigger output schema inference.
        # See elspeth-c3a98c358c.
        agg_node_config = {
            "trigger": agg_config.trigger.model_dump(),
            "output_mode": agg_config.output_mode,
            "options": dict(agg_config.options),
            "input_schema": transform_config["schema"],  # Input validation, not output
        }
        aid = node_id("aggregation", agg_name, agg_node_config)
        aggregation_ids[AggregationName(agg_name)] = aid

        # Aggregations have dynamic output by design — BatchStats produces
        # count/sum/mean, not the input fields. But _output_schema_config IS
        # correct: _build_output_schema_config() merges declared_output_fields
        # into guaranteed_fields and preserves required_fields (for derived
        # input requirements like group_by). Downstream pass-through nodes
        # (gates, coalesce branches) need output_schema_config for _best_schema_config().
        #
        # Fallback to the raw schema config for test fixtures that don't
        # compute _output_schema_config (same pattern as transforms above).
        agg_output_schema_config = transform._output_schema_config
        if agg_output_schema_config is None:
            agg_output_schema_config = _parse_contract_schema_config(
                transform_config,
                owner=f"aggregation:{agg_name}",
                component_id=agg_name,
                component_type="aggregation",
            )

        graph.add_node(
            aid,
            node_type=NodeType.AGGREGATION,
            plugin_name=agg_config.plugin,
            config=agg_node_config,
            input_schema=transform.input_schema,
            output_schema=transform.output_schema,
            output_schema_config=agg_output_schema_config,
            passes_through_input=transform.passes_through_input,
            forwards_input_fields=transform.forwards_input_fields,
            removed_input_fields=transform.removed_input_fields,
        )

    graph.set_aggregation_id_map(aggregation_ids)

    # Build config gates (no plugin instances)
    config_gate_ids: dict[GateName, NodeID] = {}
    config_gate_schema_inputs: list[tuple[NodeID, str, str]] = []

    for gate_config in gates:
        gate_node_config = {
            "condition": gate_config.condition,
            "routes": dict(gate_config.routes),
        }
        if gate_config.on_error is not None:
            gate_node_config["on_error"] = gate_config.on_error
        if gate_config.fork_to:
            gate_node_config["fork_to"] = list(gate_config.fork_to)

        gid = node_id("config_gate", gate_config.name, gate_node_config)
        config_gate_ids[GateName(gate_config.name)] = gid

        graph.add_node(
            gid,
            node_type=NodeType.GATE,
            plugin_name=f"config_gate:{gate_config.name}",
            config=gate_node_config,
        )

        config_gate_schema_inputs.append((gid, gate_config.name, gate_config.input))

        # Gate routes to fork/sinks immediately. Connection-name routes are
        # deferred until the consumer registry exists. A literal "discard"
        # route is also deferred unless a real sink by that name exists, so a
        # real connection named "discard" can win before the virtual-drop
        # sentinel fallback is applied.
        for route_label, target in gate_config.routes.items():
            if target == "fork":
                # Fork is a special routing mode - handled by fork_to branches
                graph.add_route_resolution_entry(gid, route_label, RouteDestination.fork())
            elif SinkName(target) in sink_ids:
                target_sink_id = sink_ids[SinkName(target)]
                graph.add_edge(gid, target_sink_id, label=route_label, mode=RoutingMode.MOVE)
                graph.add_route_label_entry(gid, SinkName(target), route_label)
                graph.add_route_resolution_entry(gid, route_label, RouteDestination.sink(SinkName(target)))
            else:
                gate_route_connections.append((gid, route_label, target))

        gate_entries.append(
            _GateEntry(
                node_id=gid,
                name=gate_config.name,
                fork_to=tuple(gate_config.fork_to) if gate_config.fork_to is not None else None,
                routes=dict(gate_config.routes),
            )
        )

    graph.set_config_gate_id_map(config_gate_ids)

    # ===== COALESCE IMPLEMENTATION (BUILD NODES AND MAPPINGS FIRST) =====
    # Build coalesce nodes BEFORE connecting gates (needed for branch routing)
    coalesce_ids: dict[CoalesceName, NodeID] = {}
    coalesce_branch_specs: dict[BranchName, _CoalesceBranchSpec] = {}
    coalesce_plans: dict[CoalesceName, _CoalescePlan] = {}
    if coalesce_settings:
        for coalesce_config in coalesce_settings:
            coalesce_name = CoalesceName(coalesce_config.name)
            # Coalesce merges - no schema transformation
            # Note: Pydantic validates min_length=2 for branches field
            config_dict: NodeConfig = {
                "branches": dict(coalesce_config.branches),
                # Declared order drives merge precedence at runtime
                # (first_wins/last_wins collisions, nested field order), but
                # canonical hashing sorts mapping keys — carry the order
                # explicitly so a reorder rotates node identity and topology
                # hash instead of resuming checkpoints under different merge
                # semantics (elspeth-9c5789c4ad parity).
                "branch_order": list(coalesce_config.branches),
                "policy": coalesce_config.policy,
                "merge": coalesce_config.merge,
            }
            if coalesce_config.merge == "union":
                config_dict["union_collision_policy"] = coalesce_config.union_collision_policy
            if coalesce_config.timeout_seconds is not None:
                config_dict["timeout_seconds"] = coalesce_config.timeout_seconds
            if coalesce_config.quorum_count is not None:
                config_dict["quorum_count"] = coalesce_config.quorum_count
            if coalesce_config.select_branch is not None:
                config_dict["select_branch"] = coalesce_config.select_branch

            cid = node_id("coalesce", coalesce_config.name, config_dict)
            coalesce_ids[coalesce_name] = cid

            # Map branches to this coalesce - check for duplicates
            branch_specs: list[_CoalesceBranchSpec] = []
            for branch_name, input_connection in coalesce_config.branches.items():
                branch_key = BranchName(branch_name)
                if branch_key in coalesce_branch_specs:
                    # Branch already mapped to another coalesce
                    existing_coalesce = coalesce_branch_specs[branch_key].coalesce_name
                    raise GraphValidationError(
                        f"Duplicate branch name '{branch_name}' found in coalesce settings.\n"
                        f"Branch '{branch_name}' is already mapped to coalesce '{existing_coalesce}', "
                        f"but coalesce '{coalesce_config.name}' also declares it.\n"
                        f"Each fork branch can only merge at one coalesce point.",
                        component_id=coalesce_config.name,
                        component_type="coalesce",
                    )
                spec = _CoalesceBranchSpec(
                    branch_name=branch_key,
                    coalesce_name=coalesce_name,
                    coalesce_node_id=cid,
                    input_connection=input_connection,
                    uses_transform_chain=input_connection != branch_name,
                )
                coalesce_branch_specs[branch_key] = spec
                branch_specs.append(spec)
            coalesce_plans[coalesce_name] = _CoalescePlan(
                name=coalesce_name,
                node_id=cid,
                branches=tuple(branch_specs),
            )

            graph.add_node(
                cid,
                node_type=NodeType.COALESCE,
                plugin_name=f"coalesce:{coalesce_config.name}",
                config=config_dict,
            )

        graph.set_coalesce_id_map(coalesce_ids)

    # ===== ROW_UNION IMPLEMENTATION (BUILD NODES AND MAPPINGS FIRST) =====
    # row_union is the fork-branch UNION ALL barrier (elspeth-a5b86149d4 v1):
    # correlated on row_id, require_all only, pass-through payloads. Like
    # queues, it promises no schema synthesis — its contract is observed.
    row_union_ids: dict[RowUnionName, NodeID] = {}
    row_union_id_to_config: dict[NodeID, RowUnionSettings] = {}
    row_union_branch_specs: dict[BranchName, _RowUnionBranchSpec] = {}
    if row_union_settings:
        observed_row_union_schema = SchemaConfig(mode="observed", fields=None)
        for union_config in row_union_settings:
            union_name = RowUnionName(union_config.name)
            if SinkName(union_config.on_success) in sink_ids:
                raise GraphValidationError(
                    f"row_union '{union_config.name}' on_success '{union_config.on_success}' names a sink. "
                    "A released group must continue on a processing connection; "
                    "terminal row_union -> sink release is not supported in v1.",
                    component_id=union_config.name,
                    component_type="row_union",
                )
            union_node_config: NodeConfig = {
                "branches": dict(union_config.branches),
                # Declared order is the group release order (RowUnionExecutor
                # iterates it), but canonical hashing sorts mapping keys — an
                # ordered projection must carry it or a branch reorder keeps
                # the node id / topology hash and checkpoint resume replays
                # different release semantics (elspeth-9c5789c4ad).
                "branch_order": list(union_config.branches),
                "on_success": union_config.on_success,
            }
            if union_config.timeout_seconds is not None:
                union_node_config["timeout_seconds"] = union_config.timeout_seconds

            uid = node_id("row_union", union_config.name, union_node_config)
            row_union_ids[union_name] = uid
            row_union_id_to_config[uid] = union_config

            for branch_name, input_connection in union_config.branches.items():
                branch_key = BranchName(branch_name)
                if branch_key in coalesce_branch_specs:
                    raise GraphValidationError(
                        f"Branch '{branch_name}' is already mapped to coalesce "
                        f"'{coalesce_branch_specs[branch_key].coalesce_name}', but row_union "
                        f"'{union_config.name}' also declares it.\n"
                        f"Each fork branch can only join at one barrier.",
                        component_id=union_config.name,
                        component_type="row_union",
                    )
                if branch_key in row_union_branch_specs:
                    raise GraphValidationError(
                        f"Duplicate branch name '{branch_name}' found in row_union settings.\n"
                        f"Branch '{branch_name}' is already mapped to row_union "
                        f"'{row_union_branch_specs[branch_key].row_union_name}', but row_union "
                        f"'{union_config.name}' also declares it.",
                        component_id=union_config.name,
                        component_type="row_union",
                    )
                row_union_branch_specs[branch_key] = _RowUnionBranchSpec(
                    branch_name=branch_key,
                    row_union_name=union_name,
                    row_union_node_id=uid,
                    input_connection=input_connection,
                    uses_transform_chain=input_connection != branch_name,
                )

            graph.add_node(
                uid,
                node_type=NodeType.ROW_UNION,
                plugin_name=f"row_union:{union_config.name}",
                config=union_node_config,
                output_schema_config=observed_row_union_schema,
            )

        graph.set_row_union_id_map(row_union_ids)

    # ===== BUILD COLLECTORS (EXPAND-GROUP CLOSERS; barrier-scopes spec §3) =====
    # A collector is a barrier reusing the batch-transform plugin contract.
    # Its scope binding rides the node config as the "scope" key — present on
    # collector nodes ONLY, so no pre-existing node's canonical hash moves
    # (spec §3; Task-1 corpus pins it).
    collector_ids: dict[CollectorName, NodeID] = {}
    scopes_by_closer: dict[str, ScopeSettings] = {s.closer: s for s in (scope_settings or ())}
    if collectors:
        for collector_name, (transform, collector_config) in collectors.items():
            if not transform.is_batch_aware:
                raise GraphValidationError(
                    f"Collector '{collector_name}' plugin '{collector_config.plugin}' has "
                    f"is_batch_aware=False. Collectors reuse the batch-transform plugin contract.",
                    component_id=collector_name,
                    component_type="collector",
                )
            if collector_config.name not in scopes_by_closer:
                raise GraphValidationError(
                    f"Collector '{collector_name}' has no scopes: entry binding it. A collector is an "
                    f"EXPAND-group closer and requires a scope (spec §7 rule 1).",
                    component_id=collector_name,
                    component_type="collector",
                )
            scope = scopes_by_closer[collector_config.name]
            transform_config = transform.config
            collector_node_config: NodeConfig = {
                "options": dict(collector_config.options),
                "input_schema": transform_config["schema"],
                "scope": {
                    "name": scope.name,
                    "opener": scope.opener,
                    "policy": scope.policy,
                    "on_group_failure": scope.on_group_failure,
                },
            }
            col_id = node_id("collector", collector_name, collector_node_config)
            collector_ids[CollectorName(collector_name)] = col_id
            collector_output_schema_config = transform._output_schema_config
            if collector_output_schema_config is None:
                collector_output_schema_config = _parse_contract_schema_config(
                    transform_config,
                    owner=f"collector:{collector_name}",
                    component_id=collector_name,
                    component_type="collector",
                )
            graph.add_node(
                col_id,
                node_type=NodeType.COLLECTOR,
                plugin_name=collector_config.plugin,
                config=collector_node_config,
                input_schema=transform.input_schema,
                output_schema=transform.output_schema,
                output_schema_config=collector_output_schema_config,
                passes_through_input=transform.passes_through_input,
                forwards_input_fields=transform.forwards_input_fields,
                removed_input_fields=transform.removed_input_fields,
            )
    graph.set_collector_id_map(collector_ids)

    # ===== CONNECT FORK GATES - EXPLICIT DESTINATIONS ONLY =====
    # CRITICAL: No fallback behavior. All fork branches must have explicit destinations.
    # This prevents silent configuration bugs (typos, missing destinations).
    fork_branch_owner: dict[BranchName, GateName] = {}
    coalesce_branch_plans: dict[BranchName, _CoalesceBranchPlan] = {}
    row_union_branch_gates: dict[BranchName, tuple[GateName, NodeID]] = {}
    # Unbound (no barrier) branches consumed by exactly one ordinary
    # downstream transform/gate (spec §7 E2). branch_name -> that consumer's
    # node id. Registered as a producer once `register_producer` exists
    # (below); the actual MOVE edge is drawn by the standard "MATCH
    # PRODUCERS TO CONSUMERS" pass, reusing that machinery unchanged.
    unbound_consumer_fed_branches: dict[BranchName, NodeID] = {}
    for gate_entry in gate_entries:
        if gate_entry.fork_to:
            branch_counts = Counter(gate_entry.fork_to)
            duplicates = sorted([branch for branch, count in branch_counts.items() if count > 1])
            if duplicates:
                raise GraphValidationError(
                    f"Gate '{gate_entry.name}' has duplicate fork branches: {duplicates}. Each fork branch name must be unique.",
                    component_id=gate_entry.name,
                    component_type="gate",
                )
            for branch_name in gate_entry.fork_to:
                branch_key = BranchName(branch_name)
                if branch_key in fork_branch_owner:
                    raise GraphValidationError(
                        f"Fork branch '{branch_name}' is declared by multiple gates: "
                        f"'{fork_branch_owner[branch_key]}' and '{gate_entry.name}'. "
                        "Fork branch names must be globally unique across all gates.",
                        component_id=gate_entry.name,
                        component_type="gate",
                    )
                fork_branch_owner[branch_key] = GateName(gate_entry.name)
                if branch_key in coalesce_branch_specs:
                    plan = _CoalesceBranchPlan.from_spec(
                        coalesce_branch_specs[branch_key],
                        gate_name=GateName(gate_entry.name),
                        gate_node_id=gate_entry.node_id,
                    )
                    coalesce_branch_plans[branch_key] = plan
                    if not plan.uses_transform_chain:
                        # Identity branch: direct COPY edge (current behavior)
                        graph.add_edge(
                            gate_entry.node_id,
                            plan.coalesce_node_id,
                            label=branch_name,
                            mode=RoutingMode.COPY,
                        )
                elif branch_key in row_union_branch_specs:
                    ru_spec = row_union_branch_specs[branch_key]
                    row_union_branch_gates[branch_key] = (GateName(gate_entry.name), gate_entry.node_id)
                    if not ru_spec.uses_transform_chain:
                        # Identity branch: direct COPY edge into the barrier
                        graph.add_edge(
                            gate_entry.node_id,
                            ru_spec.row_union_node_id,
                            label=branch_name,
                            mode=RoutingMode.COPY,
                        )
                elif SinkName(branch_name) in sink_ids:
                    # Explicit sink destination (branch name matches sink name)
                    graph.add_edge(
                        gate_entry.node_id,
                        sink_ids[SinkName(branch_name)],
                        label=branch_name,
                        mode=RoutingMode.COPY,
                    )
                else:
                    # Fourth path (spec §7 E2): a branch consumed by exactly
                    # one ordinary downstream transform/gate is legal —
                    # pure fan-out with no barrier claiming it at all. The
                    # consumer registry doesn't exist yet at this point in
                    # the build, so scan the raw transform/gate settings
                    # directly rather than reusing the later registry pass.
                    consumer_matches: list[tuple[NodeID, str]] = [
                        (transform_ids_by_name[wired.settings.name], f"transform '{wired.settings.name}'")
                        for wired in transforms
                        if wired.settings.input == branch_name
                    ] + [
                        (config_gate_ids[GateName(other_gate.name)], f"gate '{other_gate.name}'")
                        for other_gate in gates
                        if other_gate.input == branch_name
                    ]
                    if len(consumer_matches) == 1:
                        consumer_node_id, _description = consumer_matches[0]
                        unbound_consumer_fed_branches[branch_key] = consumer_node_id
                    elif len(consumer_matches) > 1:
                        raise GraphValidationError(
                            f"Gate '{gate_entry.name}' has fork branch '{branch_name}' with "
                            f"{len(consumer_matches)} downstream consumers: "
                            f"{sorted(description for _node_id, description in consumer_matches)}. "
                            "A fork branch may feed at most one consumer (use a gate for fan-out).",
                            component_id=gate_entry.name,
                            component_type="gate",
                        )
                    else:
                        # NO FALLBACK - this is a configuration error
                        raise GraphValidationError(
                            f"Gate '{gate_entry.name}' has fork branch '{branch_name}' with no destination.\n"
                            f"Fork branches must either:\n"
                            f"  1. Be listed in a coalesce 'branches' dict/list, or\n"
                            f"  2. Be listed in a row_union 'branches' dict/list, or\n"
                            f"  3. Match a sink name exactly, or\n"
                            f"  4. Be consumed by exactly one downstream transform/gate 'input'\n"
                            f"\n"
                            f"Available coalesce branches: {sorted(coalesce_branch_specs.keys())}\n"
                            f"Available row_union branches: {sorted(row_union_branch_specs.keys())}\n"
                            f"Available sinks: {sorted(sink_ids.keys())}",
                            component_id=gate_entry.name,
                            component_type="gate",
                        )

    # ===== WHOLE-ROSTER FORK CLOSURE (spec §7 rule 2, ruling 23) =====
    # A fork is fully bound (every branch flows to its ONE closer, rosters
    # equal) or fully unbound (pure fan-out to sinks). Mixed closure and
    # multi-closer splits are build errors; subset closure can be added
    # additively later — the reverse narrowing never could be. Rule 2
    # supersedes the old row_union-specific origin diagnostics (ancestor/
    # descendant fork generations, unrelated fork origins): any topology
    # those two arms used to catch is ALSO a roster mismatch here, since a
    # closer whose roster spans more than one gate's fork_to can never equal
    # any single contributing gate's roster (maintainer ruling 2026-08-23;
    # the arms were deleted as dead code — see the closer-centric check
    # below for the multi-gate enrichment that replaces their diagnostic
    # value).
    #
    # closer_gate_rosters accumulates, per closer, every fork gate that
    # contributes a branch to it — a closer_label with >1 entry is exactly
    # the "unrelated/ancestor-descendant fork gates" shape the deleted arms
    # used to name explicitly; the roster-equality pass below reads this
    # map so its message can still name every contributing gate.
    closer_gate_rosters: dict[str, dict[str, list[str]]] = {}
    for gate_entry in gate_entries:
        if not gate_entry.fork_to:
            continue
        closers: dict[str, str] = {}  # branch -> closer name ("coalesce:X" / "row_union:Y")
        unbound: list[str] = []
        for branch_name in gate_entry.fork_to:
            branch_key = BranchName(branch_name)
            if branch_key in coalesce_branch_specs:
                closers[branch_name] = f"coalesce:{coalesce_branch_specs[branch_key].coalesce_name}"
            elif branch_key in row_union_branch_specs:
                closers[branch_name] = f"row_union:{row_union_branch_specs[branch_key].row_union_name}"
            else:
                unbound.append(branch_name)
        if closers and unbound:
            raise GraphValidationError(
                f"Fork gate '{gate_entry.name}' has mixed closure: branches {sorted(closers)} close at a "
                f"barrier while branches {unbound} go direct to a sink or an ordinary consumer. A fork is either fully bound — "
                f"every declared branch flows to the fork's single closer — or fully unbound (pure "
                f"fan-out). Route every branch to the closer, or none (spec §7 rule 2).",
                component_id=gate_entry.name,
                component_type="gate",
            )
        distinct_closers = sorted(set(closers.values()))
        if len(distinct_closers) > 1:
            raise GraphValidationError(
                f"Fork gate '{gate_entry.name}' closes at multiple barriers: {distinct_closers}. "
                f"A fork closes entirely at ONE closer (spec §7 rule 2). Split into nested forks — an "
                f"outer pure fan-out whose branches each contain their own fork→closer pair.",
                component_id=gate_entry.name,
                component_type="gate",
            )
        if distinct_closers:
            closer_label = distinct_closers[0]
            if closer_label not in closer_gate_rosters:
                closer_gate_rosters[closer_label] = {}
            closer_gate_rosters[closer_label][gate_entry.name] = list(gate_entry.fork_to)

    # Roster equality, checked once per CLOSER across every contributing
    # gate (rather than once per gate against the closer's full roster) so
    # a multi-gate mismatch can name every contributing gate's own roster
    # plus any branch no gate produces at all, in one message.
    #
    # NOTE: legality is NOT "declared == union(all contributing gates'
    # rosters)" — a multi-gate closer whose combined rosters happen to sum
    # to exactly the declared set (the common shape: every declared branch
    # has some producer, just spread across >1 gate) would wrongly pass that
    # check, silently re-admitting the ancestor/descendant and unrelated-
    # origin topologies rule 2 is supposed to supersede. The single legal
    # shape is exactly ONE contributing gate whose own roster has zero
    # orphans against the declared set; by construction (mixed-closure and
    # multi-closer-split are already ruled out above) a single contributing
    # gate's fork_to is always a subset of `declared`, so "one gate, no
    # orphans" implies exact equality — no separate equality check needed.
    for closer_label, gate_rosters in closer_gate_rosters.items():
        kind, _, closer_name = closer_label.partition(":")
        declared = (
            {str(b.branch_name) for b in coalesce_plans[CoalesceName(closer_name)].branches}
            if kind == "coalesce"
            else {str(b) for b, spec in row_union_branch_specs.items() if str(spec.row_union_name) == closer_name}
        )
        produced: set[str] = set()
        for fork_to in gate_rosters.values():
            produced.update(fork_to)
        orphaned = sorted(declared - produced)
        if len(gate_rosters) == 1 and not orphaned:
            continue
        closer_word = "Coalesce" if kind == "coalesce" else "row_union"
        gate_summary = "; ".join(f"'{name}' declares {sorted(roster)}" for name, roster in sorted(gate_rosters.items()))
        orphan_clause = f"; no gate produces {orphaned}" if orphaned else ""
        raise GraphValidationError(
            f"{closer_word} '{closer_name}' roster mismatch: closer declares {sorted(declared)}, "
            f"drawn from {len(gate_rosters)} fork gate(s): {gate_summary}{orphan_clause}. Whole-roster "
            f"closure requires the closer's branches to come from exactly ONE gate's fork_to, with the "
            f"rosters exactly equal (spec §7 rule 2).",
            component_id=closer_name,
            component_type=kind,
        )

    # ===== VALIDATE COALESCE BRANCHES ARE PRODUCED BY GATES =====
    # All branches declared in coalesce settings must be produced by some fork gate
    if coalesce_branch_specs:
        for branch_name, spec in coalesce_branch_specs.items():
            if branch_name not in coalesce_branch_plans:
                raise GraphValidationError(
                    f"Coalesce '{spec.coalesce_name}' declares branch '{branch_name}', "
                    f"but no gate produces this branch.\n"
                    f"Branches must be listed in a gate's fork_to list to be valid.\n"
                    f"\n"
                    f"Branches produced by gates: {sorted(fork_branch_owner.keys()) if fork_branch_owner else '(none)'}\n"
                    f"Coalesce '{spec.coalesce_name}' expects branches: "
                    f"{sorted(branch.branch_name for branch in coalesce_plans[spec.coalesce_name].branches)}",
                    component_id=str(spec.coalesce_name),
                    component_type="coalesce",
                )

    # ===== VALIDATE ROW_UNION BRANCHES ARE PRODUCED BY GATES =====
    # Reachable only when a row_union's ENTIRE declared roster is disjoint
    # from every fork gate's fork_to (no gate contributes even one of its
    # branches) — verified 2026-08-23 (maintainer ruling on the rule-2
    # supersession above). A PARTIAL orphan (some declared branches produced,
    # some not) never reaches here: the producing gate(s) already enter
    # closer_gate_rosters above, so rule 2's roster-equality check fires
    # first and names the orphan branch itself (declared - produced). Keep
    # this check for the wholly-disjoint case, where no gate ever registers
    # the closer at all and rule 2 never sees it.
    if row_union_branch_specs:
        for branch_name, ru_spec in row_union_branch_specs.items():
            if branch_name not in row_union_branch_gates:
                raise GraphValidationError(
                    f"row_union '{ru_spec.row_union_name}' declares branch '{branch_name}', "
                    f"but no gate produces this branch.\n"
                    f"Branches must be listed in a gate's fork_to list to be valid.\n"
                    f"\n"
                    f"Branches produced by gates: {sorted(fork_branch_owner.keys()) if fork_branch_owner else '(none)'}",
                    component_id=str(ru_spec.row_union_name),
                    component_type="row_union",
                )
        graph.set_branch_to_row_union_map({branch: spec.row_union_name for branch, spec in row_union_branch_specs.items()})
        graph.set_row_union_branch_gates({branch: gate_node_id for branch, (_gate_name, gate_node_id) in row_union_branch_gates.items()})

    # ===== BUILD PRODUCER REGISTRY =====
    producers: dict[str, tuple[NodeID, str]] = {}
    producer_desc: dict[str, str] = {}
    queue_input_edges: defaultdict[str, list[tuple[NodeID, str, str]]] = defaultdict(list)
    gate_connection_route_labels: defaultdict[tuple[NodeID, str], list[str]] = defaultdict(list)
    # (gate_node_id, connection_name) pairs registered AS a fork branch's own
    # producer connection. gate_connection_route_labels is keyed only by
    # (gate_id, target-connection-name-string) — an ordinary route entry
    # whose target string happens to COINCIDE with a branch's own connection
    # name (e.g. a route named after one of the gate's own branches) would
    # otherwise silently override that branch edge's label at draw time
    # (2026-08-23 fix, addendum A2: the true root cause of the F1 "label
    # hole" — `edge.label in member_roster` was asking a correct question of
    # corrupted data). A fork-branch connection's edge must ALWAYS carry the
    # branch name as its label, never a same-gate route label, regardless of
    # what else targets that connection name.
    fork_branch_connections: set[tuple[NodeID, str]] = set()

    def register_producer(connection_name: str, node_id: NodeID, label: str, description: str) -> None:
        if connection_name in queue_ids:
            queue_input_edges[connection_name].append((node_id, label, description))
            return
        if connection_name in producers:
            existing_node, _existing_label = producers[connection_name]
            raise GraphValidationError(
                f"Duplicate producer for connection '{connection_name}': "
                f"{producer_desc[connection_name]} ({existing_node}) and {description} ({node_id}).",
                component_id=str(node_id),
            )
        producers[connection_name] = (node_id, label)
        producer_desc[connection_name] = description

    for source_name, source_settings_entry in source_settings_map.items():
        source_on_success = source_settings_entry.on_success
        if SinkName(source_on_success) not in sink_ids:
            register_producer(
                source_on_success,
                source_ids[source_name],
                "continue",
                f"source '{source_name}'",
            )

    for wired in transforms:
        tid = transform_ids_by_name[wired.settings.name]
        on_success = wired.settings.on_success
        if SinkName(on_success) not in sink_ids:
            register_producer(on_success, tid, "continue", f"transform '{wired.settings.name}'")

    for agg_name, (_transform, agg_settings) in aggregations.items():
        aid = aggregation_ids[AggregationName(agg_name)]
        if agg_settings.on_success is None:
            register_producer(agg_settings.name, aid, "continue", f"aggregation '{agg_settings.name}'")
        elif SinkName(agg_settings.on_success) not in sink_ids:
            register_producer(agg_settings.on_success, aid, "continue", f"aggregation '{agg_settings.name}'")

    for collector_name, (_transform, collector_settings_entry) in collectors.items():
        cid = collector_ids[CollectorName(collector_name)]
        if SinkName(collector_settings_entry.on_success) not in sink_ids:
            register_producer(collector_settings_entry.on_success, cid, "continue", f"collector '{collector_settings_entry.name}'")

    if coalesce_settings:
        for coalesce_config in coalesce_settings:
            if coalesce_config.on_success is None:
                coalesce_id = coalesce_ids[CoalesceName(coalesce_config.name)]
                register_producer(
                    coalesce_config.name,
                    coalesce_id,
                    "continue",
                    f"coalesce '{coalesce_config.name}'",
                )

    if row_union_settings:
        for union_config in row_union_settings:
            register_producer(
                union_config.on_success,
                row_union_ids[RowUnionName(union_config.name)],
                "continue",
                f"row_union '{union_config.name}'",
            )

    for queue_name, queue_id in queue_ids.items():
        producers[queue_name] = (queue_id, "continue")
        producer_desc[queue_name] = f"queue '{queue_name}'"

    # Register fork branches as produced connections (only for branches with transforms).
    # Identity branches use direct COPY edges and don't need connection registration.
    for plan in coalesce_branch_plans.values():
        if not plan.uses_transform_chain:
            continue
        register_producer(
            plan.branch_name,
            plan.gate_node_id,
            plan.branch_name,
            f"fork branch '{plan.branch_name}' from gate '{plan.gate_name}'",
        )
        fork_branch_connections.add((plan.gate_node_id, plan.branch_name))

    for branch_key, (ru_gate_name, ru_gate_node_id) in row_union_branch_gates.items():
        ru_spec = row_union_branch_specs[branch_key]
        if not ru_spec.uses_transform_chain:
            continue
        register_producer(
            ru_spec.branch_name,
            ru_gate_node_id,
            ru_spec.branch_name,
            f"fork branch '{ru_spec.branch_name}' from gate '{ru_gate_name}'",
        )
        fork_branch_connections.add((ru_gate_node_id, ru_spec.branch_name))

    # Unbound (no barrier) consumer-fed branches (spec §7 E2): registering
    # the branch as a producer here lets the standard "MATCH PRODUCERS TO
    # CONSUMERS" pass below draw the MOVE edge exactly as it already does for
    # transform-chain coalesce branches — the branch's downstream consumer
    # (a transform or gate) already registers itself as a consumer of this
    # connection name unconditionally, regardless of this branch.
    for branch_key in unbound_consumer_fed_branches:
        owning_gate_name = fork_branch_owner[branch_key]
        register_producer(
            str(branch_key),
            config_gate_ids[owning_gate_name],
            str(branch_key),
            f"fork branch '{branch_key}' from gate '{owning_gate_name}' (unbound, consumer-fed)",
        )
        fork_branch_connections.add((config_gate_ids[owning_gate_name], str(branch_key)))

    # ===== BUILD CONSUMER REGISTRY =====
    consumers: dict[str, NodeID] = {}
    consumer_claims: list[tuple[str, NodeID, str]] = []

    def register_consumer(connection_name: str, node_id: NodeID, description: str) -> None:
        consumer_claims.append((connection_name, node_id, description))
        if connection_name not in consumers:
            consumers[connection_name] = node_id

    for wired in transforms:
        register_consumer(
            wired.settings.input,
            transform_ids_by_name[wired.settings.name],
            f"transform '{wired.settings.name}'",
        )

    for agg_name, (_transform, agg_settings) in aggregations.items():
        register_consumer(
            agg_settings.input,
            aggregation_ids[AggregationName(agg_name)],
            f"aggregation '{agg_settings.name}'",
        )

    for collector_name, (_transform, collector_settings_entry) in collectors.items():
        register_consumer(
            collector_settings_entry.input,
            collector_ids[CollectorName(collector_name)],
            f"collector '{collector_settings_entry.name}'",
        )

    for gate_settings in gates:
        register_consumer(
            gate_settings.input,
            config_gate_ids[GateName(gate_settings.name)],
            f"gate '{gate_settings.name}'",
        )

    # Register coalesce nodes as consumers of transform branch input connections.
    # For transform branches, the coalesce consumes from the final transform's
    # output connection (not the branch name). The connection resolution system
    # will create MOVE edges through the transform chain automatically.
    for plan in coalesce_branch_plans.values():
        if not plan.uses_transform_chain:
            continue
        register_consumer(
            plan.input_connection,
            plan.coalesce_node_id,
            f"coalesce '{plan.coalesce_name}' branch '{plan.branch_name}'",
        )

    # Same shape for row_union transform branches: the barrier consumes the
    # final transform's output connection; connection resolution creates the
    # MOVE edges through the chain.
    for ru_spec in row_union_branch_specs.values():
        if not ru_spec.uses_transform_chain:
            continue
        register_consumer(
            ru_spec.input_connection,
            ru_spec.row_union_node_id,
            f"row_union '{ru_spec.row_union_name}' branch '{ru_spec.branch_name}'",
        )

    for gate_id, route_label, target in gate_route_connections:
        if target == "discard" and target not in consumers:
            # No real sink or consumer claimed this target. It remains the
            # virtual drop sentinel and must not create a dangling producer.
            continue

        gate_connection_key = (gate_id, target)
        gate_connection_route_labels[gate_connection_key].append(route_label)

        # Multiple routes from the same gate may converge to the same target
        # (e.g., {"true": "next_gate", "false": "next_gate"}). Only register
        # the producer once — the connection is the same regardless of which
        # route label was taken.
        if target in producers and producers[target][0] == gate_id:
            continue
        register_producer(target, gate_id, route_label, f"gate route '{route_label}' from '{gate_id}'")

    for queue_name, upstream_edges in queue_input_edges.items():
        if not upstream_edges:
            raise GraphValidationError(
                f"Queue '{queue_name}' has no upstream producers.",
                component_id=queue_name,
                component_type="queue",
            )
        queue_id = queue_ids[queue_name]
        for upstream_node_id, edge_label, _description in upstream_edges:
            graph.add_edge(upstream_node_id, queue_id, label=edge_label, mode=RoutingMode.MOVE)

    # ===== VALIDATE CONNECTION NAMESPACES =====
    cls._validate_connection_namespaces(
        producers=producers,
        consumers=consumers,
        consumer_claims=consumer_claims,
        sink_names=_sink_name_set(),
        check_dangling=False,
    )

    # Config gate schema resolution (pass 1): resolve gates whose upstream
    # producer already has a schema. Gates downstream of coalesce nodes are
    # deferred until pass-through schemas are populated in dependency order.
    deferred_config_gate_schemas: list[tuple[NodeID, str, str]] = []
    for gate_id, gate_name, input_connection in config_gate_schema_inputs:
        if input_connection not in producers:
            suggestions = _suggest_similar(input_connection, sorted(producers.keys()))
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise GraphValidationError(
                f"Gate '{gate_name}' input '{input_connection}' has no producer.{hint}\nAvailable connections: {', '.join(sorted(producers.keys()))}",
                component_id=gate_name,
                component_type="gate",
            )
        producer_id, _producer_label = producers[input_connection]
        upstream_info = graph.get_node_info(producer_id)
        if upstream_info.output_schema_config is not None:
            _assign_schema(gate_id, _best_schema_config(producer_id))
        else:
            deferred_config_gate_schemas.append((gate_id, gate_name, input_connection))

    # ===== MATCH PRODUCERS TO CONSUMERS =====
    gate_node_ids = {entry.node_id for entry in gate_entries}

    gate_default_continue_targets: dict[NodeID, NodeID] = {}
    ambiguous_continue_gates: set[NodeID] = set()

    for connection_name, consumer_id in consumers.items():
        producer_id, producer_label = producers[connection_name]
        if producer_id in gate_node_ids and producer_label != "continue":
            route_labels = gate_connection_route_labels[(producer_id, connection_name)]
            if (producer_id, connection_name) in fork_branch_connections:
                # This connection IS a fork branch's own producer connection —
                # its edge must ALWAYS carry the branch name, never get
                # overridden by a same-gate route_labels entry that
                # coincidentally targets this same connection name (addendum
                # A2; see fork_branch_connections).
                #
                # If a route ALSO targets this connection, its own edge must
                # STILL be drawn alongside the branch-name edge, not instead
                # of it (fix round 4, N1, controller-ruled): the route stays
                # live in the route-resolution map at runtime regardless of
                # what the graph's edges say, so dropping its edge here
                # removes rule 4's only witness that an unframed route
                # re-enters this branch's connection — exactly the F2 hazard
                # that limb exists to catch. The branch-name edge and a
                # route-labelled edge to the SAME target are two different
                # facts about the graph (the branch exists; a route
                # additionally feeds it), not alternatives.
                graph.add_edge(producer_id, consumer_id, label=producer_label, mode=RoutingMode.MOVE)
                for route_label in route_labels:
                    graph.add_edge(producer_id, consumer_id, label=route_label, mode=RoutingMode.MOVE)
            elif route_labels:
                for route_label in route_labels:
                    graph.add_edge(producer_id, consumer_id, label=route_label, mode=RoutingMode.MOVE)
            else:
                graph.add_edge(producer_id, consumer_id, label=producer_label, mode=RoutingMode.MOVE)
            # Preserve gate fallthrough semantics for RoutingAction.continue_():
            # when a gate has a single downstream processing target, continue
            # should route there even if explicit route labels are present.
            if producer_id not in gate_default_continue_targets:
                gate_default_continue_targets[producer_id] = consumer_id
            elif gate_default_continue_targets[producer_id] != consumer_id:
                # Ambiguous continue fallthrough (multiple processing targets).
                # Leave unresolved; GateExecutor will fail closed if a gate
                # emits continue_() without a unique continuation edge.
                ambiguous_continue_gates.add(producer_id)
        else:
            graph.add_edge(producer_id, consumer_id, label="continue", mode=RoutingMode.MOVE)

    for gate_id, continue_target in gate_default_continue_targets.items():
        if gate_id in ambiguous_continue_gates:
            continue
        graph.add_edge(gate_id, continue_target, label="continue", mode=RoutingMode.MOVE)

    # ===== RESOLVE DEFERRED GATE ROUTES =====
    for gate_id, route_label, target in gate_route_connections:
        if target in consumers:
            graph.add_route_resolution_entry(gate_id, route_label, RouteDestination.processing_node(consumers[target]))
        elif target == "discard":
            graph.add_route_resolution_entry(gate_id, route_label, RouteDestination.discard())
        else:
            suggestions = _suggest_similar(target, sorted(consumers.keys()))
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise GraphValidationError(
                f"Gate route target '{target}' is neither a sink nor a known connection name.{hint}",
                component_id=str(gate_id),
                component_type="gate",
            )

    # Ensure all declared gate route labels are resolvable before runtime.
    graph._validate_route_resolution_map_complete()

    # ===== TERMINAL ROUTING (on_success -> sinks) =====
    for wired in transforms:
        on_success = wired.settings.on_success
        tid = transform_ids_by_name[wired.settings.name]
        if SinkName(on_success) in sink_ids:
            graph.add_edge(tid, sink_ids[SinkName(on_success)], label="on_success", mode=RoutingMode.MOVE)
        elif on_success not in consumers:
            suggestions = _suggest_similar(on_success, sorted(consumers.keys()))
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise GraphValidationError(
                f"Transform '{wired.settings.name}' on_success '{on_success}' is neither a sink nor a known connection.{hint}",
                component_id=wired.settings.name,
                component_type="transform",
            )

    for agg_name, (_transform, agg_settings) in aggregations.items():
        agg_on_success = agg_settings.on_success
        if agg_on_success is None:
            continue
        aid = aggregation_ids[AggregationName(agg_name)]
        if SinkName(agg_on_success) in sink_ids:
            graph.add_edge(aid, sink_ids[SinkName(agg_on_success)], label="on_success", mode=RoutingMode.MOVE)
        elif agg_on_success not in consumers:
            suggestions = _suggest_similar(agg_on_success, sorted(consumers.keys()))
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise GraphValidationError(
                f"Aggregation '{agg_settings.name}' on_success '{agg_on_success}' is neither a sink nor a known connection.{hint}",
                component_id=agg_settings.name,
                component_type="aggregation",
            )

    for collector_name, (_transform, collector_settings_entry) in collectors.items():
        collector_on_success = collector_settings_entry.on_success
        cid = collector_ids[CollectorName(collector_name)]
        if SinkName(collector_on_success) in sink_ids:
            graph.add_edge(cid, sink_ids[SinkName(collector_on_success)], label="on_success", mode=RoutingMode.MOVE)
        elif collector_on_success not in consumers:
            suggestions = _suggest_similar(collector_on_success, sorted(consumers.keys()))
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise GraphValidationError(
                f"Collector '{collector_settings_entry.name}' on_success '{collector_on_success}' is neither a sink nor a known connection.{hint}",
                component_id=collector_settings_entry.name,
                component_type="collector",
            )

    if coalesce_settings:
        for coalesce_config in coalesce_settings:
            if coalesce_config.on_success is None:
                continue
            if coalesce_config.on_success in consumers:
                raise GraphValidationError(
                    f"Coalesce '{coalesce_config.name}' has on_success='{coalesce_config.on_success}'. "
                    "Coalesce on_success must point to a sink when configured.",
                    component_id=coalesce_config.name,
                    component_type="coalesce",
                )
            on_success_sink = SinkName(coalesce_config.on_success)
            if on_success_sink not in sink_ids:
                raise GraphValidationError(
                    f"Coalesce '{coalesce_config.name}' on_success references unknown sink "
                    f"'{coalesce_config.on_success}'. Available sinks: {sorted(sink_ids.keys())}",
                    component_id=coalesce_config.name,
                    component_type="coalesce",
                )
            graph.add_edge(
                coalesce_ids[CoalesceName(coalesce_config.name)],
                sink_ids[on_success_sink],
                label="on_success",
                mode=RoutingMode.MOVE,
            )

    for source_name, source_settings_entry in source_settings_map.items():
        source_on_success = source_settings_entry.on_success
        source_display_name = sources[source_name].name if len(sources) == 1 and source_name == "source" else source_name
        if SinkName(source_on_success) in sink_ids:
            graph.add_edge(
                source_ids[source_name],
                sink_ids[SinkName(source_on_success)],
                label="on_success",
                mode=RoutingMode.MOVE,
            )
        elif source_on_success in queue_ids:
            if source_on_success not in consumers:
                raise GraphValidationError(
                    f"Source '{source_display_name}' on_success '{source_on_success}' "
                    f"references queue '{source_on_success}' with no downstream consumer.",
                    component_id=source_name,
                    component_type="source",
                )
        elif source_on_success not in consumers and source_on_success not in queue_ids:
            suggestions = _suggest_similar(source_on_success, sorted(str(s) for s in sink_ids))
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise GraphValidationError(
                f"Source '{source_display_name}' on_success '{source_on_success}' is neither a sink nor a known connection.{hint}",
                component_id=source_name,
                component_type="source",
            )

    # Re-run namespace validation with dangling-output checks enabled now
    # that terminal on_success sink/connection validation has completed.
    cls._validate_connection_namespaces(
        producers=producers,
        consumers=consumers,
        consumer_claims=consumer_claims,
        sink_names=_sink_name_set(),
        check_dangling=True,
    )

    # ===== ADD DIVERT EDGES (quarantine/error sinks) =====
    # Divert edges represent error/quarantine data flows that bypass the
    # normal DAG execution path. They make quarantine/error sinks reachable
    # in the graph (required for node_ids and audit trail).
    #
    # These are STRUCTURAL markers, not execution paths. Rows reach these
    # sinks via exception handling (processor.py) or source validation
    # failures (orchestrator.py), not by traversing the edge during
    # normal processing.

    # Source quarantine edges
    # _on_validation_failure is defined on SourceProtocol (protocols.py:78)
    for source_name, source_instance in sources.items():
        quarantine_dest = source_instance._on_validation_failure
        if quarantine_dest != "discard" and SinkName(quarantine_dest) in sink_ids:
            graph.add_edge(
                source_ids[source_name],
                sink_ids[SinkName(quarantine_dest)],
                label="__quarantine__",
                mode=RoutingMode.DIVERT,
            )

    # Transform error edges
    for wired in transforms:
        on_error = wired.settings.on_error
        if on_error != "discard":
            if SinkName(on_error) not in sink_ids:
                suggestions = _suggest_similar(on_error, sorted(str(s) for s in sink_ids))
                hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
                raise GraphValidationError(
                    f"Transform '{wired.settings.name}' on_error '{on_error}' references unknown sink.{hint} "
                    f"Available sinks: {', '.join(sorted(str(s) for s in sink_ids))}",
                    component_id=wired.settings.name,
                    component_type="transform",
                )
            graph.add_edge(
                transform_ids_by_name[wired.settings.name],
                sink_ids[SinkName(on_error)],
                label=error_edge_label(wired.settings.name),
                mode=RoutingMode.DIVERT,
            )

    # Config-gate row-error edges. These are structural audit markers, not
    # normal route labels: GateExecutor emits the DIVERT event only when this
    # row's expression evaluation fails and a named policy sink is configured.
    for gate_config in gates:
        gate_on_error = gate_config.on_error
        if gate_on_error is None or gate_on_error == "discard":
            continue
        if SinkName(gate_on_error) not in sink_ids:
            suggestions = _suggest_similar(gate_on_error, sorted(str(s) for s in sink_ids))
            hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise GraphValidationError(
                f"Gate '{gate_config.name}' on_error '{gate_on_error}' references unknown sink.{hint} "
                f"Available sinks: {', '.join(sorted(str(s) for s in sink_ids))}",
                component_id=gate_config.name,
                component_type="gate",
            )
        graph.add_edge(
            config_gate_ids[GateName(gate_config.name)],
            sink_ids[SinkName(gate_on_error)],
            label=error_edge_label(gate_config.name),
            mode=RoutingMode.DIVERT,
        )

    # Sink failsink edges
    for sink_name_key, sink_node_id in sink_ids.items():
        sink_instance = sinks[str(sink_name_key)]
        on_write_failure = sink_instance._on_write_failure
        if on_write_failure is not None and on_write_failure != "discard":
            failsink_name = SinkName(on_write_failure)
            if failsink_name not in sink_ids:
                raise GraphValidationError(
                    f"Sink '{sink_name_key}' on_write_failure references '{on_write_failure}' "
                    f"which is not in sink_ids. Available: {sorted(str(s) for s in sink_ids)}.",
                    component_id=str(sink_name_key),
                    component_type="sink",
                )
            graph.add_edge(
                sink_node_id,
                sink_ids[failsink_name],
                label="__failsink__",
                mode=RoutingMode.DIVERT,
            )

    # ===== PIPELINE ORDERING (TOPOLOGICAL) =====
    processing_node_ids: set[NodeID] = set()
    processing_node_ids.update(queue_ids.values())
    processing_node_ids.update(transform_ids_by_name.values())
    processing_node_ids.update(aggregation_ids.values())
    processing_node_ids.update(config_gate_ids.values())
    processing_node_ids.update(coalesce_ids.values())
    processing_node_ids.update(row_union_ids.values())
    processing_node_ids.update(collector_ids.values())

    pipeline_nodes = graph.topological_processing_order(processing_node_ids)

    branch_info: dict[BranchName, BranchInfo] = {branch_name: plan.to_branch_info() for branch_name, plan in coalesce_branch_plans.items()}
    graph.set_branch_info(branch_info)
    graph.set_unbound_branch_first_nodes(dict(unbound_consumer_fed_branches))

    # ===== POPULATE PASS-THROUGH SCHEMA CONFIG =====
    # Coalesce nodes and their downstream gates are structural pass-throughs;
    # populate them in graph order so alternating fork/coalesce chains resolve
    # each producer before a downstream merge asks for its schema.
    #
    # Coalesce nodes record the upstream schema so audit logs reflect the
    # actual data contract at the merge point.
    # Schema validation is strategy-aware:
    #   union:  require compatible types on overlapping fields
    #   nested: no cross-branch constraint (each branch keyed separately)
    #   select: no cross-branch constraint (only selected branch matters)
    coalesce_id_to_config: dict[NodeID, CoalesceSettings] = {}
    if coalesce_settings:
        for coalesce_config in coalesce_settings:
            cid = coalesce_ids[CoalesceName(coalesce_config.name)]
            coalesce_id_to_config[cid] = coalesce_config

    deferred_gate_input_by_id = {gate_id: input_connection for gate_id, _gate_name, input_connection in deferred_config_gate_schemas}

    for pass_through_id in pipeline_nodes:
        if pass_through_id in deferred_gate_input_by_id:
            input_connection = deferred_gate_input_by_id[pass_through_id]
            producer_id, _producer_label = producers[input_connection]
            _assign_schema(pass_through_id, _best_schema_config(producer_id))

        if pass_through_id not in coalesce_id_to_config:
            continue

        coalesce_id = pass_through_id
        incoming_edges = graph.get_incoming_edges(coalesce_id)
        if not incoming_edges:
            raise GraphValidationError(
                f"Coalesce node '{coalesce_id}' has no incoming branches; cannot determine schema for audit.",
                component_id=str(coalesce_id),
                component_type="coalesce",
            )

        coal_config = coalesce_id_to_config[coalesce_id]

        # Build a branch_name → schema mapping from the branch plan created
        # during fork/coalesce wiring. Identity branches use the producing
        # gate schema; transform branches use their configured input
        # connection's producer.
        branch_to_schema: dict[str, SchemaConfig] = {}

        # Per-branch schemas used SOLELY for the union guaranteed_fields merge.
        # Unlike branch_to_schema (the branch producer's RAW output schema, used
        # for typed-field/mode/audit merging), this carries each branch's
        # PROPAGATION-WALKED effective guarantee so fields a pass-through branch
        # inherits from upstream (e.g. source columns carried through an LLM)
        # survive the union. Non-participating branches are skipped, mirroring
        # the composer preview's _connection_propagation_vote
        # (web/composer/state.py) so build-time and preview agree
        # (elspeth-0b14977817).
        guarantee_branch_schemas: dict[str, SchemaConfig] = {}

        coalesce_plan = coalesce_plans[CoalesceName(coal_config.name)]
        for branch_spec in coalesce_plan.branches:
            if branch_spec.branch_name not in coalesce_branch_plans:
                continue
            branch_plan = coalesce_branch_plans[branch_spec.branch_name]
            if branch_plan.uses_transform_chain:
                producer_node, _producer_label = producers[branch_plan.input_connection]
            else:
                producer_node = branch_plan.gate_node_id
            branch_to_schema[str(branch_plan.branch_name)] = _best_schema_config(producer_node)
            vote = walk_effective_guarantee_vote(graph, producer_node, {})
            if vote.participated:
                guarantee_branch_schemas[str(branch_plan.branch_name)] = SchemaConfig(
                    mode="observed",
                    fields=None,
                    guaranteed_fields=tuple(sorted(vote.fields)),
                )

        # Update branch_info with schema information for runtime tracking of
        # lost branch fields. When a branch is diverted at runtime, the coalesce
        # executor can report which fields were expected from that lost branch.
        for branch_name_str, schema in branch_to_schema.items():
            branch_key = BranchName(branch_name_str)
            if branch_key in branch_info:
                # Use replace() to preserve any future BranchInfo fields automatically
                branch_info[branch_key] = replace(branch_info[branch_key], schema=schema)

        merged_schema = merge_coalesce_schema(
            branch_to_schema,
            merge_strategy=coal_config.merge,
            require_all=coal_config.has_all_branch_semantics,
            collision_policy=coal_config.union_collision_policy,
            branch_order=tuple(coal_config.branches.keys()),
            select_branch=coal_config.select_branch,
            coalesce_id=str(coalesce_id),
            guarantee_branch_schemas=guarantee_branch_schemas or None,
        )
        _assign_schema(coalesce_id, merged_schema)

    # Update branch_info on the graph now that schemas are populated.
    # The initial set_branch_info (line ~821) stored entries without schemas.
    # This call overwrites with schema-enriched entries for runtime lost-branch
    # field tracking.
    if branch_info:
        graph.set_branch_info(branch_info)

    # PHASE 2 VALIDATION: Validate schema compatibility AFTER graph is built
    graph.validate_edge_compatibility()

    # Warn about DIVERT edges feeding correlated barriers (non-fatal).
    # set_validation_warnings ASSIGNS, so both barrier kinds contribute to one
    # list and one call — a second call would silently displace the first.
    if coalesce_id_to_config or row_union_id_to_config:
        build_warnings: list[GraphValidationWarning] = []
        if coalesce_id_to_config:
            build_warnings.extend(graph.warn_divert_coalesce_interactions(coalesce_id_to_config))
        if row_union_id_to_config:
            build_warnings.extend(graph.warn_divert_row_union_interactions(row_union_id_to_config))
        graph.set_validation_warnings(build_warnings)

    # Deep-freeze all NodeInfo configs now that schema resolution is complete.
    # NodeInfo.__post_init__ cannot freeze config because graph construction
    # replaces NodeInfo payloads during multi-step schema propagation.
    # deep_freeze converts nested dicts/lists to MappingProxyType/tuple recursively.
    graph.finalize_node_configs()

    # ===== ROW_UNION GROUP-INDIVISIBILITY GUARD (v1) =====
    # A released union group is indivisible: downstream batch triggers may
    # fire only BETWEEN complete groups, never between variants of one source
    # row. v1 enforces this structurally — any aggregation reachable from a
    # row_union may use only the implicit end_of_source trigger, which cannot
    # split a group. Group-aware count/timeout/condition triggers are the
    # production follow-up on elspeth-a5b86149d4.
    #
    # The same walk rejects a correlated barrier (coalesce or row_union)
    # downstream of a row_union. Both barrier kinds key their pending map on
    # (barrier name, row_id) with no fork_group_id, and row_union is the first
    # N-to-N primitive in the engine — it puts N same-row_id tokens on the wire.
    # A downstream coalesce therefore accepts one arrival per branch and rejects
    # the rest as late arrivals (silent loss of half of every group); a
    # downstream row_union crashes mid-run with a duplicate-arrival error that
    # blames fork/retry/resume for a topology the builder accepted.
    if row_union_ids:
        aggregation_settings_by_node: dict[NodeID, AggregationSettings] = {
            aggregation_ids[AggregationName(agg_name)]: agg_settings for agg_name, (_transform, agg_settings) in aggregations.items()
        }
        barrier_display_names: dict[NodeID, str] = {nid: str(name) for name, nid in coalesce_ids.items()}
        barrier_display_names.update({nid: str(name) for name, nid in row_union_ids.items()})
        for union_name, union_node_id in row_union_ids.items():
            visited: set[NodeID] = set()
            frontier: list[NodeID] = [union_node_id]
            while frontier:
                current = frontier.pop()
                for out_edge in graph.get_outgoing_edges(current):
                    downstream = out_edge.to_node
                    if downstream in visited:
                        continue
                    visited.add(downstream)
                    downstream_type = graph.get_node_info(downstream).node_type
                    if downstream_type == NodeType.SINK:
                        continue
                    if downstream_type in (NodeType.COALESCE, NodeType.ROW_UNION):
                        barrier_kind = downstream_type.value
                        barrier_name = barrier_display_names.get(downstream, str(downstream))
                        raise GraphValidationError(
                            f"{barrier_kind} '{barrier_name}' is downstream of row_union '{union_name}' "
                            f"with no intervening sink. row_union releases N tokens that share one row_id, "
                            f"and a correlated barrier cannot consume an N-to-N group: it keys pending "
                            f"arrivals on (barrier, row_id), so the second arrival on each branch is treated "
                            f"as a late arrival — silently discarding part of every group, or failing mid-run "
                            f"with a duplicate-arrival error. Move '{barrier_name}' upstream of the fork that "
                            f"feeds '{union_name}', or terminate the released group at a sink.",
                            component_id=str(union_name),
                            component_type="row_union",
                        )
                    downstream_agg = aggregation_settings_by_node.get(downstream)
                    if downstream_agg is not None:
                        trigger = downstream_agg.trigger
                        if trigger.has_count or trigger.has_timeout or trigger.has_condition:
                            raise GraphValidationError(
                                f"Aggregation '{downstream_agg.name}' is downstream of row_union "
                                f"'{union_name}' but declares a count/timeout/condition trigger. "
                                f"Such triggers can fire between variants of one source row, "
                                f"splitting an indivisible union group. Use the implicit "
                                f"end_of_source trigger (omit 'trigger' or set 'trigger: {{}}'), "
                                f"or move the aggregation upstream of the fork.",
                                component_id=str(union_name),
                                component_type="row_union",
                            )
                    frontier.append(downstream)

        # ===== BRANCH-INTERNAL AGGREGATION GUARD =====
        # The forward walk above cannot see an aggregation that sits INSIDE a
        # fork branch, upstream of the barrier — it walks away from the union,
        # not toward it. That shape has its own hazard, and it is not the
        # group-split one.
        #
        # A branch aggregation's flush routes through _route_transform_results,
        # which calls expand_token with a SINGLE buffered parent token. Every
        # emitted child therefore inherits that one parent's row_id: one row_id
        # contributes M arrivals to the group (colliding on (row_id, branch)
        # when M > 1) while every other buffered row_id contributes none. The
        # group can never be satisfied, whatever the flush path does with the
        # barrier binding.
        #
        # output_mode: passthrough is deliberately NOT rejected.
        # _route_passthrough_results validates 1:1 and updates the ORIGINAL
        # tokens, so every buffered row_id keeps its own arrival and the group
        # stays satisfiable. Note OutputMode defaults to TRANSFORM, so an
        # aggregation that simply omits the field lands in the rejected arm —
        # hence the diagnostic names the field explicitly.
        #
        # The walk runs BACKWARD and MUST stop at THIS union's originating fork
        # gate(s). Fork -> branch is a COPY edge only for an identity branch; a
        # transform-chain branch is wired gate -> first node as MOVE, so an
        # unbounded backward walk would cross into pre-fork topology and reject
        # an aggregation that sits before the fork — the very remedy this
        # diagnostic recommends. A fork for another union is not a boundary:
        # stopping there would hide hazards earlier in the current branch.
        configured_fork_gate_names = {gate_entry.node_id: gate_entry.name for gate_entry in gate_entries if gate_entry.fork_to}
        for union_name, union_node_id in row_union_ids.items():
            union_fork_gate_node_ids = {
                gate_node_id
                for branch_name, (_gate_name, gate_node_id) in row_union_branch_gates.items()
                if row_union_branch_specs[branch_name].row_union_name == union_name
            }
            # Ancestor/descendant fork generations and unrelated multi-gate
            # origins used to get their own targeted diagnostics here. Both
            # are now dead code: rule 2 (WHOLE-ROSTER FORK CLOSURE, above)
            # provably pre-empts both shapes — a closer whose roster spans
            # more than one gate's fork_to can never equal any single
            # contributing gate's roster, so the roster-equality check always
            # fires first and names every contributing gate (maintainer
            # ruling 2026-08-23; deleted per prerelease no-dead-code
            # doctrine).
            seen_upstream: set[NodeID] = set()
            upstream_frontier: list[NodeID] = [union_node_id]
            nested_fork_gate_names: set[str] = set()
            while upstream_frontier:
                current = upstream_frontier.pop()
                for in_edge in graph.get_incoming_edges(current):
                    upstream = in_edge.from_node
                    if upstream in seen_upstream or upstream in union_fork_gate_node_ids:
                        continue
                    seen_upstream.add(upstream)
                    if graph.get_node_info(upstream).node_type == NodeType.SINK:
                        continue
                    nested_fork_gate_name = configured_fork_gate_names.get(upstream)
                    if nested_fork_gate_name is not None:
                        nested_fork_gate_names.add(nested_fork_gate_name)
                    upstream_agg = aggregation_settings_by_node.get(upstream)
                    if upstream_agg is not None and upstream_agg.output_mode == OutputMode.TRANSFORM:
                        raise GraphValidationError(
                            f"Aggregation '{upstream_agg.name}' is inside a fork branch that feeds "
                            f"row_union '{union_name}' and uses output_mode 'transform' (the default). "
                            f"A transform-mode flush emits its rows from a single buffered parent token, "
                            f"so every emitted row carries that one parent's row_id: one row_id would "
                            f"contribute several arrivals to the union group while every other buffered "
                            f"row_id contributes none, and the group can never be satisfied. Set "
                            f"'output_mode: passthrough' on '{upstream_agg.name}' so each row keeps its "
                            f"own identity, or move the aggregation upstream of the fork that feeds "
                            f"'{union_name}'.",
                            component_id=str(union_name),
                            component_type="row_union",
                        )
                    upstream_frontier.append(upstream)
            if nested_fork_gate_names:
                raise GraphValidationError(
                    f"Fork gate(s) {sorted(nested_fork_gate_names)} are nested inside a branch that feeds "
                    f"row_union '{union_name}'. A nested fork replaces the enclosing branch identity and "
                    f"terminalizes its parent before the enclosing row_union can receive or durably lose "
                    f"that branch, so the union group can never be satisfied. Move the nested fork before "
                    f"the fork that feeds '{union_name}', or terminate the branch at a sink.",
                    component_id=str(union_name),
                    component_type="row_union",
                )

        # ===== VALIDATE ROW_UNION CHAIN BRANCHES ROOT AT THEIR OWN ALIAS =====
        # The consumer registry proves each mapped input HAS a producer, not
        # that the producing chain descends from THIS branch's fork alias. A
        # mapped input rooted elsewhere (e.g. another source, with the alias
        # connection consumed by an unrelated chain) delivers rows that never
        # traversed the fork, so they carry no branch identity and the group
        # can never complete. Left unchecked, the same trace only fires at
        # orchestrator wiring, labelled as a graph construction bug the
        # author cannot act on (elspeth-d560c5e649).
        for chain_branch_name, ru_spec in row_union_branch_specs.items():
            if not ru_spec.uses_transform_chain:
                continue
            chain_gate_name, chain_gate_node_id = row_union_branch_gates[chain_branch_name]
            try:
                graph._trace_branch_endpoints(
                    ru_spec.row_union_node_id,
                    str(chain_branch_name),
                    fork_gate_nid=chain_gate_node_id,
                )
            except GraphValidationError:
                raise GraphValidationError(
                    f"row_union '{ru_spec.row_union_name}' branch '{chain_branch_name}' maps input "
                    f"connection '{ru_spec.input_connection}', but no chain arriving at the union "
                    f"descends from fork branch '{chain_branch_name}' of gate '{chain_gate_name}'. "
                    f"Rows arriving on '{ru_spec.input_connection}' never traversed that fork "
                    f"branch, so they carry no matching branch identity and the union group can "
                    f"never complete. Map '{chain_branch_name}' to the output of the transform "
                    f"chain that consumes connection '{chain_branch_name}'.",
                    component_id=str(ru_spec.row_union_name),
                    component_type="row_union",
                ) from None

    # ===== UNIFIED GROUP-BINDING REGISTRY (barrier-scopes spec §3) =====
    registry = build_group_binding_registry(
        fork_rosters={
            GateName(gate_entry.name): (gate_entry.node_id, tuple(gate_entry.fork_to)) for gate_entry in gate_entries if gate_entry.fork_to
        },
        coalesce_plans=coalesce_plans,
        coalesce_settings_by_name={CoalesceName(c.name): c for c in (coalesce_settings or [])},
        coalesce_ids=coalesce_ids,
        row_union_branch_specs=row_union_branch_specs,
        row_union_settings_by_name={RowUnionName(u.name): u for u in (row_union_settings or [])},
        row_union_ids=row_union_ids,
        scope_settings=tuple(scope_settings or ()),
        collector_ids=collector_ids,
        transform_ids_by_name=transform_ids_by_name,
    )
    graph.set_group_bindings(registry)

    # ===== BOUND-REGION COMPUTATION (spec §7 rule 3, §6.3 depth cap) =====
    regions = compute_bound_regions(graph, registry, max_depth=max_bound_region_depth)
    graph.set_bound_regions(regions)
    validate_sese_regions(graph, regions)
    max_observed_depth = max((r.depth for r in regions), default=0)
    graph.set_max_bound_region_depth(max_observed_depth)
    graph.set_escalation_fixpoint_bound(derive_escalation_fixpoint_bound(max_observed_depth))

    # Step maps and node sequence support node_id-based processor traversal.
    graph.set_pipeline_nodes(pipeline_nodes)
    graph.set_node_step_map(graph.build_step_map())
    graph._freeze_build_metadata()

    return graph
