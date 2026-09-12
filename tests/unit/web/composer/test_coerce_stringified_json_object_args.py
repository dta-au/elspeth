"""Nested object strings are malformed output, not a transport requirement.

Earlier staging gpt-5.4-mini output motivated a coercion workaround. Public
object fields now reject that output; bounded outer JSON decoding remains.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from elspeth.web.composer.bounded_json import JsonBoundaryError, bounded_json_loads
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import (
    PatchNodeOptionsArgumentsModel,
    PatchOutputOptionsArgumentsModel,
    PatchSourceOptionsArgumentsModel,
    SetPipelineArgumentsModel,
    SetSourceArgumentsModel,
    SetSourceFromBlobArgumentsModel,
)
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool


@pytest.mark.parametrize(
    "model,baseline,field",
    [
        (SetSourceArgumentsModel, {"plugin": "text", "on_success": "rows", "on_validation_failure": "discard", "options": {}}, "options"),
        (SetSourceFromBlobArgumentsModel, {"blob_id": "b", "on_success": "rows", "options": {}}, "options"),
        (PatchSourceOptionsArgumentsModel, {"patch": {}}, "patch"),
        (PatchNodeOptionsArgumentsModel, {"node_id": "n", "patch": {}}, "patch"),
        (PatchOutputOptionsArgumentsModel, {"sink_name": "s", "patch": {}}, "patch"),
    ],
)
def test_object_fields_reject_string_encoding(model, baseline, field) -> None:
    model.model_validate(baseline)
    with pytest.raises(ValidationError):
        model.model_validate({**baseline, field: json.dumps(baseline[field])})


@pytest.mark.parametrize("component", ["source", "node", "output"])
def test_full_pipeline_rejects_nested_object_strings(component: str) -> None:
    arguments = {
        "source": {"plugin": "csv", "on_success": "rows", "options": {}},
        "nodes": [{"id": "n", "node_type": "transform", "input": "rows", "options": {}}],
        "edges": [],
        "outputs": [{"sink_name": "out", "plugin": "json", "options": {}}],
    }
    SetPipelineArgumentsModel.model_validate(arguments)
    target = arguments["source"] if component == "source" else arguments["nodes" if component == "node" else "outputs"][0]
    target["options"] = "{}"
    with pytest.raises(ValidationError):
        SetPipelineArgumentsModel.model_validate(arguments)


@pytest.mark.parametrize("value", ["{}", "column=text", "[1,2]", "null", "42", '{"a":' * 100000 + "1" + "}" * 100000])
def test_public_dispatch_rejects_object_strings_safely(value: str) -> None:
    arguments = {"plugin": "csv", "on_success": "rows", "on_validation_failure": "discard", "options": value}
    with pytest.raises(ToolArgumentError) as caught:
        execute_tool("set_source", arguments, _empty_state(), _mock_catalog())
    assert isinstance(caught.value.__cause__, ValidationError)
    assert value not in str(caught.value)


def test_outer_tool_argument_json_still_decodes_objects() -> None:
    arguments = {"blob_id": "b", "on_success": "rows", "options": {"nested": {"column": "url"}}}
    decoded = bounded_json_loads(json.dumps(arguments), label="tool arguments")
    admitted = SetSourceFromBlobArgumentsModel.model_validate(decoded)
    assert admitted.options == arguments["options"]


def test_outer_decoder_retains_depth_boundary() -> None:
    with pytest.raises(JsonBoundaryError):
        bounded_json_loads('{"a":' * 100000 + "1" + "}" * 100000, label="tool arguments")
