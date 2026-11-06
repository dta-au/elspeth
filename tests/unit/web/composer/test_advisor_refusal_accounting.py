"""Manual Advisor reservations follow actual dispatch, including failed consumers.

The tool-loop tests enter the real argument admission, graph preparation,
provider custody and P4 owners. Coverage/timeout/cancellation injections are
labelled owner-failure controls; they do not claim natural HTTP reachability.
Only the SDK transport is replaced for successful physical attempts.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from litellm import ModelResponse
from openai import APIConnectionError
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer import advisor_context, redaction, tool_batch
from elspeth.web.composer._compose_loop_carriers import _AdvisorCallSuccess, _AdvisorFirstPartyFailure
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from elspeth.web.composer.advisor_transcript import AdvisorTranscriptOwner
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.manual_advisor_allowance import ManualAdvisorAllowance
from elspeth.web.composer.pipeline_planner import PlannerBudgetPolicy, PlannerContinuationBudget
from elspeth.web.composer.protocol import ComposerContextUnsupported, ComposerContextUnsupportedReason, ComposerConvergenceError
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import NodeSpec, PipelineMetadata
from elspeth.web.composer.tools.sessions import RequestAdvisorHintArgumentsModel
from elspeth.web.composer.turn_effects import TurnEvidence
from elspeth.web.sessions.models import chat_messages_table, quota_provider_attempts_table
from tests.helpers.composer_sdk import ComposerSDKScript
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer import test_advisor_tool as advisor_fixtures

_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean
_composer_available_by_default = advisor_fixtures._composer_available_by_default

_USER = "Review the ordinary declared inputs."
# Compose-loop accounting tests stub END independently. This test module also
# exercises the actual END preparation owner through its original function.
_RUN_CHECKPOINT = AdvisorCheckpointOwner._run_advisor_checkpoint


def _manual_call(model: str, call_id: str) -> ModelResponse:
    return ModelResponse(
        model=model,
        choices=[
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": "request_advisor_hint",
                                "arguments": json.dumps(
                                    {
                                        "trigger": "proactive_security_safety",
                                        "problem_summary": "Review the declared inputs.",
                                        "recent_errors": [],
                                        "attempted_actions": [],
                                    }
                                ),
                            },
                        }
                    ],
                }
            }
        ],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )


def _reply(model: str, content: str = "The complete declared inputs were reviewed.") -> ModelResponse:
    return ModelResponse(
        model=model,
        choices=[{"message": {"role": "assistant", "content": content}}],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )


@dataclass(frozen=True)
class _FinishedDispatch:
    used: int
    calls: tuple[ComposerLLMCall, ...]


def _observe_dispatches(monkeypatch: pytest.MonkeyPatch) -> list[_FinishedDispatch]:
    observed: list[_FinishedDispatch] = []
    execute = tool_batch._execute_continuation_call_live

    async def observe(**kwargs):
        try:
            return await execute(**kwargs)
        finally:
            observed.append(_FinishedDispatch(kwargs["batch"].advisor_calls_used, kwargs["ctx"].recorder.llm_calls))

    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", observe)
    return observed


def _contains_original(error: BaseException, original: BaseException) -> bool:
    """Search exact retained causes without treating an error's text as evidence."""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is original:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _context_refusal_response(reason: ComposerContextUnsupportedReason) -> dict[str, object]:
    return {
        "success": False,
        "outbound_call_made": False,
        **ComposerContextUnsupported(reason).to_payload(),
        "budget_used": 0,
        "budget_remaining": 1,
    }


