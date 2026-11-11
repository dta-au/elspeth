"""Regression tests for the 20261111-plugins-review P2 transform findings.

Each class pins one sweep finding against the plugin's real behaviour.
"""

from typing import Any

import pytest

from elspeth.contracts.errors import PluginContractViolation
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.reference_join import ReferenceJoin
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context


def _reference_join(reference_content: str) -> ReferenceJoin:
    options: dict[str, Any] = {
        "schema": {"mode": "observed"},
        "reference_content": reference_content,
        "reference_format": "json",
        "key_field": "k",
        "reference_key_name": "sku",
        "output": {"r": "ref['rate']"},
    }
    return ReferenceJoin(options)


class TestReferenceJoinRefusesNonCanonicalValuesAtLoad:
    """Sweep finding #60: a value the row hasher refuses must fail config load, not the first matching row."""

    @pytest.mark.parametrize(
        "rate_literal",
        [
            pytest.param("NaN", id="nan"),
            pytest.param("Infinity", id="infinity"),
            pytest.param("-Infinity", id="negative-infinity"),
            pytest.param("1e999", id="overflowing-float-literal"),
            pytest.param("12345678901234567890", id="int-above-safe-range"),
            pytest.param("-9007199254740992", id="int-below-safe-range"),
            pytest.param('{"inner": [1, NaN]}', id="nested-nan"),
        ],
    )
    def test_non_canonical_value_is_refused_naming_entry_and_field(self, rate_literal: str) -> None:
        content = '[{"sku": "ok", "rate": 1}, {"sku": "bad", "rate": ' + rate_literal + "}]"
        with pytest.raises(PluginConfigError) as excinfo:
            _reference_join(content)
        message = str(excinfo.value)
        assert "'r'" in message
        assert "'bad'" in message

    def test_safe_range_boundary_integer_still_loads_and_joins(self) -> None:
        transform = _reference_join('[{"sku": "a", "rate": 9007199254740991}, {"sku": "b", "rate": -9007199254740991}]')
        result = transform.process(make_pipeline_row({"k": "a"}), make_context())
        assert result.status == "success"
        assert result.row is not None
        assert result.row["r"] == 9007199254740991


def _header_row() -> PipelineRow:
    """An OBSERVED row whose ``first_name`` came from the original header ``First Name``."""
    return PipelineRow(
        {"first_name": "A", "meta": {"origin": "X"}, "status": "ORIGINAL"},
        SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(normalized_name="first_name", original_name="First Name", python_type=str, required=False, source="inferred"),
                FieldContract(normalized_name="meta", original_name="meta", python_type=object, required=False, source="inferred"),
                FieldContract(normalized_name="status", original_name="status", python_type=str, required=False, source="inferred"),
            ),
            locked=True,
        ),
    )


class TestFieldMapperDottedSourceCollision:
    """Sweep finding #67: a dotted source must not silently overwrite an existing target when the executor gate is off."""

    def test_dotted_source_onto_existing_field_is_refused(self) -> None:
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"schema": {"mode": "observed"}, "mapping": {"First Name": "first", "meta.origin": "status"}})
        # The original-header source is what turns the executor collision gate off.
        assert transform.forwards_input_fields is False
        assert transform.passes_through_input is False

        with pytest.raises(PluginContractViolation, match=r"would overwrite existing input fields \['status'\]"):
            transform.process(_header_row(), make_context())

    def test_dotted_source_onto_new_field_still_maps(self) -> None:
        from elspeth.plugins.transforms.field_mapper import FieldMapper

        transform = FieldMapper({"schema": {"mode": "observed"}, "mapping": {"First Name": "first", "meta.origin": "origin"}})
        result = transform.process(_header_row(), make_context())
        assert result.status == "success"
        assert result.row is not None
        assert result.row["origin"] == "X"
        assert result.row["status"] == "ORIGINAL"
