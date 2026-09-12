"""Approval-owned reaction to identity authority withdrawal.

Lock order is identity then approval. No session locks are acquired here.
Future approval creation and decisions must serialize on the approver identity
before touching approval rows, and reject non-active approvers.
"""

from sqlalchemy import update

from elspeth.web.coordination.identity_lifecycle import IdentityAuthorityRevoked
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import approvals_table


class RepositoryApprovalLifecycleAuthority:
    """Revoke awaiting requests atomically with the authority transition."""

    def apply(self, connection_token: str, event: IdentityAuthorityRevoked) -> None:
        connection = _resolve_mutation_connection(connection_token)
        connection.execute(
            update(approvals_table)
            .where(
                approvals_table.c.approver_identity_id == event.identity_id,
                approvals_table.c.decision.is_(None),
            )
            .values(
                decision="revoked",
                decided_at=event.occurred_at,
                revoked_by_identity_id=event.actor_identity_id,
                revocation_actor_kind=event.actor_kind,
                revocation_event_id=event.event_id,
            )
        )
