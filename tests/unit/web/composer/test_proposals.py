from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_freeze
from elspeth.web.composer.proposals import build_tool_proposal_summary
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.redaction import SetPipelineArgumentsModel
from elspeth.web.composer.tools import get_tool_definitions, is_mutation_tool
from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS, ASYNC_TOOL_EFFECTS, resolve_tool_effects
from elspeth.web.composer.tools.declarations import EffectDomain, ToolDeclaration, ToolKind
from elspeth.web.composer.tools.sessions import _SESSION_AWARE_TOOL_HANDLERS
from tests.unit.web.composer.test_tools import _empty_state, _mock_catalog, execute_tool


def _pipeline_arguments() -> dict[str, Any]:
    return {
        "source": {"plugin": "csv", "on_success": "rows", "options": {"path": "input.csv"}},
        "nodes": [{"id": "classify_severity", "node_type": "transform", "input": "rows", "plugin": "llm_classifier"}],
        "edges": [],
        "outputs": [{"sink_name": "out", "plugin": "json", "options": {"path": "output.json"}}],
    }


def test_is_mutation_tool_uses_closed_registries() -> None:
    assert is_mutation_tool("set_pipeline") is True
    assert is_mutation_tool("set_source_from_blob") is True
    assert is_mutation_tool("get_pipeline_state") is False
    assert is_mutation_tool("preview_pipeline") is False


def test_set_pipeline_summary_is_plain_language() -> None:
    summary = build_tool_proposal_summary(
        tool_name="set_pipeline",
        arguments=_pipeline_arguments(),
        redacted_arguments={
            "source": {"plugin": "csv", "options": {}},
            "nodes": [{"id": "classify_severity", "plugin": "llm_classifier"}],
            "outputs": [{"name": "out", "plugin": "json"}],
        },
    )

    assert summary.summary == "Replace the pipeline with 1 input, 1 processing node, and 1 output."
    assert summary.rationale == "Requested by the current composer turn."
    assert summary.affects == ("graph", "validation", "yaml")


@pytest.mark.parametrize("declaration", _REGISTERED_TOOLS, ids=lambda declaration: declaration.name)
def test_every_declared_tool_has_owned_effects(declaration: ToolDeclaration) -> None:
    arguments = _pipeline_arguments() if declaration.name == "set_pipeline" else {}
    effects = resolve_tool_effects(declaration.name, arguments)
    assert all(isinstance(domain, EffectDomain) for domain in effects.domains)
    if declaration.kind in {ToolKind.DISCOVERY, ToolKind.BLOB_DISCOVERY, ToolKind.SECRET_DISCOVERY}:
        expected = ()
    elif declaration.blob_store_only:
        expected = ("blob_store",)
    else:
        expected = ("graph", "validation", "yaml")
    assert tuple(domain.value for domain in effects.domains) == expected


