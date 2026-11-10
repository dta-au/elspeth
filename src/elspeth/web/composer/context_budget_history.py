"""Context-budget history derived from a session's stored composer call records.

The budgeter needs two facts from earlier turns: the measured bytes-per-token
ratio of the provider's own prompt accounting, and which history messages an
earlier call already shed (so the same messages stay shed and the provider
prefix stays stable). Both live in the public ``llm_call_audit`` envelopes on
``role="audit"`` rows. Those rows are ELSPETH's own stored data (Tier 1): a
present-but-malformed envelope raises, it is never skipped.

An absent ``context_budget`` key means the call predates context budgeting and
is read exactly like a null value: no shedding was recorded.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, final

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.protocol import ChatMessageRecord

_LLM_CALL_AUDIT_KIND = "llm_call_audit"


def _history_key(index: int) -> str:
    return f"history:{index}"


@final
@dataclass(frozen=True, slots=True)
class ContextBudgetHistory:
    """Bytes-per-token ratio and previously shed history keys for one session."""

    bytes_per_token: float | None
    previously_shed_history: frozenset[str]

    @classmethod
    def empty(cls) -> ContextBudgetHistory:
        return cls(bytes_per_token=None, previously_shed_history=frozenset())

    @classmethod
    def from_records(cls, records: Sequence[ChatMessageRecord]) -> ContextBudgetHistory:
        ratio: float | None = None
        shed: frozenset[str] | None = None
        for row in reversed(records):
            if type(row) is not ChatMessageRecord:
                raise AuditIntegrityError("Context budget history received a non-record row")
            if row.role != "audit" or row.tool_calls is None:
                continue
            for envelope in reversed(row.tool_calls):
                if envelope.get("_kind") != _LLM_CALL_AUDIT_KIND:
                    continue
                call = envelope.get("call")
                if not isinstance(call, Mapping):
                    raise AuditIntegrityError("Stored composer call audit envelope lacks its call mapping")
                if shed is None:
                    shed = _shed_history_keys(call)
                if ratio is None:
                    ratio = _bytes_per_token(call)
                if ratio is not None and shed is not None:
                    return cls(bytes_per_token=ratio, previously_shed_history=shed)
        return cls(bytes_per_token=ratio, previously_shed_history=shed if shed is not None else frozenset())


def _bytes_per_token(call: Mapping[str, Any]) -> float | None:
    status = call.get("status")
    if type(status) is not str:
        raise AuditIntegrityError("Stored composer call audit has a non-string status")
    if status != ComposerLLMCallStatus.SUCCESS.value:
        return None
    split = call.get("request_byte_split")
    if split is None:
        return None
    if not isinstance(split, Mapping):
        raise AuditIntegrityError("Stored composer call request_byte_split is not a mapping")
    total_bytes = split.get("total_bytes")
    if type(total_bytes) is not int or total_bytes < 0:
        raise AuditIntegrityError("Stored composer call request_byte_split.total_bytes is not a non-negative integer")
    prompt_tokens = call.get("prompt_tokens")
    if prompt_tokens is not None and (type(prompt_tokens) is not int or prompt_tokens < 0):
        raise AuditIntegrityError("Stored composer call prompt_tokens is not a non-negative integer")
    if total_bytes == 0 or prompt_tokens is None or prompt_tokens == 0:
        return None
    return total_bytes / prompt_tokens


def _shed_history_keys(call: Mapping[str, Any]) -> frozenset[str] | None:
    """Return the shed set recorded by this call, or ``None`` when it recorded none."""
    budget = call.get("context_budget")
    if budget is None:
        return None
    if not isinstance(budget, Mapping):
        raise AuditIntegrityError("Stored composer call context_budget is not a mapping")
    indices = budget.get("shed_history_indices")
    if not isinstance(indices, list | tuple) or not all(type(index) is int and index >= 0 for index in indices):
        raise AuditIntegrityError("Stored composer call context_budget.shed_history_indices is not a list of indices")
    return frozenset(_history_key(index) for index in indices)
