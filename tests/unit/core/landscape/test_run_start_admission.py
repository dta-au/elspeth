"""Permit-bound run creation survives retry without recycling authority."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.run_start import RunStartPermitBinding
from elspeth.core.landscape.run_start_admission import RunStartAdmissionRepository, RunStartAdmissionState
from elspeth.core.landscape.schema import calls_table, nodes_table, run_coordination_events_table, runs_table
from tests.fixtures.landscape import leader_token_for, make_factory, make_landscape_db


def test_exact_permit_retry_does_not_mint_another_leader() -> None:
    with make_landscape_db() as db:
        factory = make_factory(db)
        binding = RunStartPermitBinding("permit-run", "permit-1", 1, "a" * 64)
        first = factory.run_lifecycle.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding)
        with db.engine.connect() as conn:
            before = conn.execute(select(run_coordination_events_table)).all()
        second = factory.run_lifecycle.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding)
        assert second.run_id == first.run_id
        assert second == factory.run_lifecycle.get_run(first.run_id)
        assert RunStartAdmissionRepository(db).observe(binding).state is RunStartAdmissionState.PREPARED
        with db.engine.connect() as conn:
            assert conn.execute(select(run_coordination_events_table)).all() == before
            assert len(conn.execute(select(runs_table)).all()) == 1


def test_cancel_issued_permit_materializes_once_without_plugins() -> None:
    with make_landscape_db() as db:
        factory = make_factory(db)
        binding = RunStartPermitBinding("cancel-run", "cancel-permit", 1, "c" * 64)
        first = factory.run_lifecycle.materialize_cancelled_permit(
            binding,
            {},
            "v1",
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        assert first.status is RunStatus.INTERRUPTED
        with db.engine.connect() as conn:
            before = conn.execute(select(run_coordination_events_table)).all()
        second = factory.run_lifecycle.materialize_cancelled_permit(
            binding,
            {},
            "v1",
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        assert first == second
        with db.engine.connect() as conn:
            assert conn.execute(select(run_coordination_events_table)).all() == before
            assert conn.execute(select(nodes_table)).all() == []
            assert conn.execute(select(calls_table)).all() == []


def test_successor_cancels_prepared_baseline_under_new_epoch() -> None:
    with make_landscape_db() as db:
        factory = make_factory(db)
        binding = RunStartPermitBinding("takeover-run", "takeover-permit", 1, "d" * 64)
        factory.run_lifecycle.begin_run(
            {},
            "v1",
            run_id=binding.run_id,
            run_start_permit=binding,
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        original = leader_token_for(db, binding.run_id)
        factory.run_coordination.release_seat(token=original)
        run = factory.run_lifecycle.materialize_cancelled_permit(
            binding,
            {},
            "v1",
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        assert run.status is RunStatus.INTERRUPTED
        with db.engine.connect() as conn:
            epochs = conn.execute(select(run_coordination_events_table.c.leader_epoch)).scalars().all()
        assert max(epoch for epoch in epochs if epoch is not None) > original.leader_epoch


def test_mismatched_permit_and_configuration_refuse_without_mutation() -> None:
    with make_landscape_db() as db:
        factory = make_factory(db)
        binding = RunStartPermitBinding("permit-run", "permit-1", 1, "a" * 64)
        factory.run_lifecycle.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding)
        with db.engine.connect() as conn:
            before = conn.execute(select(run_coordination_events_table)).all()
        with pytest.raises(AuditIntegrityError):
            factory.run_lifecycle.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=replace(binding, subject_hash="b" * 64))
        with pytest.raises(AuditIntegrityError):
            factory.run_lifecycle.begin_run({"changed": True}, "v1", run_id=binding.run_id, run_start_permit=binding)
        with db.engine.connect() as conn:
            assert conn.execute(select(run_coordination_events_table)).all() == before


def test_prepared_reset_removes_only_pure_setup_and_refuses_advanced_checkpoint() -> None:
    from datetime import UTC, datetime

    from elspeth.core.landscape.schema import checkpoints_table
    from tests.fixtures.landscape import register_test_node

    with make_landscape_db() as db:
        factory = make_factory(db)
        binding = RunStartPermitBinding("reset-run", "reset-permit", 1, "e" * 64)
        factory.run_lifecycle.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding)
        token = leader_token_for(db, binding.run_id)
        register_test_node(factory.data_flow, binding.run_id, "pure-node")
        admission = RunStartAdmissionRepository(db)
        admission.reset_prepared_initialization(binding, coordination_token=token)
        with db.engine.connect() as conn:
            assert conn.execute(select(nodes_table)).all() == []
        register_test_node(factory.data_flow, binding.run_id, "pure-node")
        with db.write_connection() as conn:
            conn.execute(
                checkpoints_table.insert().values(
                    checkpoint_id="advanced",
                    run_id=binding.run_id,
                    sequence_number=1,
                    created_at=datetime.now(UTC),
                    upstream_topology_hash="f" * 64,
                    format_version=5,
                )
            )
        with db.engine.connect() as conn:
            nodes_before = conn.execute(select(nodes_table)).all()
            checkpoints_before = conn.execute(select(checkpoints_table)).all()
        with pytest.raises(AuditIntegrityError, match="execution checkpoint"):
            admission.reset_prepared_initialization(binding, coordination_token=token)
        with db.engine.connect() as conn:
            assert conn.execute(select(nodes_table)).all() == nodes_before
            assert conn.execute(select(checkpoints_table)).all() == checkpoints_before


def test_changed_policy_and_source_schema_refuse_exact_permit_retry() -> None:
    from tests.unit.core.landscape.test_run_lifecycle_repository import _web_policy_evidence

    with make_landscape_db() as db:
        factory = make_factory(db)
        binding = RunStartPermitBinding("policy-run", "policy-permit", 1, "f" * 64)
        policy = _web_policy_evidence()
        factory.run_lifecycle.begin_run(
            {}, "v1", run_id=binding.run_id, run_start_permit=binding, source_schema_json="original", web_plugin_policy_evidence=policy
        )
        with db.engine.connect() as conn:
            before = conn.execute(select(run_coordination_events_table)).all()
        with pytest.raises(AuditIntegrityError, match="policy evidence"):
            factory.run_lifecycle.begin_run(
                {},
                "v1",
                run_id=binding.run_id,
                run_start_permit=binding,
                source_schema_json="original",
                web_plugin_policy_evidence=replace(policy, decision_codes=("changed",)),
            )
        with pytest.raises(AuditIntegrityError, match="source schema"):
            factory.run_lifecycle.begin_run(
                {}, "v1", run_id=binding.run_id, run_start_permit=binding, source_schema_json="changed", web_plugin_policy_evidence=policy
            )
        with db.engine.connect() as conn:
            assert conn.execute(select(run_coordination_events_table)).all() == before


@pytest.mark.parametrize("verb", ["run-start-reset-prepared", "run-start-effects"])
def test_stale_leader_cannot_change_start_admission_or_setup(verb: str) -> None:
    import json

    from sqlalchemy import update

    from elspeth.contracts.errors import RunLeadershipLostError
    from elspeth.core.landscape.schema import metadata, run_coordination_table
    from tests.fixtures.landscape import register_test_node

    with make_landscape_db() as db:
        factory = make_factory(db)
        binding = RunStartPermitBinding("stale-admission", "stale-permit", 1, "a" * 64)
        factory.run_lifecycle.begin_run({}, "v1", run_id=binding.run_id, run_start_permit=binding)
        stale = leader_token_for(db, binding.run_id)
        register_test_node(factory.data_flow, binding.run_id, "preserved-setup")
        # Deterministic takeover seam, matching the shared stale-token suite.
        with db.engine.begin() as connection:
            connection.execute(
                update(run_coordination_table)
                .where(run_coordination_table.c.run_id == binding.run_id)
                .values(leader_epoch=stale.leader_epoch + 1)
            )
        with db.engine.connect() as connection:
            before = {
                table.name: connection.execute(select(table)).all()
                for table in metadata.sorted_tables
                if table is not run_coordination_events_table
            }
            prior_seqs = set(connection.execute(select(run_coordination_events_table.c.seq)).scalars())
        admission = RunStartAdmissionRepository(db)
        with pytest.raises(RunLeadershipLostError) as raised:
            if verb == "run-start-reset-prepared":
                admission.reset_prepared_initialization(binding, coordination_token=stale)
            else:
                admission.mark_executing(binding, coordination_token=stale)
        assert raised.value.verb == verb
        with db.engine.connect() as connection:
            for table in metadata.sorted_tables:
                if table is not run_coordination_events_table:
                    assert connection.execute(select(table)).all() == before[table.name], table.name
            additions = [row for row in connection.execute(select(run_coordination_events_table)).all() if row.seq not in prior_seqs]
        assert len(additions) == 1
        assert additions[0].event_type == "fence_refusal"
        assert json.loads(additions[0].context_json)["verb"] == verb