@pytest.mark.parametrize("reason", ["graph_resource_limit", "graph_coverage_unavailable", "manual_advisor_allowance"])
def test_context_refusal_projection_preserves_only_the_owned_fixed_payload(reason: ComposerContextUnsupportedReason) -> None:
    payload = _context_refusal_response(reason)
    telemetry = NoopRedactionTelemetry()
    projected = redaction.redact_tool_call_response("request_advisor_hint", payload, telemetry=telemetry)
    assert projected == payload
    assert redaction.redact_tool_call_response("request_advisor_hint", projected, telemetry=telemetry) == projected
    assert telemetry.manifest_dispatch_calls == [{"tool_name": "request_advisor_hint", "shape": "declarative"}] * 2
    assert telemetry.unknown_response_key_calls == telemetry.response_projection_limit_calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"reason": "PRIVATE_UNREGISTERED_REASON"}, id="unknown-reason"),
        pytest.param(
            {"detail": ComposerContextUnsupported("manual_advisor_allowance").to_payload()["detail"]}, id="different-reasons-detail"
        ),
        pytest.param({"detail": "PRIVATE_UNREGISTERED_DETAIL"}, id="arbitrary-detail"),
        pytest.param({"error_type": "PRIVATE_ERROR_TYPE"}, id="changed-error-type"),
        pytest.param({"failure_code": "PRIVATE_FAILURE_CODE"}, id="changed-failure-code"),
        pytest.param({"reason": None}, id="null-reason"),
        pytest.param({"reason": True}, id="boolean-reason"),
        pytest.param({"detail": ["PRIVATE_DETAIL"]}, id="sequence-detail"),
        pytest.param({"detail": {"PRIVATE_KEY": "PRIVATE_DETAIL"}}, id="mapping-detail"),
        pytest.param({"error_type": 0}, id="integer-error-type"),
        pytest.param({"failure_code": False}, id="boolean-failure-code"),
        pytest.param({"success": True}, id="successful-response"),
        pytest.param({"success": 0}, id="integer-success"),
        pytest.param({"outbound_call_made": True}, id="dispatched-response"),
        pytest.param({"outbound_call_made": 0}, id="integer-dispatch"),
    ],
)
def test_context_refusal_projection_rejects_an_uncorrelated_or_foreign_tuple(overrides: dict[str, object]) -> None:
    payload = _context_refusal_response("graph_resource_limit")
    payload.update(overrides)
    projected = redaction.redact_tool_call_response("request_advisor_hint", payload, telemetry=NoopRedactionTelemetry())
    for key, expected in ComposerContextUnsupported("graph_resource_limit").to_payload().items():
        assert projected[key] != expected, "no semantic field is admitted independently of the complete fixed tuple"
    assert "PRIVATE" not in json.dumps(projected)


@pytest.mark.parametrize("missing", ["success", "outbound_call_made", "error_type", "failure_code", "reason", "detail"])
def test_context_refusal_projection_requires_every_correlated_field(missing: str) -> None:
    payload = _context_refusal_response("graph_resource_limit")
    del payload[missing]
    projected = redaction.redact_tool_call_response("request_advisor_hint", payload, telemetry=NoopRedactionTelemetry())
    for key, expected in ComposerContextUnsupported("graph_resource_limit").to_payload().items():
        assert key not in projected or projected[key] != expected


def test_context_refusal_projection_keeps_private_fields_redacted_and_other_tools_unprivileged() -> None:
    payload = _context_refusal_response("graph_coverage_unavailable")
    payload.update(
        {
            "guidance": "PRIVATE_GUIDANCE",
            "error": "PRIVATE_PROVIDER_BODY",
            "error_class": "PRIVATE_PROVIDER_CLASS",
            "provider_failure": {"PRIVATE_PROVIDER_KEY": "PRIVATE_PROVIDER_VALUE"},
            "PRIVATE_UNKNOWN_KEY": "PRIVATE_UNKNOWN_VALUE",
        }
    )
    telemetry = NoopRedactionTelemetry()
    projected = redaction.redact_tool_call_response("request_advisor_hint", payload, telemetry=telemetry)
    for key, value in ComposerContextUnsupported("graph_coverage_unavailable").to_payload().items():
        assert projected[key] == value
    assert projected["guidance"] == redaction.REDACTED_SENSITIVE_NO_SUMMARIZER
    assert projected["error"] == projected["error_class"] == "<redacted-response-text>"
    assert projected[redaction.REDACTED_UNKNOWN_RESPONSE_FIELD] == redaction.REDACTED_UNKNOWN_RESPONSE_KEY
    assert len(telemetry.unknown_response_key_calls) == 2
    assert "PRIVATE" not in json.dumps(projected)
    assert redaction.redact_tool_call_response("request_advisor_hint", projected, telemetry=NoopRedactionTelemetry()) == projected

    other = redaction.redact_tool_call_response("wire_secret_ref", payload, telemetry=NoopRedactionTelemetry())
    for key, value in ComposerContextUnsupported("graph_coverage_unavailable").to_payload().items():
        assert key not in other or other[key] != value
    assert "PRIVATE" not in json.dumps(other)
    with pytest.raises(AuditIntegrityError):
        redaction.redact_tool_call_response("not_a_registered_tool", payload, telemetry=NoopRedactionTelemetry())


