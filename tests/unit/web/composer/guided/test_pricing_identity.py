"""Guided provider routing stays independent of operator billing identity."""

from typing import Any

import pytest
from litellm.types.utils import ModelResponse

from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.guided import chat_solver
from elspeth.web.composer.guided.chat_solver import DeferredIntentManagementChatRequest
from elspeth.web.composer.guided.protocol import GuidedStep
from elspeth.web.sessions import _guided_step_chat


@pytest.mark.asyncio
@pytest.mark.parametrize("solver", ["advisory", "source", "sink", "management"])
async def test_guided_solver_routes_alias_and_prices_catalog_model(monkeypatch: pytest.MonkeyPatch, solver: str) -> None:
    requests: list[dict[str, Any]] = []

    async def complete(**kwargs: Any) -> ModelResponse:
        requests.append(kwargs)
        return ModelResponse(
            model="gpt-4o-2024-08-06",
            choices=[{"message": {"role": "assistant", "content": "Please describe the fields you need."}}],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    monkeypatch.setattr(chat_solver, "_litellm_acompletion", complete)
    recorder = BufferingRecorder()
    common: dict[str, Any] = {
        "model": "openai/operator-datazone",
        "pricing_model": "openai/gpt-4o-2024-08-06",
        "user_message": "Help me choose the fields",
        "temperature": None,
        "seed": None,
        "timeout_seconds": 5,
    }
    session_context = {"site": "pricing_test", "session_id": "session", "user_id": "user"}
    if solver == "advisory":
        await _guided_step_chat.solve_step_chat_with_auto_drop(
            **common, **session_context, step=GuidedStep.STEP_1_SOURCE, recorder=recorder
        )
    elif solver == "source":
        await _guided_step_chat.resolve_step_1_source_chat_with_auto_drop(
            **common, **session_context, plugin_hint=None, current_source=None, available_source_plugins=("csv",), recorder=recorder
        )
    elif solver == "sink":
        await _guided_step_chat.resolve_step_2_sink_chat_with_auto_drop(**common, **session_context, current_sink=None, recorder=recorder)
    else:
        await _guided_step_chat.resolve_deferred_intent_management_chat_with_auto_drop(
            **session_context,
            request=DeferredIntentManagementChatRequest(**common, step=GuidedStep.STEP_3_TRANSFORMS, context_block="safe context"),
            recorder=recorder,
        )

    assert len(requests) == 1
    assert requests[0]["model"] == "openai/operator-datazone"
    assert "pricing_model" not in requests[0]
    assert len(recorder.llm_calls) == 1
    call = recorder.llm_calls[0]
    assert call.model_requested == "openai/operator-datazone"
    assert call.provider_cost_source == "litellm.cost_per_token"
    assert call.provider_cost is not None and call.provider_cost > 0
