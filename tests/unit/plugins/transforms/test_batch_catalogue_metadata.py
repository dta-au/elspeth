"""Reference-content contract for batch and statistical transforms."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from elspeth.core.config import AggregationSettings, load_bounded_pipeline_yaml
from elspeth.testing import make_pipeline_row
from elspeth.web.catalog.knob_schema import lower_model_to_knob_schema
from tests.fixtures.catalog_reference import (
    BuiltinReference,
    assert_reference_tags,
    assert_reference_text,
    discover_builtin_references,
    parse_and_validate_example,
)
from tests.fixtures.factories import make_context

EXPECTED_BATCH_TAGS = {
    "batch_classifier_metrics": ("batch", "classification", "narrative-summary"),
    "batch_data_quality_report": ("batch", "data-quality", "profiling"),
    "batch_distribution_profile": ("batch", "distribution", "narrative-summary"),
    "batch_drift_compare": ("batch", "drift", "comparison"),
    "batch_effect_size": ("batch", "effect-size", "comparison"),
    "batch_experiment_compare": ("batch", "experiment", "comparison"),
    "batch_outlier_annotator": ("batch", "outlier", "annotation"),
    "batch_paired_preference": ("batch", "paired", "comparison"),
    "batch_replicate": ("batch", "deaggregation", "row-expansion"),
    "batch_stats": ("batch", "aggregation", "statistics"),
    "batch_threshold_summary": ("batch", "threshold", "summary"),
    "batch_top_k": ("batch", "frequency", "top-k"),
}
EXPECTED_BATCH_NAMES = set(EXPECTED_BATCH_TAGS)
BATCH_REFERENCES = tuple(
    reference
    for reference in discover_builtin_references()
    if reference.kind == "transform" and reference.plugin_cls.name.startswith("batch_")
)
BATCH_BY_NAME = {reference.plugin_cls.name: reference for reference in BATCH_REFERENCES}

_REFERENCE_FIELDS = ("usage_when_to_use", "usage_when_not_to_use", "example_use", "capability_tags")
_PLACEHOLDER_MARKERS = ("todo", "tbd", "replace-me", "placeholder", "see the technical description")
_GROUP_BY_PLUGINS = {"batch_distribution_profile", "batch_stats", "batch_top_k"}

_REQUIRED_GUIDANCE = {
    "batch_classifier_metrics": (
        ("actual", "predicted", "scalar labels", "confusion", "f1", "none pairs are excluded"),
        ("score-to-label",),
    ),
    "batch_data_quality_report": (
        ("one quality row", "configured existing field", "present none", "missing", "absent columns are errors"),
        ("row repair",),
    ),
    "batch_distribution_profile": (
        ("numeric descriptive statistics", "optional group profiles"),
        ("categorical frequency",),
    ),
    "batch_drift_compare": (
        ("same flushed window", "baseline", "comparison"),
        ("history", "p-value", "alert threshold", "cross-run monitoring"),
    ),
    "batch_effect_size": (
        ("cohen's d", "hedges' g", "unpaired numeric variants"),
        ("significance", "paired"),
    ),
    "batch_experiment_compare": (
        ("unpaired", "mean", "lift", "z", "normal-bound"),
        ("p-value",),
    ),
    "batch_outlier_annotator": (
        ("window-local", "z-score", "robust-z", "annotations"),
        ("invalid numeric rows", "reported", "not emitted"),
    ),
    "batch_paired_preference": (
        ("pair id", "matched baseline", "candidate"),
        ("split-window pairs", "never join later"),
    ),
    "batch_replicate": (
        ("bounded", "per-row", "copy expansion"),
        ("sampling", "unbounded fan-out"),
    ),
    "batch_stats": (
        ("count", "sum", "optional mean", "numeric"),
        ("original rows are replaced",),
    ),
    "batch_threshold_summary": (
        ("named threshold", "summary rows"),
        ("filtering", "routing", "annotation"),
    ),
    "batch_top_k": (
        ("type-aware scalar frequencies",),
        ("numeric distribution profiling",),
    ),
}


def _declaring_aggregation(reference: BuiltinReference) -> tuple[Mapping[str, Any], AggregationSettings]:
    example = reference.plugin_cls.example_use
    assert isinstance(example, str)
    parsed = load_bounded_pipeline_yaml(example)
    assert set(parsed) == {"aggregations"}
    aggregations = parsed["aggregations"]
    assert isinstance(aggregations, (Mapping, list))
    nodes = list(aggregations.values()) if isinstance(aggregations, Mapping) else aggregations
    assert len(nodes) == 1
    node = cast(Mapping[str, Any], nodes[0])
    return node, AggregationSettings.model_validate(node)


def _assert_constructor_is_side_effect_free(
    reference: BuiltinReference,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node, _ = _declaring_aggregation(reference)
    options = cast(Mapping[str, Any], node.get("options", {}))
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.rglob("*"))
    reference.plugin_cls(dict(options))
    assert tuple(tmp_path.rglob("*")) == before


def test_batch_catalogue_discovers_every_and_only_registered_batch_transform() -> None:
    assert set(BATCH_BY_NAME) == EXPECTED_BATCH_NAMES


@pytest.mark.parametrize("reference", BATCH_REFERENCES, ids=lambda reference: reference.plugin_cls.name)
def test_batch_catalogue_reference_content_is_class_owned_specific_valid_and_truthful(
    reference: BuiltinReference,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_cls = reference.plugin_cls
    assert all(field_name in plugin_cls.__dict__ for field_name in _REFERENCE_FIELDS)
    assert_reference_text(plugin_cls)
    assert_reference_tags(plugin_cls)
    assert plugin_cls.capability_tags == EXPECTED_BATCH_TAGS[plugin_cls.name]
    parse_and_validate_example(reference)

    node, aggregation = _declaring_aggregation(reference)
    assert node["plugin"] == plugin_cls.name
    assert aggregation.output_mode == "transform"
    all_reference_text = (
        cast(str, plugin_cls.usage_when_to_use)
        + " "
        + cast(str, plugin_cls.usage_when_not_to_use)
        + " "
        + cast(str, plugin_cls.example_use)
    ).casefold()
    assert aggregation.trigger.count is not None or (
        not aggregation.trigger.has_timeout and not aggregation.trigger.has_condition and "end-of-source" in all_reference_text
    )

    to_use_value = plugin_cls.usage_when_to_use
    not_to_use_value = plugin_cls.usage_when_not_to_use
    assert isinstance(to_use_value, str)
    assert isinstance(not_to_use_value, str)
    to_use = " ".join(to_use_value.casefold().split())
    not_to_use = " ".join(not_to_use_value.casefold().split())
    required_use, required_avoid = _REQUIRED_GUIDANCE[plugin_cls.name]
    assert all(term in to_use for term in required_use)
    assert all(term in not_to_use for term in required_avoid)

    assert not any(marker in all_reference_text for marker in _PLACEHOLDER_MARKERS)
    _assert_constructor_is_side_effect_free(reference, tmp_path, monkeypatch)


def test_group_by_guidance_is_explicitly_window_scoped() -> None:
    for name in _GROUP_BY_PLUGINS:
        plugin_cls = BATCH_BY_NAME[name].plugin_cls
        prose = f"{plugin_cls.usage_when_to_use} {plugin_cls.usage_when_not_to_use}".casefold()
        assert "group_by partitions one flushed batch" in prose
        assert "never accumulates a group across windows" in prose


def test_statistical_guidance_rejects_unsupported_inference_and_history_claims() -> None:
    drift_avoid = cast(str, BATCH_BY_NAME["batch_drift_compare"].plugin_cls.usage_when_not_to_use).casefold()
    effect_avoid = cast(str, BATCH_BY_NAME["batch_effect_size"].plugin_cls.usage_when_not_to_use).casefold()
    experiment_avoid = cast(str, BATCH_BY_NAME["batch_experiment_compare"].plugin_cls.usage_when_not_to_use).casefold()

    assert "does not retain history" in drift_avoid
    assert "does not compute p-values" in drift_avoid
    assert "does not establish statistical significance" in effect_avoid
    assert "does not compute a p-value" in experiment_avoid


def test_replicate_guidance_distinguishes_missing_wrong_type_and_unsafe_integer_counts() -> None:
    plugin_cls = BATCH_BY_NAME["batch_replicate"].plugin_cls
    prose = f"{plugin_cls.usage_when_to_use} {plugin_cls.usage_when_not_to_use}".casefold()

    assert "missing copies_field uses default_copies" in prose
    assert "a present non-integer count raises typeerror" in prose
    assert "only integer counts outside 1..max_copies are quarantined" in prose


def test_data_quality_catalogue_fields_match_the_maintained_example() -> None:
    catalogue_node, _ = _declaring_aggregation(BATCH_BY_NAME["batch_data_quality_report"])
    catalogue_options = cast(Mapping[str, Any], catalogue_node["options"])

    project_root = Path(__file__).resolve().parents[4]
    authority_path = project_root / "examples/statistical_batch_plugins/settings_data_quality_report.yaml"
    authority = load_bounded_pipeline_yaml(authority_path.read_text(encoding="utf-8"))
    authority_aggregations = authority["aggregations"]
    assert isinstance(authority_aggregations, list)
    authority_node = cast(Mapping[str, Any], authority_aggregations[0])
    assert authority_node["plugin"] == "batch_data_quality_report"
    authority_options = cast(Mapping[str, Any], authority_node["options"])

    assert catalogue_options["inspect_fields"] == authority_options["inspect_fields"]
    assert catalogue_options["inspect_fields"] == ["source", "score_text", "label"]


def test_batch_stats_guidance_allows_bounded_end_of_source_whole_source_aggregation() -> None:
    aggregation = AggregationSettings.model_validate(
        {
            "name": "final_summary",
            "plugin": "batch_stats",
            "input": "source_rows",
            "on_success": "output",
            "on_error": "discard",
            "output_mode": "transform",
            "options": {"schema": {"mode": "observed"}, "value_field": "amount"},
        }
    )
    assert not aggregation.trigger.has_count
    assert not aggregation.trigger.has_timeout
    assert not aggregation.trigger.has_condition

    plugin_cls = BATCH_BY_NAME["batch_stats"].plugin_cls
    to_use = cast(str, plugin_cls.usage_when_to_use).casefold()
    not_to_use = cast(str, plugin_cls.usage_when_not_to_use).casefold()
    assert "omit trigger or use trigger: {} for one bounded whole-source end-of-source aggregate" in to_use
    assert "count, timeout, or condition trigger creates independent windows" in to_use
    assert "not for whole-run totals" not in not_to_use


def test_batch_replicate_generated_knob_describes_absent_field_only() -> None:
    config_model = cast(type[BaseModel], BATCH_BY_NAME["batch_replicate"].plugin_cls.config_model)
    knob_schema = lower_model_to_knob_schema(
        config_model,
        plugin_kind="transform",
        plugin_name="batch_replicate",
    )
    default_copies = next(field for field in knob_schema["fields"] if field["name"] == "default_copies")

    assert default_copies["description"] == "Default number of copies when copies_field is absent"
    assert "invalid" not in default_copies["description"].casefold()


def test_batch_stats_technical_description_matches_valid_value_counting() -> None:
    plugin_cls = BATCH_BY_NAME["batch_stats"].plugin_cls
    transform = plugin_cls({"schema": {"mode": "observed"}, "value_field": "amount"})
    result = transform.process(
        [
            make_pipeline_row({"amount": 10.0}),
            make_pipeline_row({"amount": None}),
            make_pipeline_row({"amount": float("inf")}),
        ],
        make_context(),
    )
    assert result.status == "success"
    assert result.row is not None
    assert result.row["count"] == 1
    assert result.row["sum"] == 10.0
    assert result.row["batch_size"] == 3

    technical_description = plugin_cls.__doc__
    assert isinstance(technical_description, str)
    normalized = " ".join(technical_description.casefold().split())
    assert "count: number of finite, non-missing valid numeric values" in normalized
    assert "sum: sum of those finite, non-missing valid numeric values" in normalized
    assert "count: number of rows in the batch" not in normalized
    assert "sum of the value_field across all rows" not in normalized
