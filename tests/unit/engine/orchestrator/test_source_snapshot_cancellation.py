"""Cancellation cannot erase an emission's validation or quarantine evidence."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.results import SourceRow
from elspeth.contracts.schema_contract import FieldContract, SchemaContract
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator.source_snapshot import capture_source_snapshot_rows, encode_source_snapshot
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run


class _CaptureStopped(Exception):
    pass


@pytest.fixture
def capture_context(tmp_path: Path) -> Iterator[tuple[PluginContext, RecorderSetup]]:
    setup = make_recorder_with_run(payload_store=FilesystemPayloadStore(tmp_path / "payloads"))
    ctx = PluginContext(
        run_id=setup.run_id,
        node_id=setup.source_node_id,
        config={},
        landscape=setup.factory.plugin_audit_writer(),
        coordination_token=setup.coordination_token,
    )
    try:
        yield ctx, setup
    finally:
        setup.db.close()


def _row(value: str, index: int | None) -> SourceRow:
    contract = SchemaContract(
        mode="OBSERVED",
        fields=(FieldContract(normalized_name="query", original_name="query", python_type=str, required=False, source="inferred"),),
        locked=True,
    )
    return SourceRow.valid({"query": value}, contract=contract, source_row_index=index)


@pytest.mark.parametrize("kind", ["missing-index", "duplicate-index", "byte-cap", "missing-quarantine-id"])
def test_emission_validation_wins_over_coincident_cancellation(capture_context: tuple[PluginContext, RecorderSetup], kind: str) -> None:
    ctx, _setup = capture_context
    event = threading.Event()
    rows = [_row("a", 0)]
    expected: type[Exception] = ValueError
    message = "must have source_row_index"
    if kind == "duplicate-index":
        rows = [_row("a", 0), _row("b", 0)]
        expected, message = ValueError, "duplicate source row indexes"
    elif kind == "byte-cap":
        rows = [_row("x" * 1000, 0)]
        expected, message = ValueError, "exceeds the configured byte cap"
    elif kind == "missing-quarantine-id":
        rows = [SourceRow.quarantined(123, error="invalid object", destination="quarantine", source_row_index=0)]
        expected, message = OrchestrationInvariantError, "no recorded validation error"

    def source() -> Iterator[SourceRow]:
        if kind == "missing-index":
            event.set()
            yield _row("a", None)
            return
        for index, row in enumerate(rows):
            if index == len(rows) - 1:
                event.set()
            yield row

    def check() -> None:
        if event.is_set():
            raise _CaptureStopped

    with pytest.raises(expected, match=message):
        encode_source_snapshot(
            capture_source_snapshot_rows(source(), ctx, check_advance=check),
            source_name="primary",
            max_bytes=128 if kind == "byte-cap" else 4096,
        )


@pytest.mark.parametrize("error_type", [ValueError, InterruptedError])
def test_source_exception_identity_wins_over_coincident_cancellation(
    capture_context: tuple[PluginContext, RecorderSetup], error_type: type[Exception]
) -> None:
    ctx, _setup = capture_context
    event = threading.Event()
    failure = error_type("ordinary source failure")

    def source() -> Iterator[SourceRow]:
        yield _row("first", 0)
        event.set()
        raise failure

    def check() -> None:
        if event.is_set():
            raise _CaptureStopped

    with pytest.raises(error_type) as error:
        encode_source_snapshot(capture_source_snapshot_rows(source(), ctx, check_advance=check), source_name="primary", max_bytes=4096)
    assert error.value is failure


def test_quarantine_identities_bind_before_another_advance_and_before_stop(capture_context: tuple[PluginContext, RecorderSetup]) -> None:
    ctx, setup = capture_context
    event = threading.Event()
    recorded_ids: list[str] = []
    captured: list[SourceRow] = []
    advances: list[int] = []

    def source() -> Iterator[SourceRow]:
        for index in (0, 1):
            assert ctx.pop_pending_quarantine_validation_error_id(123) is None
            token = ctx.record_validation_error(123, "invalid object", "observed", "quarantine")
            assert token.error_id is not None
            recorded_ids.append(token.error_id)
            advances.append(index)
            if index == 1:
                event.set()
            yield SourceRow.quarantined(123, error="invalid object", destination="quarantine", source_row_index=index)
        pytest.fail("capture advanced after the cancellation emission")

    def check() -> None:
        if event.is_set():
            raise _CaptureStopped

    def observed() -> Iterator[SourceRow]:
        for row in capture_source_snapshot_rows(source(), ctx, check_advance=check):
            captured.append(row)
            yield row

    with pytest.raises(_CaptureStopped):
        encode_source_snapshot(observed(), source_name="primary", max_bytes=4096)
    assert advances == [0, 1]
    assert [row.validation_error_id for row in captured] == recorded_ids
    assert len(set(recorded_ids)) == 2
    assert ctx.pop_pending_quarantine_validation_error_id(123) is None
    errors = setup.factory.data_flow.get_validation_errors_for_run(setup.run_id)
    assert {error.error_id for error in errors} == set(recorded_ids)


@pytest.mark.parametrize("cancelled", [False, True])
def test_capture_checks_before_acquiring_an_iterator(capture_context: tuple[PluginContext, RecorderSetup], cancelled: bool) -> None:
    ctx, _setup = capture_context
    events: list[str] = []

    class Rows:
        def __iter__(self) -> Iterator[SourceRow]:
            events.append("iterator")
            return iter([_row("a", 0)])

    def check() -> None:
        events.append("check")
        if cancelled:
            raise _CaptureStopped

    capture = capture_source_snapshot_rows(Rows(), ctx, check_advance=check)
    if cancelled:
        with pytest.raises(_CaptureStopped):
            next(capture)
        assert events == ["check"]
    else:
        assert [row.row["query"] for row in capture] == ["a"]
        assert events == ["check", "iterator", "check", "check"]


@pytest.mark.parametrize("exit_kind", ["close", "throw"])
def test_consumer_exit_does_not_run_another_cancellation_check(
    capture_context: tuple[PluginContext, RecorderSetup], exit_kind: str
) -> None:
    ctx, _setup = capture_context
    checks: list[str] = []
    capture = capture_source_snapshot_rows([_row("a", 0)], ctx, check_advance=lambda: checks.append("check"))
    assert next(capture).source_row_index == 0
    failure = ValueError("consumer failure")
    if exit_kind == "close":
        capture.close()
    else:
        with pytest.raises(ValueError) as error:
            capture.throw(failure)
        assert error.value is failure
    assert checks == ["check"]
