"""Fail-closed contracts for the catalog-wide v3 evidence selector manifest."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from scripts.state_engine_assessment_lib import selectors as selectors_module
from scripts.state_engine_assessment_lib.common import ContractError
from scripts.state_engine_assessment_lib.selectors import (
    ARTIFACT_KINDS,
    LANE_SPECS,
    _collect_pytest_node_ids,
    validate_selector_manifest_data,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CATALOG_PATH = REPOSITORY_ROOT / "docs/architecture/state_engine/proof-catalog/v3/catalog.json"
PLUGIN_MATRIX_PATH = REPOSITORY_ROOT / "tests/golden/state_engine/plugin_lifecycle_matrix.json"


def _catalog() -> dict[str, Any]:
    value = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _plugin_facts() -> tuple[set[tuple[str, str]], dict[str, str]]:
    value = json.loads(PLUGIN_MATRIX_PATH.read_text(encoding="utf-8"))
    entries = value["plugins"]
    variants = {(entry["plugin_key"], variant) for entry in entries for variant in entry["variants"]}
    release_lanes = {entry["plugin_key"]: entry["release_lane"] for entry in entries}
    return variants, release_lanes


def _target_lane(profile_case: str, leg_id: str, plugin_key: str | None, release_lanes: dict[str, str]) -> str:
    if profile_case.startswith("sqlite-wal-"):
        return f"local-{profile_case}"
    if leg_id != "PB-09" or plugin_key is None or release_lanes[plugin_key] == "local":
        return "postgresql-16-aws"
    return f"protected-{release_lanes[plugin_key]}"


def _valid_manifest() -> tuple[dict[str, Any], set[tuple[str, str]], dict[str, str]]:
    catalog = _catalog()
    variants, release_lanes = _plugin_facts()
    lane_cells: dict[str, list[dict[str, str]]] = {lane_id: [] for lane_id in LANE_SPECS}
    for leg in catalog["legs"]:
        for case in leg["required_cases"]:
            for profile_case, dimensions in case["cell_applicability"].items():
                lane_id = _target_lane(profile_case, leg["id"], case["plugin_key"], release_lanes)
                for dimension_id, policy in dimensions.items():
                    if policy["status"] != "required":
                        continue
                    lane_cells[lane_id].append(
                        {
                            "leg_id": leg["id"],
                            "dimension_id": dimension_id,
                            "case_id": case["case_id"],
                            "profile_case": profile_case,
                        }
                    )

    lanes: list[dict[str, Any]] = []
    for lane_id, spec in LANE_SPECS.items():
        lanes.append(
            {
                "lane_id": lane_id,
                "workflow_job": spec.workflow_job,
                "profile_case": spec.profile_case,
                "marker_expression": spec.marker_expression,
                "node_ids": [f"tests/unit/architecture/test_state_engine_catalog_contract.py::test_selector_{lane_id}"],
                "cells": lane_cells[lane_id],
                "resource_variables": list(spec.resource_variables),
                "artifact_kinds": list(ARTIFACT_KINDS),
            }
        )
    return (
        {
            "schema_version": 1,
            "catalog_id": "elspeth-state-engine-v3",
            "lanes": lanes,
        },
        variants,
        release_lanes,
    )


def _validate(manifest: dict[str, Any]) -> None:
    _original, variants, release_lanes = _valid_manifest()
    validate_selector_manifest_data(
        manifest,
        _catalog(),
        pb09_variants=variants,
        plugin_release_lanes=release_lanes,
        collector=lambda _root, _marker, node_ids: list(node_ids),
        repository_root=REPOSITORY_ROOT,
    )


def _lane(manifest: dict[str, Any], lane_id: str) -> dict[str, Any]:
    return next(lane for lane in manifest["lanes"] if lane["lane_id"] == lane_id)


def test_complete_manifest_partitions_every_required_cell_and_collects_exact_nodes() -> None:
    manifest, variants, release_lanes = _valid_manifest()
    collected: list[tuple[str, str, tuple[str, ...]]] = []

    def collect(_root: Path, marker: str, nodes: Sequence[str]) -> list[str]:
        collected.append((marker, nodes[0], tuple(nodes)))
        return list(nodes)

    validate_selector_manifest_data(
        manifest,
        _catalog(),
        pb09_variants=variants,
        plugin_release_lanes=release_lanes,
        collector=collect,
        repository_root=REPOSITORY_ROOT,
    )

    assert len(collected) == len(LANE_SPECS)
    assert {marker for marker, _first, _nodes in collected} == {spec.marker_expression for spec in LANE_SPECS.values()}


def test_file_validator_derives_pb09_inventory_from_production_discriminators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    manifest_path = tmp_path / "evidence_selectors.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(selectors_module, "_collect_pytest_node_ids", lambda _root, _marker, nodes: list(nodes))

    selectors_module.validate_selector_manifest(manifest_path, CATALOG_PATH, repository_root=REPOSITORY_ROOT)


def test_schema_version_rejects_boolean_integer_masquerade() -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    manifest["schema_version"] = True

    with pytest.raises(ContractError, match="schema_version"):
        _validate(manifest)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda manifest: manifest.update(extra=True), "top-level fields"),
        (lambda manifest: _lane(manifest, "protected-live-aws").update(extra=True), "lane fields"),
        (lambda manifest: _lane(manifest, "protected-live-aws")["cells"][0].update(extra=True), "cell fields"),
    ],
)
def test_exact_closed_shapes_reject_extra_fields(mutation: Any, message: str) -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    mutation(manifest)

    with pytest.raises(ContractError, match=message):
        _validate(manifest)


def test_duplicate_cell_is_rejected() -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    lane = _lane(manifest, "local-sqlite-wal-single-process-leader")
    lane["cells"].append(copy.deepcopy(lane["cells"][0]))

    with pytest.raises(ContractError, match="duplicate selector cell"):
        _validate(manifest)


def test_missing_required_cell_is_rejected() -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    _lane(manifest, "local-sqlite-wal-single-process-leader")["cells"].pop()

    with pytest.raises(ContractError, match="missing required cells"):
        _validate(manifest)


def test_invented_or_catalog_na_cell_is_rejected() -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    lane = _lane(manifest, "local-sqlite-wal-single-process-leader")
    lane["cells"].append(
        {
            "leg_id": "PB-11",
            "dimension_id": "production_entry",
            "case_id": "db-server-time",
            "profile_case": lane["profile_case"],
        }
    )

    with pytest.raises(ContractError, match="invented or not_applicable cells"):
        _validate(manifest)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("profile_case", "sqlite-wal-single-process-leader", "profile"),
        ("workflow_job", "live_azure", "workflow job"),
        ("marker_expression", "live_provider", "marker expression"),
    ],
)
def test_lane_profile_job_and_marker_are_exact(field: str, value: str, message: str) -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    _lane(manifest, "protected-live-aws")[field] = value

    with pytest.raises(ContractError, match=message):
        _validate(manifest)


def test_cell_in_the_wrong_lane_is_rejected() -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    source = _lane(manifest, "protected-live-azure")
    target = _lane(manifest, "protected-live-aws")
    target["cells"].append(source["cells"].pop())

    with pytest.raises(ContractError, match="wrong selector lane"):
        _validate(manifest)


def test_node_ids_are_full_and_unique_proof_subjects() -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    _lane(manifest, "protected-live-aws")["node_ids"] = ["tests/integration/plugins"]

    with pytest.raises(ContractError, match="full pytest node ID"):
        _validate(manifest)

    manifest, _variants, _release_lanes = _valid_manifest()
    repeated = _lane(manifest, "protected-live-aws")["node_ids"][0]
    _lane(manifest, "protected-live-azure")["node_ids"] = [repeated]

    with pytest.raises(ContractError, match="one proof subject"):
        _validate(manifest)


def test_collection_must_equal_the_declared_nodes_without_skip_or_deselection() -> None:
    manifest, variants, release_lanes = _valid_manifest()

    with pytest.raises(ContractError, match="collection drift"):
        validate_selector_manifest_data(
            manifest,
            _catalog(),
            pb09_variants=variants,
            plugin_release_lanes=release_lanes,
            collector=lambda _root, _marker, nodes: list(nodes[:-1]),
            repository_root=REPOSITORY_ROOT,
        )


def test_postgresql_aws_profile_rejects_testcontainer_substitution() -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    _lane(manifest, "postgresql-16-aws")["node_ids"] = [
        "tests/testcontainer/core/test_barrier_recovery_postgres.py::test_local_postgres_is_not_aws"
    ]

    with pytest.raises(ContractError, match="testcontainer"):
        _validate(manifest)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("resource_variables", ["UNAPPROVED_PROVIDER_SECRET"], "resource variable"),
        ("artifact_kinds", [*ARTIFACT_KINDS, "raw_stdout"], "artifact kinds"),
    ],
)
def test_resource_and_artifact_vocabularies_are_closed(field: str, value: list[str], message: str) -> None:
    manifest, _variants, _release_lanes = _valid_manifest()
    _lane(manifest, "protected-live-aws")[field] = value

    with pytest.raises(ContractError, match=message):
        _validate(manifest)


def test_pb09_catalog_pairs_must_equal_independently_derived_inventory() -> None:
    manifest, variants, release_lanes = _valid_manifest()
    variants.remove(next(iter(variants)))

    with pytest.raises(ContractError, match="PB-09 variant inventory drift"):
        validate_selector_manifest_data(
            manifest,
            _catalog(),
            pb09_variants=variants,
            plugin_release_lanes=release_lanes,
            collector=lambda _root, _marker, node_ids: list(node_ids),
            repository_root=REPOSITORY_ROOT,
        )


def test_release_lane_inventory_must_cover_every_pb09_plugin() -> None:
    manifest, variants, release_lanes = _valid_manifest()
    release_lanes.pop(next(iter(release_lanes)))

    with pytest.raises(ContractError, match="release-lane inventory drift"):
        validate_selector_manifest_data(
            manifest,
            _catalog(),
            pb09_variants=variants,
            plugin_release_lanes=release_lanes,
            collector=lambda _root, _marker, node_ids: list(node_ids),
            repository_root=REPOSITORY_ROOT,
        )


def test_real_pytest_collector_returns_the_exact_requested_node_without_execution() -> None:
    node_id = (
        "tests/unit/plugins/test_state_engine_plugin_matrix.py::test_checked_golden_matches_live_mechanical_projection_without_writing"
    )

    assert _collect_pytest_node_ids(REPOSITORY_ROOT, "not live_provider", [node_id]) == [node_id]
