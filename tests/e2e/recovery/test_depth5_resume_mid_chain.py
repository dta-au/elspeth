"""Depth-5 nested crash + resume family (spec §8 + §6.3, WS5 Task 5a).

Crash a depth-5 all-require_all nested run after its drain-time settlement
and resume it. The satisfiability gate must not false-refuse the crashed
image (spec §8), and the resumed run must reach the SAME terminal outcome
as an uncrashed run.

The topology is IMPORTED from ``test_depth5_group_unwrap`` (WS3 Task 10's
``_nested_settings`` / ``DEPTH`` — the same document, never reshaped), and
the outcome oracle is a BASELINE leg run fresh in this module: the crashed
legs' group-loss ledgers, run statuses, and journal completeness are
compared structurally against it, which keeps this family in literal
lockstep with ``test_depth5_single_failure_unwraps_to_outermost_quarantine``
by construction.

Three facts measured 2026-08-25 that reshape the plan's sketch, each
pinned by a crashed-image precondition below:

1. **The 5-level unwrap settles during the DRAIN, not the EOF fixpoint.**
   One §E.2 intake pass cascades the entire unwrap through its internal
   work queue, so every ``escalation_fixpoint_bound`` clamp >= 1 converges
   (the plan sketched ``bound=2`` as a mid-unwrap crash; it is not one).
   Only ``bound=0`` still crashes — AFTER full settlement, before terminal
   accounting.
2. **The escalation cascade is transactionally atomic** (single-worker): an
   in-process raise after N durable ``record_group_loss`` writes rolls back
   ALL of them with the enclosing claim transaction. The durable
   "escalation partially staged" image does not exist at depth 5 on one
   worker; it requires multi-worker loss staging — the collector
   death-matrix lane's territory (META-9.1), not Task 5a's.
3. **An in-process abort sweeps BLOCKED journal rows terminal** (the
   NodeStateGuard/teardown path), so a durably-open-group crash image is
   reachable only by PROCESS DEATH. And any pre-settlement process death
   forces the loss to be staged POST-RESUME — again META-9.1's scenario,
   excluded from this lane by the dispatch brief.

The in-scope family is therefore the post-settlement crash, exercised
through both crash surfaces:

- **Config-injected in-process abort** (``escalation_fixpoint_bound=0``):
  the EOF fixpoint raises its non-convergence ``OrchestrationInvariantError``
  with every loss durable; the run marks itself FAILED and the abort sweep
  has run.
- **SIGKILL at the EOF-intake seam** (the recovery harness's pausable
  spawn): the process dies with every loss durable but NO finalization and
  NO abort sweep — the run is left ``running`` and an external-supervisor
  classification (``update_run_status(FAILED)`` + leader-heartbeat expiry,
  the death-matrix pattern) makes it resumable.

Scope note (brief, Task 5a): crash+resume WITHOUT loss scenarios only —
non-opener-worker loss and post-resume loss belong to the collector
death-matrix lane (META-9.1), not here.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select, update

from elspeth.contracts import RunStatus
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.scheduler import TokenWorkStatus
from elspeth.contracts.sink_effects import SinkEffectExecutionPurpose, SinkEffectInputKind
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import ElspethSettings, load_settings_from_config_dict
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.bound_regions import derive_escalation_fixpoint_bound
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    group_losses_table,
    run_coordination_table,
    run_workers_table,
    runs_table,
    token_outcomes_table,
    token_work_items_table,
    tokens_table,
)
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.preflight import (
    assemble_and_validate_pipeline_config,
    execution_sink_bindings_for_runtime,
    execution_sinks_for_runtime,
    sink_effect_modes_from_runtime_bindings,
    validate_pipeline_sink_effect_capabilities,
)
from elspeth.engine.processor import RowProcessor
from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
from elspeth.plugins.transforms.llm.model_catalog import read_openrouter_catalog_snapshot_id
from tests.e2e.recovery.harness import spawn_database_process_with_pause
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager
from tests.integration.pipeline.test_depth5_group_unwrap import DEPTH, _nested_settings, _substitute_paths

_PROCESS_TIMEOUT_SECONDS = 60.0
_EOF_INTAKE_SEAM = "depth5-eof-intake"


@dataclasses.dataclass(frozen=True)
class _LegOutcome:
    """The structural terminal image one leg leaves in its audit DB."""

    run_status: str
    quarantined: frozenset[tuple[str, str]]  # (closer_name, member_key)
    escalated: frozenset[tuple[str, str]]
    non_terminal_token_count: int
    blocked_row_count: int


def _assemble(settings: ElspethSettings, *, purpose: SinkEffectExecutionPurpose) -> tuple[ExecutionGraph, PipelineConfig]:
    """The production assembly sequence (mirrors test_depth5_group_unwrap's
    ``run_settings`` fixture, plus the RESUME sink-effect purpose arm)."""
    bundle = instantiate_plugins_from_config(settings, preflight_mode=True, sink_effect_purpose=purpose)
    execution_sinks = execution_sinks_for_runtime(settings, bundle.sinks)
    if purpose is SinkEffectExecutionPurpose.RESUME:
        for sink_name, sink in execution_sinks.items():
            assert sink.supports_resume, f"depth-5 fixture sink {sink_name!r} must support resume"
            sink.configure_for_resume()
    execution_bindings = execution_sink_bindings_for_runtime(settings, bundle.sink_effect_bindings)
    sink_effect_modes = sink_effect_modes_from_runtime_bindings(
        execution_sinks,
        execution_bindings,
        purpose=purpose,
        configured_options={name: settings.sinks[name].options for name in execution_sinks},
    )
    sink_effect_admission = validate_pipeline_sink_effect_capabilities(
        execution_sinks,
        configured_modes=sink_effect_modes,
        required_input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=execution_sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
        coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
        row_union_settings=list(settings.row_unions) if settings.row_unions else None,
        queues=settings.queues,
        collectors=bundle.collectors,
        scope_settings=list(settings.scopes) if settings.scopes else None,
        max_bound_region_depth=settings.max_bound_region_depth,
    )
    graph.validate()
    graph.validate_edge_compatibility()
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
        sink_effect_modes=sink_effect_modes,
        sink_effect_admission=sink_effect_admission,
    )
    return graph, config


def _load_case_settings(case_dir: Path) -> ElspethSettings:
    case_dir.mkdir(parents=True, exist_ok=True)
    input_path = case_dir / "input.csv"
    input_path.write_text("id,value\n1,10\n")  # single row (cardinality note in the imported module)
    resolved = _substitute_paths(_nested_settings(DEPTH), input_path=input_path, output_path=case_dir / "output.jsonl")
    return load_settings_from_config_dict(resolved)


def _run_fresh(db: LandscapeDB, settings: ElspethSettings, case_dir: Path, *, clamp_bound: int | None = None) -> Any:
    graph, config = _assemble(settings, purpose=SinkEffectExecutionPurpose.FRESH)
    if clamp_bound is not None:
        config = dataclasses.replace(config, escalation_fixpoint_bound=clamp_bound)
    catalog_sha256, catalog_source = read_openrouter_catalog_snapshot_id()
    checkpoint_mgr = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(settings.checkpoint)
    return Orchestrator(db, checkpoint_manager=checkpoint_mgr, checkpoint_config=checkpoint_config).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=FilesystemPayloadStore(case_dir / "payloads"),
        openrouter_catalog_sha256=catalog_sha256,
        openrouter_catalog_source=catalog_source,
    )


def _run_depth5_to_eof_intake_seam(db: LandscapeDB, pause: Callable[[], None], case_dir_str: str) -> None:
    """Child action (fresh spawn interpreter): run the depth-5 document and
    pause at the FIRST EOF barrier-intake call — drain-time settlement fully
    durable, no finalization. The parent SIGKILLs at the pause."""
    mp = pytest.MonkeyPatch()
    install_corpus_plugin_manager(mp)
    case_dir = Path(case_dir_str)
    settings = _load_case_settings(case_dir)

    real_intake = RowProcessor.run_barrier_intake

    def pausing_intake(self: RowProcessor, ctx: Any) -> Any:
        pause()
        return real_intake(self, ctx)

    mp.setattr(RowProcessor, "run_barrier_intake", pausing_intake)
    _run_fresh(db, settings, case_dir)


def _leg_outcome(db_path: Path) -> _LegOutcome:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            run_row = conn.execute(select(runs_table)).one()
            losses = conn.execute(select(group_losses_table)).mappings().all()
            minted = set(conn.execute(select(tokens_table.c.token_id)).scalars())
            completed = set(conn.execute(select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.completed == 1)).scalars())
            blocked = conn.execute(
                select(token_work_items_table.c.work_item_id).where(token_work_items_table.c.status == TokenWorkStatus.BLOCKED.value)
            ).fetchall()
    finally:
        engine.dispose()
    return _LegOutcome(
        run_status=str(run_row.status),
        quarantined=frozenset((row["closer_name"], row["member_key"]) for row in losses if row["reason"] == "quarantined"),
        escalated=frozenset((row["closer_name"], row["member_key"]) for row in losses if row["reason"] == "group_failed"),
        non_terminal_token_count=len(minted - completed),
        blocked_row_count=len(blocked),
    )


def _crashed_image(db_path: Path) -> tuple[str, RunStatus, list[str]]:
    """(run_id, status, loss reasons) at the crash instant."""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            run_row = conn.execute(select(runs_table)).one()
            reasons = list(conn.execute(select(group_losses_table.c.reason)).scalars())
    finally:
        engine.dispose()
    return str(run_row.run_id), RunStatus(run_row.status), reasons


def _assert_settled_crash_image(status: RunStatus, reasons: list[str], *, expected_status: RunStatus) -> None:
    """The post-settlement preconditions (measured facts 1-3, module docstring):
    the whole unwrap must already be durable at the crash instant."""
    assert status is expected_status, status
    assert reasons.count("quarantined") == 1
    assert reasons.count("group_failed") == DEPTH - 1, (
        "drain-time settlement must be complete at the crash instant; a partial cascade here would mean "
        f"the atomicity fact this family is built on no longer holds: {reasons}"
    )


def _resume_after_crash(db: LandscapeDB, settings: ElspethSettings, case_dir: Path, run_id: str) -> None:
    """Gate (advisory), then real ``Orchestrator.resume`` (enforcing surface):
    the resume completing is the absence of a group-satisfiability refuse."""
    resume_graph, resume_config = _assemble(settings, purpose=SinkEffectExecutionPurpose.RESUME)
    assert resume_config.escalation_fixpoint_bound == derive_escalation_fixpoint_bound(DEPTH)  # real bound, not a clamp
    checkpoint_mgr = CheckpointManager(db)
    recovery = RecoveryManager(db, checkpoint_mgr)
    check = recovery.can_resume(run_id, resume_graph)
    assert check.can_resume, f"false refuse on a settled depth-5 crash image: {check.reason}"
    resume_point = recovery.get_resume_point(run_id, resume_graph)
    assert resume_point is not None
    checkpoint_config = RuntimeCheckpointConfig.from_settings(settings.checkpoint)
    Orchestrator(db, checkpoint_manager=checkpoint_mgr, checkpoint_config=checkpoint_config).resume(
        resume_point=resume_point,
        config=resume_config,
        graph=resume_graph,
        payload_store=FilesystemPayloadStore(case_dir / "payloads"),
    )


@pytest.fixture(scope="module")
def baseline(tmp_path_factory: pytest.TempPathFactory) -> _LegOutcome:
    """Leg A: the uncrashed run — the outcome oracle for every crash leg."""
    mp = pytest.MonkeyPatch()
    baseline_dir = tmp_path_factory.mktemp("depth5-baseline")
    try:
        install_corpus_plugin_manager(mp)
        settings = _load_case_settings(baseline_dir)
        db = LandscapeDB(f"sqlite:///{baseline_dir / 'audit.db'}")
        try:
            _run_fresh(db, settings, baseline_dir)
        finally:
            db.close()
    finally:
        mp.undo()
    outcome = _leg_outcome(baseline_dir / "audit.db")
    # The baseline must BE the §6.3 acceptance image, or comparing against it
    # proves nothing: one quarantined loss at the innermost closer, one
    # escalation per enclosing level, every roster closed.
    assert outcome.quarantined == {(f"merge_{DEPTH}", f"go_{DEPTH}")}
    assert outcome.escalated == {(f"merge_{k}", f"go_{k}") for k in range(1, DEPTH)}
    assert outcome.non_terminal_token_count == 0
    assert outcome.blocked_row_count == 0
    assert RunStatus(outcome.run_status) not in (RunStatus.RUNNING, RunStatus.INTERRUPTED)
    return outcome


def _assert_resumed_matches_baseline(db_path: Path, baseline: _LegOutcome) -> None:
    resumed = _leg_outcome(db_path)
    assert resumed.quarantined == baseline.quarantined
    assert resumed.escalated == baseline.escalated
    assert resumed.non_terminal_token_count == 0
    assert resumed.blocked_row_count == 0  # every roster closed
    assert resumed.run_status == baseline.run_status


def test_depth5_in_process_abort_after_settlement_resumes_to_the_baseline_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline: _LegOutcome
) -> None:
    """Crash surface 1: config-injected in-process abort. The zero-round EOF
    clamp raises non-convergence with the whole unwrap durable; the run marks
    itself FAILED and the abort sweep has run. Resume finishes accounting and
    lands on the baseline outcome."""
    install_corpus_plugin_manager(monkeypatch)
    crash_dir = tmp_path / "crash"
    settings = _load_case_settings(crash_dir)
    db = LandscapeDB(f"sqlite:///{crash_dir / 'audit.db'}")
    try:
        with pytest.raises(OrchestrationInvariantError, match="did not converge within 0 intake/flush rounds"):
            _run_fresh(db, settings, crash_dir, clamp_bound=0)

        run_id, status, reasons = _crashed_image(crash_dir / "audit.db")
        _assert_settled_crash_image(status, reasons, expected_status=RunStatus.FAILED)

        _resume_after_crash(db, settings, crash_dir, run_id)
    finally:
        db.close()

    _assert_resumed_matches_baseline(crash_dir / "audit.db", baseline)


def test_depth5_process_death_after_settlement_resumes_to_the_baseline_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline: _LegOutcome
) -> None:
    """Crash surface 2: real process death (SIGKILL) at the EOF-intake seam.
    Settlement durable, NO finalization, NO abort sweep — the run is left
    ``running`` and the external-supervisor classification (the death-matrix
    pattern) makes it resumable. Resume must not false-refuse and must land
    on the baseline outcome."""
    install_corpus_plugin_manager(monkeypatch)  # the parent's resume leg builds the same document
    crash_dir = tmp_path / "crash"
    crash_dir.mkdir()
    database_url = f"sqlite:///{crash_dir / 'audit.db'}"
    with LandscapeDB(database_url):
        pass  # create tables; the harness child opens with create_tables=False

    with spawn_database_process_with_pause(
        database_url=database_url,
        seam=_EOF_INTAKE_SEAM,
        action=_run_depth5_to_eof_intake_seam,
        action_args=(str(crash_dir),),
    ) as child:
        child.wait_until_ready(timeout=_PROCESS_TIMEOUT_SECONDS)
        child.kill()
        assert child.wait_for_exit(timeout=_PROCESS_TIMEOUT_SECONDS).was_killed

    run_id, status, reasons = _crashed_image(crash_dir / "audit.db")
    # SIGKILL executes no finalization ceremony: settlement is durable but the
    # run is still RUNNING — the un-swept image only process death can leave.
    _assert_settled_crash_image(status, reasons, expected_status=RunStatus.RUNNING)

    # External-supervisor classification (the death-matrix pattern): the
    # production lifecycle writer marks the run FAILED and the dead leader's
    # seat and worker heartbeats are expired so the resume takeover can seize.
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    with LandscapeDB.from_url(database_url, create_tables=False) as killed_db:
        RecorderFactory(killed_db).run_lifecycle.update_run_status(run_id, RunStatus.FAILED)
        with killed_db.write_connection() as conn:
            conn.execute(
                update(run_coordination_table)
                .where(run_coordination_table.c.run_id == run_id)
                .values(leader_heartbeat_expires_at=expired_at)
            )
            conn.execute(update(run_workers_table).where(run_workers_table.c.run_id == run_id).values(heartbeat_expires_at=expired_at))
            conn.execute(
                update(token_work_items_table)
                .where(token_work_items_table.c.run_id == run_id)
                .where(token_work_items_table.c.status == TokenWorkStatus.LEASED.value)
                .values(lease_expires_at=expired_at)
            )

    # Bounded wait for the classification to be visible to the gate.
    settings = _load_case_settings(crash_dir)
    resume_graph, _resume_config = _assemble(settings, purpose=SinkEffectExecutionPurpose.FRESH)
    deadline = time.monotonic() + _PROCESS_TIMEOUT_SECONDS
    db = LandscapeDB.from_url(database_url, create_tables=False)
    try:
        recovery = RecoveryManager(db, CheckpointManager(db))
        while time.monotonic() < deadline:
            if recovery.can_resume(run_id, resume_graph).can_resume:
                break
            time.sleep(0.02)
        _resume_after_crash(db, settings, crash_dir, run_id)
    finally:
        db.close()

    _assert_resumed_matches_baseline(crash_dir / "audit.db", baseline)
