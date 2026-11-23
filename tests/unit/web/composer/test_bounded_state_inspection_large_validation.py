"""Exact get_pipeline_state reads still work when the graph's validation alone exceeds the page budget.

The synthetic large graph is invalid by construction: each LLM step requires
about 150 fields its upstream does not guarantee, and every such error lists
them. On five nodes the validation block alone is over 32 KB. A page must then
carry a bounded validation summary, so a one-field read still returns its
exact value; the whole-graph read stays refused.
"""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256

import pytest

from elspeth.contracts.errors import FrameworkBugError
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult, normalize_tool_result_validation
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.composer._large_graph import large_graph_state
from tests.unit.web.composer.test_tools import TestGetPipelineState as _StateFixture
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool

BUDGET = 32_768
SUMMARY_MAX_ENTRIES = 3


@pytest.fixture(scope="module")
def catalog() -> CatalogServiceImpl:
    return CatalogServiceImpl(get_shared_plugin_manager())


def _wire_bytes(value: object) -> int:
    return len(json.dumps(json.dumps(value, ensure_ascii=True), ensure_ascii=True).encode())


def _invalid_large_state(catalog: CatalogServiceImpl) -> tuple[CompositionState, ToolResult]:
    state = large_graph_state(5)
    preview = execute_tool("preview_pipeline", {}, state, catalog)
    # Precondition: the validation block alone is over the page budget.
    assert _wire_bytes(preview.to_dict()["validation"]) > BUDGET
    assert len(preview.validation.errors) > SUMMARY_MAX_ENTRIES
    return state, preview


def _assert_bounded_summary(result: ToolResult, preview: ToolResult) -> None:
    wire = result.to_dict()
    assert result.success, wire["validation"]["errors"][:1]
    assert _wire_bytes(wire) <= BUDGET
    # In process the result keeps the complete validation: it is the next mutation's delta base.
    assert result.validation == preview.validation
    shown = wire["validation"]
    assert shown["is_valid"] is False
    assert 0 < len(shown["errors"]) <= SUMMARY_MAX_ENTRIES
    known = [(entry.component, entry.error_code) for entry in preview.validation.errors]
    assert [(entry["component"], entry["error_code"]) for entry in shown["errors"]] == known[: len(shown["errors"])]
    assert all("contract" not in entry for entry in shown["errors"])
    assert shown["warnings"] == shown["suggestions"] == shown["semantic_contracts"] == shown["graph_repair_suggestions"] == []
    (hint,) = wire["post_call_hints"]
    assert f"{len(preview.validation.errors)} errors" in hint
    assert f"{len(preview.validation.warnings)} warnings" in hint
    assert "preview_pipeline" in hint


def test_one_field_read_returns_its_exact_value_under_large_validation(catalog: CatalogServiceImpl) -> None:
    state, preview = _invalid_large_state(catalog)
    result = execute_tool("get_pipeline_state", {"component": "step_000", "field_path": "/node/input"}, state, catalog)
    _assert_bounded_summary(result, preview)
    page = result.to_dict()["data"]
    assert page["view"] == "field"
    assert page["content"] == "conn_000"
    assert page["next_offset_chars"] is None


def test_since_start_read_returns_its_changes_under_large_validation(catalog: CatalogServiceImpl) -> None:
    baseline, preview = _invalid_large_state(catalog)
    node = baseline.nodes[0]
    current = baseline.with_node(replace(node, description="Edited during this request."))
    preview = execute_tool("preview_pipeline", {}, current, catalog)
    result = execute_tool("get_pipeline_state", {"since_start": True}, current, catalog, baseline=baseline, baseline_kind="request_start")
    _assert_bounded_summary(result, preview)
    page = result.to_dict()["data"]
    assert page["view"] == "changes"
    assert page["next_offset_chars"] is None
    changes = json.loads(page["content"])["changes"]
    assert {"operation": "set", "path": ["nodes", node.id, "description"], "value": "Edited during this request."} in changes


