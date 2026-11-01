"""Bounded conformance controls for the validate-secret-reference tool."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.secrets import ResolvedSecret, SecretInventoryItem
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import secrets
from elspeth.web.composer.tools._common import ToolContext
from elspeth.web.composer.tools._dispatch import execute_discovery_tool_with_context, execute_tool
from elspeth.web.composer.tools._registry import resolve_tool_effects
from elspeth.web.composer.tools.discovery import is_cacheable_discovery_tool
from elspeth.web.composer.tools.wire_projection import decode_wire_arguments
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot


class ReferenceProbe:
    """Synthetic resolver: obtaining a value is always an assertion failure."""

    def __init__(self, items: list[SecretInventoryItem], available: bool, *, fault: str | None = None) -> None:
        self.items = items
        self.available = available
        self.fault = fault
        self.calls: list[tuple[str, ...]] = []

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        self.calls.append(("list", user_id))
        if self.fault == "list":
            raise RuntimeError("synthetic inventory fault")
        return self.items

    def has_ref(self, user_id: str, name: str) -> bool:
        self.calls.append(("has", user_id, name))
        if self.fault == "has":
            raise RuntimeError("synthetic readiness fault")
        return self.available

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        raise AssertionError("value resolution is outside this tool's contract")


@pytest.fixture
def context() -> ToolContext:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot, user_id="synthetic-user"
    )


@pytest.mark.parametrize("dialect", list(ToolContractDialect), ids=lambda value: value.value)
@pytest.mark.parametrize(
    "name", ["SELECTED_REF", "", " spaced ", "Référence_☃", "R" * 4097], ids=["ordinary", "empty", "whitespace", "unicode", "long"]
)
def test_exact_name_wire_and_dispatch(name: str, dialect: ToolContractDialect, context: ToolContext) -> None:
    """VS-C1/C5: string identity survives the common-loop palette and decode."""
    loop = next(tool for tool in composer_loop_tool_definitions(dialect) if tool["function"]["name"] == "validate_secret_ref")
    assert loop["function"]["parameters"] == {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Secret reference name (e.g. 'OPENROUTER_API_KEY')."}},
        "required": ["name"],
        "additionalProperties": False,
    }
    assert loop["function"].get("strict") is (True if dialect is ToolContractDialect.OPENAI_STRICT else None)
    decoded = decode_wire_arguments("validate_secret_ref", dialect, {"name": name})
    assert decoded.wire_conformant is True
    assert dict(decoded.semantic) == {"name": name}
    probe = ReferenceProbe([], False)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Synthetic validation"), version=7)
    result = execute_discovery_tool_with_context(
        "validate_secret_ref", dict(decoded.semantic), state, replace(context, secret_service=probe)
    )
    assert result.success is True
    assert result.updated_state is state
    assert result.affected_nodes == ()
    assert probe.calls == [("list", "synthetic-user"), ("has", "synthetic-user", name)]
    assert admit_discovery_result("validate_secret_ref", result).response.to_wire() == {"name": name, "available": False}


@pytest.mark.parametrize(
    "arguments",
    [{}, {"name": None}, {"name": 42}, {"name": True}, {"name": []}, {"name": {}}, {"name": "REF", "scope": "server"}],
    ids=["omission", "null", "number", "boolean", "array", "object", "extra-scope"],
)
@pytest.mark.parametrize("route", ["direct", "readonly", "public-raise", "public-return"])
def test_invalid_shape_prevents_store_access(arguments: dict[str, Any], route: str, context: ToolContext) -> None:
    """VS-C1: invalid calls fail before touching either resolver operation."""
    probe = ReferenceProbe([], True)
    active = replace(context, secret_service=probe)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Synthetic validation"), version=7)
    if route == "public-return":
        result = execute_tool(
            "validate_secret_ref",
            arguments,
            state,
            active.catalog,
            plugin_snapshot=active.plugin_snapshot,
            secret_service=probe,
            user_id=active.user_id,
            validate_arguments=True,
        )
        assert result.success is False
        assert result.updated_state is state
        assert result.data is None
    else:
        with pytest.raises(ToolArgumentError):
            if route == "direct":
                secrets._handle_validate_secret_ref(arguments, state, active)
            elif route == "readonly":
                execute_discovery_tool_with_context("validate_secret_ref", arguments, state, active)
            else:
                execute_tool(
                    "validate_secret_ref",
                    arguments,
                    state,
                    active.catalog,
                    plugin_snapshot=active.plugin_snapshot,
                    secret_service=probe,
                    user_id=active.user_id,
                    raise_schema_argument_errors=True,
                )
    assert probe.calls == []


@pytest.mark.parametrize(
    "scope,kind,old_reason,available,reason",
    [
        ("user", "user_store", None, False, "value_decryption_failed"),
        ("server", "env", None, False, "env_var_not_set"),
        ("server", "other", None, False, "env_var_not_set"),
        ("user", "env", None, False, "env_var_not_set"),
        ("user", "user_store", "fingerprint_resolver_not_configured", False, "fingerprint_resolver_not_configured"),
        ("user", "user_store", "value_decryption_failed", True, None),
    ],
    ids=["stored-fallback", "env-fallback", "server-fallback", "env-kind-fallback", "preserve-known-reason", "clear-stale-reason"],
)
def test_targeted_status_replaces_inventory_availability(
    scope: str, kind: str, old_reason: str | None, available: bool, reason: str | None, context: ToolContext
) -> None:
    """VS-C3: selected metadata is preserved but status comes from has_ref."""
    item = SecretInventoryItem("SELECTED_REF", scope, old_reason is None, kind, old_reason)
    probe = ReferenceProbe([item], available)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Synthetic validation"), version=7)
    before = state.to_dict()
    result = execute_tool(
        "validate_secret_ref",
        {"name": item.name},
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        secret_service=probe,
        user_id=context.user_id,
        validate_arguments=True,
    )
    selected = admit_discovery_result("validate_secret_ref", result)
    expected = {"name": item.name, "scope": scope, "source_kind": kind, "available": available, "reason": reason}
    assert selected.response.to_wire() == expected
    # The common loop's tool message for an admitted discovery result is
    # serialize_admitted_discovery_result (tool_batch); no tool-specific projection.
    assert json.loads(serialize_admitted_discovery_result(selected))["data"] == expected
    assert result.success is True and result.updated_state is state
    assert state.to_dict() == before and result.affected_nodes == ()
    assert result.validation.is_valid is False  # Empty graph validity is separate from metadata success.
    assert probe.calls == [("list", "synthetic-user"), ("has", "synthetic-user", item.name)]


@pytest.mark.parametrize("available", [False, True])
def test_unlisted_status_is_two_fields(available: bool, context: ToolContext) -> None:
    """VS-C3: an unlisted live match is permitted; no invented scope/reason."""
    probe = ReferenceProbe([SecretInventoryItem("OTHER_REF", "server", True, "env")], available)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Synthetic validation"), version=7)
    result = execute_discovery_tool_with_context(
        "validate_secret_ref", {"name": "SELECTED_REF"}, state, replace(context, secret_service=probe)
    )
    assert result.success is True
    assert admit_discovery_result("validate_secret_ref", result).response.to_wire() == {"name": "SELECTED_REF", "available": available}


@pytest.mark.parametrize("gap", ["user", "service", "both"])
def test_missing_authority_refuses_without_store_reads(gap: str, context: ToolContext) -> None:
    """VS-C2: context is server supplied and absent authority never means miss."""
    probe = ReferenceProbe([], True)
    active = replace(context, secret_service=None if gap != "user" else probe, user_id=None if gap != "service" else context.user_id)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Synthetic validation"), version=7)
    result = execute_discovery_tool_with_context("validate_secret_ref", {"name": "SELECTED_REF"}, state, active)
    assert result.success is False and result.updated_state is state
    assert result.validation.errors[0].error_code == "secret_context_unavailable"
    assert result.validation.errors[0].message == "Secret tools require secret service context."
    assert result.data is None and probe.calls == []
    assert admit_discovery_result("validate_secret_ref", result).response is None


@pytest.mark.parametrize("fault", ["list", "has"])
def test_store_fault_is_not_missing_success(fault: str, context: ToolContext) -> None:
    """VS-C7: direct synchronous seam propagates unexpected store faults."""
    probe = ReferenceProbe([], False, fault=fault)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Synthetic validation"), version=7)
    before = state.to_dict()
    with pytest.raises(RuntimeError, match="synthetic"):
        execute_discovery_tool_with_context("validate_secret_ref", {"name": "SELECTED_REF"}, state, replace(context, secret_service=probe))
    assert state.to_dict() == before
    assert probe.calls == (
        [("list", "synthetic-user")] if fault == "list" else [("list", "synthetic-user"), ("has", "synthetic-user", "SELECTED_REF")]
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "REF", "available": True, "value": "synthetic-private-canary"},
        {"name": "REF", "available": 1},
        {"name": "REF", "scope": "user", "available": False, "source_kind": "stored", "reason": None},
        {"name": "REF", "scope": "invalid", "available": True, "source_kind": "stored", "reason": None},
    ],
    ids=["extra-value", "nonboolean", "incoherent-reason", "invalid-scope"],
)
def test_selected_egress_rejects_corrupt_or_extra_data(payload: dict[str, Any]) -> None:
    """VS-C4 negative control: selected encoder refuses extra credential fields."""
    with pytest.raises(FrameworkBugError):
        secrets._SECRET_STATUS_RESPONSE.admit(payload)


def test_secret_probe_is_not_cached_or_graph_authority(context: ToolContext) -> None:
    """VS-C6: repeats hit live readiness; declaration grants no graph effects."""
    assert is_cacheable_discovery_tool("validate_secret_ref") is False
    assert resolve_tool_effects("validate_secret_ref", {"name": "REF"}).domains == ()
    probe = ReferenceProbe([], True)
    active = replace(context, secret_service=probe)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Synthetic validation"), version=7)
    first = execute_discovery_tool_with_context("validate_secret_ref", {"name": "REF"}, state, active)
    probe.available = False
    second = execute_discovery_tool_with_context("validate_secret_ref", {"name": "REF"}, state, active)
    assert first.data["available"] is True and second.data["available"] is False
    assert probe.calls == [("list", "synthetic-user"), ("has", "synthetic-user", "REF")] * 2
