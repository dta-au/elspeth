"""Approval-owned reaction to identity authority withdrawal.

Lock order is identity then approval. No session locks are acquired here.
Future approval creation and decisions must lock both participating identities
before touching approval rows, and reject either participant when non-active.
They must preserve identity authority's admin-population-before-target order
if an administrator can participate; stable identity-ID order alone does not
replace that population lock. Non-admin participants use stable ID order.
"""

from sqlalchemy import or_, update

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
                or_(
                    approvals_table.c.approver_identity_id == event.identity_id,
                    approvals_table.c.requested_by_identity_id == event.identity_id,
                ),
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
