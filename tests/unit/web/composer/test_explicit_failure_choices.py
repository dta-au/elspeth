"""No authoring seam may turn a missing failure choice into discard."""

from copy import deepcopy
from dataclasses import replace

import pytest
import yaml

from elspeth.config_loading import load_settings_from_yaml_string
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.contracts.freeze import deep_thaw
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.null_source import NullSource
from elspeth.web.composer.protocol import SchemaViolationCode, ToolArgumentError
from elspeth.web.composer.redaction import redact_tool_call_arguments
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.required_controls import _parse_candidate_state
from elspeth.web.composer.state import NodeSpec, OutputSpec, SourceSpec
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.schema_contract import assert_set_pipeline_schema_compatible
from elspeth.web.composer.tools.wire_projection import decode_wire_arguments, project_tool
from elspeth.web.composer.yaml_generator import generate_yaml
from elspeth.web.composer.yaml_importer import (
    RuntimeYamlImportError,
    _require_ordinary_failure_choice,
    _source_from_runtime_entry,
    composition_state_from_runtime_yaml,
)
from tests.unit.web.composer.test_splice_transform_tool import _arguments as splice_arguments
from tests.unit.web.composer.test_splice_transform_tool import _state as splice_state
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool


def _ordinary_failure_yaml(kind: str, route: object) -> str:
    document = {
        "sources": {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "on_validation_failure": "discard",
                "options": {"schema": {"mode": "observed"}},
            }
        },
        "sinks": {
            "PRIVATE_SINK_MARKER": {
                "plugin": "json",
                "on_write_failure": route if kind == "sinks" else "discard",
                "options": {"path": "outputs/out.json", "schema": {"mode": "observed"}},
            }
        },
    }
    if kind != "sinks":
        document[kind] = [
            {
                "name": "PRIVATE_NODE_MARKER",
                "plugin": "passthrough",
                "input": "rows",
                "on_success": "PRIVATE_SINK_MARKER",
                "on_error": route,
                "options": {"schema": {"mode": "observed"}},
            }
        ]
    return yaml.safe_dump(document)


@pytest.mark.parametrize("kind", ["transforms", "aggregations", "sinks"])
@pytest.mark.parametrize("route", [None, "", "   ", 42, "OMITTED"])
def test_ordinary_yaml_failure_choice_is_nonblank_private_safe(kind, route):
    document = yaml.safe_load(_ordinary_failure_yaml(kind, route))
    entry = document["sinks"]["PRIVATE_SINK_MARKER"] if kind == "sinks" else document[kind][0]
    field = "on_write_failure" if kind == "sinks" else "on_error"
    if route == "OMITTED":
        entry.pop(field)
    with pytest.raises(RuntimeYamlImportError):
        _require_ordinary_failure_choice(entry, field=field, location=f"{kind}.item")
    with pytest.raises(RuntimeYamlImportError) as caught:
        composition_state_from_runtime_yaml(yaml.safe_dump(document))
    assert f"{kind}.item." in str(caught.value)
    assert "explicit non-empty" in str(caught.value)
    assert "PRIVATE_" not in str(caught.value)


@pytest.mark.parametrize("kind", ["transforms", "aggregations", "sinks"])
@pytest.mark.parametrize("route", ["discard", " discard ", " quarantine "])
def test_ordinary_yaml_failure_choice_is_canonical_without_gate_policy_change(kind, route):
    document = yaml.safe_load(_ordinary_failure_yaml(kind, route))
    document["gates"] = [{"name": "gate", "input": "rows", "condition": "True", "routes": {"true": "PRIVATE_SINK_MARKER"}}]
    imported = composition_state_from_runtime_yaml(yaml.safe_dump(document))
    assert next(node for node in imported.nodes if node.node_type == "gate").on_error is None
    if kind == "sinks":
        assert imported.outputs[0].on_write_failure == route.strip()
    else:
        assert next(node for node in imported.nodes if node.node_type != "gate").on_error == route.strip()


