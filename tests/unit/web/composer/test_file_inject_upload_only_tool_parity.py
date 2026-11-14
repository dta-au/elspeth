"""In the composer, ``file_inject`` content is a user-uploaded file on every authoring tool.

``file_inject`` writes configured content into row data, which a later prompt
template can read, and nothing reviews that content. So the only value the
composer accepts at ``files.<entry>.content`` is an ``inline_content`` marker
for a blob the user uploaded, and ``default_content`` (written into rows on a
miss) is not accepted at all. Three refusal causes follow, on every tool that
can write node options:

* a marker naming a blob that is not a user upload (planner-created, a
  pipeline output, or a fork copy of either);
* literal content;
* ``default_content`` in any form.

Each refusal steers the planner to the path that works, and each has a control
showing the same call accepted with a user-uploaded blob marker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobCreator, BlobRecord
from elspeth.contracts.enums import CreationModality
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools import ToolResult
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.sessions.models import blobs_table
from tests.unit.web.composer import test_blob_inline_tools as blob_tools
from tests.unit.web.composer import test_splice_transform_tool as splice_fixtures

# Reuse the session/blob fixture the blob-tool suite already owns.
blob_env = blob_tools.blob_env

_NODE_OPTION_TOOLS = ("upsert_node", "patch_node_options", "splice_transform", "set_pipeline")
_LLM_AUTHORED_MODALITIES = tuple(modality for modality in CreationModality if modality.requires_llm_provenance())
_MARKER_SHAPE = '{"blob_ref": "<blob_id>", "mode": "inline_content", "sha256": "<content_hash>"}'
_ERROR_CODE = "file_inject_content_not_uploaded"


def _marker(record: BlobRecord) -> dict[str, str]:
    assert record.content_hash is not None
    return {"blob_ref": str(record.id), "mode": "inline_content", "sha256": record.content_hash}


async def _run_on_this_thread(func: Callable[[], Any]) -> Any:
    """Stand in for the worker hand-off: the fixture's in-memory database is visible to this thread only."""
    return func()


async def _stored_blob_async(blob_env: dict[str, Any], *, created_by: BlobCreator, content: str, source_description: str) -> dict[str, str]:
    """Write a ready blob with ``BlobServiceImpl.create_blob``, the call the upload route makes, and return its marker."""
    service = BlobServiceImpl(blob_env["engine"], Path(blob_env["data_dir"]))
    with patch("elspeth.web.blobs.service.run_sync_in_worker", _run_on_this_thread):
        record = await service.create_blob(
            UUID(blob_env["session_id"]),
            "guidance.txt",
            content.encode(),
            "text/plain",
            created_by=created_by,
            source_description=source_description,
            session_operation_context=blob_env["operation"],
        )
    assert record.status == "ready"
    assert (record.created_by, record.creation_modality) == (created_by, CreationModality.VERBATIM)
    return _marker(record)


def _stored_blob(blob_env: dict[str, Any], *, created_by: BlobCreator, content: str, source_description: str) -> dict[str, str]:
    return asyncio.run(_stored_blob_async(blob_env, created_by=created_by, content=content, source_description=source_description))


def _upload(blob_env: dict[str, Any], *, content: str = "Check the totals.\n") -> dict[str, str]:
    """A file the user uploaded: ``created_by="user"``, as ``create_blob_upload`` writes it."""
    return _stored_blob(blob_env, created_by="user", content=content, source_description="uploaded")


