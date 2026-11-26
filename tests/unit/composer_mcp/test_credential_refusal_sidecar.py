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


@pytest.mark.parametrize(
    ("category", "tool_name"),
    [
        pytest.param("schema_shape", "set_pipeline", id="composer-schema-shape"),
        pytest.param("schema_bound", "get_pipeline_state", id="composer-schema-bound"),
        pytest.param("canonicalization", "set_metadata", id="pre-dispatch-canonicalization"),
    ],
)
@pytest.mark.asyncio
async def test_arg_error_sidecar_arguments_are_value_free(tmp_path: Path, category: str, tool_name: str) -> None:
    canary = "sk-" + "q" * 24
    arguments: dict[str, object]
    if category == "schema_bound":
        arguments = {"field_path": canary + "x" * 512}
    elif category == "schema_shape":
        arguments = {
            "source": {
                "plugin": "csv",
                "on_success": "out",
                "on_validation_failure": "discard",
                "inline_blob": {"filename": "data.csv", "mime_type": "text/csv", "content": canary},
            },
            "nodes": [{"id": "agg", "node_type": "aggregation", "input": 1}],
            "edges": [],
            "outputs": [],
        }
    else:
        # Canonicalization fails before any other check, so the canary as a top-level
        # argument NAME reaches the stored row unless the row is value-free.
        arguments = {"patch": {"name": "audit", "other": float("nan")}, canary: 1}

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
    response = await _call_handler(server.request_handlers, tool_name, arguments)

    assert response.root.isError is True
    assert canary not in response.root.content[0].text
    (sidecar,) = scratch.glob("*.events.jsonl")
    whole_sidecar = sidecar.read_text(encoding="utf-8")
    assert canary not in whole_sidecar
    row = [json.loads(line) for line in whole_sidecar.splitlines()][-1]
    assert row["status"] == "arg_error"
    assert row["error_category"] == category
    expected_class = "ValueError" if category == "canonicalization" else "ToolArgumentError"
    assert row["error_class"] == expected_class
    assert json.loads(row["arguments_canonical"]) == {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": expected_class,
        "field_count": len(arguments),
    }
    assert row["arguments_hash"] == stable_hash(json.loads(row["arguments_canonical"]))
    verify_events_sidecar_integrity(sidecar)


# Not credential-shaped, so the credential guard lets the call reach the validator under test.
_MARKER = "zzvaluemarker" + "q" * 16


@pytest.mark.parametrize(
    ("category", "tool_name", "arguments"),
    [
        pytest.param("model_validation", "set_metadata", {"patch": {"name": " ", "description": _MARKER}}, id="blank-metadata-name"),
        pytest.param(
            "semantic_rule",
            "set_source",
            {"source_name": "", "plugin": "csv", "options": {}, "on_success": _MARKER, "on_validation_failure": "discard"},
            id="invalid-source-name",
        ),
    ],
)
@pytest.mark.asyncio
async def test_handler_arg_error_categories_store_projection_without_argument_values(
    tmp_path: Path, category: str, tool_name: str, arguments: dict[str, object]
) -> None:
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
    response = await _call_handler(server.request_handlers, tool_name, arguments)

    assert response.root.isError is True
    assert _MARKER not in response.root.content[0].text
    (sidecar,) = scratch.glob("*.events.jsonl")
    whole_sidecar = sidecar.read_text(encoding="utf-8")
    assert _MARKER not in whole_sidecar
    row = [json.loads(line) for line in whole_sidecar.splitlines()][-1]
    assert row["status"] == "arg_error"
    assert row["error_class"] == "ToolArgumentError"
    assert row["error_category"] == category
    assert json.loads(row["arguments_canonical"]) == {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "ToolArgumentError",
        "field_count": len(arguments),
    }
    assert row["arguments_hash"] == stable_hash(json.loads(row["arguments_canonical"]))
    verify_events_sidecar_integrity(sidecar)
