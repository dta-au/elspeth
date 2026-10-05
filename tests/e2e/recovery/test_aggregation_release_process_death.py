"""Real process death around a multi-continuation aggregation release into a scope opener.

The end-of-source flush of ``eof_buffer`` releases two continuations, one per
document, into a json_explode scope opener whose collector closes each
document's pages. The orchestrator advances every continuation of the release
in ONE drain (``RowProcessor.drain_released_continuations``). A child process
runs the real build and run path and is SIGKILLed at one of three seams:

- ``after_flush_receipt``: the flush's receipt is committed and nothing is
  released yet;
- ``first_continuation_leased``: the release committed both continuations READY
  and the first is claimed, but its opener has not run;
- ``second_continuation_leased``: the first continuation has been driven as far
  as the drain takes it before the second is claimed. In passthrough mode that
  means its document's group is flushed and the output is sink-bound. In
  transform mode both continuations share an ingest sequence, so the first
  document has only been expanded.

A fresh process then resumes the run through the production recovery path. The
run must end exactly as an uncrashed control run does: the same terminal
outcomes, each document's group written to the sink exactly once, every token
terminal and no journal row left behind.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select, update

from elspeth.contracts import RunStatus
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.database_clock import read_landscape_transaction_time
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import (
    run_workers_table,
    runs_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
from elspeth.engine.executors.aggregation import AggregationExecutor
from tests.e2e.recovery.harness import spawn_database_process_at_seam, spawn_database_process_with_pause
from tests.e2e.recovery.test_sink_effect_process_death_matrix import (
    _PROCESS_TIMEOUT_SECONDS,
    _wait_until_run_is_resumable,
)
from tests.fixtures.landscape import expire_leader_seat, leader_coordination_token
from tests.integration.pipeline.test_aggregation_release_continuations import _DOCUMENTS, _GROUP_OUTPUT, _scope_pipeline
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline, terminal_counts

if TYPE_CHECKING:
    from collections.abc import Callable

_SEAMS = ("after_flush_receipt", "first_continuation_leased", "second_continuation_leased")
_OPENER_NODE_PREFIX = "transform_explode_"


def _run_to_seam(db: LandscapeDB, pause: Callable[[], None], seam: str, output_mode: str, work_dir: str) -> None:
    """Child: run the pipeline and pause (to be SIGKILLed) at ``seam``."""
    env = build_pipeline(Path(work_dir), _scope_pipeline(output_mode), _DOCUMENTS["two"], db=db)
    # This process is SIGKILLed at the seam, so the patch is never undone.
    patch = pytest.MonkeyPatch()
    if seam == "after_flush_receipt":
        real_execute_flush = AggregationExecutor.execute_flush

        def pause_after_receipt(self: AggregationExecutor, *args: Any, **kwargs: Any) -> Any:
            real_execute_flush(self, *args, **kwargs)
            pause()

        patch.setattr(AggregationExecutor, "execute_flush", pause_after_receipt)
    else:
        pause_on_claim = {"first_continuation_leased": 1, "second_continuation_leased": 2}[seam]
        real_claim_ready = TokenSchedulerRepository.claim_ready
        opener_claims: list[str] = []

        def claim_then_pause(self: TokenSchedulerRepository, **kwargs: Any) -> Any:
            claimed = real_claim_ready(self, **kwargs)
            if claimed is not None and claimed.node_id is not None and claimed.node_id.startswith(_OPENER_NODE_PREFIX):
                opener_claims.append(claimed.token_id)
                if len(opener_claims) == pause_on_claim:
                    pause()
            return claimed

        patch.setattr(TokenSchedulerRepository, "claim_ready", claim_then_pause)
    run_pipeline(env)


def _resume_in_fresh_process(db: LandscapeDB, output_mode: str, work_dir: str) -> None:
    """Fresh process: resume through RecoveryManager's admission and the production resume path."""
    env = build_pipeline(Path(work_dir), _scope_pipeline(output_mode), _DOCUMENTS["two"], db=db)
    result = resume_pipeline(env)
    if result.status is not RunStatus.COMPLETED:
        raise AssertionError(f"resumed run ended {result.status!r}")


def _single_run_id(db: LandscapeDB) -> str:
    with db.connection() as conn:
        return str(conn.execute(select(runs_table.c.run_id)).scalar_one())


