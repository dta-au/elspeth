"""PostgreSQL proof: a source-quarantined row survives a real crash and resume on a PostgreSQL Landscape.

The crash-then-resume matrix of
``tests/integration/pipeline/test_quarantine_resume_crash_matrix.py`` runs
here unchanged: every source plugin x quarantine error kind, crashed at W1
(another sink fails first), W2 (the quarantine write reserved, unpublished),
W3 (published, not finalized; raise and process death) and W4 (finalized,
handoff open), then resumed. The fenced ingest's one transaction, the fifth
pending-sink bundle arm, the coverage check and the QR-4 completion arm all
run against PostgreSQL's concurrency and CHECK semantics.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.quarantine_resume_matrix import (
    KINDS,
    scenario_crash_then_resume,
    scenario_mid_ingest_death_is_refused_then_abandoned,
)

from elspeth.core.landscape.database import LandscapeDB

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        # The schema owner initializes the Landscape; the CLI runtime is DML-only.
        LandscapeDB.from_url(url).close()
        yield url


@pytest.mark.parametrize("window_name", ["W1", "W2", "W3"])
@pytest.mark.parametrize("kind_name", sorted(KINDS))
def test_every_kind_resumes_to_one_quarantine_publication_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str, kind_name: str, window_name: str
) -> None:
    scenario_crash_then_resume(
        tmp_path, monkeypatch, kind_name=kind_name, window_name=window_name, process_death=False, db_url=postgres_url
    )


@pytest.mark.parametrize("kind_name", sorted(KINDS))
def test_every_kind_resumes_after_process_death_past_publication_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str, kind_name: str
) -> None:
    scenario_crash_then_resume(tmp_path, monkeypatch, kind_name=kind_name, window_name="W3", process_death=True, db_url=postgres_url)


@pytest.mark.parametrize("kind_name", ["json_type", "json_drift"])
def test_finalized_effect_with_open_handoff_is_terminalized_without_republication_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str, kind_name: str
) -> None:
    scenario_crash_then_resume(tmp_path, monkeypatch, kind_name=kind_name, window_name="W4", process_death=False, db_url=postgres_url)


@pytest.mark.parametrize("process_death", [False, True], ids=["raise", "process_death"])
@pytest.mark.parametrize("kind_name", ["json_type", "json_drift"])
def test_mid_ingest_death_is_refused_then_abandoned_on_postgres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, postgres_url: str, kind_name: str, process_death: bool
) -> None:
    scenario_mid_ingest_death_is_refused_then_abandoned(
        tmp_path, monkeypatch, kind_name=kind_name, process_death=process_death, db_url=postgres_url
    )
