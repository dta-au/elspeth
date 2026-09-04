"""Fork compensation must not demote a Tier-1 integrity failure."""

from __future__ import annotations

import asyncio
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from elspeth.contracts.blobs import BlobContentMissingError, BlobIntegrityError
from elspeth.contracts.errors import AuditIntegrityError
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


def test_ordinary_cleanup_failure_still_surfaces_the_primary_coded_failure(
    composer_test_client: TestClient,
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
        patch.object(blob_service, "cleanup_blobs_for_fork", side_effect=OSError("blob store unreachable")),
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
    assert residue[0]["exc_class"] == "OSError"
