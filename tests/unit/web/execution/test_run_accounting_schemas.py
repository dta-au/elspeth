from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from elspeth.web.execution.schemas import (
    CompletedData,
    DiscardStageSummary,
    DiscardSummary,
    ProgressData,
    RunAccounting,
    RunAccountingIntegrity,
    RunAccountingRouting,
    RunAccountingSource,
    RunAccountingTokens,
    RunResultsResponse,
    RunStatusResponse,
    revalidated_with_discard_summary,
)
from elspeth.web.sessions.schemas import RunResponse


def _fanout_accounting() -> RunAccounting:
    return RunAccounting(
        source=RunAccountingSource(rows_processed=1, rows_rejected=0, rows_read=1),
        tokens=RunAccountingTokens(
            emitted=9324,
            terminal=9324,
            succeeded=9323,
            failed=0,
            structural=1,
            pending=0,
        ),
        routing=RunAccountingRouting(
            routed_success=0,
            routed_failure=0,
            quarantined=0,
            discarded=0,
        ),
        integrity=RunAccountingIntegrity(
            closure="closed",
            missing_terminal_outcomes=0,
            duplicate_terminal_outcomes=0,
        ),
    )


def test_run_status_accepts_one_source_row_many_terminal_tokens() -> None:
    response = RunStatusResponse(
        run_id="a2a7354a-5732-475b-a4ac-ed166a9e0f25",
        status="completed",
        started_at=datetime(2026, 5, 6, 14, 30, tzinfo=UTC),
        finished_at=datetime(2026, 5, 6, 14, 31, tzinfo=UTC),
        accounting=_fanout_accounting(),
        error=None,
        landscape_run_id="a2a7354a-5732-475b-a4ac-ed166a9e0f25",
        discard_summary=None,
    )

    assert response.accounting is not None
    assert response.accounting.source.rows_processed == 1
    assert response.accounting.tokens.succeeded == 9323
    assert response.accounting.tokens.structural == 1


def test_run_results_accepts_one_source_row_many_terminal_tokens() -> None:
    response = RunResultsResponse(
        run_id="a2a7354a-5732-475b-a4ac-ed166a9e0f25",
        status="completed",
        accounting=_fanout_accounting(),
        landscape_run_id="a2a7354a-5732-475b-a4ac-ed166a9e0f25",
        error=None,
        discard_summary=None,
    )

    assert response.accounting.tokens.emitted == 9324
    assert response.accounting.tokens.terminal == 9324


def test_completed_event_carries_accounting_instead_of_mixed_rows() -> None:
    event = CompletedData(
        status="completed",
        accounting=_fanout_accounting(),
        landscape_run_id="a2a7354a-5732-475b-a4ac-ed166a9e0f25",
    )

    assert event.accounting.tokens.succeeded == 9323


def test_closed_accounting_requires_all_emitted_tokens_terminal() -> None:
    with pytest.raises(ValidationError, match="closed accounting requires pending == 0"):
        RunAccounting(
            source=RunAccountingSource(rows_processed=1, rows_rejected=0, rows_read=1),
            tokens=RunAccountingTokens(
                emitted=3,
                terminal=2,
                succeeded=2,
                failed=0,
                structural=0,
                pending=1,
            ),
            routing=RunAccountingRouting(
                routed_success=0,
                routed_failure=0,
                quarantined=0,
                discarded=0,
            ),
            integrity=RunAccountingIntegrity(
                closure="closed",
                missing_terminal_outcomes=1,
                duplicate_terminal_outcomes=0,
            ),
        )


def test_completed_status_requires_closed_accounting() -> None:
    accounting = _fanout_accounting().model_copy(
        update={
            "integrity": RunAccountingIntegrity(
                closure="open",
                missing_terminal_outcomes=1,
                duplicate_terminal_outcomes=0,
            ),
            "tokens": RunAccountingTokens(
                emitted=9324,
                terminal=9323,
                succeeded=9323,
                failed=0,
                structural=0,
                pending=1,
            ),
        }
    )

    with pytest.raises(ValidationError, match="status='completed' requires closed token accounting"):
        RunStatusResponse(
            run_id="run-1",
            status="completed",
            started_at=datetime(2026, 5, 6, 14, 30, tzinfo=UTC),
            finished_at=datetime(2026, 5, 6, 14, 31, tzinfo=UTC),
            accounting=accounting,
            error=None,
            landscape_run_id="run-1",
            discard_summary=None,
        )


def _zero_token_accounting(*, rows_processed: int, rows_rejected: int, rows_read: int) -> RunAccounting:
    """The g01 shape: no token was ever emitted; only row-unit counts move."""
    return RunAccounting(
        source=RunAccountingSource(
            rows_processed=rows_processed,
            rows_rejected=rows_rejected,
            rows_read=rows_read,
        ),
        tokens=RunAccountingTokens(
            emitted=0,
            terminal=0,
            succeeded=0,
            failed=0,
            structural=0,
            pending=0,
        ),
        routing=RunAccountingRouting(
            routed_success=0,
            routed_failure=0,
            quarantined=0,
            discarded=0,
        ),
        integrity=RunAccountingIntegrity(
            closure="closed",
            missing_terminal_outcomes=0,
            duplicate_terminal_outcomes=0,
        ),
    )


