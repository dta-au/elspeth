"""Fernet-encrypted user-scoped secret store.

Each secret is encrypted with a key derived from ``master_key`` + a per-secret
random salt via PBKDF2-HMAC-SHA256.  The salt is stored alongside the
ciphertext so decryption can re-derive the same key.

All methods are synchronous and open their own connection, making them safe to
call from worker threads without sharing connection state.
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from typing import Any, Protocol, cast, final, runtime_checkable

import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.engine import Engine

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.secrets import (
    FingerprintKeyMissingError,
    SecretDecryptionError,
    SecretInventoryItem,
)
from elspeth.contracts.security import secret_fingerprint
from elspeth.core.security.secret_loader import SecretNotFoundError, SecretRef
from elspeth.web.sessions.models import user_secrets_table

_PBKDF2_ITERATIONS = 480_000
_SALT_BYTES = 16
_USER_SECRET_CONFLICT_COLUMNS = ("name", "user_id", "auth_provider_type")
_USER_SECRET_UPSERT_UPDATE_COLUMNS = ("encrypted_value", "salt", "updated_at")


def _fingerprint_key_available() -> bool:
    """Check whether ELSPETH_FINGERPRINT_KEY is set.

    Required for audit fingerprint computation.  Without it, get_secret()
    will raise SecretNotFoundError, so has_secret() and list_secrets() must
    reflect the same availability — a secret that cannot be fingerprinted
    is not resolvable.
    """
    return bool(os.environ.get("ELSPETH_FINGERPRINT_KEY"))


def _compute_fingerprint(name: str, value: str) -> str:
    """Compute HMAC fingerprint of a secret value.

    Returns a 64-char hex digest.  Raises ``FingerprintKeyMissingError``
    when ``ELSPETH_FINGERPRINT_KEY`` is unset (or set to an empty value) —
    the fingerprint is required for audit-trail integrity, and an absent or
    empty key would otherwise surface as a confusing generic ``ValueError``.
    The typed exception lets HTTP handlers map the condition to 503 with
    actionable deployment guidance, and lets pipeline resolution fail fast
    rather than silently bucketing the miss into ``SecretResolutionError``.

    Delegates the deployment-env read to :func:`secret_fingerprint` /
    :func:`get_fingerprint_key`, which read ``ELSPETH_FINGERPRINT_KEY`` at
    call time (preserving per-call deployment-state semantics) and raise
    ``ValueError`` on both absence and an empty key.  We re-raise that as the
    typed ``FingerprintKeyMissingError`` so the boundary outcome is explicit.
    """
    try:
        return secret_fingerprint(value)
    except ValueError as exc:
        raise FingerprintKeyMissingError(
            f"ELSPETH_FINGERPRINT_KEY is not set — cannot compute fingerprint for secret {name!r}. "
            "Set the environment variable before starting the web server."
        ) from exc


def _derive_fernet_key(master_key: str, salt: bytes) -> bytes:
    """Derive a 32-byte Fernet key from *master_key* and *salt* via PBKDF2."""
    raw = hashlib.pbkdf2_hmac(
        "sha256",
        master_key.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
    )
    # Fernet requires url-safe base64 encoded 32-byte key
    return base64.urlsafe_b64encode(raw)


def _secret_binary_to_bytes(name: str, field_name: str, value: object) -> bytes:
    """Normalize DB-returned binary column values before cryptographic use."""
    value_type = type(value)
    if value_type is bytes:
        return cast(bytes, value)
    if value_type is bytearray:
        return bytes(cast(bytearray, value))
    if value_type is memoryview:
        return cast(memoryview, value).tobytes()
    raise SecretDecryptionError(
        f"Secret {name!r} is not resolvable — stored {field_name} is not binary data "
        "(possible row corruption or unsupported database driver result type)"
    )


def _upsert_update_mapping(table: sa.Table, insert_namespace: Any) -> dict[str, Any]:
    """Build the per-column update mapping for dialect-specific upsert clauses."""
    return {
        **{column: getattr(insert_namespace, column) for column in _USER_SECRET_UPSERT_UPDATE_COLUMNS},
        "version": table.c.version + 1,
    }


@runtime_checkable
class UserSecretAuthority(Protocol):
    """Handle-free capability for user-secret table mutations."""

    def upsert_encrypted_secret(
        self,
        *,
        name: str,
        user_id: str,
        auth_provider_type: AuthProviderType,
        encrypted_value: bytes,
        salt: bytes,
    ) -> None: ...

    def delete_secret(self, *, name: str, user_id: str, auth_provider_type: AuthProviderType) -> bool: ...


@final
class RepositoryUserSecretAuthority:
    """Own every user-secret write without exposing its database handle."""

    __slots__ = ("_dialect", "_engine")

    def __init__(self, engine: Engine) -> None:
        dialect = engine.dialect.name
        if dialect not in {"sqlite", "postgresql", "mysql", "mariadb"}:
            raise NotImplementedError(
                "UserSecretAuthority requires an atomic upsert, "
                f"but no implementation is registered for session database dialect {dialect!r}. "
                "Supported dialects: sqlite, postgresql, mysql, mariadb."
            )
        self._engine = engine
        self._dialect = dialect

    def upsert_encrypted_secret(
        self,
        *,
        name: str,
        user_id: str,
        auth_provider_type: AuthProviderType,
        encrypted_value: bytes,
        salt: bytes,
    ) -> None:
        """Atomically insert or rotate one encrypted user-secret row."""
        values = {
            "id": str(uuid.uuid4()),
            "name": name,
            "user_id": user_id,
            "auth_provider_type": auth_provider_type,
            "encrypted_value": encrypted_value,
            "salt": salt,
            "version": 1,
            "created_at": sa.func.current_timestamp(),
            "updated_at": sa.func.current_timestamp(),
        }
        stmt: Any
        if self._dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(user_secrets_table).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=list(_USER_SECRET_CONFLICT_COLUMNS),
                set_=_upsert_update_mapping(user_secrets_table, stmt.excluded),
            )
        elif self._dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as postgresql_insert

            stmt = postgresql_insert(user_secrets_table).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=list(_USER_SECRET_CONFLICT_COLUMNS),
                set_=_upsert_update_mapping(user_secrets_table, stmt.excluded),
            )
        else:
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            stmt = mysql_insert(user_secrets_table).values(**values)
            stmt = stmt.on_duplicate_key_update(**_upsert_update_mapping(user_secrets_table, stmt.inserted))

        with self._engine.begin() as conn:
            conn.execute(stmt)

    def delete_secret(self, *, name: str, user_id: str, auth_provider_type: AuthProviderType) -> bool:
        """Delete exactly one principal/provider-scoped secret when present."""
        with self._engine.begin() as conn:
            result = conn.execute(
                user_secrets_table.delete().where(
                    sa.and_(
                        user_secrets_table.c.name == name,
                        user_secrets_table.c.user_id == user_id,
                        user_secrets_table.c.auth_provider_type == auth_provider_type,
                    )
                )
            )
        return result.rowcount > 0


class UserSecretStore:
    """Encrypted persistence for user-scoped secrets.

    Parameters
    ----------
    engine:
        SQLAlchemy ``Engine`` connected to the session database.
    master_key:
        Application-level master key used (with a per-secret salt) to derive
        Fernet encryption keys.
    """

    def __init__(
        self,
        engine: Engine,
        master_key: str,
        *,
        mutation_authority: UserSecretAuthority | None = None,
    ) -> None:
        self._engine = engine
        self._master_key = master_key
        self._mutation_authority = mutation_authority or RepositoryUserSecretAuthority(engine)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def has_secret(self, name: str, *, user_id: str, auth_provider_type: AuthProviderType) -> bool:
        """Check if a user secret is resolvable.

        Returns True only when the secret exists, the deployment is
        configured for fingerprint computation (ELSPETH_FINGERPRINT_KEY),
        and the stored ciphertext can be decrypted with the current
        web ``secret_key``.  This aligns with get_secret().
        """
        if not _fingerprint_key_available():
            return False
        row = self._fetch_secret_row(name, user_id=user_id, auth_provider_type=auth_provider_type)
        if row is None:
            return False
        return self._row_is_resolvable(name, row=row)

    def has_secret_record(self, name: str, *, user_id: str, auth_provider_type: AuthProviderType) -> bool:
        """Check whether a user-scoped secret row exists, regardless of resolvability."""
        return self._fetch_secret_row(name, user_id=user_id, auth_provider_type=auth_provider_type) is not None

    def get_secret(self, name: str, *, user_id: str, auth_provider_type: AuthProviderType) -> tuple[str, SecretRef]:
        """Retrieve and decrypt a user secret.

        Returns
        -------
        tuple[str, SecretRef]
            The plaintext value and an audit-safe reference (no value).

        Raises
        ------
        SecretNotFoundError
            If no secret with *name* exists for *user_id* and
            *auth_provider_type*.
        FingerprintKeyMissingError
            If ``ELSPETH_FINGERPRINT_KEY`` is not set — the secret exists
            but cannot be fingerprinted for audit.  Typed separately from
            SecretNotFoundError so the HTTP layer can map to 503 with
            deployment guidance and pipeline resolution fails fast.
        SecretDecryptionError
            If the stored ciphertext cannot be decrypted with the current
            web ``secret_key`` (key rotation, row corruption, or tamper).
            HTTP layer maps to 409 with re-save guidance.
        """
        row = self._fetch_secret_row(name, user_id=user_id, auth_provider_type=auth_provider_type)
        if row is None:
            raise SecretNotFoundError(f"Secret {name!r} not found for user {user_id!r}")

        plaintext = self._decrypt_secret_value(
            name,
            encrypted_value=row.encrypted_value,
            salt=row.salt,
        )
        fp = _compute_fingerprint(name, plaintext)
        ref = SecretRef(name=name, fingerprint=fp, source="user")
        return plaintext, ref

    def set_secret(self, name: str, *, value: str, user_id: str, auth_provider_type: AuthProviderType) -> str:
        """Create or update a user secret (atomic upsert).

        Eager-fingerprint design: compute the audit fingerprint BEFORE
        encrypting and persisting so a deployment missing
        ``ELSPETH_FINGERPRINT_KEY`` fails the write atomically — no row
        is ever stored in a state where the audit trail has no
        fingerprint for it.  This also closes the TOCTOU window in the
        HTTP ``create_secret`` route: a returned fingerprint proves the
        row was both persisted and immediately resolvable.

        A fresh random salt is generated on every write so updating a
        secret also rotates the derived key.  Uses INSERT ... ON CONFLICT
        DO UPDATE for atomic concurrent writes.

        Returns
        -------
        str
            The 64-char hex fingerprint of the stored value — safe to
            surface in API responses and audit records (never the value).

        Raises
        ------
        FingerprintKeyMissingError
            If ``ELSPETH_FINGERPRINT_KEY`` is unset.  No row is written.
        """
        # Eager fingerprint: raises FingerprintKeyMissingError BEFORE the
        # write, preserving audit-trail integrity — if we cannot produce a
        # fingerprint we cannot record the write, so we must not perform
        # the write.  Intentional deviation from lazy-fingerprint designs:
        # we accept the extra HMAC cost on every write to guarantee atomic
        # audit-eligibility.
        fingerprint = _compute_fingerprint(name, value)

        salt = os.urandom(_SALT_BYTES)
        key = _derive_fernet_key(self._master_key, salt)
        encrypted = Fernet(key).encrypt(value.encode("utf-8"))
        self._mutation_authority.upsert_encrypted_secret(
            name=name,
            user_id=user_id,
            auth_provider_type=auth_provider_type,
            encrypted_value=encrypted,
            salt=salt,
        )
        return fingerprint

    def delete_secret(self, name: str, *, user_id: str, auth_provider_type: AuthProviderType) -> bool:
        """Delete a user secret.

        Returns ``True`` if a row was deleted, ``False`` if it did not exist.
        """
        return self._mutation_authority.delete_secret(
            name=name,
            user_id=user_id,
            auth_provider_type=auth_provider_type,
        )

    def list_secrets(self, *, user_id: str, auth_provider_type: AuthProviderType) -> list[SecretInventoryItem]:
        """List secret metadata for a user (no values returned).

        The inventory path is intentionally metadata-only: it does not
        select ciphertext or perform per-row decrypts.  The ``available``
        flag reflects whether rows exist in a deployment currently
        configured for fingerprint computation.  Callers that need full
        resolvability (including key-rotation/corruption detection) must
        use ``has_secret()``, ``get_secret()``, or the HTTP validate
        endpoint, which perform the bounded single-secret decrypt.

        Reason precedence is **fingerprint-key-first**, mirroring
        :meth:`ServerSecretStore.list_secrets`.  When
        ``ELSPETH_FINGERPRINT_KEY`` is unset every row reports
        ``fingerprint_resolver_not_configured`` even if its ciphertext
        would otherwise be undecryptable — the global deployment gap
        masks per-row state because fixing the per-row state without
        first fixing the deployment state would not make the secret
        resolvable.  Per-row decryption failures are surfaced by
        ``has_secret()``, ``get_secret()``, and validation routes rather
        than by inventory listing.
        """
        t = user_secrets_table
        stmt = (
            sa.select(t.c.name)
            .where(
                sa.and_(
                    t.c.user_id == user_id,
                    t.c.auth_provider_type == auth_provider_type,
                )
            )
            .order_by(t.c.name)
        )

        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()

        can_resolve = _fingerprint_key_available()
        items: list[SecretInventoryItem] = []
        for row in rows:
            if not can_resolve:
                items.append(
                    SecretInventoryItem(
                        name=row.name,
                        scope="user",
                        available=False,
                        source_kind="user_store",
                        reason="fingerprint_resolver_not_configured",
                    )
                )
                continue
            items.append(
                SecretInventoryItem(
                    name=row.name,
                    scope="user",
                    available=True,
                    source_kind="user_store",
                )
            )
        return items

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_secret_row(self, name: str, *, user_id: str, auth_provider_type: AuthProviderType) -> Any | None:
        t = user_secrets_table
        stmt = sa.select(t.c.encrypted_value, t.c.salt).where(
            sa.and_(
                t.c.name == name,
                t.c.user_id == user_id,
                t.c.auth_provider_type == auth_provider_type,
            )
        )
        with self._engine.connect() as conn:
            return conn.execute(stmt).first()

    def _decrypt_secret_value(self, name: str, *, encrypted_value: object, salt: object) -> str:
        encrypted_bytes = _secret_binary_to_bytes(name, "encrypted_value", encrypted_value)
        salt_bytes = _secret_binary_to_bytes(name, "salt", salt)
        key = _derive_fernet_key(self._master_key, salt_bytes)
        try:
            return Fernet(key).decrypt(encrypted_bytes).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise SecretDecryptionError(
                f"Secret {name!r} is not resolvable — stored value cannot be decrypted "
                "with the current web secret_key (possible key rotation or row corruption)"
            ) from exc

    def _row_is_resolvable(self, name: str, *, row: Any) -> bool:
        try:
            self._decrypt_secret_value(name, encrypted_value=row.encrypted_value, salt=row.salt)
        except SecretDecryptionError:
            return False
        return True
