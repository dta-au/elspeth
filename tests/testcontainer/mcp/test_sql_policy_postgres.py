"""Native premises for the finite PG16 policy; no query API integration yet."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent
from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from tests.fixtures.landscape import make_factory
from tests.helpers.postgres_target import PROVISIONED_POSTGRES_URL_ENV, postgres_test_target

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import metadata
from elspeth.engine.orchestrator.bootstrap import prepare_for_run
from elspeth.mcp import query_worker
from elspeth.mcp.analyzer import LandscapeAnalyzer
from elspeth.mcp.postgres_profile import _STORED_EXPRESSIONS, admit_postgres_profile
from elspeth.mcp.query_protocol import QueryBudgetExceeded, QueryWorkerError
from elspeth.mcp.query_worker import RawQueryWorker
from elspeth.mcp.server import create_server
from elspeth.mcp.sql_policy import SCALAR_TYPES, SQLPolicyError, admit_postgres_sql, admit_stored_expression

pytestmark = pytest.mark.testcontainer


@dataclass(frozen=True)
class PolicyTarget:
    admin: Engine
    reader_url: URL


@pytest.fixture(scope="module")
def policy_target() -> Iterator[PolicyTarget]:
    # These initial controls mutate catalog objects. Their authorization is
    # limited to a disposable local container, never a provisioned database.
    if os.environ.get(PROVISIONED_POSTGRES_URL_ENV):
        pytest.fail("SQL policy premises require the disposable PostgreSQL target")
    with postgres_test_target(driver="psycopg", nano_cpus=1_000_000_000, labels={"elspeth-test-owner": "sql-policy-profile"}) as url:
        prepare_for_run()
        with LandscapeDB.from_url(url) as db:
            make_factory(db).run_lifecycle.begin_run(
                config={},
                canonical_version="v1",
                run_id="sql-policy-control",
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )
        admin = create_engine(url)
        role = f"sql_policy_reader_{uuid.uuid4().hex}"
        password = uuid.uuid4().hex
        try:
            with admin.begin() as conn:
                conn.exec_driver_sql(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
                conn.exec_driver_sql(f'GRANT USAGE ON SCHEMA public TO "{role}"')
                conn.exec_driver_sql(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"')
            yield PolicyTarget(admin, make_url(url).set(username=role, password=password))
        finally:
            # The outer target owns the entire disposable cluster. Connections
            # close before it exits; no cross-test role cleanup authority needed.
            admin.dispose()


@pytest.fixture(params=["psycopg", "psycopg2"])
def policy_connection(policy_target: PolicyTarget, request: pytest.FixtureRequest) -> Iterator[Connection]:
    engine = create_engine(policy_target.reader_url.set(drivername=f"postgresql+{request.param}"))
    try:
        with engine.connect() as conn, conn.begin():
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            yield conn
    finally:
        engine.dispose()


def test_canonical_landscape_profile_and_core_select(policy_connection: Connection) -> None:
    """The unchanged real schema must be admitted before refusal tests mean anything."""
    tables = frozenset(metadata.tables)
    stored_refusals = []
    expressions = list(policy_connection.execute(_STORED_EXPRESSIONS, {"tables": sorted(tables)}))
    assert expressions
    for expression, table in expressions:
        try:
            admit_stored_expression(expression, frozenset(column.name for column in metadata.tables[table].columns))
        except SQLPolicyError as exc:
            stored_refusals.append((table, expression, str(exc)))
    assert stored_refusals == []
    admit_postgres_profile(policy_connection, tables)
    sql = """
        WITH audit AS (
            SELECT r.run_id FROM public.runs r
            JOIN public.runs n ON r.run_id = n.run_id
        )
        SELECT count(*) AS total FROM audit WHERE run_id = :run_id
    """
    params = {"run_id": "sql-policy-control"}
    admitted = admit_postgres_sql(sql, params, tables)
    assert admitted.relations == frozenset({"runs"})
    assert policy_connection.execute(text(admitted.sql), params).mappings().all() == [{"total": 1}]
    concatenation = admit_postgres_sql("SELECT 'n=' || CAST(1 AS bigint) || '!' AS value", None, tables)
    assert policy_connection.execute(text(concatenation.sql)).scalar_one() == "n=1!"


@pytest.mark.parametrize("value", ["ordinary", "雪% ' :quoted --", "'); SELECT pg_sleep(30); --"])
def test_native_bind_values_remain_data(policy_connection: Connection, value: str) -> None:
    tables = frozenset(metadata.tables)
    admit_postgres_profile(policy_connection, tables)
    params = {"value": value}
    admitted = admit_postgres_sql("SELECT :value AS value, '50%' AS percent", params, tables)
    assert policy_connection.execute(text(admitted.sql), params).mappings().all() == [{"value": value, "percent": "50%"}]


@pytest.mark.parametrize(
    "expression",
    [
        "1 + '2'",
        "'2' + 1",
        "1 + NULL",
        "NULL + 1",
        "1.5 + '2'",
        "'2' + 1.5",
        "1.5 + NULL",
        "NULL + 1.5",
        "DATE '2000-01-01' + '1'",
        "'1' + DATE '2000-01-01'",
        "DATE '2000-01-01' + NULL",
        "NULL + DATE '2000-01-01'",
        "TIME '12:00' + '1 hour'",
        "'1 hour' + TIME '12:00'",
        "TIME '12:00' + NULL",
        "NULL + TIME '12:00'",
        "INTERVAL '1 hour' + '2 hours'",
        "'2 hours' + INTERVAL '1 hour'",
        "INTERVAL '1 hour' + NULL",
        "NULL + INTERVAL '1 hour'",
        "TIMESTAMP '2000-01-01' + '1 hour'",
        "'1 hour' + TIMESTAMP '2000-01-01'",
        "TIMESTAMPTZ '2000-01-01 UTC' + NULL",
        "NULL + TIMESTAMPTZ '2000-01-01 UTC'",
        "'n=' || 1",
        "1 || '=n'",
        "'n=' || NULL",
        "NULL || '=n'",
        "round(1.5)",
        "-1",
        "1 = NULL",
        "NULL = 1",
    ],
)
def test_unknown_operator_resolution_stays_scalar(policy_connection: Connection, expression: str) -> None:
    tables = frozenset(metadata.tables)
    admit_postgres_profile(policy_connection, tables)
    scalar_oids = set(
        policy_connection.execute(
            text("SELECT oid FROM pg_catalog.pg_type WHERE typnamespace = 'pg_catalog'::regnamespace AND typname = ANY(:types)"),
            {"types": sorted(SCALAR_TYPES)},
        ).scalars()
    )
    assert scalar_oids
    admitted = admit_postgres_sql(f"SELECT {expression} AS value", None, tables)
    try:
        with policy_connection.begin_nested():
            result = policy_connection.execute(text(admitted.sql))
            assert result.cursor is not None
            assert result.cursor.description[0].type_code in scalar_oids
            result.all()
    except DBAPIError as exc:
        # Ambiguous/undefined operators and invalid ordinary literal input
        # are supported refusals, never permission for an omitted type.
        code = exc.orig.sqlstate if policy_connection.dialect.driver == "psycopg" else exc.orig.pgcode
        assert code in {"42883", "42725", "22P02", "22007"}


def test_implicit_function_and_system_columns_refuse(policy_connection: Connection) -> None:
    tables = frozenset(metadata.tables)
    admit_postgres_profile(policy_connection, tables)
    # Native positive demonstrates PostgreSQL's field/function fallback; the
    # syntax boundary must refuse it before analysis in the actual query API.
    assert policy_connection.exec_driver_sql("SELECT r.row_to_json FROM runs r").scalar_one()["run_id"] == "sql-policy-control"
    for sql in ("SELECT r.row_to_json FROM runs r", "SELECT r.ctid FROM runs r", "SELECT xmin FROM runs"):
        with pytest.raises(SQLPolicyError):
            admit_postgres_sql(sql, None, tables)
    admitted = admit_postgres_sql("SELECT d.run_id FROM (SELECT r.run_id FROM runs r) d", None, tables)
    assert policy_connection.execute(text(admitted.sql)).scalar_one() == "sql-policy-control"


def test_extra_column_profile_change_refuses(policy_target: PolicyTarget, policy_connection: Connection) -> None:
    tables = frozenset(metadata.tables)
    admit_postgres_profile(policy_connection, tables)
    with policy_target.admin.begin() as admin:
        admin.exec_driver_sql("SET LOCAL lock_timeout = '1000ms'")
        admin.exec_driver_sql("ALTER TABLE public.runs ADD COLUMN unexpected text")
    try:
        with pytest.raises(SQLPolicyError, match="noncanonical_column_identity"):
            admit_postgres_profile(policy_connection, tables)
    finally:
        with policy_target.admin.begin() as admin:
            admin.exec_driver_sql("SET LOCAL lock_timeout = '1000ms'")
            admin.exec_driver_sql("ALTER TABLE public.runs DROP COLUMN unexpected")
    admit_postgres_profile(policy_connection, tables)


def test_rls_profile_change_is_refused(policy_target: PolicyTarget, policy_connection: Connection) -> None:
    tables = frozenset(metadata.tables)
    admit_postgres_profile(policy_connection, tables)
    with policy_target.admin.begin() as admin:
        admin.exec_driver_sql("ALTER TABLE public.runs ENABLE ROW LEVEL SECURITY")
    try:
        with pytest.raises(SQLPolicyError, match="noncanonical_relation_profile"):
            admit_postgres_profile(policy_connection, tables)
    finally:
        with policy_target.admin.begin() as admin:
            admin.exec_driver_sql("ALTER TABLE public.runs DISABLE ROW LEVEL SECURITY")
    admit_postgres_profile(policy_connection, tables)


def test_custom_trigger_return_overload_is_refused(policy_target: PolicyTarget, policy_connection: Connection) -> None:
    tables = frozenset(metadata.tables)
    admit_postgres_profile(policy_connection, tables)
    admitted = admit_postgres_sql("SELECT lower(CAST(1 AS bigint)) IS NULL", None, tables)
    assert admitted.functions == frozenset({"lower"})
    with policy_target.admin.begin() as admin:
        # A harmless internal alias establishes the unsupported catalog shape.
        # Do not invoke it: a trigger pseudo-type declaration is not a guarantee
        # that arbitrary internal entry points enforce trigger-only execution.
        admin.exec_driver_sql("CREATE FUNCTION public.lower(bigint) RETURNS trigger LANGUAGE internal AS 'int8abs'")
        assert (
            admin.exec_driver_sql(
                "SELECT prorettype::regtype::text FROM pg_proc WHERE oid = 'public.lower(bigint)'::regprocedure"
            ).scalar_one()
            == "trigger"
        )
    try:
        with pytest.raises(SQLPolicyError, match=r"custom routine: public\.lower"):
            admit_postgres_profile(policy_connection, tables)
    finally:
        with policy_target.admin.begin() as admin:
            admin.exec_driver_sql("DROP FUNCTION public.lower(bigint)")
    admit_postgres_profile(policy_connection, tables)


@pytest.mark.parametrize("driver", ["psycopg", "psycopg2"])
def test_existing_temporary_shadow_is_refused(policy_target: PolicyTarget, driver: str) -> None:
    tables = frozenset(metadata.tables)
    engine = create_engine(policy_target.reader_url.set(drivername=f"postgresql+{driver}"))
    try:
        with engine.connect() as conn:
            with conn.begin():
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                admit_postgres_profile(conn, tables)
            with conn.begin():
                conn.exec_driver_sql("CREATE TEMPORARY TABLE runs (run_id text)")
                conn.exec_driver_sql("INSERT INTO pg_temp.runs VALUES ('shadow-control')")
                assert conn.exec_driver_sql("SELECT run_id FROM runs").scalar_one() == "shadow-control"
            with conn.begin():
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                with pytest.raises(SQLPolicyError):
                    admit_postgres_profile(conn, tables)
                # Explicit pg_temp LAST resolves the canonical public table even
                # in this refused session; prior temp state never gains admission.
                assert conn.exec_driver_sql("SELECT run_id FROM runs").scalar_one() == "sql-policy-control"
    finally:
        engine.dispose()


@pytest.fixture(params=["psycopg", "psycopg2"])
def reader_url(policy_target: PolicyTarget, request: pytest.FixtureRequest) -> str:
    return policy_target.reader_url.set(drivername=f"postgresql+{request.param}").render_as_string(hide_password=False)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_advisory_xact_lock(314159)",
        "SELECT set_config('application_name', 'caller-effect', false)",
        "SELECT pg_notify('sql_policy_control', 'caller-effect')",
        "SELECT query_to_xml('SELECT 1', true, false, '')",
        "SELECT pg_sleep(1)",
        "SELECT random()",
        "SELECT r.row_to_json FROM runs r",
        "SELECT r.ctid FROM runs r",
        "SELECT pg_read_file('PG_VERSION')",
        "SELECT * FROM runs FOR UPDATE",
        "SELECT * FROM generate_series(1, 3)",
        "WITH RECURSIVE n AS (SELECT 1) SELECT * FROM n",
    ],
)
def test_direct_facade_refuses_before_caller_execution(reader_url: str, sql: str) -> None:
    analyzer = LandscapeAnalyzer(reader_url)
    executed: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, _params: object, _context: object, _many: bool) -> None:
        executed.append(statement)

    try:
        assert analyzer.query("SELECT run_id FROM runs WHERE run_id=:id", {"id": "sql-policy-control"}) == [
            {"run_id": "sql-policy-control"}
        ]
        event.listen(analyzer._db.engine, "before_cursor_execute", record)
        with pytest.raises(SQLPolicyError):
            analyzer.query(sql)
        assert sql not in executed
        assert analyzer.query("SELECT 2 AS value") == [{"value": 2}]
    finally:
        event.remove(analyzer._db.engine, "before_cursor_execute", record)
        analyzer.close()


def test_native_direct_parameters_keep_existing_value_contract(reader_url: str) -> None:
    analyzer = LandscapeAnalyzer(reader_url)
    try:
        for value in (Decimal("1.50"), date(2026, 1, 1), timedelta(seconds=5), b"bytes"):
            with analyzer._db.read_only_connection() as conn:
                native = conn.execute(text("SELECT :value AS value"), {"value": value}).scalar_one()
            actual = analyzer.query("SELECT :value AS value", {"value": value})[0]["value"]
            assert type(actual) is type(native)
            if isinstance(native, memoryview):
                assert actual.tobytes() == native.tobytes() == value
            else:
                assert actual == native == value
        with pytest.raises(ValueError, match="duplicate column"):
            analyzer.query("SELECT 1 AS x, 2 AS x")
        assert analyzer.query("SELECT 1 AS value", limit=1) == [{"value": 1}]
    finally:
        analyzer.close()


@pytest.mark.parametrize(
    "create,drop,control",
    [
        (
            "CREATE FUNCTION public.sql_policy_probe() RETURNS text LANGUAGE sql STABLE AS $$ SELECT 'control'::text $$",
            "DROP FUNCTION public.sql_policy_probe()",
            "SELECT public.sql_policy_probe()",
        ),
        (
            "CREATE OPERATOR public.=== (LEFTARG=integer, RIGHTARG=integer, FUNCTION=pg_catalog.int4eq)",
            "DROP OPERATOR public.=== (integer, integer)",
            "SELECT 1 OPERATOR(public.===) 1",
        ),
        (
            "CREATE CAST (jsonb AS text) WITH INOUT",
            "DROP CAST (jsonb AS text)",
            "SELECT CAST('{}'::jsonb AS text)",
        ),
        (
            "CREATE AGGREGATE public.count(integer) (SFUNC=pg_catalog.int4pl, STYPE=integer, INITCOND='0')",
            "DROP AGGREGATE public.count(integer)",
            "SELECT public.count(1)",
        ),
        ("CREATE DOMAIN public.sql_policy_domain AS text", "DROP DOMAIN public.sql_policy_domain", "SELECT 'x'::public.sql_policy_domain"),
        (
            "CREATE TYPE public.sql_policy_enum AS ENUM ('control')",
            "DROP TYPE public.sql_policy_enum",
            "SELECT 'control'::public.sql_policy_enum",
        ),
        (
            "CREATE TABLE public.sql_policy_child () INHERITS (public.runs)",
            "DROP TABLE public.sql_policy_child",
            "SELECT count(*) FROM public.sql_policy_child",
        ),
    ],
)
def test_custom_profile_drift_refuses_same_facade(
    policy_target: PolicyTarget, reader_url: str, create: str, drop: str, control: str
) -> None:
    analyzer = LandscapeAnalyzer(reader_url)
    try:
        assert analyzer.query("SELECT 1 AS value") == [{"value": 1}]
        with policy_target.admin.begin() as admin:
            admin.exec_driver_sql(create)
            admin.exec_driver_sql(control).all()
        try:
            with pytest.raises(SQLPolicyError):
                analyzer.query("SELECT 1 AS value")
        finally:
            with policy_target.admin.begin() as admin:
                admin.exec_driver_sql(drop)
        assert analyzer.query("SELECT 1 AS value") == [{"value": 1}]
    finally:
        analyzer.close()


def test_generic_reader_ownership_and_index_collation_refuse(policy_target: PolicyTarget, reader_url: str) -> None:
    analyzer = LandscapeAnalyzer(reader_url)
    try:
        assert analyzer.query("SELECT 1 AS value") == [{"value": 1}]
        with policy_target.admin.begin() as admin:
            admin.exec_driver_sql("CREATE COLLATION public.sql_policy_collation (provider=libc, locale='C')")
        try:
            with policy_target.admin.begin() as admin:
                admin.exec_driver_sql("CREATE INDEX sql_policy_index ON public.runs (run_id COLLATE public.sql_policy_collation)")
            try:
                with pytest.raises(SQLPolicyError, match="unsupported_index_profile"):
                    analyzer.query("SELECT 1 AS value")
            finally:
                with policy_target.admin.begin() as admin:
                    admin.exec_driver_sql("DROP INDEX public.sql_policy_index")
            assert analyzer.query("SELECT 1 AS value") == [{"value": 1}]
            role = policy_target.reader_url.username
            assert role is not None
            with policy_target.admin.begin() as admin:
                admin.exec_driver_sql(f'ALTER COLLATION public.sql_policy_collation OWNER TO "{role}"')
            with pytest.raises(SQLPolicyError, match="reader_owns_database_objects"):
                analyzer.query("SELECT 1 AS value")
        finally:
            with policy_target.admin.begin() as admin:
                admin.exec_driver_sql("DROP COLLATION public.sql_policy_collation")
        assert analyzer.query("SELECT 1 AS value") == [{"value": 1}]
    finally:
        analyzer.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Qualified Linux worker isolation")
async def test_actual_mcp_sql_policy_and_exact_result(reader_url: str) -> None:
    server = create_server(reader_url)

    async def call(sql: str, params: dict[str, object] | None = None) -> CallToolResult:
        response = await server.request_handlers[CallToolRequest](
            CallToolRequest(method="tools/call", params=CallToolRequestParams(name="query", arguments={"sql": sql, "params": params}))
        )
        assert isinstance(response.root, CallToolResult)
        return response.root

    result = await call("SELECT run_id, :label AS label FROM runs WHERE run_id=:id", {"id": "sql-policy-control", "label": '雪\n"'})
    assert not result.isError
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == json.dumps([{"run_id": "sql-policy-control", "label": '雪\n"'}], indent=2)
    for sql in (
        "SELECT pg_advisory_xact_lock(314159)",
        "SELECT set_config('application_name', 'caller-effect', false)",
        "SELECT pg_notify('sql_policy_control', 'caller-effect')",
        "SELECT query_to_xml('SELECT 1', true, false, '')",
        "SELECT random()",
        "SELECT r.row_to_json FROM runs r",
    ):
        refusal = await call(sql)
        assert refusal.isError
        assert isinstance(refusal.content[0], TextContent)
        assert "policy" in refusal.content[0].text
        assert sql not in refusal.content[0].text
    worker = RawQueryWorker(reader_url)
    for sql, category in (("SELECT 'NaN'::float8 AS value", "integrity"), ("SELECT '\\xff'::bytea AS value", "serialization")):
        with pytest.raises(QueryWorkerError, match=category):
            await worker.execute(sql, None, 1)
    assert not (await call("SELECT 7 AS value")).isError


def _doubling_sql(stages: int) -> str:
    ctes = ["n0 AS MATERIALIZED (SELECT :value AS value)"]
    ctes.extend(f"n{i} AS MATERIALIZED (SELECT value || value AS value FROM n{i - 1})" for i in range(1, stages + 1))
    return "WITH " + ", ".join(ctes) + f" SELECT value FROM n{stages}"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Qualified Linux worker isolation")
async def test_postgres_worker_bounded_output_and_native_allocation(
    reader_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worker = RawQueryWorker(reader_url)
    with pytest.raises(QueryBudgetExceeded, match="output"):
        await worker.execute(_doubling_sql(9), {"value": "x" * 32768}, 1)
    native_facts = tmp_path / "native-error-facts.json"
    real_create = asyncio.create_subprocess_exec
    # Observe only a test-owned native error fact, never export raw diagnostics
    # or change the production frame classification. Apply the same limit
    # before SQLAlchemy/driver imports as the actual worker entry point.
    script = f"""
