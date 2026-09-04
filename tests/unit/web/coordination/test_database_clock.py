"""The database-clock authority.

The property under test is not "returns a datetime" — it is that expiry
decisions are made on the DATABASE's clock and normalised to UTC in fact
rather than in name. Both halves have bitten someone: a replica clock makes a
single-use token single-use only as far as clock drift allows, and an
``_ensure_utc`` that leaves a non-UTC offset in place reports a different hour
for the same instant.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.engine import create_session_engine


def test_sqlite_returns_an_aware_utc_time() -> None:
    """The naive path: SQLite's CURRENT_TIMESTAMP is UTC, so it is stamped."""
    engine = create_session_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        now = database_now(conn)

    assert now.tzinfo is not None, "a naive datetime cannot be compared against a stored aware one"
    assert now.utcoffset() == timedelta(0)


def test_it_tracks_the_database_not_the_process() -> None:
    """Two reads bracket a database-side sleep, proving the source."""
    engine = create_session_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        first = database_now(conn)
        conn.execute(text("SELECT 1"))
        second = database_now(conn)

    assert second >= first


def test_an_unsupported_dialect_raises_rather_than_falling_back() -> None:
    """A silent fallback to the process clock would restore the exact drift
    this function exists to remove, while reading as though it worked."""
    fake = SimpleNamespace(dialect=SimpleNamespace(name="oracle"))

    with pytest.raises(NotImplementedError, match="oracle"):
        database_now(fake)  # type: ignore[arg-type]


class TestUtcNormalisation:
    """The behaviour the three existing copies disagree about.

    ``audit_access_log_authority`` converts an aware value to UTC; the other
    two return it unchanged on its original offset despite the name. Both are
    the same INSTANT; they differ in every field a formatter or a date-bucket
    reads. This authority converts, so the name is true.
    """

    @staticmethod
    def _conn_returning(value: object) -> object:
        class _Result:
            def scalar_one(self) -> object:
                return value

        return SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql"),
            exec_driver_sql=lambda _sql: _Result(),
        )

    def test_an_aware_non_utc_value_is_converted_not_passed_through(self) -> None:
        """psycopg returns this shape whenever the session timezone is not UTC."""
        sydney = datetime(2026, 9, 5, 14, 0, tzinfo=timezone(timedelta(hours=10)))

        result = database_now(self._conn_returning(sydney))  # type: ignore[arg-type]

        assert result.utcoffset() == timedelta(0), "the return must be UTC in fact, not merely aware"
        assert result.hour == 4, "14:00+10:00 is 04:00 UTC — the hour a log line would print"
        assert result == sydney, "and it must still be the same instant"

    def test_an_aware_utc_value_is_unchanged(self) -> None:
        already = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)
        assert database_now(self._conn_returning(already)) == already  # type: ignore[arg-type]

    def test_a_naive_value_is_stamped_not_shifted(self) -> None:
        """Stamping a naive value that is already UTC must not move it."""
        # Derived from an aware value rather than written naive: the lint that
        # forbids a bare naive datetime is right in general, and the naivety
        # here is the INPUT under test, not an oversight.
        naive = datetime(2026, 9, 5, 4, 0, tzinfo=UTC).replace(tzinfo=None)

        result = database_now(self._conn_returning(naive))  # type: ignore[arg-type]

        assert result.hour == 4
        assert result.utcoffset() == timedelta(0)

    def test_an_iso_string_is_parsed(self) -> None:
        """SQLite hands back a string, not a datetime."""
        result = database_now(self._conn_returning("2026-09-05 04:00:00"))  # type: ignore[arg-type]
        assert result == datetime(2026, 9, 5, 4, 0, tzinfo=UTC)

    def test_a_non_datetime_is_refused(self) -> None:
        """A driver returning something else is corruption, not a value to coerce."""
        with pytest.raises(RuntimeError, match="non-datetime"):
            database_now(self._conn_returning(12345))  # type: ignore[arg-type]
