"""Tests for redact_tool_call_response walker (spec §4.2.4, §4.2.6).

Covers:
- Known/unknown/sensitive key dispatch (declarative entry path)
- Type-driven response_model path (walk_model_schema)
- Fixed sentinel '<redacted-unknown-response-key>' (W6)
- Summarizer crash discipline (M2/W5/M.8)
- Walker atomicity (W8b)
- Missing manifest entry (registry consistency)

Plan task: Phase 2 / Task 7
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ConfigDict

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.tier_registry import _TIER_1_ERRORS_VIEW
from elspeth.web.composer.redaction import (
    MANIFEST,
    REDACTED_UNKNOWN_RESPONSE_KEY,
    HandlesNoSensitiveDataReason,
    Sensitive,
    ToolRedaction,
    ToolRedactionPolicy,
    redact_arg_error_response,
    redact_tool_call_response,
)
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry

_TYPE_DRIVEN_TOOL_RESULT_TOOLS = (
    "set_source",
    "create_blob",
    "update_blob",
    "set_source_from_blob",
    "set_pipeline",
    "apply_pipeline_recipe",
    "patch_source_options",
    "patch_node_options",
    "patch_output_options",
    "splice_transform",
)

_EXTERNAL_SCALAR_CANARY = "RAW_RESPONSE_CANARY_/private/operator/path_sk-secret"
_KNOWN_ARG_ERROR_CLASSES = (
    "CanonicalizationError",
    "FloatDomainError",
    "IntegerDomainError",
    "JSONDecodeError",
    "JsonBoundaryError",
    "MissingRequiredPaths",
    "ToolArgumentError",
    "TypeError",
    "ValidationError",
    "ValueError",
)


def _tool_result_canary_response(*, success: bool) -> dict[str, Any]:
    return {
        "success": success,
        "validation": {
            "is_valid": False,
            "errors": [
                {
                    "component": _EXTERNAL_SCALAR_CANARY,
                    "message": _EXTERNAL_SCALAR_CANARY,
                    "severity": "high",
                    "error_code": _EXTERNAL_SCALAR_CANARY,
                }
            ],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": [_EXTERNAL_SCALAR_CANARY],
        "version": 7,
        "data": {
            "error": _EXTERNAL_SCALAR_CANARY,
            "nested": {"provider": _EXTERNAL_SCALAR_CANARY},
        },
        "runtime_preflight": {
            "errors": [{"message": _EXTERNAL_SCALAR_CANARY}],
        },
        "validation_delta": {
            "new_errors": [{"message": _EXTERNAL_SCALAR_CANARY}],
        },
        "post_call_hints": [_EXTERNAL_SCALAR_CANARY],
        "plugin_schemas": {
            _EXTERNAL_SCALAR_CANARY: {
                "description": _EXTERNAL_SCALAR_CANARY,
            }
        },
    }


@pytest.mark.parametrize("error_class", _KNOWN_ARG_ERROR_CLASSES)
def test_arg_error_projection_preserves_every_known_producer_class(error_class: str) -> None:
    result = redact_arg_error_response(
        error_class=error_class,
        error_message="fixed producer diagnostic",
    )

    assert result["error_class"] == error_class


def test_every_type_driven_manifest_entry_declares_a_response_model() -> None:
    missing = [name for name, entry in MANIFEST.items() if entry.argument_model is not None and entry.response_model is None]

    assert missing == []


@pytest.mark.parametrize("tool_name", _TYPE_DRIVEN_TOOL_RESULT_TOOLS)
@pytest.mark.parametrize("success", [True, False], ids=["success", "error"])
def test_type_driven_tool_result_responses_fail_closed_without_losing_public_status(
    tool_name: str,
    success: bool,
) -> None:
    response = _tool_result_canary_response(success=success)

    result = redact_tool_call_response(
        tool_name,
        response,
        telemetry=NoopRedactionTelemetry(),
    )
    serialized = json.dumps(result, sort_keys=True)

    assert result["success"] is success
    assert result["version"] == 7
    assert result["validation"]["is_valid"] is False
    assert result["validation"]["errors"][0]["severity"] == "high"
    assert set(result) == set(response)
    assert _EXTERNAL_SCALAR_CANARY not in serialized


@pytest.mark.parametrize(
    ("tool_name", "response_key"),
    [
        (tool_name, response_key)
        for tool_name in ("upsert_node", "set_metadata", "set_output", "request_advisor_hint")
        for response_key in MANIFEST[tool_name].policy.known_response_keys  # type: ignore[union-attr]
    ],
)
def test_declarative_known_response_key_never_trusts_an_arbitrary_scalar(
    tool_name: str,
    response_key: str,
) -> None:
    result = redact_tool_call_response(
        tool_name,
        {response_key: _EXTERNAL_SCALAR_CANARY},
        telemetry=NoopRedactionTelemetry(),
    )

    assert response_key in result
    assert _EXTERNAL_SCALAR_CANARY not in json.dumps(result, sort_keys=True)


def test_declarative_nested_tool_result_envelopes_scrub_external_scalars() -> None:
    response = _tool_result_canary_response(success=False)

    result = redact_tool_call_response(
        "upsert_node",
        response,
        telemetry=NoopRedactionTelemetry(),
    )
    serialized = json.dumps(result, sort_keys=True)

    assert result["success"] is False
    assert result["version"] == 7
    assert result["validation"]["is_valid"] is False
    assert result["validation"]["errors"][0]["severity"] == "high"
    assert set(result) == set(response)
    assert _EXTERNAL_SCALAR_CANARY not in serialized


def test_type_driven_tool_result_accepts_and_scrubs_row_union_schema_facts() -> None:
    response = _tool_result_canary_response(success=False)
    response["validation"]["errors"][0]["row_union_schema"] = {
        "branches": [
            {
                "branch": _EXTERNAL_SCALAR_CANARY,
                "mode": "flexible",
                "fields": [
                    {
                        "name": _EXTERNAL_SCALAR_CANARY,
                        "field_type": "str",
                        "required": True,
                        "nullable": False,
                    }
                ],
            }
        ],
        "conflicting_fields": [_EXTERNAL_SCALAR_CANARY],
    }

    result = redact_tool_call_response(
        "set_source",
        response,
        telemetry=NoopRedactionTelemetry(),
    )

    schema_facts = result["validation"]["errors"][0]["row_union_schema"]
    assert schema_facts["branches"][0]["fields"][0]["required"] is True
    assert schema_facts["branches"][0]["fields"][0]["nullable"] is False
    assert _EXTERNAL_SCALAR_CANARY not in json.dumps(schema_facts, sort_keys=True)


@pytest.mark.parametrize("tool_name", ["set_source", "upsert_node"])
def test_repair_argument_summary_never_exposes_arbitrary_key_names(tool_name: str) -> None:
    key_canary = "RAW_KEY_CANARY_/private/operator/path_sk-secret"
    response = {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [
                {
                    "code": "repair_required",
                    "connection": "source_to_transform",
                    "strategy": "repair",
                    "reason": "A repair is required.",
                    "affected_consumers": [],
                    "tool_sequence": [
                        {
                            "tool": "upsert_node",
                            "arguments": {key_canary: "secret value"},
                        }
                    ],
                }
            ],
        },
        "affected_nodes": [],
        "version": 3,
        "data": {"error": "repair required"},
    }

    result = redact_tool_call_response(
        tool_name,
        response,
        telemetry=NoopRedactionTelemetry(),
    )
    arguments_summary = result["validation"]["graph_repair_suggestions"][0]["tool_sequence"][0]["arguments"]

    assert arguments_summary == "<redacted-repair-arguments>"
    assert key_canary not in json.dumps(result, sort_keys=True)


def test_declarative_projection_rejects_excessive_depth_without_recursion() -> None:
    nested: object = "leaf"
    for _ in range(1200):
        nested = {"items": nested}

    result = redact_tool_call_response(
        "upsert_node",
        {"data": nested},
        telemetry=NoopRedactionTelemetry(),
    )

    assert result == {"_redaction_status": "response_projection_limit"}


def test_declarative_projection_rejects_excessive_width_without_amplification() -> None:
    result = redact_tool_call_response(
        "upsert_node",
        {"data": ["external"] * 20_000},
        telemetry=NoopRedactionTelemetry(),
    )

    assert result == {"_redaction_status": "response_projection_limit"}
    assert len(json.dumps(result)) < 256


def test_declarative_projection_rejects_excessive_total_nodes() -> None:
    result = redact_tool_call_response(
        "upsert_node",
        {"data": [[False] * 32 for _ in range(32)]},
        telemetry=NoopRedactionTelemetry(),
    )

    assert result == {"_redaction_status": "response_projection_limit"}


def test_type_driven_projection_rejects_oversized_validation_list_before_model_validation() -> None:
    response = _tool_result_canary_response(success=False)
    response["validation"]["errors"] = [
        {
            "component": "component",
            "message": "message",
            "severity": "high",
            "error_code": "error_code",
        }
    ] * 20_000

    result = redact_tool_call_response(
        "set_source",
        response,
        telemetry=NoopRedactionTelemetry(),
    )

    assert result == {"_redaction_status": "response_projection_limit"}


def test_largest_admitted_projection_stays_below_output_budget() -> None:
    result = redact_tool_call_response(
        "upsert_node",
        {"data": [[False] * 30 for _ in range(30)]},
        telemetry=NoopRedactionTelemetry(),
    )

    assert result != {"_redaction_status": "response_projection_limit"}
    assert len(json.dumps(result).encode("utf-8")) <= 65_536


@pytest.mark.parametrize("tool_name", ["set_source", "upsert_node"])
def test_response_projection_is_a_fixed_point(tool_name: str) -> None:
    response = _tool_result_canary_response(success=False)

    once = redact_tool_call_response(
        tool_name,
        response,
        telemetry=NoopRedactionTelemetry(),
    )
    twice = redact_tool_call_response(
        tool_name,
        once,
        telemetry=NoopRedactionTelemetry(),
    )

    assert twice == once


def test_existing_response_models_scrub_free_form_failure_and_diagnostic_text() -> None:
    blob_response = {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [
                {
                    "component": _EXTERNAL_SCALAR_CANARY,
                    "message": _EXTERNAL_SCALAR_CANARY,
                    "severity": "high",
                    "error_code": _EXTERNAL_SCALAR_CANARY,
                }
            ],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": [_EXTERNAL_SCALAR_CANARY],
        "version": 3,
        "data": {"error": _EXTERNAL_SCALAR_CANARY},
    }
    review_response = {
        "success": True,
        "validation": {
            "is_valid": True,
            "errors": [],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": [_EXTERNAL_SCALAR_CANARY],
        "version": 4,
        "data": {
            "_kind": "interpretation_review_pending",
            "event_id": _EXTERNAL_SCALAR_CANARY,
            "affected_node_id": _EXTERNAL_SCALAR_CANARY,
            "kind": "vague_term",
            "interpretation_source": _EXTERNAL_SCALAR_CANARY,
            "message": _EXTERNAL_SCALAR_CANARY,
        },
    }

    blob_result = redact_tool_call_response(
        "get_blob_content",
        blob_response,
        telemetry=NoopRedactionTelemetry(),
    )
    review_result = redact_tool_call_response(
        "request_interpretation_review",
        review_response,
        telemetry=NoopRedactionTelemetry(),
    )

    assert blob_result["success"] is False
    assert review_result["data"]["_kind"] == "interpretation_review_pending"
    assert _EXTERNAL_SCALAR_CANARY not in json.dumps(blob_result, sort_keys=True)
    assert _EXTERNAL_SCALAR_CANARY not in json.dumps(review_result, sort_keys=True)


# ---------------------------------------------------------------------------
# Helpers: construct temporary manifest-like entries for test isolation.
# We cannot mutate the module-level MANIFEST (it's a MappingProxyType) so
# tests that need non-set_source tools must patch MANIFEST or call the
# function with a name already in MANIFEST (set_source).
#
# Strategy: use monkeypatch to temporarily extend MANIFEST with test entries.
# ---------------------------------------------------------------------------


def _declarative_entry(
    *,
    sensitive_response_keys: tuple[str, ...] = (),
    known_response_keys: tuple[str, ...] = (),
    argument_summarizers: dict[str, Any] | None = None,
    response_summarizers: dict[str, Any] | None = None,
    handles_no_sensitive_data: bool = False,
    handles_no_sensitive_data_reason_struct: HandlesNoSensitiveDataReason | None = None,
) -> ToolRedaction:
    """Build a declarative ToolRedaction for test fixtures."""
    policy = ToolRedactionPolicy(
        sensitive_response_keys=sensitive_response_keys,
        known_response_keys=known_response_keys,
        argument_summarizers=argument_summarizers or {},
        handles_no_sensitive_data=handles_no_sensitive_data,
        handles_no_sensitive_data_reason_struct=handles_no_sensitive_data_reason_struct,
    )
    return ToolRedaction(policy=policy)


def _safe_reason() -> HandlesNoSensitiveDataReason:
    return HandlesNoSensitiveDataReason(
        sensitive_data_locations=("no-sensitive-surface",),
        why_arguments_safe="All arguments are structural metadata only; no user content.",
        why_responses_safe="Response contains only structural metadata; no secrets or PII.",
    )


def _patch_manifest(monkeypatch: pytest.MonkeyPatch, tool_name: str, entry: ToolRedaction) -> None:
    """Extend the module-level MANIFEST with a test entry.

    Builds a new dict from the existing proxy, adds the test entry, and
    replaces the module binding.  Monkeypatch restores the original value
    on teardown.
    """
    from types import MappingProxyType

    import elspeth.web.composer.redaction as _redaction_mod

    new_manifest = MappingProxyType({**_redaction_mod.MANIFEST, tool_name: entry})
    monkeypatch.setattr(_redaction_mod, "MANIFEST", new_manifest)


# ---------------------------------------------------------------------------
# Test 1: all keys known, none sensitive → passthrough
# ---------------------------------------------------------------------------


def test_passthrough_when_all_keys_known_and_none_sensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declarative entry: known_response_keys covers all response keys; none
    are sensitive → every value passes through unchanged."""
    tool = "t_passthrough"
    entry = _declarative_entry(
        sensitive_response_keys=(),
        known_response_keys=("status", "count"),
    )
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    response = {"status": "ok", "count": 42}
    result = redact_tool_call_response(tool, response, telemetry=tel)

    assert result == {"status": "ok", "count": 42}
    assert tel.unknown_response_key_calls == []
    assert tel.summarizer_error_calls == []


