"""Web run recovery observes a leader as live only while its seat is live.

``observe_run`` feeds ``RunRecoveryCoordinator``: a live leader makes recovery
leave the run to its owner. A lapsed seat's incumbent is not evidence that the
owner is alive, so it must observe the same as a vacant seat.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.contracts import RunStatus
from elspeth.contracts.hashing import CANONICAL_VERSION
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.web.execution.recovery import observe_run
from tests.fixtures.landscape import expire_leader_seat, leader_coordination_token


def _seed_run(landscape_url: str, run_id: str, *, status: RunStatus, seat: str) -> None:
    with LandscapeDB.from_url(landscape_url, create_tables=True) as db:
        repositories = RecorderFactory(db)
        repositories.run_lifecycle.begin_run(
            config={},
            canonical_version=CANONICAL_VERSION,
            run_id=run_id,
            openrouter_catalog_sha256="0" * 64,
            openrouter_catalog_source="bundled",
        )
        token = leader_coordination_token(repositories, run_id)
        if status is not RunStatus.RUNNING:
            repositories.run_lifecycle.complete_run(status, coordination_token=token)
        if seat == "lapsed":
            expire_leader_seat(db, run_id)
        elif seat == "released":
            repositories.run_coordination.release_seat(token=token)


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.FAILED], ids=["running", "failed"])
@pytest.mark.parametrize(("seat", "expected_live"), [("live", True), ("lapsed", False), ("released", False)])
def test_observe_run_reports_a_live_leader_only_for_a_live_seat(tmp_path: Path, status: RunStatus, seat: str, expected_live: bool) -> None:
    landscape_url = f"sqlite:///{tmp_path / 'audit.db'}"
    run_id = f"run-{status.value}-{seat}"
    _seed_run(landscape_url, run_id, status=status, seat=seat)

    observation = observe_run(landscape_url, run_id, create_tables=False)

    assert observation.status is status
    assert observation.live_leader is expected_live
