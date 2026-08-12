"""Opt-in live AWS PostgreSQL 16 lifecycle proof for Task 10.

This module never relabels a local Testcontainer as AWS. The test is skipped
unless an operator supplies both a live database URL and the exact deployment
provenance assertion.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse
from urllib.request import urlopen
from uuid import uuid4

import pytest

from elspeth.contracts import RunStatus
from elspeth.contracts.enums import TerminalPath
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import RunSourceLifecycleState
from tests.e2e.recovery.test_run_lifecycle_process_matrix import (
    _LEADER_WORKER_ID,
    _build_open_effect_run,
    _finalize_in_child,
)

if TYPE_CHECKING:
    from scripts.state_engine_profile_reporter import RuntimeProfileReporter

_URL_ENV = "ELSPETH_STATE_ENGINE_AWS_POSTGRES_URL"
_PROVENANCE_ENV = "ELSPETH_STATE_ENGINE_AWS_DEPLOYMENT_PROVENANCE"
_EXPECTED_PROVENANCE = "aws-single-leader-landscape"


def _assert_aws_ecs_rds_provenance(database_url: str) -> None:
    """Require live ECS task metadata and an AWS RDS PostgreSQL endpoint."""
    metadata_base = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if metadata_base is None:
        pytest.fail("live AWS lifecycle proof must run inside ECS with task metadata v4")
    with urlopen(f"{metadata_base}/task", timeout=5) as response:
        metadata = json.loads(response.read())
    assert isinstance(metadata, dict)
    task_arn = metadata["TaskARN"]
    cluster = metadata["Cluster"]
    assert isinstance(task_arn, str) and task_arn.startswith("arn:") and ":ecs:" in task_arn and ":task/" in task_arn
    assert isinstance(cluster, str) and cluster

    database_host = urlparse(database_url).hostname
    assert database_host is not None
    assert database_host.endswith((".rds.amazonaws.com", ".rds.amazonaws.com.cn"))


@pytest.mark.live_aws
@pytest.mark.live_provider
def test_live_aws_postgresql16_non_resumable_finalization(
    request: pytest.FixtureRequest,
) -> None:
    database_url = os.environ.get(_URL_ENV)
    provenance = os.environ.get(_PROVENANCE_ENV)
    if database_url is None or provenance is None:
        pytest.fail(f"live AWS lifecycle proof requires {_URL_ENV} and {_PROVENANCE_ENV}", pytrace=False)
    assert provenance == _EXPECTED_PROVENANCE
    _assert_aws_ecs_rds_provenance(database_url)

    with (
        LandscapeDB.from_url(database_url, create_tables=False) as db,
        db.read_only_connection() as connection,
    ):
        version = connection.exec_driver_sql("SHOW server_version").scalar_one()
        assert isinstance(version, str) and version.startswith("16")
        if request.config.pluginmanager.hasplugin("scripts.state_engine_profile_reporter"):
            reporter = cast("RuntimeProfileReporter", request.getfixturevalue("state_engine_profile"))
            reporter.observe_postgresql(connection, deployment=_EXPECTED_PROVENANCE)

    run_id = f"run-task10-live-{uuid4().hex}"
    token_id, operation_id = _build_open_effect_run(
        database_url,
        run_id=run_id,
        source_state=RunSourceLifecycleState.LOADING,
        with_checkpoint=True,
    )

    with LandscapeDB.from_url(database_url, create_tables=False) as db:
        _finalize_in_child(db, run_id, RunStatus.FAILED.value, _LEADER_WORKER_ID, 1)
        factory = RecorderFactory(db)
        run = factory.run_lifecycle.get_run(run_id)
        operation = factory.execution.get_operation(operation_id)
        outcome = factory.data_flow.get_token_outcome(token_id)

        assert run is not None and run.status is RunStatus.FAILED
        assert operation is not None and operation.status == "failed"
        assert outcome is not None and outcome.path is TerminalPath.ABANDONED
