"""Cross-process exclusion tests for shared SQLite session locking."""

from __future__ import annotations

import multiprocessing
import queue
import threading
from pathlib import Path
from typing import Any

import pytest

from elspeth.web.sessions import locking
from elspeth.web.sessions.engine import create_session_engine


class _FailingUnlockFcntl:
    """Delegate lock acquisition while failing the release operation."""

    def __init__(self, delegate: Any) -> None:
        self.LOCK_EX = delegate.LOCK_EX
        self.LOCK_UN = delegate.LOCK_UN
        self._delegate = delegate

    def flock(self, descriptor: int, operation: int) -> None:
        if operation == self.LOCK_UN:
            raise OSError("unlock failed")
        self._delegate.flock(descriptor, operation)


class _PostgresUnlockFailureConnection:
    """Minimal connection double that fails only advisory-lock release."""

    def exec_driver_sql(self, statement: str, _params: object) -> object:
        if "pg_advisory_unlock" in statement:
            raise OSError("advisory unlock failed")
        return object()

    def commit(self) -> None:
        return

    def rollback(self) -> None:
        return

    def in_transaction(self) -> bool:
        return False


def _hold_sqlite_session_lock(database_url: str, entered: object, release: object) -> None:
    from elspeth.web.sessions.locking import sqlite_process_session_lock

    engine = create_session_engine(database_url)
    try:
        with sqlite_process_session_lock(engine, "shared-session"):
            entered.put("entered")  # type: ignore[attr-defined]
            if not release.wait(timeout=15):  # type: ignore[attr-defined]
                raise RuntimeError("release barrier timed out")
    finally:
        engine.dispose()


def _hold_filesystem_session_lock(root: str, entered: object, release: object) -> None:
    from elspeth.web.sessions.locking import filesystem_session_lock

    with filesystem_session_lock(Path(root), "shared-session"):
        entered.put("entered")  # type: ignore[attr-defined]
        if not release.wait(timeout=15):  # type: ignore[attr-defined]
            raise RuntimeError("release barrier timed out")


def test_sqlite_session_lock_excludes_separate_processes(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'locking.sqlite3'}"
    context = multiprocessing.get_context("spawn")
    entered = context.Queue()
    first_release = context.Event()
    second_release = context.Event()
    first = context.Process(target=_hold_sqlite_session_lock, args=(database_url, entered, first_release))
    second = context.Process(target=_hold_sqlite_session_lock, args=(database_url, entered, second_release))

    first.start()
    assert entered.get(timeout=10) == "entered"
    second.start()
    try:
        try:
            entered.get(timeout=0.5)
        except queue.Empty:
            pass
        else:
            raise AssertionError("second process entered the same-session critical section")

        first_release.set()
        first.join(timeout=10)
        assert first.exitcode == 0
        assert entered.get(timeout=10) == "entered"
    finally:
        first_release.set()
        second_release.set()
        first.join(timeout=10)
        second.join(timeout=10)
    assert second.exitcode == 0


def test_file_backed_sqlite_session_lock_is_same_thread_reentrant(tmp_path: Path) -> None:
    from elspeth.web.sessions.locking import sqlite_process_session_lock

    engine = create_session_engine(f"sqlite:///{tmp_path / 'reentrant.sqlite3'}")
    completed = threading.Event()

    def _nest_lock() -> None:
        with sqlite_process_session_lock(engine, "shared-session"):  # noqa: SIM117 - nesting is the behavior under test
            with sqlite_process_session_lock(engine, "shared-session"):
                completed.set()

    thread = threading.Thread(target=_nest_lock, daemon=True)
    thread.start()
    thread.join(timeout=5)
    try:
        assert completed.is_set(), "nested same-thread flock self-blocked"
        assert not thread.is_alive()
    finally:
        engine.dispose()


