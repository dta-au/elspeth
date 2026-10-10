"""/recompose must classify a composer admission refusal the way /messages does.

A committed admission refusal (identity disabled, quota exhausted) is
permanent for the retry. ``ComposerAdmissionRefused`` subclasses
``ComposerServiceError``, so without its own arm the recompose route answered
502 ``composer_error`` with no ``failure_code`` and progress reason
``service_setup_failed``. The SPA hides Retry only for a permanent
``failure_code``, so it kept offering a Retry that could never succeed.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from elspeth.web.composer.protocol import ComposerAdmissionRefused, ComposerService, ComposerServiceError
from tests.helpers.composer_operations import settle_sync, strip_request_id
from tests.unit.web.sessions.test_routes import TestClient, _make_app

_REFUSAL_TEXT = "Composer admission refused: identity_disabled."


def _post_after_compose_failure(tmp_path: Path, failure: ComposerServiceError, *, route: str) -> tuple[int, Any, Any]:
    """Drive ``route`` ("messages" or "recompose") with compose raising ``failure``.

    Returns the status code, the response ``detail`` and the terminal composer
    progress snapshot for that session.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    app, service = _make_app(tmp_path)
    composer = SimpleNamespace()
    composer.compose = AsyncMock(spec=ComposerService.compose, side_effect=failure)
    app.state.composer_service = composer
    client = TestClient(app, raise_server_exceptions=False)
    session_id = client.post("/api/sessions", json={"title": "Admission"}).json()["id"]

    if route == "messages":
        settled = settle_sync(
            client,
            app,
            path=f"/api/sessions/{session_id}/messages",
            body={"content": "Build a pipeline", "operation_id": str(uuid.uuid4()), "state_id": None},
        )
    else:
        # recompose precondition: the conversation ends at the failed user turn.
        loop = asyncio.new_event_loop()
        try:
            user_message = loop.run_until_complete(
                service.add_message(uuid.UUID(session_id), "user", "Build a pipeline", writer_principal="route_user_message")
            )
        finally:
            loop.close()
        settled = settle_sync(
            client,
            app,
            path=f"/api/sessions/{session_id}/recompose",
            body={"expected_user_message_id": str(user_message.id), "operation_id": str(uuid.uuid4()), "state_id": None},
        )

    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    status, body = settled.error()
    detail = body["detail"]
    admission_request_id = settled.accepted.headers["X-Request-ID"]
    observation_request_id = settled.final.headers["X-Request-ID"]
    assert str(uuid.UUID(admission_request_id)) == admission_request_id
    assert str(uuid.UUID(observation_request_id)) == observation_request_id
    assert detail["request_id"] == admission_request_id
    assert observation_request_id != admission_request_id
    composer.compose.assert_awaited_once()
    # Compare exact refusal semantics after proving original admission provenance.
    return status, strip_request_id(detail), progress


def test_recompose_admission_refusal_is_403_with_permanent_failure_code(tmp_path: Path) -> None:
    status_code, detail, progress = _post_after_compose_failure(tmp_path, ComposerAdmissionRefused(_REFUSAL_TEXT), route="recompose")

    assert status_code == 403
    assert detail == {
        "error_type": "composer_admission_refused",
        "failure_code": "admission_refused",
        "detail": _REFUSAL_TEXT,
    }
    assert progress["phase"] == "failed"
    assert progress["reason"] == "admission_refused"


def test_recompose_admission_refusal_matches_send_message(tmp_path: Path) -> None:
    refusal = ComposerAdmissionRefused(_REFUSAL_TEXT)
    messages = _post_after_compose_failure(tmp_path / "messages", refusal, route="messages")
    recompose = _post_after_compose_failure(tmp_path / "recompose", refusal, route="recompose")

    messages_status, messages_detail, messages_progress = messages
    recompose_status, recompose_detail, recompose_progress = recompose
    assert recompose_status == messages_status
    assert recompose_detail == messages_detail
    for key in ("phase", "headline", "evidence", "likely_next", "reason"):
        assert recompose_progress[key] == messages_progress[key], key


def test_recompose_generic_composer_service_error_still_502_without_failure_code(tmp_path: Path) -> None:
    """Control: only the admission refusal is reclassified; other service errors keep the retryable 502."""
    status_code, detail, progress = _post_after_compose_failure(
        tmp_path, ComposerServiceError("prompt preparation failed"), route="recompose"
    )

    assert status_code == 502
    assert detail == {"error_type": "composer_error", "detail": "prompt preparation failed"}
    assert progress["phase"] == "failed"
    assert progress["reason"] == "service_setup_failed"
