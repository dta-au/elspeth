"""PostgreSQL proof: a re-claim after a follower's in-claim retry, and the outcomeless-FAILED finalise refusal.

RULINGS 2026-09-26 Q2. The scenarios in
``tests/integration/engine/test_follower_retry_and_reclaim.py`` run here
unchanged against a real PostgreSQL Landscape: the claim attempt base must
clear the same ``UNIQUE(token_id, step_index, attempt)`` /
``UNIQUE(token_id, node_id, attempt)`` constraints, and ``complete_run``'s
outcomeless-FAILED arm is a correlated ``NOT EXISTS`` inside the stamping
UPDATE, whose PostgreSQL form must refuse and admit exactly as SQLite's does.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.engine.test_follower_retry_and_reclaim import (
    scenario_follower_reclaim_after_an_in_claim_retry,
    scenario_leader_reclaims_a_rotated_follower_item,
    scenario_reclaim_after_upstream_retries_and_downstream_loss,
    scenario_run_completes_over_a_routed_failure,
    scenario_run_is_not_completed_over_a_claim_that_died_mid_row,
)

pytestmark = pytest.mark.testcontainer


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


@pytest.mark.parametrize("reclaimer", ["same_follower", "other_follower"])
def test_follower_reclaim_after_an_in_claim_retry_on_postgres(tmp_path: Path, postgres_url: str, reclaimer: str) -> None:
    scenario_follower_reclaim_after_an_in_claim_retry(tmp_path, reclaimer, db_url=postgres_url)


def test_leader_reclaims_a_rotated_follower_item_on_postgres(tmp_path: Path, postgres_url: str) -> None:
    scenario_leader_reclaims_a_rotated_follower_item(tmp_path, db_url=postgres_url)


def test_reclaim_after_upstream_retries_and_downstream_loss_on_postgres(tmp_path: Path, postgres_url: str) -> None:
    scenario_reclaim_after_upstream_retries_and_downstream_loss(tmp_path, db_url=postgres_url)


def test_run_completes_over_a_routed_failure_on_postgres(tmp_path: Path, postgres_url: str) -> None:
    scenario_run_completes_over_a_routed_failure(tmp_path, db_url=postgres_url)


def test_run_is_not_completed_over_a_claim_that_died_mid_row_on_postgres(tmp_path: Path, postgres_url: str) -> None:
    scenario_run_is_not_completed_over_a_claim_that_died_mid_row(tmp_path, db_url=postgres_url)
