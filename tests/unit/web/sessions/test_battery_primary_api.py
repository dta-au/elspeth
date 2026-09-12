"""Check real primary persistence and API serialization before scoring evidence.

The mixed-cohort mutation below changes captured data, not the SQL writer.
"""

import asyncio
import json
from uuid import UUID

from evals.lib.battery_capture import tool_outcomes
from evals.lib.battery_score import score_path
from fastapi import APIRouter
from fastapi.routing import APIRoute, serialize_response
from sqlalchemy import select

from elspeth.web.sessions._persist_payload import RedactedToolRow, StatePayload
from elspeth.web.sessions.models import chat_messages_table, composition_states_table
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.routes._helpers import _message_response
from elspeth.web.sessions.routes.messages import register_message_routes
from tests.unit.evals.composer_battery import threadgen as tg
from tests.unit.web.sessions.test_persist_compose_turn import _make_session

pytest_plugins = ("tests.unit.web.sessions.test_persist_compose_turn",)


def test_real_primary_writer_stamps_only_applied_call(service):
    session_id = "00000000-0000-4000-8000-000000000987"
    with service._engine.begin() as conn:
        _make_session(conn, session_id=session_id)
    outcome = service.persist_compose_turn(
        session_id=session_id,
        assistant_content="local test",
        redacted_assistant_tool_calls=(
            {"id": "applied", "function": {"name": "set_source", "arguments": "{}"}},
            {"id": "pending", "function": {"name": "upsert_node", "arguments": "{}"}},
            {"id": "unknown", "function": {"name": "set_output", "arguments": "{}"}},
        ),
        redacted_tool_rows=(
            RedactedToolRow(
                tool_call_id="applied",
                content=json.dumps({"success": True}),
                composition_state_payload=StatePayload(data=CompositionStateData(), derived_from_state_id=None),
            ),
            RedactedToolRow(
                tool_call_id="pending",
                content=json.dumps({"success": True, "data": {"status": "APPROVAL_REQUIRED"}}),
                composition_state_payload=None,
            ),
            RedactedToolRow(
                tool_call_id="unknown", content=json.dumps({"success": True, "data": "<redacted>"}), composition_state_payload=None
            ),
        ),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
    )
    assert outcome.assistant_id is not None
    with service._engine.begin() as conn:
        rows = [dict(row) for row in conn.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).mappings()]
        state_ids = conn.execute(select(composition_states_table.c.id)).scalars().all()
    tool = [r for r in rows if r["role"] == "tool"]
    assert len(state_ids) == 1
    assert [r["composition_state_id"] for r in tool] == [state_ids[0], None, None]
    assert [r["tool_calls"] for r in tool] == [None, None, None]
    records = asyncio.run(service.get_messages(UUID(session_id)))
    router = APIRouter()
    register_message_routes(router)
    route = next(r for r in router.routes if isinstance(r, APIRoute) and r.path == "/{session_id}/messages" and "GET" in r.methods)
    wire = asyncio.run(
        serialize_response(
            field=route.response_field,
            response_content=[_message_response(r) for r in records],
            exclude_unset=route.response_model_exclude_unset,
            exclude_defaults=route.response_model_exclude_defaults,
            exclude_none=route.response_model_exclude_none,
        )
    )
    assert [r["tool_calls"] for r in wire if r["role"] == "tool"] == [None, None, None]
    print("ACTUAL_ROUTE_EXPLICIT_NULL", True)
    cap = tg.capture(wire, state=None)
    print("ACTUAL_PRIMARY", tool_outcomes(cap))
    path = score_path(cap)
    print("ACTUAL_PRIMARY_COUNTS", path.applied_mutation_calls, path.approval_pending_calls, path.approval_unknown_calls)
    assert path.applied_mutation_calls == 1
    assert path.approval_pending_calls == 1
    assert path.approval_unknown_calls == 1
    # Controlled capture corruption: a shared fallback cohort association
    # cannot supply application proof to another row with missing evidence.
    captured_tools = [r for r in cap.messages if r["role"] == "tool"]
    for r in captured_tools:
        r["composition_state_id"] = state_ids[0]
    captured_tools[0]["tool_calls"] = [
        {
            "_kind": "audit",
            "invocation": {
                "tool_call_id": "applied",
                "tool_name": "set_source",
                "status": "success",
                "version_before": 0,
                "version_after": 1,
            },
        }
    ]
    captured_tools[1]["tool_calls"] = [
        {
            "_kind": "audit",
            "invocation": {
                "tool_call_id": "pending",
                "tool_name": "upsert_node",
                "status": "success",
                "version_before": 1,
                "version_after": 1,
            },
        }
    ]
    del captured_tools[2]["tool_calls"]
    mixed = score_path(cap)
    assert (mixed.applied_mutation_calls, mixed.approval_pending_calls, mixed.approval_unknown_calls) == (1, 1, 1)
    print("MIXED_SHARED_STATE_COUNTS", 1, 1, 1)
