"""PostgreSQL proof for K056 run, call, and verification lineage."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.fixtures.landscape import leader_coordination_token, register_test_node
from tests.fixtures.stores import MockPayloadStore
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts import CallStatus, CallType, NodeType
from elspeth.contracts.call_data import RawCallPayload
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import call_verifications_table, calls_table, runs_table

pytestmark = pytest.mark.testcontainer


@pytest.mark.timeout(120)
def test_postgres_call_mode_lineage_and_verdict_are_durable_and_run_bound() -> None:
    with postgres_test_target(driver="psycopg") as postgres_url:
        db = LandscapeDB.from_url(postgres_url)
        factory = RecorderFactory(db, payload_store=MockPayloadStore())
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="source")
        factory.run_lifecycle.begin_run(
            config={},
            canonical_version="v1",
            run_id="current",
            run_mode=RunMode.VERIFY,
            replay_from_run_id="source",
        )
        for run_id in ("source", "current"):
            register_test_node(factory.data_flow, run_id, "source-node", node_type=NodeType.SOURCE, plugin_name="source")
        source_op = factory.execution.begin_operation(
            "source-node", "source_load", coordination_token=leader_coordination_token(factory, "source")
        )
        current_op = factory.execution.begin_operation(
            "source-node", "source_load", coordination_token=leader_coordination_token(factory, "current")
        )
        source_call = factory.execution.record_operation_call(
            source_op.operation_id,
            CallType.HTTP,
            CallStatus.SUCCESS,
            RawCallPayload({"url": "https://example.test/a"}),
            RawCallPayload({"status": 200}),
            coordination_token=leader_coordination_token(factory, "source"),
        )
        current_call = factory.execution.record_operation_call(
            current_op.operation_id,
            CallType.HTTP,
            CallStatus.SUCCESS,
            RawCallPayload({"url": "https://example.test/a"}),
            RawCallPayload({"status": 200}),
            coordination_token=leader_coordination_token(factory, "current"),
            source_call_id=source_call.call_id,
        )
        factory.execution.record_verification_decision(
            current_run_id="current",
            current_call_id=current_call.call_id,
            source_run_id="source",
            source_call_id=source_call.call_id,
            is_match=True,
            differences_json="{}",
        )
        with pytest.raises(AuditIntegrityError, match="source call"):
            factory.execution.record_verification_decision(
                current_run_id="current",
                current_call_id=current_call.call_id,
                source_run_id="source",
                source_call_id=current_call.call_id,
                is_match=True,
                differences_json="{}",
            )
        with db.engine.connect() as conn:
            run = conn.execute(select(runs_table.c.run_mode, runs_table.c.replay_from_run_id).where(runs_table.c.run_id == "current")).one()
            call = conn.execute(select(calls_table.c.source_call_id).where(calls_table.c.call_id == current_call.call_id)).scalar_one()
            verdict = conn.execute(
                select(call_verifications_table.c.source_call_id, call_verifications_table.c.is_match).where(
                    call_verifications_table.c.current_call_id == current_call.call_id
                )
            ).one()
        assert run == ("verify", "source")
        assert call == source_call.call_id
        assert verdict == (source_call.call_id, True)
