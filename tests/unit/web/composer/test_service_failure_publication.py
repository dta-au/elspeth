"""Real issued-prefix/P4 custody for nominal service failures and its neighbors."""

import pytest
from sqlalchemy.exc import OperationalError

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_terminal_p4_data import decode_terminal_p4_result
from elspeth.web.composer.tools import ToolResult
from elspeth.web.required_sql_outcomes import RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error
from elspeth.web.sessions.continuation_p4_application_store import CommittedContinuationP4, StoredTerminalP4Application
from tests.unit.web.composer._helpers import _persisted_tool_responses
from tests.unit.web.composer.test_service_failure_dispatch import _issued_review_case, _refuse_service, _review_call, _service_error
from tests.unit.web.sessions.test_continuation_live_phases import _advance
from tests.unit.web.sessions.test_continuation_live_phases import operation_store as _operation_store

operation_store = _operation_store


async def _finish_issued_batch(owner):
    """Drive existing phases through actual P4; retain an observed native exit."""
    for _ in range(4):
        try:
            await _advance(owner)
        except Exception as original:
            return original
        if owner.p4 is not None and owner.p4.committed_outcome is not None:
            return None
    raise AssertionError("the bounded issued batch did not reach its actual required P4")


def _exception_nodes(original):
    pending, seen, nodes = [original], set(), []
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        nodes.append(error)
        if isinstance(error, BaseExceptionGroup):
            pending.extend(error.exceptions)
        if error.__cause__ is not None:
            pending.append(error.__cause__)
        if error.__context__ is not None:
            pending.append(error.__context__)
    return nodes


@pytest.mark.asyncio
async def test_current_v2_service_failure_publishes_p4_then_original(operation_store, tmp_path, monkeypatch):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        original, cause = _service_error("admission"), RuntimeError("V2 service original cause")
        entered = _refuse_service(
            operation_store[3], monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
        )
        observed = await _finish_issued_batch(owner)
        assert len(entered) == 1 and len(sdk) == 1
        assert owner.failure_custody.original is original and original.__cause__ is cause
        assert owner.p4 is not None and owner.p4.committed_outcome is not None, "the genuine required P4 must precede service propagation"
        rows = _persisted_tool_responses(owner.service, str(running.claim.session_id))
        assert [row["tool_call_id"] for row in rows] == ["pending_review"]
        assert len(owner.runtime.live.recorder.llm_calls) == 1
        assert observed is original, "a nominal service original must reach its owner after P4 without plugin-crash conversion"
        projection = project_composer_operation_error(observed, request_id=running.claim.operation_id)
        assert projection.failure_code == "http_error" and projection.http_status == 503
        assert projection.error_type == projection.body["error_type"] == "executor_unavailable"
        assert projection.body["request_id"] == running.claim.operation_id and projection.diagnostic_id is None


