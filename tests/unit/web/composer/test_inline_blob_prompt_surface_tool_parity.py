"""Every node-option authoring tool refuses an LLM-authored blob in an llm prompt surface or model.

``wire_blob_inline_ref`` refuses to bind an LLM-authored blob into an ``llm``
node's prompt surface or model (finding #1). A red-team pass showed the refusal
covered one of several authoring tools: ``upsert_node``, ``patch_node_options``,
``splice_transform`` and ``set_pipeline`` accept a hand-authored widened ``inline_content``
marker, and plugin prevalidation withholds it as a deferred value, so the
planner could route around the wire tool. Each of those tools now refuses with
the wire tool's repairable text, through the predicate /validate and run
admission share. User-uploaded (verbatim) blobs are the accepted control.
"""

from __future__ import annotations

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

_GUARDED_FIELDS = ("prompt_template", "system_prompt", "model", "queries.q.template", "queries.q", "queries")
# Plugin prevalidation withholds a marker only as a whole top-level option, so
# only these fields can reach a successful write for the verbatim control. (A
# whole-``queries`` marker is also withheld, but the multi-query provider policy
# then refuses the node for an unrelated reason.)
_TOP_LEVEL_FIELDS = frozenset({"prompt_template", "system_prompt", "model"})
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
    if field in ("prompt_template", "system_prompt", "model", "queries"):
        options[field] = marker
    elif field == "queries.q.template":
        options["queries"] = {"q": {"input_fields": {"x": "x"}, "template": marker}}
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


def _run(tool: str, field: str, marker: dict[str, str], blob_env: dict[str, Any]) -> tuple[ToolResult, CompositionState, str]:
    options = _options(field, marker)
    catalog = blob_tools._catalog()
    if tool == "upsert_node":
        state = blob_tools._inline_ref_state()
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
        node_id = "classify"
        root = field.split(".")[0]
        arguments = {"node_id": node_id, "patch": {root: options[root]}}
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


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("field", _GUARDED_FIELDS)
@pytest.mark.parametrize("tool", _TOOLS)
def test_authoring_tool_refuses_llm_authored_blob_marker_in_llm_prompt_surface(
    blob_env: dict[str, Any], tool: str, field: str, modality: CreationModality
) -> None:
    marker = _blob(blob_env, modality)

    result, state, node_id = _run(tool, field, marker, blob_env)

    assert result.success is False
    assert result.updated_state is state
    [error] = result.validation.errors
    # set_pipeline names the rejected component with its per-component prefix.
    component_prefix = rejected_component_prefix(f"node:{node_id}") if tool == "set_pipeline" else ""
    assert error.message.startswith(
        f"{component_prefix}{tool} cannot wire LLM-authored blob '{marker['blob_ref']}' into LLM node '{node_id}' option '{field}': "
    )
    if tool == "set_pipeline":
        assert error.rejected_component == f"node:{node_id}"
    assert "Prompt {{ row.x }}" not in error.message


@pytest.mark.parametrize("field", _GUARDED_FIELDS)
@pytest.mark.parametrize("tool", _TOOLS)
def test_authoring_tool_does_not_refuse_user_uploaded_blob_marker_in_llm_prompt_surface(
    blob_env: dict[str, Any], tool: str, field: str
) -> None:
    marker = _blob(blob_env, CreationModality.VERBATIM)

    result, _state, _node_id = _run(tool, field, marker, blob_env)

    assert all("LLM-authored" not in error.message for error in result.validation.errors)
    if field in _TOP_LEVEL_FIELDS and tool != "splice_transform":
        assert result.success is True, [error.message for error in result.validation.errors]


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
