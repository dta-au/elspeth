"""PostgreSQL proof that the run path's collection-sized statements run in bounded chunks (elspeth-5887 X2).

``tests/integration/pipeline/test_landscape_bind_budget.py`` proves on SQLite,
under a lowered variable ceiling, that no run-path statement binds a parameter
per row. PostgreSQL's ceiling (65,535) cannot be lowered, so these runs lower
the shared budget instead: every chunked read (the verdict and result lock
reads taken ``FOR UPDATE`` chunk by chunk in ascending token order, the sink
member read, the child read-backs) runs in several chunks, and every
executemany's rowcount is psycopg's sum over its rows, which the exact-count
checks rely on. The same cases, the same recorded result.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.test_landscape_bind_budget import bind_budget_cases, run_bind_budget_case

pytestmark = pytest.mark.testcontainer

# Budget 30: 70 rows span three chunks of every per-row read.
_BUDGET = 30
_ROWS = 70


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        yield url


@pytest.mark.timeout(300)
@pytest.mark.parametrize("case", sorted(bind_budget_cases(_ROWS)))
def test_run_path_statements_run_in_budget_chunks_on_postgres(
    tmp_path: Path, postgres_url: str, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from elspeth.core.landscape import bind_budget

    monkeypatch.setattr(bind_budget, "BIND_BUDGET_PER_STATEMENT", _BUDGET)
    run_bind_budget_case(tmp_path, bind_budget_cases(_ROWS)[case], rows=_ROWS, landscape_url=postgres_url)
