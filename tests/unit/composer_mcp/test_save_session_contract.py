"""Local save-session controls through the installed SDK and real persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

from elspeth.composer_mcp.audit import JsonlEventRecorder, events_sidecar_path, verify_events_sidecar_integrity
from elspeth.composer_mcp.server import create_server
from elspeth.composer_mcp.session import SessionManager
from elspeth.contracts.composer_audit import ComposerToolInvocation
from elspeth.web.dependencies import create_catalog_service


async def _call(server: Server, name: str, arguments: dict[str, Any]) -> CallToolResult:
    response = await server.request_handlers[CallToolRequest](
        CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
    )
    assert isinstance(response.root, CallToolResult)
    assert len(response.root.content) == 1
    assert isinstance(response.root.content[0], TextContent)
    assert response.root.structuredContent is None
    return response.root


def _payload(result: CallToolResult) -> dict[str, Any]:
    content = result.content[0]
    assert isinstance(content, TextContent)
    payload: dict[str, Any] = json.loads(content.text)
    return payload


async def _new(server: Server) -> str:
    result = await _call(server, "new_session", {"name": "synthetic save baseline"})
    assert result.isError is False
    return str(_payload(result)["data"]["session_id"])


def _server(scratch: Path) -> Server:
    return create_server(create_catalog_service(), scratch, runtime_preflight=None, runtime_preflight_settings_hash=None)


@pytest.mark.asyncio
async def test_save_persists_without_increment_or_resetting_diff_baseline(tmp_path: Path) -> None:
    server = _server(tmp_path)
    sid = await _new(server)
    path = tmp_path / f"{sid}.json"
    original = path.read_bytes()
    changed = await _call(server, "set_metadata", {"patch": {"name": "synthetic \u96ea"}})
    assert changed.isError is False
    assert path.read_bytes() == original
    diff_before = _payload(await _call(server, "diff_pipeline", {}))
    result = await _call(server, "save_session", {"session_id": sid})
    assert result.isError is False
    payload = _payload(result)
    assert set(payload) == {"success", "data", "state"}
    assert payload["success"] is True
    assert payload["data"] == {"session_id": sid}
    assert payload["state"]["version"] == 2
    assert path.read_bytes() == json.dumps(payload["state"], indent=2, sort_keys=True).encode("utf-8")
    assert SessionManager(tmp_path).load(sid).metadata.name == "synthetic \u96ea"
    assert _payload(await _call(server, "diff_pipeline", {})) == diff_before
    stable = path.stat()
    repeated = await _call(server, "save_session", {"session_id": sid})
    assert _payload(repeated) == payload
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (stable.st_ino, stable.st_mtime_ns)
    rows = [json.loads(line) for line in events_sidecar_path(tmp_path, sid).read_text().splitlines()]
    saves = [row for row in rows if row["tool_name"] == "save_session"]
    assert len(saves) == 2
    assert all(row["status"] == "success" and row["version_before"] == row["version_after"] == 2 for row in saves)
    assert all(json.loads(row["result_canonical"])["data"] == {"session_id": sid} for row in saves)
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"session_id": None},
        {"session_id": 1},
        {"session_id": True},
        {"session_id": "AAAAAAAAAAAA"},
        {"session_id": "aaaaaaaaaaa"},
        {"session_id": "aaaaaaaaaaaaa"},
        {"session_id": "../aaaaaaaaa"},
        {"session_id": "\uff41aaaaaaaaaaa"},
        {"session_id": "aaaaaaaaaaaa", "extra": True},
    ],
)
async def test_invalid_save_arguments_preserve_durable_state_and_checkout(tmp_path: Path, arguments: dict[str, Any]) -> None:
    server = _server(tmp_path)
    sid = await _new(server)
    path = tmp_path / f"{sid}.json"
    original = path.read_bytes()
    response = await _call(server, "save_session", arguments)
    assert response.isError is True
    assert path.read_bytes() == original
    row = json.loads(events_sidecar_path(tmp_path, sid).read_text().splitlines()[-1])
    assert row["tool_name"] == "save_session"
    assert row["status"] == "arg_error"
    assert row["error_class"] == "ToolArgumentError"
    assert row["version_after"] is None
    assert (await _call(server, "save_session", {"session_id": sid})).isError is False
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_same_version_byte_drift_conflicts_without_losing_working_candidate(tmp_path: Path) -> None:
    server = _server(tmp_path)
    sid = await _new(server)
    path = tmp_path / f"{sid}.json"
    original = path.read_bytes()
    candidate = _payload(await _call(server, "set_metadata", {"patch": {"description": "local unsaved candidate"}}))["state"]
    external = json.loads(original)
    external["metadata"]["description"] = "external same-version edit"
    altered = json.dumps(external, separators=(",", ":")).encode()
    path.write_bytes(altered)
    refused = await _call(server, "save_session", {"session_id": sid})
    assert refused.isError is True
    assert path.read_bytes() == altered
    row = json.loads(events_sidecar_path(tmp_path, sid).read_text().splitlines()[-1])
    assert row["status"] == "plugin_crash"
    assert row["error_class"] == row["error_message"] == "StaleSessionVersionError"
    assert row["result_canonical"] is None
    assert row["version_after"] is None
    path.write_bytes(original)
    recovered = await _call(server, "save_session", {"session_id": sid})
    assert recovered.isError is False
    assert _payload(recovered)["state"] == candidate
    assert SessionManager(tmp_path).load(sid).to_dict() == candidate


@pytest.mark.asyncio
async def test_audit_failure_after_save_is_not_rollback_and_retry_is_byte_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = _server(tmp_path)
    sid = await _new(server)
    candidate = _payload(await _call(server, "set_metadata", {"patch": {"description": "candidate before audit failure"}}))["state"]
    path = tmp_path / f"{sid}.json"
    old = path.read_bytes()
    sidecar = events_sidecar_path(tmp_path, sid)
    events_before = sidecar.read_bytes()
    original_record = JsonlEventRecorder.record

    def fail_save_audit(recorder: JsonlEventRecorder, invocation: ComposerToolInvocation) -> None:
        if invocation.tool_name == "save_session":
            raise OSError("synthetic audit write failure")
        original_record(recorder, invocation)

    with monkeypatch.context() as audit_failure:
        audit_failure.setattr(JsonlEventRecorder, "record", fail_save_audit)
        response = await _call(server, "save_session", {"session_id": sid})
    assert response.isError is True
    assert sidecar.read_bytes() == events_before
    durable = path.read_bytes()
    assert durable != old
    assert SessionManager(tmp_path).load(sid).to_dict() == candidate
    stat = path.stat()
    retry = await _call(server, "save_session", {"session_id": sid})
    assert retry.isError is False
    assert path.read_bytes() == durable
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (stat.st_ino, stat.st_mtime_ns)
    await _call(server, "set_metadata", {"patch": {"description": "after retry"}})
    assert (await _call(server, "save_session", {"session_id": sid})).isError is False
    assert SessionManager(tmp_path).load(sid).metadata.description == "after retry"
    verify_events_sidecar_integrity(sidecar)
