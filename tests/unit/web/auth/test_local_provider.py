"""Tests for LocalAuthProvider -- SQLite user store, bcrypt hashing, JWT tokens."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
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
from elspeth.web.auth.models import AccessPending, AuthenticationError, IdentityDisabled, UserIdentity, UserProfile
from elspeth.web.auth.session_token import LOCAL_AUDIENCE
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table
from elspeth.web.sessions.schema import initialize_session_schema

from .conftest import build_local_auth_provider


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
    return build_local_auth_provider(tmp_path / "auth.db", token_expiry_hours=24)


def _signed_local_token(provider: LocalAuthProvider, claims: dict[str, Any]) -> str:
    """Sign an arbitrary claim set with the provider's real signing key.

    For boundary-shape tests only: it produces a GENUINELY signed token whose
    claims are deliberately wrong, which is the only way to test what the
    envelope checks do once the signature has already passed.

    Callers must supply the full envelope (``iss``, ``aud``, ``provider``,
    ``jti``) except for the one claim under test — otherwise the token is
    refused for a reason the test did not intend and the assertion passes for
    the wrong cause.
    """
    return pyjwt.encode(claims, provider._token_issuer._signing_key, algorithm="HS256")


def _envelope(**overrides: Any) -> dict[str, Any]:
    """A complete, valid claim set to mutate one field of."""
    claims: dict[str, Any] = {
        "sub": "some-identity-id",
        "username": "alice",
        "provider": "local",
        "iss": "elspeth",
        "aud": LOCAL_AUDIENCE,
        "jti": "test-jti",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    claims.update(overrides)
    return claims


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
        # A real subprocess, because umask is process state: setting it in
        # this process would leak into every other test in the worker. The
        # collaborators are wired inline rather than imported from conftest,
        # which is not reliably importable by path from a child interpreter.
        script = """
import os
import stat
import sys
from pathlib import Path
from elspeth.web.auth.local import LocalAuthProvider
from elspeth.web.auth.session_token import LOCAL_AUDIENCE, SessionTokenIssuer

