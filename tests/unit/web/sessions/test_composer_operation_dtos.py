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


def test_failure_projection_rejects_unknown_vs_zero_and_private_shape_corruption() -> None:
    from elspeth.web.sessions.composer_operations import ComposerOperationFailureOutcome, ComposerOperationSavedRevisions
    from elspeth.web.sessions.schemas import ComposerOperationFailurePageResponse

    saved = ComposerOperationSavedRevisions(attribution="fenced", count=2, latest_state_id=SID, latest_version=7)
    outcome = ComposerOperationFailureOutcome(
        session_id=SID,
        operation_id=OP,
        kind="compose_message",
        user_message_id=None,
        settled_at=NOW,
        failure_code="operation_failed",
        error_type="operation_failed",
        diagnostic_id=OP,
        request_id="correlation",
        saved_revisions=saved,
    )
    assert ComposerOperationFailurePageResponse(session_id=SID, items=[outcome], next_cursor=None).items == [outcome]
    for changes in (
        {"body": {"detail": "private"}},
        {"error_type": "private_exception"},
        {"diagnostic_id": None},
        {"failure_code": "worker_lost"},
        {"operation_id": OP.replace("-", "")},
        {"settled_at": NOW.replace(tzinfo=None)},
    ):
        with pytest.raises(ValueError):
            ComposerOperationFailureOutcome.model_validate({**outcome.model_dump(), **changes})
    for changes in (
        {"attribution": "unavailable"},
        {"attribution": "unstarted"},
        {"count": True},
        {"count": None},
        {"count": 0},
        {"latest_state_id": None},
        {"latest_version": 0},
    ):
        with pytest.raises(ValueError):
            ComposerOperationSavedRevisions.model_validate({**saved.model_dump(), **changes})
    for items in ([outcome, outcome], [outcome.model_copy(update={"session_id": UUID(OP)})]):
        with pytest.raises(ValueError):
            ComposerOperationFailurePageResponse(session_id=SID, items=items, next_cursor=None)


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


def queued_v2_record() -> ComposerOperationRecord:
    from elspeth.web.sessions.composer_operations import composer_operation_v2_request_hash

    request = SendMessageRequest(operation_id=OP, content="hello")
    return replace(
        queued_record(),
        deadline_at=None,
        lifecycle_version=2,
        phase="queued",
        status_revision=0,
        current_segment_sequence=1,
        ownership_generation=1,
        checkpoint_id="33333333-3333-4333-8333-333333333333",
        journal_cursor=0,
        ingress_phase="pending",
        reserved_user_message_id=SID,
        request_hash=composer_operation_v2_request_hash(session_id=SID, kind="compose_message", request=request),
    )


@pytest.mark.parametrize(
    "status,phase",
    [
        ("queued", "queued"),
        ("running", "executing"),
        ("running", "continuation_queued"),
        ("running", "reconciling"),
        ("running", "retry_waiting"),
    ],
)
def test_v2_active_records_have_no_total_deadline_or_parent_claim(status, phase):
    record = replace(queued_v2_record(), status=status, phase=phase)
    assert record.deadline_at is None and record.lifecycle_version == 2
    assert not record.claim_token_present
    assert replace(record, request_json=None).request_json is None
    committed = replace(record, ingress_phase="committed", user_message_id=SID)
    assert committed.user_message_id == committed.reserved_user_message_id
    with pytest.raises(AuditIntegrityError, match="legacy deadline"):
        _ = record.legacy_deadline_at


def test_v1_deadline_and_default_lifecycle_are_preserved():
    record = queued_record()
    assert record.lifecycle_version == 1
    assert record.legacy_deadline_at == record.deadline_at == NOW + timedelta(seconds=60)
    assert record.phase is None and record.status_revision == record.journal_cursor == 0
    with pytest.raises(AuditIntegrityError):
        replace(record, deadline_at=None)
    for changes in ({"phase": "queued"}, {"checkpoint_id": OP}, {"current_segment_sequence": 1}):
        with pytest.raises(AuditIntegrityError):
            replace(record, **changes)


