"""Fork compensation must not demote a Tier-1 integrity failure."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from elspeth.contracts.blobs import BlobContentMissingError, BlobForkCleanupError, BlobForkCleanupResult, BlobIntegrityError
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.models import guided_operations_table
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient


def _fork_target(client: TestClient) -> tuple[str, UUID]:
    """Seed a session whose fork reaches durable staging."""

    created = client.post("/api/sessions", json={"title": "fork compensation"})
    assert created.status_code == 201, created.json()
    session_id = created.json()["id"]

    started = client.post(
        f"/api/sessions/{session_id}/guided/start",
        json={"operation_id": str(uuid4()), "profile": "live", "intent": "Build a live pipeline"},
    )
    assert started.status_code == 200, started.json()
    state_id = started.json()["composition_state"]["id"]

    service = client.app.state.session_service
    message = asyncio.run(
        service.add_message(
            UUID(session_id),
            "user",
            "Fork from this turn.",
            composition_state_id=UUID(state_id),
            writer_principal="route_user_message",
        )
    )
    return session_id, message.id


def test_cleanup_integrity_failure_propagates_instead_of_a_coded_terminal_failure(
    composer_test_client: TestClient,
) -> None:
    """An AuditIntegrityError raised by blob compensation keeps its type.

    The compensation integrity fault must become the durable terminal reason
    before the operation is settled: routing the request through
    ``raise_guided_operation_failure`` would answer a Tier-1 corruption signal
    with the generic terminal-failure envelope, losing the dedicated
    ``AuditIntegrityError`` handler and its failed-turn metadata. The leaked
    child blobs still get their operator-actionable residue record.
    """
    client = composer_test_client
    session_id, from_message_id = _fork_target(client)
    service = client.app.state.session_service
    blob_service = client.app.state.blob_service
    operation_id = str(uuid4())
    payload = {
        "operation_id": operation_id,
        "from_message_id": str(from_message_id),
        "new_message_content": "Build the edited request.",
    }

    with (
        capture_logs() as cap_logs,
        patch.object(service, "settle_guided_fork_operation", side_effect=RuntimeError("settlement exploded")),
        patch.object(
            blob_service,
            "cleanup_blobs_for_fork",
            side_effect=AuditIntegrityError("fork compensation could not verify blob custody"),
        ),
        pytest.raises(AuditIntegrityError, match="could not verify blob custody"),
    ):
        client.post(
            f"/api/sessions/{session_id}/fork",
            json=payload,
        )

    replay = client.post(f"/api/sessions/{session_id}/fork", json=payload)
    assert replay.status_code == 500
    assert replay.json()["detail"]["failure_code"] == "integrity_error"
    residue = [entry for entry in cap_logs if entry.get("event") == "session.fork_blob_cleanup_failed"]
    assert len(residue) == 1
    assert residue[0]["exc_class"] == "AuditIntegrityError"
    with service._engine.connect() as conn:
        row = conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).one()
    assert row.failure_diagnostics == [
        "ForkFailure[RuntimeError]: phase=settlement",
        f"RecoveryFailed[AuditIntegrityError]: fork blob cleanup failed for child {residue[0]['child_session_id']}",
    ]


@pytest.mark.parametrize(
    "diagnostic",
    [
        "Guided fork settlement parent is missing",
        "Guided fork settlement child failed staged custody validation",
        "Guided fork settlement lost archived-to-active compare-and-swap",
    ],
)
def test_settlement_integrity_diagnostic_is_durable_for_replay_forensics(
    composer_test_client: TestClient,
    diagnostic: str,
) -> None:
    """A fork settlement integrity raise must survive terminal settlement.

    The operation row currently keeps only ``integrity_error``; that makes
    settlement failures indistinguishable from staging failures after the
    request has returned and the process-local exception is gone.
    """
    client = composer_test_client
    session_id, from_message_id = _fork_target(client)
    service = client.app.state.session_service
    operation_id = str(uuid4())
    payload = {
        "operation_id": operation_id,
        "from_message_id": str(from_message_id),
        "new_message_content": "Build the edited request.",
    }

    with patch.object(
        service,
        "settle_guided_fork_operation",
        side_effect=AuditIntegrityError(diagnostic),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/fork",
            json=payload,
        )

    assert response.status_code == 500
    assert response.json()["detail"]["failure_code"] == "integrity_error"
    assert diagnostic not in response.text
    replay = client.post(f"/api/sessions/{session_id}/fork", json=payload)
    assert replay.status_code == response.status_code
    assert replay.json() == response.json()
    with service._engine.connect() as conn:
        operation = conn.execute(
            select(guided_operations_table).where(
                guided_operations_table.c.session_id == session_id,
                guided_operations_table.c.operation_id == operation_id,
            )
        ).one()

    assert operation.failure_code == "integrity_error"
    assert operation.failure_diagnostics == [diagnostic]


@pytest.mark.parametrize("error_count", [1, 40])
def test_cleanup_notes_survive_settlement_without_raw_exception_detail(composer_test_client: TestClient, error_count: int) -> None:
    client = composer_test_client
    session_id, message_id = _fork_target(client)
    service = client.app.state.session_service
    blob_service = client.app.state.blob_service
    operation_id = str(uuid4())
    blob_id = uuid4()
    secret = "storage-password-must-not-be-retained"  # secret-scan: allow-this-line
    payload = {"operation_id": operation_id, "from_message_id": str(message_id), "new_message_content": "Edited request"}
    with (
        patch.object(service, "settle_guided_fork_operation", side_effect=RuntimeError(secret)),
        patch.object(
            blob_service,
            "cleanup_blobs_for_fork",
            return_value=BlobForkCleanupResult(
                deleted_ids=(), errors=(BlobForkCleanupError(blob_id=blob_id, exc_type="OSError", detail=secret),) * error_count
            ),
        ),
    ):
        response = client.post(f"/api/sessions/{session_id}/fork", json=payload)
    replay = client.post(f"/api/sessions/{session_id}/fork", json=payload)
    assert response.status_code == replay.status_code == 500
    assert response.json() == replay.json()
    assert secret not in response.text
    with service._engine.connect() as conn:
        row = conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).one()
    assert row.failure_code == "operation_failed"
    assert len(row.failure_diagnostics) == min(1 + error_count, 32)
    if error_count == 40:
        assert row.failure_diagnostics[-1] == "DiagnosticsOmitted: 10 additional notes"
    assert secret not in "\n".join(row.failure_diagnostics)
    assert row.failure_diagnostics[0] == "ForkFailure[RuntimeError]: phase=settlement"
    assert row.failure_diagnostics[1].startswith(f"RecoveryFailed[OSError]: could not delete fork blob {blob_id} from child ")


@pytest.mark.parametrize(
    "diagnostic,expected",
    [
        ("private stored field value", "ForkFailure[AuditIntegrityError]: phase=staging"),
        (
            "fork guided correction_messages.message_id references a message outside copied slice",
            "fork guided correction_messages.message_id references a message outside copied slice",
        ),
    ],
)
def test_staging_failure_preserves_safe_phase_for_replay(composer_test_client: TestClient, diagnostic: str, expected: str) -> None:
    client = composer_test_client
    session_id, message_id = _fork_target(client)
    service = client.app.state.session_service
    operation_id = str(uuid4())
    payload = {"operation_id": operation_id, "from_message_id": str(message_id), "new_message_content": "Edited request"}
    with patch.object(service, "fork_session", side_effect=AuditIntegrityError(diagnostic)) as staging:
        response = client.post(f"/api/sessions/{session_id}/fork", json=payload)
        replay = client.post(f"/api/sessions/{session_id}/fork", json=payload)
        assert staging.call_count == 1
    assert response.status_code == replay.status_code == 500
    assert response.json() == replay.json()
    with service._engine.connect() as conn:
        row = conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).one()
    assert row.failure_code == "integrity_error"
    assert row.failure_diagnostics == [expected]


@pytest.mark.parametrize(
    "integrity_failure",
    (
        BlobIntegrityError(str(uuid4()), expected="a" * 64, actual="b" * 64),
        BlobContentMissingError(str(uuid4()), storage_path="/managed/blobs/missing"),
    ),
    ids=("hash_mismatch", "content_missing"),
)
def test_blob_cleanup_integrity_failure_propagates_instead_of_a_coded_terminal_failure(
    composer_test_client: TestClient,
    integrity_failure: BlobIntegrityError | BlobContentMissingError,
) -> None:
    """Tier-1 blob custody errors must not enter the operational BlobError arm."""
    client = composer_test_client
    session_id, from_message_id = _fork_target(client)
    service = client.app.state.session_service
    blob_service = client.app.state.blob_service
    operation_id = str(uuid4())
    payload = {
        "operation_id": operation_id,
        "from_message_id": str(from_message_id),
        "new_message_content": "Build the edited request.",
    }

    with (
        patch.object(service, "settle_guided_fork_operation", side_effect=RuntimeError("settlement exploded")),
        patch.object(blob_service, "cleanup_blobs_for_fork", side_effect=integrity_failure),
        pytest.raises(type(integrity_failure)) as exc_info,
    ):
        client.post(
            f"/api/sessions/{session_id}/fork",
            json=payload,
        )

    assert exc_info.value is integrity_failure
    replay = client.post(f"/api/sessions/{session_id}/fork", json=payload)
    assert replay.status_code == 500
    assert replay.json()["detail"]["failure_code"] == "integrity_error"

    with service._engine.connect() as conn:
        row = conn.execute(select(guided_operations_table).where(guided_operations_table.c.operation_id == operation_id)).one()
    assert len(row.failure_diagnostics) == 2
    assert row.failure_diagnostics[0] == "ForkFailure[RuntimeError]: phase=settlement"
    assert row.failure_diagnostics[1].startswith(f"RecoveryFailed[{type(integrity_failure).__name__}]: fork blob cleanup failed for child ")
    assert row.failure_diagnostics[1].endswith(f" (blob {integrity_failure.blob_id})")
    assert "/managed/blobs/missing" not in "\n".join(row.failure_diagnostics)


class _UnrenderableCleanupError(OSError):
    def __str__(self) -> str:
        raise AuditIntegrityError("cleanup exception must not be rendered")


@pytest.mark.parametrize("cleanup_error", [OSError("blob store unreachable"), _UnrenderableCleanupError()])
def test_ordinary_cleanup_failure_still_surfaces_the_primary_coded_failure(
    composer_test_client: TestClient,
    cleanup_error: OSError,
) -> None:
    """The contained arm is unchanged: a storage fault stays a note plus a log.

    Pins the boundary the integrity arm above carves out — without this, that
    arm could widen to every cleanup failure and silently convert ordinary
    compensation faults into 500 crashes.
    """
    client = composer_test_client
    session_id, from_message_id = _fork_target(client)
    service = client.app.state.session_service
    blob_service = client.app.state.blob_service

    with (
        capture_logs() as cap_logs,
        patch.object(service, "settle_guided_fork_operation", side_effect=RuntimeError("settlement exploded")),
        patch.object(blob_service, "cleanup_blobs_for_fork", side_effect=cleanup_error),
    ):
        response = client.post(
            f"/api/sessions/{session_id}/fork",
            json={
                "operation_id": str(uuid4()),
                "from_message_id": str(from_message_id),
                "new_message_content": "Build the edited request.",
            },
        )

    assert response.status_code == 500
    assert response.json()["detail"]["error_type"] == "guided_operation_terminal_failure"
    assert response.json()["detail"]["failure_code"] == "operation_failed"
    residue = [entry for entry in cap_logs if entry.get("event") == "session.fork_blob_cleanup_failed"]
    assert len(residue) == 1
    assert residue[0]["exc_class"] == type(cleanup_error).__name__
    with service._engine.connect() as conn:
        row = conn.execute(
            select(guided_operations_table).where(guided_operations_table.c.operation_id == residue[0]["operation_id"])
        ).one()
    assert row.failure_diagnostics == [
        "ForkFailure[RuntimeError]: phase=settlement",
        f"RecoveryFailed[{type(cleanup_error).__name__}]: fork blob cleanup failed for child {residue[0]['child_session_id']}",
    ]
