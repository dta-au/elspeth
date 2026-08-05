"""Regression tests for narrow recorder-failure wrapping on audit helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

import pytest

from elspeth.contracts.declaration_contracts import (
    DeclarationContractViolation,
    _attach_contract_name_from_dispatcher,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.engine.executors import SinkExecutor, TransformExecutor
from elspeth.engine.spans import SpanFactory
from elspeth.testing import make_token_info


class _ViolationPayload(TypedDict):
    missing: list[str]


class _TestBoundaryViolation(DeclarationContractViolation):
    payload_schema = _ViolationPayload


@dataclass(frozen=True)
class _TransformStub:
    name: str = "test-transform"
    node_id: str = "node-1"


class _RecordingDataFlow:
    def __init__(self, *, record_error: LandscapeRecordError | None = None) -> None:
        self._record_error = record_error
        self.token_outcomes: list[dict[str, object]] = []

    def record_token_outcome(self, **kwargs: object) -> None:
        if self._record_error is not None:
            raise self._record_error
        self.token_outcomes.append(kwargs)


def _make_violation(*, token_id: str, row_id: str) -> _TestBoundaryViolation:
    return _TestBoundaryViolation(
        plugin="test-plugin",
        node_id="node-1",
        run_id="run-1",
        row_id=row_id,
        token_id=token_id,
        payload={"missing": ["customer_id"]},
        message="boundary violation",
    )


def test_transform_terminal_contract_failure_keeps_to_audit_dict_bug_visible() -> None:
    """Transform helper must not relabel declaration-payload regressions as recorder failures."""
    data_flow = _RecordingDataFlow()
    executor = TransformExecutor(
        execution=object(),
        span_factory=SpanFactory(),
        step_resolver=lambda _node_id: 1,
        data_flow=data_flow,
    )
    transform = _TransformStub()
    token = make_token_info(token_id="token-1", row_id="row-1")
    violation = _make_violation(token_id=token.token_id, row_id=token.row_id)

    with pytest.raises(RuntimeError, match="contract_name accessed before"):
        executor._record_terminal_contract_failure(
            transform=transform,
            token=token,
            run_id="run-1",
            violation=violation,
        )

    assert data_flow.token_outcomes == []


def test_transform_terminal_contract_failure_wraps_typed_recorder_failures() -> None:
    """Transform helper still upgrades durable recorder failures to AuditIntegrityError."""
    data_flow = _RecordingDataFlow(record_error=LandscapeRecordError("audit DB down"))
    executor = TransformExecutor(
        execution=object(),
        span_factory=SpanFactory(),
        step_resolver=lambda _node_id: 1,
        data_flow=data_flow,
    )
    transform = _TransformStub()
    token = make_token_info(token_id="token-1", row_id="row-1")
    violation = _make_violation(token_id=token.token_id, row_id=token.row_id)
    _attach_contract_name_from_dispatcher(violation, "test_contract")

    with pytest.raises(AuditIntegrityError, match="Recorder failure: LandscapeRecordError: audit DB down"):
        executor._record_terminal_contract_failure(
            transform=transform,
            token=token,
            run_id="run-1",
            violation=violation,
        )


def test_sink_boundary_failure_outcomes_keep_non_recorder_bug_visible() -> None:
    """Sink helper must not relabel serializer/type bugs as recorder failures."""
    data_flow = _RecordingDataFlow(record_error=ValueError("serializer bug"))
    executor = SinkExecutor(
        execution=object(),
        data_flow=data_flow,
        span_factory=SpanFactory(),
        run_id="run-1",
    )
    token = make_token_info(token_id="token-1", row_id="row-1")
    violation = _make_violation(token_id=token.token_id, row_id=token.row_id)
    _attach_contract_name_from_dispatcher(violation, "test_contract")

    with pytest.raises(ValueError, match="serializer bug"):
        executor._record_boundary_failure_outcomes(
            tokens=[token],
            sink_name="output",
            phase="boundary_check",
            violation=violation,
        )


def test_sink_boundary_failure_outcomes_wrap_typed_recorder_failures() -> None:
    """Sink helper still upgrades durable recorder failures to AuditIntegrityError."""
    data_flow = _RecordingDataFlow(record_error=LandscapeRecordError("audit DB down"))
    executor = SinkExecutor(
        execution=object(),
        data_flow=data_flow,
        span_factory=SpanFactory(),
        run_id="run-1",
    )
    token = make_token_info(token_id="token-1", row_id="row-1")
    violation = _make_violation(token_id=token.token_id, row_id=token.row_id)
    _attach_contract_name_from_dispatcher(violation, "test_contract")

    with pytest.raises(AuditIntegrityError, match="Recorder failure: LandscapeRecordError: audit DB down"):
        executor._record_boundary_failure_outcomes(
            tokens=[token],
            sink_name="output",
            phase="boundary_check",
            violation=violation,
        )


def test_sink_boundary_failure_outcomes_terminalize_every_token_in_the_batch() -> None:
    """EVERY token in a failing batch gets a terminal outcome, not just the first.

    Sinks write batches; a boundary violation fails the whole batch. Recording
    an outcome for only some of its tokens leaves the rest pending forever on a
    FAILED run — the "emitted=N, terminal=N-k" contradiction elspeth-82d4c5146c
    and elspeth-207c9fbb0b exist to prevent, reintroduced for any batch of more
    than one row.

    Every other test of this helper uses a single-token batch, so truncating the
    loop (``for token in tokens[:1]``) passes the entire suite. This is the case
    that discriminates.
    """
    data_flow = _RecordingDataFlow()
    executor = SinkExecutor(
        execution=object(),
        data_flow=data_flow,
        span_factory=SpanFactory(),
        run_id="run-1",
    )
    tokens = [make_token_info(token_id=f"token-{i}", row_id=f"row-{i}") for i in range(1, 4)]
    # The violation names ONE failing token; the whole batch still fails.
    violation = _make_violation(token_id=tokens[0].token_id, row_id=tokens[0].row_id)
    _attach_contract_name_from_dispatcher(violation, "test_contract")

    executor._record_boundary_failure_outcomes(
        tokens=tokens,
        sink_name="output",
        phase="boundary_check",
        violation=violation,
    )

    recorded = {call["ref"].token_id for call in data_flow.token_outcomes}
    assert recorded == {"token-1", "token-2", "token-3"}
    # Each token's context is rebuilt for its own row, not the triggering one.
    by_token = {call["ref"].token_id: call["context"] for call in data_flow.token_outcomes}
    assert by_token["token-2"]["token_id"] == "token-2"
    assert by_token["token-2"]["row_id"] == "row-2"
    assert by_token["token-2"]["failing_token_id"] == "token-1"
