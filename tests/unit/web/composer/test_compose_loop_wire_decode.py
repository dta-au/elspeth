"""Compose-loop decode of provider tool arguments through ``wire_projection`` (S1 T6).

Plan: ``docs/plans/2026-09-23-composer-strict-tool-contracts-s1.md`` T6.

``run_tool_batch`` decodes every call whose tool name was in the list sent on
that call (``_WIRE_TOOL_DEFS[dialect]``) with ``decode_wire_arguments`` before
the S gate. On ``openai_strict`` a ``null`` at a promoted position becomes an
omitted key; on ``none`` decode only unwraps the set_pipeline envelope, which
is exactly today's behaviour. Each call's wire facts (``strict_sent`` D16,
``wire_conformant`` C23) are recorded on the P4 assistant ``tool_calls`` entry
(D1) and on the invocation built from the same ``DispatchAudit`` (D21).

Until T8 resolves the dialect from settings, every service is constructed on
``none``; these tests reach ``openai_strict`` by setting
``service._planner_dialect`` on the constructed service, which is the single
value both the sent tool list and decode read.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_audit import ComposerToolStatus, ToolArgumentErrorCategory
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer.protocol import ComposerPluginCrashError, ToolArgumentError
from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
from elspeth.web.composer.state import ValidationSummary
from elspeth.web.composer.tools import ToolResult
from elspeth.web.composer.tools import execute_tool as _real_execute_tool
from elspeth.web.sessions.models import chat_messages_table
from tests.unit.web.composer._helpers import (
    FakeChoice,
    FakeFunction,
    FakeLLMResponse,
    FakeMessage,
    FakeToolCall,
    _clean_advisor_checkpoint,
    _composer_service_with_session,
    _empty_state,
    _make_settings,
    _mock_catalog,
)

_STRICT = ToolContractDialect.OPENAI_STRICT
_NONE = ToolContractDialect.NONE


@pytest.fixture(autouse=True)
def _advisor_end_gate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The END advisor gate is not under test; make it a CLEAN no-op."""
    monkeypatch.setattr(ComposerServiceImpl, "_run_advisor_checkpoint", _clean_advisor_checkpoint, raising=True)


@pytest.fixture(autouse=True)
def _composer_available(monkeypatch: pytest.MonkeyPatch) -> None:
    def _available(self: ComposerServiceImpl) -> ComposerAvailability:
        return ComposerAvailability(available=True, model=self._model, provider="test")

    monkeypatch.setattr(ComposerServiceImpl, "_compute_availability", _available)


def _raw_response(*calls: tuple[str, str, str]) -> FakeLLMResponse:
    """One assistant turn whose tool calls carry already-encoded argument strings."""
    return FakeLLMResponse(
        choices=[
            FakeChoice(
                message=FakeMessage(
                    content=None,
                    tool_calls=[FakeToolCall(id=call_id, function=FakeFunction(name=name, arguments=raw)) for call_id, name, raw in calls],
                )
            )
        ]
    )


def _text_response(content: str = "Done.") -> FakeLLMResponse:
    return FakeLLMResponse(choices=[FakeChoice(message=FakeMessage(content=content, tool_calls=None))])


def _persisted_assistant_entries(service: ComposerServiceImpl, session_id: str) -> dict[str, dict[str, Any]]:
    """Every persisted assistant ``tool_calls`` entry, keyed by call id."""
    entries: dict[str, dict[str, Any]] = {}
    with service._require_sessions_service()._engine.connect() as conn:
        rows = conn.execute(
            select(chat_messages_table.c.tool_calls)
            .where(chat_messages_table.c.session_id == session_id, chat_messages_table.c.role == "assistant")
            .order_by(chat_messages_table.c.sequence_no)
        ).all()
    for row in rows:
        if row.tool_calls is None:
            continue
        for entry in row.tool_calls:
            entries[entry["id"]] = entry
    return entries


