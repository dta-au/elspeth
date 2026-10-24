"""Historical catalogs and maintained runtime policy have independent contracts."""

from copy import deepcopy
from enum import Enum
from pathlib import Path

import pytest
from scripts.state_engine_assessment_lib import catalog as subject
from scripts.state_engine_assessment_lib.common import ContractError, _semantic_sha256, load_unique_json

from elspeth.contracts import enums, scheduler

_ROOT = Path(__file__).resolve().parents[3]
_V2 = _ROOT / "docs/architecture/state_engine/proof-catalog/v2/catalog.json"
_V3 = _ROOT / "docs/architecture/state_engine/proof-catalog/v3/catalog.json"


@pytest.mark.parametrize("vocabulary", ["terminal_path", "token_work_status"])
def test_frozen_catalog_is_independent_of_future_runtime_enums(monkeypatch: pytest.MonkeyPatch, vocabulary: str) -> None:
    historical = load_unique_json(_V2)
    current = load_unique_json(_V3)
    subject.validate_catalog(historical, _V2)
    subject.validate_catalog(current, _V3)
    if vocabulary == "terminal_path":
        future_enum = Enum(
            "FutureTerminalPath", {item.name: item.value for item in enums.TerminalPath} | {"FUTURE": "future_path"}, type=str
        )
        monkeypatch.setattr(enums, "TerminalPath", future_enum)
    else:
        future_enum = Enum(
            "FutureTokenWorkStatus", {item.name: item.value for item in scheduler.TokenWorkStatus} | {"FUTURE": "future_work"}, type=str
        )
        monkeypatch.setattr(scheduler, "TokenWorkStatus", future_enum)
    subject.validate_catalog(historical, _V2)
    with pytest.raises(ContractError, match="vocabularies do not match owned runtime enums"):
        subject.validate_catalog(current, _V3)


@pytest.mark.parametrize(
    "policy", ["SUPPORTED_STATE_STORES", "SUPPORTED_DEPLOYMENTS", "SUPPORTED_LIFECYCLE_MODES", "SUPPORTED_PROFILE_CASES"]
)
def test_frozen_catalog_is_independent_of_current_support_policy(monkeypatch: pytest.MonkeyPatch, policy: str) -> None:
    historical = load_unique_json(_V2)
    current = load_unique_json(_V3)
    subject.validate_catalog(historical, _V2)
    subject.validate_catalog(current, _V3)
    if policy == "SUPPORTED_STATE_STORES":
        stores = deepcopy(subject.SUPPORTED_STATE_STORES)
        stores["postgresql-16"].append("future-deployment")
        monkeypatch.setattr(subject, policy, stores)
    elif policy == "SUPPORTED_DEPLOYMENTS":
        monkeypatch.setattr(subject, policy, [*subject.SUPPORTED_DEPLOYMENTS, "future-deployment"])
    elif policy == "SUPPORTED_LIFECYCLE_MODES":
        monkeypatch.setattr(subject, policy, [*subject.SUPPORTED_LIFECYCLE_MODES, "future-lifecycle"])
    else:
        monkeypatch.setattr(subject, policy, {})
    subject.validate_catalog(historical, _V2)
    with pytest.raises(ContractError, match="supported state-store profiles"):
        subject.validate_catalog(current, _V3)


