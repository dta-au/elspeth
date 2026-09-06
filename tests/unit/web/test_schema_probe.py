"""Fail-closed database target, schema-state, and initialization tests."""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from elspeth.core.landscape.database import SchemaCompatibilityError
from elspeth.web import schema_probe as schema_probe_module
from elspeth.web.schema_probe import (
    AWS_ECS_POOL_KWARGS,
    EXTERNAL_POSTGRES_POOL_KWARGS,
    DatabaseTargetConflictError,
    SchemaLockCleanupError,
    SchemaState,
    _run_locked,
    init_landscape_schema,
    init_session_schema,
    postgres_engine_kwargs,
    postgres_logical_target_key,
    probe_landscape_schema,
    probe_session_schema,
    require_distinct_postgres_targets,
)
from elspeth.web.sessions.models import metadata as session_metadata
from elspeth.web.sessions.schema import SessionSchemaError

_SENTINEL = "opaque-sentinel-secret SELECT raw_secret FROM vault"


class _ScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one(self) -> object:
        return self._value


class _FakeConnection:
    def __init__(
        self,
        *,
        acquisition_error: BaseException | None = None,
        unlock_error: BaseException | None = None,
        unlock_value: object = True,
        rollback_fail_at: int | None = None,
    ) -> None:
        self.dialect = SimpleNamespace(name="postgresql")
        self.acquisition_error = acquisition_error
        self.unlock_error = unlock_error
        self.unlock_value = unlock_value
        self.rollback_fail_at = rollback_fail_at
        self.transaction_active = False
        self.rollback_calls = 0
        self.invalidated = False

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: object, _params: object = None) -> _ScalarResult:
        sql = str(statement)
        if "pg_advisory_lock" in sql and "unlock" not in sql and self.acquisition_error is not None:
            raise self.acquisition_error
        if "pg_advisory_unlock" in sql:
            if self.unlock_error is not None:
                raise self.unlock_error
            self.transaction_active = True
            return _ScalarResult(self.unlock_value)
        return _ScalarResult(None)

    def in_transaction(self) -> bool:
        return self.transaction_active

    def commit(self) -> None:
        self.transaction_active = False

    def rollback(self) -> None:
        self.rollback_calls += 1
        if self.rollback_calls == self.rollback_fail_at:
            raise RuntimeError(_SENTINEL)
        self.transaction_active = False

    def invalidate(self) -> None:
        self.invalidated = True


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def connect(self) -> _FakeConnection:
        return self.connection


def _assert_redacted(value: object) -> None:
    rendered = repr(value)
    assert "sentinel-secret" not in rendered
    assert "raw_secret" not in rendered
    assert "vault" not in rendered


def _run_fake(
    connection: _FakeConnection,
    *,
    body: Callable[[Any], None] | None = None,
    verify: Callable[[Any], None] | None = None,
) -> None:
    _run_locked(
        _FakeEngine(connection),  # type: ignore[arg-type]
        target="elspeth_schema_init",
        body=body or (lambda _conn: None),
        verify=verify or (lambda _conn: None),
    )


def test_pool_kwargs_are_postgres_only_and_fresh() -> None:
    first = postgres_engine_kwargs("postgresql+psycopg://db.example/audit")
    second = postgres_engine_kwargs("postgresql://db.example/audit")
    assert first == {"pool_size": 5, "max_overflow": 5, "pool_pre_ping": True}
    assert first == second
    assert first is not second
    first["pool_size"] = 1
    assert second["pool_size"] == 5
    assert postgres_engine_kwargs("sqlite:///audit.db") == {}
    assert isinstance(AWS_ECS_POOL_KWARGS, MappingProxyType)
    assert EXTERNAL_POSTGRES_POOL_KWARGS is AWS_ECS_POOL_KWARGS


@pytest.mark.parametrize("driver", ["postgresql", "postgresql+psycopg", "postgresql+psycopg2"])
def test_logical_target_normalizes_postgres_driver_host_and_port(driver: str) -> None:
    target = postgres_logical_target_key(f"{driver}://user:ignored@DB.EXAMPLE/audit")
    assert target.host == "db.example"
    assert target.port == 5432
    assert target.database == "audit"
    assert target.explicit_schema is None


@pytest.mark.parametrize(
    ("options", "schema"),
    [
        ("-csearch_path=Foo", "foo"),
        ("-c search_path=foo_2", "foo_2"),
    ],
)
def test_logical_target_parses_single_explicit_schema(options: str, schema: str) -> None:
    target = postgres_logical_target_key(f"postgresql+psycopg://host/audit?options={options}")
    assert target.explicit_schema == schema


