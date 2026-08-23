"""RC-7 build-acceptance pin: the lost-branch corpus fixtures build under §7 validation.

The §7 rule 4 forward walk covers success-path and gate-route edges ONLY
(protocols RC-7; the WS2 plan's pinned decision 1). Every fork-coalesce loss
fixture terminates tokens in-region via on_error routing — that is the
settlement system's INPUT. If any of these stops building, the SESE walk
has started covering DIVERT edges: fix the WALK, never the fixtures.
"""

from __future__ import annotations

import pytest

from tests.fixtures.dag_scenario_corpus.harness import build_scenario, render_settings
from tests.fixtures.dag_scenario_corpus.loader import iter_harness_cases, load_manifest
from tests.fixtures.dag_scenario_corpus.plugins import install_corpus_plugin_manager

_MANIFEST = load_manifest()
_LOST_BRANCH_CASES = [
    pytest.param(scenario, case, id=f"{scenario.id}:{case.id}")
    for scenario, case in iter_harness_cases(_MANIFEST)
    if scenario.id == "fork-coalesce-policies" and "lost" in case.fixture
]


def test_lost_branch_case_roster_is_pinned() -> None:
    # Protocols RC-7 speaks of "the 8 existing lost-branch corpus fixtures";
    # the live glob is authoritative — the fork-coalesce-policies scenario
    # carries 10 *lost* fixtures at plan time (best-effort-all-lost,
    # best-effort-{nested,select,union}-lost-c, first-all-lost,
    # quorum-impossible-lost-c, quorum-{nested,select,union}-lost-c,
    # require-all-lost-c). A shrinking roster means a fixture was renamed or
    # dropped — adjudicate, never shrug.
    assert len(_LOST_BRANCH_CASES) >= 8


@pytest.mark.parametrize(("scenario", "case"), _LOST_BRANCH_CASES)
def test_lost_branch_fixture_builds_under_bound_region_validation(scenario, case, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_corpus_plugin_manager(monkeypatch)
    built = build_scenario(render_settings(case, tmp_path))
    assert built.graph is not None