def test_context_refusal_projection_keeps_existing_response_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _context_refusal_response("manual_advisor_allowance")
    payload["budget_used"] = [0] * (redaction.RESPONSE_PROJECTION_MAX_CONTAINER_WIDTH + 1)
    telemetry = NoopRedactionTelemetry()
    projected = redaction.redact_tool_call_response("request_advisor_hint", payload, telemetry=telemetry)
    assert projected["budget_used"] == "<redacted-response-projection-limit>"
    for key, value in ComposerContextUnsupported("manual_advisor_allowance").to_payload().items():
        assert projected[key] == value
    assert telemetry.response_projection_limit_calls == [{"tool_name": "request_advisor_hint", "scope": "key"}]

    payload = _context_refusal_response("manual_advisor_allowance")
    payload.update({f"overflow_{index}": "PRIVATE" for index in range(redaction.RESPONSE_PROJECTION_MAX_CONTAINER_WIDTH)})
    assert redaction.redact_tool_call_response("request_advisor_hint", payload, telemetry=telemetry) == {
        "_redaction_status": "response_projection_limit"
    }
    monkeypatch.setattr(redaction, "RESPONSE_PROJECTION_MAX_OUTPUT_BYTES", 1)
    assert redaction.redact_tool_call_response(
        "request_advisor_hint", _context_refusal_response("manual_advisor_allowance"), telemetry=telemetry
    ) == {"_redaction_status": "response_projection_limit"}
    assert telemetry.response_projection_limit_calls[-2:] == [{"tool_name": "request_advisor_hint", "scope": "row"}] * 2


def test_overlapping_reservations_cannot_pass_the_manual_backstop() -> None:
    allowance = ManualAdvisorAllowance(limit=2, used=0)
    first = allowance.reserve()
    second = allowance.reserve()
    assert first is not None and second is not None
    assert allowance.used == 0
    assert allowance.available == 0
    assert allowance.reserve() is None

    first.dispatch()
    first.close()
    assert allowance.used == 1 and allowance.available == 0
    second.close()
    assert allowance.used == 1 and allowance.available == 1

    replacement = allowance.reserve()
    assert replacement is not None
    assert allowance.reserve() is None
    replacement.dispatch()
    replacement.close()
    assert allowance.used == 2 and allowance.available == 0
    assert allowance.reserve() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_selected_value", [False, True], ids=["complete-supported", "incomplete-selected"])
