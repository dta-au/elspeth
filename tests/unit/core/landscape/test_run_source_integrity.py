"""Source-scoped recovery evidence must retain its contract and identity."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select, update

from elspeth.contracts import NodeType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.core.landscape.schema import run_sources_table, runs_table
from tests.fixtures.audit_hashing import fake_sha256
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run, register_test_node


@pytest.fixture
def recorder_setup() -> Iterator[RecorderSetup]:
    setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
    try:
        yield setup
    finally:
        setup.db.close()


def _source_setup(setup: RecorderSetup, *, contract: bool = True) -> tuple[RecorderSetup, SchemaContract]:
    schema = SchemaContract(mode="OBSERVED", fields=(), locked=True)
    setup.run_lifecycle.record_run_source(
        source_node_id=setup.source_node_id,
        source_name="orders",
        plugin_name="csv",
        config_hash=fake_sha256("csv"),
        lifecycle_state="loaded",
        source_schema_json='{"type":"object"}',
        schema_contract=schema if contract else None,
        coordination_token=setup.coordination_token,
    )
    return setup, schema


def _sources(setup: RecorderSetup) -> tuple[tuple[object, ...], ...]:
    with setup.db.read_only_connection() as conn:
        return tuple(tuple(row) for row in conn.execute(select(run_sources_table)).all())


@pytest.mark.parametrize(
    "defect, message",
    [
        ("missing-json", "no schema contract"),
        ("missing-hash", "no contract hash"),
        ("wrong-hash", "contract hash mismatch"),
        ("missing-schema", "no source schema"),
    ],
)
def test_resume_refuses_incomplete_or_divergent_source_evidence(recorder_setup: RecorderSetup, defect: str, message: str) -> None:
    setup, contract = _source_setup(recorder_setup)
    valid = setup.run_lifecycle.get_run_source_resume_records(setup.run_id)
    assert valid[setup.source_node_id].schema_contract == contract
    with setup.db.write_connection() as conn:
        if defect == "missing-json":
            conn.execute(update(run_sources_table).values(schema_contract_json=None))
        elif defect == "missing-hash":
            conn.execute(update(run_sources_table).values(schema_contract_hash=None))
        elif defect == "wrong-hash":
            conn.execute(update(run_sources_table).values(schema_contract_hash="f" * 32))
        else:
            conn.execute(update(run_sources_table).values(schema_json=None))
    before = _sources(setup)
    with pytest.raises(AuditIntegrityError, match=message):
        setup.run_lifecycle.get_run_source_resume_records(setup.run_id)
    assert _sources(setup) == before


@pytest.mark.parametrize(
    "defect, message",
    [
        ("missing-row", "row does not exist"),
        ("missing-hash", "no hash"),
        ("wrong-hash", "hash mismatch"),
        ("different-contract", "Cannot overwrite"),
    ],
)
def test_backfill_refuses_missing_or_corrupt_existing_contract(recorder_setup: RecorderSetup, defect: str, message: str) -> None:
    setup, contract = _source_setup(recorder_setup)
    node_id = setup.source_node_id
    if defect == "missing-row":
        node_id = "absent-source"
    elif defect == "different-contract":
        contract = SchemaContract(mode="OBSERVED", fields=(FieldContract("id", "id", int, True, "inferred"),), locked=True)
    else:
        with setup.db.write_connection() as conn:
            if defect == "missing-hash":
                conn.execute(update(run_sources_table).values(schema_contract_hash=None))
            else:
                conn.execute(update(run_sources_table).values(schema_contract_hash="f" * 32))
    before = _sources(setup)
    with pytest.raises(AuditIntegrityError, match=message):
        setup.run_lifecycle.update_run_source_contract(
            source_node_id=node_id, schema_contract=contract, coordination_token=setup.coordination_token
        )
    assert _sources(setup) == before


def test_first_row_contract_backfill_is_idempotent_and_resume_preserves_it(recorder_setup: RecorderSetup) -> None:
    setup, contract = _source_setup(recorder_setup, contract=False)
    setup.run_lifecycle.update_run_source_contract(
        source_node_id=setup.source_node_id, schema_contract=contract, coordination_token=setup.coordination_token
    )
    after = _sources(setup)
    setup.run_lifecycle.update_run_source_contract(
        source_node_id=setup.source_node_id, schema_contract=contract, coordination_token=setup.coordination_token
    )
    assert _sources(setup) == after
    assert setup.run_lifecycle.get_run_source_resume_records(setup.run_id)[setup.source_node_id].schema_contract == contract


@pytest.mark.parametrize("recorded, scoped", [(False, False), (True, False), (True, True)])
def test_single_source_resume_uses_scoped_mapping_or_legacy_resolution(recorder_setup: RecorderSetup, recorded: bool, scoped: bool) -> None:
    setup, _contract = _source_setup(recorder_setup)
    legacy = {"Order ID": "id"}
    mapping = {"Customer ID": "customer_id"}
    if recorded:
        setup.run_lifecycle.record_source_field_resolution(legacy, "v1", coordination_token=setup.coordination_token)
    if scoped:
        setup.run_lifecycle.record_run_source(
            source_node_id=setup.source_node_id,
            source_name="orders",
            plugin_name="csv",
            config_hash=fake_sha256("csv"),
            lifecycle_state="loaded",
            field_resolution_mapping=mapping,
            coordination_token=setup.coordination_token,
        )
    expected = mapping if scoped else legacy if recorded else None
    assert setup.run_lifecycle.get_resume_field_resolution(setup.run_id) == expected


@pytest.mark.parametrize("node_id", ["missing", "transform-1"])
def test_source_metadata_cannot_bind_missing_or_non_source_node(recorder_setup: RecorderSetup, node_id: str) -> None:
    setup, _contract = _source_setup(recorder_setup)
    register_test_node(setup.data_flow, setup.run_id, "transform-1", node_type=NodeType.TRANSFORM)
    before = _sources(setup)
    with pytest.raises(AuditIntegrityError, match=r"does not exist|expected 'source'"):
        setup.run_lifecycle.record_run_source(
            source_node_id=node_id,
            source_name="invalid",
            plugin_name="csv",
            config_hash=fake_sha256("csv"),
            lifecycle_state="loaded",
            coordination_token=setup.coordination_token,
        )
    assert _sources(setup) == before


@pytest.mark.parametrize(
    "defect, message", [("missing-run", "not found"), ("missing-manifest", "no runtime VAL"), ("binary-manifest", "expected str")]
)
def test_runtime_manifest_requires_complete_typed_audit_evidence(recorder_setup: RecorderSetup, defect: str, message: str) -> None:
    setup, _contract = _source_setup(recorder_setup)
    run_id = setup.run_id
    if defect == "missing-run":
        run_id = "missing"
    else:
        with setup.db.write_connection() as conn:
            conn.execute(update(runs_table).values(runtime_val_manifest_json=None if defect == "missing-manifest" else b"{}"))
    with setup.db.read_only_connection() as conn:
        before = tuple(conn.execute(select(runs_table)).one())
    with pytest.raises(AuditIntegrityError, match=message):
        setup.run_lifecycle.get_runtime_val_manifest(run_id)
    with setup.db.read_only_connection() as conn:
        assert tuple(conn.execute(select(runs_table)).one()) == before


def test_missing_run_cannot_return_empty_source_resolution_roster(recorder_setup: RecorderSetup) -> None:
    setup, _contract = _source_setup(recorder_setup)
    with pytest.raises(AuditIntegrityError, match="not found"):
        setup.run_lifecycle.get_source_field_resolutions("missing")