@pytest.mark.parametrize(
    "override",
    [
        "host=override.internal",
        "port=6543",
        "dbname=other",
        "hostaddr=127.0.0.2",
        "service=other",
    ],
)
def test_logical_target_rejects_connection_target_query_overrides(override: str) -> None:
    with pytest.raises(DatabaseTargetConflictError):
        postgres_logical_target_key(f"postgresql+psycopg://host/audit?{override}")


@pytest.mark.parametrize("schema", ["a" * 64, "pg_temp", "pg_catalog"])
def test_logical_target_rejects_unstable_schema_identifiers(schema: str) -> None:
    with pytest.raises(DatabaseTargetConflictError):
        postgres_logical_target_key(f"postgresql+psycopg://host/audit?options=-csearch_path={schema}")


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///audit.db",
        "postgresql+psycopg:///audit",
        "postgresql+psycopg://host/",
        "postgresql+psycopg://host/audit?options=-csearch_path=foo,public",
        "postgresql+psycopg://host/audit?options=-csearch_path=%22Foo%22",
        "postgresql+psycopg://host/audit?options=-csearch_path=$user",
        "postgresql+psycopg://host/audit?options=-csearch_path=foo%20-csearch_path=bar",
    ],
)
def test_unprovable_target_is_rejected_with_static_message(url: str) -> None:
    with pytest.raises(DatabaseTargetConflictError) as exc_info:
        postgres_logical_target_key(url)
    assert str(exc_info.value) == "PostgreSQL database target cannot be proven safe from static URL configuration."
    assert "audit" not in str(exc_info.value)


def test_distinct_servers_pass_without_schema_options() -> None:
    require_distinct_postgres_targets("postgresql://one/audit", "postgresql://two/audit")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("postgresql://host/audit", "postgresql://host/audit"),
        ("postgresql://host/audit", "postgresql://host/audit?options=-csearch_path=public"),
        (
            "postgresql://host/audit?options=-csearch_path=Foo",
            "postgresql://host/audit?options=-csearch_path=foo",
        ),
    ],
)
def test_same_database_unproven_or_equal_schema_fails(left: str, right: str) -> None:
    with pytest.raises(DatabaseTargetConflictError):
        require_distinct_postgres_targets(left, right)


def test_same_database_distinct_explicit_schemas_pass() -> None:
    require_distinct_postgres_targets(
        "postgresql://host/audit?options=-csearch_path=sessions",
        "postgresql://host/audit?options=-csearch_path=landscape",
    )


def test_empty_sqlite_targets_are_missing() -> None:
    engine = create_engine("sqlite:///:memory:")
    assert probe_session_schema(engine) is SchemaState.MISSING
    assert probe_landscape_schema(engine) is SchemaState.MISSING
    engine.dispose()


def test_session_foreign_partial_and_current_states() -> None:
    foreign = create_engine("sqlite:///:memory:")
    with foreign.begin() as conn:
        conn.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
    assert probe_session_schema(foreign) is SchemaState.STALE

    partial = create_engine("sqlite:///:memory:")
    next(iter(session_metadata.tables.values())).create(partial)
    assert probe_session_schema(partial) is SchemaState.STALE

    current = create_engine("sqlite:///:memory:")
    init_session_schema(current)
    assert probe_session_schema(current) is SchemaState.CURRENT
    foreign.dispose()
    partial.dispose()
    current.dispose()


def test_landscape_additive_gap_is_partial_and_initializer_repairs_it() -> None:
    engine = create_engine("sqlite:///:memory:")
    init_landscape_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP INDEX ix_tokens_run_id")
    assert probe_landscape_schema(engine) is SchemaState.PARTIAL
    init_landscape_schema(engine)
    assert probe_landscape_schema(engine) is SchemaState.CURRENT


def test_populated_landscape_missing_epoch_23_policy_table_is_stale_and_not_repaired() -> None:
    engine = create_engine("sqlite:///:memory:")
    init_landscape_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE run_web_plugin_policy")
        conn.exec_driver_sql("PRAGMA user_version = 22")

    assert probe_landscape_schema(engine) is SchemaState.STALE
    with pytest.raises(SchemaCompatibilityError, match=r"stale|foreign"):
        init_landscape_schema(engine)
    assert "run_web_plugin_policy" not in inspect(engine).get_table_names()
    engine.dispose()


def test_initializers_refuse_stale_nonempty_targets_without_mutation() -> None:
    session = create_engine("sqlite:///:memory:")
    landscape = create_engine("sqlite:///:memory:")
    for engine in (session, landscape):
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
    with pytest.raises(SessionSchemaError):
        init_session_schema(session)
    with pytest.raises(SchemaCompatibilityError):
        init_landscape_schema(landscape)
    assert inspect(session).get_table_names() == ["unrelated"]
    assert inspect(landscape).get_table_names() == ["unrelated"]
    session.dispose()
    landscape.dispose()


