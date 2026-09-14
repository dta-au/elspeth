"""GET /api/sessions/{sid}/messages ``include_rejection_reasons`` audit-grade opt-in.

elspeth-3e28029d2f persisted the reason a refused composer tool call returned
to the planner (``composition_rejection_events``; operator ruling 2026-09-02:
session data, the private audit-attribution surface). This file pins the READ
side: the owner-only, access-logged audit-grade messages view attaches that
reason to ``role="tool"`` rows ONLY when the caller opts in with
``include_tool_rows=true&include_rejection_reasons=true``.

The chat tool row's ``content`` stays the redacted projection, the SPA's
``include_tool_rows=true`` recovery query never carries a reason, and the
``planner_payload`` column never crosses HTTP.
"""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import insert

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._compose_loop_carriers import _AdmittedToolCall, _AdmittedToolFunction, _ToolOutcome
from elspeth.web.composer.redaction import redact_arg_error_response, redact_failure_response
from elspeth.web.composer.state import CompositionState, PipelineMetadata, ValidationEntry, ValidationSummary
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.turn_audit import build_rejection_records
from elspeth.web.sessions._persist_payload import RedactedToolRow, RejectionRecord
from elspeth.web.sessions.models import composition_rejection_events_table, session_operation_fences_table
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient
from tests.unit.web.conftest import _make_session
from tests.unit.web.sessions.test_persist_compose_turn import _test_compose_context

ARG_ERROR_SENTINEL = "L5-REJECTION-SENTINEL 'patch.name' must be a string, got int"
VALIDATION_SENTINEL = "L5-VALIDATION-SENTINEL requirement 'lead quality:score_lead' has no resolvable prompt wiring."
PLANNER_PAYLOAD_SENTINEL = "L5-PLANNER-PAYLOAD-SENTINEL-never-on-the-wire"
REDACTED_ARG_ERROR_PLACEHOLDER = "<redacted-arg-error-message>"


async def _get(test_client: TestClient, url: str, *, raise_app_exceptions: bool = True) -> Response:
    async with AsyncClient(
        transport=ASGITransport(app=test_client.app, raise_app_exceptions=raise_app_exceptions),
        base_url="http://test",
        cookies=test_client.cookies,
    ) as client:
        response = await client.get(url)
        test_client.cookies.update(response.cookies)
        return response


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _call(call_id: str, tool_name: str) -> _AdmittedToolCall:
    return _AdmittedToolCall(id=call_id, function=_AdmittedToolFunction(name=tool_name, arguments="{}"))


def _arg_error_outcome(call_id: str) -> _ToolOutcome:
    return _ToolOutcome(
        call=_call(call_id, "set_metadata"),
        response=None,
        error_class="ToolArgumentError",
        error_message=ARG_ERROR_SENTINEL,
        pre_version=1,
        post_version=1,
    )


def _plugin_crash_outcome(call_id: str) -> _ToolOutcome:
    # tool_batch.py's crash arms set BOTH fields to ``type(exc).__name__``
    # (elspeth-bb5e10f4c5): the raw ``str(exc)`` never reaches the outcome.
    return _ToolOutcome(
        call=_call(call_id, "upsert_node"),
        response=None,
        error_class="RuntimeError",
        error_message="RuntimeError",
        pre_version=1,
        post_version=1,
    )


def _validation_rejection_outcome(call_id: str) -> _ToolOutcome:
    return _ToolOutcome(
        call=_call(call_id, "set_pipeline"),
        response=ToolResult(
            success=False,
            updated_state=_empty_state(),
            validation=ValidationSummary(
                is_valid=False,
                errors=(
                    ValidationEntry(
                        component="transform:score_lead",
                        message=VALIDATION_SENTINEL,
                        severity="high",
                        error_code="vague_term_unwired",
                    ),
                ),
            ),
            affected_nodes=(),
        ),
        error_class=None,
        error_message=None,
        pre_version=1,
        post_version=1,
    )


def _success_outcome(call_id: str) -> _ToolOutcome:
    return _ToolOutcome(
        call=_call(call_id, "get_pipeline_state"),
        response=ToolResult(
            success=True,
            updated_state=_empty_state(),
            validation=ValidationSummary(is_valid=True, errors=()),
            affected_nodes=(),
        ),
        error_class=None,
        error_message=None,
        pre_version=1,
        post_version=1,
    )


