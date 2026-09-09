"""DAGNavigator: Pure topology queries for DAG traversal.

Extracted from RowProcessor to create a clean service boundary for
DAG navigation concerns. All methods are pure queries on immutable
topology data — no mutable state dependencies.

Used by:
- RowProcessor (node and terminal route resolution)
- Future: aggregation flush helpers (routing without RowProcessor coupling)
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

from elspeth.contracts import TransformProtocol
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.types import CoalesceName, CollectorName, NodeID, RowUnionName
from elspeth.core.config import GateSettings
from elspeth.core.dag.group_bindings import CloserKind
from elspeth.engine.orchestrator.plugin_types import RowPlugin


class DAGTraversalSnapshot(Protocol):
    """Traversal fields consumed by DAGNavigator.from_traversal_context()."""

    @property
    def coalesce_node_map(self) -> Mapping[CoalesceName, NodeID]: ...

    @property
    def row_union_node_map(self) -> Mapping[RowUnionName, NodeID]: ...

    @property
    def collector_node_map(self) -> Mapping[CollectorName, NodeID]: ...

    @property
    def node_to_plugin(self) -> Mapping[NodeID, RowPlugin | GateSettings]: ...

    @property
    def node_to_next(self) -> Mapping[NodeID, NodeID | None]: ...

    @property
    def branch_first_node(self) -> Mapping[str, NodeID]: ...

    @property
    def structural_node_ids(self) -> frozenset[NodeID]: ...


class DAGNavigator:
    """Pure topology queries for DAG traversal.

    Resolves next-nodes, coalesce identifiers, branch starts, and terminal
    sinks. All methods are pure queries on immutable data — no mutable state
    mutations.

    Constructed from a DAGTraversalContext (built by orchestrator) plus
    supplementary routing data from RowProcessor's constructor params.
    """

    def __init__(
        self,
        *,
        node_to_plugin: Mapping[NodeID, RowPlugin | GateSettings],
        node_to_next: Mapping[NodeID, NodeID | None],
        coalesce_node_ids: Mapping[CoalesceName, NodeID],
        structural_node_ids: frozenset[NodeID],
        coalesce_name_by_node_id: Mapping[NodeID, CoalesceName],
        coalesce_on_success_map: Mapping[CoalesceName, str],
        sink_names: frozenset[str],
        branch_first_node: Mapping[str, NodeID] | None = None,
        row_union_node_ids: Mapping[RowUnionName, NodeID] | None = None,
        collector_node_ids: Mapping[CollectorName, NodeID] | None = None,
        collector_on_success_map: Mapping[CollectorName, str] | None = None,
    ) -> None:
        # Wrap all mappings in MappingProxyType for true immutability
        self._node_to_plugin: Mapping[NodeID, RowPlugin | GateSettings] = MappingProxyType(dict(node_to_plugin))
        self._fork_gate_node_ids = frozenset(
            node_id for node_id, plugin in self._node_to_plugin.items() if isinstance(plugin, GateSettings)
        )
        self._node_to_next: Mapping[NodeID, NodeID | None] = MappingProxyType(dict(node_to_next))
        self._coalesce_node_ids: Mapping[CoalesceName, NodeID] = MappingProxyType(dict(coalesce_node_ids))
        self._structural_node_ids = structural_node_ids
        self._coalesce_name_by_node_id: Mapping[NodeID, CoalesceName] = MappingProxyType(dict(coalesce_name_by_node_id))
        self._coalesce_on_success_map: Mapping[CoalesceName, str] = MappingProxyType(dict(coalesce_on_success_map))
        self._sink_names = sink_names
        self._branch_first_node: Mapping[str, NodeID] = MappingProxyType(dict(branch_first_node or {}))
        self._row_union_node_ids: Mapping[RowUnionName, NodeID] = MappingProxyType(dict(row_union_node_ids or {}))
        self._row_union_name_by_node_id: Mapping[NodeID, RowUnionName] = MappingProxyType(
            {node_id: name for name, node_id in (row_union_node_ids or {}).items()}
        )
        self._collector_node_ids: Mapping[CollectorName, NodeID] = MappingProxyType(dict(collector_node_ids or {}))
        self._collector_name_by_node_id: Mapping[NodeID, CollectorName] = MappingProxyType(
            {node_id: name for name, node_id in (collector_node_ids or {}).items()}
        )
        self._collector_on_success_map: Mapping[CollectorName, str] = MappingProxyType(dict(collector_on_success_map or {}))
        # ONE closer registry for the jump walk's terminal arm (elspeth-b6a0a85a15):
        # node_id -> (CloserKind, configured name), derived from the same per-kind
        # name maps the resolvers use. The walk never enumerates barrier kinds
        # inline — a new closer kind joins the taxonomy HERE or a terminal walk
        # ending at it fails closed with the generic no-sink invariant.
        self._closer_by_node_id: Mapping[NodeID, tuple[CloserKind, str]] = MappingProxyType(
            {
                **{node_id: (CloserKind.COALESCE, str(name)) for node_id, name in self._coalesce_name_by_node_id.items()},
                **{node_id: (CloserKind.ROW_UNION, str(name)) for node_id, name in self._row_union_name_by_node_id.items()},
                **{node_id: (CloserKind.COLLECTOR, str(name)) for node_id, name in self._collector_name_by_node_id.items()},
            }
        )

    @classmethod
    def from_traversal_context(
        cls,
        traversal: DAGTraversalSnapshot,
        *,
        coalesce_on_success_map: Mapping[CoalesceName, str] | None = None,
        collector_on_success_map: Mapping[CollectorName, str] | None = None,
        sink_names: frozenset[str] | None = None,
    ) -> DAGNavigator:
        """Create a DAGNavigator from a DAGTraversalContext plus supplementary params.

        Consumes the context's explicit structural_node_ids allowlist —
        never the complement of node_to_plugin, which silently classified
        unmapped plugin nodes as skippable (elspeth-c522931bd1) — and
        derives coalesce_name_by_node_id automatically.
        """
        coalesce_node_ids = dict(traversal.coalesce_node_map)
        row_union_node_ids = dict(traversal.row_union_node_map)
        collector_node_ids = dict(traversal.collector_node_map)
        node_to_plugin = dict(traversal.node_to_plugin)
        node_to_next = dict(traversal.node_to_next)

        # Barrier nodes are structural by definition; the union keeps that
        # invariant even for snapshot implementations that omit them.
        structural_node_ids = (
            frozenset(traversal.structural_node_ids)
            | frozenset(coalesce_node_ids.values())
            | frozenset(row_union_node_ids.values())
            | frozenset(collector_node_ids.values())
        )
        coalesce_name_by_node_id = {node_id: coalesce_name for coalesce_name, node_id in coalesce_node_ids.items()}

        return cls(
            node_to_plugin=node_to_plugin,
            node_to_next=node_to_next,
            coalesce_node_ids=coalesce_node_ids,
            structural_node_ids=structural_node_ids,
            coalesce_name_by_node_id=coalesce_name_by_node_id,
            coalesce_on_success_map=coalesce_on_success_map or {},
            sink_names=sink_names or frozenset(),
            branch_first_node=dict(traversal.branch_first_node),
            row_union_node_ids=row_union_node_ids,
            collector_node_ids=collector_node_ids,
            collector_on_success_map=collector_on_success_map or {},
        )

    def resolve_plugin_for_node(self, node_id: NodeID) -> TransformProtocol | GateSettings | None:
        """Resolve the plugin/gate associated with a processing node.

        Returns None for structural nodes (e.g. coalesce and row-union points)
        that exist in the DAG traversal but have no plugin to execute. The
        caller skips these nodes and continues to the next processing node.

        Raises OrchestrationInvariantError for unknown nodes that are neither
        plugin-bearing nor structural — this would indicate a graph construction bug.
        """
        if node_id in self._node_to_plugin:
            return self._node_to_plugin[node_id]
        if node_id in self._structural_node_ids:
            return None
        raise OrchestrationInvariantError(
            f"Node ID '{node_id}' is neither a plugin node nor a known structural node. "
            f"Plugin nodes: {sorted(self._node_to_plugin.keys())}, "
            f"structural nodes: {sorted(self._structural_node_ids)}"
        )

    def is_fork_gate_node(self, node_id: NodeID) -> bool:
        """Return whether ``node_id`` is a configured fork gate node.

        Continuation routing depends on a reified topology predicate, not on
        local plugin type probing in the work-item factory. A structural or
        unknown node cannot be a valid fork-origin cursor and fails here.
        """
        if node_id in self._structural_node_ids:
            raise OrchestrationInvariantError(f"Node ID '{node_id}' is structural and cannot be used as a fork-origin continuation cursor")
        self.resolve_plugin_for_node(node_id)
        return node_id in self._fork_gate_node_ids

    def resolve_next_node(self, node_id: NodeID) -> NodeID | None:
        """Resolve the next processing node from traversal metadata."""
        if node_id not in self._node_to_next:
            raise OrchestrationInvariantError(
                f"Node ID '{node_id}' missing from traversal next-node map (terminal nodes must have explicit None entries)"
            )
        return self._node_to_next[node_id]

    def resolve_coalesce_sink(self, coalesce_name: CoalesceName, *, context: str) -> str:
        """Resolve terminal sink for coalesce outcomes with invariant validation."""
        if coalesce_name not in self._coalesce_on_success_map:
            raise OrchestrationInvariantError(
                f"Coalesce '{coalesce_name}' not in on_success map. "
                f"Available: {sorted(self._coalesce_on_success_map.keys())}. "
                f"Context: {context}"
            )
        return self._coalesce_on_success_map[coalesce_name]

    def resolve_collector_sink(self, collector_name: CollectorName, *, context: str) -> str:
        """Resolve terminal sink for a collector release with invariant validation.

        Only TERMINAL collectors (on_success names a sink) have an entry —
        graph-authoritative via get_terminal_sink_map(), mirroring
        resolve_coalesce_sink. A walk that ends at a collector missing here
        is an invariant violation: a non-terminal collector has a next node,
        so the walk would have continued past it.
        """
        if collector_name not in self._collector_on_success_map:
            raise OrchestrationInvariantError(
                f"Collector '{collector_name}' not in on_success map. "
                f"Available: {sorted(self._collector_on_success_map.keys())}. "
                f"Context: {context}"
            )
        return self._collector_on_success_map[collector_name]

    def _resolve_terminal_closer_sink(self, node_id: NodeID, *, context: str) -> str | None:
        """Terminal-arm dispatch for the jump walk: the closer's sink, or None
        for a non-closer terminal node.

        Dispatches on the single closer registry built at construction. A
        row_union here is a broken builder invariant — its on_success must be
        a processing connection (builder-enforced), so it can never be the
        terminal node of a walk (elspeth-b6a0a85a15's sibling-arm analysis).
        """
        if node_id not in self._closer_by_node_id:
            return None
        kind, name = self._closer_by_node_id[node_id]
        if kind is CloserKind.COALESCE:
            return self.resolve_coalesce_sink(CoalesceName(name), context=context)
        if kind is CloserKind.COLLECTOR:
            return self.resolve_collector_sink(CollectorName(name), context=context)
        raise OrchestrationInvariantError(
            f"Jump-target walk ended at row_union '{name}' (node '{node_id}'), which cannot be terminal: "
            f"the builder requires a row_union's on_success to be a processing connection. "
            f"Context: {context}"
        )

    def resolve_coalesce_node(self, coalesce_name: CoalesceName) -> NodeID:
        """Resolve a coalesce node id from its configured coalesce name."""
        try:
            return self._coalesce_node_ids[coalesce_name]
        except KeyError as exc:
            raise OrchestrationInvariantError(
                f"Unknown coalesce name '{coalesce_name}' — "
                f"not in coalesce_node_ids map. "
                f"Known coalesce names: {sorted(self._coalesce_node_ids.keys())}"
            ) from exc

    def resolve_row_union_node(self, row_union_name: RowUnionName) -> NodeID:
        """Resolve a row_union node id from its configured barrier name."""
        try:
            return self._row_union_node_ids[row_union_name]
        except KeyError as exc:
            raise OrchestrationInvariantError(
                f"Unknown row_union name '{row_union_name}' — not in row_union_node_ids map. "
                f"Known row_union names: {sorted(self._row_union_node_ids.keys())}"
            ) from exc

    def resolve_coalesce_name(self, coalesce_node_id: NodeID) -> CoalesceName:
        """Resolve a coalesce name from its structural node id."""
        try:
            return self._coalesce_name_by_node_id[coalesce_node_id]
        except KeyError as exc:
            raise OrchestrationInvariantError(
                f"Unknown coalesce node id '{coalesce_node_id}' — "
                f"not in coalesce_name_by_node_id map. "
                f"Known coalesce nodes: {sorted(self._coalesce_name_by_node_id.keys())}"
            ) from exc

    def resolve_jump_target_sink(self, start_node_id: NodeID) -> str | None:
        """Resolve terminal on_success sink reachable from a route jump target.

        Returns None when the jump target contains a gate that will self-route
        at execution time (gates determine sink destinations dynamically via
        their routes config, so no static on_success resolution is needed).
        """
        node_id: NodeID | None = start_node_id
        resolved_sink: str | None = None
        encountered_gate = False
        iterations = 0
        max_iterations = len(self._node_to_next) + 1

        while node_id is not None:
            iterations += 1
            if iterations > max_iterations:
                raise OrchestrationInvariantError(
                    f"Jump-target sink resolution exceeded {max_iterations} iterations from node '{start_node_id}'. "
                    "Possible cycle in traversal map."
                )

            plugin = self.resolve_plugin_for_node(node_id)
            # Nominal (negative) dispatch — elspeth-8783933d99: any non-gate
            # plugin is a transform (node_to_plugin is closed by construction).
            # Re-measuring TransformProtocol conformance here silently skipped
            # non-conforming transforms and mis-resolved the jump-target sink.
            if isinstance(plugin, GateSettings):
                encountered_gate = True
            elif plugin is not None and plugin.on_success is not None:
                candidate_sink = plugin.on_success
                if not self._sink_names or candidate_sink in self._sink_names:
                    resolved_sink = candidate_sink

            next_node_id = self.resolve_next_node(node_id)
            if next_node_id is None:
                # Terminal barrier arm: a walk ending at a closer resolves the
                # closer's own sink (coalesce and terminal collector alike —
                # elspeth-b6a0a85a15). Non-closer terminals fall through to the
                # no-sink invariant below.
                closer_sink = self._resolve_terminal_closer_sink(
                    node_id,
                    context=f"walk started at node '{start_node_id}'",
                )
                if closer_sink is not None:
                    resolved_sink = closer_sink

            node_id = next_node_id

        if encountered_gate:
            return None

        if resolved_sink is None:
            raise OrchestrationInvariantError(
                f"Jump-target sink resolution reached terminal path with no sink from node '{start_node_id}'. "
                "A gate route jump must resolve to a terminal sink to avoid stale routing state."
            )

        if resolved_sink is not None and self._sink_names and resolved_sink not in self._sink_names:
            raise OrchestrationInvariantError(
                f"Jump-target sink resolution returned '{resolved_sink}' which is not a configured sink. "
                f"Available sinks: {sorted(self._sink_names)}. Walk started at node '{start_node_id}'."
            )
        return resolved_sink

    def resolve_branch_first_node(self, branch_name: str) -> NodeID:
        """First processing node for a fork branch routed to a barrier.

        Exposes the _branch_first_node lookup for fresh fork children and the
        resume path (RowProcessor.resume_incomplete_token).

        _branch_first_node covers all coalesce- and row-union-bound branches
        (built by ExecutionGraph.get_branch_first_nodes). Calling it for a
        fork-to-sink branch raises because terminal branches are not in the map.

        Raises:
            OrchestrationInvariantError: If branch_name is not in the branch_first_node
                map. This indicates a logic error in the caller: only
                barrier-bound branches are registered, not fork-to-sink branches.
        """
        try:
            return self._branch_first_node[branch_name]
        except KeyError as exc:
            raise OrchestrationInvariantError(
                f"Unknown branch name '{branch_name}' — not in branch_first_node map. "
                f"Only barrier-bound branches are registered here; fork-to-sink branches "
                f"are not. Known: {sorted(self._branch_first_node.keys())}"
            ) from exc
