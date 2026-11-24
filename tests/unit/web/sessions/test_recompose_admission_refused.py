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
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_progress import ComposerProgressEvent
from elspeth.contracts.credential_material import find_credential_material
from elspeth.web.composer.protocol import ComposerAdmissionRefused, ComposerService, ComposerServiceError
from elspeth.web.credential_guard import (
    CREDENTIAL_REFUSAL_HEADLINE,
    CREDENTIAL_REFUSAL_NEXT_STEP,
    MODEL_OUTPUT_CREDENTIAL_REFUSAL_HEADLINE,
    MODEL_OUTPUT_CREDENTIAL_REFUSAL_NEXT_STEP,
    CredentialMaterialRefused,
)
from elspeth.web.sessions.models import chat_messages_table
from tests.helpers.composer_operations import settle_sync, strip_request_id
from tests.unit.web.sessions.test_routes import TestClient, _llm_call, _make_app

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


def _credential_refusal(surface: str) -> CredentialMaterialRefused:
    finding = find_credential_material({"value": "sk-" + "c" * 24}, purpose="admission")
    assert finding is not None
    return CredentialMaterialRefused(surface=surface, finding=finding)


@pytest.mark.parametrize(
    ("surface", "headline", "next_step"),
    [
        ("composer_provider_request", CREDENTIAL_REFUSAL_HEADLINE, CREDENTIAL_REFUSAL_NEXT_STEP),
        ("composer_provider_response", MODEL_OUTPUT_CREDENTIAL_REFUSAL_HEADLINE, MODEL_OUTPUT_CREDENTIAL_REFUSAL_NEXT_STEP),
        ("composer_tool_arguments", MODEL_OUTPUT_CREDENTIAL_REFUSAL_HEADLINE, MODEL_OUTPUT_CREDENTIAL_REFUSAL_NEXT_STEP),
    ],
    ids=["user-or-pipeline-content", "model-response", "model-tool-arguments"],
)
def test_a_credential_refusal_shows_the_headline_and_next_step_for_its_cause(
    tmp_path: Path, surface: str, headline: str, next_step: str
) -> None:
    refusal = _credential_refusal(surface)
    status_code, detail, progress = _post_after_compose_failure(tmp_path, refusal, route="messages")

    assert status_code == 422
    assert detail["failure_code"] == "credential_material_rejected"
    assert progress["phase"] == "failed"
    assert progress["reason"] == "admission_refused"
    assert progress["headline"] == headline
    # The evidence line is the refusal's detail, bounded as every progress text is.
    bounded = ComposerProgressEvent(phase="failed", headline=headline, evidence=(refusal.detail,), reason="admission_refused").evidence
    assert progress["evidence"] == list(bounded)
    assert progress["likely_next"] == next_step


def test_a_non_credential_admission_refusal_keeps_the_access_and_quota_step(tmp_path: Path) -> None:
    _, _, progress = _post_after_compose_failure(tmp_path, ComposerAdmissionRefused(_REFUSAL_TEXT), route="messages")

    assert progress["headline"] == "This request was refused by the admission policy."
    assert progress["likely_next"] == "Ask an administrator to review your access and quota configuration."


def test_a_dispatch_refusal_s_llm_calls_are_durable_after_the_route_refuses(tmp_path: Path) -> None:
    # The compose loop attaches its LLM calls to a dispatch-time refusal, as it
    # does to the wire scan's; the route's admission arm must make them durable.
    refusal = _credential_refusal("composer_tool_arguments")
    refusal.llm_calls = (_llm_call(provider_request_id="chatcmpl-dispatch-refusal"),)
    app, service = _make_app(tmp_path)
    composer = SimpleNamespace()
    composer.compose = AsyncMock(spec=ComposerService.compose, side_effect=refusal)
    app.state.composer_service = composer
    client = TestClient(app, raise_server_exceptions=False)
    session_id = client.post("/api/sessions", json={"title": "Dispatch refusal"}).json()["id"]
    settled = settle_sync(
        client,
        app,
        path=f"/api/sessions/{session_id}/messages",
        body={"content": "Build a pipeline", "operation_id": str(uuid.uuid4()), "state_id": None},
    )
    status, _body = settled.error()
    assert status == 422

    with service._engine.connect() as conn:
        envelopes = [
            entry
            for (tool_calls,) in conn.execute(
                select(chat_messages_table.c.tool_calls).where(
                    chat_messages_table.c.session_id == session_id, chat_messages_table.c.tool_calls.is_not(None)
                )
            )
            for entry in (json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls) or ()
        ]
    stored = [entry for entry in envelopes if entry["_kind"] == "llm_call_audit"]
    assert [entry["call"]["provider_request_id"] for entry in stored] == ["chatcmpl-dispatch-refusal"]
