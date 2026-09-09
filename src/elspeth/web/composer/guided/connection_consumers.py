"""Canonical projection from runtime connection labels to their consumers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from elspeth.web.composer.state import CompositionState, _coalesce_branch_connections

type ConsumerKind = Literal["node", "output"]
type ConsumerIdentity = tuple[ConsumerKind, str]


def canonical_connection_consumers(
    state: CompositionState,
    *,
    node_identities: Mapping[str, str],
    output_identities: Mapping[str, str],
) -> dict[str, tuple[ConsumerIdentity, ...]]:
    """Map each connection label to ordered, kind-qualified consumer IDs."""

    node_names = {node.id for node in state.nodes}
    output_names = {output.name for output in state.outputs}
    if set(node_identities) != node_names:
        raise ValueError("canonical consumer projection requires exact node identities")
    if set(output_identities) != output_names:
        raise ValueError("canonical consumer projection requires exact output identities")

    consumers: dict[str, list[ConsumerIdentity]] = {}
    for node in state.nodes:
        identity: ConsumerIdentity = ("node", node_identities[node.id])
        if node.node_type in ("coalesce", "row_union"):
            for connection in _coalesce_branch_connections(node.branches):
                if connection not in consumers:
                    consumers[connection] = []
                destinations = consumers[connection]
                if identity not in destinations:
                    destinations.append(identity)
            continue
        consumers.setdefault(node.input, []).append(identity)
    for output in state.outputs:
        consumers.setdefault(output.name, []).append(("output", output_identities[output.name]))
    return {connection: tuple(destinations) for connection, destinations in consumers.items()}
