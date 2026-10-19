"""Whole-window duplicate membership preserves every member for admission."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from elspeth.contracts.node_state_context import AggregationBatchContext
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.batch_duplicate_annotator import BatchDuplicateAnnotator
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context


def options(**overrides):
    return {
        "schema": {"mode": "observed"},
        "rules": [{"name": "repeated_text", "cells": [{"field": "first"}, {"field": "second"}]}],
        **overrides,
    }


def context(size: int):
    return replace(make_context(), aggregation_batch=AggregationBatchContext("end_of_source", "batch", size, 1, size, 1, size, True))


def annotate(data, **overrides):
    transform = BatchDuplicateAnnotator(options(**overrides))
    rows = [make_pipeline_row(row) for row in data]
    before = [row.to_dict() for row in rows]
    result = transform.process(rows, context(len(rows)))
    assert [row.to_dict() for row in rows] == before
    assert result.status == "success" and result.rows is not None
    assert len(result.rows) == len(rows)
    assert len({id(row.contract) for row in result.rows}) == 1
    for original, emitted in zip(before, result.rows, strict=True):
        assert {key: emitted.to_dict()[key] for key in original} == original
        assert type(emitted["duplicates_valid"]) is bool
        assert type(emitted["duplicates_causes"]) is str
    return result.rows


def test_late_cross_field_equality_invalidates_both_ends_without_collapsing_application_ids() -> None:
    rows = annotate(
        [
            {"case_id": "same", "first": "late match", "second": "first unique"},
            {"case_id": "same", "first": "independent", "second": "different"},
            {"case_id": "same", "first": "last unique", "second": "late match"},
        ]
    )
    assert [row["duplicates_valid"] for row in rows] == [False, True, False]
    assert [json.loads(row["duplicates_causes"]) for row in rows] == [
        [{"rule": "repeated_text", "field": "first", "category": "duplicate"}],
        [],
        [{"rule": "repeated_text", "field": "second", "category": "duplicate"}],
    ]
    assert all("late match" not in row["duplicates_causes"] for row in rows)


def test_overlapping_and_within_row_groups_keep_all_static_causes() -> None:
    rows = annotate(
        [
            {"first": "alpha", "second": "beta"},
            {"first": "alpha", "second": "alpha"},
            {"first": "gamma", "second": "beta"},
        ]
    )
    assert [row["duplicates_valid"] for row in rows] == [False, False, False]
    assert [[cause["field"] for cause in json.loads(row["duplicates_causes"])] for row in rows] == [
        ["first", "second"],
        ["first", "second"],
        ["second"],
    ]


def test_exact_strings_are_not_trimmed_casefolded_or_unicode_normalized() -> None:
    rows = annotate([{"first": "Text", "second": "text"}, {"first": " Text", "second": "Text "}, {"first": "é", "second": "e\u0301"}])
    assert [row["duplicates_valid"] for row in rows] == [True, True, True]


def test_independent_rules_do_not_merge_domains_and_missing_cells_are_member_local() -> None:
    rows = annotate(
        [{"first": "same", "second": "other"}, {"first": "different", "second": "same"}, {"first": "last"}],
        rules=[{"name": "one", "cells": [{"field": "first"}]}, {"name": "two", "cells": [{"field": "second"}]}],
    )
    assert [row["duplicates_valid"] for row in rows] == [True, True, False]
    assert json.loads(rows[2]["duplicates_causes"]) == [{"rule": "two", "field": "second", "category": "invalid_string"}]


def test_single_member_keeps_nullable_metadata_and_declares_annotation_types() -> None:
    row = make_pipeline_row({"first": "a", "second": "b", "optional": None})
    result = BatchDuplicateAnnotator(options()).process([row], context(1))
    assert result.status == "success" and result.rows is not None
    output = result.rows[0]
    before = {field.normalized_name: field for field in row.contract.fields}
    after = {field.normalized_name: field for field in output.contract.fields}
    assert all(after[name] == field for name, field in before.items())
    assert after["duplicates_valid"].python_type is bool
    assert after["duplicates_causes"].python_type is str
    assert output.to_dict() == {"first": "a", "second": "b", "optional": None, "duplicates_valid": True, "duplicates_causes": "[]"}


@pytest.mark.parametrize("excess", [False, True], ids=["at-limit", "over-limit"])
def test_complete_window_row_capacity_never_splits_or_drops_members(excess: bool) -> None:
    rows = [make_pipeline_row({"first": str(index), "second": f"other-{index}"}) for index in range(4096 + excess)]
    result = BatchDuplicateAnnotator(options()).process(rows, context(len(rows)))
    if excess:
        assert result.status == "error" and result.reason["cause"] == "comparison_capacity_exceeded"
        assert result.rows is None
    else:
        assert result.status == "success" and result.rows is not None and len(result.rows) == 4096
        assert all(row["duplicates_valid"] is True for row in result.rows)


def test_comparison_character_capacity_counts_exempt_valid_text_and_refuses_whole_window() -> None:
    transform = BatchDuplicateAnnotator(options(rules=[{"name": "same", "cells": [{"field": "first", "when": "False"}]}]))
    rows = [make_pipeline_row({"first": "x" * (4 * 1024 * 1024 + 1)})]
    result = transform.process(rows, context(1))
    assert result.status == "error" and result.reason["cause"] == "comparison_capacity_exceeded"
    assert result.rows is None


@pytest.mark.parametrize("invalid", [None, 3, True, ["not text"]])
def test_malformed_cell_invalidates_only_its_member_and_other_cells_still_group(invalid) -> None:
    rows = annotate(
        [{"first": invalid, "second": "shared"}, {"first": "shared", "second": "different"}, {"first": "alone", "second": "untouched"}]
    )
    assert [row["duplicates_valid"] for row in rows] == [False, False, True]
    assert {cause["category"] for cause in json.loads(rows[0]["duplicates_causes"])} == {"invalid_string", "duplicate"}


def test_eligibility_exempts_only_valid_strings_and_fails_closed_on_missing_or_nonboolean() -> None:
    rules = [{"name": "same", "cells": [{"field": "first", "when": "row['eligible']"}]}]
    rows = annotate(
        [
            {"first": "same", "eligible": False},
            {"first": "same", "eligible": True},
            {"first": None, "eligible": False},
            {"first": "other", "eligible": 1},
            {"first": "last"},
        ],
        rules=rules,
    )
    assert [row["duplicates_valid"] for row in rows] == [True, True, False, False, False]
    assert [json.loads(row["duplicates_causes"])[0]["category"] for row in rows[2:]] == [
        "invalid_string",
        "eligibility_not_boolean",
        "eligibility_evaluation_error",
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"is_end_of_source": False},
        {"trigger_type": "count"},
        {"flush_index": 2},
        {"row_start": 2, "row_end": 2, "rows_seen_total": 2},
        {"batch_size": 2},
    ],
)
def test_incomplete_or_noninitial_window_cannot_admit_rows(change) -> None:
    transform = BatchDuplicateAnnotator(options())
    ctx = context(1)
    ctx.aggregation_batch = replace(ctx.aggregation_batch, **change)
    result = transform.process([make_pipeline_row({"first": "a", "second": "b"})], ctx)
    assert result.status == "error" and result.reason["cause"] == "complete_source_window_required"


def test_missing_context_and_empty_input_are_explicit_refusals() -> None:
    transform = BatchDuplicateAnnotator(options())
    assert transform.process([], make_context()).status == "error"
    result = transform.process([make_pipeline_row({"first": "a", "second": "b"})], make_context())
    assert result.status == "error" and result.reason["cause"] == "complete_source_window_required"


def test_output_original_header_alias_is_refused_before_annotation() -> None:
    row = PipelineRow(
        {"first": "a", "second": "b", "carried": "original"},
        SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract("first", "first", str, True, "declared"),
                FieldContract("second", "second", str, True, "declared"),
                FieldContract("carried", "duplicates_valid", str, True, "declared"),
            ),
            locked=True,
        ),
    )
    result = BatchDuplicateAnnotator(options()).process([row], context(1))
    assert result.status == "error" and result.reason["cause"] == "output_field_collision"


def test_comparison_reads_canonical_value_when_original_alias_shadows_its_name() -> None:
    shadowed = PipelineRow(
        {"first": "match", "renamed": "different", "second": "unique"},
        SchemaContract(
            mode="FLEXIBLE",
            fields=(
                FieldContract("renamed", "first", str, True, "declared"),
                FieldContract("second", "second", str, True, "declared"),
            ),
            locked=True,
        ),
    )
    other = PipelineRow({"first": "match", "renamed": "also different", "second": "last"}, shadowed.contract)
    result = BatchDuplicateAnnotator(options()).process([shadowed, other], context(2))
    assert result.status == "success" and result.rows is not None
    assert [row["duplicates_valid"] for row in result.rows] == [False, False]
    assert all(
        json.loads(row["duplicates_causes"]) == [{"rule": "repeated_text", "field": "first", "category": "duplicate"}]
        for row in result.rows
    )
    assert result.rows[0].to_dict()["first"] == "match"


@pytest.mark.parametrize(
    "rules",
    [
        [],
        [{"name": "same", "cells": [{"field": "first"}, {"field": "first"}]}],
        [{"name": "unsafe", "cells": [{"field": "first", "when": "row.__class__"}]}],
        [{"name": "alias", "cells": [{"field": "Original Header"}]}],
    ],
)
def test_ambiguous_or_unsafe_configuration_is_rejected(rules) -> None:
    with pytest.raises(PluginConfigError):
        BatchDuplicateAnnotator(options(rules=rules))


def test_declared_reads_are_honest_without_poisoning_batch_presence_requirements() -> None:
    transform = BatchDuplicateAnnotator(options(rules=[{"name": "same", "cells": [{"field": "first", "when": "row['eligible']"}]}]))
    assert {"first", "eligible"} <= transform.consumed_input_fields
    assert not transform.schema_required_input_fields()
    assert {field.name: field.field_type for field in transform.created_output_fields()} == {
        "duplicates_valid": "bool",
        "duplicates_causes": "str",
    }


@pytest.mark.parametrize("trigger", [{}, {"count": 1}, {"timeout_seconds": 1}, {"condition": "True"}])
def test_real_runtime_and_composer_both_refuse_early_trigger_before_intake(tmp_path, trigger) -> None:
    import yaml

    from elspeth.config_loading import load_settings_from_yaml_string
    from elspeth.web.composer.state import _batch_aware_placement_error
    from tests.integration.pipeline.test_expression_type_build_controls import _graph

    source = tmp_path / "input.csv"
    source.write_text("first,second\na,b\n")
    settings = load_settings_from_yaml_string(
        yaml.safe_dump(
            {
                "sources": {
                    "src": {
                        "plugin": "csv",
                        "on_success": "raw",
                        "options": {
                            "path": str(source),
                            "schema": {"mode": "fixed", "fields": ["first: str", "second: str"]},
                            "on_validation_failure": "discard",
                        },
                    }
                },
                "aggregations": [
                    {
                        "name": "comparison",
                        "plugin": "batch_duplicate_annotator",
                        "input": "raw",
                        "on_success": "output",
                        "on_error": "discard",
                        "trigger": trigger,
                        "output_mode": "passthrough",
                        "options": options(),
                    }
                ],
                "sinks": {
                    "output": {
                        "plugin": "json",
                        "on_write_failure": "discard",
                        "options": {"path": str(tmp_path / "output.jsonl"), "schema": {"mode": "observed"}},
                    }
                },
            }
        )
    )
    message = _batch_aware_placement_error("comparison", "aggregation", "batch_duplicate_annotator", "passthrough", trigger)
    if trigger:
        assert message is not None and "EOF-only" in message
        with pytest.raises(ValueError, match="EOF-only"):
            _graph(settings)
    else:
        assert message is None
        graph = _graph(settings)
        assert "comparison" in graph.get_aggregation_id_map()
