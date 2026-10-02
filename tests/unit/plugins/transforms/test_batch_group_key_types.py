"""Batch key admission must agree across every scalar grouping arm."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.transforms.batch_distribution_profile import BatchDistributionProfile
from elspeth.plugins.transforms.batch_drift_compare import BatchDriftCompare
from elspeth.plugins.transforms.batch_effect_size import BatchEffectSize
from elspeth.plugins.transforms.batch_experiment_compare import BatchExperimentCompare
from elspeth.plugins.transforms.batch_paired_preference import BatchPairedPreference
from elspeth.plugins.transforms.batch_stats import BatchStats
from elspeth.plugins.transforms.batch_top_k import BatchTopK
from elspeth.testing import make_contract, make_row
from tests.fixtures.factories import make_context

KEY_ARMS = [
    pytest.param(BatchStats, {"value_field": "score", "group_by": "key"}, "key", id="stats-group"),
    pytest.param(BatchDistributionProfile, {"value_field": "score", "group_by": "key"}, "key", id="distribution-group"),
    pytest.param(BatchTopK, {"field": "score", "group_by": "key", "k": 2}, "key", id="top-k-group"),
    pytest.param(
        BatchDriftCompare, {"value_field": "score", "cohort_field": "key", "value_type": "numeric"}, "key", id="drift-numeric-cohort"
    ),
    pytest.param(
        BatchDriftCompare,
        {"value_field": "score", "cohort_field": "key", "value_type": "categorical"},
        "key",
        id="drift-categorical-cohort",
    ),
    pytest.param(BatchEffectSize, {"score_field": "score", "variant_field": "key"}, "key", id="effect-variant"),
    pytest.param(BatchExperimentCompare, {"score_field": "score", "variant_field": "key"}, "key", id="experiment-variant"),
    pytest.param(BatchPairedPreference, {"score_field": "score", "variant_field": "key", "pair_field": "pair"}, "key", id="paired-variant"),
    pytest.param(BatchPairedPreference, {"score_field": "score", "variant_field": "key", "pair_field": "pair"}, "pair", id="paired-pair"),
]


def _rows(field: str, key: object) -> list[dict[str, Any]]:
    rows = [
        {"key": "baseline", "pair": "p1", "score": 1},
        {"key": "candidate", "pair": "p1", "score": 2},
        {"key": "baseline", "pair": "p2", "score": 3},
        {"key": "candidate", "pair": "p2", "score": 4},
    ]
    for row in rows:
        if row[field] == ("candidate" if field == "key" else "p1"):
            row[field] = key
    return rows


@pytest.mark.parametrize(("plugin", "options", "field"), KEY_ARMS)
@pytest.mark.parametrize("bad_key", [["PRIVATE-KEY-CONTENT"], {"private": "PRIVATE-KEY-CONTENT"}], ids=["list", "dict"])
def test_container_key_fails_entire_batch_without_recording_value(
    plugin: type[BaseTransform], options: dict[str, Any], field: str, bad_key: object
) -> None:
    transform = plugin({"schema": {"mode": "observed"}, **options})
    contract = make_contract(fields={"key": object, "pair": object, "score": int})
    rows = [make_row(row, contract=contract) for row in _rows(field, bad_key)]

    result = transform.process(rows, make_context())

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == "wrong_type"
    assert result.reason["field"] == field
    assert result.reason["expected"] == "a scalar group key"
    assert result.reason["actual_type"] == ("tuple" if isinstance(bad_key, list) else "mappingproxy")
    assert result.reason["error"].endswith(f"in row {1 if field == 'key' else 0}")
    assert "PRIVATE-KEY-CONTENT" not in str(result.reason)
    assert result.row is None
    assert result.rows is None


@pytest.mark.parametrize(("plugin", "options", "field"), KEY_ARMS)
@pytest.mark.parametrize("key", ["candidate", 7, 2.5, True, None, Decimal("2.5"), datetime(2026, 1, 1, tzinfo=UTC)])
def test_scalar_key_remains_a_valid_category(plugin: type[BaseTransform], options: dict[str, Any], field: str, key: object) -> None:
    transform = plugin({"schema": {"mode": "observed"}, **options})
    contract = make_contract(fields={"key": object, "pair": object, "score": int})
    result = transform.process([make_row(row, contract=contract) for row in _rows(field, key)], make_context())
    assert result.status == "success", result.reason