def _source_validation_discard_summary(count: int) -> DiscardSummary:
    return DiscardSummary(
        total=count,
        validation_errors=count,
        transform_errors=0,
        gate_errors=0,
        sink_discards=0,
        stages=(
            DiscardStageSummary(
                stage="source_validation",
                node_id="source_tickets",
                count=count,
            ),
        ),
    )


def _build_run_status_response(
    accounting: RunAccounting | None,
    discard_summary: DiscardSummary | None,
) -> RunStatusResponse:
    return RunStatusResponse(
        run_id="run-1",
        status="empty",
        started_at=datetime(2026, 8, 6, 8, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 6, 8, 1, tzinfo=UTC),
        accounting=accounting,
        error=None,
        landscape_run_id="landscape-run-1",
        discard_summary=discard_summary,
    )


def _build_run_results_response(
    accounting: RunAccounting | None,
    discard_summary: DiscardSummary | None,
) -> RunResultsResponse:
    return RunResultsResponse(
        run_id="run-1",
        status="empty",
        accounting=accounting,
        landscape_run_id="landscape-run-1",
        error=None,
        discard_summary=discard_summary,
    )


def _build_session_run_response(
    accounting: RunAccounting | None,
    discard_summary: DiscardSummary | None,
) -> RunResponse:
    return RunResponse(
        id="run-1",
        session_id="session-1",
        status="empty",
        accounting=accounting,
        error=None,
        started_at=datetime(2026, 8, 6, 8, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 6, 8, 1, tzinfo=UTC),
        composition_version=1,
        discard_summary=discard_summary,
    )


_CARRIER_BUILDERS = [
    pytest.param(_build_run_status_response, id="RunStatusResponse"),
    pytest.param(_build_run_results_response, id="RunResultsResponse"),
    pytest.param(_build_session_run_response, id="RunResponse"),
]

_CarrierBuilder = Callable[[RunAccounting | None, DiscardSummary | None], object]


def test_rows_read_must_equal_processed_plus_rejected() -> None:
    with pytest.raises(ValidationError, match="rows_read"):
        RunAccountingSource(rows_processed=2, rows_rejected=1, rows_read=4)


@pytest.mark.parametrize("build_carrier", _CARRIER_BUILDERS)
def test_carrier_accepts_reconciled_discard_summary(build_carrier: _CarrierBuilder) -> None:
    carrier = build_carrier(
        _zero_token_accounting(rows_processed=0, rows_rejected=4, rows_read=4),
        _source_validation_discard_summary(4),
    )

    assert carrier.accounting.source.rows_rejected == 4  # type: ignore[attr-defined]
    assert carrier.discard_summary.validation_errors == 4  # type: ignore[attr-defined]


@pytest.mark.parametrize("build_carrier", _CARRIER_BUILDERS)
def test_carrier_rejects_contradictory_discard_summary(build_carrier: _CarrierBuilder) -> None:
    """The original g01 payload — rows_rejected=0 beside validation_errors=4 — must not serialize."""
    with pytest.raises(ValidationError, match="rows_rejected"):
        build_carrier(
            _zero_token_accounting(rows_processed=0, rows_rejected=0, rows_read=0),
            _source_validation_discard_summary(4),
        )


@pytest.mark.parametrize("build_carrier", _CARRIER_BUILDERS)
def test_carrier_accepts_absent_discard_summary(build_carrier: _CarrierBuilder) -> None:
    """No summary attached (no discards, or a carrier that has not attached one yet) stays valid."""
    carrier = build_carrier(
        _zero_token_accounting(rows_processed=0, rows_rejected=4, rows_read=4),
        None,
    )

    assert carrier.discard_summary is None  # type: ignore[attr-defined]


def test_revalidated_attach_helper_rejects_contradictory_summary() -> None:
    """model_copy(update=...) skips validators; the attach helper must not.

    ``get_run_status`` attaches the discard summary to an already-validated
    RunStatusResponse. A plain ``model_copy`` would let the g01 contradiction
    out of exactly that carrier, so the attach path must re-run validation.
    """
    status = _build_run_status_response(
        _zero_token_accounting(rows_processed=0, rows_rejected=0, rows_read=0),
        None,
    )

    with pytest.raises(ValidationError, match="rows_rejected"):
        revalidated_with_discard_summary(status, _source_validation_discard_summary(4))


def test_revalidated_attach_helper_attaches_reconciled_summary() -> None:
    status = _build_run_status_response(
        _zero_token_accounting(rows_processed=0, rows_rejected=4, rows_read=4),
        None,
    )

    attached = revalidated_with_discard_summary(status, _source_validation_discard_summary(4))

    assert attached.discard_summary is not None
    assert attached.discard_summary.total == 4
    assert attached.run_id == status.run_id


def test_progress_event_uses_explicit_source_and_token_names() -> None:
    progress = ProgressData(
        source_rows_processed=1,
        tokens_succeeded=9323,
        tokens_failed=0,
        tokens_quarantined=0,
        tokens_routed_success=0,
        tokens_routed_failure=0,
    )

    payload = progress.model_dump()

    assert payload["source_rows_processed"] == 1
    assert payload["tokens_succeeded"] == 9323
    assert "rows_processed" not in payload
