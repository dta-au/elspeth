"""Local authentication provider -- SQLite user store with bcrypt and JWT.

Uses bcrypt for password hashing and PyJWT for JWT token creation
and validation. The SQLite database is created at db_path on first use.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from urllib.parse import urlencode

import bcrypt
import jwt
import structlog
from jwt.exceptions import PyJWTError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth.models import AuthenticationError, UserIdentity, UserProfile
from elspeth.web.validation import has_visible_content

_slog = structlog.get_logger(__name__)

_EMAIL_VERIFICATION_TOKEN_BYTES = 32
_EMAIL_VERIFICATION_TOKEN_TTL_SECONDS = 24 * 60 * 60
_EMAIL_VERIFICATION_AUDIT_RETRY_SECONDS = 5 * 60
_TOKEN_AUDIT_INTENT_GRACE_SECONDS = 5 * 60
_PENDING_REGISTRATION_RETENTION_SECONDS = 7 * 24 * 60 * 60
_EMAIL_VERIFICATION_DELIVERY_FIELDS = frozenset(
    {
        "delivery_id",
        "email",
        "token",
        "user_id",
        "verification_url",
    }
)
MAX_BCRYPT_PASSWORD_BYTES = 72


class LocalAuthRegistrationConflict(ValueError):
    """A requested local registration conflicts with an existing account."""


class LocalAuthStorageSecurityError(RuntimeError):
    """The local credential store failed its owner-only file admission."""


def bcrypt_password_bytes(password: str) -> bytes:
    """Encode a password after enforcing bcrypt's UTF-8 byte limit."""
    encoded_password = password.encode("utf-8")
    if len(encoded_password) > MAX_BCRYPT_PASSWORD_BYTES:
        raise ValueError(f"password must not exceed {MAX_BCRYPT_PASSWORD_BYTES} bytes")
    return encoded_password


def _required_visible_string_claim(payload: dict[str, object], claim_name: str) -> str:
    """Extract a required local-JWT claim as a visible string."""
    try:
        value = payload[claim_name]
    except KeyError as exc:
        raise AuthenticationError("Invalid token") from exc
    if not isinstance(value, str) or not has_visible_content(value):
        raise AuthenticationError("Invalid token")
    return value


def _verification_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical_email_verification_delivery(
    record: Mapping[str, object],
    *,
    context: str,
) -> tuple[str, bytes]:
    """Validate and canonically bind one first-party delivery record."""
    if "delivery_id" not in record:
        raise AuditIntegrityError(f"{context} is missing delivery_id")
    if set(record) != _EMAIL_VERIFICATION_DELIVERY_FIELDS:
        raise AuditIntegrityError(f"{context} must contain exactly the required delivery fields")
    for field_name in _EMAIL_VERIFICATION_DELIVERY_FIELDS:
        value = record[field_name]
        if type(value) is not str or value == "":
            raise AuditIntegrityError(f"{context} has an invalid {field_name}")
    delivery_id = cast(str, record["delivery_id"])
    canonical = json.dumps(dict(record), sort_keys=True, separators=(",", ":")).encode()
    return delivery_id, canonical


