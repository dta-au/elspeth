"""Direct contract tests for execution-validation diagnostics."""

from __future__ import annotations

import pytest

from elspeth.contracts.data import CompatibilityResult
from elspeth.core.dag.models import EdgeContractError, GraphValidationWarning
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.manager import PluginNotFoundError
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.execution import _validation_authoring as authoring
from elspeth.web.execution import _validation_diagnostics as diagnostics
from elspeth.web.execution import validation as validation_facade


def _edge_error() -> EdgeContractError:
    return EdgeContractError(
        "incompatible",
        from_node_id="producer",
        to_node_id="consumer",
        producer_schema_name="ProducerSchema",
        consumer_schema_name="ConsumerSchema",
        compatibility_result=CompatibilityResult(
            compatible=False,
            type_mismatches=(("value", "str", "int"),),
        ),
        component_type="transform",
    )


def test_facade_diagnostic_exports_are_the_direct_implementations() -> None:
    """Legacy imports and patch targets must keep resolving by identity."""
    exported_names = (
        "_edge_patch_target_for_node_id",
        "_find_identity_node_advisories",
        "_graph_warning_to_validation_warning",
        "_infer_component_type_from_plugin_error",
    )

    for name in exported_names:
        assert getattr(validation_facade, name) is getattr(diagnostics, name)
    assert validation_facade._collect_secret_refs is authoring._collect_secret_refs


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            PluginConfigError(
                "Invalid CSV config",
                cause="missing path",
                plugin_class="CsvSourceConfig",
                component_type="source",
            ),
            "source",
        ),
        (PluginConfigError("Generic config error"), None),
        (PluginNotFoundError("No plugin named 'missing'"), None),
    ],
)
def test_plugin_error_component_type_compatibility(error: PluginConfigError | PluginNotFoundError, expected: str | None) -> None:
    assert validation_facade._infer_component_type_from_plugin_error(error) == expected


def test_facade_edge_suggestion_preserves_live_patch_target(monkeypatch: pytest.MonkeyPatch) -> None:
    patched_target = diagnostics._EdgePatchTarget(
        component_id="patched",
        component_type="transform",
        display_name="patched component",
        schema_patch_tool_call="patched_tool()",
    )
    monkeypatch.setattr(validation_facade, "_edge_patch_target_for_node_id", lambda *args, **kwargs: patched_target)

    suggestion = validation_facade._build_edge_contract_suggestion(_edge_error())

    assert "patched_tool()" in suggestion


def test_facade_edge_formatter_preserves_live_suggestion_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(validation_facade, "_build_edge_contract_suggestion", lambda *args, **kwargs: "patched suggestion")

    _message, suggestion = validation_facade._format_edge_contract_failure(_edge_error())

    assert suggestion == "patched suggestion"


def test_direct_secret_collection_preserves_depth_first_order_and_scope() -> None:
    payload = {
        "source": {"secret_ref": "SOURCE_TOKEN", "secret_scope": "server"},
        "nodes": [
            {"token": {"secret_ref": "NODE_TOKEN"}},
            {"nested": ["${ENV_TOKEN}"]},
        ],
    }

    assert authoring._collect_secret_refs(payload, {"ENV_TOKEN"}) == [
        ("SOURCE_TOKEN", "server"),
        ("NODE_TOKEN", None),
        ("ENV_TOKEN", None),
    ]


def test_direct_graph_warning_conversion_preserves_first_node_attribution() -> None:
    result = diagnostics._graph_warning_to_validation_warning(
        GraphValidationWarning(code="graph.warning", message="review this edge", node_ids=("producer", "consumer"))
    )

    assert result.component_id == "producer"
    assert result.component_type == "graph"
    assert result.message == "review this edge"
    assert result.warning_code == "graph.warning"


def test_direct_identity_advisory_handles_empty_state() -> None:
    state = CompositionState(
        source=None,
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )

    assert diagnostics._find_identity_node_advisories(state) == []
