"""An out-of-claim aggregation release advances all its continuations in ONE drain.

A timeout or end-of-source aggregation flush inserts every continuation READY
in one ``complete_barrier`` transaction. The orchestrator used to drive each
continuation through its own ``process_token`` drain, but a drain claims every
READY row of the run. So the first continuation's drain also advanced its
siblings, and the next per-continuation enqueue replayed a work item that had
already moved on. The scheduler refused the incompatible replay and the run
aborted with ``LandscapeRecordError`` (exit 4). A sibling that opened a scope
went terminal as its expand parent. A sibling that reached a second barrier was
held with the transform's output as its row. Either way the replayed image no
longer matched. A lone continuation, or siblings that were all PENDING_SINK with
an unchanged row, passed by accident.

These tests run the real build and run path. The release feeds a json_explode
scope opener and a batch_stats collector, in both output modes, for one, two and
three documents (the third does not parse as an array and is discarded by the
opener). A second shape has no scope: the release feeds a transform and then a
second end-of-source barrier. The resume variant faults the flush itself, so the
resumed run's end-of-source flush makes the release; it must end as the
uncrashed control does, with each collector group flushed exactly once. Real
process death after the release committed, and between the continuations, is
``tests/e2e/recovery/test_aggregation_release_process_death.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from elspeth.contracts import Determinism, PipelineRow
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import token_outcomes_table, token_work_items_table, tokens_table
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.transforms.batch_replicate import BatchReplicate
from elspeth.plugins.transforms.batch_stats import BatchStats
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline, resume_pipeline, run_pipeline, terminal_counts

_RELEASE_INTO_SCOPE = """
aggregations:
  - name: eof_buffer
    plugin: batch_replicate
    input: buffered
    on_success: rows
    on_error: discard
    trigger: {{count: 100}}
    output_mode: OUTPUT_MODE
    options:
      include_copy_index: false
      schema: {{mode: observed}}
transforms:
  - name: explode
    plugin: json_explode
    input: rows
    on_success: pages
    on_error: discard
    options:
      array_field: items
      output_field: item
      schema: {{mode: observed}}
collectors:
  - name: page_stitcher
    plugin: batch_stats
    input: pages
    on_success: out
    options:
      value_field: item
      schema: {{mode: observed}}
scopes:
  - name: document_pages
    opener: explode
    closer: page_stitcher
    policy: require_all
"""

_RELEASE_INTO_SECOND_BARRIER = """
aggregations:
  - name: eof_buffer
    plugin: batch_replicate
    input: buffered
    on_success: rows
    on_error: discard
    trigger: {{count: 100}}
    output_mode: passthrough
    options:
      include_copy_index: false
      schema: {{mode: observed}}
  - name: second_buffer
    plugin: batch_replicate
    input: tagged
    on_success: out
    on_error: discard
    trigger: {{count: 100}}
    output_mode: passthrough
    options:
      include_copy_index: false
      schema: {{mode: observed}}
transforms:
  - name: tag
    plugin: value_transform
    input: rows
    on_success: tagged
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: tagged
          expression: "row['id'] + 100"
"""

_DOCUMENTS = {
    "one": [{"id": 1, "items": [3, 1, 2]}],
    "two": [{"id": 1, "items": [3, 1, 2]}, {"id": 2, "items": [5, 7]}],
    "three_one_unparseable": [{"id": 1, "items": [3, 1, 2]}, {"id": 2, "items": "not-a-list"}, {"id": 3, "items": [5, 7]}],
}

# The documents whose ``items`` json_explode can expand (document 2 of the third set is not an array).
_PARSEABLE_IDS = {"one": [1], "two": [1, 2], "three_one_unparseable": [1, 3]}

# The stats row each parseable document's group produces.
_GROUP_OUTPUT = {
    1: {"batch_size": 3, "count": 3, "mean": 2, "sum": 6},
    2: {"batch_size": 2, "count": 2, "mean": 6, "sum": 12},
    3: {"batch_size": 2, "count": 2, "mean": 6, "sum": 12},
}

# Captured once, so a patch wraps the real plugin, not an earlier wrapper.
_REAL_BATCH_STATS_PROCESS = BatchStats.process
_REAL_BATCH_REPLICATE_PROCESS = BatchReplicate.process


class _FlushFault(RuntimeError):
    """An ordinary exception from a batch plugin's flush: the run aborts, and resume retries the flush."""


def _scope_pipeline(output_mode: str) -> str:
    return _RELEASE_INTO_SCOPE.replace("OUTPUT_MODE", output_mode)


