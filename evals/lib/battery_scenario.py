"""scenario.json contract for the composer battery (spec §2)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.lib.battery_topology import TNode, Topology
from evals.lib.scenario_from_example import extract_structural_target

_REQUIRED_KEYS = (
    "case",
    "example",
    "variant",
    "corpus_version",
    "surface",
    "canonical_arguments",
    "expected_topology",
    "option_assertions",
    "floor",
    "red_criteria",
    "green_criteria",
)
# CLOSED criteria vocabularies (spec §2). rgr's open string vocabulary has already drifted three ways; here an
# unknown key is a load error, so a misspelled criterion can never silently disable a gate.
GREEN_KEYS: frozenset[str] = frozenset(
    {"topology_matches_expected", "option_assertions_hold", "must_discover_schema_before_first_mutation", "is_valid"}
)
RED_KEYS: frozenset[str] = frozenset({"passivity_phrases", "build_failure_sentinels"})
_FLOOR_KEYS = ("tool_bearing_calls", "components", "repairs", "backtracks", "derivation", "pre_calibration", "post_calibration")


@dataclass
class Floor:
    tool_bearing_calls: int
    components: dict[str, int]
    repairs: int
    backtracks: int
    derivation: list[str]
    pre_calibration: int
    post_calibration: int | None


@dataclass
class Scenario:
    case: str
    example: str | None
    variant: str | None
    corpus_version: int
    surface_required: str
    classifier_decision: str
    canonical_arguments: dict[str, Any]
    expected_topology: dict[str, Any]
    option_assertions: list[list[Any]]
    floor: Floor
    red_criteria: dict[str, Any]
    green_criteria: dict[str, Any]
    path: Path = field(default_factory=Path)


def load_scenario(path: Path) -> Scenario:
    doc = json.loads(Path(path).read_text())
    missing = [k for k in _REQUIRED_KEYS if k not in doc]
    if missing:
        raise ValueError(f"{path}: scenario missing keys {missing}")
    floor_doc = doc["floor"]
    fmissing = [k for k in _FLOOR_KEYS if k not in floor_doc]
    if fmissing:
        raise ValueError(f"{path}: floor missing keys {fmissing}")
    surface = doc["surface"]
    bad_green = sorted(set(doc["green_criteria"]) - GREEN_KEYS)
    bad_red = sorted(set(doc["red_criteria"]) - RED_KEYS)
    if bad_green or bad_red:
        raise ValueError(
            f"{path}: unknown criteria keys green={bad_green} red={bad_red} (closed vocabulary: {sorted(GREEN_KEYS)} / {sorted(RED_KEYS)})"
        )
    if any(not isinstance(v, bool) for v in doc["green_criteria"].values()):
        raise ValueError(f"{path}: green_criteria values must be booleans")
    return Scenario(
        case=str(doc["case"]),
        example=doc["example"],
        variant=doc["variant"],
        corpus_version=int(doc["corpus_version"]),
        surface_required=str(surface["required"]),
        classifier_decision=str(surface["classifier_decision"]),
        canonical_arguments=dict(doc["canonical_arguments"]),
        expected_topology=dict(doc["expected_topology"]),
        option_assertions=[list(a) for a in doc["option_assertions"]],
        floor=Floor(**{k: floor_doc[k] for k in _FLOOR_KEYS}),
        red_criteria=dict(doc["red_criteria"]),
        green_criteria=dict(doc["green_criteria"]),
        path=Path(path),
    )


def topology_to_dict(t: Topology) -> dict[str, Any]:
    return {
        "nodes": [{"kind": n.kind, "plugin": n.plugin, "extras": dict(n.extras)} for n in t.nodes],
        "edges": [[a, b, k] for a, b, k in t.edges],
    }


def topology_from_dict(d: Mapping[str, Any]) -> Topology:
    nodes = tuple(
        TNode(str(n["kind"]), n.get("plugin"), tuple(sorted((str(k), str(v)) for k, v in dict(n.get("extras") or {}).items())))
        for n in d["nodes"]
    )
    edges = tuple(sorted((int(a), int(b), str(k)) for a, b, k in d["edges"]))
    return Topology(nodes, edges)


def validate_canonical_arguments(args: Mapping[str, Any]) -> None:
    """Same two checks the parity fixtures get: schema + trained-operator plugin availability."""
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.redaction import SetPipelineArgumentsModel
    from elspeth.web.dependencies import create_catalog_service
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId

    model = SetPipelineArgumentsModel.model_validate(dict(args))
    if not model.nodes or not model.outputs:
        raise ValueError("canonical_arguments must declare at least one node and one output")
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    view = PolicyCatalogView.for_trained_operator(catalog, snapshot)
    refs: set[PluginId] = set()
    if model.source is not None:
        refs.add(PluginId("source", model.source.plugin))
    if model.sources is not None:
        for named in model.sources.values():
            refs.add(PluginId("source", named.plugin))
    for node in model.nodes:
        if node.plugin is not None:
            refs.add(PluginId("transform", node.plugin))
    for output in model.outputs:
        refs.add(PluginId("sink", output.plugin))
    for plugin_id in sorted(refs, key=str):
        reason = view.unavailable_reason(plugin_id)
        if reason is not None:
            raise ValueError(f"plugin {plugin_id} unavailable to a trained operator ({reason})")


def extractor_cross_check(scenario: Scenario, repo_root: Path) -> list[str]:
    """The example extractor's node-kind multiset (fork→gate) must be ⊆ expected_topology's."""
    if not scenario.example:
        return []
    target = extract_structural_target(repo_root / scenario.example, scenario.variant)
    extracted: list[str] = ["gate" for _ in target["gates"]]
    extracted += ["coalesce" for _ in target["coalesce_nodes"]]
    extracted += ["aggregation" for _ in target["aggregations"]]
    extracted += ["transform" for _ in target["transforms"]]
    expected_kinds = [n["kind"] for n in scenario.expected_topology.get("nodes", [])]
    violations: list[str] = []
    for kind in set(extracted):
        if extracted.count(kind) > expected_kinds.count(kind):
            violations.append(f"extractor sees {extracted.count(kind)} x {kind}, expected_topology has {expected_kinds.count(kind)}")
    return violations


__all__ = [
    "Floor",
    "Scenario",
    "extractor_cross_check",
    "load_scenario",
    "topology_from_dict",
    "topology_to_dict",
    "validate_canonical_arguments",
]
