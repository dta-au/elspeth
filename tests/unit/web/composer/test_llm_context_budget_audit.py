"""Context-budget decision record on composer LLM audit rows.

``ComposerContextBudgetRecord`` lives in ``contracts/`` and parses the shed-key
forms (``history:<int>``, ``tool:<tool_call_id>``, ``hatch:<int>``) itself so
the contract layer never imports from ``web/``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from elspeth.contracts.composer_llm_audit import (
    ComposerContextBudgetRecord,
    ComposerLLMCall,
    ComposerLLMCallStatus,
)
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record

_HASH = "a" * 64


def _record(shed_keys: tuple[str, ...] = ()) -> ComposerContextBudgetRecord:
    return ComposerContextBudgetRecord.from_shed_keys(
        policy_hash=_HASH,
        bytes_per_token=3.0,
        estimated_tokens_before=900,
        estimated_tokens_after=500,
        request_wire_bytes=1500,
        shed_keys=shed_keys,
    )


def _call(context_budget: ComposerContextBudgetRecord | None) -> ComposerLLMCall:
    return build_llm_call_record(
        model_requested="openrouter/deepseek/deepseek-v4.1-flash",
        messages=[{"role": "system", "content": "skill"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=None,
        context_budget=context_budget,
    )


def _public(call: ComposerLLMCall) -> dict[str, Any]:
    return cast("dict[str, Any]", llm_call_audit_envelope(call)["call"])


class TestFromShedKeys:
    def test_keys_are_partitioned_and_indices_sorted(self) -> None:
        record = _record(("tool:b", "history:3", "hatch:1", "history:1"))

        assert record.shed_history_indices == (1, 3)
        assert record.stubbed_tool_call_ids == ("b",)
        assert record.shed_hatch_event_indices == (1,)
        assert record.bytes_per_token == "3.0"

    def test_tool_ids_keep_request_order_and_may_contain_colons(self) -> None:
        record = _record(("tool:call:z", "tool:a"))

        assert record.stubbed_tool_call_ids == ("call:z", "a")

    def test_unknown_prefix_is_refused(self) -> None:
        with pytest.raises(ValueError, match="shed key"):
            _record(("system:0",))

    def test_key_without_separator_is_refused(self) -> None:
        with pytest.raises(ValueError, match="shed key"):
            _record(("history",))

    def test_non_integer_index_is_refused(self) -> None:
        with pytest.raises(ValueError, match="shed key"):
            _record(("history:x",))


class TestClosedShape:
    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("policy_hash", 1),
            ("bytes_per_token", 3.0),
            ("estimated_tokens_before", True),
            ("estimated_tokens_after", "5"),
            ("request_wire_bytes", 1.5),
            ("request_wire_bytes", True),
            ("shed_history_indices", [1]),
            ("stubbed_tool_call_ids", ["a"]),
            ("shed_hatch_event_indices", (True,)),
        ],
    )
    def test_malformed_field_is_refused(self, field_name: str, value: Any) -> None:
        values: dict[str, Any] = {
            "policy_hash": _HASH,
            "bytes_per_token": "3.0",
            "estimated_tokens_before": 1,
            "estimated_tokens_after": 1,
            "request_wire_bytes": 3,
            "shed_history_indices": (),
            "stubbed_tool_call_ids": (),
            "shed_hatch_event_indices": (),
        }
        values[field_name] = value

        with pytest.raises(TypeError, match=field_name):
            ComposerContextBudgetRecord(**values)

    def test_call_refuses_an_unowned_record(self) -> None:
        unowned: Any = _record().to_dict()

        with pytest.raises(TypeError, match="context_budget"):
            _call(unowned)


class TestPublicEnvelope:
    def test_record_is_exposed_with_empty_lists_when_nothing_was_shed(self) -> None:
        call = _call(_record())

        payload = _public(call)

        assert payload["context_budget"] == {
            "policy_hash": _HASH,
            "bytes_per_token": "3.0",
            "estimated_tokens_before": 900,
            "estimated_tokens_after": 500,
            "request_wire_bytes": 1500,
            "shed_history_indices": [],
            "stubbed_tool_call_ids": [],
            "shed_hatch_event_indices": [],
        }

    def test_shed_decisions_are_exposed(self) -> None:
        payload = _public(_call(_record(("history:2", "tool:t1", "hatch:0"))))

        budget = payload["context_budget"]
        assert budget["shed_history_indices"] == [2]
        assert budget["stubbed_tool_call_ids"] == ["t1"]
        assert budget["shed_hatch_event_indices"] == [0]

    def test_call_without_a_record_exposes_none(self) -> None:
        payload = _public(_call(None))

        assert "context_budget" in payload
        assert payload["context_budget"] is None
