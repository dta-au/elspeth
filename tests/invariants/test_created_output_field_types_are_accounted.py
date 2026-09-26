"""Governance harness for ADR-050 Decision 12 — every field a transform stamps is concrete, or says why not.

Decision 12 swept every shipped transform so that each field its own code
computes declares the type that code fixes, and the engine checks it: a plugin
emitting the wrong type is then routed as the plugin's fault. A field left
``any`` is checked by nothing, so reverting one computed field to ``any``
silently re-opens that drift for the field, and the per-field conformance
cases (``test_output_declared_types_conform.py``, the plugin-family
declaration cases) only cover the fields that are concrete: a reverted field's
case simply disappears.

This census is the ratchet. It builds every registered transform from its
``probe_config()`` (input schema forced to observed, as the ADR-009 harness
does), plus the variant configs that switch on further created fields, reads
the declaration stamp table, and requires every ``any`` field to match a
reason in ``_REASONS``: a row-carried value whose type is the data's, a
list/mapping the schema DSL cannot type, the value_transform expression target
(B7 ruling), or a probe config's own ``any`` declaration. An unaccounted
``any`` field fails, and so does a stale reason that no longer matches any
field, so the table cannot drift from the code in either direction.
"""

from __future__ import annotations

import copy
import fnmatch
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from elspeth.plugins.infrastructure.base import BaseTransform
from tests.invariants.test_pass_through_invariants import _registered_transform_classes

_CARRIED = "carried from the rows: the value is a row value, its type is the data's"
_LIST_OR_MAPPING = "list/mapping: the schema DSL has no type for it"
_EXPRESSION = "value_transform expression target: its type depends on the expression and the data (B7 ruling)"
_PROBE_AUTHORED = "the probe config's own schema declares the field 'any'"

# (plugin name, field-name glob) -> why the field stays ``any``.
_REASONS: dict[tuple[str, str], str] = {
    ("batch_classifier_metrics", "confusion_matrix"): _LIST_OR_MAPPING,
    ("batch_classifier_metrics", "labels"): _LIST_OR_MAPPING,
    ("batch_classifier_metrics", "per_label"): _LIST_OR_MAPPING,
    ("batch_classifier_metrics", "missing_indices"): _LIST_OR_MAPPING,
    ("batch_data_quality_report", "observed_type_counts"): _LIST_OR_MAPPING,
    ("batch_distribution_profile", "missing_indices"): _LIST_OR_MAPPING,
    ("batch_distribution_profile", "non_finite_indices"): _LIST_OR_MAPPING,
    ("batch_distribution_profile", "g"): _CARRIED,
    ("batch_drift_compare", "baseline_cohort"): _CARRIED,
    ("batch_drift_compare", "cohort"): _CARRIED,
    ("batch_drift_compare", "category_shifts"): _LIST_OR_MAPPING,
    ("batch_drift_compare", "new_categories"): _LIST_OR_MAPPING,
    ("batch_effect_size", "baseline_variant"): _CARRIED,
    ("batch_effect_size", "variant"): _CARRIED,
    ("batch_effect_size", "*_indices"): _LIST_OR_MAPPING,
    ("batch_experiment_compare", "baseline_variant"): _CARRIED,
    ("batch_experiment_compare", "variant"): _CARRIED,
    ("batch_experiment_compare", "*_indices"): _LIST_OR_MAPPING,
    ("batch_outlier_annotator", "outlier_*_indices"): _LIST_OR_MAPPING,
    ("batch_paired_preference", "baseline_variant"): _CARRIED,
    ("batch_paired_preference", "variant"): _CARRIED,
    ("batch_paired_preference", "incomplete_pairs"): _LIST_OR_MAPPING,
    ("batch_stats", "skipped_*_indices"): _LIST_OR_MAPPING,
    ("batch_stats", "g"): _CARRIED,
    ("batch_top_k", "group_value"): _CARRIED,
    ("batch_top_k", "top_values"): _LIST_OR_MAPPING,
    ("blob_json_expand", "probe_*"): _PROBE_AUTHORED,
    ("json_explode", "item"): _CARRIED,
    ("llm", "*_usage"): _LIST_OR_MAPPING,
    ("aws_textract_document_analysis", "tx_meta"): _LIST_OR_MAPPING,
    ("aws_textract_document_analysis", "tx_result"): _LIST_OR_MAPPING,
    ("aws_textract_document_analysis", "tx_tables"): _LIST_OR_MAPPING,
    ("aws_textract_document_analysis", "tx_forms"): _LIST_OR_MAPPING,
    ("aws_textract_inline_analysis", "tx_meta"): _LIST_OR_MAPPING,
    ("aws_textract_inline_analysis", "tx_result"): _LIST_OR_MAPPING,
    ("aws_textract_inline_analysis", "tx_tables"): _LIST_OR_MAPPING,
    ("aws_textract_inline_analysis", "tx_forms"): _LIST_OR_MAPPING,
    ("azure_document_intelligence", "di_result"): _LIST_OR_MAPPING,
    ("azure_document_intelligence", "di_tables"): _LIST_OR_MAPPING,
    ("azure_document_intelligence", "di_kv"): _LIST_OR_MAPPING,
    ("value_transform", "*"): _EXPRESSION,
}

_OBSERVED: dict[str, Any] = {"schema": {"mode": "observed"}}


def _with(**options: Any) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A variant: the probe config with ``options`` set on top."""
    return lambda probe: {**probe, **options}


def _replace(options: dict[str, Any]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A variant: ``options`` alone, not derived from the probe config."""
    return lambda _probe: copy.deepcopy(options)


