"""Secret discovery closes metadata shapes without introducing secret values."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import BaseModel

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.secrets import SecretInventoryItem
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.response_contracts import ResponseContract
from elspeth.web.composer.tools._registry import response_contract_for
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, _SecretServiceDouble, execute_tool


def _contract(name: str) -> ResponseContract:
    contract = response_contract_for(name)
    assert contract is not None, f"{name} needs its producer-selected response contract"
    return contract


def _item() -> dict[str, object]:
    return {"name": "ref", "scope": "user", "available": True, "source_kind": "stored", "reason": None}


@pytest.mark.parametrize(
    "case",
    [
        "empty_inventory",
        "inventory",
        "matched_ready",
        "matched_unavailable",
        "matched_env_unavailable",
        "unmatched_unavailable",
        "unmatched_ready",
    ],
)
def test_real_secret_producers_preserve_legacy_bytes(case: str) -> None:
    ready = SecretInventoryItem(name="clé", scope="user", available=True, source_kind="stored")
    unavailable = SecretInventoryItem(name="TEAM_REF", scope="org", available=False, source_kind="stored", reason="value_decryption_failed")
    server = SecretInventoryItem(name="SERVER_REF", scope="server", available=True, source_kind="env")
    service = _SecretServiceDouble()
    service.list_refs.return_value = []
    service.has_ref.return_value = False
    arguments: dict[str, object] = {}
    tool_name = "list_secret_refs" if case in {"empty_inventory", "inventory"} else "validate_secret_ref"
    if case == "inventory":
        service.list_refs.return_value = [ready, unavailable, server]
    elif case == "matched_ready":
        service.list_refs.return_value = [unavailable]
        service.has_ref.return_value = True
        arguments = {"name": "TEAM_REF"}
    elif case == "matched_unavailable":
        service.list_refs.return_value = [ready]
        arguments = {"name": "clé"}
    elif case == "matched_env_unavailable":
        service.list_refs.return_value = [server]
        arguments = {"name": "SERVER_REF"}
    elif case.startswith("unmatched_"):
        service.has_ref.return_value = case == "unmatched_ready"
        arguments = {"name": "UNLISTED"}
    result = execute_tool(tool_name, arguments, _empty_state(), _mock_catalog(), secret_service=service, user_id="response-test")
    assert result.success
    _contract(tool_name)
    admitted = admit_discovery_result(tool_name, result)
    fixture = json.loads(Path(__file__).with_name("secret_response_legacy_wire.json").read_text())
    assert serialize_admitted_discovery_result(admitted) == fixture["cases"][case]
    assert admitted.response is not None
    assert admitted.response.readmit(_contract(tool_name)).to_wire() == admitted.response.to_wire()


@pytest.mark.parametrize(
    "field,value", [("name", 7), ("scope", "private-value"), ("available", 1), ("source_kind", None), ("reason", "private-value")]
)
def test_secret_inventory_rejects_wrong_owned_scalar(field: str, value: object) -> None:
    item = _item()
    item[field] = value
    contract = _contract("list_secret_refs")
    with pytest.raises(FrameworkBugError) as caught:
        contract.admit([item])
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("tool_name", ["list_secret_refs", "validate_secret_ref"])
def test_secret_inventory_rejects_extra_and_missing_fields(tool_name: str) -> None:
    contract = _contract(tool_name)
    for item in ({**_item(), "value": "PRIVATE_MATERIAL"}, {key: value for key, value in _item().items() if key != "reason"}):
        candidate = [item] if tool_name == "list_secret_refs" else item
        with pytest.raises(FrameworkBugError) as caught:
            contract.admit(candidate)
        assert "PRIVATE_MATERIAL" not in str(caught.value)


@pytest.mark.parametrize("available,reason", [(True, "env_var_not_set"), (False, None)])
def test_secret_inventory_rejects_incoherent_availability(available: bool, reason: str | None) -> None:
    with pytest.raises(FrameworkBugError):
        _contract("list_secret_refs").admit([{**_item(), "available": available, "reason": reason}])


def test_unmatched_ref_has_its_own_closed_shape() -> None:
    contract = _contract("validate_secret_ref")
    for available in (False, True):
        value = {"name": "", "available": available}
        assert contract.admit(value).to_wire() == value
    for malformed in ({"name": "ref", "available": "yes"}, {"name": "ref"}, {"name": "ref", "available": False, "reason": None}):
        with pytest.raises(FrameworkBugError):
            contract.admit(malformed)


def test_secret_admission_owns_immutable_copy_and_fresh_wire() -> None:
    raw = _item()
    contract = _contract("list_secret_refs")
    admitted = contract.admit([MappingProxyType(raw)])
    raw["name"] = "changed"
    wire = admitted.to_wire()
    assert isinstance(wire, list)
    assert wire == [_item()]
    wire.clear()
    assert admitted.to_wire() == [_item()]
    assert admitted.readmit(contract).to_wire() == [_item()]
    with pytest.raises(FrameworkBugError):
        admitted.readmit(_contract("validate_secret_ref"))


def test_secret_admission_rechecks_nominal_records_and_refuses_impostors() -> None:
    contract = _contract("list_secret_refs")
    valid = SecretInventoryItem(name="ref", scope="user", available=True, source_kind="stored")
    assert contract.admit((valid,)).to_wire() == [_item()]
    with pytest.raises(FrozenInstanceError):
        valid.name = "changed"
    malformed = SecretInventoryItem(name=7, scope="user", available=True)
    with pytest.raises(FrameworkBugError):
        contract.admit((malformed,))

    class InventoryImpostor(BaseModel):
        name: str = "ref"

    with pytest.raises(FrameworkBugError):
        contract.admit([InventoryImpostor()])
    with pytest.raises(FrameworkBugError):
        contract.admit({"items": []})