def _composer_blob(blob_env: dict[str, Any], modality: CreationModality, *, content: str = "Check the totals.\n") -> dict[str, str]:
    """A blob the composer's ``create_blob`` tool wrote, with ``modality``.

    ``VERBATIM`` is the case where the content is text from the user's message:
    still ``created_by="assistant"``, so still not an upload.
    """
    llm_authored = modality.requires_llm_provenance()
    created = blob_tools._create_blob(blob_env, filename="guidance.txt", content=content, llm_authored=llm_authored)
    assert created.success is True
    blob_id = created.data["blob_id"]
    if llm_authored:
        with blob_env["engine"].begin() as conn:
            conn.execute(blobs_table.update().where(blobs_table.c.id == blob_id).values(creation_modality=modality.value))
    with blob_env["engine"].connect() as conn:
        row = conn.execute(blobs_table.select().where(blobs_table.c.id == blob_id)).one()
    assert (row.created_by, row.creation_modality) == ("assistant", modality.value)
    return {"blob_ref": blob_id, "mode": "inline_content", "sha256": created.data["content_hash"]}


def _not_uploaded_verbatim_blob(blob_env: dict[str, Any], kind: str) -> dict[str, str]:
    """A blob that records ``VERBATIM`` and is not a user upload.

    ``copy_blobs_for_fork`` keeps the source's ``created_by`` and relabels the
    copy ``VERBATIM`` with no provenance, so a fork copy of a planner-created
    blob is the row written here for ``fork_copy_of_composer_blob``.
    """
    if kind == "composer_create_blob_verbatim":
        return _composer_blob(blob_env, CreationModality.VERBATIM)
    if kind == "fork_copy_of_composer_blob":
        return _stored_blob(
            blob_env,
            created_by="assistant",
            content="Check the totals.\n",
            source_description=f"copied from session fork (original: {uuid4()})",
        )
    if kind == "pipeline_output":
        return _stored_blob(blob_env, created_by="pipeline", content="Check the totals.\n", source_description="pipeline output")
    raise AssertionError(f"unknown blob kind {kind!r}")


_NOT_UPLOADED_VERBATIM_KINDS = ("composer_create_blob_verbatim", "fork_copy_of_composer_blob", "pipeline_output")


def _blob(blob_env: dict[str, Any], modality: CreationModality, *, content: str = "Check the totals.\n") -> dict[str, str]:
    """A user upload for ``VERBATIM``; a composer-created blob for every LLM-authored modality."""
    if modality is CreationModality.VERBATIM:
        return _upload(blob_env, content=content)
    return _composer_blob(blob_env, modality, content=content)


def _options(content: object, **extra: object) -> dict[str, Any]:
    return {
        "schema": {"mode": "observed"},
        "mode": "select",
        "key_field": "kind",
        "output_field": "guidance",
        "files": {"invoice": {"content": content, "format": "text"}},
        **extra,
    }


