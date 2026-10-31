"""F1 classifications through actual fenced terminal and history producers."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from elspeth.web.async_workers import AsyncWorkerAdmissionTimeoutError
from elspeth.web.required_executor import RequiredGenerationUnavailable
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.composer_operations import (
    ComposerOperationError,
    composer_operation_result_hash,
)
from elspeth.web.sessions.schemas import ComposerOperationFailurePageResponse, ComposerOperationStatusResponse
from tests.unit.web.coordination.test_composer_operation_authority import start
from tests.unit.web.sessions.test_composer_operation_error_parity import mutation_error
from tests.unit.web.sessions.test_pipeline_rejection_callers import mounted_app

pytest_plugins = ("tests.unit.web.coordination.test_composer_operation_authority",)


async def _actual_failure_responses(operation_store, tmp_path, original):
    _engine, repository, authority, service, sid = operation_store
    admitted, running = start(operation_store)
    projected = project_composer_operation_error(original, request_id=admitted.request_id)
    try:
        await service.fail_composer_async_operation(running, failure=projected)
    finally:
        repository.release(running.session_operation_context)
    stored = authority.get(session_id=sid, operation_id=admitted.operation_id)
    assert stored is not None and stored.status == "failed"
    assert stored.result_json is not None
    assert stored.result_sha256 == composer_operation_result_hash(stored.result_json)
    recovered = ComposerOperationError.model_validate_json(stored.result_json, strict=True)
    assert recovered == projected
    app = mounted_app(operation_store, tmp_path)
    app.state.composer_async_operation_authority = authority
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        terminal = await client.get(f"/api/sessions/{sid}/operations/{admitted.operation_id}")
        history = await client.get(f"/api/sessions/{sid}/operations", params={"operation_id": admitted.operation_id})
    assert terminal.status_code == history.status_code == 200
    assert terminal.headers["Cache-Control"] == history.headers["Cache-Control"] == "no-store"
    terminal_dto = ComposerOperationStatusResponse.model_validate_json(terminal.content, strict=True)
    history_dto = ComposerOperationFailurePageResponse.model_validate_json(history.content, strict=True)
    assert terminal_dto.operation_id == admitted.operation_id and terminal_dto.status == "failed"
    assert terminal_dto.error == recovered and terminal_dto.result is None
    assert len(history_dto.items) == 1 and history_dto.next_cursor is None
    outcome = history_dto.items[0]
    assert outcome.session_id == sid and outcome.operation_id == admitted.operation_id
    assert outcome.kind == admitted.kind and outcome.failure_code == recovered.failure_code
    assert outcome.request_id == admitted.request_id == "correlation"
    assert outcome.saved_revisions.attribution == "fenced" and outcome.saved_revisions.count == 0
    assert "PRIVATE" not in terminal.text + history.text
    return projected, terminal_dto, history_dto


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["database", "generation", "admission"])
async def test_actual_availability_failure_survives_sealed_terminal_and_history(operation_store, tmp_path, failure_kind):
    original = (
        OperationalError("PRIVATE statement", {}, RuntimeError("PRIVATE driver"))
        if failure_kind == "database"
        else RequiredGenerationUnavailable("PRIVATE generation")
        if failure_kind == "generation"
        else AsyncWorkerAdmissionTimeoutError("PRIVATE admission")
    )
    projected, terminal, history = await _actual_failure_responses(operation_store, tmp_path, original)
    expected_category = "database_unavailable" if failure_kind == "database" else "executor_unavailable"
    assert projected.http_status == 503 and projected.failure_code == "http_error"
    assert projected.error_type == expected_category, "the actual terminal producer must classify the nominal availability origin"
    assert terminal.error is not None and terminal.error.error_type == expected_category
    assert history.items[0].error_type == expected_category, "closed SQL history must retain the produced executor category"
    assert projected.diagnostic_id is None and history.items[0].diagnostic_id is None


@pytest.mark.asyncio
async def test_actual_u29_history_keeps_its_closed_category_and_error_codec(operation_store, tmp_path):
    original = mutation_error()
    projected, terminal, history = await _actual_failure_responses(operation_store, tmp_path, original)
    assert projected.http_status == 409 and projected.failure_code == "http_error"
    assert projected.error_type == history.items[0].error_type == "composer_mutation_authority_revoked"
    assert terminal.error is not None and terminal.error.body == {"detail": original.to_payload()}
    assert "proposal" not in json.dumps(terminal.error.body)
    assert ComposerOperationError.model_validate_json(projected.model_dump_json(), strict=True) == projected


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["unknown-category", "private-field", "wrong-session", "non-nominal-count"])
async def test_actual_history_producer_retains_strict_public_codec(operation_store, tmp_path, corruption):
    original = OperationalError("PRIVATE statement", {}, RuntimeError("PRIVATE driver"))
    projected, _terminal, history = await _actual_failure_responses(operation_store, tmp_path, original)
    assert projected.error_type == history.items[0].error_type == "database_unavailable"
    wire = json.loads(history.model_dump_json())
    item = wire["items"][0]
    if corruption == "unknown-category":
        item["error_type"] = "unreviewed_failure_category"
    elif corruption == "private-field":
        item["body"] = {"detail": "PRIVATE injected body"}
    elif corruption == "wrong-session":
        item["session_id"] = str(UUID(int=history.session_id.int ^ 1))
    else:
        item["saved_revisions"]["count"] = False
    with pytest.raises(ValidationError):
        ComposerOperationFailurePageResponse.model_validate_json(json.dumps(wire), strict=True)
    assert ComposerOperationFailurePageResponse.model_validate_json(history.model_dump_json(), strict=True) == history
