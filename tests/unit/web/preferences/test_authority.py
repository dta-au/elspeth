"""Concurrency-authority contract tests for user preferences."""

from __future__ import annotations

import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Barrier
from typing import NoReturn

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

import elspeth.web.preferences.service as preferences_service
from elspeth.contracts import advisory_locks
from elspeth.web.preferences.models import ComposerMode, ComposerPreferences, UpdateComposerPreferencesRequest
from elspeth.web.preferences.service import (
    ComposerPreferencesTransition,
    PreferencesService,
    RepositoryUserPreferenceAuthority,
    UserPreferenceAuthority,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import metadata, user_preferences_table


def _prefs(*, mode: ComposerMode = "guided", updated_at: datetime | None = None) -> ComposerPreferences:
    return ComposerPreferences(
        default_mode=mode,
        banner_dismissed_at=None,
        freeform_intro_dismissed_at=None,
        tutorial_completed_at=None,
        tutorial_stage=None,
        tutorial_session_id=None,
        tutorial_run_id=None,
        tutorial_source_data_hash=None,
        updated_at=updated_at,
    )


@dataclass
class _CapturingAuthority:
    result: ComposerPreferencesTransition
    calls: list[tuple[str, UpdateComposerPreferencesRequest]] = field(default_factory=list)

    def apply_patch(self, user_id: str, payload: UpdateComposerPreferencesRequest) -> ComposerPreferencesTransition:
        self.calls.append((user_id, payload))
        return self.result


class _RecordingCounter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, object]]] = []

    def add(self, amount: int, *, attributes: dict[str, object]) -> None:
        self.calls.append((amount, dict(attributes)))


@pytest.fixture()
def engine():
    engine = create_session_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_user_preference_authority_protocol_is_handle_free() -> None:
    assert getattr(UserPreferenceAuthority, "_is_runtime_protocol", False)
    assert set(UserPreferenceAuthority.__dict__) & {"connection", "engine", "execute"} == set()
    assert list(inspect.signature(UserPreferenceAuthority.apply_patch).parameters) == ["self", "user_id", "payload"]
    assert list(inspect.signature(RepositoryUserPreferenceAuthority).parameters) == ["engine"]
    assert not hasattr(RepositoryUserPreferenceAuthority, "execute")


def test_service_delegates_exact_patch_via_worker(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = UpdateComposerPreferencesRequest(default_mode="freeform")
    transition = ComposerPreferencesTransition(prior=None, current=_prefs(mode="freeform", updated_at=datetime.now(UTC)))
    authority = _CapturingAuthority(transition)
    assert isinstance(authority, UserPreferenceAuthority)
    service = PreferencesService(engine, mutation_authority=authority)
    worker_calls: list[object] = []

    async def _run_worker(call):
        worker_calls.append(call)
        return call()

    monkeypatch.setattr(preferences_service, "run_sync_in_worker", _run_worker)

    result = __import__("asyncio").run(service.update_composer_preferences("alice", payload))

    assert result is transition
    assert authority.calls == [("alice", payload)]
    assert len(worker_calls) == 1


def test_authority_failure_emits_zero_telemetry(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingAuthority:
        def apply_patch(self, user_id: str, payload: UpdateComposerPreferencesRequest) -> NoReturn:
            raise RuntimeError("commit failed")

    patch_counter = _RecordingCounter()
    tutorial_calls: list[str] = []
    monkeypatch.setattr(preferences_service, "_PREFERENCES_PATCH_COUNTER", patch_counter)
    monkeypatch.setattr(preferences_service, "record_tutorial_completed_path", tutorial_calls.append)
    service = PreferencesService(engine, mutation_authority=_FailingAuthority())

    with pytest.raises(RuntimeError, match="commit failed"):
        __import__("asyncio").run(
            service.update_composer_preferences(
                "alice",
                UpdateComposerPreferencesRequest(tutorial_completed_at=datetime.now(UTC)),
            )
        )

    assert patch_counter.calls == []
    assert tutorial_calls == []


def test_repository_uses_database_current_timestamp_and_returns_stored_value(engine) -> None:
    statements: list[str] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _capture_sql(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    service = PreferencesService(engine, mutation_authority=RepositoryUserPreferenceAuthority(engine))
    event_timestamp = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)
    transition = __import__("asyncio").run(
        service.update_composer_preferences(
            "db-clock",
            UpdateComposerPreferencesRequest(default_mode="freeform", banner_dismissed_at=event_timestamp),
        )
    )
    with engine.connect() as conn:
        stored = conn.execute(
            sa.select(user_preferences_table.c.updated_at).where(user_preferences_table.c.user_id == "db-clock")
        ).scalar_one()

    assert transition.current.updated_at == stored
    assert transition.current.banner_dismissed_at == event_timestamp
    insert_sql = next(statement for statement in statements if statement.lstrip().upper().startswith("INSERT INTO USER_PREFERENCES"))
    assert "CURRENT_TIMESTAMP" in insert_sql.upper()


def test_empty_patch_without_row_is_no_write(engine) -> None:
    transition = RepositoryUserPreferenceAuthority(engine).apply_patch("empty-user", UpdateComposerPreferencesRequest())

    assert transition == ComposerPreferencesTransition(prior=None, current=_prefs())
    with engine.connect() as conn:
        assert conn.execute(sa.select(user_preferences_table).where(user_preferences_table.c.user_id == "empty-user")).first() is None


def test_corrupt_prior_rolls_back_and_emits_zero_telemetry(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    service = PreferencesService(engine, mutation_authority=RepositoryUserPreferenceAuthority(engine))
    __import__("asyncio").run(
        service.update_composer_preferences("corrupt-user", UpdateComposerPreferencesRequest(default_mode="freeform"))
    )
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            sa.text("UPDATE user_preferences SET default_composer_mode = 'kiosk' WHERE user_id = :user_id"),
            {"user_id": "corrupt-user"},
        )
        conn.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
    counter = _RecordingCounter()
    monkeypatch.setattr(preferences_service, "_PREFERENCES_PATCH_COUNTER", counter)

    with pytest.raises(preferences_service.CorruptPreferencesError, match="kiosk"):
        __import__("asyncio").run(
            service.update_composer_preferences(
                "corrupt-user",
                UpdateComposerPreferencesRequest(default_mode="guided"),
            )
        )

    assert counter.calls == []
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text("SELECT default_composer_mode FROM user_preferences WHERE user_id = 'corrupt-user'")).scalar_one()
            == "kiosk"
        )


