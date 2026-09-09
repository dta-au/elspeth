"""Execute the Terraform database bootstrap against controlled DBAPI rows."""

from __future__ import annotations

import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

from elspeth.web import aws_rds_trust

REPO_ROOT = Path(__file__).resolve().parents[3]
BOOTSTRAP_TF = REPO_ROOT / "deploy" / "aws-ecs" / "terraform" / "modules" / "scenario" / "database_bootstrap.tf"


def _bootstrap_script() -> str:
    source = BOOTSTRAP_TF.read_text(encoding="utf-8")
    start = source.index("database_bootstrap_script = <<-PY\n") + len("database_bootstrap_script = <<-PY\n")
    end = source.index("\n  PY", start)
    return textwrap.dedent(source[start:end])


class _SQL:
    def __init__(self, value: str) -> None:
        self.value = value

    def format(self, *_values: object) -> _SQL:
        return self

    def __str__(self) -> str:
        return self.value


@dataclass
class _Result:
    row: object

    def fetchone(self) -> object:
        return self.row


@dataclass
class _Connection:
    state: _DatabaseState
    admin: bool

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: object, _parameters: object = None) -> _Result:
        rendered = str(query)
        self.state.statements.append(rendered)
        if rendered == "SELECT current_user":
            return _Result(self.state.current_user_row)
        if rendered.startswith("SELECT 1 FROM pg_roles"):
            return _Result(self.state.role_rows.pop(0))
        if rendered.startswith("SELECT 1 FROM pg_database"):
            return _Result(self.state.database_rows.pop(0))
        return _Result(None)


@dataclass
class _DatabaseState:
    current_user_row: object = ("a_bootstrap",)
    role_rows: list[object] = field(default_factory=lambda: [None, (1,)])
    database_rows: list[object] = field(default_factory=lambda: [None, (1,)])
    statements: list[str] = field(default_factory=list)

    def connect(self, url: str, *, autocommit: bool) -> _Connection:
        assert autocommit is True
        return _Connection(self, admin=url.endswith("/postgres?sslmode=verify-full"))


def _execute(monkeypatch: pytest.MonkeyPatch, state: _DatabaseState) -> None:
    psycopg = ModuleType("psycopg")
    psycopg.connect = state.connect  # type: ignore[attr-defined]
    sql = ModuleType("psycopg.sql")
    sql.SQL = _SQL  # type: ignore[attr-defined]
    sql.Identifier = lambda value: value  # type: ignore[attr-defined]
    sql.Literal = lambda value: value  # type: ignore[attr-defined]
    psycopg.sql = sql  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.sql", sql)
    monkeypatch.setattr(aws_rds_trust, "verify_aws_rds_trust_bundle", lambda: None)
    environment = {
        "ELSPETH_DB_ADMIN_URL": "postgresql://a_bootstrap@db.example/postgres?sslmode=verify-full",
        "ELSPETH_DB_SCHEMA_ROLE": "a_schema",
        "ELSPETH_DB_RUNTIME_ROLE": "a_runtime",
        "ELSPETH_DB_SCHEMA_PASSWORD": "x",
        "ELSPETH_DB_RUNTIME_PASSWORD": "y",
        "ELSPETH_DB_SESSION_DATABASE": "elspeth_session",
        "ELSPETH_DB_LANDSCAPE_DATABASE": "elspeth_landscape",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    exec(compile(_bootstrap_script(), str(BOOTSTRAP_TF), "exec"), {"__name__": "database_bootstrap_test"})


def test_database_bootstrap_accepts_only_exact_dbapi_sentinels(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _DatabaseState()

    _execute(monkeypatch, state)

    assert state.statements.count("SELECT current_user") == 1
    assert sum(statement.startswith("SELECT 1 FROM pg_roles") for statement in state.statements) == 2
    assert sum(statement.startswith("SELECT 1 FROM pg_database") for statement in state.statements) == 2


@pytest.mark.parametrize("row", (None, (), ("a_bootstrap", "extra"), (1,), ["a_bootstrap"]))
def test_database_bootstrap_rejects_malformed_current_user_before_ddl(
    monkeypatch: pytest.MonkeyPatch,
    row: object,
) -> None:
    state = _DatabaseState(current_user_row=row)

    with pytest.raises(SystemExit, match="database_bootstrap_dbapi_row_invalid"):
        _execute(monkeypatch, state)

    assert state.statements == ["SELECT current_user"]


@pytest.mark.parametrize("row", ((), (2,), (1, 1), [1], "1"))
def test_database_bootstrap_rejects_malformed_role_existence_before_role_ddl(
    monkeypatch: pytest.MonkeyPatch,
    row: object,
) -> None:
    state = _DatabaseState(role_rows=[row])

    with pytest.raises(SystemExit, match="database_bootstrap_dbapi_row_invalid"):
        _execute(monkeypatch, state)

    assert state.statements == ["SELECT current_user", "SELECT 1 FROM pg_roles WHERE rolname = %s"]


@pytest.mark.parametrize("row", ((), (2,), (1, 1), [1], "1"))
def test_database_bootstrap_rejects_malformed_database_existence_before_database_ddl(
    monkeypatch: pytest.MonkeyPatch,
    row: object,
) -> None:
    state = _DatabaseState(database_rows=[row])

    with pytest.raises(SystemExit, match="database_bootstrap_dbapi_row_invalid"):
        _execute(monkeypatch, state)

    assert not any(statement.startswith(("CREATE DATABASE", "ALTER DATABASE")) for statement in state.statements)
