"""Generate frontend/evaluation cases through the actual session HTTP producer.

Run from a checkout with both source roots on PYTHONPATH:
python -m tests.fixtures.web.composer.generate_composition_state_validation_errors
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from uuid import UUID

from pydantic import JsonValue

from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.sessions.protocol import CompositionStateRecord, CompositionValidationError
from elspeth.web.sessions.routes._helpers import _state_response


class CompositionStateValidationFixture(TypedDict):
    states: dict[str, dict[str, JsonValue]]
    versions: list[dict[str, JsonValue]]


def composition_state_validation_error_cases() -> CompositionStateValidationFixture:
    """Return real CompositionStateResponse JSON, preserving collection presence."""
    composition = CompositionState(
        version=1,
        sources={
            "primary": SourceSpec(
                plugin="csv",
                on_success="rows",
                options={"path": "input.csv", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
                id="classify",
                node_type="transform",
                plugin="passthrough",
                input="rows",
                on_success="out",
                on_error="discard",
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        outputs=(OutputSpec(name="out", plugin="json", options={"path": "output.json"}, on_write_failure="discard"),),
        edges=(),
        metadata=PipelineMetadata(name="HTTP fixture", description=""),
    ).to_dict()
    errors = {
        "null": None,
        "empty": (),
        "coded": (
            CompositionValidationError(message="Review is pending", error_code="interpretation_review_pending", component="classify"),
        ),
        "unexpected": (CompositionValidationError(message="Output is invalid", error_code="invalid_output_path", component="out"),),
        "uncoded": (CompositionValidationError(message="interpretation_review_pending: message only", error_code=None, component=None),),
        "guided_invalid": (
            CompositionValidationError(message="guided_composition_invalid", error_code="guided_composition_invalid", component=None),
        ),
    }
    result = {}
    for name, values in errors.items():
        record = CompositionStateRecord(
            id=UUID("00000000-0000-4000-8000-000000000001"),
            session_id=UUID("00000000-0000-4000-8000-000000000002"),
            version=1,
            sources=composition["sources"],
            nodes=composition["nodes"],
            edges=composition["edges"],
            outputs=composition["outputs"],
            metadata_=composition["metadata"],
            is_valid=name in {"null", "empty"},
            validation_errors=values,
            created_at=datetime(2026, 9, 12, tzinfo=UTC),
            derived_from_state_id=None,
        )
        result[name] = _state_response(record).model_dump(mode="json")
    return {"states": result, "versions": list(result.values())}


def main() -> None:
    output = Path(__file__).with_name("composition_state_validation_errors.json")
    output.write_text(json.dumps(composition_state_validation_error_cases(), indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
