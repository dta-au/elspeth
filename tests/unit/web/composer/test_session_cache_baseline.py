"""Integrity controls for the graphs and counters used by cache benchmarks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Thread, current_thread

import pytest
from pydantic import BaseModel

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager, scoped_plugin_manager
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.state import CompositionState, ValidationSummary
from tests.fixtures.composer_session_cache import (
    SCENARIO_NAMES,
    ScenarioName,
    count_composer_work,
    make_composer_validation_scenario,
    make_small_composer_state,
    run_validation,
)


@pytest.mark.parametrize("name", SCENARIO_NAMES)
def test_benchmark_fixture_has_the_declared_validation_outcome(name: ScenarioName) -> None:
    scenario = make_composer_validation_scenario(name)
    assert run_validation(scenario).is_valid is scenario.expected_valid
    if name == "chain_80":
        assert len(scenario.state.nodes) == 80


def test_reconstructed_graph_preserves_the_reference_validation() -> None:
    state = make_small_composer_state()
    reconstructed = CompositionState.from_dict(state.to_dict())
    assert composition_content_hash(state) == composition_content_hash(reconstructed)
    assert state.validate() == reconstructed.validate()


def test_same_version_different_content_is_a_real_invalid_candidate() -> None:
    original = make_small_composer_state()
    invalid = replace(original, sources={}, version=original.version)
    assert original.version == invalid.version
    assert original.validate().is_valid
    assert not invalid.validate().is_valid
    assert "no_source_configured" in {entry.error_code for entry in invalid.validate().errors}
    assert composition_content_hash(original) != composition_content_hash(invalid)


def test_profile_fixture_keeps_the_authoring_alias_and_uses_current_policy() -> None:
    scenario = make_composer_validation_scenario("operator_profile")
    view = scenario.policy_catalog
    assert view is not None
    assert not view.snapshot.is_trained_operator
    assert scenario.state.nodes[0].options["profile"] == "fixture-prompt-guard"
    assert "guardrail_identifier" not in scenario.state.nodes[0].options
    assert scenario.validate().is_valid
    assert scenario.state.nodes[0].options["profile"] == "fixture-prompt-guard"


def test_work_counter_detects_production_walks_and_ignores_an_inert_read() -> None:
    state = make_small_composer_state()
    with count_composer_work() as counts:
        assert state.version >= 1
        assert counts.validation_walks == 0
        assert counts.transform_constructions == 0
        assert counts.json_schema_generations == 0
        assert state.validate().is_valid
        assert counts.validation_walks == 1
        assert counts.transform_constructions > 0


def test_work_counter_detects_actual_json_schema_generation() -> None:
    class FixtureModel(BaseModel):
        field: str

    with count_composer_work() as counts:
        assert counts.json_schema_generations == 0
        assert FixtureModel.model_json_schema()["properties"]["field"]["type"] == "string"
        assert counts.json_schema_generations == 1
        assert counts.validation_walks == 0
        assert counts.transform_constructions == 0


def test_work_counter_detects_deliberately_duplicated_validation() -> None:
    state = make_small_composer_state()
    with count_composer_work() as counts:
        first = state.validate()
        constructions_per_walk = counts.transform_constructions
        assert counts.validation_walks == 1
        assert constructions_per_walk > 0
        second = state.validate()
        assert first == second
        assert counts.validation_walks == 2
        assert counts.transform_constructions == 2 * constructions_per_walk


def test_work_counter_counts_workers_using_the_selected_live_manager() -> None:
    state = make_small_composer_state()
    manager = get_shared_plugin_manager()
    with count_composer_work() as serial_counts:
        expected = state.validate()
    start = Barrier(2)

    def validate_on_worker() -> tuple[Thread, ValidationSummary]:
        # Context variables do not automatically follow executor submissions.
        with scoped_plugin_manager(manager):
            assert get_shared_plugin_manager() is manager
            start.wait(timeout=5)
            return current_thread(), state.validate()

    with count_composer_work() as worker_counts, ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(validate_on_worker)
        second = executor.submit(validate_on_worker)
        outcomes = (first.result(timeout=10), second.result(timeout=10))
    assert outcomes[0][0] is not outcomes[1][0]
    assert all(summary == expected for _thread, summary in outcomes)
    assert worker_counts.validation_walks == 2
    assert worker_counts.transform_constructions == 2 * serial_counts.transform_constructions


def test_supplied_validation_mode_receives_the_exact_benchmark_input() -> None:
    scenario = make_composer_validation_scenario("shipped_small")
    observed: list[CompositionState] = []

    def reference_mode(state: CompositionState) -> ValidationSummary:
        observed.append(state)
        return state.validate()

    assert run_validation(scenario, reference_mode) == scenario.validate()
    assert observed == [scenario.state]
