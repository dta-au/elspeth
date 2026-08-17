from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.lib.battery_scenario import (
    extractor_cross_check,
    load_scenario,
    topology_from_dict,
    topology_to_dict,
    validate_canonical_arguments,
)
from evals.lib.battery_topology import topology_from_pipeline

REPO = Path(__file__).resolve().parents[4]
SCENARIOS = REPO / "evals/composer-battery/scenarios"
PARITY = REPO / "evals/composer-parity/fixtures"


def test_fork_coalesce_scenario_loads_and_round_trips() -> None:
    sc = load_scenario(SCENARIOS / "fork_coalesce/scenario.json")
    assert sc.case == "fork_coalesce"
    assert sc.floor.tool_bearing_calls == 2
    derived = topology_from_pipeline(sc.canonical_arguments)
    assert topology_to_dict(derived) == sc.expected_topology
    assert topology_from_dict(sc.expected_topology) == derived


def test_canonical_arguments_validate_like_parity_fixtures() -> None:
    args = json.loads((PARITY / "fork_coalesce.json").read_text())["canonical_arguments"]
    validate_canonical_arguments(args)  # must not raise


def test_canonical_arguments_reject_unknown_plugin() -> None:
    args = json.loads((PARITY / "linear_transform.json").read_text())["canonical_arguments"]
    args["nodes"][0]["plugin"] = "definitely_not_a_plugin"
    with pytest.raises(ValueError, match="plugin"):
        validate_canonical_arguments(args)


def test_missing_key_is_a_loud_error(tmp_path: Path) -> None:
    doc = json.loads((SCENARIOS / "fork_coalesce/scenario.json").read_text())
    del doc["floor"]
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="floor"):
        load_scenario(p)


def test_criteria_vocabulary_is_closed(tmp_path: Path) -> None:
    doc = json.loads((SCENARIOS / "fork_coalesce/scenario.json").read_text())
    doc["green_criteria"]["topology_matches_expcted"] = True  # typo must not silently create an unchecked gate
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="topology_matches_expcted"):
        load_scenario(p)


def test_extractor_cross_check_passes_for_fork_coalesce() -> None:
    sc = load_scenario(SCENARIOS / "fork_coalesce/scenario.json")
    assert extractor_cross_check(sc, REPO) == []


def test_extractor_cross_check_flags_missing_kind() -> None:
    sc = load_scenario(SCENARIOS / "fork_coalesce/scenario.json")
    stripped = json.loads(json.dumps(sc.expected_topology))
    stripped["nodes"] = [n for n in stripped["nodes"] if n["kind"] != "coalesce"]
    sc.expected_topology = stripped
    assert any("coalesce" in v for v in extractor_cross_check(sc, REPO))