# ---------------------------------------------------------------------------
# Test 2: sensitive key, no summarizer → REDACTED sentinel
# ---------------------------------------------------------------------------


def test_sensitive_key_without_summarizer_becomes_redacted_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declarative entry: a sensitive_response_key with no summarizer is
    substituted with the no-summarizer sentinel (not the unknown-key sentinel).

    Declarative entries have no response_summarizers; the only available
    substitution for a declarative sensitive key is the sentinel.
    """
    from elspeth.web.composer.redaction import REDACTED_SENSITIVE_NO_SUMMARIZER

    tool = "t_no_summarizer"
    entry = _declarative_entry(
        sensitive_response_keys=("secret",),
        known_response_keys=("status", "secret"),
    )
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    response = {"status": "ok", "secret": "CANARY_SECRET"}
    result = redact_tool_call_response(tool, response, telemetry=tel)

    assert result["status"] == "ok"
    assert result["secret"] == REDACTED_SENSITIVE_NO_SUMMARIZER
    assert "CANARY_SECRET" not in result.values()
    # Unknown-key counter must NOT fire (this is a known sensitive key, not unknown)
    assert tel.unknown_response_key_calls == []


# ---------------------------------------------------------------------------
# Test 3: type-driven response_model, Sensitive field has summarizer
# ---------------------------------------------------------------------------


def test_sensitive_key_with_summarizer_uses_summarizer_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Type-driven entry with response_model: Sensitive field with a summarizer
    uses the summarizer output rather than the raw value or sentinel."""

    class _ResponseModel(BaseModel):
        status: str
        token: Annotated[str, Sensitive(summarizer=lambda v: f"<summarized:{len(v)}>")]

        model_config = ConfigDict(extra="forbid")

    class _ArgModel(BaseModel):
        query: str

        model_config = ConfigDict(extra="forbid")

    tool = "t_with_summarizer"
    entry = ToolRedaction(argument_model=_ArgModel, response_model=_ResponseModel)
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    response = {"status": "ok", "token": "SECRETTOKEN"}
    result = redact_tool_call_response(tool, response, telemetry=tel)

    assert result["status"] == "ok"
    assert result["token"] == "<summarized:11>"
    assert "SECRETTOKEN" not in str(result)


