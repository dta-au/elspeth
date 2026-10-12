"""Exact bootstrap admission for the local credential store.

Unsupported stores are inspected without DDL or data repair. Schema statements
are shared by fresh creation and the reference used for existing-store admission.
"""

from __future__ import annotations

import sqlite3

AUTH_SCHEMA_STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS users (\n                    user_id TEXT PRIMARY KEY,\n                    password_hash TEXT NOT NULL,\n                    display_name TEXT NOT NULL,\n                    email TEXT,\n                    email_verified INTEGER NOT NULL DEFAULT 1\n                )",
    "CREATE TABLE IF NOT EXISTS email_verification_tokens (\n                    token_hash TEXT PRIMARY KEY,\n                    user_id TEXT NOT NULL,\n                    created_at INTEGER NOT NULL,\n                    expires_at INTEGER NOT NULL,\n                    used_at INTEGER,\n                    FOREIGN KEY (user_id) REFERENCES users(user_id)\n                )",
    "CREATE INDEX IF NOT EXISTS ix_email_verification_tokens_user_id ON email_verification_tokens (user_id)",
    "CREATE TABLE IF NOT EXISTS email_verification_outbox (\n                    delivery_id TEXT PRIMARY KEY,\n                    token_hash TEXT NOT NULL UNIQUE,\n                    user_id TEXT NOT NULL,\n                    payload_json TEXT NOT NULL,\n                    created_at INTEGER NOT NULL,\n                    published_at INTEGER,\n                    FOREIGN KEY (token_hash) REFERENCES email_verification_tokens(token_hash),\n                    FOREIGN KEY (user_id) REFERENCES users(user_id)\n                )",
    "CREATE INDEX IF NOT EXISTS ix_email_verification_outbox_pending ON email_verification_outbox (published_at, created_at)",
    "CREATE TABLE IF NOT EXISTS token_audit_intents (\n                    intent_id TEXT PRIMARY KEY,\n                    user_id TEXT NOT NULL,\n                    issuance_path TEXT NOT NULL,\n                    token_hash TEXT,\n                    claimed_at INTEGER,\n                    created_at INTEGER NOT NULL,\n                    FOREIGN KEY (user_id) REFERENCES users(user_id)\n                )",
    "CREATE INDEX IF NOT EXISTS ix_token_audit_intents_created_at ON token_audit_intents (created_at)",
)


class LocalAuthSchemaError(RuntimeError):
    """The configured credential store is unsupported by this build."""


def create_local_auth_schema(connection: sqlite3.Connection) -> None:
    """Build the credential schema on an already admitted fresh target."""
    for statement in AUTH_SCHEMA_STATEMENTS:
        connection.execute(statement)


def _catalog(connection: sqlite3.Connection) -> list[tuple[str, str, str, str | None]]:
    return [
        (kind, name, table, None if sql is None else " ".join(sql.split()))
        for kind, name, table, sql in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    ]


def admit_local_auth_schema(connection: sqlite3.Connection) -> bool:
    """Return True for a genuinely fresh target, False for a compatible store."""
    actual = _catalog(connection)
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if application_id != 0 or user_version != 0:
        raise LocalAuthSchemaError(
            "Local auth database has unsupported schema sentinels. Preserve this store unchanged "
            "and configure an explicitly new credential store."
        )
    if not actual:
        return True
    reference = sqlite3.connect(":memory:")
    try:
        create_local_auth_schema(reference)
        expected = _catalog(reference)
    finally:
        reference.close()
    if actual != expected:
        raise LocalAuthSchemaError(
            "Local auth database schema is unsupported. Preserve this store unchanged and configure an explicitly new credential store."
        )
    return False
