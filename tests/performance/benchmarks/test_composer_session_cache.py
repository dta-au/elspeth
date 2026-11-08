"""Repeatable local work/latency baseline for the Composer session-cache rollout.

Select explicitly with ``-m performance -n 0``. Timing is reported by the
benchmark plugin; correctness and compute-count controls live in ordinary
unit tests. Graphs are already hydrated: these measure local analysis, not
request/database/provider latency. No absolute latency promise is made.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Literal

import pytest

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.analysis import ComposerAnalysis
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.session_cache import ComposerSessionCacheRegistry, SessionCacheKey
from tests.fixtures.composer_session_cache import (
    SCENARIO_NAMES,
    ScenarioName,
    count_composer_work,
    make_composer_validation_scenario,
    run_validation,
    validation_for_analysis,
)

if TYPE_CHECKING:
    from pytest_benchmark.fixture import BenchmarkFixture


@pytest.mark.performance
@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_composer_validation_reference(benchmark: BenchmarkFixture, name: ScenarioName) -> None:
    scenario = make_composer_validation_scenario(name)
    expected = run_validation(scenario)
    assert expected.is_valid is scenario.expected_valid
    manager = get_shared_plugin_manager()
    with count_composer_work() as counts:
        measured = run_validation(scenario)
    assert measured == expected
    benchmark.extra_info.update(
        {
            "fixture": name,
            "mode": "uncached_reference",
            "state_content_hash": composition_content_hash(scenario.state),
            "graph_nodes": len(scenario.state.nodes),
            "expected_valid": scenario.expected_valid,
            "measured_work": asdict(counts),
            "live_registry": {
                "sources": len(manager.get_sources()),
                "transforms": len(manager.get_transforms()),
                "sinks": len(manager.get_sinks()),
            },
        }
    )
    result = benchmark.pedantic(run_validation, args=(scenario,), rounds=5, iterations=1, warmup_rounds=1)
    assert result == expected


@pytest.mark.performance
def test_composer_catalog_listing_reference(benchmark: BenchmarkFixture) -> None:
    catalog = CatalogServiceImpl(get_shared_plugin_manager())

    def list_catalog():
        return (*catalog.list_sources(), *catalog.list_transforms(), *catalog.list_sinks())

    expected = list_catalog()
    with count_composer_work() as counts:
        measured = list_catalog()
    assert measured == expected
    benchmark.extra_info.update(
        {
            "fixture": "complete_live_catalog",
            "mode": "catalog_reference",
            "live_plugins": len(expected),
            "measured_work": asdict(counts),
        }
    )
    result = benchmark.pedantic(list_catalog, rounds=5, iterations=1, warmup_rounds=1)
    assert result == expected


@pytest.mark.performance
@pytest.mark.parametrize("name", SCENARIO_NAMES)
@pytest.mark.parametrize("mode", ["disabled", "cold", "warm"])
def test_composer_session_analysis_modes(
    benchmark: BenchmarkFixture, name: ScenarioName, mode: Literal["disabled", "cold", "warm"]
) -> None:
    scenario = make_composer_validation_scenario(name)
    expected = run_validation(scenario)
    registry = ComposerSessionCacheRegistry(enabled=mode != "disabled")
    key = SessionCacheKey("performance-fixture", name)

    def read_session():
        if mode == "cold":
            # Explicitly discard the prior idle owner before every measured
            # request. Cold includes ordinary retirement/borrow overhead.
            with registry.retire_session(key.session_id):
                pass
        with registry.borrow(key) as lease:
            validator = validation_for_analysis(scenario, ComposerAnalysis(lease.cache))
            return run_validation(scenario, validator)

    if mode == "warm":
        assert read_session() == expected
    with count_composer_work() as counts:
        measured = read_session()
    assert measured == expected
    assert counts.validation_walks == (0 if mode == "warm" else 1)
    if mode == "warm":
        assert counts.transform_constructions == 0
    benchmark.extra_info.update(
        {
            "fixture": name,
            "mode": mode,
            "state_content_hash": composition_content_hash(scenario.state),
            "graph_nodes": len(scenario.state.nodes),
            "expected_valid": scenario.expected_valid,
            "measured_work": asdict(counts),
            "cache_before_timing": asdict(registry.snapshot()),
        }
    )
    result = benchmark.pedantic(read_session, rounds=5, iterations=1, warmup_rounds=0)
    assert result == expected
    snapshot = registry.snapshot()
    benchmark.extra_info["cache_after_timing"] = asdict(snapshot)
    assert snapshot.weight_bytes <= registry.limits.total_weight_bytes
    assert snapshot.borrows == snapshot.pins == snapshot.pending_entries == snapshot.queued_jobs == 0
    registry.begin_shutdown()
    assert registry.snapshot().weight_bytes == 0
