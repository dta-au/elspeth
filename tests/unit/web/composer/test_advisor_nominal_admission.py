"""Nominal manual-Advisor admission through the real B2 preparation/execution.

These focused unit tests use the existing raw SDK response fixture and real
session/service/context/argument decoder/Advisor validator. Budget zero makes
the post-admission provider decision observable without a synthetic Advisor
answer. Primary gateway admission and audit run before the scripted SDK reply.
Existing required-guard/settlement tests remain separate obligations.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import tool_batch
from elspeth.web.composer.advisor_checkpoint import AdvisorCheckpointOwner
from tests.helpers.composer_sdk import ComposerSDKScript
from tests.unit.web.composer import _helpers as composer_helpers
from tests.unit.web.composer import test_advisor_tool as advisor_fixtures

_stub_advisor_end_gate_clean = composer_helpers._stub_advisor_end_gate_clean
_composer_available_by_default = advisor_fixtures._composer_available_by_default


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
    monkeypatch,
    tmp_path,
    instance_override,
    payload_kind,
):
    settings = advisor_fixtures._make_settings(budget=0, data_dir=tmp_path).model_copy(update={"composer_advisor_max_prompt_tokens": 1000})
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(),
        settings=settings,
    )
    owner = service._advisor_checkpoint
    assert type(owner) is AdvisorCheckpointOwner
    assert owner._settings is settings
    payload = _arguments()
    if payload_kind == "unknown-key":
        payload["unknown_complete_input_marker"] = "must not be projected away"
    elif payload_kind == "prompt-cap":
        payload["recent_errors"] = ["X" * 1500] * 3

    prepared = []
    admitted = []
    overrides = []
    execute = tool_batch._execute_continuation_call_live
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

    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", observe_execute)
    monkeypatch.setattr(AdvisorCheckpointOwner, "_validate_advisor_arguments", observe_actual_validator)
    if instance_override:
        monkeypatch.setattr(owner, "_validate_advisor_arguments", forbidden_instance_override)

    initial_state = advisor_fixtures._empty_state()
    turns = [
        advisor_fixtures._make_raw_tool_call("nominal_advisor", "request_advisor_hint", payload),
        advisor_fixtures._make_raw_text_only_response("The request remains available for the next turn."),
    ]
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=turns,
        advisor_model=settings.composer_advisor_model,
        advisor=[AssertionError("budget zero or argument refusal must precede Advisor SDK dispatch")],
    )
    primary, sdk = script.primary, script.advisor
    with patch("litellm.acompletion", new=script):
        result = await service.compose(
            "Review the complete input safely.",
            [],
            initial_state,
            session_id=session_id,
        )

    assert primary.call_count == 2
    assert sdk.call_count == 0
    assert overrides == []
    assert len(prepared) == len(admitted) == 1
    assert prepared[0][0] is admitted[0][0] is owner
    assert prepared[0][1] is admitted[0][1]
    assert admitted[0][1] == payload
    assert admitted[0][1]["schema_excerpt"] == payload["schema_excerpt"]
    invocations = [item for item in result.tool_invocations if item.tool_name == "request_advisor_hint"]
    assert len(invocations) == 1
    invocation = invocations[0]
    assert json.loads(invocation.arguments_canonical) == payload
    assert invocation.version_before == 1
    assert result.state == initial_state
    assert invocation.result_canonical is not None
    response = json.loads(invocation.result_canonical)
    if payload_kind == "valid":
        assert invocation.version_after == 1
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


@pytest.mark.asyncio
@pytest.mark.parametrize("receiver_kind", ["foreign", "subclass"])
async def test_foreign_or_subclass_receiver_is_a_first_party_invariant_before_admission(
    monkeypatch,
    tmp_path,
    receiver_kind,
):
    settings = advisor_fixtures._make_settings(budget=0, data_dir=tmp_path)
    service, session_id = composer_helpers._composer_service_with_session(
        catalog=advisor_fixtures._mock_catalog(),
        settings=settings,
    )
    owned = service._advisor_checkpoint
    attempts = []

    class ForeignOwner:
        def _validate_advisor_arguments(self, arguments):
            attempts.append(arguments)
            raise AssertionError("foreign receiver reached admission")

    class DerivedOwner(AdvisorCheckpointOwner):
        def _validate_advisor_arguments(self, arguments):
            attempts.append(arguments)
            raise AssertionError("subclass receiver reached admission")

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
    monkeypatch.setattr(service, "_advisor_checkpoint", replacement)
    prepared = []
    execute = tool_batch._execute_continuation_call_live

    async def observe_execute(**kwargs):
        prepared.append((kwargs["ctx"].advisor_checkpoint, kwargs["call"].arguments))
        return await execute(**kwargs)

    monkeypatch.setattr(tool_batch, "_execute_continuation_call_live", observe_execute)
    payload = _arguments()
    script = ComposerSDKScript(
        primary_model=service._provider_gateway._model,
        primary=[advisor_fixtures._make_raw_tool_call("nominal_advisor", "request_advisor_hint", payload)],
        advisor_model=settings.composer_advisor_model,
        advisor=[AssertionError("foreign receiver must fail before Advisor SDK dispatch")],
    )
    primary, sdk = script.primary, script.advisor
    with (
        patch("litellm.acompletion", new=script),
        pytest.raises(AuditIntegrityError, match="Manual Advisor admission requires the owned checkpoint owner"),
    ):
        await service.compose(
            "Review the complete input safely.",
            [],
            advisor_fixtures._empty_state(),
            session_id=session_id,
        )

    assert primary.call_count == 1
    assert sdk.call_count == 0
    assert attempts == []
    assert len(prepared) == 1
    assert prepared[0][0] is replacement
    assert prepared[0][1] == payload
