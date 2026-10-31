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
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import insert

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.tools.generation import explain_validation_code
from elspeth.web.composer.visible_tool_event import VisibleToolEvent
from elspeth.web.sessions.models import chat_messages_table, composition_states_table
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.routes._helpers import _tool_call_outcomes_by_call_id
from tests.helpers.composition_state import stored_default_pipeline_metadata
from tests.helpers.session_fences import make_compose_context
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
                metadata_=stored_default_pipeline_metadata(),
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
                    "content": json.dumps(
                        {
                            "success": False,
                            "validation": {"errors": [{"error_code": "plugin_options_invalid", "message": "<redacted-response-text>"}]},
                        }
                    ),
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


async def _with_visible_companions(test_client: TestClient, session_id: str) -> list[ChatMessageRecord]:
    """Attach actual producer envelopes to the fixture's durable read image."""
    rows = await test_client.app.state.session_service.get_messages(UUID(session_id), limit=None)
    assistant = next(row for row in rows if row.role == "assistant")
    assert assistant.tool_calls is not None
    calls = {call["id"]: call for call in assistant.tool_calls}
    context = make_compose_context(session_id)
    result = []
    for row in rows:
        if row.role != "tool":
            result.append(row)
            continue
        assert row.tool_call_id is not None
        call = calls[row.tool_call_id]
        event = VisibleToolEvent.capture(
            context=context,
            request_id=None,
            tool_call_id=row.tool_call_id,
            tool_name=call["function"]["name"],
            received_arguments=call["function"]["arguments"],
            effective_arguments=call["function"]["arguments"],
            # This is evidence, never an authority to classify a lookup as
            # applied or grant a state version absent from the durable row.
            selected_result='{"success": true, "version_after": 999}',
            display_arguments=call["function"]["arguments"],
            display_result=row.content,
        ).with_parent(str(row.parent_assistant_id))
        result.append(replace(row, tool_calls=[event.to_envelope()]))
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("include_tool_rows", [False, True], ids=["conversation", "tool-opt-in"])
async def test_visible_companions_preserve_api_outcomes_and_do_not_become_calls(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch, include_tool_rows: bool
) -> None:
    seeded = _seed_compose_turn_with_outcomes(test_client)
    session_id = seeded["session_id"]
    records = await _with_visible_companions(test_client, session_id)

    async def companion_read(sid: UUID, *, limit: int | None, include_private_evidence: bool) -> list[ChatMessageRecord]:
        assert str(sid) == session_id and limit is None and include_private_evidence is False
        return records

    monkeypatch.setattr(test_client.app.state.session_service, "get_messages", companion_read)
    response = await _get(test_client, f"/api/sessions/{session_id}/messages?include_tool_rows={str(include_tool_rows).lower()}")
    assert response.status_code == 200
    rows = response.json()
    assistant = next(row for row in rows if row["role"] == "assistant")
    assert [call["id"] for call in assistant["tool_calls"]] == ["call_mutate", "call_read", "call_reject"]
    assert [call["outcome"] for call in assistant["tool_calls"]] == ["applied", "completed", "rejected"]
    assert [call["applied_state_version"] for call in assistant["tool_calls"]] == [2, None, None]
    tools = [row for row in rows if row["role"] == "tool"]
    if include_tool_rows:
        assert [row["tool_call_id"] for row in tools] == ["call_mutate", "call_read", "call_reject"]
        for row in tools:
            (envelope,) = row["tool_calls"]
            event = VisibleToolEvent.from_envelope(envelope)
            assert event.tool_call_id == row["tool_call_id"]
            assert event.parent_assistant_id == row["parent_assistant_id"]
    else:
        assert tools == []


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["payload_hash", "schema"])
async def test_visible_companion_corruption_is_not_silently_treated_as_a_primary_row(test_client: TestClient, damage: str) -> None:
    seeded = _seed_compose_turn_with_outcomes(test_client)
    rows = await _with_visible_companions(test_client, seeded["session_id"])
    row = next(row for row in rows if row.tool_call_id == "call_read")
    assert row.tool_calls is not None
    (event,) = row.tool_calls
    damaged = replace(row, tool_calls=[dict(event) | {damage: "invalid"}])
    with pytest.raises(AuditIntegrityError):
        _tool_call_outcomes_by_call_id([damaged], state_versions_by_id={})


@pytest.mark.asyncio
async def test_malformed_legacy_invocation_still_fails_closed_with_a_state_id(test_client: TestClient) -> None:
    seeded = _seed_compose_turn_with_outcomes(test_client)
    rows = await test_client.app.state.session_service.get_messages(UUID(seeded["session_id"]), limit=None)
    row = next(row for row in rows if row.tool_call_id == "call_mutate")
    malformed = replace(row, tool_calls=[{"_kind": "audit"}])
    with pytest.raises(KeyError, match="invocation"):
        _tool_call_outcomes_by_call_id([malformed], state_versions_by_id={str(row.composition_state_id): 2})


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
async def test_normal_conversation_resync_keeps_public_rejection_guidance_without_audit_opt_in(test_client: TestClient) -> None:
    seeded = _seed_compose_turn_with_outcomes(test_client)
    session_id = seeded["session_id"]

    for _ in range(2):
        response = await _get(test_client, f"/api/sessions/{session_id}/messages")
        assert response.status_code == 200
        rows = response.json()
        assert [row["role"] for row in rows] == ["user", "assistant"]
        assistant = rows[1]
        assert [call["id"] for call in assistant["tool_calls"]] == ["call_mutate", "call_read", "call_reject"]
        assert assistant["tool_calls"][2]["rejection"] == {
            "error_code": "plugin_options_invalid",
            "guidance": list(explain_validation_code("plugin_options_invalid")),
        }
        assert all(row["rejection"] is None for row in rows)
        assert "<redacted-response-text>" not in response.text


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
