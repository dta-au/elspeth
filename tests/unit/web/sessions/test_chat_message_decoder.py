"""Actual typed SQLAlchemy row conversion, without granting read authority."""

from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID

import pytest
import structlog
from sqlalchemy import literal, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.control_messages import anti_anchor_control_envelope
from elspeth.web.sessions import service as service_module
from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row
from elspeth.web.sessions.converters import chat_message_record_from_row as legacy_chat_message_record_from_row
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.protocol import ChatMessageRecord
from elspeth.web.sessions.routes._helpers import _composer_chat_history
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry

MESSAGE = "00000000-0000-4000-8000-000000000301"
SESSION = "00000000-0000-4000-8000-000000000302"
STATE = "00000000-0000-4000-8000-000000000303"
PARENT = "00000000-0000-4000-8000-000000000304"
OPERATION = "00000000-0000-4000-8000-000000000305"
STAMP = datetime(2026, 10, 21, 12, 34, 56, 789012, tzinfo=UTC).replace(tzinfo=None)


def _message_row(engine, **changes):
    # Real typed SELECT results: no fabricated row class or schema bypass.
    values = {
        "id": MESSAGE,
        "session_id": SESSION,
        "role": "user",
        "content": "Original request",
        "raw_content": None,
        "tool_calls": None,
        "created_at": STAMP,
        "sequence_no": 17,
        "composition_state_id": None,
        "writer_principal": "route_user_message",
        "tool_call_id": None,
        "parent_assistant_id": None,
    }
    values.update(changes)
    with engine.connect() as conn:
        return conn.execute(
            select(
                *(literal(value, type_=chat_messages_table.c[name].type).label(name) for name, value in values.items()),
                literal(OPERATION).label("operation_id"),
            )
        ).one()


@pytest.fixture(params=["function", "legacy", "service"])
def decode_row(request, engine):
    if request.param == "function":
        return chat_message_record_from_row
    if request.param == "legacy":
        return legacy_chat_message_record_from_row
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.row_decoder"))
    return service._row_to_chat_message_record


def test_decoder_preserves_complete_row_and_frozen_tool_wire(engine, decode_row):
    wire = [{"id": "issued-call", "type": "function", "function": {"name": "get_pipeline_state", "arguments": "{}"}}]
    row = _message_row(
        engine,
        role="assistant",
        content="Model prose\nOperator suffix",
        raw_content="Model prose",
        tool_calls=wire,
        composition_state_id=STATE,
        writer_principal="compose_loop",
    )
    record = decode_row(row)
    assert type(record) is ChatMessageRecord
    assert record == ChatMessageRecord(
        id=UUID(MESSAGE),
        session_id=UUID(SESSION),
        role="assistant",
        content="Model prose\nOperator suffix",
        raw_content="Model prose",
        tool_calls=wire,
        created_at=STAMP.replace(tzinfo=UTC),
        sequence_no=17,
        composition_state_id=UUID(STATE),
        writer_principal="compose_loop",
        tool_call_id=None,
        parent_assistant_id=None,
        operation_id=None,
    )
    assert type(record.tool_calls) is tuple
    assert type(record.tool_calls[0]) is MappingProxyType
    assert type(record.tool_calls[0]["function"]) is MappingProxyType
    row.tool_calls[0]["function"]["arguments"] = '{"changed":true}'
    assert record.tool_calls[0]["function"]["arguments"] == "{}"
    assert _composer_chat_history((record,)) == [{"role": "assistant", "content": "Model prose"}]


def test_decoder_preserves_tool_parent_and_nullable_defaults(engine, decode_row):
    record = decode_row(
        _message_row(engine, role="tool", writer_principal="compose_loop", tool_call_id="issued-call", parent_assistant_id=PARENT)
    )
    assert record.tool_call_id == "issued-call" and record.parent_assistant_id == UUID(PARENT)
    assert record.raw_content is None and record.tool_calls is None and record.composition_state_id is None
    assert record.operation_id is None
    assert _composer_chat_history((record,)) == []