def _read_jsonl_deliveries(fd: int) -> dict[str, bytes]:
    """Validate a locked verification outbox and normalize its final newline."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(fd, 64 * 1024):
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        return {}

    deliveries: dict[str, bytes] = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        try:
            record = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AuditIntegrityError(f"Email verification outbox line {line_number} is malformed") from exc
        if type(record) is not dict:
            raise AuditIntegrityError(f"Email verification outbox line {line_number} must be a JSON object")
        delivery_id, canonical = _canonical_email_verification_delivery(
            record,
            context=f"Email verification outbox line {line_number}",
        )
        if delivery_id in deliveries:
            raise AuditIntegrityError(f"Email verification outbox line {line_number} has a duplicate delivery_id")
        deliveries[delivery_id] = canonical

    if not content.endswith(b"\n"):
        os.lseek(fd, 0, os.SEEK_END)
        if os.write(fd, b"\n") != 1:
            raise OSError("Email verification outbox newline repair was incomplete")
        os.fsync(fd)
    return deliveries


def _append_email_verification_record(outbox_path: Path, record: Mapping[str, object]) -> None:
    """Idempotently append one durable verification delivery record.

    The stable ``delivery_id`` bridges the SQLite outbox intent and JSONL
    publication. A crash after append but before acknowledgement is recovered
    by scanning the locked file and acknowledging the already-present ID.
    """
    delivery_id, canonical = _canonical_email_verification_delivery(
        record,
        context="Email verification delivery",
    )
    payload = canonical + b"\n"
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(outbox_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        published = _read_jsonl_deliveries(fd)
        if delivery_id in published:
            if published[delivery_id] != canonical:
                raise AuditIntegrityError("Published email verification delivery does not match the durable intent")
            return
        original_size = os.lseek(fd, 0, os.SEEK_END)
        try:
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError("Email verification outbox append was incomplete")
            os.fsync(fd)
        except BaseException:
            os.ftruncate(fd, original_size)
            os.fsync(fd)
            raise
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _open_owner_only_database(path: Path) -> int:
    """Open or atomically create ``path`` without following a final symlink."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise LocalAuthStorageSecurityError("Local auth storage requires no-follow file admission")
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK | nofollow
    created = False
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        try:
            descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LocalAuthStorageSecurityError("Local auth database must be a regular owner-only file, not a symlink") from exc
        raise

    try:
        if created:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        identity = os.fstat(descriptor)
        mode = stat.S_IMODE(identity.st_mode)
        if not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.geteuid() or identity.st_nlink != 1 or mode != 0o600:
            raise LocalAuthStorageSecurityError("Local auth database must be a regular owner-only file with mode 0600")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class LocalAuthProvider:
    """Authenticates users against a local SQLite database with bcrypt + JWT."""

    # Pre-computed dummy hash for constant-time comparison on failed lookups.
    # Eagerly initialized to avoid a data race on first concurrent access.
    # The ~200ms bcrypt cost is paid once at class load time.
    _dummy_hash: bytes = bcrypt.hashpw(b"dummy", bcrypt.gensalt())

    @classmethod
    def _get_dummy_hash(cls) -> bytes:
        return cls._dummy_hash

    def __init__(
        self,
        db_path: Path,
        secret_key: str,
        token_expiry_hours: int = 24,
        max_refresh_chain_hours: int = 168,
    ) -> None:
        self._db_path = db_path
        self._secret_key = secret_key
        self._token_expiry_hours = token_expiry_hours
        self._max_refresh_chain_hours = max_refresh_chain_hours
        self._ensure_schema()
        with self._connect(immediate=True) as conn:
            now = int(time.time())
            self._reap_stale_pending_registrations(conn, now=now)
            self._reclaim_stale_token_audit_intents(conn, now=now)

    def _get_conn(self) -> sqlite3.Connection:
        """Open SQLite through a validated, no-follow database descriptor."""
        descriptor = _open_owner_only_database(self._db_path)
        try:
            identity = os.fstat(descriptor)
            descriptor_root = Path("/proc/self/fd")
            if not descriptor_root.is_dir():
                descriptor_root = Path("/dev/fd")
            if not descriptor_root.is_dir():
                raise LocalAuthStorageSecurityError("Local auth storage requires a descriptor-backed filesystem path")
            conn = sqlite3.connect(str(descriptor_root / str(descriptor)))
            try:
                current = os.stat(self._db_path, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or current.st_dev != identity.st_dev or current.st_ino != identity.st_ino:
                    raise LocalAuthStorageSecurityError("Local auth database path changed during secure open")
            except BaseException:
                conn.close()
                raise
            return conn
        finally:
            os.close(descriptor)

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a transaction-scoped SQLite connection and always close it.

        ``immediate=True`` acquires SQLite's write reservation before the
        first read. Consistency-sensitive auth workflows use it so validation,
        mutation, required audit, and commit form one serialized unit.
        """
        conn = self._get_conn()
        try:
            if immediate:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        """Create the users table if it does not exist."""
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    email TEXT,
                    email_verified INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "email_verified" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_verification_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_email_verification_tokens_user_id ON email_verification_tokens (user_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS email_verification_outbox (
                    delivery_id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    published_at INTEGER,
                    FOREIGN KEY (token_hash) REFERENCES email_verification_tokens(token_hash),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_email_verification_outbox_pending ON email_verification_outbox (published_at, created_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_audit_intents (
                    intent_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    issuance_path TEXT NOT NULL,
                    token_hash TEXT,
                    claimed_at INTEGER,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS ix_token_audit_intents_created_at ON token_audit_intents (created_at)")

    def create_user(
        self,
        user_id: str,
        password: str,
        display_name: str,
        email: str | None = None,
        *,
        email_verified: bool = True,
    ) -> None:
        """Create a new user with a bcrypt-hashed password.

        Raises ValueError if a user with the given user_id already exists
        or if display_name is empty.
        """
        if not display_name:
            raise ValueError("display_name must not be empty")
        password_hash = bcrypt.hashpw(bcrypt_password_bytes(password), bcrypt.gensalt()).decode()
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (user_id, password_hash, display_name, email, email_verified) VALUES (?, ?, ?, ?, ?)",
                    (
                        user_id,
                        password_hash,
                        display_name,
                        email,
                        1 if email_verified else 0,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LocalAuthRegistrationConflict(f"User already exists: {user_id}") from exc

    def register_open_user_with_audit(
        self,
        user_id: str,
        password: str,
        display_name: str,
        email: str | None,
        *,
        record_token_issued: Callable[[str], None],
    ) -> str:
        """Create and durably commit an open-registration user, then audit it.

        The auth.db transaction commits the user together with a durable
        ``token_issued`` audit intent, then the Landscape callback delivers
        the record and the intent is cleared. A failed commit therefore
        cannot leave a ``token_issued`` record for a user that was never
        created, and a crash between commit and delivery leaves an intent
        that the reclaim sweep resolves by quarantining the unaudited
        account. A failed audit remains fatal: the just-committed user is
        compensatingly deleted and the audit error propagates; if cleanup
        itself fails, the inconsistency surfaces as
        :class:`AuditIntegrityError` and the surviving intent keeps the
        account reclaimable. A cancelled async caller may abandon the worker
        future, but the synchronous critical section itself continues
        through commit and audit or compensation.
        """
        if not display_name:
            raise ValueError("display_name must not be empty")
        password_hash = bcrypt.hashpw(bcrypt_password_bytes(password), bcrypt.gensalt()).decode()
        now = int(time.time())
        intent_id = secrets.token_urlsafe(18)
        with self._connect(immediate=True) as conn:
            self._reclaim_stale_token_audit_intents(conn, now=now)
            try:
                conn.execute(
                    "INSERT INTO users (user_id, password_hash, display_name, email, email_verified) VALUES (?, ?, ?, ?, 1)",
                    (user_id, password_hash, display_name, email),
                )
            except sqlite3.IntegrityError as exc:
                raise LocalAuthRegistrationConflict(f"User already exists: {user_id}") from exc
            conn.execute(
                """
                INSERT INTO token_audit_intents
                    (intent_id, user_id, issuance_path, token_hash, claimed_at, created_at)
                VALUES (?, ?, 'register', NULL, NULL, ?)
                """,
                (intent_id, user_id, now),
            )
            access_token = self._issue_token(user_id, user_id)
        try:
            record_token_issued(access_token)
        except BaseException as audit_error:
            try:
                self._compensate_open_registration(user_id)
            except BaseException as cleanup_error:
                raise AuditIntegrityError(
                    f"Open registration for {user_id!r} committed, its required token_issued "
                    f"audit failed ({audit_error!r}), and the compensating cleanup also failed"
                ) from cleanup_error
            raise audit_error
        try:
            with self._connect(immediate=True) as conn:
                delivery_owned = self._clear_token_audit_intent(
                    conn,
                    intent_id=intent_id,
                    user_id=user_id,
                    issuance_path="register",
                )
        except BaseException as mark_error:
            try:
                self._compensate_open_registration(user_id)
            except BaseException as cleanup_error:
                raise AuditIntegrityError(
                    f"Open registration for {user_id!r} was audited but its audit intent could not "
                    f"be cleared ({mark_error!r}) and the compensating cleanup also failed"
                ) from cleanup_error
            raise AuditIntegrityError(
                f"Open registration for {user_id!r} was audited but its audit intent could not be "
                "cleared; the account was removed so the reclaim sweep cannot later quarantine an "
                "audited account"
            ) from mark_error
        if not delivery_owned:
            # A reclaim sweep already resolved this issuance generation. Do
            # not compensate by user_id: that could delete a later account
            # created after the sweep released this identifier.
            raise AuditIntegrityError("Token audit intent ownership was lost before delivery completion")
        return access_token

    def _compensate_open_registration(self, user_id: str) -> None:
        """Remove a committed open registration together with its audit intent."""
        with self._connect(immediate=True) as conn:
            self._delete_user_rows(conn, user_id)

    @staticmethod
    def _delete_user_rows(conn: sqlite3.Connection, user_id: str) -> bool:
        """Delete a user and every dependent row, including audit intents."""
        conn.execute("DELETE FROM token_audit_intents WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM email_verification_outbox WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM email_verification_tokens WHERE user_id = ?", (user_id,))
        cursor = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        return cursor.rowcount > 0

    @staticmethod
    def _clear_token_audit_intent(
        conn: sqlite3.Connection,
        *,
        intent_id: str,
        user_id: str,
        issuance_path: str,
    ) -> bool:
        """Return whether delivery consumed the exact intent it owns."""
        cleared = conn.execute(
            """
            DELETE FROM token_audit_intents
            WHERE intent_id = ? AND user_id = ? AND issuance_path = ?
            """,
            (intent_id, user_id, issuance_path),
        )
        return cleared.rowcount == 1

    def delete_user(self, user_id: str) -> bool:
        """Delete a local auth user and any pending verification tokens."""
        with self._connect() as conn:
            return self._delete_user_rows(conn, user_id)

    def create_email_verification_token(
        self,
        user_id: str,
        *,
        ttl_seconds: int = _EMAIL_VERIFICATION_TOKEN_TTL_SECONDS,
    ) -> str:
        """Create a one-use verification token for an unverified local user."""
        now = int(time.time())
        token = secrets.token_urlsafe(_EMAIL_VERIFICATION_TOKEN_BYTES)
        token_hash = _verification_token_hash(token)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT email_verified FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"User not found: {user_id}")
            if row[0]:
                raise ValueError(f"User already verified: {user_id}")
            conn.execute(
                "DELETE FROM email_verification_tokens WHERE user_id = ? AND used_at IS NULL",
                (user_id,),
            )
            conn.execute(
                """
                INSERT INTO email_verification_tokens
                    (token_hash, user_id, created_at, expires_at, used_at)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (token_hash, user_id, now, now + ttl_seconds),
            )
        return token

    def _insert_verification_delivery(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        email: str,
        verification_origin: str,
        now: int,
    ) -> None:
        token = secrets.token_urlsafe(_EMAIL_VERIFICATION_TOKEN_BYTES)
        token_hash = _verification_token_hash(token)
        delivery_id = secrets.token_urlsafe(18)
        record = {
            "delivery_id": delivery_id,
            "user_id": user_id,
            "email": email,
            "token": token,
            "verification_url": f"{verification_origin}/?{urlencode({'verify_token': token})}",
        }
        conn.execute(
            """
            INSERT INTO email_verification_tokens
                (token_hash, user_id, created_at, expires_at, used_at)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (token_hash, user_id, now, now + _EMAIL_VERIFICATION_TOKEN_TTL_SECONDS),
        )
        conn.execute(
            """
            INSERT INTO email_verification_outbox
                (delivery_id, token_hash, user_id, payload_json, created_at, published_at)
            VALUES (?, ?, ?, ?, ?, NULL)
            """,
            (delivery_id, token_hash, user_id, json.dumps(record, sort_keys=True), now),
        )

    def _reap_stale_pending_registrations(self, conn: sqlite3.Connection, *, now: int) -> None:
        cutoff = now - _PENDING_REGISTRATION_RETENTION_SECONDS
        stale_users = [
            row[0]
            for row in conn.execute(
                """
                SELECT users.user_id
                FROM users
                WHERE users.email_verified = 0
                  AND EXISTS (
                      SELECT 1 FROM email_verification_tokens AS tokens
                      WHERE tokens.user_id = users.user_id
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM email_verification_tokens AS tokens
                      WHERE tokens.user_id = users.user_id
                        AND tokens.expires_at >= ?
                  )
                """,
                (cutoff,),
            ).fetchall()
        ]
        for stale_user_id in stale_users:
            conn.execute("DELETE FROM email_verification_outbox WHERE user_id = ?", (stale_user_id,))
            conn.execute("DELETE FROM email_verification_tokens WHERE user_id = ?", (stale_user_id,))
            conn.execute("DELETE FROM users WHERE user_id = ? AND email_verified = 0", (stale_user_id,))

    def _reclaim_stale_token_audit_intents(self, conn: sqlite3.Connection, *, now: int) -> None:
        """Resolve ``token_issued`` audit intents that survived a crash.

        An intent that outlives the delivery grace window means the process
        died between the durable auth.db commit and the Landscape delivery
        (or between delivery and the intent clear). The Landscape callback is
        request-bound and cannot be replayed here, so reconciliation restores
        the pre-issuance auth state instead: an open registration is
        quarantined by deleting the never-audited account, and an email
        verification is returned to its retryable unverified state. Either
        way, no unaudited auth state survives silently.
        """
        cutoff = now - _TOKEN_AUDIT_INTENT_GRACE_SECONDS
        stale = conn.execute(
            """
            SELECT intent_id, user_id, issuance_path, token_hash, claimed_at
            FROM token_audit_intents
            WHERE created_at <= ?
            ORDER BY created_at, intent_id
            """,
            (cutoff,),
        ).fetchall()
        for intent_id, user_id, issuance_path, token_hash, claimed_at in stale:
            if issuance_path == "register":
                self._delete_user_rows(conn, user_id)
                fully_restored = True
            else:
                fully_restored = self._restore_retryable_verification(
                    conn,
                    user_id=user_id,
                    token_hash=token_hash,
                    claimed_at=claimed_at,
                    now=now,
                    intent_id=intent_id,
                )
            _slog.warning(
                "token_issued_audit_intent_reclaimed",
                user_id=user_id,
                issuance_path=issuance_path,
                fully_restored=fully_restored,
            )

    def _restore_retryable_verification(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        token_hash: str | None,
        claimed_at: int | None,
        now: int,
        intent_id: str,
    ) -> bool:
        """Return a claimed email verification to unverified, retryable state."""
        retry_deadline = now + _EMAIL_VERIFICATION_AUDIT_RETRY_SECONDS
        restored_user = conn.execute(
            "UPDATE users SET email_verified = 0 WHERE user_id = ? AND email_verified = 1",
            (user_id,),
        )
        restored_token = conn.execute(
            """
            UPDATE email_verification_tokens
            SET used_at = NULL, expires_at = MAX(expires_at, ?)
            WHERE token_hash = ? AND user_id = ? AND used_at = ?
            """,
            (retry_deadline, token_hash, user_id, claimed_at),
        )
        conn.execute("DELETE FROM token_audit_intents WHERE intent_id = ?", (intent_id,))
        return restored_user.rowcount == 1 and restored_token.rowcount == 1

    def _restore_retryable_verification_or_raise(
        self,
        user_id: str,
        *,
        token_hash: str,
        claimed_at: int,
        intent_id: str,
    ) -> None:
        """Compensate a delivered-or-failed verification audit, loudly on failure."""
        try:
            with self._connect(immediate=True) as conn:
                restored = self._restore_retryable_verification(
                    conn,
                    user_id=user_id,
                    token_hash=token_hash,
                    claimed_at=claimed_at,
                    now=int(time.time()),
                    intent_id=intent_id,
                )
                if not restored:
                    raise AuditIntegrityError("Email verification audit failure could not restore retryable state")
        except AuditIntegrityError:
            raise
        except BaseException as restore_error:
            raise AuditIntegrityError("Email verification audit failure could not restore retryable state") from restore_error

    def register_email_verified_user(
        self,
        user_id: str,
        password: str,
        display_name: str,
        email: str,
        *,
        verification_origin: str,
        outbox_path: Path,
    ) -> None:
        """Persist a resumable pending registration and publish its delivery.

        User, one-use token, and stable outbox intent commit in one SQLite
        transaction. Publication is idempotent by ``delivery_id``; a retry or
        process restart can therefore finish an append whose acknowledgement
        was interrupted without duplicating the message.
        """
        if not display_name:
            raise ValueError("display_name must not be empty")
        password_bytes = bcrypt_password_bytes(password)
        password_hash = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode()
        now = int(time.time())
        with self._connect(immediate=True) as conn:
            self._reap_stale_pending_registrations(conn, now=now)
            self._reclaim_stale_token_audit_intents(conn, now=now)
            existing = conn.execute(
                "SELECT password_hash, display_name, email, email_verified FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO users (user_id, password_hash, display_name, email, email_verified) VALUES (?, ?, ?, ?, 0)",
                    (user_id, password_hash, display_name, email),
                )
            else:
                existing_hash, existing_name, existing_email, email_verified = existing
                matches_pending_registration = (
                    not email_verified
                    and existing_name == display_name
                    and existing_email == email
                    and bcrypt.checkpw(password_bytes, existing_hash.encode())
                )
                if not matches_pending_registration:
                    raise LocalAuthRegistrationConflict(f"User already exists: {user_id}")

            active_delivery = conn.execute(
                """
                SELECT 1
                FROM email_verification_tokens AS tokens
                JOIN email_verification_outbox AS outbox ON outbox.token_hash = tokens.token_hash
                WHERE tokens.user_id = ? AND tokens.used_at IS NULL AND tokens.expires_at >= ?
                LIMIT 1
                """,
                (user_id, now),
            ).fetchone()
            if active_delivery is None:
                conn.execute("DELETE FROM email_verification_outbox WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM email_verification_tokens WHERE user_id = ? AND used_at IS NULL", (user_id,))
                self._insert_verification_delivery(
                    conn,
                    user_id=user_id,
                    email=email,
                    verification_origin=verification_origin,
                    now=now,
                )
        self.publish_pending_email_verifications(outbox_path)

    def publish_pending_email_verifications(self, outbox_path: Path) -> None:
        """Publish and acknowledge every durable verification intent."""
        with self._connect() as conn:
            pending = conn.execute(
                """
                SELECT delivery_id, payload_json
                FROM email_verification_outbox
                WHERE published_at IS NULL
                ORDER BY created_at, delivery_id
                """
            ).fetchall()
        for delivery_id, payload_json in pending:
            try:
                record = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise AuditIntegrityError("Stored email verification outbox payload is malformed") from exc
            if type(record) is not dict:
                raise AuditIntegrityError("Stored email verification outbox binding is malformed")
            try:
                stored_delivery_id = record["delivery_id"]
            except KeyError as exc:
                raise AuditIntegrityError("Stored email verification outbox binding is malformed") from exc
            if stored_delivery_id != delivery_id:
                raise AuditIntegrityError("Stored email verification outbox binding is malformed")
            _append_email_verification_record(outbox_path, record)
            with self._connect(immediate=True) as conn:
                conn.execute(
                    "UPDATE email_verification_outbox SET published_at = ? WHERE delivery_id = ? AND published_at IS NULL",
                    (int(time.time()), delivery_id),
                )

    def verify_email_and_issue_token(
        self,
        token: str,
        *,
        record_token_issued: Callable[[UserIdentity, str], None],
    ) -> str:
        """Consume, activate, and commit under one SQLite write fence, then audit.

        Exactly one caller can claim the one-use token. The claim commits
        together with a durable ``token_issued`` audit intent, then the
        required Landscape write happens and the intent is cleared. A failed
        commit therefore cannot leave a ``token_issued`` record for a token
        that was never consumed, and a crash between commit and delivery
        leaves an intent that the reclaim sweep resolves by restoring the
        retryable unverified state. If the Landscape write fails in-process,
        a compensating transaction performs the same restoration and grants a
        bounded retry window before the original exception propagates; if
        that restoration fails, the inconsistency surfaces as
        :class:`AuditIntegrityError`.
        """
        token_hash = _verification_token_hash(token)
        now = int(time.time())
        intent_id = secrets.token_urlsafe(18)
        with self._connect(immediate=True) as conn:
            self._reclaim_stale_token_audit_intents(conn, now=now)
            row = conn.execute(
                """
                SELECT tokens.user_id, tokens.expires_at, tokens.used_at, users.email_verified
                FROM email_verification_tokens AS tokens
                JOIN users ON users.user_id = tokens.user_id
                WHERE tokens.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                raise AuthenticationError("Invalid email verification token")
            user_id, expires_at, used_at, email_verified = row
            if used_at is not None:
                raise AuthenticationError("Email verification token already used")
            if expires_at <= now:
                raise AuthenticationError("Email verification token expired")
            if email_verified:
                raise AuthenticationError("Email already verified")
            token_result = conn.execute(
                """
                UPDATE email_verification_tokens
                SET used_at = ?
                WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
                """,
                (now, token_hash, now),
            )
            user_result = conn.execute(
                "UPDATE users SET email_verified = 1 WHERE user_id = ? AND email_verified = 0",
                (user_id,),
            )
            if token_result.rowcount != 1 or user_result.rowcount != 1:
                raise AuditIntegrityError("Email verification claim lost its transaction precondition")

            conn.execute(
                """
                INSERT INTO token_audit_intents
                    (intent_id, user_id, issuance_path, token_hash, claimed_at, created_at)
                VALUES (?, ?, 'email_verification', ?, ?, ?)
                """,
                (intent_id, user_id, token_hash, now, now),
            )
            identity = UserIdentity(user_id=user_id, username=user_id)
            access_token = self._issue_token(user_id, user_id)

        try:
            record_token_issued(identity, access_token)
        except BaseException as audit_error:
            self._restore_retryable_verification_or_raise(
                user_id,
                token_hash=token_hash,
                claimed_at=now,
                intent_id=intent_id,
            )
            raise audit_error
        try:
            with self._connect(immediate=True) as conn:
                delivery_owned = self._clear_token_audit_intent(
                    conn,
                    intent_id=intent_id,
                    user_id=user_id,
                    issuance_path="email_verification",
                )
        except BaseException as mark_error:
            self._restore_retryable_verification_or_raise(
                user_id,
                token_hash=token_hash,
                claimed_at=now,
                intent_id=intent_id,
            )
            raise AuditIntegrityError(
                "Email verification was audited but its audit intent could not be cleared; "
                "the verification was restored for an audited retry"
            ) from mark_error
        if not delivery_owned:
            # The sweep that removed this exact intent already restored its
            # auth state. A second compensation could clobber a later retry.
            raise AuditIntegrityError("Token audit intent ownership was lost before delivery completion")
        return access_token

    async def login(self, username: str, password: str) -> str:
        """Authenticate with username/password and return a JWT.

        Raises AuthenticationError("Invalid credentials") on failure.
        Uses constant-time comparison to prevent username enumeration
        via timing side-channel.

        Blocking bcrypt/sqlite work is offloaded to a bounded worker.
        """
        return await run_sync_in_worker(self._login_sync, username, password)

    def _login_sync(self, username: str, password: str) -> str:
        """Synchronous login — called via run_sync_in_worker."""
        # Early rejection for empty credentials. This exits before the
        # bcrypt path, so it is faster than a real login attempt. This is
        # acceptable: empty credentials are syntactically invalid (not a
        # guessable input), so the timing difference does not enable
        # credential enumeration.
        if not username or not password:
            raise AuthenticationError("Invalid credentials")
        try:
            password_bytes = bcrypt_password_bytes(password)
        except ValueError as exc:
            raise AuthenticationError("Invalid credentials") from exc

        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_hash, email_verified FROM users WHERE user_id = ?",
                (username,),
            ).fetchone()

        if row is None:
            # Constant-time: hash against dummy to prevent timing oracle
            bcrypt.checkpw(password_bytes, self._get_dummy_hash())
            raise AuthenticationError("Invalid credentials")

        if not bcrypt.checkpw(password_bytes, row[0].encode()):
            raise AuthenticationError("Invalid credentials")

        if not row[1]:
            raise AuthenticationError("Email verification required")

        return self._issue_token(username, username)

    def _issue_token(self, user_id: str, username: str, *, issued_at: int | None = None) -> str:
        now = int(time.time())
        iat = now if issued_at is None else issued_at
        payload = {
            "sub": user_id,
            "username": username,
            "iat": iat,
            "exp": now + self._token_expiry_hours * 3600,
        }
        token: str = jwt.encode(payload, self._secret_key, algorithm="HS256")
        return token

    async def refresh(self, user_id: str, username: str, *, original_iat: int) -> str:
        """Issue a new JWT for an already-authenticated user.

        Verifies the user still exists in the database — a deleted
        user must not be able to obtain fresh tokens via refresh.

        Called by the token refresh route. Does NOT re-verify
        credentials — the caller (get_current_user middleware)
        has already validated the existing token.

        Blocking sqlite work is offloaded to a bounded worker.
        """
        return await run_sync_in_worker(self._refresh_sync, user_id, username, original_iat)

    def _refresh_sync(self, user_id: str, username: str, original_iat: int | None) -> str:
        """Synchronous refresh — called via run_sync_in_worker."""
        now = int(time.time())

        # Max refresh chain: reject if the original token was issued too
        # long ago.  This bounds how long a stolen token can be refreshed
        # indefinitely without re-authentication.  Without a session DB
        # (Sub-2c/2d), this is the only revocation-like mechanism.
        if original_iat is None:
            raise AuthenticationError("Token missing iat — please re-authenticate")
        chain_age_hours = (now - original_iat) / 3600
        if chain_age_hours > self._max_refresh_chain_hours:
            raise AuthenticationError("Token refresh chain expired — please re-authenticate")

        with self._connect() as conn:
            row = conn.execute(
                "SELECT email_verified FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            raise AuthenticationError("User not found")
        if not row[0]:
            raise AuthenticationError("Email verification required")

        # Carry forward the original iat so the chain age accumulates.
        # New logins get a fresh iat; refreshes preserve the original.
        return self._issue_token(user_id, username, issued_at=original_iat)

    async def authenticate(self, token: str) -> UserIdentity:
        """Validate a JWT and return the authenticated identity.

        Raises AuthenticationError("Invalid token") on decode failure, expiry,
        or if the user has been deleted since the token was issued.
        """
        try:
            payload = jwt.decode(token, self._secret_key, algorithms=["HS256"])
        except PyJWTError as exc:
            raise AuthenticationError("Invalid token") from exc

        user_id = _required_visible_string_claim(payload, "sub")
        username = _required_visible_string_claim(payload, "username")

        # Verify user still exists — deleted users must not retain access
        exists = await run_sync_in_worker(self._user_exists, user_id)
        if not exists:
            raise AuthenticationError("Invalid token")

        return UserIdentity(
            user_id=user_id,
            username=username,
        )

    def _user_exists(self, user_id: str) -> bool:
        """Check if a verified user still exists in auth.db."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ? AND email_verified = 1",
                (user_id,),
            ).fetchone()
            return row is not None

    def _query_user(self, user_id: str) -> tuple[str, str | None] | None:
        """Synchronous DB lookup — called via run_sync_in_worker."""
        with self._connect() as conn:
            row: tuple[str, str | None] | None = conn.execute(
                "SELECT display_name, email FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return row

    async def get_user_info(self, token: str) -> UserProfile:
        """Decode the JWT, then query the users table for full profile.

        The DB query is offloaded to a thread to avoid blocking the
        event loop — sqlite3 is synchronous.
        """
        identity = await self.authenticate(token)

        row = await run_sync_in_worker(self._query_user, identity.user_id)

        if row is None:
            raise AuthenticationError("User not found")

        return UserProfile(
            user_id=identity.user_id,
            username=identity.username,
            display_name=row[0],
            email=row[1],
        )
