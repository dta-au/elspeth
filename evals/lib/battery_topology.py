"""Topology oracle for the composer battery (spec §2, Decision 9).

Both a ``set_pipeline`` args dict and a ``CompositionState.to_dict()`` are
projected into the same ``Topology``: typed nodes plus edges derived from
connection NAMES (``source.on_success`` → ``node.input``; ``routes`` /
``fork_to`` / ``on_success`` → the node whose ``input`` matches, or the
output whose name matches). Node ids and fork labels are author-chosen
strings and are ignored; plugin, node type, edge type, coalesce ``policy``
and ``merge``, and node cardinality are exact. Option values are ignored
unless an ``option_assertion`` names them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

OptionAssertion = tuple[str, str, Any]

_EXTRA_KEYS: tuple[str, ...] = ("policy", "merge")


@dataclass(frozen=True)
class TNode:
    kind: str
    plugin: str | None
    extras: tuple[tuple[str, str], ...]

    def signature(self) -> tuple[str, str | None, tuple[tuple[str, str], ...]]:
        return (self.kind, self.plugin, self.extras)


@dataclass(frozen=True)
class Topology:
    nodes: tuple[TNode, ...]
    edges: tuple[tuple[int, int, str], ...]


@dataclass(frozen=True)
class MatchResult:
    ok: bool
    reason: str | None = None


def _node_extras(node: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    extras: list[tuple[str, str]] = []
    for key in _EXTRA_KEYS:
        if node.get(key) is not None:
            extras.append((key, str(node[key])))
    if node.get("fork_to"):
        extras.append(("fork_count", str(len(node["fork_to"]))))
    if isinstance(node.get("routes"), Mapping):
        extras.append(("route_count", str(len(node["routes"]))))
    branches = node.get("branches")
    if isinstance(branches, (Mapping, list, tuple)) and branches:
        extras.append(("branch_count", str(len(branches))))
    return tuple(sorted(extras))


def topology_from_pipeline(doc: Mapping[str, Any]) -> Topology:
    """Project args (``source``/``sink_name``) or state (``sources``/``name``) into a Topology."""
    if "sources" in doc and isinstance(doc["sources"], Mapping) and doc["sources"]:
        # CompositionState shape; the canonical single source is the first declared.
        source_spec = next(iter(doc["sources"].values()))
    else:
        source_spec = doc.get("source") or {}
    nodes_in = list(doc.get("nodes") or [])
    outputs_in = list(doc.get("outputs") or [])

    tnodes: list[TNode] = [TNode("source", source_spec.get("plugin"), ())]
    for n in nodes_in:
        tnodes.append(TNode(str(n.get("node_type")), n.get("plugin"), _node_extras(n)))
    output_index_by_name: dict[str, int] = {}
    for o in outputs_in:
        name = o.get("sink_name") if "sink_name" in o else o.get("name")
        output_index_by_name[str(name)] = len(tnodes)
        tnodes.append(TNode("output", o.get("plugin"), ()))

    # connection name → consuming node index (a node's ``input``); coalesce
    # branches are also consumers of the branch connection names. A node ID is
    # ALSO a producer name: nodes without an on_success (coalesce, aggregation
    # in some shapes) are consumed by ``input: <that node's id>``.
    producer_of: dict[str, int] = {str(n["id"]): i for i, n in enumerate(nodes_in, start=1) if n.get("id") is not None}
    consumers: dict[str, list[int]] = {}
    for i, n in enumerate(nodes_in, start=1):
        if n.get("input") is not None:
            consumers.setdefault(str(n["input"]), []).append(i)
        branches = n.get("branches")
        if isinstance(branches, Mapping):
            for conn in branches.values():
                consumers.setdefault(str(conn), []).append(i)
        elif isinstance(branches, (list, tuple)):
            for conn in branches:
                consumers.setdefault(str(conn), []).append(i)

    def targets(conn: Any) -> list[int]:
        if conn is None or conn == "discard":
            return []
        c = str(conn)
        found = list(dict.fromkeys(consumers.get(c, [])))
        if c in output_index_by_name:
            found.append(output_index_by_name[c])
        return found

    edges: list[tuple[int, int, str]] = []
    for t in targets(source_spec.get("on_success")):
        edges.append((0, t, "on_success"))
    for i, n in enumerate(nodes_in, start=1):
        if n.get("input") is not None and str(n["input"]) in producer_of:
            edges.append((producer_of[str(n["input"])], i, "on_success"))  # id-addressed producer (coalesce → consumer)
        for t in targets(n.get("on_success")):
            edges.append((i, t, "on_success"))
        for t in targets(n.get("on_error")):
            edges.append((i, t, "on_error"))
        routes = n.get("routes")
        if isinstance(routes, Mapping):
            for conn in routes.values():
                if conn == "fork":
                    continue
                for t in targets(conn):
                    edges.append((i, t, "route"))
        for conn in n.get("fork_to") or []:
            for t in targets(conn):
                edges.append((i, t, "fork"))
    return Topology(tuple(tnodes), tuple(sorted(edges)))  # MULTISET: two parallel fork edges are two edges


def _edge_multiset(t: Topology, mapping: Sequence[int]) -> tuple[tuple[int, int, str], ...]:
    return tuple(sorted((mapping[a], mapping[b], k) for a, b, k in t.edges))


def topologies_match(
    expected: Topology,
    observed: Topology,
    *,
    option_values: Mapping[str, Mapping[str, Any]] | None = None,
    option_assertions: Sequence[OptionAssertion] = (),
) -> MatchResult:
    """Exact isomorphism on typed nodes + typed edges; option assertions checked afterwards."""
    if len(expected.nodes) != len(observed.nodes):
        return MatchResult(False, f"node count {len(observed.nodes)} != expected {len(expected.nodes)}")
    exp_sigs = sorted((n.signature() for n in expected.nodes), key=_sig_key)
    obs_sigs = sorted((n.signature() for n in observed.nodes), key=_sig_key)
    if exp_sigs != obs_sigs:
        for e, o in zip(exp_sigs, obs_sigs, strict=True):
            if e != o:
                return MatchResult(False, f"node signature mismatch: expected {e}, observed {o}")
    # candidate correspondences: observed index j may play expected index i iff signatures equal
    n = len(expected.nodes)
    candidates = [[j for j in range(n) if observed.nodes[j].signature() == expected.nodes[i].signature()] for i in range(n)]
    exp_edges = tuple(sorted(expected.edges))

    def search(i: int, used: set[int], mapping: list[int]) -> bool:
        if i == n:
            return _edge_multiset(observed, _invert(mapping)) == exp_edges
        for j in candidates[i]:
            if j in used:
                continue
            mapping.append(j)
            used.add(j)
            if search(i + 1, used, mapping):
                return True
            used.discard(j)
            mapping.pop()
        return False

    def _invert(mapping: Sequence[int]) -> list[int]:
        inv = [0] * n
        for i, j in enumerate(mapping):
            inv[j] = i
        return inv

    if not search(0, set(), []):
        return MatchResult(False, "edge structure differs (no node correspondence reproduces the expected edges)")
    for kind_or_plugin, key, expected_value in option_assertions:
        actual = (option_values or {}).get(kind_or_plugin, {}).get(key, _MISSING)
        if actual is _MISSING or actual != expected_value:
            return MatchResult(False, f"option assertion failed: {kind_or_plugin}.{key} expected {expected_value!r}, observed {actual!r}")
    return MatchResult(True, None)


_MISSING = object()


def _sig_key(sig: tuple[str, str | None, tuple[tuple[str, str], ...]]) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    return (sig[0], sig[1] or "", sig[2])  # None plugins sort; never a TypeError on mixed kinds


def observed_option_values(doc: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect ``options`` per node kind and per plugin name for option assertions."""
    out: dict[str, dict[str, Any]] = {}
    for n in doc.get("nodes") or []:
        opts = dict(n.get("options") or {})
        for key in (n.get("node_type"), n.get("plugin")):
            if key:
                out.setdefault(str(key), {}).update(opts)
    return out


__all__ = ["MatchResult", "OptionAssertion", "TNode", "Topology", "observed_option_values", "topologies_match", "topology_from_pipeline"]