@pytest.mark.parametrize("empty", [None, ""])
def test_decoder_preserves_optional_uuid_truthiness_and_sequence_none(engine, decode_row, empty):
    record = decode_row(_message_row(engine, composition_state_id=empty, parent_assistant_id=empty, sequence_no=None))
    assert record.composition_state_id is None and record.parent_assistant_id is None and record.sequence_no is None


def test_decoder_requires_explicit_operation_instead_of_reading_row_column(engine, decode_row):
    row = _message_row(engine)
    assert row.operation_id == OPERATION
    assert decode_row(row).operation_id is None
    record = decode_row(row, operation_id=OPERATION)
    assert record.operation_id == UUID(OPERATION)
    assert record.id == UUID(MESSAGE) and record.session_id == UUID(SESSION)
    assert record.created_at == STAMP.replace(tzinfo=UTC)


@pytest.mark.parametrize("role,principal", [("assistant", "compose_loop"), ("user", "session_fork"), ("audit", "compose_loop")])
def test_decoder_refuses_operation_before_malformed_uuid(engine, decode_row, role, principal):
    row = _message_row(engine, id="not-a-uuid", role=role, writer_principal=principal)
    with pytest.raises(AuditIntegrityError, match="ingress receipt is attached to a non-user message"):
        decode_row(row, operation_id=OPERATION)


@pytest.mark.parametrize("field", ["id", "session_id", "composition_state_id", "parent_assistant_id"])
def test_decoder_refuses_invalid_required_or_present_optional_uuid(engine, decode_row, field):
    with pytest.raises(ValueError):
        decode_row(_message_row(engine, **{field: "not-a-uuid"}))


def test_decoder_refuses_invalid_explicit_operation_uuid(engine, decode_row):
    with pytest.raises(ValueError):
        decode_row(_message_row(engine), operation_id="not-a-uuid")


@pytest.mark.parametrize("field,match", [("role", r"chat_messages\.role"), ("writer_principal", r"chat_messages\.writer_principal")])
def test_decoder_preserves_owned_role_and_principal_refusal(engine, decode_row, field, match):
    with pytest.raises(AuditIntegrityError, match=match):
        decode_row(_message_row(engine, **{field: "unowned-value"}))


def test_decoder_preserves_control_audit_until_actual_history_replay(engine, decode_row):
    content = "Reconsider the repeated argument"
    row = _message_row(
        engine, role="audit", writer_principal="compose_loop", content=content, tool_calls=[anti_anchor_control_envelope(content)]
    )
    record = decode_row(row)
    assert record.role == "audit" and record.writer_principal == "compose_loop"
    assert record.tool_calls == (anti_anchor_control_envelope(content),)
    assert _composer_chat_history((record,)) == [{"role": "user", "content": content}]


def test_service_delegate_forwards_actual_row_and_operation_once(engine, monkeypatch):
    service = SessionServiceImpl(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test.row_decoder"))
    row = _message_row(engine)
    observed = []

    def observe(actual_row, *, operation_id=None):
        result = chat_message_record_from_row(actual_row, operation_id=operation_id)
        observed.append((actual_row, operation_id, result))
        return result

    monkeypatch.setattr(service_module, "chat_message_record_from_row", observe)
    result = service._row_to_chat_message_record(row, operation_id=OPERATION)
    assert len(observed) == 1
    assert observed[0][0] is row and observed[0][1] == OPERATION and result is observed[0][2]
    assert result.operation_id == UUID(OPERATION)


def test_decoder_compatibility_reexport_and_service_import_are_the_canonical_callable():
    assert legacy_chat_message_record_from_row is chat_message_record_from_row
    assert service_module.chat_message_record_from_row is chat_message_record_from_row
    assert chat_message_record_from_row.__module__ == "elspeth.web.sessions.chat_message_decoder"