@pytest.mark.parametrize("version", [0, 3, True, "2"])
def test_record_rejects_unknown_or_nonexact_lifecycle(version):
    with pytest.raises(AuditIntegrityError):
        replace(queued_record(), lifecycle_version=version)


@pytest.mark.parametrize(
    "changes",
    [
        {"deadline_at": NOW},
        {"session_id": str(SID)},
        {"operation_id": "not-canonical"},
        {"kind": "unknown"},
        {"status": "unknown"},
        {"request_hash": "A" * 64},
        {"request_hash": "a" * 64},
        {"actor_user_id": ""},
        {"request_id": ""},
        {"status_revision": True},
        {"status_revision": -1},
        {"journal_cursor": True},
        {"journal_cursor": -1},
        {"current_segment_sequence": True},
        {"current_segment_sequence": 0},
        {"ownership_generation": True},
        {"ownership_generation": 0},
        {"checkpoint_id": None},
        {"checkpoint_id": "not-canonical"},
        {"ingress_phase": None},
        {"ingress_phase": "unknown"},
        {"reserved_user_message_id": None},
        {"reserved_user_message_id": str(SID)},
        {"user_message_id": SID},
        {"ingress_phase": "committed"},
        {"ingress_phase": "committed", "user_message_id": UUID(OP)},
        {"attempt": True},
        {"attempt": -1},
        {"claim_token_present": True},
        {"claim_expires_at": NOW},
        {"phase": None},
        {"phase": "settled"},
        {"settled_at": NOW},
        {"result_json": "{}"},
        {"failure_code": "worker_lost"},
        {"created_at": "not-time"},
        {"updated_at": NOW - timedelta(seconds=1)},
        {"started_at": NOW - timedelta(seconds=1)},
        {"request_json": "{}"},
        {"base_state_id": SID},
    ],
)
def test_v2_record_refuses_corrupt_active_bundles(changes):
    with pytest.raises(AuditIntegrityError):
        replace(queued_v2_record(), **changes)


def failed_v2_record():
    failure = ComposerOperationError(
        http_status=503,
        failure_code="worker_lost",
        error_type="composer_operation_worker_lost",
        body={"detail": "Lost"},
        diagnostic_id=None,
    )
    encoded = failure.model_dump_json()
    return replace(
        queued_v2_record(),
        status="failed",
        phase="settled",
        request_json=None,
        settled_at=NOW,
        failure_code="worker_lost",
        settled_by="owner_terminal",
        result_schema="composer_operation_error.v1",
        result_json=encoded,
        result_sha256=composer_operation_result_hash(encoded),
    )


def completed_v2_record():
    from elspeth.web.sessions.operation_codec import strict_response_hash
    from elspeth.web.sessions.schemas import ChatMessageResponse, MessageWithStateResponse

    response = MessageWithStateResponse(
        message=ChatMessageResponse(id="assistant", session_id=str(SID), role="assistant", content="Done", segments=[], created_at=NOW),
        proposals=[],
    )
    return replace(
        queued_v2_record(),
        status="completed",
        phase="settled",
        ingress_phase="committed",
        user_message_id=SID,
        request_json=None,
        settled_at=NOW,
        settled_by="owner_terminal",
        result_schema="message_with_state.v1",
        result_json=response.model_dump_json(),
        result_sha256=strict_response_hash(response),
    )


def test_v2_terminal_receipts_validate_actual_strict_result_and_digest():
    assert failed_v2_record().status == "failed"
    assert completed_v2_record().status == "completed"


@pytest.mark.parametrize("factory", [failed_v2_record, completed_v2_record])
@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "executing"},
        {"request_json": "{}"},
        {"settled_at": None},
        {"settled_by": None},
        {"result_json": None},
        {"result_json": "{}"},
        {"result_sha256": "a" * 64},
        {"result_schema": "unknown"},
        {"claim_token_present": True},
    ],
)
def test_v2_terminal_receipts_refuse_changed_bundles(factory, changes):
    with pytest.raises(AuditIntegrityError):
        replace(factory(), **changes)


