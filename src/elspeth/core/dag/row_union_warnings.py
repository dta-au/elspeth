"""Divert/row_union interaction warning policy.

Build-time audit warnings for DIVERT (on_error routing) edges inside the
branch chains that feed a row_union barrier. Sibling of
``coalesce_warnings`` (same shape, same reuse of
``find_divert_transforms_in_chain``); ExecutionGraph keeps a thin delegator
so builder-side callers stay symmetric with the coalesce path.

row_union is require_all by definition with no partial release, so a DIVERT
here is strictly worse than the coalesce case it mirrors: losing one branch
does not merge a thinner row, it fails the WHOLE correlated group closed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from elspeth.contracts import RoutingMode
from elspeth.contracts.enums import NodeType
from elspeth.contracts.types import NodeID
from elspeth.core.dag.coalesce_warnings import find_divert_transforms_in_chain
from elspeth.core.dag.models import GraphValidationWarning

if TYPE_CHECKING:
    from elspeth.core.config import RowUnionSettings
    from elspeth.core.dag.graph import ExecutionGraph


def warn_divert_row_union_interactions(
    graph: ExecutionGraph,
    row_union_configs: dict[NodeID, RowUnionSettings],
) -> list[GraphValidationWarning]:
    """Detect DIVERT edges in branch chains that feed a row_union.

    Emits one warning code:

    ``DIVERT_ROW_UNION_GROUP_LOSS``: a transform with on_error routing sits in
    a branch chain feeding a row_union. A diverted row never arrives at the
    barrier, and row_union's v1 arrival policy is require_all with no partial
    release — so the whole correlated group fails closed and the rows the
    OTHER branches produced successfully are discarded with it.

    This is deliberately one code rather than the coalesce module's two: for
    coalesce, arrival-timing (``DIVERT_COALESCE_REQUIRE_ALL``) and merged-field
    loss (``DIVERT_COALESCE_EXCLUSIVE_FIELDS``) are separable conditions. For
    row_union they collapse — payloads are never merged, and any missing
    branch loses the entire group regardless of which fields it carried.

    Build-time warning, not an error: the configuration is valid, but the
    branch-loss blast radius is wider than an author usually expects.

    Args:
        graph: The execution graph to inspect.
        row_union_configs: Mapping of row_union node IDs to their settings.

    Returns:
        List of warnings (also logged via structlog).
    """
    import structlog

    log = structlog.get_logger()

    # Pre-compute transforms with DIVERT edges (exit early if none).
    divert_transforms: set[NodeID] = set()
    for edge in graph.get_edges():
        if edge.mode == RoutingMode.DIVERT and graph.get_node_info(edge.from_node).node_type == NodeType.TRANSFORM:
            divert_transforms.add(edge.from_node)

    if not divert_transforms:
        return []

    warnings: list[GraphValidationWarning] = []

    for union_nid, union_config in row_union_configs.items():
        declared_branches = ", ".join(f"'{branch}'" for branch in union_config.branches)

        for edge in graph.get_incoming_edges(str(union_nid)):
            # Identity branches (COPY from gate straight to the barrier) have
            # no transforms of their own — nothing in-branch can divert.
            if edge.mode != RoutingMode.MOVE:
                continue

            chain_diverts = find_divert_transforms_in_chain(graph, edge.from_node, divert_transforms)
            if not chain_diverts:
                continue

            transform_str = ", ".join(str(t) for t in sorted(chain_diverts))
            warning = GraphValidationWarning(
                code="DIVERT_ROW_UNION_GROUP_LOSS",
                message=(
                    f"Transform '{transform_str}' has on_error routing (DIVERT edge) in a branch "
                    f"feeding row_union '{union_nid}'. row_union requires every declared branch "
                    f"({declared_branches}) and never releases a partial group, so a diverted row "
                    f"fails the WHOLE group closed: the rows the sibling branches produced "
                    f"successfully are discarded too, and the audit trail records a group failure "
                    f"rather than the branch outputs that were lost with it."
                ),
                node_ids=(str(union_nid), *(str(t) for t in sorted(chain_diverts))),
            )
            warnings.append(warning)
            log.warning(
                "divert_row_union_group_loss",
                code=warning.code,
                row_union=str(union_nid),
                divert_transforms=[str(t) for t in sorted(chain_diverts)],
                declared_branches=list(union_config.branches),
                message=warning.message,
            )

    return warnings
