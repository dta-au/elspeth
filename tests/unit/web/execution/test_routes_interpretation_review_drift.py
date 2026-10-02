"""POST /execute maps resolved-review drift to a structured 409 (finding #18).

The strict materializer's drift guards used to raise a bare ``ValueError``
that fell into the route's last ``except ValueError`` arm: a 404 "not found"
carrying the raw integrity message. ``InterpretationReviewIntegrityError``
subclasses ``ValueError``, so its arm must sit above that bare arm.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.web.execution.protocol import ExecutionService
from elspeth.web.interpretation_state import InterpretationReviewIntegrityError
from tests.unit.web.execution.test_routes import _create_test_app, _execution_service

_RAW_INTEGRITY_MESSAGE = f"llm node 'rate' prompt-template review hash drifted (stored {'b' * 64})"


@pytest.mark.asyncio
async def test_execute_maps_review_drift_to_structured_409() -> None:
    exc = InterpretationReviewIntegrityError(
        _RAW_INTEGRITY_MESSAGE,
        component_id="rate",
        component_type="transform",
        kind=InterpretationKind.LLM_PROMPT_TEMPLATE,
    )
    svc = _execution_service()
    svc.execute = AsyncMock(spec=ExecutionService.execute, side_effect=exc)
    app = _create_test_app(execution_service=svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/sessions/{uuid4()}/execute")

    assert resp.status_code == 409
    public_detail = (
        "The approved llm_prompt_template review for transform 'rate' no longer matches the current pipeline, so the run was not started."
    )
    assert resp.json()["detail"] == {
        "error_type": "interpretation_review_drift",
        "kind": "interpretation_review_drift",
        "detail": public_detail,
        "message": public_detail,
        "component_id": "rate",
        "component_type": "transform",
        "review_kind": "llm_prompt_template",
    }
    assert "b" * 64 not in resp.text
    assert "hash drifted" not in resp.text


@pytest.mark.asyncio
async def test_source_review_drift_names_the_source_component() -> None:
    exc = InterpretationReviewIntegrityError(
        "invented source review drift: reviewed content hash does not match current source content hash",
        component_id="source:orders",
        component_type="source",
        kind=InterpretationKind.INVENTED_SOURCE,
    )
    svc = _execution_service()
    svc.execute = AsyncMock(spec=ExecutionService.execute, side_effect=exc)
    app = _create_test_app(execution_service=svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/sessions/{uuid4()}/execute")

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error_type"] == "interpretation_review_drift"
    assert detail["component_id"] == "source:orders"
    assert detail["component_type"] == "source"
    assert detail["review_kind"] == "invented_source"
    assert "content hash" not in resp.text


@pytest.mark.asyncio
async def test_plain_value_error_keeps_the_not_found_mapping() -> None:
    """Control: only the typed integrity error leaves the bare ValueError arm."""
    svc = _execution_service()
    svc.execute = AsyncMock(spec=ExecutionService.execute, side_effect=ValueError("No composition state for session"))
    app = _create_test_app(execution_service=svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/sessions/{uuid4()}/execute")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "No composition state for session"
