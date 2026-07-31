"""Focused reference-content tests for the CSV source."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from elspeth.core.config import load_bounded_pipeline_yaml
from elspeth.plugins.sources.csv_source import CSVSource
from tests.fixtures.catalog_reference import (
    BuiltinReference,
    assert_reference_tags,
    assert_reference_text,
    parse_and_validate_example,
)


def test_csv_source_reference_content_is_specific_and_valid() -> None:
    assert_reference_text(CSVSource)
    assert_reference_tags(CSVSource)
    parse_and_validate_example(BuiltinReference("source", CSVSource))


def test_csv_source_guidance_describes_its_boundary_and_exclusions() -> None:
    when_to_use = CSVSource.usage_when_to_use.casefold()
    when_not_to_use = CSVSource.usage_when_not_to_use.casefold()
    assert all(term in when_to_use for term in ("finite", "coerc", "incremental"))
    assert all(term in when_not_to_use for term in ("inline", "unbounded", "http"))


def test_csv_source_example_has_current_shape_schema_and_routing() -> None:
    parsed = load_bounded_pipeline_yaml(CSVSource.example_use)
    assert set(parsed) == {"sources"}
    sources = cast(Mapping[str, object], parsed["sources"])
    assert set(sources) == {"primary"}
    source = cast(Mapping[str, Any], sources["primary"])
    assert source["plugin"] == CSVSource.name
    assert source["on_success"] == "output"
    assert source["options"] == {
        "path": "data/input.csv",
        "schema": {"mode": "observed"},
        "on_validation_failure": "discard",
    }


def test_csv_source_has_exact_discovery_tags() -> None:
    assert CSVSource.capability_tags == ("csv", "file", "batch", "tabular")


def test_csv_source_declared_audit_characteristics_includes_coerce() -> None:
    """CSV source coerces external string data to typed columns per
    Tier-3 boundary rules; that's a notable audit trait worth declaring."""
    assert "coerce" in CSVSource.audit_characteristics
