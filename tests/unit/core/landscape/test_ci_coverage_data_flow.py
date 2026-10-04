"""Fail-closed recovery proofs for durable flow and journal evidence."""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import Column, Engine, Integer, MetaData, String, Table, create_engine, func, select, update

from elspeth.contracts import BatchStatus, NodeType, TerminalOutcome, TerminalPath
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.landscape.batch_lineage import batch_retry_lineage_ids, batch_retry_lineage_ids_on
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.journal import LandscapeJournal
from elspeth.core.landscape.schema import (
    batches_table,
    coalesce_effects_table,
    group_records_table,
    rows_table,
    sidecar_journal_outbox_table,
    token_lineage_frames_table,
    token_outcomes_table,
    token_parents_table,
    tokens_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from tests.fixtures.landscape import RecorderSetup, claim_test_work_item, make_recorder_with_run, register_test_node

_CONTRACT = SchemaContract(mode="OBSERVED", fields=(), locked=True)


@pytest.fixture
def journal_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite:///:memory:")
    sidecar_journal_outbox_table.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _records(batch_id: str, size: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": "2026-01-15T12:00:00+00:00",
            "statement": "INSERT INTO rows (id) VALUES (?)",
            "parameters": [f"row-{ordinal}"],
            "executemany": False,
            "journal_batch_id": batch_id,
            "journal_batch_ordinal": ordinal,
            "journal_batch_size": size,
        }
        for ordinal in range(size)
    ]


def _seed_outbox(engine: Engine, journal: LandscapeJournal, batch_id: str, records_json: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sidecar_journal_outbox_table.insert().values(
                batch_id=batch_id,
                journal_owner=journal._owner_key,
                created_at=datetime.now(UTC),
                records_json=records_json,
            )
        )


@pytest.mark.parametrize(
    "records_json",
    [
        "{",
        "{}",
        "[]",
        "[0]",
        '[{"journal_batch_id": "b"}]',
        json.dumps([{**_records("other", 1)[0]}]),
        json.dumps([{**_records("b", 1)[0], "journal_batch_ordinal": 1}]),
        json.dumps([{**_records("b", 1)[0], "journal_batch_size": 2}]),
    ],
    ids=["invalid-json", "not-list", "empty", "not-record", "missing-metadata", "foreign-id", "ordinal", "size"],
)
def test_corrupt_outbox_is_never_acknowledged(journal_engine: Engine, tmp_path: Path, records_json: str) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=True)
    _seed_outbox(journal_engine, journal, "b", records_json)
    journal.attach(journal_engine)

    with pytest.raises(AuditIntegrityError, match="sidecar journal outbox"):
        journal.recover_pending(journal_engine)

    assert not journal_path.exists()
    with journal_engine.connect() as conn:
        assert conn.scalar(select(sidecar_journal_outbox_table.c.records_json)) == records_json


@pytest.mark.parametrize(
    "corruption",
    [
        "partial-middle",
        "missing-metadata",
        "wrong-size",
        "wrong-ordinal-type",
        "wrong-content",
        "duplicate",
        "unrelated-tail",
        "wrong-prefix",
    ],
)
def test_existing_sidecar_corruption_preserves_file_and_outbox(journal_engine: Engine, tmp_path: Path, corruption: str) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=True)
    records = _records("b")
    first, second = [json.dumps(record) + "\n" for record in records]
    if corruption == "partial-middle":
        content = first + json.dumps({"unrelated": True}) + "\n" + second
    elif corruption == "missing-metadata":
        content = json.dumps({"journal_batch_id": "b"}) + "\n"
    elif corruption == "wrong-size":
        content = json.dumps({**records[0], "journal_batch_size": 3}) + "\n"
    elif corruption == "wrong-ordinal-type":
        content = json.dumps({**records[0], "journal_batch_ordinal": "0"}) + "\n"
    elif corruption == "wrong-content":
        content = json.dumps({**records[0], "parameters": ["changed"]}) + "\n"
    elif corruption == "duplicate":
        content = first + second + first
    elif corruption == "unrelated-tail":
        content = first + second + "garbage"
    else:
        content = first + "unrelated"
    journal_path.write_text(content, encoding="utf-8")
    journal_path.chmod(0o600)
    _seed_outbox(journal_engine, journal, "b", json.dumps(records))
    journal.attach(journal_engine)

    with pytest.raises(AuditIntegrityError, match="Landscape journal"):
        journal.recover_pending(journal_engine)

    assert journal_path.read_text(encoding="utf-8") == content
    with journal_engine.connect() as conn:
        assert conn.scalar(select(sidecar_journal_outbox_table.c.batch_id)) == "b"


