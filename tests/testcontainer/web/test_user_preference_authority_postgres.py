"""PostgreSQL absent-row serialization proof for user preferences."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import sqlalchemy as sa

from elspeth.contracts.advisory_locks import ELSPETH_USER_PREFERENCES_LOCK_CLASSID
from elspeth.web.preferences.models import UpdateComposerPreferencesRequest
from elspeth.web.preferences.service import RepositoryUserPreferenceAuthority, decode_preferences_row
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import user_preferences_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.testcontainer


def test_postgres_updated_at_uses_post_lock_database_wall_clock(
    external_deployment_postgres_url: str,
) -> None:
    holder_engine = create_session_engine(external_deployment_postgres_url)
    writer_engine = create_session_engine(external_deployment_postgres_url)
    observer_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(holder_engine)
    authority = RepositoryUserPreferenceAuthority(writer_engine)
    user_id = f"postgres-preference-clock-{uuid4().hex}"
    writer_pid: list[int] = []
    advisory_attempted = Event()

    @sa.event.listens_for(writer_engine, "connect", once=True)
    def _capture_writer_pid(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SELECT pg_backend_pid()")
            writer_pid.append(cursor.fetchone()[0])
        finally:
            cursor.close()

    @sa.event.listens_for(writer_engine, "before_cursor_execute")
    def _observe_advisory_attempt(conn, cursor, statement, parameters, context, executemany) -> None:
        if "pg_advisory_xact_lock" in statement:
            advisory_attempted.set()

    holder = holder_engine.connect()
    holder_tx = holder.begin()
    holder.exec_driver_sql(
        "SELECT pg_catalog.pg_advisory_xact_lock(%s, pg_catalog.hashtext(%s))",
        (ELSPETH_USER_PREFERENCES_LOCK_CLASSID, user_id),
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(authority.apply_patch, user_id, UpdateComposerPreferencesRequest(default_mode="freeform"))
            assert advisory_attempted.wait(timeout=10), "writer did not attempt the preferences advisory lock"
            assert writer_pid

            deadline = monotonic() + 10
            waiting = False
            with observer_engine.connect() as observer:
                while monotonic() < deadline:
                    wait_state = observer.execute(
                        sa.text("SELECT wait_event_type FROM pg_catalog.pg_stat_activity WHERE pid = :pid"),
                        {"pid": writer_pid[0]},
                    ).scalar_one()
                    if wait_state == "Lock":
                        waiting = True
                        break
                    sleep(0.01)
                assert waiting, "writer backend never reached a deterministic PostgreSQL lock wait"
                sampled_before_release = observer.exec_driver_sql("SELECT clock_timestamp()").scalar_one()

            holder_tx.commit()
            transition = future.result(timeout=20)

        with writer_engine.connect() as conn:
            stored = conn.execute(
                sa.select(user_preferences_table.c.updated_at).where(user_preferences_table.c.user_id == user_id)
            ).scalar_one()
        assert transition.current.updated_at == stored
        assert stored >= sampled_before_release
    finally:
        if holder_tx.is_active:
            holder_tx.rollback()
        holder.close()
        with holder_engine.begin() as conn:
            conn.execute(user_preferences_table.delete().where(user_preferences_table.c.user_id == user_id))
        holder_engine.dispose()
        writer_engine.dispose()
        observer_engine.dispose()


def test_postgres_absent_row_partial_patches_serialize_across_independent_engines(
    external_deployment_postgres_url: str,
) -> None:
    first_engine = create_session_engine(external_deployment_postgres_url)
    second_engine = create_session_engine(external_deployment_postgres_url)
    initialize_session_schema(first_engine)
    first = RepositoryUserPreferenceAuthority(first_engine)
    second = RepositoryUserPreferenceAuthority(second_engine)
    user_id = f"postgres-preference-authority-{uuid4().hex}"
    banner_timestamp = datetime(2026, 7, 30, 1, 2, 3, tzinfo=UTC)
    barrier = Barrier(2)

    def patch(authority: RepositoryUserPreferenceAuthority, payload: UpdateComposerPreferencesRequest):
        barrier.wait(timeout=10)
        return authority.apply_patch(user_id, payload)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(patch, first, UpdateComposerPreferencesRequest(default_mode="freeform")),
                pool.submit(patch, second, UpdateComposerPreferencesRequest(banner_dismissed_at=banner_timestamp)),
            )
            transitions = [future.result(timeout=30) for future in futures]

        assert sum(transition.prior is None for transition in transitions) == 1
        first_transition = next(transition for transition in transitions if transition.prior is None)
        second_transition = next(transition for transition in transitions if transition.prior is not None)
        assert second_transition.prior == first_transition.current
        assert second_transition.current.default_mode == "freeform"
        assert second_transition.current.banner_dismissed_at == banner_timestamp

        with first_engine.connect() as conn:
            row = conn.execute(
                sa.select(
                    user_preferences_table.c.default_composer_mode,
                    user_preferences_table.c.banner_dismissed_at,
                    sa.cast(user_preferences_table.c.tutorial_completed_at, sa.String).label("tutorial_completed_at"),
                    user_preferences_table.c.freeform_intro_dismissed_at,
                    user_preferences_table.c.tutorial_stage,
                    user_preferences_table.c.tutorial_session_id,
                    user_preferences_table.c.tutorial_run_id,
                    user_preferences_table.c.tutorial_source_data_hash,
                    user_preferences_table.c.updated_at,
                ).where(user_preferences_table.c.user_id == user_id)
            ).one()
        final = decode_preferences_row(row, user_id)
        assert final.default_mode == "freeform"
        assert final.banner_dismissed_at == banner_timestamp
    finally:
        with first_engine.begin() as conn:
            conn.execute(user_preferences_table.delete().where(user_preferences_table.c.user_id == user_id))
        first_engine.dispose()
        second_engine.dispose()
