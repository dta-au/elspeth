"""Observable local-MCP creation, refusal and partial-effect controls."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest, TextContent

from elspeth.composer_mcp import session as session_module
from elspeth.composer_mcp.audit import JsonlEventRecorder, verify_events_sidecar_integrity
from elspeth.composer_mcp.server import _build_tool_defs, _dispatch_tool, create_server
from elspeth.composer_mcp.session import SessionManager
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material_for_tool
from elspeth.web.dependencies import create_catalog_service


def local_server(scratch: Path) -> Server:
    return create_server(create_catalog_service(), scratch, runtime_preflight=None, runtime_preflight_settings_hash=None)


async def call(server: Server, name: str, arguments: dict[str, Any] | None = None):
    request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
    return (await server.request_handlers[CallToolRequest](request)).root


async def payload(server: Server, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    response = await call(server, name, arguments)
    assert response.isError is False
    assert len(response.content) == 1 and isinstance(response.content[0], TextContent)
    assert response.structuredContent is None
    return json.loads(response.content[0].text)


def rows(scratch: Path, session_id: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (scratch / f"{session_id}.events.jsonl").read_text().splitlines()]


@pytest.mark.asyncio
async def test_actual_offering_is_local_closed_optional_name(tmp_path: Path) -> None:
    """NS-C1: actual SDK list handler matches exact registered declaration."""
    server = local_server(tmp_path)
    result = await server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
    selected = [tool for tool in result.root.tools if tool.name == "new_session"]
    assert len(selected) == 1
    tool = selected[0]
    definition = next(item for item in _build_tool_defs() if item["name"] == "new_session")
    assert tool.description == definition["description"] == "Create a new empty composition session. Returns session_id."
    assert (
        tool.inputSchema
        == definition["parameters"]
        == {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Pipeline name (default: 'Untitled Pipeline')."}},
            "required": [],
            "additionalProperties": False,
        }
    )
    assert tool.annotations is None and tool.outputSchema is None
    assert not list(tmp_path.iterdir())  # Listing/initialization has not created a session.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments,expected",
    [
        (None, "Untitled Pipeline"),
        ({}, "Untitled Pipeline"),
        ({"name": "Synthetic"}, "Synthetic"),
        ({"name": ""}, ""),
        ({"name": " spaced "}, " spaced "),
        ({"name": "Référence_☃"}, "Référence_☃"),
        ({"name": "N" * 4097}, "N" * 4097),
    ],
    ids=["whole-null", "omitted-name", "ordinary", "empty", "whitespace", "unicode", "long"],
)
async def test_new_immediately_persists_exact_empty_state_and_audit(
    tmp_path: Path, arguments: dict[str, Any] | None, expected: str
) -> None:
    """NS-C2/C3/C5: local tool success has a file and exact audited response."""
    scratch = tmp_path / "sessions"
    server = local_server(scratch)
    result = await payload(server, "new_session", arguments)
    assert set(result) == {"success", "data", "state"} and result["success"] is True
    session_id = result["data"]["session_id"]
    assert re.fullmatch(r"[0-9a-f]{12}", session_id)
    assert result["data"] == {"session_id": session_id, "name": expected}
    checkout = SessionManager(scratch).checkout(session_id)
    assert checkout.state.metadata.name == expected and checkout.state.version == 1
    assert (
        checkout.state.to_dict()
        == result["state"]
        == {
            "sources": {},
            "nodes": [],
            "edges": [],
            "outputs": [],
            "metadata": {"name": expected, "description": ""},
            "version": 1,
        }
    )
    assert (scratch / f"{session_id}.json").read_bytes() == json.dumps(result["state"], indent=2, sort_keys=True).encode()
    assert checkout.state.validate().is_valid is False  # Creation is not runnable-pipeline acceptance.
    audit = rows(scratch, session_id)
    assert len(audit) == 1
    assert audit[0]["tool_name"] == "new_session" and audit[0]["status"] == "success"
    assert audit[0]["actor"] == "composer-mcp:cli"
    assert audit[0]["version_before"] == audit[0]["version_after"] == 1
    assert json.loads(audit[0]["result_canonical"]) == result
    assert json.loads(audit[0]["arguments_canonical"]) == (arguments or {})
    verify_events_sidecar_integrity(scratch / f"{session_id}.events.jsonl")
    assert not list(scratch.glob("*.tmp"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{"name": None}, {"name": 42}, {"name": True}, {"name": []}, {"name": {}}, {"name": "Safe", "session_id": "a" * 12}],
    ids=["name-null", "number", "boolean", "array", "object", "extra-authority"],
)
async def test_bad_shape_preserves_active_file_state_and_records_arg_error(tmp_path: Path, arguments: dict[str, Any]) -> None:
    """NS-C1/C4: refusal cannot replace the active graph/checkout or allocate file."""
    server = local_server(tmp_path)
    created = await payload(server, "new_session", {"name": "Retained"})
    session_id = created["data"]["session_id"]
    before = (tmp_path / f"{session_id}.json").read_bytes()
    result = await call(server, "new_session", arguments)
    assert result.isError is True
    assert sorted(path.name for path in tmp_path.glob("*.json")) == [f"{session_id}.json"]
    assert (tmp_path / f"{session_id}.json").read_bytes() == before
    failed = rows(tmp_path, session_id)[-1]
    assert failed["status"] == "arg_error" and failed["version_after"] is None
    assert failed["error_class"] == "ToolArgumentError"
    current = await payload(server, "get_pipeline_state", {})
    assert current["state"] == created["state"]
    saved = await payload(server, "save_session", {"session_id": session_id})
    assert saved["success"] is True  # Original active checkout remains authoritative.


@pytest.mark.asyncio
async def test_second_new_replaces_working_graph_and_diff_baseline_without_saving_old_edits(tmp_path: Path) -> None:
    """NS-C3/C6: repeated calls add sessions and intentionally discard unsaved work."""
    server = local_server(tmp_path)
    first = await payload(server, "new_session", {"name": "First"})
    first_id = first["data"]["session_id"]
    first_bytes = (tmp_path / f"{first_id}.json").read_bytes()
    edited = await payload(server, "set_metadata", {"patch": {"name": "Unsaved edit"}})
    assert edited["state"]["metadata"]["name"] == "Unsaved edit"
    assert edited["state"]["version"] > 1
    assert (tmp_path / f"{first_id}.json").read_bytes() == first_bytes
    second = await payload(server, "new_session", {"name": "Second"})
    second_id = second["data"]["session_id"]
    assert second_id != first_id
    assert sorted(path.stem for path in tmp_path.glob("*.json")) == sorted([first_id, second_id])
    assert (tmp_path / f"{first_id}.json").read_bytes() == first_bytes
    assert second["state"]["version"] == 1 and second["state"]["metadata"]["name"] == "Second"
    diff = await payload(server, "diff_pipeline", {})
    assert diff["data"]["from_version"] == diff["version"] == 1
    assert diff["data"]["total_changes"] == 0
    assert rows(tmp_path, second_id)[0]["tool_name"] == "new_session"
    refused_old_save = await call(server, "save_session", {"session_id": first_id})
    assert refused_old_save.isError is True
    assert json.loads(refused_old_save.content[0].text)["code"] == "session_checkout_mismatch"


@pytest.mark.asyncio
async def test_save_failure_does_not_adopt_proposed_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NS-C4: inject actual persistence failure before replace, retaining old state."""
    server = local_server(tmp_path)
    first = await payload(server, "new_session", {"name": "Retained"})
    first_id = first["data"]["session_id"]
    original = (tmp_path / f"{first_id}.json").read_bytes()

    def fail_write(self: SessionManager, path: Path, serialized: bytes) -> None:
        raise OSError("synthetic write failure")

    with monkeypatch.context() as change:
        change.setattr(SessionManager, "_atomic_write", fail_write)
        response = await call(server, "new_session", {"name": "Not adopted"})
    assert response.isError is True
    assert (tmp_path / f"{first_id}.json").read_bytes() == original
    assert sorted(path.stem for path in tmp_path.glob("*.json")) == [first_id]
    fault = rows(tmp_path, first_id)[-1]
    assert fault["status"] == "plugin_crash" and fault["error_class"] == "OSError"
    assert fault["result_canonical"] is None
    assert (await payload(server, "get_pipeline_state", {}))["state"] == first["state"]


