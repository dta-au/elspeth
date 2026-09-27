"""The build's declared-input proof keyed by node NAME, for pinning (elspeth-5887fb7928 R2, systems C4).

The runtime classifies a declared-input miss against
``ExecutionGraph.get_declared_input_proof()``: a miss of a proven field
aborts, a miss the build never proved routes (ADR-013 Amendment
2026-09-27). A change to the presence-vote computation that UNDER-proves
therefore turns a loud abort into a quiet per-row route — nothing else
notices. The examples corpus and the DAG scenario corpus each pin this map,
so any vote change moves a pin and has to be judged.

Keys are ``<kind>:<name>`` rather than node ids: a node id hashes the node's
config, which carries per-run paths in the scenario corpus. Every entry the
graph publishes must map to a name — an unnamed entry fails the pin rather
than disappearing from it.
"""

from __future__ import annotations

from elspeth.core.dag import ExecutionGraph


def named_declared_input_proof(graph: ExecutionGraph) -> dict[str, list[str]]:
    """``{"transform:<name>" | "aggregation:<name>" | "collector:<name>": sorted proven fields}``."""
    names: dict[str, str] = {}
    for name, node_id in graph.get_transform_name_id_map().items():
        names[str(node_id)] = f"transform:{name}"
    for name, node_id in graph.get_aggregation_id_map().items():
        names[str(node_id)] = f"aggregation:{name}"
    for name, node_id in graph.get_collector_id_map().items():
        names[str(node_id)] = f"collector:{name}"
    proof = graph.get_declared_input_proof()
    unnamed = sorted(str(node_id) for node_id in proof if str(node_id) not in names)
    assert not unnamed, f"declared-input proof entries with no node name: {unnamed}"
    return {names[str(node_id)]: sorted(fields) for node_id, fields in sorted(proof.items(), key=lambda item: names[str(item[0])])}
