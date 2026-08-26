"""A row_union-released token crashed mid-flight resumes via the JOURNAL, not mint frames.

elspeth-54edda5699: row_union pops the FORK frame in memory only
(``row_union_executor.py`` via ``pop_fork_frame``); ``token_lineage_frames``
rows are mint frames and never rewritten. ``get_resume_workset`` therefore
projects an ``IncompleteTokenSpec`` for a released-but-unfinished token whose
``lineage_path`` has silently REGAINED the popped FORK frame — and
``classify_resume_start`` would misroute it back through the whole fork branch
(re-arriving at a barrier whose group already released).

Measured 2026-08-26: that projection is real but is NEVER dispatched on the
orchestrator resume path, because ``run_resume_processing_loop`` drains the
durable scheduler journal FIRST (``resume.py`` — journal rows carry the popped
``lineage_path_json``, the byte-exact pre-crash path) and then DISCARDS the
row-replay work outright (``unprocessed_rows = ()``); a mixed image where a
recovered row lacks scheduler coverage fail-closes with AuditIntegrityError
instead of silently re-driving. This module pins that healing mechanism:

* the crashed image really carries the divergence (journal bytes = popped
  path; workset spec bytes = mint path with the FORK frame back), and
* resume completes every released token from the journal — nothing
  re-executes (no node_state above attempt 0), one terminal outcome per
  token, and the journal's post-union lineage bytes are unchanged across the
  crash boundary.

Killing either half of the healer (the drain-first precedence or the
coverage refusal) re-dispatches the mint-frame specs and fails the
one-outcome-per-token assertion here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select, update

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.enums import RunStatus
from elspeth.contracts.identity import FrameKind
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import CheckpointSettings, load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB, RecorderFactory
from elspeth.core.landscape.schema import (
    node_states_table,
    run_coordination_table,
    token_lineage_frames_table,
    token_outcomes_table,
    token_work_items_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.executors.sink import SinkExecutor
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from tests.e2e.recovery.harness import spawn_database_process_at_seam, spawn_database_process_with_pause

_RUN_ID = "row-union-released-token-resume"
_CRASH_SEAM = "after-union-release-before-sink-write"
_PROCESS_TIMEOUT_SECONDS = 120.0


def _build_union_pipeline(base: Path):
    """source -> gate fork (2 branches) -> identity row_union -> transform -> sink."""
    input_path = base / "input.jsonl"
    output_path = base / "out.jsonl"
    settings = load_settings_from_yaml_string(
        f"""
sources:
  rows:
    plugin: json
    on_success: routed
    options:
      path: {input_path}
      format: jsonl
      on_validation_failure: discard
      schema:
        mode: observed
gates:
  - name: variant_fork
    input: routed
    condition: "True"
    routes:
      'true': fork
      'false': output
    fork_to: [control_branch, treatment_branch]
row_unions:
  - name: variant_union
    branches: [control_branch, treatment_branch]
    on_success: union_out
transforms:
  - name: after_union
    plugin: passthrough
    input: union_out
    on_success: output
    on_error: discard
    options:
      schema:
        mode: observed
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: {output_path}
      format: jsonl
      mode: write
      schema:
        mode: observed