def _tool_row_content(outcome: _ToolOutcome) -> str:
    """A stand-in for the redacted chat tool row the compose loop persists."""
    if outcome.error_class == "ToolArgumentError":
        return json.dumps(redact_arg_error_response(error_class=outcome.error_class, error_message=outcome.error_message))
    if outcome.error_class is not None:
        return json.dumps(
            redact_failure_response(status="plugin_crash", error_class=outcome.error_class, error_message=outcome.error_message)
        )
    if isinstance(outcome.response, ToolResult) and not outcome.response.success:
        return json.dumps({"success": False})
    return json.dumps({"success": True})


def _open_session(test_client: TestClient) -> str:
    service = test_client.app.state.phase3_sessions_service
    session_id = str(uuid4())
    context = _test_compose_context(session_id)
    with service._engine.begin() as conn:
        _make_session(conn, session_id=session_id, user_id="alice")
        conn.execute(
            insert(session_operation_fences_table).values(
                session_id=session_id,
                operation_id=context.fence.operation_id,
                lease_token=context.fence.lease_token,
                operation_kind=context.operation_kind.value,
                owner_instance_id="rejection-reasons-test-owner",
                operation_epoch=context.fence.operation_epoch,
                lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
                released_at=None,
            )
        )
    return session_id


async def _persist_turn(
    test_client: TestClient,
    session_id: str,
    outcomes: tuple[_ToolOutcome, ...],
    *,
    rejection_records: tuple[RejectionRecord, ...] | None = None,
) -> None:
    service = test_client.app.state.phase3_sessions_service
    await service.persist_compose_turn_async(
        session_id=session_id,
        assistant_content="calling tools",
        redacted_assistant_tool_calls=tuple(
            {"id": o.call.id, "type": "function", "function": {"name": o.call.function.name, "arguments": "{}"}} for o in outcomes
        ),
        redacted_tool_rows=tuple(
            RedactedToolRow(tool_call_id=o.call.id, content=_tool_row_content(o), composition_state_payload=None) for o in outcomes
        ),
        rejection_records=build_rejection_records(outcomes) if rejection_records is None else rejection_records,
        parent_composition_state_id=None,
        expected_current_state_id=None,
        writer_principal="compose_loop",
        plugin_crash_pending=False,
        session_operation_context=_test_compose_context(session_id),
    )


async def _seed_mixed_turn(test_client: TestClient) -> str:
    session_id = _open_session(test_client)
    await _persist_turn(
        test_client,
        session_id,
        (
            _arg_error_outcome("call_arg"),
            _validation_rejection_outcome("call_validation"),
            _plugin_crash_outcome("call_crash"),
            _success_outcome("call_ok"),
        ),
    )
    return session_id


def _tool_rows_by_call_id(body: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["tool_call_id"]: row for row in body if row["role"] == "tool"}


@pytest.mark.asyncio
async def test_include_tool_rows_alone_never_carries_a_rejection_reason(test_client: TestClient) -> None:
    """Known-negative: the SPA recovery transcript's exact query stays redacted."""
    session_id = await _seed_mixed_turn(test_client)

    response = await _get(test_client, f"/api/sessions/{session_id}/messages?include_tool_rows=true")

    assert response.status_code == 200
    body = response.json()
    assert {row["role"] for row in body} == {"assistant", "tool"}
    # The field is always present in the shape and null unless opted in.
    assert all("rejection" in row and row["rejection"] is None for row in body)
    assert ARG_ERROR_SENTINEL not in response.text
    assert VALIDATION_SENTINEL not in response.text
    # Known-positive for the byte scan: it does see tool-row content.
    assert REDACTED_ARG_ERROR_PLACEHOLDER in response.text


