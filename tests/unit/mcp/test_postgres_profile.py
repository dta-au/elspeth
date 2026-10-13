"""Connection ownership/control flow; native catalog predicates need PG tests."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import Connection

from elspeth.mcp.postgres_profile import (
    _admit_routine,
    _Routine,
    _trigger_body,
    admit_postgres_profile,
)
from elspeth.mcp.sql_policy import SQLPolicyError


def _connection(reason: str | None, expressions: tuple[str, ...] = ()) -> MagicMock:
    conn = MagicMock(spec=Connection)
    conn.dialect = SimpleNamespace(name="postgresql")
    profile_result = MagicMock(spec=["scalar_one_or_none"])
    profile_result.scalar_one_or_none.return_value = reason
    conn.execute.side_effect = [profile_result, (), tuple((expression, "runs") for expression in expressions)]
    return conn


def test_profile_accepts_owned_connection_and_canonical_expressions() -> None:
    conn = _connection(None, ("status = 'completed'", "run_id IS NOT NULL"))
    admit_postgres_profile(conn, frozenset({"runs"}))
    assert conn.exec_driver_sql.call_args_list[0].args == ("SET LOCAL search_path = pg_catalog, public, pg_temp",)
    assert conn.execute.call_count == 3


def test_profile_refusal_does_not_continue_to_stored_expressions() -> None:
    conn = _connection("temporary_resolution_objects")
    with pytest.raises(SQLPolicyError, match="temporary_resolution_objects"):
        admit_postgres_profile(conn, frozenset({"runs"}))
    assert conn.execute.call_count == 1


def test_profile_does_not_reuse_prior_connection_verdict() -> None:
    conn = _connection(None)
    first = list(conn.execute.side_effect)
    refused = MagicMock(spec=["scalar_one_or_none"])
    refused.scalar_one_or_none.return_value = "custom_execution_objects"
    conn.execute.side_effect = [*first, refused]
    admit_postgres_profile(conn, frozenset({"runs"}))
    with pytest.raises(SQLPolicyError, match="custom_execution_objects"):
        admit_postgres_profile(conn, frozenset({"runs"}))
    assert conn.execute.call_count == 4


def test_stored_check_effect_is_refused_after_catalog_profile() -> None:
    conn = _connection(None, ("set_config('statement_timeout','0',false) IS NOT NULL",))
    with pytest.raises(SQLPolicyError):
        admit_postgres_profile(conn, frozenset({"runs"}))


def test_empty_metadata_refuses_before_connection_use() -> None:
    conn = _connection(None)
    with pytest.raises(SQLPolicyError, match="canonical Landscape"):
        admit_postgres_profile(conn, frozenset())
    conn.execute.assert_not_called()
    conn.exec_driver_sql.assert_not_called()


def _wrapper() -> _Routine:
    return _Routine(
        2003,
        "pg_catalog",
        "textanycat",
        "sql",
        "s",
        False,
        False,
        "text, anynonarray",
        "text",
        "select $1 operator(pg_catalog.||) $2::pg_catalog.text",
        False,
        "f",
        False,
        [],
        0,
    )


def test_exact_bootstrap_scalar_wrapper_is_admitted() -> None:
    _admit_routine(_wrapper())
    _admit_routine(
        replace(
            _wrapper(),
            oid=2004,
            name="anytextcat",
            arguments="anynonarray, text",
            source="select $1::pg_catalog.text operator(pg_catalog.||) $2",
        )
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"oid": 2005},
        {"schema": "public"},
        {"name": "custom"},
        {"language": "plpgsql"},
        {"volatility": "v"},
        {"security_definer": True},
        {"configured": True},
        {"arguments": "text, record"},
        {"result": "trigger"},
        {"source": "select pg_sleep(1)"},
        {"parsed_body": True},
        {"kind": "p"},
        {"variadic": True},
        {"attachment_count": 1},
    ],
)
def test_sql_wrapper_identity_and_body_are_closed(mutation: dict[str, object]) -> None:
    with pytest.raises(SQLPolicyError):
        _admit_routine(replace(_wrapper(), **mutation))


def _trigger() -> _Routine:
    name = "fn_audit_export_chunk_insert_validate"
    return _Routine(
        17000,
        "public",
        name,
        "plpgsql",
        "v",
        False,
        False,
        "",
        "trigger",
        _trigger_body(name),
        False,
        "f",
        False,
        ["trg_audit_export_chunk_insert_validate:audit_export_snapshot_chunks:7"],
        1,
    )


def test_canonical_attached_writer_trigger_is_admitted() -> None:
    _admit_routine(_trigger())


@pytest.mark.parametrize(
    "mutation",
    [
        {"name": "lower", "arguments": "bigint"},
        {"name": "arbitrary_trigger"},
        {"arguments": "text"},
        {"schema": "other"},
        {"source": "BEGIN RETURN NEW; END;"},
        {"language": "internal"},
        {"security_definer": True},
        {"configured": True},
        {"attachments": []},
        {"attachment_count": 2},
    ],
)
def test_trigger_return_type_does_not_exempt_custom_routines(mutation: dict[str, object]) -> None:
    with pytest.raises(SQLPolicyError):
        _admit_routine(replace(_trigger(), **mutation))


def _parsed_wrapper() -> _Routine:
    return replace(
        _wrapper(),
        oid=1708,
        name="round",
        volatility="i",
        arguments="numeric",
        result="numeric",
        source="",
        parsed_body=True,
        parsed_definition=(
            "CREATE OR REPLACE FUNCTION pg_catalog.round(numeric)\n"
            " RETURNS numeric\n LANGUAGE sql\n IMMUTABLE PARALLEL SAFE STRICT COST 1\nRETURN round($1, 0)\n"
        ),
    )


def test_exact_parsed_pg16_wrapper_is_admitted() -> None:
    _admit_routine(_parsed_wrapper())


@pytest.mark.parametrize(
    "mutation",
    [
        {"oid": 1709},
        {"schema": "public"},
        {"name": "lower"},
        {"arguments": "bigint"},
        {"result": "trigger"},
        {"volatility": "v"},
        {"security_definer": True},
        {"configured": True},
        {"variadic": True},
        {"kind": "a"},
        {"source": "select pg_sleep(1)"},
        {"parsed_body": False},
        {"parsed_definition": "RETURN pg_catalog.set_config('application_name','effect',false)"},
    ],
)
def test_parsed_body_cannot_acquire_a_general_sql_exception(mutation: dict[str, object]) -> None:
    with pytest.raises(SQLPolicyError):
        _admit_routine(replace(_parsed_wrapper(), **mutation))