def test_offset_continuation_pages_return_the_exact_value_under_large_validation(catalog: CatalogServiceImpl) -> None:
    state, _ = _invalid_large_state(catalog)
    value = 'Exact "quoted" page content 日本語\\\n' * 2500
    node = state.nodes[0]
    state = state.with_node(replace(node, options={**node.options, "prompt_template": value}))
    preview = execute_tool("preview_pipeline", {}, state, catalog)
    args: dict[str, object] = {"component": node.id, "field_path": "/node/options/prompt_template"}
    parts: list[str] = []
    while True:
        result = execute_tool("get_pipeline_state", args, state, catalog)
        _assert_bounded_summary(result, preview)
        page = result.to_dict()["data"]
        assert page["sha256"] == sha256(value.encode()).hexdigest()
        parts.append(page["content"])
        if page["next_offset_chars"] is None:
            break
        args["offset_chars"] = page["next_offset_chars"]
        args["expected_sha256"] = page["sha256"]
    assert len(parts) > 1
    assert "".join(parts) == value


@pytest.mark.parametrize(
    ("component", "selected"),
    [
        ("step_000", lambda data: data["node"]["id"] == "step_000"),
        ("source", lambda data: list(data["sources"]) == ["source"]),
        ("conn_005", lambda data: data["output"]["sink_name"] == "conn_005"),
        ("set_pipeline_arguments", None),
    ],
)
def test_component_only_read_returns_its_structure_under_large_validation(catalog: CatalogServiceImpl, component, selected) -> None:
    state, preview = _invalid_large_state(catalog)
    result = execute_tool("get_pipeline_state", {"component": component}, state, catalog)
    _assert_bounded_summary(result, preview)
    page = result.to_dict()["data"]
    assert page["view"] == "structure"
    assert page["offset_chars"] == 0
    if selected is not None:
        # A single component's structure fits on one page once the summary replaces the full block.
        assert page["next_offset_chars"] is None
        assert selected(json.loads(page["content"]))
    else:
        assert page["content"]


@pytest.mark.parametrize("args", [{}, {"component": "full"}, {"component": "pipeline"}, {"component": ""}])
def test_whole_graph_read_under_large_validation_is_still_refused(catalog: CatalogServiceImpl, args) -> None:
    state, _ = _invalid_large_state(catalog)
    wire = execute_tool("get_pipeline_state", args, state, catalog).to_dict()
    assert not wire["success"]
    assert _wire_bytes(wire) <= BUDGET
    (refusal,) = wire["validation"]["errors"]
    assert refusal["error_code"] == "inspection_response_too_large"
    # The refusal names the reads that still work, in static text only.
    assert "Name a component or pass field_path" in refusal["message"]
    assert "preview_pipeline" in refusal["message"]


def test_a_summarized_page_is_never_revalidated_under_another_snapshot(catalog: CatalogServiceImpl) -> None:
    state, _ = _invalid_large_state(catalog)
    view = PolicyCatalogView.for_trained_operator(catalog, PluginAvailabilitySnapshot.for_trained_operator(catalog))
    read = {"component": "step_000", "field_path": "/node/input"}
    summarized = execute_tool("get_pipeline_state", read, state, catalog)
    assert "post_call_hints" in summarized.to_dict()
    with pytest.raises(FrameworkBugError, match="cannot be revalidated"):
        normalize_tool_result_validation(replace(summarized, _validation_snapshot_hash="0" * 64), view)
    # Control: a page with full validation still revalidates on a snapshot change.
    small = _StateFixture()._build_populated_state()
    fitting = execute_tool("get_pipeline_state", {"component": "t1", "field_path": "/node/input"}, small, catalog)
    assert "post_call_hints" not in fitting.to_dict()
    normalize_tool_result_validation(replace(fitting, _validation_snapshot_hash="0" * 64), view)


def test_a_page_that_fits_with_full_validation_is_unchanged() -> None:
    state = _StateFixture()._build_populated_state()
    result = execute_tool("get_pipeline_state", {"component": "t1", "field_path": "/node/input"}, state, _mock_catalog())
    wire = result.to_dict()
    assert result.success
    assert "post_call_hints" not in wire
    assert wire["validation"]["errors"] == [entry.to_dict() for entry in result.validation.errors]
    assert wire["validation"]["warnings"] == [entry.to_dict() for entry in result.validation.warnings]