# ---------------------------------------------------------------------------
# Test 4: unknown key → fixed sentinel + telemetry counter
# ---------------------------------------------------------------------------


def test_unknown_key_becomes_fixed_sentinel_with_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declarative unknown key is aggregated without retaining its name."""
    tool = "t_unknown_key"
    entry = _declarative_entry(
        sensitive_response_keys=(),
        known_response_keys=("status",),
    )
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    response = {"status": "ok", "unknown_field": "MYSTERY"}
    result = redact_tool_call_response(tool, response, telemetry=tel)

    assert result["status"] == "ok"
    assert "unknown_field" not in result
    assert result["_unknown_response"] == REDACTED_UNKNOWN_RESPONSE_KEY
    # Exact string equality, not regex/prefix
    assert result["_unknown_response"] == "<redacted-unknown-response-key>"
    # Counter fires once per unknown key
    assert tel.unknown_response_key_calls == [{"tool_name": tool}]


def test_unknown_key_counter_fires_once_per_unknown_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two unknown keys → counter fires twice (once per key)."""
    tool = "t_two_unknowns"
    entry = _declarative_entry(
        sensitive_response_keys=(),
        known_response_keys=("status",),
    )
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    response = {"status": "ok", "x": 1, "y": 2}
    result = redact_tool_call_response(tool, response, telemetry=tel)

    assert result == {
        "status": "ok",
        "_unknown_response": REDACTED_UNKNOWN_RESPONSE_KEY,
    }
    assert len(tel.unknown_response_key_calls) == 2


