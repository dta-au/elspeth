"""Executable producer-to-HTTP proof for failed-turn audit diagnostics."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from fastapi.responses import JSONResponse

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.app import create_app
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedToolCall,
    _AdmittedToolFunction,
)
from elspeth.web.composer.turn_audit import persist_turn_audit
from tests.helpers.session_fences import make_compose_context
from tests.unit.web.test_composer_exception_handlers import _AUDIT_INTEGRITY_DETAIL, _audit_request, _settings


def exercise_audit_failed_turn() -> None:
    """Vary attempted calls at P4 and observe the registered HTTP consumer.

    The persistence seam fails before any tool responses are committed.
    ``persist_turn_audit`` itself must attach the diagnostic; constructing
    metadata here would bypass the producer this exercise protects.
    """

    async def exercise(data_dir: Path) -> None:
        app = create_app(_settings(data_dir))
        service = app.state.composer_service
        sessions = app.state.session_service
        handler = app.exception_handlers[AuditIntegrityError]
        request = _audit_request("audit-consumer-request")
        sql_canary = "private-sql-audit-consumer-canary"
        provider_canary = "private-provider-audit-consumer-canary"
        bodies = []
        for attempted in (1, 3):
            calls = tuple(
                _AdmittedToolCall(
                    id=f"audit-call-{index}",
                    function=_AdmittedToolFunction(name="get_pipeline", arguments="{}"),
                )
                for index in range(attempted)
            )
            fault = AuditIntegrityError(sql_canary)
            assert fault.failed_turn is None
            with (
                patch.object(sessions, "persist_compose_turn_async", autospec=True, side_effect=fault) as persist,
                pytest.raises(AuditIntegrityError) as caught,
            ):
                await persist_turn_audit(
                    service,
                    tool_outcomes=(),
                    decoded_args_by_call_id={},
                    assistant_message=_AdmittedAssistantMessage(content=provider_canary),
                    raw_assistant_content=provider_canary,
                    assistant_tool_calls=calls,
                    crash_pending=False,
                    session_id="audit-consumer-session",
                    session_operation_context=make_compose_context("audit-consumer-session"),
                    current_state_id=None,
                    persisted_tool_call_turn=False,
                    persisted_assistant_message_id=None,
                    persisted_assistant_content=None,
                    assistant_row_uses_current_dispatch=True,
                )
            persist.assert_awaited_once()
            assert persist.call_args.kwargs["raw_content"] == provider_canary
            assert caught.value is fault
            response = await handler(request, caught.value)
            assert isinstance(response, JSONResponse)
            assert response.status_code == 500
            body = json.loads(response.body)
            assert body["error_type"] == "audit_integrity_error"
            assert body["detail"] == _AUDIT_INTEGRITY_DETAIL
            assert body["request_id"] == "audit-consumer-request"
            assert body["failed_turn"] == {
                "assistant_message_id": None,
                "tool_calls_attempted": attempted,
                "tool_responses_persisted": 0,
                "transcript_url": None,
            }
            assert sql_canary not in response.body.decode()
            assert provider_canary not in response.body.decode()
            assert "diagnostic" not in body
            bodies.append(body)
        assert bodies[0]["failed_turn"] != bodies[1]["failed_turn"]

        # A pre-P4 failure has no annotation and must remain distinguishable.
        unannotated = AuditIntegrityError(sql_canary)
        assert unannotated.failed_turn is None
        degraded_response = await handler(request, unannotated)
        assert isinstance(degraded_response, JSONResponse)
        assert degraded_response.status_code == 500
        degraded = json.loads(degraded_response.body)
        assert degraded["error_type"] == "audit_integrity_error"
        assert degraded["detail"] == _AUDIT_INTEGRITY_DETAIL
        assert degraded["request_id"] == "audit-consumer-request"
        assert degraded["diagnostic"] == "no_failed_turn_metadata"
        assert degraded["reason"] == "originated outside compose-loop annotation scope"
        assert "failed_turn" not in degraded
        assert sql_canary not in degraded_response.body.decode()
        assert provider_canary not in degraded_response.body.decode()

    with TemporaryDirectory(prefix="elspeth-audit-consumer-") as directory:
        asyncio.run(exercise(Path(directory)))
