"""Unit tests for durable scheduler contract value objects."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import MappingProxyType

import pytest

from elspeth.contracts.scheduler import (
    BufferedOutcomeSpec,
    GroupLossSpec,
    SchedulerEvent,
    SchedulerEventType,
    TokenWorkStatus,
)


def test_buffered_outcome_context_is_frozen_after_construction() -> None:
    source_context = {"branch": "left", "nested": {"attempt": 1}}
    spec = BufferedOutcomeSpec(batch_id="batch-1", context=source_context)

    assert isinstance(spec.context, MappingProxyType)
    assert isinstance(spec.context["nested"], MappingProxyType)

    source_context["branch"] = "right"
    assert spec.context["branch"] == "left"

    with pytest.raises(TypeError):
        spec.context["branch"] = "right"  # type: ignore[index]


def test_scheduler_event_rejects_missing_required_event_type() -> None:
    with pytest.raises(TypeError, match="event_type must be SchedulerEventType"):
        SchedulerEvent(
            event_id="event-1",
            run_id="run-1",
            token_id="token-1",
            work_item_id="work-1",
            event_type=None,  # type: ignore[arg-type]
            to_status=TokenWorkStatus.READY,
            to_attempt=1,
            recorded_at=datetime.now(UTC),
        )


def test_scheduler_event_rejects_missing_required_to_status() -> None:
    with pytest.raises(TypeError, match="to_status must be TokenWorkStatus"):
        SchedulerEvent(
            event_id="event-1",
            run_id="run-1",
            token_id="token-1",
            work_item_id="work-1",
            event_type=SchedulerEventType.ENQUEUE,
            to_status=None,  # type: ignore[arg-type]
            to_attempt=1,
            recorded_at=datetime.now(UTC),
        )


def test_scheduler_event_allows_missing_optional_from_status() -> None:
    event = SchedulerEvent(
        event_id="event-1",
        run_id="run-1",
        token_id="token-1",
        work_item_id="work-1",
        event_type=SchedulerEventType.ENQUEUE,
        from_status=None,
        to_status=TokenWorkStatus.READY,
        to_attempt=1,
        recorded_at=datetime.now(UTC),
    )

    assert event.from_status is None


class TestGroupLossSpec:
    def test_construction_and_field_order(self) -> None:
        spec = GroupLossSpec(
            closer_name="merge_paths",
            group_id="fg-1",
            member_key="path_c",
            token_id="tok-3",
            reason="dropped_by_filter",
        )
        assert (spec.closer_name, spec.group_id, spec.member_key, spec.token_id, spec.reason) == (
            "merge_paths",
            "fg-1",
            "path_c",
            "tok-3",
            "dropped_by_filter",
        )

    def test_frozen(self) -> None:
        spec = GroupLossSpec(closer_name="c", group_id="g", member_key="m", token_id="t", reason="r")
        with pytest.raises(FrozenInstanceError):
            spec.reason = "other"  # type: ignore[misc]
