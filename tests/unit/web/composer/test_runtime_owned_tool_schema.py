"""Provider-visible ownership contract for Composer option writes.

Regression coverage for elspeth-de89544aca.  The runtime write gates have
always rejected resolver/runtime-owned review metadata, but the advertised
tool schemas used to describe every payload as an unconstrained options
object.  That forced the planner to spend a provider turn discovering a rule
the server already knew.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from elspeth.web.composer.tools import get_tool_definitions
from elspeth.web.composer.tools._common import (
    _AUTHOR_OWNED_INTERPRETATION_REQUIREMENT_FIELDS_TEXT,
    _RESOLVER_OWNED_INTERPRETATION_REQUIREMENT_FIELDS,
    _RUNTIME_OWNED_LLM_OPTION_KEYS,
    _SERVER_OWNED_SOURCE_OPTION_KEYS,
    INTERPRETATION_REQUIREMENTS_KEY,
    _resolver_owned_interpretation_requirement_error,
    _runtime_owned_llm_option_error,
)


def _definitions() -> dict[str, Mapping[str, Any]]:
    return {definition["name"]: definition for definition in get_tool_definitions()}


def _source_option_schemas() -> dict[str, Mapping[str, Any]]:
    definitions = _definitions()
    set_pipeline = definitions["set_pipeline"]["parameters"]["properties"]
    return {
        "set_source.options": definitions["set_source"]["parameters"]["properties"]["options"],
        "set_source_from_blob.options": definitions["set_source_from_blob"]["parameters"]["properties"]["options"],
        "set_source_from_blobs.options": definitions["set_source_from_blobs"]["parameters"]["properties"]["options"],
        "patch_source_options.patch": definitions["patch_source_options"]["parameters"]["properties"]["patch"],
        "set_pipeline.source.options": set_pipeline["source"]["properties"]["options"],
        "set_pipeline.sources.*.options": set_pipeline["sources"]["additionalProperties"]["properties"]["options"],
    }


def _node_option_schemas() -> dict[str, Mapping[str, Any]]:
    definitions = _definitions()
    set_pipeline = definitions["set_pipeline"]["parameters"]["properties"]
    return {
        "upsert_node.options": definitions["upsert_node"]["parameters"]["properties"]["options"],
        "patch_node_options.patch": definitions["patch_node_options"]["parameters"]["properties"]["patch"],
        "splice_transform.node.options": definitions["splice_transform"]["parameters"]["properties"]["node"]["properties"]["options"],
        "set_pipeline.nodes[].options": set_pipeline["nodes"]["items"]["properties"]["options"],
    }


def _output_option_schemas() -> dict[str, Mapping[str, Any]]:
    definitions = _definitions()
    set_pipeline = definitions["set_pipeline"]["parameters"]["properties"]
    return {
        "set_output.options": definitions["set_output"]["parameters"]["properties"]["options"],
        "patch_output_options.patch": definitions["patch_output_options"]["parameters"]["properties"]["patch"],
        "set_pipeline.outputs[].options": set_pipeline["outputs"]["items"]["properties"]["options"],
    }


@pytest.mark.parametrize("schema_name", sorted(_source_option_schemas()))
def test_source_option_schema_discloses_every_resolver_owned_review_field(schema_name: str) -> None:
    description = _source_option_schemas()[schema_name]["description"]

    assert "not settable" in description
    assert _AUTHOR_OWNED_INTERPRETATION_REQUIREMENT_FIELDS_TEXT in description
    for field_name in _SERVER_OWNED_SOURCE_OPTION_KEYS:
        assert field_name in description
    for field_name in _RESOLVER_OWNED_INTERPRETATION_REQUIREMENT_FIELDS:
        assert field_name in description


@pytest.mark.parametrize("schema_name", sorted(_node_option_schemas()))
def test_node_option_schema_discloses_runtime_and_resolver_owned_fields(schema_name: str) -> None:
    description = _node_option_schemas()[schema_name]["description"]

    assert "not settable" in description
    assert _AUTHOR_OWNED_INTERPRETATION_REQUIREMENT_FIELDS_TEXT in description
    for field_name in _RUNTIME_OWNED_LLM_OPTION_KEYS | _RESOLVER_OWNED_INTERPRETATION_REQUIREMENT_FIELDS:
        assert field_name in description


@pytest.mark.parametrize("schema_name", sorted(_output_option_schemas()))
def test_output_option_schema_discloses_forbidden_review_root(schema_name: str) -> None:
    description = _output_option_schemas()[schema_name]["description"]

    assert "not settable" in description
    assert INTERPRETATION_REQUIREMENTS_KEY in description
    assert "source or node" in description


def test_blob_inline_field_path_schema_discloses_forbidden_metadata_roots() -> None:
    field_path = _definitions()["wire_blob_inline_ref"]["parameters"]["properties"]["field_path"]
    description = field_path["description"]

    assert "not settable" in description
    assert "interpretation_requirements" in description
    for field_name in _RUNTIME_OWNED_LLM_OPTION_KEYS:
        assert field_name in description
    for field_name in _SERVER_OWNED_SOURCE_OPTION_KEYS:
        assert field_name in description
    assert "LLM node" in description
    assert "patch_node_options" in description
    assert "upsert_node" in description


@pytest.mark.parametrize("tool_name", ["patch_node_options", "upsert_node", "set_pipeline", "splice_transform"])
def test_runtime_owned_hash_rejection_names_the_retry_tool(tool_name: str) -> None:
    error = _runtime_owned_llm_option_error(
        "llm",
        {"resolved_prompt_template_hash": None},
        tool_name=tool_name,
    )

    assert error is not None
    assert f"retry {tool_name}" in error
    assert "only the author-owned option change" in error
    assert "re-derives" in error


@pytest.mark.parametrize("tool_name", ["patch_source_options", "patch_node_options", "set_pipeline"])
def test_resolver_owned_review_rejection_names_authoring_and_resolution_tools(tool_name: str) -> None:
    error = _resolver_owned_interpretation_requirement_error(
        {
            "interpretation_requirements": [
                {
                    "kind": "pipeline_decision",
                    "user_term": "drop_raw_html_fields",
                    "draft": "Drop raw HTML.",
                    "status": None,
                }
            ]
        },
        tool_name=tool_name,
    )

    assert error is not None
    assert f"retry {tool_name}" in error
    assert _AUTHOR_OWNED_INTERPRETATION_REQUIREMENT_FIELDS_TEXT in error
    assert "request_interpretation_review" in error
    assert "resolve_interpretation_event" not in error
