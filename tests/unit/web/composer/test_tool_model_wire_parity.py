"""Live MODEL key parity plus behavioral admission probes (distinct claims)."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from scripts.cicd.composer_wire_census import census_model_wire

from elspeth.contracts.secrets import WebSecretResolver
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.tool_error_payloads import arg_error_payload
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.schema_contract import assert_tool_model_key_parity
from elspeth.web.composer.tools.transforms import _UpsertNodeArgumentsModel
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool


def test_every_shipped_tool_has_proven_original_input_model() -> None:
    rows = census_model_wire()
    definitions = get_tool_definitions()
    assert rows and set(rows) == {definition["name"] for definition in definitions}
    for row in rows.values():
        assert row.model_class is not None, f"{row.tool}: missing MODEL admission: {row.site}"
        assert not row.site.startswith("unresolved:"), row.site
        assert_tool_model_key_parity(tool_name=row.tool, shipped=row.shipped, model_fields=row.model_fields)


@pytest.mark.parametrize("tool", [definition["name"] for definition in get_tool_definitions()])
def test_schema_feedback_remains_safe_when_read_and_reconstructed(tool: str) -> None:
    error = ToolArgumentError(
        argument=f"{tool} arguments",
        expected="object conforming to secret-model-name",
        actual_type="dict",
    )
    assert "actual JSON objects and arrays" in error.expected
    assert "secret-model-name" not in error.expected
    assert str(error) == f"'{error.argument}' must be {error.expected}, got {error.actual_type}"
    rebuilt = ToolArgumentError(argument=error.argument, expected=error.expected, actual_type=error.actual_type)
    assert rebuilt.expected == error.expected
    assert str(rebuilt) == str(error)


def test_unknown_feedback_prose_is_not_admitted() -> None:
    error = ToolArgumentError(argument="tool arguments", expected="secret-model-name", actual_type="dict")
    assert error.expected == "a valid value"
    assert "secret-model-name" not in str(error)


@pytest.mark.parametrize("shipped,model", [(frozenset({"new"}), frozenset()), (frozenset(), frozenset({"hidden"}))])
def test_model_key_pin_refuses_both_directions(shipped, model) -> None:
    with pytest.raises(RuntimeError, match="MODEL key mismatch"):
        assert_tool_model_key_parity(tool_name="probe", shipped=shipped, model_fields=model)


_EMPTY_TOOLS = tuple(definition["name"] for definition in get_tool_definitions() if not definition["parameters"]["properties"])


@pytest.mark.parametrize("tool", _EMPTY_TOOLS)
def test_empty_handler_admits_only_empty_original_input(tool: str) -> None:
    state = _empty_state()
    secrets = MagicMock(spec=WebSecretResolver)
    secrets.list_refs.return_value = []
    # Domain failures without session context are legitimate positive admission.
    execute_tool(tool, {}, state, _mock_catalog(), secret_service=secrets, user_id="test")
    with pytest.raises(ToolArgumentError):
        execute_tool(tool, {"invented": "secret-value"}, state, _mock_catalog(), secret_service=secrets, user_id="test")


@pytest.mark.parametrize("value", [True, "3"])
@pytest.mark.parametrize("full_pipeline", [False, True])
def test_expected_output_count_rejects_coerced_scalar(value, full_pipeline: bool) -> None:
    node = {"id": "a", "node_type": "transform", "input": "rows", "expected_output_count": 3}
    if full_pipeline:
        arguments = {"source": {"plugin": "csv", "on_success": "rows"}, "nodes": [node], "edges": [], "outputs": []}
        SetPipelineArgumentsModel.model_validate(arguments)
        node["expected_output_count"] = value
        with pytest.raises(ValidationError):
            SetPipelineArgumentsModel.model_validate(arguments)
    else:
        _UpsertNodeArgumentsModel.model_validate(node)
        node["expected_output_count"] = value
        with pytest.raises(ToolArgumentError):
            execute_tool("upsert_node", node, _empty_state(), _mock_catalog())


@pytest.mark.parametrize("value", [3, 3.0, None])
def test_json_integer_and_nullable_controls(value) -> None:
    node = {"id": "a", "node_type": "transform", "input": "rows", "expected_output_count": value}
    assert _UpsertNodeArgumentsModel.model_validate(node).expected_output_count == value
    SetPipelineArgumentsModel.model_validate(
        {"source": {"plugin": "csv", "on_success": "rows"}, "nodes": [node], "edges": [], "outputs": []}
    )


@pytest.mark.parametrize("field", ["count", "timeout_seconds"])
@pytest.mark.parametrize("value", [True, "3"])
def test_trigger_strict_scalars(field: str, value) -> None:
    node = {"id": "a", "node_type": "aggregation", "input": "rows", "trigger": {field: 3}}
    _UpsertNodeArgumentsModel.model_validate(node)
    node["trigger"][field] = value
    with pytest.raises(ValidationError):
        _UpsertNodeArgumentsModel.model_validate(node)
    with pytest.raises(ValidationError):
        SetPipelineArgumentsModel.model_validate(
            {"source": {"plugin": "csv", "on_success": "rows"}, "nodes": [node], "edges": [], "outputs": []}
        )


def test_owned_trigger_rejects_unknown_fields() -> None:
    node = {"id": "a", "node_type": "aggregation", "input": "rows", "trigger": {"count": 3}}
    _UpsertNodeArgumentsModel.model_validate(node)
    node["trigger"]["invented"] = 1
    with pytest.raises(ToolArgumentError):
        execute_tool("upsert_node", node, _empty_state(), _mock_catalog())


@pytest.mark.parametrize("value", [True, "3", 3.5, None, 0, -1])
def test_list_models_limit_rejects_invalid_values(value) -> None:
    with pytest.raises(ToolArgumentError):
        execute_tool("list_models", {"limit": value}, _empty_state(), _mock_catalog())


def test_list_models_filter_default_and_positive_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "elspeth.web.composer.tools.generation.read_litellm_model_list", lambda: [f"example/model{i:02}" for i in range(60)]
    )
    state = _empty_state()
    default = execute_tool("list_models", {"provider": "example/"}, state, _mock_catalog())
    limited = execute_tool("list_models", {"provider": "example/", "limit": 1}, state, _mock_catalog())
    integral = execute_tool("list_models", {"provider": "example/", "limit": 1.0}, state, _mock_catalog())
    assert len(default.data["models"]) == 50
    assert len(limited.data["models"]) == 1
    assert integral.data == limited.data


@pytest.mark.parametrize(
    "tool,args,field",
    [
        ("get_pipeline_state", {}, "component"),
        ("list_models", {}, "provider"),
        ("set_metadata", {"patch": {}}, "patch"),
    ],
)
def test_omitted_nonnull_fields_reject_explicit_null(tool, args, field) -> None:
    execute_tool(tool, args, _empty_state(), _mock_catalog())
    bad = deepcopy(args)
    if field == "patch":
        bad[field]["name"] = None
    else:
        bad[field] = None
    with pytest.raises(ToolArgumentError):
        execute_tool(tool, bad, _empty_state(), _mock_catalog())


@pytest.mark.parametrize("value", ["", None, 3])
def test_clear_source_rejects_invalid_name(value) -> None:
    with pytest.raises(ToolArgumentError):
        execute_tool("clear_source", {"source_name": value}, _empty_state(), _mock_catalog())


def test_corrected_published_constraints() -> None:
    schemas = {d["name"]: d["parameters"] for d in get_tool_definitions()}
    assert schemas["list_models"]["properties"]["limit"]["minimum"] == 1
    assert schemas["list_models"]["properties"]["limit"]["default"] == 50
    assert schemas["clear_source"]["properties"]["source_name"]["minLength"] == 1
    assert schemas["clear_source"]["properties"]["source_name"]["default"] == "source"
    assert not Draft202012Validator(schemas["set_metadata"]).is_valid({"patch": {"invented": 1}})
    assert not Draft202012Validator(schemas["set_pipeline"]).is_valid(
        {"source": {"plugin": "csv", "on_success": "rows", "invented": 1}, "nodes": [], "edges": [], "outputs": []}
    )


def test_json_looking_content_remains_literal_string_data() -> None:
    from elspeth.web.composer.redaction import CreateBlobArgumentsModel, SetSourceFromBlobArgumentsModel

    literal = '{"token":"value"}'
    assert (
        CreateBlobArgumentsModel.model_validate({"filename": "a.json", "mime_type": "application/json", "content": literal}).content
        == literal
    )
    source = SetSourceFromBlobArgumentsModel.model_validate({"blob_id": "b", "on_success": "rows", "options": {"template": literal}})
    assert source.options["template"] == literal


@pytest.mark.parametrize(
    "model,arguments,field",
    [
        pytest.param("create_blob", {"filename": "a.txt", "mime_type": "text/plain", "content": "x"}, "description"),
        pytest.param("set_source_from_blob", {"blob_id": "b", "on_success": "rows"}, "plugin"),
        pytest.param("set_source_from_blob", {"blob_id": "b", "on_success": "rows"}, "on_validation_failure"),
    ],
)
def test_shared_redaction_model_omission_is_not_supplied_null(model, arguments, field) -> None:
    from elspeth.web.composer.redaction import CreateBlobArgumentsModel, SetSourceFromBlobArgumentsModel

    owner = CreateBlobArgumentsModel if model == "create_blob" else SetSourceFromBlobArgumentsModel
    owner.model_validate(arguments)
    schema = owner.model_json_schema()
    assert not Draft202012Validator(schema).is_valid({**arguments, field: None})
    with pytest.raises(ValidationError):
        owner.model_validate({**arguments, field: None})


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("list_models", {"limit": True}),
        ("get_plugin_schema", {"plugin_type": "invalid-secret-value", "name": "csv"}),
        ("list_models", {"provider": None}),
        ("set_source", {"plugin": "csv", "on_success": "rows", "on_validation_failure": "discard", "options": '{"secret":"do-not-echo"}'}),
    ],
)
def test_structural_feedback_teaches_actual_json_types(tool, arguments) -> None:
    with pytest.raises(ToolArgumentError) as caught:
        execute_tool(tool, arguments, _empty_state(), _mock_catalog(), validate_arguments=True, raise_schema_argument_errors=True)
    assert "actual JSON objects and arrays" in caught.value.expected
    assert "actual JSON objects and arrays" in arg_error_payload(caught.value, tool)["error"]
    assert "invalid-secret-value" not in str(caught.value)
    assert "do-not-echo" not in str(caught.value)
