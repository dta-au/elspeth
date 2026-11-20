"""Removed fork repair fields cannot enter a new tool or stored audit payload."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import insert

from elspeth.contracts.composer_audit import ComposerToolInvocation, ComposerToolStatus
from elspeth.core.canonical import canonical_json
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.audit_storage import redacted_tool_invocation_content_and_envelope
from elspeth.web.composer.prompts import history_message_indices
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import _duplicate_consumer_repair_suggestions
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.routes._helpers import _composer_chat_history
from tests.unit.web.composer._fork_facts_graph import chain
from tests.unit.web.composer.test_tools import _mock_catalog, execute_tool

CANARY = "CANARY-7f3a-repair"
TOOLS = ("create_blob", "get_blob_content", "request_interpretation_review", "set_pipeline", "patch_node_options")
REMOVED = {"strategy", "current_input", "new_input", "tool_sequence", "tool", "arguments"}


def _old_response(arguments: object) -> dict[str, Any]:
    return {
        "success": True,
        "validation": {
            "is_valid": False,
            "errors": [],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [
                {
                    "code": "duplicate_connection_consumer",
                    "connection": "shared",
                    "reason": "reason",
                    "strategy": CANARY,
                    "affected_consumers": [{"id": "llm_00", "current_input": CANARY, "new_input": CANARY}],
                    "tool_sequence": [{"tool": CANARY, "arguments": arguments}],
                }
            ],
        },
    }


def _invocation(tool: str, response: dict[str, object]) -> ComposerToolInvocation:
    arguments = canonical_json({})
    result = canonical_json(response)
    now = datetime(2026, 11, 20, tzinfo=UTC)
    return ComposerToolInvocation(
        tool_call_id="call_repair_old",
        tool_name=tool,
        arguments_canonical=arguments,
        arguments_hash=hashlib.sha256(arguments.encode()).hexdigest(),
        result_canonical=result,
        result_hash=hashlib.sha256(result.encode()).hexdigest(),
        status=ComposerToolStatus.SUCCESS,
        error_class=None,
        error_message=None,
        version_before=1,
        version_after=1,
        started_at=now,
        finished_at=now,
        latency_ms=1,
        actor="composer-web:test",
        authority_arguments_canonical=arguments if tool == "set_pipeline" else None,
        authority_arguments_hash=hashlib.sha256(arguments.encode()).hexdigest() if tool == "set_pipeline" else None,
    )


def _exact_set_pipeline_arguments(state: CompositionState, catalog: CatalogService) -> dict[str, Any]:
    """Read the exact authoring document through get_pipeline_state, following its pages.

    This graph's exact arguments exceed the state-inspection byte budget, so a
    plain read returns a structural view. ``field_path=""`` selects the whole
    document as exact JSON pages bound to one content hash.
    """
    arguments: dict[str, Any] = {"component": "set_pipeline_arguments", "field_path": ""}
    chunks: list[str] = []
    while True:
        page = execute_tool("get_pipeline_state", arguments, state, catalog).to_dict()["data"]
        assert (page["view"], page["encoding"]) == ("field", "json")
        chunks.append(page["content"])
        if page["next_offset_chars"] is None:
            return json.loads("".join(chunks))
        arguments = {**arguments, "offset_chars": page["next_offset_chars"], "expected_sha256": page["sha256"]}


def test_duplicate_consumer_producer_and_shared_tool_validation_have_only_facts() -> None:
    state = chain(steps=2, shared=2, llm_count=2)
    suggestions = _duplicate_consumer_repair_suggestions(state, state.validate())
    # Facts plus one direction sentence: the connection, every consumer id,
    # and the reason. No node data, strategy, or ready-made tool call.
    assert suggestions == [
        {
            "code": "duplicate_connection_consumer",
            "connection": "shared",
            "reason": (
                "One connection can feed one processing node; every node listed reads this one. "
                "Change only the input bindings; preserve existing node options."
            ),
            "affected_consumers": [{"id": "llm_00"}, {"id": "llm_01"}],
        }
    ]

    catalog = _mock_catalog()
    cases = {
        "create_blob": {},
        "get_blob_content": {},
        "request_interpretation_review": {},
        "patch_node_options": {"node_id": "llm_00", "patch": {}},
    }
    for tool, arguments in cases.items():
        result = execute_tool(tool, arguments, state, catalog).to_dict()
        assert result["validation"]["graph_repair_suggestions"] == suggestions, tool
        serialized = json.dumps(result["validation"]["graph_repair_suggestions"])
        assert all(f'"{key}"' not in serialized for key in REMOVED), tool

    # set_pipeline refuses an unchanged invalid candidate. Its response uses
    # the same ToolResult validation serializer and shadow as these tools.
    candidate = _exact_set_pipeline_arguments(state, catalog)
    set_result = execute_tool("set_pipeline", candidate, state, catalog, validate_arguments=True).to_dict()
    assert set_result["validation"]["graph_repair_suggestions"] == []


@pytest.mark.parametrize("arguments", [{CANARY: CANARY}, "<redacted-repair-arguments>"])
def test_injected_old_shape_never_emits_a_redacted_response(arguments: object) -> None:
    old = _old_response(arguments)
    for tool in TOOLS:
        # Typed response admission refuses this obsolete schema before output.
        with pytest.raises(ValidationError):
            redact_tool_call_response(tool, old, telemetry=NoopRedactionTelemetry())
    # Declarative tools take the separate, fail-closed validation fallback.
    projected = redact_tool_call_response("list_sources", old, telemetry=NoopRedactionTelemetry())
    assert CANARY not in json.dumps(projected)


@pytest.mark.parametrize("arguments", [{CANARY: CANARY}, "<redacted-repair-arguments>"])
def test_injected_old_shape_never_enters_audit_tool_row(arguments: object) -> None:
    old = _old_response(arguments)
    for tool in TOOLS:
        with pytest.raises(ValidationError):
            redacted_tool_invocation_content_and_envelope(_invocation(tool, old))
    content, record = redacted_tool_invocation_content_and_envelope(_invocation("list_sources", old))
    assert CANARY not in content
    assert CANARY not in json.dumps(record)


@pytest.mark.asyncio
async def test_old_stored_tool_row_is_excluded_from_provider_history(composer_service_with_real_sessions, result_session_id) -> None:
    # Older releases stored only the argument sentinel in tool content.
    old = _old_response("<redacted-repair-arguments>")
    old["validation"]["graph_repair_suggestions"][0]["strategy"] = "old-strategy"
    old["validation"]["graph_repair_suggestions"][0]["affected_consumers"][0]["current_input"] = "old-input"
    old["validation"]["graph_repair_suggestions"][0]["affected_consumers"][0]["new_input"] = "old-new-input"
    old["validation"]["graph_repair_suggestions"][0]["tool_sequence"][0]["tool"] = "old-tool"
    service = composer_service_with_real_sessions._sessions_service
    assistant_id, tool_id = str(uuid4()), str(uuid4())
    now = datetime(2026, 11, 20, tzinfo=UTC)
    with service._engine.begin() as conn:
        conn.execute(
            insert(chat_messages_table).values(
                id=assistant_id,
                session_id=result_session_id,
                role="assistant",
                content="",
                sequence_no=1,
                writer_principal="compose_loop",
                created_at=now,
            )
        )
        conn.execute(
            insert(chat_messages_table).values(
                id=tool_id,
                session_id=result_session_id,
                role="tool",
                content=json.dumps(old),
                tool_calls=[{"_kind": "audit", "invocation": {"arguments_canonical": '"<redacted-repair-arguments>"'}}],
                tool_call_id="call_repair_old",
                parent_assistant_id=assistant_id,
                sequence_no=2,
                writer_principal="compose_loop",
                created_at=now,
            )
        )
    stored = await service.get_messages(UUID(result_session_id))
    assert len(stored) == 2
    assert CANARY not in stored[1].content
    assert stored[1].tool_calls[0]["invocation"]["arguments_canonical"] == '"<redacted-repair-arguments>"'
    history = _composer_chat_history(stored)
    assert history == [{"role": "assistant", "content": ""}]
    assert history_message_indices(history) == ()
    assert all(value not in json.dumps(history) for value in (CANARY, "old-strategy", "old-input", "old-new-input"))
