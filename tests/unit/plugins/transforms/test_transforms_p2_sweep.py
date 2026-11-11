"""Regression tests for the 20261111-plugins-review P2 transform findings.

Each class pins one sweep finding against the plugin's real behaviour.
"""

from typing import Any

import pytest

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
