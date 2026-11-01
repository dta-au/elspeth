"""Original schema-narrowing controls exercise the actual common-loop gate.

Graph isomorphism cannot detect an advertised schema that a scripted response
ignores. Keep the independent canonical-schema comparison upstream of provider,
proposal and commit, including enum/limit narrowing that field-name parity alone
cannot catch. No production cap is introduced by these adversarial mutations.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest

from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import stable_hash
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.capability_skill import CAPABILITY_CORE_NODE_GUIDANCE
from elspeth.web.composer.tools.schema_contract import canonical_set_pipeline_schema
from elspeth.web.composer.tools.wire_projection import wire_tool_definitions
from tests.helpers.continuation_app import continuation_app, install_sdk, submit_and_settle
from tests.integration.web.composer.parity.conftest import PARITY_FIXTURES

_SCHEMA_IDENTITY_GATE_MESSAGE = "Compose loop advertised pipeline schema differs from canonical"


def _node_props(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Return the advertised ``nodes[].properties`` map (the node item schema)."""
    return pipeline["properties"]["nodes"]["items"]["properties"]


def _remove_fork_to(pipeline: dict[str, Any]) -> None:
    """Drop ``nodes[].fork_to`` — narrows away every fork-based topology."""
    props = _node_props(pipeline)
    assert "fork_to" in props, "control precondition: advertised nodes[].fork_to must exist to be narrowed away"
    del props["fork_to"]


def _remove_merge(pipeline: dict[str, Any]) -> None:
    """Drop ``nodes[].merge`` — narrows away the coalesce merge strategy."""
    props = _node_props(pipeline)
    assert "merge" in props, "control precondition: advertised nodes[].merge must exist to be narrowed away"
    del props["merge"]


def _enum_node_type_omitting_queue(pipeline: dict[str, Any]) -> None:
    """Close ``nodes[].node_type`` to an enum that omits ``queue``.

    The canonical schema advertises ``node_type`` as an open ``{"type": "string"}``
    — there is no existing enum to shrink, so introducing one is itself the
    structural narrowing that changes the schema hash. The enum lists every real
    node family (``CAPABILITY_CORE_NODE_GUIDANCE``) except ``queue``, so a queue
    node is no longer advertised as authorable.
    """
    node_type = _node_props(pipeline)["node_type"]
    assert "enum" not in node_type, (
        "control precondition: advertised nodes[].node_type must be an open string (no enum) "
        "so that introducing one is a genuine structural narrowing"
    )
    assert "queue" in CAPABILITY_CORE_NODE_GUIDANCE, "queue must be a real node family for its omission to narrow"
    node_type["enum"] = [family for family in sorted(CAPABILITY_CORE_NODE_GUIDANCE) if family != "queue"]


def _cap_named_sources_at_one(pipeline: dict[str, Any]) -> None:
    """Cap the ``sources`` object at one entry — narrows away multi-source."""
    sources = pipeline["properties"]["sources"]
    assert "maxProperties" not in sources, "control precondition: advertised sources must be uncapped to be narrowed"
    sources["maxProperties"] = 1


@dataclass(frozen=True)
class _NarrowingControl:
    """One named narrowing of the advertised terminal ``pipeline`` schema."""

    field: str
    narrow: Callable[[dict[str, Any]], None]


_CONTROLS: tuple[_NarrowingControl, ...] = (
    _NarrowingControl("nodes[].fork_to", _remove_fork_to),
    _NarrowingControl("nodes[].merge", _remove_merge),
    _NarrowingControl("nodes[].node_type[enum-omit-queue]", _enum_node_type_omitting_queue),
    _NarrowingControl("sources[maxProperties=1]", _cap_named_sources_at_one),
)


def _fixture(class_name: str) -> dict[str, Any]:
    return copy.deepcopy(next(fixture for fixture in PARITY_FIXTURES if fixture["class"] == class_name))


def _advertised_pipeline(tools):
    function = next(tool["function"] for tool in tools if tool["function"]["name"] == "set_pipeline")
    return function["parameters"]["properties"]["pipeline"]


def _build_loop_tools(tools, monkeypatch):
    monkeypatch.setattr(provider_gateway, "wire_tool_definitions", lambda dialect: copy.deepcopy(tools))
    return provider_gateway.composer_loop_tool_definitions(ToolContractDialect.NONE)


class TestManifestSchemaIdentityGate:
    """Preserve the original native IDs while moving their guard to the loop."""

    def test_baseline_un_narrowed_schema_matches(self, monkeypatch):
        tools = wire_tool_definitions(ToolContractDialect.NONE)
        advertised = _advertised_pipeline(tools)
        assert stable_hash(advertised) == stable_hash(canonical_set_pipeline_schema())
        actual = _build_loop_tools(tools, monkeypatch)
        assert stable_hash(_advertised_pipeline(actual)) == stable_hash(advertised)

    @pytest.mark.parametrize("control", _CONTROLS, ids=lambda control: control.field)
    def test_narrowing_trips_schema_identity_gate(self, control, monkeypatch):
        tools = wire_tool_definitions(ToolContractDialect.NONE)
        advertised = _advertised_pipeline(tools)
        control.narrow(advertised)
        assert stable_hash(advertised) != stable_hash(canonical_set_pipeline_schema()), control.field
        with pytest.raises(AuditIntegrityError) as caught:
            _build_loop_tools(tools, monkeypatch)
        assert str(caught.value) == _SCHEMA_IDENTITY_GATE_MESSAGE


@pytest.mark.asyncio
async def test_freeform_drive_narrowed_advertised_schema_trips_gate_upstream_of_graph(tmp_path, monkeypatch):
    """A narrowed advertised schema refuses the Continuation request before SDK entry.

    Drives the production message route, worker and Continuation driver
    (tests/helpers/continuation_app.py). Continuation builds the loop palette
    per request (continuation_composition_initialization), so the identity
    gate fires there, upstream of any provider call, graph or proposal.
    """
    sdk_calls = []

    async def forbidden_sdk(**kwargs):
        sdk_calls.append(kwargs)
        raise AssertionError("Narrowed advertised schema must refuse before SDK entry")

    install_sdk(monkeypatch, forbidden_sdk)
    fixture = _fixture("fork_coalesce")
    async with continuation_app(tmp_path, monkeypatch) as harness:
        sessions = harness.app.state.session_service
        await sessions.update_composer_preferences(harness.session_id, trust_mode="explicit_approve", density_default="high", actor="test")
        original = provider_gateway.wire_tool_definitions

        def narrowed(dialect):
            tools = original(dialect)
            _remove_fork_to(_advertised_pipeline(tools))
            return tools

        monkeypatch.setattr(provider_gateway, "wire_tool_definitions", narrowed)
        with pytest.raises(AuditIntegrityError) as caught:
            provider_gateway.composer_loop_tool_definitions(harness.app.state.composer_service._planner_dialect)
        assert str(caught.value) == _SCHEMA_IDENTITY_GATE_MESSAGE
        snapshot = await submit_and_settle(harness, {"content": fixture["intent"], "operation_id": str(uuid4()), "state_id": None})
        assert snapshot.status == "failed" and snapshot.error is not None, snapshot
        assert snapshot.error.body["error_type"] == "audit_integrity_error"
        assert sdk_calls == []
        assert await sessions.get_current_state(harness.session_id) is None
        assert await sessions.list_composition_proposals(harness.session_id, status="pending") == []