def test_recovery_repairs_second_record_torn_tail_and_skips_blank_lines(journal_engine: Engine, tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=True)
    records = _records("b")
    first, second = [json.dumps(record) + "\n" for record in records]
    journal_path.write_text("\n" + first + second[:20], encoding="utf-8")
    journal_path.chmod(0o600)
    _seed_outbox(journal_engine, journal, "b", json.dumps(records))
    journal.attach(journal_engine)

    journal.recover_pending(journal_engine)
    journal.recover_pending(journal_engine)

    assert journal_path.read_text(encoding="utf-8") == "\n" + first + second
    with journal_engine.connect() as conn:
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == []


def test_ack_failure_keeps_published_batch_recoverable_without_duplicate(journal_engine: Engine, tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=True)
    _seed_outbox(journal_engine, journal, "b", json.dumps(_records("b")))
    journal.attach(journal_engine)
    original_commit = journal._dialect_do_commit
    assert original_commit is not None

    def refuse_ack(connection: object) -> None:
        raise OSError("acknowledgement unavailable")

    with patch.object(journal, "_dialect_do_commit", refuse_ack), pytest.raises(OSError, match="acknowledgement unavailable"):
        journal.recover_pending(journal_engine)
    published = journal_path.read_bytes()
    with journal_engine.connect() as conn:
        assert conn.scalar(select(sidecar_journal_outbox_table.c.batch_id)) == "b"

    journal.recover_pending(journal_engine)

    assert journal_path.read_bytes() == published
    with journal_engine.connect() as conn:
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == []


@pytest.mark.parametrize("operation", ["read", "read-write"])
def test_recovery_readers_refuse_public_permissions(tmp_path: Path, operation: str) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal_path.write_text("sensitive audit data", encoding="utf-8")
    journal_path.chmod(0o644)
    journal = LandscapeJournal(str(journal_path), fail_on_error=True)

    with pytest.raises(PermissionError, match="owner-only"):
        if operation == "read":
            journal._open_owner_only_read()
        else:
            journal._open_owner_only_read_write()

    assert journal_path.read_text(encoding="utf-8") == "sensitive audit data"


def test_savepoint_release_and_rollback_publish_only_committed_rows(journal_engine: Engine, tmp_path: Path) -> None:
    table = Table("journal_rows", MetaData(), Column("id", Integer, primary_key=True))
    table.create(journal_engine)
    journal_path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=True)
    journal.attach(journal_engine)

    with journal_engine.begin() as conn:
        conn.execute(table.insert().values(id=1))
        discarded = conn.begin_nested()
        conn.execute(table.insert().values(id=2))
        discarded.rollback()
        with conn.begin_nested():
            conn.execute(table.insert().values(id=3))

    records = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    assert [record["parameters"] for record in records] == [[1], [3]]
    assert {record["journal_batch_size"] for record in records} == {2}
    with journal_engine.connect() as conn:
        assert conn.scalars(select(table.c.id).order_by(table.c.id)).all() == [1, 3]
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == []


@pytest.mark.parametrize("strict", [True, False], ids=["strict-rollback", "explicit-error-witness"])
def test_unreadable_call_payload_never_silently_disappears(journal_engine: Engine, tmp_path: Path, strict: bool) -> None:
    payload_path = tmp_path / "payloads"
    payload_store = FilesystemPayloadStore(payload_path)
    invalid_utf8_ref = payload_store.store(b"\xff")
    missing_ref = "a" * 64
    table = Table(
        "calls", MetaData(), Column("id", Integer, primary_key=True), Column("request_ref", String), Column("response_ref", String)
    )
    table.create(journal_engine)
    journal_path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=strict, include_payloads=True, payload_base_path=str(payload_path))
    journal.attach(journal_engine)

    def write_call() -> None:
        with journal_engine.begin() as conn:
            conn.execute(table.insert().values(id=1, request_ref=invalid_utf8_ref, response_ref=missing_ref))

    if strict:
        with pytest.raises(UnicodeDecodeError):
            write_call()
        assert not journal_path.exists()
        with journal_engine.connect() as conn:
            assert conn.scalar(select(table.c.id)) is None
            assert conn.execute(select(sidecar_journal_outbox_table)).all() == []
    else:
        write_call()
        record = json.loads(journal_path.read_text(encoding="utf-8"))
        assert record["request_ref"] == invalid_utf8_ref
        assert record["response_ref"] == missing_ref
        assert record["request_payload"] is None
        assert record["response_payload"] is None
        assert record["request_payload_error"].startswith("payload_decode_failed:")
        assert record["response_payload_error"].startswith("payload_read_failed:")
        assert "_payload_ref_columns" not in record


def test_raw_driver_sql_with_payload_capture_commits_without_inventing_call_refs(journal_engine: Engine, tmp_path: Path) -> None:
    table = Table("journal_rows", MetaData(), Column("id", Integer, primary_key=True))
    table.create(journal_engine)
    journal_path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=True, include_payloads=True, payload_base_path=str(tmp_path / "payloads"))
    journal.attach(journal_engine)

    with journal_engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO journal_rows(id) VALUES (?)", (1,))

    record = json.loads(journal_path.read_text(encoding="utf-8"))
    assert record["parameters"] == [1]
    assert set(record) == {
        "timestamp",
        "statement",
        "parameters",
        "executemany",
        "journal_batch_id",
        "journal_batch_ordinal",
        "journal_batch_size",
    }


@pytest.mark.parametrize("kind", ["file", "symlink", "public-directory", "foreign-owner"])
def test_owner_only_directory_check_refuses_unsafe_ancestor(tmp_path: Path, kind: str) -> None:
    directory = tmp_path / "ancestor"
    if kind == "file":
        directory.write_text("not a directory", encoding="utf-8")
    elif kind == "symlink":
        directory.symlink_to(tmp_path, target_is_directory=True)
    else:
        directory.mkdir(mode=0o755 if kind == "public-directory" else 0o700)
        if kind == "public-directory":
            # mkdir's mode is filtered by umask; construct the actual unsafe input.
            directory.chmod(0o755)
            assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    if kind == "foreign-owner":
        # The OS stat result remains real; a process with a different uid
        # must refuse this directory instead of accepting another owner's data.
        with (
            patch("elspeth.core.landscape.journal.os.getuid", return_value=directory.stat().st_uid + 1),
            pytest.raises(PermissionError, match="owned by"),
        ):
            LandscapeJournal._verify_owner_only_dir(directory)
    else:
        with pytest.raises(OSError, match=r"directory|owner-only"):
            LandscapeJournal._verify_owner_only_dir(directory)