@pytest.mark.parametrize("choice", [None, "discard", "quarantine"])
def test_yaml_optional_gate_failure_choice_is_preserved(choice):
    document = yaml.safe_load(_ordinary_failure_yaml("sinks", "discard"))
    document["gates"] = [
        {"name": "gate", "input": "rows", "condition": "True", "routes": {"true": "PRIVATE_SINK_MARKER"}, "on_error": choice}
    ]
    imported = composition_state_from_runtime_yaml(yaml.safe_dump(document))
    assert imported.nodes[0].on_error == choice


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "upsert_node",
            {
                "id": "step",
                "node_type": "transform",
                "plugin": "passthrough",
                "input": "rows",
                "on_success": "out",
                "options": {"schema": {"mode": "observed"}},
            },
        ),
        (
            "upsert_node",
            {
                "id": "step",
                "node_type": "aggregation",
                "plugin": "passthrough",
                "input": "rows",
                "on_success": "out",
                "options": {"schema": {"mode": "observed"}},
            },
        ),
        ("set_source", {"plugin": "csv", "on_success": "rows", "options": {"path": "rows.csv", "schema": {"mode": "observed"}}}),
        ("set_source_from_blob", {"blob_id": "00000000-0000-0000-0000-000000000001", "on_success": "rows"}),
        ("set_source_from_blobs", {"blob_ids": ["00000000-0000-0000-0000-000000000001"], "on_success": "rows"}),
        ("set_output", {"sink_name": "out", "plugin": "json", "options": {"path": "out.json", "schema": {"mode": "observed"}}}),
    ],
)
def test_new_component_missing_choice_is_owned_semantic_rejection(tool, arguments):
    state = _empty_state()
    result = execute_tool(tool, arguments, state, _mock_catalog(), validate_arguments=True)
    assert not result.success
    assert result.updated_state is state
    assert result.validation.errors[0].error_code == "failure_route_required"
    assert "explicit non-empty" in result.validation.errors[0].message


@pytest.mark.parametrize("route", ["discard", "quarantine"])
def test_upsert_preserves_omitted_existing_choice_and_rejects_null_empty(route):
    node = NodeSpec(
        id="step",
        node_type="transform",
        plugin="passthrough",
        input="rows",
        on_success="out",
        on_error=route,
        options={"schema": {"mode": "observed"}},
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    state = replace(_empty_state(), nodes=(node,))
    arguments = {
        "id": "step",
        "node_type": "transform",
        "plugin": "passthrough",
        "input": "rows",
        "on_success": "out",
        "options": {"schema": {"mode": "observed"}},
    }
    result = execute_tool("upsert_node", arguments, state, _mock_catalog())
    assert result.success
    assert result.updated_state.nodes[0].on_error == route
    for invalid in (None, "   "):
        rejected = execute_tool("upsert_node", {**arguments, "on_error": invalid}, state, _mock_catalog())
        assert not rejected.success
        assert rejected.updated_state is state
        assert rejected.validation.errors[0].error_code == "failure_route_required"
    with pytest.raises(ToolArgumentError):
        execute_tool("upsert_node", {**arguments, "on_error": ""}, state, _mock_catalog())


@pytest.mark.parametrize("tool", ["set_source", "set_output"])
def test_same_identity_update_preserves_named_choice(tool):
    state = replace(
        _empty_state(),
        sources={"source": SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="quarantine")},
        outputs=(OutputSpec(name="out", plugin="json", options={}, on_write_failure="quarantine"),),
    )
    arguments = (
        {"plugin": "csv", "on_success": "rows", "options": {"path": "rows.csv", "schema": {"mode": "observed"}}}
        if tool == "set_source"
        else {"sink_name": "out", "plugin": "json", "options": {"path": "out.json", "schema": {"mode": "observed"}}}
    )
    result = execute_tool(tool, arguments, state, _mock_catalog())
    assert result.success
    if tool == "set_source":
        assert result.updated_state.sources["source"].on_validation_failure == "quarantine"
    else:
        assert result.updated_state.outputs[0].on_write_failure == "quarantine"


@pytest.mark.parametrize("plugin", ["csv", "null"])
@pytest.mark.parametrize("route", [None, "", "   ", True, 42])
@pytest.mark.parametrize("invalid_spelling", ["entry", "options"])
def test_public_yaml_rejects_invalid_source_choice_without_echo(plugin, route, invalid_spelling):
    entry = {
        "plugin": plugin,
        "on_success": "rows",
        "options": {"on_validation_failure": route if invalid_spelling == "options" else "quarantine"},
        "on_validation_failure": route if invalid_spelling == "entry" else "quarantine",
    }
    with pytest.raises(RuntimeYamlImportError, match="on_validation_failure"):
        _source_from_runtime_entry("source", entry)


