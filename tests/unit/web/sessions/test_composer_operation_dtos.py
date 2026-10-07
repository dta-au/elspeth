"""Strict semantic/byte/status and exact-lease stream admission controls."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.sessions.composer_operations import (
    COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH,
    ComposerOperationClaim,
    ComposerOperationError,
    ComposerOperationRecord,
    ComposerOperationRunning,
    composer_operation_request_hash,
    composer_operation_result_hash,
    validate_composer_operation_request_json,
)
from elspeth.web.sessions.schemas import (
    ComposerOperationStatusResponse,
    ComposerOperationStreamHeartbeatFrame,
    ComposerOperationStreamProgressFrame,
    ComposerOperationStreamProgressPayload,
    ComposerOperationStreamStatusFrame,
    ComposerOperationStreamStatusPayload,
    ComposerOperationStreamTerminalFrame,
    ComposerOperationStreamTerminalPayload,
    RecomposeRequest,
    SendMessageRequest,
    encode_composer_operation_stream_frame,
)

SID = UUID("11111111-1111-4111-8111-111111111111")
OP = "22222222-2222-4222-8222-222222222222"
NOW = datetime(2026, 10, 6, tzinfo=UTC)


def queued_record() -> ComposerOperationRecord:
    request = SendMessageRequest(operation_id=OP, content="hello")
    return ComposerOperationRecord(
        session_id=SID,
        operation_id=OP,
        kind="compose_message",
        status="queued",
        request_hash=composer_operation_request_hash(session_id=SID, kind="compose_message", request=request),
        actor_user_id="alice",
        request_id="transport",
        base_state_id=None,
        deadline_at=NOW + timedelta(seconds=60),
        created_at=NOW,
        updated_at=NOW,
        started_at=None,
        settled_at=None,
        cancel_requested_at=None,
        claim_token_present=False,
        claim_owner_instance_id=None,
        claim_expires_at=None,
        attempt=0,
        session_operation_id=None,
        session_operation_epoch=None,
        user_message_id=None,
        failure_code=None,
        settled_by=None,
        result_schema=None,
        result_json=None,
        result_sha256=None,
        request_json=request.model_dump_json(),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"operation_id": OP.upper()},
        {"operation_id": OP.replace("-", "")},
        {"operation_id": 3},
        {"content": 3},
        {"content": " "},
        {"state_id": str(SID).replace("-", "")},
        {"state_id": 3},
        {"extra": True},
        {"content": "x" * 65537},
    ],
)
def test_request_refuses_noncanonical_coercion_extras_and_bad_content(changes) -> None:
    # Uppercase a UUID containing hexadecimal letters for the spelling control.
    if changes == {"operation_id": OP.upper()}:
        changes = {"operation_id": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"}
    with pytest.raises(ValueError):
        SendMessageRequest.model_validate({"operation_id": OP, "content": "hello", **changes})


def test_recompose_is_strict_and_base_omission_is_null() -> None:
    request = RecomposeRequest.model_validate({"operation_id": OP, "expected_user_message_id": str(SID)})
    assert request.expected_user_message_id == SID
    assert request.state_id is None
    with pytest.raises(ValueError):
        RecomposeRequest.model_validate({"operation_id": OP, "expected_user_message_id": None})


def test_worst_case_json_escape_and_utf8_bounds() -> None:
    request = SendMessageRequest(operation_id=OP, content="😀" * 65536, state_id=SID)
    ascii_json = json.dumps(request.model_dump(mode="json"), ensure_ascii=True)
    utf8_json = request.model_dump_json()
    assert len(ascii_json.encode("utf-8")) == 786555
    assert len(utf8_json.encode("utf-8")) == 262262
    assert len(ascii_json.encode("utf-8")) <= COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH
    validate_composer_operation_request_json(ascii_json)
    validate_composer_operation_request_json(utf8_json)
    # Text character length is below the cap; encoded UTF-8 bytes are above it.
    oversized = "😀" * (COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH // 4 + 1)
    assert len(oversized) < COMPOSER_OPERATION_REQUEST_JSON_MAX_LENGTH
    with pytest.raises(ValueError):
        validate_composer_operation_request_json(oversized)
    retry = RecomposeRequest(operation_id=OP, expected_user_message_id=SID, state_id=SID)
    assert len(retry.model_dump_json().encode("utf-8")) == 171


@pytest.mark.parametrize(
    "changes",
    [
        {"attempt": True},
        {"status": "unknown"},
        {"request_hash": "A" * 64},
        {"claim_token_present": True},
        {"session_operation_id": "partial"},
        {"settled_at": NOW},
        {"user_message_id": SID},
        {"updated_at": NOW - timedelta(seconds=1)},
        {"request_json": "{}"},
        {"base_state_id": SID},
    ],
)
def test_tier_one_record_refuses_corrupt_live_bundles(changes) -> None:
    with pytest.raises(AuditIntegrityError):
        replace(queued_record(), **changes)


def test_record_running_terminal_digest_and_poll_projection() -> None:
    queued = queued_record()
    running = replace(
        queued,
        status="running",
        attempt=1,
        claim_token_present=True,
        claim_owner_instance_id="worker",
        session_operation_id="session-op",
        session_operation_epoch=1,
        started_at=NOW,
    )
    assert replace(running, request_json=None).status == "running"
    error = ComposerOperationError(
        http_status=499, failure_code="request_cancelled", error_type="request_cancelled", body={"detail": "stopped"}, diagnostic_id=None
    )
    encoded = error.model_dump_json()
    terminal = replace(
        running,
        status="failed",
        request_json=None,
        claim_token_present=False,
        failure_code="request_cancelled",
        settled_by="owner_terminal",
        settled_at=NOW,
        result_schema="composer_operation_error.v1",
        result_json=encoded,
        result_sha256=composer_operation_result_hash(encoded),
    )
    assert terminal.session_operation_epoch == 1
    for changes in (
        {"result_sha256": "a" * 64},
        {"failure_code": "http_error"},
        {"session_operation_id": None},
        {"result_schema": "unknown"},
    ):
        with pytest.raises(AuditIntegrityError):
            replace(terminal, **changes)


def test_claim_running_custody_refuses_cross_session_and_noncompose() -> None:
    claim = ComposerOperationClaim(SID, OP, "claim", 1)
    context = SessionOperationContext(SessionOperationFence(str(SID), "fence", "lease", 1), SessionOperationKind.COMPOSE)
    assert ComposerOperationRunning(claim, context).claim == claim
    for foreign in (
        SessionOperationContext(SessionOperationFence(OP, "fence", "lease", 1), SessionOperationKind.COMPOSE),
        SessionOperationContext(context.fence, SessionOperationKind.PROPOSAL),
    ):
        with pytest.raises(ValueError):
            ComposerOperationRunning(claim, foreign)
    with pytest.raises(ValueError):
        ComposerOperationClaim(SID, OP, "claim", True)


def test_status_response_requires_authoritative_terminal_shape() -> None:
    base = {
        "operation_id": OP,
        "kind": "compose_message",
        "status": "running",
        "poll_after_ms": 1000,
        "cancel_requested": False,
        "deadline_at": NOW,
        "deadline_remaining_ms": 1000,
    }
    assert ComposerOperationStatusResponse(**base).result is None
    for changes in ({"status": "completed"}, {"status": "failed"}, {"extra": 1}, {"poll_after_ms": "1000"}):
        with pytest.raises(ValueError):
            ComposerOperationStatusResponse(**{**base, **changes})


def test_stream_closed_frames_and_safe_progress_exact_lease_binding() -> None:
    common = {"session_id": str(SID), "operation_id": OP, "sequence": 1}
    frame = ComposerOperationStreamStatusFrame(
        **common, payload=ComposerOperationStreamStatusPayload(status="running", cancel_requested=False, deadline_remaining_ms=1000)
    )
    assert encode_composer_operation_stream_frame(frame).startswith(b"event: status\ndata: ")
    assert b"answer" not in encode_composer_operation_stream_frame(
        ComposerOperationStreamTerminalFrame(**common, payload=ComposerOperationStreamTerminalPayload(status="completed"))
    )
    heartbeat = ComposerOperationStreamHeartbeatFrame(**common)
    assert "payload" not in heartbeat.model_dump()
    with pytest.raises(ValueError):
        ComposerOperationStreamHeartbeatFrame(**common, payload={"answer": "secret"})
    payload = ComposerOperationStreamProgressPayload(
        session_operation_id="fence",
        session_operation_epoch=1,
        request_token="lease",
        request_id="user-message",
        phase="calling_model",
        headline="Working",
        updated_at=NOW,
    )
    progress = ComposerOperationStreamProgressFrame(**common, payload=payload)
    assert progress.payload.request_id != progress.operation_id
    for changes in ({"session_operation_epoch": 0}, {"headline": "x" * 181}, {"evidence": ("ok",) * 5}, {"answer": "provider text"}):
        with pytest.raises(ValueError):
            ComposerOperationStreamProgressPayload(**{**payload.model_dump(), **changes})
    with pytest.raises(ValueError):
        encode_composer_operation_stream_frame(progress.model_copy(update={"operation_id": "wrong"}))
    # A deliberately corrupted owned instance cannot bypass serialization validation.
    with pytest.raises(ValueError):
        encode_composer_operation_stream_frame(
            progress.model_copy(update={"payload": payload.model_copy(update={"headline": "x" * 70000})})
        )
