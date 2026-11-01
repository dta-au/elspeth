"""Synthetic public-dispatch controls for the wire-secret calling contract."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.secrets import ResolvedSecret, SecretInventoryItem
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolContext, ToolResult
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.composer.tools.wire_projection import decode_wire_arguments
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.secrets.wiring_policy import SecretWiringComponentType, SecretWiringPolicy, SecretWiringRule
from tests.unit.web.composer.test_secret_rejection_contracts import _state

_REF = "SYNTHETIC_WIRE_REF"


class _SyntheticResolver:
    """Models only targeted availability; values and inventory are forbidden."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, str]] = []

    def has_ref(self, user_id: str, name: str) -> bool:
        self.calls.append((user_id, name))
        return self.available and name == _REF

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        raise AssertionError("wire must not list all references")

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        raise AssertionError("wire must not obtain a secret value")


@pytest.fixture
def wire_context() -> ToolContext:
    full = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(full)
    return ToolContext(
        catalog=PolicyCatalogView.for_trained_operator(full, snapshot),
        plugin_snapshot=snapshot,
        secret_service=_SyntheticResolver(),
        user_id="synthetic-wire-user",
    )


def _policy(component: SecretWiringComponentType, plugin: str, key: str = "api_key") -> SecretWiringPolicy:
    assert component in {"source", "transform", "sink"}
    return SecretWiringPolicy(rules=(SecretWiringRule(_REF, component, plugin, key),))


def _call(state: CompositionState, args: dict[str, Any], context: ToolContext) -> ToolResult:
    before = state.to_dict()
    result = execute_tool(
        "wire_secret_ref",
        args,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        secret_service=context.secret_service,
        secret_wiring_policy=context.secret_wiring_policy,
        user_id=context.user_id,
        validate_arguments=True,
        raise_schema_argument_errors=True,
    )
    assert state.to_dict() == before
    assert "synthetic-never-return-this-value" not in json.dumps(result.to_dict())
    return result


@pytest.mark.parametrize("dialect", [ToolContractDialect.NONE, ToolContractDialect.OPENAI_STRICT])
@pytest.mark.parametrize("selection", ["sole-omitted", "plural-explicit", "plural-omitted", "prefixed", "whitespace"])
def test_named_source_selector_is_exact_and_conditionally_required(
    wire_context: ToolContext, dialect: ToolContractDialect, selection: str
) -> None:
    original = _state()
    sources = {"left": original.sources["source"]}
    if selection != "sole-omitted":
        sources["right"] = original.sources["source"]
    state = replace(original, sources=sources)
    args: dict[str, Any] = {"name": _REF, "target": "source", "option_key": "api_key"}
    if selection in {"plural-explicit", "prefixed", "whitespace"}:
        args["target_id"] = {"plural-explicit": "left", "prefixed": "source:left", "whitespace": " left "}[selection]
    wire = dict(args)
    if dialect is ToolContractDialect.OPENAI_STRICT and "target_id" not in wire:
        wire["target_id"] = None
    decoded = decode_wire_arguments("wire_secret_ref", dialect, wire)
    assert decoded.wire_conformant is True
    assert deep_thaw(decoded.semantic) == args
    result = _call(state, deep_thaw(decoded.semantic), replace(wire_context, secret_wiring_policy=_policy("source", "json")))
    if selection in {"sole-omitted", "plural-explicit"}:
        assert result.success is True
        assert result.updated_state.version == 8
        assert result.affected_nodes == ("source:left",)
        assert deep_thaw(result.updated_state.sources["left"].options)["api_key"] == {"secret_ref": _REF}
        if "right" in sources:
            assert result.updated_state.sources["right"] == state.sources["right"]
        assert result.updated_state.nodes == state.nodes
        assert result.updated_state.outputs == state.outputs
        assert result.data is None
        assert result.prior_validation is not None
        assert "validation_delta" in result.to_dict()
    else:
        assert result.success is False
        assert result.updated_state is state
        assert result.affected_nodes == ()
        assert result.data is None
        assert result.validation.errors[0].error_code == ("secret_target_required" if selection == "plural-omitted" else "source_not_found")
        assert result.validation_guidance is not None


def test_strict_missing_selector_is_classified_before_semantic_admission(wire_context: ToolContext) -> None:
    args = {"name": _REF, "target": "source", "option_key": "api_key"}
    decoded = decode_wire_arguments("wire_secret_ref", ToolContractDialect.OPENAI_STRICT, args)
    assert decoded.wire_conformant is False
    assert deep_thaw(decoded.semantic) == args
    result = _call(_state(), deep_thaw(decoded.semantic), replace(wire_context, secret_wiring_policy=_policy("source", "json")))
    assert result.success is True


@pytest.mark.parametrize(
    "target,component,plugin,target_id",
    [("source", "source", "json", None), ("node", "transform", "passthrough", "clean"), ("output", "sink", "json", "main")],
)
def test_repeat_wire_changes_version_but_never_resolves_value(
    wire_context: ToolContext, target: str, component: SecretWiringComponentType, plugin: str, target_id: str | None
) -> None:
    args = {"name": _REF, "target": target, "option_key": "api_key"}
    if target_id is not None:
        args["target_id"] = target_id
    resolver = _SyntheticResolver()
    context = replace(wire_context, secret_service=resolver, secret_wiring_policy=_policy(component, plugin))
    first = _call(_state(), args, context)
    second = _call(first.updated_state, args, context)
    assert first.success is second.success is True
    assert first.updated_state.version == 8
    assert second.updated_state.version == 9
    assert second.updated_state.to_dict() == {**first.updated_state.to_dict(), "version": 9}
    assert resolver.calls == [("synthetic-wire-user", _REF), ("synthetic-wire-user", _REF)]


def test_dotted_option_key_is_literal_top_level_assignment(wire_context: ToolContext) -> None:
    state = _state()
    node = replace(state.nodes[0], options={**deep_thaw(state.nodes[0].options), "nested": {"api_key": "untouched"}})
    state = replace(state, nodes=(node,))
    args = {"name": _REF, "target": "node", "target_id": "clean", "option_key": "nested.api_key"}
    result = _call(state, args, replace(wire_context, secret_wiring_policy=_policy("transform", "passthrough", "nested.api_key")))
    assert result.success is True
    options = deep_thaw(result.updated_state.nodes[0].options)
    assert options["nested"] == {"api_key": "untouched"}
    assert options["nested.api_key"] == {"secret_ref": _REF}


@pytest.mark.parametrize("name", ["", "SYNTHETIC_WIRE_REF ", "\uff33YNTHETIC_WIRE_REF"])
def test_reference_name_is_not_normalized(wire_context: ToolContext, name: str) -> None:
    resolver = _SyntheticResolver()
    result = _call(_state(), {"name": name, "target": "source", "option_key": "api_key"}, replace(wire_context, secret_service=resolver))
    assert result.success is False
    assert result.updated_state.version == 7
    assert result.validation.errors[0].error_code == "missing_secret_ref"
    assert resolver.calls == [("synthetic-wire-user", name)]


@pytest.mark.parametrize("extra", [{"target_id": None}, {"name": None}, {"target": "transform"}, {"option_key": 1}, {"extra": True}])
def test_public_schema_refuses_invalid_semantic_shapes_before_resolver(wire_context: ToolContext, extra: dict[str, Any]) -> None:
    resolver = _SyntheticResolver()
    args = {"name": _REF, "target": "source", "option_key": "api_key", **extra}
    with pytest.raises(ToolArgumentError):
        _call(_state(), args, replace(wire_context, secret_service=resolver))
    assert resolver.calls == []