def test_declarative_unknown_top_level_key_name_is_aggregated_without_leaking() -> None:
    key_canary = "RAW_TOP_LEVEL_KEY_/private/operator/path_sk-secret"
    result = redact_tool_call_response(
        "upsert_node",
        {
            "success": True,
            key_canary: "untrusted value",
        },
        telemetry=NoopRedactionTelemetry(),
    )

    assert result == {
        "success": True,
        "_unknown_response": REDACTED_UNKNOWN_RESPONSE_KEY,
    }
    assert key_canary not in json.dumps(result, sort_keys=True)


# ---------------------------------------------------------------------------
# Test 5: type-driven response_model walks via walk_model_schema
# ---------------------------------------------------------------------------


def test_type_driven_response_model_walks_via_schema_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Type-driven entry with response_model: Sensitive[T] fields on the
    model are substituted; non-sensitive fields pass through."""

    class _ResponseModel(BaseModel):
        status: str
        api_key: Annotated[str, Sensitive()]  # no summarizer → sentinel

        model_config = ConfigDict(extra="forbid")

    class _ArgModel(BaseModel):
        query: str

        model_config = ConfigDict(extra="forbid")

    from elspeth.web.composer.redaction import REDACTED_SENSITIVE_NO_SUMMARIZER

    tool = "t_type_driven_response"
    entry = ToolRedaction(argument_model=_ArgModel, response_model=_ResponseModel)
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    response = {"status": "healthy", "api_key": "sk-super-secret"}
    result = redact_tool_call_response(tool, response, telemetry=tel)

    assert result["status"] == "healthy"
    assert result["api_key"] == REDACTED_SENSITIVE_NO_SUMMARIZER
    assert "sk-super-secret" not in str(result)


def test_get_blob_content_redacts_tool_result_envelope_content() -> None:
    """``get_blob_content`` redacts nested ``data.content`` in ToolResult."""

    response = {
        "success": True,
        "validation": {
            "is_valid": True,
            "errors": [],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": [],
        "version": 2,
        "data": {
            "blob_id": "blob-1",
            "filename": "input.csv",
            "mime_type": "text/csv",
            "content": "secret,row\n1,2\n",
            "truncated": False,
            "size_bytes": 15,
        },
    }

    result = redact_tool_call_response("get_blob_content", response, telemetry=NoopRedactionTelemetry())

    assert result["data"]["content"] == "<redacted-blob-content>"
    assert "secret,row" not in str(result)


def test_get_blob_content_redacts_tool_result_failure_envelope() -> None:
    """Recoverable failure text is summarized without losing public status."""

    response = {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": [],
        "version": 2,
        "data": {"error": "Blob 'blob-1' not found."},
    }

    result = redact_tool_call_response("get_blob_content", response, telemetry=NoopRedactionTelemetry())

    assert result["success"] is False
    assert result["version"] == 2
    assert result["data"]["error"] == "<redacted-response-text>"
    assert "blob-1" not in json.dumps(result, sort_keys=True)


def test_get_blob_content_populated_validation_envelope_redacts_external_scalars() -> None:
    """A fully-populated validation envelope retains status and safe summaries.

    Mechanically pins that ``GetBlobContentValidationModel`` (and its nested
    shadow models) matches the real ``ToolResult.to_dict()`` validation-envelope
    shape — every key produced by ``_semantic_contracts_payload`` and
    ``_graph_repair_suggestions`` in tools/_common.py must be accepted by the
    ``extra="forbid"`` shadow models. The other fixtures use empty lists and
    never exercise a populated ``semantic_contracts`` / ``graph_repair_suggestions``
    element, so a key-name drift between the builder and the shadow model would
    pass them silently; this test fails loudly on such drift.

    It also confirms the heterogeneous repair-tool-call ``arguments`` mapping
    keeps its dedicated structural sketch while surrounding externally derived
    text is summarized. Closed booleans, severity, and counts remain useful.
    """
    response = {
        "success": True,
        "validation": {
            "is_valid": False,
            "errors": [
                {"component": "connection:shared", "message": "Duplicate consumer for connection shared", "severity": "high"},
            ],
            "warnings": [
                {"component": "node:t1", "message": "Observed schema in use", "severity": "low"},
            ],
            "suggestions": [
                {"component": "graph", "message": "Consider a fork gate", "severity": "medium"},
            ],
            "semantic_contracts": [
                {
                    "from_id": "source",
                    "to_id": "t1",
                    "consumer_plugin": "passthrough",
                    "producer_plugin": "csv",
                    "producer_field": "url",
                    "consumer_field": "url",
                    "outcome": "satisfied",
                    "requirement_code": "REQ-001",
                },
                {
                    "from_id": "source",
                    "to_id": "t2",
                    "consumer_plugin": "llm",
                    "producer_plugin": None,
                    "producer_field": "rating",
                    "consumer_field": "rating",
                    "outcome": "unsatisfied",
                    "requirement_code": "REQ-002",
                },
            ],
            "graph_repair_suggestions": [
                {
                    "code": "duplicate_consumer_connection",
                    "connection": "shared",
                    "strategy": "insert_fork_gate",
                    "reason": "Give each consumer a unique branch input.",
                    "affected_consumers": [
                        {"id": "t1", "current_input": "shared", "new_input": "shared_to_t1"},
                    ],
                    "tool_sequence": [
                        {"tool": "upsert_node", "arguments": {"id": "t1", "input": "shared_to_t1", "node_type": "transform"}},
                        {"tool": "preview_pipeline", "arguments": {}},
                    ],
                },
            ],
        },
        "affected_nodes": ["t1"],
        "version": 3,
        "data": {
            "blob_id": "blob-1",
            "filename": "input.csv",
            "mime_type": "text/csv",
            "content": "secret,row\n1,2\n",
            "truncated": False,
            "size_bytes": 15,
        },
    }

    result = redact_tool_call_response("get_blob_content", response, telemetry=NoopRedactionTelemetry())

    # Blob bytes redacted (existing behaviour).
    assert result["data"]["content"] == "<redacted-blob-content>"

    # The single Sensitive repair-arguments leaf is summarized to key count.
    repair = result["validation"]["graph_repair_suggestions"][0]
    assert repair["tool_sequence"][0]["arguments"] == "<redacted-repair-arguments>"
    assert repair["tool_sequence"][1]["arguments"] == "<redacted-repair-arguments>"

    # Public status survives while free-form/provider/operator text is summarized.
    assert result["validation"]["is_valid"] is False
    assert result["validation"]["errors"][0]["severity"] == "high"
    assert result["validation"]["errors"][0]["message"] == "<redacted-response-text>"
    assert result["validation"]["semantic_contracts"][1]["producer_plugin"] is None
    assert result["validation"]["semantic_contracts"][0]["requirement_code"] == "<redacted-response-text>"
    assert repair["affected_consumers"][0]["new_input"] == "<redacted-response-text>"
    assert repair["code"] == "<redacted-response-text>"

    # The repair-argument VALUES never reach the redacted output (only the
    # affected_consumers descriptor — a non-sensitive structural field — may
    # legitimately echo the new input name).
    assert "'input': 'shared_to_t1'" not in str(result)


def test_declarative_tool_result_redacts_nested_repair_arguments() -> None:
    """Declarative response policies must still descend into repair guidance.

    ``upsert_node`` keeps the ToolResult envelope declarative: top-level
    ``validation`` and ``data`` are known response keys, but both can carry
    nested credential-repair tool calls with open ``arguments`` mappings.
    Those mappings must be structurally summarized before persistence, not
    copied through with credential material intact.
    """
    sentinel = "sk-test-declarative-nested-secret"
    response = {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [
                {
                    "code": "credential_wiring_required",
                    "connection": "source_to_enrich",
                    "strategy": "wire_secret_ref",
                    "reason": "Credential field must be wired by reference.",
                    "affected_consumers": [
                        {"id": "enrich", "current_input": "source", "new_input": "source_to_enrich"},
                    ],
                    "tool_sequence": [
                        {
                            "tool": "upsert_node",
                            "arguments": {
                                "id": "enrich",
                                "options": {"api_key": sentinel},
                            },
                        },
                    ],
                },
            ],
        },
        "affected_nodes": ["enrich"],
        "version": 4,
        "data": {
            "error": "credential wiring required",
            "repair": {
                "tool_sequence": [
                    {
                        "tool": "wire_secret_ref",
                        "arguments": {
                            "name": "OPENAI_API_KEY",
                            "target": "node",
                            "target_id": "enrich",
                            "option_key": "api_key",
                            "proof": sentinel,
                        },
                    },
                ],
                "arguments": {"api_key": sentinel},
            },
        },
    }

    result = redact_tool_call_response("upsert_node", response, telemetry=NoopRedactionTelemetry())

    assert result["validation"]["graph_repair_suggestions"][0]["tool_sequence"][0]["arguments"] == "<redacted-repair-arguments>"
    repair = result["data"]["repair"]
    projected_arguments = repair["tool_sequence"][0]["arguments"]
    assert isinstance(projected_arguments, dict)
    assert set(projected_arguments) == {
        "_redacted_response_field_1",
        "_redacted_response_field_2",
        "_redacted_response_field_3",
        "_redacted_response_field_4",
        "_redacted_response_field_5",
    }
    assert isinstance(repair["arguments"], dict)
    assert sentinel not in json.dumps(result, sort_keys=True)


def test_declarative_tool_result_preserves_shared_optional_envelope_keys() -> None:
    """Declarative ToolResult policies share the full ``ToolResult.to_dict`` envelope."""
    response = {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": [],
        "version": 7,
        "data": {"error": "invalid options"},
        "post_call_hints": ["Call get_plugin_schema for source/csv before retrying."],
        "plugin_schemas": {
            "source/csv": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    }

    result = redact_tool_call_response("upsert_node", response, telemetry=NoopRedactionTelemetry())

    assert result["post_call_hints"] == ["<redacted-response-text>"]
    assert "source/csv" not in result["plugin_schemas"]
    assert result["plugin_schemas"]["_redacted_response_field_1"]["type"] == "<redacted-response-text>"
    assert REDACTED_UNKNOWN_RESPONSE_KEY not in json.dumps(result, sort_keys=True)


# ---------------------------------------------------------------------------
# Test 6: summarizer raises → telemetry counter BEFORE AuditIntegrityError
# ---------------------------------------------------------------------------


def test_summarizer_raise_yields_audit_integrity_error_and_fires_telemetry_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summarizer that raises RuntimeError → walker fires summarizer_error
    counter BEFORE raising AuditIntegrityError chained from the RuntimeError.

    This is the rev-2 M.8 discipline: counter must fire before raise so OTel
    scrapes see it even when the request dies after the raise.

    Uses the type-driven path (response_model with a crashing summarizer).
    """

    def _crashing_summarizer(v: Any) -> str:
        raise RuntimeError("boom")

    class _ResponseModel(BaseModel):
        status: str
        token: Annotated[str, Sensitive(summarizer=_crashing_summarizer)]

        model_config = ConfigDict(extra="forbid")

    class _ArgModel(BaseModel):
        query: str

        model_config = ConfigDict(extra="forbid")

    tool = "t_crashing_summarizer"
    entry = ToolRedaction(argument_model=_ArgModel, response_model=_ResponseModel)
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    with pytest.raises(AuditIntegrityError) as exc_info:
        redact_tool_call_response(tool, {"status": "ok", "token": "SECRET"}, telemetry=tel)

    # Telemetry counter fired (BEFORE the raise)
    assert tel.summarizer_error_calls == [{"tool_name": tool}]
    # AuditIntegrityError chains the underlying RuntimeError
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "boom"


