"""PostgreSQL proof: resume's coverage check and the quarantine handoff on a real PostgreSQL Landscape.

The scenarios in ``tests/integration/pipeline/test_resume_coverage_refusal.py``
run here unchanged: a real ``elspeth run`` crashes at the first sink
reservation, the fenced source-quarantine ingest has already parked the
quarantined row PENDING_SINK beside the valid rows, and resume's coverage check
(a correlated ``NOT EXISTS`` census under the leader fence) must refuse and
record a corrupted store, leave ABANDONED tokens to the ADR-038 belt, evaluate
under the won seat, and let the uncorrupted crash resume to every token
terminal. The ``resume_refused`` event must pass the ``event_type`` CHECK.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.test_resume_coverage_refusal import (
    scenario_abandoned_token_is_refused_before_any_redrive,
    scenario_coverage_is_evaluated_under_the_won_seat,
    scenario_run_with_no_work_left_is_refused_not_finalized,
    scenario_token_without_work_item_or_outcome_is_refused_and_recorded,
    scenario_uncorrupted_crash_resumes_to_every_token_terminal,
)

from elspeth.core.landscape.database import LandscapeDB

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        # The schema owner initializes the Landscape; the CLI runtime is DML-only.
        LandscapeDB.from_url(url).close()
        yield url


def test_token_without_work_item_or_outcome_is_refused_and_recorded_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str
) -> None:
    scenario_token_without_work_item_or_outcome_is_refused_and_recorded(tmp_path, monkeypatch, db_url=postgres_url)


@pytest.mark.parametrize("decided", [False, True], ids=["abandoned", "decided-and-abandoned"])
def test_abandoned_token_is_refused_before_any_redrive_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str, decided: bool
) -> None:
    scenario_abandoned_token_is_refused_before_any_redrive(tmp_path, monkeypatch, db_url=postgres_url, decided=decided)


def test_coverage_is_evaluated_under_the_won_seat_on_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str) -> None:
    scenario_coverage_is_evaluated_under_the_won_seat(tmp_path, monkeypatch, db_url=postgres_url)


def test_uncorrupted_crash_resumes_to_every_token_terminal_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str
) -> None:
    scenario_uncorrupted_crash_resumes_to_every_token_terminal(tmp_path, monkeypatch, db_url=postgres_url)


def test_run_with_no_work_left_is_refused_not_finalized_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str
) -> None:
    scenario_run_with_no_work_left_is_refused_not_finalized(tmp_path, monkeypatch, db_url=postgres_url)