import json, resource, sys
from pathlib import Path
memory = int(sys.argv[1])
resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
from sqlalchemy import Engine, event
from elspeth.mcp.query_worker import worker_main
native_errors = []
def observe(context):
    error = context.original_exception
    if context.dialect.driver == 'psycopg':
        import psycopg
        assert isinstance(error, psycopg.Error)
        state = error.sqlstate
    else:
        import psycopg2
        assert isinstance(error, psycopg2.Error)
        state = error.pgcode
    native_errors.append({{'sqlstate': state, 'native_exception': type(error).__name__}})
    assert len(native_errors) <= 8
    Path({str(native_facts)!r}).write_text(json.dumps(native_errors))
event.listen(Engine, 'handle_error', observe)
raise SystemExit(worker_main())
"""

    async def observed_child(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        assert args[-2] == "elspeth.mcp.query_worker"
        return await real_create(sys.executable, "-c", script, args[-1], **kwargs)

    monkeypatch.setattr(query_worker.asyncio, "create_subprocess_exec", observed_child)
    large_sql = _doubling_sql(12)
    parameters = {"value": "x" * 32768}
    # Same driver, statement, inputs and observer: at the ordinary address-space
    # limit the native scalar reaches the known JSON output refusal. Lower only
    # the address-space limit for the contrasting native client failure.
    with pytest.raises(QueryBudgetExceeded, match="output"):
        await worker.execute(large_sql, parameters, 1)
    assert not native_facts.exists()
    monkeypatch.setattr(query_worker, "WORKER_MEMORY_BYTES", 512 * 1024 * 1024)
    assert json.loads(await worker.execute("SELECT 1 AS value", None, 1)) == [{"value": 1}]
    with pytest.raises(QueryWorkerError, match="database"):
        await worker.execute(large_sql, parameters, 1)
    facts = json.loads(native_facts.read_text())
    assert facts
    assert all(fact["sqlstate"] is None for fact in facts), facts
    monkeypatch.setattr(query_worker.asyncio, "create_subprocess_exec", real_create)
    assert json.loads(await worker.execute("SELECT 1 AS value", None, 1)) == [{"value": 1}]


@pytest.fixture(scope="module")
def cancellation_rows(policy_target: PolicyTarget) -> None:
    with LandscapeDB.from_url(policy_target.admin.url.render_as_string(hide_password=False)) as db:
        factory = make_factory(db)
        for index in range(50):
            factory.run_lifecycle.begin_run(
                config={},
                canonical_version="v1",
                run_id=f"sql-cancel-{index}",
                openrouter_catalog_sha256="0" * 64,
                openrouter_catalog_source="bundled",
            )


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Qualified Linux worker isolation")
@pytest.mark.parametrize("ending", ["caller-cancel", "parent-deadline", "statement-timeout"])
async def test_native_cancellation_releases_owned_backend(
    policy_target: PolicyTarget,
    reader_url: str,
    cancellation_rows: None,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    application = "sql_policy_cancel_" + uuid.uuid4().hex
    url = make_url(reader_url).update_query_dict({"application_name": application}).render_as_string(hide_password=False)

    def active() -> int:
        with policy_target.admin.connect() as admin:
            return admin.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE application_name=:app AND state='active' "
                    "AND (query LIKE '%sql_policy_workload%' OR query LIKE 'FETCH %')"
                ),
                {"app": application},
            ).scalar_one()

    def backends() -> int:
        with policy_target.admin.connect() as admin:
            return admin.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE application_name=:app"), {"app": application}
            ).scalar_one()

    assert active() == 0
    assert backends() == 0
    if ending == "parent-deadline":
        monkeypatch.setattr(query_worker, "QUERY_SECONDS", 5.0)
        monkeypatch.setattr(query_worker, "CLEANUP_SECONDS", 0.2)
    sql = "SELECT count(*) AS sql_policy_workload FROM runs a CROSS JOIN runs b CROSS JOIN runs c CROSS JOIN runs d CROSS JOIN runs e CROSS JOIN runs f CROSS JOIN runs g CROSS JOIN runs h"
    worker = RawQueryWorker(url)
    started = time.monotonic()
    task = asyncio.create_task(worker.execute(sql, None, 1))
    try:
        for _ in range(200):
            if active() == 1:
                assert backends() == 1
                break
            if task.done():
                await task
                pytest.fail("Control workload finished before activity observation")
            await asyncio.sleep(0.05)
        else:
            pytest.fail("Control workload never became active")
        if ending == "caller-cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif ending == "parent-deadline":
            with pytest.raises(QueryBudgetExceeded, match="wall-clock"):
                await task
            assert time.monotonic() - started < 8
        else:
            with pytest.raises(QueryWorkerError, match="database"):
                await task
            assert time.monotonic() - started < 20
        for _ in range(40):
            if backends() == 0:
                break
            await asyncio.sleep(0.05)
        assert active() == 0
        assert backends() == 0
        assert json.loads(await worker.execute("SELECT 1 AS value", None, 1)) == [{"value": 1}]
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
