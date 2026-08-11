"""Canonical proof-catalog validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from .common import (
    APPLICABILITY_PROFILE_IDS,
    CANONICAL_V2_ACCEPTANCE_SHA256,
    CANONICAL_V2_APPLICABILITY_SHA256,
    CANONICAL_V2_EVIDENCE_CONTRACT_SHA256,
    CANONICAL_V2_HARD_GATES_SHA256,
    CANONICAL_V2_LEGS_SHA256,
    CANONICAL_V2_TOP_LEVEL_KEYS,
    DIMENSIONS,
    FAMILY_LABELS,
    HARD_GATE_IDS,
    SUPPORTED_DEPLOYMENTS,
    SUPPORTED_LIFECYCLE_MODES,
    SUPPORTED_PROFILE_CASES,
    SUPPORTED_STATE_STORES,
    _dict,
    _expected_leg_ids,
    _list,
    _require,
    _semantic_sha256,
    _string,
    _strings,
    _unique,
)


def _live_plugin_inventory() -> dict[str, list[str]]:
    from elspeth.plugins.infrastructure.discovery import discover_all_plugins

    return {kind: sorted(cast(Any, plugin).name for plugin in plugin_classes) for kind, plugin_classes in discover_all_plugins().items()}


def _validate_profile_cases(catalog: dict[str, Any]) -> list[str]:
    profiles = _dict(catalog["execution_profiles"], "execution_profiles")
    stores = _list(profiles.get("state_store"), "execution_profiles.state_store")
    store_by_id: dict[str, dict[str, Any]] = {}
    for index, raw_store in enumerate(stores):
        store = _dict(raw_store, f"state_store[{index}]")
        store_id = _string(store.get("id"), f"state_store[{index}].id")
        _require(store_id not in store_by_id, f"duplicate state-store profile: {store_id}")
        store_by_id[store_id] = store

    actual_store_deployments = {
        store_id: _strings(store.get("deployments"), f"state-store profile {store_id} deployments")
        for store_id, store in store_by_id.items()
    }
    _require(
        actual_store_deployments == SUPPORTED_STATE_STORES and all(store.get("required") is True for store in store_by_id.values()),
        "catalog supported state-store profiles do not match the v2 contract",
    )

    deployments = _strings(profiles.get("deployments"), "execution_profiles.deployments")
    lifecycle_modes = _strings(profiles.get("lifecycle_modes"), "execution_profiles.lifecycle_modes")
    _unique(deployments, "execution_profiles.deployments")
    _unique(lifecycle_modes, "execution_profiles.lifecycle_modes")
    _require(deployments == SUPPORTED_DEPLOYMENTS, "catalog supported state-store profiles have deployment drift")
    _require(
        lifecycle_modes == SUPPORTED_LIFECYCLE_MODES,
        "catalog supported state-store profiles have lifecycle drift",
    )

    cases = _list(profiles.get("profile_cases"), "execution_profiles.profile_cases")
    case_ids: list[str] = []
    declared_pairs: set[tuple[str, str]] = set()
    for index, raw_case in enumerate(cases):
        case = _dict(raw_case, f"profile case {index}")
        _require(
            set(case) == {"id", "state_store", "deployment", "lifecycle_modes"},
            f"profile case {index} has unexpected fields",
        )
        case_id = _string(case.get("id"), f"profile case {index} id")
        store_id = _string(case.get("state_store"), f"profile case {case_id} state_store")
        deployment = _string(case.get("deployment"), f"profile case {case_id} deployment")
        _require(store_id in store_by_id, f"profile case {case_id} references unknown state store")
        _require(deployment in deployments, f"profile case {case_id} references unknown deployment")
        allowed_deployments = _strings(
            store_by_id[store_id].get("deployments"),
            f"state-store profile {store_id} deployments",
        )
        _require(
            deployment in allowed_deployments,
            f"profile case {case_id} is not supported by state store {store_id}",
        )
        modes = _strings(case.get("lifecycle_modes"), f"profile case {case_id} lifecycle_modes")
        _unique(modes, f"profile case {case_id} lifecycle_modes")
        _require(
            set(modes) <= set(lifecycle_modes),
            f"profile case {case_id} references unknown lifecycle mode",
        )
        case_ids.append(case_id)
        declared_pairs.add((store_id, deployment))
    _unique(case_ids, "execution_profiles.profile_cases")

    actual_cases = {case["id"]: (case["state_store"], case["deployment"], case["lifecycle_modes"]) for case in cases}
    _require(
        actual_cases == SUPPORTED_PROFILE_CASES,
        "catalog supported state-store profiles do not match the four v2 profile cases",
    )

    expected_pairs = {
        (store_id, deployment)
        for store_id, store in store_by_id.items()
        for deployment in _strings(store.get("deployments"), f"state-store profile {store_id} deployments")
    }
    _require(
        declared_pairs == expected_pairs and len(case_ids) == len(expected_pairs),
        "profile cases must cover every supported state-store/deployment pair exactly once",
    )
    return case_ids


def validate_catalog(catalog: dict[str, Any], catalog_path: Path) -> None:
    _require(catalog.get("schema_version") == 1, "catalog schema_version must be 1")
    catalog_id = _string(catalog.get("catalog_id"), "catalog_id")
    if catalog_id == "elspeth-state-engine-v2":
        _require(set(catalog) == CANONICAL_V2_TOP_LEVEL_KEYS, "canonical v2 top-level schema changed")
        _require(
            catalog.get("criteria_ref") == "docs/architecture/state_engine/completeness-criteria.md"
            and catalog.get("architecture_ref") == "docs/architecture/state_engine/architecture.md",
            "canonical v2 normative references changed",
        )
    expected_ids = _expected_leg_ids(catalog_id)
    _require(catalog.get("dimensions") == DIMENSIONS, "catalog dimensions do not match the contract")
    _require(
        catalog.get("required_leg_ids") == expected_ids,
        "catalog required_leg_ids do not match the catalog version",
    )

    legs = _list(catalog.get("legs"), "catalog legs")
    leg_ids = [_dict(leg, f"catalog leg {index}").get("id") for index, leg in enumerate(legs)]
    _require(leg_ids == expected_ids, "catalog leg ordering does not match required_leg_ids")
    _require(len(set(leg_ids)) == len(leg_ids), "catalog leg IDs contain duplicates")
    if catalog_id == "elspeth-state-engine-v2":
        _require(
            _semantic_sha256(legs) == CANONICAL_V2_LEGS_SHA256,
            "canonical v2 leg manifest changed without a catalog identity revision",
        )
    catalog_by_id = {leg["id"]: leg for leg in legs}
    for leg_id, leg in catalog_by_id.items():
        expected_leg_fields = {"id", "family", "title", "contract", "applicability_profile"}
        if "required_cases" in leg:
            expected_leg_fields.add("required_cases")
        _require(set(leg) == expected_leg_fields, f"catalog leg {leg_id} has unexpected fields")
        _require(leg.get("family") in FAMILY_LABELS, f"catalog leg {leg_id} has unknown family")
        for field in ("title", "contract", "applicability_profile"):
            value = leg.get(field)
            _require(
                isinstance(value, str) and bool(value.strip()),
                f"catalog leg {leg_id} {field} must be a non-empty string",
            )
        cases = _strings(leg.get("required_cases", ["leg-contract"]), f"catalog leg {leg_id} cases")
        _require(bool(cases), f"catalog leg {leg_id} has no required cases")
        _unique(cases, f"catalog leg {leg_id} required_cases")
    families = {leg["family"] for leg in legs}
    acceptance = _dict(catalog.get("family_dimension_acceptance"), "family acceptance")
    _require(set(acceptance) == families, "family acceptance does not cover catalog families")
    if catalog_id == "elspeth-state-engine-v2":
        _require(
            _semantic_sha256(acceptance) == CANONICAL_V2_ACCEPTANCE_SHA256,
            "canonical v2 family acceptance changed without a catalog identity revision",
        )
    for family, raw_dimensions in acceptance.items():
        dimensions = _dict(raw_dimensions, f"family acceptance {family}")
        _require(list(dimensions) == DIMENSIONS, f"family acceptance {family} is out of order")
        _require(
            all(isinstance(value, str) and bool(value.strip()) for value in dimensions.values()),
            f"family acceptance {family} must contain non-empty text",
        )
    profiles = _dict(catalog.get("execution_profiles"), "execution_profiles")
    plugin_inventory = _dict(profiles.get("first_party_plugins"), "first-party plugin inventory")
    _require(
        set(plugin_inventory) == {"sources", "transforms", "sinks", "inventory_rule"},
        "first-party plugin inventory has unexpected fields",
    )
    live_plugins = _live_plugin_inventory()
    for kind in ("sources", "transforms", "sinks"):
        names = _strings(plugin_inventory.get(kind), f"first-party plugin inventory {kind}")
        _require(names == sorted(set(names)), f"first-party plugin inventory {kind} is not sorted/unique")
        _require(names == live_plugins[kind], f"first-party plugin inventory {kind} is stale")

    applicability = _dict(catalog.get("applicability_profiles"), "applicability_profiles")
    _require(
        set(applicability) == APPLICABILITY_PROFILE_IDS[catalog_id],
        "catalog applicability profile namespace does not match the versioned contract",
    )
    if catalog_id == "elspeth-state-engine-v2":
        from elspeth.contracts.enums import TerminalPath
        from elspeth.contracts.scheduler import TokenWorkStatus

        _require(
            catalog.get("vocabularies")
            == {
                "token_work_status": [item.value for item in TokenWorkStatus],
                "terminal_path": [item.value for item in TerminalPath],
            },
            "catalog vocabularies do not match owned runtime enums",
        )
        profile_case_ids = _validate_profile_cases(catalog)
        expected_profile_fields = {"default_case_id", "profile_case_applicability", *DIMENSIONS}
    else:
        profile_case_ids = []
        expected_profile_fields = {"default_case_id", *DIMENSIONS}
    for profile_id, raw_profile in applicability.items():
        profile = _dict(raw_profile, f"applicability profile {profile_id}")
        _require(
            set(profile) == expected_profile_fields,
            f"applicability profile {profile_id} has unexpected fields",
        )
        _require(
            all(profile[dimension] in {"required", "not_applicable"} for dimension in DIMENSIONS),
            f"applicability profile {profile_id} has an invalid dimension policy",
        )
        if profile_case_ids:
            case_applicability = _dict(
                profile.get("profile_case_applicability"),
                f"applicability profile {profile_id} profile cases",
            )
            _require(
                list(case_applicability) == profile_case_ids,
                f"applicability profile {profile_id} must preserve profile-case identity and ordering",
            )
            _require(
                set(case_applicability.values()) <= {"required", "not_applicable"},
                f"applicability profile {profile_id} has invalid profile-case applicability",
            )
    for leg_id, leg in catalog_by_id.items():
        _require(
            leg.get("applicability_profile") in applicability,
            f"catalog leg {leg_id} references unknown applicability profile",
        )
    if catalog_id == "elspeth-state-engine-v2":
        _require(
            _semantic_sha256(applicability) == CANONICAL_V2_APPLICABILITY_SHA256,
            "canonical v2 applicability changed without a catalog identity revision",
        )

    gates = _list(catalog.get("hard_gates"), "catalog hard_gates")
    gate_ids = [_dict(gate, f"hard gate {index}").get("id") for index, gate in enumerate(gates)]
    _require(gate_ids == HARD_GATE_IDS, "catalog hard-gate identity or ordering changed")
    if catalog_id == "elspeth-state-engine-v2":
        _require(
            _semantic_sha256(gates) == CANONICAL_V2_HARD_GATES_SHA256,
            "canonical v2 hard gates changed without a catalog identity revision",
        )
    for gate in gates:
        expected_gate_fields = {"id", "title", "dimensions"} if catalog_id == "elspeth-state-engine-v2" else {"id", "title"}
        _require(set(gate) == expected_gate_fields, f"hard gate {gate.get('id')} has unexpected fields")
        title = gate.get("title")
        _require(isinstance(title, str) and bool(title.strip()), f"hard gate {gate.get('id')} title must be non-empty")
        if catalog_id == "elspeth-state-engine-v2":
            gate_dimensions = _strings(gate.get("dimensions"), f"hard gate {gate.get('id')} dimensions")
            _require(bool(gate_dimensions), f"hard gate {gate.get('id')} must map at least one dimension")
            _unique(gate_dimensions, f"hard gate {gate.get('id')} dimensions")
            _require(set(gate_dimensions) <= set(DIMENSIONS), f"hard gate {gate.get('id')} maps an unknown dimension")
    if catalog_id == "elspeth-state-engine-v2":
        evidence_contract = _dict(catalog.get("evidence_contract"), "catalog evidence_contract")
        _require(
            _semantic_sha256(evidence_contract) == CANONICAL_V2_EVIDENCE_CONTRACT_SHA256,
            "canonical v2 evidence contract changed without a catalog identity revision",
        )
    _require(catalog_path.is_file(), f"catalog file does not exist: {catalog_path}")
