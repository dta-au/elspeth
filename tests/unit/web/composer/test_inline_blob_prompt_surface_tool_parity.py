"""Every node-option authoring tool applies the same prompt file policy.

Scalar prompt files bind and open review, independent of creator. Model and
structured query positions remain refused through every authoring path.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools._common import rejected_component_prefix
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.composer import test_blob_inline_tools as blob_tools
from tests.unit.web.composer import test_splice_transform_tool as splice_fixtures

# Reuse the session/blob fixture the blob-tool suite already owns.
blob_env = blob_tools.blob_env

_SCALAR_FIELDS = ("prompt_template", "system_prompt", "queries.q.template", "queries.0.template")
_REFUSED_FIELDS = ("model", "queries.q", "queries")
_NON_SCALAR_REFUSAL = "File-backed LLM prompts bind only to prompt_template, system_prompt, or a query template."
_LLM_AUTHORED_MODALITIES = tuple(modality for modality in CreationModality if modality.requires_llm_provenance())


def _blob(blob_env: dict[str, Any], modality: CreationModality) -> dict[str, str]:
    """Create a ready blob with ``modality`` and return its inline_content marker."""
    llm_authored = modality.requires_llm_provenance()
    created = blob_tools._create_blob(blob_env, content="Prompt {{ row.x }}", llm_authored=llm_authored)
    assert created.success is True
    blob_id = created.data["blob_id"]
    if llm_authored:
        with blob_env["engine"].begin() as conn:
            conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(creation_modality=modality.value))
    return {"blob_ref": blob_id, "mode": "inline_content", "sha256": created.data["content_hash"]}


def _options(field: str, marker: dict[str, str]) -> dict[str, Any]:
    options = dict(deep_thaw(blob_tools._inline_ref_state().nodes[0].options))
    if field.startswith("queries"):
        options["max_capacity_retry_seconds"] = 30
    if field in ("prompt_template", "system_prompt", "model", "queries"):
        options[field] = marker
    elif field == "queries.q.template":
        options["queries"] = {"q": {"input_fields": {"x": "x"}, "template": marker}}
    elif field == "queries.0.template":
        options["queries"] = [{"name": "q", "input_fields": {"x": "x"}, "template": marker}]
    elif field == "queries.q":
        options["queries"] = {"q": marker}
    else:
        raise AssertionError(f"unknown guarded field {field!r}")
    return options


def _set_pipeline_arguments(blob_env: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """A whole pipeline with one ``llm`` node; paths sit inside the session's allowed roots."""
    data_dir = Path(blob_env["data_dir"])
    source_dir = data_dir / "blobs" / blob_env["session_id"]
    output_dir = data_dir / "outputs" / blob_env["session_id"]
    source_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return {
        "source": {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": str(source_dir / "input.csv"), "schema": {"mode": "observed"}},
            "on_validation_failure": "discard",
        },
        "nodes": [
            {
                "id": "classify",
                "node_type": "transform",
                "plugin": "llm",
                "input": "rows",
                "on_success": "classified",
                "on_error": "discard",
                "options": options,
            }
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "classified",
                "plugin": "json",
                "options": {
                    "path": str(output_dir / "out.jsonl"),
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
    }


def _run(
    tool: str, field: str, marker: dict[str, str], blob_env: dict[str, Any], *, structured: bool = False
) -> tuple[ToolResult, CompositionState, str]:
    options = _options(field, marker)
    if structured:
        options["prompt_template_parts"] = [{"kind": "text", "text": "Original structured prompt"}]
    catalog = blob_tools._catalog()
    if tool == "upsert_node":
        state = blob_tools._inline_ref_state()
        if structured:
            node = state.nodes[0]
            state = replace(
                state, nodes=(replace(node, options={**node.options, "prompt_template_parts": options["prompt_template_parts"]}),)
            )
        node_id = "classify"
        arguments: dict[str, Any] = {
            "id": node_id,
            "node_type": "transform",
            "plugin": "llm",
            "input": "rows",
            "on_success": "classified",
            "on_error": "discard",
            "options": options,
        }
    elif tool == "patch_node_options":
        state = blob_tools._inline_ref_state()
        if structured:
            node = state.nodes[0]
            state = replace(
                state, nodes=(replace(node, options={**node.options, "prompt_template_parts": options["prompt_template_parts"]}),)
            )
        node_id = "classify"
        root = field.split(".")[0]
        patch = {root: options[root]}
        if root == "queries":
            patch["max_capacity_retry_seconds"] = 30
        arguments = {"node_id": node_id, "patch": patch}
    elif tool == "splice_transform":
        state = splice_fixtures._state()
        node_id = "inserted"
        catalog = create_catalog_service()
        arguments = {
            "predecessor_id": "before",
            "successor_id": "after",
            "node": {"id": node_id, "plugin": "llm", "options": options, "on_error": "discard"},
        }
    elif tool == "set_pipeline":
        state = blob_tools._empty_state()
        node_id = "classify"
        arguments = _set_pipeline_arguments(blob_env, options)
    else:
        raise AssertionError(f"unknown tool {tool!r}")
    result = blob_tools.execute_tool(
        tool,
        arguments,
        state,
        catalog,
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
    )
    return result, state, node_id


_TOOLS = ("upsert_node", "patch_node_options", "splice_transform", "set_pipeline")


@pytest.mark.parametrize("tool", _TOOLS)
def test_structured_prompt_refuses_manually_supplied_marker(blob_env: dict[str, Any], tool: str) -> None:
    marker = _blob(blob_env, CreationModality.VERBATIM)

    result, state, _node_id = _run(tool, "prompt_template", marker, blob_env, structured=True)

    assert result.success is False
    assert result.updated_state is state
    assert result.validation.errors[0].error_code == "prompt_template_parts_required"
    assert "interpretation slots" in result.validation.errors[0].message


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _REFUSED_FIELDS)
@pytest.mark.parametrize("tool", _TOOLS)
def test_authoring_tool_refuses_llm_authored_blob_in_non_scalar_position(
    blob_env: dict[str, Any], tool: str, field: str, modality: CreationModality
) -> None:
    marker = _blob(blob_env, modality)

    result, state, node_id = _run(tool, field, marker, blob_env)

    assert result.success is False
    assert result.updated_state is state
    [error] = result.validation.errors
    # set_pipeline names the rejected component with its per-component prefix.
    component_prefix = rejected_component_prefix(f"node:{node_id}") if tool == "set_pipeline" else ""
    assert error.message == f"{component_prefix}{_NON_SCALAR_REFUSAL}"
    assert error.error_code == "llm_authored_inline_blob_content"
    if tool == "set_pipeline":
        assert error.rejected_component == f"node:{node_id}"
    assert "Prompt {{ row.x }}" not in error.message


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _SCALAR_FIELDS)
@pytest.mark.parametrize("tool", _TOOLS)
def test_authoring_tool_binds_llm_authored_scalar_prompt_file_for_review(
    blob_env: dict[str, Any], tool: str, field: str, modality: CreationModality
) -> None:
    marker = _blob(blob_env, modality)

    result, _state, node_id = _run(tool, field, marker, blob_env)

    assert result.success is True, [error.message for error in result.validation.errors]
    assert result.updated_state is not None
    node = next(node for node in result.updated_state.nodes if node.id == node_id)
    requirements = deep_thaw(node.options["interpretation_requirements"])
    assert any(item["kind"] == "llm_prompt_template" and item["status"] == "pending" for item in requirements)