def test_disabled_journal_retains_outbox_until_periodic_recovery(journal_engine: Engine, tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=False)
    _seed_outbox(journal_engine, journal, "b", json.dumps(_records("b", 1)))
    journal.attach(journal_engine)
    with patch.object(journal, "_open_owner_only_append", side_effect=OSError("storage unavailable"), autospec=True):
        for _attempt in range(journal._MAX_CONSECUTIVE_FAILURES):
            journal.recover_pending(journal_engine)
    assert journal._disabled is True

    journal.recover_pending(journal_engine)
    assert not journal_path.exists()
    with journal_engine.connect() as conn:
        assert conn.scalar(select(sidecar_journal_outbox_table.c.batch_id)) == "b"
    for _attempt in range(100 - journal._total_dropped):
        journal.recover_pending(journal_engine)

    assert journal._disabled is False
    assert len(journal_path.read_text(encoding="utf-8").splitlines()) == 1
    with journal_engine.connect() as conn:
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == []


def test_journal_refuses_second_attachment_and_recovery_on_another_engine(journal_engine: Engine, tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(journal_path), fail_on_error=True)
    journal.attach(journal_engine)
    with pytest.raises(RuntimeError, match="already attached"):
        journal.attach(journal_engine)
    other_engine = create_engine("sqlite:///:memory:")
    try:
        with pytest.raises(RuntimeError, match="attached to this engine"):
            journal.recover_pending(other_engine)
    finally:
        other_engine.dispose()
    assert not journal_path.exists()


def test_owner_only_file_reader_refuses_another_owners_content(tmp_path: Path) -> None:
    journal_path = tmp_path / "journal.jsonl"
    journal_path.write_text("sensitive audit data", encoding="utf-8")
    journal_path.chmod(0o600)
    journal = LandscapeJournal(str(journal_path), fail_on_error=True)
    with (
        patch("elspeth.core.landscape.journal.os.getuid", return_value=journal_path.stat().st_uid + 1),
        pytest.raises(PermissionError, match="owned by"),
    ):
        journal._open_owner_only_read()
    assert journal_path.read_text(encoding="utf-8") == "sensitive audit data"


@pytest.fixture
def flow_setup() -> Iterator[RecorderSetup]:
    setup = make_recorder_with_run()
    register_test_node(setup.data_flow, setup.run_id, "aggregate", node_type=NodeType.AGGREGATION)
    try:
        yield setup
    finally:
        setup.db.close()


@pytest.mark.parametrize("on_connection", [True, False], ids=["transaction-writer", "restore-reader"])
@pytest.mark.parametrize("corruption", ["missing", "wrong-run", "wrong-node", "cycle", "valid"])
def test_retry_chain_readers_refuse_foreign_or_cyclic_evidence(flow_setup: RecorderSetup, on_connection: bool, corruption: str) -> None:
    setup = flow_setup
    original = setup.execution.create_batch(coordination_token=setup.coordination_token, aggregation_node_id="aggregate")
    setup.execution.complete_batch(coordination_token=setup.coordination_token, batch_id=original.batch_id, status=BatchStatus.FAILED)
    retry = setup.execution.retry_batch(original.batch_id, coordination_token=setup.coordination_token)
    ancestor_id = original.batch_id
    expected_run_id = setup.run_id
    expected_node_id = "aggregate"
    if corruption == "missing":
        ancestor_id = "missing-batch"
    elif corruption == "wrong-run":
        expected_run_id = "another-run"
    elif corruption == "wrong-node":
        expected_node_id = "another-node"
    elif corruption == "cycle":
        # The SQL FK admits a cycle; the reader must refuse it, not loop.
        with setup.db.engine.begin() as conn:
            conn.execute(
                update(batches_table).where(batches_table.c.batch_id == original.batch_id).values(retry_of_batch_id=retry.batch_id)
            )

    with setup.db.connection() as conn:

        def read_chain() -> tuple[str, ...]:
            arguments = {
                "batch_id": retry.batch_id,
                "run_id": expected_run_id,
                "aggregation_node_id": expected_node_id,
                "retry_of_batch_id": ancestor_id,
            }
            if on_connection:
                return batch_retry_lineage_ids_on(conn, **arguments)
            return batch_retry_lineage_ids(lambda query: conn.execute(query).one_or_none(), **arguments)

        if corruption == "valid":
            assert read_chain() == (retry.batch_id, original.batch_id)
        else:
            with pytest.raises(AuditIntegrityError, match="retry"):
                read_chain()


