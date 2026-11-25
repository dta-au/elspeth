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
from sqlalchemy import Engine, MetaData, select

from elspeth.contracts.credential_material import find_credential_material
from elspeth.web.composer.protocol import ComposerPluginCrashError
from elspeth.web.composer.provider_gateway import _admit_composer_llm_completion
from elspeth.web.composer.service import ComposerServiceImpl
from elspeth.web.credential_guard import MODEL_OUTPUT_CREDENTIAL_REFUSAL_HEADLINE, CredentialMaterialRefused
from elspeth.web.sessions.models import chat_messages_table
from elspeth.web.sessions.routes._helpers import _llm_calls_from_exception


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


# The gateway and the tool batch scan a model response for recognised
# credentials before any call dispatches. The test scripts the gateway call and
# removes the batch's scan to reach the dispatcher's own scan with real
# arguments: the defence-in-depth case it exists for.
_CANARY = "sk-" + "c" * 24
_KEPT_NAME = "Kept before the refusal"


def _two_call_batch() -> Any:
    calls = [
        SimpleNamespace(id="kept", function=SimpleNamespace(name="set_metadata", arguments=json.dumps({"patch": {"name": _KEPT_NAME}}))),
        SimpleNamespace(
            id="refused",
            function=SimpleNamespace(name="set_metadata", arguments=json.dumps({"patch": {"name": f"Refused {_CANARY}"}})),
        ),
    ]
    return _admit_composer_llm_completion(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=calls))])
    )


def _session_rows_containing(engine: Engine, needle: str) -> list[str]:
    """Every table and column of the session store whose stored text holds ``needle``."""
    metadata = MetaData()
    metadata.reflect(bind=engine)
    hits: list[str] = []
    with engine.connect() as conn:
        for table in metadata.sorted_tables:
            for row in conn.execute(select(table)).mappings():
                hits.extend(f"{table.name}.{column}" for column, value in row.items() if value is not None and needle in str(value))
    return hits


def _tool_row_contents(engine: Engine, tool_call_id: str) -> list[dict[str, Any]]:
    """The stored content of every tool-result row for ``tool_call_id``."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(chat_messages_table.c.content).where(
                chat_messages_table.c.role == "tool", chat_messages_table.c.tool_call_id == tool_call_id
            )
        ).all()
    return [json.loads(content) for (content,) in rows]


@pytest.mark.asyncio
async def test_a_dispatch_refusal_keeps_the_batch_evidence_and_stores_no_refused_value(
    composer_service_with_real_sessions: ComposerServiceImpl,
    result_session_id: str,
) -> None:
    responses = [_two_call_batch(), _text_response()]

    async def _fake_llm(_messages: Any, _tools: Any, **_kwargs: Any) -> Any:
        return responses.pop(0)

    driver: Any = composer_service_with_real_sessions
    engine = composer_service_with_real_sessions._sessions_service._engine
    # The batch's wire scan refuses a recognised credential before any call
    # dispatches; removing that first line reaches the dispatcher's own scan.
    with (
        patch("elspeth.web.composer.tool_batch.require_no_credential_material_in_tool_wire", lambda *_a, **_k: None),
        pytest.raises(CredentialMaterialRefused) as caught,
    ):
        await driver._run_one_turn_for_test(llm=_fake_llm, session_id=result_session_id, current_state_id=None)

    # The dispatcher's own scan refused call 2.
    assert caught.value.surface == "composer_tool_arguments"
    # The refusal carries this request's LLM call, as the wire scan's refusal does.
    assert len(_llm_calls_from_exception(caught.value)) == 1
    # Call 1 ran: its state is durable (and this is the sweep's positive control).
    assert "composition_states.metadata_" in _session_rows_containing(engine, _KEPT_NAME)
    # The refused value is in no table and no column.
    assert _session_rows_containing(engine, _CANARY) == []
    # Each call's stored record says what happened to it.
    assert [row["success"] for row in _tool_row_contents(engine, "kept")] == [True]
    assert [(row["_redaction_status"], row["error_category"]) for row in _tool_row_contents(engine, "refused")] == [
        ("arg_error", "credential_material")
    ]
