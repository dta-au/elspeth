"""Effect issuance rolls back if its remaining transaction work consumes the lease."""

from __future__ import annotations

from datetime import timedelta
from time import sleep

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Connection

from elspeth.contracts.sink_effects import SinkEffectLease
from elspeth.core.landscape.lease_deadlines import LeaseDeadlineExpiredError
from elspeth.core.landscape.schema import sink_effects_table
from tests.fixtures.landscape import expire_sink_effect_lease, leader_coordination_token, make_factory, make_landscape_db
from tests.unit.core.landscape.test_sink_effect_lifecycle import _claim, _plan, _reserved


@pytest.mark.parametrize("action", ("preparation", "acquire", "reserved_heartbeat", "in_flight_heartbeat", "takeover"))
def test_consumed_effect_deadline_rolls_back_without_retry(action: str) -> None:
    db = make_landscape_db()
    try:
        factory = make_factory(db)
        effect = _reserved(factory)
        repo = factory.execution.sink_effects
        token = leader_coordination_token(factory, effect.run_id)
        claim = _claim(factory, effect.effect_id, run_id=effect.run_id)
        if action in {"acquire", "in_flight_heartbeat", "takeover"}:
            repo.complete_plan(effect.effect_id, _plan(effect.effect_id), claim=claim, coordination_token=token)
        if action in {"in_flight_heartbeat", "takeover"}:
            claim = repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=30), coordination_token=token)
        if action == "takeover":
            expire_sink_effect_lease(db.engine, effect.effect_id)
        with db.read_only_connection() as conn:
            before = conn.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id == effect.effect_id)).one()
        writes = 0

        def delay_after_issuance(
            _conn: Connection, _cursor: object, statement: str, _parameters: object, _context: object, _executemany: bool
        ) -> None:
            nonlocal writes
            if statement.startswith("UPDATE sink_effects SET") and "lease_expires_at=" in statement:
                writes += 1
                sleep(1.1)

        def issue() -> SinkEffectLease:
            if action == "preparation":
                return repo.claim_preparation(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=1), coordination_token=token)
            if action == "acquire":
                return repo.acquire_lease(effect.effect_id, owner="worker-a", ttl=timedelta(seconds=1), coordination_token=token)
            if action == "takeover":
                return repo.takeover_expired(effect.effect_id, owner="worker-b", ttl=timedelta(seconds=1), coordination_token=token)
            return repo.heartbeat_lease(
                effect.effect_id, owner=claim.owner, generation=claim.generation, ttl=timedelta(seconds=1), coordination_token=token
            )

        event.listen(db.engine, "after_cursor_execute", delay_after_issuance)
        try:
            with pytest.raises(LeaseDeadlineExpiredError):
                issue()
        finally:
            event.remove(db.engine, "after_cursor_execute", delay_after_issuance)
        assert writes == 1, "a completion timeout must not replay effect issuance"
        with db.read_only_connection() as conn:
            after = conn.execute(select(sink_effects_table).where(sink_effects_table.c.effect_id == effect.effect_id)).one()
        assert after == before
    finally:
        db.close()
