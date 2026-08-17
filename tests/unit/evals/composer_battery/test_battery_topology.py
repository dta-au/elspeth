"""Isomorphism oracle — labels/ids ignored, structure/policy/merge/cardinality exact."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from evals.lib.battery_topology import topologies_match, topology_from_pipeline

FIXTURE = Path(__file__).resolve().parents[4] / "evals/composer-parity/fixtures/fork_coalesce.json"


def _fork_args() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())["canonical_arguments"]


def _as_state(args: dict[str, Any]) -> dict[str, Any]:
    """Project a set_pipeline args dict into the CompositionState.to_dict() shape."""
    src = dict(args["source"])
    return {
        "version": 1,
        "sources": {"source": src},
        "nodes": copy.deepcopy(args["nodes"]),
        "edges": [],
        "outputs": [{"name": o["sink_name"], "plugin": o["plugin"], "options": o.get("options", {})} for o in args["outputs"]],
    }


def test_source_and_output_key_renaming_projects_identically() -> None:
    """args (``source``/``sink_name``) and state (``sources``/``name``) shapes project the same.
    Deliberately weak on nodes (they are copied verbatim); the real args→state anchor is
    Task 3's ``test_canonical_payload_commits_to_the_expected_topology``."""
    args = _fork_args()
    assert topology_from_pipeline(args) == topology_from_pipeline(_as_state(args))


def test_fixture_projects_four_edges_including_the_coalesce_output() -> None:
    """A coalesce has no on_success; its consumer references it BY NODE ID (``finalize.input == "merge_results"``).
    The projection must register node ids as producers, keep parallel fork edges as a multiset,
    and type coalesce arity — otherwise orphaned/half-wired graphs match the canonical one."""
    topo = topology_from_pipeline(_fork_args())
    assert len(topo.edges) == 5, topo.edges  # source->gate, gate->coalesce x2 (fork), coalesce->finalize, finalize->output
    kinds = [e[2] for e in topo.edges]
    assert kinds.count("fork") == 2 and kinds.count("on_success") == 3
    coalesce = next(n for n in topo.nodes if n.kind == "coalesce")
    assert ("branch_count", "2") in coalesce.extras
    exp = topo
    orphan = _fork_args()
    next(n for n in orphan["nodes"] if n["id"] == "finalize")["input"] = "nowhere_at_all"
    assert not topologies_match(exp, topology_from_pipeline(orphan)).ok
    half = _fork_args()
    next(n for n in half["nodes"] if n["node_type"] == "coalesce")["branches"] = {"path_a": "path_a"}
    assert not topologies_match(exp, topology_from_pipeline(half)).ok
    one_fork = _fork_args()
    next(n for n in one_fork["nodes"] if n["node_type"] == "gate")["fork_to"] = ["path_a"]
    assert not topologies_match(exp, topology_from_pipeline(one_fork)).ok


def test_renamed_ids_and_fork_labels_still_match() -> None:
    args = _fork_args()
    renamed = copy.deepcopy(args)
    old_ids = {n["id"] for n in renamed["nodes"]}
    for n in renamed["nodes"]:
        n["id"] = "x_" + n["id"]
        if n.get("input") in old_ids:
            n["input"] = "x_" + n["input"]  # id-valued inputs follow the rename
    fork = next(n for n in renamed["nodes"] if n["node_type"] == "gate")
    fork["fork_to"] = ["b1", "b2"]
    coalesce = next(n for n in renamed["nodes"] if n["node_type"] == "coalesce")
    coalesce["branches"] = {"b1": "b1", "b2": "b2"}
    coalesce["input"] = "b1"
    result = topologies_match(topology_from_pipeline(args), topology_from_pipeline(renamed))
    assert result.ok, result.reason


def test_wrong_coalesce_merge_fails() -> None:
    args = _fork_args()
    bad = copy.deepcopy(args)
    next(n for n in bad["nodes"] if n["node_type"] == "coalesce")["merge"] = "first_wins"
    result = topologies_match(topology_from_pipeline(args), topology_from_pipeline(bad))
    assert not result.ok and "merge" in (result.reason or "")


def test_wrong_coalesce_policy_fails() -> None:
    args = _fork_args()
    bad = copy.deepcopy(args)
    next(n for n in bad["nodes"] if n["node_type"] == "coalesce")["policy"] = "first_available"
    result = topologies_match(topology_from_pipeline(args), topology_from_pipeline(bad))
    assert not result.ok and "policy" in (result.reason or "")


