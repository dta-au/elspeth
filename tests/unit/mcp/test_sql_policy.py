"""Positive and refusal controls for the restricted PG16 syntax boundary."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import psycopg, psycopg2

from elspeth.mcp import sql_policy
from elspeth.mcp.sql_policy import SQLPolicyError, admit_postgres_sql, admit_stored_expression

TABLES = frozenset({"runs", "nodes"})


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 AS value",
        "SELECT run_id, status FROM runs WHERE status = 'completed' ORDER BY run_id LIMIT 10",
        "SELECT r.run_id, count(*) AS n FROM runs r LEFT JOIN nodes n ON n.run_id=r.run_id GROUP BY r.run_id HAVING count(*)>0",
        "WITH x AS (SELECT run_id FROM runs) SELECT * FROM x",
        "SELECT * FROM (SELECT run_id FROM public.runs) q",
        "SELECT DISTINCT status FROM runs",
        "SELECT r.* FROM runs r JOIN nodes n USING (run_id)",
        "SELECT count(DISTINCT run_id) FILTER (WHERE status='completed') FROM runs",
        "SELECT CASE WHEN 1=1 THEN 2 ELSE 3 END, COALESCE(NULL,1), NULLIF(1,2)",
        "SELECT 1 BETWEEN 0 AND 2, 1 IN (1,2), 1 IS DISTINCT FROM 2",
        "SELECT CAST(1 AS numeric), 1::integer, true, 1.2, 'text'",
        "SELECT * FROM runs WHERE EXISTS (SELECT 1 FROM nodes) AND true IS TRUE",
        "SELECT min(run_id), max(run_id), count(run_id) FROM runs",
        "SELECT abs(-1), round(1.25,1), lower('UPPER'), length('text')",
        "SELECT * FROM runs WHERE status LIKE 'comp%'",
        'SELECT "r"."run_id" AS "résultat" FROM "public"."runs" AS "r"',
        "SELECT '; /* ordinary string */' AS value -- ordinary comment\n;",
    ],
)
def test_supported_relational_queries(sql: str) -> None:
    admitted = admit_postgres_sql(sql, None, TABLES)
    assert admitted.sql == sql
    assert admitted.relations <= TABLES


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_catalog.pg_advisory_xact_lock(1)",
        "SELECT set_config('statement_timeout','0',false)",
        "SELECT pg_notify('channel','value')",
        "SELECT query_to_xml('SELECT 1',true,false,'')",
        "SELECT random()",
        "SELECT TRIM(BOTH FROM 'text')",
        "SELECT clock_timestamp()",
        "SELECT pg_sleep(1)",
        "SELECT public.lower('x')",
        "SELECT date_trunc('day',now())",
        "SELECT * FROM pg_catalog.pg_settings",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM foreign_rows",
        "SELECT * FROM runs FOR UPDATE",
        "SELECT 1 INTO new_table",
        "WITH x AS (DELETE FROM runs RETURNING *) SELECT * FROM x",
        "WITH RECURSIVE x AS (SELECT 1 UNION ALL SELECT 1 FROM x) SELECT * FROM x",
        "SELECT row_number() OVER ()",
        "SELECT * FROM generate_series(1,3)",
        "SELECT 1 UNION SELECT 2",
        "SELECT 1; SELECT 2",
        "DELETE FROM runs",
        "SELECT 'x'::regclass",
        "SELECT 'x'::xml",
        "SELECT 1::public.custom_type",
        "SELECT 'x' COLLATE public.custom_collation",
        "SELECT 1 OPERATOR(public.+) 2",
        "SELECT ARRAY[1,2]",
        "SELECT ROW(1,2)",
        "SELECT r FROM runs r",
        "SELECT count(r) FROM runs r",
        "SELECT (run_id).field FROM runs",
        "SELECT 1::numeric(10,2)",
        "SELECT sum(1 ORDER BY 1)",
        "SELECT $1",
        "SELECT count()",
        "SELECT lower(*)",
    ],
)
def test_unsupported_queries_refuse(sql: str) -> None:
    with pytest.raises(SQLPolicyError):
        admit_postgres_sql(sql, None, TABLES)


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        5,
        1.5,
        Decimal("1.50"),
        "ordinary text",
        b"bytes",
        date(2026, 1, 1),
        datetime(2026, 1, 1, tzinfo=UTC),
        time(12, 30),
        timedelta(seconds=5),
    ],
)
def test_native_parameters_are_not_json_coerced(value: object) -> None:
    params = {"value": value}
    admitted = admit_postgres_sql("SELECT :value AS value", params, TABLES)
    assert params["value"] is value
    assert admitted.parameter_names == ("value",)
    assert admitted.positional_sql == "SELECT $1 AS value"


@pytest.mark.parametrize("value", [object(), [1], {"value": 1}, float("nan"), float("inf"), Decimal("NaN")])
def test_unsupported_native_parameters_refuse(value: object) -> None:
    with pytest.raises(SQLPolicyError):
        admit_postgres_sql("SELECT :value", {"value": value}, TABLES)


@pytest.mark.parametrize("params", [None, {}, {"different": 1}, {"value": 1, "extra": 2}])
def test_bind_names_must_match(params: dict[str, object] | None) -> None:
    with pytest.raises(SQLPolicyError, match="binds"):
        admit_postgres_sql("SELECT :value", params, TABLES)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ':value'",
        "SELECT :value, ':value'",
        "SELECT $$ :value $$",
        "SELECT :value -- :value\n",
        "SELECT 1 /* :value */",
    ],
)
def test_bind_inside_literal_or_comment_is_not_an_executable_parameter(sql: str) -> None:
    with pytest.raises(SQLPolicyError, match="bind structure"):
        admit_postgres_sql(sql, {"value": "'||pg_catalog.set_config('application_name','effect',false)||'"}, TABLES)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT '100%', :value",
        'SELECT :value AS "résultat"',
        'SELECT run_id AS "résultat", :value FROM runs',
        "SELECT '\\:value', :value",
        "SELECT $tag$ordinary literal$tag$, :value",
        "SELECT :value, :value",
    ],
)
def test_bind_bridge_preserves_literal_bytes_and_unicode_locations(sql: str) -> None:
    admitted = admit_postgres_sql(sql, {"value": "ordinary % data"}, TABLES)
    assert admitted.sql == sql
    assert admitted.parameter_names == ("value",)


def test_authored_positional_placeholder_cannot_alias_named_bind() -> None:
    with pytest.raises(SQLPolicyError, match="bind structure"):
        admit_postgres_sql("SELECT :value, $1", {"value": 1}, TABLES)


@pytest.mark.parametrize("dialect_class", [psycopg.dialect, psycopg2.dialect])
def test_bind_compilation_preserves_structure_for_both_drivers(dialect_class: type) -> None:
    sql = "SELECT CAST(:value AS text), :value, :other AS other"
    admitted = admit_postgres_sql(sql, {"value": "'; SELECT pg_sleep(1); --", "other": 3}, TABLES)
    compiled = text(sql).compile(dialect=dialect_class())
    assert str(compiled) == "SELECT CAST(%(value)s AS text), %(value)s, %(other)s AS other"
    assert admitted.positional_sql == "SELECT CAST($1 AS text), $1, $2 AS other"
    assert admitted.parameter_names == ("value", "other")


def test_text_budget_refuses_before_parser_or_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_compile(*args: object, **kwargs: object) -> None:
        pytest.fail("oversized input reached compilation")

    monkeypatch.setattr(sql_policy, "text", forbidden_compile)
    with pytest.raises(SQLPolicyError, match="text budget"):
        admit_postgres_sql("SELECT '" + "x" * sql_policy.SQL_BYTES + "'", None, TABLES)


@pytest.mark.parametrize("sql", ["", "SELECT '\x00'", "SELECT '\ud800'"])
def test_invalid_text_refuses(sql: str) -> None:
    with pytest.raises(SQLPolicyError):
        admit_postgres_sql(sql, None, TABLES)


@pytest.mark.parametrize(
    "expression",
    [
        "length(btrim(permit_id)) > 0",
        "length(TRIM(BOTH FROM permit_id)) > 0",
        "hash ~ '^[0-9a-f]{64}$'",
        "status = ANY(ARRAY['ready'::text,'done'::text])",
        "status = ANY((ARRAY['ready', 'done'])::text[])",
        "node_id IS NULL",
        "completed = 1",
    ],
)
def test_canonical_stored_expression_families(expression: str) -> None:
    admit_stored_expression(expression, frozenset({"permit_id", "hash", "status", "node_id", "completed"}))


@pytest.mark.parametrize(
    "expression",
    [
        "set_config('statement_timeout','0',false) IS NOT NULL",
        "pg_advisory_lock(1) IS NULL",
        "custom_check(value)",
        "EXISTS(SELECT 1 FROM runs)",
        "value = ANY(ARRAY[ARRAY['ready']]::text[])",
        "value = ANY(ARRAY[lower('ready')])",
        "value = ANY(ARRAY[1 + 1])",
        "value = ANY(ARRAY['ready'] || ARRAY['done'])",
        "ARRAY['ready'] = ARRAY['done']",
        "length(ARRAY['ready']::text)",
    ],
)
def test_stored_expression_does_not_admit_effects(expression: str) -> None:
    with pytest.raises(SQLPolicyError):
        admit_stored_expression(expression, frozenset({"value"}))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT r.row_to_json FROM runs r",
        "SELECT row_to_json FROM runs",
        "SELECT r.ctid FROM runs r",
        "SELECT tableoid, xmin FROM runs",
        "SELECT absent.run_id FROM runs r",
        "WITH x AS (SELECT 1 AS row_to_json) SELECT r.row_to_json FROM runs r, x",
        "SELECT q.row_to_json FROM (SELECT run_id FROM runs) q",
        "SELECT run_id FROM runs r, nodes n",
        "SELECT run_id AS s FROM runs r WHERE r.run_id = s",
    ],
)
def test_column_names_cannot_invoke_functions_or_escape_scalar_sources(sql: str) -> None:
    with pytest.raises(SQLPolicyError):
        admit_postgres_sql(sql, None, TABLES)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT r.run_id FROM runs r",
        "WITH x AS (SELECT run_id AS row_to_json FROM runs) SELECT x.row_to_json FROM x",
        "WITH x(value) AS (SELECT 1) SELECT value FROM x",
        "SELECT q.value FROM (SELECT 1 AS value) q",
        "SELECT r.run_id AS result FROM runs r ORDER BY result",
        "SELECT status AS state, count(*) FROM runs GROUP BY state ORDER BY state",
        "SELECT r.run_id FROM runs r WHERE EXISTS(SELECT 1 FROM nodes n WHERE n.run_id = r.run_id)",
        "SELECT r.* FROM runs r JOIN nodes n USING(run_id)",
        "SELECT q.run_id FROM (SELECT r.* FROM runs r) q",
    ],
)
def test_columns_and_derived_scalar_aliases_remain_supported(sql: str) -> None:
    admit_postgres_sql(sql, None, TABLES)


def test_aware_time_parameters_are_refused_before_adaptation() -> None:
    with pytest.raises(SQLPolicyError, match="time-with-timezone"):
        admit_postgres_sql("SELECT :value", {"value": time(12, tzinfo=UTC)}, TABLES)