os.umask(0)
path = Path(sys.argv[1])
LocalAuthProvider(
    db_path=path,
    token_issuer=SessionTokenIssuer(
        signing_key=b"subprocess-test-signing-key-32b!",
        provider="local",
        audience=LOCAL_AUDIENCE,
        token_expiry_hours=24,
        max_refresh_chain_hours=168,
        principal_is_active=lambda identity_id: True,
    ),
    admit_identity=lambda claims: (_ for _ in ()).throw(AssertionError("no login happens here")),
)
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
            build_local_auth_provider(db_path)

    def test_auth_database_rejects_symlink(self, tmp_path) -> None:
        target = tmp_path / "target.db"
        build_local_auth_provider(target)
        db_path = tmp_path / "auth.db"
        db_path.symlink_to(target)

        with pytest.raises(RuntimeError, match="regular owner-only file"):
            build_local_auth_provider(db_path)

    def test_open_owner_only_database_creates_missing_file_owner_only(self, tmp_path) -> None:
        """The ``FileNotFoundError`` arm creates the file and the identity check admits it."""
        db_path = tmp_path / "auth.db"

        descriptor = auth_local._open_owner_only_database(db_path)
        try:
            identity = os.fstat(descriptor)
        finally:
            os.close(descriptor)

        assert stat.S_ISREG(identity.st_mode)
        assert stat.S_IMODE(identity.st_mode) == 0o600
        assert identity.st_ino == db_path.stat().st_ino

    def test_open_owner_only_database_recovers_lost_create_race(self, tmp_path, monkeypatch) -> None:
        """The ``FileExistsError`` arm reopens the file a concurrent creator won, never a fresh one."""
        db_path = tmp_path / "auth.db"
        real_open = os.open
        attempts: list[int] = []

        def racing_open(path: Any, flags: int, mode: int = 0o777, *args: Any, **kwargs: Any) -> int:
            attempts.append(flags)
            if flags & os.O_CREAT:
                # Another process creates the same path between our lookup and our exclusive create.
                winner = real_open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
                os.write(winner, b"winner")
                os.close(winner)
            return real_open(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(os, "open", racing_open)
        descriptor = auth_local._open_owner_only_database(db_path)
        try:
            assert os.read(descriptor, 16) == b"winner"
            assert os.fstat(descriptor).st_ino == db_path.stat().st_ino
        finally:
            os.close(descriptor)

        assert [bool(flags & os.O_CREAT) for flags in attempts] == [False, True, False]

    def test_open_owner_only_database_lost_race_still_enforces_identity(self, tmp_path, monkeypatch) -> None:
        """A file the concurrent creator left group-readable is rejected on the reopen path too."""
        db_path = tmp_path / "auth.db"
        real_open = os.open

        def racing_open(path: Any, flags: int, mode: int = 0o777, *args: Any, **kwargs: Any) -> int:
            if flags & os.O_CREAT:
                winner = real_open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o640)
                os.close(winner)
            return real_open(path, flags, mode, *args, **kwargs)

        monkeypatch.setattr(os, "open", racing_open)
        with pytest.raises(auth_local.LocalAuthStorageSecurityError, match="mode 0600"):
            auth_local._open_owner_only_database(db_path)

    def test_open_owner_only_database_requires_no_follow_admission(self, tmp_path, monkeypatch) -> None:
        """A platform without ``os.O_NOFOLLOW`` fails closed by name before any open is attempted."""
        db_path = tmp_path / "auth.db"
        monkeypatch.delattr(os, "O_NOFOLLOW")

        with pytest.raises(auth_local.LocalAuthStorageSecurityError, match="no-follow"):
            auth_local._open_owner_only_database(db_path)
        assert not db_path.exists()

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
            restarted = build_local_auth_provider(provider._db_path)
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
            restarted = build_local_auth_provider(provider._db_path)
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
            restarted = build_local_auth_provider(provider._db_path)
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

        restarted = build_local_auth_provider(provider._db_path)

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
        restarted = build_local_auth_provider(provider._db_path)

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

        restarted = build_local_auth_provider(provider._db_path)

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
        provider = build_local_auth_provider(tmp_path / "auth.db")
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
        restarted = build_local_auth_provider(tmp_path / "auth.db")
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
        provider = build_local_auth_provider(tmp_path / "auth.db")
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
        provider = build_local_auth_provider(tmp_path / "auth.db")
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
        provider = build_local_auth_provider(tmp_path / "auth.db")
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
        provider = build_local_auth_provider(tmp_path / "auth.db")
        provider.register_email_verified_user(
            "alice",
            "password123",
            "Alice",
            "alice@example.com",
            verification_origin="https://composer.example.test",
            outbox_path=tmp_path / "email-verifications.jsonl",
        )

        now[0] += auth_local._EMAIL_VERIFICATION_TOKEN_TTL_SECONDS + auth_local._PENDING_REGISTRATION_RETENTION_SECONDS + 1
        restarted = build_local_auth_provider(tmp_path / "auth.db")
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
        assert identity.username == "alice"
        # user_id is the IDENTITY_ID, not the username. It is the value every
        # ownership row points at, so it must be the identity substrate's key
        # and not a display string an administrator can change.
        assert identity.user_id != "alice"
        assert provider._token_issuer.decode(token).identity_id == identity.user_id

    @pytest.mark.asyncio
    async def test_authenticate_garbage_token(self, provider) -> None:
        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.authenticate("garbage-not-a-jwt")

    @pytest.mark.asyncio
    async def test_authenticate_expired_token(self, tmp_path) -> None:
        """Token with 0-second expiry should fail after creation."""

        provider = build_local_auth_provider(tmp_path / "auth.db", token_expiry_hours=24)
        provider.create_user("alice", "pw", display_name="Alice")

        # A COMPLETE envelope whose only defect is expiry, so the refusal can
        # only be the expiry check.
        expired_token = _signed_local_token(
            provider,
            _envelope(iat=int(time.time()) - 7200, exp=int(time.time()) - 10),
        )

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
        provider = build_local_auth_provider(tmp_path / "auth.db")
        # Every claim correct; only the signing key is wrong.
        bad_token = pyjwt.encode(_envelope(), "wrong-key", algorithm="HS256")
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
        # The profile is read from auth.db by USERNAME while user_id carries
        # the identity_id — the two are different keys into different stores.
        assert profile.user_id == provider._token_issuer.decode(token).identity_id
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

    async def _logged_in(self, provider) -> str:
        provider.create_user("alice", "pw", display_name="Alice")
        return await provider.login("alice", "pw")

    @staticmethod
    def _backdated(provider, token: str, *, age_seconds: int) -> str:
        """Re-mint a real token with its chain start pushed into the past.

        Refresh now takes the TOKEN, so a test can no longer hand it a
        fabricated ``original_iat`` — which is the point: no caller can claim
        a chain age the signature does not support. To age a chain, the test
        must mint a genuine token with an old ``iat``.
        """
        claims = provider._token_issuer.decode(token)
        return provider._token_issuer.mint(
            identity_id=claims.identity_id,
            username=claims.username,
            issued_at=int(time.time()) - age_seconds,
        )

    @pytest.mark.asyncio
    async def test_refresh_deleted_user_raises(self, provider) -> None:
        """A deleted user cannot obtain fresh tokens via refresh."""
        token = await self._logged_in(provider)
        # Access _db_path directly — no public API to delete users by design
        _delete_user(provider, "alice")
        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.refresh(token)

    @pytest.mark.asyncio
    async def test_refresh_valid_user_returns_jwt(self, provider) -> None:
        token = await provider.refresh(await self._logged_in(provider))
        assert isinstance(token, str)
        assert len(token.split(".")) == 3

    @pytest.mark.asyncio
    async def test_refresh_with_iat_within_limit_succeeds(self, provider) -> None:
        """A chain inside max_refresh_chain_hours is renewed."""
        aged = self._backdated(provider, await self._logged_in(provider), age_seconds=3600)
        token = await provider.refresh(aged)
        assert isinstance(token, str)
        assert len(token.split(".")) == 3

    @pytest.mark.asyncio
    async def test_refresh_with_expired_chain_raises(self, provider) -> None:
        """A chain older than max_refresh_chain_hours (168h) is refused."""
        aged = self._backdated(provider, await self._logged_in(provider), age_seconds=8 * 24 * 3600)
        with pytest.raises(AuthenticationError, match="Token refresh chain expired"):
            await provider.refresh(aged)

    @pytest.mark.asyncio
    async def test_refresh_carries_original_iat_forward(self, provider) -> None:
        """Refreshed token preserves the original iat, not a fresh one.

        The expected value is READ BACK from the aged token rather than
        recomputed from the clock. Computing it twice raced a one-second tick
        and failed roughly once per full-suite run.
        """
        aged = self._backdated(provider, await self._logged_in(provider), age_seconds=7200)
        original_iat = provider._token_issuer.decode(aged).issued_at

        renewed = await provider.refresh(aged)

        assert provider._token_issuer.decode(renewed).issued_at == original_iat

    @pytest.mark.asyncio
    async def test_refresh_of_a_token_without_iat_raises(self, provider) -> None:
        """A chain with no start cannot be bounded, so it is refused.

        Previously the ROUTE checked this against the middleware's unverified
        claims. It is now a decode requirement, which means a token missing
        ``iat`` cannot reach any refresh path at all.
        """
        token = await self._logged_in(provider)
        claims = provider._token_issuer.decode(token)
        payload = _envelope(sub=claims.identity_id)
        del payload["iat"]
        no_iat = _signed_local_token(provider, payload)

        with pytest.raises(AuthenticationError, match="Invalid token"):
            await provider.refresh(no_iat)


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