def test_audit_integrity_error_in_tier_1_errors_registry() -> None:
    """AuditIntegrityError is registered in TIER_1_ERRORS per spec §9 RSK-03 / §4.5.

    This test is in the response-walker test file because the task spec
    requires it here; it is not specific to the response walker — it validates
    the registry invariant that the walker's raises depend on.
    """
    tier_1_classes = set(_TIER_1_ERRORS_VIEW)
    assert AuditIntegrityError in tier_1_classes, (
        f"AuditIntegrityError is not in TIER_1_ERRORS. Registered classes: {sorted(c.__name__ for c in tier_1_classes)}"
    )


# ---------------------------------------------------------------------------
# Test 7: summarizer non-str return → telemetry counter BEFORE AuditIntegrityError
# ---------------------------------------------------------------------------


def test_summarizer_non_str_return_yields_audit_integrity_error_and_fires_telemetry_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Summarizer that returns a non-str value → walker fires summarizer_error
    counter BEFORE raising AuditIntegrityError with a message naming the type."""

    def _bad_summarizer(v: Any) -> Any:
        return {"x": 1}  # non-str return

    class _ResponseModel(BaseModel):
        status: str
        token: Annotated[str, Sensitive(summarizer=_bad_summarizer)]

        model_config = ConfigDict(extra="forbid")

    class _ArgModel(BaseModel):
        query: str

        model_config = ConfigDict(extra="forbid")

    tool = "t_bad_return_summarizer"
    entry = ToolRedaction(argument_model=_ArgModel, response_model=_ResponseModel)
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    with pytest.raises(AuditIntegrityError) as exc_info:
        redact_tool_call_response(tool, {"status": "ok", "token": "SECRET"}, telemetry=tel)

    # Telemetry counter fired (BEFORE the raise)
    assert tel.summarizer_error_calls == [{"tool_name": tool}]
    # Message names the actual returned type
    assert "dict" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 8: missing manifest entry → AuditIntegrityError
# ---------------------------------------------------------------------------


def test_missing_manifest_entry_yields_audit_integrity_error() -> None:
    """redact_tool_call_response for a tool name not in MANIFEST raises
    AuditIntegrityError immediately (registry-consistency invariant)."""
    tel = NoopRedactionTelemetry()
    with pytest.raises(AuditIntegrityError):
        redact_tool_call_response("not_in_manifest", {"x": 1}, telemetry=tel)


# ---------------------------------------------------------------------------
# Test 9: empty known_response_keys → unknown response keys aggregated
# ---------------------------------------------------------------------------


def test_empty_known_response_keys_sentinelises_every_response_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declarative entry with handles_no_sensitive_data=True and
    known_response_keys=(): every response key is unknown → names are removed
    and one fixed aggregate sentinel remains.

    Task 6's ToolRedactionPolicy validator forbids known_response_keys=()
    when handles_no_sensitive_data=False. This fixture therefore uses
    handles_no_sensitive_data=True (the only construction path that permits
    empty known_response_keys).

    The walker applies the policy mechanically: unknown key check fires for
    every key since neither sensitive_response_keys nor known_response_keys
    covers them.
    """
    tool = "t_empty_known"
    entry = _declarative_entry(
        sensitive_response_keys=(),
        known_response_keys=(),  # empty
        handles_no_sensitive_data=True,
        handles_no_sensitive_data_reason_struct=_safe_reason(),
    )
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    response = {"alpha": "a", "beta": "b"}
    result = redact_tool_call_response(tool, response, telemetry=tel)

    assert result == {"_unknown_response": REDACTED_UNKNOWN_RESPONSE_KEY}
    # Counter fires once per unknown key
    assert len(tel.unknown_response_key_calls) == 2