def test_commit_failure_rolls_back_and_emits_zero_telemetry(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _RecordingCounter()
    monkeypatch.setattr(preferences_service, "_PREFERENCES_PATCH_COUNTER", counter)
    service = PreferencesService(engine, mutation_authority=RepositoryUserPreferenceAuthority(engine))

    @sa.event.listens_for(engine, "commit", once=True)
    def _fail_commit(conn) -> NoReturn:
        raise RuntimeError("synthetic commit failure")

    with pytest.raises(RuntimeError, match="synthetic commit failure"):
        __import__("asyncio").run(
            service.update_composer_preferences(
                "commit-failure-user",
                UpdateComposerPreferencesRequest(default_mode="freeform"),
            )
        )

    assert counter.calls == []
    with engine.connect() as conn:
        assert (
            conn.execute(sa.select(user_preferences_table).where(user_preferences_table.c.user_id == "commit-failure-user")).first() is None
        )


def test_preferences_advisory_classid_is_unique_and_immutable_abi() -> None:
    classids = {
        name: value for name, value in vars(advisory_locks).items() if name.startswith("ELSPETH_") and name.endswith("_LOCK_CLASSID")
    }

    assert advisory_locks.ELSPETH_USER_PREFERENCES_LOCK_CLASSID == 0x50524546
    assert len(set(classids.values())) == len(classids)


def test_sqlite_concurrent_partial_patches_form_prior_chain_and_final_union(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'authority-race.db'}"
    first_engine = create_session_engine(database_url)
    second_engine = create_session_engine(database_url)
    metadata.create_all(first_engine)
    first = RepositoryUserPreferenceAuthority(first_engine)
    second = RepositoryUserPreferenceAuthority(second_engine)
    barrier = Barrier(2)
    stamp = datetime(2026, 7, 30, 1, 2, 3, tzinfo=UTC)

    def patch(authority: RepositoryUserPreferenceAuthority, payload: UpdateComposerPreferencesRequest):
        barrier.wait(timeout=10)
        return authority.apply_patch("race-user", payload)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(patch, first, UpdateComposerPreferencesRequest(default_mode="freeform")),
                pool.submit(patch, second, UpdateComposerPreferencesRequest(banner_dismissed_at=stamp)),
            )
            transitions = [future.result(timeout=20) for future in futures]
        with first_engine.connect() as conn:
            final = preferences_service.decode_preferences_row(
                conn.execute(preferences_service._select_preferences_for_user("race-user")).one(),
                "race-user",
            )
    finally:
        first_engine.dispose()
        second_engine.dispose()

    assert sum(transition.prior is None for transition in transitions) == 1
    second_transition = next(transition for transition in transitions if transition.prior is not None)
    first_current = next(transition.current for transition in transitions if transition.prior is None)
    assert second_transition.prior.default_mode == first_current.default_mode
    assert second_transition.prior.banner_dismissed_at == (
        first_current.banner_dismissed_at.replace(tzinfo=None) if first_current.banner_dismissed_at is not None else None
    )
    assert second_transition.prior.updated_at == first_current.updated_at
    assert final.default_mode == "freeform"
    assert final.banner_dismissed_at is not None
    assert second_transition.current.default_mode == "freeform"
    assert second_transition.current.banner_dismissed_at is not None
