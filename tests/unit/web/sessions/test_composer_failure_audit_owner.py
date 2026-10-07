"""Failure audit helpers own their tickets before the live-lease terminal write."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from litellm.exceptions import BadGatewayError
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkSource, RequiredWorkSubphase
from elspeth.web.sessions.routes import _helpers
from tests.helpers.composer_operations import current_head_state_id_sync, message_body
from tests.unit.web.sessions.test_routes import TestClient, _llm_call, _make_app, _settled_composer_response


@pytest.mark.parametrize("failure_phase", [None, "preparation", "sql"])
def test_provider_failure_audit_owner_settles_under_live_lease(tmp_path, monkeypatch, failure_phase):
    app, service = _make_app(tmp_path)
    provider_error = BadGatewayError(message="controlled provider failure", llm_provider="test", model="test/model")
    provider_error.llm_calls = (
        _llm_call(status=ComposerLLMCallStatus.API_ERROR, error_class="BadGatewayError", error_message="BadGatewayError"),
    )
    app.state.composer_service = SimpleNamespace(compose=AsyncMock(side_effect=provider_error))
    original_reserve = RequiredWorkCoordinator.reserve
    terminal_owners = []
    audit_error = AuditIntegrityError("controlled audit preparation failure")
    sql_error = OperationalError("controlled required audit SQL", {}, RuntimeError("controlled SQL cause"))

    def reserve(self, source, *, transition_ordinal=0, semantic_ordinal=0, recurrence_ordinal=0, sql_attempt_ordinal=0, producer=False):
        if source is RequiredWorkSource.TERMINAL_FAILURE_SQL:
            assert not self._release_prepared
            terminal_owners.append(self)
        return original_reserve(
            self,
            source,
            transition_ordinal=transition_ordinal,
            semantic_ordinal=semantic_ordinal,
            recurrence_ordinal=recurrence_ordinal,
            sql_attempt_ordinal=sql_attempt_ordinal,
            producer=producer,
        )

    def fail_preparation(*args, **kwargs):
        raise audit_error

    def fail_sql(*args, **kwargs):
        raise sql_error

    monkeypatch.setattr(RequiredWorkCoordinator, "reserve", reserve)
    if failure_phase == "preparation":
        monkeypatch.setattr(_helpers, "llm_call_audit_summary", fail_preparation)
    elif failure_phase == "sql":
        monkeypatch.setattr(service, "_write_audit_cohort_on_connection", fail_sql)
    client = TestClient(app, raise_server_exceptions=False)
    created = client.post("/api/sessions", json={"title": "Failure audit"})
    session_id = UUID(created.json()["id"])
    response = _settled_composer_response(
        client,
        app,
        path=f"/api/sessions/{session_id}/messages",
        body=message_body("Hello", state_id=current_head_state_id_sync(client, session_id)),
    )
    assert response.status_code == (502 if failure_phase is None else 500)
    assert len(terminal_owners) == 1
    coordinator = terminal_owners[0]
    audit_receipts = [
        receipt
        for receipt in coordinator.tickets
        if receipt.key.stage.name == "REQUIRED_TURN_AUDIT" and receipt.key.subphase is RequiredWorkSubphase.PRODUCER
    ]
    assert len(audit_receipts) == (1 if failure_phase is None else 2)
    assert len({receipt.key for receipt in audit_receipts}) == len(audit_receipts)
    assert coordinator.all_completed
    retained = set()
    for receipt in coordinator.failure_receipts():
        _retained_originals(receipt.original_root, retained)
    assert id(provider_error) in retained
    if failure_phase == "preparation":
        assert id(audit_error) in retained
    elif failure_phase == "sql":
        assert id(sql_error) in retained
    assert app.state.composer_service.compose.await_count == 1


def _retained_originals(error, seen):
    if id(error) in seen:
        return
    seen.add(id(error))
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            _retained_originals(child, seen)
    if error.__cause__ is not None:
        _retained_originals(error.__cause__, seen)
    if error.__context__ is not None:
        _retained_originals(error.__context__, seen)
