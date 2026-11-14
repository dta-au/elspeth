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
from elspeth.web.composer.context_budget import CONTEXT_BUDGET_MAX_BYTES_PER_TOKEN, CONTEXT_BUDGET_MIN_BYTES_PER_TOKEN
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
    is never what the ratio is read from: it is set to a size that would
    give a different ratio.
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


def test_empty_has_no_ratio_and_no_shed_set() -> None:
    empty = ContextBudgetHistory.empty()
    assert empty.bytes_per_token is None
    assert empty.previously_shed_history == frozenset()


def test_records_without_audit_rows_give_empty() -> None:
    assert ContextBudgetHistory.from_records([_user_row(1)]) == ContextBudgetHistory.empty()
    assert ContextBudgetHistory.from_records([]) == ContextBudgetHistory.empty()


def test_the_ratio_is_the_recorded_wire_bytes_over_the_prompt_tokens() -> None:
    history = ContextBudgetHistory.from_records([_audit_row(1, _call(total_bytes=4321, prompt_tokens=1000))])
    assert history.bytes_per_token == 4321 / 1000
    assert history.ratio_for(_COMPOSER) == 4.321


def test_a_budgeted_record_without_wire_bytes_gives_no_ratio_so_the_fallback_applies() -> None:
    history = ContextBudgetHistory.from_records([_audit_row(1, _call(total_bytes=None, shed=[2]))])
    assert history.bytes_per_token is None and history.ratio_for(_COMPOSER) is None
    # Its shed set is still read.
    assert history.previously_shed_history == frozenset({"history:2"})


def test_newest_successful_budgeted_call_wins_for_the_ratio() -> None:
    rows = [_audit_row(1, _call(total_bytes=2000, prompt_tokens=1000)), _audit_row(2, _call(total_bytes=4500, prompt_tokens=1000))]
    history = ContextBudgetHistory.from_records(rows)
    assert history.bytes_per_token == 4.5
    assert history.ratio_for(_COMPOSER) == 4.5


def test_failed_call_is_skipped_for_the_ratio() -> None:
    rows = [_audit_row(1, _call(total_bytes=2000, prompt_tokens=1000)), _audit_row(2, _call(status="error", total_bytes=9000))]
    assert ContextBudgetHistory.from_records(rows).ratio_for(_COMPOSER) == 2.0


@pytest.mark.parametrize(
    "newest",
    [
        _call(total_bytes=None),
        _call(prompt_tokens=None),
        _call(prompt_tokens=0),
        _call(total_bytes=0),
    ],
)
def test_unmeasurable_call_is_skipped_for_the_ratio(newest: dict[str, Any]) -> None:
    rows = [_audit_row(1, _call(total_bytes=2000, prompt_tokens=1000)), _audit_row(2, newest)]
    assert ContextBudgetHistory.from_records(rows).ratio_for(_COMPOSER) == 2.0


@pytest.mark.parametrize("only", [_call(total_bytes=None), _call(prompt_tokens=None), _call(prompt_tokens=0), _call(status="error")])
def test_ratio_is_none_when_no_call_qualifies(only: dict[str, Any]) -> None:
    history = ContextBudgetHistory.from_records([_audit_row(1, only)])
    assert history.bytes_per_token is None
    assert history.ratio_for(_COMPOSER) is None


@pytest.mark.parametrize("unbudgeted", [None, _NO_KEY], ids=["null-context-budget", "no-context-budget-key"])
def test_a_call_that_was_not_budgeted_never_gives_the_ratio(unbudgeted: Any) -> None:
    """An advisor or auto-title call (null budget) or a call from before budgeting (no key) is another kind of request."""
    composer_call = _call(total_bytes=4000, prompt_tokens=1000)
    # Newest: the END-gate advisor call, on another model with a sparser tokenizer.
    advisor_call = _call(total_bytes=9000, prompt_tokens=1000, shed=unbudgeted, model=_ADVISOR)
    history = ContextBudgetHistory.from_records([_audit_row(1, composer_call), _audit_row(2, advisor_call)])
    assert history.bytes_per_token == 4.0
    assert history.ratio_for(_COMPOSER) == 4.0
    assert history.ratio_for(_ADVISOR) == 4.0  # no budgeted call on that model: the newest budgeted call of any model
    # Alone, it gives nothing: the session falls back to the policy's ratio.
    alone = ContextBudgetHistory.from_records([_audit_row(1, advisor_call)])
    assert alone.bytes_per_token is None and alone.ratio_for(_ADVISOR) is None


