"""State response shapes captured from immutable a6c58c68 producers."""

import importlib.util
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType

import pytest
from pydantic import BaseModel, JsonValue

from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState, EdgeSpec, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool


def state_cases():
    source = SourceSpec(
        "csv", "rows", {"path": "input.csv", "schema": {"mode": "observed"}, "custom": [None, True, 1, 1.5, "é"]}, "discard", "Read source"
    )
    node = NodeSpec(
        "t1", "transform", "passthrough", "rows", "out", "discard", {"schema": {"mode": "observed"}}, None, None, None, None, None, None
    )
    aggregation = NodeSpec(
        "batch",
        "aggregation",
        "batch_stats",
        "rows",
        "out",
        "discard",
        {},
        None,
        None,
        None,
        None,
        None,
        None,
        trigger={"count": 2, "timeout_seconds": 1.5, "condition": None},
        output_mode="transform",
        expected_output_count=1,
    )
    output = OutputSpec("out", "json", {"path": "result.json", "schema": {"mode": "observed"}}, "discard", None)
    state = CompositionState(
        sources={"source": source},
        nodes=(node, aggregation),
        edges=(EdgeSpec("e1", "source", "t1", "on_success", None),),
        outputs=(output,),
        metadata=PipelineMetadata(name="Example é", description=None),
        version=7,
    )
    empty = CompositionState(sources={}, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    named = replace(state, sources={"first": source, "second": replace(source, description=None)})
    blob = replace(
        state,
        sources={
            "source": replace(
                source,
                options={
                    "path": "/internal/blobs/session/data.csv",
                    "blob_ref": "blob-123",
                    "source_authoring": {"origin": "uploaded"},
                    "schema": {"mode": "observed"},
                },
            )
        },
    )
    cases = {
        "empty_full": (empty, {}),
        "empty_sources": (empty, {"component": "source"}),
        "empty_authoring": (empty, {"component": "set_pipeline_arguments"}),
        "full": (state, {}),
        "full_alias": (state, {"component": " ALL "}),
        "sources": (state, {"component": "source"}),
        "node": (state, {"component": "t1"}),
        "aggregation": (state, {"component": "batch"}),
        "output": (state, {"component": "out"}),
        "authoring": (state, {"component": "set_pipeline_arguments"}),
        "named_full": (named, {}),
        "named_authoring": (named, {"component": "set_pipeline_arguments"}),
        "blob_full": (blob, {}),
        "blob_authoring": (blob, {"component": "set_pipeline_arguments"}),
        "node_alias_collision": (replace(state, nodes=(replace(node, id="full"),)), {"component": "full"}),
        "output_alias_collision": (replace(state, outputs=(replace(output, name="full"),)), {"component": "full"}),
    }
    return cases


LEGACY_WIRES = {
    "empty_full": '{"sources": {}, "nodes": [], "outputs": [], "edges": [], "metadata": {"name": "Untitled '
    'Pipeline", "description": ""}, "inspection": {"requested_component": null, '
    '"resolved_component": "full", "accepted_full_state_aliases": ["", "full", "all", '
    '"pipeline"]}}',
    "empty_sources": '{"sources": {}}',
    "empty_authoring": '{"nodes": [], "edges": [], "outputs": [], "metadata": {"name": "Untitled Pipeline", '
    '"description": ""}, "sources": {}}',
    "full": '{"sources": {"source": {"plugin": "csv", "on_success": "rows", "options": {"path": "input.csv", '
    '"schema": {"mode": "observed"}, "custom": [null, true, 1, 1.5, "\\u00e9"]}, '
    '"on_validation_failure": "discard", "description": "Read source"}}, "nodes": [{"id": "t1", '
    '"node_type": "transform", "plugin": "passthrough", "input": "rows", "on_success": "out", '
    '"on_error": "discard", "options": {"schema": {"mode": "observed"}}, "condition": null, "routes": '
    'null, "fork_to": null, "branches": null, "policy": null, "merge": null, "trigger": null, '
    '"output_mode": null, "expected_output_count": null, "timeout_seconds": null, "description": null, '
    '"scope_name": null, "scope_opener": null, "scope_policy": null}, {"id": "batch", "node_type": '
    '"aggregation", "plugin": "batch_stats", "input": "rows", "on_success": "out", "on_error": '
    '"discard", "options": {}, "condition": null, "routes": null, "fork_to": null, "branches": null, '
    '"policy": null, "merge": null, "trigger": {"count": 2, "timeout_seconds": 1.5, "condition": null}, '
    '"output_mode": "transform", "expected_output_count": 1, "timeout_seconds": null, "description": '
    'null, "scope_name": null, "scope_opener": null, "scope_policy": null}], "outputs": [{"sink_name": '
    '"out", "plugin": "json", "options": {"path": "result.json", "schema": {"mode": "observed"}}, '
    '"on_write_failure": "discard", "description": null}], "edges": [{"id": "e1", "from_node": '
    '"source", "to_node": "t1", "edge_type": "on_success", "label": null}], "metadata": {"name": '
    '"Example \\u00e9", "description": null}, "inspection": {"requested_component": null, '
    '"resolved_component": "full", "accepted_full_state_aliases": ["", "full", "all", "pipeline"]}}',
    "full_alias": '{"sources": {"source": {"plugin": "csv", "on_success": "rows", "options": {"path": '
    '"input.csv", "schema": {"mode": "observed"}, "custom": [null, true, 1, 1.5, "\\u00e9"]}, '
    '"on_validation_failure": "discard", "description": "Read source"}}, "nodes": [{"id": "t1", '
    '"node_type": "transform", "plugin": "passthrough", "input": "rows", "on_success": "out", '
    '"on_error": "discard", "options": {"schema": {"mode": "observed"}}, "condition": null, '
    '"routes": null, "fork_to": null, "branches": null, "policy": null, "merge": null, "trigger": '
    'null, "output_mode": null, "expected_output_count": null, "timeout_seconds": null, '
    '"description": null, "scope_name": null, "scope_opener": null, "scope_policy": null}, {"id": '
    '"batch", "node_type": "aggregation", "plugin": "batch_stats", "input": "rows", "on_success": '
    '"out", "on_error": "discard", "options": {}, "condition": null, "routes": null, "fork_to": '
    'null, "branches": null, "policy": null, "merge": null, "trigger": {"count": 2, '
    '"timeout_seconds": 1.5, "condition": null}, "output_mode": "transform", '
    '"expected_output_count": 1, "timeout_seconds": null, "description": null, "scope_name": '
    'null, "scope_opener": null, "scope_policy": null}], "outputs": [{"sink_name": "out", '
    '"plugin": "json", "options": {"path": "result.json", "schema": {"mode": "observed"}}, '
    '"on_write_failure": "discard", "description": null}], "edges": [{"id": "e1", "from_node": '
    '"source", "to_node": "t1", "edge_type": "on_success", "label": null}], "metadata": {"name": '
    '"Example \\u00e9", "description": null}, "inspection": {"requested_component": " ALL ", '
    '"resolved_component": "full", "accepted_full_state_aliases": ["", "full", "all", '
    '"pipeline"]}}',
    "sources": '{"sources": {"source": {"plugin": "csv", "on_success": "rows", "options": {"path": "input.csv", '
    '"schema": {"mode": "observed"}, "custom": [null, true, 1, 1.5, "\\u00e9"]}, '
    '"on_validation_failure": "discard", "description": "Read source"}}}',
    "node": '{"node": {"id": "t1", "node_type": "transform", "plugin": "passthrough", "input": "rows", '
    '"on_success": "out", "on_error": "discard", "options": {"schema": {"mode": "observed"}}, '
    '"condition": null, "routes": null, "fork_to": null, "branches": null, "policy": null, "merge": '
    'null, "trigger": null, "output_mode": null, "expected_output_count": null, "timeout_seconds": '
    'null, "description": null, "scope_name": null, "scope_opener": null, "scope_policy": null}}',
    "aggregation": '{"node": {"id": "batch", "node_type": "aggregation", "plugin": "batch_stats", "input": '
    '"rows", "on_success": "out", "on_error": "discard", "options": {}, "condition": null, '
    '"routes": null, "fork_to": null, "branches": null, "policy": null, "merge": null, '
    '"trigger": {"count": 2, "timeout_seconds": 1.5, "condition": null}, "output_mode": '
    '"transform", "expected_output_count": 1, "timeout_seconds": null, "description": null, '
    '"scope_name": null, "scope_opener": null, "scope_policy": null}}',
    "output": '{"output": {"sink_name": "out", "plugin": "json", "options": {"path": "result.json", "schema": '
    '{"mode": "observed"}}, "on_write_failure": "discard", "description": null}}',
    "authoring": '{"nodes": [{"id": "t1", "node_type": "transform", "plugin": "passthrough", "input": "rows", '
    '"on_success": "out", "on_error": "discard", "options": {"schema": {"mode": "observed"}}, '
    '"condition": null, "routes": null, "fork_to": null, "branches": null, "policy": null, '
    '"merge": null, "trigger": null, "output_mode": null, "expected_output_count": null, '
    '"timeout_seconds": null, "description": null, "scope_name": null, "scope_opener": null, '
    '"scope_policy": null}, {"id": "batch", "node_type": "aggregation", "plugin": "batch_stats", '
    '"input": "rows", "on_success": "out", "on_error": "discard", "options": {}, "condition": '
    'null, "routes": null, "fork_to": null, "branches": null, "policy": null, "merge": null, '
    '"trigger": {"count": 2, "timeout_seconds": 1.5, "condition": null}, "output_mode": '
    '"transform", "expected_output_count": 1, "timeout_seconds": null, "description": null, '
    '"scope_name": null, "scope_opener": null, "scope_policy": null}], "edges": [{"id": "e1", '
    '"from_node": "source", "to_node": "t1", "edge_type": "on_success", "label": null}], '
    '"outputs": [{"sink_name": "out", "plugin": "json", "options": {"path": "result.json", '
    '"schema": {"mode": "observed"}}, "on_write_failure": "discard", "description": null}], '
    '"metadata": {"name": "Example \\u00e9", "description": null}, "source": {"plugin": "csv", '
    '"on_success": "rows", "options": {"path": "input.csv", "schema": {"mode": "observed"}, '
    '"custom": [null, true, 1, 1.5, "\\u00e9"]}, "on_validation_failure": "discard", '
    '"description": "Read source"}}',
    "named_full": '{"sources": {"first": {"plugin": "csv", "on_success": "rows", "options": {"path": '
    '"input.csv", "schema": {"mode": "observed"}, "custom": [null, true, 1, 1.5, "\\u00e9"]}, '
    '"on_validation_failure": "discard", "description": "Read source"}, "second": {"plugin": '
    '"csv", "on_success": "rows", "options": {"path": "input.csv", "schema": {"mode": '
    '"observed"}, "custom": [null, true, 1, 1.5, "\\u00e9"]}, "on_validation_failure": "discard", '
    '"description": null}}, "nodes": [{"id": "t1", "node_type": "transform", "plugin": '
    '"passthrough", "input": "rows", "on_success": "out", "on_error": "discard", "options": '
    '{"schema": {"mode": "observed"}}, "condition": null, "routes": null, "fork_to": null, '
    '"branches": null, "policy": null, "merge": null, "trigger": null, "output_mode": null, '
    '"expected_output_count": null, "timeout_seconds": null, "description": null, "scope_name": '
    'null, "scope_opener": null, "scope_policy": null}, {"id": "batch", "node_type": '
    '"aggregation", "plugin": "batch_stats", "input": "rows", "on_success": "out", "on_error": '
    '"discard", "options": {}, "condition": null, "routes": null, "fork_to": null, "branches": '
    'null, "policy": null, "merge": null, "trigger": {"count": 2, "timeout_seconds": 1.5, '
    '"condition": null}, "output_mode": "transform", "expected_output_count": 1, '
    '"timeout_seconds": null, "description": null, "scope_name": null, "scope_opener": null, '
    '"scope_policy": null}], "outputs": [{"sink_name": "out", "plugin": "json", "options": '
    '{"path": "result.json", "schema": {"mode": "observed"}}, "on_write_failure": "discard", '
    '"description": null}], "edges": [{"id": "e1", "from_node": "source", "to_node": "t1", '
    '"edge_type": "on_success", "label": null}], "metadata": {"name": "Example \\u00e9", '
    '"description": null}, "inspection": {"requested_component": null, "resolved_component": '
    '"full", "accepted_full_state_aliases": ["", "full", "all", "pipeline"]}}',
    "named_authoring": '{"nodes": [{"id": "t1", "node_type": "transform", "plugin": "passthrough", "input": '
    '"rows", "on_success": "out", "on_error": "discard", "options": {"schema": {"mode": '
    '"observed"}}, "condition": null, "routes": null, "fork_to": null, "branches": null, '
    '"policy": null, "merge": null, "trigger": null, "output_mode": null, '
    '"expected_output_count": null, "timeout_seconds": null, "description": null, '
    '"scope_name": null, "scope_opener": null, "scope_policy": null}, {"id": "batch", '
    '"node_type": "aggregation", "plugin": "batch_stats", "input": "rows", "on_success": '
    '"out", "on_error": "discard", "options": {}, "condition": null, "routes": null, '
    '"fork_to": null, "branches": null, "policy": null, "merge": null, "trigger": {"count": '
    '2, "timeout_seconds": 1.5, "condition": null}, "output_mode": "transform", '
    '"expected_output_count": 1, "timeout_seconds": null, "description": null, "scope_name": '
    'null, "scope_opener": null, "scope_policy": null}], "edges": [{"id": "e1", "from_node": '
    '"source", "to_node": "t1", "edge_type": "on_success", "label": null}], "outputs": '
    '[{"sink_name": "out", "plugin": "json", "options": {"path": "result.json", "schema": '
    '{"mode": "observed"}}, "on_write_failure": "discard", "description": null}], '
    '"metadata": {"name": "Example \\u00e9", "description": null}, "sources": {"first": '
    '{"plugin": "csv", "on_success": "rows", "options": {"path": "input.csv", "schema": '
    '{"mode": "observed"}, "custom": [null, true, 1, 1.5, "\\u00e9"]}, '
    '"on_validation_failure": "discard", "description": "Read source"}, "second": {"plugin": '
    '"csv", "on_success": "rows", "options": {"path": "input.csv", "schema": {"mode": '
    '"observed"}, "custom": [null, true, 1, 1.5, "\\u00e9"]}, "on_validation_failure": '
    '"discard", "description": null}}}',
    "blob_full": '{"sources": {"source": {"plugin": "csv", "on_success": "rows", "options": {"path": '
    '"<redacted-blob-source-path>", "blob_ref": "blob-123", "source_authoring": {"origin": '
    '"uploaded"}, "schema": {"mode": "observed"}}, "on_validation_failure": "discard", '
    '"description": "Read source"}}, "nodes": [{"id": "t1", "node_type": "transform", "plugin": '
    '"passthrough", "input": "rows", "on_success": "out", "on_error": "discard", "options": '
    '{"schema": {"mode": "observed"}}, "condition": null, "routes": null, "fork_to": null, '
    '"branches": null, "policy": null, "merge": null, "trigger": null, "output_mode": null, '
    '"expected_output_count": null, "timeout_seconds": null, "description": null, "scope_name": '
    'null, "scope_opener": null, "scope_policy": null}, {"id": "batch", "node_type": '
    '"aggregation", "plugin": "batch_stats", "input": "rows", "on_success": "out", "on_error": '
    '"discard", "options": {}, "condition": null, "routes": null, "fork_to": null, "branches": '
    'null, "policy": null, "merge": null, "trigger": {"count": 2, "timeout_seconds": 1.5, '
    '"condition": null}, "output_mode": "transform", "expected_output_count": 1, '
    '"timeout_seconds": null, "description": null, "scope_name": null, "scope_opener": null, '
    '"scope_policy": null}], "outputs": [{"sink_name": "out", "plugin": "json", "options": '
    '{"path": "result.json", "schema": {"mode": "observed"}}, "on_write_failure": "discard", '
    '"description": null}], "edges": [{"id": "e1", "from_node": "source", "to_node": "t1", '
    '"edge_type": "on_success", "label": null}], "metadata": {"name": "Example \\u00e9", '
    '"description": null}, "inspection": {"requested_component": null, "resolved_component": '
    '"full", "accepted_full_state_aliases": ["", "full", "all", "pipeline"]}}',
    "blob_authoring": '{"nodes": [{"id": "t1", "node_type": "transform", "plugin": "passthrough", "input": '
    '"rows", "on_success": "out", "on_error": "discard", "options": {"schema": {"mode": '
    '"observed"}}, "condition": null, "routes": null, "fork_to": null, "branches": null, '
    '"policy": null, "merge": null, "trigger": null, "output_mode": null, '
    '"expected_output_count": null, "timeout_seconds": null, "description": null, '
    '"scope_name": null, "scope_opener": null, "scope_policy": null}, {"id": "batch", '
    '"node_type": "aggregation", "plugin": "batch_stats", "input": "rows", "on_success": '
    '"out", "on_error": "discard", "options": {}, "condition": null, "routes": null, '
    '"fork_to": null, "branches": null, "policy": null, "merge": null, "trigger": {"count": '
    '2, "timeout_seconds": 1.5, "condition": null}, "output_mode": "transform", '
    '"expected_output_count": 1, "timeout_seconds": null, "description": null, "scope_name": '
    'null, "scope_opener": null, "scope_policy": null}], "edges": [{"id": "e1", "from_node": '
    '"source", "to_node": "t1", "edge_type": "on_success", "label": null}], "outputs": '
    '[{"sink_name": "out", "plugin": "json", "options": {"path": "result.json", "schema": '
    '{"mode": "observed"}}, "on_write_failure": "discard", "description": null}], "metadata": '
    '{"name": "Example \\u00e9", "description": null}, "source": {"plugin": "csv", '
    '"on_success": "rows", "blob_id": "blob-123", "options": {"schema": {"mode": '
    '"observed"}}, "on_validation_failure": "discard", "description": "Read source"}}',
    "node_alias_collision": '{"node": {"id": "full", "node_type": "transform", "plugin": "passthrough", '
    '"input": "rows", "on_success": "out", "on_error": "discard", "options": {"schema": '
    '{"mode": "observed"}}, "condition": null, "routes": null, "fork_to": null, '
    '"branches": null, "policy": null, "merge": null, "trigger": null, "output_mode": '
    'null, "expected_output_count": null, "timeout_seconds": null, "description": null, '
    '"scope_name": null, "scope_opener": null, "scope_policy": null}}',
    "output_alias_collision": '{"output": {"sink_name": "full", "plugin": "json", "options": {"path": '
    '"result.json", "schema": {"mode": "observed"}}, "on_write_failure": "discard", '
    '"description": null}}',
}


def _contract():
    assert importlib.util.find_spec("elspeth.web.composer.tools.state_responses") is not None, "State response contract missing"
    from elspeth.web.composer.tools.state_responses import PIPELINE_STATE_RESPONSE_CONTRACT

    return PIPELINE_STATE_RESPONSE_CONTRACT


def test_state_declaration_selects_owned_response_contract():
    from elspeth.web.composer.tools.sessions import _GET_PIPELINE_STATE_DECLARATION

    assert _GET_PIPELINE_STATE_DECLARATION.response_contract is _contract()


def test_selected_discovery_admission_rejects_corrupt_real_state_producer():
    from elspeth.web.composer.discovery_response import admit_discovery_result

    state, arguments = state_cases()["full"]
    result = execute_tool("get_pipeline_state", arguments, state, _mock_catalog())
    accepted = admit_discovery_result("get_pipeline_state", result)
    assert accepted.response is not None
    assert accepted.response.to_wire() == deep_thaw(result.data)
    bad = deep_thaw(result.data)
    bad["nodes"][0]["condition"] = []
    with pytest.raises(FrameworkBugError, match="get_pipeline_state"):
        admit_discovery_result("get_pipeline_state", replace(result, data=bad))


@pytest.mark.parametrize("name", tuple(LEGACY_WIRES))
def test_actual_state_producer_preserves_legacy_wire(name):
    state, arguments = state_cases()[name]
    result = execute_tool("get_pipeline_state", arguments, state, _mock_catalog())
    assert result.success
    assert json.dumps(deep_thaw(result.data)) == LEGACY_WIRES[name]
    admitted = _contract().admit(result.data)
    assert json.dumps(admitted.to_wire()) == LEGACY_WIRES[name]
    assert json.dumps(admitted.readmit(_contract()).to_wire()) == LEGACY_WIRES[name]


@pytest.mark.parametrize(
    "case,path,bad",
    [
        ("full", (), {"unknown": "extra"}),
        ("full", ("inspection", "requested_component"), 5),
        ("full", ("inspection", "resolved_component"), "node"),
        ("full", ("inspection", "accepted_full_state_aliases"), ["full"]),
        ("full", ("metadata", "name"), False),
        ("sources", ("sources", "source", "plugin"), 1),
        ("sources", ("sources", "source", "options", "bad"), object()),
        ("sources", ("sources", "source", "options", "bad"), float("inf")),
        ("node", ("node", "id"), False),
        ("node", ("node", "node_type"), "impostor"),
        ("node", ("node", "condition"), []),
        ("node", ("node", "routes"), {"true": 2}),
        ("node", ("node", "fork_to"), [False]),
        ("node", ("node", "branches"), {"a": 3}),
        ("aggregation", ("node", "trigger", "count"), True),
        ("aggregation", ("node", "trigger", "extra"), "bad"),
        ("node", ("node", "expected_output_count"), True),
        ("node", ("node", "timeout_seconds"), "1"),
        ("output", ("output", "on_write_failure"), 1),
        ("full", ("edges", 0, "edge_type"), "impostor"),
        ("authoring", ("source", "inline_blob"), {}),
        ("blob_authoring", ("source", "blob_id"), None),
        ("blob_authoring", ("source", "blob_id"), ""),
    ],
)
def test_corrupt_nested_state_response_is_framework_bug(case, path, bad):
    value = json.loads(LEGACY_WIRES[case])
    if not path:
        value.update(bad)
    else:
        target = value
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = bad
    with pytest.raises(FrameworkBugError, match="get_pipeline_state"):
        _contract().admit(value)


def test_required_state_keys_cannot_disappear():
    for wire in LEGACY_WIRES.values():
        value = json.loads(wire)
        for key in tuple(value):
            if key == "inspection":
                # This deletion is another valid untagged variant: the exact
                # named-sources authoring response. Request custody is separate.
                continue
            changed = deepcopy(value)
            del changed[key]
            with pytest.raises(FrameworkBugError):
                _contract().admit(changed)


@pytest.mark.parametrize("path", [("sources", "source"), ("nodes", 0), ("outputs", 0), ("edges", 0), ("metadata",), ("inspection",)])
def test_required_nested_diagnostic_fields_cannot_disappear(path):
    original = json.loads(LEGACY_WIRES["full"])
    record = original
    for part in path:
        record = record[part]
    for key in record:
        changed = deepcopy(original)
        target = changed
        for part in path:
            target = target[part]
        del target[key]
        with pytest.raises(FrameworkBugError, match="get_pipeline_state"):
            _contract().admit(changed)


def test_admitted_state_is_detached_from_mutable_raw_inputs():
    value = json.loads(LEGACY_WIRES["full"])
    admitted = _contract().admit(value)
    value["sources"]["source"]["options"]["custom"].append("later")
    assert json.dumps(admitted.to_wire()) == LEGACY_WIRES["full"]


def test_admitted_state_and_nested_configuration_are_immutable():
    from elspeth.web.composer.tools.state_responses import SourceStateResponse

    admitted = _contract().admit(json.loads(LEGACY_WIRES["sources"]))
    assert type(admitted.value) is SourceStateResponse
    with pytest.raises(FrozenInstanceError):
        admitted.value.sources = {}
    with pytest.raises(FrozenInstanceError):
        admitted.value.sources["source"].plugin = "changed"
    with pytest.raises(TypeError):
        admitted.value.sources["source"].options["path"] = "changed"


@pytest.mark.parametrize("field,bad", [("routes", False), ("condition", []), ("fork_to", 0), ("options", {"mutable": []})])
def test_readmit_rechecks_nominal_node_before_falsey_serializer_normalization(field, bad):
    from elspeth.web.composer.tools.state_responses import NodeStateResponse

    admitted = _contract().admit(json.loads(LEGACY_WIRES["node"]))
    assert type(admitted.value) is NodeStateResponse
    object.__setattr__(admitted.value.node, field, bad)
    with pytest.raises(FrameworkBugError, match="get_pipeline_state"):
        admitted.readmit(_contract())


def test_readmit_rejects_mapping_impostor_in_nominal_node_slot():
    admitted = _contract().admit(json.loads(LEGACY_WIRES["node"]))
    object.__setattr__(admitted.value, "node", json.loads(LEGACY_WIRES["node"])["node"])
    with pytest.raises(FrameworkBugError, match="get_pipeline_state"):
        admitted.readmit(_contract())


def test_readmit_rejects_mutable_config_leaf_inside_nominal_source():
    from elspeth.web.composer.tools.state_responses import SourceStateResponse

    admitted = _contract().admit(json.loads(LEGACY_WIRES["sources"]))
    assert type(admitted.value) is SourceStateResponse
    object.__setattr__(admitted.value.sources["source"], "options", MappingProxyType({"mutable": []}))
    with pytest.raises(FrameworkBugError, match="get_pipeline_state"):
        admitted.readmit(_contract())


def test_readmit_rejects_foreign_contract_identity():
    from elspeth.web.composer.tools.state_responses import PipelineStateResponseContract

    admitted = _contract().admit(json.loads(LEGACY_WIRES["node"]))
    with pytest.raises(FrameworkBugError, match="contract changed"):
        admitted.readmit(PipelineStateResponseContract())


def test_readmit_rejects_raw_mapping_substituted_for_canonical_root():
    raw = json.loads(LEGACY_WIRES["node"])
    admitted = _contract().admit(raw)
    object.__setattr__(admitted, "value", raw)
    with pytest.raises(FrameworkBugError, match="get_pipeline_state"):
        admitted.readmit(_contract())


def test_readmit_rejects_different_owned_variant_substituted_for_canonical_root():
    admitted = _contract().admit(json.loads(LEGACY_WIRES["node"]))
    replacement = _contract().admit(json.loads(LEGACY_WIRES["output"]))
    object.__setattr__(admitted, "value", replacement.value)
    with pytest.raises(FrameworkBugError, match="get_pipeline_state"):
        admitted.readmit(_contract())


@pytest.mark.parametrize("branches", [("left", "right"), {"a": "left", "b": "right"}])
def test_real_coalesce_branch_variants_remain_precise(branches):
    state, _ = state_cases()["full"]
    node = replace(
        state.nodes[0],
        node_type="coalesce",
        plugin=None,
        input="left",
        branches=branches,
        policy="require_all",
        merge="union",
        timeout_seconds=1.5,
    )
    state = replace(state, nodes=(node,))
    result = execute_tool("get_pipeline_state", {"component": node.id}, state, _mock_catalog())
    assert result.success
    assert _contract().admit(result.data).to_wire() == deep_thaw(result.data)


def test_real_gate_routes_and_forks_remain_precise():
    state, _ = state_cases()["full"]
    node = replace(
        state.nodes[0],
        node_type="gate",
        plugin=None,
        on_success=None,
        condition="row['ready']",
        routes={"true": "left", "false": "right"},
        fork_to=("left", "right"),
    )
    result = execute_tool("get_pipeline_state", {"component": node.id}, replace(state, nodes=(node,)), _mock_catalog())
    assert result.success
    assert _contract().admit(result.data).to_wire() == deep_thaw(result.data)


def test_real_alias_collision_prefers_node_then_output_then_full():
    from elspeth.web.composer.tools.state_responses import FullStateResponse, NodeStateResponse, OutputStateResponse

    state, _ = state_cases()["full"]
    state = replace(state, nodes=(replace(state.nodes[0], id="full"),), outputs=(replace(state.outputs[0], name="full"),))
    for candidate, expected in [
        (state, NodeStateResponse),
        (replace(state, nodes=()), OutputStateResponse),
        (replace(state, nodes=(), outputs=()), FullStateResponse),
    ]:
        result = execute_tool("get_pipeline_state", {"component": "full"}, candidate, _mock_catalog())
        assert type(_contract().admit(result.data).value) is expected


def test_diagnostic_metadata_is_not_admitted_as_authoring_arguments(monkeypatch):
    from elspeth.web.composer.redaction import SetPipelineArgumentsModel

    def forbid_authoring_admission(*args, **kwargs):
        raise AssertionError("Authoring input model used for diagnostic response")

    monkeypatch.setattr(SetPipelineArgumentsModel, "model_validate", forbid_authoring_admission)
    admitted = _contract().admit(json.loads(LEGACY_WIRES["blob_full"]))
    assert json.dumps(admitted.to_wire()) == LEGACY_WIRES["blob_full"]


def test_raw_cycle_and_foreign_serializer_impostor_are_rejected_safely():
    class Impostor:
        def to_dict(self):
            return json.loads(LEGACY_WIRES["node"])

    value = json.loads(LEGACY_WIRES["sources"])
    value["sources"]["source"]["options"]["cycle"] = value
    for bad in (value, Impostor()):
        with pytest.raises(FrameworkBugError) as error:
            _contract().admit(bad)
        assert str(error.value) == "Invalid get_pipeline_state producer response"


def test_unrelated_model_with_matching_json_shape_is_not_owned_response():
    class ForeignModel(BaseModel):
        node: dict[str, JsonValue]

    raw = json.loads(LEGACY_WIRES["node"])
    unrelated = ForeignModel.model_validate(raw)
    assert unrelated.model_dump() == raw
    with pytest.raises(FrameworkBugError, match="get_pipeline_state"):
        _contract().admit(unrelated)
