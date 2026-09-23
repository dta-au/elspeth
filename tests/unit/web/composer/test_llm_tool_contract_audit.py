"""The tool-contract dialect and strict-tool count on composer LLM audit rows.

S1 of the strict tool contracts plan
(``docs/plans/2026-09-23-composer-strict-tool-contracts-s1.md`` T5, D11) records,
for every outbound composer LLM call, which dialect its tool list was sent
under and how many tools carried ``strict: true``. Both are derived inside
``build_llm_call_record`` from the exact ``tools`` list it receives, the same
bytes ``tools_spec_hash`` covers, so the record cannot disagree with what was
sent:

- every tool's ``function`` carries a ``strict`` key holding an exact ``bool``
  → ``openai_strict`` and the number of ``True`` stamps;
- no tool carries the key → ``none`` and ``0``;
- anything else (a mix, or a non-bool stamp) → ``AuditIntegrityError``;
- no tools → ``None`` / ``None``.

``_LLM_CALL_PUBLIC_AUDIT_FIELDS`` is a closed whitelist, so the persisted
projection is pinned end to end, with a mutation control per key.
"""

from __future__ import annotations

import time
from dataclasses import fields
from datetime import UTC, datetime
from typing import Any

import pytest

import elspeth.web.composer.audit as composer_audit
from elspeth.contracts.composer_llm_audit import (
    ComposerLLMCall,
    ComposerLLMCallStatus,
    ToolContractDialect,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.tools.wire_projection import stamp_planner_terminal, wire_tool_definitions
from elspeth.web.sessions.guided_audit import prepare_guided_audit_rows

_NEW_FIELDS = ("tool_contract_dialect", "strict_tool_count")


def _tool(name: str, **function_extra: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            **function_extra,
        },
    }


def _record(tools: list[dict[str, Any]] | None) -> ComposerLLMCall:
    return build_llm_call_record(
        model_requested="openrouter/deepseek/deepseek-v4.1-flash",
        messages=[{"role": "user", "content": "hello"}],
        tools=tools,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=None,
    )


def _llm_call(**overrides: Any) -> ComposerLLMCall:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "model_requested": "openrouter/deepseek/deepseek-v4.1-flash",
        "model_returned": "deepseek/deepseek-v4.1-flash",
        "status": ComposerLLMCallStatus.SUCCESS,
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "total_tokens": 2,
        "latency_ms": 5,
        "provider_request_id": "req-1",
        "messages_hash": "a" * 64,
        "tools_spec_hash": None,
        "declared_tool_names": (),
        "started_at": now,
        "finished_at": now,
        "error_class": None,
        "error_message": None,
        "temperature": None,
        "seed": None,
    }
    values.update(overrides)
    return ComposerLLMCall(**values)


def _envelope_call(call: ComposerLLMCall) -> dict[str, object]:
    payload = llm_call_audit_envelope(call)["call"]
    assert isinstance(payload, dict)
    return payload


class TestDeclaredFields:
    @pytest.mark.parametrize("field_name", _NEW_FIELDS)
    def test_llm_call_declares_the_field(self, field_name: str) -> None:
        assert field_name in {field.name for field in fields(ComposerLLMCall)}

    @pytest.mark.parametrize("field_name", _NEW_FIELDS)
    def test_public_audit_whitelist_carries_the_field(self, field_name: str) -> None:
        assert field_name in composer_audit._LLM_CALL_PUBLIC_AUDIT_FIELDS

    def test_both_default_to_none(self) -> None:
        call = _llm_call()

        assert call.tool_contract_dialect is None
        assert call.strict_tool_count is None


class TestContract:
    @pytest.mark.parametrize(
        ("dialect", "count"),
        [
            (ToolContractDialect.OPENAI_STRICT, 32),
            (ToolContractDialect.OPENAI_STRICT, 0),
            (ToolContractDialect.NONE, 0),
            (None, None),
        ],
    )
    def test_accepts_consistent_pairs(self, dialect: ToolContractDialect | None, count: int | None) -> None:
        call = _llm_call(tool_contract_dialect=dialect, strict_tool_count=count)

        assert call.tool_contract_dialect is dialect
        assert call.strict_tool_count == count

    @pytest.mark.parametrize(
        ("dialect", "count"),
        [(ToolContractDialect.OPENAI_STRICT, None), (ToolContractDialect.NONE, None), (None, 0), (None, 3)],
    )
    def test_both_or_neither(self, dialect: ToolContractDialect | None, count: int | None) -> None:
        with pytest.raises(ValueError, match="together"):
            _llm_call(tool_contract_dialect=dialect, strict_tool_count=count)

    def test_negative_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="strict_tool_count"):
            _llm_call(tool_contract_dialect=ToolContractDialect.OPENAI_STRICT, strict_tool_count=-1)

    def test_none_dialect_with_a_strict_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="none"):
            _llm_call(tool_contract_dialect=ToolContractDialect.NONE, strict_tool_count=1)

    @pytest.mark.parametrize("count", [True, 1.0, "1"], ids=["bool", "float", "str"])
    def test_count_must_be_an_exact_int(self, count: object) -> None:
        with pytest.raises(TypeError, match="strict_tool_count"):
            _llm_call(tool_contract_dialect=ToolContractDialect.OPENAI_STRICT, strict_tool_count=count)

    @pytest.mark.parametrize("dialect", ["openai_strict", "none"])
    def test_dialect_must_be_the_owned_enum(self, dialect: str) -> None:
        with pytest.raises(TypeError, match="tool_contract_dialect"):
            _llm_call(tool_contract_dialect=dialect, strict_tool_count=0)


