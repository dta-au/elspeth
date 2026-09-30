"""Recovery must distinguish an EOF tear from changed committed evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.journal import LandscapeJournal
from elspeth.core.landscape.schema import sidecar_journal_outbox_table
from tests.unit.core.landscape.test_journal import _insert_outbox_batch, _outbox_records


@pytest.mark.parametrize("fail_on_error", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("journal_batch_ordinal", False),
        ("journal_batch_ordinal", 0.0),
        ("journal_batch_size", True),
        ("journal_batch_size", 1.0),
    ],
)
def test_recovery_refuses_numeric_metadata_impostors_without_appending_or_acknowledging(
    tmp_path: Path, field: str, value: bool | float, fail_on_error: bool
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    sidecar_journal_outbox_table.create(engine)
    path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(path), fail_on_error=fail_on_error)
    records = json.loads(json.dumps(_outbox_records("a" * 32)))
    records[0][field] = value
    _insert_outbox_batch(engine, journal, "a" * 32, records)
    original_bytes = b"{}\n"
    path.write_bytes(original_bytes)
    path.chmod(0o600)
    journal.attach(engine)
    with engine.connect() as conn:
        before = conn.execute(select(sidecar_journal_outbox_table)).all()
    try:
        with pytest.raises(AuditIntegrityError, match="metadata is inconsistent"):
            journal.recover_pending(engine)
        assert path.read_bytes() == original_bytes
        with engine.connect() as conn:
            assert conn.execute(select(sidecar_journal_outbox_table)).all() == before
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "corruption",
    ["invalid-json", "not-list", "empty", "non-object", "missing-id", "wrong-id", "wrong-ordinal", "wrong-size"],
)
def test_recovery_preserves_corrupt_outbox_until_evidence_is_repaired(tmp_path: Path, corruption: str) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    sidecar_journal_outbox_table.create(engine)
    path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(path), fail_on_error=True)
    batch_id = "a" * 32
    records = _outbox_records(batch_id, size=2)
    raw = json.loads(json.dumps(records))
    if corruption == "invalid-json":
        encoded = "["
    elif corruption == "not-list":
        encoded = "{}"
    elif corruption == "empty":
        encoded = "[]"
    elif corruption == "non-object":
        encoded = "[1]"
    else:
        if corruption == "missing-id":
            del raw[0]["journal_batch_id"]
        elif corruption == "wrong-id":
            raw[0]["journal_batch_id"] = "b" * 32
        elif corruption == "wrong-ordinal":
            raw[1]["journal_batch_ordinal"] = 0
        else:
            raw[0]["journal_batch_size"] = 3
        encoded = json.dumps(raw)
    _insert_outbox_batch(engine, journal, batch_id, records)
    with engine.begin() as conn:
        conn.execute(sidecar_journal_outbox_table.update().values(records_json=encoded))
    journal.attach(engine)
    with engine.connect() as conn:
        before = conn.execute(select(sidecar_journal_outbox_table)).all()
    with pytest.raises(AuditIntegrityError):
        journal.recover_pending(engine)
    with engine.connect() as conn:
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == before
    assert not path.exists()
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("UPDATE sidecar_journal_outbox SET records_json = ?", (json.dumps(records),))
        connection.commit()
        cursor.close()
    finally:
        connection.close()
    journal.recover_pending(engine)
    journal.recover_pending(engine)
    assert path.read_text().splitlines() == [journal._serialize_record(record) for record in records]
    with engine.connect() as conn:
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == []
    engine.dispose()


@pytest.mark.parametrize(
    "corruption",
    [
        "missing-ordinal",
        "wrong-size",
        "string-ordinal",
        "wrong-ordinal",
        "changed-content",
        "duplicate",
        "partial-then-unrelated",
        "invalid-utf8",
        "unrecognized-tail",
        "changed-second-tail",
        "complete-with-tail",
    ],
)
def test_recovery_refuses_changed_sidecar_without_acknowledging(tmp_path: Path, corruption: str) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    sidecar_journal_outbox_table.create(engine)
    path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(path), fail_on_error=True)
    batch_id = "a" * 32
    records = _outbox_records(batch_id, size=2)
    _insert_outbox_batch(engine, journal, batch_id, records)
    expected = [journal._serialize_record(record).encode() + b"\n" for record in records]
    first = dict(records[0])
    if corruption == "missing-ordinal":
        del first["journal_batch_ordinal"]
    elif corruption == "wrong-size":
        first["journal_batch_size"] = 3
    elif corruption == "string-ordinal":
        first["journal_batch_ordinal"] = "0"
    elif corruption == "wrong-ordinal":
        first["journal_batch_ordinal"] = 1
    elif corruption == "changed-content":
        first["statement"] = "DELETE FROM rows"
    if corruption in {"missing-ordinal", "wrong-size", "string-ordinal", "wrong-ordinal", "changed-content"}:
        data = json.dumps(first).encode() + b"\n"
    elif corruption == "duplicate":
        data = b"".join(expected + expected)
    elif corruption == "partial-then-unrelated":
        data = expected[0] + b"{}\n"
    elif corruption == "invalid-utf8":
        data = b"\xff\n"
    elif corruption == "unrecognized-tail":
        data = b"garbled"
    elif corruption == "changed-second-tail":
        data = expected[0] + b"garbled"
    else:
        data = b"".join(expected) + b"garbled"
    path.write_bytes(data)
    path.chmod(0o600)
    journal.attach(engine)
    with engine.connect() as conn:
        before = conn.execute(select(sidecar_journal_outbox_table)).all()
    with pytest.raises(AuditIntegrityError):
        journal.recover_pending(engine)
    assert path.read_bytes() == data
    with engine.connect() as conn:
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == before
    path.write_bytes(b"")
    journal.recover_pending(engine)
    journal.recover_pending(engine)
    assert path.read_bytes() == b"".join(expected)
    engine.dispose()


@pytest.mark.parametrize("tear", ["first-record", "after-first-record", "second-record", "complete-without-newline"])
def test_exact_committed_eof_prefix_recovers_once(tmp_path: Path, tear: str) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    sidecar_journal_outbox_table.create(engine)
    path = tmp_path / "journal.jsonl"
    journal = LandscapeJournal(str(path), fail_on_error=True)
    records = _outbox_records("a" * 32, size=2)
    _insert_outbox_batch(engine, journal, "a" * 32, records)
    lines = [journal._serialize_record(record).encode() + b"\n" for record in records]
    prefix = b"\n{}\n"
    if tear == "first-record":
        data = lines[0][:20]
    elif tear == "after-first-record":
        data = lines[0]
    elif tear == "second-record":
        data = lines[0] + lines[1][:20]
    else:
        data = b"".join(lines)[:-1]
    path.write_bytes(prefix + data)
    path.chmod(0o600)
    journal.attach(engine)
    journal.recover_pending(engine)
    journal.recover_pending(engine)
    assert path.read_bytes() == prefix + b"".join(lines)
    with engine.connect() as conn:
        assert conn.execute(select(sidecar_journal_outbox_table)).all() == []
    engine.dispose()
