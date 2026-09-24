"""One neutral recovery call for a rootless build request that ran no tools."""

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from elspeth.web.composer import service as composer_service_module
from tests.unit.web.composer._helpers import _stub_advisor_end_gate_clean as _stub_advisor_end_gate_clean
from tests.unit.web.composer.test_service import (
    ValidationResult,
    _composer_service_with_session,
    _empty_state,
    _make_llm_response,
    _make_settings,
    _mock_catalog,
)

_REQUEST = (
    "I've got a handful of customer complaints - make up 6 realistic ones in a csv "
    "with an id and a complaint field, we'll be triaging them by urgency"
)
_FALSE_COMPLETION = "The six complaints are bound as the pipeline CSV source. Please confirm the urgency rubric."


@pytest.mark.asyncio
async def test_rootless_build_prose_gets_one_neutral_retry_then_tool_execution() -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    tool_reply = _make_llm_response(
        tool_calls=[
            {
                "id": "source-call",
                "name": "set_source",
                "arguments": {
                    "plugin": "csv",
                    "on_success": "t1",
                    "options": {"path": f"/data/blobs/{sid}/data.csv", "schema": {"mode": "observed"}},
                    "on_validation_failure": "quarantine",
                },
            }
        ]
    )
    with (
        patch.object(service, "_call_llm", new_callable=AsyncMock) as completion,
        patch.object(service, "_runtime_preflight", return_value=ValidationResult(is_valid=True, checks=[], errors=[])),
    ):
        completion.side_effect = [
            _make_llm_response(content=_FALSE_COMPLETION),
            tool_reply,
            _make_llm_response(content="The source is now configured."),
        ]
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert completion.call_count == 3
    assert result.repair_turns_used == 1
    assert result.state.sources["source"].plugin == "csv"
    assert any(call.tool_name == "set_source" and call.status.value == "success" for call in result.tool_invocations)


@pytest.mark.asyncio
async def test_repeated_rootless_prose_ends_after_one_neutral_retry() -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    observed = []

    async def respond(*args: Any, **kwargs: Any) -> Any:
        observed.append(deepcopy(args[0]))
        return _make_llm_response(content=_FALSE_COMPLETION)

    with patch.object(service, "_call_llm", side_effect=respond) as completion:
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert completion.call_count == 2
    assert result.repair_turns_used == 1
    assert not result.tool_invocations
    assert not result.state.sources
    assert "pipeline is still empty" in result.message
    repair = observed[1][-1]["content"]
    assert "No tool has run this turn" in repair
    assert "authorized work" in repair
    assert "revoked construction" in repair
    assert any(message["role"] == "assistant" and message["content"] == _FALSE_COMPLETION for message in observed[1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message", ["Do not build anything; explain how CSV parsing works.", "Are you able to run pipelines yourself?"]
)
async def test_neutral_retry_keeps_explanation_and_revocation_without_mutation(user_message: str) -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    with patch.object(
        service, "_call_llm", return_value=_make_llm_response(content="I can explain that without changing anything.")
    ) as completion:
        result = await service.compose(user_message, [], _empty_state(), session_id=sid)
    assert completion.call_count == 2
    assert result.repair_turns_used == 1
    assert not result.tool_invocations
    assert not result.state.sources


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_message", "repair_budget"),
    [("Hello, what can you do?", 2), (_REQUEST, 0)],
    ids=["greeting", "disabled-repairs"],
)
async def test_ineligible_request_does_not_add_a_call(user_message: str, repair_budget: int, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(composer_service_module, "_MAX_REPAIR_TURNS", repair_budget)
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    with patch.object(service, "_call_llm", return_value=_make_llm_response(content="Here is my response.")) as completion:
        result = await service.compose(user_message, [], _empty_state(), session_id=sid)
    assert completion.call_count == 1
    assert result.repair_turns_used == 0
    assert not result.tool_invocations


@pytest.mark.asyncio
async def test_expired_deadline_does_not_start_rootless_retry() -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    terminate = service._try_terminate_no_tools

    async def terminate_after_deadline(**kwargs: Any) -> Any:
        kwargs["deadline"] = 0.0
        return await terminate(**kwargs)

    with (
        patch.object(service, "_call_llm", return_value=_make_llm_response(content=_FALSE_COMPLETION)) as completion,
        patch.object(service, "_try_terminate_no_tools", side_effect=terminate_after_deadline),
    ):
        result = await service.compose(_REQUEST, [], _empty_state(), session_id=sid)
    assert completion.call_count == 1
    assert result.repair_turns_used == 0