class TestD12AdmissionWallOnEveryIssuancePath:
    """The pending wall belongs to ISSUANCE, not to logging in.

    Three paths hand out a session token: login, open registration, and email
    verification. A wall that only ``login`` honours is not a wall — and the
    failure is quiet rather than loud, because the token an unadmitted path
    issues is inert. The person receives a credential that never works and an
    error they cannot tell apart from an expired session, while a
    ``token_issued`` audit row asserts a token was issued to a principal that
    has no admission. A provably false audit row is worse than a refusal.

    Every test here uses a CLOSED provider (``registration_open=False``),
    which is what a ``registration_mode`` of ``email_verified`` or ``closed``
    produces in app.py. The shared fixture defaults to open, so none of these
    paths were exercised against a pending identity before this class existed.
    """

    @pytest.fixture
    def closed(self, tmp_path):
        """A deployment where D12 actually bites: first sight lands pending."""
        return build_local_auth_provider(tmp_path / "auth.db", registration_open=False)

    @pytest.mark.asyncio
    async def test_login_refuses_a_pending_identity(self, closed) -> None:
        closed.create_user("alice", "pw", display_name="Alice")
        with pytest.raises(AccessPending):
            await closed.login("alice", "pw")

    @pytest.mark.asyncio
    async def test_the_refusal_is_not_a_credential_failure(self, closed) -> None:
        """The password was RIGHT. Saying otherwise misreports it to the admin."""
        closed.create_user("alice", "pw", display_name="Alice")
        with pytest.raises(AuthenticationError) as excinfo:
            await closed.login("alice", "pw")
        assert "Invalid credentials" not in excinfo.value.detail

    def test_email_verification_refuses_a_pending_identity(self, closed) -> None:
        """The path that actually fires in a registration_mode=email_verified deployment.

        Without the wall this returns 200 with a token that every
        authenticated route then rejects.
        """
        closed.create_user("alice", "pw", display_name="Alice", email="a@example.com", email_verified=False)
        verification = closed.create_email_verification_token("alice")

        with pytest.raises(AccessPending):
            closed.verify_email_and_issue_token(
                verification,
                record_token_issued=lambda _identity, _token: None,
            )

    def test_no_token_issued_audit_is_written_for_a_refused_admission(self, closed) -> None:
        """The audit trail must not claim a token that was never issued."""
        closed.create_user("alice", "pw", display_name="Alice", email="a@example.com", email_verified=False)
        verification = closed.create_email_verification_token("alice")
        recorded: list[str] = []

        with pytest.raises(AuthenticationError):
            closed.verify_email_and_issue_token(
                verification,
                record_token_issued=lambda _identity, token: recorded.append(token),
            )

        assert recorded == []

    def test_open_registration_refuses_a_disabled_identity(self, tmp_path) -> None:
        """Re-registering a freed username must not resurrect a disabled admission.

        Reachable on the OPEN path, which is why this one does not use the
        ``closed`` fixture: an identity already disabled, whose credential row
        was then deleted, can be re-registered under the same username.
        Without the wall that returns a token bound to the disabled identity.
        """
        engine = create_session_engine(f"sqlite:///{tmp_path / 'identities.db'}")
        initialize_session_schema(engine)
        provider = build_local_auth_provider(tmp_path / "auth.db", session_engine=engine)

        provider.create_user("alice", "pw", display_name="Alice")
        identity_id = provider._admit("alice").record.identity_id
        with engine.begin() as conn:
            conn.execute(identities_table.update().where(identities_table.c.identity_id == identity_id).values(access_state="disabled"))
        _delete_user(provider, "alice")

        with pytest.raises(IdentityDisabled):
            provider.register_open_user_with_audit(
                "alice",
                "new-password",
                "Alice Again",
                None,
                record_token_issued=lambda _token: None,
            )