def _source_token(setup: RecorderSetup):
    return setup.data_flow.create_row_with_token(
        coordination_token=setup.coordination_token,
        source_node_id=setup.source_node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"value": 1},
    )


def _fork(setup: RecorderSetup, row, parent):
    member = setup.coordination_token.membership
    claim = claim_test_work_item(setup.factory, member_token=member, token_id=parent.token_id, node_id=setup.source_node_id)
    return setup.data_flow.fork_token(
        parent_ref=TokenRef(parent.token_id, setup.run_id),
        row_id=row.row_id,
        branches=["left", "right"],
        member_token=member,
        work_item=claim,
    )


@pytest.mark.parametrize(
    ("verb", "table_name"),
    [
        ("source", "rows"),
        ("source", "tokens"),
        ("fork", "tokens"),
        ("fork", "token_parents"),
        ("fork", "token_lineage_frames"),
        ("fork", "group_records"),
        ("fork", "token_outcomes"),
        ("expand", "tokens"),
        ("expand", "token_parents"),
        ("expand", "token_lineage_frames"),
        ("expand", "group_records"),
        ("expand", "token_outcomes"),
        ("empty-expand", "group_records"),
    ],
)
def test_database_silent_insert_refusal_rolls_back_entire_opener(flow_setup: RecorderSetup, verb: str, table_name: str) -> None:
    setup = flow_setup
    row, parent = _source_token(setup)
    claim = claim_test_work_item(
        setup.factory,
        member_token=setup.coordination_token.membership,
        token_id=parent.token_id,
        node_id=setup.source_node_id,
    )
    tables = (rows_table, tokens_table, token_parents_table, token_lineage_frames_table, group_records_table, token_outcomes_table)
    with setup.db.engine.begin() as conn:
        before = tuple(conn.scalar(select(func.count()).select_from(table)) for table in tables)
        # SQLite genuinely reports zero affected rows: this is not a mocked
        # result object. No partial lineage or source acceptance may survive.
        conn.exec_driver_sql(f"CREATE TRIGGER refuse_insert BEFORE INSERT ON {table_name} BEGIN SELECT RAISE(IGNORE); END")

    with pytest.raises(AuditIntegrityError, match=r"zero rows|incomplete batch"):
        if verb == "source":
            setup.data_flow.create_row_with_token(
                coordination_token=setup.coordination_token,
                source_node_id=setup.source_node_id,
                row_index=1,
                source_row_index=1,
                ingest_sequence=1,
                data={"value": 2},
            )
        elif verb == "fork":
            setup.data_flow.fork_token(
                parent_ref=TokenRef(parent.token_id, setup.run_id),
                row_id=row.row_id,
                branches=["left", "right"],
                member_token=setup.coordination_token.membership,
                work_item=claim,
            )
        elif verb == "expand":
            setup.data_flow.expand_token(
                parent_ref=TokenRef(parent.token_id, setup.run_id),
                row_id=row.row_id,
                child_payloads=[{"value": 1}, {"value": 2}],
                output_contract=_CONTRACT,
                member_token=setup.coordination_token.membership,
            )
        else:
            setup.data_flow.record_empty_expansion(
                TokenRef(parent.token_id, setup.run_id), member_token=setup.coordination_token.membership
            )

    with setup.db.connection() as conn:
        after = tuple(conn.scalar(select(func.count()).select_from(table)) for table in tables)
    assert after == before