async def test_actual_manual_selected_coverage_controls_dispatch_and_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, missing_selected_value: bool
) -> None:
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    state = replace(
        advisor_fixtures._empty_state(),
        metadata=PipelineMetadata(description="Complete benign graph description. " * 100),
    )
    materialize = advisor_context.materialize_advisor_complete_values
    selected = []

    def resolve(*args, **kwargs):
        values = materialize(*args, **kwargs)
        assert values and len(values) == 1
        selected.append(values)
        return () if missing_selected_value else values

    monkeypatch.setattr(advisor_context, "materialize_advisor_complete_values", resolve)
    observed = _observe_dispatches(monkeypatch)
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[
            _manual_call(service._provider_gateway._model, "coverage-owner"),
            _reply(service._provider_gateway._model),
        ],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[_reply(service._settings.composer_advisor_model)],
    )
    with patch("litellm.acompletion", new=script):
        if missing_selected_value:
            with pytest.raises(ComposerContextUnsupported) as caught:
                await service.compose(_USER, [], state, session_id=session_id)
            assert caught.value.reason == "graph_coverage_unavailable"
            assert len(caught.value.llm_calls) == 1
        else:
            result = await service.compose(_USER, [], state, session_id=session_id)
            assert result.state == state

    assert len(selected) == len(observed) == 1
    assert observed[0].used == (0 if missing_selected_value else 1)
    assert script.advisor.await_count == (0 if missing_selected_value else 1)
    assert script.primary.await_count == (1 if missing_selected_value else 2)
    rows = composer_helpers._persisted_tool_responses(service, session_id)
    assert len(rows) == 1 and rows[0]["tool_call_id"] == "coverage-owner"
    payload = json.loads(rows[0]["result_canonical"])
    assert "plugin_crash" not in rows[0]["result_canonical"]
    if missing_selected_value:
        assert payload["failure_code"] == "context_unsupported"
        assert payload["error_type"] == "composer_context_unsupported"
        assert payload["reason"] == "graph_coverage_unavailable"
        assert payload["detail"] == ComposerContextUnsupported("graph_coverage_unavailable").to_payload()["detail"]
        assert payload["success"] is False and payload["budget_used"] == 0 and payload["budget_remaining"] == 1
        assert payload["outbound_call_made"] is False
        with service._require_sessions_service()._engine.connect() as connection:
            audit_rows = connection.execute(
                select(chat_messages_table.c.tool_calls).where(
                    chat_messages_table.c.session_id == session_id, chat_messages_table.c.role == "audit"
                )
            ).all()
        guard_roles = [
            row.tool_calls[0]["check"]["role"]
            for row in audit_rows
            if row.tool_calls and row.tool_calls[0]["_kind"] == "prompt_guard_audit"
        ]
        assert guard_roles == ["composer"]
    else:
        assert payload["status"] == "SUCCESS"
        assert payload["budget_used"] == 1 and payload["budget_remaining"] == 0


@pytest.mark.asyncio
async def test_pre_dispatch_advisor_timeout_releases_before_feedback_and_next_manual_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    inventory = service._advisor_checkpoint._current_env_ref_names
    attempted = []

    async def current_inventory(sid):
        attempted.append(sid)
        if len(attempted) == 1:
            raise TimeoutError("injected preparation timeout before physical admission")
        return await inventory(sid)

    monkeypatch.setattr(service._advisor_checkpoint, "_current_env_ref_names", current_inventory)
    monkeypatch.setattr(tool_batch, "_remaining_compose_seconds", lambda _deadline: 80.0)
    observed = _observe_dispatches(monkeypatch)
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[
            _manual_call(service._provider_gateway._model, "preparation-timeout"),
            _manual_call(service._provider_gateway._model, "after-released-reservation"),
            _reply(service._provider_gateway._model),
        ],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[_reply(service._settings.composer_advisor_model)],
    )
    with patch("litellm.acompletion", new=script):
        await service.compose(_USER, [], advisor_fixtures._empty_state(), session_id=session_id)

    assert attempted == [session_id, session_id]
    assert [item.used for item in observed] == [0, 1]
    assert script.primary.await_count == 3 and script.advisor.await_count == 1
    rows = composer_helpers._persisted_tool_responses(service, session_id)
    assert [row["tool_call_id"] for row in rows] == ["preparation-timeout", "after-released-reservation"]
    timeout, success = [json.loads(row["result_canonical"]) for row in rows]
    assert timeout["status"] == "ADVISOR_ERROR"
    assert timeout["budget_used"] == 0 and timeout["budget_remaining"] == 1
    assert success["status"] == "SUCCESS"
    assert success["budget_used"] == 1 and success["budget_remaining"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["compose-timeout", "cancellation"])
