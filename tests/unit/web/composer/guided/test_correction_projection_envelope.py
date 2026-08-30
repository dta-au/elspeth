"""Envelope pins for the Tier-1 Step-3 proposal-correction projection normalizer.

``_guided_proposal_correction_wire_payload`` reads a server-built (first-party)
PROPOSE_PIPELINE projection: required keys are accessed directly and every
deviation from the fixed envelope crashes as one ``AuditIntegrityError``, never
a defaulted or partially normalized wire payload.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.planning import _guided_proposal_correction_wire_payload


def _well_formed() -> dict[str, Any]:
    return {
        "graph": {"sources": [{"name": "src"}], "edges": [{"from": "a", "to": "b"}]},
        "nodes": [{"id": "n1"}],
        "outputs": [{"name": "out"}],
    }


def test_envelope_normalizes_graph_collections_to_wire_names() -> None:
    payload = _well_formed()

    wire = _guided_proposal_correction_wire_payload(payload)

    assert wire == {
        "sources": [{"name": "src"}],
        "nodes": [{"id": "n1"}],
        "connections": [{"from": "a", "to": "b"}],
        "outputs": [{"name": "out"}],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("graph"),
        lambda p: p.pop("nodes"),
        lambda p: p.pop("outputs"),
        lambda p: p.__setitem__("graph", ["not", "a", "dict"]),
        lambda p: p["graph"].pop("sources"),
        lambda p: p["graph"].pop("edges"),
        lambda p: p["graph"].__setitem__("sources", "not-a-list"),
        lambda p: p["graph"].__setitem__("edges", {"not": "a-list"}),
        lambda p: p.__setitem__("nodes", "not-a-list"),
        lambda p: p.__setitem__("outputs", {"not": "a-list"}),
    ],
    ids=[
        "missing_graph",
        "missing_nodes",
        "missing_outputs",
        "graph_not_dict",
        "graph_missing_sources",
        "graph_missing_edges",
        "sources_not_list",
        "edges_not_list",
        "nodes_not_list",
        "outputs_not_list",
    ],
)
def test_envelope_crashes_on_any_projection_malformation(mutate: Any) -> None:
    payload = _well_formed()
    mutate(payload)

    with pytest.raises(AuditIntegrityError, match="guided proposal correction projection is malformed"):
        _guided_proposal_correction_wire_payload(payload)
