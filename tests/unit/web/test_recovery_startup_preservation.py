"""Synthetic-only preservation controls for recovery-era startup admission."""

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from elspeth.core.landscape.database import LandscapeDB, SchemaCompatibilityError
from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
from elspeth.web.auth.local import LocalAuthStorageSecurityError, validate_existing_local_auth_store
from elspeth.web.auth.local_schema import AUTH_SCHEMA_STATEMENTS, LocalAuthSchemaError
from elspeth.web.local_store_admission import admit_local_stores
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import SESSION_DB_APPLICATION_ID, SESSION_SCHEMA_EPOCH
from elspeth.web.sessions.schema import SessionSchemaError, initialize_session_schema, probe_current_schema


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


def _snapshot(path: Path):
    with closing(sqlite3.connect(path)) as connection:
        return (
            tuple(connection.iterdump()),
            connection.execute("PRAGMA application_id").fetchone()[0],
            connection.execute("PRAGMA user_version").fetchone()[0],
            connection.execute("PRAGMA journal_mode").fetchone()[0],
        )


def _unsupported(path: Path, kind: str, *, current_epoch: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        if kind in {"old72", "old73", "future"}:
            epoch = {"old72": 72, "old73": 73, "future": current_epoch + 1}[kind]
            connection.execute(f"PRAGMA user_version = {epoch}")
            connection.execute("CREATE TABLE retained_user_data (value TEXT NOT NULL)")
            connection.execute("INSERT INTO retained_user_data VALUES ('keep this row')")
        elif kind == "foreign":
            connection.execute("PRAGMA application_id = 12345")
            connection.execute("CREATE TABLE retained_user_data (value TEXT NOT NULL)")
            connection.execute("INSERT INTO retained_user_data VALUES ('keep this row')")
        elif kind == "partial":
            connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO sessions VALUES ('retained-session')")
        elif kind == "stamped-empty":
            connection.execute(f"PRAGMA user_version = {current_epoch}")
        elif kind == "foreign-empty":
            connection.execute("PRAGMA application_id = 12345")
        elif kind == "views-only":
            connection.execute("CREATE VIEW retained_view AS SELECT 'retained' AS value")
        else:
            raise AssertionError(f"Unknown synthetic corruption {kind}")
        connection.commit()
    path.chmod(0o600)


@pytest.mark.parametrize("kind", ["old72", "old73", "future", "foreign", "partial", "stamped-empty", "foreign-empty", "views-only"])
def test_session_writer_admission_refuses_before_wal_or_bootstrap(tmp_path, kind):
    path = tmp_path / "sessions.db"
    _unsupported(path, kind, current_epoch=SESSION_SCHEMA_EPOCH)
    before = _snapshot(path)
    assert before[-1] == "delete"
    with pytest.raises(SessionSchemaError):
        engine = create_session_engine(_url(path))
        try:
            initialize_session_schema(engine)
        finally:
            engine.dispose()
    assert _snapshot(path) == before


@pytest.mark.parametrize("kind", ["foreign", "partial", "stamped-empty", "foreign-empty", "views-only", "future"])
def test_landscape_writer_refuses_without_header_schema_or_row_changes(tmp_path, kind):
    path = tmp_path / "audit.db"
    _unsupported(path, kind, current_epoch=SQLITE_SCHEMA_EPOCH)
    before = _snapshot(path)
    with pytest.raises(SchemaCompatibilityError), LandscapeDB.from_url(_url(path)):
        pass
    assert _snapshot(path) == before


@pytest.mark.parametrize("broken_store", ["session", "landscape", "auth"])
def test_all_store_admission_refuses_before_creating_other_targets(tmp_path, broken_store, closed_local_settings):
    data_dir = tmp_path / "data"
    paths = {"session": data_dir / "sessions.db", "landscape": data_dir / "runs" / "audit.db", "auth": data_dir / "auth.db"}
    path = paths[broken_store]
    _unsupported(path, "foreign", current_epoch=SESSION_SCHEMA_EPOCH)
    settings = closed_local_settings.model_copy(
        update={"data_dir": data_dir, "session_db_url": _url(paths["session"]), "landscape_url": _url(paths["landscape"])}
    )
    before = _snapshot(path)
    with pytest.raises((SessionSchemaError, SchemaCompatibilityError, LocalAuthSchemaError)):
        admit_local_stores(settings)
    assert _snapshot(path) == before
    for name, other in paths.items():
        if name != broken_store:
            assert not other.exists()


def test_absent_targets_admit_without_creation_then_fresh_current_stores_reopen(tmp_path, closed_local_settings):
    settings = closed_local_settings.model_copy(update={"data_dir": tmp_path / "data"})
    admit_local_stores(settings)
    assert not settings.data_dir.exists()
    settings.data_dir.mkdir()
    (settings.data_dir / "runs").mkdir()
    engine = create_session_engine(settings.get_session_db_url())
    try:
        initialize_session_schema(engine)
        assert probe_current_schema(engine)
    finally:
        engine.dispose()
    with LandscapeDB.from_url(settings.get_landscape_url()):
        pass
    auth_path = settings.data_dir / "auth.db"
    with closing(sqlite3.connect(auth_path)) as connection:
        for statement in AUTH_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute("INSERT INTO users VALUES ('kept-user', 'synthetic-hash', 'Kept', NULL, 1)")
        connection.commit()
    auth_path.chmod(0o600)
    before = _snapshot(auth_path)
    admit_local_stores(settings)
    validate_existing_local_auth_store(auth_path)
    assert _snapshot(auth_path) == before


def test_readonly_session_probe_sees_committed_wal_and_rejects_writes(tmp_path):
    path = tmp_path / "sessions # escaped.db"
    engine = create_session_engine(_url(path))
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA wal_autocheckpoint=0")
        initialize_session_schema(engine)
        assert Path(f"{path}-wal").stat().st_size > 0
        # Negative control: the physical main file lacks the committed WAL
        # schema. Using immutable=1 for the actual probe would misclassify it.
        with closing(sqlite3.connect(f"file:{quote(str(path))}?immutable=1", uri=True)) as main_file:
            assert main_file.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
        reader = create_session_engine(_url(path), read_only=True)
        try:
            assert probe_current_schema(reader)
            with reader.connect() as connection:
                assert connection.execute(text("PRAGMA application_id")).scalar_one() == SESSION_DB_APPLICATION_ID
                with pytest.raises(OperationalError, match="readonly"):
                    connection.exec_driver_sql("CREATE TABLE forbidden (value TEXT)")
        finally:
            reader.dispose()
    finally:
        engine.dispose()


@pytest.mark.parametrize("store", ["session", "landscape"])
def test_uri_url_cannot_bypass_preservation_admission(tmp_path, store):
    path = tmp_path / "foreign.db"
    _unsupported(path, "foreign", current_epoch=SESSION_SCHEMA_EPOCH)
    before = _snapshot(path)
    uri_url = f"sqlite:///file:{path}?uri=true"
    with pytest.raises((ValueError, SessionSchemaError, SchemaCompatibilityError)):
        if store == "session":
            engine = create_session_engine(uri_url)
            engine.dispose()
        else:
            with LandscapeDB.from_url(uri_url):
                pass
    assert _snapshot(path) == before


def test_dangling_auth_symlink_is_refused_before_other_store_creation(tmp_path, closed_local_settings):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    auth_path = data_dir / "auth.db"
    target = tmp_path / "missing-credential-target.db"
    auth_path.symlink_to(target)
    settings = closed_local_settings.model_copy(update={"data_dir": data_dir})
    with pytest.raises(LocalAuthStorageSecurityError, match="symlink"):
        admit_local_stores(settings)
    assert auth_path.is_symlink()
    assert not target.exists()
    assert not Path(settings.get_session_db_url().removeprefix("sqlite:///")).exists()
    assert not Path(settings.get_landscape_url().removeprefix("sqlite:///")).exists()
