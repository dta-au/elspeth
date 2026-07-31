"""Regression coverage for legacy composer tool-invocation persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.authority_hashing import composer_authority_canonical_json
from elspeth.web.sessions.protocol import SessionServiceProtocol
from elspeth.web.sessions.routes._helpers import _persist_tool_invocations


@dataclass
class _CapturedMessage:
    session_id: UUID
    role: str
    content: str
    kwargs: dict[str, Any]


@dataclass
class _CapturingSessionService:
    messages: list[_CapturedMessage] = field(default_factory=list)

    async def add_message(
        self,
        session_id: UUID,
        role: str,
        content: str,
        **kwargs: Any,
    ) -> None:
        self.messages.append(_CapturedMessage(session_id=session_id, role=role, content=content, kwargs=kwargs))


def _hash_canonical(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_legacy_tool_invocation_persistence_redacts_advisor_payloads() -> None:
    """Legacy route drains must not mirror raw advisor arguments or guidance."""

    raw_problem = "RAW_PROBLEM: user pasted an internal exception and partial schema"
    raw_error = "RAW_ERROR: validator echoed the user's private column name"
    raw_action = "RAW_ACTION: tried set_pipeline with sensitive prose"
    raw_schema = "RAW_SCHEMA: internal schema excerpt"
    raw_guidance = "RAW_GUIDANCE: frontier model advice with sensitive details"
    raw_extra_context = "RAW_EXTRA_CONTEXT: unbounded context smuggled through an unknown key"
    arguments = {
        "trigger": "reactive_stuck",
        "problem_summary": raw_problem,
        "recent_errors": [raw_error],
        "attempted_actions": [raw_action],
        "schema_excerpt": raw_schema,
        "full_context": raw_extra_context,
    }
    result = {
        "status": "SUCCESS",
        "guidance": raw_guidance,
        "model": "frontier-test",
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "cached_prompt_tokens": 0,
        "advisor_latency_ms": 42,
        "budget_used": 1,
        "budget_remaining": 2,
        "note": "metadata is safe",
    }
    arguments_canonical = canonical_json(arguments)
    result_canonical = canonical_json(result)
    invocation = ComposerToolInvocation(
        tool_call_id="call_advisor_1",
        tool_name="request_advisor_hint",
        arguments_canonical=arguments_canonical,
        arguments_hash=_hash_canonical(arguments_canonical),
        result_canonical=result_canonical,
        result_hash=_hash_canonical(result_canonical),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=3,
        version_after=3,
        started_at=datetime(2026, 5, 24, tzinfo=UTC),
        finished_at=datetime(2026, 5, 24, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )
    service = _CapturingSessionService()

    await _persist_tool_invocations(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (invocation,),
        composition_state_id=None,
        parent_assistant_id=uuid4(),
        plugin_crash_pending=False,
    )

    assert len(service.messages) == 1
    message = service.messages[0]
    persisted_blob = json.dumps(
        {
            "content": json.loads(message.content),
            "tool_calls": message.kwargs["tool_calls"],
        },
        sort_keys=True,
    )
    assert raw_problem not in persisted_blob
    assert raw_error not in persisted_blob
    assert raw_action not in persisted_blob
    assert raw_schema not in persisted_blob
    assert raw_guidance not in persisted_blob
    assert "full_context" not in persisted_blob
    assert raw_extra_context not in persisted_blob
    assert "<advisor-problem-summary:" in persisted_blob
    assert "<advisor-recent-errors:1-entries>" in persisted_blob
    assert "<advisor-attempted-actions:1-entries>" in persisted_blob
    assert "<advisor-schema-excerpt:" in persisted_blob
    assert "<redacted-unknown-argument-key>" in persisted_blob
    assert '"guidance": "<redacted>"' in persisted_blob


@pytest.mark.asyncio
async def test_legacy_tool_invocation_persistence_scrubs_type_driven_response_canaries() -> None:
    """Persisted tool rows and their HTTP/audit envelope share the safe projection."""

    canary = "RAW_PERSISTED_RESPONSE_/private/provider/path_sk-secret"
    arguments = {
        "plugin": "csv",
        "options": {},
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    result = {
        "success": False,
        "validation": {
            "is_valid": False,
            "errors": [
                {
                    "component": canary,
                    "message": canary,
                    "severity": "high",
                    "error_code": canary,
                }
            ],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": [canary],
        "version": 3,
        "data": {"error": canary, "provider": canary},
    }
    arguments_canonical = canonical_json(arguments)
    result_canonical = canonical_json(result)
    invocation = ComposerToolInvocation(
        tool_call_id="call_set_source_canary",
        tool_name="set_source",
        arguments_canonical=arguments_canonical,
        arguments_hash=_hash_canonical(arguments_canonical),
        result_canonical=result_canonical,
        result_hash=_hash_canonical(result_canonical),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=2,
        version_after=3,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )
    service = _CapturingSessionService()

    await _persist_tool_invocations(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (invocation,),
        composition_state_id=None,
        parent_assistant_id=uuid4(),
        plugin_crash_pending=False,
    )

    assert len(service.messages) == 1
    message = service.messages[0]
    serialized_http_and_audit = json.dumps(
        {
            "content": json.loads(message.content),
            "tool_calls": message.kwargs["tool_calls"],
        },
        sort_keys=True,
    )
    persisted_content = json.loads(message.content)
    persisted_invocation = message.kwargs["tool_calls"][0]["invocation"]

    assert persisted_content["success"] is False
    assert persisted_content["version"] == 3
    assert canary not in serialized_http_and_audit
    assert canary not in persisted_invocation["result_canonical"]


@pytest.mark.asyncio
async def test_legacy_persistence_aggregates_unknown_top_level_response_key_name() -> None:
    key_canary = "RAW_TOP_LEVEL_PERSISTED_KEY_/private/operator/path_sk-secret"
    arguments = {
        "id": "t1",
        "node_type": "transform",
        "input": "source",
        "plugin": "passthrough",
    }
    result = {"success": True, key_canary: "untrusted value"}
    arguments_canonical = canonical_json(arguments)
    result_canonical = canonical_json(result)
    invocation = ComposerToolInvocation(
        tool_call_id="call_unknown_top_level_response_key",
        tool_name="upsert_node",
        arguments_canonical=arguments_canonical,
        arguments_hash=_hash_canonical(arguments_canonical),
        result_canonical=result_canonical,
        result_hash=_hash_canonical(result_canonical),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=2,
        version_after=2,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )
    service = _CapturingSessionService()

    await _persist_tool_invocations(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (invocation,),
        composition_state_id=None,
        parent_assistant_id=None,
        plugin_crash_pending=False,
    )

    message = service.messages[0]
    persisted_invocation = message.kwargs["tool_calls"][0]["invocation"]
    expected = {"success": True, "_unknown_response": "<redacted-unknown-response-key>"}
    assert json.loads(message.content) == expected
    assert json.loads(persisted_invocation["result_canonical"]) == expected
    assert key_canary not in json.dumps({"content": message.content, "invocation": persisted_invocation})


@pytest.mark.asyncio
async def test_legacy_persistence_bounds_deep_response_before_recursive_projection() -> None:
    nested: object = "RAW_DEEP_PERSISTENCE_CANARY_/private/operator/path_sk-secret"
    for _ in range(40):
        nested = [nested]
    arguments = {
        "id": "t1",
        "node_type": "transform",
        "input": "source",
        "plugin": "passthrough",
    }
    result = {
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
        "version": 3,
        "data": nested,
    }
    arguments_canonical = canonical_json(arguments)
    result_canonical = canonical_json(result)
    invocation = ComposerToolInvocation(
        tool_call_id="call_deep_response_canary",
        tool_name="upsert_node",
        arguments_canonical=arguments_canonical,
        arguments_hash=_hash_canonical(arguments_canonical),
        result_canonical=result_canonical,
        result_hash=_hash_canonical(result_canonical),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=2,
        version_after=3,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )
    service = _CapturingSessionService()

    await _persist_tool_invocations(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (invocation,),
        composition_state_id=None,
        parent_assistant_id=None,
        plugin_crash_pending=False,
    )

    message = service.messages[0]
    expected = {"_redaction_status": "response_projection_limit"}
    assert json.loads(message.content) == expected
    assert json.loads(message.kwargs["tool_calls"][0]["invocation"]["result_canonical"]) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "set_source",
            {
                "plugin": "csv",
                "options": {},
                "on_success": "rows",
                "on_validation_failure": "discard",
            },
        ),
        (
            "upsert_node",
            {
                "id": "t1",
                "node_type": "transform",
                "input": "source",
                "plugin": "passthrough",
            },
        ),
    ],
)
async def test_legacy_persistence_scrubs_repair_argument_key_canaries(
    tool_name: str,
    arguments: dict[str, object],
) -> None:
    key_canary = "RAW_KEY_CANARY_/private/operator/path_sk-secret"
    result = {
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
    arguments_canonical = canonical_json(arguments)
    result_canonical = canonical_json(result)
    invocation = ComposerToolInvocation(
        tool_call_id=f"call_{tool_name}_repair_key",
        tool_name=tool_name,
        arguments_canonical=arguments_canonical,
        arguments_hash=_hash_canonical(arguments_canonical),
        result_canonical=result_canonical,
        result_hash=_hash_canonical(result_canonical),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=2,
        version_after=3,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )
    service = _CapturingSessionService()

    await _persist_tool_invocations(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (invocation,),
        composition_state_id=None,
        parent_assistant_id=uuid4(),
        plugin_crash_pending=False,
    )

    persisted_blob = json.dumps(
        {
            "content": json.loads(service.messages[0].content),
            "tool_calls": service.messages[0].kwargs["tool_calls"],
        },
        sort_keys=True,
    )
    assert "<redacted-repair-arguments>" in persisted_blob
    assert key_canary not in persisted_blob


@pytest.mark.asyncio
async def test_schema_valid_semantic_arg_error_persists_only_closed_argument_projection() -> None:
    filename_canary = "RAW_FILENAME_/private/operator/path_sk-secret.csv"
    description_canary = "RAW_DESCRIPTION_/private/operator/path_sk-secret"
    arguments = {
        "filename": filename_canary,
        "mime_type": "text/csv",
        "content": "safe content",
        "description": description_canary,
    }
    result = {"error": "Tool 'create_blob' failed: semantic constraint"}
    arguments_canonical = canonical_json(arguments)
    result_canonical = canonical_json(result)
    invocation = ComposerToolInvocation(
        tool_call_id="call_create_blob_semantic_arg_error",
        tool_name="create_blob",
        arguments_canonical=arguments_canonical,
        arguments_hash=_hash_canonical(arguments_canonical),
        result_canonical=result_canonical,
        result_hash=_hash_canonical(result_canonical),
        status=ComposerToolStatus.ARG_ERROR,
        error_class="ToolArgumentError",
        error_message="invalid value",
        version_before=3,
        version_after=None,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )
    service = _CapturingSessionService()

    await _persist_tool_invocations(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (invocation,),
        composition_state_id=None,
        parent_assistant_id=None,
        plugin_crash_pending=False,
    )

    message = service.messages[0]
    envelope = message.kwargs["tool_calls"][0]
    persisted_invocation = envelope["invocation"]
    expected_arguments = {
        "_redaction_status": "invalid_tool_arguments",
        "error_class": "ToolArgumentError",
        "field_count": 4,
    }
    expected_canonical = canonical_json(expected_arguments)

    assert json.loads(persisted_invocation["arguments_canonical"]) == expected_arguments
    assert persisted_invocation["arguments_hash"] == _hash_canonical(expected_canonical)
    persisted_blob = json.dumps(
        {
            "content": json.loads(message.content),
            "envelope": envelope,
        },
        sort_keys=True,
    )
    assert filename_canary not in persisted_blob
    assert description_canary not in persisted_blob


@pytest.mark.asyncio
async def test_legacy_set_pipeline_rejects_malformed_bound_content_hash() -> None:
    hash_canary = "RAW_HASH_/private/operator/path_sk-secret"
    arguments = {
        "source": None,
        "nodes": [],
        "edges": [],
        "outputs": [],
    }
    result = {
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
        "version": 1,
        "pipeline_content_hash_schema": "composer.pipeline-dispatch-result.v1",
        "pipeline_content_hash": hash_canary,
    }
    arguments_canonical = canonical_json(arguments)
    authority_arguments_canonical = composer_authority_canonical_json(arguments)
    result_canonical = canonical_json(result)
    invocation = ComposerToolInvocation(
        tool_call_id="call_set_pipeline_hash_canary",
        tool_name="set_pipeline",
        arguments_canonical=arguments_canonical,
        arguments_hash=_hash_canonical(arguments_canonical),
        result_canonical=result_canonical,
        result_hash=_hash_canonical(result_canonical),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=0,
        version_after=1,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
        authority_arguments_canonical=authority_arguments_canonical,
        authority_arguments_hash=_hash_canonical(authority_arguments_canonical),
    )
    service = _CapturingSessionService()

    with pytest.raises(AuditIntegrityError, match="content hash is malformed"):
        await _persist_tool_invocations(
            cast(SessionServiceProtocol, service),
            uuid4(),
            (invocation,),
            composition_state_id=None,
            parent_assistant_id=None,
            plugin_crash_pending=False,
        )

    assert service.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_class", "error_message", "expected"),
    [
        (
            ComposerToolStatus.PLUGIN_CRASH,
            "RAW_CLASS_/private/operator/path_sk-secret",
            "RAW_MESSAGE_/private/operator/path_sk-secret",
            {
                "_redaction_status": "plugin_crash",
                "error_class": "<redacted-plugin-crash-class>",
                "error_message": "<redacted-failure-message>",
            },
        ),
        (
            ComposerToolStatus.CANCELLED,
            "CancelledError",
            "RAW_CANCEL_/private/operator/path_sk-secret",
            {
                "_redaction_status": "cancelled",
                "error_class": "CancelledError",
                "error_message": "cancelled",
            },
        ),
        (
            ComposerToolStatus.SUCCESS,
            "RAW_GENERIC_CLASS_/private/operator/path_sk-secret",
            "RAW_GENERIC_MESSAGE_/private/operator/path_sk-secret",
            {
                "_redaction_status": "failure",
                "error_class": "<redacted-failure-class>",
                "error_message": "<redacted-failure-message>",
            },
        ),
    ],
)
async def test_legacy_non_arg_failures_use_closed_content_and_envelope_projection(
    status: ComposerToolStatus,
    error_class: str,
    error_message: str,
    expected: dict[str, object],
) -> None:
    arguments = {"blob_id": str(uuid4())}
    arguments_canonical = canonical_json(arguments)
    invocation = ComposerToolInvocation(
        tool_call_id=f"call_{status.value}_canary",
        tool_name="get_blob_content",
        arguments_canonical=arguments_canonical,
        arguments_hash=_hash_canonical(arguments_canonical),
        result_canonical=None,
        result_hash=None,
        status=status,
        error_class=error_class,
        error_message=error_message,
        version_before=3,
        version_after=None,
        started_at=datetime(2026, 7, 27, tzinfo=UTC),
        finished_at=datetime(2026, 7, 27, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )
    service = _CapturingSessionService()

    await _persist_tool_invocations(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (invocation,),
        composition_state_id=None,
        parent_assistant_id=None,
        plugin_crash_pending=False,
    )

    message = service.messages[0]
    persisted_invocation = message.kwargs["tool_calls"][0]["invocation"]
    assert json.loads(message.content) == expected
    assert persisted_invocation["error_class"] == expected["error_class"]
    assert persisted_invocation["error_message"] == expected["error_message"]
    if error_class != expected["error_class"]:
        assert error_class not in json.dumps({"content": message.content, "invocation": persisted_invocation})
    assert error_message not in json.dumps({"content": message.content, "invocation": persisted_invocation})


@pytest.mark.asyncio
async def test_arg_error_result_for_response_model_tool_persists_without_success_model_validation() -> None:
    """ARG_ERROR payloads use their own closed persistence projection."""

    canary = "RAW_ARG_ERROR_/private/operator/path_sk-secret"
    error_class_canary = f"Custom{canary}"
    arguments = {"blob_id": {"unexpected": "value"}}
    result = {
        "error": f"Tool 'get_blob_content' failed: {canary}",
        "validation_errors": [{"type": "value_error", "loc": [canary]}],
    }
    arguments_canonical = canonical_json(arguments)
    result_canonical = canonical_json(result)
    invocation = ComposerToolInvocation(
        tool_call_id="call_blob_error_1",
        tool_name="get_blob_content",
        arguments_canonical=arguments_canonical,
        arguments_hash=_hash_canonical(arguments_canonical),
        result_canonical=result_canonical,
        result_hash=_hash_canonical(result_canonical),
        status=ComposerToolStatus.ARG_ERROR,
        error_class=error_class_canary,
        error_message=canary,
        version_before=3,
        version_after=None,
        started_at=datetime(2026, 5, 24, tzinfo=UTC),
        finished_at=datetime(2026, 5, 24, tzinfo=UTC),
        latency_ms=12,
        actor="composer-web:user-test",
    )
    service = _CapturingSessionService()

    await _persist_tool_invocations(
        cast(SessionServiceProtocol, service),
        uuid4(),
        (invocation,),
        composition_state_id=None,
        parent_assistant_id=None,
        plugin_crash_pending=False,
    )

    assert len(service.messages) == 1
    message = service.messages[0]
    persisted_content = json.loads(message.content)
    envelope = message.kwargs["tool_calls"][0]

    assert persisted_content["_redaction_status"] == "arg_error"
    assert persisted_content["error_class"] == "<redacted-arg-error-class>"
    assert persisted_content["error_message"] == "<redacted-arg-error-message>"
    assert persisted_content["result"]["field_count"] == 2
    assert persisted_content["result"]["validation_error_count"] == 1
    persisted_blob = json.dumps({"content": persisted_content, "envelope": envelope}, sort_keys=True)
    assert canary not in persisted_blob
    assert error_class_canary not in persisted_blob
    assert envelope["_kind"] == "audit"
    assert json.loads(envelope["invocation"]["result_canonical"]) == persisted_content
    assert envelope["invocation"]["error_message"] == "<redacted-arg-error-message>"
    assert envelope["invocation"]["error_class"] == "<redacted-arg-error-class>"
