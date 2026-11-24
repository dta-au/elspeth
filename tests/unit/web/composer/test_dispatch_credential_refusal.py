"""A credential refusal at tool dispatch leaves the compose loop as a credential refusal.

The dispatch-time argument scan refuses the model's tool arguments. The loop's
plugin-crash wrap must not turn that into a ComposerPluginCrashError: the turn
reports it the way it reports the wire scan's refusal, with the model-output
wording.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.contracts.credential_material import find_credential_material
from elspeth.web.composer.protocol import ComposerPluginCrashError
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.credential_guard import MODEL_OUTPUT_CREDENTIAL_REFUSAL_HEADLINE, CredentialMaterialRefused


def _tool_call_response() -> Any:
    call = SimpleNamespace(id="call_1", function=SimpleNamespace(name="set_metadata", arguments=json.dumps({"patch": {"name": "x"}})))
    return _admit_composer_llm_completion(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))])
    )


def _text_response() -> Any:
    return _admit_composer_llm_completion(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Done.", tool_calls=None))])
    )


def _dispatch_refusal() -> CredentialMaterialRefused:
    finding = find_credential_material({"value": "sk-" + "d" * 24}, purpose="admission")
    assert finding is not None
    return CredentialMaterialRefused(surface="composer_tool_arguments", finding=finding)


@pytest.mark.asyncio
async def test_a_dispatch_time_credential_refusal_leaves_the_turn_as_a_credential_refusal(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    responses = [_tool_call_response(), _text_response()]
    refusal = _dispatch_refusal()

    async def _fake_llm(_messages: Any, _tools: Any, **_kwargs: Any) -> Any:
        return responses.pop(0)

    driver: Any = composer_service_with_real_sessions
    with patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=refusal), pytest.raises(CredentialMaterialRefused) as caught:
        await driver._run_one_turn_for_test(llm=_fake_llm, session_id=result_session_id, current_state_id=None)
    assert caught.value is refusal
    assert not isinstance(caught.value, ComposerPluginCrashError)
    # The model wrote the arguments, so the turn reports the model-output cause.
    assert caught.value.from_model_output
    assert caught.value.progress_headline == MODEL_OUTPUT_CREDENTIAL_REFUSAL_HEADLINE
