"""CSV column ordering follows schema/row order, never display-map order."""

from pathlib import Path
from typing import Any

import pytest

from elspeth.plugins.sinks.csv_sink import CSVSink


@pytest.mark.parametrize("schema_mode", ["observed", "fixed"])
@pytest.mark.parametrize("custom_headers", [False, True])
def test_csv_column_order_is_independent_of_display_mapping(tmp_path: Path, schema_mode: str, custom_headers: bool) -> None:
    schema: dict[str, Any] = {"mode": schema_mode}
    if schema_mode == "fixed":
        schema["fields"] = ["id: str", "qty: str", "price: str"]
    config: dict[str, Any] = {"path": str(tmp_path / "quarantine.csv"), "schema": schema}
    display_names = {"qty": "Quantity", "id": "Order", "price": "Price"}
    if custom_headers:
        config["headers"] = display_names
    sink = CSVSink(config)

    fields, display = sink._get_field_names_and_display({"id": "o2", "price": "5", "qty": "bad"})

    expected_fields = ["id", "qty", "price"] if schema_mode == "fixed" else ["id", "price", "qty"]
    assert fields == expected_fields
    assert display == ([display_names[field] for field in expected_fields] if custom_headers else expected_fields)


def test_csv_assistance_teaches_order_and_display_as_separate_controls() -> None:
    assistance = CSVSink.get_agent_assistance()
    assert assistance is not None
    ordering_hints = [hint for hint in assistance.composer_hints if "column order" in hint]
    assert len(ordering_hints) == 1
    hint = ordering_hints[0]
    assert "schema.fields" in hint
    assert "fixed" in hint
    assert "observed" in hint
    assert "headers" in hint and "display names" in hint
