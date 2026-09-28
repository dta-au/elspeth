"""PostgreSQL proof: a process death inside a mint window resumes to the clean image on a PostgreSQL Landscape.

The scenarios of ``tests/integration/pipeline/test_resume_mint_window_crash.py``
run here unchanged: a real run killed right after fork / expand / collect
commits its products, the dead run's seat and leases lapsed through the
database clock, then a real resume that must reconcile the committed products
(never re-mint them) and finish every token exactly once.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.helpers.postgres_target import postgres_test_target
from tests.integration.pipeline.mint_window_crash import WINDOWS, scenario_mint_window_death_resumes_to_the_clean_image

from elspeth.core.landscape.database import LandscapeDB

pytestmark = pytest.mark.testcontainer


@pytest.fixture
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as url:
        # The schema owner initializes the Landscape; the CLI runtime is DML-only.
        LandscapeDB.from_url(url).close()
        yield url


@pytest.mark.parametrize("window_name", sorted(WINDOWS))
def test_mint_window_death_resumes_to_the_clean_image_on_postgres(tmp_path: Path, postgres_url: str, window_name: str) -> None:
    scenario_mint_window_death_resumes_to_the_clean_image(tmp_path, window_name=window_name, db_url=postgres_url)
