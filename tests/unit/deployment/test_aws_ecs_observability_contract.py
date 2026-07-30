"""Focused contracts for AWS ECS operator metric queryability and alarms."""

from __future__ import annotations

import re
from pathlib import Path

from elspeth.web.operator_telemetry import AWS_OPERATOR_PIPELINE_METRIC_NAMES

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIO_MODULE = REPO_ROOT / "deploy/aws-ecs/terraform/modules/scenario"
OBSERVABILITY = SCENARIO_MODULE / "iam_observability.tf"


def _observability() -> str:
    return OBSERVABILITY.read_text(encoding="utf-8")


def test_dashboard_pipeline_series_match_the_application_metric_inventory() -> None:
    source = _observability()

    for metric_name in AWS_OPERATOR_PIPELINE_METRIC_NAMES:
        assert f'name = "{metric_name}"' in source
    assert "llm.cost" not in source
    assert 'title = "LLM token totals"' in source


def test_composer_failure_queries_use_emitted_names_and_exact_point_dimensions() -> None:
    source = _observability()

    assert "composer.runtime_preflight_total" not in source
    assert "composer.authoring_validation_total" not in source
    assert '"composer.runtime_preflight.total"' in source
    assert '"composer.authoring_validation.total"' in source
    assert 'for result in ["failed", "exception"]' in source
    for telemetry_source in (
        "compose",
        "recompose",
        "convergence",
        "plugin_crash",
        "runtime_preflight",
        "yaml_export",
        "state_seed",
        "cached_preflight",
    ):
        assert f'"{telemetry_source}"' in source
    assert re.search(
        r'name\s*=\s*"composer\.runtime_preflight\.total".*?'
        r'dimensions\s*=\s*\{\s*outcome\s*=\s*"failure"\s*\}',
        source,
        re.DOTALL,
    )
    assert re.search(
        r'name\s*=\s*"composer\.authoring_validation\.total".*?'
        r'dimensions\s*=\s*\{\s*outcome\s*=\s*"invalid"\s*\}',
        source,
        re.DOTALL,
    )
    assert "sort(keys(metric.dimensions))" in source


def test_dashboard_and_alarms_cover_truthful_candidate_and_rollback_identities() -> None:
    source = _observability()

    assert "candidate_cloudwatch_dimension_map" in source
    assert "rollback_cloudwatch_dimension_map" in source
    assert "cloudwatch_dimension_maps" in source
    assert 'var.scenario_id == "B"' in source
    assert '"service.version"        = var.rollback_baseline_sha' in source
    assert '"aws.ecs.task.family"    = local.rollback_web_family' in source
    assert '"aws.ecs.task.revision"  = tostring(aws_ecs_task_definition.rollback_web[0].revision)' in source
    assert "for identity_dimensions in local.cloudwatch_dimension_lists" in source
    assert re.search(
        r'dynamic "metric_query"\s*\{\s*'
        r"for_each\s*=\s*local\.cloudwatch_dimension_maps_by_id",
        source,
        re.DOTALL,
    )
    assert "dimensions  = metric_query.value" in source


def test_export_failure_alarm_uses_positive_period_increments_and_can_recover() -> None:
    source = _observability()
    expected = "IF(DIFF(SUM(METRICS())) > 0, DIFF(SUM(METRICS())), 0)"

    assert f'expression  = "{expected}"' in source
    assert 'expression  = "failures + drops"' not in source
    assert "operator.telemetry.export_failures" in source
    assert "operator.telemetry.queue_drops" in source
