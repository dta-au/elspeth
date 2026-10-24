"""Local list controls; TC-018/020 cases characterize known nonconformance."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

from elspeth.composer_mcp.audit import events_sidecar_path, verify_events_sidecar_integrity
from elspeth.composer_mcp.server import create_server
from elspeth.composer_mcp.session import SessionManager
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material_for_tool
from elspeth.web.dependencies import create_catalog_service


def _server(scratch: Path) -> Server:
    return create_server(create_catalog_service(), scratch, runtime_preflight=None, runtime_preflight_settings_hash=None)


async def _call(server: Server, name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
    )
    assert isinstance(result.root, CallToolResult)
    assert len(result.root.content) == 1
    assert isinstance(result.root.content[0], TextContent)
    assert result.root.structuredContent is None
    return result.root


def _payload(result: CallToolResult) -> dict[str, Any]:
    assert isinstance(result.content[0], TextContent)
    value: dict[str, Any] = json.loads(result.content[0].text)
    return value


async def _new(server: Server) -> str:
    result = await _call(server, "new_session", {"name": "benign active session"})
    assert result.isError is False
    return str(_payload(result)["data"]["session_id"])


def _rows(scratch: Path, sid: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in events_sidecar_path(scratch, sid).read_text().splitlines()]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, None], ids=["empty-object", "sdk-null"])
async def test_empty_listing_before_selection_buffers_audit_without_creating_store(
    tmp_path: Path, arguments: dict[str, Any] | None
) -> None:
    scratch = tmp_path / "absent"
    server = _server(scratch)
    response = await _call(server, "list_sessions", arguments)
    assert response.isError is False
    payload = _payload(response)
    assert payload == {
        "success": True,
        "data": {"sessions": []},
        "state": {
            "version": 1,
            "metadata": {"name": "Untitled Pipeline", "description": ""},
            "sources": {},
            "nodes": [],
            "edges": [],
            "outputs": [],
        },
    }
    assert not scratch.exists()
    sid = await _new(server)
    rows = _rows(scratch, sid)
    assert [row["tool_name"] for row in rows] == ["list_sessions", "new_session"]
    assert rows[0]["arguments_canonical"] == "{}"
    assert rows[0]["result_canonical"] == canonical_json(payload)
    verify_events_sidecar_integrity(events_sidecar_path(scratch, sid))


@pytest.mark.asyncio
async def test_sorted_saved_metadata_ignores_invalid_and_corrupt_files_without_repair(tmp_path: Path) -> None:
    manager = SessionManager(tmp_path)
    _, template = manager.new_session()
    ids = ["bbbbbbbbbbbb", "000000000000", "aaaaaaaaaaaa"]
    names = ["duplicate", "snow \u96ea", "duplicate"]
    for sid, name in zip(ids, names, strict=True):
        manager.save(sid, template.with_metadata({"name": name}))
    payload = json.dumps(template.to_dict())
    fixtures = {
        "AAAAAAAAAAAA.json": payload.encode(),
        "short.json": payload.encode(),
        "cccccccccccc.json": b"{broken",
        "dddddddddddd.json": b"[]",
        "eeeeeeeeeeee.json": b"{}",
        "ffffffffffff.json": payload.encode("utf-8-sig"),
    }
    for filename, data in fixtures.items():
        (tmp_path / filename).write_bytes(data)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "111111111111.json").write_text(payload)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    locks_before = sorted(p.name for p in tmp_path.glob(".*.lock"))
    server = _server(tmp_path)
    response = await _call(server, "list_sessions", {})
    assert response.isError is False
    sessions = _payload(response)["data"]["sessions"]
    assert sessions == [
        {"session_id": "000000000000", "name": "snow \u96ea", "version": 2},
        {"session_id": "aaaaaaaaaaaa", "name": "duplicate", "version": 2},
        {"session_id": "bbbbbbbbbbbb", "name": "duplicate", "version": 2},
    ]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
    assert sorted(p.name for p in tmp_path.glob(".*.lock")) == locks_before
    assert not list(tmp_path.glob("*.events.jsonl"))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_version", [False, True], ids=["nullable-name-accepted", "nonnumeric-version-skipped"])
async def test_parsed_metadata_and_version_have_distinct_reconstruction_checks(tmp_path: Path, bad_version: bool) -> None:
    manager = SessionManager(tmp_path)
    _, state = manager.new_session()
    path = manager.save("aaaaaaaaaaaa", state)
    data = json.loads(path.read_text())
    data["metadata"]["name"] = None
    data["version"] = "synthetic nonnumeric version" if bad_version else 1
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    response = await _call(_server(tmp_path), "list_sessions", {})
    assert response.isError is False
    expected = [] if bad_version else [{"session_id": "aaaaaaaaaaaa", "name": None, "version": 1}]
    assert _payload(response)["data"]["sessions"] == expected
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_listing_retains_unsaved_graph_diff_baseline_and_active_checkout(tmp_path: Path) -> None:
    server = _server(tmp_path)
    sid = await _new(server)
    path = tmp_path / f"{sid}.json"
    original = path.read_bytes()
    changed = _payload(await _call(server, "set_metadata", {"patch": {"name": "unsaved edit"}}))["state"]
    diff_before = _payload(await _call(server, "diff_pipeline", {}))
    response = await _call(server, "list_sessions", {})
    listed = _payload(response)
    assert listed["state"] == changed
    assert listed["data"] == {"sessions": [{"session_id": sid, "name": "benign active session", "version": 1}]}
    assert path.read_bytes() == original
    assert _payload(await _call(server, "diff_pipeline", {})) == diff_before
    saved = await _call(server, "save_session", {"session_id": sid})
    assert saved.isError is False
    assert SessionManager(tmp_path).load(sid).to_dict() == changed
    row = next(row for row in _rows(tmp_path, sid) if row["tool_name"] == "list_sessions")
    assert row["status"] == "success"
    assert row["version_before"] == row["version_after"] == 2
    assert row["result_canonical"] == canonical_json(listed)
    assert row["result_hash"] == stable_hash(listed)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["vanished", "unreadable", "unexpected"])
async def test_file_read_faults_skip_only_declared_classes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    manager = SessionManager(tmp_path)
    _, state = manager.new_session(name="kept")
    victim = manager.save("aaaaaaaaaaaa", state)
    manager.save("bbbbbbbbbbbb", state)
    original = victim.read_bytes()
    read = Path.read_bytes

    def faulty_read(path: Path) -> bytes:
        if path == victim:
            if failure == "vanished":
                raise FileNotFoundError("synthetic vanished file")
            if failure == "unreadable":
                raise PermissionError("synthetic unreadable file")
            raise RuntimeError("synthetic unexpected read fault")
        return read(path)

    server = _server(tmp_path)
    with monkeypatch.context() as fault:
        fault.setattr(Path, "read_bytes", faulty_read)
        response = await _call(server, "list_sessions", {})
    if failure == "unexpected":
        assert response.isError is True
        assert isinstance(response.content[0], TextContent)
        assert response.content[0].text == "synthetic unexpected read fault"
    else:
        assert response.isError is False
        assert _payload(response)["data"]["sessions"] == [{"session_id": "bbbbbbbbbbbb", "name": "kept", "version": 1}]
    assert victim.read_bytes() == original
    assert len(_payload(await _call(server, "list_sessions", {}))["data"]["sessions"]) == 2


@pytest.mark.asyncio
async def test_ordinary_extra_is_schema_error_and_paired_valid_list_still_works(tmp_path: Path) -> None:
    server = _server(tmp_path)
    sid = await _new(server)
    before = (tmp_path / f"{sid}.json").read_bytes()
    result = await _call(server, "list_sessions", {"filter": "all"})
    assert result.isError is True
    row = _rows(tmp_path, sid)[-1]
    assert row["status"] == "arg_error" and row["error_class"] == "ToolArgumentError"
    assert row["version_after"] is None
    assert (tmp_path / f"{sid}.json").read_bytes() == before
    assert (await _call(server, "list_sessions", {})).isError is False


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["buffered", "active"])
async def test_tc018_disk_name_is_admitted_before_list_response_and_audit(tmp_path: Path, active: bool) -> None:
    """TC-018 (#327): listed disk metadata passes credential admission first.

    Formerly KNOWN_NONCONFORMANCE: a recognized synthetic name in a saved
    file reached the listing response and its audit row. The listing now
    refuses whole, fail-closed, and neither the response nor any audit row
    carries the disk value. No real credential.
    """
    synthetic = "sk-" + "a" * 24
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool("new_session", {"name": synthetic}, surface="synthetic-positive")
    require_no_credential_material_for_tool("new_session", {"name": "benign metadata"}, surface="synthetic-negative")
    manager = SessionManager(tmp_path)
    _, state = manager.new_session(name="benign metadata")
    path = manager.save("aaaaaaaaaaaa", state)
    disk = json.loads(path.read_text())
    disk["metadata"]["name"] = synthetic
    path.write_text(json.dumps(disk))
    before = path.read_bytes()
    server = _server(tmp_path)
    if active:
        sid = await _new(server)
    response = await _call(server, "list_sessions", {})
    assert response.isError is True
    assert isinstance(response.content[0], TextContent)
    assert synthetic not in response.content[0].text
    assert path.read_bytes() == before
    if not active:
        assert not list(tmp_path.glob("*.events.jsonl"))
        sid = await _new(server)
    list_rows = [row for row in _rows(tmp_path, sid) if row["tool_name"] == "list_sessions"]
    assert len(list_rows) == 1
    assert list_rows[0]["status"] == "plugin_crash"
    assert list_rows[0]["error_class"] == list_rows[0]["error_message"] == "CredentialMaterialRefused"
    assert list_rows[0]["result_canonical"] is None and list_rows[0]["result_hash"] is None
    assert synthetic not in events_sidecar_path(tmp_path, sid).read_text()
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["buffered", "active"])
async def test_tc020_refused_list_extra_withholds_raw_arguments(tmp_path: Path, active: bool) -> None:
    """TC-020 (#327): refused arguments reach audit only as a value-free stand-in.

    Formerly KNOWN_NONCONFORMANCE: admission refused but the audit row kept
    the raw canonical arguments.
    """
    synthetic = "sk-" + "b" * 24
    arguments = {"extra": synthetic}
    with pytest.raises(CredentialMaterialRefused) as caught:
        require_no_credential_material_for_tool("list_sessions", arguments, surface="composer_mcp_tool_arguments")
    withheld = {"_arguments_withheld": caught.value.to_payload()}
    require_no_credential_material_for_tool("list_sessions", {"extra": "benign"}, surface="synthetic-negative")
    server = _server(tmp_path)
    if active:
        sid = await _new(server)
        path = tmp_path / f"{sid}.json"
        before = path.read_bytes()
    refused = await _call(server, "list_sessions", arguments)
    assert refused.isError is True
    assert isinstance(refused.content[0], TextContent)
    assert synthetic not in refused.content[0].text
    if not active:
        assert not list(tmp_path.iterdir())
        sid = await _new(server)
    else:
        assert path.read_bytes() == before
    rows = [row for row in _rows(tmp_path, sid) if row["tool_name"] == "list_sessions"]
    assert len(rows) == 1
    assert rows[0]["status"] == "plugin_crash"
    assert rows[0]["error_class"] == rows[0]["error_message"] == "CredentialMaterialRefused"
    assert rows[0]["arguments_canonical"] == canonical_json(withheld)
    assert rows[0]["arguments_hash"] == stable_hash(withheld) != stable_hash(arguments)
    assert synthetic not in events_sidecar_path(tmp_path, sid).read_text()
    assert rows[0]["result_canonical"] is None
    assert synthetic not in (tmp_path / f"{sid}.json").read_text()
    assert (await _call(server, "list_sessions", {})).isError is False
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))
