"""Tests for Landscape-derived web run accounting."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from elspeth.contracts.audit import _TERMINAL_PAIR_FIELD_CONSTRAINTS
from elspeth.contracts.enums import NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import run_sources_table, token_outcomes_table, tokens_table, transform_errors_table
from elspeth.web.execution.accounting import (
    _bounded_violations,
    load_run_accounting_for_settings,
    load_run_accounting_from_db,
    load_run_accounting_map_from_db,
)

_OBSERVED_SCHEMA = SchemaConfig.from_dict({"mode": "observed"})
_NOW = datetime(2026, 5, 6, tzinfo=UTC)

# Synthetic evidence values satisfying the ADR-019 pair-field constraints for
# seeded outcomes. ``_legal_evidence_fields`` derives the per-pair legal shape
# from the SAME canonical table the accounting read model validates against,
# so seeds stay legal as the constraint table evolves.
_SYNTHETIC_EVIDENCE: dict[str, str] = {
    "sink_name": "sink-out",
    "batch_id": "batch-1",
    "fork_group_id": "fork-1",
    "join_group_id": "join-1",
    "expand_group_id": "expand-1",
    "error_hash": "e" * 16,
}


def _legal_evidence_fields(outcome: TerminalOutcome | None, path: TerminalPath) -> dict[str, str | None]:
    constraints = _TERMINAL_PAIR_FIELD_CONSTRAINTS[(outcome, path)]
    fields: dict[str, str | None] = dict.fromkeys(_SYNTHETIC_EVIDENCE)
    for name in constraints.required:
        fields[name] = _SYNTHETIC_EVIDENCE[name]
    for name, value in constraints.exact.items():
        assert isinstance(value, str)
        fields[name] = value
    return fields


@dataclass(frozen=True)
class _SettingsFake:
    landscape_url: str
    landscape_passphrase: str | None = None

    def get_landscape_url(self) -> str:
        return self.landscape_url


def _setup_run_with_row(db: LandscapeDB, *, run_id: str, row_id: str = "row-1") -> None:
    factory = RecorderFactory(db)
    factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=run_id)
    factory.data_flow.register_node(
        run_id=run_id,
        node_id="source",
        plugin_name="text",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        schema_config=_OBSERVED_SCHEMA,
    )
    factory.data_flow.create_row(run_id, "source", 0, {"value": "input"}, row_id=row_id, source_row_index=0, ingest_sequence=0)


def _setup_empty_run(db: LandscapeDB, *, run_id: str) -> None:
    RecorderFactory(db).run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=run_id)


def _insert_tokens(db: LandscapeDB, *, run_id: str, row_id: str, token_ids: list[str]) -> None:
    with db.write_connection() as conn:
        conn.execute(
            tokens_table.insert(),
            [
                {
                    "token_id": token_id,
                    "row_id": row_id,
                    "run_id": run_id,
                    "fork_group_id": None,
                    "join_group_id": None,
                    "expand_group_id": None,
                    "branch_name": None,
                    "step_in_pipeline": 0,
                    "created_at": _NOW,
                }
                for token_id in token_ids
            ],
        )


def _insert_completed_outcomes(
    db: LandscapeDB,
    *,
    run_id: str,
    token_ids: list[str],
    outcome: TerminalOutcome,
    path: TerminalPath,
    **field_overrides: str | None,
) -> None:
    fields = _legal_evidence_fields(outcome, path)
    fields.update(field_overrides)
    with db.write_connection() as conn:
        conn.execute(
            token_outcomes_table.insert(),
            [
                {
                    "outcome_id": f"outcome-{token_id}-{path.value}",
                    "run_id": run_id,
                    "token_id": token_id,
                    "outcome": outcome.value,
                    "path": path.value,
                    "completed": 1,
                    "recorded_at": _NOW,
                    "context_json": None,
                    "expected_branches_json": None,
                    **fields,
                }
                for token_id in token_ids
            ],
        )


def _insert_outcome(
    db: LandscapeDB,
    *,
    run_id: str,
    token_id: str,
    outcome: TerminalOutcome | None,
    path: TerminalPath,
    completed: int,
    outcome_value: str | None = None,
    **field_overrides: str | None,
) -> None:
    # Evidence auto-fill applies to completed rows only: the accounting read
    # model enforces pair-field evidence for completed outcomes (what closure
    # trusts), while a non-completed BUFFERED row's batch linkage is FK-bound
    # to a real batches row this fixture does not build.
    fields = (
        _legal_evidence_fields(outcome, path)
        if completed == 1 and (outcome, path) in _TERMINAL_PAIR_FIELD_CONSTRAINTS
        else dict.fromkeys(_SYNTHETIC_EVIDENCE)
    )
    fields.update(field_overrides)
    with db.write_connection() as conn:
        conn.execute(
            token_outcomes_table.insert().values(
                outcome_id=f"outcome-{token_id}-{path.value}-{completed}",
                run_id=run_id,
                token_id=token_id,
                outcome=outcome_value if outcome_value is not None else (outcome.value if outcome is not None else None),
                path=path.value,
                completed=completed,
                recorded_at=_NOW,
                context_json=None,
                expected_branches_json=None,
                **fields,
            )
        )


def test_one_source_row_expands_to_many_tokens_and_closes() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-1")
        success_token_ids = [f"child-{i}" for i in range(9323)]
        _insert_tokens(db, run_id="run-1", row_id="row-1", token_ids=["parent", *success_token_ids])
        _insert_completed_outcomes(
            db,
            run_id="run-1",
            token_ids=success_token_ids,
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
        )
        _insert_completed_outcomes(
            db,
            run_id="run-1",
            token_ids=["parent"],
            outcome=TerminalOutcome.TRANSIENT,
            path=TerminalPath.EXPAND_PARENT,
        )

        accounting = load_run_accounting_from_db(db, landscape_run_id="run-1")

        assert accounting.source.rows_processed == 1
        assert accounting.tokens.emitted == 9324
        assert accounting.tokens.terminal == 9324
        assert accounting.tokens.succeeded == 9323
        assert accounting.tokens.failed == 0
        assert accounting.tokens.structural == 1
        assert accounting.tokens.pending == 0
        assert accounting.integrity.closure == "closed"
        assert accounting.integrity.missing_terminal_outcomes == 0
        assert accounting.integrity.duplicate_terminal_outcomes == 0
    finally:
        db.close()


def test_source_accounting_projects_named_sources_and_aggregate_total() -> None:
    db = LandscapeDB.in_memory()
    try:
        factory = RecorderFactory(db)
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="run-multi")
        for source_name, source_node_id in (("orders", "source-orders"), ("refunds", "source-refunds")):
            factory.data_flow.register_node(
                run_id="run-multi",
                node_id=source_node_id,
                plugin_name="csv",
                node_type=NodeType.SOURCE,
                plugin_version="1.0",
                config={},
                schema_config=_OBSERVED_SCHEMA,
            )
            with db.write_connection() as conn:
                conn.execute(
                    run_sources_table.insert().values(
                        run_id="run-multi",
                        source_node_id=source_node_id,
                        source_name=source_name,
                        plugin_name="csv",
                        lifecycle_state="loaded",
                        config_hash=f"hash-{source_name}",
                        schema_json=None,
                        schema_contract_json=None,
                        schema_contract_hash=None,
                        field_resolution_json=None,
                        recorded_at=_NOW,
                    )
                )

        factory.data_flow.create_row(
            "run-multi",
            "source-orders",
            0,
            {"id": "order-1"},
            row_id="orders-0",
            source_row_index=0,
            ingest_sequence=0,
        )
        factory.data_flow.create_row(
            "run-multi",
            "source-orders",
            1,
            {"id": "order-2"},
            row_id="orders-1",
            source_row_index=1,
            ingest_sequence=1,
        )
        factory.data_flow.create_row(
            "run-multi",
            "source-refunds",
            2,
            {"id": "refund-1"},
            row_id="refunds-0",
            source_row_index=0,
            ingest_sequence=2,
        )

        accounting = load_run_accounting_from_db(db, landscape_run_id="run-multi")

        assert accounting.source.rows_processed == 3
        assert accounting.sources["orders"].rows_processed == 2
        assert accounting.sources["refunds"].rows_processed == 1
    finally:
        db.close()


def test_rows_rejected_counts_only_discard_destination_validation_errors() -> None:
    """A quarantined row is ADMITTED — it is already inside rows_processed.

    Counting ``validation_errors`` without the ``destination='discard'`` scope
    double-counts quarantined rows, so a run that quarantines every row would
    report twice as many rows read as the file holds.
    """
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-vr")
        factory = RecorderFactory(db)
        factory.data_flow.record_validation_error("run-vr", "source", {"amount": "alpha"}, "amount: not an int", "fixed", "discard")
        factory.data_flow.record_validation_error("run-vr", "source", {"amount": "beta"}, "amount: not an int", "fixed", "discard")
        factory.data_flow.record_validation_error("run-vr", "source", {"amount": "gamma"}, "amount: not an int", "fixed", "rejects")

        accounting = load_run_accounting_from_db(db, landscape_run_id="run-vr")

        assert accounting.source.rows_processed == 1
        assert accounting.source.rows_rejected == 2
        assert accounting.source.rows_read == 3
        assert accounting.sources["source"].rows_rejected == 2
        assert accounting.sources["source"].rows_read == 3
    finally:
        db.close()


def test_transform_discards_do_not_feed_source_rows_rejected() -> None:
    """A row discarded at transform validation was admitted and has a rows row.

    Feeding ``transform_errors`` into ``rows_rejected`` would double-count it
    against ``rows_processed`` — the same bug as the unscoped count, one table
    over.
    """
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-te")
        factory = RecorderFactory(db)
        factory.data_flow.register_node(
            run_id="run-te",
            node_id="transform",
            plugin_name="value_transform",
            node_type=NodeType.TRANSFORM,
            plugin_version="1.0",
            config={},
            schema_config=_OBSERVED_SCHEMA,
        )
        _insert_tokens(db, run_id="run-te", row_id="row-1", token_ids=["token-1"])
        with db.write_connection() as conn:
            conn.execute(
                transform_errors_table.insert().values(
                    error_id="terr-1",
                    run_id="run-te",
                    token_id="token-1",
                    transform_id="transform",
                    row_hash="hash-1",
                    row_data_json=None,
                    error_details_json=None,
                    destination="discard",
                    created_at=_NOW,
                )
            )

        accounting = load_run_accounting_from_db(db, landscape_run_id="run-te")

        assert accounting.source.rows_processed == 1
        assert accounting.source.rows_rejected == 0
        assert accounting.source.rows_read == 1
    finally:
        db.close()


def test_all_rows_rejected_source_still_appears_in_per_source_accounting() -> None:
    """The g01 shape at projection level: zero admitted rows must not erase the source."""
    db = LandscapeDB.in_memory()
    try:
        factory = RecorderFactory(db)
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="run-g01")
        factory.data_flow.register_node(
            run_id="run-g01",
            node_id="source-tickets",
            plugin_name="csv",
            node_type=NodeType.SOURCE,
            plugin_version="1.0",
            config={},
            schema_config=_OBSERVED_SCHEMA,
        )
        with db.write_connection() as conn:
            conn.execute(
                run_sources_table.insert().values(
                    run_id="run-g01",
                    source_node_id="source-tickets",
                    source_name="tickets",
                    plugin_name="csv",
                    lifecycle_state="loaded",
                    config_hash="hash-tickets",
                    schema_json=None,
                    schema_contract_json=None,
                    schema_contract_hash=None,
                    field_resolution_json=None,
                    recorded_at=_NOW,
                )
            )
        for index in range(4):
            factory.data_flow.record_validation_error(
                "run-g01",
                "source-tickets",
                {"amount": f"value-{index}"},
                "amount: not an int",
                "fixed",
                "discard",
            )

        accounting = load_run_accounting_from_db(db, landscape_run_id="run-g01")

        assert accounting.source.rows_processed == 0
        assert accounting.source.rows_rejected == 4
        assert accounting.source.rows_read == 4
        assert accounting.sources["tickets"].rows_processed == 0
        assert accounting.sources["tickets"].rows_rejected == 4
        assert accounting.sources["tickets"].rows_read == 4
    finally:
        db.close()


def test_missing_completed_terminal_outcome_marks_token_pending_and_open() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-2")
        _insert_tokens(db, run_id="run-2", row_id="row-1", token_ids=["token-1"])

        accounting = load_run_accounting_from_db(db, landscape_run_id="run-2")

        assert accounting.tokens.emitted == 1
        assert accounting.tokens.terminal == 0
        assert accounting.tokens.pending == 1
        assert accounting.integrity.missing_terminal_outcomes == 1
        assert accounting.integrity.duplicate_terminal_outcomes == 0
        assert accounting.integrity.closure == "open"
    finally:
        db.close()


def test_abandoned_outcome_closes_missing_fate_without_counting_terminal_success() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-abandoned")
        _insert_tokens(db, run_id="run-abandoned", row_id="row-1", token_ids=["token-1"])
        _insert_outcome(
            db,
            run_id="run-abandoned",
            token_id="token-1",
            outcome=None,
            path=TerminalPath.ABANDONED,
            completed=0,
        )

        accounting = load_run_accounting_from_db(db, landscape_run_id="run-abandoned")

        assert accounting.tokens.emitted == 1
        assert accounting.tokens.terminal == 0
        assert accounting.tokens.pending == 0
        assert accounting.tokens.abandoned == 1
        assert accounting.integrity.missing_terminal_outcomes == 0
        assert accounting.integrity.closure == "abandoned"
    finally:
        db.close()


def test_decided_and_abandoned_same_token_is_an_audit_contradiction() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-contradiction")
        _insert_tokens(db, run_id="run-contradiction", row_id="row-1", token_ids=["token-1"])
        _insert_completed_outcomes(
            db,
            run_id="run-contradiction",
            token_ids=["token-1"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
        )
        _insert_outcome(
            db,
            run_id="run-contradiction",
            token_id="token-1",
            outcome=None,
            path=TerminalPath.ABANDONED,
            completed=0,
        )

        with pytest.raises(ValueError, match="both terminally decided and marked ABANDONED"):
            load_run_accounting_from_db(db, landscape_run_id="run-contradiction")
    finally:
        db.close()


def test_batch_loader_returns_accounting_for_requested_runs() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-a", row_id="row-a")
        _insert_tokens(db, run_id="run-a", row_id="row-a", token_ids=["token-a"])
        _insert_completed_outcomes(
            db,
            run_id="run-a",
            token_ids=["token-a"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
        )
        _setup_run_with_row(db, run_id="run-b", row_id="row-b")
        _insert_tokens(db, run_id="run-b", row_id="row-b", token_ids=["token-b"])

        batch = load_run_accounting_map_from_db(db, ("run-b", "run-a", "run-a"))
        accounting = batch.accounting

        assert batch.corrupt == {}
        assert tuple(accounting) == ("run-a", "run-b")
        assert accounting["run-a"].integrity.closure == "closed"
        assert accounting["run-a"].tokens.succeeded == 1
        assert accounting["run-b"].integrity.closure == "open"
        assert accounting["run-b"].integrity.missing_terminal_outcomes == 1
    finally:
        db.close()


def test_batch_loader_does_not_fabricate_absent_landscape_runs() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_empty_run(db, run_id="empty-run")

        batch = load_run_accounting_map_from_db(db, ("missing-run", "empty-run"))
        accounting = batch.accounting

        assert set(accounting) == {"empty-run"}
        assert accounting["empty-run"].source.rows_processed == 0
        assert accounting["empty-run"].tokens.emitted == 0
        assert accounting["empty-run"].integrity.closure == "closed"
    finally:
        db.close()


def test_settings_loader_returns_empty_for_missing_sqlite_database(tmp_path: Path) -> None:
    settings = _SettingsFake(landscape_url=f"sqlite:///{tmp_path / 'missing-audit.db'}")

    batch = load_run_accounting_for_settings(settings, ("run-1", None))

    assert batch.accounting == {}
    assert batch.corrupt == {}


def test_routing_subset_classification_counts_completed_terminal_pairs() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-3")
        token_ids = ["gate", "error", "quarantine", "discard", "gate-error-discard"]
        _insert_tokens(db, run_id="run-3", row_id="row-1", token_ids=token_ids)
        _insert_completed_outcomes(
            db,
            run_id="run-3",
            token_ids=["gate"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.GATE_ROUTED,
        )
        _insert_completed_outcomes(
            db,
            run_id="run-3",
            token_ids=["error"],
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.ON_ERROR_ROUTED,
        )
        _insert_completed_outcomes(
            db,
            run_id="run-3",
            token_ids=["quarantine"],
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.QUARANTINED_AT_SOURCE,
        )
        _insert_completed_outcomes(
            db,
            run_id="run-3",
            token_ids=["discard"],
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.SINK_DISCARDED,
        )
        _insert_completed_outcomes(
            db,
            run_id="run-3",
            token_ids=["gate-error-discard"],
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.GATE_ERROR_DISCARDED,
        )

        accounting = load_run_accounting_from_db(db, landscape_run_id="run-3")

        assert accounting.tokens.emitted == 5
        assert accounting.tokens.terminal == 5
        assert accounting.tokens.succeeded == 1
        assert accounting.tokens.failed == 4
        assert accounting.routing.routed_success == 1
        assert accounting.routing.routed_failure == 1
        assert accounting.routing.quarantined == 1
        assert accounting.routing.discarded == 2
        assert accounting.integrity.closure == "closed"
    finally:
        db.close()


def test_buffered_non_completed_outcomes_do_not_count_as_terminal() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-4")
        _insert_tokens(db, run_id="run-4", row_id="row-1", token_ids=["token-1"])
        _insert_outcome(
            db,
            run_id="run-4",
            token_id="token-1",
            outcome=None,
            path=TerminalPath.BUFFERED,
            completed=0,
        )

        accounting = load_run_accounting_from_db(db, landscape_run_id="run-4")

        assert accounting.tokens.emitted == 1
        assert accounting.tokens.terminal == 0
        assert accounting.tokens.pending == 1
        assert accounting.integrity.missing_terminal_outcomes == 1
        assert accounting.integrity.closure == "open"
    finally:
        db.close()


def test_duplicate_completed_outcomes_raise_clear_integrity_error() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-6")
        _insert_tokens(db, run_id="run-6", row_id="row-1", token_ids=["duplicate-token", "missing-token"])
        with db.write_connection() as conn:
            conn.execute(text("DROP INDEX ix_token_outcomes_terminal_unique"))
        _insert_completed_outcomes(
            db,
            run_id="run-6",
            token_ids=["duplicate-token"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
        )
        _insert_completed_outcomes(
            db,
            run_id="run-6",
            token_ids=["duplicate-token"],
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
        )

        with pytest.raises(ValueError, match="duplicate completed terminal outcomes"):
            load_run_accounting_from_db(db, landscape_run_id="run-6")
    finally:
        db.close()


def test_corrupt_run_is_isolated_and_healthy_runs_remain_available() -> None:
    """One corrupt run must not hide accounting for healthy runs in the same batch (elspeth-d5578ccd98)."""
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-good", row_id="row-good")
        _insert_tokens(db, run_id="run-good", row_id="row-good", token_ids=["token-good"])
        _insert_completed_outcomes(
            db,
            run_id="run-good",
            token_ids=["token-good"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
        )

        _setup_run_with_row(db, run_id="run-bad", row_id="row-bad")
        _insert_tokens(db, run_id="run-bad", row_id="row-bad", token_ids=["dup-token"])
        with db.write_connection() as conn:
            conn.execute(text("DROP INDEX ix_token_outcomes_terminal_unique"))
        _insert_completed_outcomes(
            db,
            run_id="run-bad",
            token_ids=["dup-token"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
        )
        _insert_completed_outcomes(
            db,
            run_id="run-bad",
            token_ids=["dup-token"],
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.UNROUTED,
        )

        batch = load_run_accounting_map_from_db(db, ("run-good", "run-bad"))

        assert set(batch.accounting) == {"run-good"}
        assert batch.accounting["run-good"].integrity.closure == "closed"
        assert set(batch.corrupt) == {"run-bad"}
        corruption = batch.corrupt["run-bad"]
        assert corruption.landscape_run_id == "run-bad"
        assert any("duplicate completed terminal outcomes" in violation for violation in corruption.violations)
    finally:
        db.close()


def test_unknown_outcome_enum_marks_run_corrupt_not_closed() -> None:
    """A count-complete run with a non-canonical outcome string must not read 'closed'."""
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-enum")
        _insert_tokens(db, run_id="run-enum", row_id="row-1", token_ids=["token-1"])
        _insert_outcome(
            db,
            run_id="run-enum",
            token_id="token-1",
            outcome=None,
            outcome_value="banana",
            path=TerminalPath.DEFAULT_FLOW,
            completed=1,
        )

        batch = load_run_accounting_map_from_db(db, ("run-enum",))

        assert batch.accounting == {}
        assert set(batch.corrupt) == {"run-enum"}
        assert any("banana" in violation for violation in batch.corrupt["run-enum"].violations)
    finally:
        db.close()


def test_illegal_terminal_pair_marks_run_corrupt() -> None:
    """(FAILURE, DEFAULT_FLOW) is not a legal ADR-019 pair and must fail per-run integrity."""
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-pair")
        _insert_tokens(db, run_id="run-pair", row_id="row-1", token_ids=["token-1"])
        _insert_outcome(
            db,
            run_id="run-pair",
            token_id="token-1",
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.DEFAULT_FLOW,
            completed=1,
            sink_name="sink-out",
        )

        batch = load_run_accounting_map_from_db(db, ("run-pair",))

        assert batch.accounting == {}
        assert any("not a legal" in violation for violation in batch.corrupt["run-pair"].violations)
    finally:
        db.close()


def test_missing_required_evidence_marks_run_corrupt() -> None:
    """(SUCCESS, DEFAULT_FLOW) requires sink_name; NULL evidence must fail per-run integrity."""
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-evidence")
        _insert_tokens(db, run_id="run-evidence", row_id="row-1", token_ids=["token-1"])
        _insert_completed_outcomes(
            db,
            run_id="run-evidence",
            token_ids=["token-1"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
            sink_name=None,
        )

        batch = load_run_accounting_map_from_db(db, ("run-evidence",))

        assert batch.accounting == {}
        assert any("sink_name" in violation for violation in batch.corrupt["run-evidence"].violations)
    finally:
        db.close()


def test_forbidden_evidence_marks_run_corrupt() -> None:
    """(SUCCESS, GATE_DISCARDED) forbids discriminator fields; a stray sink_name must fail."""
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-forbidden")
        _insert_tokens(db, run_id="run-forbidden", row_id="row-1", token_ids=["token-1"])
        _insert_completed_outcomes(
            db,
            run_id="run-forbidden",
            token_ids=["token-1"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.GATE_DISCARDED,
            sink_name="sink-out",
        )

        batch = load_run_accounting_map_from_db(db, ("run-forbidden",))

        assert batch.accounting == {}
        assert any("forbids sink_name" in violation for violation in batch.corrupt["run-forbidden"].violations)
    finally:
        db.close()


def test_exact_evidence_violation_marks_run_corrupt() -> None:
    """(FAILURE, SINK_DISCARDED) requires the discard sink sentinel as sink_name."""
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-exact")
        _insert_tokens(db, run_id="run-exact", row_id="row-1", token_ids=["token-1"])
        _insert_completed_outcomes(
            db,
            run_id="run-exact",
            token_ids=["token-1"],
            outcome=TerminalOutcome.FAILURE,
            path=TerminalPath.SINK_DISCARDED,
            sink_name="some-other-sink",
        )

        batch = load_run_accounting_map_from_db(db, ("run-exact",))

        assert batch.accounting == {}
        assert any("sink_name" in violation for violation in batch.corrupt["run-exact"].violations)
    finally:
        db.close()


def test_completed_with_null_outcome_marks_run_corrupt() -> None:
    """completed=1 with outcome NULL violates the ADR-019 XOR invariant."""
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-xor")
        _insert_tokens(db, run_id="run-xor", row_id="row-1", token_ids=["token-1"])
        _insert_outcome(
            db,
            run_id="run-xor",
            token_id="token-1",
            outcome=None,
            path=TerminalPath.DEFAULT_FLOW,
            completed=1,
            sink_name="sink-out",
        )

        batch = load_run_accounting_map_from_db(db, ("run-xor",))

        assert batch.accounting == {}
        assert set(batch.corrupt) == {"run-xor"}
    finally:
        db.close()


def test_not_completed_with_outcome_marks_run_corrupt() -> None:
    """completed=0 with a non-NULL outcome violates the ADR-019 XOR invariant."""
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-xor2")
        _insert_tokens(db, run_id="run-xor2", row_id="row-1", token_ids=["token-1"])
        _insert_outcome(
            db,
            run_id="run-xor2",
            token_id="token-1",
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.BUFFERED,
            completed=0,
        )

        batch = load_run_accounting_map_from_db(db, ("run-xor2",))

        assert batch.accounting == {}
        assert set(batch.corrupt) == {"run-xor2"}
    finally:
        db.close()


@pytest.mark.parametrize("raw_completed", [pytest.param(1.5, id="fractional"), pytest.param("not-an-integer", id="text")])
def test_non_integer_completed_marks_run_corrupt_without_escaping_batch(raw_completed: object) -> None:
    """The accounting boundary must validate raw SQLite values before interpreting them."""
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-completed-shape")
        _insert_tokens(db, run_id="run-completed-shape", row_id="row-1", token_ids=["token-1"])
        _insert_completed_outcomes(
            db,
            run_id="run-completed-shape",
            token_ids=["token-1"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
        )
        with db.write_connection() as conn:
            conn.exec_driver_sql(
                "UPDATE token_outcomes SET completed = ? WHERE run_id = ?",
                (raw_completed, "run-completed-shape"),
            )

        batch = load_run_accounting_map_from_db(db, ("run-completed-shape",))

        assert batch.accounting == {}
        corruption = batch.corrupt["run-completed-shape"]
        assert any("non-boolean completed" in violation for violation in corruption.violations)
    finally:
        db.close()


@pytest.mark.parametrize("corrupt_field", ["path", "outcome", "sink_name"])
def test_corrupt_outcome_diagnostics_bound_stored_values(corrupt_field: str) -> None:
    """One oversized stored discriminator must not create an oversized API diagnostic."""
    db = LandscapeDB.in_memory()
    oversized_value = f"{corrupt_field}\n" + ("x" * 200_000)
    try:
        _setup_run_with_row(db, run_id="run-oversized-diagnostic")
        _insert_tokens(db, run_id="run-oversized-diagnostic", row_id="row-1", token_ids=["token-1"])
        _insert_completed_outcomes(
            db,
            run_id="run-oversized-diagnostic",
            token_ids=["token-1"],
            outcome=TerminalOutcome.FAILURE if corrupt_field == "sink_name" else TerminalOutcome.SUCCESS,
            path=TerminalPath.SINK_DISCARDED if corrupt_field == "sink_name" else TerminalPath.DEFAULT_FLOW,
        )
        with db.write_connection() as conn:
            conn.exec_driver_sql(
                f"UPDATE token_outcomes SET {corrupt_field} = ? WHERE run_id = ?",
                (oversized_value, "run-oversized-diagnostic"),
            )

        batch = load_run_accounting_map_from_db(db, ("run-oversized-diagnostic",))

        violations = batch.corrupt["run-oversized-diagnostic"].violations
        digest = hashlib.sha256(oversized_value.encode("utf-8")).hexdigest()[:16]
        assert all(len(violation) < 1024 for violation in violations)
        assert oversized_value not in "".join(violations)
        assert "\n" not in "".join(violations)
        assert f"sha256={digest}" in "".join(violations)
    finally:
        db.close()


def test_violation_bounding_is_deduplicated_and_permutation_stable() -> None:
    violations = [f"violation-{index:02d}" for index in range(10)]
    scrambled_with_duplicate = [violations[index] for index in (9, 0, 4, 2, 7, 0, 5, 1, 8, 6, 3)]
    expected = [*violations[:8], "+2 more violation(s) truncated"]

    assert _bounded_violations(scrambled_with_duplicate) == expected
    assert _bounded_violations(list(reversed(scrambled_with_duplicate))) == expected


def test_sqlite_blocks_duplicate_completed_terminal_outcomes() -> None:
    db = LandscapeDB.in_memory()
    try:
        _setup_run_with_row(db, run_id="run-5")
        _insert_tokens(db, run_id="run-5", row_id="row-1", token_ids=["token-1"])
        _insert_completed_outcomes(
            db,
            run_id="run-5",
            token_ids=["token-1"],
            outcome=TerminalOutcome.SUCCESS,
            path=TerminalPath.DEFAULT_FLOW,
        )

        with pytest.raises(IntegrityError):
            _insert_completed_outcomes(
                db,
                run_id="run-5",
                token_ids=["token-1"],
                outcome=TerminalOutcome.FAILURE,
                path=TerminalPath.UNROUTED,
            )
    finally:
        db.close()


def _count_sqlite_vm_steps(db: LandscapeDB, run_id: str) -> int:
    """Return SQLite virtual-machine steps spent projecting one run's accounting.

    A wall-clock budget would be flaky under parallel workers; VM steps are a
    deterministic cost proxy, and the assertion below reads a RATIO of two
    scales rather than an absolute, so it does not pin a SQLite version's
    instruction counts.
    """
    steps = 0

    def _tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    def _attach(dbapi_connection: object, _record: object, _proxy: object) -> None:
        # pysqlite connection; the progress handler is the only cost meter
        # SQLite exposes without a compile-time status API.
        assert isinstance(dbapi_connection, sqlite3.Connection)
        dbapi_connection.set_progress_handler(_tick, 100)

    event.listen(db.engine, "checkout", _attach)
    try:
        load_run_accounting_map_from_db(db, (run_id,))
    finally:
        event.remove(db.engine, "checkout", _attach)
    return steps


def _seed_decided_run(db: LandscapeDB, *, run_id: str, token_count: int) -> None:
    _setup_run_with_row(db, run_id=run_id)
    token_ids = [f"token-{index}" for index in range(token_count)]
    _insert_tokens(db, run_id=run_id, row_id="row-1", token_ids=token_ids)
    _insert_completed_outcomes(
        db,
        run_id=run_id,
        token_ids=token_ids,
        outcome=TerminalOutcome.SUCCESS,
        path=TerminalPath.DEFAULT_FLOW,
    )


def test_accounting_cost_grows_with_token_count_not_its_square() -> None:
    """Pin the census as linear in a run's size, not quadratic (elspeth-c675c8c2d9).

    The defect: the per-token census joined ``tokens`` to ``token_outcomes`` on
    (run_id, token_id) with no index covering the pair, so SQLite resolved the
    inner loop through ``ix_token_outcomes_run_id`` and re-scanned every outcome
    for every token — then paid it four times over. A 60k-row run took 618s.

    Scaling BOTH sides by 8 multiplies quadratic work by ~64 and linear work by
    ~8, so the ratio discriminates the two implementations regardless of how the
    census is written.
    """
    small_db = LandscapeDB.in_memory()
    large_db = LandscapeDB.in_memory()
    try:
        _seed_decided_run(small_db, run_id="run-small", token_count=150)
        _seed_decided_run(large_db, run_id="run-large", token_count=1200)

        small_steps = _count_sqlite_vm_steps(small_db, "run-small")
        large_steps = _count_sqlite_vm_steps(large_db, "run-large")

        # Cost must not be bought with wrong answers: both runs are fully
        # decided, so accounting must be present and closed.
        for db, run_id, token_count in ((small_db, "run-small", 150), (large_db, "run-large", 1200)):
            accounting = load_run_accounting_map_from_db(db, (run_id,)).accounting[run_id]
            assert accounting.integrity.closure == "closed"
            assert accounting.tokens.emitted == token_count

        growth = large_steps / max(small_steps, 1)
        assert growth < 20, f"census cost grew {growth:.1f}x for an 8x larger run ({small_steps} -> {large_steps} steps) — quadratic"
    finally:
        small_db.close()
        large_db.close()
