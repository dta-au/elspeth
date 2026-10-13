"""Execute the shipped Compose bootstrap and prove its actual runtime boundary."""

from __future__ import annotations

import os
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError, ProgrammingError
from tests.helpers import postgres_target
from tests.unit.core.landscape.test_auth_event_export_snapshot import insert_event
from tests.unit.deployment.test_compose_bundle import COMPOSE_TEST_ENVIRONMENT, POSTGRES_IMAGE, POSTGRES_INIT, _rendered_bundle

from elspeth.web.schema_probe import SchemaState, init_landscape_schema, init_session_schema, probe_landscape_schema, probe_session_schema

pytestmark = pytest.mark.testcontainer


@dataclass(frozen=True)
class _ComposePostgres:
    name: str
    admin_url: URL
    rendered: dict[str, Any] = field(repr=False)

    def bootstrap(self, overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        credentials = {name: value for name, value in COMPOSE_TEST_ENVIRONMENT.items() if name.startswith("POSTGRES_")}
        credentials.update(overrides or {})
        command = ["docker", "exec", "-i"]
        for name in credentials:
            command.extend(["--env", name])
        command.extend(
            [
                self.name,
                "psql",
                "--host",
                "127.0.0.1",
                "--username",
                "elspeth",
                "--dbname",
                "postgres",
                "--set",
                "ON_ERROR_STOP=1",
                "--file",
                "/bootstrap/postgres-init.sql",
            ]
        )
        return subprocess.run(command, env=os.environ | credentials, check=False, capture_output=True, text=True)

    def engine(self, service: str, database: str) -> Engine:
        key = "ELSPETH_WEB__SESSION_DB_URL" if database == "session" else "ELSPETH_WEB__LANDSCAPE_URL"
        url = make_url(self.rendered["services"][service]["environment"][key])
        return create_engine(url.set(host=self.admin_url.host, port=self.admin_url.port))


@pytest.fixture(scope="module")
def compose_postgres() -> Iterator[_ComposePostgres]:
    # This package provisions fixed production database/role names. It must
    # never bootstrap them into an acceptance driver's shared external server.
    if postgres_target.provisioned_postgres_url() is not None:
        pytest.skip("Compose bootstrap requires its own disposable cluster, not a provisioned acceptance server")
    rendered = _rendered_bundle()
    name = f"elspeth-compose-privileges-{uuid.uuid4().hex}"
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(postgres_target, "POSTGRES_IMAGE", POSTGRES_IMAGE)
        with postgres_target.postgres_test_target(
            driver="psycopg",
            username="elspeth",
            password=COMPOSE_TEST_ENVIRONMENT["POSTGRES_PASSWORD"],
            dbname="postgres",
            name=name,
            volumes=[(str(POSTGRES_INIT), "/bootstrap/postgres-init.sql", "ro")],
        ) as url:
            cluster = _ComposePostgres(name, make_url(url), rendered)
            result = cluster.bootstrap()
            assert result.returncode == 0, result.stdout + result.stderr
            for database, initializer in (("session", init_session_schema), ("landscape", init_landscape_schema)):
                owner = cluster.engine("web-init", database)
                try:
                    initializer(owner)
                finally:
                    owner.dispose()
            yield cluster


def _assert_denied(engine: Engine, statement: str) -> None:
    # CREATE DATABASE must reach PostgreSQL's privilege check rather than
    # failing first because a transaction block forbids the command.
    with pytest.raises(ProgrammingError) as error, engine.connect() as connection:
        connection.execution_options(isolation_level="AUTOCOMMIT").exec_driver_sql(statement)
    assert isinstance(error.value.orig, psycopg.errors.InsufficientPrivilege)
    assert error.value.orig.sqlstate == "42501"


def _assert_no_database_access(runtime: Engine, database: str) -> None:
    with runtime.connect() as connection:
        allowed = connection.execute(
            text("SELECT has_database_privilege(current_user, :database, 'CONNECT')"), {"database": database}
        ).scalar_one()
        assert allowed is False
    target = create_engine(runtime.url.set(database=database))
    try:
        # libpq FATALs are OperationalError, without executed-SQL SQLSTATEs.
        with pytest.raises(OperationalError) as error, target.connect():
            pass
        assert isinstance(error.value.orig, psycopg.OperationalError)
        message = str(error.value.orig)
        assert "permission denied for database" in message or "not currently accepting connections" in message
    finally:
        target.dispose()


@pytest.mark.parametrize("database", ["session", "landscape"])
def test_rendered_runtime_roles_have_dml_and_cannot_escape_their_database(
    compose_postgres: _ComposePostgres,
    database: str,
) -> None:
    runtime = compose_postgres.engine("web", database)
    owner = compose_postgres.engine("web-init", database)
    owner_role = "elspeth_session_schema" if database == "session" else "elspeth_landscape_schema"
    expected_runtime = "elspeth_session_runtime" if database == "session" else "elspeth_landscape_runtime"
    protected_table = "chat_messages" if database == "session" else "audit_export_snapshots"
    probe = probe_session_schema if database == "session" else probe_landscape_schema
    try:
        assert probe(runtime) is SchemaState.CURRENT
        with runtime.connect() as connection:
            identity = connection.execute(
                text(
                    "SELECT current_user, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
            assert identity == (expected_runtime, False, False, False, False, False)
        # Positive control: owner DDL works, then its default privileges permit
        # the same sequence, function, and DML operations from the runtime URL.
        with owner.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE compose_privilege_probe (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, value integer)"
            )
            connection.exec_driver_sql("CREATE FUNCTION compose_privilege_function() RETURNS integer LANGUAGE SQL AS 'SELECT 7'")
        try:
            with runtime.begin() as connection:
                key = connection.exec_driver_sql("INSERT INTO compose_privilege_probe(value) VALUES (1) RETURNING id").scalar_one()
                connection.execute(text("UPDATE compose_privilege_probe SET value=2 WHERE id=:id"), {"id": key})
                assert connection.exec_driver_sql("SELECT value FROM compose_privilege_probe").scalar_one() == 2
                assert connection.exec_driver_sql("SELECT compose_privilege_function()").scalar_one() == 7
                connection.execute(text("DELETE FROM compose_privilege_probe WHERE id=:id"), {"id": key})
            for statement in (
                "CREATE ROLE compose_forbidden_role",
                "CREATE DATABASE compose_forbidden_database",
                "CREATE SCHEMA compose_forbidden_schema",
                "CREATE TABLE compose_forbidden_table (id integer)",
                f"SET ROLE {owner_role}",
                "SELECT pg_read_file('PG_VERSION')",
                f"ALTER TABLE {protected_table} DISABLE TRIGGER ALL",
                f"TRUNCATE {protected_table}",
                "SET session_replication_role = replica",
                "CREATE OR REPLACE FUNCTION compose_privilege_function() RETURNS integer LANGUAGE SQL AS 'SELECT 8'",
            ):
                _assert_denied(runtime, statement)
        finally:
            with owner.begin() as connection:
                connection.exec_driver_sql("DROP TABLE compose_privilege_probe")
                connection.exec_driver_sql("DROP FUNCTION compose_privilege_function()")
        other_database = "elspeth_landscape" if database == "session" else "elspeth_sessions"
        for inaccessible in (other_database, "postgres", "template0", "template1"):
            _assert_no_database_access(runtime, inaccessible)
        assert probe(runtime) is SchemaState.CURRENT
    finally:
        runtime.dispose()
        owner.dispose()


def test_bootstrap_rerun_preserves_normal_audit_writes_and_scoped_roles(compose_postgres: _ComposePostgres) -> None:
    runtime = compose_postgres.engine("web", "landscape")
    try:
        with runtime.begin() as connection:
            insert_event(connection, "compose-runtime-audit-event")
        result = compose_postgres.bootstrap()
        assert result.returncode == 0, result.stdout + result.stderr
        # Deployment maintenance closes prior application backends; restarted
        # services create a fresh pool rather than reusing the previous socket.
        runtime.dispose()
        with runtime.connect() as connection:
            assert (
                connection.exec_driver_sql("SELECT count(*) FROM auth_events WHERE event_id='compose-runtime-audit-event'").scalar_one()
                == 1
            )
        assert probe_landscape_schema(runtime) is SchemaState.CURRENT
    finally:
        runtime.dispose()


@pytest.mark.parametrize("password", ["'bad:/@password", "0" * 47, "1" * 48])
def test_bootstrap_rejects_ambiguous_or_shared_passwords_without_leaking_them(
    compose_postgres: _ComposePostgres,
    password: str,
) -> None:
    result = compose_postgres.bootstrap({"POSTGRES_SESSION_RUNTIME_PASSWORD": password})
    assert result.returncode != 0
    assert "compose_database_passwords_invalid" in result.stdout + result.stderr
    assert password not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "mutation,repair",
    [
        ("ALTER ROLE elspeth_session_runtime SUPERUSER", "ALTER ROLE elspeth_session_runtime NOSUPERUSER"),
        ("GRANT elspeth_session_schema TO elspeth_session_runtime", "REVOKE elspeth_session_schema FROM elspeth_session_runtime"),
        (
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO elspeth_session_runtime",
            "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) FROM elspeth_session_runtime",
        ),
        (
            "GRANT SET ON PARAMETER session_replication_role TO PUBLIC",
            "REVOKE SET ON PARAMETER session_replication_role FROM PUBLIC",
        ),
        (
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO PUBLIC",
            "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) FROM PUBLIC",
        ),
    ],
)
def test_bootstrap_refuses_privileged_existing_roles(
    compose_postgres: _ComposePostgres,
    mutation: str,
    repair: str,
) -> None:
    admin = create_engine(compose_postgres.admin_url)
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(mutation)
        try:
            result = compose_postgres.bootstrap()
            assert result.returncode != 0
            assert "compose_database_roles_unsafe" in result.stdout + result.stderr
        finally:
            with admin.begin() as connection:
                connection.exec_driver_sql(repair)
    finally:
        admin.dispose()


def test_bootstrap_refuses_legacy_database_owners_without_migrating(compose_postgres: _ComposePostgres) -> None:
    admin = create_engine(compose_postgres.admin_url)
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql("ALTER DATABASE elspeth_sessions OWNER TO elspeth")
        try:
            result = compose_postgres.bootstrap()
            assert result.returncode != 0
            assert "compose_database_legacy_owner" in result.stdout + result.stderr
            with admin.connect() as connection:
                assert (
                    connection.exec_driver_sql(
                        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='elspeth_sessions'"
                    ).scalar_one()
                    == "elspeth"
                )
        finally:
            with admin.begin() as connection:
                connection.exec_driver_sql("ALTER DATABASE elspeth_sessions OWNER TO elspeth_session_schema")
    finally:
        admin.dispose()


@pytest.mark.parametrize(
    "mutation,repair,reason",
    [
        (
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO elspeth_session_runtime",
            "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) FROM elspeth_session_runtime",
            "compose_database_objects_unsafe",
        ),
        (
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO PUBLIC",
            "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) FROM PUBLIC",
            "compose_database_objects_unsafe",
        ),
        (
            "GRANT SELECT ON pg_catalog.pg_authid TO PUBLIC",
            "REVOKE SELECT ON pg_catalog.pg_authid FROM PUBLIC",
            "compose_database_objects_unsafe",
        ),
        (
            "GRANT SELECT (rolpassword) ON pg_catalog.pg_authid TO PUBLIC",
            "REVOKE SELECT (rolpassword) ON pg_catalog.pg_authid FROM PUBLIC",
            "compose_database_objects_unsafe",
        ),
        (
            "GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO elspeth_session_schema",
            "REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) FROM elspeth_session_schema",
            "compose_database_objects_unsafe",
        ),
        (
            "GRANT SELECT ON pg_catalog.pg_authid TO elspeth_session_schema",
            "REVOKE SELECT ON pg_catalog.pg_authid FROM elspeth_session_schema",
            "compose_database_objects_unsafe",
        ),
        (
            "GRANT SELECT (rolpassword) ON pg_catalog.pg_authid TO elspeth_session_schema",
            "REVOKE SELECT (rolpassword) ON pg_catalog.pg_authid FROM elspeth_session_schema",
            "compose_database_objects_unsafe",
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_session_schema GRANT ALL ON TABLES TO elspeth_session_runtime",
            "ALTER DEFAULT PRIVILEGES FOR ROLE elspeth_session_schema REVOKE ALL ON TABLES FROM elspeth_session_runtime",
            "compose_database_objects_unsafe",
        ),
        (
            "GRANT SET ON PARAMETER session_replication_role TO elspeth_session_runtime",
            "REVOKE SET ON PARAMETER session_replication_role FROM elspeth_session_runtime",
            "compose_database_roles_unsafe",
        ),
        (
            "ALTER TABLE chat_messages OWNER TO elspeth_session_runtime",
            "ALTER TABLE chat_messages OWNER TO elspeth_session_schema",
            "compose_database_objects_unsafe",
        ),
    ],
)
def test_bootstrap_refuses_direct_privilege_and_object_ownership_escapes(
    compose_postgres: _ComposePostgres,
    mutation: str,
    repair: str,
    reason: str,
) -> None:
    admin = create_engine(compose_postgres.admin_url.set(database="elspeth_sessions"))
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(mutation)
        try:
            result = compose_postgres.bootstrap()
            assert result.returncode != 0
            assert reason in result.stdout + result.stderr
        finally:
            with admin.begin() as connection:
                connection.exec_driver_sql(repair)
    finally:
        admin.dispose()


def test_bootstrap_recreates_a_missing_application_database(compose_postgres: _ComposePostgres) -> None:
    admin = create_engine(compose_postgres.admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql("DROP DATABASE elspeth_sessions WITH (FORCE)")
        result = compose_postgres.bootstrap()
        assert result.returncode == 0, result.stdout + result.stderr
        owner = compose_postgres.engine("web-init", "session")
        runtime = compose_postgres.engine("web", "session")
        try:
            init_session_schema(owner)
            assert probe_session_schema(runtime) is SchemaState.CURRENT
        finally:
            owner.dispose()
            runtime.dispose()
    finally:
        admin.dispose()


def test_bootstrap_blocks_template_database_public_function_escape(compose_postgres: _ComposePostgres) -> None:
    admin = create_engine(compose_postgres.admin_url)
    template_admin = create_engine(compose_postgres.admin_url.set(database="template1"))
    runtime = compose_postgres.engine("web", "session")
    escaped = create_engine(runtime.url.set(database="template1"))
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql("GRANT CONNECT ON DATABASE template1 TO PUBLIC")
        with template_admin.begin() as connection:
            connection.exec_driver_sql("GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO PUBLIC")
        try:
            # Positive control: this reused-volume mutation really permits the
            # disclosed runtime credential to read a server file before rerun.
            with escaped.connect() as connection:
                assert connection.exec_driver_sql("SELECT pg_read_file('PG_VERSION')").scalar_one().strip() == "16"
                old_pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            result = compose_postgres.bootstrap()
            assert result.returncode == 0, result.stdout + result.stderr
            with admin.connect() as connection:
                assert connection.execute(text("SELECT count(*) FROM pg_stat_activity WHERE pid=:pid"), {"pid": old_pid}).scalar_one() == 0
            with pytest.raises(OperationalError), escaped.connect() as connection:
                connection.exec_driver_sql("SELECT pg_read_file('PG_VERSION')")
            runtime.dispose()
            _assert_no_database_access(runtime, "template1")
        finally:
            with template_admin.begin() as connection:
                connection.exec_driver_sql("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) FROM PUBLIC")
            with admin.begin() as connection:
                connection.exec_driver_sql("REVOKE ALL ON DATABASE template1 FROM PUBLIC")
    finally:
        escaped.dispose()
        runtime.dispose()
        template_admin.dispose()
        admin.dispose()


def test_bootstrap_refuses_unrelated_databases_without_changing_them(compose_postgres: _ComposePostgres) -> None:
    admin = create_engine(compose_postgres.admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql("CREATE DATABASE compose_unrelated")
        try:
            with admin.connect() as connection:
                before = connection.exec_driver_sql("SELECT datdba, datacl FROM pg_database WHERE datname='compose_unrelated'").one()
            result = compose_postgres.bootstrap()
            assert result.returncode != 0
            assert "compose_database_cluster_not_dedicated" in result.stdout + result.stderr
            with admin.connect() as connection:
                assert (
                    connection.exec_driver_sql("SELECT datdba, datacl FROM pg_database WHERE datname='compose_unrelated'").one() == before
                )
        finally:
            with admin.connect() as connection:
                connection.exec_driver_sql("DROP DATABASE compose_unrelated WITH (FORCE)")
    finally:
        admin.dispose()


@pytest.mark.parametrize("database", ["session", "landscape"])
def test_bootstrap_refuses_schema_owned_public_security_definer(compose_postgres: _ComposePostgres, database: str) -> None:
    db_name = "elspeth_sessions" if database == "session" else "elspeth_landscape"
    owner_role = "elspeth_session_schema" if database == "session" else "elspeth_landscape_schema"
    runtime_role = "elspeth_session_runtime" if database == "session" else "elspeth_landscape_runtime"
    admin = create_engine(compose_postgres.admin_url.set(database=db_name))
    owner = compose_postgres.engine("web-init", database)
    runtime = compose_postgres.engine("web", database)
    try:
        with owner.begin() as connection:
            connection.exec_driver_sql(
                "CREATE FUNCTION public.compose_owner_context() RETURNS text LANGUAGE SQL SECURITY DEFINER "
                "AS $$ SELECT current_user::text $$"
            )
            connection.exec_driver_sql(
                "CREATE FUNCTION public.compose_invoker_context() RETURNS text LANGUAGE SQL SECURITY INVOKER "
                "AS $$ SELECT current_user::text $$"
            )
        try:
            # The actual schema-owner defaults expose elevated execution even
            # though the runtime cannot create objects in public itself.
            _assert_denied(runtime, "CREATE TABLE public.compose_forbidden_owner_probe (id integer)")
            with runtime.connect() as connection:
                assert connection.exec_driver_sql("SELECT public.compose_owner_context()").scalar_one() == owner_role
                assert connection.exec_driver_sql("SELECT public.compose_invoker_context()").scalar_one() == runtime_role
            owner.dispose()
            runtime.dispose()
            result = compose_postgres.bootstrap()
            assert result.returncode != 0
            assert "compose_database_objects_unsafe" in result.stdout + result.stderr
            with admin.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT count(*) = 4 AND bool_and(NOT rolcanlogin) FROM pg_roles "
                    "WHERE rolname IN ('elspeth_session_schema', 'elspeth_landscape_schema', "
                    "'elspeth_session_runtime', 'elspeth_landscape_runtime')"
                ).scalar_one()
                assert connection.exec_driver_sql(
                    "SELECT prosecdef FROM pg_proc WHERE oid='public.compose_owner_context()'::regprocedure"
                ).scalar_one()
        finally:
            with admin.begin() as connection:
                connection.exec_driver_sql("DROP FUNCTION public.compose_owner_context()")
            result = compose_postgres.bootstrap()
            assert result.returncode == 0, result.stdout + result.stderr
            with runtime.connect() as connection:
                assert connection.exec_driver_sql("SELECT public.compose_invoker_context()").scalar_one() == runtime_role
            with admin.begin() as connection:
                connection.exec_driver_sql("DROP FUNCTION public.compose_invoker_context()")
    finally:
        runtime.dispose()
        owner.dispose()
        admin.dispose()


def test_bootstrap_refuses_public_security_definer_wrapper_in_an_unknown_schema(compose_postgres: _ComposePostgres) -> None:
    admin = create_engine(compose_postgres.admin_url.set(database="elspeth_sessions"))
    runtime = compose_postgres.engine("web", "session")
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql("CREATE SCHEMA compose_escape")
            connection.exec_driver_sql("GRANT USAGE ON SCHEMA compose_escape TO PUBLIC")
            connection.exec_driver_sql(
                "CREATE FUNCTION compose_escape.read_file() RETURNS text LANGUAGE SQL SECURITY DEFINER "
                "AS $$ SELECT pg_catalog.pg_read_file('PG_VERSION') $$"
            )
        try:
            with runtime.connect() as connection:
                assert connection.exec_driver_sql("SELECT compose_escape.read_file()").scalar_one().strip() == "16"
            result = compose_postgres.bootstrap()
            assert result.returncode != 0
            assert "compose_database_objects_unsafe" in result.stdout + result.stderr
        finally:
            with admin.begin() as connection:
                connection.exec_driver_sql("DROP SCHEMA compose_escape CASCADE")
    finally:
        runtime.dispose()
        admin.dispose()


@pytest.mark.parametrize("database", ["elspeth_sessions", "elspeth_landscape"])
def test_bootstrap_refuses_custom_routines_in_a_builtin_schema(compose_postgres: _ComposePostgres, database: str) -> None:
    admin = create_engine(compose_postgres.admin_url.set(database=database))
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(
                "CREATE FUNCTION information_schema.compose_custom() RETURNS integer LANGUAGE SQL SECURITY DEFINER AS $$ SELECT 1 $$"
            )
            assert connection.exec_driver_sql("SELECT 'information_schema.compose_custom()'::regprocedure::oid >= 16384").scalar_one()
            assert connection.exec_driver_sql(
                "SELECT has_function_privilege('elspeth_session_runtime', 'information_schema.compose_custom()', 'EXECUTE')"
            ).scalar_one()
        try:
            result = compose_postgres.bootstrap()
            assert result.returncode != 0
            assert "compose_database_objects_unsafe" in result.stdout + result.stderr
            with admin.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT count(*) = 4 AND bool_and(NOT rolcanlogin) FROM pg_roles "
                    "WHERE rolname IN ('elspeth_session_schema', 'elspeth_landscape_schema', "
                    "'elspeth_session_runtime', 'elspeth_landscape_runtime')"
                ).scalar_one()
        finally:
            with admin.begin() as connection:
                connection.exec_driver_sql("DROP FUNCTION information_schema.compose_custom()")
        result = compose_postgres.bootstrap()
        assert result.returncode == 0, result.stdout + result.stderr
        with admin.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT count(*) = 4 AND bool_and(rolcanlogin) FROM pg_roles "
                "WHERE rolname IN ('elspeth_session_schema', 'elspeth_landscape_schema', "
                "'elspeth_session_runtime', 'elspeth_landscape_runtime')"
            ).scalar_one()
    finally:
        admin.dispose()


