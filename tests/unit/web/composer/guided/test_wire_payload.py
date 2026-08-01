"""Tests for the authoritative arbitrary-DAG Step-4 wire payload."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from elspeth.web.composer.guided.protocol import TurnType, WireStageData, validate_payload
from elspeth.web.composer.state import NodeSpec


def _wire_payload() -> WireStageData:
    return {
        "proposal_id": "00000000-0000-4000-8000-000000000001",
        "draft_hash": "d" * 64,
        "sources": [
            {
                "stable_id": "00000000-0000-4000-8000-000000000002",
                "label": "source-1",
                "plugin": "csv",
                "on_validation_failure": "discard",
                "guaranteed_fields": ["text"],
                "row_cardinality": {
                    "input": "none",
                    "output": "zero_or_many",
                    "expected_output_count": None,
                },
            }
        ],
        "nodes": [],
        "outputs": [
            {
                "stable_id": "00000000-0000-4000-8000-000000000003",
                "label": "output-1",
                "plugin": "json",
                "on_write_failure": "discard",
                "required_fields": ["text"],
                "business_schema": {
                    "mode": "observed",
                    "fields": [],
                    "guaranteed_fields": [],
                    "required_fields": [],
                },
            }
        ],
        "connections": [
            {
                "stable_id": "00000000-0000-4000-8000-000000000004",
                "from_endpoint": {
                    "kind": "source",
                    "stable_id": "00000000-0000-4000-8000-000000000002",
                },
                "to_endpoint": {
                    "kind": "output",
                    "stable_id": "00000000-0000-4000-8000-000000000003",
                },
                "flow": {"kind": "source_success", "branch": None},
                "schema_contract": None,
            }
        ],
        "semantic_contracts": [],
        "warnings": [],
        "blockers": [],
        "can_confirm": True,
    }


def test_wire_stage_data_has_only_the_candidate_derived_contract() -> None:
    payload = _wire_payload()

    assert set(payload) == {
        "proposal_id",
        "draft_hash",
        "sources",
        "nodes",
        "outputs",
        "connections",
        "semantic_contracts",
        "warnings",
        "blockers",
        "can_confirm",
    }
    assert "topology" not in payload
    assert "edge_contracts" not in payload


def test_valid_arbitrary_dag_wire_payload_passes() -> None:
    assert validate_payload(TurnType.CONFIRM_WIRING, _wire_payload()) is None


@pytest.mark.parametrize("removed_field", ("advisor_findings", "signoff_outcome", "passes_remaining"))
def test_wire_payload_rejects_removed_advisor_signoff_fields(removed_field: str) -> None:
    payload = deepcopy(_wire_payload())
    payload[removed_field] = 0 if removed_field == "passes_remaining" else "legacy"  # type: ignore[literal-required]

    error = validate_payload(TurnType.CONFIRM_WIRING, payload)

    assert error is not None
    assert "unexpected" in error


@pytest.mark.parametrize(
    "missing",
    (
        "proposal_id",
        "draft_hash",
        "sources",
        "nodes",
        "outputs",
        "connections",
        "semantic_contracts",
        "warnings",
        "blockers",
        "can_confirm",
    ),
)
def test_each_required_wire_field_is_required(missing: str) -> None:
    payload = deepcopy(_wire_payload())
    del payload[missing]  # type: ignore[misc]

    error = validate_payload(TurnType.CONFIRM_WIRING, payload)

    assert error is not None
    assert missing in error


def test_connection_preserves_stable_endpoints_and_flow() -> None:
    connection = _wire_payload()["connections"][0]

    assert connection["from_endpoint"]["kind"] == "source"
    assert connection["to_endpoint"]["kind"] == "output"
    assert connection["flow"] == {"kind": "source_success", "branch": None}


def test_node_cardinality_closes_validation_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.web.composer.guided.emitters import _node_cardinality
    from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

    tracking = TrackingPluginManager(get_shared_plugin_manager())
    monkeypatch.setattr(
        "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
        lambda: tracking,
    )
    node = SimpleNamespace(node_type="transform", expected_output_count=None)
    executable_node = SimpleNamespace(
        plugin="value_transform",
        options={
            "schema": {"mode": "observed"},
            "operations": [{"target": "copy", "expression": "row['value']"}],
        },
    )

    cardinality = _node_cardinality(node, executable_node)

    assert cardinality["output"] == "one"
    assert len(tracking.instances) == 1
    assert tracking.instances[0].close_count == 1


def test_node_cardinality_shuts_down_concrete_llm_probe_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real resource-owning LLM validation probe releases its executor."""
    from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
    from elspeth.web.composer.guided.emitters import _node_cardinality
    from tests.unit.web.composer._probe_lifecycle_helpers import TrackingPluginManager

    tracking = TrackingPluginManager(get_shared_plugin_manager())
    monkeypatch.setattr(
        "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
        lambda: tracking,
    )
    node = SimpleNamespace(node_type="transform", expected_output_count=None)
    executable_node = SimpleNamespace(
        plugin="llm",
        options={
            "provider": "openrouter",
            "api_key": "probe-key",
            "model": "openai/gpt-4o",
            "prompt_template": "Evaluate: {{ row.text_content }}",
            "schema": {"mode": "observed"},
            "required_input_fields": ["text"],
            "queries": {"quality": {"input_fields": {"text_content": "text"}}},
            "pool_size": 2,
        },
    )

    _node_cardinality(node, executable_node)

    transform = tracking.instances[0]._delegate
    assert transform._query_executor is not None
    assert transform._query_executor._shutdown_event.is_set()


def test_node_cardinality_preserves_inspection_failure_when_close_also_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup failure is secondary to the cardinality inspection failure."""
    from elspeth.web.composer.guided.emitters import _node_cardinality

    class FailingTransform:
        @property
        def creates_tokens(self) -> bool:
            raise ValueError("primary cardinality inspection failure")

        @property
        def can_drop_rows(self) -> bool:
            return False

        def close(self) -> None:
            raise RuntimeError("secondary transform close failure")

    class FailingManager:
        def create_transform(self, _plugin: str, _options: object) -> FailingTransform:
            return FailingTransform()

    monkeypatch.setattr(
        "elspeth.plugins.infrastructure.manager.get_shared_plugin_manager",
        lambda: FailingManager(),
    )
    node = NodeSpec(
        id="probe",
        node_type="transform",
        plugin="probe",
        input="input",
        on_success="output",
        on_error="discard",
        options={},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )

    with pytest.raises(ValueError, match="primary cardinality inspection failure") as exc_info:
        _node_cardinality(node, node)

    assert any("transform.close failed during cardinality inspection: RuntimeError" in note for note in exc_info.value.__notes__)