def test_effect_authority_matches_shipped_and_async_tools() -> None:
    assert set(ASYNC_TOOL_EFFECTS) == set(_SESSION_AWARE_TOOL_HANDLERS) | {"request_advisor_hint"}
    assert {declaration.name for declaration in _REGISTERED_TOOLS} | set(ASYNC_TOOL_EFFECTS) == {
        definition["name"] for definition in get_tool_definitions()
    }


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("get_pipeline_state", {}, ()),
        ("create_blob", {}, ("blob_store",)),
        ("update_blob", {"content": None}, ("blob_store",)),
        ("delete_blob", {}, ("blob_store",)),
        ("wire_blob_inline_ref", {}, ("graph", "validation", "yaml")),
        ("request_interpretation_review", {}, ("interpretation",)),
        ("request_advisor_hint", {}, ()),
    ],
)
@pytest.mark.parametrize("frozen", [False, True])
def test_summary_uses_prospective_effects(name: str, arguments: dict[str, Any], expected: tuple[str, ...], frozen: bool) -> None:
    owned: Mapping[str, Any] = deep_freeze(arguments) if frozen else arguments
    summary = build_tool_proposal_summary(tool_name=name, arguments=owned, redacted_arguments={})
    assert summary.affects == expected


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("custody", ["inline_blob", "blob_id", "none"])
def test_set_pipeline_effects_use_admitted_custody(custody: str, frozen: bool) -> None:
    arguments = _pipeline_arguments()
    if custody == "inline_blob":
        arguments["source"]["inline_blob"] = {"filename": "input.csv", "mime_type": "text/csv", "content": "id\n1\n"}
    elif custody == "blob_id":
        arguments["source"]["blob_id"] = "custodied"
    else:
        arguments["source"]["inline_blob"] = None
    owned: Mapping[str, Any] = deep_freeze(arguments) if frozen else arguments
    SetPipelineArgumentsModel.model_validate(owned)
    expected = ("graph", "validation", "yaml", "blob_store") if custody == "inline_blob" else ("graph", "validation", "yaml")
    assert tuple(domain.value for domain in resolve_tool_effects("set_pipeline", owned).domains) == expected
    assert build_tool_proposal_summary(tool_name="set_pipeline", arguments=owned, redacted_arguments={}).affects == expected


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("invalid", ["source_type", "sources_type", "missing", "both", "source_null", "sources_null", "nodes_type"])
@pytest.mark.parametrize("surface", ["effects", "summary"])
def test_set_pipeline_projections_reject_invalid_complete_input(invalid: str, frozen: bool, surface: str) -> None:
    arguments = _pipeline_arguments()
    SetPipelineArgumentsModel.model_validate(arguments)
    assert tuple(domain.value for domain in resolve_tool_effects("set_pipeline", arguments).domains) == ("graph", "validation", "yaml")
    assert build_tool_proposal_summary(tool_name="set_pipeline", arguments=arguments, redacted_arguments={}).summary == (
        "Replace the pipeline with 1 input, 1 processing node, and 1 output."
    )
    malformed = deepcopy(arguments)
    if invalid == "source_type":
        malformed["source"] = "PRIVATE_MALFORMED_SOURCE"
    elif invalid == "sources_type":
        del malformed["source"]
        malformed["sources"] = "PRIVATE_MALFORMED_SOURCE"
    elif invalid == "missing":
        del malformed["source"]
    elif invalid == "both":
        malformed["sources"] = {"other": deepcopy(arguments["source"])}
    elif invalid == "source_null":
        malformed["source"] = None
    elif invalid == "sources_null":
        del malformed["source"]
        malformed["sources"] = None
    else:
        malformed["nodes"] = "PRIVATE_MALFORMED_SOURCE"
    owned: Mapping[str, Any] = deep_freeze(malformed) if frozen else malformed
    with pytest.raises(ToolArgumentError) as caught:
        if surface == "effects":
            resolve_tool_effects("set_pipeline", owned)
        else:
            build_tool_proposal_summary(tool_name="set_pipeline", arguments=owned, redacted_arguments={})
    assert caught.value.argument == "set_pipeline arguments"
    assert "PRIVATE_MALFORMED_SOURCE" not in str(caught.value)
    assert "actual JSON objects and arrays" in caught.value.expected


@pytest.mark.parametrize("validate_arguments", [False, True])
def test_set_pipeline_dispatch_retains_safe_model_rejection(validate_arguments: bool) -> None:
    arguments = _pipeline_arguments()
    arguments["nodes"] = []
    arguments["source"]["on_success"] = "out"
    arguments["source"]["options"]["schema"] = {"mode": "observed"}
    arguments["outputs"][0]["options"]["schema"] = {"mode": "observed"}
    SetPipelineArgumentsModel.model_validate(arguments)
    state = _empty_state()
    result = execute_tool("set_pipeline", arguments, state, _mock_catalog(), validate_arguments=validate_arguments)
    assert result.success
    malformed = {**arguments, "source": "PRIVATE_MALFORMED_SOURCE"}
    with pytest.raises(ToolArgumentError) as caught:
        execute_tool(
            "set_pipeline",
            malformed,
            state,
            _mock_catalog(),
            validate_arguments=validate_arguments,
            raise_schema_argument_errors=validate_arguments,
        )
    assert "PRIVATE_MALFORMED_SOURCE" not in str(caught.value)
    assert state == _empty_state()


