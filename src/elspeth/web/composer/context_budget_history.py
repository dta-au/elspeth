"""Context-budget history derived from a session's stored composer call records.

The budgeter needs two facts from earlier turns: the measured bytes-per-token
ratio of the provider's own prompt accounting, and which history messages an
earlier call already shed (so the same messages stay shed and the provider
prefix stays stable). Both live in the public ``llm_call_audit`` envelopes on
``role="audit"`` rows. Those rows are ELSPETH's own stored data (Tier 1): a
present-but-malformed envelope raises, it is never skipped.

Both facts are read only from calls that carry a non-null ``context_budget``:
the budgeted composer calls (compose loop, planner, escape hatch), whose byte
split and prompt tokens describe a composer request. An advisor or auto-title
call stores the same envelope with a null ``context_budget``; it is a
differently shaped request, often on another model, and gives neither fact.

An absent ``context_budget`` key means the call predates context budgeting and
is read exactly like a null value: no shedding was recorded and no ratio is
taken. The first budgeted turn of a session written before the upgrade
therefore runs on the policy's fallback ratio, which over-estimates tokens:
the conservative direction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, final

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.context_budget import CONTEXT_BUDGET_MAX_BYTES_PER_TOKEN, CONTEXT_BUDGET_MIN_BYTES_PER_TOKEN
from elspeth.web.sessions.protocol import ChatMessageRecord

_LLM_CALL_AUDIT_KIND = "llm_call_audit"


def _history_key(index: int) -> str:
    return f"history:{index}"


@final
@dataclass(frozen=True, slots=True)
class ContextBudgetHistory:
    """Bytes-per-token ratios and previously shed history keys for one session.

    ``bytes_per_token`` is the ratio of the newest measurable budgeted call of
    any model. ``bytes_per_token_by_model`` pairs each requested model with
    the ratio of its own newest measurable budgeted call, newest model first.
    Use :meth:`ratio_for` at the call site, which knows the model it budgets.
    """

    bytes_per_token: float | None
    previously_shed_history: frozenset[str]
    bytes_per_token_by_model: tuple[tuple[str, float], ...] = ()

    @classmethod
    def empty(cls) -> ContextBudgetHistory:
        return cls(bytes_per_token=None, previously_shed_history=frozenset())

    def ratio_for(self, model: str) -> float | None:
        """The ratio for a call to ``model``: its own newest, else the newest of any model, else ``None``.

        Tokenizers differ between models, so a ratio measured on the same
        model is preferred. ``None`` leaves the decision on the policy's
        fallback ratio.
        """
        for measured_model, ratio in self.bytes_per_token_by_model:
            if measured_model == model:
                return ratio
        return self.bytes_per_token

    @classmethod
    def from_records(cls, records: Sequence[ChatMessageRecord]) -> ContextBudgetHistory:
        newest_ratio: float | None = None
        ratio_by_model: dict[str, float] = {}
        shed: frozenset[str] | None = None
        for row in reversed(records):
            if type(row) is not ChatMessageRecord:
                raise AuditIntegrityError("Context budget history received a non-record row")
            if row.role != "audit" or row.tool_calls is None:
                continue
            for envelope in reversed(row.tool_calls):
                # An audit row also carries envelopes of other kinds, and older
                # rows carry envelopes with no kind at all; neither is a call audit.
                if "_kind" not in envelope or envelope["_kind"] != _LLM_CALL_AUDIT_KIND:
                    continue
                if "call" not in envelope or not isinstance(envelope["call"], Mapping):
                    raise AuditIntegrityError("Stored composer call audit envelope lacks its call mapping")
                call = envelope["call"]
                budget = _context_budget(call)
                if budget is None:
                    continue
                if shed is None:
                    shed = _shed_history_keys(budget)
                ratio = _bytes_per_token(call)
                if ratio is None:
                    continue
                model = _model_requested(call)
                if newest_ratio is None:
                    newest_ratio = ratio
                if model not in ratio_by_model:
                    ratio_by_model[model] = ratio
        return cls(
            bytes_per_token=newest_ratio,
            previously_shed_history=shed if shed is not None else frozenset(),
            bytes_per_token_by_model=tuple(ratio_by_model.items()),
        )


def _required(call: Mapping[str, Any], key: str) -> Any:
    """Read a key every budgeted call record stores; its absence is corruption, not an older shape."""
    if key not in call:
        raise AuditIntegrityError(f"Stored budgeted composer call audit lacks {key}")
    return call[key]


def _context_budget(call: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return the call's stored context-budget record, or ``None`` when it was not a budgeted call."""
    # Absent on records written before context budgeting; null on advisor and auto-title calls.
    if "context_budget" not in call or call["context_budget"] is None:
        return None
    budget = call["context_budget"]
    if not isinstance(budget, Mapping):
        raise AuditIntegrityError("Stored composer call context_budget is not a mapping")
    return budget


def _model_requested(call: Mapping[str, Any]) -> str:
    model = _required(call, "model_requested")
    if type(model) is not str or not model:
        raise AuditIntegrityError("Stored composer call audit has no requested model")
    return model


def _bytes_per_token(call: Mapping[str, Any]) -> float | None:
    """The clamped ratio a budgeted call measured, or ``None`` when it failed or reported no usable counts."""
    status = _required(call, "status")
    if type(status) is not str:
        raise AuditIntegrityError("Stored composer call audit has a non-string status")
    split = _required(call, "request_byte_split")
    prompt_tokens = _required(call, "prompt_tokens")
    if split is not None:
        if not isinstance(split, Mapping):
            raise AuditIntegrityError("Stored composer call request_byte_split is not a mapping")
        if "total_bytes" not in split or type(split["total_bytes"]) is not int or split["total_bytes"] < 0:
            raise AuditIntegrityError("Stored composer call request_byte_split.total_bytes is not a non-negative integer")
    if prompt_tokens is not None and (type(prompt_tokens) is not int or prompt_tokens < 0):
        raise AuditIntegrityError("Stored composer call prompt_tokens is not a non-negative integer")
    if status != ComposerLLMCallStatus.SUCCESS.value or split is None or prompt_tokens is None:
        return None
    total_bytes: int = split["total_bytes"]
    if total_bytes == 0 or prompt_tokens == 0:
        return None
    # Positive integers divide to a finite positive float; the clamp bounds
    # what provider-reported token counts can do to the estimate.
    return min(max(total_bytes / prompt_tokens, CONTEXT_BUDGET_MIN_BYTES_PER_TOKEN), CONTEXT_BUDGET_MAX_BYTES_PER_TOKEN)


def _shed_history_keys(budget: Mapping[str, Any]) -> frozenset[str]:
    """Return the history keys a stored context-budget record shed."""
    if "shed_history_indices" not in budget:
        raise AuditIntegrityError("Stored composer call context_budget lacks shed_history_indices")
    indices = budget["shed_history_indices"]
    if not isinstance(indices, list | tuple) or not all(type(index) is int and index >= 0 for index in indices):
        raise AuditIntegrityError("Stored composer call context_budget.shed_history_indices is not a list of indices")
    return frozenset(_history_key(index) for index in indices)
