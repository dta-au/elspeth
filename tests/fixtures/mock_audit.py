"""Explicit mock-only audit identities for tests with no Landscape database.

Never pass these values to a real repository. Database-backed tests obtain
tokens from their seat and work items from the scheduler's actual claim.
"""

from typing import TypedDict
from unittest.mock import Mock

from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.scheduler import TokenWorkItem


class MockAuditAuthority(TypedDict):
    coordination_token: CoordinationToken
    member_token: WorkerMembershipToken
    work_item: TokenWorkItem


class MockItemAuditAuthority(TypedDict):
    member_token: WorkerMembershipToken
    work_item: TokenWorkItem


def mock_item_audit_authority(run_id: str = "test-run") -> MockItemAuditAuthority:
    """Build row-claim arguments for a repository test double."""
    authority = mock_audit_authority(run_id)
    return {"member_token": authority["member_token"], "work_item": authority["work_item"]}


def mock_audit_authority(run_id: str = "test-run") -> MockAuditAuthority:
    """Build arguments for audited clients whose recorder is a test double."""
    leader = CoordinationToken(run_id=run_id, worker_id=f"mock:{run_id}", leader_epoch=1)
    claim = Mock(spec=TokenWorkItem)
    claim.run_id = run_id
    claim.lease_owner = leader.worker_id
    return {"coordination_token": leader, "member_token": leader.membership, "work_item": claim}
