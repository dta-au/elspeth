"""Retained queue policy bounds and the synchronous transport cap."""

from __future__ import annotations

import pytest

from elspeth.web.async_workers import MAX_WORKERS
from elspeth.web.config import WebSettings
from tests.unit.web.test_config import _settings


@pytest.mark.parametrize(
    "name,value",
    [
        ("composer_async_max_queued_operations", 0),
        ("composer_async_max_queued_operations", 10001),
        ("composer_async_worker_concurrency", 0),
        ("composer_async_worker_concurrency", 17),
        ("composer_async_claim_lease_seconds", 4),
        ("composer_async_claim_lease_seconds", 601),
        ("composer_async_scan_interval_seconds", 0),
        ("composer_async_scan_interval_seconds", 61),
        ("composer_async_poll_after_ms", 99),
        ("composer_async_poll_after_ms", 60001),
        ("composer_async_drain_seconds", 0),
        ("composer_async_drain_seconds", 121),
    ],
)
def test_invalid_worker_policy_bounds(name, value) -> None:
    with pytest.raises(ValueError):
        _settings(**{name: value})


def test_defaults_share_sync_worker_capacity_and_sync_cap() -> None:
    settings = _settings()
    assert settings.composer_async_worker_concurrency == 4
    assert WebSettings.model_fields["composer_async_worker_concurrency"].metadata[-1].le == MAX_WORKERS
    assert settings.composer_async_max_queued_operations == 64
    assert settings.composer_sync_timeout_seconds == settings.composer_timeout_seconds
    # model_copy skips validators so this isolates the derived cap while N16 owns decoupling.
    uncoupled = settings.model_copy(
        update={
            "composer_timeout_seconds": 600.0,
            "composer_transport_idle_ceiling_seconds": 300.0,
            "composer_transport_headroom_seconds": 30.0,
        }
    )
    assert uncoupled.composer_sync_timeout_seconds == 270.0
