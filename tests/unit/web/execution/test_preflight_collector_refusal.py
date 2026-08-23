"""Runtime preflight refuses collector-bearing bundles until WS4 (C1 teach-and-fail fix).

Collectors validate (Stage 1) and build (DAG), but the engine's
graph-registration invariant (pinned decision 5) refuses any collector-bearing
graph at run time. Before this refusal, ``build_validated_runtime_graph``
admitted such bundles green — composer-green then crashed at the orchestrator.
The web preflight now mirrors the engine invariant so composer-green means
RUNNABLE, with one shared message kernel across both entry paths.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag.models import GraphValidationError
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.preflight import build_validated_runtime_graph
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

_MESSAGE_KERNEL = "collector execution lands in WS4 and cannot run yet"

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


def test_preflight_refuses_a_collector_bearing_bundle() -> None:
    settings = load_settings_from_yaml_string(_COLLECTOR_SETTINGS_YAML)
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())

    with pytest.raises(GraphValidationError, match=re.escape(_MESSAGE_KERNEL)):
        build_validated_runtime_graph(settings, plugin_snapshot=snapshot)


def test_refusal_kernel_mirrors_the_engine_invariant_wording() -> None:
    """Both entry paths speak one kernel: `elspeth run` hits the engine's
    graph-registration raise, the web run path hits the preflight refusal.
    Pin the shared phrase in the engine module's source so a reword on either
    side reddens here instead of drifting the two operator messages apart."""
    from elspeth.engine.orchestrator import graph_registration

    source_file = inspect.getsourcefile(graph_registration)
    assert source_file is not None
    assert _MESSAGE_KERNEL in Path(source_file).read_text(encoding="utf-8")


def test_refusal_message_resolves_through_the_repair_catalogue() -> None:
    """The runtime-surfaced text limb: the planner's repair loop and the
    explain tool match raw preflight text against _VALIDATION_ERROR_PATTERNS
    in list order — the refusal must land on its own entry, not fall through
    to the generic no-match arm."""
    from elspeth.web.composer.tools.generation import _VALIDATION_ERROR_PATTERNS

    message = (
        "Pipeline contains collector node(s) (EXPAND-group closers, barrier-scopes spec §3); "
        "collector execution lands in WS4 and cannot run yet."
    )
    for pattern, explanation, fix in _VALIDATION_ERROR_PATTERNS:
        if re.search(pattern, message):
            assert "collector execution is not yet available" in explanation
            assert "remove the collector" in fix.lower()
            break
    else:
        pytest.fail("no _VALIDATION_ERROR_PATTERNS entry matches the collector runnability refusal")
