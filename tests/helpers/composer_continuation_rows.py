"""Insert lifecycle-2 Composer operations directly, for route projection tests.

Rows satisfy the epoch-79 v2 CHECK bundles. Private values carry recognisable
markers so a test can prove no projection leaks them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import Engine, insert

from elspeth.web.coordination.composer_operation_authority import _terminal_values
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operation_errors import request_cancelled_error
from elspeth.web.sessions.composer_operations import composer_operation_v2_request_hash
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.schemas import SendMessageRequest

PRIVATE_MARKERS = {
    "claim_token": "PRIVATE-CLAIM-MARKER",
    "lease_token": "PRIVATE-LEASE-MARKER",
    "fence_id": "PRIVATE-FENCE-MARKER",
}


@dataclass(frozen=True, slots=True)
class SeededV2Operation:
    operation_id: str
    checkpoint_id: str
    status_revision: int


def insert_v2_operation(
    engine: Engine,
    *,
    session_id: UUID,
    actor_user_id: str,
    state: Literal["queued", "running", "failed"],
    status_revision: int = 3,
    cancel_requested: bool = False,
) -> SeededV2Operation:
    request = SendMessageRequest(operation_id=str(uuid4()), content="Seeded continuation request")
    checkpoint_id = str(uuid4())
    with engine.begin() as conn:
        now = database_now(conn)
        created = now - timedelta(minutes=2)
        parent: dict[str, object] = {
            "session_id": str(session_id),
            "operation_id": request.operation_id,
            "kind": "compose_message",
            "status": "queued" if state == "queued" else "running" if state == "running" else "failed",
            "lifecycle_version": 2,
            "phase": {"queued": "queued", "running": "executing", "failed": "settled"}[state],
            "status_revision": status_revision,
            "current_segment_sequence": 1,
            "ownership_generation": 1,
            "checkpoint_id": checkpoint_id,
            "ingress_phase": "pending",
            "reserved_user_message_id": str(uuid4()),
            "request_hash": composer_operation_v2_request_hash(session_id=session_id, kind="compose_message", request=request),
            "actor_user_id": actor_user_id,
            "request_json": request.model_dump_json(),
            "attempt": 1,
            "created_at": created,
            "updated_at": now,
            "cancel_requested_at": now if cancel_requested else None,
        }
        if state == "running":
            parent.update(
                started_at=created,
                session_operation_id=PRIVATE_MARKERS["fence_id"],
                session_operation_epoch=7,
                session_operation_lease_token=PRIVATE_MARKERS["lease_token"],
                claim_owner_instance_id="seeded-owner",
            )
        if state == "failed":
            parent.update(_terminal_values(request_cancelled_error(request_id=None), now=now, settled_by="request_cancel"))
            parent["cancel_requested_at"] = now
        conn.execute(insert(jobs).values(**parent))
        segment: dict[str, object] = {
            "session_id": str(session_id),
            "operation_id": request.operation_id,
            "sequence": 1,
            "generation": 1,
            "state": {"queued": "queued", "running": "running", "failed": "settled"}[state],
            "enqueued_at": created,
            "next_eligible_at": created,
            "checkpoint_id": checkpoint_id,
            "next_action_json": "{}",
            "policy_sha256": "a" * 64,
        }
        if state in ("running", "failed"):
            segment.update(
                started_at=created,
                cooperative_at=created + timedelta(seconds=10),
                deadline_at=now + timedelta(minutes=3),
                session_operation_id=PRIVATE_MARKERS["fence_id"],
                session_operation_epoch=7,
                session_operation_lease_token=PRIVATE_MARKERS["lease_token"],
            )
        if state == "running":
            segment.update(
                claim_token=PRIVATE_MARKERS["claim_token"],
                claim_owner_instance_id="seeded-owner",
                claim_expires_at=now + timedelta(minutes=3),
            )
        conn.execute(insert(segments).values(**segment))
    return SeededV2Operation(request.operation_id, checkpoint_id, status_revision)


def settle_v2_operation_failed(engine: Engine, *, session_id: UUID, operation_id: str, status_revision: int) -> None:
    """Settle a seeded live v2 parent as request_cancelled, advancing its revision."""
    from sqlalchemy import update

    with engine.begin() as conn:
        now = database_now(conn)
        values = dict(_terminal_values(request_cancelled_error(request_id=None), now=now, settled_by="request_cancel"))
        conn.execute(
            update(jobs)
            .where(jobs.c.session_id == str(session_id), jobs.c.operation_id == operation_id)
            .values(**values, phase="settled", status_revision=status_revision, cancel_requested_at=now)
        )