def test_session_tableless_foreign_sentinels_are_stale() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA application_id = 123456")
        conn.exec_driver_sql("PRAGMA user_version = 654321")
    assert probe_session_schema(engine) is SchemaState.STALE
    engine.dispose()


def test_session_initializer_verifies_noop_create_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_all produced no tables at all — a defect in this build.

    Deliberately NOT the same message as a shape mismatch after a successful
    create: an incomplete table set is a first-party bug, while drift is a
    database an operator can delete. Both once said "initialization did not
    produce the current schema", which made them indistinguishable in a log.
    """
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(session_metadata, "create_all", lambda **_kwargs: None)
    with pytest.raises(SessionSchemaError, match="created no elspeth_schema_identity table"):
        init_session_schema(engine)
    engine.dispose()


# --------------------------------------------------------------------------
# A schema failure must name what drifted (elspeth-d0e62aea41).
#
# The precise subject was never missing: ``_validate_current_schema`` computes
# "<table>.<constraint> CHECK constraint SQL mismatch" with both sides, and
# ``probe_current_schema`` discarded it to answer a yes/no question. Every
# test below asserts on the SUBJECT, not merely that something was raised —
# "an error occurred" is exactly the state these replace.
# --------------------------------------------------------------------------

_DECLARED_CHECK = "CHECK (relationship_type IN ('approver'))"
_DRIFTED_CHECK = "CHECK (relationship_type IN ('approver', 'sponsor'))"


def _drift_one_check_constraint(engine) -> None:
    """Recreate one table with a widened CHECK, as a stale deployment would.

    SQLite cannot ALTER a constraint, so the table is recreated from its own
    stored DDL with one predicate changed. That keeps every other column,
    index and foreign key byte-identical, so the collector has exactly one
    thing to find.
    """
    with engine.begin() as conn:
        ddl = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='identity_relationships'").scalar_one()
        assert _DECLARED_CHECK in ddl, "the fixture must drift the constraint the model actually declares"
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        conn.exec_driver_sql("DROP TABLE identity_relationships")
        conn.exec_driver_sql(ddl.replace(_DECLARED_CHECK, _DRIFTED_CHECK))


def test_an_existing_database_with_a_drifted_check_names_the_constraint() -> None:
    """The operator-facing path: a deployment whose DB predates a schema edit."""
    engine = create_engine("sqlite:///:memory:")
    init_session_schema(engine)
    _drift_one_check_constraint(engine)

    with pytest.raises(SessionSchemaError) as raised:
        init_session_schema(engine)

    message = str(raised.value)
    assert "identity_relationships.ck_identity_relationships_type" in message
    assert "CHECK constraint SQL mismatch" in message
    assert "sponsor" in message, "the message must carry the FOUND side, not only the expected one"
    # The instruction the old sentence carried must not have been traded away
    # for the detail: an operator needs both.
    assert "Delete the old session database and restart" in message
    engine.dispose()


def test_a_fresh_create_that_does_not_validate_names_the_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact shape of elspeth-d0e62aea41: an EMPTY database, creation
    runs, and what it produced does not match the model.

    This is the ``verify`` path rather than the stale-database path, and it is
    the one that reached operators as "did not produce the current schema"
    with no table, constraint or subject in it.
    """
    engine = create_engine("sqlite:///:memory:")
    real_create = schema_probe_module._create_session_tables

    def create_then_drift(conn: Any, **kwargs: Any) -> None:
        real_create(conn, **kwargs)
        ddl = conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='identity_relationships'").scalar_one()
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        conn.exec_driver_sql("DROP TABLE identity_relationships")
        conn.exec_driver_sql(ddl.replace(_DECLARED_CHECK, _DRIFTED_CHECK))

    monkeypatch.setattr(schema_probe_module, "_create_session_tables", create_then_drift)

    with pytest.raises(SessionSchemaError) as raised:
        init_session_schema(engine)

    message = str(raised.value)
    assert "identity_relationships.ck_identity_relationships_type" in message
    assert "CHECK constraint SQL mismatch" in message
    assert "did not produce the current schema" not in message, "the sentence that named nothing must be gone"
    engine.dispose()


