"""Local MCP load: custody, refusal, projection and existing nonconformance."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest, TextContent

from elspeth.composer_mcp.audit import JsonlEventRecorder, verify_events_sidecar_integrity
from elspeth.composer_mcp.server import _build_tool_defs, _dispatch_tool, create_server
from elspeth.composer_mcp.session import SessionManager
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


def disk_state(name: str = "Disk graph") -> CompositionState:
    return CompositionState(
        nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name=name, description="Synthetic saved graph"), version=7
    )


def write_session(scratch: Path, state: CompositionState | None = None) -> bytes:
    SessionManager(scratch).save(TARGET, disk_state() if state is None else state)
    return (scratch / f"{TARGET}.json").read_bytes()


async def call(server: Server, name: str, arguments: dict[str, Any] | None = None):
    request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
    return (await server.request_handlers[CallToolRequest](request)).root


async def payload(server: Server, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    result = await call(server, name, arguments)
    assert result.isError is False and result.structuredContent is None
    assert len(result.content) == 1 and isinstance(result.content[0], TextContent)
    return json.loads(result.content[0].text)


def rows(scratch: Path, session_id: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (scratch / f"{session_id}.events.jsonl").read_text().splitlines()]


@pytest.mark.asyncio
async def test_actual_offering_is_required_id_without_path_authority(tmp_path: Path) -> None:
    """LS-C1: actual SDK declaration, not an assumed Web/provider variant."""
    server = local_server(tmp_path)
    offered = await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    selected = [tool for tool in offered.root.tools if tool.name == "load_session"]
    assert len(selected) == 1
    tool = selected[0]
    definition = next(item for item in _build_tool_defs() if item["name"] == "load_session")
    assert tool.description == definition["description"] == "Load a previously saved composition session."
    assert (
        tool.inputSchema
        == definition["parameters"]
        == {
            "type": "object",
            "properties": {"session_id": {"type": "string", "description": "Session ID to load."}},
            "required": ["session_id"],
            "additionalProperties": False,
        }
    )
    assert tool.annotations is None and tool.outputSchema is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("already_active", [False, True], ids=["initial", "replace-unsaved"])
async def test_load_adopts_disk_state_checkout_and_baseline_without_rewriting_file(tmp_path: Path, already_active: bool) -> None:
    """LS-C2/C3: real saved bytes, public SDK and active checkout observers."""
    before = write_session(tmp_path)
    server = local_server(tmp_path)
    if already_active:
        old = await payload(server, "new_session", {"name": "Old"})
        old_id = old["data"]["session_id"]
        old_bytes = (tmp_path / f"{old_id}.json").read_bytes()
        await payload(server, "set_metadata", {"patch": {"name": "Unsaved"}})
        assert (await payload(server, "diff_pipeline", {}))["data"]["total_changes"] > 0
    loaded = await payload(server, "load_session", {"session_id": TARGET})
    assert set(loaded) == {"success", "data", "state"} and loaded["success"] is True
    assert loaded["data"] == {"session_id": TARGET}
    assert loaded["state"] == disk_state().to_dict()
    assert (tmp_path / f"{TARGET}.json").read_bytes() == before
    assert SessionManager(tmp_path).checkout(TARGET).state.validate().is_valid is False
    event = rows(tmp_path, TARGET)[-1]
    assert event["tool_name"] == "load_session" and event["status"] == "success"
    assert event["actor"] == "composer-mcp:cli" and event["version_after"] == 7
    assert json.loads(event["arguments_canonical"]) == {"session_id": TARGET}
    assert json.loads(event["result_canonical"]) == loaded
    verify_events_sidecar_integrity(tmp_path / f"{TARGET}.events.jsonl")
    diff = await payload(server, "diff_pipeline", {})
    assert diff["data"]["from_version"] == diff["version"] == 7 and diff["data"]["total_changes"] == 0
    if already_active:
        assert (tmp_path / f"{old_id}.json").read_bytes() == old_bytes
        refused = await call(server, "save_session", {"session_id": old_id})
        assert refused.isError is True
        assert json.loads(refused.content[0].text)["code"] == "session_checkout_mismatch"
    assert (await payload(server, "save_session", {"session_id": TARGET}))["success"] is True
    assert (tmp_path / f"{TARGET}.json").read_bytes() == before


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
        {"session_id": TARGET, "path": "elsewhere"},
        {"session_id": TARGET.upper()},
        {"session_id": "a" * 11},
        {"session_id": "a" * 13},
        {"session_id": "../escape"},
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
        "extra-path",
        "uppercase",
        "short",
        "long",
        "traversal",
        "newline",
    ],
)
async def test_invalid_arguments_preserve_state_checkout_file_and_diff(tmp_path: Path, arguments: dict[str, Any] | None) -> None:
    """LS-C1/C4: schema/semantic refusal is distinct from missing-file TC019."""
    target_bytes = write_session(tmp_path)
    server = local_server(tmp_path)
    active = await payload(server, "new_session", {"name": "Active"})
    active_id = active["data"]["session_id"]
    active_bytes = (tmp_path / f"{active_id}.json").read_bytes()
    edited = await payload(server, "set_metadata", {"patch": {"name": "Unsaved"}})
    before_diff = await payload(server, "diff_pipeline", {})
    refused = await call(server, "load_session", arguments)
    assert refused.isError is True
    assert (tmp_path / f"{TARGET}.json").read_bytes() == target_bytes
    assert (tmp_path / f"{active_id}.json").read_bytes() == active_bytes
    assert (await payload(server, "get_pipeline_state", {}))["state"] == edited["state"]
    assert await payload(server, "diff_pipeline", {}) == before_diff
    refusal = next(row for row in reversed(rows(tmp_path, active_id)) if row["tool_name"] == "load_session")
    assert refusal["status"] == "arg_error" and refusal["error_class"] == "ToolArgumentError"
    assert refusal["version_after"] is None
    assert not (tmp_path / f"{TARGET}.events.jsonl").exists()
    assert (await payload(server, "save_session", {"session_id": active_id}))["success"] is True


@pytest.mark.asyncio
async def test_tc019_missing_load_preserves_graph_checkout_and_diff_baseline(tmp_path: Path) -> None:
    """TC-019 (#327): an unsuccessful load keeps the intended diff baseline.

    Formerly KNOWN_NONCONFORMANCE: the false result rebased the baseline onto
    the unsaved working state, so the next diff reported zero changes.
    """
    server = local_server(tmp_path)
    first = await payload(server, "new_session", {"name": "Baseline"})
    active_id = first["data"]["session_id"]
    original = (tmp_path / f"{active_id}.json").read_bytes()
    edited = await payload(server, "set_metadata", {"patch": {"name": "Unsaved"}})
    before = await payload(server, "diff_pipeline", {})
    assert before["data"]["total_changes"] > 0 and before["data"]["from_version"] == 1
    refused = await payload(server, "load_session", {"session_id": MISSING})
    assert refused == {"success": False, "error": f"Session not found: {MISSING}", "state": edited["state"]}
    assert (tmp_path / f"{active_id}.json").read_bytes() == original
    assert not (tmp_path / f"{MISSING}.json").exists()
    event = rows(tmp_path, active_id)[-1]
    assert event["tool_name"] == "load_session" and event["status"] == "success"
    assert json.loads(event["result_canonical"]) == refused and event["version_after"] == edited["state"]["version"]
    after = await payload(server, "diff_pipeline", {})
    assert after == before
    assert after["data"]["from_version"] == 1 and after["data"]["total_changes"] > 0
    assert (await payload(server, "get_pipeline_state", {}))["state"] == edited["state"]
    assert (await payload(server, "save_session", {"session_id": active_id}))["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [b"\xff", b"\xef\xbb\xbf{}", b"{", b"[]", b"{}", b'{"nodes":[null]}'],
    ids=["utf8", "bom", "json", "nonobject", "missing-keys", "nested-invalid"],
)
async def test_corrupt_file_is_crash_not_notfound_and_preserves_old_baseline(tmp_path: Path, raw: bytes) -> None:
    """LS-C4: real malformed disk input, no parser or dispatcher replacement."""
    write_session(tmp_path)
    target = tmp_path / f"{TARGET}.json"
    target.write_bytes(raw)
    server = local_server(tmp_path)
    active = await payload(server, "new_session", {"name": "Retained"})
    active_id = active["data"]["session_id"]
    edited = await payload(server, "set_metadata", {"patch": {"name": "Unsaved"}})
    before = await payload(server, "diff_pipeline", {})
    refused = await call(server, "load_session", {"session_id": TARGET})
    assert refused.isError is True and "Corrupt session file" in refused.content[0].text
    assert target.read_bytes() == raw
    assert (await payload(server, "get_pipeline_state", {}))["state"] == edited["state"]
    assert await payload(server, "diff_pipeline", {}) == before
    row = next(row for row in reversed(rows(tmp_path, active_id)) if row["tool_name"] == "load_session")
    assert row["status"] == "plugin_crash" and row["error_class"] == row["error_message"] == "CorruptSessionFileError"
    assert row["result_canonical"] is None and row["version_after"] is None
    assert not (tmp_path / f"{TARGET}.events.jsonl").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("recognized", [False, True], ids=["benign-control", "tc018-recognized"])
@pytest.mark.parametrize("already_active", [False, True], ids=["pre-resolution", "active-session"])
async def test_tc018_loaded_state_is_admitted_before_adoption_response_and_audit(
    tmp_path: Path, recognized: bool, already_active: bool
) -> None:
    """TC-018 (#327): disk-derived state passes credential admission before it is used.

    Formerly KNOWN_NONCONFORMANCE: a recognized synthetic value in a saved
    file was adopted, returned and audited, and only the next call's
    old-state guard refused. A refused load must now leave the working
    graph, diff baseline, checkout and audit scope as they were, and no
    response or audit row may carry the disk value. No real credential.
    """
    name = "sk-" + "a" * 24 if recognized else "benign-label"
    state = disk_state(name)
    if recognized:
        with pytest.raises(CredentialMaterialRefused):
            require_no_credential_material_in_state(state, surface="synthetic-positive-control")
    else:
        require_no_credential_material_in_state(state, surface="synthetic-negative-control")
    before = write_session(tmp_path, state)
    server = local_server(tmp_path)
    if already_active:
        active = await payload(server, "new_session", {"name": "Retained"})
        active_id = active["data"]["session_id"]
        edited = await payload(server, "set_metadata", {"patch": {"name": "Unsaved"}})
        diff_before = await payload(server, "diff_pipeline", {})
    response = await call(server, "load_session", {"session_id": TARGET})
    assert (tmp_path / f"{TARGET}.json").read_bytes() == before
    if not recognized:
        assert response.isError is False
        loaded = json.loads(response.content[0].text)
        assert loaded["success"] is True and loaded["state"]["metadata"]["name"] == name
        event = rows(tmp_path, TARGET)[-1]
        assert event["status"] == "success" and json.loads(event["result_canonical"])["state"]["metadata"]["name"] == name
        assert (await payload(server, "get_pipeline_state", {}))["state"] == loaded["state"]
        return
    assert response.isError is True and name not in response.content[0].text
    assert not (tmp_path / f"{TARGET}.events.jsonl").exists()
    if already_active:
        assert (await payload(server, "get_pipeline_state", {}))["state"] == edited["state"]
        assert await payload(server, "diff_pipeline", {}) == diff_before
        assert (await payload(server, "save_session", {"session_id": active_id}))["success"] is True
        audit_sid = active_id
    else:
        assert list(tmp_path.glob("*.events.jsonl")) == []
        later = await payload(server, "new_session", {"name": "Later resolution"})
        audit_sid = later["data"]["session_id"]
    audit_rows = rows(tmp_path, audit_sid)
    refused_rows = [row for row in audit_rows if row["tool_name"] == "load_session"]
    assert len(refused_rows) == 1
    assert refused_rows[0]["status"] == "plugin_crash"
    assert refused_rows[0]["error_class"] == refused_rows[0]["error_message"] == "CredentialMaterialRefused"
    assert refused_rows[0]["result_canonical"] is None and refused_rows[0]["version_after"] is None
    assert name not in (tmp_path / f"{audit_sid}.events.jsonl").read_text()
    verify_events_sidecar_integrity(tmp_path / f"{audit_sid}.events.jsonl")


@pytest.mark.asyncio
async def test_contaminated_old_state_blocks_loading_a_clean_target(tmp_path: Path) -> None:
    """LS-C4 boundary: old-state guard refuses even an attempted clean load.

    Since TC-018 the server refuses a contaminated load, so a contaminated
    working state is no longer reachable through MCP calls. The old-state
    guard remains as defence in depth and is exercised at the dispatcher.
    """
    synthetic = "sk-" + "a" * 24
    write_session(tmp_path, disk_state(synthetic))
    clean_id = "b123456789cd"
    SessionManager(tmp_path).save(clean_id, disk_state("Clean"))
    clean_bytes = (tmp_path / f"{clean_id}.json").read_bytes()
    server = local_server(tmp_path)
    refused_dirty = await call(server, "load_session", {"session_id": TARGET})
    assert refused_dirty.isError is True and synthetic not in refused_dirty.content[0].text
    loaded_clean = await payload(server, "load_session", {"session_id": clean_id})
    assert loaded_clean["state"]["metadata"]["name"] == "Clean"
    assert (tmp_path / f"{clean_id}.json").read_bytes() == clean_bytes

    manager = SessionManager(tmp_path)
    checkout_ref: list[Any] = [None]
    with pytest.raises(CredentialMaterialRefused):
        _dispatch_tool(
            "load_session",
            {"session_id": clean_id},
            disk_state(synthetic),
            create_catalog_service(),
            tmp_path,
            session_manager=manager,
            session_checkout_ref=checkout_ref,
        )
    assert checkout_ref == [None]
    assert (tmp_path / f"{clean_id}.json").read_bytes() == clean_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("already_active", [False, True], ids=["pre-resolution", "active-session"])
async def test_tc020_refused_extra_argument_is_withheld_from_immediate_or_buffered_audit(tmp_path: Path, already_active: bool) -> None:
    """TC-020 (#327): refused arguments reach audit only as a value-free stand-in.

    Formerly KNOWN_NONCONFORMANCE: the raw canonical arguments were captured
    before credential admission and recorded after it refused.
    """
    write_session(tmp_path)
    synthetic = "sk-" + "a" * 24
    args = {"session_id": TARGET, "extra": synthetic}
    with pytest.raises(CredentialMaterialRefused) as caught:
        require_no_credential_material_for_tool("load_session", args, surface="composer_mcp_tool_arguments")
    withheld = {"_arguments_withheld": caught.value.to_payload()}
    require_no_credential_material_for_tool("load_session", {"session_id": TARGET, "extra": "benign"}, surface="synthetic-negative-control")
    server = local_server(tmp_path)
    if already_active:
        active = await payload(server, "new_session", {"name": "Retained"})
        sid = active["data"]["session_id"]
    refused = await call(server, "load_session", args)
    assert refused.isError is True and synthetic not in refused.content[0].text
    assert not (tmp_path / f"{TARGET}.events.jsonl").exists()
    if already_active:
        assert (await payload(server, "get_pipeline_state", {}))["state"] == active["state"]
    else:
        await payload(server, "load_session", {"session_id": TARGET})
        sid = TARGET
    refused_rows = [row for row in rows(tmp_path, sid) if row["tool_name"] == "load_session" and row["status"] == "plugin_crash"]
    assert len(refused_rows) == 1
    assert refused_rows[0]["error_class"] == "CredentialMaterialRefused"
    assert json.loads(refused_rows[0]["arguments_canonical"]) == withheld
    assert refused_rows[0]["arguments_hash"] == stable_hash(withheld) != stable_hash(args)
    assert synthetic not in (tmp_path / f"{sid}.events.jsonl").read_text()
    assert synthetic not in (tmp_path / f"{TARGET}.json").read_text()


@pytest.mark.asyncio
async def test_load_redacts_blob_path_only_after_private_adoption(tmp_path: Path) -> None:
    """LS-C2/C5: public source projection differs from durable/private state."""
    path_marker = "synthetic/internal-storage.csv"
    state = CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="main",
            options={"path": path_marker, "file": path_marker, "blob_ref": "synthetic-blob"},
            on_validation_failure="discard",
        ),
        nodes=(),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(name="Blob-bound"),
        version=7,
    )
    before = write_session(tmp_path, state)
    server = local_server(tmp_path)
    loaded = await payload(server, "load_session", {"session_id": TARGET})
    options = loaded["state"]["sources"]["source"]["options"]
    assert options["path"] == options["file"] == REDACTED_BLOB_SOURCE_PATH
    assert options["blob_ref"] == "synthetic-blob"
    assert path_marker not in json.dumps(loaded)
    assert json.loads(rows(tmp_path, TARGET)[-1]["result_canonical"]) == loaded
    assert (tmp_path / f"{TARGET}.json").read_bytes() == before
    assert SessionManager(tmp_path).checkout(TARGET).state.sources["source"].options["path"] == path_marker


@pytest.mark.asyncio
async def test_post_load_audit_failure_does_not_roll_back_active_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LS-C6: injected recorder failure after real load/adoption; not OS incidence."""
    before = write_session(tmp_path)
    server = local_server(tmp_path)

    def fail_record(self: JsonlEventRecorder, invocation: object) -> None:
        raise OSError("synthetic audit failure")

    with monkeypatch.context() as change:
        change.setattr(JsonlEventRecorder, "record", fail_record)
        failed = await call(server, "load_session", {"session_id": TARGET})
    assert failed.isError is True
    assert not (tmp_path / f"{TARGET}.events.jsonl").exists()
    assert (tmp_path / f"{TARGET}.json").read_bytes() == before
    assert (await payload(server, "get_pipeline_state", {}))["state"] == disk_state().to_dict()
    assert (await payload(server, "diff_pipeline", {}))["data"]["total_changes"] == 0
    assert (await payload(server, "save_session", {"session_id": TARGET}))["success"] is True
    assert (tmp_path / f"{TARGET}.json").read_bytes() == before


@pytest.mark.asyncio
async def test_raw_byte_checkout_survives_normalization_and_rejects_external_formatting_change(tmp_path: Path) -> None:
    """LS-C2/C3: load leaves bytes untouched; save observer measures loaded token."""
    data = disk_state().to_dict()
    data["unknown_legacy_field"] = "not an epoch"
    raw = json.dumps(data, separators=(",", ":")).encode()
    tmp_path.joinpath(f"{TARGET}.json").write_bytes(raw)
    checkout = SessionManager(tmp_path).checkout(TARGET)
    assert checkout.token._digest == hashlib.sha256(raw).hexdigest()
    assert "unknown_legacy_field" not in checkout.state.to_dict()
    server = local_server(tmp_path)
    loaded = await payload(server, "load_session", {"session_id": TARGET})
    assert loaded["state"] == disk_state().to_dict()
    assert tmp_path.joinpath(f"{TARGET}.json").read_bytes() == raw
    reformatted = json.dumps(data, indent=4).encode()
    tmp_path.joinpath(f"{TARGET}.json").write_bytes(reformatted)
    await payload(server, "set_metadata", {"patch": {"name": "Unsaved"}})
    refused = await call(server, "save_session", {"session_id": TARGET})
    assert refused.isError is True and "Refusing stale save" in refused.content[0].text
    assert tmp_path.joinpath(f"{TARGET}.json").read_bytes() == reformatted
    reloaded = await payload(server, "load_session", {"session_id": TARGET})
    assert reloaded["state"] == loaded["state"]
    assert (await payload(server, "diff_pipeline", {}))["data"]["total_changes"] == 0


def test_manager_load_is_not_server_adoption_and_dispatch_requires_authority(tmp_path: Path) -> None:
    """LS-C2/C4: direct library reconstruction versus public embedding context."""
    before = write_session(tmp_path)
    manager = SessionManager(tmp_path)
    assert manager.load(TARGET).to_dict() == disk_state().to_dict()
    assert not list(tmp_path.glob("*.events.jsonl"))
    with pytest.raises(RuntimeError, match="server-owned persistence authority"):
        _dispatch_tool("load_session", {"session_id": TARGET}, disk_state("Clean current"), create_catalog_service(), tmp_path)
    assert (tmp_path / f"{TARGET}.json").read_bytes() == before


@pytest.mark.asyncio
async def test_cancellation_before_handler_start_does_not_load_or_record(tmp_path: Path) -> None:
    """LS-C7 scheduling only; not mid-read/lock/fsync/transport rollback proof."""
    before = write_session(tmp_path)
    server = local_server(tmp_path)
    task = asyncio.create_task(call(server, "load_session", {"session_id": TARGET}))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / f"{TARGET}.json").read_bytes() == before
    assert not list(tmp_path.glob("*.events.jsonl"))
