"""U-22 F4: /messages and /recompose settle a context refusal as a permanent 422.

``ComposerContextUnsupported`` subclasses ``ComposerAdmissionRefused``. Without
its own arm the turn answered 403 ``composer_admission_refused`` and told the
user to ask an administrator about access and quota, which is wrong: the graph
itself is outside the supported envelope, and retrying it can never succeed.
Both routes must settle the operation as HTTP 422 with the carrier's value-free
payload, a permanent ``failure_code`` and the ``context_unsupported`` progress
reason. ``settle_sync`` drives the real asynchronous operation settlement.
These tests inject the typed owner result; natural preparation reachability
is covered separately by preparation-owner tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.web.composer.protocol import ComposerContextUnsupported
from tests.unit.web.sessions.test_recompose_admission_refused import _post_after_compose_failure


@pytest.mark.parametrize("route", ["messages", "recompose"])
@pytest.mark.parametrize("reason", ["graph_resource_limit", "graph_coverage_unavailable", "manual_advisor_allowance"])
def test_context_refusal_settles_as_permanent_422(tmp_path: Path, route: str, reason) -> None:
    refusal = ComposerContextUnsupported(reason)
    status_code, detail, progress = _post_after_compose_failure(tmp_path, refusal, route=route)

    assert status_code == 422
    assert detail == refusal.to_payload()
    assert detail["error_type"] == "composer_context_unsupported"
    assert detail["failure_code"] == "context_unsupported"
    assert detail["reason"] == reason
    assert progress["phase"] == "failed"
    assert progress["reason"] == "context_unsupported"
    assert "administrator" not in progress["likely_next"]


def test_ordinary_admission_refusal_keeps_its_403(tmp_path: Path) -> None:
    """Control: only the context refusal is reclassified."""
    from elspeth.web.composer.protocol import ComposerAdmissionRefused

    status_code, detail, progress = _post_after_compose_failure(
        tmp_path, ComposerAdmissionRefused("Composer admission refused: identity_disabled."), route="messages"
    )
    assert status_code == 403
    assert detail["error_type"] == "composer_admission_refused"
    assert progress["reason"] == "admission_refused"


@pytest.mark.parametrize("reason", ["graph_resource_limit", "graph_coverage_unavailable", "manual_advisor_allowance"])
def test_context_refusal_payload_has_only_fixed_public_members(reason) -> None:
    from elspeth.contracts.errors import FailedTurnMetadata
    from elspeth.web.composer.state import CompositionState, PipelineMetadata
    from tests.unit.web.sessions.test_routes import _llm_call
    from tests.unit.web.sessions.test_unwind_persist_forensics import _tool_invocation

    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(name="PRIVATE graph name"), version=2)
    call = _llm_call()
    invocation = _tool_invocation("request_advisor_hint")
    failed_turn = FailedTurnMetadata(assistant_message_id="PRIVATE assistant", tool_calls_attempted=1)
    refusal = ComposerContextUnsupported(reason).with_evidence(
        state=state, initial_version=1, tool_invocations=(invocation,), llm_calls=(call,), failed_turn=failed_turn
    )

    assert refusal.partial_state is state
    assert refusal.llm_calls == (call,)
    assert refusal.tool_invocations == (invocation,)
    assert refusal.failed_turn is failed_turn
    assert set(refusal.to_payload()) == {"error_type", "failure_code", "reason", "detail"}
    assert "PRIVATE" not in str(refusal) and "PRIVATE" not in repr(refusal.to_payload())
    assert ComposerContextUnsupported(reason).with_evidence(state=state, initial_version=2).partial_state is None


def test_context_refusal_rejects_non_nominal_evidence_and_unknown_reasons() -> None:
    from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
    from elspeth.web.composer.state import CompositionState, PipelineMetadata

    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=2)
    with pytest.raises(FrameworkBugError, match="Unknown composer context refusal reason") as unknown:
        ComposerContextUnsupported("PRIVATE unknown reason")
    assert "PRIVATE" not in str(unknown.value)
    with pytest.raises(AuditIntegrityError, match="owned composition state"):
        ComposerContextUnsupported("graph_resource_limit", partial_state=object())
    with pytest.raises(AuditIntegrityError, match="owned immutable tool invocations"):
        ComposerContextUnsupported("graph_resource_limit", tool_invocations=(object(),))
    with pytest.raises(AuditIntegrityError, match="owned immutable model calls"):
        ComposerContextUnsupported("graph_resource_limit", llm_calls=(object(),))
    with pytest.raises(AuditIntegrityError, match="owned failed-turn metadata"):
        ComposerContextUnsupported("graph_resource_limit", failed_turn=object())
    with pytest.raises(AuditIntegrityError, match="exact persisted version"):
        ComposerContextUnsupported("graph_resource_limit").with_evidence(state=state, initial_version=True)


@pytest.mark.parametrize("reason", ["graph_resource_limit", "graph_coverage_unavailable", "manual_advisor_allowance"])
def test_context_refusal_has_identical_typed_and_http_operation_projection(reason) -> None:
    from fastapi import HTTPException

    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    refusal = ComposerContextUnsupported(reason)
    projected = project_composer_operation_error(refusal, request_id="original-request")
    http_projected = project_composer_operation_error(
        HTTPException(status_code=422, detail=refusal.to_payload()), request_id="original-request"
    )
    assert projected == http_projected
    assert projected.http_status == 422
    assert projected.failure_code == "http_error", "operation storage keeps its existing closed failure vocabulary"
    assert projected.body == {"detail": {**refusal.to_payload(), "request_id": "original-request"}}
    assert projected.body["detail"]["failure_code"] == "context_unsupported"
    assert projected.diagnostic_id is None


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("failure_kind", ["integrity", "database", "sql", "storage", "fence", "cancel", "unknown"])
def test_context_refusal_never_hides_an_integrity_or_mixed_operation_failure(reverse: bool, failure_kind: str) -> None:
    import asyncio
    import errno

    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.coordination.contracts import FenceLossReason, SessionOperationFenceLost
    from elspeth.web.sessions.composer_operation_errors import project_composer_operation_error

    failures: dict[str, BaseException] = {
        "integrity": AuditIntegrityError("PRIVATE audit fault"),
        "database": OperationalError("PRIVATE statement", {}, RuntimeError("PRIVATE driver")),
        "sql": SQLAlchemyError("PRIVATE SQL fault"),
        "storage": OSError(errno.ENOSPC, "PRIVATE storage fault"),
        "fence": SessionOperationFenceLost(FenceLossReason.STALE_EPOCH),
        "cancel": asyncio.CancelledError("PRIVATE lifecycle marker"),
        "unknown": RuntimeError("PRIVATE unknown fault"),
    }
    leaves = [ComposerContextUnsupported("graph_resource_limit"), failures[failure_kind]]
    if reverse:
        leaves.reverse()
    projected = project_composer_operation_error(BaseExceptionGroup("PRIVATE joined outcomes", leaves), request_id="original-request")
    expected = {
        "integrity": (500, "audit_integrity_error"),
        "database": (503, "database_unavailable"),
        "storage": (503, "storage_unavailable"),
    }
    assert (projected.http_status, projected.error_type) == expected.get(failure_kind, (500, "operation_failed"))
    assert "PRIVATE" not in projected.model_dump_json()


@pytest.mark.asyncio
async def test_title_required_result_preserves_the_nominal_context_refusal() -> None:
    import asyncio

    from elspeth.web.sessions.composer_turn import _required_title_result

    refusal = ComposerContextUnsupported("graph_resource_limit")

    async def refused_title() -> None:
        raise refusal

    task = asyncio.create_task(refused_title())
    with pytest.raises(ComposerContextUnsupported) as child:
        await task
    with pytest.raises(ComposerContextUnsupported) as settled:
        _required_title_result(task)
    assert child.value is settled.value is refusal


@pytest.mark.asyncio
async def test_context_refusal_turn_drains_its_outstanding_cohort_exactly_once(tmp_path: Path) -> None:
    from fastapi import HTTPException

    from elspeth.web.sessions import composer_turn
    from elspeth.web.sessions.composer_app_services import composer_app_services
    from tests.unit.web.sessions.test_routes import _llm_call, _llm_call_audit_rows, _make_app, _make_composer_mock
    from tests.unit.web.sessions.test_run_composer_turn import _row, _run, _running_job
    from tests.unit.web.sessions.test_unwind_persist_forensics import _tool_invocation

    app, service = _make_app(tmp_path)
    composer = _make_composer_mock()
    call = _llm_call()
    tool = _tool_invocation("request_advisor_hint")
    refusal = ComposerContextUnsupported("graph_coverage_unavailable", llm_calls=(call,), tool_invocations=(tool,))
    composer.compose.side_effect = refusal
    app.state.composer_service = composer
    session = await service.create_session("alice", "Context refusal", "local")
    async with _running_job(app, service, session.id, kind="compose_message", content="Review the draft.") as job:
        with pytest.raises(HTTPException) as caught:
            await _run(app, job)
        assert caught.value.status_code == 422 and caught.value.__cause__ is refusal
        assert job.observation.audit_cohort_durable is True
        assert job.observation.pending_exception_llm_calls == (call,)
        assert job.observation.pending_exception_tool_invocations == (tool,)
        assert (await _row(job)).status == "running", "failure settlement remains the worker's responsibility"
        before = await service.get_messages(session.id, limit=None)
        await composer_turn.persist_cancelled_turn_audit(composer_app_services(app), job.running, job.observation)
        after = await service.get_messages(session.id, limit=None)
        assert after == before
    rows = _llm_call_audit_rows(after)
    assert len(rows) == 1 and rows[0][1]["call"]["status"] == "success"
    invocations = [
        envelope["invocation"]
        for row in after
        for envelope in (row.tool_calls or ())
        if envelope["_kind"] == "audit" and envelope["invocation"]["tool_call_id"] == tool.tool_call_id
    ]
    assert len(invocations) == 1 and invocations[0]["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["integrity", "database"])
async def test_required_refusal_audit_failure_outranks_422_and_keeps_the_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions import composer_turn
    from tests.unit.web.sessions.test_routes import _llm_call, _llm_call_audit_rows, _make_app, _make_composer_mock
    from tests.unit.web.sessions.test_run_composer_turn import _row, _run, _running_job

    app, service = _make_app(tmp_path)
    composer = _make_composer_mock()
    call = _llm_call()
    refusal = ComposerContextUnsupported("graph_resource_limit", llm_calls=(call,))
    audit_failure = AuditIntegrityError("PRIVATE required audit fault") if failure_kind == "integrity" else SQLAlchemyError("PRIVATE SQL")
    composer.compose.side_effect = refusal
    app.state.composer_service = composer
    session = await service.create_session("alice", "Required context evidence", "local")
    audit_attempts = 0

    async def fail_required_cohort(*_args, **_kwargs) -> None:
        nonlocal audit_attempts
        audit_attempts += 1
        raise audit_failure

    monkeypatch.setattr(composer_turn, "_persist_turn_audit_cohort", fail_required_cohort)
    async with _running_job(app, service, session.id, kind="compose_message", content="Review the draft.") as job:
        with pytest.raises(type(audit_failure)) as caught:
            await _run(app, job)
        assert caught.value is audit_failure
        assert caught.value.__context__ is refusal
        assert audit_attempts == 1
        assert job.observation.audit_cohort_durable is False
        assert job.observation.pending_exception_llm_calls == (call,)
        assert (await _row(job)).status == "running"
        assert _llm_call_audit_rows(await service.get_messages(session.id, limit=None)) == []
