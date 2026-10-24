"""Delete METHOD controls against private synthetic stores only; no cleanup."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest, TextContent

from elspeth.composer_mcp import session as session_module
from elspeth.composer_mcp.audit import JsonlEventRecorder, verify_events_sidecar_integrity
from elspeth.composer_mcp.server import _build_tool_defs, _dispatch_tool, create_server
from elspeth.composer_mcp.session import SessionManager, SessionNotFoundError
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.redaction import REDACTED_BLOB_SOURCE_PATH
from elspeth.web.composer.state import CompositionState, PipelineMetadata, SourceSpec
from elspeth.web.credential_guard import (
    CredentialMaterialRefused,
    require_no_credential_material_for_tool,
    require_no_credential_material_in_state,
)
from elspeth.web.dependencies import create_catalog_service

TARGET = "a123456789bc"
MISSING = "d123456789ef"


def local_server(scratch: Path) -> Server:
    return create_server(create_catalog_service(), scratch, runtime_preflight=None, runtime_preflight_settings_hash=None)


def state(name: str = "Synthetic disk graph") -> CompositionState:
    return CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name=name), version=7)


def write_target(scratch: Path, graph: CompositionState | None = None) -> Path:
    SessionManager(scratch).save(TARGET, state() if graph is None else graph)
    return scratch / f"{TARGET}.json"


async def call(server: Server, name: str, arguments: dict[str, Any] | None = None):
    request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
    return (await server.request_handlers[CallToolRequest](request)).root


async def payload(server: Server, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await call(server, name, arguments)
    assert response.isError is False and response.structuredContent is None
    assert len(response.content) == 1 and isinstance(response.content[0], TextContent)
    return json.loads(response.content[0].text)


def rows(scratch: Path, session_id: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (scratch / f"{session_id}.events.jsonl").read_text().splitlines()]


@pytest.mark.asyncio
async def test_actual_offering_has_required_id_and_no_advertised_confirmation(tmp_path: Path) -> None:
    """DS-C1: source declaration equals actual locally offered MCP Tool."""
    server = local_server(tmp_path)
    result = await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    selected = [tool for tool in result.root.tools if tool.name == "delete_session"]
    assert len(selected) == 1
    tool = selected[0]
    declaration = next(item for item in _build_tool_defs() if item["name"] == "delete_session")
    assert tool.description == declaration["description"] == "Delete a saved composition session."
    assert (
        tool.inputSchema
        == declaration["parameters"]
        == {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "Session ID to delete."}},
            "required": ["session_id"],
            "additionalProperties": False,
        }
    )
    assert tool.annotations is None and tool.outputSchema is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_active_delete_keeps_working_diff_and_audit_but_clears_checkout(tmp_path: Path) -> None:
    """DS-C2/C3/C5: real active delete and later supported observers."""
    server = local_server(tmp_path)
    created = await payload(server, "new_session", {"name": "Saved baseline"})
    sid = created["data"]["session_id"]
    edited = await payload(server, "set_metadata", {"patch": {"name": "Unsaved working"}})
    before_diff = await payload(server, "diff_pipeline", {})
    assert before_diff["data"]["total_changes"] > 0
    sidecar = tmp_path / f"{sid}.events.jsonl"
    before_events = sidecar.read_bytes()
    lock = tmp_path / f".{sid}.lock"
    assert lock.exists()  # Actual Linux/fcntl test environment, not every platform.
    deleted = await payload(server, "delete_session", {"session_id": sid})
    assert deleted == {"success": True, "data": {"session_id": sid}, "state": edited["state"]}
    assert not (tmp_path / f"{sid}.json").exists()
    assert lock.exists() and sidecar.read_bytes().startswith(before_events)
    event = rows(tmp_path, sid)[-1]
    assert event["tool_name"] == "delete_session" and event["status"] == "success"
    assert event["version_before"] == event["version_after"] == edited["state"]["version"]
    assert event["actor"] == "composer-mcp:cli"
    assert json.loads(event["arguments_canonical"]) == {"session_id": sid}
    assert json.loads(event["result_canonical"]) == deleted
    verify_events_sidecar_integrity(sidecar)
    at_delete = sidecar.read_bytes()
    assert (await payload(server, "get_pipeline_state", {}))["state"] == edited["state"]
    assert await payload(server, "diff_pipeline", {}) == before_diff
    refused_save = await call(server, "save_session", {"session_id": sid})
    assert refused_save.isError is True
    assert json.loads(refused_save.content[0].text)["active_session_id"] is None
    repeated = await payload(server, "delete_session", {"session_id": sid})
    assert repeated == {"success": False, "error": f"Session not found: {sid}", "state": edited["state"]}
    assert not (tmp_path / f"{sid}.json").exists() and sidecar.read_bytes() == at_delete
    replacement = await payload(server, "new_session", {"name": "Later audit resolution"})
    later = rows(tmp_path, replacement["data"]["session_id"])
    assert [event["tool_name"] for event in later] == [
        "get_pipeline_state",
        "diff_pipeline",
        "save_session",
        "delete_session",
        "new_session",
    ]
    assert json.loads(later[-2]["result_canonical"])["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["unresolved", "other-active"])
async def test_nonactive_delete_preserves_target_sidecar_and_uses_current_audit_scope(tmp_path: Path, active: bool) -> None:
    """DS-C2/C3/C5: target ID is not automatically the audit attribution ID."""
    target = write_target(tmp_path)
    sidecar = tmp_path / f"{TARGET}.events.jsonl"
    retained = b"synthetic preexisting sidecar bytes; deliberately not parsed\n"
    sidecar.write_bytes(retained)
    server = local_server(tmp_path)
    if active:
        current = await payload(server, "new_session", {"name": "Other active"})
        sid = current["data"]["session_id"]
        before = (tmp_path / f"{sid}.json").read_bytes()
    deleted = await payload(server, "delete_session", {"session_id": TARGET})
    assert deleted["success"] is True and deleted["data"] == {"session_id": TARGET}
    assert not target.exists() and sidecar.read_bytes() == retained
    if active:
        assert deleted["state"] == current["state"]
        assert rows(tmp_path, sid)[-1]["tool_name"] == "delete_session"
        assert json.loads(rows(tmp_path, sid)[-1]["arguments_canonical"]) == {"session_id": TARGET}
        assert (await payload(server, "save_session", {"session_id": sid}))["success"] is True
        assert (tmp_path / f"{sid}.json").read_bytes() == before
    else:
        created = await payload(server, "new_session", {"name": "Resolution"})
        later = rows(tmp_path, created["data"]["session_id"])
        assert [event["tool_name"] for event in later] == ["delete_session", "new_session"]
        assert json.loads(later[0]["result_canonical"])["success"] is True
    assert sidecar.read_bytes() == retained


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {},
        {"session_id": None},
        {"session_id": True},
        {"session_id": 7},
        {"session_id": []},
        {"session_id": {}},
        {"session_id": TARGET, "confirm": True},
        {"session_id": TARGET.upper()},
        {"session_id": "a" * 11},
        {"session_id": "a" * 13},
        {"session_id": "../outside"},
        {"session_id": TARGET + "\n"},
    ],
    ids=[
        "whole-null",
        "missing",
        "null",
        "boolean",
        "number",
        "array",
        "object",
        "extra-confirm",
        "uppercase",
        "short",
        "long",
        "traversal",
        "newline",
    ],
)
async def test_invalid_arguments_preserve_files_state_diff_and_checkout(tmp_path: Path, arguments: dict[str, Any] | None) -> None:
    """DS-C1/C4: no target unlink, no graph/baseline/authority change."""
    target = write_target(tmp_path)
    original = target.read_bytes()
    server = local_server(tmp_path)
    active = await payload(server, "new_session", {"name": "Retained"})
    sid = active["data"]["session_id"]
    active_bytes = (tmp_path / f"{sid}.json").read_bytes()
    edited = await payload(server, "set_metadata", {"patch": {"name": "Unsaved"}})
    diff = await payload(server, "diff_pipeline", {})
    refused = await call(server, "delete_session", arguments)
    assert refused.isError is True and target.read_bytes() == original
    assert (tmp_path / f"{sid}.json").read_bytes() == active_bytes
    assert (await payload(server, "get_pipeline_state", {}))["state"] == edited["state"]
    assert await payload(server, "diff_pipeline", {}) == diff
    event = next(row for row in reversed(rows(tmp_path, sid)) if row["tool_name"] == "delete_session")
    assert event["status"] == "arg_error" and event["error_class"] == "ToolArgumentError"
    assert event["version_after"] is None
    assert (await payload(server, "save_session", {"session_id": sid}))["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["before-store", "active-unsaved"])
async def test_missing_delete_is_false_body_sdk_nonerror_and_preserves_baseline(tmp_path: Path, active: bool) -> None:
    """DS-C4: missing delete has no load-style TC019 baseline transition."""
    scratch = tmp_path / "sessions"
    server = local_server(scratch)
    if active:
        created = await payload(server, "new_session", {"name": "Retained"})
        sid = created["data"]["session_id"]
        original = (scratch / f"{sid}.json").read_bytes()
        current = await payload(server, "set_metadata", {"patch": {"name": "Unsaved"}})
        before = await payload(server, "diff_pipeline", {})
    refused = await payload(server, "delete_session", {"session_id": MISSING})
    assert refused["success"] is False and refused["error"] == f"Session not found: {MISSING}"
    assert not (scratch / f"{MISSING}.json").exists()
    if active:
        assert refused["state"] == current["state"]
        assert await payload(server, "diff_pipeline", {}) == before
        assert (scratch / f"{sid}.json").read_bytes() == original
        event = next(row for row in reversed(rows(scratch, sid)) if row["tool_name"] == "delete_session")
        assert event["status"] == "success" and json.loads(event["result_canonical"])["success"] is False
        assert (await payload(server, "save_session", {"session_id": sid}))["success"] is True
    else:
        assert not scratch.exists()  # Audit remains buffered; no store/lock created.
        later = await payload(server, "new_session", {"name": "Resolution"})
        event = rows(scratch, later["data"]["session_id"])[0]
        assert event["tool_name"] == "delete_session" and event["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", [b"\xff", b"{", b"[]", b'{"sources":[{}]}'], ids=["invalid-utf8", "invalid-json", "nonobject", "unparseable-state"]
)
async def test_nonactive_delete_does_not_parse_target_or_erase_sidecar(tmp_path: Path, raw: bytes) -> None:
    """DS-C2: canonical deletion does not require successful load/parser admission."""
    target = write_target(tmp_path)
    target.write_bytes(raw)
    sidecar = tmp_path / f"{TARGET}.events.jsonl"
    sidecar.write_bytes(b"synthetic historical sidecar retained\n")
    server = local_server(tmp_path)
    current = await payload(server, "new_session", {"name": "Clean active"})
    deleted = await payload(server, "delete_session", {"session_id": TARGET})
    assert deleted["success"] is True and deleted["state"] == current["state"]
    assert not target.exists() and sidecar.read_bytes() == b"synthetic historical sidecar retained\n"
    assert rows(tmp_path, current["data"]["session_id"])[-1]["status"] == "success"


@pytest.mark.asyncio
async def test_disk_recognized_content_is_not_read_while_current_state_is_guarded(tmp_path: Path) -> None:
    """DS-C4: safe deletion has no disk-content egress.

    Since TC-018 the server refuses a contaminated load, so the old-state
    guard that blocks deletion is exercised at the dispatcher instead.
    """
    synthetic = "sk-" + "a" * 24
    dirty = state(synthetic)
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_in_state(dirty, surface="synthetic-positive")
    require_no_credential_material_in_state(state("benign"), surface="synthetic-negative")
    target = write_target(tmp_path, dirty)
    server = local_server(tmp_path)
    current = await payload(server, "new_session", {"name": "Clean active"})
    sid = current["data"]["session_id"]
    deleted = await payload(server, "delete_session", {"session_id": TARGET})
    assert deleted["success"] is True and synthetic not in json.dumps(deleted)
    assert not target.exists() and synthetic not in (tmp_path / f"{sid}.events.jsonl").read_text()
    target = write_target(tmp_path, dirty)
    fresh = local_server(tmp_path)
    refused_load = await call(fresh, "load_session", {"session_id": TARGET})
    assert refused_load.isError is True and synthetic not in refused_load.content[0].text
    before = target.read_bytes()
    checkout_ref: list[Any] = [None]
    with pytest.raises(CredentialMaterialRefused):
        _dispatch_tool(
            "delete_session",
            {"session_id": TARGET},
            dirty,
            create_catalog_service(),
            tmp_path,
            session_manager=SessionManager(tmp_path),
            session_checkout_ref=checkout_ref,
        )
    assert target.read_bytes() == before
    assert not (tmp_path / f"{TARGET}.events.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["buffered", "immediate"])
async def test_tc020_refused_extra_argument_withheld_without_target_deletion(tmp_path: Path, active: bool) -> None:
    """TC-020 (#327): a refused delete keeps the target and withholds the raw arguments.

    Formerly KNOWN_NONCONFORMANCE: the business refusal was right but the raw
    canonical arguments were retained in the audit row.
    """
    target = write_target(tmp_path)
    before = target.read_bytes()
    synthetic = "sk-" + "a" * 24
    arguments = {"session_id": TARGET, "extra": synthetic}
    with pytest.raises(CredentialMaterialRefused) as caught:
        require_no_credential_material_for_tool("delete_session", arguments, surface="composer_mcp_tool_arguments")
    withheld = {"_arguments_withheld": caught.value.to_payload()}
    require_no_credential_material_for_tool("delete_session", {"session_id": TARGET, "extra": "benign"}, surface="synthetic-negative")
    server = local_server(tmp_path)
    if active:
        current = await payload(server, "new_session", {"name": "Retained"})
        sid = current["data"]["session_id"]
    refused = await call(server, "delete_session", arguments)
    assert refused.isError is True and synthetic not in refused.content[0].text
    assert target.read_bytes() == before
    if not active:
        assert not list(tmp_path.glob("*.events.jsonl"))
        current = await payload(server, "new_session", {"name": "Resolution"})
        sid = current["data"]["session_id"]
    refused_rows = [event for event in rows(tmp_path, sid) if event["tool_name"] == "delete_session"]
    assert len(refused_rows) == 1 and refused_rows[0]["status"] == "plugin_crash"
    assert refused_rows[0]["error_class"] == "CredentialMaterialRefused"
    assert json.loads(refused_rows[0]["arguments_canonical"]) == withheld
    assert refused_rows[0]["arguments_hash"] == stable_hash(withheld) != stable_hash(arguments)
    assert synthetic not in (tmp_path / f"{sid}.events.jsonl").read_text()
    assert (await payload(server, "get_pipeline_state", {}))["state"] == current["state"]


@pytest.mark.asyncio
async def test_injected_unlink_permission_error_preserves_file_active_authority_and_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DS-C4: injected Python PermissionError, not measured OS permission incidence."""
    server = local_server(tmp_path)
    created = await payload(server, "new_session", {"name": "Retained"})
    sid = created["data"]["session_id"]
    target = tmp_path / f"{sid}.json"
    original = target.read_bytes()
    current = await payload(server, "set_metadata", {"patch": {"name": "Unsaved"}})
    diff = await payload(server, "diff_pipeline", {})
    ordinary_unlink = Path.unlink

    def deny_selected(path: Path, missing_ok: bool = False) -> None:
        if path == target:
            raise PermissionError("synthetic unlink denial")
        ordinary_unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as change:
        change.setattr(Path, "unlink", deny_selected)
        refused = await call(server, "delete_session", {"session_id": sid})
    assert refused.isError is True and target.read_bytes() == original
    event = rows(tmp_path, sid)[-1]
    assert event["status"] == "plugin_crash" and event["error_class"] == event["error_message"] == "PermissionError"
    assert event["result_canonical"] is None and event["version_after"] is None
    assert (await payload(server, "get_pipeline_state", {}))["state"] == current["state"]
    assert await payload(server, "diff_pipeline", {}) == diff
    assert (await payload(server, "save_session", {"session_id": sid}))["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["inside-lock", "at-unlink"])
async def test_injected_disappearance_becomes_missing_without_clearing_active_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """DS-C4: deterministic boundary injection, not actual concurrent-process proof."""
    server = local_server(tmp_path)
    created = await payload(server, "new_session", {"name": "Retained"})
    sid = created["data"]["session_id"]
    target = tmp_path / f"{sid}.json"
    if stage == "inside-lock":
        ordinary_lock = SessionManager._locked_session

        @contextmanager
        def disappear_on_lock(manager: SessionManager, session_id: str) -> Iterator[None]:
            with ordinary_lock(manager, session_id):
                target.unlink()
                yield

        with monkeypatch.context() as change:
            change.setattr(SessionManager, "_locked_session", disappear_on_lock)
            missing = await payload(server, "delete_session", {"session_id": sid})
    else:
        ordinary_unlink = Path.unlink

        def disappear_at_unlink(path: Path, missing_ok: bool = False) -> None:
            ordinary_unlink(path, missing_ok=missing_ok)
            if path == target:
                raise FileNotFoundError("synthetic disappear before unlink observed completion")

        with monkeypatch.context() as change:
            change.setattr(Path, "unlink", disappear_at_unlink)
            missing = await payload(server, "delete_session", {"session_id": sid})
    assert missing == {"success": False, "error": f"Session not found: {sid}", "state": created["state"]}
    assert not target.exists()
    refused_save = await call(server, "save_session", {"session_id": sid})
    assert refused_save.isError is True and "Refusing stale save" in refused_save.content[0].text
    assert not target.exists()  # Retained checkout refuses resurrection of externally removed file.
    assert rows(tmp_path, sid)[-1]["error_class"] == "StaleSessionVersionError"


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_active", [False, True], ids=["nonactive-target", "active-target"])
async def test_injected_tombstone_failure_leaves_deletion_and_conditional_authority_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected_active: bool
) -> None:
    """DS-C6: record-before-append failure does not undo unlink; active clears in finally."""
    server = local_server(tmp_path)
    current = await payload(server, "new_session", {"name": "Active"})
    sid = current["data"]["session_id"]
    target_id = sid if selected_active else TARGET
    target = tmp_path / f"{sid}.json" if selected_active else write_target(tmp_path)
    sidecar = tmp_path / f"{sid}.events.jsonl"
    before = sidecar.read_bytes()
    ordinary_record = JsonlEventRecorder.record

    def fail_delete_only(recorder: JsonlEventRecorder, invocation: Any) -> None:
        if invocation.tool_name == "delete_session":
            raise OSError("synthetic tombstone record failure")
        ordinary_record(recorder, invocation)

    with monkeypatch.context() as change:
        change.setattr(JsonlEventRecorder, "record", fail_delete_only)
        failed = await call(server, "delete_session", {"session_id": target_id})
    assert failed.isError is True and not target.exists()
    assert sidecar.read_bytes() == before  # No claim that a tombstone was durable.
    assert (await payload(server, "get_pipeline_state", {}))["state"] == current["state"]
    saved = await call(server, "save_session", {"session_id": sid})
    if selected_active:
        assert saved.isError is True and json.loads(saved.content[0].text)["active_session_id"] is None
        assert sidecar.read_bytes() == before
    else:
        assert saved.isError is False and json.loads(saved.content[0].text)["success"] is True
        assert (tmp_path / f"{sid}.json").exists()


@pytest.mark.asyncio
async def test_active_delete_returns_public_projection_and_keeps_private_working_graph(tmp_path: Path) -> None:
    """DS-C2/C3: canonical removal is not memory erasure or blob deletion."""
    marker = "synthetic/internal.csv"
    graph = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="main",
            options={"path": marker, "file": marker, "blob_ref": "synthetic-blob"},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="Bound source"),
        version=7,
    )
    write_target(tmp_path, graph)
    server = local_server(tmp_path)
    await payload(server, "load_session", {"session_id": TARGET})
    deleted = await payload(server, "delete_session", {"session_id": TARGET})
    assert deleted["success"] is True and not (tmp_path / f"{TARGET}.json").exists()
    options = deleted["state"]["sources"]["source"]["options"]
    assert options["path"] == options["file"] == REDACTED_BLOB_SOURCE_PATH
    assert options["blob_ref"] == "synthetic-blob" and marker not in json.dumps(deleted)
    assert json.loads(rows(tmp_path, TARGET)[-1]["result_canonical"]) == deleted
    assert (await payload(server, "get_pipeline_state", {}))["state"]["sources"]["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH
    assert (await payload(server, "diff_pipeline", {}))["data"]["total_changes"] == 0


def test_manager_delete_retains_lock_registry_sidecar_and_has_no_server_authority_effect(tmp_path: Path) -> None:
    """DS-C2/C4: real helper effect; private token is not a delete precondition."""
    target = write_target(tmp_path)
    manager = SessionManager(tmp_path)
    checkout = manager.checkout(TARGET)
    lock = session_module._session_lock(TARGET)
    sidecar = tmp_path / f"{TARGET}.events.jsonl"
    sidecar.write_bytes(b"synthetic sidecar\n")
    with pytest.raises(RuntimeError, match="server-owned persistence authority"):
        _dispatch_tool("delete_session", {"session_id": TARGET}, state(), create_catalog_service(), tmp_path)
    assert target.exists()
    manager.delete(TARGET)
    assert not target.exists() and sidecar.read_bytes() == b"synthetic sidecar\n"
    assert (tmp_path / f".{TARGET}.lock").exists() and session_module._session_lock(TARGET) is lock
    assert checkout.session_id == TARGET and checkout.state.to_dict() == state().to_dict()  # Immutable old checkout object still exists.
    with pytest.raises(SessionNotFoundError):
        manager.delete(TARGET)


@pytest.mark.asyncio
async def test_cancellation_before_handler_start_preserves_target_and_audit(tmp_path: Path) -> None:
    """DS-C7 scheduling only, not mid-unlink/fsync/transport rollback proof."""
    target = write_target(tmp_path)
    before = target.read_bytes()
    server = local_server(tmp_path)
    task = asyncio.create_task(call(server, "delete_session", {"session_id": TARGET}))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert target.read_bytes() == before and not list(tmp_path.glob("*.events.jsonl"))
