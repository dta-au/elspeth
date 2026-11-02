"""Nominal manual-Advisor admission through the current Continuation owner.

The existing phase harness initializes a real composition, dispatches PRIMARY
through the SDK boundary, and prepares its provider-authored tool call. These
controls advance that one issued tool through its real execution owner; they
do not recreate the deleted compose loop or substitute an Advisor result.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import continuation_live_phases
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.sessions.test_continuation_live_phases import _advance, _phase_case

operation_store = _operation_store


def _arguments():
    return {
        "trigger": "proactive_security_safety",
        "problem_summary": "Review the complete optional schema and input handling.",
        "recent_errors": ["No secret material is present."],
        "attempted_actions": ["Read the complete published schema."],
        "schema_excerpt": '{"optional_tail":"must reach actual admission"}',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("instance_override", [False, True], ids=["ordinary-owner", "instance-override-ignored"])
@pytest.mark.parametrize("payload_kind", ["valid", "unknown-key", "prompt-cap"])
async def test_actual_owner_receives_complete_arguments_and_its_actual_prompt_cap(
    operation_store,
    monkeypatch,
    tmp_path,
    instance_override,
    payload_kind,
):
    payload = _arguments()
    if payload_kind == "unknown-key":
        payload["unknown_complete_input_marker"] = "must not be projected away"
    elif payload_kind == "prompt-cap":
        payload["recent_errors"] = ["X" * 1500] * 3

    prepared = []
    admitted = []
    overrides = []
    execute = continuation_live_phases._execute_continuation_call_live
    validate = AdvisorCheckpointOwner._validate_advisor_arguments

    async def observe_execute(**kwargs):
        prepared.append((kwargs["ctx"].advisor_checkpoint, kwargs["call"].arguments))
        return await execute(**kwargs)

    def observe_actual_validator(self, arguments):
        admitted.append((self, arguments))
        return validate(self, arguments)

    def forbidden_instance_override(arguments):
        overrides.append(arguments)
        raise AssertionError("an instance attribute must not replace nominal admission")

    monkeypatch.setattr(continuation_live_phases, "_execute_continuation_call_live", observe_execute)
    monkeypatch.setattr(AdvisorCheckpointOwner, "_validate_advisor_arguments", observe_actual_validator)
    async with _phase_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_call=("nominal_advisor", "request_advisor_hint", payload),
        settings_overrides={"composer_advisor_max_calls_per_compose": 0, "composer_advisor_max_prompt_tokens": 1000},
    ) as (phases, _running, _primary, sdk):
        service = phases.service
        owner = service._advisor_checkpoint
        assert type(owner) is AdvisorCheckpointOwner
        assert owner._settings is service._settings
        assert owner._settings.composer_advisor_max_prompt_tokens == 1000
        assert service._settings.composer_advisor_max_calls_per_compose == 0
        if instance_override:
            monkeypatch.setattr(owner, "_validate_advisor_arguments", forbidden_instance_override)
        initial_state = phases.runtime.live.state
        await _advance(phases)

        # Only the real PRIMARY call reached the SDK. Budget zero and argument
        # refusal each stop manual Advisor dispatch before another SDK call.
        assert len(sdk) == 1
        assert overrides == []
        assert len(prepared) == len(admitted) == 1
        assert prepared[0][0] is admitted[0][0] is owner
        assert prepared[0][1] is admitted[0][1]
        assert admitted[0][1] == payload
        assert admitted[0][1]["schema_excerpt"] == payload["schema_excerpt"]
        invocations = [item for item in phases.runtime.live.recorder.invocations if item.tool_name == "request_advisor_hint"]
        assert len(invocations) == 1
        invocation = invocations[0]
        assert json.loads(invocation.arguments_canonical) == payload
        assert invocation.version_before == initial_state.version
        assert phases.runtime.live.state == initial_state
        assert invocation.result_canonical is not None
        response = json.loads(invocation.result_canonical)
        if payload_kind == "valid":
            assert invocation.version_after == initial_state.version
            assert invocation.status.name == "SUCCESS"
            assert response["status"] == "BUDGET_EXHAUSTED"
            assert response["budget_used"] == response["budget_remaining"] == 0
        else:
            assert invocation.version_after is None
            assert invocation.status.name == "ARG_ERROR"
            assert response["status"] == "ARG_ERROR"
            if payload_kind == "prompt-cap":
                assert invocation.error_category is ToolArgumentErrorCategory.PROMPT_BUDGET
                assert "prompt size" in response["error"]
            else:
                assert invocation.error_category is ToolArgumentErrorCategory.SCHEMA_SHAPE
                assert "must conform to the published schema" in response["error"]
    phases.work.coordinator.assert_completed()


@pytest.mark.asyncio
@pytest.mark.parametrize("receiver_kind", ["foreign", "subclass"])
async def test_foreign_or_subclass_receiver_is_a_first_party_invariant_before_admission(
    operation_store,
    monkeypatch,
    tmp_path,
    receiver_kind,
):
    attempts = []

    class ForeignOwner:
        def _validate_advisor_arguments(self, arguments):
            attempts.append(arguments)
            raise AssertionError("foreign receiver reached admission")

    class DerivedOwner(AdvisorCheckpointOwner):
        def _validate_advisor_arguments(self, arguments):
            attempts.append(arguments)
            raise AssertionError("subclass receiver reached admission")

    payload = _arguments()
    async with _phase_case(
        operation_store,
        tmp_path,
        monkeypatch,
        tool_call=("nominal_advisor", "request_advisor_hint", payload),
        settings_overrides={"composer_advisor_max_calls_per_compose": 0},
    ) as (phases, _running, _primary, sdk):
        owned = phases.service._advisor_checkpoint
        if receiver_kind == "foreign":
            replacement = ForeignOwner()
        else:
            replacement = DerivedOwner(
                settings=owned._settings,
                composer_skill_text=owned._composer_skill_text,
                advisor_endpoint_base_url=owned._advisor_endpoint_base_url,
                advisor_endpoint_api_key=owned._advisor_endpoint_api_key,
                sessions_service=owned._sessions_service,
                chargeable_admission=owned._chargeable_admission,
                prompt_guard_mount=owned._prompt_guard_mount,
                secret_service=owned._secret_service,
            )
        prepared = []
        execute = continuation_live_phases._execute_continuation_call_live

        async def substitute_receiver(**kwargs):
            # Current inventory already refuses a foreign service owner.
            # Substitute at the issued tool's execution boundary to exercise
            # manual admission's independent nominal receiver invariant.
            assert kwargs["ctx"].advisor_checkpoint is owned
            context = replace(kwargs["ctx"], advisor_checkpoint=replacement)
            prepared.append((context.advisor_checkpoint, kwargs["call"].arguments))
            return await execute(**{**kwargs, "ctx": context})

        monkeypatch.setattr(continuation_live_phases, "_execute_continuation_call_live", substitute_receiver)
        with pytest.raises(AuditIntegrityError, match="Manual Advisor admission requires the owned checkpoint owner"):
            await _advance(phases)

        assert len(sdk) == 1
        assert attempts == []
        assert len(prepared) == 1
        assert prepared[0][0] is replacement
        assert prepared[0][1] == payload
    phases.work.coordinator.assert_completed()