class TestDerivation:
    def test_stamped_loop_list_is_openai_strict_with_the_true_count(self) -> None:
        tools = wire_tool_definitions(ToolContractDialect.OPENAI_STRICT)
        expected = sum(1 for tool in tools if tool["function"]["strict"] is True)

        record = _record(tools)

        assert record.tool_contract_dialect is ToolContractDialect.OPENAI_STRICT
        assert record.strict_tool_count == expected
        assert expected == 32

    def test_unstamped_loop_list_is_none_with_zero(self) -> None:
        record = _record(wire_tool_definitions(ToolContractDialect.NONE))

        assert record.tool_contract_dialect is ToolContractDialect.NONE
        assert record.strict_tool_count == 0

    def test_a_list_stamped_only_false_is_openai_strict_with_zero(self) -> None:
        terminal = stamp_planner_terminal(_tool("emit_pipeline_proposal"), ToolContractDialect.OPENAI_STRICT)

        record = _record([terminal])

        assert record.tool_contract_dialect is ToolContractDialect.OPENAI_STRICT
        assert record.strict_tool_count == 0

    def test_counts_only_true_stamps(self) -> None:
        record = _record([_tool("a", strict=True), _tool("b", strict=False), _tool("c", strict=True)])

        assert record.tool_contract_dialect is ToolContractDialect.OPENAI_STRICT
        assert record.strict_tool_count == 2

    def test_mixed_list_raises(self) -> None:
        with pytest.raises(AuditIntegrityError, match="mixes strict-stamped and unstamped tools"):
            _record([_tool("a", strict=True), _tool("b")])

    @pytest.mark.parametrize("stamp", ["true", 1, None], ids=["str", "int", "null"])
    def test_non_bool_stamp_raises(self, stamp: object) -> None:
        with pytest.raises(AuditIntegrityError, match="mixes strict-stamped and unstamped tools"):
            _record([_tool("a", strict=stamp), _tool("b", strict=True)])

    @pytest.mark.parametrize("tools", [None, []], ids=["none", "empty"])
    def test_no_tools_records_absence(self, tools: list[dict[str, Any]] | None) -> None:
        record = _record(tools)

        assert record.tool_contract_dialect is None
        assert record.strict_tool_count is None


class TestSurvivesToThePersistedProjection:
    def test_public_audit_envelope_exposes_both(self) -> None:
        call_payload = _envelope_call(_record(wire_tool_definitions(ToolContractDialect.OPENAI_STRICT)))

        assert call_payload["tool_contract_dialect"] == "openai_strict"
        assert call_payload["strict_tool_count"] == 32

    def test_to_dict_writes_the_dialect_as_its_string_value(self) -> None:
        raw = _llm_call(tool_contract_dialect=ToolContractDialect.NONE, strict_tool_count=0).to_dict()

        assert type(raw["tool_contract_dialect"]) is str
        assert raw["tool_contract_dialect"] == "none"
        assert raw["strict_tool_count"] == 0

    def test_absence_persists_as_null_not_omitted(self) -> None:
        call_payload = _envelope_call(_llm_call())

        for field_name in _NEW_FIELDS:
            assert field_name in call_payload
            assert call_payload[field_name] is None

    @pytest.mark.parametrize("field_name", _NEW_FIELDS)
    def test_mutation_control_dropping_the_whitelist_entry_loses_the_field(self, monkeypatch: pytest.MonkeyPatch, field_name: str) -> None:
        """The instrument above must go red when the whitelist entry is removed."""
        mutated = tuple(field for field in composer_audit._LLM_CALL_PUBLIC_AUDIT_FIELDS if field != field_name)
        assert len(mutated) == len(composer_audit._LLM_CALL_PUBLIC_AUDIT_FIELDS) - 1
        monkeypatch.setattr(composer_audit, "_LLM_CALL_PUBLIC_AUDIT_FIELDS", mutated)

        call_payload = _envelope_call(_llm_call(tool_contract_dialect=ToolContractDialect.OPENAI_STRICT, strict_tool_count=3))

        assert field_name not in call_payload

    def test_guided_failure_row_preserves_both(self) -> None:
        rows = prepare_guided_audit_rows(
            invocations=(),
            llm_calls=(
                _llm_call(
                    tool_contract_dialect=ToolContractDialect.OPENAI_STRICT,
                    strict_tool_count=19,
                    status=ComposerLLMCallStatus.MALFORMED_RESPONSE,
                    error_class="MalformedResponse",
                    error_message="truncated mid tool call",
                ),
            ),
            chat_turns=(),
        )

        (row,) = rows
        assert row.envelope["call"]["tool_contract_dialect"] == "openai_strict"
        assert row.envelope["call"]["strict_tool_count"] == 19