class _Turn:
    """A driven compose turn: the handler spy, the sent tool lists and the outputs."""

    def __init__(self, dialect: ToolContractDialect, *calls: tuple[str, str, str]) -> None:
        self.dialect = dialect
        self.calls = calls
        self.handler_arguments: dict[str, dict[str, Any]] = {}
        self.sent_tool_lists: list[list[dict[str, Any]]] = []

    async def run(self) -> tuple[ComposerServiceImpl, str, Any]:
        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        service._planner_dialect = self.dialect
        responses = [_raw_response(*self.calls), _text_response()]

        async def _llm(messages: Any, tools: Any) -> FakeLLMResponse:
            self.sent_tool_lists.append(tools)
            return responses.pop(0) if responses else _text_response()

        def _spy(tool_name: str, arguments: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
            self.handler_arguments[tool_name] = json.loads(json.dumps(arguments))
            return _real_execute_tool(tool_name, arguments, *args, **kwargs)

        with patch("elspeth.web.composer.tool_batch.execute_tool", side_effect=_spy):
            result = await service._run_one_turn_for_test(llm=_llm, session_id=session_id, initial_state=_empty_state())
        return service, session_id, result


def _outcome(result: Any, call_id: str) -> Any:
    (outcome,) = [outcome for outcome in result.tool_outcomes if outcome.call.id == call_id]
    return outcome


def _invocation(result: Any, call_id: str) -> Any:
    (invocation,) = [invocation for invocation in result.tool_invocations if invocation.tool_call_id == call_id]
    return invocation


class TestStrictDecode:
    @pytest.mark.asyncio
    async def test_null_at_promoted_positions_is_admitted_as_absent(self) -> None:
        turn = _Turn(_STRICT, ("call_lm", "list_models", json.dumps({"provider": None, "limit": None})))
        service, session_id, result = await turn.run()

        outcome = _outcome(result, "call_lm")
        assert outcome.error_class is None
        assert turn.handler_arguments["list_models"] == {}
        entry = _persisted_assistant_entries(service, session_id)["call_lm"]
        assert entry["strict_sent"] is True
        assert entry["wire_conformant"] is True
        # The list that was sent and the dialect decode used are the same value.
        assert all(tool["function"]["strict"] in (True, False) for tool in turn.sent_tool_lists[0])

    @pytest.mark.asyncio
    async def test_omitted_wire_required_keys_are_admitted_and_classified_nonconformant(self) -> None:
        turn = _Turn(_STRICT, ("call_lm", "list_models", json.dumps({})))
        service, session_id, result = await turn.run()

        assert _outcome(result, "call_lm").error_class is None
        assert turn.handler_arguments["list_models"] == {}
        entry = _persisted_assistant_entries(service, session_id)["call_lm"]
        assert entry["strict_sent"] is True
        assert entry["wire_conformant"] is False

    @pytest.mark.asyncio
    async def test_none_dialect_does_not_strip_and_s_rejects_the_null_as_today(self) -> None:
        turn = _Turn(_NONE, ("call_lm", "list_models", json.dumps({"provider": None})))
        service, session_id, result = await turn.run()

        outcome = _outcome(result, "call_lm")
        assert outcome.error_class == "ToolArgumentError"
        assert outcome.error_category is ToolArgumentErrorCategory.SCHEMA_SHAPE
        entry = _persisted_assistant_entries(service, session_id)["call_lm"]
        assert entry["strict_sent"] is None
        assert entry["wire_conformant"] is False
        assert all("strict" not in tool["function"] for tool in turn.sent_tool_lists[0])

    @pytest.mark.asyncio
    async def test_option_tool_records_an_explicit_strict_false(self) -> None:
        turn = _Turn(_STRICT, ("call_node", "upsert_node", json.dumps({"id": "n1"})))
        service, session_id, result = await turn.run()

        entry = _persisted_assistant_entries(service, session_id)["call_node"]
        assert entry["strict_sent"] is False
        assert _outcome(result, "call_node").strict_sent is False

    @pytest.mark.asyncio
    async def test_unknown_tool_never_reaches_decode(self) -> None:
        turn = _Turn(_STRICT, ("call_unknown", "not_a_tool", json.dumps({"x": None})))
        service, session_id, result = await turn.run()

        outcome = _outcome(result, "call_unknown")
        assert (outcome.strict_sent, outcome.wire_conformant) == (None, None)
        entry = _persisted_assistant_entries(service, session_id)["call_unknown"]
        assert (entry["strict_sent"], entry["wire_conformant"]) == (None, None)


# (branch id, tool name, raw arguments, expected facts on openai_strict)
_BRANCHES: tuple[Any, ...] = (
    pytest.param(
        "call_success",
        "set_metadata",
        json.dumps({"patch": {"name": "Step", "description": "A description"}}),
        (True, True),
        id="success",
    ),
    pytest.param("call_non_object", "list_models", json.dumps([1]), (True, False), id="non-object"),
    pytest.param("call_envelope", "set_pipeline", json.dumps({"x": 1}), (False, False), id="envelope-rejection"),
    pytest.param("call_json", "list_models", "{not json", (True, None), id="json-failure"),
    pytest.param("call_unknown", "not_a_tool", json.dumps({}), (None, None), id="unknown-tool"),
)


class TestWireFactParity:
    """D21: each invocation's facts equal the P4 row's (from ``_ToolOutcome``) for the same call."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dialect", [_STRICT, _NONE], ids=["openai_strict", "none"])
    @pytest.mark.parametrize(("call_id", "tool_name", "raw", "strict_facts"), _BRANCHES)
    async def test_invocation_and_p4_row_carry_the_same_facts(
        self,
        dialect: ToolContractDialect,
        call_id: str,
        tool_name: str,
        raw: str,
        strict_facts: tuple[bool | None, bool | None],
    ) -> None:
        turn = _Turn(dialect, (call_id, tool_name, raw))
        service, session_id, result = await turn.run()

        expected = strict_facts if dialect is _STRICT else (None, strict_facts[1])
        outcome = _outcome(result, call_id)
        invocation = _invocation(result, call_id)
        entry = _persisted_assistant_entries(service, session_id)[call_id]

        assert (outcome.strict_sent, outcome.wire_conformant) == expected
        assert (invocation.strict_sent, invocation.wire_conformant) == expected
        assert (entry["strict_sent"], entry["wire_conformant"]) == expected
        expected_status = ComposerToolStatus.SUCCESS if call_id in {"call_success", "call_unknown"} else ComposerToolStatus.ARG_ERROR
        assert invocation.status is expected_status

    @pytest.mark.asyncio
    async def test_envelope_rejection_keeps_todays_record(self) -> None:
        turn = _Turn(_STRICT, ("call_envelope", "set_pipeline", json.dumps({"pipeline": None})))
        _service, _session_id, result = await turn.run()

        invocation = _invocation(result, "call_envelope")
        assert invocation.status is ComposerToolStatus.ARG_ERROR
        assert invocation.error_class == "ToolArgumentError"
        assert invocation.error_category is ToolArgumentErrorCategory.WIRE_ENVELOPE
        assert invocation.error_message == "Tool 'set_pipeline' arguments must contain exactly one 'pipeline' object field."
        assert json.loads(invocation.arguments_canonical) == {
            "_redaction_status": "invalid_tool_arguments",
            "error_class": "ToolArgumentError",
        }
        assert "set_pipeline" not in turn.handler_arguments


class TestTranscriptBytes:
    """``_replace_llm_tool_call_arguments`` with an explicit dialect and semantic flag."""

    @staticmethod
    def _rewrite(name: str, arguments: dict[str, Any], *, dialect: ToolContractDialect, semantic: bool) -> str:
        from elspeth.web.composer.tool_batch import _replace_llm_tool_call_arguments

        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": "{}"}}],
            }
        ]
        _replace_llm_tool_call_arguments(messages, tool_call_id="call_1", arguments=arguments, dialect=dialect, semantic=semantic)
        encoded = messages[0]["tool_calls"][0]["function"]["arguments"]
        assert type(encoded) is str
        return encoded

    @pytest.mark.parametrize("dialect", [_STRICT, _NONE], ids=["openai_strict", "none"])
    def test_semantic_and_sentinel_branches_give_identical_set_pipeline_bytes(self, dialect: ToolContractDialect) -> None:
        semantic_arguments = {"source": {"plugin": "csv", "on_success": "rows"}, "nodes": [], "edges": [], "outputs": []}

        semantic = self._rewrite("set_pipeline", semantic_arguments, dialect=dialect, semantic=True)
        sentinel = self._rewrite("set_pipeline", semantic_arguments, dialect=dialect, semantic=False)

        assert semantic == sentinel
        assert json.loads(semantic) == {"pipeline": semantic_arguments}

    @pytest.mark.parametrize("dialect", [_STRICT, _NONE], ids=["openai_strict", "none"])
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("set_pipeline", '{"pipeline":{"_redaction_status":"invalid_tool_arguments","error_class":"ToolArgumentError"}}'),
            ("list_models", '{"_redaction_status":"invalid_tool_arguments","error_class":"ToolArgumentError"}'),
        ],
    )
    def test_sentinel_bytes_equal_todays_output(self, dialect: ToolContractDialect, name: str, expected: str) -> None:
        """Expected strings: the lane's ``dispatch_probe.log`` item 1, measured on the base."""
        sentinel = {"_redaction_status": "invalid_tool_arguments", "error_class": "ToolArgumentError"}

        assert self._rewrite(name, sentinel, dialect=dialect, semantic=False) == expected


class TestPersistedP4RowCarriesTheFacts:
    """A copy of the SUCCESS -> ARG_ERROR -> PLUGIN_CRASH canary, reading the P4 assistant rows."""

    @pytest.mark.asyncio
    async def test_success_arg_error_and_plugin_crash_rows_carry_both_keys(self) -> None:
        service, session_id = _composer_service_with_session(catalog=_mock_catalog(), settings=_make_settings())
        service._planner_dialect = _STRICT
        state = _empty_state()
        turns = [
            _raw_response((call_id, "set_metadata", json.dumps({"patch": {"name": f"Step {index}", "description": None}})))
            for index, call_id in enumerate(("call_success", "call_arg_error", "call_plugin_crash"))
        ]
        success_result = ToolResult(
            success=True,
            updated_state=replace(state, version=2),
            validation=ValidationSummary(is_valid=True, errors=(), warnings=(), suggestions=(), semantic_contracts=()),
            affected_nodes=(),
        )
        with (
            patch.object(service, "_call_llm", new_callable=AsyncMock) as mock_llm,
            patch(
                "elspeth.web.composer.tool_batch.execute_tool",
                side_effect=[
                    success_result,
                    ToolArgumentError(argument="patch", expected="a string", actual_type="int"),
                    RuntimeError("synthetic plugin bug"),
                ],
            ),
        ):
            mock_llm.side_effect = turns
            with pytest.raises(ComposerPluginCrashError):
                await service.compose("Drive the sequence", [], state, session_id=session_id)

        entries = _persisted_assistant_entries(service, session_id)
        for call_id in ("call_success", "call_arg_error", "call_plugin_crash"):
            entry = entries[call_id]
            assert set(entry) == {"id", "type", "function", "strict_sent", "wire_conformant"}
            assert entry["strict_sent"] is True
            assert entry["wire_conformant"] is True
