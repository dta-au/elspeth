"""Repeatable local work/latency baseline for the Composer session-cache rollout.

Select explicitly with ``-m performance -n 0``. Timing is reported by the
benchmark plugin; correctness and compute-count controls live in ordinary
unit tests. These tests make no absolute latency promise or provider calls.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

import pytest

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from tests.fixtures.composer_session_cache import (
    SCENARIO_NAMES,
    ScenarioName,
    count_composer_work,
    make_composer_validation_scenario,
    run_validation,
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