@pytest.mark.parametrize("database", ["session", "landscape"])
@pytest.mark.parametrize(
    ("create", "alter", "drop"),
    [
        (
            "CREATE TYPE public.runtime_owned AS ENUM ('one')",
            "ALTER TYPE public.runtime_owned ADD VALUE 'two'",
            "DROP TYPE public.runtime_owned",
        ),
        (
            "CREATE DOMAIN public.runtime_owned AS integer",
            "ALTER DOMAIN public.runtime_owned ADD CONSTRAINT positive CHECK (VALUE > 0)",
            "DROP DOMAIN public.runtime_owned",
        ),
        (
            'CREATE COLLATION public.runtime_owned FROM pg_catalog."C"',
            "ALTER COLLATION public.runtime_owned REFRESH VERSION",
            "DROP COLLATION public.runtime_owned",
        ),
    ],
    ids=["enum", "domain", "collation"],
)
def test_bootstrap_refuses_runtime_ownership_of_non_relation_objects(
    compose_postgres: _ComposePostgres, database: str, create: str, alter: str, drop: str
) -> None:
    db_name = "elspeth_sessions" if database == "session" else "elspeth_landscape"
    runtime_role = "elspeth_session_runtime" if database == "session" else "elspeth_landscape_runtime"
    object_kind = "COLLATION" if create.startswith("CREATE COLLATION") else "TYPE"
    admin = create_engine(compose_postgres.admin_url.set(database=db_name))
    runtime = compose_postgres.engine("web", database)
    try:
        with admin.begin() as connection:
            connection.exec_driver_sql(create)
            connection.exec_driver_sql(f"ALTER {object_kind} public.runtime_owned OWNER TO {runtime_role}")
        try:
            # Negative control: CREATE was already sealed. Positive control:
            # ownership still permits DDL without that namespace privilege.
            _assert_denied(runtime, "CREATE TYPE public.runtime_new AS ENUM ('one')")
            with runtime.begin() as connection:
                connection.exec_driver_sql(alter)
            runtime.dispose()
            result = compose_postgres.bootstrap()
            assert result.returncode != 0
            assert "compose_database_objects_unsafe" in result.stdout + result.stderr
            with admin.connect() as connection:
                assert connection.exec_driver_sql(
                    "SELECT count(*) = 4 AND bool_and(NOT rolcanlogin) FROM pg_roles "
                    "WHERE rolname IN ('elspeth_session_schema', 'elspeth_landscape_schema', "
                    "'elspeth_session_runtime', 'elspeth_landscape_runtime')"
                ).scalar_one()
        finally:
            with admin.begin() as connection:
                connection.exec_driver_sql(drop)
        result = compose_postgres.bootstrap()
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        runtime.dispose()
        admin.dispose()
