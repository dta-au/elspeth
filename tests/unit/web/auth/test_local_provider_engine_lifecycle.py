"""The auth helper disposes exactly the session engines it owns."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import Engine

from . import conftest as auth_fixtures


@contextmanager
def _observed_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Engine, Mock]]:
    engine = auth_fixtures.create_session_engine(f"sqlite:///{tmp_path / 'identity-substrate.db'}")
    dispose = Mock(spec=engine.dispose, wraps=engine.dispose)
    monkeypatch.setattr(engine, "dispose", dispose)
    monkeypatch.setattr(auth_fixtures, "create_session_engine", lambda _url: engine)
    try:
        yield engine, dispose
    finally:
        engine.dispose()


def test_successful_build_transfers_disposal_to_its_scope(tmp_path, monkeypatch):
    with _observed_engine(tmp_path, monkeypatch) as (_engine, dispose):
        with auth_fixtures.local_auth_engine_scope():
            provider = auth_fixtures.build_local_auth_provider(tmp_path / "auth.db")
            provider.create_user("alice", "password123", "Alice")
            dispose.assert_not_called()
        dispose.assert_called_once_with()


@pytest.mark.parametrize("failure_phase", ["schema", "provider"])
def test_failed_build_disposes_immediately(tmp_path, monkeypatch, failure_phase):
    def fail(*_args, **_kwargs):
        raise OSError("construction failed")

    with _observed_engine(tmp_path, monkeypatch) as (_engine, dispose):
        if failure_phase == "schema":
            monkeypatch.setattr(auth_fixtures, "initialize_session_schema", fail)
        else:
            monkeypatch.setattr(auth_fixtures, "LocalAuthProvider", fail)
        with auth_fixtures.local_auth_engine_scope():
            with pytest.raises(OSError, match="construction failed"):
                auth_fixtures.build_local_auth_provider(tmp_path / "auth.db")
            dispose.assert_called_once_with()
        dispose.assert_called_once_with()


@pytest.mark.parametrize("construction_fails", [False, True])
def test_supplied_engine_remains_caller_owned(tmp_path, monkeypatch, construction_fails):
    with _observed_engine(tmp_path, monkeypatch) as (engine, dispose):
        auth_fixtures.initialize_session_schema(engine)
        with auth_fixtures.local_auth_engine_scope():
            if construction_fails:

                def fail(*_args, **_kwargs):
                    raise OSError("construction failed")

                monkeypatch.setattr(auth_fixtures, "LocalAuthProvider", fail)
                with pytest.raises(OSError, match="construction failed"):
                    auth_fixtures.build_local_auth_provider(tmp_path / "auth.db", session_engine=engine)
            else:
                provider = auth_fixtures.build_local_auth_provider(tmp_path / "auth.db", session_engine=engine)
                provider.create_user("alice", "password123", "Alice")
        dispose.assert_not_called()


def test_unowned_build_refuses_before_allocating_an_engine(tmp_path, monkeypatch):
    create_engine = Mock(spec=auth_fixtures.create_session_engine)
    monkeypatch.setattr(auth_fixtures, "create_session_engine", create_engine)
    token = auth_fixtures._local_auth_engine_owner.set(None)
    try:
        with pytest.raises(RuntimeError, match="requires local_auth_engine_scope"):
            auth_fixtures.build_local_auth_provider(tmp_path / "auth.db")
    finally:
        auth_fixtures._local_auth_engine_owner.reset(token)
    create_engine.assert_not_called()
