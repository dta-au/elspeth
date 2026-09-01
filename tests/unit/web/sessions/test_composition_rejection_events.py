"""Durable composer rejection records (elspeth-3e28029d2f).

Operator ruling 2026-09-02: when a composer mutation tool rejects a payload,
the reason the planner saw — text AND reasoning — persists as SESSION data
(not Landscape data), keyed to session + the composition state that was
current at rejection time. These tests pin the persistence primitive.

Uses the shared ``engine`` fixture and ``_make_session`` helper from
``tests/unit/web/conftest.py``.
"""

from __future__ import annotations

import json

import pytest
import structlog
from sqlalchemy import text

from elspeth.web.sessions._persist_payload import (
    RedactedToolRow,
    RejectionRecord,
    StatePayload,
)
from elspeth.web.sessions.protocol import CompositionStateData
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.unit.web.conftest import _make_session


@pytest.fixture
def service(engine, tmp_path) -> SessionServiceImpl:
    return SessionServiceImpl(
        engine,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
    )


_PLANNER_PAYLOAD = json.dumps(
    {
        "success": False,
        "validation": {
            "errors": [
                {
                    "component": "transform:score_lead",
                    "message": (
                        "Pending vague_term review is not wired for resolution on "
                        "node 'score_lead': requirement 'lead quality:score_lead' "
                        "has no resolvable prompt wiring."
                    ),
                    "error_code": "vague_term_unwired",
                }
            ]
        },
    }
)


def _rejection_record(tool_call_id: str = "tc_fail") -> RejectionRecord:
    return RejectionRecord(
        tool_call_id=tool_call_id,
        tool_name="set_pipeline",
        error_code="vague_term_unwired",
        message=(
            "Pending vague_term review is not wired for resolution on node "
            "'score_lead': requirement 'lead quality:score_lead' has no "
            "resolvable prompt wiring."
        ),
        planner_payload=_PLANNER_PAYLOAD,
    )


def test_persist_compose_turn_persists_rejection_record(service):
    """A rejection record rides the compose-turn transaction into
    ``composition_rejection_events`` with the UNREDACTED planner payload."""
    with service._engine.begin() as conn:
        _make_session(conn, session_id="s_rej")

    service.persist_compose_turn(
        session_id="s_rej",
        assistant_content="attempting pipeline",
        redacted_assistant_tool_calls=({"id": "tc_fail", "function": {"name": "set_pipeline"}},),
        redacted_tool_rows=(
            RedactedToolRow(
                tool_call_id="tc_fail",
                content='{"error_code": "<redacted-response-text>"}',
                composition_state_payload=None,
            ),
        ),
        rejection_records=(_rejection_record(),),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
    )

    with service._engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT session_id, tool_call_id, tool_name, error_code, message, "
                "planner_payload, composition_state_id, created_at "
                "FROM composition_rejection_events WHERE session_id='s_rej'"
            )
        ).fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row.tool_call_id == "tc_fail"
    assert row.tool_name == "set_pipeline"
    assert row.error_code == "vague_term_unwired"
    assert "no resolvable prompt wiring" in row.message
    # The payload is exactly what the planner saw — unredacted.
    assert json.loads(row.planner_payload)["success"] is False
    assert "lead quality:score_lead" in row.planner_payload
    # A rejection advances no state: with no prior state, the linkage is NULL.
    assert row.composition_state_id is None
    assert row.created_at is not None


def test_rejection_record_links_to_state_current_at_rejection(service):
    """With a state committed earlier in the same turn, the rejection row
    links to it — the version the operator would inspect to reproduce."""
    with service._engine.begin() as conn:
        _make_session(conn, session_id="s_rej2")

    service.persist_compose_turn(
        session_id="s_rej2",
        assistant_content="two calls: one commits, one is refused",
        redacted_assistant_tool_calls=(
            {"id": "tc_ok", "function": {"name": "set_pipeline"}},
            {"id": "tc_fail", "function": {"name": "set_pipeline"}},
        ),
        redacted_tool_rows=(
            RedactedToolRow(
                tool_call_id="tc_ok",
                content='{"ok": true}',
                composition_state_payload=StatePayload(
                    data=CompositionStateData(),
                    derived_from_state_id=None,
                ),
            ),
            RedactedToolRow(
                tool_call_id="tc_fail",
                content='{"error_code": "<redacted-response-text>"}',
                composition_state_payload=None,
            ),
        ),
        rejection_records=(_rejection_record(),),
        parent_composition_state_id=None,
        expected_current_state_id=None,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
    )

    with service._engine.begin() as conn:
        state_id = conn.execute(text("SELECT id FROM composition_states WHERE session_id='s_rej2'")).scalar_one()
        linked = conn.execute(text("SELECT composition_state_id FROM composition_rejection_events WHERE session_id='s_rej2'")).scalar_one()

    assert linked == state_id


def test_persist_compose_turn_rejects_rejection_for_unknown_tool_call_id(service):
    """A rejection record whose tool_call_id is not among this turn's tool
    rows is a caller contract violation — refused before any insert."""
    with service._engine.begin() as conn:
        _make_session(conn, session_id="s_rej3")

    with pytest.raises(ValueError, match="tc_ghost"):
        service.persist_compose_turn(
            session_id="s_rej3",
            assistant_content="x",
            redacted_assistant_tool_calls=({"id": "tc_1", "function": {"name": "set_pipeline"}},),
            redacted_tool_rows=(
                RedactedToolRow(
                    tool_call_id="tc_1",
                    content="{}",
                    composition_state_payload=None,
                ),
            ),
            rejection_records=(_rejection_record(tool_call_id="tc_ghost"),),
            parent_composition_state_id=None,
            expected_current_state_id=None,
            writer_principal="compose_loop",
            plugin_crash_pending=False,
        )

    with service._engine.begin() as conn:
        chat_count = conn.execute(text("SELECT count(*) FROM chat_messages WHERE session_id='s_rej3'")).scalar_one()
        rej_count = conn.execute(text("SELECT count(*) FROM composition_rejection_events WHERE session_id='s_rej3'")).scalar_one()
    assert chat_count == 0
    assert rej_count == 0
