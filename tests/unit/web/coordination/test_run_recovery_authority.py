"""Focused transaction semantics for global run recovery."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy.sql.dml import Update

from elspeth.web.coordination import run_recovery_authority as recovery_module
from elspeth.web.coordination.run_recovery_authority import RepositoryGlobalRunRecoveryAuthority
from elspeth.web.sessions.engine import create_session_engine


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def one_or_none(self) -> Any:
        return self._value

    def scalar_one(self) -> Any:
        return self._value


class _LostRaceConnection:
    """Return a live candidate, then make its predicate-bearing CAS lose."""

    dialect = SimpleNamespace(name="sqlite")

    def __init__(self, *, session_id: str) -> None:
        self._session_id = session_id
        self._select_count = 0
        self.update_count = 0
        self.now = datetime(2026, 8, 2, 5, 0, tzinfo=UTC)

    def exec_driver_sql(self, _statement: str) -> _Result:
        return _Result(self.now)

    def execute(self, statement: Any) -> _Result:
        if isinstance(statement, Update):
            self.update_count += 1
            return _Result(None)
        self._select_count += 1
        if self._select_count == 1:
            return _Result(SimpleNamespace(id=self._session_id))
        if self._select_count == 2:
            return _Result(SimpleNamespace(released_at=self.now))
        if self._select_count == 3:
            return _Result(
                SimpleNamespace(
                    session_id=self._session_id,
                    status="running",
                    started_at=self.now,
                )
            )
        raise AssertionError("unexpected recovery query")


def test_zero_row_cancellation_cas_is_a_benign_lost_race(monkeypatch) -> None:
    engine = create_session_engine("sqlite:///:memory:")
    authority = RepositoryGlobalRunRecoveryAuthority(engine)
    session_id = str(uuid4())
    connection = _LostRaceConnection(session_id=session_id)

    @contextmanager
    def lost_race_transaction(_engine, locked_session_id: str):
        assert locked_session_id == session_id
        yield connection

    monkeypatch.setattr(recovery_module, "locked_session_transaction", lost_race_transaction)

    result = authority._cancel_candidate(
        run_id=str(uuid4()),
        session_id=session_id,
        max_age_seconds=None,
        exclude_run_ids=frozenset(),
        reason="recovered",
    )

    assert result is None
    assert connection.update_count == 1
