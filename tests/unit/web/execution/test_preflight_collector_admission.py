"""Runtime preflight ADMITS collector-bearing bundles (integration C4, the lift).

Successor of test_preflight_collector_refusal.py: the engine's
graph-registration invariant and the web preflight mirror that refused any
collector-bearing graph ("collector execution lands in WS4 and cannot run
yet") are gone, together with the composer catalogue entry that explained
that refusal. A green runtime preflight means RUNNABLE, and a collector
pipeline is runnable now.
"""

from __future__ import annotations

import re
from pathlib import Path

from elspeth.contracts.types import CollectorName
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.web.composer.tools.generation import _VALIDATION_ERROR_PATTERNS
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.preflight import build_validated_runtime_graph
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_RETIRED_KERNEL = "collector execution lands in WS4"
_REPO_ROOT = Path(__file__).resolve().parents[4]

_COLLECTOR_SETTINGS_YAML = """
sources:
  main:
    plugin: csv
    on_success: rows
    options:
      path: in.csv
      on_validation_failure: discard
      schema:
        mode: observed
transforms:
  - name: explode
    plugin: json_explode
    input: rows
    on_success: pages
    on_error: discard
    options:
      array_field: items
      schema:
        mode: observed
collectors:
  - name: page_stitcher
    plugin: batch_stats
    input: pages
    on_success: out
    on_error: discard
    options:
      value_field: item
      schema:
        mode: observed
scopes:
  - name: document_pages
    opener: explode
    closer: page_stitcher
    policy: require_all
    on_group_failure: quarantine
sinks:
  out:
    plugin: json
    options:
      path: out.json
      schema:
        mode: observed
    on_write_failure: discard
"""


def test_preflight_admits_a_collector_bearing_bundle() -> None:
    settings = load_settings_from_yaml_string(_COLLECTOR_SETTINGS_YAML)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())

    runtime = build_validated_runtime_graph(settings, plugin_snapshot=snapshot)

    assert set(runtime.graph.get_collector_id_map()) == {CollectorName("page_stitcher")}
    assert set(runtime.graph.get_collector_transform_map()) == {CollectorName("page_stitcher")}


def test_the_retired_refusal_has_no_catalogue_entry() -> None:
    """The explainer entry for the refusal is dead advice once nothing raises
    it: no pattern may match the retired kernel (a match would tell an author
    to remove a collector that runs)."""
    message = f"Pipeline contains collector node(s); {_RETIRED_KERNEL} and cannot run yet."
    assert [pattern for pattern, _explanation, _fix in _VALIDATION_ERROR_PATTERNS if re.search(pattern, message)] == []


def test_the_retired_kernel_survives_only_in_history() -> None:
    """Negative pin for the lift: no live source, test, doc, config or
    frontend text still states the refusal — only the WS2 plan's history.
    A plain file walk (not git grep) so the pin holds in a `git archive`
    export too."""
    hits: list[str] = []
    for root in ("src", "tests", "docs", "config", "frontend/src"):
        for path in (_REPO_ROOT / root).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md", ".yaml", ".yml", ".ts", ".tsx", ".json", ".txt"}:
                continue
            if _RETIRED_KERNEL in path.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(path.relative_to(_REPO_ROOT)))
    assert sorted(hit for hit in hits if not hit.startswith("docs/superpowers/plans/")) == [str(Path(__file__).relative_to(_REPO_ROOT))]