def test_the_ratio_prefers_the_newest_budgeted_call_of_the_same_model() -> None:
    hatch = "test/escape-hatch"
    rows = [
        _audit_row(1, _call(total_bytes=4000, prompt_tokens=1000, model=_COMPOSER)),
        _audit_row(2, _call(total_bytes=2500, prompt_tokens=1000, model=hatch)),
        _audit_row(3, _call(total_bytes=4400, prompt_tokens=1000, model=_COMPOSER)),
        _audit_row(4, _call(total_bytes=3000, prompt_tokens=1000, model=hatch)),
    ]
    history = ContextBudgetHistory.from_records(rows)
    assert history.ratio_for(_COMPOSER) == 4.4
    assert history.ratio_for(hatch) == 3.0
    # A model with no budgeted call takes the newest budgeted call of any model.
    assert history.ratio_for("some/other-model") == 3.0
    assert history.bytes_per_token == 3.0
    assert history.bytes_per_token_by_model == ((hatch, 3.0), (_COMPOSER, 4.4))


@pytest.mark.parametrize(
    ("total_bytes", "prompt_tokens", "expected"),
    [
        (12_000, 1000, CONTEXT_BUDGET_MAX_BYTES_PER_TOKEN),  # e.g. prompt_tokens reported without cached tokens
        (6_001, 1000, CONTEXT_BUDGET_MAX_BYTES_PER_TOKEN),
        (6_000, 1000, 6.0),
        (5_000, 1000, 5.0),
        (2_000, 1000, 2.0),
        (1_999, 1000, CONTEXT_BUDGET_MIN_BYTES_PER_TOKEN),
        (10, 1000, CONTEXT_BUDGET_MIN_BYTES_PER_TOKEN),
    ],
)
def test_a_ratio_outside_the_sane_range_is_clamped_not_rejected(total_bytes: int, prompt_tokens: int, expected: float) -> None:
    assert (CONTEXT_BUDGET_MIN_BYTES_PER_TOKEN, CONTEXT_BUDGET_MAX_BYTES_PER_TOKEN) == (2.0, 6.0)
    history = ContextBudgetHistory.from_records([_audit_row(1, _call(total_bytes=total_bytes, prompt_tokens=prompt_tokens))])
    assert history.ratio_for(_COMPOSER) == expected
    assert history.bytes_per_token == expected


def test_ratio_for_without_per_model_ratios_is_the_any_model_ratio() -> None:
    assert ContextBudgetHistory(bytes_per_token=4.0, previously_shed_history=frozenset()).ratio_for(_COMPOSER) == 4.0
    assert ContextBudgetHistory.empty().ratio_for(_COMPOSER) is None


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
    assert history.bytes_per_token == 3.0


def test_absent_context_budget_key_means_no_shedding_recorded_and_no_ratio() -> None:
    """A session written before context budgeting: its first budgeted turn uses the fallback ratio."""
    history = ContextBudgetHistory.from_records([_audit_row(1, _call(shed=_NO_KEY))])
    assert history == ContextBudgetHistory.empty()


def _without(call: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in call.items() if name != key}


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
        # A budgeted call always stores these keys; their absence is corruption, not an older shape.
        _without(_call(), "status"),
        _without(_call(), "prompt_tokens"),
        _without(_call(), "model_requested"),
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
    assert ContextBudgetHistory.from_records([row, other]).ratio_for(_COMPOSER) == 4.0


def test_a_real_stored_call_envelope_gives_its_model_the_ratio() -> None:
    """Control for the required-key reads: the envelope the product writes carries every key the reader demands."""
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

    # Wire bytes over prompt tokens, not the content split (1200 bytes) over them.
    assert call.request_byte_split is not None and call.request_byte_split.total_bytes == 1200 < wire_bytes
    assert history.ratio_for(_COMPOSER) == wire_bytes / 300
    assert history.previously_shed_history == frozenset({"history:3"})
