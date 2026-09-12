"""Coordination observations survive heartbeat, exception, and CLI projection."""

from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from unittest.mock import create_autospec, patch

import pytest
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.audit import Checkpoint
from elspeth.contracts.checkpoint import ResumeCheck, ResumePoint
from elspeth.contracts.coordination import CoordinationSnapshot, WorkerMembershipLost, WorkerMembershipToken
from elspeth.contracts.errors import RunWorkerEvictedError
from elspeth.core.landscape import LandscapeDB
from elspeth.engine.orchestrator.follower import FollowerProcessor
from elspeth.engine.orchestrator.heartbeat import RunHeartbeatThread
from tests.integration.cli.test_cli import _make_jsonl_settings

_RUN_ID = "run-loss-diagnostic"
_WORKER_ID = f"worker:{_RUN_ID}:abc"
_TOKEN = WorkerMembershipToken(run_id=_RUN_ID, worker_id=_WORKER_ID)


class _HeartbeatRepository:
    def __init__(self, loss: Literal["membership", "seat"]) -> None:
        self.loss = loss

    def worker_heartbeat(
        self, *, member_token: WorkerMembershipToken, window_seconds: float
    ) -> CoordinationSnapshot | WorkerMembershipLost:
        if self.loss == "membership":
            return WorkerMembershipLost(member_token=member_token)
        return CoordinationSnapshot(leader_worker_id="worker:replacement:xyz", leader_epoch=2, seat_live=True, worker_active=True)

    def record_heartbeat_degraded(self, *, member_token: WorkerMembershipToken, failures: int, now: datetime) -> None:
        raise AssertionError("successful observations must not record contention")


def _observed_error(loss: Literal["membership", "seat"] | None) -> RunWorkerEvictedError:
    if loss is None:
        return RunWorkerEvictedError(worker_id=_WORKER_ID, run_id=_RUN_ID)
    heartbeat = RunHeartbeatThread(_HeartbeatRepository(loss), member_token=_TOKEN)
    heartbeat._step_beat()
    with pytest.raises(RunWorkerEvictedError) as raised:
        heartbeat.check_and_raise()
    return raised.value


@pytest.mark.parametrize("command", ["run", "resume", "join"])
@pytest.mark.parametrize("output_format", ["console", "json"])
@pytest.mark.parametrize(
    ("loss", "expected_reason"),
    [
        pytest.param(None, None, id="unobserved"),
        pytest.param(
            "membership",
            "heartbeat refused by the membership fence: our run_workers row is no longer 'active' (no seat state observed)",
            id="membership-refused",
        ),
        pytest.param("seat", "seat taken by 'worker:replacement:xyz' (our worker_id='worker:run-loss-diagnostic:abc')", id="seat-taken"),
    ],
)
def test_command_preserves_observed_loss(
    tmp_path: Path,
    command: str,
    output_format: str,
    loss: Literal["membership", "seat"] | None,
    expected_reason: str | None,
) -> None:
    """Real commands render heartbeat-produced errors at their execution seam.

    Admission/recovery and execution are controlled so each CLI handler can
    be tested independently. The join seat-loss case is synthetic handler
    coverage, not a claim that a follower observes leader deposition.
    """
    import json

    settings_path = _make_jsonl_settings(tmp_path)
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}")
    db.close()
    error = _observed_error(loss)
    args = ["--no-dotenv", command]
    if command != "run":
        args.append(_RUN_ID)
    args.extend(["--settings", str(settings_path), "--format", output_format])
    if command != "join":
        args.append("--execute")

    with ExitStack() as stack:
        if command == "run":
            stack.enter_context(patch("elspeth.cli._execute_pipeline_with_instances", autospec=True, side_effect=error))
        elif command == "resume":
            point = ResumePoint(
                checkpoint=Checkpoint(
                    checkpoint_id="checkpoint-loss",
                    run_id=_RUN_ID,
                    sequence_number=0,
                    created_at=datetime(2026, 9, 12, tzinfo=UTC),
                    upstream_topology_hash="a" * 64,
                ),
                sequence_number=0,
            )
            stack.enter_context(
                patch("elspeth.core.checkpoint.RecoveryManager.can_resume", autospec=True, return_value=ResumeCheck(can_resume=True))
            )
            stack.enter_context(patch("elspeth.core.checkpoint.RecoveryManager.get_resume_point", autospec=True, return_value=point))
            stack.enter_context(patch("elspeth.core.checkpoint.RecoveryManager.get_unprocessed_rows", autospec=True, return_value=[]))
            stack.enter_context(patch("elspeth.core.checkpoint.RecoveryManager.count_blocked_barrier_items", autospec=True, return_value=0))
            stack.enter_context(patch("elspeth.cli._execute_resume_with_instances", autospec=True, side_effect=error))
        else:
            stack.enter_context(patch("elspeth.engine.Orchestrator.join_run", autospec=True, return_value=_TOKEN))
            follower = create_autospec(FollowerProcessor, instance=True)
            follower.run.side_effect = error
            stack.enter_context(
                patch("elspeth.engine.orchestrator.follower.build_follower_processor", autospec=True, return_value=follower)
            )
        result = CliRunner().invoke(app, args)

    assert result.exit_code == 3, (result.output, result.exception)
    assert "Traceback" not in result.stderr
    if output_format == "json":
        payload = json.loads(result.stderr)
        assert payload["event"] == "evicted"
        assert payload["run_id"] == _RUN_ID
        assert payload["worker_id"] == _WORKER_ID
        message = payload["message"]
        if expected_reason is None:
            assert "reason" not in payload
        else:
            assert payload["reason"] == expected_reason
    else:
        message = result.stderr
    assert _WORKER_ID in message
    assert _RUN_ID in message
    assert "re-admit under a fresh identity" in message
    if expected_reason is None:
        assert "Observed:" not in message
    else:
        assert expected_reason in message
        assert "lost coordination" in message
        assert "is no longer an active member" not in message
