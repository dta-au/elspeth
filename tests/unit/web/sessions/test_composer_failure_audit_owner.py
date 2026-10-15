"""Failure audit helpers own their tickets before the live-lease terminal write."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from litellm.exceptions import BadGatewayError
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.diagnostic_evidence import DIAGNOSTIC_EVIDENCE_KIND, ComposerDiagnosticEvidence
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.required_work import RequiredWorkCoordinator, RequiredWorkSource, RequiredWorkSubphase
from elspeth.web.sessions.composer_operations import ComposerOperationError
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
    app.state.composer_service = SimpleNamespace(compose=AsyncMock(spec=ComposerServiceImpl.compose, side_effect=provider_error))
    original_reserve = RequiredWorkCoordinator.reserve
    terminal_owners = []
    audit_error = AuditIntegrityError("controlled audit preparation failure")
    sql_error = OperationalError("controlled required audit SQL", {}, RuntimeError("controlled SQL cause"))
    original_write = service._write_audit_cohort_on_connection
    failed_sql_attempts = 0
    terminal_contexts = []

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

    def fail_sql(conn, **kwargs):
        nonlocal failed_sql_attempts
        drafts = kwargs["drafts"]
        assert len(drafts) == 1 and drafts[0].tool_calls is not None and len(drafts[0].tool_calls) == 1
        envelope = drafts[0].tool_calls[0]
        if envelope["_kind"] == "llm_call_audit":
            failed_sql_attempts += 1
            raise sql_error
        # Terminal private facts use the same writer, but are a separate,
        # mandatory transaction from both deliberately failed LLM cohorts.
        assert envelope["_kind"] == DIAGNOSTIC_EVIDENCE_KIND
        context = kwargs["session_operation_context"]
        assert context == terminal_owners[0].authority.context
        evidence = ComposerDiagnosticEvidence.from_envelope(
            envelope,
            expected_session_id=str(kwargs["session_id"]),
            expected_operation_id=context.fence.operation_id,
            expected_tool_call_id="operation:" + body["operation_id"],
        )
        assert evidence.operation_epoch == context.fence.operation_epoch
        assert evidence.tool_name == "composer_failure" and evidence.status == "operation_failure"
        terminal_contexts.append(context)
        return original_write(conn, **kwargs)

    monkeypatch.setattr(RequiredWorkCoordinator, "reserve", reserve)
    if failure_phase == "preparation":
        monkeypatch.setattr(_helpers, "llm_call_audit_summary", fail_preparation)
    elif failure_phase == "sql":
        monkeypatch.setattr(service, "_write_audit_cohort_on_connection", fail_sql)
    client = TestClient(app, raise_server_exceptions=False)
    created = client.post("/api/sessions", json={"title": "Failure audit"})
    session_id = UUID(created.json()["id"])
    body = message_body("Hello", state_id=current_head_state_id_sync(client, session_id))
    response = _settled_composer_response(
        client,
        app,
        path=f"/api/sessions/{session_id}/messages",
        body=body,
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
        assert failed_sql_attempts == 2
        assert terminal_contexts == [coordinator.authority.context]
        messages = asyncio.run(service.get_messages(session_id, limit=None))
        assert [message.role for message in messages] == ["user", "audit"]
        assert messages[1].tool_calls is not None and len(messages[1].tool_calls) == 1
        record = app.state.composer_async_operation_authority.get(session_id=session_id, operation_id=body["operation_id"])
        assert record is not None and record.user_message_id == messages[0].id
        selected = ComposerOperationError.model_validate_json(record.result_json)
        evidence = ComposerDiagnosticEvidence.from_envelope(
            messages[1].tool_calls[0],
            expected_session_id=str(session_id),
            expected_operation_id=coordinator.authority.context.fence.operation_id,
            expected_tool_call_id="operation:" + record.operation_id,
        )
        assert evidence.operation_epoch == coordinator.authority.context.fence.operation_epoch
        assert evidence.arguments.content is not None and evidence.result.content is not None
        assert json.loads(evidence.arguments.content) == {
            "correlation_kind": "internal_operation",
            "durable_operation_id": record.operation_id,
            "request_id": record.request_id,
            "diagnostic_id": selected.diagnostic_id,
            "failure_code": selected.failure_code,
        }
        facts = json.loads(evidence.result.content)
        assert facts["record_type"] == "failure_facts"
        assert {"BadGatewayError", "OperationalError"} <= {node["class"] for node in facts["nodes"]}
        assert "controlled SQL cause" in evidence.result.content
        assert "controlled SQL cause" not in response.text
        assert "controlled provider failure" not in response.text
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