def test_v2_terminal_receipt_refuses_failure_code_mismatch_and_cancelled_success():
    with pytest.raises(AuditIntegrityError):
        replace(failed_v2_record(), failure_code="request_cancelled")
    for changes in ({"failure_code": "worker_lost"}, {"cancel_requested_at": NOW}, {"user_message_id": None, "ingress_phase": "pending"}):
        with pytest.raises(AuditIntegrityError):
            replace(completed_v2_record(), **changes)


def test_actual_v2_parent_sql_row_decodes_every_continuation_field(tmp_path):
    from sqlalchemy import insert, select

    from elspeth.web.coordination.composer_operation_authority import _record_from_row
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
    from elspeth.web.sessions.composer_operations import composer_operation_v2_request_hash
    from elspeth.web.sessions.engine import create_session_engine
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.schema import initialize_session_schema
    from tests.fixtures.identities import ensure_test_identity

    engine = create_session_engine(f"sqlite:///{tmp_path / 'v2-record.db'}")
    try:
        initialize_session_schema(engine)
        with engine.begin() as connection:
            ensure_test_identity(connection, identity_id="alice")
        repository = SQLiteLocalSessionOperationAuthority(engine)
        session = repository.create_session_with_initial_fence(
            user_id="alice", title="V2 record projection", auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30
        )
        request = SendMessageRequest(operation_id=OP, content="hello")
        with engine.begin() as connection:
            now = database_now(connection)
            # Explicit SQL fixture: this does not enable runtime V2 admission.
            connection.execute(
                insert(jobs).values(
                    session_id=str(session.id),
                    operation_id=OP,
                    kind="compose_message",
                    status="queued",
                    request_hash=composer_operation_v2_request_hash(session_id=session.id, kind="compose_message", request=request),
                    actor_user_id="alice",
                    request_id="transport",
                    request_json=request.model_dump_json(),
                    deadline_at=None,
                    lifecycle_version=2,
                    phase="continuation_queued",
                    status_revision=7,
                    current_segment_sequence=2,
                    ownership_generation=3,
                    checkpoint_id=OP,
                    journal_cursor=4,
                    ingress_phase="pending",
                    reserved_user_message_id=str(SID),
                    created_at=now,
                    updated_at=now,
                )
            )
        with engine.connect() as connection:
            row = connection.execute(select(jobs)).one()
            full = _record_from_row(row, request_json=row.request_json)
            poll = _record_from_row(row)
        assert full.session_id == session.id and full.lifecycle_version == 2 and full.deadline_at is None
        assert (full.phase, full.status_revision, full.current_segment_sequence, full.ownership_generation) == (
            "continuation_queued",
            7,
            2,
            3,
        )
        assert (full.checkpoint_id, full.journal_cursor, full.ingress_phase, full.reserved_user_message_id) == (OP, 4, "pending", SID)
        assert full.request_json == request.model_dump_json()
        assert poll == replace(full, request_json=None)
    finally:
        engine.dispose()


def committed_v2_recompose_record(expected_user_message_id=SID):
    from elspeth.web.sessions.composer_operations import composer_operation_v2_request_hash

    request = RecomposeRequest(operation_id=OP, expected_user_message_id=expected_user_message_id)
    return replace(
        queued_v2_record(),
        kind="compose_recompose",
        ingress_phase="committed",
        user_message_id=SID,
        request_json=request.model_dump_json(),
        request_hash=composer_operation_v2_request_hash(session_id=SID, kind="compose_recompose", request=request),
    )


def test_v2_recompose_record_uses_its_separate_lifecycle_hash():
    request = RecomposeRequest(operation_id=OP, expected_user_message_id=SID)
    record = committed_v2_recompose_record()
    assert record.kind == "compose_recompose"
    assert record.user_message_id == record.reserved_user_message_id == request.expected_user_message_id
    assert replace(record, request_json=None).user_message_id == SID
    with pytest.raises(AuditIntegrityError):
        replace(record, request_hash=composer_operation_request_hash(session_id=SID, kind="compose_recompose", request=request))


