"""Current admission hash and actual paid PRIMARY cost/ledger source."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.continuation_cost import read_continuation_cost_snapshot_on_connection
from elspeth.web.sessions.models import quota_provider_attempts_table as physical
from elspeth.web.sessions.models import token_usage_ledger_table as usage
from tests.unit.web.sessions.test_continuation_v4_metadata_live import _v4_tool_case
from tests.unit.web.sessions.test_continuation_v4_metadata_live import operation_store as _operation_store

operation_store = _operation_store


@pytest.mark.asyncio
async def test_cost_reader_replays_actual_paid_primary_and_refuses_stale_policy_hash(operation_store, tmp_path, monkeypatch):
    async with _v4_tool_case(operation_store, tmp_path, monkeypatch) as (store, owner, running, sdk):
        root = owner.runtime.source.selected.stack
        assert owner.primary is not None and owner.primary.outcome.audit_call is not None
        audit = owner.primary.outcome.audit_call
        policy = owner.runtime.source.policy
        assert root.provider_cost_policy.admission_policy_hash == root.policy_hash == policy.policy_hash
        with store[0].connect() as conn:
            cost = read_continuation_cost_snapshot_on_connection(conn, root)
            attempt = conn.execute(select(physical).where(physical.c.attempt_id == audit.call_id)).one()
            ledger = conn.execute(select(usage).where(usage.c.entry_id == attempt.ledger_entry_id)).one()
            corrupt = root.model_copy(
                update={"provider_cost_policy": root.provider_cost_policy.model_copy(update={"admission_policy_hash": "f" * 64})}
            )
            with pytest.raises(AuditIntegrityError):
                read_continuation_cost_snapshot_on_connection(conn, corrupt)
        assert cost is not None and owner.provider_owner.cost_budget is not None
        assert cost == owner.provider_owner.cost_budget.snapshot()
        assert cost.physical_calls == len(cost.charges) == len(sdk) == 1
        assert cost.charges[0].call_id == audit.call_id == attempt.attempt_id
        assert attempt.operation_id == running.claim.operation_id and attempt.ledger_entry_id == ledger.entry_id
        expected_cost = None if audit.provider_cost is None else Decimal(str(audit.provider_cost))
        assert cost.charges[0].cost == expected_cost and cost.charges[0].response_accountable is True
