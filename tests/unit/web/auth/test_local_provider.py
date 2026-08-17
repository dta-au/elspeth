"""Tests for LocalAuthProvider -- SQLite user store, bcrypt hashing, JWT tokens."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Any

import jwt as pyjwt
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.auth import local as auth_local
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.auth.models import AuthenticationError, UserIdentity, UserProfile


class _CommitFailingConnection:
    """A ``sqlite3.Connection`` stand-in whose commit always fails.

    The delegated surface is written out explicitly rather than forwarded:
    ``LocalAuthProvider`` reaches for exactly ``execute``/``rollback``/
    ``close``/``commit`` on the connection it opens, so a double that
    declares those four models the real contract. A forwarding
    ``__getattr__`` would instead let a provider change reach an
    undeclared method and silently keep passing.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return self._real.execute(sql, parameters)

    def rollback(self) -> None:
        self._real.rollback()

    def close(self) -> None:
        self._real.close()

    def commit(self) -> None:
        raise sqlite3.OperationalError("simulated disk I/O error at commit")


def _fail_commits(provider: LocalAuthProvider, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every subsequent provider transaction fail at commit time."""
    real_get_conn = provider._get_conn

    def failing_get_conn() -> Any:
        return _CommitFailingConnection(real_get_conn())

    monkeypatch.setattr(provider, "_get_conn", failing_get_conn)


@pytest.fixture
def provider(tmp_path):
    """Create a LocalAuthProvider with a temporary SQLite database."""
    return LocalAuthProvider(
        db_path=tmp_path / "auth.db",
        secret_key="test-secret-key-for-unit-tests",
        token_expiry_hours=24,
    )


def _signed_local_token(provider: LocalAuthProvider, claims: dict[str, Any]) -> str:
    """Create a signed local JWT for boundary-shape tests."""
    return pyjwt.encode(claims, provider._secret_key, algorithm="HS256")


def _delete_user(provider: LocalAuthProvider, user_id: str) -> None:
    """Delete a test user without leaking sqlite3's transaction-only context manager."""
    with closing(sqlite3.connect(str(provider._db_path))) as conn, conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))


def _audit_intents(provider: LocalAuthProvider) -> list[tuple[str, str]]:
    """Read surviving token_issued audit intents directly from auth.db."""
    with closing(sqlite3.connect(str(provider._db_path))) as conn:
        return conn.execute("SELECT user_id, issuance_path FROM token_audit_intents ORDER BY created_at, intent_id").fetchall()