def _mark_killed_run_failed(database_url: str, run_id: str) -> None:
    """The external supervisor's classification of a SIGKILLed leader (the depth-5 resume pattern).

    The production lifecycle writer marks the run FAILED, and the dead leader's
    seat, its worker heartbeat and the scheduler lease it held are expired, so
    the resume takeover can seize the run and reap the killed claim.
    """
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
        RecorderFactory(killed_db).run_lifecycle.update_run_status(
            RunStatus.FAILED, coordination_token=leader_coordination_token(RecorderFactory(killed_db), run_id)
        )
        with killed_db.write_connection() as conn:
            expired_at = read_landscape_transaction_time(conn) - timedelta(seconds=1)
            conn.execute(update(run_workers_table).where(run_workers_table.c.run_id == run_id).values(heartbeat_expires_at=expired_at))
            conn.execute(
                update(token_work_items_table)
                .where(token_work_items_table.c.run_id == run_id)
                .where(token_work_items_table.c.status == TokenWorkStatus.LEASED.value)
                .values(lease_expires_at=expired_at)
            )
        expire_leader_seat(killed_db, run_id)


def _journal_statuses(db: LandscapeDB) -> dict[str, int]:
    with db.connection() as conn:
        return {
            str(status): int(count)
            for status, count in conn.execute(
                select(token_work_items_table.c.status, func.count()).group_by(token_work_items_table.c.status)
            ).all()
        }


def _tokens_without_a_terminal_outcome(db: LandscapeDB) -> int:
    with db.connection() as conn:
        terminal = select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.completed == 1)
        return int(
            conn.execute(select(func.count()).select_from(tokens_table).where(tokens_table.c.token_id.not_in(terminal))).scalar_one()
        )


def _output_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return sorted((json.loads(line) for line in path.read_text().splitlines()), key=json.dumps)


@pytest.mark.parametrize("seam", _SEAMS)
@pytest.mark.parametrize("output_mode", ["passthrough", "transform"])
def test_a_killed_release_into_a_scope_opener_resumes_to_the_uncrashed_outcome(tmp_path: Path, output_mode: str, seam: str) -> None:
    control_env = build_pipeline(tmp_path / "control", _scope_pipeline(output_mode), _DOCUMENTS["two"])
    assert run_pipeline(control_env).status is RunStatus.COMPLETED
    expected_rows = sorted((_GROUP_OUTPUT[doc["id"]] for doc in _DOCUMENTS["two"]), key=json.dumps)
    assert _output_rows(control_env["output_path"]) == expected_rows

    work_dir = tmp_path / "killed"
    work_dir.mkdir()
    database_url = f"sqlite:///{work_dir / 'audit.db'}"
    with LandscapeDB(database_url):
        pass

    with spawn_database_process_with_pause(
        database_url=database_url,
        seam=seam,
        action=_run_to_seam,
        action_args=(seam, output_mode, str(work_dir)),
    ) as child:
        ready = child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert ready.pid != os.getpid()
        child.kill()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).was_killed

    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
        run_id = _single_run_id(killed_db)
        killed_journal = _journal_statuses(killed_db)
    # The kill left the release unfinished: nothing is sink-written yet.
    assert _output_rows(work_dir / "out.jsonl") == []
    if seam == "after_flush_receipt":
        assert killed_journal == {TokenWorkStatus.BLOCKED.value: 2}
    else:
        assert killed_journal[TokenWorkStatus.LEASED.value] == 1

    _mark_killed_run_failed(database_url, run_id)
    _wait_until_run_is_resumable(database_url, run_id)
    with spawn_database_process_at_seam(
        database_url=database_url,
        seam="fresh-process-recovery-completed",
        action=_resume_in_fresh_process,
        action_args=(output_mode, str(work_dir)),
    ) as recovery:
        recovery.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        recovery.release()
        assert recovery.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0

    with LandscapeDB.from_url(database_url, create_tables=False) as recovered:
        assert _output_rows(work_dir / "out.jsonl") == expected_rows
        assert terminal_counts(recovered) == terminal_counts(control_env["db"])
        assert _tokens_without_a_terminal_outcome(recovered) == 0
        assert set(_journal_statuses(recovered)) == {TokenWorkStatus.TERMINAL.value}