async def test_pre_dispatch_lifecycle_exits_release_the_manual_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lifecycle: str
) -> None:
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    failure = (
        asyncio.CancelledError("original cancellation during Advisor preparation")
        if lifecycle == "cancellation"
        else TimeoutError("injected preparation timeout at compose deadline")
    )

    async def unavailable_inventory(_sid):
        raise failure

    monkeypatch.setattr(service._advisor_checkpoint, "_current_env_ref_names", unavailable_inventory)
    monkeypatch.setattr(tool_batch, "_remaining_compose_seconds", lambda _deadline: 5.0)
    observed = _observe_dispatches(monkeypatch)
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[_manual_call(service._provider_gateway._model, "lifecycle-before-dispatch")],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[],
    )
    expected = (asyncio.CancelledError, BaseExceptionGroup) if lifecycle == "cancellation" else ComposerConvergenceError
    with patch("litellm.acompletion", new=script), pytest.raises(expected) as caught:
        await service.compose(_USER, [], advisor_fixtures._empty_state(), session_id=session_id)

    assert len(observed) == 1 and observed[0].used == 0
    assert script.primary.await_count == 1 and script.advisor.await_count == 0
    assert all(call.model_requested != service._settings.composer_advisor_model for call in observed[0].calls)
    if lifecycle == "cancellation":
        assert _contains_original(caught.value, failure)
    else:
        assert caught.value.budget_exhausted == "timeout"
        rows = composer_helpers._persisted_tool_responses(service, session_id)
        assert len(rows) == 1
        payload = json.loads(rows[0]["result_canonical"])
        assert payload["status"] == "COMPOSE_TIMEOUT"
        assert payload["budget_used"] == 0 and payload["budget_remaining"] == 1


@pytest.mark.asyncio
async def test_sdk_entered_recoverable_provider_failure_spends_allowance_and_blocks_the_next_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    failure = APIConnectionError(request=httpx.Request("POST", "https://provider.example.test/v1/chat/completions"))
    observed = _observe_dispatches(monkeypatch)
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[
            _manual_call(service._provider_gateway._model, "sdk-entered-provider-failure"),
            _manual_call(service._provider_gateway._model, "next-hint-at-backstop"),
            _reply(service._provider_gateway._model),
        ],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[failure],
    )
    with patch("litellm.acompletion", new=script):
        result = await service.compose(_USER, [], advisor_fixtures._empty_state(), session_id=session_id)

    assert script.primary.await_count == 3 and script.advisor.await_count == 1
    assert [item.used for item in observed] == [1, 1]
    rows = composer_helpers._persisted_tool_responses(service, session_id)
    assert [row["tool_call_id"] for row in rows] == ["sdk-entered-provider-failure", "next-hint-at-backstop"]
    failed, exhausted = [json.loads(row["result_canonical"]) for row in rows]
    assert failed["status"] == "ADVISOR_ERROR" and failed["error_class"] == "<redacted-response-text>"
    assert failed["budget_used"] == 1 and failed["budget_remaining"] == 0
    assert exhausted["status"] == "BUDGET_EXHAUSTED"
    assert exhausted["budget_used"] == 1 and exhausted["budget_remaining"] == 0
    advisor_calls = [call for call in result.llm_calls if call.model_requested == service._settings.composer_advisor_model]
    assert len(advisor_calls) == 1
    assert advisor_calls[0].status.name == "API_ERROR" and advisor_calls[0].error_class == "APIConnectionError"


