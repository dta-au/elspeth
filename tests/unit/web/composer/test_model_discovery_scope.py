"""The list_models loop tool reports scope and completeness for each request."""

from __future__ import annotations

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import execute_tool, generation
from elspeth.web.composer.tools._common import ToolContext


@pytest.fixture
def model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        generation,
        "read_litellm_model_list",
        lambda: ["anthropic/model-a", "anthropic/model-b", "openai/model-c", "prefixless", "openrouter/anthropic/bundled"],
    )
    monkeypatch.setattr(generation, "get_catalog_values", lambda _catalog: frozenset({"anthropic/live"}))


@pytest.mark.usefixtures("model_catalog")
@pytest.mark.parametrize(
    "arguments, expected_count",
    [
        pytest.param({"provider": "anthropic", "limit": 2}, 2, id="complete-at-limit"),
        pytest.param({"provider": "anthropic", "limit": 1}, 2, id="truncated"),
        pytest.param({"provider": "openai", "limit": 1}, 1, id="other-provider"),
        pytest.param({"provider": "anthropic/model-a", "limit": 1}, 1, id="complete-subset"),
        pytest.param({"provider": "anthropic", "limit": 10}, 2, id="limit-above-count"),
        pytest.param({"limit": 10}, None, id="summary-ignores-limit"),
        pytest.param({"provider": "openrouter/", "limit": 10}, 1, id="live-alias"),
        pytest.param({"provider": "openrouter/anthropic", "limit": 10}, 1, id="live-versus-bundled-prefix"),
        pytest.param({"provider": "openrouter", "limit": 10}, 1, id="bare-live-prefix"),
        pytest.param({"provider": "", "limit": 10}, 1, id="empty-provider-selects-prefixless"),
    ],
)
def test_list_models_reports_scope_and_completeness(
    tool_context: ToolContext,
    arguments: dict,
    expected_count: int | None,
) -> None:
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    result = execute_tool(
        "list_models",
        arguments,
        state,
        tool_context.catalog,
        plugin_snapshot=tool_context.plugin_snapshot,
    )

    assert result.success is True
    data = deep_thaw(result.data)
    if expected_count is None:
        assert data["providers"] == {"anthropic": 2, "openai": 1, "": 1, "openrouter": 1}
        assert data["total_models"] == 5
    else:
        assert data["count"] == expected_count
        limit = arguments["limit"] if "limit" in arguments else 50
        assert len(data["models"]) == min(limit, expected_count)
        assert data["truncated"] is (expected_count > limit)
