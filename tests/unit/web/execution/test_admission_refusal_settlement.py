"""Admission refusal keeps its recovery marker until both stores are settled."""

from contextlib import nullcontext
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest

from elspeth.contracts.blobs import BlobFinalizationError, BlobFinalizationResult
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.hashing import CANONICAL_VERSION
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_coordination_repository import WriteLockHeldError
from elspeth.web.blobs.service import BlobServiceImpl
from tests.fixtures.landscape import leader_token_for
from tests.unit.web.execution.test_service import _execute_lease
from tests.unit.web.execution.test_service import _live_execute_lease as _live_execute_lease
from tests.unit.web.execution.test_service import broadcaster as broadcaster
from tests.unit.web.execution.test_service import mock_loop as mock_loop
from tests.unit.web.execution.test_service import mock_session_service as mock_session_service
from tests.unit.web.execution.test_service import mock_settings as mock_settings
from tests.unit.web.execution.test_service import real_loop as real_loop
from tests.unit.web.execution.test_service import service as service


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_running_landscape_is_failed_before_cleanup_ack(request, cleanup_fails):
    from unittest.mock import create_autospec

    execution_service = request.getfixturevalue("service")
    sessions = request.getfixturevalue("mock_session_service")
    loop = request.getfixturevalue("real_loop")
    with LandscapeDB.in_memory() as db:
        repositories = RecorderFactory(db)
        run = repositories.run_lifecycle.begin_run(
            {},
            CANONICAL_VERSION,
            run_id=str(uuid4()),
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        repositories.run_coordination.release_seat(token=leader_token_for(db, run.run_id))
        execution_service._blob_service = create_autospec(BlobServiceImpl, instance=True)
        execution_service._blob_service.finalize_run_output_blobs.return_value = BlobFinalizationResult(
            finalized=(),
            errors=(BlobFinalizationError(uuid4(), "OSError", "test cleanup failure"),) if cleanup_fails else (),
        )
        with (
            patch("elspeth.web.execution.service.open_landscape_db", return_value=nullcontext(db)),
        ):
            if cleanup_fails:
                with pytest.raises(RuntimeError, match="cleanup remains pending"):
                    loop.run_until_complete(execution_service._settle_admission_refusal(UUID(run.run_id), _execute_lease()))
                sessions.session_operation_authority.mutate.assert_not_called()
                # The lease owns its child failure too: explicitly verify that
                # close surfaces it while still releasing lifecycle resources.
                with pytest.raises(RuntimeError, match="cleanup remains pending"):
                    loop.run_until_complete(_execute_lease().close())
                assert _execute_lease().closed
            else:
                loop.run_until_complete(execution_service._settle_admission_refusal(UUID(run.run_id), _execute_lease()))
                sessions.session_operation_authority.mutate.assert_called_once()
        assert repositories.run_lifecycle.get_run(run.run_id).status is RunStatus.FAILED


@pytest.mark.parametrize("initial_status", [RunStatus.RUNNING, RunStatus.FAILED, RunStatus.INTERRUPTED])
def test_competing_resume_is_blocked_until_refusal_settlement_finishes(request, tmp_path, initial_status):
    """A real SQLite writer proves the seat remains held across async cleanup."""
    from unittest.mock import create_autospec

    execution_service = request.getfixturevalue("service")
    sessions = request.getfixturevalue("mock_session_service")
    loop = request.getfixturevalue("real_loop")
    with LandscapeDB.from_url(f"sqlite:///{tmp_path}/audit.db", connect_args={"timeout": 0.1}) as db:
        repositories = RecorderFactory(db)
        run = repositories.run_lifecycle.begin_run(
            {},
            CANONICAL_VERSION,
            run_id=str(uuid4()),
            openrouter_catalog_sha256="a" * 64,
            openrouter_catalog_source="bundled",
        )
        initial_token = leader_token_for(db, run.run_id)
        if initial_status is not RunStatus.RUNNING:
            repositories.run_lifecycle.complete_run(initial_status, coordination_token=initial_token)
        repositories.run_coordination.release_seat(token=initial_token)
        execution_service._blob_service = create_autospec(BlobServiceImpl, instance=True)
        expected_status = RunStatus.FAILED if initial_status is RunStatus.RUNNING else initial_status
        observed = []

        async def competing_resume(*args, **kwargs):
            observed.append(repositories.run_lifecycle.get_run(run.run_id).status)
            with pytest.raises(WriteLockHeldError):
                repositories.run_coordination.acquire_run_leadership(
                    run_id=run.run_id,
                    worker_id="competing-resumer",
                    window_seconds=60,
                    entry_point="resume",
                )
            assert repositories.run_lifecycle.get_run(run.run_id).status is expected_status
            return BlobFinalizationResult(finalized=(), errors=())

        execution_service._blob_service.finalize_run_output_blobs.side_effect = competing_resume
        with patch("elspeth.web.execution.service.open_landscape_db", return_value=nullcontext(db)):
            loop.run_until_complete(execution_service._settle_admission_refusal(UUID(run.run_id), _execute_lease()))
        assert observed == [expected_status]
        sessions.session_operation_authority.mutate.assert_called_once()
        # Positive control: the identical takeover succeeds after the fence
        # closes, so a permanently failing test resumer cannot bless this gate.
        token = repositories.run_coordination.acquire_run_leadership(
            run_id=run.run_id,
            worker_id="after-cleanup",
            window_seconds=60,
            entry_point="resume",
        )
        assert repositories.run_lifecycle.get_run(run.run_id).status is RunStatus.RUNNING
        repositories.run_coordination.release_seat(token=token)
