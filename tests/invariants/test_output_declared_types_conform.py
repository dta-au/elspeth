"""Governance harness for ADR-050's concrete declarations — every declared type holds on real computations.

A batch plugin declares the type its own code fixes for every field it
computes (``created_output_fields()``), and the engine checks each emitted
value against that declaration at the flush postflight
(``engine/executors/declared_output_types.verify_created_output_types``) by
the one admission rule, ``declared_type_admits``: exact type, except that an
``int`` satisfies a ``float`` declaration (ADR-050 Decision 5, ruling C3).
A declaration the code does not honour on some valid input would route
valid data as a plugin bug, so this harness drives each shipped batch
plugin's real ``process()`` over the inputs that exercise its type-deciding
branches — int rows (where a statistic declared ``float`` may be an exact int,
such as a sum, a minimum or an annotated value), float rows, a singleton
(undefined dispersion emitted as None), no spread, missing and non-finite
values, grouping — and asserts the value check passes and every concrete
declaration was actually exercised.

The roster test fails when a registered batch-aware transform declares a
concrete type and has no case here, so a new plugin cannot land its
declarations unexercised.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.schema_contract import PipelineRow, declared_type_admits
from elspeth.contracts.type_normalization import classify_runtime_type
from elspeth.engine.executors.declared_output_types import verify_created_output_types
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.testing import make_pipeline_row
from tests.invariants.test_pass_through_invariants import (
    _emitted_rows_from_result,
    _probe_context,
    _registered_transform_classes,
)

_NAN = float("nan")

# (case id, plugin name, config, rows). Every case must succeed.
_CASES: list[tuple[str, str, dict[str, Any], list[dict[str, Any]]]] = [
    ("stats-int", "batch_stats", {"schema": {"mode": "observed"}, "value_field": "v", "compute_mean": True}, [{"v": 1}, {"v": 2}]),
    (
        "stats-float-grouped-skips",
        "batch_stats",
        {"schema": {"mode": "observed"}, "value_field": "v", "group_by": "g", "compute_mean": True},
        [{"v": 1.5, "g": "a"}, {"v": None, "g": "a"}, {"v": _NAN, "g": "a"}, {"v": 3, "g": "b"}],
    ),
    ("profile-int-odd", "batch_distribution_profile", {"schema": {"mode": "observed"}, "value_field": "v"}, [{"v": 1}, {"v": 2}, {"v": 3}]),
    ("profile-int-singleton", "batch_distribution_profile", {"schema": {"mode": "observed"}, "value_field": "v"}, [{"v": 4}]),
    (
        "profile-float-grouped-skips",
        "batch_distribution_profile",
        {"schema": {"mode": "observed"}, "value_field": "v", "group_by": "g"},
        [{"v": 0.5, "g": 1}, {"v": 2, "g": 1}, {"v": None, "g": 1}, {"v": _NAN, "g": 2}, {"v": 7, "g": 2}],
    ),
    (
        "classifier-binary-str",
        "batch_classifier_metrics",
        {"schema": {"mode": "observed"}, "actual_field": "a", "predicted_field": "p", "positive_label": "yes"},
        [{"a": "yes", "p": "yes"}, {"a": "no", "p": "yes"}, {"a": "no", "p": "no"}, {"a": None, "p": "no"}],
    ),
    (
        "classifier-binary-int-undefined-ratios",
        "batch_classifier_metrics",
        {"schema": {"mode": "observed"}, "actual_field": "a", "predicted_field": "p", "positive_label": 1},
        [{"a": 0, "p": 0}, {"a": 0, "p": 0}],
    ),
    (
        "classifier-binary-bool",
        "batch_classifier_metrics",
        {"schema": {"mode": "observed"}, "actual_field": "a", "predicted_field": "p", "positive_label": True},
        [{"a": True, "p": False}, {"a": False, "p": False}],
    ),
    (
        "quality",
        "batch_data_quality_report",
        {"schema": {"mode": "observed"}, "inspect_fields": ["x", "y"]},
        [{"x": 1, "y": ""}, {"x": None, "y": "a"}, {"x": _NAN, "y": "a"}, {"x": [1], "y": None}],
    ),
    (
        "drift-numeric-int",
        "batch_drift_compare",
        {"schema": {"mode": "observed"}, "cohort_field": "c", "value_field": "v"},
        [{"c": "base", "v": 1}, {"c": "base", "v": 3}, {"c": "new", "v": 2}, {"c": "new", "v": None}],
    ),
    (
        "drift-categorical",
        "batch_drift_compare",
        {"schema": {"mode": "observed"}, "cohort_field": "c", "value_field": "v", "value_type": "categorical"},
        [{"c": "base", "v": "x"}, {"c": "base", "v": "y"}, {"c": "new", "v": "x"}, {"c": "new", "v": "z"}],
    ),
    (
        "effect-int",
        "batch_effect_size",
        {"schema": {"mode": "observed"}, "variant_field": "g", "score_field": "s"},
        [{"g": "b", "s": 1}, {"g": "b", "s": 3}, {"g": "c", "s": 4}, {"g": "c", "s": 6}],
    ),
    (
        "effect-singletons-and-skips",
        "batch_effect_size",
        {"schema": {"mode": "observed"}, "variant_field": "g", "score_field": "s"},
        [{"g": "b", "s": 2}, {"g": "b", "s": None}, {"g": "c", "s": 5.5}, {"g": "c", "s": _NAN}],
    ),
    (
        "effect-no-spread",
        "batch_effect_size",
        {"schema": {"mode": "observed"}, "variant_field": "g", "score_field": "s"},
        [{"g": "b", "s": 2}, {"g": "b", "s": 2}, {"g": "c", "s": 2}, {"g": "c", "s": 2}],
    ),
    (
        "experiment-int",
        "batch_experiment_compare",
        {"schema": {"mode": "observed"}, "variant_field": "g", "score_field": "s"},
        [{"g": "b", "s": 1}, {"g": "b", "s": 3}, {"g": "c", "s": 4}, {"g": "c", "s": 6}],
    ),
    (
        "experiment-zero-baseline-singleton-skips",
        "batch_experiment_compare",
        {"schema": {"mode": "observed"}, "variant_field": "g", "score_field": "s"},
        [{"g": "b", "s": 0}, {"g": "b", "s": None}, {"g": "c", "s": 2.5}, {"g": "c", "s": _NAN}],
    ),
    (
        "experiment-no-spread",
        "batch_experiment_compare",
        {"schema": {"mode": "observed"}, "variant_field": "g", "score_field": "s"},
        [{"g": "b", "s": 2}, {"g": "b", "s": 2}, {"g": "c", "s": 3}, {"g": "c", "s": 3}],
    ),
    (
        "paired-int",
        "batch_paired_preference",
        {"schema": {"mode": "observed"}, "pair_field": "k", "variant_field": "g", "score_field": "s"},
        [
            {"k": 1, "g": "b", "s": 1},
            {"k": 1, "g": "c", "s": 2},
            {"k": 2, "g": "b", "s": 3},
            {"k": 2, "g": "c", "s": 3},
            {"k": 3, "g": "b", "s": 5},
            {"k": 3, "g": "c", "s": 4},
            {"k": 4, "g": "b", "s": 1},
        ],
    ),
    (
        "paired-all-ties-single-pair",
        "batch_paired_preference",
        {"schema": {"mode": "observed"}, "pair_field": "k", "variant_field": "g", "score_field": "s"},
        [{"k": 1, "g": "b", "s": 2}, {"k": 1, "g": "c", "s": 2}],
    ),
    (
        "threshold-int",
        "batch_threshold_summary",
        {
            "schema": {"mode": "observed"},
            "value_field": "v",
            "thresholds": [{"name": "high", "operator": ">=", "value": 2}, {"name": "low", "operator": "<", "value": 1.5}],
        },
        [{"v": 1}, {"v": 2}, {"v": None}, {"v": _NAN}],
    ),
    (
        "top-k",
        "batch_top_k",
        {"schema": {"mode": "observed"}, "field": "v", "k": 2},
        [{"v": "a"}, {"v": "a"}, {"v": 1}, {"v": None}, {"v": _NAN}],
    ),
    (
        "top-k-grouped",
        "batch_top_k",
        {"schema": {"mode": "observed"}, "field": "v", "group_by": "g"},
        [{"v": 1, "g": "x"}, {"v": 2, "g": "y"}],
    ),
    (
        "outlier-int",
        "batch_outlier_annotator",
        {"schema": {"mode": "observed"}, "value_field": "v"},
        [{"v": 1}, {"v": 2}, {"v": 2}, {"v": 100}, {"v": None}, {"v": _NAN}],
    ),
    ("outlier-no-spread", "batch_outlier_annotator", {"schema": {"mode": "observed"}, "value_field": "v"}, [{"v": 3}, {"v": 3}]),
    ("outlier-singleton", "batch_outlier_annotator", {"schema": {"mode": "observed"}, "value_field": "v"}, [{"v": 3}]),
    (
        "rank-mixed",
        "batch_rank",
        {"schema": {"mode": "observed"}, "value_field": "v"},
        [{"v": 1}, {"v": 2.5}, {"v": 2.5}, {"v": None}, {"v": _NAN}, {"w": 0}],
    ),
    ("rank-singleton-dense", "batch_rank", {"schema": {"mode": "observed"}, "value_field": "v", "ties": "dense"}, [{"v": 3}]),
    (
        "replicate",
        "batch_replicate",
        {"schema": {"mode": "observed"}, "copies_field": "copies", "include_copy_index": True},
        [{"copies": 2, "id": 1}, {"copies": 1, "id": 2}],
    ),
    ("report", "report_assemble", {"schema": {"mode": "observed"}, "text_field": "line"}, [{"line": "one"}, {"line": "two"}]),
]


def _transform(plugin_name: str, config: dict[str, Any]) -> BaseTransform:
    [transform_cls] = [cls for cls in _registered_transform_classes() if cls.name == plugin_name]
    return transform_cls(config)


def _concrete_declarations(transform: BaseTransform) -> dict[str, type]:
    return {
        name: contract.python_type
        for name, contract in transform._stamped_output_field_contracts().items()
        if contract.python_type is not object
    }


@pytest.mark.parametrize(("case_id", "plugin_name", "config", "rows"), _CASES, ids=[case[0] for case in _CASES])
def test_every_declared_type_holds_on_the_real_computation(
    case_id: str, plugin_name: str, config: dict[str, Any], rows: list[dict[str, Any]]
) -> None:
    transform = _transform(plugin_name, config)
    input_rows = [make_pipeline_row(row) for row in rows]
    result = transform.process(input_rows, _probe_context(transform))
    assert result.status == "success", (case_id, result.reason)
    emitted = _emitted_rows_from_result(result)
    assert emitted, case_id

    verify_created_output_types(transform=transform, emitted_rows=emitted)

    concrete = _concrete_declarations(transform)
    checked = _checked_concrete_fields(emitted, concrete)
    assert checked, f"{case_id}: no concrete declaration was exercised; the case arms nothing"


def _checked_concrete_fields(emitted: list[PipelineRow], concrete: dict[str, type]) -> set[str]:
    """Every concrete-declared field present with a non-None value, each admitted by its declared type."""
    checked: set[str] = set()
    for row in emitted:
        values = row.to_dict()
        for name, python_type in concrete.items():
            if name not in values or values[name] is None:
                continue
            assert declared_type_admits(python_type, classify_runtime_type(values[name])), (name, python_type, type(values[name]))
            checked.add(name)
    return checked


def test_every_concrete_declaration_of_every_batch_plugin_is_exercised() -> None:
    """Roster: each batch-aware transform with a concrete declaration has cases that emit every concrete field.

    Across a plugin's cases, every concrete-typed field it declares for some
    configuration must appear with a non-None value at least once, so no
    declared type ships without a computation that proves it.
    """
    batch_classes = [cls for cls in _registered_transform_classes() if cls.is_batch_aware]
    assert batch_classes, "plugin registration found no batch-aware transform; the roster would pass vacuously"
    covered: dict[str, set[str]] = {}
    declared: dict[str, set[str]] = {}
    for _case_id, plugin_name, config, rows in _CASES:
        transform = _transform(plugin_name, config)
        concrete = _concrete_declarations(transform)
        declared.setdefault(plugin_name, set()).update(concrete)
        result = transform.process([make_pipeline_row(row) for row in rows], _probe_context(transform))
        assert result.status == "success", (plugin_name, result.reason)
        covered.setdefault(plugin_name, set()).update(_checked_concrete_fields(_emitted_rows_from_result(result), concrete))

    missing_plugins = sorted(
        cls.name for cls in batch_classes if cls.name not in declared and _concrete_declarations(cls(cls.probe_config()))
    )
    assert not missing_plugins, f"batch transforms with concrete declarations but no conformance case: {missing_plugins}"
    unexercised = {plugin: sorted(declared[plugin] - covered[plugin]) for plugin in declared if declared[plugin] - covered[plugin]}
    assert not unexercised, f"concrete declarations never emitted with a value by any case: {unexercised}"
