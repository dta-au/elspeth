"""Telemetry for every projection-budget branch of ``redact_tool_call_response``.

The per-key projection budget degrades an over-budget top-level value to a
per-key sentinel, and three residual cases still replace the whole row with
``{_redaction_status: response_projection_limit}``. Both outcomes drop data
from the persisted audit row, so each one must be counted: a silent limit
branch hides how often audit rows lose their payload. ``scope`` is ``"key"``
for a per-key degrade and ``"row"`` for the whole-row stub.
"""

from __future__ import annotations

from typing import Any

import pytest

import elspeth.web.composer.redaction as redaction_mod
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry

_STUB = {"_redaction_status": "response_projection_limit"}
_PER_KEY_SENTINEL = "<redacted-response-projection-limit>"
_OVER_WIDTH = 65  # RESPONSE_PROJECTION_MAX_CONTAINER_WIDTH + 1


def _envelope(*, success: bool, version: int, **extra: Any) -> dict[str, Any]:
    return {
        "success": success,
        "validation": {
            "is_valid": True,
            "errors": [],
            "warnings": [],
            "suggestions": [],
            "semantic_contracts": [],
            "graph_repair_suggestions": [],
        },
        "affected_nodes": [],
        "version": version,
        **extra,
    }


def test_top_level_width_stub_counts_a_row_limit() -> None:
    telemetry = NoopRedactionTelemetry()
    response: dict[str, Any] = {f"key_{index}": index for index in range(_OVER_WIDTH)}

    result = redact_tool_call_response("list_models", response, telemetry=telemetry)

    assert result == _STUB
    assert telemetry.response_projection_limit_calls == [{"tool_name": "list_models", "scope": "row"}]


def test_type_driven_required_key_stub_counts_a_row_limit() -> None:
    telemetry = NoopRedactionTelemetry()
    response = _envelope(success=True, version=1)
    response["affected_nodes"] = [f"node_{index}" for index in range(_OVER_WIDTH)]

    result = redact_tool_call_response("set_pipeline", response, telemetry=telemetry)

    assert result == _STUB
    assert telemetry.response_projection_limit_calls == [{"tool_name": "set_pipeline", "scope": "row"}]


def test_type_driven_undeclared_key_stub_counts_a_row_limit() -> None:
    telemetry = NoopRedactionTelemetry()
    response = _envelope(success=True, version=1, undeclared=["external"] * _OVER_WIDTH)

    result = redact_tool_call_response("set_pipeline", response, telemetry=telemetry)

    assert result == _STUB
    assert telemetry.response_projection_limit_calls == [{"tool_name": "set_pipeline", "scope": "row"}]


def test_type_driven_optional_key_degrade_counts_a_key_limit() -> None:
    telemetry = NoopRedactionTelemetry()
    response = _envelope(success=False, version=9, data={"items": ["external"] * _OVER_WIDTH})

    result = redact_tool_call_response("set_pipeline", response, telemetry=telemetry)

    assert result["data"] == _PER_KEY_SENTINEL
    assert telemetry.response_projection_limit_calls == [{"tool_name": "set_pipeline", "scope": "key"}]


def test_declarative_known_key_degrade_counts_a_key_limit() -> None:
    telemetry = NoopRedactionTelemetry()
    response = _envelope(success=False, version=3, data=["external"] * _OVER_WIDTH)

    result = redact_tool_call_response("upsert_node", response, telemetry=telemetry)

    assert result["data"] == _PER_KEY_SENTINEL
    assert telemetry.response_projection_limit_calls == [{"tool_name": "upsert_node", "scope": "key"}]


@pytest.mark.parametrize(
    ("tool_name", "over_budget_values"),
    [
        (
            "upsert_node",
            {
                "data": ["external"] * _OVER_WIDTH,
                "affected_nodes": [f"node_{index}" for index in range(_OVER_WIDTH)],
            },
        ),
        (
            # Both keys are optional on the typed model, so neither forces the
            # whole-row stub and each takes the per-key sentinel.
            "set_pipeline",
            {
                "data": {"items": ["external"] * _OVER_WIDTH},
                "runtime_preflight": {"items": ["external"] * _OVER_WIDTH},
            },
        ),
    ],
    ids=["declarative", "type-driven"],
)
def test_each_degraded_key_is_counted_once(tool_name: str, over_budget_values: dict[str, Any]) -> None:
    telemetry = NoopRedactionTelemetry()
    response = _envelope(success=False, version=3)
    response.update(over_budget_values)

    result = redact_tool_call_response(tool_name, response, telemetry=telemetry)

    for key in over_budget_values:
        assert result[key] == _PER_KEY_SENTINEL
    assert telemetry.response_projection_limit_calls == [
        {"tool_name": tool_name, "scope": "key"},
        {"tool_name": tool_name, "scope": "key"},
    ]


@pytest.mark.parametrize("tool_name", ["upsert_node", "set_pipeline"], ids=["declarative", "type-driven"])
def test_output_byte_budget_stub_counts_a_row_limit(monkeypatch: pytest.MonkeyPatch, tool_name: str) -> None:
    # Every top-level value is within the depth/width/node budget, so the only
    # limit that can fire is the projected row's output byte budget.
    monkeypatch.setattr(redaction_mod, "RESPONSE_PROJECTION_MAX_OUTPUT_BYTES", 1)
    telemetry = NoopRedactionTelemetry()

    result = redact_tool_call_response(tool_name, _envelope(success=True, version=2), telemetry=telemetry)

    assert result == _STUB
    assert telemetry.response_projection_limit_calls == [{"tool_name": tool_name, "scope": "row"}]


@pytest.mark.parametrize("tool_name", ["upsert_node", "set_pipeline", "list_models"])
def test_within_budget_response_counts_no_limit(tool_name: str) -> None:
    telemetry = NoopRedactionTelemetry()

    result = redact_tool_call_response(tool_name, _envelope(success=True, version=2), telemetry=telemetry)

    assert result != _STUB
    assert telemetry.response_projection_limit_calls == []


def test_already_stubbed_row_is_not_counted_again() -> None:
    telemetry = NoopRedactionTelemetry()

    result = redact_tool_call_response("list_models", dict(_STUB), telemetry=telemetry)

    assert result == _STUB
    assert telemetry.response_projection_limit_calls == []
