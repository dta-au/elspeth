"""Epoch-78 admission preserves/refuses its distinct predecessors.

Fixtures are isolated synthetic stores. Historical epochs emulate declared
reader identities; they do not execute old binaries or establish conversion.
The .6 and quota-only epoch-76 fixtures differ in quota DDL, while the separate
graph-review tests establish the retained semantic authority.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import pytest
from sqlalchemy import CheckConstraint, DefaultClause, Index, MetaData, text
from sqlalchemy.schema import Table

from elspeth.web.sessions import schema
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import (
    SESSION_SCHEMA_EPOCH,
    identities_table,
    metadata,
    quota_policies_table,
    quota_provider_attempts_table,
    token_usage_ledger_table,
)
from tests.fixtures.identities import ensure_test_identity

Shape = Literal[
    "current",
    "predecessor",
    "storage_nullable",
    "identity_storage_check",
    "slot_nullable",
    "slot_default",
    "slot_check",
    "singleton_key",
    "singleton_predicate",
]
type Snapshot = tuple[dict[str, bytes], list[tuple], list[tuple], list[tuple]]


def _quota_fixture_table(shape: Shape) -> Table:
    isolated = MetaData()
    identities_table.to_metadata(isolated)
    table = quota_policies_table.to_metadata(isolated)
    if shape in {"predecessor", "identity_storage_check"}:
        check = next(c for c in table.constraints if c.name == "ck_quota_policies_identity_storage_required")
        table.constraints.remove(check)
    if shape in {"predecessor", "slot_check"}:
        check = next(c for c in table.constraints if c.name == "ck_quota_policies_container_slot")
        table.constraints.remove(check)
    if shape in {"predecessor", "storage_nullable"}:
        table.c.storage_bytes.nullable = False
    if shape == "slot_nullable":
        table.c.container_slot.nullable = True
    if shape == "slot_default":
        table.c.container_slot.server_default = DefaultClause("2")
    if shape in {"predecessor", "singleton_key", "singleton_predicate"}:
        index = next(i for i in table.indexes if i.name == "uq_quota_policies_active_container_ceiling")
        table.indexes.remove(index)
        predicate = table.c.revoked_at.is_(None) & table.c.identity_id.is_(None)
        if shape == "singleton_predicate":
            # Restricting to config rows loses cross-actor coverage while
            # retaining all seeded numeric personal/container/history rows.
            predicate = predicate & (table.c.set_by_actor == "config")
        column = table.c.set_by_actor if shape in {"predecessor", "singleton_key"} else table.c.container_slot
        Index(index.name, column, unique=True, sqlite_where=predicate, postgresql_where=predicate)
    if shape == "predecessor":
        table._columns.remove(table.c.container_slot)
    return table


def _source_store(
    path: Path,
    *,
    epoch: int,
    shape: Shape,
    identity_only: bool,
    monkeypatch: pytest.MonkeyPatch,
    journal_mode: Literal["delete", "wal"] = "delete",
) -> None:
    engine = create_session_engine(f"sqlite:///{path}")
    with monkeypatch.context() as creation:
        creation.setattr(schema, "SESSION_SCHEMA_EPOCH", epoch)
        creation.setattr(schema, "_COORDINATION_HARD_CUT_EPOCH", epoch)
        schema.initialize_session_schema(engine)
    table = quota_policies_table
    if shape != "current":
        table = _quota_fixture_table(shape)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE quota_policies"))
            table.create(connection)
    with engine.begin() as connection:
        ensure_test_identity(connection, identity_id="epoch-fixture")
        for policy_id, identity, storage, actor, revoked in [
            ("container", None, 777, "config", None),
            ("personal", "epoch-fixture", 888, "identity", None),
            ("retained", None, 999, "system", datetime(2026, 1, 1, tzinfo=UTC)),
        ]:
            values = {
                "policy_id": policy_id,
                "identity_id": identity,
                "tokens_per_day": 111,
                "storage_bytes": storage,
                "set_by_actor": actor,
                "set_by_identity_id": identity,
                "set_at": datetime(2026, 1, 1, tzinfo=UTC),
                "revoked_at": revoked,
            }
            if "container_slot" in table.c:
                values["container_slot"] = 1
            connection.execute(table.insert().values(**values))
        if identity_only:
            connection.execute(text("PRAGMA user_version = 0"))
    engine.dispose()
    if journal_mode == "delete":
        # Explicitly static synthetic fixtures retain strict whole-family
        # preservation. Operational WAL readers have separate controls below.
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        assert set(_family_bytes(path)) == {path.name}


def _family_bytes(path: Path) -> dict[str, bytes]:
    # Include unexpected family members so no extra sidecar is ignored.
    return {
        candidate.name: candidate.read_bytes()
        for candidate in path.parent.iterdir()
        if candidate.name == path.name or candidate.name.startswith(path.name + "-")
    }


def _snapshot(path: Path, *, mode: Literal["checkpointed-main", "committed-wal"] = "checkpointed-main") -> Snapshot:
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    if mode == "checkpointed-main":
        wal = Path(str(path) + "-wal")
        assert not wal.exists() or wal.stat().st_size == 0, "main-only snapshot requires a checkpointed fixture"
        uri += "&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        objects = connection.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        identity = connection.execute("SELECT * FROM elspeth_schema_identity ORDER BY singleton_id").fetchall()
        policies = connection.execute("SELECT * FROM quota_policies ORDER BY policy_id").fetchall()
    return _family_bytes(path), objects, identity, policies


def _assert_checkpointed_wal_preserved(path: Path, before: Snapshot, after: Snapshot) -> None:
    assert after[1:] == before[1:], "catalog, identity or policy rows changed"
    previous, current = before[0], after[0]
    assert previous.keys() <= current.keys(), "existing family member removed"
    for name, content in previous.items():
        assert current[name] == content, f"existing family bytes changed: {name}"
    added = current.keys() - previous.keys()
    if added:
        assert added == {path.name + "-wal", path.name + "-shm"}, "unexpected new family member"
        assert current[path.name + "-wal"] == b"", "reader created a nonempty WAL"
        # The measured SQLite fixture creates one 32 KiB coordination block.
        # This is a bound for this fixture, not a universal WAL-index format.
        assert len(current[path.name + "-shm"]) == 32768, "unexpected fixture coordination size"


def _assert_live_wal_preserved(path: Path, before: Snapshot, after: Snapshot) -> None:
    assert after[1:] == before[1:], "catalog, identity or policy rows changed"
    previous, current = before[0], after[0]
    assert current.keys() == previous.keys(), "live family member added or removed"
    assert previous[path.name + "-wal"], "live fixture must contain committed WAL content"
    shm_name = path.name + "-shm"
    assert shm_name in previous, "live fixture must already own its coordination file"
    for name, content in previous.items():
        if name == shm_name:
            assert len(current[name]) == len(content), "live coordination allocation changed"
        else:
            assert current[name] == content, f"existing durable family bytes changed: {name}"


@pytest.mark.parametrize(
    ("epoch", "shape"),
    [(75, "predecessor"), (76, "predecessor"), (76, "current"), (77, "current")],
    ids=["main75", "graph-review76", "quota-only76", "integrated77"],
)
@pytest.mark.parametrize("identity_only", [False, True], ids=["sqlite-header", "identity-row"])
@pytest.mark.parametrize("entry", ["factory", "initializer", "probe"])
def test_current_epoch_refuses_predecessor_contracts_without_any_store_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, epoch: int, shape: Shape, identity_only: bool, entry: str
) -> None:
    assert SESSION_SCHEMA_EPOCH == schema._COORDINATION_HARD_CUT_EPOCH == 79
    path = tmp_path / "predecessor.db"
    _source_store(path, epoch=epoch, shape=shape, identity_only=identity_only, monkeypatch=monkeypatch)
    before = _snapshot(path)
    assert [row[3] for row in before[3]] == [777, 888, 999]
    if entry == "factory":
        with pytest.raises(schema.SessionSchemaError, match="Preserve this store unchanged"):
            create_session_engine(f"sqlite:///{path}")
    else:
        # This read-only inspection handle skips writable schema admission;
        # initializer/probe must produce their own refusal below.
        engine = create_session_engine(f"sqlite:///{path}", read_only=True)
        try:
            if entry == "initializer":
                with pytest.raises(schema.SessionSchemaError, match="Preserve this store unchanged"):
                    schema.initialize_session_schema(engine)
            else:
                assert schema.probe_current_schema(engine) is False
        finally:
            engine.dispose()
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "shape",
    ["storage_nullable", "identity_storage_check", "slot_nullable", "slot_default", "slot_check", "singleton_key", "singleton_predicate"],
)
def test_current_epoch_matching_identity_refuses_wrong_quota_shape_and_preserves_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: Shape
) -> None:
    path = tmp_path / "wrong-shape.db"
    _source_store(path, epoch=SESSION_SCHEMA_EPOCH, shape=shape, identity_only=False, monkeypatch=monkeypatch)
    before = _snapshot(path)
    with pytest.raises(schema.SessionSchemaError, match="Preserve this store unchanged"):
        create_session_engine(f"sqlite:///{path}")
    assert _snapshot(path) == before


def test_current_epoch_exact_quota_shape_and_numeric_history_remain_admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "current.db"
    _source_store(path, epoch=SESSION_SCHEMA_EPOCH, shape="current", identity_only=False, monkeypatch=monkeypatch, journal_mode="wal")
    before = _snapshot(path)
    engine = create_session_engine(f"sqlite:///{path}")
    try:
        schema.initialize_session_schema(engine)
        assert schema.probe_current_schema(engine) is True
    finally:
        engine.dispose()
    assert _snapshot(path) == before
    assert quota_policies_table.c.storage_bytes.nullable is True
    assert quota_policies_table.c.container_slot.nullable is False
    assert isinstance(
        next(c for c in quota_policies_table.constraints if c.name == "ck_quota_policies_identity_storage_required"), CheckConstraint
    )


@pytest.mark.parametrize(
    ("table_name", "constraint_name"),
    [
        ("token_usage_ledger", "ck_token_usage_ledger_prompt_nonnegative"),
        ("token_usage_ledger", "ck_token_usage_ledger_run"),
        ("token_usage_ledger", "uq_token_usage_ledger_custody"),
        ("quota_provider_attempts", "fk_quota_provider_attempts_ledger_custody"),
        ("quota_provider_attempts", "fk_quota_provider_attempts_ledger_run"),
        ("quota_provider_attempts", "uq_quota_provider_attempts_ledger_entry"),
    ],
)
def test_current_epoch_refuses_missing_quota_integrity_constraints_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table_name: str, constraint_name: str
) -> None:
    path = tmp_path / "missing-integrity.db"
    _source_store(path, epoch=SESSION_SCHEMA_EPOCH, shape="current", identity_only=False, monkeypatch=monkeypatch)
    replacement = MetaData()
    for table in metadata.tables.values():
        table.to_metadata(replacement)
    changed = replacement.tables[table_name]
    changed.constraints.remove(next(constraint for constraint in changed.constraints if constraint.name == constraint_name))
    engine = create_session_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        quota_provider_attempts_table.drop(conn)
        token_usage_ledger_table.drop(conn)
        replacement.tables["token_usage_ledger"].create(conn)
        replacement.tables["quota_provider_attempts"].create(conn)
    engine.dispose()
    # The intentional mutation used the ordinary WAL writer. Return this
    # synthetic fixture to static mode before measuring refusal preservation.
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    before = _snapshot(path)
    with pytest.raises(schema.SessionSchemaError, match="Preserve this store unchanged"):
        create_session_engine(f"sqlite:///{path}")
    assert _snapshot(path) == before


@pytest.mark.parametrize(("epoch", "shape"), [(75, "predecessor"), (77, "identity_storage_check")])
def test_factory_refusal_preserves_checkpointed_wal_store_and_bounds_reader_coordination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, epoch: int, shape: Shape
) -> None:
    path = tmp_path / "quiescent-wal.db"
    _source_store(path, epoch=epoch, shape=shape, identity_only=False, monkeypatch=monkeypatch, journal_mode="wal")
    before = _snapshot(path)
    assert set(before[0]) == {path.name}
    with pytest.raises(schema.SessionSchemaError, match="Preserve this store unchanged"):
        admitted = create_session_engine(f"sqlite:///{path}")
        admitted.dispose()
    _assert_checkpointed_wal_preserved(path, before, _snapshot(path))


@pytest.mark.parametrize("fault", ["epoch", "quota-shape"])
def test_factory_refusal_observes_committed_wal_facts_and_preserves_live_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, fault: str
) -> None:
    path = tmp_path / "live-wal.db"
    _source_store(path, epoch=SESSION_SCHEMA_EPOCH, shape="current", identity_only=False, monkeypatch=monkeypatch, journal_mode="wal")
    writer_engine = create_session_engine(f"sqlite:///{path}")
    try:
        assert schema.probe_current_schema(writer_engine) is True
        with writer_engine.connect() as writer:
            writer.exec_driver_sql("PRAGMA wal_autocheckpoint=0")
            writer.commit()
            current_main = _snapshot(path)
            with writer.begin():
                if fault == "epoch":
                    writer.exec_driver_sql("PRAGMA user_version=75")
                    writer.exec_driver_sql("UPDATE elspeth_schema_identity SET schema_epoch=75")
                else:
                    policies = [dict(row) for row in writer.execute(quota_policies_table.select()).mappings()]
                    replacement = _quota_fixture_table("identity_storage_check")
                    writer.exec_driver_sql("DROP TABLE quota_policies")
                    replacement.create(writer)
                    writer.execute(replacement.insert(), policies)
            before = _snapshot(path, mode="committed-wal")
            assert before[0][path.name] == current_main[0][path.name], "incompatibility must remain WAL-only"
            assert before[1:] != current_main[1:], "live oracle must observe the committed incompatible facts"
            if fault == "epoch":
                assert writer.exec_driver_sql("PRAGMA user_version").scalar_one() == 75
                expected = "schema version 75"
            else:
                quota_sql = next(sql for kind, name, sql in before[1] if kind == "table" and name == "quota_policies")
                assert "ck_quota_policies_identity_storage_required" not in quota_sql
                expected = "ck_quota_policies_identity_storage_required"
            writer.commit()
            request.node.user_properties.append(("live_wal_incompatible", fault))
            with pytest.raises(schema.SessionSchemaError, match="Preserve this store unchanged") as refusal:
                admitted = create_session_engine(f"sqlite:///{path}")
                admitted.dispose()
            assert expected in str(refusal.value)
            _assert_live_wal_preserved(path, before, _snapshot(path, mode="committed-wal"))
            # Admission leaves the paused synthetic owner usable; only that
            # owner performs this subsequent write, after preservation checks.
            with writer.begin():
                writer.execute(
                    quota_policies_table.update().where(quota_policies_table.c.policy_id == "container").values(tokens_per_day=112)
                )
            continued = _snapshot(path, mode="committed-wal")
            assert continued[3][0][2] == 112
            assert [row[3] for row in continued[3]] == [777, 888, 999]
    finally:
        writer_engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    [
        "main-byte",
        "live-main-byte",
        "existing-wal-byte",
        "existing-wal-removed",
        "new-nonempty-wal",
        "extra-file",
        "catalog",
        "identity-row",
        "policy-row",
    ],
)
def test_wal_preservation_oracles_detect_actual_copy_mutations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    source = tmp_path / "source.db"
    _source_store(source, epoch=75, shape="predecessor", identity_only=False, monkeypatch=monkeypatch, journal_mode="wal")
    live_family = mutation in {"live-main-byte", "existing-wal-byte", "existing-wal-removed"}
    if live_family:
        with closing(sqlite3.connect(source)) as writer:
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE quota_policies SET tokens_per_day=112 WHERE policy_id='container'")
            writer.commit()
            observed = _snapshot(source, mode="committed-wal")
            assert observed[0][source.name + "-wal"]
    else:
        observed = _snapshot(source)
    copied_directory = tmp_path / "copy"
    copied_directory.mkdir()
    path = copied_directory / source.name
    for name, content in observed[0].items():
        (copied_directory / name).write_bytes(content)
    before: Snapshot = (_family_bytes(path), *observed[1:])
    _assert_checkpointed_wal_preserved(path, before, (_family_bytes(path), *observed[1:]))
    if live_family:
        _assert_live_wal_preserved(path, before, (_family_bytes(path), *observed[1:]))
    if mutation in {"main-byte", "live-main-byte"}:
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
    elif mutation == "existing-wal-byte":
        wal_path = Path(str(path) + "-wal")
        content = bytearray(wal_path.read_bytes())
        content[-1] ^= 1
        wal_path.write_bytes(content)
    elif mutation == "existing-wal-removed":
        Path(str(path) + "-wal").unlink()
    elif mutation == "new-nonempty-wal":
        Path(str(path) + "-wal").write_bytes(b"unexpected-write")
        Path(str(path) + "-shm").write_bytes(bytes(32768))
    elif mutation == "extra-file":
        Path(str(path) + "-unexpected").write_bytes(b"unexpected-artifact")
    else:
        with closing(sqlite3.connect(path)) as writer:
            if mutation == "catalog":
                writer.execute("CREATE TABLE unexpected (value TEXT)")
            elif mutation == "identity-row":
                writer.execute("UPDATE elspeth_schema_identity SET schema_epoch=76")
            else:
                writer.execute("UPDATE quota_policies SET storage_bytes=778 WHERE policy_id='container'")
            writer.commit()
    after: Snapshot = _snapshot(path) if mutation in {"catalog", "identity-row", "policy-row"} else (_family_bytes(path), *observed[1:])
    assert after[0] != before[0], "planted filesystem mutation must activate"
    if mutation in {"main-byte", "live-main-byte", "existing-wal-byte"}:
        expected = "existing family bytes changed"
    elif mutation == "existing-wal-removed":
        expected = "existing family member removed"
    elif mutation == "new-nonempty-wal":
        expected = "reader created a nonempty WAL"
    elif mutation == "extra-file":
        expected = "unexpected new family member"
    else:
        expected = "catalog, identity or policy rows changed"
    with pytest.raises(AssertionError, match=expected):
        _assert_checkpointed_wal_preserved(path, before, after)
    if live_family:
        expected = "live family member added or removed" if mutation == "existing-wal-removed" else "existing durable family bytes changed"
        with pytest.raises(AssertionError, match=expected):
            _assert_live_wal_preserved(path, before, after)
