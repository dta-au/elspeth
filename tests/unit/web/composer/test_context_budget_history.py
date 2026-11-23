"""Tests for the stored-call reader behind the composer context budget."""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from elspeth.contracts.composer_llm_audit import ComposerContextBudgetRecord, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.context_budget_apply import request_wire_bytes
from elspeth.web.composer.context_budget_history import ContextBudgetHistory
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.sessions.protocol import ChatMessageRecord

_SESSION = uuid.uuid4()
_NO_KEY: Any = object()


_COMPOSER = "openrouter/openai/gpt-5.5"
_ADVISOR = "anthropic/claude-sonnet-4-6"


def _budget(shed: list[int] | Any, wire_bytes: int | None) -> dict[str, Any]:
    budget = {
        "policy_hash": "h",
        "bytes_per_token": "3.0",
        "estimated_tokens_before": 10,
        "estimated_tokens_after": 5,
        "shed_history_indices": shed,
        "stubbed_tool_call_ids": [],
        "shed_hatch_event_indices": [],
    }
    if wire_bytes is not None:
        budget["request_wire_bytes"] = wire_bytes
    return budget


def _call(
    *,
    status: str = "success",
    total_bytes: int | None = 3000,
    prompt_tokens: int | None = 1000,
    shed: list[int] | None | Any = (),
    model: str = _COMPOSER,
) -> dict[str, Any]:
    """A stored call. By default a budgeted composer call that shed nothing.

    ``total_bytes`` is the wire size the call's context-budget record stores
    (``request_wire_bytes``); ``None`` omits the field, as a pre-release
    build's record does. ``shed=None`` stores a null ``context_budget`` (an
    advisor or auto-title call); ``shed=_NO_KEY`` omits the key (a call from
    before context budgeting). The content split is stored on every call and
    is ignored by the shed-state reader.
    """
    call: dict[str, Any] = {
        "model_requested": model,
        "status": status,
        "prompt_tokens": prompt_tokens,
        "request_byte_split": {"message_bytes": [777], "tools_bytes": None, "total_bytes": 777},
    }
    if shed is not _NO_KEY:
        call["context_budget"] = None if shed is None else _budget(list(shed), total_bytes)
    return call


def _audit_row(seq: int, call: Any) -> ChatMessageRecord:
    return ChatMessageRecord(
        id=uuid.uuid4(),
        session_id=_SESSION,
        role="audit",
        content="",
        created_at=datetime.now(UTC),
        writer_principal="compose_loop",
        sequence_no=seq,
        tool_calls=[{"_kind": "llm_call_audit", "call": call}],
    )


def _user_row(seq: int) -> ChatMessageRecord:
    return ChatMessageRecord(
        id=uuid.uuid4(),
        session_id=_SESSION,
        role="user",
        content="hello",
        created_at=datetime.now(UTC),
        writer_principal="route_user_message",
        sequence_no=seq,
    )


def test_empty_has_no_shed_set() -> None:
    empty = ContextBudgetHistory.empty()
    assert empty.previously_shed_history == frozenset()


def test_records_without_audit_rows_give_empty() -> None:
    assert ContextBudgetHistory.from_records([_user_row(1)]) == ContextBudgetHistory.empty()
    assert ContextBudgetHistory.from_records([]) == ContextBudgetHistory.empty()


def test_a_budgeted_record_without_wire_bytes_still_supplies_shed_state() -> None:
    history = ContextBudgetHistory.from_records([_audit_row(1, _call(total_bytes=None, shed=[2]))])
    assert history.previously_shed_history == frozenset({"history:2"})


@pytest.mark.parametrize("unbudgeted", [None, _NO_KEY], ids=["null-context-budget", "no-context-budget-key"])
def test_a_call_that_was_not_budgeted_never_changes_shed_state(unbudgeted: Any) -> None:
    """An advisor or auto-title call (null budget) or a call from before budgeting (no key) is another kind of request."""
    composer_call = _call(shed=[3])
    advisor_call = _call(shed=unbudgeted, model=_ADVISOR)
    history = ContextBudgetHistory.from_records([_audit_row(1, composer_call), _audit_row(2, advisor_call)])
    assert history.previously_shed_history == frozenset({"history:3"})
    alone = ContextBudgetHistory.from_records([_audit_row(1, advisor_call)])
    assert alone == ContextBudgetHistory.empty()


