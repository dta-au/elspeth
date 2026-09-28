"""Composer preserves configured node attribution for expression type refusals."""

from __future__ import annotations

from pathlib import Path

import yaml

from elspeth.core.dag.models import GraphValidationError
from elspeth.plugins.transforms.value_transform import ValueTransform
from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml


def test_composer_bind_refusal_names_the_configured_transform(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "input.csv"
    source.write_text("x\n1\n")
    document = {
        "sources": {
            "src": {
                "plugin": "csv",
                "on_success": "raw",
                "options": {"path": str(source), "on_validation_failure": "discard", "schema": {"mode": "fixed", "fields": ["x: int"]}},
            }
        },
        "transforms": [
            {
                "name": "calculate",
                "plugin": "value_transform",
                "input": "raw",
                "on_success": "out",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "flexible", "fields": ["x: int"]},
                    "operations": [{"target": "x", "expression": "row['x'] > 0"}],
                },
            }
        ],
        "sinks": {
            "out": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {"path": str(tmp_path / "out.jsonl"), "schema": {"mode": "observed"}},
            }
        },
    }
    state = composition_state_from_runtime_yaml(yaml.safe_dump(document, sort_keys=False))
    observed_attribution: list[tuple[str | None, str | None]] = []
    original_bind = ValueTransform.bind_upstream_input_types

    def capture_bind(self: ValueTransform, fields, *, component_id: str | None = None) -> None:
        try:
            original_bind(self, fields, component_id=component_id)
        except GraphValidationError as exc:
            observed_attribution.append((exc.component_id, exc.component_type))
            raise

    with monkeypatch.context() as patcher:
        patcher.setattr(ValueTransform, "bind_upstream_input_types", capture_bind)
        result = state.validate()

    assert observed_attribution == [("calculate", "transform")]
    [error] = [entry for entry in result.errors if entry.error_code == "value_transform_result_type_incompatible"]
    assert error.component == "node:calculate"
    assert not result.is_valid
