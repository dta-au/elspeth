"""U-22 F4: a context refusal projects as a permanent, value-free 422.

``ComposerContextUnsupported`` subclasses ``ComposerAdmissionRefused``. Without
its own arm an operation answered 403 ``composer_admission_refused`` and told
the user to ask an administrator about access and quota, which is wrong: the
graph itself is outside the supported envelope, and retrying it can never
succeed. These tests pin the carrier's fixed public payload, its nominal
evidence guards, and the operation-error projection the worker settles with.
"""

from __future__ import annotations

import pytest

from elspeth.web.composer.protocol import ComposerContextUnsupported


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
