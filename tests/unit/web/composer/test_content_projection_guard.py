"""Complete-value reuse enters the existing audited guard on each actual use."""

from dataclasses import replace
from uuid import UUID

import litellm
import pytest

from elspeth.contracts.composer_content import ComposerContentScope
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.prompt_guard import PromptGuardDecision
from elspeth.web.composer import prompt_guard, provider_gateway
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result
from elspeth.web.composer.pipeline_proposal import composition_content_hash
from elspeth.web.composer.prompt_guard import PromptGuardRefused, prompt_guard_user_scope
from elspeth.web.composer.prompts import build_composition_content_view
from elspeth.web.composer.provider_quota import ProviderInvocationFamily
from elspeth.web.composer.state import PipelineMetadata
from tests.unit.web.composer._helpers import _empty_state, _make_settings
from tests.unit.web.composer.test_content_projection import _dispatch
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.composer.test_prompt_guard_wiring import _Client, _guarded_service

pytest_plugins = ("tests.unit.web.composer.test_prompt_guard_wiring",)

_USER = "Inspect the neutral graph value."


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["composer", "advisor"])
@pytest.mark.parametrize("oversized", [False, True])
async def test_each_complete_value_use_is_freshly_inspected_and_audited(invocation, tool_context, monkeypatch, role, oversized):
    engine, sessions, owner, coordinator = invocation
    operation = owner.required_work.coordinator.authority.context
    client = _Client(PromptGuardDecision("allow", "clean"))
    service = _guarded_service(
        invocation,
        monkeypatch,
        composer_client=client,
        advisor_client=client,
        # Admit the complete fixture at the separate caller budget so this
        # negative control reaches the unchanged configured detector limit.
        settings=_make_settings(composer_prompt_guard_mode="required", composer_advisor_max_prompt_tokens=12_000),
    )
    context = replace(
        tool_context,
        session_id=operation.fence.session_id,
        user_id="alice",
        session_operation_context=operation,
        session_operation_authority=sessions.session_operation_authority,
    )
    complete = "x" * (25_000 if oversized else 3_000)
    graph = replace(_empty_state(), metadata=PipelineMetadata(description=complete))
    view = build_composition_content_view(
        graph, content_scope=ComposerContentScope(UUID(context.session_id), composition_content_hash(graph))
    )
    reference = view.manifest.values[0].reference_id
    inspected_messages = []
    actual_sdk = []
    make_request = prompt_guard._request

    def inspect_exact_request(**kwargs):
        result = make_request(**kwargs)
        inspected_messages.append(deep_thaw(kwargs["messages"]))
        return result

    async def completion(**kwargs):
        # Existing guard audit commits before entering the provider boundary.
        assert len(_checks(engine)) == len(actual_sdk) + 1
        actual_sdk.append(kwargs)
        return litellm.ModelResponse(
            model=kwargs["model"],
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            choices=[{"message": {"role": "assistant", "content": "The neutral graph value is retained."}}],
        )

    monkeypatch.setattr(prompt_guard, "_request", inspect_exact_request)
    monkeypatch.setattr(litellm, "acompletion", completion)
    gateway = provider_gateway.ProviderGateway(
        model="test/model", settings=_make_settings(composer_prompt_guard_mode="required"), endpoint_base_url=None, endpoint_api_key=None
    )
    recorder = BufferingRecorder()
    for _ in range(1 if oversized else 2):
        result = _dispatch(reference, graph, context)
        assert result.success
        materialized = serialize_admitted_discovery_result(admit_discovery_result("get_complete_pipeline_value", result))
        assert complete in materialized
        with prompt_guard_user_scope(_USER):
            if role == "composer":
                call = gateway._call_llm_with_audit(
                    [{"role": "user", "content": materialized}],
                    [],
                    timeout=10,
                    recorder=recorder,
                    provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
                )
            else:
                call = service._advisor_checkpoint._call_advisor_with_audit(
                    {
                        "trigger": "proactive_security_safety",
                        "problem_summary": "Inspect the neutral graph value.",
                        "recent_errors": [],
                        "attempted_actions": [],
                        "schema_excerpt": materialized,
                    },
                    recorder=recorder,
                    provider_owner=owner,
                )
            if oversized:
                with pytest.raises(PromptGuardRefused):
                    await call
            else:
                await call
    checks = _checks(engine)
    if oversized:
        assert actual_sdk == [] and recorder.llm_calls == () and client.requests == []
        assert len(checks) == 1 and checks[0]["reason"] == "unsupported_input"
    else:
        assert len(actual_sdk) == len(client.requests) == len(checks) == len(recorder.llm_calls) == len(inspected_messages) == 2
        assert client.requests[0] is not client.requests[1]
        for sdk, inspected, captured, check, request in zip(
            actual_sdk, inspected_messages, recorder.llm_calls, checks, client.requests, strict=True
        ):
            assert sdk["messages"] == inspected
            assert captured.messages_hash == stable_hash(sdk["messages"])
            assert check["request_hash"] == request.request_hash and check["role"] == role
            assert complete in str(sdk["messages"])
    coordinator.assert_completed()
