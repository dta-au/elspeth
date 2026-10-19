"""Reuse the process-death harness for complete-source duplicate decisions."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import RunStatus
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.scheduler_repository import TokenSchedulerRepository
from elspeth.core.landscape.schema import batch_members_table, tokens_table
from elspeth.engine.executors.aggregation import AggregationExecutor
from elspeth.plugins.transforms.batch_duplicate_annotator import BatchDuplicateAnnotator
from tests.e2e.recovery.harness import spawn_database_process_at_seam, spawn_database_process_with_pause
from tests.e2e.recovery.test_aggregation_release_process_death import (
    _journal_statuses,
    _mark_killed_run_failed,
    _output_rows,
    _single_run_id,
    _tokens_without_a_terminal_outcome,
)
from tests.e2e.recovery.test_sink_effect_process_death_matrix import _PROCESS_TIMEOUT_SECONDS, _wait_until_run_is_resumable
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline, terminal_counts

_PIPELINE = """
aggregations:
  - name: compare
    plugin: batch_duplicate_annotator
    input: buffered
    on_success: annotated
    on_error: discard
    output_mode: passthrough
    options:
      schema: {{mode: observed}}
      rules:
        - name: repeated
          cells: [{{field: first}}, {{field: second}}]
transforms:
  - name: consume
    plugin: passthrough
    input: annotated
    on_success: out
    on_error: discard
    options:
      schema: {{mode: observed}}
"""
_ROWS = [
    {"case_id": "same", "first": "alpha", "second": "beta"},
    {"case_id": "same", "first": "unique", "second": "different"},
    {"case_id": "same", "first": "beta", "second": "alpha"},
]
_CAUSES = '[{"rule":"repeated","field":"first","category":"duplicate"},{"rule":"repeated","field":"second","category":"duplicate"}]'
_EXPECTED = [
    {**row, "duplicates_valid": index == 1, "duplicates_causes": "[]" if index == 1 else _CAUSES}
    for index, row in enumerate(_ROWS)
]


def _run_to_seam(db: LandscapeDB, pause: Callable[[], None], seam: str, work_dir: str) -> None:
    env = build_pipeline(Path(work_dir), _PIPELINE, _ROWS, db=db)
    patch = pytest.MonkeyPatch()
    if seam == "after_flush_receipt":
        original = AggregationExecutor.execute_flush

        def pause_after_receipt(self: AggregationExecutor, *args: Any, **kwargs: Any) -> Any:
            result = original(self, *args, **kwargs)
            pause()
            return result

        patch.setattr(AggregationExecutor, "execute_flush", pause_after_receipt)
    else:
        original_claim = TokenSchedulerRepository.claim_ready
        consumer = env["graph"].get_transform_id_map()[0]
        claims = []

        def pause_on_claim(self: TokenSchedulerRepository, **kwargs: Any) -> Any:
            claimed = original_claim(self, **kwargs)
            if claimed is not None and claimed.node_id == consumer:
                claims.append(claimed.token_id)
                if len(claims) == {"first_continuation_leased": 1, "second_continuation_leased": 2}[seam]:
                    pause()
            return claimed

        patch.setattr(TokenSchedulerRepository, "claim_ready", pause_on_claim)
    run_pipeline(env)


def _resume_without_recomputing(db: LandscapeDB, work_dir: str) -> None:
    env = build_pipeline(Path(work_dir), _PIPELINE, _ROWS, db=db)

    def forbidden_recomputation(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Committed complete-source annotation must not be recomputed")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(BatchDuplicateAnnotator, "process", forbidden_recomputation)
        assert resume_pipeline(env).status is RunStatus.COMPLETED


@pytest.mark.parametrize("seam", ["after_flush_receipt", "first_continuation_leased", "second_continuation_leased"])
def test_committed_duplicate_decisions_survive_process_death_and_partial_release(tmp_path: Path, seam: str) -> None:
    control = build_pipeline(tmp_path / "control", _PIPELINE, _ROWS)
    assert run_pipeline(control).status is RunStatus.COMPLETED
    expected = sorted((dict(sorted(row.items())) for row in _EXPECTED), key=json.dumps)
    assert _output_rows(control["output_path"]) == expected
    work_dir = tmp_path / "killed"
    work_dir.mkdir()
    database_url = f"sqlite:///{work_dir / 'audit.db'}"
    with LandscapeDB(database_url):
        pass
    with spawn_database_process_with_pause(
        database_url=database_url, seam=seam, action=_run_to_seam, action_args=(seam, str(work_dir))
    ) as child:
        child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        child.kill()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).was_killed
    with LandscapeDB.from_url(database_url, create_tables=False) as killed:
        run_id = _single_run_id(killed)
        with killed.connection() as connection:
            original_tokens = set(connection.execute(select(batch_members_table.c.token_id)).scalars())
        assert len(original_tokens) == 3
        if seam == "after_flush_receipt":
            assert _journal_statuses(killed) == {TokenWorkStatus.BLOCKED.value: 3}
            assert _output_rows(work_dir / "out.jsonl") == []
        else:
            assert _journal_statuses(killed)[TokenWorkStatus.LEASED.value] == 1
    _mark_killed_run_failed(database_url, run_id)
    _wait_until_run_is_resumable(database_url, run_id)
    with spawn_database_process_at_seam(
        database_url=database_url, seam="duplicate-release-recovered", action=_resume_without_recomputing, action_args=(str(work_dir),)
    ) as recovery:
        recovery.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        recovery.release()
        assert recovery.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0
    with LandscapeDB.from_url(database_url, create_tables=False) as recovered:
        assert _output_rows(work_dir / "out.jsonl") == expected
        assert terminal_counts(recovered) == terminal_counts(control["db"])
        assert _tokens_without_a_terminal_outcome(recovered) == 0
        assert set(_journal_statuses(recovered)) == {TokenWorkStatus.TERMINAL.value}
        with recovered.connection() as connection:
            assert set(connection.execute(select(tokens_table.c.token_id)).scalars()) == original_tokens
