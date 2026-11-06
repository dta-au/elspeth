"""Audit record for a row whose claim follows a shutdown request on the CLI path.

With settings present the production processor always carries a
RetryManager. A non-snapshot source interrupted mid-iteration makes the run
non-resumable, so a row ingested after the shutdown request is never decided
by a plugin: ADR-038 records it ``(NULL, ABANDONED)`` at finalization rather
than inventing a failure for work that never started.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import PipelineRow, RunStatus
from elspeth.contracts.checkpoint import ResumeRefusalCause
from elspeth.contracts.errors import GracefulShutdownError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.core.checkpoint import RecoveryManager
from elspeth.core.landscape.schema import (
    node_states_table,
    rows_table,
    run_sources_table,
    runs_table,
    token_outcomes_table,
    tokens_table,
)
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.sources.json_source import JSONSource
from elspeth.plugins.transforms.value_transform import ValueTransform
from tests.integration.pipeline.test_barrier_hold_payload import build_pipeline

_BODY = """
transforms:
  - name: lift
    plugin: value_transform
    input: buffered
    on_success: out
    on_error: discard
    options:
      schema: {{mode: observed}}
      operations:
        - target: n
          expression: "row['item'] * 10"
"""


def test_row_ingested_after_shutdown_request_is_abandoned_not_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = build_pipeline(tmp_path, _BODY, [{"id": i, "item": i} for i in (1, 2, 3)])
    assert env["settings"] is not None  # the CLI shape: the processor carries a RetryManager
    event = threading.Event()
    original_load = JSONSource.load
    original_process = ValueTransform.process
    calls: list[int] = []

    def request_shutdown_before_second_row(source: JSONSource, ctx: Any):
        for index, row in enumerate(original_load(source, ctx)):
            if index == 1:
                event.set()
            yield row

    def record(self: ValueTransform, row: PipelineRow, ctx: PluginContext) -> TransformResult:
        calls.append(row["id"])
        return original_process(self, row, ctx)

    monkeypatch.setattr(JSONSource, "load", request_shutdown_before_second_row)
    monkeypatch.setattr(ValueTransform, "process", record)
    catalog_sha256, catalog_source = env["catalog"]
    with pytest.raises(GracefulShutdownError) as error:
        env["orchestrator"].run(
            env["config"],
            graph=env["graph"],
            settings=env["settings"],
            payload_store=env["payload_store"],
            openrouter_catalog_sha256=catalog_sha256,
            openrouter_catalog_source=catalog_source,
            shutdown_event=event,
        )
    run_id = error.value.run_id

    # Only the row claimed before the request reached the plugin.
    assert calls == [1]
    output = [json.loads(line) for line in env["output_path"].read_text().splitlines()]
    assert [row["id"] for row in output] == [1]

    with env["db"].connection() as conn:
        assert conn.execute(select(runs_table.c.status)).scalar_one() == RunStatus.INTERRUPTED.value
        assert conn.execute(select(run_sources_table.c.lifecycle_state)).scalar_one() == "interrupted"
        outcomes = {
            source_row_index: (outcome, path, completed, error_hash, context_json)
            for source_row_index, outcome, path, completed, error_hash, context_json in conn.execute(
                select(
                    rows_table.c.source_row_index,
                    token_outcomes_table.c.outcome,
                    token_outcomes_table.c.path,
                    token_outcomes_table.c.completed,
                    token_outcomes_table.c.error_hash,
                    token_outcomes_table.c.context_json,
                )
                .select_from(
                    token_outcomes_table.join(tokens_table, token_outcomes_table.c.token_id == tokens_table.c.token_id).join(
                        rows_table, tokens_table.c.row_id == rows_table.c.row_id
                    )
                )
                .where(token_outcomes_table.c.run_id == run_id)
            ).all()
        }
        failed_states = conn.execute(select(node_states_table.c.state_id).where(node_states_table.c.status == "failed")).all()
        abandoned_ref = conn.execute(select(rows_table.c.source_data_ref).where(rows_table.c.source_row_index == 1)).scalar_one()

    assert sorted(outcomes) == [0, 1]
    # The abandoned row stays attributable: its source payload is retrievable.
    assert abandoned_ref is not None
    assert json.loads(env["payload_store"].retrieve(abandoned_ref)) == {"id": 2, "item": 2}
    assert outcomes[0][:3] == ("success", "default_flow", 1)
    # The second row was ingested but never started: undecided and abandoned,
    # with no failure verdict, error hash or failed node state invented for it.
    outcome, path, completed, error_hash, context_json = outcomes[1]
    assert (outcome, path, completed, error_hash) == (None, "abandoned", 0, None)
    context = json.loads(context_json)
    assert context == {
        "abandoned_by": "run_finalization",
        "run_status": RunStatus.INTERRUPTED.value,
        "non_resumable_arms": ["incomplete_sources"],
        "incomplete_sources": {"docs": "interrupted"},
    }
    assert failed_states == []

    check = RecoveryManager(env["db"], env["checkpoints"]).can_resume(run_id, env["graph"])
    assert not check.can_resume
    assert check.cause is ResumeRefusalCause.SOURCE_NOT_EXHAUSTED
