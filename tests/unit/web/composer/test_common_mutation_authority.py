"""Pure request ceilings intersect the current session's mutation authority."""

import json
from dataclasses import FrozenInstanceError, dataclass, replace

import pytest

from elspeth.contracts.composer_mutation import (
    ComposerAutoMutationRefused,
    ComposerMutationCeiling,
    ComposerMutationRevocation,
    MutationTrustMode,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdmittedToolCall, _AdmittedToolFunction, _ToolOutcome
from elspeth.web.composer.redaction import redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._common import ToolResult, _failure_result
from elspeth.web.composer.turn_audit import persist_turn_audit


class _StringIdentity(str):
    pass


class _CeilingSubclass(ComposerMutationCeiling):
    pass


class _RevocationSubclass(ComposerMutationRevocation):
    pass


@dataclass(frozen=True)
class _ContextImpostor:
    fence: SessionOperationFence
    operation_kind: SessionOperationKind


def _context(session_id: str = "session", operation_kind: SessionOperationKind = SessionOperationKind.COMPOSE) -> SessionOperationContext:
    return SessionOperationContext(SessionOperationFence(session_id, "operation", "lease", 1), operation_kind)


@pytest.mark.parametrize(
    "initial,current,allowed",
    [
        ("auto_commit", "auto_commit", True),
        ("auto_commit", "explicit_approve", False),
        ("explicit_approve", "auto_commit", False),
        ("explicit_approve", "explicit_approve", False),
    ],
)
def test_request_ceiling_intersects_current_trust(initial: MutationTrustMode, current: MutationTrustMode, allowed: bool) -> None:
    ceiling = ComposerMutationCeiling("session", "request", initial)
    assert ceiling.permits_auto(current) is allowed
    assert ceiling.initial_trust_mode == initial


@pytest.mark.parametrize("identity", ["", None, 1, True, _StringIdentity("session")])
def test_ceiling_rejects_invalid_session_identity(identity) -> None:
    with pytest.raises(ValueError, match="requires its session identity"):
        ComposerMutationCeiling(identity, "request", "auto_commit")


@pytest.mark.parametrize("identity", ["", None, 1, True, _StringIdentity("request")])
def test_ceiling_rejects_invalid_logical_request_identity(identity) -> None:
    with pytest.raises(ValueError, match="requires its logical request identity"):
        ComposerMutationCeiling("session", identity, "auto_commit")


@pytest.mark.parametrize("mode", ["", "AUTO_COMMIT", "approve", None, False, 1, [], {}])
def test_ceiling_rejects_invalid_initial_trust_mode(mode) -> None:
    with pytest.raises(ValueError, match="invalid initial trust mode"):
        ComposerMutationCeiling("session", "request", mode)


@pytest.mark.parametrize("mode", ["", "AUTO_COMMIT", "approve", None, False, 1, [], {}])
def test_ceiling_rejects_invalid_current_trust_mode(mode) -> None:
    ceiling = ComposerMutationCeiling("session", "request", "auto_commit")
    with pytest.raises(AuditIntegrityError, match="Session has an invalid trust mode"):
        ceiling.permits_auto(mode)


def test_ceiling_retains_immutable_logical_request_identity_and_initial_authority() -> None:
    ceiling = ComposerMutationCeiling("session", "logical-request", "explicit_approve")
    with pytest.raises(FrozenInstanceError):
        ceiling.initial_trust_mode = "auto_commit"
    with pytest.raises(FrozenInstanceError):
        ceiling.request_id = "replacement-request"
    assert ceiling.request_id == "logical-request"
    assert ceiling.initial_trust_mode == "explicit_approve"
    assert replace(ceiling, request_id="another-request").request_id == "another-request"
    assert ceiling.request_id == "logical-request"


def test_ceiling_accepts_actual_current_compose_context_for_its_session() -> None:
    ceiling = ComposerMutationCeiling("session", "request", "auto_commit")
    context = _context()
    assert type(context) is SessionOperationContext
    ceiling.validate_context(context)


@pytest.mark.parametrize("operation_kind", [kind for kind in SessionOperationKind if kind is not SessionOperationKind.COMPOSE])
def test_ceiling_rejects_other_operation_roles(operation_kind: SessionOperationKind) -> None:
    ceiling = ComposerMutationCeiling("session", "request", "auto_commit")
    with pytest.raises(AuditIntegrityError, match="differs from current COMPOSE custody"):
        ceiling.validate_context(_context(operation_kind=operation_kind))


def test_ceiling_rejects_current_context_from_another_session() -> None:
    ceiling = ComposerMutationCeiling("session", "request", "auto_commit")
    with pytest.raises(AuditIntegrityError, match="differs from current COMPOSE custody"):
        ceiling.validate_context(_context(session_id="other-session"))


@pytest.mark.parametrize("context", [None, object(), _ContextImpostor(_context().fence, SessionOperationKind.COMPOSE)])
def test_ceiling_rejects_non_nominal_context_even_with_matching_fields(context) -> None:
    ceiling = ComposerMutationCeiling("session", "request", "auto_commit")
    with pytest.raises(AuditIntegrityError, match="differs from current COMPOSE custody"):
        ceiling.validate_context(context)


@pytest.mark.parametrize(
    "initial,current",
    [("auto_commit", "explicit_approve"), ("explicit_approve", "auto_commit"), ("explicit_approve", "explicit_approve")],
)
def test_revocation_carries_the_exact_ceiling_and_current_mode(initial: MutationTrustMode, current: MutationTrustMode) -> None:
    ceiling = ComposerMutationCeiling("session", "request", initial)
    revocation = ComposerMutationRevocation(ceiling, current)
    assert revocation.ceiling is ceiling
    assert revocation.current_trust_mode == current
    with pytest.raises(FrozenInstanceError):
        revocation.current_trust_mode = "auto_commit"


def test_allowed_mutation_cannot_be_reported_as_revoked() -> None:
    ceiling = ComposerMutationCeiling("session", "request", "auto_commit")
    with pytest.raises(ValueError, match="Allowed mutation cannot be reported as revoked"):
        ComposerMutationRevocation(ceiling, "auto_commit")


@pytest.mark.parametrize("ceiling", [None, object(), _CeilingSubclass("session", "request", "explicit_approve")])
def test_revocation_requires_an_exact_owned_ceiling(ceiling) -> None:
    with pytest.raises(TypeError, match="requires an owned ceiling"):
        ComposerMutationRevocation(ceiling, "explicit_approve")


@pytest.mark.parametrize("mode", ["invalid", None, True, [], {}])
def test_revocation_rejects_invalid_current_mode(mode) -> None:
    ceiling = ComposerMutationCeiling("session", "request", "explicit_approve")
    with pytest.raises(AuditIntegrityError, match="Session has an invalid trust mode"):
        ComposerMutationRevocation(ceiling, mode)


def test_refusal_carries_the_exact_owned_revocation() -> None:
    revocation = ComposerMutationRevocation(ComposerMutationCeiling("session", "request", "explicit_approve"), "auto_commit")
    refusal = ComposerAutoMutationRefused(revocation)
    assert isinstance(refusal, RuntimeError)
    assert refusal.revocation is revocation
    assert str(refusal) == "Automatic mutation authority is no longer available for this request."


@pytest.mark.parametrize(
    "revocation",
    [None, object(), _RevocationSubclass(ComposerMutationCeiling("session", "request", "explicit_approve"), "explicit_approve")],
)
def test_refusal_requires_an_exact_owned_revocation(revocation) -> None:
    with pytest.raises(TypeError, match="requires an owned revocation"):
        ComposerAutoMutationRefused(revocation)


def test_revocation_uses_existing_closed_redaction_schema():
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    failure = _failure_result(state, "PRIVATE diagnostic", error_code="auto_mutation_revoked", with_state_validation=False)
    credential_marker = "sk" + "-proj-" + "x" * 48
    failure = replace(
        failure,
        data={
            "candidate": "PRIVATE candidate marker",
            "inline_blob": "PRIVATE inline marker",
            "credential": credential_marker,
        },
    )
    public = redact_tool_call_response("set_pipeline", failure.to_dict(), telemetry=NoopRedactionTelemetry())
    assert public["success"] is False and public["version"] == state.version
    assert [entry["error_code"] for entry in public["validation"]["errors"]] == ["auto_mutation_revoked"]
    assert "PRIVATE" not in json.dumps(public)
    assert credential_marker not in json.dumps(public)
    unknown = failure.to_dict()
    unknown["validation"]["errors"][0]["error_code"] = "PRIVATE-unregistered-code"
    negative = redact_tool_call_response("set_pipeline", unknown, telemetry=NoopRedactionTelemetry())
    assert "PRIVATE" not in json.dumps(negative)


@pytest.mark.asyncio
async def test_sessionless_p4_keeps_graph_result_without_session_event(tmp_path):
    from tests.integration.web.composer.test_freeform_pipeline_planner import _pipeline

    base = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    candidate = replace(base, metadata=PipelineMetadata(name="Storage-only sessionless control"), version=2)
    arguments = _pipeline(tmp_path, "storage-only")
    call = _AdmittedToolCall("sessionless-control", _AdmittedToolFunction("set_pipeline", json.dumps(arguments)))
    result = ToolResult(success=True, updated_state=candidate, validation=candidate.validate(), affected_nodes=())
    outcome = _ToolOutcome(
        call=call, response=result, error_class=None, error_category=None, error_message=None, pre_version=1, post_version=2
    )
    persisted = await persist_turn_audit(
        sessions_service=None,
        redaction_telemetry=NoopRedactionTelemetry(),
        tool_outcomes=(outcome,),
        mutation_ceiling=None,
        authority_revocation=None,
        base_state=base,
        decoded_args_by_call_id={call.id: arguments},
        private_authorings=(),
        private_invocations=(),
        assistant_message=_AdmittedAssistantMessage(None),
        raw_assistant_content=None,
        assistant_tool_calls=(call,),
        crash_pending=False,
        session_id=None,
        session_operation_context=None,
        current_state_id=None,
        persisted_tool_call_turn=False,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        assistant_row_uses_current_dispatch=True,
    )
    assert persisted.authority_revocation is None
    assert persisted.audit_outcome is None and not persisted.persisted_tool_call_turn
    assert len(persisted.redacted_tool_rows) == 1
    row = persisted.redacted_tool_rows[0]
    assert row.visible_event is None and row.composition_state_payload is not None
    assert json.loads(row.content)["success"] is True
