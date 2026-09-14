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

from elspeth.web.composer.protocol import ComposerService
from elspeth.web.composer.service import ComposerAdmissionRefused, ComposerServiceError
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
        response = client.post(f"/api/sessions/{session_id}/messages", json={"content": "Build a pipeline"})
    else:
        # recompose precondition: the conversation ends at the failed user turn.
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                service.add_message(uuid.UUID(session_id), "user", "Build a pipeline", writer_principal="route_user_message")
            )
        finally:
            loop.close()
        response = client.post(f"/api/sessions/{session_id}/recompose")

    progress = client.get(f"/api/sessions/{session_id}/composer-progress").json()
    return response.status_code, response.json()["detail"], progress


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