def test_v2_recompose_request_refuses_different_reserved_original_message():
    with pytest.raises(AuditIntegrityError):
        committed_v2_recompose_record(expected_user_message_id=UUID(OP))


@pytest.mark.parametrize("omit_body", [False, True])
def test_v2_recompose_ingress_must_be_committed_even_for_poll_projection(omit_body):
    record = committed_v2_recompose_record()
    with pytest.raises(AuditIntegrityError):
        replace(record, ingress_phase="pending", user_message_id=None, request_json=None if omit_body else record.request_json)


@pytest.mark.parametrize("factory", [failed_v2_record, completed_v2_record])
def test_v2_terminal_recompose_preserves_committed_original_message(factory):
    original = committed_v2_recompose_record()
    terminal = replace(
        factory(), kind="compose_recompose", ingress_phase="committed", user_message_id=SID, request_hash=original.request_hash
    )
    assert terminal.user_message_id == terminal.reserved_user_message_id == SID
    with pytest.raises(AuditIntegrityError):
        replace(terminal, ingress_phase="pending", user_message_id=None)


@pytest.mark.parametrize("factory", [queued_v2_record, failed_v2_record, completed_v2_record])
@pytest.mark.parametrize(
    "changes",
    [
        {"claim_owner_instance_id": 42},
        {"claim_owner_instance_id": ""},
        {"claim_owner_instance_id": "x" * 129},
        {"session_operation_id": 42},
        {"session_operation_id": ""},
        {"session_operation_epoch": True},
        {"session_operation_epoch": 0},
        {"session_operation_epoch": -1},
        {"session_operation_epoch": 1.0},
        {"session_operation_epoch": "1"},
    ],
)
def test_v2_optional_owner_fence_projection_refuses_invalid_nominal_values(factory, changes):
    with pytest.raises(AuditIntegrityError):
        replace(factory(), **changes)


@pytest.mark.parametrize("factory", [queued_v2_record, failed_v2_record, completed_v2_record])
@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"claim_owner_instance_id": "owner"},
        {"claim_owner_instance_id": "x" * 128},
        {"session_operation_id": "operation"},
        {"session_operation_epoch": 1},
        {"claim_owner_instance_id": "owner", "session_operation_id": "operation", "session_operation_epoch": 2},
    ],
)
def test_v2_optional_owner_fence_projection_allows_absent_and_valid_values(factory, changes):
    record = replace(factory(), **changes)
    assert record.status == factory().status
    assert record.claim_token_present is False
    assert record.claim_expires_at is None
    assert replace(record, request_json=None).claim_owner_instance_id == record.claim_owner_instance_id


@pytest.mark.parametrize("changes", [{"claim_token_present": 0}, {"base_state_id": str(SID), "request_json": None}])
def test_v2_record_owned_projection_fields_remain_nominal(changes):
    with pytest.raises(AuditIntegrityError):
        replace(queued_v2_record(), **changes)


def test_v1_status_projection_preserves_deadline_wire_bytes():
    from elspeth.web.sessions.routes.composer.operations import composer_operation_status_response

    projected = composer_operation_status_response(queued_record(), db_now=NOW, poll_after_ms=1000)
    assert projected.model_dump_json() == (
        '{"operation_id":"22222222-2222-4222-8222-222222222222","kind":"compose_message","status":"queued",'
        '"poll_after_ms":1000,"cancel_requested":false,"deadline_at":"2026-10-06T00:01:00Z",'
        '"deadline_remaining_ms":60000,"result":null,"error":null}'
    )


def test_v2_record_is_refused_by_legacy_status_projection():
    from elspeth.web.sessions.routes.composer.operations import composer_operation_status_response

    with pytest.raises(AuditIntegrityError, match="legacy deadline"):
        composer_operation_status_response(queued_v2_record(), db_now=NOW, poll_after_ms=1000)
