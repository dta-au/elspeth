"""Explicit mock-only audit identities for tests with no Landscape database.

Never pass these values to a real repository. Database-backed tests obtain
tokens from their seat and work items from the scheduler's actual claim.
"""

from datetime import UTC, datetime, timedelta
from typing import TypedDict

from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.scheduler import TokenWorkItem, TokenWorkStatus


class MockAuditAuthority(TypedDict):
    coordination_token: CoordinationToken
    member_token: WorkerMembershipToken
    work_item: TokenWorkItem


class MockItemAuditAuthority(TypedDict):
    member_token: WorkerMembershipToken
    work_item: TokenWorkItem


def mock_item_audit_authority(
    run_id: str = "test-run",
    *,
    token_id: str = "token-1",
    row_id: str = "row-1",
    node_id: str | None = None,
    member_token: WorkerMembershipToken | None = None,
) -> MockItemAuditAuthority:
    """Build row-claim arguments for a repository test double."""
    if member_token is None:
        member_token = CoordinationToken(run_id=run_id, worker_id=f"mock:{run_id}", leader_epoch=1).membership
    if member_token.run_id != run_id:
        raise ValueError("mock audit membership must belong to the requested run")
    timestamp = datetime.now(UTC)
    claim = TokenWorkItem(
        work_item_id=f"mock:{run_id}:{token_id}",
        run_id=run_id,
        token_id=token_id,
        row_id=row_id,
        node_id=node_id,
        step_index=0,
        ingest_sequence=0,
        row_payload_json="{}",
        status=TokenWorkStatus.LEASED,
        attempt=1,
        available_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
        lease_owner=member_token.worker_id,
        lease_expires_at=timestamp + timedelta(minutes=5),
    )
    return {"member_token": member_token, "work_item": claim}


def mock_audit_authority(
    run_id: str = "test-run",
    *,
    token_id: str = "token-1",
    row_id: str = "row-1",
    node_id: str | None = None,
) -> MockAuditAuthority:
    """Build arguments for audited clients whose recorder is a test double."""
    leader = CoordinationToken(run_id=run_id, worker_id=f"mock:{run_id}", leader_epoch=1)
    item = mock_item_audit_authority(run_id, token_id=token_id, row_id=row_id, node_id=node_id, member_token=leader.membership)
    return {"coordination_token": leader, **item}
