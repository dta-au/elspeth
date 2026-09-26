"""PostgreSQL proof: resume's requeue of undecided FAILED work and mark_failed's payload retention.

Lane-owner decision on E1's needs_ruling (RULINGS 2026-09-26, option A2). The
scenarios in ``tests/integration/engine/test_resume_requeue_failed_work.py``
run here unchanged against a real PostgreSQL Landscape: the retention rule is
a correlated ``EXISTS`` inside ``mark_failed``'s UPDATE, the requeue is a
leader-fenced ``SELECT ... FOR UPDATE`` + rotating ``UPDATE ... RETURNING``
whose predicate is another correlated ``NOT EXISTS``, and the new
``resume_requeue_failed`` event must pass the ``event_type`` CHECK.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.engine.test_resume_requeue_failed_work import (
    scenario_decided_failed_item_is_not_requeued,
    scenario_token_holding_only_a_buffered_outcome_is_undecided,
    scenario_undecided_failed_item_is_requeued_and_redriven,
    scenario_undecided_item_named_by_a_group_loss_is_refused,
    scenario_undecided_item_with_purged_payload_is_refused,
)

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


def test_undecided_failed_item_is_requeued_and_redriven_on_postgres(tmp_path: Path, postgres_url: str) -> None:
    scenario_undecided_failed_item_is_requeued_and_redriven(tmp_path, db_url=postgres_url)


def test_decided_failed_item_is_not_requeued_on_postgres(tmp_path: Path, postgres_url: str) -> None:
    scenario_decided_failed_item_is_not_requeued(tmp_path, db_url=postgres_url)


def test_token_holding_only_a_buffered_outcome_is_undecided_on_postgres(tmp_path: Path, postgres_url: str) -> None:
    scenario_token_holding_only_a_buffered_outcome_is_undecided(tmp_path, db_url=postgres_url)


def test_undecided_item_with_purged_payload_is_refused_on_postgres(tmp_path: Path, postgres_url: str) -> None:
    scenario_undecided_item_with_purged_payload_is_refused(tmp_path, db_url=postgres_url)


def test_undecided_item_named_by_a_group_loss_is_refused_on_postgres(tmp_path: Path, postgres_url: str) -> None:
    scenario_undecided_item_named_by_a_group_loss_is_refused(tmp_path, db_url=postgres_url)


def test_a_store_made_before_the_requeue_event_fold_is_refused_on_postgres() -> None:
    """The epoch-46 fold is only sound if PostgreSQL also refuses the narrower pre-fold CHECK at open."""
    from sqlalchemy import create_engine, text

    from elspeth.contracts.scheduler import SchedulerEventType
    from elspeth.core.landscape.database import LandscapeDB, SchemaCompatibilityError

    pre_fold_values = ", ".join(
        f"'{member.value}'" for member in SchedulerEventType if member is not SchedulerEventType.RESUME_REQUEUE_FAILED
    )
    with postgres_test_target(driver="psycopg") as url:
        LandscapeDB.from_url(url).close()
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE scheduler_events DROP CONSTRAINT ck_scheduler_events_event_type"))
            conn.execute(
                text(
                    f"ALTER TABLE scheduler_events ADD CONSTRAINT ck_scheduler_events_event_type CHECK (event_type IN ({pre_fold_values}))"
                )
            )
        engine.dispose()

        with pytest.raises(SchemaCompatibilityError) as exc_info:
            LandscapeDB.from_url(url)

    assert "scheduler_events.ck_scheduler_events_event_type CHECK constraint SQL mismatch" in str(exc_info.value)
