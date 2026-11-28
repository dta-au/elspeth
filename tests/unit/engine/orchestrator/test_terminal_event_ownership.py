"""Terminal run event ownership tests."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from elspeth.cli_formatters import create_console_formatters, create_json_formatters
from elspeth.contracts import Determinism, PipelineRow, RunStatus
from elspeth.contracts.enums import TerminalPath
from elspeth.contracts.errors import AuditIntegrityError, GracefulShutdownError
from elspeth.contracts.events import RunCompletionStatus, RunFinished, RunSummary
from elspeth.contracts.run_result import RunResult
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import runs_table, token_outcomes_table
from elspeth.engine._best_effort import best_effort
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.ceremony import RunCeremony
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.testing import make_pipeline_row
from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
from tests.fixtures.exception_coalesce_consumer import make_diagnostic_recorder
from tests.fixtures.pipeline import build_linear_pipeline
from tests.fixtures.stores import MockPayloadStore


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def subscribe(self, event_type: type, handler: Any) -> None:
        pass

    def emit(self, event: Any) -> None:
        self.events.append(event)


class RecordingTelemetry:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def handle_event(self, event: Any) -> None:
        self.events.append(event)

    def flush(self) -> None:
        pass


def test_run_ceremony_emits_terminal_run_events() -> None:
    event_bus = RecordingEvents()
    telemetry = RecordingTelemetry()
    ceremony = RunCeremony(events=event_bus, telemetry=telemetry)
    result = RunResult(
        run_id="run-1",
        status=RunStatus.COMPLETED,
        rows_processed=2,
        rows_succeeded=2,
        rows_failed=0,
        rows_routed_success=1,
        rows_routed_failure=0,
        routed_destinations={"success_sink": 1},
    )

    ceremony.emit_run_finished(
        run_id=result.run_id,
        status=result.status,
        row_count=result.rows_processed,
        duration_seconds=1.25,
    )
    ceremony.emit_run_summary(
        run_id=result.run_id,
        status=RunCompletionStatus.COMPLETED,
        rows_processed=result.rows_processed,
        rows_succeeded=result.rows_succeeded,
        rows_failed=result.rows_failed,
        rows_quarantined=result.rows_quarantined,
        duration_seconds=1.25,
        exit_code=0,
        rows_routed_success=result.rows_routed_success,
        rows_routed_failure=result.rows_routed_failure,
        routed_destinations=result.routed_destinations,
    )

    assert isinstance(telemetry.events[-1], RunFinished)
    assert telemetry.events[-1].run_id == "run-1"
    assert telemetry.events[-1].duration_ms == 1250

    assert isinstance(event_bus.events[-1], RunSummary)
    assert event_bus.events[-1].run_id == "run-1"
    assert event_bus.events[-1].status == RunCompletionStatus.COMPLETED
    assert event_bus.events[-1].routed_destinations == (("success_sink", 1),)


def test_run_ceremony_emits_partial_summary_from_run_result(monkeypatch: pytest.MonkeyPatch) -> None:
    event_bus = RecordingEvents()
    ceremony = RunCeremony(events=event_bus, telemetry=None)
    result = RunResult(
        run_id="run-partial",
        status=RunStatus.COMPLETED_WITH_FAILURES,
        rows_processed=3,
        rows_succeeded=2,
        rows_failed=1,
        rows_quarantined=1,
        rows_routed_success=2,
        rows_routed_failure=1,
        routed_destinations={"default": 2, "quarantine": 1},
        collector_groups_failed=2,
    )

    class _FrozenClockTime:
        """Stand-in for the ``time`` name as seen from inside
        ``elspeth.engine.orchestrator.ceremony``, scoped to that module only.

        ``monkeypatch.setattr("...ceremony.time.perf_counter", ...)`` would
        resolve ``...ceremony.time`` to the *real* ``time`` module (Python
        modules are process-wide singletons; the ceremony module merely
        imports the same object everyone else does) and replace
        ``time.perf_counter`` for the whole process during the test.
        Rebinding the ``time`` *name inside the ceremony module's own
        namespace* keeps the freeze local to the code path under test;
        ``perf_counter`` is the only member the ceremony module reads from
        ``time``, so it is the only member this stand-in defines: any other
        name raises ``AttributeError`` and fails the test loudly rather than
        being forwarded to the real module behind the test's back.
        """

        perf_counter = staticmethod(lambda: 42.5)

    monkeypatch.setattr("elspeth.engine.orchestrator.ceremony.time", _FrozenClockTime())

    ceremony.emit_partial_summary(
        run_id=result.run_id,
        result=result,
        start_time=40.0,
    )

    assert len(event_bus.events) == 1
    summary = event_bus.events[0]
    assert isinstance(summary, RunSummary)
    assert summary.run_id == "run-partial"
    assert summary.status == RunCompletionStatus.PARTIAL
    assert summary.exit_code == 1
    assert summary.total_rows == 3
    assert summary.succeeded == 2
    assert summary.failed == 1
    assert summary.quarantined == 1
    assert summary.collector_groups_failed == 2
    assert summary.duration_seconds == 2.5
    assert summary.routed_success == 2
    assert summary.routed_failure == 1
    assert summary.routed_destinations == (("default", 2), ("quarantine", 1))


def test_interrupted_summary_reads_durable_collector_failure_count() -> None:
    setup = make_diagnostic_recorder(run_id="run-interrupted-groups")
    try:
        event_bus = RecordingEvents()
        ceremony = RunCeremony(events=event_bus, telemetry=None)
        shutdown = GracefulShutdownError(rows_processed=0, run_id=setup.run_id)
        with patch.object(setup.factory.run_status_projection, "count_failed_collector_groups", return_value=2) as count_failed:
            ceremony.emit_interrupted_ceremony(
                setup.run_id,
                setup.factory,
                shutdown,
                0.0,
                coordination_token=setup.coordination_token,
            )

        count_failed.assert_called_once_with(setup.run_id)
        summary = event_bus.events[-1]
        assert isinstance(summary, RunSummary)
        assert summary.status is RunCompletionStatus.INTERRUPTED
        assert summary.failed == 0
        assert summary.collector_groups_failed == 2
    finally:
        setup.db.close()


def _live_failed_result(run_id: str) -> RunResult:
    return RunResult(
        run_id=run_id,
        status=RunStatus.FAILED,
        rows_processed=3,
        rows_succeeded=3,
        rows_failed=0,
        rows_routed_success=1,
        rows_routed_failure=0,
        routed_destinations={"default": 1},
    )


def test_failed_summary_derivation_failure_preserves_failed_stamp_and_reports_unknown_outcome_counts() -> None:
    setup = make_diagnostic_recorder(run_id="run-failed-summary-derive")
    try:
        event_bus = RecordingEvents()
        ceremony = RunCeremony(events=event_bus, telemetry=None)
        derive_calls: list[tuple[str, RunStatus | None, bool]] = []

        def fail_after_stamp(factory: object, run_id: str, *, allow_finalization_abandonment: bool) -> None:
            run = setup.factory.run_lifecycle.get_run(run_id)
            derive_calls.append((run_id, None if run is None else run.status, allow_finalization_abandonment))
            raise RuntimeError("injected audit projection failure")

        with patch("elspeth.engine.orchestrator.ceremony.derive_terminal_status_from_audit", side_effect=fail_after_stamp):
            ceremony.emit_failed_ceremony(
                setup.run_id, setup.factory, 0.0, _live_failed_result(setup.run_id), coordination_token=setup.coordination_token
            )

        # The FAILED stamp was durable before the summary derive ran.
        assert derive_calls == [(setup.run_id, RunStatus.FAILED, True)]
        summary = event_bus.events[-1]
        assert isinstance(summary, RunSummary)
        assert summary.status is RunCompletionStatus.FAILED
        assert summary.total_rows == 3
        assert summary.outcome_counts_known is False
        assert summary.succeeded == summary.failed == summary.quarantined == 0
        assert summary.routed_success == summary.routed_failure == 0
        assert summary.routed_destinations == ()
        with patch("elspeth.cli_formatters.typer.echo") as echo:
            create_console_formatters(prefix="Run")[RunSummary](summary)
        rendered = echo.call_args.args[0]
        assert rendered.startswith("\n✗ Run FAILED: 3 rows processed | outcome counts unknown (audit summary unavailable) | ")
        assert "✓" not in rendered
    finally:
        setup.db.close()


def test_failed_summary_derivation_audit_corruption_escapes_after_failed_stamp() -> None:
    """A Tier-1 derive error ends the ceremony where it is raised, as a Tier-1
    ``finalize_run`` error does: no RunFinished and no RunSummary follow it."""
    setup = make_diagnostic_recorder(run_id="run-failed-summary-tier1")
    try:
        event_bus = RecordingEvents()
        telemetry = RecordingTelemetry()
        ceremony = RunCeremony(events=event_bus, telemetry=telemetry)

        with (
            patch(
                "elspeth.engine.orchestrator.ceremony.derive_terminal_status_from_audit",
                side_effect=AuditIntegrityError("injected audit contradiction"),
            ),
            pytest.raises(AuditIntegrityError, match="injected audit contradiction"),
            best_effort("Generic failure ceremony on run failure", run_id=setup.run_id),
        ):
            ceremony.emit_failed_ceremony(
                setup.run_id, setup.factory, 0.0, _live_failed_result(setup.run_id), coordination_token=setup.coordination_token
            )

        run = setup.factory.run_lifecycle.get_run(setup.run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert event_bus.events == []
        assert telemetry.events == []
    finally:
        setup.db.close()


def test_failed_summary_without_live_result_reports_rows_processed_unknown_when_derive_fails() -> None:
    """No live result and no audit summary: rows processed is unknown, never 0."""
    setup = make_diagnostic_recorder(run_id="run-failed-summary-no-live")
    try:
        event_bus = RecordingEvents()
        telemetry = RecordingTelemetry()
        ceremony = RunCeremony(events=event_bus, telemetry=telemetry)

        with patch(
            "elspeth.engine.orchestrator.ceremony.derive_terminal_status_from_audit",
            side_effect=RuntimeError("injected audit projection failure"),
        ):
            ceremony.emit_failed_ceremony(setup.run_id, setup.factory, 0.0, coordination_token=setup.coordination_token)

        run = setup.factory.run_lifecycle.get_run(setup.run_id)
        assert run is not None
        assert run.status is RunStatus.FAILED
        summary = event_bus.events[-1]
        assert isinstance(summary, RunSummary)
        assert summary.total_rows_known is False
        assert summary.outcome_counts_known is False
        assert summary.total_rows == 0
        with patch("elspeth.cli_formatters.typer.echo") as echo:
            create_console_formatters(prefix="Run")[RunSummary](summary)
        rendered = echo.call_args.args[0]
        assert rendered.startswith("\n✗ Run FAILED: rows processed unknown | outcome counts unknown (audit summary unavailable) | ")
        assert "0 rows processed" not in rendered
        with patch("elspeth.cli_formatters.typer.echo") as echo:
            create_json_formatters()[RunSummary](summary)
        payload = json.loads(echo.call_args.args[0])
        assert payload["total_rows"] is None
        assert payload["succeeded"] is None
        finished = [event for event in telemetry.events if isinstance(event, RunFinished)]
        assert len(finished) == 1
        assert finished[0].status is RunStatus.FAILED
    finally:
        setup.db.close()


class _FailOnSecondRowTransform(BaseTransform):
    """Passes the first row, then crashes the run on the second."""

    name = "fail_on_second_row"
    determinism = Determinism.DETERMINISTIC
    input_schema = _TestSchema
    output_schema = _TestSchema

    def __init__(self) -> None:
        super().__init__({"schema": {"mode": "observed"}})
        self._count = 0

    def process(self, row: PipelineRow, ctx: Any) -> TransformResult:
        self._count += 1
        if self._count == 2:
            raise RuntimeError("boom on second row")
        return TransformResult.success(make_pipeline_row(dict(row)), success_reason={"action": "passthrough"})


def test_failed_run_summary_reads_audit_after_finalization_abandons_undecided_tokens() -> None:
    """A real FAILED run with no checkpoint is non-resumable, so its FAILED stamp
    writes ABANDONED records in the same transaction; the summary derive reads
    them afterwards and reports confirmed outcomes."""
    db = LandscapeDB.in_memory()
    try:
        event_bus = RecordingEvents()
        transform = _FailOnSecondRowTransform()
        source, _transforms, sinks, graph = build_linear_pipeline([{"value": 1}, {"value": 2}], transforms=[transform])
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"default": as_sink(sinks["default"])},
        )

        with pytest.raises(RuntimeError, match="boom on second row"):
            Orchestrator(db, event_bus=event_bus).run(config, graph=graph, payload_store=MockPayloadStore())

        with db.engine.connect() as connection:
            statuses = connection.execute(select(runs_table.c.status)).scalars().all()
            abandoned = connection.execute(
                select(token_outcomes_table.c.token_id).where(token_outcomes_table.c.path == TerminalPath.ABANDONED.value)
            ).all()
        assert statuses == [RunStatus.FAILED.value]
        assert len(abandoned) >= 1
        summaries = [event for event in event_bus.events if isinstance(event, RunSummary)]
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary.status is RunCompletionStatus.FAILED
        assert summary.outcome_counts_known is True
        assert summary.total_rows == 2
        assert summary.succeeded == summary.failed == summary.quarantined == 0
    finally:
        db.close()


def test_failed_run_summary_without_live_result_reads_rows_processed_from_audit() -> None:
    """The generic failure arm passes no live result. Here every row reached its
    sink before the failure, so the audit summary is readable and rows processed
    comes from it, not a placeholder 0."""
    db = LandscapeDB.in_memory()
    try:
        event_bus = RecordingEvents()
        telemetry = RecordingTelemetry()
        source, _transforms, sinks, graph = build_linear_pipeline([{"value": 1}, {"value": 2}])
        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[],
            sinks={"default": as_sink(sinks["default"])},
        )

        with (
            patch(
                "elspeth.engine.orchestrator.run_lifecycle.assert_terminal_counter_parity",
                side_effect=RuntimeError("injected failure after processing"),
            ),
            pytest.raises(RuntimeError, match="injected failure after processing"),
        ):
            Orchestrator(db, event_bus=event_bus, telemetry_manager=telemetry).run(config, graph=graph, payload_store=MockPayloadStore())

        with db.engine.connect() as connection:
            statuses = connection.execute(select(runs_table.c.status)).scalars().all()
        assert statuses == [RunStatus.FAILED.value]
        summaries = [event for event in event_bus.events if isinstance(event, RunSummary)]
        assert len(summaries) == 1
        summary = summaries[0]
        assert summary.status is RunCompletionStatus.FAILED
        assert summary.outcome_counts_known is True
        assert summary.total_rows_known is True
        assert summary.total_rows == 2
        assert summary.succeeded == 2
        finished = [event for event in telemetry.events if isinstance(event, RunFinished)]
        assert len(finished) == 1
        assert finished[0].status is RunStatus.FAILED
        assert finished[0].row_count == 2
    finally:
        db.close()


def test_only_the_failed_summary_relaxes_the_finalization_abandonment_belt() -> None:
    """ADR-038's ABANDONED belt is relaxed for exactly one production reader:
    the FAILED summary derive in ``RunCeremony``. Any other ``src`` call that
    passes ``allow_finalization_abandonment`` (under either derive name) fails,
    and so does any call to either derive name that unpacks ``**`` keywords,
    because an unpacked mapping can carry the flag without naming it."""
    repo_root = Path(__file__).parents[4]
    src_root = repo_root / "src/elspeth"
    derive_names = {"derive_terminal_status_from_audit", "derive_resume_terminal_status_from_audit"}
    callers: list[tuple[str, str, object]] = []
    unpacked: list[tuple[str, str]] = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        relative = str(path.relative_to(repo_root))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_name = node.func.id if isinstance(node.func, ast.Name) else ast.unparse(node.func)
            for keyword in node.keywords:
                if keyword.arg is None and func_name.rsplit(".", 1)[-1] in derive_names:
                    unpacked.append((relative, func_name))
                if keyword.arg != "allow_finalization_abandonment":
                    continue
                value = keyword.value.value if isinstance(keyword.value, ast.Constant) else ast.unparse(keyword.value)
                callers.append((relative, func_name, value))

    assert callers == [("src/elspeth/engine/orchestrator/ceremony.py", "derive_terminal_status_from_audit", True)]
    assert unpacked == []


def test_only_the_ceremony_reads_the_relaxed_failed_summary_derive() -> None:
    """The relaxed derive is wrapped by ``_failed_result_from_audit``; a second
    reader of that wrapper would inherit the relaxation without passing the
    keyword, so every reference to it must stay inside ``ceremony.py``: one
    definition and one call."""
    repo_root = Path(__file__).parents[4]
    src_root = repo_root / "src/elspeth"
    wrapper = "_failed_result_from_audit"
    references: list[tuple[str, str]] = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        relative = str(path.relative_to(repo_root))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == wrapper:
                references.append((relative, "def"))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == wrapper:
                references.append((relative, "call"))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == wrapper:
                references.append((relative, "attribute call"))
            elif isinstance(node, ast.ImportFrom) and any(alias.name == wrapper for alias in node.names):
                references.append((relative, "import"))
            elif isinstance(node, ast.Name) and node.id == wrapper and not isinstance(node.ctx, ast.Store):
                references.append((relative, "name"))
            elif isinstance(node, ast.Attribute) and node.attr == wrapper:
                references.append((relative, "attribute"))

    ceremony = "src/elspeth/engine/orchestrator/ceremony.py"
    assert sorted(references) == [(ceremony, "call"), (ceremony, "def"), (ceremony, "name")]


def test_run_and_resume_coordinators_do_not_construct_terminal_events_directly() -> None:
    repo_root = Path(__file__).parents[4]
    checked_paths = (
        repo_root / "src/elspeth/engine/orchestrator/run_lifecycle.py",
        repo_root / "src/elspeth/engine/orchestrator/resume.py",
    )
    offenders: list[str] = []
    for path in checked_paths:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"RunFinished", "RunSummary"}:
                offenders.append(f"{path.relative_to(repo_root)}:{node.lineno}:{node.func.id}")

    assert offenders == []


def test_run_lifecycle_uses_partial_summary_helper() -> None:
    repo_root = Path(__file__).parents[4]
    path = repo_root / "src/elspeth/engine/orchestrator/run_lifecycle.py"
    tree = ast.parse(path.read_text(), filename=str(path))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "emit_run_summary":
            continue
        for keyword in node.keywords:
            if keyword.arg == "status" and isinstance(keyword.value, ast.Attribute) and keyword.value.attr == "PARTIAL":
                offenders.append(f"{path.relative_to(repo_root)}:{node.lineno}:emit_run_summary(PARTIAL)")

    assert offenders == []
