"""Refuse incomplete or contradictory durable lifecycle and resume evidence."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from elspeth.contracts import AggregationResultMember, BatchStatus, NodeStateStatus, NodeType, OutputMode, TriggerType
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.engine import CoalesceParentCompletion
from elspeth.contracts.enums import AggregationMemberAction, CollectorGroupFailureReason, FrameKind, RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.node_state_context import AggregationFlushContext
from elspeth.contracts.run_start import RunStartPermitBinding
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.collector_group_failure_holds import collector_group_failure_hold_error
from elspeth.core.landscape.data_flow.outcomes import record_buffered_outcome_guarded
from elspeth.core.landscape.errors import LandscapePostCommitError, LandscapeRecordError, LandscapeRecordNotFoundError
from elspeth.core.landscape.execution.batches import add_batch_member_guarded
from elspeth.core.landscape.schema import (
    aggregation_result_members_table,
    aggregation_result_outputs_table,
    aggregation_results_table,
    batch_members_table,
    batches_table,
    coalesce_effect_members_table,
    coalesce_effects_table,
    collector_group_failures_table,
    node_states_table,
    run_sources_table,
    run_web_plugin_policy_table,
    runs_table,
    token_outcomes_table,
    tokens_table,
)
from tests.fixtures.group_lineage import ensure_fork_group_record
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run, register_test_node
from tests.unit.core.landscape.ci_coverage_lifecycle_node_faults import (
    node_fault_setup as node_fault_setup,
)
from tests.unit.core.landscape.ci_coverage_lifecycle_node_faults import (
    test_atomic_insert_refuses_broken_database_readback as test_atomic_insert_refuses_broken_database_readback,
)
from tests.unit.core.landscape.ci_coverage_lifecycle_node_faults import (
    test_bulk_begin_refuses_database_rejection as test_bulk_begin_refuses_database_rejection,
)
from tests.unit.core.landscape.ci_coverage_lifecycle_node_faults import (
    test_bulk_completion_refuses_missing_state_and_rolls_back_existing_state as test_bulk_completion_refuses_missing_state_and_rolls_back_existing_state,
)
from tests.unit.core.landscape.ci_coverage_lifecycle_node_faults import (
    test_completion_does_not_write_a_foreign_run_state as test_completion_does_not_write_a_foreign_run_state,
)
from tests.unit.core.landscape.ci_coverage_lifecycle_node_faults import (
    test_completion_refuses_broken_database_readback as test_completion_refuses_broken_database_readback,
)
from tests.unit.core.landscape.ci_coverage_lifecycle_node_faults import (
    test_quarantined_completed_state_records_noncanonical_input_hash as test_quarantined_completed_state_records_noncanonical_input_hash,
)
from tests.unit.core.landscape.ci_coverage_lifecycle_node_faults import (
    test_source_reconciliation_refuses_different_start_and_completion_times as test_source_reconciliation_refuses_different_start_and_completion_times,
)
from tests.unit.core.landscape.ci_coverage_lifecycle_node_faults import (
    test_source_reconciliation_refuses_partial_witness_insert as test_source_reconciliation_refuses_partial_witness_insert,
)


@pytest.fixture
def setup() -> Iterator[RecorderSetup]:
    value = make_recorder_with_run(run_id="run-1", source_node_id="source-0")
    try:
        yield value
    finally:
        value.db.close()


def _source(setup: RecorderSetup, **changes: Any) -> None:
    values = {
        "source_node_id": "source-0",
        "source_name": "input",
        "plugin_name": "csv",
        "config_hash": "a" * 64,
        "lifecycle_state": "loading",
        "source_schema_json": "{}",
        "schema_contract": SchemaContract(mode="OBSERVED", fields=(), locked=True),
        "coordination_token": setup.coordination_token,
    }
    values.update(changes)
    setup.run_lifecycle.record_run_source(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"auth_provider_type": "unknown", "initiated_by_user_id": "user"}, "auth_provider_type"),
        ({"openrouter_catalog_source": "unknown"}, "openrouter_catalog_source"),
        ({"run_mode": "live"}, "must be a RunMode"),
        ({"run_mode": RunMode.REPLAY}, "require one"),
        ({"replay_from_run_id": "run-1"}, "live runs have no replay source"),
        ({"run_id": "new-run", "run_mode": RunMode.REPLAY, "replay_from_run_id": "new-run"}, "cite itself"),
    ],
)
def test_begin_run_refuses_invalid_identity(setup: RecorderSetup, changes: dict[str, object], message: str) -> None:
    values = {"config": {}, "canonical_version": "v1", "run_id": "invalid-run"}
    values.update(changes)
    with pytest.raises(AuditIntegrityError, match=message):
        setup.run_lifecycle.begin_run(**values)
    assert setup.run_lifecycle.get_run("invalid-run") is None


def test_authenticated_run_attribution_round_trips(setup: RecorderSetup) -> None:
    setup.run_lifecycle.begin_run(
        config={}, canonical_version="v1", run_id="attributed", initiated_by_user_id="user", auth_provider_type="local"
    )
    assert setup.run_lifecycle.get_run_attribution("attributed") == ("user", "local")
    assert setup.run_lifecycle.get_run_attribution(setup.run_id) is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_json": None}, "no source schema"),
        ({"schema_contract_hash": None}, "no contract hash"),
        ({"schema_contract_hash": "f" * 32}, "contract hash mismatch"),
    ],
)
def test_resume_refuses_incomplete_source_evidence(setup: RecorderSetup, changes: dict[str, object], message: str) -> None:
    _source(setup)
    assert set(setup.run_lifecycle.get_run_source_resume_records(setup.run_id)) == {"source-0"}
    with setup.db.write_connection() as conn:
        assert conn.execute(run_sources_table.update().values(**changes)).rowcount == 1
    with pytest.raises(AuditIntegrityError, match=message):
        setup.run_lifecycle.get_run_source_resume_records(setup.run_id)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"schema_contract_hash": None}, "no hash"),
        ({"schema_contract_hash": "f" * 32}, "hash mismatch"),
    ],
)
def test_contract_backfill_refuses_existing_corruption(setup: RecorderSetup, changes: dict[str, object], message: str) -> None:
    _source(setup)
    with setup.db.write_connection() as conn:
        conn.execute(run_sources_table.update().values(**changes))
    with pytest.raises(AuditIntegrityError, match=message):
        setup.run_lifecycle.update_run_source_contract(
            source_node_id="source-0",
            schema_contract=SchemaContract(mode="OBSERVED", fields=(), locked=True),
            coordination_token=setup.coordination_token,
        )


def test_contract_backfill_refuses_missing_source_record(setup: RecorderSetup) -> None:
    with pytest.raises(AuditIntegrityError, match="does not exist"):
        setup.run_lifecycle.update_run_source_contract(
            source_node_id="source-0",
            schema_contract=SchemaContract(mode="OBSERVED", fields=(), locked=True),
            coordination_token=setup.coordination_token,
        )


@pytest.mark.parametrize("source_mapping", [None, {"original": "normalized"}])
def test_single_source_header_resume_uses_source_mapping_or_legacy_fallback(
    setup: RecorderSetup, source_mapping: dict[str, str] | None
) -> None:
    setup.run_lifecycle.record_source_field_resolution({"legacy": "normalized"}, "v1", coordination_token=setup.coordination_token)
    assert setup.run_lifecycle.get_resume_field_resolution(setup.run_id) == {"legacy": "normalized"}
    _source(setup, field_resolution_mapping=source_mapping)
    assert setup.run_lifecycle.get_resume_field_resolution(setup.run_id) == (source_mapping or {"legacy": "normalized"})


@pytest.mark.parametrize("reader", ["manifest", "fields"])
def test_resume_reader_refuses_missing_run(setup: RecorderSetup, reader: str) -> None:
    with pytest.raises(AuditIntegrityError, match="not found"):
        if reader == "manifest":
            setup.run_lifecycle.get_runtime_val_manifest("absent")
        else:
            setup.run_lifecycle.get_source_field_resolutions("absent")


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("runtime_val_manifest_json", None, "no runtime VAL manifest"),
        ("runtime_val_manifest_json", b"not-text", "expected str"),
        ("source_schema_json", b"not-text", "expected str"),
    ],
)
def test_run_resume_refuses_invalid_stored_evidence(setup: RecorderSetup, column: str, value: object, message: str) -> None:
    with setup.db.write_connection() as conn:
        assert conn.execute(runs_table.update().values({column: value})).rowcount == 1
    with pytest.raises(AuditIntegrityError, match=message):
        if column == "runtime_val_manifest_json":
            setup.run_lifecycle.get_runtime_val_manifest(setup.run_id)
        else:
            setup.run_lifecycle.get_source_schema(setup.run_id)


@pytest.mark.parametrize("operation", ["INSERT", "UPDATE"])
@pytest.mark.parametrize("fault", ["ignore", "reject"])
def test_source_metadata_write_refuses_silent_or_explicit_database_rejection(setup: RecorderSetup, operation: str, fault: str) -> None:
    if operation == "UPDATE":
        _source(setup)
    action = "SELECT RAISE(IGNORE)" if fault == "ignore" else "SELECT RAISE(ABORT, 'injected source rejection')"
    with setup.db.write_connection() as conn:
        conn.exec_driver_sql(f"CREATE TRIGGER refuse_source BEFORE {operation} ON run_sources BEGIN {action}; END")
    expected = LandscapeRecordNotFoundError if operation == "UPDATE" and fault == "ignore" else LandscapeRecordError
    with pytest.raises(expected):
        _source(setup, source_name="changed")
    records = setup.run_lifecycle.get_run_source_lifecycle_records(setup.run_id)
    assert len(records) == (1 if operation == "UPDATE" else 0)
    if records:
        assert records["source-0"].source_name == "input"


def test_contract_backfill_refuses_a_silent_conditional_update_failure(setup: RecorderSetup) -> None:
    _source(setup, schema_contract=None)
    with setup.db.write_connection() as conn:
        conn.exec_driver_sql("CREATE TRIGGER refuse_contract BEFORE UPDATE ON run_sources BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(AuditIntegrityError, match="conditional update affected no rows"):
        setup.run_lifecycle.update_run_source_contract(
            source_node_id="source-0",
            schema_contract=SchemaContract(mode="OBSERVED", fields=(), locked=True),
            coordination_token=setup.coordination_token,
        )


@pytest.mark.parametrize("pending", [False, True])
def test_export_compare_and_set_refuses_silently_rejected_update(setup: RecorderSetup, pending: bool) -> None:
    from elspeth.contracts import ExportStatus

    setup.run_lifecycle.set_export_status(ExportStatus.PENDING, coordination_token=setup.coordination_token)
    with setup.db.write_connection() as conn:
        conn.exec_driver_sql("CREATE TRIGGER refuse_export BEFORE UPDATE ON runs BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(AuditIntegrityError, match="unsupported status"):
        if pending:
            setup.run_lifecycle.set_export_pending_unless_completed(coordination_token=setup.coordination_token)
        else:
            setup.run_lifecycle.set_export_failed_unless_completed(error="failure", coordination_token=setup.coordination_token)


def test_export_failure_requires_plain_error_string(setup: RecorderSetup) -> None:
    with pytest.raises(TypeError, match="exact string"):
        setup.run_lifecycle.set_export_failed_unless_completed(error=1, coordination_token=setup.coordination_token)


@pytest.mark.parametrize("case", ["legacy", "wrong-run", "attribution", "remove-policy", "add-policy", "corrupt-policy"])
def test_permit_retry_refuses_changed_authority_or_baseline(setup: RecorderSetup, case: str) -> None:
    from tests.unit.core.landscape.test_run_lifecycle_repository import _web_policy_evidence

    binding = RunStartPermitBinding("permit-run", "permit-1", 1, "a" * 64)
    policy = _web_policy_evidence()
    values = {"config": {}, "canonical_version": "v1", "run_id": binding.run_id, "run_start_permit": binding}
    if case == "legacy":
        values.update(run_id=setup.run_id, run_start_permit=RunStartPermitBinding(setup.run_id, "legacy", 1, "b" * 64))
        message = "legacy run has no permit-bound baseline"
    elif case == "wrong-run":
        values["run_id"] = "different-run"
        message = "exact run UUID"
    else:
        if case in {"remove-policy", "corrupt-policy"}:
            values["web_plugin_policy_evidence"] = policy
        setup.run_lifecycle.begin_run(**values)
        original = setup.run_lifecycle.get_run(binding.run_id)
        if case == "attribution":
            values.update(initiated_by_user_id="user", auth_provider_type="local")
            message = "changed run attribution"
        elif case == "remove-policy":
            values["web_plugin_policy_evidence"] = None
            message = "removed web plugin policy evidence"
        elif case == "add-policy":
            values["web_plugin_policy_evidence"] = policy
            message = "added web plugin policy evidence"
        else:
            with setup.db.write_connection() as conn:
                conn.execute(run_web_plugin_policy_table.update().values(admission_decision_json="{}", admission_decision_hash="f" * 64))
            message = "corrupt admission evidence"
    with pytest.raises(AuditIntegrityError, match=message):
        setup.run_lifecycle.begin_run(**values)
    if case not in {"legacy", "wrong-run"}:
        assert setup.run_lifecycle.get_run(binding.run_id) == original


def test_execution_lookup_does_not_resolve_foreign_or_missing_identity(setup: RecorderSetup) -> None:
    row, token = setup.data_flow.create_row_with_token(
        "source-0", 0, {"value": 1}, source_row_index=0, ingest_sequence=0, coordination_token=setup.coordination_token
    )
    assert setup.execution.row_id_for_token(run_id=setup.run_id, token_id=token.token_id) == row.row_id
    assert setup.execution.row_id_for_token(run_id="foreign", token_id=token.token_id) is None
    assert setup.execution.row_id_for_token(run_id=setup.run_id, token_id="absent") is None
    assert setup.execution.get_group_record(run_id=setup.run_id, group_id="absent") is None


def test_aggregation_receipt_normalizes_a_rejected_atomic_write(setup: RecorderSetup) -> None:
    values = _batch(setup)
    with setup.db.write_connection() as conn:
        conn.exec_driver_sql(
            "CREATE TRIGGER reject_receipt BEFORE INSERT ON aggregation_results BEGIN SELECT RAISE(ABORT, 'rejected'); END"
        )
    with pytest.raises(LandscapeRecordError, match="complete_aggregation_result failed"):
        setup.execution.complete_aggregation_result(**values)
    assert setup.execution.get_batch("batch-1").status is BatchStatus.EXECUTING
    assert setup.execution.get_node_state("state-1").status is NodeStateStatus.OPEN


def test_aggregation_receipt_recovers_a_lost_commit_acknowledgement(setup: RecorderSetup, monkeypatch: pytest.MonkeyPatch) -> None:
    import elspeth.core.landscape.execution_repository as module

    values = _batch(setup)
    real_transaction = module.fenced_leader_transaction

    @contextmanager
    def commit_then_lose_acknowledgement(*args: Any, **kwargs: Any) -> Iterator[Any]:
        with real_transaction(*args, **kwargs) as conn:
            yield conn
        raise OperationalError("COMMIT", {}, RuntimeError("lost acknowledgement"))

    with monkeypatch.context() as patch:
        patch.setattr(module, "fenced_leader_transaction", commit_then_lose_acknowledgement)
        receipt = setup.execution.complete_aggregation_result(**values)
    assert receipt.batch_id == "batch-1"
    assert setup.execution.get_batch("batch-1").status is BatchStatus.COMPLETED
    assert setup.execution.complete_aggregation_result(**values) == receipt


def test_aggregation_receipt_reports_committed_but_unverifiable_readback(setup: RecorderSetup, monkeypatch: pytest.MonkeyPatch) -> None:
    values = _batch(setup)

    @contextmanager
    def unavailable_read(_db: object) -> Iterator[Any]:
        raise OperationalError("SELECT", {}, RuntimeError("read transport unavailable"))
        yield

    with monkeypatch.context() as patch:
        patch.setattr(type(setup.db), "read_only_connection", unavailable_read)
        with pytest.raises(LandscapePostCommitError, match="failed exact post-commit readback"):
            setup.execution.complete_aggregation_result(**values)
    assert setup.execution.get_batch("batch-1").status is BatchStatus.COMPLETED
    assert setup.execution.get_node_state("state-1").status is NodeStateStatus.COMPLETED
    assert setup.execution.complete_aggregation_result(**values).batch_id == "batch-1"


def _batch(setup: RecorderSetup) -> dict[str, Any]:
    register_test_node(setup.data_flow, setup.run_id, "agg-1", node_type=NodeType.AGGREGATION, plugin_name="aggregator")
    for ordinal in range(2):
        setup.data_flow.create_row_with_token(
            setup.source_node_id,
            ordinal,
            {"value": ordinal},
            row_id=f"row-{ordinal}",
            token_id=f"tok-{ordinal}",
            source_row_index=ordinal,
            ingest_sequence=ordinal,
            coordination_token=setup.coordination_token,
        )
    state = setup.execution.begin_node_state(
        "tok-0", "agg-1", 1, {"value": 0}, state_id="state-1", member_token=setup.coordination_token.membership
    )
    setup.execution.create_batch("agg-1", batch_id="batch-1", coordination_token=setup.coordination_token)
    with setup.db.write_connection() as conn:
        for ordinal in range(2):
            add_batch_member_guarded(conn, batch_id="batch-1", token_id=f"tok-{ordinal}", ordinal=ordinal, expected_run_id=setup.run_id)
            record_buffered_outcome_guarded(
                conn, run_id=setup.run_id, token_id=f"tok-{ordinal}", batch_id="batch-1", recorded_at=datetime.now(UTC)
            )
    setup.execution.update_batch_status(
        "batch-1", BatchStatus.EXECUTING, state_id=state.state_id, coordination_token=setup.coordination_token
    )
    output = PipelineRow({"total": 1}, SchemaContract(mode="OBSERVED", fields=(), locked=True))
    return {
        "batch_id": "batch-1",
        "aggregation_node_id": "agg-1",
        "state_id": state.state_id,
        "trigger_type": TriggerType.END_OF_SOURCE,
        "output_mode": OutputMode.TRANSFORM,
        "output_rows": (output,),
        "output_shape": "single",
        "output_hash": stable_hash(output.to_dict()),
        "members": tuple(
            AggregationResultMember(TokenRef(token_id=f"tok-{ordinal}", run_id=setup.run_id), AggregationMemberAction.CONSUME_BATCH, None)
            for ordinal in range(2)
        ),
        "expansion_parent_token_id": "tok-0",
        "duration_ms": 1.0,
        "success_reason": None,
        "context_after": AggregationFlushContext(
            trigger_type=TriggerType.END_OF_SOURCE.value,
            buffer_size=2,
            batch_id="batch-1",
            flush_index=1,
            rows_seen_total=2,
            row_start=1,
            row_end=2,
            is_end_of_source=True,
        ),
        "coordination_token": setup.coordination_token,
    }


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("mode", "nominal OutputMode"),
        ("no-members", "ordered members"),
        ("foreign", "cross run identity"),
        ("duplicate", "duplicate members"),
        ("quarantine", "divergent quarantine"),
        ("error-hash", "forbids error_hash"),
        ("shape", "single or multi"),
        ("single-many", "exactly one output"),
        ("transform-action", "illegal member action"),
        ("parent", "first consumed"),
        ("empty-shape", "empty shape and no expansion parent"),
        ("empty-action", "illegal member action"),
        ("passthrough-shape", "one output per member"),
        ("passthrough-action", "illegal member action"),
        ("passthrough-empty-shape", "empty shape and no expansion parent"),
        ("passthrough-empty-action", "illegal member action"),
        ("hash", "output hash disagrees"),
        ("state", "wrong-node state"),
        ("batch", "wrong-node batch"),
        ("order", "ordered batch membership"),
        ("closed", "OPEN state"),
    ],
)
def test_aggregation_receipt_refuses_contradictory_claims(setup: RecorderSetup, case: str, message: str) -> None:
    values = _batch(setup)
    members = values["members"]
    if case == "mode":
        values["output_mode"] = "transform"
    elif case == "no-members":
        values["members"] = ()
    elif case == "foreign":
        values["members"] = (AggregationResultMember(TokenRef("tok-0", "foreign"), AggregationMemberAction.CONSUME_BATCH, None),)
    elif case == "duplicate":
        values["members"] = (members[0], members[0])
    elif case in {"quarantine", "error-hash", "transform-action"}:
        action = AggregationMemberAction.QUARANTINE if case == "quarantine" else AggregationMemberAction.CONTINUE_PASSTHROUGH
        values["members"] = (AggregationResultMember(members[0].member_ref, action, "bad" if case != "transform-action" else None),)
    elif case == "shape":
        values["output_shape"] = "empty"
    elif case == "single-many":
        values["output_rows"] *= 2
    elif case == "parent":
        values["expansion_parent_token_id"] = "tok-1"
    elif case.startswith("empty") or case.startswith("passthrough-empty"):
        values.update(output_rows=(), output_shape="empty", expansion_parent_token_id=None, output_hash=stable_hash([]))
        if "passthrough" in case:
            values["output_mode"] = OutputMode.PASSTHROUGH
        if case.endswith("shape"):
            values["output_shape"] = "single"
    elif case.startswith("passthrough"):
        values.update(output_mode=OutputMode.PASSTHROUGH, expansion_parent_token_id=None)
        if case == "passthrough-action":
            values.update(output_shape="multi", output_rows=values["output_rows"] * 2)
    elif case == "hash":
        values["output_hash"] = "wrong"
    elif case == "state":
        values["state_id"] = "absent"
    elif case == "batch":
        values["batch_id"] = "absent"
    elif case == "order":
        values["members"] = tuple(reversed(members))
        values["expansion_parent_token_id"] = "tok-1"
    elif case == "closed":
        setup.execution.complete_node_state(
            "state-1", NodeStateStatus.COMPLETED, duration_ms=1, output_data={"total": 1}, member_token=setup.coordination_token.membership
        )
    with pytest.raises(AuditIntegrityError, match=message):
        setup.execution.complete_aggregation_result(**values)
    with setup.db.read_only_connection() as conn:
        assert conn.execute(select(aggregation_results_table)).all() == []
    assert setup.execution.get_batch("batch-1").status is BatchStatus.EXECUTING


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("claimed", "already has expansion claim"),
        ("completion", "divergent completion identity"),
        ("membership", "invalid batch membership"),
        ("blocked", "BLOCKED member set"),
        ("disposition", "ordered member actions"),
        ("quarantine", "divergent quarantine action"),
        ("error-hash", "non-quarantine action"),
        ("transform-action", "illegal transform member actions"),
        ("parent", "divergent expansion parent"),
        ("empty-transform", "illegal empty transform member actions"),
        ("passthrough", "illegal passthrough member actions"),
        ("empty-passthrough", "illegal empty passthrough member actions"),
        ("terminal", "terminal outcomes"),
        ("buffered", "live BUFFERED outcome"),
        ("outputs", "invalid ordered output references"),
    ],
)
def test_restore_refuses_corrupt_committed_aggregation_receipt(setup: RecorderSetup, case: str, message: str) -> None:
    values = _batch(setup)
    setup.execution.complete_aggregation_result(**values)
    restore = setup.factory.barrier_restore
    assert (
        len(
            restore.list_committed_aggregation_output_receipts(
                setup.run_id, aggregation_node_id="agg-1", blocked_token_ids=("tok-0", "tok-1")
            )
        )
        == 1
    )
    blocked = ("tok-0", "tok-1")
    with setup.db.write_connection() as conn:
        if case == "claimed":
            conn.execute(batches_table.update().values(expansion_group_id="claimed"))
        elif case == "completion":
            conn.execute(node_states_table.update().values(output_hash="f" * 64))
        elif case == "membership":
            conn.execute(batch_members_table.update().where(batch_members_table.c.ordinal == 1).values(ordinal=3))
        elif case == "blocked":
            blocked = ("tok-0",)
        elif case == "disposition":
            conn.execute(aggregation_result_members_table.delete().where(aggregation_result_members_table.c.ordinal == 1))
        elif case == "quarantine":
            conn.execute(aggregation_result_members_table.update().values(action="quarantine", error_hash="f" * 16))
        elif case == "error-hash":
            # A damaged database can predate the CHECK that forbids this pair;
            # the restore reader must independently refuse that audit evidence.
            conn.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            conn.execute(aggregation_result_members_table.update().values(error_hash="f" * 16))
            conn.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        elif case == "transform-action":
            conn.execute(aggregation_result_members_table.update().values(action="continue_passthrough"))
        elif case == "parent":
            conn.execute(aggregation_results_table.update().values(expansion_parent_token_id="tok-1"))
        elif case in {"empty-transform", "passthrough", "empty-passthrough"}:
            changes = {"output_shape": "empty" if case.startswith("empty") else "multi", "expansion_parent_token_id": None}
            if "passthrough" in case:
                changes["output_mode"] = "passthrough"
            conn.execute(aggregation_results_table.update().values(**changes))
        elif case == "terminal":
            conn.execute(
                token_outcomes_table.insert().values(
                    outcome_id="terminal",
                    run_id=setup.run_id,
                    token_id="tok-0",
                    outcome="transient",
                    path="batch_consumed",
                    completed=1,
                    batch_id="batch-1",
                    recorded_at=datetime.now(UTC),
                )
            )
        elif case == "buffered":
            conn.execute(token_outcomes_table.delete())
        elif case == "outputs":
            conn.execute(aggregation_result_outputs_table.update().values(ordinal=3))
    with pytest.raises(AuditIntegrityError, match=message):
        restore.list_committed_aggregation_output_receipts(setup.run_id, aggregation_node_id="agg-1", blocked_token_ids=blocked)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("membership", "incomplete ordered membership"),
        ("outcomes", "lacks exact terminal parent outcomes"),
        ("outcome-kind", "divergent parent outcomes"),
        ("token", "divergent result-token identity"),
        ("hash", "divergent identity hashes"),
        ("continued", "continuation evidence"),
    ],
)
def test_coalesce_restore_refuses_damaged_commit_receipt(setup: RecorderSetup, case: str, message: str) -> None:
    register_test_node(setup.data_flow, setup.run_id, "merge-1", node_type=NodeType.COALESCE, plugin_name="coalesce")
    row, _source = setup.data_flow.create_row_with_token(
        "source-0", 0, {"value": 1}, source_row_index=0, ingest_sequence=0, coordination_token=setup.coordination_token
    )
    parents = tuple(
        setup.data_flow.create_token(
            row.row_id,
            coordination_token=setup.coordination_token,
            lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id="fork-group", member_key=branch),),
        )
        for branch in ("left", "right")
    )
    ensure_fork_group_record(setup.factory, run_id=setup.run_id, group_id="fork-group", opener_token_id=parents[0].token_id)
    refs = tuple(TokenRef(token.token_id, setup.run_id) for token in parents)
    completions = tuple(
        CoalesceParentCompletion(
            parent_ref=ref,
            state_id=setup.execution.begin_node_state(
                ref.token_id, "merge-1", 1, {"value": 1}, member_token=setup.coordination_token.membership
            ).state_id,
            duration_ms=1,
            context_after=None,
        )
        for ref in refs
    )
    merged = setup.data_flow.coalesce_tokens(
        parent_refs=refs,
        row_id=row.row_id,
        coalesce_node_id="merge-1",
        parent_state_ids=[c.state_id for c in completions],
        merged_payload={"merged": 1},
        merged_contract=SchemaContract(mode="OBSERVED", fields=(), locked=True),
        step_in_pipeline=1,
        coordination_token=setup.coordination_token,
    )
    setup.data_flow.finalize_coalesce_effect(merged=merged, parent_completions=completions, coordination_token=setup.coordination_token)
    restore = setup.factory.barrier_restore
    read_values = {
        "run_id": setup.run_id,
        "coalesce_node_id": "merge-1",
        "coalesce_name": "merge",
        "group_id": "fork-group",
        "blocked_token_ids": tuple(ref.token_id for ref in refs),
    }
    assert restore.get_committed_coalesce_residual(**read_values).result_token_id == merged.token_id
    with setup.db.write_connection() as conn:
        if case == "membership":
            conn.execute(coalesce_effect_members_table.update().where(coalesce_effect_members_table.c.ordinal == 1).values(ordinal=3))
        elif case == "outcomes":
            conn.execute(token_outcomes_table.delete().where(token_outcomes_table.c.token_id == refs[0].token_id))
        elif case == "outcome-kind":
            conn.execute(token_outcomes_table.update().values(outcome="success", path="default_flow"))
        elif case == "token":
            conn.execute(tokens_table.update().where(tokens_table.c.token_id == merged.token_id).values(token_data_ref="f" * 64))
        elif case == "hash":
            conn.execute(coalesce_effects_table.update().values(effect_hash="f" * 64))
        elif case == "continued":
            conn.execute(
                token_outcomes_table.insert().values(
                    outcome_id="continued",
                    run_id=setup.run_id,
                    token_id=merged.token_id,
                    outcome="success",
                    path="default_flow",
                    completed=1,
                    recorded_at=datetime.now(UTC),
                )
            )
    with pytest.raises(AuditIntegrityError, match=message):
        restore.get_committed_coalesce_residual(**read_values)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("group", "requires a group ID"),
        ("foreign", "cross run identity"),
        ("duplicate", "duplicate members"),
        ("missing-token", "missing or foreign member token"),
        ("missing-state", "wrong-node state"),
        ("closed", "own OPEN accept-time hold"),
        ("wrong-hold", "own OPEN accept-time hold"),
        ("flush-fields", "together or not at all"),
        ("closed-flush", "OPEN flush state"),
        ("recorded", "already recorded"),
    ],
)
def test_collector_verdict_refuses_partial_or_foreign_evidence(setup: RecorderSetup, case: str, message: str) -> None:
    from elspeth.contracts.errors import ExecutionError

    _batch(setup)
    ensure_fork_group_record(setup.factory, run_id=setup.run_id, group_id="group-1", opener_token_id="tok-0")
    register_test_node(setup.data_flow, setup.run_id, "collector-1", node_type=NodeType.COLLECTOR, plugin_name="collector")
    refs = tuple(TokenRef(f"tok-{ordinal}", setup.run_id) for ordinal in range(2))
    holds = tuple(
        (
            ref,
            setup.execution.begin_node_state(
                ref.token_id, "collector-1", 2, {"value": 1}, member_token=setup.coordination_token.membership
            ).state_id,
            1.0,
        )
        for ref in refs
    )
    reason = CollectorGroupFailureReason.COLLECTOR_TRANSFORM_ERROR
    error = collector_group_failure_hold_error(group_id="group-1", failure_reason=reason, lost_members=())
    values = {
        "group_id": "group-1",
        "collector_node_id": "collector-1",
        "failure_reason": reason,
        "flush_state_id": None,
        "flush_error": None,
        "flush_duration_ms": None,
        "member_holds": holds,
        "hold_error": error,
        "coordination_token": setup.coordination_token,
    }
    if case == "group":
        values["group_id"] = ""
    elif case == "foreign":
        values["member_holds"] = ((TokenRef("tok-0", "foreign"), holds[0][1], 1.0),)
    elif case == "duplicate":
        values["member_holds"] = (holds[0], holds[0])
    elif case == "missing-token":
        values["member_holds"] = ((TokenRef("missing", setup.run_id), holds[0][1], 1.0),)
    elif case == "missing-state":
        values["member_holds"] = ((refs[0], "missing", 1.0),)
    elif case == "wrong-hold":
        values["member_holds"] = ((refs[0], holds[1][1], 1.0),)
    elif case in {"closed", "closed-flush"}:
        setup.execution.complete_node_state(
            holds[0][1],
            NodeStateStatus.FAILED,
            duration_ms=1,
            error=ExecutionError(exception="failed", exception_type="RuntimeError"),
            member_token=setup.coordination_token.membership,
        )
        if case == "closed-flush":
            values.update(flush_state_id=holds[0][1], flush_error=error, flush_duration_ms=1.0)
    elif case == "flush-fields":
        values["flush_state_id"] = holds[0][1]
    elif case == "recorded":
        setup.execution.complete_collector_failure(**values)
    with pytest.raises(AuditIntegrityError, match=message):
        setup.execution.complete_collector_failure(**values)
    with setup.db.read_only_connection() as conn:
        assert len(conn.execute(select(collector_group_failures_table)).all()) == (1 if case == "recorded" else 0)