def test_unknown_tool_rejects_summary_and_effect_lookup() -> None:
    with pytest.raises(AssertionError, match="Unknown composer tool"):
        resolve_tool_effects("unknown", {})
    with pytest.raises(AssertionError, match="Unknown composer tool"):
        build_tool_proposal_summary(tool_name="unknown", arguments={}, redacted_arguments={})


def test_effect_resolution_rejects_corrupted_owned_kind() -> None:
    declaration = ToolDeclaration(
        name="corrupted",
        handler=_REGISTERED_TOOLS[0].handler,
        kind=ToolKind.DISCOVERY,
        description="Exercise an owned declaration corrupted after admission.",
        json_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    object.__setattr__(declaration, "kind", "unknown")
    with pytest.raises(AssertionError, match="Unsupported tool kind"):
        declaration.resolve_effects({})


def test_named_inputs_and_mixed_processing_nodes() -> None:
    arguments = deep_freeze(
        {
            "sources": {
                "first": {"plugin": "csv", "on_success": "first_rows"},
                "second": {"plugin": "csv", "on_success": "second_rows"},
            },
            "nodes": [
                {"id": "t", "node_type": "transform", "input": "first_rows"},
                {"id": "g", "node_type": "gate", "input": "second_rows"},
                {"id": "c", "node_type": "collector", "input": "union"},
            ],
            "edges": [],
            "outputs": [{"sink_name": "out_a", "plugin": "json"}, {"sink_name": "out_b", "plugin": "json"}],
        }
    )
    SetPipelineArgumentsModel.model_validate(arguments)
    summary = build_tool_proposal_summary(tool_name="set_pipeline", arguments=arguments, redacted_arguments={})
    assert summary.summary == "Replace the pipeline with 2 inputs, 3 processing nodes, and 2 outputs."


@pytest.mark.parametrize(
    ("name", "key", "expected"),
    [
        ("set_source", "plugin", "Set the pipeline source to safe-label."),
        ("patch_node_options", "node_id", 'Update options for node "safe-label".'),
        ("patch_output_options", "sink_name", 'Update options for output "safe-label".'),
        ("update_blob", "blob_id", 'Overwrite session file "safe-label".'),
        ("delete_blob", "blob_id", 'Delete session file "safe-label".'),
    ],
)
def test_labels_only_use_redacted_arguments(name: str, key: str, expected: str) -> None:
    summary = build_tool_proposal_summary(
        tool_name=name,
        arguments={key: "SECRET_SENTINEL_RAW", "content": "SECRET_SENTINEL_RAW"},
        redacted_arguments={key: "safe-label"},
    )
    assert summary.summary == expected
    assert "SECRET_SENTINEL_RAW" not in repr(summary)


def test_redacted_blob_identifier_uses_generic_file_label() -> None:
    summary = build_tool_proposal_summary(tool_name="delete_blob", arguments={}, redacted_arguments={"blob_id": "<redacted>"})
    assert summary.summary == "Delete a session file."


def test_patch_node_options_summary_names_target_node() -> None:
    summary = build_tool_proposal_summary(
        tool_name="patch_node_options",
        arguments={"node_id": "classify_severity", "patch": {"model": "claude-haiku-4-5"}},
        redacted_arguments={"node_id": "classify_severity", "patch": {"model": "claude-haiku-4-5"}},
    )

    assert summary.summary == 'Update options for node "classify_severity".'
    assert summary.affects == ("graph", "validation", "yaml")