"""
    )
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        queues=settings.queues,
        row_union_settings=list(settings.row_unions),
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )
    config = replace(config, sink_effect_modes={"output": "write"})
    return config, graph, settings


def _run_union_to_sink_write_seam(db: LandscapeDB, pause: Callable[[], None], base_path: str) -> None:
    """Release BOTH rows' union groups durably, then pause at the first sink write.

    At the pause every released token has completed ``after_union`` (or holds
    a post-union journal row) and NO terminal outcome exists for it — the
    exact released-but-unfinished window elspeth-54edda5699 describes, in a
    RESUMABLE image (the source finished loading before sink flush).
    """
    base = Path(base_path)
    (base / "input.jsonl").write_text('{"id": 1, "amount": 10}\n{"id": 2, "amount": 20}\n')
    config, graph, settings = _build_union_pipeline(base)

    real_write = SinkExecutor.write

    def pause_before_first_write(self: SinkExecutor, *args: object, **kwargs: object) -> object:
        pause()
        return real_write(self, *args, **kwargs)  # pragma: no cover - parent kills at the pause

    SinkExecutor.write = pause_before_first_write  # type: ignore[method-assign]
    Orchestrator(
        db,
        checkpoint_manager=CheckpointManager(db),
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    ).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=FilesystemPayloadStore(base / "payloads"),
        run_id=_RUN_ID,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )


def _resume_union_after_sink_death(db: LandscapeDB, base_path: str) -> None:
    """Fresh-process resume oracle: every released token completes exactly once."""
    base = Path(base_path)
    config, graph, settings = _build_union_pipeline(base)
    checkpoint_manager = CheckpointManager(db)
    recovery = RecoveryManager(db, checkpoint_manager)
    resume_point = recovery.get_resume_point(_RUN_ID, graph)
    assert resume_point is not None, recovery.can_resume(_RUN_ID, graph)
    result = Orchestrator(
        db,
        checkpoint_manager=checkpoint_manager,
        checkpoint_config=RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row")),
    ).resume(
        resume_point,
        config,
        graph,
        settings=settings,
        payload_store=FilesystemPayloadStore(base / "payloads"),
    )
    assert result.status is RunStatus.COMPLETED
    output_rows = [json.loads(line) for line in (base / "out.jsonl").read_text().splitlines() if line.strip()]
    assert sorted(row["id"] for row in output_rows) == [1, 1, 2, 2], output_rows


def test_released_row_union_tokens_resume_from_the_journal_not_mint_frames(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{tmp_path / 'row-union-released.db'}"
    with LandscapeDB(database_url):
        pass

    with spawn_database_process_with_pause(
        database_url=database_url,
        seam=_CRASH_SEAM,
        action=_run_union_to_sink_write_seam,
        action_args=(str(tmp_path),),
    ) as child:
        ready = child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        assert ready.pid != os.getpid()
        child.kill()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).was_killed

    # ── The crashed image really is the hazard the ticket describes ──
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
        with killed_db.connection() as conn:
            frame_rows = conn.execute(
                select(token_lineage_frames_table.c.token_id, token_lineage_frames_table.c.kind).where(
                    token_lineage_frames_table.c.run_id == _RUN_ID
                )
            ).all()
            released_token_ids = {row.token_id for row in frame_rows}
            # Two rows x two branches, every mint record a FORK frame.
            assert len(released_token_ids) == 4
            assert {row.kind for row in frame_rows} == {FrameKind.FORK.value}
            # No released token has a terminal outcome yet, and none is blocked.
            outcome_rows = conn.execute(
                select(token_outcomes_table.c.token_id).where(
                    token_outcomes_table.c.run_id == _RUN_ID,
                    token_outcomes_table.c.token_id.in_(released_token_ids),
                    token_outcomes_table.c.completed == 1,
                )
            ).all()
            assert outcome_rows == []
            post_union_rows = conn.execute(
                select(token_work_items_table.c.token_id, token_work_items_table.c.status, token_work_items_table.c.lineage_path_json)
                .where(token_work_items_table.c.run_id == _RUN_ID)
                .where(token_work_items_table.c.token_id.in_(released_token_ids))
                .where(token_work_items_table.c.barrier_key.is_(None))
            ).all()
            assert {row.token_id for row in post_union_rows} == released_token_ids
            assert all(row.status != "blocked" for row in post_union_rows)
            # The journal's post-release rows carry the POPPED path — the
            # byte-exact pre-crash lineage.
            assert {row.lineage_path_json for row in post_union_rows} == {"[]"}

        # Supervisor classification, as production would record it.
        RecorderFactory(killed_db).run_lifecycle.update_run_status(_RUN_ID, RunStatus.FAILED)
        with killed_db.write_connection() as conn:
            conn.execute(
                update(run_coordination_table)
                .where(run_coordination_table.c.run_id == _RUN_ID)
                .values(leader_heartbeat_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )

        # The workset PROJECTION regains the popped FORK frame (the ticket's
        # divergence): every released token appears as an incomplete spec whose
        # lineage_path was rebuilt from MINT frames. This is the hazard the
        # journal-drain precedence must keep un-dispatched.
        workset = RecoveryManager(killed_db, CheckpointManager(killed_db)).get_resume_workset(_RUN_ID)
        specs = [spec for specs in workset.incomplete_by_row.values() for spec in specs]
        assert {spec.token_id for spec in specs} == released_token_ids
        assert all(len(spec.lineage_path) == 1 and spec.lineage_path[0].kind is FrameKind.FORK for spec in specs), (
            "workset specs no longer regain the FORK frame — update this pin alongside the projection change"
        )

    with spawn_database_process_at_seam(
        database_url=database_url,
        seam="fresh-process-recovery-completed",
        action=_resume_union_after_sink_death,
        action_args=(str(tmp_path),),
    ) as child:
        child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        child.release()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).exitcode == 0

    # ── Healing verified: journal-served, byte-identical, exactly-once ──
    with LandscapeDB.from_url(database_url, create_tables=False) as recovered, recovered.connection() as conn:
        outcome_rows = conn.execute(
            select(token_outcomes_table.c.token_id)
            .where(token_outcomes_table.c.run_id == _RUN_ID)
            .where(token_outcomes_table.c.token_id.in_(released_token_ids))
            .where(token_outcomes_table.c.completed == 1)
        ).all()
        # Exactly one terminal outcome per released token: the journal drain
        # completed them; a re-dispatch of the mint-frame specs would have
        # re-run the fork branches and doubled (or late-arrival-killed) these.
        assert sorted(row.token_id for row in outcome_rows) == sorted(released_token_ids)
        # Nothing re-executed: the resumed process wrote no bumped attempts.
        attempts = conn.execute(select(node_states_table.c.attempt).where(node_states_table.c.run_id == _RUN_ID)).scalars()
        assert set(attempts) == {0}
        # The post-union journal rows still carry the popped path: the
        # continuation's lineage bytes are identical across the crash boundary.
        post_union_lineage = conn.execute(
            select(token_work_items_table.c.lineage_path_json)
            .where(token_work_items_table.c.run_id == _RUN_ID)
            .where(token_work_items_table.c.token_id.in_(released_token_ids))
            .where(token_work_items_table.c.barrier_key.is_(None))
        ).scalars()
        assert set(post_union_lineage) == {"[]"}