@pytest.mark.asyncio
async def test_actual_terminal_unavailable_publication_is_integrity_controlling_and_keeps_origins(operation_store, tmp_path, monkeypatch):
    async with _issued_review_case(operation_store, tmp_path, monkeypatch) as (owner, running, sdk):
        sessions = operation_store[3]
        original, service_cause = _service_error("admission"), RuntimeError("service original prior cause")
        reached = _refuse_service(
            sessions, monkeypatch, seam="write", original=original, cause=service_cause, context=running.session_operation_context
        )
        audit_error = OperationalError("actual required chat insert refusal", {}, RuntimeError("actual DBAPI origin"))
        audit_cause = RuntimeError("publication original prior cause")
        audit_error.__cause__ = audit_cause
        actual_publish = sessions.persist_continuation_compose_turn
        publish_calls, insert_attempts, physical_returns = [], [], []

        def insert(conn, **kwargs):
            assert kwargs["session_id"] == str(running.claim.session_id)
            assert kwargs["session_operation_context"] is running.session_operation_context
            assert kwargs["role"] == "assistant" and kwargs["writer_principal"] == "compose_loop"
            sessions._assert_session_write_lock_held(conn, kwargs["session_id"], caller="terminal unavailable test")
            insert_attempts.append(kwargs)
            # The real transaction catches this SQL failure and writes its own
            # unavailable receipt/application. The test supplies no outcome.
            raise audit_error

        async def publish(**kwargs):
            assert kwargs["session_operation_context"] is running.session_operation_context
            publish_calls.append(kwargs["required_work"])
            result = await actual_publish(**kwargs)
            physical_returns.append(result)
            return result

        monkeypatch.setattr(sessions, "_insert_chat_message", insert)
        monkeypatch.setattr(sessions, "persist_continuation_compose_turn", publish)
        observed = await _finish_issued_batch(owner)
        assert len(reached) == len(insert_attempts) == len(publish_calls) == len(physical_returns) == len(sdk) == 1
        phase = owner.p4
        assert phase is not None and phase.publication is not None and phase.committed_outcome is not None
        assert phase.physical is physical_returns[0] and type(phase.physical) is RequiredSQLReturned
        assert type(phase.physical.value) is CommittedContinuationP4
        assert phase.committed_outcome is phase.physical.value
        outcome = phase.committed_outcome.audit_outcome
        assert outcome is phase.publication.audit_outcome is phase.writer_observation.candidate
        assert outcome.unwind_audit_failed and outcome.assistant_id is None and outcome.checkpoint is None
        assert outcome.checkpoint_disposition == "unavailable" and outcome.continuation_retention_failure is None
        assert type(phase.application) is StoredTerminalP4Application
        assert phase.application is phase.committed_outcome.application is phase.writer_observation.application_candidate
        retained = decode_terminal_p4_result(
            phase.application.result_document, input_document=phase.application.input_document, env_ref_names=phase.inventory.names
        )
        assert retained.receipt.unwind_audit_failed and retained.receipt.checkpoint_disposition == "unavailable"
        assert retained.receipt.chat_message_ids == retained.receipt.inserted_state_ids == retained.receipt.invocation_ids == ()
        assert phase.runtime.drained == retained.drained == owner.terminal_completion.runtime.drained
        assert phase.runtime.graph.batch.batch.service_failure is original
        assert not _persisted_tool_responses(owner.service, str(running.claim.session_id))
        assert phase.application_sql is None
        projection = tuple(
            ticket for ticket in owner.work.coordinator.tickets if ticket.key.source is RequiredWorkSource.COMPOSE_CHECKPOINT_PROJECTION
        )
        assert len(projection) == 1 and projection[0].complete and publish_calls[0].complete
        assert phase.physical is publish_calls[0]._finish_once_handoff
        nodes = _exception_nodes(observed)
        assert any(error is original for error in nodes) and any(error is audit_error for error in nodes)
        assert original.__cause__ is service_cause and audit_error.__cause__ is audit_cause
        assert any(error is audit_error for error in phase.deferred_failures), "actual SQL companion must reach live P4 custody"
        assert all(any(error is companion for error in nodes) for companion in (*phase.deferred_failures, *phase.deferred_cancellations))
        integrity = [error for error in nodes if isinstance(error, AuditIntegrityError)]
        assert any(error.failed_turn is phase.publication.failed_turn for error in integrity)
        assert phase.publication.failed_turn.assistant_message_id is None
        assert phase.publication.failed_turn.tool_responses_persisted == 0
        selected = project_composer_operation_error(observed, request_id=running.claim.operation_id)
        assert selected.failure_code == "http_error" and selected.http_status == 500
        assert selected.error_type == selected.body["error_type"] == "audit_integrity_error", (
            "unavailable required audit cannot become an ordinary service retry"
        )
        assert selected.body["request_id"] == running.claim.operation_id and selected.diagnostic_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("service_refuses", (False, True), ids=("healthy-prefix", "service-original"))
async def test_service_failure_preserves_prior_mutation_and_stops_next_issued_mutation(
    operation_store, tmp_path, monkeypatch, service_refuses
):
    calls = (
        ("before_service", "set_metadata", {"patch": {"name": "Prior successful mutation"}}),
        _review_call(),
        ("after_service", "set_metadata", {"patch": {"name": "Only the healthy neighbor may reach this"}}),
    )
    async with _issued_review_case(operation_store, tmp_path, monkeypatch, calls=calls) as (owner, running, sdk):
        version = owner.runtime.live.state.version
        original, cause = _service_error("sql"), RuntimeError("prior-mutation service cause")
        entered = []
        if service_refuses:
            entered = _refuse_service(
                operation_store[3], monkeypatch, seam="write", original=original, cause=cause, context=running.session_operation_context
            )
        observed = await _finish_issued_batch(owner)
        assert len(sdk) == len(owner.runtime.live.recorder.llm_calls) == 1
        assert owner.p4 is not None and owner.p4.committed_outcome is not None
        expected_calls = ["before_service", "pending_review"] if service_refuses else [call[0] for call in calls]
        outcomes = owner.runtime.graph.batch.batch.tool_outcomes
        assert [outcome.call.id for outcome in outcomes] == expected_calls
        assert isinstance(outcomes[0].response, ToolResult) and outcomes[0].response.success is True
        assert outcomes[0].post_version == version + 1
        rows = _persisted_tool_responses(owner.service, str(running.claim.session_id))
        assert [row["tool_call_id"] for row in rows] == expected_calls, "each actual executed tool response must persist exactly once"
        if service_refuses:
            assert len(entered) == 1 and original.__cause__ is cause
            assert owner.runtime.live.state.metadata.name == "Prior successful mutation"
            assert owner.runtime.live.state.version == version + 1
            assert observed is original, "required P4 must preserve the original service disposition and prior mutation"
            assert owner.failure_custody.wrapper is None
        else:
            assert observed is None
            assert owner.runtime.live.state.metadata.name == "Only the healthy neighbor may reach this"
            assert owner.runtime.live.state.version == version + 2
            assert all(item.status is ComposerToolStatus.SUCCESS for item in owner.runtime.live.recorder.invocations)
            events = await operation_store[3].list_interpretation_events(running.claim.session_id, status="pending")
            assert len(events) == 1 and events[0].tool_call_id == "pending_review"