@pytest.mark.parametrize("divergence", ["payload", "contract", "step", "parent-links"])
def test_coalesce_replay_refuses_divergence_without_materializing_again(flow_setup: RecorderSetup, divergence: str) -> None:
    setup = flow_setup
    register_test_node(setup.data_flow, setup.run_id, "merge", node_type=NodeType.COALESCE)
    row, parent = _source_token(setup)
    children, _group = _fork(setup, row, parent)
    refs = [TokenRef(child.token_id, setup.run_id) for child in children]
    merged = setup.data_flow.coalesce_tokens(
        parent_refs=refs,
        row_id=row.row_id,
        merged_payload={"value": 1},
        merged_contract=_CONTRACT,
        coordination_token=setup.coordination_token,
        coalesce_node_id="merge",
        step_in_pipeline=2,
    )
    if divergence == "parent-links":
        with setup.db.engine.begin() as conn:
            conn.execute(token_parents_table.delete().where(token_parents_table.c.token_id == merged.token_id))
    contract = SchemaContract(mode="OBSERVED", fields=(), locked=False) if divergence == "contract" else _CONTRACT
    with pytest.raises(AuditIntegrityError, match=r"divergent|differs"):
        setup.data_flow.coalesce_tokens(
            parent_refs=refs,
            row_id=row.row_id,
            merged_payload={"value": 2 if divergence == "payload" else 1},
            merged_contract=contract,
            coordination_token=setup.coordination_token,
            coalesce_node_id="merge",
            step_in_pipeline=3 if divergence == "step" else 2,
        )
    with setup.db.connection() as conn:
        assert conn.scalar(select(func.count()).select_from(coalesce_effects_table)) == 1
        assert conn.scalar(select(func.count()).select_from(tokens_table)) == 4


def test_expansion_refuses_decided_parent(flow_setup: RecorderSetup) -> None:
    setup = flow_setup
    row, parent = _source_token(setup)
    setup.data_flow.record_token_outcome_leader(
        coordination_token=setup.coordination_token,
        ref=TokenRef(parent.token_id, setup.run_id),
        outcome=TerminalOutcome.SUCCESS,
        path=TerminalPath.COALESCED,
    )
    with pytest.raises(AuditIntegrityError, match="already has a terminal"):
        setup.data_flow.expand_token(
            parent_ref=TokenRef(parent.token_id, setup.run_id),
            row_id=row.row_id,
            child_payloads=[{"value": 2}],
            output_contract=_CONTRACT,
            member_token=setup.coordination_token.membership,
        )
    assert setup.query.get_token_parents(parent.token_id) == []
    with setup.db.connection() as conn:
        assert conn.scalar(select(func.count()).select_from(tokens_table)) == 1


@pytest.mark.parametrize("corruption", ["extra-open-group", "different-outer-path"])
def test_coalesce_refuses_to_erase_unrelated_lineage(flow_setup: RecorderSetup, corruption: str) -> None:
    setup = flow_setup
    row, parent = _source_token(setup)
    children, group_id = _fork(setup, row, parent)
    with setup.db.engine.begin() as conn:
        if corruption == "different-outer-path":
            conn.execute(
                update(token_lineage_frames_table).where(token_lineage_frames_table.c.token_id == children[1].token_id).values(depth=1)
            )
            depth = 0
        else:
            depth = 1
        conn.execute(
            token_lineage_frames_table.insert().values(
                token_id=children[1].token_id,
                run_id=setup.run_id,
                depth=depth,
                kind="fork",
                group_id=group_id,
                member_key="unrelated",
            )
        )
    with pytest.raises(AuditIntegrityError, match=r"strict pop|remaining lineage"):
        setup.data_flow.coalesce_tokens(
            parent_refs=[TokenRef(child.token_id, setup.run_id) for child in children],
            row_id=row.row_id,
            merged_payload={"value": 1},
            merged_contract=_CONTRACT,
            coordination_token=setup.coordination_token,
        )
    with setup.db.connection() as conn:
        assert conn.scalar(select(func.count()).select_from(coalesce_effects_table)) == 0
        assert conn.scalar(select(func.count()).select_from(tokens_table)) == 3


