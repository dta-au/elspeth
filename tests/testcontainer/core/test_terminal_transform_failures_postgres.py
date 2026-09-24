"""PostgreSQL proof that the latest attempt decides a failed token's count (elspeth-5887fb7928).

``deciding_transform_errors`` ranks each token's ``transform_errors`` rows
with a ``row_number()`` window and keeps the latest. The SQLite cases in
``tests/unit/core/landscape/test_terminal_transform_failures.py`` prove the
semantics. The batch pipeline in ``test_aggregation_error_route_postgres.py``
sends the query to PostgreSQL, but every token there has one attempt, so the
ranking never has to choose. These cases give it two attempts per token, and
one failed token beside tokens that completed the same node.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from tests.helpers.postgres_target import postgres_test_target
from tests.unit.core.landscape.test_terminal_transform_failures import (
    _NOTHING_FAILED,
    _completed_state,
    _Counts,
    _counts,
    _error,
    _error_created_order,
    _setup,
    _terminal,
    _token,
)

from elspeth.contracts import NodeStateStatus
from elspeth.contracts.enums import TerminalOutcome, TerminalPath
from elspeth.core.landscape.data_flow import errors as data_flow_errors
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import node_states_table, transform_errors_table

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_db() -> Iterator[LandscapeDB]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        db = LandscapeDB.from_url(postgres_url)
        try:
            assert db.engine.dialect.name == "postgresql"
            yield db
        finally:
            db.close()


def test_the_latest_attempt_decides_the_count_on_postgres(postgres_db: LandscapeDB) -> None:
    """X then Y: counted once, at Y, under Y's category. A delivered retry counts nowhere."""
    setup = _setup("pg-x-then-y", db=postgres_db)
    token = _token(setup, 0)
    _error(setup, token, "xform", reason="api_error", destination="discard")
    _error(setup, token, "yform", reason="validation_failed", destination="discard")
    _terminal(setup, token, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)
    assert _error_created_order(setup, token) == ["xform", "yform"]

    assert _counts(setup) == _Counts(
        discarded={"yform": 1},
        categories=[("yform", "validation_failed", 1)],
        run_summary_transform=1,
        analysis_total=1,
        analysis_by_plugin={"scorer": 1},
    )

    delivered = _setup("pg-delivered-after-retry", db=postgres_db)
    retried = _token(delivered, 0)
    _error(delivered, retried, "xform", reason="api_error", destination="discard")
    _error(delivered, retried, "xform", reason="api_error", destination="discard")
    _terminal(delivered, retried, TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW, sink_name="output")

    assert _counts(delivered) == _NOTHING_FAILED


def test_a_failed_token_counts_beside_tokens_that_completed_the_node_on_postgres(postgres_db: LandscapeDB) -> None:
    """Two tokens COMPLETED X and one failed there: the completed-state exclusion is per token."""
    setup = _setup("pg-mixed-node", db=postgres_db)
    passed_first, failed, passed_last = _token(setup, 0), _token(setup, 1), _token(setup, 2)
    for passed in (passed_first, passed_last):
        _completed_state(setup, passed, "xform")
        _terminal(setup, passed, TerminalOutcome.SUCCESS, TerminalPath.DEFAULT_FLOW, sink_name="output")
    _error(setup, failed, "xform", reason="api_error", destination="discard")
    _terminal(setup, failed, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)
    with setup.db.connection() as conn:
        completed_at_x = (
            conn.execute(
                select(node_states_table.c.token_id)
                .where(node_states_table.c.run_id == setup.run_id)
                .where(node_states_table.c.node_id == "xform")
                .where(node_states_table.c.status == NodeStateStatus.COMPLETED)
            )
            .scalars()
            .all()
        )
    assert sorted(completed_at_x) == sorted([passed_first, passed_last]), "control: two other tokens COMPLETED the failing node"

    assert _counts(setup) == _Counts(
        discarded={"xform": 1},
        categories=[("xform", "api_error", 1)],
        run_summary_transform=1,
        analysis_total=1,
        analysis_by_plugin={"mapper": 1},
    )


def test_a_created_at_tie_counts_the_token_once_on_postgres(postgres_db: LandscapeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two attempts stamped at the same instant: the ``error_id`` tie-break picks one row."""
    setup = _setup("pg-created-at-tie", db=postgres_db)
    token = _token(setup, 0)
    instant = datetime(2026, 9, 23, tzinfo=UTC)
    monkeypatch.setattr(data_flow_errors, "now", lambda: instant)
    _error(setup, token, "xform", reason="api_error", destination="discard")
    _error(setup, token, "xform", reason="validation_failed", destination="discard")
    _terminal(setup, token, TerminalOutcome.FAILURE, TerminalPath.QUARANTINED_AT_SOURCE)
    with setup.db.connection() as conn:
        stamps = conn.execute(select(transform_errors_table.c.created_at).where(transform_errors_table.c.token_id == token)).scalars().all()
    assert len(stamps) == 2 and len(set(stamps)) == 1, f"control: the two attempts must tie on created_at, got {stamps}"

    counts = _counts(setup)

    assert len(counts.categories) == 1, counts.categories
    assert counts.categories[0][0::2] == ("xform", 1)
    assert counts.discarded == {"xform": 1}
    assert counts.run_summary_transform == counts.analysis_total == 1
