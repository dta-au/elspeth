"""Focused reference-content tests for the CSV source."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from elspeth.contracts.contexts import SourceContext
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
    """CSV can coerce external strings when a schema declares typed fields."""
    assert "coerce" in CSVSource.audit_characteristics


def test_csv_catalog_coercion_claim_distinguishes_observed_from_typed_modes(tmp_path: Path) -> None:
    """The catalog must describe ``coerce`` as a schema-dependent capability.

    CSV parsing produces strings. Observed mode has no declared types and
    therefore preserves those strings, while fixed and flexible schemas do
    coerce their declared fields at the Tier-3 source boundary. The static
    catalog metadata must say so instead of promising coercion for the default
    observed-mode example.
    """

    class _Context:
        def record_validation_error(self, **kwargs: object) -> None:
            raise AssertionError(f"unexpected validation error: {kwargs}")

    csv_path = tmp_path / "amount.csv"
    csv_path.write_text("amount\n250.00\n")
    context = cast(SourceContext, _Context())

    def loaded_amount(schema: Mapping[str, object]) -> object:
        source = CSVSource(
            {
                "path": str(csv_path),
                "schema": dict(schema),
                "on_validation_failure": "discard",
            }
        )
        [row] = list(source.load(context))
        return row.row["amount"]

    observed_amount = loaded_amount({"mode": "observed"})
    assert observed_amount == "250.00"
    assert type(observed_amount) is str
    for mode in ("fixed", "flexible"):
        amount = loaded_amount({"mode": mode, "fields": ["amount: float"]})
        assert amount == 250.0
        assert type(amount) is float

    assistance = CSVSource.get_agent_assistance(issue_code=None)
    assert assistance is not None
    catalog_copy = "\n".join(
        (
            CSVSource.usage_when_to_use,
            assistance.summary,
        )
    ).casefold()
    assert "observed" in catalog_copy
    assert "string" in catalog_copy
    assert "fixed" in catalog_copy
    assert "flexible" in catalog_copy
    assert "declared" in catalog_copy