@pytest.mark.parametrize("plugin", ["csv", "null"])
def test_public_yaml_omission_rejected_runtime_null_unchanged(plugin):
    with pytest.raises(RuntimeYamlImportError, match="on_validation_failure"):
        _source_from_runtime_entry("source", {"plugin": plugin, "on_success": "rows", "options": {}})
    assert type(NullSource({})) is NullSource


def test_yaml_dual_spelling_compares_valid_trimmed_values():
    imported = _source_from_runtime_entry(
        "source",
        {
            "plugin": "csv",
            "on_success": "rows",
            "on_validation_failure": " quarantine ",
            "options": {"on_validation_failure": "quarantine"},
        },
    )
    assert imported.on_validation_failure == "quarantine"


def test_yaml_conflict_diagnostic_does_not_echo_authored_values():
    with pytest.raises(RuntimeYamlImportError) as caught:
        _source_from_runtime_entry(
            "source",
            {
                "plugin": "csv",
                "on_success": "rows",
                "on_validation_failure": "PRIVATE_ROUTE_A",
                "options": {"on_validation_failure": "PRIVATE_ROUTE_B"},
            },
        )
    assert "conflicting" in str(caught.value)
    assert "PRIVATE_ROUTE" not in str(caught.value)


@pytest.mark.parametrize("route", ["discard", "result"])
def test_splice_requires_new_choice_but_preserves_omitted_idempotent_replay(route):
    state = splice_state()
    arguments = splice_arguments()
    arguments["node"]["on_error"] = route
    omitted = {**arguments, "node": {k: v for k, v in arguments["node"].items() if k != "on_error"}}
    rejected = execute_tool("splice_transform", omitted, state, _mock_catalog(), validate_arguments=True)
    assert not rejected.success
    assert rejected.updated_state is state
    assert rejected.validation.errors[0].error_code == "failure_route_required"
    inserted = execute_tool("splice_transform", arguments, state, _mock_catalog(), validate_arguments=True)
    assert inserted.success
    replay = execute_tool("splice_transform", omitted, inserted.updated_state, _mock_catalog(), validate_arguments=True)
    assert replay.success
    assert replay.updated_state is inserted.updated_state
    assert replay.updated_state.nodes[1].on_error == route
    assert replay.data["already_applied"] is True
    for invalid in (None, "", "   "):
        bad = {**arguments, "node": {**arguments["node"], "on_error": invalid}}
        refused = execute_tool("splice_transform", bad, inserted.updated_state, _mock_catalog(), validate_arguments=True)
        assert not refused.success
        assert refused.updated_state is inserted.updated_state
    stale = replace(
        inserted.updated_state,
        nodes=tuple(replace(node, on_error=None) if node.id == "inserted" else node for node in inserted.updated_state.nodes),
    )
    refused = execute_tool("splice_transform", omitted, stale, _mock_catalog(), validate_arguments=True)
    assert not refused.success
    assert refused.updated_state is stale
    changed = replace(
        inserted.updated_state,
        nodes=tuple(replace(node, on_success="other") if node.id == "before" else node for node in inserted.updated_state.nodes),
    )
    refused = execute_tool("splice_transform", omitted, changed, _mock_catalog(), validate_arguments=True)
    assert not refused.success
    assert refused.updated_state is changed


def test_omission_cannot_borrow_a_different_identity_or_node_kind():
    state = splice_state()
    arguments = {
        "plugin": "csv",
        "source_name": "other",
        "on_success": "rows",
        "options": {"path": "rows.csv", "schema": {"mode": "observed"}},
    }
    source = execute_tool("set_source", arguments, state, _mock_catalog(), validate_arguments=True)
    assert not source.success and source.updated_state is state
    node = execute_tool(
        "upsert_node",
        {"id": "before", "node_type": "aggregation", "plugin": "passthrough", "input": "rows"},
        state,
        _mock_catalog(),
        validate_arguments=True,
    )
    assert not node.success and node.updated_state is state
    assert node.validation.errors[0].error_code == "failure_route_required"


