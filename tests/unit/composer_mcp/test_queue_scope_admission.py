"""SDK queue refusals preserve private working state and durable checkout."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from elspeth.composer_mcp import server as server_module
from elspeth.composer_mcp.session import SessionManager
from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.web.dependencies import create_catalog_service
from tests.unit.composer_mcp.test_call_tool_audit import _call_handler, _ProbeRecorder
from tests.unit.web.composer.test_queue_scope_admission import (
    MALFORMED_SCOPE_CASES,
    QUEUE,
    SCOPE_CASES,
    private_snapshot,
    scope_message,
)


@pytest.mark.asyncio
async def test_sdk_scope_refusals_preserve_private_state_checkout_and_saved_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = _ProbeRecorder()
    server = server_module.create_server(
        create_catalog_service(),
        tmp_path,
        recorder=probe,
        runtime_preflight=None,
        runtime_preflight_settings_hash=None,
    )
    observed = []
    real_dispatch = server_module._dispatch_tool

    def observe(name: str, arguments: dict[str, Any], state: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        before = private_snapshot(state)
        checkout = kwargs["session_checkout_ref"][0]
        result = real_dispatch(name, arguments, state, *args, **kwargs)
        observed.append((name, before, checkout, deepcopy(result)))
        return result

    monkeypatch.setattr(server_module, "_dispatch_tool", observe)

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        count = len(probe.invocations)
        response = await _call_handler(server.request_handlers, name, arguments)
        assert len(probe.invocations) == count + 1
        assert not response.root.isError
        return json.loads(response.root.content[0].text)

    created = await call("new_session", {"name": "Private queue"})
    session_id = created["data"]["session_id"]
    canonical = await call("upsert_node", {**QUEUE, "description": "Saved queue"})
    assert canonical["success"]
    assert (await call("save_session", {"session_id": session_id}))["success"]
    manager = SessionManager(tmp_path)
    saved = manager.checkout(session_id)
    before = private_snapshot(saved.state)
    path = tmp_path / f"{session_id}.json"
    saved_bytes = path.read_bytes()
    saved_stat = path.stat()
    for override in SCOPE_CASES:
        result = await call("upsert_node", {**QUEUE, "description": "Must not replace", **override})
        assert not result["success"]
        error = result["validation"]["errors"][0]
        assert (error["component"], error["error_code"], error["rejected_component"]) == (
            "rejected_mutation",
            "queue_config_invalid",
            "node:inbound",
        )
        assert error["message"] == scope_message(override)
        assert result["affected_nodes"] == [] and "validation_delta" not in result
        assert "applied_component" not in result
        assert probe.invocations[-1].status == ComposerToolStatus.SUCCESS
        _, dispatch_before, checkout, private_result = observed[-1]
        assert dispatch_before == before and checkout == saved
        assert private_result["state"] == before[0]
        # The next real call sees the SDK's reconstructed private working state.
        readback = await call("get_pipeline_state", {})
        assert readback["success"] and any(node["id"] == "inbound" for node in readback["state"]["nodes"])
        assert observed[-1][1] == before and observed[-1][2] == saved
        assert path.read_bytes() == saved_bytes and manager.checkout(session_id) == saved
        assert (path.stat().st_ino, path.stat().st_mtime_ns) == (saved_stat.st_ino, saved_stat.st_mtime_ns)

    # A distinct canonical edit, explicit save and reload prove checkout authority survives.
    assert (await call("upsert_node", {**QUEUE, "description": "Explicitly persisted"}))["success"]
    assert path.read_bytes() == saved_bytes
    assert (await call("save_session", {"session_id": session_id}))["success"]
    assert path.read_bytes() != saved_bytes
    assert (await call("load_session", {"session_id": session_id}))["success"]
    await call("get_pipeline_state", {})
    assert observed[-1][1] == private_snapshot(manager.load(session_id))
    assert manager.load(session_id).nodes[0].description == "Explicitly persisted"


@pytest.mark.asyncio
@pytest.mark.parametrize("override", MALFORMED_SCOPE_CASES)
async def test_sdk_malformed_scope_is_audited_argument_error_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    override: dict[str, Any],
) -> None:
    probe = _ProbeRecorder()
    server = server_module.create_server(
        create_catalog_service(),
        tmp_path,
        recorder=probe,
        runtime_preflight=None,
        runtime_preflight_settings_hash=None,
    )
    await _call_handler(server.request_handlers, "new_session", {"name": "Malformed queue"})
    real_dispatch = server_module._dispatch_tool
    dispatches = []

    def observe(*args: Any, **kwargs: Any) -> dict[str, Any]:
        dispatches.append(args[0])
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(server_module, "_dispatch_tool", observe)
    response = await _call_handler(server.request_handlers, "upsert_node", {**QUEUE, **override})
    assert response.root.isError is True
    assert response.root.content[0].text.startswith("Tool error: ")
    assert len(probe.invocations) == 2 and probe.invocations[-1].status == ComposerToolStatus.ARG_ERROR
    assert dispatches == []
    readback = await _call_handler(server.request_handlers, "get_pipeline_state", {})
    assert json.loads(readback.root.content[0].text)["state"]["nodes"] == []
