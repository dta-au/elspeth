"""Per-top-level-key projection budget for ``redact_tool_call_response``.

The projection budget (depth / width / node count) used to run over the RAW
response before any per-key disposition. A single wide subtree — including
one the declarative path discards unread (an undeclared ``data`` key) —
replaced the whole persisted row with ``{_redaction_status:
response_projection_limit}`` and dropped the ``success`` / ``validation`` /
``version`` framing. The unfiltered ``list_models`` discovery call (87
providers under ``data.providers``) collapsed on every call.

The bound now applies per top-level key: unknown and sensitive keys are never
walked (they become fixed sentinels), an over-budget known key degrades to a
per-key sentinel, and only the cases with no honest per-key disposition keep
the whole-row stub.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from elspeth.web.composer.redaction import (
    REDACTED_SENSITIVE_NO_SUMMARIZER,
    REDACTED_UNKNOWN_RESPONSE_FIELD,
    REDACTED_UNKNOWN_RESPONSE_KEY,
    RESPONSE_PROJECTION_MAX_DEPTH,
    redact_tool_call_response,
)
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry

_STUB = {"_redaction_status": "response_projection_limit"}
_PER_KEY_SENTINEL = "<redacted-response-projection-limit>"
_OVER_WIDTH = 65  # RESPONSE_PROJECTION_MAX_CONTAINER_WIDTH + 1
# upsert_node is a declarative manifest entry that declares ``data`` known;
# set_pipeline is type-driven with ``data`` optional on its response model.
_DEPTH_PATHS = pytest.mark.parametrize("tool_name", ["upsert_node", "set_pipeline"], ids=["declarative", "type-driven"])


def _nested(container_levels: int) -> object:
    """A scalar wrapped in ``container_levels`` lists: the scalar sits that many levels below the value."""
    value: object = "external"
    for _ in range(container_levels):
        value = [value]
    return value


def _empty_validation() -> dict[str, Any]:
    return {
        "is_valid": True,
        "errors": [],
        "warnings": [],
        "suggestions": [],
        "semantic_contracts": [],
        "graph_repair_suggestions": [],
    }


def _envelope(*, success: bool, version: int, **extra: Any) -> dict[str, Any]:
    return {
        "success": success,
        "validation": _empty_validation(),
        "affected_nodes": [],
        "version": version,
        **extra,
    }


def test_declarative_unknown_wide_data_keeps_envelope_framing() -> None:
    # list_models declares no known_response_keys, so ``data`` is an unknown
    # key: it must become the unknown-key sentinel without being walked, and
    # the envelope framing must survive.
    providers = [{"provider": f"provider-{index}/", "count": index} for index in range(87)]
    response = _envelope(success=True, version=4, data={"providers": providers})

    result = redact_tool_call_response("list_models", response, telemetry=NoopRedactionTelemetry())

    assert result != _STUB
    assert result["success"] is True
    assert result["version"] == 4
    assert result["validation"]["is_valid"] is True
    assert result[REDACTED_UNKNOWN_RESPONSE_FIELD] == REDACTED_UNKNOWN_RESPONSE_KEY
    assert "provider-0/" not in json.dumps(result)


def test_real_unfiltered_list_models_call_keeps_envelope_framing() -> None:
    from elspeth.web.composer.state import CompositionState, PipelineMetadata
    from elspeth.web.composer.tools.generation import _execute_list_models

    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    response = _execute_list_models({}, state, None).to_dict()
    # Precondition: the default discovery call really is wider than the
    # container-width budget, otherwise this test proves nothing.
    assert len(response["data"]["providers"]) > 64

    result = redact_tool_call_response("list_models", response, telemetry=NoopRedactionTelemetry())

    assert result != _STUB
    assert result["success"] is True
    assert result["version"] == 1
    assert set(result) >= {"success", "validation", "affected_nodes", "version"}


def test_declarative_unknown_key_is_never_walked() -> None:
    nested: object = "leaf"
    for _ in range(1200):
        nested = {"items": nested}
    response = _envelope(success=False, version=2, stray=nested)

    result = redact_tool_call_response("list_models", response, telemetry=NoopRedactionTelemetry())

    assert result["success"] is False
    assert result["version"] == 2
    assert result[REDACTED_UNKNOWN_RESPONSE_FIELD] == REDACTED_UNKNOWN_RESPONSE_KEY


def test_declarative_sensitive_key_is_never_walked() -> None:
    # request_advisor_hint declares ``guidance`` sensitive: the no-summarizer
    # sentinel applies whatever its size, and the framing survives.
    response = {"status": "SUCCESS", "guidance": ["advice"] * 20_000}

    result = redact_tool_call_response("request_advisor_hint", response, telemetry=NoopRedactionTelemetry())

    assert result == {"status": "SUCCESS", "guidance": REDACTED_SENSITIVE_NO_SUMMARIZER}


def test_declarative_known_key_over_budget_degrades_to_per_key_sentinel() -> None:
    # upsert_node declares ``data`` known: an over-wide ``data`` loses only
    # itself; success=False (read by the REJECTED outcome label) survives.
    response = _envelope(success=False, version=3, data=["external"] * _OVER_WIDTH)

    result = redact_tool_call_response("upsert_node", response, telemetry=NoopRedactionTelemetry())

    assert result == {
        "success": False,
        "validation": _empty_validation(),
        "affected_nodes": [],
        "version": 3,
        "data": _PER_KEY_SENTINEL,
    }


def test_declarative_envelope_key_over_budget_degrades_to_per_key_sentinel() -> None:
    response = _envelope(success=False, version=5)
    response["affected_nodes"] = [f"node_{index}" for index in range(_OVER_WIDTH)]

    result = redact_tool_call_response("upsert_node", response, telemetry=NoopRedactionTelemetry())

    assert result["success"] is False
    assert result["version"] == 5
    assert result["affected_nodes"] == _PER_KEY_SENTINEL
    assert list(result) == list(response)


def test_type_driven_optional_key_over_budget_degrades_to_per_key_sentinel() -> None:
    response = _envelope(success=False, version=9, data={"items": ["external"] * _OVER_WIDTH})

    result = redact_tool_call_response("set_pipeline", response, telemetry=NoopRedactionTelemetry())

    assert result["success"] is False
    assert result["version"] == 9
    assert result["data"] == _PER_KEY_SENTINEL
    assert list(result) == list(response)
    assert "external" not in json.dumps(result)


def test_type_driven_required_key_over_budget_keeps_whole_row_stub() -> None:
    # Residual: a REQUIRED typed field (validation / affected_nodes) cannot be
    # replaced by a string sentinel before model validation, so this case
    # still collapses the whole row (see the lane report's open items).
    response = _envelope(success=True, version=1)
    response["affected_nodes"] = [f"node_{index}" for index in range(_OVER_WIDTH)]

    result = redact_tool_call_response("set_pipeline", response, telemetry=NoopRedactionTelemetry())

    assert result == _STUB


def test_top_level_width_over_budget_keeps_whole_row_stub() -> None:
    response: dict[str, Any] = {f"key_{index}": index for index in range(_OVER_WIDTH)}

    result = redact_tool_call_response("list_models", response, telemetry=NoopRedactionTelemetry())

    assert result == _STUB


def test_within_budget_response_is_unchanged_by_the_per_key_bound() -> None:
    response = _envelope(success=True, version=6, data=["external"] * 64)

    result = redact_tool_call_response("upsert_node", response, telemetry=NoopRedactionTelemetry())

    assert result["data"] != _PER_KEY_SENTINEL
    assert result["success"] is True


@_DEPTH_PATHS
def test_top_level_value_one_level_inside_the_depth_budget_is_projected(tool_name: str) -> None:
    """A top-level value is depth 1, so its scalar at MAX_DEPTH - 1 container levels sits exactly at the limit."""
    response = _envelope(success=False, version=7, data=_nested(RESPONSE_PROJECTION_MAX_DEPTH - 1))

    result = redact_tool_call_response(tool_name, response, telemetry=NoopRedactionTelemetry())

    assert result["data"] != _PER_KEY_SENTINEL
    assert result["success"] is False
    assert result["version"] == 7
    assert "external" not in json.dumps(result)


@_DEPTH_PATHS
def test_top_level_value_at_the_depth_budget_degrades_to_per_key_sentinel(tool_name: str) -> None:
    response = _envelope(success=False, version=8, data=_nested(RESPONSE_PROJECTION_MAX_DEPTH))

    result = redact_tool_call_response(tool_name, response, telemetry=NoopRedactionTelemetry())

    assert result["data"] == _PER_KEY_SENTINEL
    assert result["success"] is False
    assert result["version"] == 8
    assert list(result) == list(response)
