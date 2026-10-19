"""Run chronology keeps a database UTC anchor and caches the first outcome."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.execution import provider_clock
from elspeth.web.execution.provider_clock import RunProviderAttemptClock


def test_first_outcome_stamp_crosses_utc_midnight_and_is_stable_under_concurrent_reclassification(monkeypatch) -> None:
    ticks = Mock(side_effect=[100_000_000_000, 103_500_000_000])
    monkeypatch.setattr(provider_clock, "monotonic_ns", ticks)
    admitted = datetime(2026, 10, 19, 23, 59, 58, tzinfo=UTC)
    clock = RunProviderAttemptClock()
    clock.bind(ProviderAttempt("first", admitted))
    with ThreadPoolExecutor(max_workers=4) as workers:
        outcomes = tuple(workers.map(clock.timestamp_for_outcome, ["first"] * 8))
    assert outcomes == (admitted + timedelta(seconds=3.5),) * 8
    assert outcomes[0].day == 20
    assert ticks.call_count == 2


def test_separate_attempts_cannot_share_or_replace_clock_binding(monkeypatch) -> None:
    ticks = Mock(side_effect=[10_000_000_000, 20_000_000_000, 21_000_000_000, 22_000_000_000, 23_000_000_000])
    monkeypatch.setattr(provider_clock, "monotonic_ns", ticks)
    started = datetime(2026, 10, 19, tzinfo=UTC)
    clock = RunProviderAttemptClock()
    clock.bind(ProviderAttempt("one", started))
    clock.bind(ProviderAttempt("two", started + timedelta(seconds=10)))
    assert clock.timestamp_for_outcome("two") == started + timedelta(seconds=11)
    assert clock.timestamp_for_outcome("one") == started + timedelta(seconds=12)
    with pytest.raises(AuditIntegrityError, match="bound more than once"):
        clock.bind(ProviderAttempt("one", started))
    with pytest.raises(AuditIntegrityError, match="no admitted clock binding"):
        clock.timestamp_for_outcome("unknown")
    with pytest.raises(AuditIntegrityError, match="no admitted clock binding"):
        RunProviderAttemptClock().timestamp_for_outcome("one")


def test_invalid_admission_and_backwards_monotonic_clock_fail_closed(monkeypatch) -> None:
    clock = RunProviderAttemptClock()
    with pytest.raises(AuditIntegrityError, match="aware timestamp"):
        clock.bind(ProviderAttempt("naive", datetime(2026, 10, 19, tzinfo=UTC).replace(tzinfo=None)))
    ticks = Mock(side_effect=[20, 10])
    monkeypatch.setattr(provider_clock, "monotonic_ns", ticks)
    clock.bind(ProviderAttempt("one", datetime(2026, 10, 19, tzinfo=UTC)))
    with pytest.raises(AuditIntegrityError, match="moved backwards"):
        clock.timestamp_for_outcome("one")