# ---------------------------------------------------------------------------
# Test 10: walker atomicity — mid-walk raise leaves no partial dict observable
# ---------------------------------------------------------------------------


def test_walker_atomicity_on_mid_walk_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mid-walk summarizer raise → no partially-built dict reaches the caller.

    The canonical atomicity test: a sentinel object is assigned BEFORE the
    call. After pytest.raises catches the AuditIntegrityError, the sentinel
    is still the same object (the call returned nothing).

    Fixture: response with three keys; the second key's summarizer raises so
    the first key has been processed but the final dict is never returned.
    """
    call_log: list[str] = []

    def _ok_summarizer(v: Any) -> str:
        call_log.append("ok")
        return "<ok>"

    def _crashing_summarizer(v: Any) -> str:
        call_log.append("crash")
        raise RuntimeError("mid-walk crash")

    class _ResponseModel(BaseModel):
        key_a: Annotated[str, Sensitive(summarizer=_ok_summarizer)]
        key_b: Annotated[str, Sensitive(summarizer=_crashing_summarizer)]
        key_c: str  # non-sensitive, reached only after key_b in schema order

        model_config = ConfigDict(extra="forbid")

    class _ArgModel(BaseModel):
        query: str

        model_config = ConfigDict(extra="forbid")

    tool = "t_atomicity"
    entry = ToolRedaction(argument_model=_ArgModel, response_model=_ResponseModel)
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    sentinel = object()
    result = sentinel

    with pytest.raises(AuditIntegrityError):
        result = redact_tool_call_response(
            tool,
            {"key_a": "v_a", "key_b": "v_b", "key_c": "v_c"},
            telemetry=tel,
        )

    # The mid-walk crash means no partial dict reached the caller.
    assert result is sentinel
    # key_a's summarizer was called (it appears before key_b in schema order)
    assert "ok" in call_log
    # The crash happened
    assert "crash" in call_log
    # Telemetry counter fired before the raise
    assert tel.summarizer_error_calls == [{"tool_name": tool}]


# ---------------------------------------------------------------------------
# Test 11: manifest_dispatch beacon fires for type-driven response path
# ---------------------------------------------------------------------------


def test_response_walker_emits_manifest_dispatch_for_type_driven_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §4.2.4: manifest_dispatch beacon fires per invocation, not per
    direction. Task 7 fix-up: redact_tool_call_response was silently omitting
    this emission while redact_tool_call_arguments correctly emits it.
    Restoring symmetry so the operational-progress dashboard reflects both
    directions."""

    class _ResponseModel(BaseModel):
        status: str

        model_config = ConfigDict(extra="forbid")

    class _ArgModel(BaseModel):
        query: str

        model_config = ConfigDict(extra="forbid")

    tool = "t_dispatch_type_driven"
    entry = ToolRedaction(argument_model=_ArgModel, response_model=_ResponseModel)
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    redact_tool_call_response(tool, {"status": "ok"}, telemetry=tel)

    assert tel.manifest_dispatch_calls == [{"tool_name": tool, "shape": "type_driven"}]


