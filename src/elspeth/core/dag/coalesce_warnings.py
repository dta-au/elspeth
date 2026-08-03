"""Divert/coalesce interaction warning policy.

Build-time audit warnings for DIVERT (on_error routing) edges that feed
coalesce nodes: require_all stalls and branch-exclusive field loss.
Extracted from graph.py (elspeth-b2c6ab6db8): warning policy is
audit/contract logic, not graph topology. ExecutionGraph keeps a thin
delegator with the historical signature.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from elspeth.contracts import RoutingMode
from elspeth.contracts.enums import NodeType
from elspeth.contracts.types import CoalesceName, NodeID
from elspeth.core.dag.models import GraphValidationWarning

if TYPE_CHECKING:
    from elspeth.core.config import CoalesceSettings
    from elspeth.core.dag.graph import ExecutionGraph


def find_divert_nodes_in_chain(
    graph: ExecutionGraph,
    start_node: NodeID,
    divert_nodes: set[NodeID],
    *,
    stop_nodes: frozenset[NodeID] = frozenset(),
) -> set[NodeID]:
    """Find DIVERT-capable processing nodes by walking backwards from a node.

    Walks backwards through MOVE edges from the start node, collecting
    transforms or gates that have DIVERT edges. Intermediate routing gates are
    part of supported branch topology, so the walk crosses and inspects GATE nodes and
    stops only when the chain leaves the branch-processing path or reaches
    a caller-declared boundary in ``stop_nodes``.

    Args:
        graph: The execution graph to walk
        start_node: The node to start walking backwards from
        divert_nodes: Pre-computed set of transforms/gates with DIVERT edges
        stop_nodes: Walk boundary. A stop node is never collected or
            crossed — the walk ends BEFORE examining it. Both barrier
            callers pass their barrier's originating fork gate(s) here
            (row_union: elspeth-94d68e7aca; coalesce: elspeth-321f335ff2);
            the default empty set leaves the walk unbounded.

    Returns:
        Set of processing-node IDs in the chain that have DIVERT edges.
    """
    found: set[NodeID] = set()
    current = start_node
    visited: set[NodeID] = set()

    while current not in visited:
        if current in stop_nodes:
            break
        visited.add(current)
        current_info = graph.get_node_info(current)
        if current_info.node_type in {NodeType.TRANSFORM, NodeType.GATE}:
            if current in divert_nodes:
                found.add(current)
        else:
            break

        # Walk backwards via MOVE edge (skip DIVERT edges to stay on main chain)
        predecessor: NodeID | None = None
        for pred_from, _pred_to, _pred_key, pred_data in graph._graph.in_edges(current, keys=True, data=True):
            if pred_data["mode"] == RoutingMode.DIVERT:
                continue
            if pred_data["mode"] == RoutingMode.MOVE:
                predecessor = NodeID(pred_from)
                break

        if predecessor is None:
            break
        current = predecessor

    return found


def warn_divert_coalesce_interactions(
    graph: ExecutionGraph,
    coalesce_configs: dict[NodeID, CoalesceSettings],
) -> list[GraphValidationWarning]:
    """Detect DIVERT edges that feed coalesces with audit/data implications.

    Emits two types of warnings:

    1. ``DIVERT_COALESCE_REQUIRE_ALL``: A transform or gate with on_error routing
       feeds a ``require_all`` coalesce. Diverted rows cause other branches
       to wait indefinitely until end-of-source flush.

    2. ``DIVERT_COALESCE_EXCLUSIVE_FIELDS``: A transform or gate with on_error routing
       is in a branch that carries fields not guaranteed by any other branch.
       If a row is diverted, those fields are silently lost from the merged
       output — the audit trail won't record what fields were expected but
       absent. This warning applies to ALL policies, not just ``require_all``.

    This is a build-time warning, not an error — the configuration is valid
    but likely to cause operational or audit surprises.

    Algorithm:
      1. Pre-compute transform/gate node IDs that have outgoing DIVERT edges.
      2. For each ``require_all`` coalesce, warn about DIVERT timing issues.
      3. For ALL coalesces with DIVERT-bearing branches, check for exclusive
         fields that would be lost if the branch is diverted.

    Args:
        graph: The execution graph to inspect
        coalesce_configs: Mapping of coalesce node IDs to their settings.

    Returns:
        List of warnings (also logged via structlog).
    """
    import structlog

    log = structlog.get_logger()

    # Step 1: pre-compute processing nodes with DIVERT edges (exit early if none)
    divert_nodes: set[NodeID] = set()
    for edge in graph.get_edges():
        if edge.mode == RoutingMode.DIVERT:
            from_info = graph.get_node_info(edge.from_node)
            if from_info.node_type in {NodeType.TRANSFORM, NodeType.GATE}:
                divert_nodes.add(edge.from_node)

    if not divert_nodes:
        return []

    # Fork gates by node id, with the branch names their fork_to declares.
    # Gate node configs carry fork_to from the builder; after
    # finalize_node_configs the config is a deep-frozen mapping and the list
    # a tuple, so access stays Mapping-safe.
    fork_gate_branches: dict[NodeID, frozenset[str]] = {}
    for node in graph.get_nodes():
        if node.node_type != NodeType.GATE:
            continue
        fork_to = node.config.get("fork_to")
        if fork_to:
            fork_gate_branches[node.node_id] = frozenset(str(branch) for branch in fork_to)

    warnings: list[GraphValidationWarning] = []

    # Step 2: check each coalesce for DIVERT interactions
    for coalesce_nid, coal_config in coalesce_configs.items():
        # The walk below runs BACKWARD and MUST stop at THIS coalesce's
        # originating fork gate(s) — the gate(s) whose fork_to declares any of
        # the coalesce's branches (elspeth-321f335ff2, mirroring the
        # row_union bound from elspeth-94d68e7aca). Fork -> branch is a COPY
        # edge only for an identity branch; a transform-chain branch is wired
        # gate -> first transform as MOVE, so an unbounded backward walk would
        # cross into pre-fork topology and charge every branch with a DIVERT
        # that sits before the fork. That is a false positive for BOTH
        # warnings here: a row diverted upstream of the fork never forks, so
        # no branch arrives, no group forms or waits (require_all), and no
        # merged output exists to be silently missing the branch's exclusive
        # fields — the row's absence is the DIVERT's own audit record. A fork
        # for another barrier is not a boundary: stopping there would hide
        # genuine branch-local diverts deeper in the current chain.
        # Intermediate non-fork routing gates are still crossed.
        coalesce_branch_names = frozenset(str(branch) for branch in coal_config.branches)
        origin_fork_gates = frozenset(
            gate_nid for gate_nid, forked_branches in fork_gate_branches.items() if forked_branches & coalesce_branch_names
        )

        # Track incoming edges and whether their chains have DIVERT nodes
        # Maps: from_node → (edge_label, edge_mode, divert_nodes_in_chain)
        incoming_divert_map: dict[NodeID, tuple[str, RoutingMode, set[NodeID]]] = {}

        for from_id, _to_id, _key, data in graph._graph.in_edges(coalesce_nid, keys=True, data=True):
            edge_mode = data["mode"]
            edge_label = data["label"]
            from_nid = NodeID(from_id)

            # Identity branches (COPY from gate) have no transforms — skip
            if edge_mode == RoutingMode.COPY:
                continue

            # Processing branch: walk backwards to find DIVERT nodes
            if edge_mode == RoutingMode.MOVE:
                chain_diverts = find_divert_nodes_in_chain(
                    graph,
                    from_nid,
                    divert_nodes,
                    stop_nodes=origin_fork_gates,
                )
                if chain_diverts:
                    incoming_divert_map[from_nid] = (edge_label, edge_mode, chain_diverts)

        if not incoming_divert_map:
            continue  # No DIVERT risk for this coalesce

        # Step 2a: DIVERT_COALESCE_REQUIRE_ALL for require_all policy
        if coal_config.policy == "require_all":
            for _from_nid, (_edge_label, _mode, chain_diverts) in incoming_divert_map.items():
                for divert_nid in chain_diverts:
                    warning = GraphValidationWarning(
                        code="DIVERT_COALESCE_REQUIRE_ALL",
                        message=(
                            f"Processing node '{divert_nid}' has on_error routing (DIVERT edge) "
                            f"and feeds require_all coalesce '{coalesce_nid}'. "
                            f"Rows diverted on error will never reach the coalesce, "
                            f"causing other branches to wait until end-of-source flush."
                        ),
                        node_ids=(str(divert_nid), str(coalesce_nid)),
                    )
                    warnings.append(warning)
                    log.warning(
                        "divert_coalesce_interaction",
                        code=warning.code,
                        divert_node=str(divert_nid),
                        coalesce=str(coalesce_nid),
                        message=warning.message,
                    )
                    break  # One warning per branch is enough

        # Step 2b: DIVERT_COALESCE_EXCLUSIVE_FIELDS for exclusive field loss
        # Only relevant for union merge (nested/select don't lose fields the same way)
        if coal_config.merge != "union":
            continue

        # Use authoritative branch schemas from _branch_info (populated by builder)
        # rather than re-deriving with weaker heuristics.
        branch_schemas = graph.get_coalesce_branch_schemas(CoalesceName(coal_config.name))

        # Match incoming edges with DIVERT to branch names
        for from_nid, (edge_label, _mode, chain_diverts) in incoming_divert_map.items():
            # Try to find which branch this edge belongs to
            matched_branch: str | None = None

            # First, check if edge_label matches a branch name directly
            if edge_label in coal_config.branches:
                matched_branch = edge_label
            else:
                # For transform branches with "continue" edges, match via producer node
                for branch_name, _input_conn in coal_config.branches.items():
                    if branch_name in branch_schemas:
                        # branch_name ∈ branch_schemas guarantees it is also a key
                        # in graph._branch_info (branch_schemas is derived from it in
                        # get_coalesce_branch_schemas), so the trace cannot KeyError
                        # here. A GraphValidationError from the trace is an explicit
                        # graph-construction-bug signal on first-party state (see
                        # _trace_branch_endpoints docstring) and must surface — not be
                        # swallowed, which would silently skip a branch match and
                        # suppress a real audit-loss warning. This mirrors the
                        # un-guarded trace call in get_branch_first_nodes.
                        _first, last = graph._trace_branch_endpoints(coalesce_nid, branch_name)
                        if from_nid == last:
                            matched_branch = branch_name
                            break

            if matched_branch is None or matched_branch not in branch_schemas:
                continue

            branch_schema = branch_schemas[matched_branch]

            # Get guaranteed fields from this branch
            branch_fields = branch_schema.get_effective_guaranteed_fields()
            if not branch_fields:
                continue  # No guaranteed fields — nothing to lose

            # Get union of guaranteed fields from ALL OTHER branches
            other_fields: set[str] = set()
            for other_name, other_schema in branch_schemas.items():
                if other_name == matched_branch:
                    continue
                other_fields.update(other_schema.get_effective_guaranteed_fields())

            # Fields exclusive to this branch (not in any other branch)
            exclusive_fields = branch_fields - other_fields

            if not exclusive_fields:
                continue  # No exclusive fields — loss is covered by other branches

            # Emit warning for exclusive field loss
            divert_node_str = ", ".join(str(node_id) for node_id in sorted(chain_diverts))
            fields_str = ", ".join(sorted(exclusive_fields))
            warning = GraphValidationWarning(
                code="DIVERT_COALESCE_EXCLUSIVE_FIELDS",
                message=(
                    f"Branch '{matched_branch}' has processing nodes with on_error routing "
                    f"({divert_node_str}) and carries fields exclusive to this branch: "
                    f"[{fields_str}]. If a row is diverted, these fields will be "
                    f"silently absent from the coalesce '{coalesce_nid}' merged output. "
                    f"The audit trail won't record which fields were expected but missing."
                ),
                node_ids=(str(coalesce_nid), matched_branch),
            )
            warnings.append(warning)
            log.warning(
                "divert_coalesce_exclusive_fields",
                code=warning.code,
                branch=matched_branch,
                coalesce=str(coalesce_nid),
                exclusive_fields=sorted(exclusive_fields),
                divert_nodes=[str(node_id) for node_id in chain_diverts],
                message=warning.message,
            )

    return warnings
