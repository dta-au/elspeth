"""TC-020 (#327) red-team follow-ups: the MCP argument admission that precedes audit capture.

``call_tool`` admits the caller's arguments before anything records them.
These cases pin the properties the red-team found unpinned on 67c2ec2d3:

- F2: admission precedes the canonicalization sentinel, whose
  ``_payload_keys`` would otherwise persist a credential-shaped key;
- F4: admission uses the working state, so a field_mapper patch on an
  existing mapping label is not falsely refused;
- F5: a guard defect other than a refusal is audited as a plugin crash,
  never as a success.

Synthetic values only; no real credential.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server import Server
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

import elspeth.composer_mcp.server as server_module
from elspeth.composer_mcp.audit import events_sidecar_path, verify_events_sidecar_integrity
from elspeth.composer_mcp.server import create_server
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material_for_tool
from elspeth.web.dependencies import create_catalog_service

# Built from parts so the repository secret scanner does not read a literal.
SYNTHETIC_KEY_NAME = "AKIA" + "QWERTYUIOPASDFGH"


def _server(scratch: Path) -> Server:
    return create_server(create_catalog_service(), scratch, runtime_preflight=None, runtime_preflight_settings_hash=None)


async def _call(server: Server, name: str, arguments: dict[str, Any] | None) -> CallToolResult:
    result = await server.request_handlers[CallToolRequest](
        CallToolRequest(method="tools/call", params=CallToolRequestParams(name=name, arguments=arguments))
    )
    assert isinstance(result.root, CallToolResult)
    assert len(result.root.content) == 1
    assert isinstance(result.root.content[0], TextContent)
    return result.root


async def _active_session(server: Server) -> str:
    created = await _call(server, "new_session", {"name": "Active"})
    assert created.isError is False
    assert isinstance(created.content[0], TextContent)
    return str(json.loads(created.content[0].text)["data"]["session_id"])


def _rows(scratch: Path, sid: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in events_sidecar_path(scratch, sid).read_text().splitlines()]


@pytest.mark.asyncio
async def test_refused_key_with_uncanonicalizable_value_is_withheld_not_listed_in_the_sentinel(tmp_path: Path) -> None:
    """F2: a credential-shaped key must not reach audit through ``_payload_keys``.

    An integer above 2**53 fails canonicalization, whose sentinel records the
    sorted top-level keys. Admission runs first, so the refusal's value-free
    stand-in is recorded instead.
    """
    arguments = {SYNTHETIC_KEY_NAME: 2**60}
    with pytest.raises(CredentialMaterialRefused) as caught:
        require_no_credential_material_for_tool("list_sessions", arguments, surface="composer_mcp_tool_arguments")
    with pytest.raises(ValueError):
        canonical_json(arguments)
    withheld = {"_arguments_withheld": caught.value.to_payload()}
    server = _server(tmp_path)
    sid = await _active_session(server)

    refused = await _call(server, "list_sessions", arguments)

    assert refused.isError is True
    assert isinstance(refused.content[0], TextContent)
    assert SYNTHETIC_KEY_NAME not in refused.content[0].text
    row = _rows(tmp_path, sid)[-1]
    assert row["tool_name"] == "list_sessions"
    assert row["status"] == "plugin_crash" and row["error_class"] == "CredentialMaterialRefused"
    assert json.loads(row["arguments_canonical"]) == withheld
    assert row["arguments_hash"] == stable_hash(withheld)
    assert SYNTHETIC_KEY_NAME not in events_sidecar_path(tmp_path, sid).read_text()
    verify_events_sidecar_integrity(events_sidecar_path(tmp_path, sid))


@pytest.mark.asyncio
async def test_admission_uses_working_state_so_a_field_mapper_label_patch_is_not_refused(tmp_path: Path) -> None:
    """F4: the pre-audit admission must receive the working state.

    Without it, a patch to a field_mapper mapping whose existing label looks
    secret-like (``customer_key``) is refused as credential material.
    """
    server = _server(tmp_path)
    await _active_session(server)
    node = {
        "id": "rename_customer",
        "node_type": "transform",
        "plugin": "field_mapper",
        "input": "rows",
        "on_success": "output",
        "on_error": "discard",
        "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}},
    }
    patch_arguments = {"node_id": "rename_customer", "patch": {"mapping": {"customer_key": "cust_id"}}}
    with pytest.raises(CredentialMaterialRefused):
        require_no_credential_material_for_tool("patch_node_options", patch_arguments, surface="stateless-control")

    upserted = await _call(server, "upsert_node", node)
    assert upserted.isError is False
    patched = await _call(server, "patch_node_options", patch_arguments)

    assert patched.isError is False
    assert isinstance(patched.content[0], TextContent)
    assert json.loads(patched.content[0].text)["success"] is True


@pytest.mark.asyncio
async def test_a_guard_defect_is_audited_as_a_plugin_crash_not_a_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F5: a non-refusal exception from admission must not be recorded as SUCCESS."""
    server = _server(tmp_path)
    sid = await _active_session(server)

    def defective_guard(*_args: object, **_kwargs: object) -> None:
        raise TypeError("injected guard defect")

    monkeypatch.setattr(server_module, "require_no_credential_material_for_tool", defective_guard)
    failed = await _call(server, "list_sessions", {})

    assert failed.isError is True
    row = _rows(tmp_path, sid)[-1]
    assert row["tool_name"] == "list_sessions"
    assert row["status"] == "plugin_crash"
    assert row["error_class"] == row["error_message"] == "TypeError"
    assert row["result_canonical"] is None and row["version_after"] is None