def _count_collector_flushes(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    calls: list[list[Any]] = []

    def process(self: BatchStats, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        calls.append([row["item"] for row in rows])
        return _REAL_BATCH_STATS_PROCESS(self, rows, ctx)

    monkeypatch.setattr(BatchStats, "process", process)
    return calls


def _output_rows(env: dict[str, Any]) -> list[dict[str, Any]]:
    """The sink's rows, sorted: sibling groups may close in either order (the scheduler's claim order)."""
    return sorted((json.loads(line) for line in env["output_path"].read_text().splitlines()), key=json.dumps)


def _tokens_without_a_terminal_outcome(db: LandscapeDB) -> int:
    with db.connection() as conn:
        terminal = select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.completed == 1)
        return int(
            conn.execute(select(func.count()).select_from(tokens_table).where(tokens_table.c.token_id.not_in(terminal))).scalar_one()
        )


def _journal_statuses(db: LandscapeDB) -> dict[str, int]:
    with db.connection() as conn:
        return {
            str(status): int(count)
            for status, count in conn.execute(
                select(token_work_items_table.c.status, func.count()).group_by(token_work_items_table.c.status)
            ).all()
        }


@pytest.mark.parametrize("documents", sorted(_DOCUMENTS))
@pytest.mark.parametrize("output_mode", ["passthrough", "transform"])
def test_a_release_into_a_scope_opener_completes_every_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_mode: str, documents: str
) -> None:
    docs = _DOCUMENTS[documents]
    parseable = _PARSEABLE_IDS[documents]
    unparseable = len(docs) - len(parseable)
    env = build_pipeline(tmp_path, _scope_pipeline(output_mode), docs)
    flushes = _count_collector_flushes(monkeypatch)

    result = run_pipeline(env)

    assert result.status is (RunStatus.COMPLETED_WITH_FAILURES if unparseable else RunStatus.COMPLETED)
    assert _output_rows(env) == sorted((_GROUP_OUTPUT[doc_id] for doc_id in parseable), key=json.dumps)
    assert len(flushes) == len(parseable), "each document's group is flushed exactly once"
    assert _tokens_without_a_terminal_outcome(env["db"]) == 0
    journal = _journal_statuses(env["db"])
    # The opener's discard of an unparseable document leaves its work item FAILED, as it does without the release.
    assert journal.pop(TokenWorkStatus.FAILED.value, 0) == unparseable
    assert set(journal) == {TokenWorkStatus.TERMINAL.value}


def test_a_release_into_a_transform_then_a_second_barrier_completes(tmp_path: Path) -> None:
    """No scope at all: a sibling held at the second barrier carries the transform's row, which the replay refused."""
    docs = _DOCUMENTS["two"]
    env = build_pipeline(tmp_path, _RELEASE_INTO_SECOND_BARRIER, docs)

    result = run_pipeline(env)

    assert result.status is RunStatus.COMPLETED
    assert _output_rows(env) == sorted((dict(doc, tagged=doc["id"] + 100) for doc in docs), key=json.dumps)
    assert _tokens_without_a_terminal_outcome(env["db"]) == 0
    assert set(_journal_statuses(env["db"])) == {TokenWorkStatus.TERMINAL.value}


def _fault_the_first_flush(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The aggregation's first flush raises; later flushes work. Returns a record that the fault fired."""
    # The fault makes the plugin's behaviour call-dependent, so it is declared so:
    # resume re-invokes it by design rather than by accident.
    monkeypatch.setattr(BatchReplicate, "determinism", Determinism.NON_DETERMINISTIC)
    fired: list[str] = []

    def replicate(self: BatchReplicate, rows: list[PipelineRow], ctx: PluginContext) -> TransformResult:
        if not fired:
            fired.append("flush")
            raise _FlushFault("in the aggregation flush, before the release")
        return _REAL_BATCH_REPLICATE_PROCESS(self, rows, ctx)

    monkeypatch.setattr(BatchReplicate, "process", replicate)
    return fired


@pytest.mark.parametrize("output_mode", ["passthrough", "transform"])
def test_a_resumed_flush_makes_the_release_to_the_uncrashed_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output_mode: str
) -> None:
    """The first flush faults, so resume's end-of-source flush is the one that releases both continuations."""
    docs = _DOCUMENTS["two"]
    control_env = build_pipeline(tmp_path / "control", _scope_pipeline(output_mode), docs)
    control = run_pipeline(control_env)
    assert control.status is RunStatus.COMPLETED

    env = build_pipeline(tmp_path / "faulted", _scope_pipeline(output_mode), docs)
    flushes = _count_collector_flushes(monkeypatch)
    # The fault stays patched through the resume (it fires once): resume refuses
    # a node whose plugin implementation differs from the one the run recorded.
    fired = _fault_the_first_flush(monkeypatch)
    with pytest.raises(_FlushFault):
        run_pipeline(env)
    assert fired == ["flush"]
    assert flushes == []

    resumed = resume_pipeline(env)

    assert resumed.status is RunStatus.COMPLETED
    assert len(flushes) == 2, "each group is flushed exactly once"
    assert _output_rows(env) == _output_rows(control_env)
    assert terminal_counts(env["db"]) == terminal_counts(control_env["db"])
    assert _tokens_without_a_terminal_outcome(env["db"]) == 0
    assert set(_journal_statuses(env["db"])) == {TokenWorkStatus.TERMINAL.value}
