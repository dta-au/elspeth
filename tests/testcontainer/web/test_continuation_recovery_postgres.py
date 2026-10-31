"""The real worker reaper/Stop race controls on PostgreSQL."""

import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target, operation_store
from tests.unit.web.sessions.test_continuation_recovery import (
    test_periodic_sweep_recovers_owner_lost_after_startup,
    test_reap_once_recovers_released_v2_owner_before_claim,
    test_reaper_excludes_locally_owned_and_adoption_pending_jobs,
    test_reaper_stop_marker_wins_discovery_to_writer_race,
)

pytestmark = pytest.mark.testcontainer

__all__ = [
    "operation_postgres_target",
    "operation_store",
    "test_periodic_sweep_recovers_owner_lost_after_startup",
    "test_reap_once_recovers_released_v2_owner_before_claim",
    "test_reaper_excludes_locally_owned_and_adoption_pending_jobs",
    "test_reaper_stop_marker_wins_discovery_to_writer_race",
]
