"""Corpus integrity: every case has a prompt, a scenario, a valid payload, a stored topology
that round-trips, an extractor cross-check that holds, and a prompt that stays on the compose loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.lib.battery_corpus import SCENARIOS_DIR, load_corpus, parse_corpus
from evals.lib.battery_scenario import (
    extractor_cross_check,
    load_scenario,
    topology_from_dict,
    topology_to_dict,
    validate_canonical_arguments,
)
from evals.lib.battery_topology import topology_from_pipeline

from elspeth.web.composer.no_tool_policy import PipelineMutationIntentDecision, classify_pipeline_mutation_intent

REPO = Path(__file__).resolve().parents[4]
EXPECTED_CASES = {
    "canary",
    "transform_pipeline",
    "boolean_routing",
    "explicit_routing",
    "threshold_gate",
    "deep_routing",
    "error_routing",
    "fork_coalesce",
    "row_union_ab_experiment",
    "batch_aggregation",
    "statistical_batch_plugins",
    "json_explode",
    "deaggregation",
    "template_lookups",
    "multi_query_assessment",
    "openrouter_sentiment",
    "llm_source",
    "schema_contracts_demo",
    "report_assemble",
}


def test_parse_corpus_takes_first_unlabelled_fence_verbatim() -> None:
    md = "corpus_version: 3\n\n## alpha\n\nIntro.\n\n```text\nlabelled\n```\n\n```\n  Verbatim  bytes\nline two\n```\n\n## beta\n\n```\nb\n```\n"
    version, cases = parse_corpus(md)
    assert version == 3
    assert [c.name for c in cases] == ["alpha", "beta"]
    assert cases[0].prompt == "  Verbatim  bytes\nline two"


def _present_cases() -> list[str]:
    _, cases = load_corpus()
    return sorted(cases)


def test_corpus_and_scenarios_cover_the_same_cases() -> None:
    """The corpus is the roster. Every corpus case has a scenario and vice versa; the FULL 19-name roster is
    required once the corpus is frozen (corpus_version >= 1) — while it is under construction (version 0) a
    subset is allowed so the tree stays green between authoring batches (Task 3a → 3b)."""
    version, cases = load_corpus()
    scenario_dirs = {p.name for p in SCENARIOS_DIR.iterdir() if (p / "scenario.json").exists()}
    assert set(cases) == scenario_dirs, f"corpus/scenario mismatch: {set(cases) ^ scenario_dirs}"
    assert set(cases) <= EXPECTED_CASES, f"unexpected cases: {set(cases) - EXPECTED_CASES}"
    if version >= 1:
        assert set(cases) == EXPECTED_CASES, f"frozen corpus is missing cases: {EXPECTED_CASES - set(cases)}"


@pytest.mark.parametrize("case", _present_cases())
def test_scenario_is_sound(case: str) -> None:
    sc = load_scenario(SCENARIOS_DIR / case / "scenario.json")
    validate_canonical_arguments(sc.canonical_arguments)
    derived = topology_from_pipeline(sc.canonical_arguments)
    assert topology_to_dict(derived) == sc.expected_topology, f"{case}: expected_topology is stale — regenerate"
    assert topology_from_dict(sc.expected_topology) == derived
    assert extractor_cross_check(sc, REPO) == []
    assert sc.floor.tool_bearing_calls == sum(sc.floor.components.values())
    assert sc.floor.repairs == 0 and sc.floor.backtracks == 0
    # the pre/post-calibration record is the floor's only audit trail (spec §1)
    assert sc.floor.tool_bearing_calls in {sc.floor.pre_calibration, sc.floor.post_calibration}
    if sc.corpus_version >= 1:
        assert sc.floor.post_calibration is not None, f"{case}: frozen scenario has no post_calibration floor"


@pytest.mark.parametrize("case", _present_cases())
def test_prompt_stays_on_the_compose_loop(case: str) -> None:
    """Decision 7 surface gate, run in CI: a classifier-grammar edit must fail here, not silently re-route."""
    _, cases = load_corpus()
    sc = load_scenario(SCENARIOS_DIR / case / "scenario.json")
    decision = classify_pipeline_mutation_intent(cases[case].prompt)
    assert decision is not PipelineMutationIntentDecision.EXPLICIT_MUTATION, f"{case}: prompt would route to the planner"
    assert decision.name == sc.classifier_decision, f"{case}: recorded classifier_decision {sc.classifier_decision} != {decision.name}"


def test_corpus_version_matches_every_scenario() -> None:
    version, cases = load_corpus()
    for case in cases:
        assert load_scenario(SCENARIOS_DIR / case / "scenario.json").corpus_version == version


@pytest.fixture(scope="module")
def tool_context():
    """Real builtin catalog + trained-operator policy view — the same wiring tests/unit/web/composer/test_recipes.py uses."""
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.catalog.service import CatalogServiceImpl
    from elspeth.web.composer.tools._common import ToolContext
    from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

    pm = PluginManager()
    pm.register_builtin_plugins()
    catalog = CatalogServiceImpl(pm)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return ToolContext(catalog=PolicyCatalogView.for_trained_operator(catalog, snapshot), plugin_snapshot=snapshot)


@pytest.mark.parametrize("case", _present_cases())
def test_canonical_payload_commits_to_the_expected_topology(case: str, tool_context) -> None:
    """Spec §2 second oracle test: the topology of the state the server's own args→state path builds
    for the canonical payload ≡ the stored expected_topology (hermetic for ``path`` sources)."""
    from elspeth.web.composer.state import CompositionState, PipelineMetadata
    from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate

    sc = load_scenario(SCENARIOS_DIR / case / "scenario.json")
    assert "inline_blob" not in json.dumps(sc.canonical_arguments), (
        f"{case}: Decision 9 requires a plain `path` source in canonical_arguments (the server anchor must stay hermetic)"
    )
    empty = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    candidate = build_set_pipeline_candidate(sc.canonical_arguments, empty, tool_context)
    assert candidate.result.success, candidate.result.to_dict()
    committed = topology_from_pipeline(candidate.result.updated_state.to_dict())
    assert topology_to_dict(committed) == sc.expected_topology, f"{case}: server args→state projection differs from expected_topology"
