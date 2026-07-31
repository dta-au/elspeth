"""Route-boundary exception handlers for compose-loop persistence failures."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse
from structlog.testing import capture_logs

from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.web.app import create_app
from elspeth.web.config import WebSettings
from elspeth.web.preferences.service import CorruptPreferencesError
from elspeth.web.sessions.audit_story_service import AuditStoryIntegrityError, AuditStoryNotRecordedError
from elspeth.web.sessions.protocol import AuditAccessLogWriteError, StaleComposeStateError


def _settings(tmp_path: Path) -> WebSettings:
    return WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
    )


_AUDIT_INTEGRITY_DETAIL = "ELSPETH stopped before replying because it could not verify this session's audit trail."


def _audit_request(request_id: str = "req-audit-integrity-1") -> Request:
    """Request double with the fields the handler reads.

    ``RequestIdMiddleware`` guarantees ``request.state.request_id`` on
    every request that reaches an app-level exception handler; the
    handler additionally reads ``request.url.path`` and
    ``request.method`` for the structured log event.
    """
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/sessions/abc/messages",
            "headers": [],
            "query_string": b"",
        }
    )
    request.state.request_id = request_id
    return request


@pytest.mark.asyncio
async def test_audit_integrity_error_handler_returns_static_500(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[AuditIntegrityError]

    with capture_logs() as logs:
        response = await handler(
            _audit_request("req-ft-1"),
            AuditIntegrityError(
                "hidden sql detail",
                failed_turn=FailedTurnMetadata(
                    assistant_message_id=None,
                    tool_calls_attempted=2,
                    tool_responses_persisted=0,
                ),
            ),
        )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error_type"] == "audit_integrity_error"
    assert body["detail"] == _AUDIT_INTEGRITY_DETAIL
    assert body["request_id"] == "req-ft-1"
    assert body["failed_turn"] == {
        "assistant_message_id": None,
        "tool_calls_attempted": 2,
        "tool_responses_persisted": 0,
        "transcript_url": None,
    }
    # The raise-site message reaches the SERVER log only — never the body.
    assert "hidden sql detail" not in response.body.decode()
    events = [log for log in logs if log["event"] == "http_audit_integrity_error"]
    assert len(events) == 1
    event = events[0]
    assert event["path"] == "/api/sessions/abc/messages"
    assert event["method"] == "POST"
    assert event["request_id"] == "req-ft-1"
    assert event["exc_class"] == "AuditIntegrityError"
    assert event["message"] == "hidden sql detail"
    assert event["failed_turn_present"] is True


def test_create_app_wires_existing_session_service_into_composer(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))

    assert app.state.composer_service._sessions_service is app.state.session_service


@pytest.mark.asyncio
async def test_audit_integrity_error_handler_returns_degraded_body_without_failed_turn(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[AuditIntegrityError]

    with capture_logs() as logs:
        response = await handler(_audit_request("req-no-ft-1"), AuditIntegrityError("outside compose loop"))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error_type"] == "audit_integrity_error"
    assert body["detail"] == _AUDIT_INTEGRITY_DETAIL
    assert body["request_id"] == "req-no-ft-1"
    assert body["diagnostic"] == "no_failed_turn_metadata"
    assert body["reason"] == "originated outside compose-loop annotation scope"
    assert "failed_turn" not in body
    events = [log for log in logs if log["event"] == "http_audit_integrity_error"]
    assert len(events) == 1
    event = events[0]
    assert event["request_id"] == "req-no-ft-1"
    assert event["exc_class"] == "AuditIntegrityError"
    assert event["message"] == "outside compose loop"
    assert event["failed_turn_present"] is False


@pytest.mark.asyncio
async def test_audit_integrity_log_distinguishes_snapshot_guard_from_cohort_poison(tmp_path: Path) -> None:
    """The ~40 byte-identical fail-closed 500s are distinguishable in the LOG.

    The response body is deliberately static, so the ONLY discriminator
    between (a) the send_message transcript snapshot guard and (b) the
    guided-failure cohort verifier refusing a poisoned session is the
    server-authored exception message captured by the handler's
    ``message`` field. Pin both raise-site phrasings so a refactor that
    collapses them back into indistinguishable copy fails here.
    """
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[AuditIntegrityError]
    snapshot_guard_message = (
        "Tier 1 audit anomaly: send_message transcript snapshot for session s does not end at inserted user message m. "
        "Refusing to compose against interleaved session history."
    )
    cohort_message = "guided failure audit cohort does not match the exact durable evidence rows"

    with capture_logs() as logs:
        await handler(_audit_request("req-a"), AuditIntegrityError(snapshot_guard_message))
        await handler(_audit_request("req-b"), AuditIntegrityError(cohort_message))

    messages = [log["message"] for log in logs if log["event"] == "http_audit_integrity_error"]
    assert messages == [snapshot_guard_message, cohort_message]
    assert messages[0] != messages[1]


@pytest.mark.asyncio
async def test_stale_compose_state_error_handler_returns_409(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[StaleComposeStateError]

    response = await handler(cast(Request, object()), StaleComposeStateError("stale"))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    assert json.loads(response.body)["error_type"] == "stale_compose_state"


@pytest.mark.asyncio
async def test_audit_access_log_write_error_handler_returns_static_500(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[AuditAccessLogWriteError]

    response = await handler(cast(Request, object()), AuditAccessLogWriteError("hidden db path"))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error_type"] == "audit_access_log_write_failed"
    assert "hidden db path" not in response.body.decode()


@pytest.mark.asyncio
async def test_corrupt_preferences_error_handler_returns_structured_500(tmp_path: Path) -> None:
    # ``CorruptPreferencesError`` is the named Tier-1 read-guard exception
    # the preferences service raises when a stored row violates a closed-
    # list invariant (default_composer_mode outside ``_VALID_MODES``,
    # tutorial_completed_at unparseable, etc.). The exception's docstring
    # (``preferences/service.py``) promises that "the application's
    # exception handlers (`app.py`) match this specific failure mode for
    # incident response without string-grepping the message" — but no
    # handler existed before this test. Without the handler the backend
    # returned a bare 500 and the frontend swallowed it (App.tsx
    # bootstrapPrefs().catch(console.error)) leaving a corrupt-row user
    # with no signal anything was wrong.
    #
    # The structured body exposes ``field_name`` (closed enum) and
    # ``user_id`` (the caller's own id) so the frontend can distinguish
    # corruption from transient unavailability; ``bad_value`` is
    # deliberately NOT exposed (could carry arbitrary content).
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[CorruptPreferencesError]

    response = await handler(
        cast(Request, object()),
        CorruptPreferencesError("alice", "bogus_mode", field_name="default_composer_mode"),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error_type"] == "corrupt_preferences"
    assert body["field_name"] == "default_composer_mode"
    assert body["user_id"] == "alice"
    # bad_value deliberately not in body — could be arbitrary content
    assert "bogus_mode" not in response.body.decode()


@pytest.mark.asyncio
async def test_audit_story_integrity_error_handler_returns_structured_500(tmp_path: Path) -> None:
    # The audit-story route in ``sessions/routes.py`` raises
    # ``AuditStoryIntegrityError`` (a sibling of ``AuditIntegrityError``)
    # when either (a) the session-runs row never got a landscape_run_id,
    # or (b) the Landscape projection itself is corrupt (missing rows,
    # non-bool seeded_from_cache, cache replay without a cache_key). A
    # NULL llm_call_count is NOT integrity — that is the never-recorded
    # absent state, mapped to 404 via AuditStoryNotRecordedError. The named
    # type carries the discriminator that lets the handler return a
    # structured ``error_type`` body — without it, incident-response code
    # would have to string-grep the message. This test guards two
    # regressions: the handler going missing, and the route flattening
    # the named exception back to bare ``RuntimeError`` (which would
    # bypass this handler).
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[AuditStoryIntegrityError]

    response = await handler(
        cast(Request, object()),
        AuditStoryIntegrityError("Landscape run 'abc-123' has non-bool seeded_from_cache=None"),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error_type"] == "audit_story_integrity_error"
    assert body["detail"] == "Landscape run 'abc-123' has non-bool seeded_from_cache=None"


@pytest.mark.asyncio
async def test_audit_story_not_recorded_error_handler_returns_structured_404(tmp_path: Path) -> None:
    # ``AuditStoryNotRecordedError`` is the ABSENT state: the run exists but
    # its audit-story columns were never written (normal for every
    # non-tutorial run today). It maps to a structured 404 with a stable
    # machine code — never the integrity 500 — and the detail is fixed
    # plain language, not the internal exception text.
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[AuditStoryNotRecordedError]

    response = await handler(
        cast(Request, object()),
        AuditStoryNotRecordedError("Landscape run 'abc-123' has NULL llm_call_count"),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 404
    body = json.loads(response.body)
    assert body["error_type"] == "audit_story_not_recorded"
    assert "abc-123" not in response.body.decode()
