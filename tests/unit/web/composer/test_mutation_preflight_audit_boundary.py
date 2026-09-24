"""Post-mutation preflight failures cannot rewrite successful tool audit."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from elspeth.contracts.composer_audit import ComposerToolStatus
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import FailedTurnMetadata
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
from elspeth.web.composer.anti_anchor import AntiAnchorTracker
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.protocol import ComposerRuntimePreflightError
from elspeth.web.composer.service import ComposerServiceImpl, _admit_composer_llm_completion
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools import ToolResult
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.schemas import ValidationError, ValidationReadiness, ValidationResult
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.execution.test_validation_complaint_triage import complaint_triage_state

from .conftest import _fake_llm_response
from .test_preview_policy_snapshot import _service


@pytest.mark.anyio
@pytest.mark.parametrize("failure_type", [RuntimeError, TimeoutError, None])
@pytest.mark.parametrize("carrier_has_failed_turn", [True, False])
async def test_applied_mutation_is_audited_before_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception] | None,
    carrier_has_failed_turn: bool,
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    service = _service(tmp_path, snapshot)
    service._sessions_service = composer_service_with_real_sessions._sessions_service
    state = complaint_triage_state(pending=True)
    classifier = state.nodes[0]
    state = replace(
        state,
        nodes=(
            replace(
                classifier,
                options={
                    **classifier.options,
                    "system_prompt": "Categorize complaints. Reply with the single category word.",
                    "interpretation_requirements": [
                        {
                            **requirement,
                            "event_id": None,
                            "accepted_value": None,
                            "accepted_artifact_hash": None,
                            "resolved_prompt_template_hash": None,
                        }
                        for requirement in classifier.options["interpretation_requirements"]
                    ],
                    "prompt_template": "Classify {{ row.complaint }} using pending interpretation",
                    "prompt_template_parts": [
                        {"kind": "text", "text": "Classify {{ row.complaint }} using "},
                        {"kind": "interpretation_ref", "requirement_id": "category:classify_category"},
                    ],
                },
            ),
            *state.nodes[1:],
        ),
    )
    recorder = BufferingRecorder()
    recorder.record_llm_call(
        build_llm_call_record(
            model_requested="test-model",
            messages=[],
            tools=None,
            status=ComposerLLMCallStatus.SUCCESS,
            started_at=datetime.now(UTC),
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
            error_class=None,
            error_message=None,
        )
    )
    original = failure_type("preflight worker failed") if failure_type is not None else None
    failed_turn = FailedTurnMetadata(assistant_message_id=str(uuid4()), tool_calls_attempted=1)
    observations = []

    findings = ValidationResult(
        is_valid=False,
        checks=[],
        errors=[
            ValidationError(
                component_id="tidy_columns",
                component_type="transform",
                message="Edge contract violation: consumer requires str, producer emits Any",
                suggestion=None,
                error_code="graph_structure",
            )
        ],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=False, completion_ready=False, blockers=[]),
    )

    async def preflight(candidate: CompositionState, **kwargs: Any) -> ValidationResult:
        observations.append((candidate, kwargs["deadline"], recorder.invocations))
        if original is not None:
            raise ComposerRuntimePreflightError(
                original_exc=original,
                partial_state=candidate,
                failed_turn=failed_turn if carrier_has_failed_turn else None,
            )
        return findings

    monkeypatch.setattr(service, "_cached_runtime_preflight", preflight)
    completion = _admit_composer_llm_completion(
        _fake_llm_response(tool_calls=({"id": "mutation", "name": "set_metadata", "arguments": {"patch": {"name": "Complaint triage"}}},))
    )
    deadline = asyncio.get_running_loop().time() + 60
    messages = []

    async def dispatch():
        return await service._dispatch_tool_batch(
            call_model=_CallModelOutcome(completion=completion),
            state=state,
            last_validation=None,
            last_runtime_preflight=None,
            llm_messages=messages,
            recorder=recorder,
            anti_anchor=AntiAnchorTracker(),
            discovery_cache={},
            runtime_preflight_cache={},
            session_id=result_session_id,
            user_id=None,
            user_message_id=None,
            user_message_content=None,
            current_state_id=str(uuid4()),
            actor="test",
            initial_version=state.version,
            deadline=deadline,
            progress=None,
            session_scope="mutation-preflight-failure",
            advisor_calls_used=0,
            composition_turns_used=2,
            discovery_turns_used=1,
            failed_turn=failed_turn,
            cancellation_requested=asyncio.Event(),
            plugin_snapshot=snapshot,
            policy_catalog=PolicyCatalogView(catalog, snapshot, service._operator_profile_registry),
            session_operation_context=SessionOperationContext(
                SessionOperationFence(result_session_id, str(uuid4()), str(uuid4()), 1), SessionOperationKind.COMPOSE
            ),
        )

    if failure_type is None:
        outcome, _ = await dispatch()
        (tool_outcome,) = outcome.tool_outcomes
        assert isinstance(tool_outcome.response, ToolResult)
        assert tool_outcome.response.success is True
        # P4 persists this enriched response, independently of the already
        # recorded mutation result. The LLM and replay see the same findings.
        assert tool_outcome.response.runtime_preflight is findings
        assert outcome.last_runtime_preflight is findings
        feedback = json.loads(next(message["content"] for message in messages if message["role"] == "tool"))
        assert feedback["runtime_preflight"]["errors"][0]["message"] == findings.errors[0].message
        assert observations[0][2] == recorder.invocations
        assert len(recorder.invocations) == 1
        assert recorder.invocations[0].status is ComposerToolStatus.SUCCESS
        assert recorder.invocations[0].version_after == outcome.state.version
        return

    with pytest.raises(ComposerRuntimePreflightError) as raised:
        await dispatch()

    ((candidate, observed_deadline, prior_invocations),) = observations
    assert candidate.version == state.version + 1
    assert candidate.metadata.name == "Complaint triage"
    assert observed_deadline == deadline
    assert len(prior_invocations) == 1
    (invocation,) = recorder.invocations
    assert prior_invocations == recorder.invocations
    assert invocation.status is ComposerToolStatus.SUCCESS
    assert invocation.version_before == state.version
    assert invocation.version_after == candidate.version
    assert invocation.error_class is None
    assert raised.value.original_exc is original
    assert raised.value.__cause__ is original
    assert raised.value.partial_state is candidate
    assert raised.value.failed_turn is failed_turn
    assert raised.value.tool_invocations == recorder.invocations
    assert raised.value.llm_calls == recorder.llm_calls