@pytest.mark.parametrize("field", _SCALAR_FIELDS)
@pytest.mark.parametrize("tool", _TOOLS)
def test_authoring_tool_binds_user_uploaded_scalar_prompt_file_for_review(blob_env: dict[str, Any], tool: str, field: str) -> None:
    marker = _blob(blob_env, CreationModality.VERBATIM)

    result, _state, node_id = _run(tool, field, marker, blob_env)

    assert result.success is True, [error.message for error in result.validation.errors]
    assert result.updated_state is not None
    node = next(node for node in result.updated_state.nodes if node.id == node_id)
    requirements = deep_thaw(node.options["interpretation_requirements"])
    assert any(item["kind"] == "llm_prompt_template" and item["status"] == "pending" for item in requirements)


@pytest.mark.parametrize("field", _REFUSED_FIELDS)
@pytest.mark.parametrize("tool", _TOOLS)
def test_authoring_tool_refuses_uploaded_file_in_non_scalar_position(blob_env: dict[str, Any], tool: str, field: str) -> None:
    marker = _blob(blob_env, CreationModality.VERBATIM)

    result, state, node_id = _run(tool, field, marker, blob_env)

    assert result.success is False
    assert result.updated_state is state
    [error] = result.validation.errors
    component_prefix = rejected_component_prefix(f"node:{node_id}") if tool == "set_pipeline" else ""
    assert error.message == f"{component_prefix}{_NON_SCALAR_REFUSAL}"
    assert error.error_code == "llm_authored_inline_blob_content"


@pytest.mark.parametrize("tool", _TOOLS)
def test_authoring_tool_leaves_llm_authored_blob_outside_the_prompt_surface_alone(blob_env: dict[str, Any], tool: str) -> None:
    """``response_field`` is not a prompt surface: the modality guard does not fire there."""
    marker = _blob(blob_env, CreationModality.LLM_GENERATED)
    options = _options("prompt_template", marker)
    options["prompt_template"] = "Placeholder"
    options["response_field"] = marker
    if tool == "splice_transform":
        state = splice_fixtures._state()
    elif tool == "set_pipeline":
        state = blob_tools._empty_state()
    else:
        state = blob_tools._inline_ref_state()
    if tool == "upsert_node":
        arguments: dict[str, Any] = {
            "id": "classify",
            "node_type": "transform",
            "plugin": "llm",
            "input": "rows",
            "on_success": "classified",
            "on_error": "discard",
            "options": options,
        }
    elif tool == "patch_node_options":
        arguments = {"node_id": "classify", "patch": {"response_field": marker}}
    elif tool == "set_pipeline":
        arguments = _set_pipeline_arguments(blob_env, options)
    else:
        arguments = {
            "predecessor_id": "before",
            "successor_id": "after",
            "node": {"id": "inserted", "plugin": "llm", "options": options, "on_error": "discard"},
        }
    outcome = blob_tools.execute_tool(
        tool,
        arguments,
        state,
        create_catalog_service() if tool == "splice_transform" else blob_tools._catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
    )

    assert all("LLM-authored" not in error.message for error in outcome.validation.errors)
