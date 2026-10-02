"""Coordination event labels keep their value after caller mutation."""

from datetime import UTC, datetime

import pytest

from elspeth.core.landscape.run_coordination_repository import CoordinationEventRow


def test_coordination_event_detaches_and_freezes_context() -> None:
    labels = {"reason": "departed"}
    event = CoordinationEventRow(
        event_type="worker_depart", worker_id="worker", leader_epoch=None, recorded_at=datetime.now(UTC), context=labels
    )
    labels["reason"] = "changed"
    assert event.context == {"reason": "departed"}
    with pytest.raises(TypeError):
        dict.__setitem__(event.context, "reason", "changed")


def test_coordination_event_absent_context_remains_absent() -> None:
    event = CoordinationEventRow(event_type="worker_depart", worker_id="worker", leader_epoch=None, recorded_at=datetime.now(UTC))
    assert event.context is None
