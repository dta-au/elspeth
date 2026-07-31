"""Typed mutation-boundary tests for encrypted user-secret persistence."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import NoReturn

import pytest
import sqlalchemy as sa

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.secrets import FingerprintKeyMissingError
from elspeth.web.secrets.user_store import RepositoryUserSecretAuthority, UserSecretAuthority, UserSecretStore
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import user_secrets_table
from elspeth.web.sessions.schema import initialize_session_schema


@dataclass
class _CapturingAuthority:
    upserts: list[dict[str, object]] = field(default_factory=list)
    deletes: list[tuple[str, str, AuthProviderType]] = field(default_factory=list)
    delete_result: bool = True

    def upsert_encrypted_secret(
        self,
        *,
        name: str,
        user_id: str,
        auth_provider_type: AuthProviderType,
        encrypted_value: bytes,
        salt: bytes,
    ) -> None:
        self.upserts.append(
            {
                "name": name,
                "user_id": user_id,
                "auth_provider_type": auth_provider_type,
                "encrypted_value": encrypted_value,
                "salt": salt,
            }
        )

    def delete_secret(self, *, name: str, user_id: str, auth_provider_type: AuthProviderType) -> bool:
        self.deletes.append((name, user_id, auth_provider_type))
        return self.delete_result


@pytest.fixture()
def engine():
    engine = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(engine)
    return engine


def test_user_secret_authority_protocol_is_handle_free() -> None:
    """The mutation capability must expose domain operations, never a DB handle."""
    assert getattr(UserSecretAuthority, "_is_runtime_protocol", False) is True
    assert set(UserSecretAuthority.__dict__) & {"connection", "engine", "execute"} == set()
    assert tuple(inspect.signature(UserSecretAuthority.upsert_encrypted_secret).parameters) == (
        "self",
        "name",
        "user_id",
        "auth_provider_type",
        "encrypted_value",
        "salt",
    )
    assert tuple(inspect.signature(UserSecretAuthority.delete_secret).parameters) == (
        "self",
        "name",
        "user_id",
        "auth_provider_type",
    )
    assert not hasattr(RepositoryUserSecretAuthority, "execute")


def test_store_delegates_only_encrypted_material_and_exact_scope(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "authority-test-fingerprint-key")
    authority = _CapturingAuthority()
    assert isinstance(authority, UserSecretAuthority)
    store = UserSecretStore(engine, "test-master-key", mutation_authority=authority)

    fingerprint = store.set_secret("TOKEN", value="plaintext-value", user_id="alice", auth_provider_type="oidc")
    deleted = store.delete_secret("TOKEN", user_id="alice", auth_provider_type="oidc")

    assert len(fingerprint) == 64
    assert authority.deletes == [("TOKEN", "alice", "oidc")]
    assert deleted is True
    assert len(authority.upserts) == 1
    captured = authority.upserts[0]
    assert captured["name"] == "TOKEN"
    assert captured["user_id"] == "alice"
    assert captured["auth_provider_type"] == "oidc"
    encrypted_value = captured["encrypted_value"]
    salt = captured["salt"]
    assert isinstance(encrypted_value, bytes)
    assert isinstance(salt, bytes)
    assert encrypted_value != b"plaintext-value"
    assert b"plaintext-value" not in encrypted_value
    assert len(salt) == 16
    assert "value" not in captured
    assert "master_key" not in captured


def test_fingerprint_failure_happens_before_authority_invocation(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY", raising=False)
    authority = _CapturingAuthority()
    store = UserSecretStore(engine, "test-master-key", mutation_authority=authority)

    with pytest.raises(FingerprintKeyMissingError):
        store.set_secret("TOKEN", value="plaintext-value", user_id="alice", auth_provider_type="local")

    assert authority.upserts == []


def test_authority_failure_propagates_without_success_result(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "authority-test-fingerprint-key")

    class _FailingAuthority(_CapturingAuthority):
        def upsert_encrypted_secret(
            self,
            *,
            name: str,
            user_id: str,
            auth_provider_type: AuthProviderType,
            encrypted_value: bytes,
            salt: bytes,
        ) -> NoReturn:
            raise RuntimeError("commit failed")

    store = UserSecretStore(engine, "test-master-key", mutation_authority=_FailingAuthority())

    with pytest.raises(RuntimeError, match="commit failed"):
        store.set_secret("TOKEN", value="plaintext-value", user_id="alice", auth_provider_type="local")


def test_repository_authority_uses_database_clock_and_preserves_creation_time(
    engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "authority-test-fingerprint-key")
    store = UserSecretStore(
        engine,
        "test-master-key",
        mutation_authority=RepositoryUserSecretAuthority(engine),
    )
    with engine.connect() as conn:
        before = conn.execute(sa.select(sa.func.current_timestamp())).scalar_one()

    store.set_secret("TOKEN", value="first", user_id="alice", auth_provider_type="local")
    with engine.connect() as conn:
        first = conn.execute(
            sa.select(
                user_secrets_table.c.id,
                user_secrets_table.c.created_at,
                user_secrets_table.c.updated_at,
                user_secrets_table.c.version,
            ).where(
                sa.and_(
                    user_secrets_table.c.name == "TOKEN",
                    user_secrets_table.c.user_id == "alice",
                    user_secrets_table.c.auth_provider_type == "local",
                )
            )
        ).one()

    store.set_secret("TOKEN", value="second", user_id="alice", auth_provider_type="local")
    with engine.connect() as conn:
        second = conn.execute(
            sa.select(
                user_secrets_table.c.id,
                user_secrets_table.c.created_at,
                user_secrets_table.c.updated_at,
                user_secrets_table.c.version,
            ).where(
                sa.and_(
                    user_secrets_table.c.name == "TOKEN",
                    user_secrets_table.c.user_id == "alice",
                    user_secrets_table.c.auth_provider_type == "local",
                )
            )
        ).one()
        after = conn.execute(sa.select(sa.func.current_timestamp())).scalar_one()

    assert before <= first.created_at <= after
    assert before <= first.updated_at <= after
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert before <= second.updated_at <= after
    assert (first.version, second.version) == (1, 2)
