"""Route-boundary exception handlers for compose-loop persistence failures."""

from __future__ import annotations

import errno
import json
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from structlog.testing import capture_logs

from elspeth.contracts.errors import AuditIntegrityError, FailedTurnMetadata
from elspeth.contracts.secrets import FingerprintKeyMissingError, SecretDecryptionError
from elspeth.web.app import create_app
from elspeth.web.config import WebSettings
from elspeth.web.middleware.request_id import MAX_REQUEST_ID_LENGTH
from elspeth.web.preferences.service import CorruptPreferencesError
from elspeth.web.sessions.audit_story_service import AuditStoryIntegrityError, AuditStoryNotRecordedError
from elspeth.web.sessions.protocol import (
    AuditAccessLogWriteError,
    GuidedOperationFailed,
    RunAlreadyActiveError,
    StaleComposeStateError,
)
from elspeth.web.sessions.routes.guided_operations import raise_guided_operation_failure
from tests.unit.web._sync_asgi_client import SyncASGITestClient


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

    response = await handler(_audit_request("req-stale-1"), StaleComposeStateError("stale"))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["error_type"] == "stale_compose_state"
    assert body["request_id"] == "req-stale-1"


@pytest.mark.asyncio
async def test_audit_access_log_write_error_handler_returns_static_500(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[AuditAccessLogWriteError]

    response = await handler(_audit_request("req-aal-1"), AuditAccessLogWriteError("hidden db path"))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error_type"] == "audit_access_log_write_failed"
    assert body["request_id"] == "req-aal-1"
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
        _audit_request("req-prefs-1"),
        CorruptPreferencesError("alice", "bogus_mode", field_name="default_composer_mode"),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error_type"] == "corrupt_preferences"
    assert body["field_name"] == "default_composer_mode"
    assert body["user_id"] == "alice"
    assert body["request_id"] == "req-prefs-1"
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
        _audit_request("req-story-int-1"),
        AuditStoryIntegrityError("Landscape run 'abc-123' has non-bool seeded_from_cache=None"),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["error_type"] == "audit_story_integrity_error"
    assert body["detail"] == "Landscape run 'abc-123' has non-bool seeded_from_cache=None"
    assert body["request_id"] == "req-story-int-1"


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
        _audit_request("req-story-abs-1"),
        AuditStoryNotRecordedError("Landscape run 'abc-123' has NULL llm_call_count"),
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 404
    body = json.loads(response.body)
    assert body["error_type"] == "audit_story_not_recorded"
    assert body["request_id"] == "req-story-abs-1"
    assert "abc-123" not in response.body.decode()


class TestHTTPExceptionRequestIdEnvelope:
    """Every dict-shaped error envelope carries the response's correlation id.

    R2-F16b: ``RequestIdMiddleware`` stamps ``X-Request-ID`` on every
    response, and ``_audit_integrity_error_handler`` puts the same id in
    its body — but the guided routes consume their terminal exception
    in-route and re-raise a *closed* ``HTTPException``
    (``raise_guided_operation_failure``). That envelope never passed
    through an app-level handler, so the header correlated to nothing a
    user could quote back.

    The fix is ONE boundary rather than N routes: an app-level
    ``HTTPException`` handler injects ``request.state.request_id`` into
    any dict detail that does not already carry one, then delegates to
    FastAPI's default rendering. String details are untouched — a bare
    ``detail="..."`` is a plain-language message, not an envelope, and
    wrapping it would change the client contract of ~200 raise sites.
    """

    def test_exactly_one_http_exception_handler_registered_on_the_starlette_class(self, tmp_path: Path) -> None:
        """Compose with FastAPI's default handler; do not fork the boundary.

        FastAPI's ``setup()`` registers its default renderer against
        ``starlette.exceptions.HTTPException``. Registering ours against
        ``fastapi.HTTPException`` instead would create a SECOND key:
        MRO lookup would send route-raised ``fastapi.HTTPException``s to
        ours and router-raised 404s to FastAPI's, so half the envelopes
        would silently miss the id.
        """
        app = create_app(_settings(tmp_path))

        http_exception_keys = [key for key in app.exception_handlers if key in (StarletteHTTPException, HTTPException)]
        assert http_exception_keys == [StarletteHTTPException]

    @pytest.mark.asyncio
    async def test_dict_detail_gains_the_request_id(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        handler = app.exception_handlers[StarletteHTTPException]

        with capture_logs() as logs:
            response = await handler(
                _audit_request("req-dict-1"),
                HTTPException(status_code=500, detail={"error_type": "guided_operation_terminal_failure"}),
            )

        assert response.status_code == 500
        assert json.loads(response.body)["detail"] == {
            "error_type": "guided_operation_terminal_failure",
            "request_id": "req-dict-1",
        }
        assert logs == [
            {
                "event": "http_error_envelope",
                "log_level": "warning",
                "request_id": "req-dict-1",
                "status_code": 500,
            }
        ]

    @pytest.mark.asyncio
    async def test_dict_detail_log_never_projects_the_error_envelope(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        handler = app.exception_handlers[StarletteHTTPException]
        secret_canary = "provider-secret-correlation-canary"

        with capture_logs() as logs:
            await handler(
                _audit_request("req-bounded-1"),
                HTTPException(
                    status_code=422,
                    detail={
                        "error_type": "convergence",
                        "detail": secret_canary,
                        "provider_detail": secret_canary,
                        "session_id": secret_canary,
                    },
                ),
            )

        assert secret_canary not in repr(logs)
        assert logs == [
            {
                "event": "http_error_envelope",
                "log_level": "warning",
                "request_id": "req-bounded-1",
                "status_code": 422,
            }
        ]

    @pytest.mark.asyncio
    async def test_string_detail_is_left_exactly_as_raised(self, tmp_path: Path) -> None:
        app = create_app(_settings(tmp_path))
        handler = app.exception_handlers[StarletteHTTPException]

        with capture_logs() as logs:
            response = await handler(
                _audit_request("req-str-1"),
                HTTPException(status_code=400, detail="proposal_id must be a canonical UUID"),
            )

        assert response.status_code == 400
        assert json.loads(response.body) == {"detail": "proposal_id must be a canonical UUID"}
        assert "req-str-1" not in response.body.decode()
        assert not [log for log in logs if log["event"] == "http_error_envelope"]

    @pytest.mark.asyncio
    async def test_an_envelope_that_already_carries_a_request_id_is_not_overwritten(self, tmp_path: Path) -> None:
        """A route that sourced its own id keeps it — the boundary only fills gaps."""
        app = create_app(_settings(tmp_path))
        handler = app.exception_handlers[StarletteHTTPException]

        with capture_logs() as logs:
            response = await handler(
                _audit_request("req-from-middleware"),
                HTTPException(status_code=500, detail={"error_type": "x", "request_id": "req-from-route"}),
            )

        assert json.loads(response.body)["detail"]["request_id"] == "req-from-route"
        assert logs == [
            {
                "event": "http_error_envelope",
                "log_level": "warning",
                "request_id": "req-from-middleware",
                "status_code": 500,
            }
        ]

    @pytest.mark.asyncio
    async def test_response_headers_survive_the_rewrap(self, tmp_path: Path) -> None:
        """``HTTPException.headers`` is load-bearing for 401/429 — do not drop it."""
        app = create_app(_settings(tmp_path))
        handler = app.exception_handlers[StarletteHTTPException]

        response = await handler(
            _audit_request("req-hdr-1"),
            HTTPException(
                status_code=429,
                detail={"error_type": "rate_limited"},
                headers={"Retry-After": "30"},
            ),
        )

        assert response.headers["Retry-After"] == "30"
        assert json.loads(response.body)["detail"]["request_id"] == "req-hdr-1"

    @pytest.mark.asyncio
    async def test_the_guided_terminal_failure_envelope_is_covered_by_the_boundary(self, tmp_path: Path) -> None:
        """The exact envelope ``raise_guided_operation_failure`` closes over.

        The guided routes never reach an app-level handler for their own
        exception class — they catch it, settle the operation, and raise
        this. Pinning the composed result here is what makes the boundary
        (rather than four route edits) the fix.
        """
        app = create_app(_settings(tmp_path))
        handler = app.exception_handlers[StarletteHTTPException]

        with pytest.raises(HTTPException) as caught:
            raise_guided_operation_failure(GuidedOperationFailed(failure_code="integrity_error"))
        response = await handler(_audit_request("req-guided-1"), caught.value)

        assert response.status_code == 500
        assert json.loads(response.body)["detail"] == {
            "error_type": "guided_operation_terminal_failure",
            "failure_code": "integrity_error",
            "detail": "The operation failed an integrity check.",
            "request_id": "req-guided-1",
        }

    def test_the_body_request_id_equals_the_response_header_end_to_end(self, tmp_path: Path) -> None:
        """The finding, stated as a test: the header must correlate to something.

        Handler-level tests never exercise registration, middleware
        ordering, or dispatch. This one drives a real request through
        ``create_app``'s full middleware stack so the id in the body is
        provably the SAME id the operator reads off ``X-Request-ID``.
        """
        app = create_app(_settings(tmp_path))

        @app.get("/api/_probe/guided-terminal-failure")
        async def _probe_guided_terminal_failure() -> None:
            raise_guided_operation_failure(GuidedOperationFailed(failure_code="integrity_error"))

        @app.get("/api/_probe/string-detail")
        async def _probe_string_detail() -> None:
            raise HTTPException(status_code=409, detail="a plain-language message")

        # ``create_app`` mounts the built SPA as a StaticFiles Mount at "",
        # which matches EVERY path — so a route appended after it never runs
        # and these probes would 404 instead of exercising the boundary. The
        # mount is conditional on a built ``web/frontend/dist`` existing
        # (app.py's ``if frontend_dist.is_dir():``), so whether it is there
        # depends on whether the checkout has been built — which is exactly
        # why this needs pinning rather than luck.
        # Move the probes to the front of the table.
        app.router.routes.insert(0, app.router.routes.pop())
        app.router.routes.insert(0, app.router.routes.pop())

        client = SyncASGITestClient(app)

        response = client.get("/api/_probe/guided-terminal-failure")
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert detail["failure_code"] == "integrity_error"
        assert detail["request_id"] == response.headers["X-Request-ID"]
        assert detail["request_id"]

        # An inbound id is honoured, so the correlation works for a caller
        # that already owns a trace id.
        supplied = client.get("/api/_probe/guided-terminal-failure", headers={"X-Request-ID": "trace-abc-123"})
        assert supplied.json()["detail"]["request_id"] == "trace-abc-123"

        # ... and the string-detail contract is unchanged end to end.
        plain = client.get("/api/_probe/string-detail")
        assert plain.status_code == 409
        assert plain.json() == {"detail": "a plain-language message"}

    @pytest.mark.parametrize(
        "supplied",
        [
            pytest.param("trace|log-injection", id="unsafe-characters"),
            pytest.param("A" * (MAX_REQUEST_ID_LENGTH + 1), id="oversized"),
        ],
    )
    def test_unsafe_inbound_id_is_regenerated_before_body_header_and_log(self, tmp_path: Path, supplied: str) -> None:
        app = create_app(_settings(tmp_path))

        @app.get("/api/_probe/correlated-error")
        async def _probe_correlated_error() -> None:
            raise HTTPException(status_code=422, detail={"error_type": "convergence"})

        app.router.routes.insert(0, app.router.routes.pop())
        client = SyncASGITestClient(app)

        with capture_logs() as logs:
            response = client.get("/api/_probe/correlated-error", headers={"X-Request-ID": supplied})

        request_id = response.json()["detail"]["request_id"]
        assert request_id != supplied
        assert uuid.UUID(request_id).version == 4
        assert response.headers["X-Request-ID"] == request_id
        events = [log for log in logs if log["event"] == "http_error_envelope"]
        assert events == [
            {
                "event": "http_error_envelope",
                "log_level": "warning",
                "request_id": request_id,
                "status_code": 422,
            }
        ]


@pytest.mark.asyncio
async def test_run_already_active_error_handler_returns_correlated_409(tmp_path: Path) -> None:
    """Seam contract D: a flat 409 envelope, now correlated like its siblings.

    This handler renders its ``JSONResponse`` directly rather than raising an
    ``HTTPException``, so it never reaches the app-level ``HTTPException``
    boundary that injects ``request_id`` — it has to source the id itself.
    """
    app = create_app(_settings(tmp_path))
    handler = app.exception_handlers[RunAlreadyActiveError]

    exc = RunAlreadyActiveError("session-1")
    response = await handler(_audit_request("req-run-active-1"), exc)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["error_type"] == "run_already_active"
    assert body["detail"] == str(exc)
    assert body["request_id"] == "req-run-active-1"


@pytest.mark.asyncio
async def test_every_composer_error_envelope_carries_a_request_id(tmp_path: Path) -> None:
    """R2-F16b, stated once as a whole-surface invariant.

    The per-handler tests above each pin one envelope. This one pins the
    *rule*: every app-level handler that renders a structured (``error_type``)
    body must correlate to the ``X-Request-ID`` the same response carries.
    A new handler added without ``request_id`` fails here even if nobody
    remembers to write it a dedicated test.

    The two validation handlers are excluded by construction, not by
    oversight: their 422 bodies are lists of field errors with no
    ``error_type`` discriminator, and neither is a composer envelope. The
    ``HTTPException`` boundary is excluded because it is the injector.
    """
    app = create_app(_settings(tmp_path))
    cases: list[tuple[type[Exception], Exception]] = [
        (AuditIntegrityError, AuditIntegrityError("x")),
        (CorruptPreferencesError, CorruptPreferencesError("alice", "bad", field_name="default_composer_mode")),
        (AuditStoryIntegrityError, AuditStoryIntegrityError("x")),
        (AuditStoryNotRecordedError, AuditStoryNotRecordedError("x")),
        (StaleComposeStateError, StaleComposeStateError("x")),
        (AuditAccessLogWriteError, AuditAccessLogWriteError("x")),
        (RunAlreadyActiveError, RunAlreadyActiveError("x")),
        (FingerprintKeyMissingError, FingerprintKeyMissingError("x")),
        (SecretDecryptionError, SecretDecryptionError("x")),
        (OperationalError, OperationalError("SELECT 1", {}, Exception("db down"))),
        # Only a retryable errno becomes a 503 envelope; anything else is
        # deliberately re-raised as a real 500.
        (OSError, OSError(errno.ENOSPC, "no space left on device")),
    ]
    # Every registered handler that can render an ``error_type`` body is
    # covered; if a new one is registered, this assertion names it.
    excluded = (StarletteHTTPException, RequestValidationError, WebSocketRequestValidationError)
    structured = {key for key in app.exception_handlers if isinstance(key, type) and issubclass(key, Exception) and key not in excluded}
    assert structured == {case[0] for case in cases}

    for exc_type, exc in cases:
        response = await app.exception_handlers[exc_type](_audit_request("req-invariant-1"), exc)
        body = json.loads(response.body)
        assert body["request_id"] == "req-invariant-1", exc_type.__name__
