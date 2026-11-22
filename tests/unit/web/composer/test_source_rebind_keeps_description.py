"""Rebinding a source to an uploaded file keeps the source's stored description.

``set_source_from_blob`` and ``set_source_from_blobs`` take no description, so
a rebind that rebuilt the source without one erased the Spec-tab sentence
and gave the composer no way to put it back. A rebind keeps whatever the
source already had; a new source still starts without one.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest

from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import ToolResult
from elspeth.web.dependencies import create_catalog_service
from tests.unit.web.composer import test_blob_inline_tools as blob_tools
from tests.unit.web.composer import test_file_inject_upload_only_tool_parity as parity

blob_env = blob_tools.blob_env

_DESCRIPTION = "Read every row of the uploaded orders file."


def _upload_csv(blob_env: dict[str, Any], filename: str, content: str) -> str:
    service = BlobServiceImpl(blob_env["engine"], Path(blob_env["data_dir"]))

    async def create() -> str:
        with patch("elspeth.web.blobs.service.run_sync_in_worker", parity._run_on_this_thread):
            record = await service.create_blob(
                UUID(blob_env["session_id"]),
                filename,
                content.encode(),
                "text/csv",
                created_by="user",
                source_description="uploaded",
                session_operation_context=blob_env["operation"],
            )
        assert record.status == "ready"
        return str(record.id)

    return asyncio.run(create())


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


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _arguments(tool: str, source_name: str, blob_id: str) -> dict[str, Any]:
    if tool == "set_source_from_blob":
        return {
            "blob_id": blob_id,
            "source_name": source_name,
            "on_success": f"{source_name}_rows",
            "on_validation_failure": "discard",
            "options": {"schema": {"mode": "observed"}},
        }
    return {"blob_ids": [blob_id], "source_name": source_name, "on_success": f"{source_name}_rows", "on_validation_failure": "discard"}


def _bound_with_description(blob_env: dict[str, Any], tool: str, source_name: str) -> CompositionState:
    """A source bound to a first upload, carrying a stored description."""
    first = _upload_csv(blob_env, "orders_a.csv", "order_id,total\n1,10\n")
    bound = _execute(tool, _arguments(tool, source_name, first), _empty_state(), blob_env)
    assert bound.success is True, [error.message for error in bound.validation.errors]
    stored = bound.updated_state.sources[source_name]
    assert stored.description is None
    return replace(bound.updated_state, sources={source_name: replace(stored, description=_DESCRIPTION)})


@pytest.mark.parametrize("tool", ["set_source_from_blob", "set_source_from_blobs"])
@pytest.mark.parametrize("source_name", ["source", "submissions"])
def test_rebinding_to_another_upload_keeps_the_stored_description(blob_env: dict[str, Any], tool: str, source_name: str) -> None:
    state = _bound_with_description(blob_env, tool, source_name)
    second = _upload_csv(blob_env, "orders_b.csv", "order_id,total\n2,20\n")

    result = _execute(tool, _arguments(tool, source_name, second), state, blob_env)

    assert result.success is True, [error.message for error in result.validation.errors]
    rebound = result.updated_state.sources[source_name]
    assert rebound.options != state.sources[source_name].options  # the binding really moved to the second file
    assert rebound.description == _DESCRIPTION


@pytest.mark.parametrize("tool", ["set_source_from_blob", "set_source_from_blobs"])
def test_rebinding_one_source_leaves_another_source_name_without_a_description(blob_env: dict[str, Any], tool: str) -> None:
    """The kept description belongs to the source being rebound, not to whichever source has one."""
    state = _bound_with_description(blob_env, tool, "source")
    other = _upload_csv(blob_env, "orders_c.csv", "order_id,total\n3,30\n")

    result = _execute(tool, _arguments(tool, "returns", other), state, blob_env)

    assert result.success is True, [error.message for error in result.validation.errors]
    assert result.updated_state.sources["returns"].description is None
    assert result.updated_state.sources["source"].description == _DESCRIPTION