def _node(node_id: str, options: dict[str, Any]) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin="file_inject",
        input="rows",
        on_success="guided",
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _state(*nodes: NodeSpec) -> CompositionState:
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": "/tmp/input.csv", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=nodes,
        edges=(),
        outputs=(
            OutputSpec(
                name="guided",
                plugin="json",
                options={"path": "/tmp/output.jsonl", "format": "jsonl", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _set_pipeline_arguments(blob_env: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
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
                "id": "inject",
                "node_type": "transform",
                "plugin": "file_inject",
                "input": "rows",
                "on_success": "guided",
                "on_error": "discard",
                "options": options,
            }
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "guided",
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


def _execute(tool: str, arguments: dict[str, Any], state: CompositionState, blob_env: dict[str, Any]) -> ToolResult:
    return blob_tools.execute_tool(
        tool,
        arguments,
        state,
        create_catalog_service(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
    )


def _run(tool: str, options: dict[str, Any], blob_env: dict[str, Any]) -> tuple[ToolResult, CompositionState, str]:
    """Author ``options`` onto a ``file_inject`` node with ``tool``.

    ``upsert_node``, ``splice_transform`` and ``set_pipeline`` create the node.
    ``patch_node_options`` needs one to exist, and a node can only exist with
    an uploaded file attached, so its starting node holds a second upload.
    """
    if tool == "upsert_node":
        state = _state()
        node_id = "inject"
        arguments: dict[str, Any] = {
            "id": node_id,
            "node_type": "transform",
            "plugin": "file_inject",
            "input": "rows",
            "on_success": "guided",
            "on_error": "discard",
            "options": options,
        }
    elif tool == "patch_node_options":
        node_id = "inject"
        state = _state(_node(node_id, _options(_blob(blob_env, CreationModality.VERBATIM, content="Earlier upload.\n"))))
        arguments = {"node_id": node_id, "patch": {key: value for key, value in options.items() if key != "schema"}}
    elif tool == "splice_transform":
        state = splice_fixtures._state()
        node_id = "inserted"
        arguments = {
            "predecessor_id": "before",
            "successor_id": "after",
            "node": {"id": node_id, "plugin": "file_inject", "options": options, "on_error": "discard"},
        }
    elif tool == "set_pipeline":
        state = blob_tools._empty_state()
        node_id = "inject"
        arguments = _set_pipeline_arguments(blob_env, options)
    else:
        raise AssertionError(f"unknown tool {tool!r}")
    return _execute(tool, arguments, state, blob_env), state, node_id


def _assert_refused(result: ToolResult, state: CompositionState, *, tool: str, node_id: str, option_path: str) -> str:
    assert result.success is False
    assert result.updated_state is state
    # A state with no consumer yet also reports its own graph errors; the refusal is the coded one.
    [error] = [error for error in result.validation.errors if error.error_code == _ERROR_CODE]
    assert f"{tool} cannot set file_inject node '{node_id}' option '{option_path}': " in error.message
    return error.message


def _assert_steers_to_an_upload(message: str, *, node_id: str) -> None:
    """Each of the three steps, the concrete option path and the marker shape."""
    step_1 = message.index("(1) tell the user to save the text as a file and upload it")
    step_2 = message.index("(2) once it is uploaded, find it with list_blobs / get_blob_metadata")
    step_3 = message.index("(3) attach it")
    assert step_1 < step_2 < step_3
    assert f"wire_blob_inline_ref at 'node:{node_id}.options.files.invoice.content'" in message
    assert _MARKER_SHAPE in message
    # The rule is this plugin's: nothing may suggest planner-created files are refused elsewhere.
    assert "In the composer, file_inject content must be a file the user uploaded" in message


def _assert_uploaded_marker_stored(result: ToolResult, node_id: str, marker: dict[str, str]) -> None:
    assert result.success is True, [error.message for error in result.validation.errors]
    [node] = [node for node in result.updated_state.nodes if node.id == node_id]
    assert deep_thaw(node.options)["files"]["invoice"]["content"] == marker


# ── cause 1: a marker naming an LLM-authored blob ───────────────────────────


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
@pytest.mark.parametrize("tool", _NODE_OPTION_TOOLS)
def test_llm_authored_blob_refused_for_file_inject_content(blob_env: dict[str, Any], tool: str, modality: CreationModality) -> None:
    marker = _blob(blob_env, modality)

    result, state, node_id = _run(tool, _options(marker), blob_env)

    message = _assert_refused(result, state, tool=tool, node_id=node_id, option_path="files.invoice.content")
    assert f"blob '{marker['blob_ref']}' is not a file the user uploaded" in message
    _assert_steers_to_an_upload(message, node_id=node_id)
    assert "Check the totals." not in message


@pytest.mark.parametrize("tool", _NODE_OPTION_TOOLS)
def test_user_uploaded_blob_accepted_for_file_inject_content(blob_env: dict[str, Any], tool: str) -> None:
    """Control for every refusal below, and the new-node path: one call carrying the uploaded marker."""
    marker = _blob(blob_env, CreationModality.VERBATIM)

    result, _state_before, node_id = _run(tool, _options(marker), blob_env)

    _assert_uploaded_marker_stored(result, node_id, marker)


@pytest.mark.parametrize("modality", _LLM_AUTHORED_MODALITIES, ids=lambda modality: modality.value)
def test_wire_blob_inline_ref_refuses_llm_authored_blob_for_file_inject_content(
    blob_env: dict[str, Any], modality: CreationModality
) -> None:
    uploaded = _blob(blob_env, CreationModality.VERBATIM, content="Earlier upload.\n")
    state = _state(_node("inject", _options(uploaded)))
    authored = _blob(blob_env, modality)

    result = _execute(
        "wire_blob_inline_ref",
        {"field_path": "node:inject.options.files.invoice.content", "blob_id": authored["blob_ref"]},
        state,
        blob_env,
    )

    assert result.success is False
    assert result.updated_state is state
    [error] = result.validation.errors
    assert "wire_blob_inline_ref cannot set file_inject node 'inject' option 'files.invoice.content': " in error.message
    assert f"blob '{authored['blob_ref']}' is not a file the user uploaded" in error.message
    assert error.error_code == _ERROR_CODE
    _assert_steers_to_an_upload(error.message, node_id="inject")


@pytest.mark.parametrize("option_path", ["files", "files.invoice", "files.invoice.content"])
def test_wire_blob_inline_ref_refuses_llm_authored_blob_anywhere_beneath_files(blob_env: dict[str, Any], option_path: str) -> None:
    uploaded = _blob(blob_env, CreationModality.VERBATIM, content="Earlier upload.\n")
    state = _state(_node("inject", _options(uploaded)))
    authored = _blob(blob_env, CreationModality.LLM_GENERATED)

    result = _execute(
        "wire_blob_inline_ref", {"field_path": f"node:inject.options.{option_path}", "blob_id": authored["blob_ref"]}, state, blob_env
    )

    assert result.success is False
    assert result.updated_state is state
    assert f"cannot set file_inject node 'inject' option '{option_path}': " in result.validation.errors[0].message


def test_wire_blob_inline_ref_accepts_user_uploaded_blob_for_file_inject_content(blob_env: dict[str, Any]) -> None:
    earlier = _blob(blob_env, CreationModality.VERBATIM, content="Earlier upload.\n")
    state = _state(_node("inject", _options(earlier)))
    uploaded = _blob(blob_env, CreationModality.VERBATIM)

    result = _execute(
        "wire_blob_inline_ref",
        {"field_path": "node:inject.options.files.invoice.content", "blob_id": uploaded["blob_ref"]},
        state,
        blob_env,
    )

    _assert_uploaded_marker_stored(result, "inject", uploaded)


# ── cause 1, continued: VERBATIM blobs that are not user uploads ─────────────


@pytest.mark.parametrize("kind", _NOT_UPLOADED_VERBATIM_KINDS)
@pytest.mark.parametrize("tool", _NODE_OPTION_TOOLS)
def test_verbatim_blob_the_user_did_not_upload_refused_for_file_inject_content(blob_env: dict[str, Any], tool: str, kind: str) -> None:
    """Creator and modality are read as a pair: VERBATIM alone does not make a blob an upload."""
    marker = _not_uploaded_verbatim_blob(blob_env, kind)

    result, state, node_id = _run(tool, _options(marker), blob_env)

    message = _assert_refused(result, state, tool=tool, node_id=node_id, option_path="files.invoice.content")
    assert f"blob '{marker['blob_ref']}' is not a file the user uploaded" in message
    assert "LLM-authored" not in message and "created by the composer" not in message
    _assert_steers_to_an_upload(message, node_id=node_id)


@pytest.mark.parametrize("kind", _NOT_UPLOADED_VERBATIM_KINDS)
def test_wire_blob_inline_ref_refuses_verbatim_blob_the_user_did_not_upload(blob_env: dict[str, Any], kind: str) -> None:
    state = _state(_node("inject", _options(_upload(blob_env, content="Earlier upload.\n"))))
    marker = _not_uploaded_verbatim_blob(blob_env, kind)

    result = _execute(
        "wire_blob_inline_ref", {"field_path": "node:inject.options.files.invoice.content", "blob_id": marker["blob_ref"]}, state, blob_env
    )

    assert result.success is False
    assert result.updated_state is state
    [error] = result.validation.errors
    assert error.error_code == _ERROR_CODE
    assert f"blob '{marker['blob_ref']}' is not a file the user uploaded" in error.message
    _assert_steers_to_an_upload(error.message, node_id="inject")


@pytest.mark.parametrize("tool", (*_NODE_OPTION_TOOLS, "wire_blob_inline_ref"))
def test_session_fork_copy_of_a_user_upload_is_still_accepted(blob_env: dict[str, Any], tool: str) -> None:
    """A fork copy keeps its source's creator, so a copied upload is still an upload."""
    marker = _stored_blob(
        blob_env, created_by="user", content="Check the totals.\n", source_description=f"copied from session fork (original: {uuid4()})"
    )

    if tool == "wire_blob_inline_ref":
        state = _state(_node("inject", _options(_upload(blob_env, content="Earlier upload.\n"))))
        result = _execute(tool, {"field_path": "node:inject.options.files.invoice.content", "blob_id": marker["blob_ref"]}, state, blob_env)
        node_id = "inject"
    else:
        result, _state_before, node_id = _run(tool, _options(marker), blob_env)

    _assert_uploaded_marker_stored(result, node_id, marker)


@pytest.mark.parametrize("tool", (*_NODE_OPTION_TOOLS, "wire_blob_inline_ref"))
def test_user_upload_the_composer_rewrote_with_its_own_words_is_refused(blob_env: dict[str, Any], tool: str) -> None:
    """An agent write is an agent write, even when the new bytes are a substring of the user's message.

    ``update_blob`` with content copied from the user's message records
    ``VERBATIM`` (no LLM provenance), so modality alone would still call the
    rewritten upload an upload. The creator flips to ``assistant`` at the rewrite.
    """
    marker = _upload(blob_env, content="Original upload.\n")
    rewritten = "Check the totals.\n"
    updated = blob_tools.execute_tool(
        "update_blob",
        {"blob_id": marker["blob_ref"], "content": rewritten},
        blob_tools._empty_state(),
        blob_tools._catalog(),
        data_dir=blob_env["data_dir"],
        session_engine=blob_env["engine"],
        session_id=blob_env["session_id"],
        session_operation_context=blob_env["operation"],
        session_operation_authority=blob_env["authority"],
        user_message_id="user-message-1",
        user_message_content=f"Use this exact content:\n{rewritten}",
    )
    assert updated.success is True, [error.message for error in updated.validation.errors]
    with blob_env["engine"].connect() as conn:
        row = conn.execute(blobs_table.select().where(blobs_table.c.id == marker["blob_ref"])).one()
    assert (row.created_by, row.creation_modality) == ("assistant", CreationModality.VERBATIM.value)
    rewritten_marker = {"blob_ref": marker["blob_ref"], "mode": "inline_content", "sha256": updated.data["content_hash"]}

    if tool == "wire_blob_inline_ref":
        state = _state(_node("inject", _options(_upload(blob_env, content="Earlier upload.\n"))))
        result = _execute(
            tool, {"field_path": "node:inject.options.files.invoice.content", "blob_id": rewritten_marker["blob_ref"]}, state, blob_env
        )
        node_id = "inject"
    else:
        result, state, node_id = _run(tool, _options(rewritten_marker), blob_env)

    message = _assert_refused(result, state, tool=tool, node_id=node_id, option_path="files.invoice.content")
    assert f"blob '{marker['blob_ref']}' is not a file the user uploaded" in message


@pytest.mark.parametrize("tool", _NODE_OPTION_TOOLS)
def test_marker_for_a_blob_outside_the_session_is_deferred_at_authoring(blob_env: dict[str, Any], tool: str) -> None:
    """No record to judge here; /validate reports the blob missing (see the readiness tests)."""
    marker = {"blob_ref": str(uuid4()), "mode": "inline_content", "sha256": "0" * 64}

    result, _state_before, node_id = _run(tool, _options(marker), blob_env)

    assert all(error.error_code != _ERROR_CODE for error in result.validation.errors)
    _assert_uploaded_marker_stored(result, node_id, marker)


# ── cause 2: literal content ────────────────────────────────────────────────


@pytest.mark.parametrize("tool", _NODE_OPTION_TOOLS)
def test_literal_content_refused_for_file_inject(blob_env: dict[str, Any], tool: str) -> None:
    result, state, node_id = _run(tool, _options("Check the totals against the line items.\n"), blob_env)

    message = _assert_refused(result, state, tool=tool, node_id=node_id, option_path="files.invoice.content")
    assert "it holds literal content, not an uploaded file" in message
    _assert_steers_to_an_upload(message, node_id=node_id)
    assert "Check the totals against the line items." not in message

    # The same call with the user's upload attached is the path that works.
    marker = _blob(blob_env, CreationModality.VERBATIM)
    repaired, _state_before, node_id = _run(tool, _options(marker), blob_env)
    _assert_uploaded_marker_stored(repaired, node_id, marker)


@pytest.mark.parametrize("tool", _NODE_OPTION_TOOLS)
def test_literal_content_refused_in_any_entry(blob_env: dict[str, Any], tool: str) -> None:
    """One uploaded entry does not carry a literal sibling past the guard."""
    options = _options(_blob(blob_env, CreationModality.VERBATIM))
    options["files"]["contract"] = {"content": "Check the parties.\n", "format": "text"}

    result, state, node_id = _run(tool, options, blob_env)

    message = _assert_refused(result, state, tool=tool, node_id=node_id, option_path="files.contract.content")
    assert f"wire_blob_inline_ref at 'node:{node_id}.options.files.contract.content'" in message


def test_patch_adding_a_literal_entry_beside_an_upload_is_refused(blob_env: dict[str, Any]) -> None:
    """The patch is judged on the options the node would hold, not on the patch alone."""
    uploaded = _upload(blob_env)
    state = _state(_node("inject", _options(uploaded)))
    patch = {
        "files": {"invoice": {"content": uploaded, "format": "text"}, "contract": {"content": "Check the parties.\n", "format": "text"}}
    }

    result = _execute("patch_node_options", {"node_id": "inject", "patch": patch}, state, blob_env)

    message = _assert_refused(result, state, tool="patch_node_options", node_id="inject", option_path="files.contract.content")
    assert "wire_blob_inline_ref at 'node:inject.options.files.contract.content'" in message


@pytest.mark.parametrize("content_kind", ["uploaded_marker", "literal"])
@pytest.mark.parametrize("entry_name", ["v1.2", "Type A", "2024"])
@pytest.mark.parametrize("tool", _NODE_OPTION_TOOLS)
def test_entry_name_no_inline_reference_can_address_is_refused(
    blob_env: dict[str, Any], tool: str, entry_name: str, content_kind: str
) -> None:
    """An inline-reference field path cannot name such an entry, so no upload can ever be attached to it.

    Without this refusal a marker beneath the entry is stored and ``/validate``
    then fails on the field path instead of telling the planner what to change.
    """
    options = _options(_upload(blob_env))
    content: object = _upload(blob_env, content="Check the parties.\n") if content_kind == "uploaded_marker" else "Check the parties.\n"
    options["files"][entry_name] = {"content": content, "format": "text"}

    result, state, node_id = _run(tool, options, blob_env)

    message = _assert_refused(result, state, tool=tool, node_id=node_id, option_path=f"files.{entry_name}")
    assert f"an uploaded file cannot be attached to an entry named '{entry_name}'" in message
    assert "an entry name must start with a letter or '_' and contain only letters, digits, '_' and '-'" in message
    assert "Give the entry a name of that form, then attach the user's upload to it." in message
    assert "wire_blob_inline_ref at" not in message


@pytest.mark.parametrize("entry_name", ["invoice", "_x", "Type-A_2"])
def test_addressable_entry_names_agree_with_the_inline_reference_grammar(blob_env: dict[str, Any], entry_name: str) -> None:
    """The guard's name rule is the grammar ``wire_blob_inline_ref`` itself accepts."""
    earlier = _upload(blob_env, content="Earlier upload.\n")
    options = _options(earlier)
    options["files"][entry_name] = {"content": earlier, "format": "text"}
    state = _state(_node("inject", options))
    uploaded = _upload(blob_env)

    result = _execute(
        "wire_blob_inline_ref",
        {"field_path": f"node:inject.options.files.{entry_name}.content", "blob_id": uploaded["blob_ref"]},
        state,
        blob_env,
    )

    assert result.success is True, [error.message for error in result.validation.errors]


@pytest.mark.parametrize("entry_name", ["v1.2", "Type A", "2024"])
def test_wire_blob_inline_ref_cannot_address_the_names_the_guard_refuses(blob_env: dict[str, Any], entry_name: str) -> None:
    state = _state(_node("inject", _options(_upload(blob_env, content="Earlier upload.\n"))))
    uploaded = _upload(blob_env)

    result = _execute(
        "wire_blob_inline_ref",
        {"field_path": f"node:inject.options.files.{entry_name}.content", "blob_id": uploaded["blob_ref"]},
        state,
        blob_env,
    )

    assert result.success is False
    assert result.updated_state is state


@pytest.mark.parametrize("tool", _NODE_OPTION_TOOLS)
def test_non_inline_marker_is_not_an_upload(blob_env: dict[str, Any], tool: str) -> None:
    """A ``bind_source`` marker is a mapping, but it is not an attached upload."""
    uploaded = _blob(blob_env, CreationModality.VERBATIM)

    result, state, node_id = _run(tool, _options({"blob_ref": uploaded["blob_ref"], "mode": "bind_source"}), blob_env)

    _assert_refused(result, state, tool=tool, node_id=node_id, option_path="files.invoice.content")


# ── cause 3: default_content in any form ────────────────────────────────────


def _assert_steers_away_from_default_content(message: str) -> None:
    assert "default_content is not accepted in the composer" in message
    assert message.endswith("Use on_miss: null or on_miss: fail and handle the miss downstream.")
    # In mode select a miss names no entry, so an extra files entry is not a fallback.
    assert "files entry" not in message
    assert "dta-au/elspeth#361" not in message


@pytest.mark.parametrize("form", ["literal", "uploaded_marker", "llm_authored_marker"])
@pytest.mark.parametrize("tool", _NODE_OPTION_TOOLS)
def test_default_content_refused_for_file_inject(blob_env: dict[str, Any], tool: str, form: str) -> None:
    uploaded = _blob(blob_env, CreationModality.VERBATIM)
    default_content: object
    if form == "literal":
        default_content = "Planner fallback wording.\n"
    elif form == "uploaded_marker":
        default_content = _blob(blob_env, CreationModality.VERBATIM, content="Uploaded fallback wording.\n")
    else:
        default_content = _blob(blob_env, CreationModality.LLM_GENERATED, content="Drafted fallback wording.\n")

    result, state, node_id = _run(tool, _options(uploaded, on_miss="default", default_content=default_content), blob_env)

    message = _assert_refused(result, state, tool=tool, node_id=node_id, option_path="default_content")
    _assert_steers_away_from_default_content(message)
    assert "fallback wording" not in message

    # The same node with on_miss: null is the path that works.
    repaired, _state_before, node_id = _run(tool, _options(uploaded, on_miss="null"), blob_env)
    _assert_uploaded_marker_stored(repaired, node_id, uploaded)


def test_wire_blob_inline_ref_refuses_default_content_for_file_inject(blob_env: dict[str, Any]) -> None:
    uploaded = _blob(blob_env, CreationModality.VERBATIM)
    state = _state(_node("inject", _options(uploaded)))
    fallback = _blob(blob_env, CreationModality.VERBATIM, content="Uploaded fallback wording.\n")

    result = _execute(
        "wire_blob_inline_ref", {"field_path": "node:inject.options.default_content", "blob_id": fallback["blob_ref"]}, state, blob_env
    )

    assert result.success is False
    assert result.updated_state is state
    [error] = result.validation.errors
    assert "wire_blob_inline_ref cannot set file_inject node 'inject' option 'default_content': " in error.message
    assert error.error_code == _ERROR_CODE
    _assert_steers_away_from_default_content(error.message)


# ── what the guard leaves alone ─────────────────────────────────────────────


def test_llm_authored_blob_still_allowed_on_reference_join(blob_env: dict[str, Any]) -> None:
    """``reference_join.reference_content`` is unguarded in this change; pinned so it does not move silently."""
    authored = _blob(blob_env, CreationModality.LLM_GENERATED, content="code,label\nA,Alpha\n")
    arguments = {
        "id": "lookup",
        "node_type": "transform",
        "plugin": "reference_join",
        "input": "rows",
        "on_success": "guided",
        "on_error": "discard",
        "options": {
            "schema": {"mode": "observed"},
            "reference_content": authored,
            "reference_format": "csv",
            "key_field": "code",
            "reference_key_name": "code",
            "output": "label",
            "on_miss": "null",
        },
    }

    result = _execute("upsert_node", arguments, _state(), blob_env)

    assert all("cannot set" not in error.message and "LLM-authored" not in error.message for error in result.validation.errors)
    assert all(error.error_code != _ERROR_CODE for error in result.validation.errors)


def test_literal_text_in_an_llm_prompt_is_untouched_by_the_file_inject_rule(blob_env: dict[str, Any]) -> None:
    """Planner-written prompt text stays allowed behind its own review."""
    result = _execute(
        "patch_node_options",
        {"node_id": "classify", "patch": {"system_prompt": "You are careful."}},
        blob_tools._inline_ref_state(),
        blob_env,
    )

    assert all(error.error_code != _ERROR_CODE for error in result.validation.errors)
    assert result.success is True, [error.message for error in result.validation.errors]


# ── the planner surface sees codes and static guidance, never the message ───


def test_planner_feedback_guidance_carries_the_working_path() -> None:
    """``emit_pipeline_proposal`` feedback withholds validator messages, so the code's static guidance must steer alone."""
    from elspeth.web.composer.error_codes import REGISTERED_ERROR_CODES
    from elspeth.web.composer.tools.generation import _CLOSED_VALIDATION_ERROR_CODES, explain_validation_code
    from elspeth.web.execution._validation_materialization import FILE_INJECT_UPLOAD_MARKER_SHAPE

    assert _ERROR_CODE in REGISTERED_ERROR_CODES
    assert _ERROR_CODE in _CLOSED_VALIDATION_ERROR_CODES
    guidance = explain_validation_code(_ERROR_CODE)
    assert guidance is not None
    explanation, suggested_fix = guidance
    assert "In the composer, file_inject content must be a file the user uploaded" in explanation
    assert "Tell the user to save the text as a file and upload it" in suggested_fix
    assert "list_blobs / get_blob_metadata" in suggested_fix
    assert "wire_blob_inline_ref at node:<node_id>.options.files.<entry_name>.content" in suggested_fix
    assert FILE_INJECT_UPLOAD_MARKER_SHAPE == _MARKER_SHAPE
    assert _MARKER_SHAPE in suggested_fix
    assert "use on_miss: null or on_miss: fail" in suggested_fix