def test_the_probe_and_the_validator_disagreeing_gets_its_own_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contradiction branch, which must not silently reuse the vague text.

    If the probe reports stale and re-validation finds nothing, one of the two
    is wrong about the same database. That is a defect in this build, not a
    schema an operator can repair, so it must not read like drift.
    """
    engine = create_engine("sqlite:///:memory:")
    init_session_schema(engine)
    monkeypatch.setattr(schema_probe_module, "probe_current_schema", lambda _bind: False)

    with pytest.raises(SessionSchemaError) as raised:
        init_session_schema(engine)

    message = str(raised.value)
    assert "re-validating it found nothing wrong" in message
    assert "defect in one of them" in message
    engine.dispose()


def test_session_initializer_is_noop_when_schema_is_current(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    init_session_schema(engine)

    def unexpected_create_all(**_kwargs: object) -> None:
        pytest.fail("create_all must not run for a current schema")

    monkeypatch.setattr(session_metadata, "create_all", unexpected_create_all)
    init_session_schema(engine)
    engine.dispose()


def test_locked_body_and_verify_share_the_same_connection() -> None:
    engine = create_engine("sqlite:///:memory:")
    seen: list[int] = []
    _run_locked(
        engine,
        target="test",
        body=lambda conn: seen.append(id(conn)),
        verify=lambda conn: seen.append(id(conn)),
    )
    assert len(seen) == 2
    assert seen[0] == seen[1]
    engine.dispose()


def _earlier_body(conn: Any) -> None:
    conn.transaction_active = True
    raise KeyboardInterrupt(_SENTINEL)


@pytest.mark.parametrize("with_earlier", [False, True])
def test_pre_unlock_rollback_failure_is_redacted_and_preserves_earlier(with_earlier: bool) -> None:
    conn = _FakeConnection(rollback_fail_at=1)
    body: Callable[[Any], None]
    if with_earlier:
        body = _earlier_body
        expected: type[BaseException] = KeyboardInterrupt
    else:

        def body(_candidate: Any) -> None:
            return None

        expected = SchemaLockCleanupError
    with capture_logs() as logs, pytest.raises(expected) as exc_info:
        _run_fake(conn, body=body, verify=lambda candidate: setattr(candidate, "transaction_active", True))
    assert conn.invalidated
    if not with_earlier:
        _assert_redacted(exc_info.value)
    _assert_redacted(logs)


@pytest.mark.parametrize("with_earlier", [False, True])
def test_post_unlock_rollback_failure_is_redacted_and_preserves_earlier(with_earlier: bool) -> None:
    conn = _FakeConnection(rollback_fail_at=1)

    def body(_conn: Any) -> None:
        if with_earlier:
            raise KeyboardInterrupt(_SENTINEL)

    expected: type[BaseException] = KeyboardInterrupt if with_earlier else SchemaLockCleanupError
    with capture_logs() as logs, pytest.raises(expected) as exc_info:
        _run_fake(conn, body=body)
    assert conn.invalidated
    if not with_earlier:
        _assert_redacted(exc_info.value)
    _assert_redacted(logs)


@pytest.mark.parametrize("mode", ["false", "exception", "interrupt"])
@pytest.mark.parametrize("with_earlier", [False, True])
def test_unlock_failure_invalidates_redacts_and_preserves_earlier(mode: str, with_earlier: bool) -> None:
    kwargs: dict[str, object] = {}
    if mode == "false":
        kwargs["unlock_value"] = False
    elif mode == "exception":
        kwargs["unlock_error"] = RuntimeError(_SENTINEL)
    else:
        kwargs["unlock_error"] = KeyboardInterrupt(_SENTINEL)
    conn = _FakeConnection(**kwargs)

    def body(_conn: Any) -> None:
        if with_earlier:
            raise KeyboardInterrupt(_SENTINEL)

    expected: type[BaseException] = KeyboardInterrupt if with_earlier else SchemaLockCleanupError
    with capture_logs() as logs, pytest.raises(expected) as exc_info:
        _run_fake(conn, body=body)
    assert conn.invalidated
    if not with_earlier:
        _assert_redacted(exc_info.value)
    _assert_redacted(logs)


class _OriginalDatabaseError(RuntimeError):
    sqlstate = "08006"


def test_non_busy_lock_operational_error_is_static_redacted_and_invalidates() -> None:
    raw = OperationalError(
        "SELECT raw_secret FROM vault",
        {"password": "sentinel-secret"},
        _OriginalDatabaseError(_SENTINEL),
    )
    conn = _FakeConnection(acquisition_error=raw)
    with capture_logs() as logs, pytest.raises(RuntimeError) as exc_info:
        _run_fake(conn)
    assert type(exc_info.value) is schema_probe_module.SchemaInitError
    assert exc_info.value.__cause__ is raw
    assert conn.invalidated
    _assert_redacted(exc_info.value)
    _assert_redacted(logs)
