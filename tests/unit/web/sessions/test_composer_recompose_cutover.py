"""Live declaration and durable DTO controls for the recompose cutover."""

from copy import deepcopy
from typing import get_type_hints

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from elspeth.web.sessions import schemas
from elspeth.web.sessions.routes.composer import compose
from elspeth.web.sessions.schemas import ComposerOperationAcceptedResponse, RecomposeRequest


def _assert_durable_recompose_schema(schema: dict) -> None:
    assert {"operation_id", "expected_user_message_id"} <= set(schema["required"])
    assert set(schema["properties"]) == {"operation_id", "expected_user_message_id", "state_id"}
    assert schema["additionalProperties"] is False


def test_mounted_recompose_advertises_durable_admission_and_canonical_request() -> None:
    app = FastAPI()
    app.include_router(compose.router, prefix="/api/sessions")
    route = next(route for route in app.routes if isinstance(route, APIRoute) and route.endpoint is compose.recompose)
    assert route.status_code == 202
    assert route.response_model is ComposerOperationAcceptedResponse
    operation = app.openapi()["paths"]["/api/sessions/{session_id}/recompose"]["post"]
    assert operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ComposerOperationAcceptedResponse"
    }
    assert "200" not in operation["responses"]
    assert get_type_hints(compose.parse_recompose_body)["return"] is RecomposeRequest
    _assert_durable_recompose_schema(RecomposeRequest.model_json_schema())
    assert "LegacyRecomposeRequest" not in vars(schemas)


def test_durable_recompose_schema_control_rejects_missing_operation_identity() -> None:
    schema = deepcopy(RecomposeRequest.model_json_schema())
    _assert_durable_recompose_schema(schema)
    schema["required"].remove("operation_id")
    with pytest.raises(AssertionError):
        _assert_durable_recompose_schema(schema)
