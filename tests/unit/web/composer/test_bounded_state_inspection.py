"""Bounded exact reads through get_pipeline_state."""

import json
from dataclasses import replace
from hashlib import sha256

from elspeth.web.composer.discovery_response import admit_discovery_result
from elspeth.web.composer.pipeline_planner import _ParsedToolCall, _resolved_discovery_information_keys
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from tests.unit.web.composer.test_tools import TestGetPipelineState as _StateFixture
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool


def _read(args, state):
    try:
        return execute_tool("get_pipeline_state", args, state, _mock_catalog()).to_dict()
    except ToolArgumentError as exc:
        return dict(arg_error_payload(exc, "get_pipeline_state"))


def _wire_bytes(value: object) -> int:
    return len(json.dumps(json.dumps(value, ensure_ascii=True), ensure_ascii=True).encode())


def test_large_read_returns_structure_and_exact_hash_bound_pages() -> None:
    state = _StateFixture()._build_populated_state()
    value = 'Exact "quoted" content 日本語\\\n' * 3000
    node = state.nodes[0]
    state = state.with_node(replace(node, options={**node.options, "large_note": value}))
    overview = _read({}, state)
    assert overview["success"]
    assert _wire_bytes(overview) <= 32_768
    assert overview["data"]["view"] == "structure"
    assert "large_note" not in overview["data"]["content"]
    assert json.loads(overview["data"]["content"])["nodes"][0]["id"] == node.id
    args: dict[str, object] = {"component": node.id, "field_path": "/node/options/large_note"}
    parts: list[str] = []
    while True:
        result = _read(args, state)
        assert "success" in result, result
        assert result["success"]
        assert _wire_bytes(result) <= 32_768
        page = result["data"]
        assert page["sha256"] == sha256(value.encode()).hexdigest()
        parts.append(page["content"])
        if page["next_offset_chars"] is None:
            break
        args["offset_chars"] = page["next_offset_chars"]
        args["expected_sha256"] = page["sha256"]
    assert "".join(parts) == value
    args["expected_sha256"] = "0" * 64
    assert not execute_tool("get_pipeline_state", args, state, _mock_catalog()).success
    del args["expected_sha256"]
    assert not execute_tool("get_pipeline_state", args, state, _mock_catalog()).success


def test_structure_and_partial_page_do_not_claim_full_coverage() -> None:
    state = _StateFixture()._build_populated_state()
    state = state.with_node(replace(state.nodes[0], options={"large_note": "content" * 6000}))
    for args, expected in (({}, ("pipeline.structure",)), ({"field_path": ""}, ())):
        try:
            result = execute_tool("get_pipeline_state", args, state, _mock_catalog())
        except ToolArgumentError as exc:
            payload = arg_error_payload(exc, "get_pipeline_state")
            assert "success" in payload, payload
            raise AssertionError("unreachable argument-error payload") from exc
        call = _ParsedToolCall("inspection", "get_pipeline_state", json.dumps(args), args)
        assert _resolved_discovery_information_keys(call, admit_discovery_result("get_pipeline_state", result)) == expected


def test_invalid_pointer_and_offset_refuse_without_disclosing_content() -> None:
    state = _StateFixture()._build_populated_state()
    secret_text = "Synthetic inspection-only content " * 2000
    state = state.with_node(replace(state.nodes[0], options={"large_note": secret_text}))
    selected = _read({"component": "t1", "field_path": "/node/options/large_note"}, state)
    assert "success" in selected, selected
    assert selected["success"]
    assert selected["data"]["view"] == "field"
    for args in (
        {"component": "t1", "field_path": "/node/options/missing"},
        {"component": "t1", "field_path": "/node/options/large_note", "offset_chars": 1},
        {
            "component": "t1",
            "field_path": "/node/options/large_note",
            "offset_chars": 999999,
            "expected_sha256": sha256(secret_text.encode()).hexdigest(),
        },
    ):
        result = _read(args, state)
        assert "success" in result, result
        assert not result["success"]
        assert _wire_bytes(result) <= 32_768
        assert secret_text not in json.dumps(result)
        assert result["validation"]["errors"][0]["error_code"] == "argument_error"


def test_large_validation_diagnostics_are_refused_with_a_bounded_envelope() -> None:
    state = _StateFixture()._build_populated_state()
    nodes = tuple(replace(state.nodes[0], id=f"node_{index}") for index in range(400))
    state = replace(state, nodes=nodes)
    result = _read({}, state)
    assert not result["success"]
    assert _wire_bytes(result) <= 32_768
    refusal = result["validation"]["errors"][0]
    assert refusal["message"].startswith("Validation diagnostics exceed the inspection response budget.")
    assert refusal["error_code"] == "inspection_response_too_large"
    preview = execute_tool("preview_pipeline", {}, state, _mock_catalog()).to_dict()
    assert preview["success"]
    assert _wire_bytes(preview) > 32_768
    assert preview["validation"]["errors"]


def test_oversized_early_refusal_falls_back_to_the_response_size_code() -> None:
    state = _StateFixture()._build_populated_state()
    result = _read({"component": "x" * 40_000}, state)
    assert "success" in result, result
    assert not result["success"]
    assert _wire_bytes(result) <= 32_768
    assert "x" * 40_000 not in json.dumps(result)
    (refusal,) = result["validation"]["errors"]
    assert refusal["message"].startswith("Inspection diagnostics exceed the response budget.")
    assert refusal["error_code"] == "inspection_response_too_large"