def test_current_plugin_amendment_has_its_own_pin_and_preserves_frozen_history(monkeypatch: pytest.MonkeyPatch) -> None:
    historical_bytes = _V2.read_bytes()
    historical_pin = subject.CANONICAL_V2_EXECUTION_PROFILES_SHA256
    historical = load_unique_json(_V2)
    current = load_unique_json(_V3)
    discovered = subject._live_plugin_inventory()
    future_name = "future_historical_inventory_control"
    assert future_name not in discovered["transforms"]
    changed_inventory = deepcopy(discovered)
    changed_inventory["transforms"] = sorted([*changed_inventory["transforms"], future_name])
    monkeypatch.setattr(subject, "_live_plugin_inventory", lambda: changed_inventory)
    subject.validate_catalog(historical, _V2)
    with pytest.raises(ContractError, match="first-party plugin inventory transforms is stale"):
        subject.validate_catalog(current, _V3)

    current["execution_profiles"]["first_party_plugins"]["transforms"] = changed_inventory["transforms"]
    pb09 = next(leg for leg in current["legs"] if leg["id"] == "PB-09")
    case = deepcopy(pb09["required_cases"][0])
    case.update(case_id=f"transform:{future_name}", plugin_key=f"transform:{future_name}", variant_id="default")
    pb09["required_cases"].append(case)
    with pytest.raises(ContractError, match="canonical v3 execution profiles changed"):
        subject.validate_catalog(current, _V3)
    monkeypatch.setattr(subject, "CANONICAL_V3_EXECUTION_PROFILES_SHA256", _semantic_sha256(current["execution_profiles"]))
    subject.validate_catalog(current, _V3)
    subject.validate_catalog(historical, _V2)
    assert historical_pin == subject.CANONICAL_V2_EXECUTION_PROFILES_SHA256
    assert _V2.read_bytes() == historical_bytes

    pb09["required_cases"].pop()
    with pytest.raises(ContractError, match="PB-09 plugin keys"):
        subject.validate_catalog(current, _V3)


def test_current_support_policy_cannot_be_bypassed_by_updating_only_the_current_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    current = load_unique_json(_V3)
    profiles = current["execution_profiles"]
    postgres = next(store for store in profiles["state_store"] if store["id"] == "postgresql-16")
    postgres["deployments"].append("unsupported-multi-host")
    profiles["deployments"].append("unsupported-multi-host")
    profiles["profile_cases"].append(
        {
            "id": "postgresql-16-unsupported-multi-host",
            "state_store": "postgresql-16",
            "deployment": "unsupported-multi-host",
            "lifecycle_modes": ["fresh"],
        }
    )
    monkeypatch.setattr(subject, "CANONICAL_V3_EXECUTION_PROFILES_SHA256", _semantic_sha256(profiles))
    with pytest.raises(ContractError, match="supported state-store profiles do not match the current contract"):
        subject.validate_catalog(current, _V3)


def test_current_catalog_refuses_current_inventory_written_into_historical_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    historical = load_unique_json(_V2)
    current = load_unique_json(_V3)
    subject.validate_catalog(current, _V3)

    plugin_name = "batch_duplicate_annotator"
    inventory = historical["execution_profiles"]["first_party_plugins"]["transforms"]
    assert plugin_name not in inventory
    assert plugin_name in current["execution_profiles"]["first_party_plugins"]["transforms"]
    inventory.append(plugin_name)
    inventory.sort()
    pb09 = next(leg for leg in historical["legs"] if leg["id"] == "PB-09")
    pb09["required_cases"].append(f"transform:{plugin_name}")
    pb09["required_cases"].sort()

    def load_predecessor(path: Path) -> dict[str, object]:
        assert path == _V2
        return historical

    monkeypatch.setattr(subject, "load_unique_json", load_predecessor)
    with pytest.raises(ContractError, match="canonical v2 leg manifest changed"):
        subject.validate_catalog(current, _V3)


def test_historical_profile_mutation_is_still_refused() -> None:
    historical = load_unique_json(_V2)
    historical["execution_profiles"]["state_store"][0]["scope"] += " changed"
    with pytest.raises(ContractError, match="canonical v2 execution profiles changed"):
        subject.validate_catalog(historical, _V2)


def test_historical_vocabulary_mutation_is_still_refused() -> None:
    historical = load_unique_json(_V2)
    historical["vocabularies"]["terminal_path"].append("invented_historical_path")
    with pytest.raises(ContractError, match="vocabularies do not match the historical v2 contract"):
        subject.validate_catalog(historical, _V2)