# Variant configs that switch on created fields the probe config leaves off.
_VARIANTS: dict[str, tuple[Callable[[dict[str, Any]], dict[str, Any]], ...]] = {
    "batch_stats": (_replace({**_OBSERVED, "value_field": "v", "group_by": "g", "compute_mean": True}),),
    "batch_distribution_profile": (_replace({**_OBSERVED, "value_field": "v", "group_by": "g"}),),
    "batch_classifier_metrics": (
        _replace({**_OBSERVED, "actual_field": "a", "predicted_field": "p", "positive_label": "yes"}),
        _replace({**_OBSERVED, "actual_field": "a", "predicted_field": "p", "positive_label": 1}),
    ),
    "batch_drift_compare": (_replace({**_OBSERVED, "cohort_field": "c", "value_field": "v", "value_type": "categorical"}),),
    "batch_top_k": (_replace({**_OBSERVED, "field": "v", "group_by": "g"}),),
    "llm": (
        _with(
            output_fields=[
                {"suffix": "score", "type": "integer"},
                {"suffix": "confidence", "type": "number"},
                {"suffix": "ok", "type": "boolean"},
                {"suffix": "band", "type": "enum", "values": ["a", "b"]},
                {"suffix": "why", "type": "string"},
            ]
        ),
        _with(
            prompt_template="Evaluate: {{ row.text_content }}",
            queries={
                "quality": {
                    "input_fields": {"text_content": "text"},
                    "output_fields": [{"suffix": "score", "type": "integer"}, {"suffix": "confidence", "type": "number"}],
                },
                "tone": {"input_fields": {"text_content": "text"}},
            },
        ),
    ),
    "aws_textract_document_analysis": (
        _with(
            page_count_field="tx_pages",
            metadata_field="tx_meta",
            result_field="tx_result",
            extract={"tables": "tx_tables", "forms": "tx_forms"},
        ),
    ),
    "aws_textract_inline_analysis": (
        _with(
            page_count_field="tx_pages",
            metadata_field="tx_meta",
            result_field="tx_result",
            extract={"tables": "tx_tables", "forms": "tx_forms"},
        ),
    ),
    "azure_document_intelligence": (
        _with(page_count_field="di_pages", result_field="di_result", extract={"tables": "di_tables", "key_value_pairs": "di_kv"}),
    ),
}


def _observed_input(config: dict[str, Any]) -> dict[str, Any]:
    """Force an authored input schema to observed, so only the plugin's own and the probe's declarations remain."""
    schema = config.get("schema")
    if type(schema) is dict and schema.get("mode") != "observed":
        return {**config, "schema": {"mode": "observed"}}
    return config


def _untyped_fields(classes: list[type[BaseTransform]]) -> set[tuple[str, str]]:
    """Every (plugin, field) the stamp table declares ``any``, over each probe config and variant."""
    untyped: set[tuple[str, str]] = set()
    for cls in classes:
        probe = copy.deepcopy(cls.probe_config())
        configs = [probe, *(variant(copy.deepcopy(probe)) for variant in _VARIANTS.get(cls.name, ()))]
        for config in configs:
            transform = cls(_observed_input(config))
            try:
                for field, contract in transform._stamped_output_field_contracts().items():
                    if contract.python_type is object:
                        untyped.add((cls.name, field))
            finally:
                transform.close()
    return untyped


def _account(untyped: set[tuple[str, str]], reasons: Mapping[tuple[str, str], str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(unaccounted ``any`` fields, stale reasons)."""
    used: set[tuple[str, str]] = set()
    unaccounted: list[tuple[str, str]] = []
    for plugin, field in sorted(untyped):
        matched = [key for key in reasons if key[0] == plugin and fnmatch.fnmatchcase(field, key[1])]
        if matched:
            used.update(matched)
        else:
            unaccounted.append((plugin, field))
    return unaccounted, sorted(key for key in reasons if key not in used)


@pytest.fixture(scope="module")
def untyped_fields() -> set[tuple[str, str]]:
    classes = _registered_transform_classes()
    assert classes, "Expected registered transforms; plugin registration may have failed."
    unknown_variants = set(_VARIANTS) - {cls.name for cls in classes}
    assert not unknown_variants, f"variants for unregistered transforms: {sorted(unknown_variants)}"
    return _untyped_fields(classes)


def test_every_any_field_a_transform_stamps_is_accounted_for(untyped_fields: set[tuple[str, str]]) -> None:
    unaccounted, _stale = _account(untyped_fields, _REASONS)
    assert unaccounted == [], (
        "these stamped fields are 'any' with no reason: declare the type the plugin's code fixes "
        "(created_output_fields), or add a reason to _REASONS if the value's type really is the data's"
    )


def test_no_reason_is_stale(untyped_fields: set[tuple[str, str]]) -> None:
    _unaccounted, stale = _account(untyped_fields, _REASONS)
    assert stale == [], "these reasons match no 'any' field any more: the field became concrete, so drop the reason"


def test_the_census_sees_a_field_it_cannot_account_for(untyped_fields: set[tuple[str, str]]) -> None:
    """Control: drop one reason and the census reports its field as unaccounted; plant one and it reports it stale."""
    assert ("batch_top_k", "top_values") in untyped_fields
    baseline_unaccounted, baseline_stale = _account(untyped_fields, _REASONS)
    reasons = {key: why for key, why in _REASONS.items() if key != ("batch_top_k", "top_values")}
    unaccounted, stale = _account(untyped_fields, reasons)
    assert unaccounted == sorted([*baseline_unaccounted, ("batch_top_k", "top_values")])
    assert stale == baseline_stale
    planted = {**_REASONS, ("batch_top_k", "no_such_field"): _CARRIED}
    assert _account(untyped_fields, planted) == (baseline_unaccounted, sorted([*baseline_stale, ("batch_top_k", "no_such_field")]))