@pytest.mark.parametrize("plugin", ["csv", "json"])
def test_same_source_identity_plugin_replacement_retains_named_choice(plugin):
    state = replace(
        _empty_state(), sources={"source": SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="quarantine")}
    )
    result = execute_tool(
        "set_source",
        {"plugin": plugin, "on_success": "rows", "options": {"path": "rows.data", "schema": {"mode": "observed"}}},
        state,
        _mock_catalog(),
        validate_arguments=True,
    )
    assert result.success
    assert result.updated_state.sources["source"].on_validation_failure == "quarantine"


def test_options_only_patches_preserve_actual_failure_routes():
    state = replace(
        _empty_state(),
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "rows.csv", "schema": {"mode": "observed"}},
                on_validation_failure="quarantine",
            )
        },
        outputs=(
            OutputSpec(
                name="out", plugin="json", options={"path": "out.json", "schema": {"mode": "observed"}}, on_write_failure="quarantine"
            ),
        ),
    )
    source = execute_tool("patch_source_options", {"patch": {"path": "changed.csv"}}, state, _mock_catalog())
    assert source.success
    assert source.updated_state.sources["source"].on_validation_failure == "quarantine"
    output = execute_tool("patch_output_options", {"sink_name": "out", "patch": {"path": "changed.json"}}, state, _mock_catalog())
    assert output.success
    assert output.updated_state.outputs[0].on_write_failure == "quarantine"


def test_actual_full_schema_model_parity_keeps_conditional_node_negative_control():
    schema = next(item["parameters"] for item in get_tool_definitions() if item["name"] == "set_pipeline")
    assert_set_pipeline_schema_compatible(advertised_schema=schema)
    changed = deepcopy(schema)
    changed["properties"]["nodes"]["items"]["required"].append("on_error")
    with pytest.raises(RuntimeError, match="advertised required fields are optional"):
        assert_set_pipeline_schema_compatible(advertised_schema=changed)


@pytest.mark.parametrize("route", ["discard", "result"])
def test_explicit_route_survives_yaml_runtime_source_validation_and_reimport(route):
    state = splice_state()
    state = replace(state, sources={name: replace(source, on_validation_failure=route) for name, source in state.sources.items()})
    document = generate_yaml(state)
    settings = load_settings_from_yaml_string(document)
    source_settings = settings.sources["source"]
    runtime_source = CSVSource(source_settings.options)
    assert runtime_source.config["on_validation_failure"] == route
    imported = composition_state_from_runtime_yaml(document)
    assert imported.sources["source"].on_validation_failure == route
    assert [node.on_error for node in imported.nodes] == [node.on_error for node in state.nodes]
    assert [output.on_write_failure for output in imported.outputs] == [output.on_write_failure for output in state.outputs]