@pytest.mark.asyncio
async def test_audit_failure_after_create_leaves_file_and_active_state_retry_adds_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NS-C5/C6: absence of successful response is not rollback of saved session."""
    server = local_server(tmp_path)

    def fail_record(self: JsonlEventRecorder, invocation: object) -> None:
        raise OSError("synthetic audit failure")

    with monkeypatch.context() as change:
        change.setattr(JsonlEventRecorder, "record", fail_record)
        response = await call(server, "new_session", {"name": "Committed before audit"})
    assert response.isError is True
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    first_id = files[0].stem
    assert not (tmp_path / f"{first_id}.events.jsonl").exists()
    current = await payload(server, "get_pipeline_state", {})
    assert current["state"]["metadata"]["name"] == "Committed before audit"
    retry = await payload(server, "new_session", {"name": "Committed before audit"})
    assert retry["data"]["session_id"] != first_id
    assert len(list(tmp_path.glob("*.json"))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True], ids=["pre-resolution", "active-session"])
async def test_tc020_refused_synthetic_control_is_retained_in_audit_arguments(tmp_path: Path, active: bool) -> None:
    """KNOWN_NONCONFORMANCE TC-020 characterization, not a security PASS."""
    synthetic = "sk-" + "a" * 24
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool("new_session", {"name": synthetic}, surface="synthetic-control")
    require_no_credential_material_for_tool("new_session", {"name": "benign-label"}, surface="synthetic-negative-control")
    server = local_server(tmp_path)
    if active:
        clean = await payload(server, "new_session", {"name": "Retained"})
        session_id = clean["data"]["session_id"]
        before = (tmp_path / f"{session_id}.json").read_bytes()
    refused = await call(server, "new_session", {"name": synthetic})
    assert refused.isError is True
    assert synthetic not in refused.content[0].text
    if not active:
        assert list(tmp_path.iterdir()) == []
        clean = await payload(server, "new_session", {"name": "Later resolution"})
        session_id = clean["data"]["session_id"]
    else:
        assert (tmp_path / f"{session_id}.json").read_bytes() == before
    audit = rows(tmp_path, session_id)
    refused_rows = [row for row in audit if json.loads(row["arguments_canonical"]) == {"name": synthetic}]
    assert len(refused_rows) == 1
    assert refused_rows[0]["status"] == "plugin_crash"
    assert synthetic not in (tmp_path / f"{session_id}.json").read_text()
    assert (await payload(server, "get_pipeline_state", {}))["state"] == clean["state"]


def test_manager_helper_is_not_tool_persistence_and_dispatch_requires_authority(tmp_path: Path) -> None:
    """NS-C2: direct helper creation and local-MCP tool are different contracts."""
    manager = SessionManager(tmp_path)
    _, state = manager.new_session(name="Memory only")
    assert list(tmp_path.iterdir()) == [] and state.version == 1
    with pytest.raises(RuntimeError, match="server-owned persistence authority"):
        _dispatch_tool("new_session", {}, state, create_catalog_service(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_forced_id_collision_preserves_file_or_refuses_divergent_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NS-C6 boundary: no UUID collision loop; exact-byte save can be a no-op."""
    fixed = UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(session_module.uuid, "uuid4", lambda: fixed)
    manager = SessionManager(tmp_path)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="Before"), version=1)
    ref = [None]
    first = _dispatch_tool(
        "new_session", {"name": "Same"}, state, create_catalog_service(), tmp_path, session_manager=manager, session_checkout_ref=ref
    )
    file = tmp_path / f"{first['data']['session_id']}.json"
    before = file.read_bytes()
    again = _dispatch_tool(
        "new_session", {"name": "Same"}, state, create_catalog_service(), tmp_path, session_manager=manager, session_checkout_ref=ref
    )
    assert again["data"] == first["data"] and file.read_bytes() == before
    from elspeth.composer_mcp.session import StaleSessionVersionError

    with pytest.raises(StaleSessionVersionError):
        _dispatch_tool(
            "new_session",
            {"name": "Different"},
            state,
            create_catalog_service(),
            tmp_path,
            session_manager=manager,
            session_checkout_ref=ref,
        )
    assert file.read_bytes() == before


@pytest.mark.asyncio
async def test_cancellation_before_handler_start_has_no_creation_or_audit(tmp_path: Path) -> None:
    """NS-C7 narrow scheduling control; no mid-fsync/transport cancellation proof."""
    server = local_server(tmp_path)
    task = asyncio.create_task(call(server, "new_session", {"name": "Never started"}))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []
