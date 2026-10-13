"""Auth generation union refuses prior credentials without changing retained state."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from elspeth.web.app import create_app
from elspeth.web.auth.local import LocalAuthProvider, validate_existing_local_auth_store
from elspeth.web.auth.local_schema import AUTH_SCHEMA_STATEMENTS, LocalAuthSchemaError
from elspeth.web.config import WebSettings
from elspeth.web.local_store_admission import admit_local_stores

# Independent pre-union catalog from the recovery baseline: intentionally does
# not derive from the current canonical schema, so adding generation cannot
# silently turn this refusal fixture into a compatible store.
_PRE_UNION_SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS users (\n                    user_id TEXT PRIMARY KEY,\n                    password_hash TEXT NOT NULL,\n                    display_name TEXT NOT NULL,\n                    email TEXT,\n                    email_verified INTEGER NOT NULL DEFAULT 1\n                )",
    "CREATE TABLE IF NOT EXISTS email_verification_tokens (\n                    token_hash TEXT PRIMARY KEY,\n                    user_id TEXT NOT NULL,\n                    created_at INTEGER NOT NULL,\n                    expires_at INTEGER NOT NULL,\n                    used_at INTEGER,\n                    FOREIGN KEY (user_id) REFERENCES users(user_id)\n                )",
    "CREATE INDEX IF NOT EXISTS ix_email_verification_tokens_user_id ON email_verification_tokens (user_id)",
    "CREATE TABLE IF NOT EXISTS email_verification_outbox (\n                    delivery_id TEXT PRIMARY KEY,\n                    token_hash TEXT NOT NULL UNIQUE,\n                    user_id TEXT NOT NULL,\n                    payload_json TEXT NOT NULL,\n                    created_at INTEGER NOT NULL,\n                    published_at INTEGER,\n                    FOREIGN KEY (token_hash) REFERENCES email_verification_tokens(token_hash),\n                    FOREIGN KEY (user_id) REFERENCES users(user_id)\n                )",
    "CREATE INDEX IF NOT EXISTS ix_email_verification_outbox_pending ON email_verification_outbox (published_at, created_at)",
    "CREATE TABLE IF NOT EXISTS token_audit_intents (\n                    intent_id TEXT PRIMARY KEY,\n                    user_id TEXT NOT NULL,\n                    issuance_path TEXT NOT NULL,\n                    token_hash TEXT,\n                    claimed_at INTEGER,\n                    created_at INTEGER NOT NULL,\n                    FOREIGN KEY (user_id) REFERENCES users(user_id)\n                )",
    "CREATE INDEX IF NOT EXISTS ix_token_audit_intents_created_at ON token_audit_intents (created_at)",
)


def _catalog_and_rows(path: Path):
    with closing(sqlite3.connect(path)) as connection:
        return (
            connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall(),
            tuple(connection.iterdump()),
        )


def _administration_provider(path: Path) -> LocalAuthProvider:
    def no_retirement(*_args):
        raise AssertionError("Schema refusal must not retire identities")

    return LocalAuthProvider.for_account_administration(path, retire_identity=no_retirement)


@pytest.mark.parametrize("operation", ["constructor", "read-only-probe", "store-admission", "startup"])
def test_pre_union_auth_refusal_preserves_bytes_catalog_users_and_outbox(tmp_path, closed_local_settings, operation):
    path = tmp_path / "auth.db"
    with closing(sqlite3.connect(path)) as connection:
        for statement in _PRE_UNION_SCHEMA_STATEMENTS:
            connection.execute(statement)
        connection.execute("INSERT INTO users VALUES ('retained-user', 'synthetic-hash', 'Retained', 'retained@example.com', 0)")
        connection.execute("INSERT INTO email_verification_tokens VALUES ('retained-token-hash', 'retained-user', 1, 2, NULL)")
        connection.execute(
            "INSERT INTO email_verification_outbox VALUES ('retained-delivery', 'retained-token-hash', 'retained-user', '{}', 1, NULL)"
        )
        connection.execute("INSERT INTO token_audit_intents VALUES ('retained-intent', 'retained-user', 'register', NULL, NULL, 1)")
        connection.commit()
        assert "credential_generation" not in {row[1] for row in connection.execute("PRAGMA table_info(users)")}
    path.chmod(0o600)
    outbox = tmp_path / "email-verifications.jsonl"
    outbox.write_bytes(b'{"retained":"synthetic delivery"}\n')
    before_bytes = path.read_bytes()
    before_catalog = _catalog_and_rows(path)
    before_outbox = outbox.read_bytes()
    settings: WebSettings = closed_local_settings.model_copy(update={"data_dir": tmp_path})
    with pytest.raises(LocalAuthSchemaError, match="Preserve this store unchanged"):
        if operation == "constructor":
            _administration_provider(path)
        elif operation == "read-only-probe":
            validate_existing_local_auth_store(path)
        elif operation == "store-admission":
            admit_local_stores(settings)
        else:
            create_app(settings)
    assert path.read_bytes() == before_bytes
    assert _catalog_and_rows(path) == before_catalog
    assert outbox.read_bytes() == before_outbox
    assert sorted(item.name for item in tmp_path.iterdir()) == ["auth.db", "email-verifications.jsonl"]


@pytest.mark.parametrize(
    "replacement",
    [
        "credential_generation INTEGER DEFAULT 0 CHECK (credential_generation >= 0)",
        "credential_generation INTEGER NOT NULL DEFAULT 0",
        "credential_generation INTEGER NOT NULL DEFAULT 1 CHECK (credential_generation >= 0)",
    ],
)
def test_auth_generation_shape_tampering_is_refused_unchanged(tmp_path, replacement):
    path = tmp_path / "auth.db"
    generation = "credential_generation INTEGER NOT NULL DEFAULT 0 CHECK (credential_generation >= 0)"
    assert AUTH_SCHEMA_STATEMENTS[0].count(generation) == 1
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(AUTH_SCHEMA_STATEMENTS[0].replace(generation, replacement))
        for statement in AUTH_SCHEMA_STATEMENTS[1:]:
            connection.execute(statement)
        connection.commit()
    path.chmod(0o600)
    before = path.read_bytes(), _catalog_and_rows(path)
    with pytest.raises(LocalAuthSchemaError):
        _administration_provider(path)
    assert (path.read_bytes(), _catalog_and_rows(path)) == before


def test_fresh_auth_generation_defaults_to_zero_and_rejects_negative(tmp_path):
    path = tmp_path / "auth.db"
    _administration_provider(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("INSERT INTO users (user_id, password_hash, display_name) VALUES ('new-user', 'synthetic-hash', 'New')")
        assert connection.execute("SELECT credential_generation FROM users").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute("UPDATE users SET credential_generation = -1")
        connection.commit()
    validate_existing_local_auth_store(path)
