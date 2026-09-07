"""Membership and item-lease authority for row audit writes (ADR-030 D4)."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import update
from sqlalchemy.engine import Connection

from elspeth.contracts.coordination import WorkerMembershipToken
from elspeth.contracts.errors import AuditIntegrityError, SchedulerLeaseLostError
from elspeth.contracts.scheduler import TokenWorkItem, TokenWorkStatus
from elspeth.core.landscape.database import Tier1Engine
from elspeth.core.landscape.run_coordination_repository import fenced_member_transaction
from elspeth.core.landscape.schema import token_work_items_table


@contextmanager
def fenced_item_transaction(
    engine: Tier1Engine,
    *,
    member_token: WorkerMembershipToken,
    work_item: TokenWorkItem,
    verb: str,
) -> Iterator[Connection]:
    """Lock active membership, then the exact claimed attempt, before audit SQL.

    The verify-UPDATE holds the item row until the audit transaction commits.
    Recovery rotates ``work_item_id`` and increments ``attempt``; a heartbeat
    only extends the deadline, so a heartbeat cannot invalidate the claim value
    being used by an in-flight plugin. Expiry alone does not revoke an item:
    the recovery CAS transfers its authority, as it does for scheduler writes.
    """
    if not isinstance(member_token, WorkerMembershipToken):
        raise TypeError("Item writes require a WorkerMembershipToken")
    if not isinstance(work_item, TokenWorkItem):
        raise TypeError("Item writes require the claimed TokenWorkItem")
    if work_item.run_id != member_token.run_id or work_item.lease_owner != member_token.worker_id:
        raise AuditIntegrityError("Item authority must belong to the admitted worker and run")
    if work_item.status is not TokenWorkStatus.LEASED:
        raise AuditIntegrityError("Item authority must be a claimed LEASED work item")

    with fenced_member_transaction(engine, member_token=member_token, verb=verb) as conn:
        result = conn.execute(
            update(token_work_items_table)
            .where(
                token_work_items_table.c.run_id == member_token.run_id,
                token_work_items_table.c.work_item_id == work_item.work_item_id,
                token_work_items_table.c.attempt == work_item.attempt,
                token_work_items_table.c.token_id == work_item.token_id,
                token_work_items_table.c.row_id == work_item.row_id,
                token_work_items_table.c.lease_owner == member_token.worker_id,
                token_work_items_table.c.status == TokenWorkStatus.LEASED.value,
            )
            .values(status=TokenWorkStatus.LEASED.value)
        )
        if result.rowcount != 1:
            raise SchedulerLeaseLostError(
                work_item_id=work_item.work_item_id,
                lease_owner=member_token.worker_id,
                run_id=member_token.run_id,
            )
        yield conn