@pytest.mark.parametrize("empty_first", [True, False], ids=["empty-to-output", "output-to-empty"])
def test_collector_replay_cannot_change_release_cardinality(flow_setup: RecorderSetup, empty_first: bool) -> None:
    setup = flow_setup
    row, parent = _source_token(setup)
    members, group_id = setup.data_flow.expand_token(
        parent_ref=TokenRef(parent.token_id, setup.run_id),
        row_id=row.row_id,
        child_payloads=[{"value": 1}, {"value": 2}],
        output_contract=_CONTRACT,
        member_token=setup.coordination_token.membership,
    )
    refs = [TokenRef(member.token_id, setup.run_id) for member in members]
    release = setup.data_flow.collect_tokens(
        member_refs=refs,
        group_id=group_id,
        collector_node_id="collector",
        output_payloads=[] if empty_first else [{"value": 3}],
        output_contracts=[] if empty_first else [_CONTRACT],
        coordination_token=setup.coordination_token,
    )
    with pytest.raises(AuditIntegrityError, match="divergent collect replay"):
        setup.data_flow.collect_tokens(
            member_refs=refs,
            group_id=group_id,
            collector_node_id="collector",
            output_payloads=[{"value": 3}] if empty_first else [],
            output_contracts=[_CONTRACT] if empty_first else [],
            coordination_token=setup.coordination_token,
        )
    groups = setup.data_flow.get_group_records_for_run(setup.run_id)
    assert {group["group_id"] for group in groups} == {group_id, release.release_group_id}


@pytest.mark.parametrize("query_kind", ["ambiguous", "missing-table", "missing-lock-table"])
def test_outcome_queries_refuse_ambiguous_or_unavailable_evidence(flow_setup: RecorderSetup, query_kind: str) -> None:
    setup = flow_setup
    original = setup.execution.create_batch(coordination_token=setup.coordination_token, aggregation_node_id="aggregate")
    setup.execution.complete_batch(coordination_token=setup.coordination_token, batch_id=original.batch_id, status=BatchStatus.FAILED)
    setup.execution.retry_batch(original.batch_id, coordination_token=setup.coordination_token)
    with setup.db.connection() as conn, pytest.raises(LandscapeRecordError, match=r"ambiguous|database rejected"):
        if query_kind == "ambiguous":
            setup.data_flow.outcomes._execute_fetchone(select(batches_table.c.batch_id), conn=conn)
        else:
            absent = Table("absent_evidence", MetaData(), Column("id", Integer))
            if query_kind == "missing-lock-table":
                setup.data_flow.outcomes._execute_lock_query(conn, select(absent.c.id), operation="evidence lock")
            else:
                setup.data_flow.outcomes._execute_fetchone(select(absent.c.id), conn=conn)


@pytest.mark.parametrize("invalid", ["empty", "duplicates", "state-arity", "state-empty", "state-duplicates"])
def test_coalesce_refuses_invalid_parent_roster_before_writing(flow_setup: RecorderSetup, invalid: str) -> None:
    setup = flow_setup
    row, parent = _source_token(setup)
    children, _group = _fork(setup, row, parent)
    refs = [TokenRef(child.token_id, setup.run_id) for child in children]
    states = None
    if invalid == "empty":
        refs = []
    elif invalid == "duplicates":
        refs = [refs[0], refs[0]]
    elif invalid == "state-arity":
        states = ["one-state"]
    elif invalid == "state-empty":
        states = ["one-state", ""]
    else:
        states = ["one-state", "one-state"]
    with pytest.raises(AuditIntegrityError, match=r"parent|identities"):
        setup.data_flow.coalesce_tokens(
            parent_refs=refs,
            parent_state_ids=states,
            row_id=row.row_id,
            merged_payload={"value": 1},
            merged_contract=_CONTRACT,
            coordination_token=setup.coordination_token,
        )
    with setup.db.connection() as conn:
        assert conn.scalar(select(func.count()).select_from(coalesce_effects_table)) == 0
        assert conn.scalar(select(func.count()).select_from(tokens_table)) == 3