# ---------------------------------------------------------------------------
# Test 12: manifest_dispatch beacon fires for declarative response path
# ---------------------------------------------------------------------------


def test_response_walker_emits_manifest_dispatch_for_declarative_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §4.2.4 walker-wide emission requirement: declarative branch also
    emits the manifest_dispatch beacon once per invocation."""
    tool = "t_dispatch_declarative"
    entry = _declarative_entry(
        sensitive_response_keys=(),
        known_response_keys=("status",),
    )
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    redact_tool_call_response(tool, {"status": "ok"}, telemetry=tel)

    assert tel.manifest_dispatch_calls == [{"tool_name": tool, "shape": "declarative"}]


# ---------------------------------------------------------------------------
# ToolResult envelope keys are implicitly known for declarative entries
# ---------------------------------------------------------------------------


def test_tool_result_envelope_keys_are_implicitly_known_for_declarative_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """{success, validation, version} — the closed, engine-produced ToolResult
    dispatch envelope — must survive declarative redaction even when the policy
    does not declare them, so audit rows retain the dispatch outcome. Live
    forensics regression: every planner discovery invocation row read
    '<redacted-unknown-response-key>' for all three, leaving the audit trail
    blind to tool outcomes. ``data`` and any other undeclared key stay
    fail-closed.
    """
    tool = "t_envelope"
    entry = _declarative_entry(known_response_keys=("summary",))
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    validation = {
        "is_valid": False,
        "errors": [{"component": "node", "message": "bad option", "severity": "high", "error_code": "x"}],
    }
    response = {
        "success": False,
        "validation": validation,
        "version": 3,
        "summary": "s",
        "data": {"content": "SECRET-CONTENT"},
    }
    result = redact_tool_call_response(tool, response, telemetry=tel)

    assert result["success"] is False
    assert result["validation"] == "<redacted-response-mapping>"
    assert result["version"] == 3
    assert result["summary"] == "<redacted-response-text>"
    assert "data" not in result
    assert result["_unknown_response"] == REDACTED_UNKNOWN_RESPONSE_KEY
    assert "SECRET-CONTENT" not in str(result)
    assert len(tel.unknown_response_key_calls) == 1


def test_declared_sensitive_envelope_key_still_wins_over_implicit_knowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A policy that declares an envelope key sensitive keeps its sentinel —
    the implicit envelope allowlist never overrides an explicit sensitivity
    declaration."""
    from elspeth.web.composer.redaction import REDACTED_SENSITIVE_NO_SUMMARIZER

    tool = "t_envelope_sensitive"
    entry = _declarative_entry(
        sensitive_response_keys=("validation",),
        known_response_keys=("validation",),
    )
    _patch_manifest(monkeypatch, tool, entry)

    tel = NoopRedactionTelemetry()
    response = {"success": True, "validation": {"is_valid": True}, "version": 1}
    result = redact_tool_call_response(tool, response, telemetry=tel)

    assert result["validation"] == REDACTED_SENSITIVE_NO_SUMMARIZER
    assert result["success"] is True
    assert result["version"] == 1
