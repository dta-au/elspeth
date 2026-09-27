"""PostgreSQL proof that no run-path statement binds a parameter per row (elspeth-5887 X2).

``tests/integration/pipeline/test_landscape_bind_budget.py`` proves this on
SQLite by lowering its variable ceiling so SQLite itself refuses such a
statement. PostgreSQL's ceiling (65,535) cannot be lowered, so these runs count
instead: ``record_statement_binds`` records the most parameters any one
statement execution bound during the run, and no statement may bind as many
as the run has rows. An unchunked read over the rows' tokens binds at least
one parameter per row and fails that assertion.

The runs carry 600 rows, above the 500-id chunks of the reads that predate the
shared budget, with the shared budget lowered to 100, so every read chunked on
it (the verdict and result lock reads taken ``FOR UPDATE`` chunk by chunk in
ascending token order, the sink member read, the child read-backs) runs in
several chunks. The same cases must also reach the same recorded result, which
exercises psycopg's executemany rowcount (the sum over its rows) that the
exact-count checks rely on.

What the count does not see: SQLAlchemy's insertmanyvalues pages, which the
driver sizes by the dialect's own ceiling, so they cannot outgrow the
database; and a statement that binds per row over a collection smaller than
the run's rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.fixtures.landscape import record_statement_binds
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.test_landscape_bind_budget import bind_budget_cases, run_bind_budget_case

pytestmark = pytest.mark.testcontainer

_BUDGET = 100
_ROWS = 600


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


@pytest.mark.timeout(600)
@pytest.mark.parametrize("case", sorted(bind_budget_cases(_ROWS)))
def test_run_path_statements_do_not_bind_per_row_on_postgres(
    tmp_path: Path, postgres_url: str, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from elspeth.core.landscape import bind_budget

    monkeypatch.setattr(bind_budget, "BIND_BUDGET_PER_STATEMENT", _BUDGET)
    with record_statement_binds() as binds:
        run_bind_budget_case(tmp_path, bind_budget_cases(_ROWS)[case], rows=_ROWS, landscape_url=postgres_url)

    assert binds.executions > 0
    assert binds.max_binds < _ROWS, f"{binds.max_binds} binds in one statement: {binds.statement}"