def test_shed_set_comes_from_newest_call_with_non_null_context_budget() -> None:
    rows = [
        _audit_row(1, _call(shed=[0])),
        _audit_row(2, _call(shed=[0, 2, 5])),
        _audit_row(3, _call(status="error", shed=None)),
        _audit_row(4, _call(status="error", total_bytes=None, shed=_NO_KEY)),
    ]
    history = ContextBudgetHistory.from_records(rows)
    # Rows 3/4 carry a null or absent budget; the newest non-null one is row 2.
    assert history.previously_shed_history == frozenset({"history:0", "history:2", "history:5"})


def test_newest_non_null_budget_is_a_failed_call() -> None:
    rows = [_audit_row(1, _call(shed=[0])), _audit_row(2, _call(status="error", total_bytes=None, shed=[1]))]
    history = ContextBudgetHistory.from_records(rows)
    assert history.previously_shed_history == frozenset({"history:1"})


def test_absent_context_budget_key_means_no_shedding_recorded() -> None:
    """A session written before context budgeting has no shed state."""
    history = ContextBudgetHistory.from_records([_audit_row(1, _call(shed=_NO_KEY))])
    assert history == ContextBudgetHistory.empty()


@pytest.mark.parametrize(
    "bad_call",
    [
        {**_call(), "context_budget": _budget([], "3")},
        {**_call(), "context_budget": _budget([], -1)},
        {**_call(), "context_budget": _budget([], True)},
        {**_call(), "prompt_tokens": "1"},
        {**_call(), "status": 7},
        {**_call(), "model_requested": ""},
        {**_call(), "model_requested": 7},
        {**_call(), "context_budget": "x"},
        {**_call(), "context_budget": {"shed_history_indices": "0"}},
        {**_call(), "context_budget": {"shed_history_indices": [True]}},
        {**_call(), "context_budget": {}},
        # A budgeted call always stores these keys; their absence is corruption.
        {name: value for name, value in _call().items() if name != "status"},
        {name: value for name, value in _call().items() if name != "prompt_tokens"},
        {name: value for name, value in _call().items() if name != "model_requested"},
    ],
)
def test_malformed_budgeted_call_raises(bad_call: dict[str, Any]) -> None:
    with pytest.raises(AuditIntegrityError):
        ContextBudgetHistory.from_records([_audit_row(1, bad_call)])


def test_envelope_without_call_mapping_raises() -> None:
    with pytest.raises(AuditIntegrityError):
        ContextBudgetHistory.from_records([_audit_row(1, "not-a-mapping")])
    row = _audit_row(1, _call())
    assert row.tool_calls is not None
    with pytest.raises(AuditIntegrityError):
        ContextBudgetHistory.from_records([replace(row, tool_calls=[{"_kind": "llm_call_audit"}])])


def test_envelopes_of_another_kind_or_none_are_not_call_audits() -> None:
    row = _audit_row(1, _call(total_bytes=4000))
    other = replace(row, tool_calls=[{"_kind": "audit", "invocation": {}}, {"legacy": True}])
    assert ContextBudgetHistory.from_records([other]) == ContextBudgetHistory.empty()
    assert ContextBudgetHistory.from_records([row, other]) == ContextBudgetHistory.from_records([row])


def test_a_real_stored_call_envelope_gives_its_shed_state() -> None:
    """Control for the stored shed indices read from the product's envelope."""
    messages = [{"role": "system", "content": "s" * 900}, {"role": "user", "content": "u" * 300}]
    wire_bytes = request_wire_bytes(messages, None)
    call = build_llm_call_record(
        model_requested=_COMPOSER,
        messages=messages,
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=SimpleNamespace(usage=SimpleNamespace(prompt_tokens=300, completion_tokens=1, total_tokens=301)),
        context_budget=ComposerContextBudgetRecord.from_shed_keys(
            policy_hash="a" * 64,
            bytes_per_token=3.0,
            estimated_tokens_before=400,
            estimated_tokens_after=400,
            request_wire_bytes=wire_bytes,
            shed_keys=("history:3",),
        ),
    )
    assert call.prompt_tokens == 300
    row = replace(_audit_row(1, _call()), tool_calls=[llm_call_audit_envelope(call)])

    history = ContextBudgetHistory.from_records([row])

    assert history.previously_shed_history == frozenset({"history:3"})
