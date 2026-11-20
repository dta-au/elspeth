"""Cumulative inspection through the shipped tool surface."""

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from elspeth.web.composer.pipeline_planner import _tool_information_keys
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from tests.unit.web.composer.test_tools import TestGetPipelineState as _StateFixture
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool


def _read(args, state, **kwargs):
    try:
        return execute_tool("get_pipeline_state", args, state, _mock_catalog(), **kwargs).to_dict()
    except ToolArgumentError as exc:
        return dict(arg_error_payload(exc, "get_pipeline_state"))


def test_changes_are_identity_keyed_and_accumulate_from_turn_start() -> None:
    baseline = _StateFixture()._build_populated_state()
    node = baseline.nodes[0]
    current = baseline.with_node(replace(node, input="first"))
    current = current.with_node(replace(current.nodes[0], input="second"))
    result = _read({"since_start": True}, current, baseline=baseline, baseline_kind="request_start")
    assert "success" in result, result
    assert result["success"]
    assert result["data"]["baseline_kind"] == "request_start"
    assert result["data"]["from_version"] == baseline.version
    assert {"operation": "set", "path": ["nodes", node.id, "input"], "value": "second"} in result["data"]["changes"]
    assert _tool_information_keys("get_pipeline_state", {"since_start": True}) == ("pipeline.diff",)


def test_changes_refuse_missing_baseline_and_duplicate_ids() -> None:
    baseline = _StateFixture()._build_populated_state()
    missing = _read({"since_start": True}, baseline)
    assert "success" in missing, missing
    assert not missing["success"]
    assert missing["validation"]["errors"][0]["error_code"] == "diff_baseline_unavailable"
    duplicate = replace(baseline, nodes=(*baseline.nodes, baseline.nodes[0]))
    result = _read({"since_start": True}, duplicate, baseline=baseline, baseline_kind="request_start")
    assert not result["success"]
    assert result["validation"]["errors"][0]["error_code"] == "duplicate_component_identity"
    baseline_duplicate = _read({"since_start": True}, baseline, baseline=duplicate, baseline_kind="request_start")
    assert not baseline_duplicate["success"]
    assert baseline_duplicate["validation"]["errors"][0]["error_code"] == "duplicate_component_identity"


@pytest.mark.parametrize("carrier", ["path", "file"])
def test_changes_redact_blob_backed_source_storage(carrier: str) -> None:
    baseline = _StateFixture()._build_populated_state()
    name, source = next(iter(baseline.sources.items()))
    storage = "/private/review-storage/input.csv"
    current = replace(
        baseline,
        sources={name: replace(source, options={**source.options, "blob_ref": "11111111-1111-4111-8111-111111111111", carrier: storage})},
    )
    result = _read({"since_start": True}, current, baseline=_empty_state(), baseline_kind="request_start")
    assert "success" in result, result
    assert result["success"], result
    assert storage not in json.dumps(result)
    source_changes = [change for change in result["data"]["changes"] if change["path"][0] == "sources"]
    assert source_changes
    assert storage not in json.dumps(source_changes)


def test_changes_reconstruct_identity_order_removals_nulls_and_boolean() -> None:
    baseline = _StateFixture()._build_populated_state()
    baseline = baseline.with_node(replace(baseline.nodes[0], options={"probe": {"flag": 1, "remove": "old", "items": ["a", "b", "c"]}}))
    added = replace(baseline.nodes[0], id="inserted", input="rows", options={})
    changed = replace(baseline.nodes[0], options={"probe": {"flag": True, "nullable": None, "items": ["a", "updated"]}})
    current = replace(baseline, nodes=(added, changed), outputs=tuple(reversed(baseline.outputs)), version=baseline.version + 1)

    def projection(state):
        result = execute_tool("get_pipeline_state", {}, state, _mock_catalog()).to_dict()
        assert "success" in result, result
        assert result["success"], result
        payload = result["data"]
        del payload["inspection"]
        for collection, identity in (("nodes", "id"), ("edges", "id"), ("outputs", "sink_name")):
            items = payload[collection]
            payload[collection + "_order"] = [item[identity] for item in items]
            payload[collection] = {item[identity]: item for item in items}
        return payload

    result = _read({"since_start": True}, current, baseline=baseline, baseline_kind="request_start")
    assert "success" in result, result
    assert result["success"], result
    changes = result["data"]["changes"]
    patched = deepcopy(projection(baseline))
    for change in changes:
        parent = patched
        for part in change["path"][:-1]:
            parent = parent[int(part)] if isinstance(parent, list) else parent[part]
        key = int(change["path"][-1]) if isinstance(parent, list) else change["path"][-1]
        if change["operation"] == "remove":
            del parent[key]
        elif isinstance(parent, list) and key == len(parent):
            parent.append(change["value"])
        else:
            parent[key] = change["value"]
    assert patched == projection(current)
    assert next(change["value"] for change in changes if change["path"] == ["nodes", "t1", "options", "probe", "flag"]) is True
