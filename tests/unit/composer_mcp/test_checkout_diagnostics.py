"""Checkout refusal diagnostics through the SDK and durable JSONL audit."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

from elspeth.composer_mcp.audit import events_sidecar_path, verify_events_sidecar_integrity
from elspeth.composer_mcp.server import create_server
from elspeth.composer_mcp.session import SessionManager
from elspeth.core.canonical import canonical_json, stable_hash
from tests.unit.composer_mcp.test_server import _empty_state, _mock_catalog


async def _call(server: Server, name: str, session_id: str) -> CallToolResult:
    response = await server.request_handlers[CallToolRequest](
        CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments={"session_id": session_id}))
    )
    assert isinstance(response.root, CallToolResult)
    return response.root


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [None, "bbbbbbbbbbbb", "cccccccccccc"])
async def test_checkout_refusal_preserves_diagnostic_in_sdk_and_audit(tmp_path: Path, active: str | None) -> None:
    requested = "aaaaaaaaaaaa"
    manager = SessionManager(tmp_path)
    requested_path = manager.save(requested, _empty_state())
    before = requested_path.read_bytes()
    server = create_server(_mock_catalog(), tmp_path, runtime_preflight=None, runtime_preflight_settings_hash=None)
    if active is not None:
        active_state = replace(_empty_state(), metadata=replace(_empty_state().metadata, name="private graph metadata"), version=3)
        active_path = manager.save(active, active_state)
        active_before = active_path.read_bytes()
        loaded = await _call(server, "load_session", active)
        assert loaded.isError is False

    response = await _call(server, "save_session", requested)
    assert response.isError is True
    assert len(response.content) == 1
    content = response.content[0]
    assert isinstance(content, TextContent)
    assert content.text.lstrip().startswith("{"), "checkout refusal must carry structured JSON diagnostics"
    payload = json.loads(content.text)
    expected_message = (
        f"Cannot save session {requested}: active checkout is {active}"
        if active is not None
        else f"Cannot save session {requested}: no active checkout"
    )
    assert payload == {
        "success": False,
        "error": expected_message,
        "code": "session_checkout_mismatch",
        "requested_session_id": requested,
        "active_session_id": active,
    }
    assert requested_path.read_bytes() == before
    if active is None:
        # Resolve the documented pre-session buffer using an ordinary load.
        await _call(server, "load_session", requested)
    else:
        assert active_path.read_bytes() == active_before

    sidecar = events_sidecar_path(tmp_path, active if active is not None else requested)
    rows = [json.loads(line) for line in sidecar.read_text().splitlines()]
    save_rows = [row for row in rows if row["tool_name"] == "save_session"]
    assert len(save_rows) == 1
    row = save_rows[0]
    assert row["status"] == "success"
    assert row["result_canonical"] == canonical_json(payload)
    assert row["result_hash"] == stable_hash(payload)
    assert row["error_class"] is None
    assert row["error_message"] is None
    assert row["version_before"] == row["version_after"] == (3 if active is not None else 1)
    verify_events_sidecar_integrity(sidecar)

    # The refusal must leave checkout authority and in-memory state usable.
    saved = await _call(server, "save_session", active if active is not None else requested)
    assert saved.isError is False
    assert requested_path.read_bytes() == before
    if active is not None:
        assert active_path.read_bytes() == active_before


@pytest.mark.asyncio
async def test_unexpected_save_failure_remains_plugin_crash(tmp_path: Path) -> None:
    session_id = "aaaaaaaaaaaa"
    manager = SessionManager(tmp_path)
    path = manager.save(session_id, _empty_state())
    before = path.read_bytes()
    server = create_server(_mock_catalog(), tmp_path, runtime_preflight=None, runtime_preflight_settings_hash=None)
    await _call(server, "load_session", session_id)
    with patch.object(SessionManager, "save_if_current", autospec=True, side_effect=ValueError("unexpected save failure")):
        response = await _call(server, "save_session", session_id)
    assert response.isError is True
    assert path.read_bytes() == before
    sidecar = events_sidecar_path(tmp_path, session_id)
    rows = [json.loads(line) for line in sidecar.read_text().splitlines()]
    row = rows[-1]
    assert row["tool_name"] == "save_session"
    assert row["status"] == "plugin_crash"
    assert row["error_class"] == row["error_message"] == "ValueError"
    assert row["result_canonical"] is None
    assert row["result_hash"] is None
    assert row["version_after"] is None
    verify_events_sidecar_integrity(sidecar)
