"""Startup refusal preserves synthetic credential stores and sibling targets."""

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import SecretBytes

from elspeth.web.app import create_app
from elspeth.web.auth.local import (
    LocalAuthProvider,
    LocalAuthStorageSecurityError,
    _connect_owner_only_database,
    validate_existing_local_auth_store,
)
from elspeth.web.auth.local_schema import AUTH_SCHEMA_STATEMENTS, LocalAuthSchemaError
from elspeth.web.config import WebSettings
from elspeth.web.local_store_admission import admit_local_stores


def _provider(path: Path) -> LocalAuthProvider:
    def unexpected_retirement(*_args):
        raise AssertionError("Startup must not retire identities")

    return LocalAuthProvider.for_account_administration(path, retire_identity=unexpected_retirement)


def _settings(path: Path) -> WebSettings:
    return WebSettings(
        data_dir=path,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(bytes(32)),
        operator_metrics_bearer_token="operator-metrics-token-for-tests-0001",
    )


def _fingerprint(path: Path):
    identity = path.stat()
    return (path.read_bytes(), identity.st_ino, identity.st_mode, identity.st_mtime_ns)


def _unsupported(path: Path, shape: str) -> None:
    if shape == "header":
        path.write_bytes(b"synthetic foreign file; preserve these bytes")
    else:
        with closing(sqlite3.connect(path)) as conn:
            if shape == "legacy":
                conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, password_hash TEXT NOT NULL, display_name TEXT NOT NULL)")
                conn.execute("INSERT INTO users VALUES ('synthetic-user', 'synthetic-hash', 'Synthetic')")
            elif shape == "partial":
                conn.execute(AUTH_SCHEMA_STATEMENTS[0])
            elif shape == "foreign":
                conn.execute("CREATE TABLE unrelated (value TEXT)")
                conn.execute("INSERT INTO unrelated VALUES ('preserve')")
            elif shape == "view":
                conn.execute("CREATE VIEW unrelated AS SELECT 1 AS value")
            elif shape == "application_id":
                conn.execute("PRAGMA application_id = 1234")
            elif shape == "user_version":
                conn.execute("PRAGMA user_version = 99")
            else:
                raise AssertionError(shape)
            conn.commit()
    path.chmod(0o600)


def test_fresh_auth_creation_and_current_read_only_admission(tmp_path):
    path = tmp_path / "auth.db"
    provider = _provider(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert provider.list_users() == []
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO users (user_id, password_hash, display_name, email_verified) VALUES (?, ?, ?, 1)",
            ("synthetic-user", "synthetic-hash", "Synthetic"),
        )
        conn.commit()
    before = _fingerprint(path)
    validate_existing_local_auth_store(path)
    admit_local_stores(_settings(tmp_path))
    with (
        closing(_connect_owner_only_database(path, read_only=True)) as conn,
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        conn.execute("DELETE FROM users")
    assert _fingerprint(path) == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["auth.db"]
    assert [user.user_id for user in _provider(path).list_users()] == ["synthetic-user"]
    assert _fingerprint(path) == before


def test_read_only_auth_validation_does_not_create_missing_target(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        validate_existing_local_auth_store(path)
    assert not path.exists()


def test_fresh_store_preflight_and_empty_auth_allocation_are_nonmutating(tmp_path):
    settings = _settings(tmp_path)
    admit_local_stores(settings)
    assert list(tmp_path.iterdir()) == []
    path = tmp_path / "auth.db"
    path.touch(mode=0o600)
    before = _fingerprint(path)
    admit_local_stores(settings)
    assert _fingerprint(path) == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["auth.db"]
    assert _provider(path).list_users() == []


@pytest.mark.parametrize("shape", ["legacy", "partial", "foreign", "view", "application_id", "user_version", "header"])
def test_unsupported_auth_refusal_preserves_exact_store(tmp_path, shape):
    path = tmp_path / "auth.db"
    _unsupported(path, shape)
    before = _fingerprint(path)
    for operation in (validate_existing_local_auth_store, _provider):
        with pytest.raises((LocalAuthSchemaError, sqlite3.DatabaseError)):
            operation(path)
        assert _fingerprint(path) == before
        assert sorted(item.name for item in tmp_path.iterdir()) == ["auth.db"]


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400, 0o700])
def test_auth_admission_refuses_insecure_or_unexpected_mode_without_chmod(tmp_path, mode):
    path = tmp_path / "auth.db"
    _provider(path)
    path.chmod(mode)
    before = _fingerprint(path)
    for operation in (validate_existing_local_auth_store, _provider):
        with pytest.raises((LocalAuthStorageSecurityError, PermissionError)):
            operation(path)
        assert _fingerprint(path) == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_auth_admission_refuses_nonexclusive_regular_file(tmp_path, kind):
    target = tmp_path / "original.db"
    _provider(target)
    path = tmp_path / "auth.db"
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        os.mkfifo(path, 0o600)
    before = _fingerprint(target)
    for operation in (validate_existing_local_auth_store, _provider):
        with pytest.raises(LocalAuthStorageSecurityError):
            operation(path)
        assert _fingerprint(target) == before


@pytest.mark.parametrize("shape", ["legacy", "partial", "foreign", "header", "dangling_symlink"])
def test_incompatible_auth_prevents_creating_missing_sibling_stores(tmp_path, shape):
    path = tmp_path / "auth.db"
    if shape == "dangling_symlink":
        path.symlink_to(tmp_path / "missing-target.db")
        before = None
    else:
        _unsupported(path, shape)
        before = _fingerprint(path)
    settings = _settings(tmp_path)
    for operation in (admit_local_stores, create_app):
        with pytest.raises((LocalAuthSchemaError, LocalAuthStorageSecurityError, sqlite3.DatabaseError)):
            operation(settings)
        assert sorted(item.name for item in tmp_path.iterdir()) == ["auth.db"]
        if before is not None:
            assert _fingerprint(path) == before
        else:
            assert path.is_symlink() and path.readlink() == tmp_path / "missing-target.db"
