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
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from elspeth.contracts.types import CollectorName
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.template_materialization import FILE_BACKED_TEMPLATE_OPTION_REGISTRY
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer.tools.generation import _VALIDATION_ERROR_PATTERNS
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.preflight import build_validated_runtime_graph
from elspeth.web.paths import NESTED_LOCAL_PATH_OPTION_KEYS
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
    options:
      value_field: item
      schema:
        mode: observed
scopes:
  - name: document_pages
    opener: explode
    closer: page_stitcher
    policy: require_all
sinks:
  out:
    plugin: json
    options:
      path: out.json
      schema:
        mode: observed
    on_write_failure: discard
"""


def _collector_exclusion_conflicts(
    plugin_classes: Iterable[type[Any]],
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    """Return registry facts that reactivate the two collector exclusions."""
    materialized_content_keys = {rule.content_key for rule in FILE_BACKED_TEMPLATE_OPTION_REGISTRY}
    content_conflicts: dict[str, tuple[str, ...]] = {}
    provider_config_plugins: list[str] = []
    for plugin_cls in plugin_classes:
        config_model = plugin_cls.get_config_model()
        fields = set() if config_model is None else set(config_model.model_fields)
        content_fields = tuple(sorted(fields & materialized_content_keys))
        if content_fields:
            content_conflicts[plugin_cls.name] = content_fields
        if "provider_config" in fields:
            provider_config_plugins.append(plugin_cls.name)
    return content_conflicts, tuple(sorted(provider_config_plugins))


def test_collector_materialization_exclusions_have_live_registry_triggers() -> None:
    """Fail when a batch plugin makes either documented exclusion reachable."""
    transforms = tuple(get_shared_plugin_manager().get_transforms())
    batch_plugins = tuple(plugin_cls for plugin_cls in transforms if plugin_cls.is_batch_aware)
    assert batch_plugins, "the batch-plugin registry is empty, so the exclusion check is inert"

    content_conflicts, provider_config_plugins = _collector_exclusion_conflicts(batch_plugins)

    assert not content_conflicts, (
        f"batch-aware plugins now own file-materialized content fields {content_conflicts}; add collectors to "
        "PLUGIN_OPTION_COLLECTIONS in core/template_materialization.py and cover collector materialization"
    )
    assert not provider_config_plugins, (
        f"batch-aware plugins {list(provider_config_plugins)} now declare provider_config, which can carry local path keys "
        f"{list(NESTED_LOCAL_PATH_OPTION_KEYS)}; widen resolve_runtime_yaml_paths to collectors and cover path rewriting"
    )


def test_collector_exclusion_trigger_detects_current_non_batch_owners() -> None:
    """Positive control: prove the registry scan detects both option families."""
    transforms = tuple(get_shared_plugin_manager().get_transforms())
    content_conflicts, provider_config_plugins = _collector_exclusion_conflicts(transforms)
    detected_content_keys = {field for fields in content_conflicts.values() for field in fields}
    registered_content_keys = {rule.content_key for rule in FILE_BACKED_TEMPLATE_OPTION_REGISTRY}

    assert registered_content_keys
    assert detected_content_keys == registered_content_keys
    assert NESTED_LOCAL_PATH_OPTION_KEYS
    assert provider_config_plugins


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
    assert sorted(hit for hit in hits if not hit.startswith("docs/plans/")) == [str(Path(__file__).relative_to(_REPO_ROOT))]