@pytest.mark.asyncio
async def test_sdk_entered_consumer_failure_spends_allowance_without_recorder_buffer_growth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    observed = _observe_dispatches(monkeypatch)
    record = BufferingRecorder.record_llm_call
    original = AuditIntegrityError("injected required Advisor recorder consumer failure")
    failed_calls = []

    def fail_before_buffer_growth(self, call, *, provider_custody=None):
        if call.model_requested == service._settings.composer_advisor_model:
            failed_calls.append(call)
            raise original
        return record(self, call, provider_custody=provider_custody)

    monkeypatch.setattr(BufferingRecorder, "record_llm_call", fail_before_buffer_growth)
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[_manual_call(service._provider_gateway._model, "sdk-before-recorder-failure")],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[_reply(service._settings.composer_advisor_model)],
    )
    with patch("litellm.acompletion", new=script), pytest.raises((AuditIntegrityError, BaseExceptionGroup)) as caught:
        await service.compose(_USER, [], advisor_fixtures._empty_state(), session_id=session_id)

    assert _contains_original(caught.value, original)
    assert script.primary.await_count == script.advisor.await_count == len(failed_calls) == len(observed) == 1
    assert observed[0].used == 1
    assert all(call is not failed_calls[0] for call in observed[0].calls)
    assert failed_calls[0].call_id is not None
    with service._require_sessions_service()._engine.connect() as connection:
        attempts = connection.execute(
            select(quota_provider_attempts_table).where(quota_provider_attempts_table.c.session_id == session_id)
        ).all()
    assert len(attempts) == 2
    assert all(attempt.settled_at is not None and attempt.ledger_entry_id is not None for attempt in attempts)
    assert failed_calls[0].call_id in {attempt.attempt_id for attempt in attempts}


@pytest.mark.asyncio
async def test_sdk_entered_cancellation_keeps_its_spent_manual_allowance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    original = asyncio.CancelledError("original cancellation after physical Advisor entry")
    observed = _observe_dispatches(monkeypatch)
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[_manual_call(service._provider_gateway._model, "sdk-entered-cancellation")],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[original],
    )
    with patch("litellm.acompletion", new=script), pytest.raises((asyncio.CancelledError, BaseExceptionGroup)) as caught:
        await service.compose(_USER, [], advisor_fixtures._empty_state(), session_id=session_id)

    assert _contains_original(caught.value, original)
    assert script.primary.await_count == script.advisor.await_count == len(observed) == 1
    assert observed[0].used == 1
    advisor_calls = [call for call in observed[0].calls if call.model_requested == service._settings.composer_advisor_model]
    assert len(advisor_calls) == 1 and advisor_calls[0].status.name == "CANCELLED"


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_transcript", [False, True], ids=["public-allowance", "required-evidence-exemption"])
async def test_expanded_manual_graph_uses_the_actual_transcript_conditioned_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owned_transcript: bool
) -> None:
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    options = {f"ordinary_note_{index}": f"Complete value {index}: " + "ordinary detail " * 205 for index in range(6)}
    node = NodeSpec(
        id="ordinary_step",
        node_type="transform",
        plugin="passthrough",
        input="records",
        on_success="result",
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )
    state = replace(advisor_fixtures._empty_state(), nodes=(node,))
    arguments = RequestAdvisorHintArgumentsModel(
        trigger="proactive_security_safety", problem_summary="Review the complete graph.", recent_errors=[], attempted_actions=[]
    )
    recorder = BufferingRecorder()
    dispatched = []
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[_reply(service._settings.composer_advisor_model)],
    )
    async with advisor_fixtures._owned_direct_advisor(service, session_id, genuine_user_message=_USER) as provider:
        context = provider.required_work.coordinator.authority.context
        transcript = AdvisorTranscriptOwner(request=_USER).snapshot() if owned_transcript else None
        with patch("litellm.acompletion", new=script):
            outcome = await service._advisor_checkpoint._call_advisor_for_tool(
                arguments,
                recorder=recorder,
                provider_owner=provider,
                advisor_transcript=transcript,
                current_state=state,
                session_id=session_id,
                session_operation_context=context,
                on_provider_dispatch=lambda: dispatched.append(True),
            )
    if owned_transcript:
        assert type(outcome) is _AdvisorCallSuccess
        assert dispatched == [True] and script.advisor.await_count == len(recorder.llm_calls) == 1
        user_content = script.advisor.call_args.kwargs["messages"][1]["content"]
        assert len(user_content) > 16_000
        for value in options.values():
            assert value in user_content
    else:
        assert type(outcome) is _AdvisorFirstPartyFailure
        assert type(outcome.original_exc) is ComposerContextUnsupported
        assert outcome.original_exc.reason == "manual_advisor_allowance"
        assert dispatched == [] and script.advisor.await_count == 0 and recorder.llm_calls == ()


