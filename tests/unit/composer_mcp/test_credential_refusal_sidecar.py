"""Credential refusals in MCP tool arguments leave value-free audit evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from elspeth.composer_mcp.audit import verify_events_sidecar_integrity
from elspeth.composer_mcp.server import create_server
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.web.composer import tool_error_payloads
from elspeth.web.composer.audit import CREDENTIAL_REFUSAL_ERROR_MESSAGE
from elspeth.web.dependencies import create_catalog_service
from tests.unit.composer_mcp.test_call_tool_audit import _call_handler


@pytest.mark.asyncio
async def test_dispatch_credential_refusal_sidecar_omits_credential_and_keeps_following_call(tmp_path: Path) -> None:
    canary = "sk-" + "q" * 24
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    server = create_server(
        create_catalog_service(),
        scratch,
        recorder=None,
        runtime_preflight=None,
        runtime_preflight_settings_hash=None,
    )

    await _call_handler(server.request_handlers, "new_session", {"name": "audit"})
    refused = await _call_handler(server.request_handlers, "set_metadata", {"patch": {"name": canary}})
    accepted = await _call_handler(server.request_handlers, "set_metadata", {"patch": {"name": "ordinary name"}})

    assert refused.root.isError is True
    assert accepted.root.isError is not True
    (sidecar,) = scratch.glob("*.events.jsonl")
    whole_sidecar = sidecar.read_text(encoding="utf-8")
    assert canary not in whole_sidecar
    assert "ordinary name" in whole_sidecar
    rows = [json.loads(line) for line in whole_sidecar.splitlines()]
    assert [row["tool_name"] for row in rows] == ["new_session", "set_metadata", "set_metadata"]
    refusal_row = rows[1]
    assert refusal_row["status"] == "arg_error"
    assert refusal_row["error_category"] == "credential_material"
    assert refusal_row["error_class"] == "CredentialMaterialRefused"
    assert refusal_row["error_message"] == CREDENTIAL_REFUSAL_ERROR_MESSAGE
    projection = tool_error_payloads.arg_error_arguments_projection(
        error_class="CredentialMaterialRefused",
        field_count=1,
    )
    # The row's own error_class names the exception; the stored projection carries
    # only the redacted marker, because this class is outside the closed set.
    assert json.loads(refusal_row["arguments_canonical"]) == {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "<redacted-arg-error-class>",
        "field_count": 1,
    }
    assert refusal_row["arguments_canonical"] == canonical_json(projection)
    assert refusal_row["arguments_hash"] == stable_hash(projection)
    verify_events_sidecar_integrity(sidecar)


def test_arg_error_arguments_projection_exact_dict() -> None:
    assert tool_error_payloads.arg_error_arguments_projection(
        error_class="CredentialMaterialRefused",
        field_count=2,
    ) == {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "<redacted-arg-error-class>",
        "field_count": 2,
    }