@pytest.mark.asyncio
async def test_opt_in_attaches_arg_error_reason_and_keeps_tool_row_content_redacted(test_client: TestClient) -> None:
    session_id = await _seed_mixed_turn(test_client)

    response = await _get(
        test_client,
        f"/api/sessions/{session_id}/messages?include_tool_rows=true&include_rejection_reasons=true",
    )

    assert response.status_code == 200
    body = response.json()
    tool_rows = _tool_rows_by_call_id(body)
    arg_row = tool_rows["call_arg"]
    assert arg_row["rejection"]["message"] == ARG_ERROR_SENTINEL
    assert arg_row["rejection"]["error_code"] == "ToolArgumentError"
    assert arg_row["rejection"]["tool_name"] == "set_metadata"
    assert arg_row["rejection"]["composition_state_id"] is None
    assert set(arg_row["rejection"]) == {"tool_name", "error_code", "message", "composition_state_id", "created_at"}
    # Redaction is unchanged: the chat tool row still carries the placeholder.
    assert json.loads(arg_row["content"])["error_message"] == REDACTED_ARG_ERROR_PLACEHOLDER
    # A successful call has no rejection row and stays null.
    assert tool_rows["call_ok"]["rejection"] is None
    # Non-tool rows never carry a rejection.
    assert all(row["rejection"] is None for row in body if row["role"] != "tool")


@pytest.mark.asyncio
async def test_opt_in_attaches_validation_rejection_primary_entry(test_client: TestClient) -> None:
    session_id = await _seed_mixed_turn(test_client)

    response = await _get(
        test_client,
        f"/api/sessions/{session_id}/messages?include_tool_rows=true&include_rejection_reasons=true",
    )

    assert response.status_code == 200
    rejection = _tool_rows_by_call_id(response.json())["call_validation"]["rejection"]
    assert rejection["message"] == VALIDATION_SENTINEL
    assert rejection["error_code"] == "vague_term_unwired"
    assert rejection["tool_name"] == "set_pipeline"


@pytest.mark.asyncio
async def test_opt_in_plugin_crash_reason_is_the_class_name_only(test_client: TestClient) -> None:
    session_id = await _seed_mixed_turn(test_client)

    response = await _get(
        test_client,
        f"/api/sessions/{session_id}/messages?include_tool_rows=true&include_rejection_reasons=true",
    )

    assert response.status_code == 200
    rejection = _tool_rows_by_call_id(response.json())["call_crash"]["rejection"]
    assert rejection == {
        "tool_name": "upsert_node",
        "error_code": "RuntimeError",
        "message": "RuntimeError",
        "composition_state_id": None,
        "created_at": rejection["created_at"],
    }


@pytest.mark.asyncio
async def test_rejection_reasons_without_tool_rows_is_422_and_writes_no_access_log(test_client: TestClient) -> None:
    session_id = await _seed_mixed_turn(test_client)

    response = await _get(test_client, f"/api/sessions/{session_id}/messages?include_rejection_reasons=true")

    assert response.status_code == 422
    assert ARG_ERROR_SENTINEL not in response.text
    sessions_service = test_client.app.state.session_service
    assert sessions_service.list_audit_access_log(session_id=session_id) == []


@pytest.mark.asyncio
async def test_access_log_records_the_rejection_reasons_arg(test_client: TestClient) -> None:
    session_id = await _seed_mixed_turn(test_client)

    response = await _get(
        test_client,
        f"/api/sessions/{session_id}/messages?include_tool_rows=true&include_rejection_reasons=true&api_key=secret",
    )

    assert response.status_code == 200
    sessions_service = test_client.app.state.session_service
    rows = sessions_service.list_audit_access_log(session_id=session_id)
    assert len(rows) == 1
    assert rows[0].query_args == {"include_tool_rows": "true", "include_rejection_reasons": "true"}


@pytest.mark.asyncio
async def test_rejection_reasons_fail_closed_when_access_log_write_fails(
    test_client: TestClient,
    inject_audit_access_log_write_failure,
) -> None:
    session_id = await _seed_mixed_turn(test_client)
    sessions_service = test_client.app.state.session_service
    inject_audit_access_log_write_failure(sessions_service)

    response = await _get(
        test_client,
        f"/api/sessions/{session_id}/messages?include_tool_rows=true&include_rejection_reasons=true",
        raise_app_exceptions=False,
    )

    assert response.status_code == 500
    assert response.json().get("error_type") == "audit_access_log_write_failed"
    assert ARG_ERROR_SENTINEL not in response.text
    assert VALIDATION_SENTINEL not in response.text


