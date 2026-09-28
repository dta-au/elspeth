"""A process death between a mint commit and its producer's completion is resumed to the clean image.

Before the coverage check learned the mint window, resume refused these
healthy crashes permanently (``resume_refused``: uncovered_undecided_tokens),
although re-driving the open producer reconciles the committed products; base
(release) resumed them correctly. Each test is a real run killed right after
the named mint verb commits, then a real resume (see ``mint_window_crash``).
The PostgreSQL twin is
``tests/testcontainer/core/test_resume_mint_window_crash_postgres.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.pipeline.mint_window_crash import WINDOWS, scenario_mint_window_death_resumes_to_the_clean_image


@pytest.mark.timeout(300)
@pytest.mark.parametrize("window_name", sorted(WINDOWS))
def test_mint_window_death_resumes_to_the_clean_image(tmp_path: Path, window_name: str) -> None:
    scenario_mint_window_death_resumes_to_the_clean_image(tmp_path, window_name=window_name, db_url=f"sqlite:///{tmp_path / 'audit.db'}")
