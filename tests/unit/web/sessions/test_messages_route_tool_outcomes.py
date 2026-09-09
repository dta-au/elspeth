"""GET /messages stamps server-derived tool-call outcomes (elspeth-f5e6723133).

The conversation view's assistant rows carry OpenAI-shaped tool_calls
envelopes. In auto_commit mode executed mutations have no proposal rows, so
the SPA had nothing to distinguish an applied mutation from a lookup and
labelled everything "Looked up". The route now projects the Tier-1
role="tool" rows (per-call ``composition_state_id`` from the primary writer)
into ``outcome`` / ``applied_state_version`` stamps on the matching
envelopes — server-authenticated, never name-derived.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import insert

from elspeth.web.sessions.models import chat_messages_table, composition_states_table
from tests.unit.web.conftest import _make_session


async def _get(test_client: TestClient, url: str) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=test_client.app),
        base_url="http://test",
        cookies=test_client.cookies,
    ) as client:
        response = await client.get(url)
        test_client.cookies.update(response.cookies)
        return response


def _seed_compose_turn_with_outcomes(test_client: TestClient) -> dict[str, Any]:
    """One assistant round-trip: an applied mutation, a lookup, a rejection."""
    session_id = str(uuid4())
    assistant_id = str(uuid4())
    state_id = str(uuid4())
    now = datetime.now(UTC)
    with test_client.app.state.phase3_engine.begin() as conn:
        _make_session(conn, session_id=session_id, user_id="alice")
        conn.execute(
            insert(composition_states_table).values(
                id=state_id,
                session_id=session_id,
                version=2,
                is_valid=False,
                provenance="tool_call",
                created_at=now,
            )
        )
        conn.execute(
            insert(chat_messages_table),
            [
                {
                    "id": str(uuid4()),
                    "session_id": session_id,
                    "role": "user",
                    "content": "Build it",
                    "raw_content": None,
                    "tool_calls": None,
                    "tool_call_id": None,
                    "sequence_no": 1,
                    "writer_principal": "route_user_message",
                    "created_at": now,
                    "composition_state_id": None,
                    "parent_assistant_id": None,
                },
                {
                    "id": assistant_id,
                    "session_id": session_id,
                    "role": "assistant",
                    "content": "Applying the pipeline",
                    "raw_content": None,
                    "tool_calls": [
                        {"id": "call_mutate", "type": "function", "function": {"name": "upsert_node", "arguments": "{}"}},
                        {"id": "call_read", "type": "function", "function": {"name": "list_transforms", "arguments": "{}"}},
                        {"id": "call_reject", "type": "function", "function": {"name": "upsert_node", "arguments": "{}"}},
                    ],
                    "tool_call_id": None,
                    "sequence_no": 2,
                    "writer_principal": "compose_loop",
                    "created_at": now,
                    "composition_state_id": None,
                    "parent_assistant_id": None,
                },
                {
                    "id": str(uuid4()),
                    "session_id": session_id,
                    "role": "tool",
                    "content": json.dumps({"success": True}),
                    "raw_content": None,
                    "tool_calls": None,
                    "tool_call_id": "call_mutate",
                    "sequence_no": 3,
                    "writer_principal": "compose_loop",
                    "created_at": now,
                    # Primary-writer semantics: this call inserted state v2.
                    "composition_state_id": state_id,
                    "parent_assistant_id": assistant_id,
                },
                {
                    "id": str(uuid4()),
                    "session_id": session_id,
                    "role": "tool",
                    "content": json.dumps({"success": True, "data": {"transforms": []}}),
                    "raw_content": None,
                    "tool_calls": None,
                    "tool_call_id": "call_read",
                    "sequence_no": 4,
                    "writer_principal": "compose_loop",
                    "created_at": now,
                    "composition_state_id": None,
                    "parent_assistant_id": assistant_id,
                },
                {
                    "id": str(uuid4()),
                    "session_id": session_id,
                    "role": "tool",
                    "content": json.dumps({"success": False, "validation": {"errors": ["bad options"]}}),
                    "raw_content": None,
                    "tool_calls": None,
                    "tool_call_id": "call_reject",
                    "sequence_no": 5,
                    "writer_principal": "compose_loop",
                    "created_at": now,
                    "composition_state_id": None,
                    "parent_assistant_id": assistant_id,
                },
            ],
        )
    return {"session_id": session_id, "assistant_id": assistant_id}


@pytest.mark.asyncio
async def test_conversation_view_stamps_outcomes_on_assistant_envelopes(
    test_client: TestClient,
) -> None:
    seeded = _seed_compose_turn_with_outcomes(test_client)
    session_id = seeded["session_id"]

    response = await _get(test_client, f"/api/sessions/{session_id}/messages")

    assert response.status_code == 200
    rows = response.json()
    assistant = next(row for row in rows if row["role"] == "assistant")
    by_id = {entry["id"]: entry for entry in assistant["tool_calls"]}

    assert by_id["call_mutate"]["outcome"] == "applied"
    assert by_id["call_mutate"]["applied_state_version"] == 2
    assert by_id["call_read"]["outcome"] == "completed"
    assert by_id["call_read"]["applied_state_version"] is None
    assert by_id["call_reject"]["outcome"] == "rejected"
    assert by_id["call_reject"]["applied_state_version"] is None


@pytest.mark.asyncio
async def test_envelopes_without_tool_rows_are_left_unstamped(
    test_client: TestClient,
) -> None:
    # A turn whose tool rows never landed (e.g. unwind path) must not invent
    # outcomes — the SPA falls back to its conservative default label.
    session_id = str(uuid4())
    now = datetime.now(UTC)
    with test_client.app.state.phase3_engine.begin() as conn:
        _make_session(conn, session_id=session_id, user_id="alice")
        conn.execute(
            insert(chat_messages_table).values(
                id=str(uuid4()),
                session_id=session_id,
                role="assistant",
                content="Calling a tool",
                raw_content=None,
                tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "upsert_node", "arguments": "{}"}}],
                tool_call_id=None,
                sequence_no=1,
                writer_principal="compose_loop",
                created_at=now,
                composition_state_id=None,
                parent_assistant_id=None,
            )
        )

    response = await _get(test_client, f"/api/sessions/{session_id}/messages")

    assert response.status_code == 200
    rows = response.json()
    (assistant,) = [row for row in rows if row["role"] == "assistant"]
    (entry,) = assistant["tool_calls"]
    assert "outcome" not in entry