def test_filesystem_session_lock_excludes_separate_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    entered = context.Queue()
    first_release = context.Event()
    second_release = context.Event()
    first = context.Process(target=_hold_filesystem_session_lock, args=(str(tmp_path), entered, first_release))
    second = context.Process(target=_hold_filesystem_session_lock, args=(str(tmp_path), entered, second_release))

    first.start()
    assert entered.get(timeout=10) == "entered"
    second.start()
    try:
        try:
            entered.get(timeout=0.5)
        except queue.Empty:
            pass
        else:
            raise AssertionError("second process entered the same filesystem-session critical section")

        first_release.set()
        first.join(timeout=10)
        assert first.exitcode == 0
        assert entered.get(timeout=10) == "entered"
    finally:
        first_release.set()
        second_release.set()
        first.join(timeout=10)
        second.join(timeout=10)
    assert second.exitcode == 0


def test_filesystem_session_lock_is_same_thread_reentrant(tmp_path: Path) -> None:
    from elspeth.web.sessions.locking import filesystem_session_lock

    completed = threading.Event()

    def _nest_lock() -> None:
        with filesystem_session_lock(tmp_path, "shared-session"):  # noqa: SIM117 - nesting is the behavior under test
            with filesystem_session_lock(tmp_path, "shared-session"):
                completed.set()

    thread = threading.Thread(target=_nest_lock, daemon=True)
    thread.start()
    thread.join(timeout=5)
    assert completed.is_set(), "nested same-thread filesystem flock self-blocked"
    assert not thread.is_alive()


def test_filesystem_unlock_failure_preserves_primary_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locking, "_fcntl", _FailingUnlockFcntl(locking._fcntl))

    with (
        pytest.raises(LookupError, match="primary failure") as exc_info,
        locking.filesystem_session_lock(tmp_path, "shared-session"),
    ):
        raise LookupError("primary failure")

    assert exc_info.value.__notes__ == ["Filesystem session lock release also failed (OSError)"]


def test_filesystem_unlock_failure_surfaces_without_primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(locking, "_fcntl", _FailingUnlockFcntl(locking._fcntl))

    with (
        pytest.raises(OSError, match="unlock failed"),
        locking.filesystem_session_lock(tmp_path, "shared-session"),
    ):
        pass


def test_sqlite_unlock_failure_preserves_primary_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'primary.sqlite3'}")
    monkeypatch.setattr(locking, "_fcntl", _FailingUnlockFcntl(locking._fcntl))

    try:
        with (
            pytest.raises(LookupError, match="primary failure") as exc_info,
            locking.sqlite_process_session_lock(engine, "shared-session"),
        ):
            raise LookupError("primary failure")
    finally:
        engine.dispose()

    assert exc_info.value.__notes__ == ["SQLite session lock release also failed (OSError)"]


def test_sqlite_unlock_failure_surfaces_without_primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'cleanup.sqlite3'}")
    monkeypatch.setattr(locking, "_fcntl", _FailingUnlockFcntl(locking._fcntl))

    try:
        with (
            pytest.raises(OSError, match="unlock failed"),
            locking.sqlite_process_session_lock(engine, "shared-session"),
        ):
            pass
    finally:
        engine.dispose()


def test_postgres_unlock_failure_preserves_primary_exception() -> None:
    conn = _PostgresUnlockFailureConnection()

    with (
        pytest.raises(LookupError, match="primary failure") as exc_info,
        locking.postgres_session_advisory_lock(conn, "shared-session"),  # type: ignore[arg-type]
    ):
        raise LookupError("primary failure")

    assert exc_info.value.__notes__ == ["PostgreSQL session advisory lock release also failed (OSError)"]


def test_postgres_unlock_failure_surfaces_without_primary() -> None:
    conn = _PostgresUnlockFailureConnection()

    with (
        pytest.raises(OSError, match="advisory unlock failed"),
        locking.postgres_session_advisory_lock(conn, "shared-session"),  # type: ignore[arg-type]
    ):
        pass


def test_postgres_unlock_failure_surfaces_after_unrelated_caught_exception() -> None:
    conn = _PostgresUnlockFailureConnection()
    outer: LookupError | None = None

    try:
        raise LookupError("already handled")
    except LookupError as exc:
        outer = exc
        with (
            pytest.raises(OSError, match="advisory unlock failed"),
            locking.postgres_session_advisory_lock(conn, "shared-session"),  # type: ignore[arg-type]
        ):
            pass

    assert outer is not None
    assert getattr(outer, "__notes__", ()) == ()
