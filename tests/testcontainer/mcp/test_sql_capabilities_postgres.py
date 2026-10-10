"""Real PostgreSQL admission for caller-authored MCP audit SQL."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from tests.fixtures.landscape import make_factory
from tests.helpers.postgres_target import postgres_test_target

from elspeth.core.landscape.database import LandscapeDB
from elspeth.engine.orchestrator.bootstrap import prepare_for_run
from elspeth.mcp.analyzer import LandscapeAnalyzer
from elspeth.mcp.sql_guard import MCPQueryPrivilegeError

pytestmark = pytest.mark.testcontainer


@dataclass(frozen=True)
class ReaderTarget:
    admin: Engine
    admin_url: str
    reader_url: str
    role: str


@pytest.fixture(scope="module")
def reader_target() -> Iterator[ReaderTarget]:
    with postgres_test_target(driver="psycopg") as url:
        prepare_for_run()
        with LandscapeDB.from_url(url) as db:
            make_factory(db).run_lifecycle.begin_run(
                config={},
                canonical_version="v1",
                run_id="sql-capability-control",
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        admin = create_engine(url)
        role = f"mcp_reader_{uuid.uuid4().hex}"
        password = uuid.uuid4().hex
        with admin.begin() as conn:
            conn.exec_driver_sql(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
            conn.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role}"')
            conn.exec_driver_sql(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"')
        reader_url = make_url(url).set(username=role, password=password).render_as_string(hide_password=False)
        try:
            yield ReaderTarget(admin, url, reader_url, role)
        finally:
            with admin.begin() as conn:
                conn.exec_driver_sql(f'DROP OWNED BY "{role}"')
                conn.exec_driver_sql(f'DROP ROLE "{role}"')
            admin.dispose()


def test_builtin_initial_acl_survives_public_grant(reader_target: ReaderTarget) -> None:
    """Control the ACL instrument against both initially public and privileged routines."""
    with reader_target.admin.begin() as conn:
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        assert conn.execute(text("SELECT pg_read_file('PG_VERSION')")).scalar_one().strip() == "16"
    with reader_target.admin.begin() as conn:
        initial = conn.execute(
            text("""
            SELECT initprivs::text FROM pg_catalog.pg_init_privs
            WHERE classoid = 'pg_catalog.pg_proc'::regclass
              AND objoid = 'pg_catalog.pg_read_file(text)'::regprocedure
              AND privtype = 'i'
        """)
        ).scalar_one()
        assert not conn.execute(
            text("""
            SELECT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(i.initprivs) a
                           WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')
            FROM pg_catalog.pg_init_privs i
            WHERE i.classoid = 'pg_catalog.pg_proc'::regclass
              AND i.objoid = 'pg_catalog.pg_read_file(text)'::regprocedure
        """)
        ).scalar_one()
        assert conn.execute(
            text("""
            SELECT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(pg_catalog.acldefault('f', p.proowner)) a
                           WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')
            FROM pg_catalog.pg_proc p WHERE p.oid = 'pg_catalog.count()'::regprocedure
        """)
        ).scalar_one()
        conn.exec_driver_sql("GRANT EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) TO PUBLIC")
        try:
            assert (
                conn.execute(
                    text("""
                SELECT initprivs::text FROM pg_catalog.pg_init_privs
                WHERE classoid = 'pg_catalog.pg_proc'::regclass
                  AND objoid = 'pg_catalog.pg_read_file(text)'::regprocedure
                  AND privtype = 'i'
            """)
                ).scalar_one()
                == initial
            )
        finally:
            conn.exec_driver_sql("REVOKE EXECUTE ON FUNCTION pg_catalog.pg_read_file(text) FROM PUBLIC")


def test_restricted_reader_keeps_audit_selects_and_database_denies_file_read(reader_target: ReaderTarget) -> None:
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        assert analyzer.query("SELECT 1 AS value") == [{"value": 1}]
        assert analyzer.query(
            """
            WITH audit AS (SELECT r.run_id FROM runs r JOIN runs n ON r.run_id = n.run_id)
            SELECT count(*) AS total FROM audit WHERE run_id = :run_id
        """,
            params={"run_id": "sql-capability-control"},
        ) == [{"total": 1}]
        with pytest.raises(DBAPIError) as failure:
            analyzer.query("SELECT pg_catalog.pg_read_file('PG_VERSION') AS body")
        assert failure.value.orig.sqlstate == "42501"
        assert analyzer.query("SELECT 2 AS value") == [{"value": 2}]
    finally:
        analyzer.close()


def test_admin_is_refused_before_caller_sql(reader_target: ReaderTarget) -> None:
    analyzer = LandscapeAnalyzer(reader_target.admin_url)
    payload = "SELECT pg_catalog.pg_read_file('PG_VERSION') AS body"
    executed: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, _parameters: object, _context: object, _many: bool) -> None:
        executed.append(statement)

    event.listen(analyzer._db.engine, "before_cursor_execute", record)
    try:
        with pytest.raises(MCPQueryPrivilegeError):
            analyzer.query(payload)
        assert executed
        assert payload not in executed
    finally:
        event.remove(analyzer._db.engine, "before_cursor_execute", record)
        analyzer.close()


@pytest.mark.parametrize("grantee", ["reader", "PUBLIC"])
@pytest.mark.parametrize("signature", ["pg_read_file(text)", "pg_read_binary_file(text,bigint,bigint)", "pg_ls_dir(text)"])
def test_direct_and_public_execute_grants_are_refused(reader_target: ReaderTarget, signature: str, grantee: str) -> None:
    recipient = f'"{reader_target.role}"' if grantee == "reader" else "PUBLIC"
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        assert analyzer.query("SELECT count(*) AS total FROM runs") == [{"total": 1}]
        with reader_target.admin.begin() as conn:
            conn.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION pg_catalog.{signature} TO {recipient}")
        try:
            with pytest.raises(MCPQueryPrivilegeError, match="privileged built-in"):
                analyzer.query("SELECT 1 AS value")
        finally:
            with reader_target.admin.begin() as conn:
                conn.exec_driver_sql(f"REVOKE EXECUTE ON FUNCTION pg_catalog.{signature} FROM {recipient}")
        assert analyzer.query("SELECT 1 AS value") == [{"value": 1}]
    finally:
        analyzer.close()


@pytest.mark.parametrize("inherited", [True, False])
def test_membership_is_refused_even_without_inheritance(reader_target: ReaderTarget, inherited: bool) -> None:
    group = f"mcp_group_{uuid.uuid4().hex}"
    with reader_target.admin.begin() as conn:
        conn.exec_driver_sql(f'CREATE ROLE "{group}"')
        conn.exec_driver_sql(f'GRANT pg_read_server_files TO "{group}"')
        conn.exec_driver_sql(f'GRANT "{group}" TO "{reader_target.role}" WITH INHERIT {str(inherited).upper()}')
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        with pytest.raises(MCPQueryPrivilegeError, match="memberships"):
            analyzer.query("SELECT 1")
    finally:
        analyzer.close()
        with reader_target.admin.begin() as conn:
            conn.exec_driver_sql(f'DROP ROLE "{group}"')


def test_restricted_effective_role_cannot_hide_admin_session(reader_target: ReaderTarget) -> None:
    disguised = make_url(reader_target.admin_url).update_query_dict({"options": f"-c role={reader_target.role}"})
    analyzer = LandscapeAnalyzer(disguised.render_as_string(hide_password=False))
    try:
        with pytest.raises(MCPQueryPrivilegeError):
            analyzer.query("SELECT 1")
    finally:
        analyzer.close()


def test_security_definer_wrapper_is_refused(reader_target: ReaderTarget) -> None:
    with reader_target.admin.begin() as conn:
        conn.exec_driver_sql("""
            CREATE FUNCTION public.mcp_file_wrapper() RETURNS text
            LANGUAGE sql SECURITY DEFINER AS $$ SELECT pg_catalog.pg_read_file('PG_VERSION') $$
        """)
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        # Prove the wrapper really confers file authority with a low-level connection.
        with analyzer._db.read_only_connection() as conn:
            assert conn.execute(text("SELECT public.mcp_file_wrapper()")).scalar_one().strip() == "16"
        with pytest.raises(MCPQueryPrivilegeError, match="custom routines"):
            analyzer.query("SELECT public.mcp_file_wrapper() AS body")
    finally:
        analyzer.close()
        with reader_target.admin.begin() as conn:
            conn.exec_driver_sql("DROP FUNCTION public.mcp_file_wrapper()")


def test_column_write_grant_is_refused(reader_target: ReaderTarget) -> None:
    with reader_target.admin.begin() as conn:
        conn.exec_driver_sql(f'GRANT UPDATE(status) ON runs TO "{reader_target.role}"')
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        with pytest.raises(MCPQueryPrivilegeError, match="write privileges"):
            analyzer.query("SELECT 1")
    finally:
        analyzer.close()
        with reader_target.admin.begin() as conn:
            conn.exec_driver_sql(f'REVOKE UPDATE(status) ON runs FROM "{reader_target.role}"')


def test_view_cannot_confer_read_authority_outside_landscape(reader_target: ReaderTarget) -> None:
    with reader_target.admin.begin() as conn:
        conn.exec_driver_sql("CREATE SCHEMA mcp_private")
        conn.exec_driver_sql("CREATE TABLE mcp_private.secret (body text)")
        conn.exec_driver_sql("INSERT INTO mcp_private.secret VALUES ('private-control')")
        conn.exec_driver_sql("CREATE VIEW public.mcp_view AS SELECT body FROM mcp_private.secret")
        conn.exec_driver_sql(f'GRANT SELECT ON public.mcp_view TO "{reader_target.role}"')
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        with analyzer._db.read_only_connection() as conn:
            assert conn.execute(text("SELECT body FROM public.mcp_view")).scalar_one() == "private-control"
        with pytest.raises(MCPQueryPrivilegeError):
            analyzer.query("SELECT body FROM public.mcp_view")
    finally:
        analyzer.close()
        with reader_target.admin.begin() as conn:
            conn.exec_driver_sql("DROP VIEW public.mcp_view")
            conn.exec_driver_sql("DROP SCHEMA mcp_private CASCADE")


@pytest.mark.parametrize("grantee", ["reader", "PUBLIC"])
@pytest.mark.parametrize("grant", ["SELECT ON pg_catalog.pg_authid", "SELECT(subconninfo) ON pg_catalog.pg_subscription"])
def test_privileged_catalog_table_and_column_grants_are_refused(reader_target: ReaderTarget, grantee: str, grant: str) -> None:
    recipient = f'"{reader_target.role}"' if grantee == "reader" else "PUBLIC"
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        assert analyzer.query("SELECT 1 AS value") == [{"value": 1}]
        with reader_target.admin.begin() as conn:
            conn.exec_driver_sql(f"GRANT {grant} TO {recipient}")
        try:
            with pytest.raises(MCPQueryPrivilegeError, match="privileged catalog"):
                analyzer.query("SELECT 1")
        finally:
            with reader_target.admin.begin() as conn:
                conn.exec_driver_sql(f"REVOKE {grant} FROM {recipient}")
        assert analyzer.query("SELECT 1 AS value") == [{"value": 1}]
    finally:
        analyzer.close()


@pytest.mark.parametrize("attribute", ["CREATEDB", "CREATEROLE", "BYPASSRLS", "REPLICATION"])
def test_administrative_role_attribute_is_refused(reader_target: ReaderTarget, attribute: str) -> None:
    with reader_target.admin.begin() as conn:
        conn.exec_driver_sql(f'ALTER ROLE "{reader_target.role}" {attribute}')
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        with pytest.raises(MCPQueryPrivilegeError, match="administrative role"):
            analyzer.query("SELECT 1")
    finally:
        analyzer.close()
        with reader_target.admin.begin() as conn:
            conn.exec_driver_sql(f'ALTER ROLE "{reader_target.role}" NO{attribute}')


def test_custom_operator_using_builtin_function_is_refused(reader_target: ReaderTarget) -> None:
    with reader_target.admin.begin() as conn:
        conn.exec_driver_sql("CREATE OPERATOR public.=== (LEFTARG = integer, RIGHTARG = integer, FUNCTION = pg_catalog.int4eq)")
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        with analyzer._db.read_only_connection() as conn:
            assert conn.execute(text("SELECT 1 OPERATOR(public.===) 1")).scalar_one()
        with pytest.raises(MCPQueryPrivilegeError, match="custom operators"):
            analyzer.query("SELECT 1 OPERATOR(public.===) 1")
    finally:
        analyzer.close()
        with reader_target.admin.begin() as conn:
            conn.exec_driver_sql("DROP OPERATOR public.=== (integer, integer)")


def test_custom_domain_is_refused(reader_target: ReaderTarget) -> None:
    with reader_target.admin.begin() as conn:
        conn.exec_driver_sql("CREATE DOMAIN public.mcp_domain AS text")
    analyzer = LandscapeAnalyzer(reader_target.reader_url)
    try:
        with pytest.raises(MCPQueryPrivilegeError, match="custom types"):
            analyzer.query("SELECT CAST('control' AS public.mcp_domain)")
    finally:
        analyzer.close()
        with reader_target.admin.begin() as conn:
            conn.exec_driver_sql("DROP DOMAIN public.mcp_domain")
