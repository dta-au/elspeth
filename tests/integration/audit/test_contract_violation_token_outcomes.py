"""Terminal token outcomes for schema-validation contract violations (elspeth-82d4c5146c).

Every token must reach exactly one terminal outcome. Without one, run
accounting reports a pending token on a finished run (failed=0,
closure='open'); with two, the audit store rejects the second write. Both are
contradictions the audit evidence should never contain.

WHO writes that outcome moved with elspeth-181db83da7 and now follows the tier
of the violation. A bare ``PluginContractViolation`` is Tier 2, so the
processor converts it into a routable transform error and the ``on_error``
destination terminalizes the token — the run finishes rather than crashing, and
the executor records nothing. A Tier-1 subclass (the declaration-contract
hierarchy) still crashes the run, so the executor still records FAILURE/UNROUTED
itself. Sink-boundary violations are a separate surface and keep the crashing
shape. The tests below are split along exactly that line.

SCOPE BOUNDARY, learned the hard way: this applies to ROW-LEVEL violations,
whose fate is decided where they raise. It does NOT extend to the aggregation
flush sites, which look identical but must never terminalize — see
``test_aggregation_eof_flush_violation_leaves_genuinely_retryable_tokens``.
The aggregation half is instead decided at run finalization (ADR-038): a
FAILED/INTERRUPTED stamp on a non-resumable run records ``(NULL, ABANDONED)``
for every undecided token — see
``test_aggregation_count_flush_violation_abandons_tokens_at_finalization``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import Determinism, PluginSchema, ResumePoint, RunStatus
from elspeth.contracts.config.runtime import RuntimeCheckpointConfig
from elspeth.contracts.enums import OutputMode, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import IncompleteSourceResumeError, PluginContractViolation
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.contracts.types import AggregationName
from elspeth.core.checkpoint import CheckpointManager, RecoveryManager
from elspeth.core.config import AggregationSettings, CheckpointSettings, SourceSettings, TriggerConfig
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import run_sources_table, runs_table, token_outcomes_table, tokens_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.web.execution.accounting import load_run_accounting_from_db
from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
from tests.fixtures.pipeline import build_linear_pipeline
from tests.fixtures.plugins import CollectSink, ListSource, PassTransform
from tests.fixtures.stores import MockPayloadStore


class _RequiresMissingField(PluginSchema):
    field_the_row_never_has: str


def _assert_failed_token_outcome(db: LandscapeDB) -> None:
    with db.connection() as conn:
        run_rows = conn.execute(select(runs_table)).fetchall()
        assert len(run_rows) == 1
        run_row = run_rows[0]
        token_rows = conn.execute(select(tokens_table).where(tokens_table.c.run_id == run_row.run_id)).fetchall()
        outcome_rows = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == run_row.run_id)).fetchall()

    assert run_row.status == RunStatus.FAILED
    assert len(token_rows) == 1
    assert len(outcome_rows) == 1
    assert outcome_rows[0].token_id == token_rows[0].token_id
    assert outcome_rows[0].outcome == TerminalOutcome.FAILURE.value
    assert outcome_rows[0].path == TerminalPath.UNROUTED.value


def _assert_routed_token_outcome(db: LandscapeDB) -> None:
    """A Tier-2 violation ends as exactly one on_error-routed FAILURE outcome.

    ``_assert_failed_token_outcome``'s FAILED/UNROUTED shape belonged to the era
    when the executor pre-recorded the outcome and the run then aborted. Since
    elspeth-181db83da7 the processor converts the violation into a routable
    transform error, so the run finishes and the routing path writes the token's
    single outcome. The count assertion is the point: one row, never two, never
    zero.
    """
    with db.connection() as conn:
        run_rows = conn.execute(select(runs_table)).fetchall()
        assert len(run_rows) == 1
        run_row = run_rows[0]
        token_rows = conn.execute(select(tokens_table).where(tokens_table.c.run_id == run_row.run_id)).fetchall()
        outcome_rows = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == run_row.run_id)).fetchall()

    assert len(token_rows) == 1
    assert len(outcome_rows) == 1
    assert outcome_rows[0].token_id == token_rows[0].token_id
    assert outcome_rows[0].outcome == TerminalOutcome.FAILURE.value
    # These fixtures leave on_error at its "discard" default, so the router
    # terminalizes the row on the discard path rather than diverting it to a
    # named sink. The value is asserted so a silent change of destination
    # cannot pass as "still exactly one outcome".
    assert outcome_rows[0].path == TerminalPath.QUARANTINED_AT_SOURCE.value


def test_transform_input_validation_violation_routes_and_terminalizes_once() -> None:
    """The g11/g08/g04 battery shape: transform input validation routes the row."""
    db = LandscapeDB.in_memory()
    payload_store = MockPayloadStore()

    failing_transform = PassTransform()
    failing_transform.input_schema = _RequiresMissingField
    source, transforms, sinks, graph = build_linear_pipeline([{"value": 42}], transforms=[failing_transform])
    sink = sinks["default"]

    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=transforms,
        sinks={"default": as_sink(sink)},
    )

    Orchestrator(db).run(config, graph=graph, payload_store=payload_store)

    assert sink.results == []
    _assert_routed_token_outcome(db)


class _BatchInputValidationFailer(BaseTransform):
    """Batch transform whose input_schema demands a field the rows never carry."""

    name = "batch-input-validation-failer"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = None
    input_schema = _RequiresMissingField
    output_schema = _TestSchema
    is_batch_aware = True
    on_success = "output"
    on_error = "discard"

    def __init__(self) -> None:
        super().__init__({"schema": {"mode": "observed"}})

    def process(self, rows: list[PipelineRow], ctx: Any) -> TransformResult:  # type: ignore[override]
        shared_contract = rows[0].contract
        return TransformResult.success_multi(
            tuple(PipelineRow(row.to_dict(), shared_contract) for row in rows),
            success_reason={"action": "passthrough"},
        )


def _build_failing_aggregation(
    transform: BaseTransform,
    *,
    source_data: list[dict[str, Any]],
    trigger_count: int,
) -> tuple[PipelineConfig, ExecutionGraph, CollectSink]:
    """Build ``ListSource → Aggregation(transform) → CollectSink``.

    ``trigger_count`` selects WHICH flush path runs, and that choice decides
    whether the buffered tokens are recoverable — see the two tests below:
      * ``== len(source_data)`` → count-triggered mid-load flush
      * ``>  len(source_data)`` → END_OF_SOURCE flush after exhaustion
    """
    source = ListSource(source_data, name="list_source", on_success="agg_in")
    output_sink = CollectSink("output")
    agg_settings = AggregationSettings(
        name="failing_agg",
        plugin=transform.name,
        input="agg_in",
        on_success="output",
        on_error="discard",
        trigger=TriggerConfig(count=trigger_count, timeout_seconds=3600),
        output_mode=OutputMode.TRANSFORM,
    )
    graph = ExecutionGraph.from_plugin_instances(
        sources={"primary": as_source(source)},
        source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="agg_in", options={})},
        transforms=[],
        sinks={"output": as_sink(output_sink)},
        aggregations={"failing_agg": (as_transform(transform), agg_settings)},
        gates=[],
    )
    agg_node_id = graph.get_aggregation_id_map()[AggregationName("failing_agg")]
    transform.node_id = agg_node_id
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[as_transform(transform)],
        sinks={"output": as_sink(output_sink)},
        aggregation_settings={agg_node_id: agg_settings},
    )
    return config, graph, output_sink


def _source_lifecycle_states(db: LandscapeDB, run_id: str) -> dict[str, str]:
    with db.engine.connect() as conn:
        rows = conn.execute(
            select(run_sources_table.c.source_name, run_sources_table.c.lifecycle_state).where(run_sources_table.c.run_id == run_id)
        ).all()
    return {str(row.source_name): str(row.lifecycle_state) for row in rows}


def _run_failing_aggregation(
    db: LandscapeDB, tmp_path: Path, *, trigger_count: int
) -> tuple[str, RecoveryManager, ExecutionGraph, CheckpointManager]:
    """Crash a 3-row aggregation on an input-validation violation, checkpointed."""
    checkpoint_mgr = CheckpointManager(db)
    checkpoint_config = RuntimeCheckpointConfig.from_settings(CheckpointSettings(enabled=True, frequency="every_row"))
    config, graph, output_sink = _build_failing_aggregation(
        _BatchInputValidationFailer(),
        source_data=[{"value": 1}, {"value": 2}, {"value": 3}],
        trigger_count=trigger_count,
    )
    orchestrator = Orchestrator(db, checkpoint_manager=checkpoint_mgr, checkpoint_config=checkpoint_config)

    with pytest.raises(PluginContractViolation, match="input validation failed"):
        orchestrator.run(config, graph=graph, payload_store=FilesystemPayloadStore(tmp_path / "payloads"))

    assert output_sink.results == []
    with db.connection() as conn:
        run_rows = conn.execute(select(runs_table)).fetchall()
    assert len(run_rows) == 1
    return str(run_rows[0].run_id), RecoveryManager(db, checkpoint_mgr), graph, checkpoint_mgr


def _assert_all_tokens_undecided(db: LandscapeDB, run_id: str) -> None:
    accounting = load_run_accounting_from_db(db, landscape_run_id=run_id)
    assert accounting.tokens.emitted == 3
    assert accounting.tokens.failed == 0
    assert accounting.tokens.pending == 3
    assert accounting.tokens.abandoned == 0
    assert accounting.integrity.closure == "open"


def _assert_all_tokens_abandoned(db: LandscapeDB, run_id: str) -> None:
    """ADR-038: undecided-forever tokens carry explicit abandonment records."""
    accounting = load_run_accounting_from_db(db, landscape_run_id=run_id)
    assert accounting.tokens.emitted == 3
    assert accounting.tokens.failed == 0
    assert accounting.tokens.pending == 0
    assert accounting.tokens.abandoned == 3
    assert accounting.integrity.missing_terminal_outcomes == 0
    assert accounting.integrity.closure == "abandoned"
    with db.connection() as conn:
        abandoned_rows = conn.execute(
            select(token_outcomes_table)
            .where(token_outcomes_table.c.run_id == run_id)
            .where(token_outcomes_table.c.path == TerminalPath.ABANDONED.value)
        ).fetchall()
    assert len(abandoned_rows) == 3
    for row in abandoned_rows:
        assert row.outcome is None
        assert row.completed == 0


def test_aggregation_eof_flush_violation_leaves_genuinely_retryable_tokens(tmp_path: Path) -> None:
    """END_OF_SOURCE flush: the tokens are undecided AND really recoverable.

    This is the case the "don't terminalize a raised flush" rule actually
    describes. ``execute_flush``'s failure arm clears the IN-MEMORY buffer while
    the DURABLE journal rows stay BLOCKED — journal-first intake (ADR-030 §E.2)
    — so the flush is retryable, not abandoned. Recording FAILURE here would be
    a false record about rows a resume may yet carry to success, which is what
    ``tests/integration/pipeline/test_eof_resume_proof.py`` demonstrates end to
    end with a transform that succeeds on its second attempt.

    Worse than a false record: ``barrier_coordination`` restore (§E.3a) sweeps
    on exactly the ``(FAILURE, UNROUTED)`` pair and RELEASES the matching
    BLOCKED journal rows, so writing that pair here would silently discard the
    buffered work a resume depends on.

    The trigger count is ABOVE the row count, so no count trigger fires and the
    buffer flushes at end of source — the source therefore reaches ``exhausted``
    and the run is genuinely resumable.

    ADR-038's finalization abandonment sweep must NOT fire here: the sources
    are complete and checkpoints exist, so a resume can still decide these
    tokens. ``closure='open'`` with ``abandoned=0`` is the honest state.
    """
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    run_id, recovery, graph, _ckpt = _run_failing_aggregation(db, tmp_path, trigger_count=5)

    _assert_all_tokens_undecided(db, run_id)

    # The discriminator: exhausted source ⇒ the pending tokens are recoverable,
    # so "undecided" is the honest state rather than a stranded one.
    assert _source_lifecycle_states(db, run_id) == {"primary": "exhausted"}
    assert recovery.can_resume(run_id, graph).can_resume


def test_aggregation_count_flush_violation_abandons_tokens_at_finalization(tmp_path: Path) -> None:
    """Count-triggered flush: undecided tokens that NOTHING will ever decide.

    The honest counterpart to the END_OF_SOURCE case: a count trigger fires on
    the third row's INTAKE, while the source is still ``loading``. The run is
    therefore NOT resumable — the advisory gate refuses (elspeth-1f5b83cd28)
    and the resume itself refuses with ``IncompleteSourceResumeError``,
    because the source never reached a complete lifecycle state.

    This was elspeth-82d4c5146c's original symptom (``closure='open'``,
    ``missing_terminal_outcomes == batch size`` on a finished run) and the
    **ADR-019 non-terminal-path gap** (elspeth-b4254f9a01). ADR-038 closes it
    at the only sound seam: the raise site still records nothing (terminalizing
    there would trip the §E.3a sweep and destroy the END_OF_SOURCE sibling's
    resume state — see the test above), and instead the FAILED finalization —
    inside its fenced terminal transaction, after the stamp — records
    ``(NULL, ABANDONED)`` for every undecided token because the run's sources
    never reached a complete lifecycle state. The tokens remain undecided
    (``completed=0``, ``outcome IS NULL``) but the audit trail now explains
    that nothing will ever decide them: ``closure='abandoned'``, not a
    contradiction.
    """
    db = LandscapeDB(f"sqlite:///{tmp_path / 'audit.db'}")
    run_id, recovery, graph, checkpoint_mgr = _run_failing_aggregation(db, tmp_path, trigger_count=3)

    _assert_all_tokens_abandoned(db, run_id)

    # Mid-load flush: the source never completed, so nothing can pick these up.
    # elspeth-1f5b83cd28: the advisory gate, the enforcing guard, and the
    # ADR-038 abandonment sweep now all agree on that fact.
    assert _source_lifecycle_states(db, run_id) == {"primary": "loading"}
    check = recovery.can_resume(run_id, graph)
    assert not check.can_resume
    assert check.reason is not None
    assert "primary=loading" in check.reason
    latest_checkpoint = checkpoint_mgr.get_latest_checkpoint(run_id)
    assert latest_checkpoint is not None
    resume_point = ResumePoint(checkpoint=latest_checkpoint, sequence_number=latest_checkpoint.sequence_number)
    with pytest.raises(IncompleteSourceResumeError):
        Orchestrator(db, checkpoint_manager=checkpoint_mgr).resume(
            resume_point=resume_point,
            config=_build_failing_aggregation(
                _BatchInputValidationFailer(),
                source_data=[{"value": 1}, {"value": 2}, {"value": 3}],
                trigger_count=3,
            )[0],
            graph=graph,
            payload_store=FilesystemPayloadStore(tmp_path / "payloads"),
        )

    # Still abandoned after the refused resume — the abandonment sweep is
    # idempotent and the refusal wrote no competing records.
    _assert_all_tokens_abandoned(db, run_id)


class _NonCanonicalEmitter(BaseTransform):
    """Transform emitting NaN — passes output-schema validation, fails hashing."""

    name = "non-canonical-emitter"
    determinism = Determinism.DETERMINISTIC
    plugin_version = "1.0.0"
    source_file_hash: str | None = None
    input_schema = _TestSchema
    output_schema = _TestSchema

    def __init__(self) -> None:
        super().__init__({"schema": {"mode": "observed"}})

    def process(self, row: Any, ctx: Any) -> TransformResult:  # type: ignore[override]
        data = row.to_dict()
        data["value"] = float("nan")
        return TransformResult.success(PipelineRow(data, row.contract), success_reason={"action": "nan"})


def test_transform_canonicalization_violation_records_terminal_outcome() -> None:
    """Canonicalization failure terminalizes through a REAL store, not a mock.

    ``_populate_result_audit_fields`` raises after the plugin has already
    succeeded, so its violation is the last of the executor's contract-violation
    sites to reach the recorder. The unit test for it asserts against a
    MagicMock, which cannot see whether the row actually lands in
    ``token_outcomes`` or whether run accounting agrees — precisely the blind
    spot that let a wrong position ship elsewhere on this ticket. This drives it
    through ``Orchestrator.run`` and the same accounting derivation the web run
    view uses.

    It is the load-bearing test for BOTH halves of the ownership move in
    elspeth-181db83da7: the accounting derivation raises on a duplicate
    terminal outcome (so it catches the executor pre-recording one the router
    also writes), and ``pending == 0`` catches the opposite failure of moving
    ownership to a path that writes none at all.
    """
    db = LandscapeDB.in_memory()
    payload_store = MockPayloadStore()

    source, transforms, sinks, graph = build_linear_pipeline([{"value": 42}], transforms=[_NonCanonicalEmitter()])
    sink = sinks["default"]
    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=transforms,
        sinks={"default": as_sink(sink)},
    )

    Orchestrator(db).run(config, graph=graph, payload_store=payload_store)

    assert sink.results == []
    _assert_routed_token_outcome(db)

    with db.connection() as conn:
        run_id = str(conn.execute(select(runs_table)).fetchall()[0].run_id)
    accounting = load_run_accounting_from_db(db, landscape_run_id=run_id)
    assert accounting.tokens.failed == 1
    assert accounting.tokens.pending == 0
    assert accounting.integrity.duplicate_terminal_outcomes == 0
    assert accounting.integrity.closure == "closed"


def test_sink_input_validation_violation_records_terminal_outcome() -> None:
    """Sink-boundary schema validation must also terminalize the tokens it fails."""
    db = LandscapeDB.in_memory()
    payload_store = MockPayloadStore()

    source, _tx_list, sinks, graph = build_linear_pipeline([{"value": 42}], transforms=[])
    sink = sinks["default"]
    sink.input_schema = _RequiresMissingField

    config = PipelineConfig(
        sources={"primary": as_source(source)},
        transforms=[],
        sinks={"default": as_sink(sink)},
    )

    with pytest.raises(PluginContractViolation, match="input validation failed"):
        Orchestrator(db).run(config, graph=graph, payload_store=payload_store)

    assert sink.results == []
    _assert_failed_token_outcome(db)