def test_gate_omission_preserves_choice_and_null_clears_to_fail_fast():
    node = NodeSpec(
        id="gate",
        node_type="gate",
        plugin=None,
        input="rows",
        on_success=None,
        on_error="quarantine",
        options={},
        condition="True",
        routes={"true": "out", "false": "discard"},
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    state = replace(_empty_state(), nodes=(node,))
    arguments = {"id": "gate", "node_type": "gate", "input": "rows", "condition": "True", "routes": {"true": "out", "false": "discard"}}
    preserved = execute_tool("upsert_node", arguments, state, _mock_catalog(), validate_arguments=True)
    assert preserved.success
    assert preserved.updated_state.nodes[0].on_error == "quarantine"
    cleared = execute_tool("upsert_node", {**arguments, "on_error": None}, state, _mock_catalog(), validate_arguments=True)
    assert cleared.success
    assert cleared.updated_state.nodes[0].on_error is None


def test_full_replacement_missing_choices_cannot_be_autowired_or_borrowed():
    state = replace(
        _empty_state(), sources={"source": SourceSpec(plugin="csv", on_success="rows", options={}, on_validation_failure="discard")}
    )
    candidate = {
        "sources": {"source": {"plugin": "csv", "on_success": "rows", "options": {"path": "rows.csv", "schema": {"mode": "observed"}}}},
        "nodes": [],
        "edges": [],
        "outputs": [{"sink_name": "out", "plugin": "json", "options": {"path": "out.json", "schema": {"mode": "observed"}}}],
    }
    assert _parse_candidate_state(candidate) is None
    with pytest.raises(ToolArgumentError):
        execute_tool("set_pipeline", candidate, state, _mock_catalog(), validate_arguments=True, raise_schema_argument_errors=True)
    assert state.version == 1
    assert state.sources["source"].on_validation_failure == "discard"


@pytest.mark.parametrize("carrier", ["source", "sources", "outputs"])
@pytest.mark.parametrize("supplied", [False, True])
def test_full_schema_choice_error_has_owned_repair_and_sanitized_path(carrier, supplied):
    source = {"plugin": "csv", "on_success": "out", "on_validation_failure": "discard", "options": {}}
    output = {"sink_name": "out", "plugin": "json", "on_write_failure": "discard", "options": {}}
    candidate = {"nodes": [], "edges": [], "outputs": [output]}
    field = "on_write_failure" if carrier == "outputs" else "on_validation_failure"
    if carrier == "sources":
        candidate["sources"] = {"PRIVATE_SOURCE_NAME": source}
    else:
        candidate["source"] = source
    target = output if carrier == "outputs" else source
    if supplied:
        target[field] = None
    else:
        target.pop(field)
    expected = {"source": ("source", field), "sources": ("sources", "item", field), "outputs": ("outputs", "index", field)}[carrier]
    with pytest.raises(ToolArgumentError) as caught:
        execute_tool("set_pipeline", candidate, _empty_state(), _mock_catalog(), validate_arguments=True, raise_schema_argument_errors=True)
    assert any(
        v.loc == expected and v.code == (SchemaViolationCode.INVALID_TYPE if supplied else SchemaViolationCode.MISSING)
        for v in caught.value.schema_violations
    )
    payload = arg_error_payload(caught.value, "set_pipeline")
    assert "explicit non-empty failure choice" in payload["repair_instruction"]
    assert "Full replacement does not reuse prior choices" in payload["repair_instruction"]
    assert "PRIVATE_SOURCE_NAME" not in str(payload)


@pytest.mark.parametrize("kind", ["transform", "aggregation"])
def test_full_node_conditional_choice_refuses_without_borrowing(kind):
    candidate = {
        "source": {"plugin": "csv", "on_success": "rows", "on_validation_failure": "discard", "options": {}},
        "nodes": [{"id": "step", "node_type": kind, "input": "rows", "plugin": "passthrough", "on_success": "out", "options": {}}],
        "edges": [],
        "outputs": [{"sink_name": "out", "plugin": "json", "on_write_failure": "discard", "options": {}}],
    }
    state = splice_state()
    result = execute_tool("set_pipeline", candidate, state, _mock_catalog(), validate_arguments=True, raise_schema_argument_errors=True)
    assert not result.success
    assert result.updated_state is state
    assert result.validation.errors[0].error_code == "failure_route_required"


@pytest.mark.parametrize("dialect", list(ToolContractDialect))
@pytest.mark.parametrize(
    ("tool", "arguments", "field"),
    [
        ("set_source", {"plugin": "csv", "on_success": "rows", "options": {}}, "on_validation_failure"),
        ("set_source_from_blob", {"blob_id": "blob", "on_success": "rows"}, "on_validation_failure"),
        ("set_source_from_blobs", {"blob_ids": ["blob"], "on_success": "rows"}, "on_validation_failure"),
        ("upsert_node", {"id": "step", "node_type": "transform", "input": "rows"}, "on_error"),
        ("set_output", {"sink_name": "out", "plugin": "json", "options": {}}, "on_write_failure"),
    ],
)
def test_actual_wire_decode_and_redaction_preserve_presence(tool, arguments, field, dialect):
    definition = next(item for item in get_tool_definitions() if item["name"] == tool)
    projected = project_tool(definition, dialect)
    assert not projected.strict_capable
    assert not projected.promoted_paths
    for supplied in (False, True):
        raw = {**arguments, field: None} if supplied else arguments
        decoded = decode_wire_arguments(tool, dialect, raw)
        assert decoded.wire_conformant
        semantic = deep_thaw(decoded.semantic)
        assert (field in semantic) is supplied
        if supplied:
            assert semantic[field] is None
        redacted = redact_tool_call_arguments(tool, semantic, telemetry=NoopRedactionTelemetry())
        assert (field in redacted) is supplied
