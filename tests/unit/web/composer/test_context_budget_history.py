"""Tests for the stored-call reader behind the composer context budget."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.context_budget_history import ContextBudgetHistory
from elspeth.web.sessions.protocol import ChatMessageRecord

_SESSION = uuid.uuid4()
_NO_KEY: Any = object()


def _call(
    *,
    status: str = "success",
    total_bytes: int | None = 3000,
    prompt_tokens: int | None = 1000,
    shed: list[int] | None | Any = _NO_KEY,
) -> dict[str, Any]:
    call: dict[str, Any] = {
        "status": status,
        "prompt_tokens": prompt_tokens,
        "request_byte_split": (
            None if total_bytes is None else {"message_bytes": total_bytes, "tools_bytes": 0, "total_bytes": total_bytes}
        ),
    }
    if shed is not _NO_KEY:
        call["context_budget"] = (
            None
            if shed is None
            else {
                "policy_hash": "h",
                "bytes_per_token": 3.0,
                "estimated_tokens_before": 10,
                "estimated_tokens_after": 5,
                "shed_history_indices": shed,
                "stubbed_tool_call_ids": [],
                "shed_hatch_event_indices": [],
            }
        )
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


def test_newest_successful_call_wins_for_the_ratio() -> None:
    rows = [_audit_row(1, _call(total_bytes=2000, prompt_tokens=1000)), _audit_row(2, _call(total_bytes=4500, prompt_tokens=1000))]
    assert ContextBudgetHistory.from_records(rows).bytes_per_token == 4.5


def test_failed_call_is_skipped_for_the_ratio() -> None:
    rows = [_audit_row(1, _call(total_bytes=2000, prompt_tokens=1000)), _audit_row(2, _call(status="error", total_bytes=9000))]
    assert ContextBudgetHistory.from_records(rows).bytes_per_token == 2.0


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
    assert ContextBudgetHistory.from_records(rows).bytes_per_token == 2.0


@pytest.mark.parametrize("only", [_call(total_bytes=None), _call(prompt_tokens=None), _call(prompt_tokens=0), _call(status="error")])
def test_ratio_is_none_when_no_call_qualifies(only: dict[str, Any]) -> None:
    assert ContextBudgetHistory.from_records([_audit_row(1, only)]).bytes_per_token is None


def test_shed_set_comes_from_newest_call_with_non_null_context_budget() -> None:
    rows = [
        _audit_row(1, _call(shed=[0])),
        _audit_row(2, _call(shed=[0, 2, 5])),
        _audit_row(3, _call(status="error", shed=None)),
        _audit_row(4, _call(status="error", total_bytes=None)),
    ]
    history = ContextBudgetHistory.from_records(rows)
    # Rows 3/4 carry a null or absent budget; the newest non-null one is row 2.
    assert history.previously_shed_history == frozenset({"history:0", "history:2", "history:5"})


def test_newest_non_null_budget_is_a_failed_call() -> None:
    rows = [_audit_row(1, _call(shed=[0])), _audit_row(2, _call(status="error", total_bytes=None, shed=[1]))]
    history = ContextBudgetHistory.from_records(rows)
    assert history.previously_shed_history == frozenset({"history:1"})
    assert history.bytes_per_token == 3.0


def test_absent_context_budget_key_means_no_shedding_recorded() -> None:
    history = ContextBudgetHistory.from_records([_audit_row(1, _call())])
    assert history.previously_shed_history == frozenset()
    assert history.bytes_per_token == 3.0


@pytest.mark.parametrize(
    "bad_call",
    [
        {"status": "success", "prompt_tokens": 1, "request_byte_split": "x"},
        {"status": "success", "prompt_tokens": 1, "request_byte_split": {"total_bytes": "3"}},
        {"status": "success", "prompt_tokens": "1", "request_byte_split": {"total_bytes": 3}},
        {"status": 7, "prompt_tokens": 1, "request_byte_split": None},
        {"status": "success", "prompt_tokens": 1, "request_byte_split": None, "context_budget": "x"},
        {"status": "success", "prompt_tokens": 1, "request_byte_split": None, "context_budget": {"shed_history_indices": "0"}},
        {"status": "success", "prompt_tokens": 1, "request_byte_split": None, "context_budget": {"shed_history_indices": [True]}},
        {"status": "success", "prompt_tokens": 1, "request_byte_split": None, "context_budget": {}},
    ],
)
def test_malformed_call_raises(bad_call: dict[str, Any]) -> None:
    with pytest.raises(AuditIntegrityError):
        ContextBudgetHistory.from_records([_audit_row(1, bad_call)])


def test_envelope_without_call_mapping_raises() -> None:
    with pytest.raises(AuditIntegrityError):
        ContextBudgetHistory.from_records([_audit_row(1, "not-a-mapping")])
