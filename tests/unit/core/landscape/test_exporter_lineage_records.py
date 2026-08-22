"""WS1b Phase B flip: export records carry lineage_path, not the retired
tri-fields, and the two new unified-lineage record types round-trip."""


def test_token_export_record_carries_lineage_path_not_tri_fields() -> None:
    from elspeth.contracts.export_records import TokenExportRecord

    keys = set(TokenExportRecord.__annotations__)
    assert "lineage_path" in keys
    assert {"branch_name", "fork_group_id", "expand_group_id"} & keys == set()
    assert "join_group_id" in keys  # merge event — kept


def test_token_outcome_export_record_drops_retired_columns() -> None:
    from elspeth.contracts.export_records import TokenOutcomeExportRecord

    keys = set(TokenOutcomeExportRecord.__annotations__)
    assert {"fork_group_id", "join_group_id", "expand_group_id", "expected_branches_json"} & keys == set()


def test_group_loss_export_record_mirrors_the_ledger_ddl() -> None:
    from elspeth.contracts.export_records import GroupLossExportRecord

    assert set(GroupLossExportRecord.__annotations__) == {
        "record_type",
        "loss_id",
        "run_id",
        "closer_name",
        "group_id",
        "member_key",
        "token_id",
        "reason",
        "recorded_by",
        "recorded_at",
        "adopted_epoch",
    }


def test_fork_coalesce_export_round_trips_lineage_path_and_group_records() -> None:
    """Run a real fork->coalesce pipeline through the exporter and assert every
    fork-child token record carries a lineage_path matching its persisted
    token_lineage_frames rows, and each group_record row round-trips."""
    from elspeth.contracts.audit import TokenRef
    from elspeth.contracts.enums import NodeType
    from elspeth.contracts.schema_contract import SchemaContract
    from elspeth.core.landscape.exporter import LandscapeExporter
    from tests.fixtures.landscape import make_recorder_with_run, register_test_node

    setup = make_recorder_with_run(run_id="run-export-1", source_node_id="source-0", source_plugin_name="csv")
    db, factory = setup.db, setup.factory
    register_test_node(factory.data_flow, setup.run_id, "fork-0", node_type=NodeType.GATE, plugin_name="gate")

    row = factory.data_flow.create_row(
        run_id=setup.run_id,
        source_node_id="source-0",
        row_index=0,
        data={"col": "val"},
        source_row_index=0,
        ingest_sequence=0,
    )
    token = factory.data_flow.create_token(row.row_id)
    children, fork_group_id = factory.data_flow.fork_token(
        parent_ref=TokenRef(token_id=token.token_id, run_id=setup.run_id),
        row_id=row.row_id,
        branches=["a", "b"],
    )
    minimal_contract = SchemaContract(mode="OBSERVED", fields=(), locked=True)
    factory.data_flow.coalesce_tokens(
        parent_refs=[TokenRef(token_id=c.token_id, run_id=setup.run_id) for c in children],
        row_id=row.row_id,
        merged_payload={"merged": True},
        merged_contract=minimal_contract,
    )

    exporter = LandscapeExporter(db)
    records = list(exporter._iter_records(setup.run_id))

    token_records = {r["token_id"]: r for r in records if r["record_type"] == "token"}
    for child in children:
        record = token_records[child.token_id]
        expected_path = [[frame.kind.value, frame.group_id, frame.member_key] for frame in child.lineage_path]
        assert record["lineage_path"] == expected_path
        assert record["lineage_path"][-1][0] == "fork"
        assert record["lineage_path"][-1][1] == fork_group_id

    group_records = [r for r in records if r["record_type"] == "group_record"]
    assert any(g["group_id"] == fork_group_id and g["kind"] == "fork" and g["member_count"] == 2 for g in group_records)