@pytest.mark.asyncio
async def test_final_preparation_refusal_does_not_reserve_a_continuation_attempt(tmp_path: Path) -> None:
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    recorder = BufferingRecorder()
    arguments = {
        "trigger": "proactive_security_safety",
        "problem_summary": "Review the complete supplied excerpt.",
        "recent_errors": [],
        "attempted_actions": [],
        "schema_excerpt": "ordinary complete schema detail " * 600,
    }
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[],
    )
    async with advisor_fixtures._owned_direct_advisor(service, session_id, genuine_user_message=_USER) as provider:
        context = provider.required_work.coordinator.authority.context
        evidence = TurnEvidence(
            session_id=session_id,
            session_operation_context=context,
            request_id="final-preparation-refusal",
            request=_USER,
            initial_state_id=None,
            initial_state=advisor_fixtures._empty_state(),
        )
        budget = PlannerContinuationBudget(
            policy=PlannerBudgetPolicy(1, 100_000, 1024, Decimal("1")),
            session_id=session_id,
            request_id="final-preparation-refusal",
            request_hash=stable_hash(_USER),
            provider_owner=provider,
            evidence_owner=evidence,
            deadline=asyncio.get_running_loop().time() + 30,
            reserved_calls=0,
            validated_cost=Decimal("0"),
            cost_known=True,
        )
        with patch("litellm.acompletion", new=script), pytest.raises(ComposerContextUnsupported) as caught:
            await service._advisor_checkpoint._call_advisor_with_audit(
                arguments, recorder=recorder, provider_owner=provider, continuation_budget=budget
            )
        assert caught.value.reason == "manual_advisor_allowance"
        assert budget.reserved_calls == 0
    assert script.advisor.await_count == 0 and recorder.llm_calls == ()


@pytest.mark.asyncio
async def test_actual_checkpoint_reserves_only_after_final_preparation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(), settings=advisor_fixtures._make_settings(budget=1, data_dir=tmp_path)
    )
    state = advisor_fixtures._empty_state()
    recorder = BufferingRecorder()
    prepared = []

    def refuse_final_message(arguments):
        summary = arguments["_elspeth_advisor_summary_parts"]
        assert type(summary) is advisor_context.AdvisorSummaryParts
        prepared.append(summary)
        raise ComposerContextUnsupported("graph_resource_limit")

    monkeypatch.setattr(advisor_context, "build_advisor_user_message", refuse_final_message)
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[],
        advisor_model=service._settings.composer_advisor_model,
        advisor=[],
    )
    async with advisor_fixtures._owned_direct_advisor(service, session_id, genuine_user_message=_USER) as provider:
        context = provider.required_work.coordinator.authority.context
        evidence = TurnEvidence(
            session_id=session_id,
            session_operation_context=context,
            request_id="checkpoint-final-preparation",
            request=_USER,
            initial_state_id=None,
            initial_state=state,
        )
        review = evidence.review_candidate(candidate="Review the declared inputs.", request=_USER, current_state=state)
        budget = PlannerContinuationBudget(
            policy=PlannerBudgetPolicy(1, 100_000, 1024, Decimal("1")),
            session_id=session_id,
            request_id="checkpoint-final-preparation",
            request_hash=stable_hash(_USER),
            provider_owner=provider,
            evidence_owner=evidence,
            deadline=asyncio.get_running_loop().time() + 30,
            reserved_calls=0,
            validated_cost=Decimal("0"),
            cost_known=True,
        )
        budget.bind_publication(review)
        with patch("litellm.acompletion", new=script), pytest.raises(ComposerContextUnsupported) as caught:
            await _RUN_CHECKPOINT(
                service._advisor_checkpoint,
                phase="end",
                state=state,
                session_id=session_id,
                recorder=recorder,
                session_operation_context=context,
                provider_owner=provider,
                publication_review=review,
                continuation_budget=budget,
            )
        assert caught.value.reason == "graph_resource_limit"
        assert len(prepared) == 1
        assert budget.reserved_calls == 0
    assert script.advisor.await_count == 0 and recorder.llm_calls == ()