def _insert_crashed_intent(
    provider: LocalAuthProvider,
    *,
    user_id: str,
    issuance_path: str,
    created_at: int,
    token_hash: str | None = None,
    claimed_at: int | None = None,
) -> None:
    """Model a process crash that left a committed, undelivered audit intent."""
    with provider._connect(immediate=True) as conn:
        conn.execute(
            """
            INSERT INTO token_audit_intents
                (intent_id, user_id, issuance_path, token_hash, claimed_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (f"crashed-{user_id}", user_id, issuance_path, token_hash, claimed_at, created_at),
        )


class TestCreateUser:
    """Tests for user creation."""

    @pytest.mark.parametrize(
        ("password", "is_valid"),
        [
            ("a" * 72, True),
            ("a" * 73, False),
            ("é" * 36, True),
            ("é" * 36 + "a", False),
        ],
    )
    def test_create_user_enforces_bcrypt_password_byte_limit(self, provider, password: str, is_valid: bool) -> None:
        if is_valid:
            provider.create_user("alice", password, display_name="Alice")
        else:
            with pytest.raises(ValueError, match="72 bytes"):
                provider.create_user("alice", password, display_name="Alice")

    def test_create_user_succeeds(self, provider) -> None:
        provider.create_user("alice", "password123", display_name="Alice Smith")
        # No exception means success

    def test_create_user_with_email(self, provider) -> None:
        provider.create_user(
            "alice",
            "password123",
            display_name="Alice Smith",
            email="alice@example.com",
        )

    def test_create_duplicate_user_raises_value_error(self, provider) -> None:
        provider.create_user("alice", "password123", display_name="Alice")
        with pytest.raises(ValueError, match="alice"):
            provider.create_user("alice", "other-password", display_name="Alice 2")

    def test_create_user_empty_display_name_raises(self, provider) -> None:
        with pytest.raises(ValueError, match="display_name must not be empty"):
            provider.create_user("alice", "password123", display_name="")

    def test_auth_database_is_created_owner_only_under_permissive_umask(self, tmp_path) -> None:
        db_path = tmp_path / "auth.db"
        script = """
import os
import stat
import sys
from pathlib import Path
from elspeth.web.auth.local import LocalAuthProvider

os.umask(0)
path = Path(sys.argv[1])
LocalAuthProvider(db_path=path, secret_key="subprocess-test-key")
print(oct(stat.S_IMODE(path.stat().st_mode)))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(db_path)],
            check=True,
            capture_output=True,
            text=True,
        )

        assert completed.stdout.strip() == "0o600"

    def test_auth_database_rejects_unsafe_existing_mode(self, tmp_path) -> None:
        db_path = tmp_path / "auth.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE existing (value INTEGER)")
        db_path.chmod(0o644)

        with pytest.raises(RuntimeError, match="owner-only"):
            LocalAuthProvider(db_path=db_path, secret_key="test-key")

    def test_auth_database_rejects_symlink(self, tmp_path) -> None:
        target = tmp_path / "target.db"
        LocalAuthProvider(db_path=target, secret_key="test-key")
        db_path = tmp_path / "auth.db"
        db_path.symlink_to(target)

        with pytest.raises(RuntimeError, match="regular owner-only file"):
            LocalAuthProvider(db_path=db_path, secret_key="test-key")

    @pytest.mark.asyncio
    async def test_unverified_user_cannot_login_until_email_token_is_verified(self, provider) -> None:
        provider.create_user(
            "alice",
            "password123",
            display_name="Alice",
            email="alice@example.com",
            email_verified=False,
        )
        token = provider.create_email_verification_token("alice")

        with pytest.raises(AuthenticationError, match="Email verification required"):
            await provider.login("alice", "password123")

        verified_token = provider.verify_email_and_issue_token(
            token,
            record_token_issued=lambda _identity, _access_token: None,
        )
        assert len(verified_token.split(".")) == 3
        login_token = await provider.login("alice", "password123")
        assert len(login_token.split(".")) == 3

        with pytest.raises(AuthenticationError, match="already used"):
            provider.verify_email_and_issue_token(
                token,
                record_token_issued=lambda _identity, _access_token: None,
            )

    @pytest.mark.asyncio
    async def test_delete_user_removes_account_and_invalidates_tokens(self, provider) -> None:
        provider.create_user("alice", "password123", display_name="Alice")
        token = await provider.login("alice", "password123")

        assert provider.delete_user("alice") is True
        assert provider.delete_user("alice") is False

        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.authenticate(token)

    def test_open_registration_audit_failure_removes_the_committed_user(self, provider) -> None:
        """Audit runs after the durable commit; a failed audit compensates.

        The user is durable while the required audit callback runs (so no
        phantom token_issued record can precede the account), and an audit
        failure deletes the unaudited account before the error propagates.
        """
        audit_entered = threading.Event()
        release_audit = threading.Event()

        def fail_required_audit(_token: str) -> None:
            audit_entered.set()
            assert release_audit.wait(timeout=2)
            raise OSError("Landscape unavailable")

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                provider.register_open_user_with_audit,
                "alice",
                "password123",
                "Alice",
                None,
                record_token_issued=fail_required_audit,
            )
            assert audit_entered.wait(timeout=2)
            # The account is durable before the audit callback observes it.
            token = provider._login_sync("alice", "password123")
            assert len(token.split(".")) == 3
            release_audit.set()
            with pytest.raises(OSError, match="Landscape unavailable"):
                future.result(timeout=2)

        # The audit failure compensated: the unaudited account is gone.
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            provider._login_sync("alice", "password123")

    @pytest.mark.asyncio
    async def test_cancelled_open_registration_finishes_audit_and_state_together(self, provider) -> None:
        audit_entered = threading.Event()
        release_audit = threading.Event()
        audit_finished = threading.Event()

        def record_required_audit(_token: str) -> None:
            audit_entered.set()
            assert release_audit.wait(timeout=2)
            audit_finished.set()

        task = asyncio.create_task(
            run_sync_in_worker(
                provider.register_open_user_with_audit,
                "alice",
                "password123",
                "Alice",
                None,
                record_token_issued=record_required_audit,
            )
        )
        assert await asyncio.to_thread(audit_entered.wait, 2)
        task.cancel()
        release_audit.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(audit_finished.wait, 2)

        token = await provider.login("alice", "password123")
        assert len(token.split(".")) == 3

    def test_registration_commit_failure_after_audit_emits_no_phantom_token_issued(
        self,
        provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed auth.db commit must not leave a token_issued audit record.

        The external audit callback must only observe issuance once the user
        row is durable; otherwise Landscape asserts a token was issued for a
        user that never existed.
        """
        issued_tokens: list[str] = []
        _fail_commits(provider, monkeypatch)

        with pytest.raises(sqlite3.OperationalError, match="simulated disk I/O error"):
            provider.register_open_user_with_audit(
                "alice",
                "password123",
                "Alice",
                None,
                record_token_issued=issued_tokens.append,
            )
        monkeypatch.undo()

        assert issued_tokens == []
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            provider._login_sync("alice", "password123")

    def test_verification_commit_failure_after_audit_emits_no_phantom_token_issued(
        self,
        provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A failed verification commit must not leave a token_issued record."""
        provider.create_user(
            "alice",
            "password123",
            display_name="Alice",
            email="alice@example.com",
            email_verified=False,
        )
        token = provider.create_email_verification_token("alice")
        issued_tokens: list[str] = []
        _fail_commits(provider, monkeypatch)

        with pytest.raises(sqlite3.OperationalError, match="simulated disk I/O error"):
            provider.verify_email_and_issue_token(
                token,
                record_token_issued=lambda _identity, access_token: issued_tokens.append(access_token),
            )
        monkeypatch.undo()

        assert issued_tokens == []
        # The rollback restored the claimable token: verification still works.
        access_token = provider.verify_email_and_issue_token(
            token,
            record_token_issued=lambda _identity, _access_token: None,
        )
        assert len(access_token.split(".")) == 3

    def test_registration_audit_failure_with_failed_cleanup_surfaces_audit_integrity_error(
        self,
        provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the compensating cleanup fails too, the inconsistency is loud.

        The surviving durable intent then lets a later drain point reclaim
        the unaudited account instead of leaving it silently durable.
        """
        registered_at = int(time.time())

        def fail_required_audit(_token: str) -> None:
            _fail_commits(provider, monkeypatch)
            raise OSError("Landscape unavailable")

        with pytest.raises(AuditIntegrityError, match="cleanup"):
            provider.register_open_user_with_audit(
                "alice",
                "password123",
                "Alice",
                None,
                record_token_issued=fail_required_audit,
            )
        monkeypatch.undo()

        # The account and its intent survived the failed cleanup...
        assert _audit_intents(provider) == [("alice", "register")]
        # ...and the next drain point past the grace window quarantines it.
        monkeypatch.setattr(
            auth_local.time,
            "time",
            lambda: registered_at + auth_local._TOKEN_AUDIT_INTENT_GRACE_SECONDS + 1,
        )
        provider.register_open_user_with_audit(
            "carol",
            "password789",
            "Carol",
            None,
            record_token_issued=lambda _token: None,
        )
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            provider._login_sync("alice", "password123")

    def test_registration_audit_intent_is_durable_until_delivered(self, provider) -> None:
        """The commit durably records an undelivered intent, cleared on delivery."""
        audit_entered = threading.Event()
        release_audit = threading.Event()

        def record_required_audit(_token: str) -> None:
            audit_entered.set()
            assert release_audit.wait(timeout=2)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                provider.register_open_user_with_audit,
                "alice",
                "password123",
                "Alice",
                None,
                record_token_issued=record_required_audit,
            )
            assert audit_entered.wait(timeout=2)
            # The durable pre-delivery window is marked, never silent.
            assert _audit_intents(provider) == [("alice", "register")]
            release_audit.set()
            token = future.result(timeout=2)

        assert len(token.split(".")) == 3
        assert _audit_intents(provider) == []

    def test_reclaimed_active_registration_cannot_return_token(
        self,
        provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale-intent sweep must fence a callback that later completes."""
        now = [1_000_000]
        monkeypatch.setattr(auth_local.time, "time", lambda: now[0])
        audit_entered = threading.Event()
        release_audit = threading.Event()

        def record_required_audit(_token: str) -> None:
            audit_entered.set()
            assert release_audit.wait(timeout=2)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                provider.register_open_user_with_audit,
                "alice",
                "password123",
                "Alice",
                None,
                record_token_issued=record_required_audit,
            )
            assert audit_entered.wait(timeout=2)
            now[0] += auth_local._TOKEN_AUDIT_INTENT_GRACE_SECONDS + 1
            restarted = LocalAuthProvider(
                db_path=provider._db_path,
                secret_key="test-secret-key-for-unit-tests",
            )
            replacement_token = restarted.register_open_user_with_audit(
                "alice",
                "replacement456",
                "Replacement Alice",
                None,
                record_token_issued=lambda _token: None,
            )
            release_audit.set()

            with pytest.raises(AuditIntegrityError):
                future.result(timeout=2)

        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            provider._login_sync("alice", "password123")
        assert len(replacement_token.split(".")) == 3
        assert len(restarted._login_sync("alice", "replacement456").split(".")) == 3

    def test_reclaimed_registration_audit_failure_spares_replacement_account(
        self,
        provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A late audit failure must not compensate away a replacement account.

        Once the stale-intent sweep has reclaimed the original registration,
        the same user_id may belong to a replacement registration; the
        original call's compensating cleanup is fenced to its own intent
        generation and must leave the replacement untouched.
        """
        now = [1_000_000]
        monkeypatch.setattr(auth_local.time, "time", lambda: now[0])
        audit_entered = threading.Event()
        release_audit = threading.Event()

        def fail_required_audit(_token: str) -> None:
            audit_entered.set()
            assert release_audit.wait(timeout=2)
            raise OSError("Landscape unavailable")

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                provider.register_open_user_with_audit,
                "alice",
                "password123",
                "Alice",
                None,
                record_token_issued=fail_required_audit,
            )
            assert audit_entered.wait(timeout=2)
            now[0] += auth_local._TOKEN_AUDIT_INTENT_GRACE_SECONDS + 1
            restarted = LocalAuthProvider(
                db_path=provider._db_path,
                secret_key="test-secret-key-for-unit-tests",
            )
            replacement_token = restarted.register_open_user_with_audit(
                "alice",
                "replacement456",
                "Replacement Alice",
                None,
                record_token_issued=lambda _token: None,
            )
            release_audit.set()

            with pytest.raises(OSError, match="Landscape unavailable"):
                future.result(timeout=2)

        # The replacement account survived the fenced compensation.
        assert len(replacement_token.split(".")) == 3
        assert len(restarted._login_sync("alice", "replacement456").split(".")) == 3
        assert _audit_intents(provider) == []

    def test_compensation_is_noop_once_intent_ownership_is_lost(self, provider) -> None:
        """Compensation keyed to a consumed intent must not touch a replacement."""
        provider.create_user("alice", "replacement456", display_name="Replacement Alice")

        owned = provider._compensate_open_registration("alice", intent_id="original-generation")

        assert owned is False
        assert len(provider._login_sync("alice", "replacement456").split(".")) == 3

    def test_reclaimed_active_verification_cannot_return_token(
        self,
        provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A reclaimed verification cannot publish a usable access token."""
        now = [1_000_000]
        monkeypatch.setattr(auth_local.time, "time", lambda: now[0])
        provider.create_user(
            "alice",
            "password123",
            display_name="Alice",
            email="alice@example.com",
            email_verified=False,
        )
        verification_token = provider.create_email_verification_token("alice")
        audit_entered = threading.Event()
        release_audit = threading.Event()

        def record_required_audit(_identity: UserIdentity, _access_token: str) -> None:
            audit_entered.set()
            assert release_audit.wait(timeout=2)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                provider.verify_email_and_issue_token,
                verification_token,
                record_token_issued=record_required_audit,
            )
            assert audit_entered.wait(timeout=2)
            now[0] += auth_local._TOKEN_AUDIT_INTENT_GRACE_SECONDS + 1
            restarted = LocalAuthProvider(
                db_path=provider._db_path,
                secret_key="test-secret-key-for-unit-tests",
            )
            retry_token = restarted.verify_email_and_issue_token(
                verification_token,
                record_token_issued=lambda _identity, _access_token: None,
            )
            release_audit.set()

            with pytest.raises(AuditIntegrityError):
                future.result(timeout=2)

        assert len(retry_token.split(".")) == 3
        assert len(restarted._login_sync("alice", "password123").split(".")) == 3

    def test_startup_reclaims_crashed_registration_audit_intent(self, provider) -> None:
        """Crash between commit and delivery: startup quarantines the account."""
        provider.create_user("alice", "password123", display_name="Alice")
        _insert_crashed_intent(provider, user_id="alice", issuance_path="register", created_at=1_000)

        restarted = LocalAuthProvider(
            db_path=provider._db_path,
            secret_key="test-secret-key-for-unit-tests",
        )

        assert _audit_intents(restarted) == []
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            restarted._login_sync("alice", "password123")

    def test_startup_reclaims_crashed_verification_audit_intent(
        self,
        provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Crash between claim commit and delivery: startup restores retryability."""
        now = [1_000_000]
        monkeypatch.setattr(auth_local.time, "time", lambda: now[0])
        provider.create_user(
            "alice",
            "password123",
            display_name="Alice",
            email="alice@example.com",
            email_verified=False,
        )
        token = provider.create_email_verification_token("alice")
        token_hash = auth_local._verification_token_hash(token)
        # Model the crash: the claim committed durably, delivery never happened.
        with provider._connect(immediate=True) as conn:
            conn.execute(
                "UPDATE email_verification_tokens SET used_at = ? WHERE token_hash = ?",
                (now[0], token_hash),
            )
            conn.execute("UPDATE users SET email_verified = 1 WHERE user_id = 'alice'")
        _insert_crashed_intent(
            provider,
            user_id="alice",
            issuance_path="email_verification",
            created_at=now[0],
            token_hash=token_hash,
            claimed_at=now[0],
        )

        now[0] += auth_local._TOKEN_AUDIT_INTENT_GRACE_SECONDS + 1
        restarted = LocalAuthProvider(
            db_path=provider._db_path,
            secret_key="test-secret-key-for-unit-tests",
        )

        assert _audit_intents(restarted) == []
        issued_tokens: list[str] = []
        access_token = restarted.verify_email_and_issue_token(
            token,
            record_token_issued=lambda _identity, issued: issued_tokens.append(issued),
        )
        assert len(access_token.split(".")) == 3
        assert issued_tokens == [access_token]

    def test_fresh_audit_intents_survive_restart_within_grace(self, provider) -> None:
        """In-flight deliveries are protected from concurrent startup sweeps."""
        provider.create_user("alice", "password123", display_name="Alice")
        _insert_crashed_intent(
            provider,
            user_id="alice",
            issuance_path="register",
            created_at=int(time.time()),
        )

        restarted = LocalAuthProvider(
            db_path=provider._db_path,
            secret_key="test-secret-key-for-unit-tests",
        )

        assert _audit_intents(restarted) == [("alice", "register")]
        token = restarted._login_sync("alice", "password123")
        assert len(token.split(".")) == 3

    def test_stale_registration_intent_is_reclaimed_by_next_registration(self, provider) -> None:
        """The next-operation drain resolves crashed intents without a restart."""
        provider.create_user("alice", "password123", display_name="Alice")
        _insert_crashed_intent(provider, user_id="alice", issuance_path="register", created_at=1_000)

        bob_token = provider.register_open_user_with_audit(
            "bob",
            "password456",
            "Bob",
            None,
            record_token_issued=lambda _token: None,
        )

        assert len(bob_token.split(".")) == 3
        assert _audit_intents(provider) == []
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            provider._login_sync("alice", "password123")
        assert len(provider._login_sync("bob", "password456").split(".")) == 3

    def test_delivered_audit_with_stuck_intent_fails_closed(
        self,
        provider,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the intent cannot be cleared after delivery, the failure is loud."""
        issued_tokens: list[str] = []

        def record_then_break_commits(access_token: str) -> None:
            issued_tokens.append(access_token)
            _fail_commits(provider, monkeypatch)

        with pytest.raises(AuditIntegrityError, match="could not be cleared"):
            provider.register_open_user_with_audit(
                "alice",
                "password123",
                "Alice",
                None,
                record_token_issued=record_then_break_commits,
            )
        monkeypatch.undo()

        assert len(issued_tokens) == 1
        # The intent survives, so a later drain point resolves the account.
        assert _audit_intents(provider) == [("alice", "register")]

    def test_email_verification_token_has_exactly_one_concurrent_consumer(self, provider) -> None:
        provider.create_user(
            "alice",
            "password123",
            display_name="Alice",
            email="alice@example.com",
            email_verified=False,
        )
        token = provider.create_email_verification_token("alice")

        def consume() -> str:
            return provider.verify_email_and_issue_token(
                token,
                record_token_issued=lambda _identity, _access_token: None,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(consume) for _ in range(2)]
        outcomes: list[str] = []
        failures: list[BaseException] = []
        for future in futures:
            try:
                outcomes.append(future.result())
            except BaseException as exc:
                failures.append(exc)

        assert len(outcomes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], AuthenticationError)

    def test_email_verification_token_is_expired_at_boundary(self, provider, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(auth_local.time, "time", lambda: 1_000)
        provider.create_user(
            "alice",
            "password123",
            display_name="Alice",
            email="alice@example.com",
            email_verified=False,
        )
        token = provider.create_email_verification_token("alice", ttl_seconds=0)
        issued_tokens: list[str] = []

        with pytest.raises(AuthenticationError, match="expired"):
            provider.verify_email_and_issue_token(
                token,
                record_token_issued=lambda _identity, access_token: issued_tokens.append(access_token),
            )

        assert issued_tokens == []
        with provider._connect() as conn:
            state = conn.execute(
                """
                SELECT tokens.used_at, users.email_verified
                FROM email_verification_tokens AS tokens
                JOIN users ON users.user_id = tokens.user_id
                WHERE tokens.user_id = ?
                """,
                ("alice",),
            ).fetchone()
        assert state == (None, 0)

    def test_verification_audit_failure_restores_bounded_retry_lifetime(self, provider, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1_000]
        monkeypatch.setattr(auth_local.time, "time", lambda: now[0])
        provider.create_user(
            "alice",
            "password123",
            display_name="Alice",
            email="alice@example.com",
            email_verified=False,
        )
        token = provider.create_email_verification_token("alice", ttl_seconds=1)

        def fail_required_audit(_identity: UserIdentity, _access_token: str) -> None:
            now[0] = 1_001
            raise OSError("Landscape unavailable")

        with pytest.raises(OSError, match="Landscape unavailable"):
            provider.verify_email_and_issue_token(token, record_token_issued=fail_required_audit)

        now[0] = 1_002
        access_token = provider.verify_email_and_issue_token(
            token,
            record_token_issued=lambda _identity, _access_token: None,
        )
        assert len(access_token.split(".")) == 3

    def test_email_registration_outbox_recovers_after_publish_failure_and_restart(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        provider = LocalAuthProvider(db_path=tmp_path / "auth.db", secret_key="test-key")
        outbox_path = tmp_path / "email-verifications.jsonl"
        real_append = auth_local._append_email_verification_record

        def fail_publish(*args, **kwargs) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(auth_local, "_append_email_verification_record", fail_publish)
        with pytest.raises(OSError, match="disk full"):
            provider.register_email_verified_user(
                "alice",
                "password123",
                "Alice",
                "alice@example.com",
                verification_origin="https://composer.example.test",
                outbox_path=outbox_path,
            )

        with pytest.raises(AuthenticationError, match="Email verification required"):
            provider._login_sync("alice", "password123")

        monkeypatch.setattr(auth_local, "_append_email_verification_record", real_append)
        restarted = LocalAuthProvider(db_path=tmp_path / "auth.db", secret_key="test-key")
        restarted.publish_pending_email_verifications(outbox_path)

        records = [json.loads(line) for line in outbox_path.read_text(encoding="utf-8").splitlines()]
        assert len(records) == 1
        assert records[0]["delivery_id"]
        assert records[0]["user_id"] == "alice"

    def test_email_outbox_partial_append_is_truncated_before_retry(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        outbox_path = tmp_path / "email-verifications.jsonl"
        record = {
            "delivery_id": "delivery-1",
            "user_id": "alice",
            "email": "alice@example.com",
            "token": "verification-token",
            "verification_url": "https://composer.example.test/?verify_token=verification-token",
        }
        real_write = auth_local.os.write

        def short_write(fd: int, payload: bytes) -> int:
            return real_write(fd, payload[: len(payload) // 2])

        monkeypatch.setattr(auth_local.os, "write", short_write)
        with pytest.raises(OSError, match="incomplete"):
            auth_local._append_email_verification_record(outbox_path, record)
        assert outbox_path.read_bytes() == b""

        monkeypatch.setattr(auth_local.os, "write", real_write)
        auth_local._append_email_verification_record(outbox_path, record)
        assert [json.loads(line) for line in outbox_path.read_text().splitlines()] == [record]

    def test_email_outbox_normalizes_valid_final_record_before_append(self, tmp_path) -> None:
        outbox_path = tmp_path / "email-verifications.jsonl"
        published = {
            "delivery_id": "published",
            "user_id": "alice",
            "email": "alice@example.com",
            "token": "published-token",
            "verification_url": "https://composer.example.test/?verify_token=published-token",
        }
        record = {
            "delivery_id": "delivery-1",
            "user_id": "bob",
            "email": "bob@example.com",
            "token": "next-token",
            "verification_url": "https://composer.example.test/?verify_token=next-token",
        }
        published_payload = json.dumps(published, sort_keys=True, separators=(",", ":")).encode()
        record_payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        outbox_path.write_bytes(published_payload)

        auth_local._append_email_verification_record(outbox_path, record)

        assert outbox_path.read_bytes() == published_payload + b"\n" + record_payload + b"\n"

    @pytest.mark.parametrize(
        "existing",
        [
            b'{"delivery_id":"crashed"',
            (
                b'{"delivery_id":"published","email":"published@example.com","token":"published-token",'
                b'"user_id":"published-user","verification_url":'
                b'"https://composer.example.test/?verify_token=published-token"}\n{"delivery_id":"crashed"'
            ),
        ],
        ids=["malformed-only", "mixed-valid-and-malformed"],
    )
    def test_email_outbox_rejects_malformed_final_record_without_mutation(
        self,
        tmp_path,
        existing: bytes,
    ) -> None:
        outbox_path = tmp_path / "email-verifications.jsonl"
        outbox_path.write_bytes(existing)
        record = {
            "delivery_id": "delivery-1",
            "user_id": "alice",
            "email": "alice@example.com",
            "token": "verification-token",
            "verification_url": "https://composer.example.test/?verify_token=verification-token",
        }

        with pytest.raises(auth_local.AuditIntegrityError, match="malformed"):
            auth_local._append_email_verification_record(outbox_path, record)

        assert outbox_path.read_bytes() == existing

    @pytest.mark.parametrize(
        "existing",
        [
            b'{"user_id":"corrupt"}',
            (
                b'{"delivery_id":"published","email":"published@example.com","token":"published-token",'
                b'"user_id":"published-user","verification_url":'
                b'"https://composer.example.test/?verify_token=published-token"}\n{"user_id":"corrupt"}'
            ),
        ],
        ids=["invalid-only", "mixed-valid-and-invalid"],
    )
    def test_email_outbox_rejects_final_record_without_delivery_id_without_mutation(
        self,
        tmp_path,
        existing: bytes,
    ) -> None:
        outbox_path = tmp_path / "email-verifications.jsonl"
        outbox_path.write_bytes(existing)
        record = {
            "delivery_id": "delivery-1",
            "user_id": "alice",
            "email": "alice@example.com",
            "token": "verification-token",
            "verification_url": "https://composer.example.test/?verify_token=verification-token",
        }

        with pytest.raises(auth_local.AuditIntegrityError, match="delivery_id"):
            auth_local._append_email_verification_record(outbox_path, record)

        assert outbox_path.read_bytes() == existing

    def test_retry_after_expiry_rotates_pending_registration_delivery(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        now = [1_000]
        monkeypatch.setattr(auth_local.time, "time", lambda: now[0])
        provider = LocalAuthProvider(db_path=tmp_path / "auth.db", secret_key="test-key")
        outbox_path = tmp_path / "email-verifications.jsonl"
        kwargs = {
            "verification_origin": "https://composer.example.test",
            "outbox_path": outbox_path,
        }
        provider.register_email_verified_user(
            "alice",
            "password123",
            "Alice",
            "alice@example.com",
            **kwargs,
        )
        first = json.loads(outbox_path.read_text().splitlines()[0])

        now[0] += auth_local._EMAIL_VERIFICATION_TOKEN_TTL_SECONDS + 1
        provider.register_email_verified_user(
            "alice",
            "password123",
            "Alice",
            "alice@example.com",
            **kwargs,
        )
        records = [json.loads(line) for line in outbox_path.read_text().splitlines()]

        assert len(records) == 2
        assert records[1]["delivery_id"] != first["delivery_id"]
        assert records[1]["token"] != first["token"]

    def test_publish_retry_deduplicates_append_before_ack_crash(self, tmp_path) -> None:
        provider = LocalAuthProvider(db_path=tmp_path / "auth.db", secret_key="test-key")
        outbox_path = tmp_path / "email-verifications.jsonl"
        provider.register_email_verified_user(
            "alice",
            "password123",
            "Alice",
            "alice@example.com",
            verification_origin="https://composer.example.test",
            outbox_path=outbox_path,
        )
        with provider._connect() as conn:
            conn.execute("UPDATE email_verification_outbox SET published_at = NULL")

        provider.publish_pending_email_verifications(outbox_path)

        records = [json.loads(line) for line in outbox_path.read_text().splitlines()]
        assert len(records) == 1

    def test_publish_retry_rejects_divergent_payload_for_existing_delivery_id(self, tmp_path) -> None:
        provider = LocalAuthProvider(db_path=tmp_path / "auth.db", secret_key="test-key")
        outbox_path = tmp_path / "email-verifications.jsonl"
        provider.register_email_verified_user(
            "alice",
            "password123",
            "Alice",
            "alice@example.com",
            verification_origin="https://composer.example.test",
            outbox_path=outbox_path,
        )
        intended = json.loads(outbox_path.read_text().splitlines()[0])
        divergent = dict(intended)
        divergent["user_id"] = "mallory"
        divergent["email"] = "mallory@example.com"
        divergent["token"] = "wrong-token"
        divergent["verification_url"] = "https://composer.example.test/?verify_token=wrong-token"
        original_bytes = (json.dumps(divergent, sort_keys=True, separators=(",", ":")) + "\n").encode()
        outbox_path.write_bytes(original_bytes)
        with provider._connect() as conn:
            conn.execute("UPDATE email_verification_outbox SET published_at = NULL")

        with pytest.raises(auth_local.AuditIntegrityError, match="does not match"):
            provider.publish_pending_email_verifications(outbox_path)

        assert outbox_path.read_bytes() == original_bytes
        with provider._connect() as conn:
            published_at = conn.execute(
                "SELECT published_at FROM email_verification_outbox WHERE delivery_id = ?",
                (intended["delivery_id"],),
            ).fetchone()[0]
        assert published_at is None

    def test_email_outbox_rejects_duplicate_delivery_id_records(self, tmp_path) -> None:
        outbox_path = tmp_path / "email-verifications.jsonl"
        published = {
            "delivery_id": "published",
            "user_id": "alice",
            "email": "alice@example.com",
            "token": "verification-token",
            "verification_url": "https://composer.example.test/?verify_token=verification-token",
        }
        record = {
            "delivery_id": "delivery-1",
            "user_id": "bob",
            "email": "bob@example.com",
            "token": "second-token",
            "verification_url": "https://composer.example.test/?verify_token=second-token",
        }
        published_line = (json.dumps(published, sort_keys=True, separators=(",", ":")) + "\n").encode()
        original_bytes = published_line + published_line
        outbox_path.write_bytes(original_bytes)

        with pytest.raises(auth_local.AuditIntegrityError, match="duplicate delivery_id"):
            auth_local._append_email_verification_record(outbox_path, record)

        assert outbox_path.read_bytes() == original_bytes

    def test_startup_reclaims_pending_registration_after_retention_window(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        now = [1_000]
        monkeypatch.setattr(auth_local.time, "time", lambda: now[0])
        provider = LocalAuthProvider(db_path=tmp_path / "auth.db", secret_key="test-key")
        provider.register_email_verified_user(
            "alice",
            "password123",
            "Alice",
            "alice@example.com",
            verification_origin="https://composer.example.test",
            outbox_path=tmp_path / "email-verifications.jsonl",
        )

        now[0] += auth_local._EMAIL_VERIFICATION_TOKEN_TTL_SECONDS + auth_local._PENDING_REGISTRATION_RETENTION_SECONDS + 1
        restarted = LocalAuthProvider(db_path=tmp_path / "auth.db", secret_key="test-key")
        restarted.create_user("alice", "replacement-password", display_name="Replacement")

        token = restarted._login_sync("alice", "replacement-password")
        assert len(token.split(".")) == 3


class TestLogin:
    """Tests for username/password login."""

    @pytest.mark.asyncio
    async def test_login_rejects_password_that_collides_after_bcrypt_limit(self, provider) -> None:
        password = "a" * 72
        provider.create_user("alice", password, display_name="Alice")

        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await provider.login("alice", password + "b")

    @pytest.mark.asyncio
    async def test_login_returns_jwt_string(self, provider) -> None:
        provider.create_user("alice", "password123", display_name="Alice")
        token = await provider.login("alice", "password123")
        assert isinstance(token, str)
        assert len(token) > 0
        # JWT has three dot-separated segments
        assert len(token.split(".")) == 3

    @pytest.mark.asyncio
    async def test_login_wrong_password_raises(self, provider) -> None:
        provider.create_user("alice", "password123", display_name="Alice")
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await provider.login("alice", "wrong-password")

    @pytest.mark.asyncio
    async def test_login_unknown_user_raises(self, provider) -> None:
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await provider.login("nonexistent", "password")


class TestAuthenticate:
    """Tests for JWT token validation."""

    @pytest.mark.asyncio
    async def test_authenticate_valid_token(self, provider) -> None:
        provider.create_user("alice", "pw", display_name="Alice")
        token = await provider.login("alice", "pw")
        identity = await provider.authenticate(token)
        assert isinstance(identity, UserIdentity)
        assert identity.user_id == "alice"
        assert identity.username == "alice"

    @pytest.mark.asyncio
    async def test_authenticate_garbage_token(self, provider) -> None:
        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.authenticate("garbage-not-a-jwt")

    @pytest.mark.asyncio
    async def test_authenticate_expired_token(self, tmp_path) -> None:
        """Token with 0-second expiry should fail after creation."""
        import jwt as pyjwt

        provider = LocalAuthProvider(
            db_path=tmp_path / "auth.db",
            secret_key="test-key",
            token_expiry_hours=24,
        )
        provider.create_user("alice", "pw", display_name="Alice")

        # Manually create an already-expired token
        payload = {
            "sub": "alice",
            "username": "alice",
            "exp": int(time.time()) - 10,  # 10 seconds in the past
        }
        expired_token = pyjwt.encode(payload, "test-key", algorithm="HS256")

        with pytest.raises(AuthenticationError):
            await provider.authenticate(expired_token)

    @pytest.mark.asyncio
    async def test_authenticate_deleted_user_rejected(self, provider) -> None:
        """A deleted user's JWT must be rejected by authenticate()."""
        provider.create_user("alice", "pw", display_name="Alice")
        token = await provider.login("alice", "pw")

        # Delete the user behind the provider's back
        _delete_user(provider, "alice")

        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.authenticate(token)

    @pytest.mark.asyncio
    async def test_authenticate_wrong_secret_key(self, tmp_path) -> None:
        """Token signed with a different key should fail."""
        provider = LocalAuthProvider(
            db_path=tmp_path / "auth.db",
            secret_key="correct-key",
        )
        payload = {
            "sub": "alice",
            "username": "alice",
            "exp": int(time.time()) + 3600,
        }
        bad_token = pyjwt.encode(payload, "wrong-key", algorithm="HS256")
        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.authenticate(bad_token)

    @pytest.mark.asyncio
    async def test_authenticate_missing_username_claim_raises_authentication_error(self, provider) -> None:
        """Signed local tokens without username must not escape as KeyError."""
        provider.create_user("alice", "pw", display_name="Alice")
        token = _signed_local_token(
            provider,
            {
                "sub": "alice",
                "exp": int(time.time()) + 3600,
            },
        )

        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.authenticate(token)

    @pytest.mark.asyncio
    async def test_authenticate_missing_sub_claim_raises_authentication_error(self, provider) -> None:
        """Signed local tokens without sub must not escape as KeyError."""
        token = _signed_local_token(
            provider,
            {
                "username": "alice",
                "exp": int(time.time()) + 3600,
            },
        )

        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.authenticate(token)

    @pytest.mark.asyncio
    async def test_authenticate_non_string_username_claim_raises_authentication_error(self, provider) -> None:
        """Signed local tokens with non-string username must not reach UserIdentity."""
        provider.create_user("alice", "pw", display_name="Alice")
        token = _signed_local_token(
            provider,
            {
                "sub": "alice",
                "username": {"name": "alice"},
                "exp": int(time.time()) + 3600,
            },
        )

        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.authenticate(token)

    @pytest.mark.asyncio
    async def test_authenticate_non_string_sub_claim_raises_authentication_error(self, provider) -> None:
        """Signed local tokens with non-string sub must not reach the user lookup."""
        token = _signed_local_token(
            provider,
            {
                "sub": {"id": "alice"},
                "username": "alice",
                "exp": int(time.time()) + 3600,
            },
        )

        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.authenticate(token)


class TestGetUserInfo:
    """Tests for full user profile retrieval."""

    @pytest.mark.asyncio
    async def test_get_user_info_returns_profile(self, provider) -> None:
        provider.create_user(
            "alice",
            "pw",
            display_name="Alice Smith",
            email="alice@example.com",
        )
        token = await provider.login("alice", "pw")
        profile = await provider.get_user_info(token)
        assert isinstance(profile, UserProfile)
        assert profile.user_id == "alice"
        assert profile.username == "alice"
        assert profile.display_name == "Alice Smith"
        assert profile.email == "alice@example.com"
        assert profile.groups == ()

    @pytest.mark.asyncio
    async def test_get_user_info_no_email(self, provider) -> None:
        provider.create_user("bob", "pw", display_name="Bob")
        token = await provider.login("bob", "pw")
        profile = await provider.get_user_info(token)
        assert profile.email is None

    @pytest.mark.asyncio
    async def test_get_user_info_invalid_token(self, provider) -> None:
        with pytest.raises(AuthenticationError):
            await provider.get_user_info("garbage-token")

    @pytest.mark.asyncio
    async def test_get_user_info_deleted_user(self, provider) -> None:
        """User deleted between login (token issued) and get_user_info call."""
        provider.create_user("alice", "pw", display_name="Alice")
        token = await provider.login("alice", "pw")

        # Access _db_path directly — no public API to delete users by design
        _delete_user(provider, "alice")

        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.get_user_info(token)


class TestLoginEdgeCases:
    """Edge-case tests for login input validation."""

    @pytest.mark.asyncio
    async def test_login_empty_username_raises(self, provider) -> None:
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await provider.login("", "some-password")

    @pytest.mark.asyncio
    async def test_login_empty_password_raises(self, provider) -> None:
        provider.create_user("alice", "pw", display_name="Alice")
        with pytest.raises(AuthenticationError, match="Invalid credentials"):
            await provider.login("alice", "")


class TestProtocolConformance:
    """Verify LocalAuthProvider satisfies the AuthProvider protocol."""

    def test_local_satisfies_auth_provider(self, provider) -> None:
        from elspeth.web.auth.protocol import AuthProvider

        assert isinstance(provider, AuthProvider)


class TestTimingDefense:
    """Verify constant-time behavior for unknown users."""

    @pytest.mark.asyncio
    async def test_login_unknown_user_still_hashes(self, provider) -> None:
        """Verify constant-time behavior: bcrypt.checkpw is called even for unknown users."""
        import unittest.mock as mock

        with mock.patch("elspeth.web.auth.local.bcrypt.checkpw", return_value=False) as mock_checkpw:
            with pytest.raises(AuthenticationError, match="Invalid credentials"):
                await provider.login("nonexistent", "password")
            # bcrypt.checkpw must be called even for nonexistent users (timing defense)
            mock_checkpw.assert_called_once()


class TestRefresh:
    """Tests for the token refresh method."""

    @pytest.mark.asyncio
    async def test_refresh_deleted_user_raises(self, provider) -> None:
        """A deleted user cannot obtain fresh tokens via refresh."""
        provider.create_user("alice", "pw", display_name="Alice")
        # Access _db_path directly — no public API to delete users by design
        _delete_user(provider, "alice")
        with pytest.raises(AuthenticationError, match="User not found"):
            await provider.refresh("alice", "alice", original_iat=int(time.time()))

    @pytest.mark.asyncio
    async def test_refresh_valid_user_returns_jwt(self, provider) -> None:
        provider.create_user("alice", "pw", display_name="Alice")
        token = await provider.refresh("alice", "alice", original_iat=int(time.time()))
        assert isinstance(token, str)
        assert len(token.split(".")) == 3

    @pytest.mark.asyncio
    async def test_refresh_with_iat_within_limit_succeeds(self, provider) -> None:
        """Refresh with original_iat within max_refresh_chain_hours succeeds."""
        provider.create_user("alice", "pw", display_name="Alice")
        recent_iat = int(time.time()) - 3600  # 1 hour ago
        token = await provider.refresh("alice", "alice", original_iat=recent_iat)
        assert isinstance(token, str)
        assert len(token.split(".")) == 3

    @pytest.mark.asyncio
    async def test_refresh_with_expired_chain_raises(self, provider) -> None:
        """Refresh with original_iat older than max_refresh_chain_hours raises."""
        provider.create_user("alice", "pw", display_name="Alice")
        # Default max_refresh_chain_hours=168 (7 days). Set iat to 8 days ago.
        old_iat = int(time.time()) - (8 * 24 * 3600)
        with pytest.raises(AuthenticationError, match="Token refresh chain expired"):
            await provider.refresh("alice", "alice", original_iat=old_iat)

    @pytest.mark.asyncio
    async def test_refresh_carries_original_iat_forward(self, provider) -> None:
        """Refreshed token preserves the original iat, not a fresh one."""
        import jwt

        provider.create_user("alice", "pw", display_name="Alice")
        original_iat = int(time.time()) - 7200  # 2 hours ago
        token = await provider.refresh("alice", "alice", original_iat=original_iat)
        claims = jwt.decode(token, "test-secret-key-for-unit-tests", algorithms=["HS256"])
        assert claims["iat"] == original_iat

    @pytest.mark.asyncio
    async def test_refresh_without_iat_raises(self, provider) -> None:
        """Refresh without original_iat must not start a fresh chain."""
        provider.create_user("alice", "pw", display_name="Alice")
        with pytest.raises(AuthenticationError, match="Token missing iat"):
            await provider.refresh("alice", "alice", original_iat=None)


class TestListUsers:
    """list_users powers the dev-admin user management surface."""

    def test_lists_accounts_sorted_by_user_id(self, provider) -> None:
        provider.create_user("bob", "password123", display_name="Bob", email="bob@example.com")
        provider.create_user("alice", "password123", display_name="Alice")

        accounts = provider.list_users()

        assert [account.user_id for account in accounts] == ["alice", "bob"]
        assert accounts[0].display_name == "Alice"
        assert accounts[0].email is None
        assert accounts[0].email_verified is True
        assert accounts[1].email == "bob@example.com"

    def test_reports_unverified_accounts(self, provider, tmp_path) -> None:
        provider.create_user("carol", "password123", display_name="Carol", email_verified=False)

        accounts = provider.list_users()

        assert [account.email_verified for account in accounts] == [False]

    def test_empty_store_lists_nothing(self, provider) -> None:
        assert provider.list_users() == []


class TestSetPassword:
    """set_password backs the dev-admin reset flow."""

    @pytest.mark.asyncio
    async def test_new_password_logs_in_and_old_password_fails(self, provider) -> None:
        provider.create_user("alice", "old-password-1", display_name="Alice")

        provider.set_password("alice", "new-password-2")

        token = await provider.login("alice", "new-password-2")
        assert token
        with pytest.raises(AuthenticationError):
            await provider.login("alice", "old-password-1")

    def test_unknown_user_raises(self, provider) -> None:
        with pytest.raises(ValueError, match="alice"):
            provider.set_password("alice", "new-password-2")

    def test_oversized_password_rejected(self, provider) -> None:
        provider.create_user("alice", "old-password-1", display_name="Alice")
        with pytest.raises(ValueError, match="72"):
            provider.set_password("alice", "x" * 80)