@pytest.mark.asyncio
async def test_planner_payload_never_reaches_the_wire(test_client: TestClient) -> None:
    session_id = _open_session(test_client)
    outcome = _arg_error_outcome("call_payload")
    await _persist_turn(
        test_client,
        session_id,
        (outcome,),
        rejection_records=(
            RejectionRecord(
                tool_call_id="call_payload",
                tool_name="set_metadata",
                error_code="ToolArgumentError",
                message=ARG_ERROR_SENTINEL,
                planner_payload=json.dumps({"error_class": "ToolArgumentError", "error_message": PLANNER_PAYLOAD_SENTINEL}),
            ),
        ),
    )

    response = await _get(
        test_client,
        f"/api/sessions/{session_id}/messages?include_tool_rows=true&include_llm_audit=true&include_raw_content=true&include_rejection_reasons=true",
    )

    assert response.status_code == 200
    # Known-positive: the reason itself is on the wire.
    assert ARG_ERROR_SENTINEL in response.text
    assert PLANNER_PAYLOAD_SENTINEL not in response.text


@pytest.mark.asyncio
async def test_duplicate_rejection_rows_for_one_tool_call_id_crash_the_view(test_client: TestClient) -> None:
    session_id = await _seed_mixed_turn(test_client)
    with test_client.app.state.phase3_engine.begin() as conn:
        conn.execute(
            insert(composition_rejection_events_table).values(
                id=str(uuid4()),
                session_id=session_id,
                composition_state_id=None,
                tool_call_id="call_arg",
                tool_name="set_metadata",
                error_code="ToolArgumentError",
                message="a second reason for the same call",
                planner_payload="{}",
                created_at=datetime.now(UTC),
            )
        )

    response = await _get(
        test_client,
        f"/api/sessions/{session_id}/messages?include_tool_rows=true&include_rejection_reasons=true",
        raise_app_exceptions=False,
    )

    assert response.status_code == 500
    assert "a second reason for the same call" not in response.text
    assert ARG_ERROR_SENTINEL not in response.text


@pytest.mark.asyncio
async def test_service_lists_rejection_events_without_planner_payload(test_client: TestClient) -> None:
    from elspeth.web.sessions.protocol import CompositionRejectionEventRecord

    session_id = await _seed_mixed_turn(test_client)
    service = test_client.app.state.phase3_sessions_service

    records = await service.list_composition_rejection_events(UUID(session_id))

    assert all(type(record) is CompositionRejectionEventRecord for record in records)
    assert {record.tool_call_id for record in records} == {"call_arg", "call_validation", "call_crash"}
    assert all(record.session_id == session_id for record in records)
    assert all(record.created_at.tzinfo is not None for record in records)
    assert "planner_payload" not in {field.name for field in fields(CompositionRejectionEventRecord)}
    # Another session's rows are never returned.
    assert await service.list_composition_rejection_events(uuid4()) == ()


def test_rejection_record_rejects_a_corrupt_scalar() -> None:
    from elspeth.web.sessions.protocol import CompositionRejectionEventRecord

    valid: dict[str, Any] = {
        "id": "r1",
        "session_id": "s1",
        "tool_call_id": "call_1",
        "tool_name": "set_metadata",
        "error_code": None,
        "message": "reason",
        "composition_state_id": None,
        "created_at": datetime.now(UTC),
    }
    CompositionRejectionEventRecord(**valid)
    for field_name, corrupt in (
        ("message", 7),
        ("tool_call_id", None),
        ("error_code", 3),
        ("composition_state_id", b"state"),
        ("created_at", "2026-09-14T00:00:00Z"),
    ):
        with pytest.raises(AuditIntegrityError):
            CompositionRejectionEventRecord(**{**valid, field_name: corrupt})


def test_rejection_index_refuses_a_duplicate_tool_call_id() -> None:
    from elspeth.web.sessions.protocol import CompositionRejectionEventRecord
    from elspeth.web.sessions.routes._helpers import _rejections_by_tool_call_id

    def record(record_id: str, tool_call_id: str) -> CompositionRejectionEventRecord:
        return CompositionRejectionEventRecord(
            id=record_id,
            session_id="s1",
            tool_call_id=tool_call_id,
            tool_name="set_metadata",
            error_code=None,
            message="reason",
            composition_state_id=None,
            created_at=datetime.now(UTC),
        )

    indexed = _rejections_by_tool_call_id((record("r1", "call_1"), record("r2", "call_2")))
    assert set(indexed) == {"call_1", "call_2"}
    with pytest.raises(AuditIntegrityError):
        _rejections_by_tool_call_id((record("r1", "call_1"), record("r2", "call_1")))