def test_mixed_plugin_and_pluginless_nodes_of_one_kind_do_not_crash() -> None:
    args = {
        "source": {"plugin": "csv", "on_success": "in", "options": {"path": "r.csv"}},
        "nodes": [
            {"id": "a", "node_type": "transform", "plugin": "passthrough", "input": "in", "on_success": "mid", "on_error": "discard"},
            {"id": "b", "node_type": "transform", "input": "mid", "on_success": "out", "on_error": "discard"},  # plugin-less
        ],
        "outputs": [{"sink_name": "out", "plugin": "json"}],
    }
    other = copy.deepcopy(args)
    other["nodes"].reverse()
    other["nodes"][0]["input"], other["nodes"][1]["input"] = "in", "mid"
    other["nodes"][0]["on_success"], other["nodes"][1]["on_success"] = "mid", "out"
    result = topologies_match(topology_from_pipeline(args), topology_from_pipeline(other))
    assert not result.ok  # passthrough-first vs pluginless-first are different graphs; must be a verdict, not a TypeError


def test_extra_passthrough_node_fails() -> None:
    args = _fork_args()
    bad = copy.deepcopy(args)
    fin = next(n for n in bad["nodes"] if n["id"] == "finalize")
    fin["on_success"] = "extra_in"
    bad["nodes"].append(
        {
            "id": "extra",
            "node_type": "transform",
            "plugin": "passthrough",
            "input": "extra_in",
            "on_success": "merged",
            "on_error": "discard",
        }
    )
    result = topologies_match(topology_from_pipeline(args), topology_from_pipeline(bad))
    assert not result.ok and "node" in (result.reason or "")


def test_sink_plugin_swap_fails() -> None:
    args = _fork_args()
    bad = copy.deepcopy(args)
    bad["outputs"][0]["plugin"] = "jsonl"
    assert not topologies_match(topology_from_pipeline(args), topology_from_pipeline(bad)).ok


def test_swapped_route_wiring_between_two_gates_fails() -> None:
    """Two same-typed gates, three distinguishable sinks; which gate reaches which typed sink is structure.
    (Renaming connection names alone is NOT a different graph — that would be a label test.)"""
    base = {
        "source": {"plugin": "csv", "on_success": "in", "options": {"path": "r.csv"}},
        "nodes": [
            {"id": "g1", "node_type": "gate", "input": "in", "condition": "True", "routes": {"true": "mid", "false": "csv_out"}},
            {"id": "g2", "node_type": "gate", "input": "mid", "condition": "True", "routes": {"true": "json_out", "false": "jsonl_out"}},
        ],
        "outputs": [
            {"sink_name": "json_out", "plugin": "json"},
            {"sink_name": "csv_out", "plugin": "csv"},
            {"sink_name": "jsonl_out", "plugin": "jsonl"},
        ],
    }
    swapped = copy.deepcopy(base)
    swapped["nodes"][0]["routes"] = {"true": "mid", "false": "json_out"}  # source-fed gate now feeds json directly
    swapped["nodes"][1]["routes"] = {"true": "csv_out", "false": "jsonl_out"}
    result = topologies_match(topology_from_pipeline(base), topology_from_pipeline(swapped))
    assert not result.ok
    relabelled = copy.deepcopy(base)  # same graph, different connection names → MUST match
    relabelled["nodes"][0]["routes"] = {"true": "m2", "false": "csv_out"}
    relabelled["nodes"][1]["input"] = "m2"
    assert topologies_match(topology_from_pipeline(base), topology_from_pipeline(relabelled)).ok


def test_option_assertion_pins_threshold_only_when_listed() -> None:
    args = {
        "source": {"plugin": "csv", "on_success": "in", "options": {"path": "r.csv"}},
        "nodes": [
            {
                "id": "g",
                "node_type": "gate",
                "input": "in",
                "condition": "row['amount'] > 100",
                "routes": {"true": "hi", "false": "lo"},
                "options": {"threshold": 100},
            }
        ],
        "outputs": [{"sink_name": "hi", "plugin": "json"}, {"sink_name": "lo", "plugin": "json"}],
    }
    other = copy.deepcopy(args)
    other["nodes"][0]["options"]["threshold"] = 250
    exp, obs = topology_from_pipeline(args), topology_from_pipeline(other)
    assert topologies_match(exp, obs).ok  # option values ignored by default
    observed_options = {"gate": {"threshold": 250}}
    result = topologies_match(exp, obs, option_values=observed_options, option_assertions=[("gate", "threshold", 100)])
    assert not result.ok and "threshold" in (result.reason or "")
    held = topologies_match(exp, obs, option_values={"gate": {"threshold": 100}}, option_assertions=[("gate", "threshold", 100)])
    assert held.ok  # positive arm: an assertion that holds must not fail merely because assertions are listed