@pytest.mark.parametrize("empty_release", [True, False])
def test_collector_silent_group_insert_refusal_rolls_back_children(flow_setup: RecorderSetup, empty_release: bool) -> None:
    setup = flow_setup
    row, parent = _source_token(setup)
    members, group_id = setup.data_flow.expand_token(
        parent_ref=TokenRef(parent.token_id, setup.run_id),
        row_id=row.row_id,
        child_payloads=[{"value": 1}, {"value": 2}],
        output_contract=_CONTRACT,
        member_token=setup.coordination_token.membership,
    )
    with setup.db.engine.begin() as conn:
        conn.exec_driver_sql("CREATE TRIGGER refuse_insert BEFORE INSERT ON group_records BEGIN SELECT RAISE(IGNORE); END")
    with pytest.raises(AuditIntegrityError, match="zero rows"):
        setup.data_flow.collect_tokens(
            member_refs=[TokenRef(member.token_id, setup.run_id) for member in members],
            group_id=group_id,
            collector_node_id="collector",
            output_payloads=[] if empty_release else [{"value": 3}],
            output_contracts=[] if empty_release else [_CONTRACT],
            coordination_token=setup.coordination_token,
        )
    with setup.db.connection() as conn:
        assert conn.scalar(select(func.count()).select_from(tokens_table)) == 3
        assert conn.scalar(select(func.count()).select_from(group_records_table)) == 1


@pytest.mark.parametrize("verb", ["coalesce", "expand", "collect"])
def test_payload_store_identity_violation_cannot_create_lineage(flow_setup: RecorderSetup, verb: str) -> None:
    setup = flow_setup
    row, parent = _source_token(setup)
    if verb == "coalesce":
        members, group_id = _fork(setup, row, parent)
    elif verb == "collect":
        members, group_id = setup.data_flow.expand_token(
            parent_ref=TokenRef(parent.token_id, setup.run_id),
            row_id=row.row_id,
            child_payloads=[{"value": 1}, {"value": 2}],
            output_contract=_CONTRACT,
            member_token=setup.coordination_token.membership,
        )
    else:
        members, group_id = [], ""
    store = setup.data_flow.tokens._payload_store
    assert store is not None
    with (
        patch.object(store, "store", return_value="a" * 64, autospec=True),
        pytest.raises(AuditIntegrityError, match=r"content.address|SHA-256"),
    ):
        if verb == "coalesce":
            setup.data_flow.coalesce_tokens(
                parent_refs=[TokenRef(member.token_id, setup.run_id) for member in members],
                row_id=row.row_id,
                merged_payload={"value": 3},
                merged_contract=_CONTRACT,
                coordination_token=setup.coordination_token,
            )
        elif verb == "expand":
            setup.data_flow.expand_token(
                parent_ref=TokenRef(parent.token_id, setup.run_id),
                row_id=row.row_id,
                child_payloads=[{"value": 3}],
                output_contract=_CONTRACT,
                member_token=setup.coordination_token.membership,
            )
        else:
            setup.data_flow.collect_tokens(
                member_refs=[TokenRef(member.token_id, setup.run_id) for member in members],
                group_id=group_id,
                collector_node_id="collector",
                output_payloads=[{"value": 3}],
                output_contracts=[_CONTRACT],
                coordination_token=setup.coordination_token,
            )
    with setup.db.connection() as conn:
        assert conn.scalar(select(func.count()).select_from(tokens_table)) == (1 if verb == "expand" else 3)
        assert conn.scalar(select(func.count()).select_from(coalesce_effects_table)) == 0
