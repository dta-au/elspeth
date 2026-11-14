"""Context-budget history derived from a session's stored composer call records.

The budgeter needs two facts from earlier turns: the measured bytes-per-token
ratio of the provider's own prompt accounting, and which history messages an
earlier call already shed (so the same messages stay shed and the provider
prefix stays stable). Both live in the public ``llm_call_audit`` envelopes on
``role="audit"`` rows. Those rows are ELSPETH's own stored data (Tier 1): a
present-but-malformed envelope raises, it is never skipped.

Both facts are read only from calls that carry a non-null ``context_budget``:
the budgeted composer calls (compose loop, planner, escape hatch). The ratio
is the record's ``request_wire_bytes`` (the size of the request that call
sent, on the measure the budget is judged on) over the call's reported
prompt tokens. An advisor or auto-title
call stores the same envelope with a null ``context_budget``; it is a
differently shaped request, often on another model, and gives neither fact.

An absent ``context_budget`` key means the call predates context budgeting and
is read exactly like a null value: no shedding was recorded and no ratio is
taken. The first budgeted turn of a session written before the upgrade
therefore runs on the policy's fallback ratio, which over-estimates tokens:
the conservative direction. A budgeted record without ``request_wire_bytes``
(written only by pre-release builds) likewise gives no ratio.
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
                call = _budgeted_call(envelope["call"])
                if call is None:
                    continue
                if shed is None:
                    shed = call.shed_history
                if call.bytes_per_token is None:
                    continue
                if newest_ratio is None:
                    newest_ratio = call.bytes_per_token
                if call.model_requested not in ratio_by_model:
                    ratio_by_model[call.model_requested] = call.bytes_per_token
        return cls(
            bytes_per_token=newest_ratio,
            previously_shed_history=shed if shed is not None else frozenset(),
            bytes_per_token_by_model=tuple(ratio_by_model.items()),
        )


@final
@dataclass(frozen=True, slots=True)
class _BudgetedCall:
    """What the history needs from one stored budgeted call, parsed and validated."""

    model_requested: str
    shed_history: frozenset[str]
    # The clamped ratio the call measured; ``None`` when it failed or reported no usable counts.
    bytes_per_token: float | None


def _budgeted_call(call: Mapping[str, Any]) -> _BudgetedCall | None:
    """Parse a stored call; ``None`` when it was not a budgeted composer call.

    ``context_budget`` is absent on records written before context budgeting
    and null on advisor and auto-title calls. Every other key read here is
    stored on every budgeted call, so its absence is corruption, not an older
    shape, and raises.
    """
    if "context_budget" not in call or call["context_budget"] is None:
        return None
    budget = call["context_budget"]
    if not isinstance(budget, Mapping):
        raise AuditIntegrityError("Stored composer call context_budget is not a mapping")
    for key in ("model_requested", "status", "prompt_tokens"):
        if key not in call:
            raise AuditIntegrityError(f"Stored budgeted composer call audit lacks {key}")
    model, status, prompt_tokens = call["model_requested"], call["status"], call["prompt_tokens"]
    if type(model) is not str or not model:
        raise AuditIntegrityError("Stored composer call audit has no requested model")
    if type(status) is not str:
        raise AuditIntegrityError("Stored composer call audit has a non-string status")
    if prompt_tokens is not None and (type(prompt_tokens) is not int or prompt_tokens < 0):
        raise AuditIntegrityError("Stored composer call prompt_tokens is not a non-negative integer")
    if "shed_history_indices" not in budget:
        raise AuditIntegrityError("Stored composer call context_budget lacks shed_history_indices")
    indices = budget["shed_history_indices"]
    if not isinstance(indices, list | tuple) or not all(type(index) is int and index >= 0 for index in indices):
        raise AuditIntegrityError("Stored composer call context_budget.shed_history_indices is not a list of indices")
    ratio: float | None = None
    # ``request_wire_bytes`` is absent only on records written by pre-release
    # builds: no ratio, so the fallback applies.
    if "request_wire_bytes" in budget:
        wire_bytes = budget["request_wire_bytes"]
        if type(wire_bytes) is not int or wire_bytes < 0:
            raise AuditIntegrityError("Stored composer call context_budget.request_wire_bytes is not a non-negative integer")
        if status == ComposerLLMCallStatus.SUCCESS.value and prompt_tokens is not None and prompt_tokens > 0 and wire_bytes > 0:
            # Positive integers divide to a finite positive float; the clamp
            # bounds what provider-reported token counts can do to the estimate.
            ratio = min(max(wire_bytes / prompt_tokens, CONTEXT_BUDGET_MIN_BYTES_PER_TOKEN), CONTEXT_BUDGET_MAX_BYTES_PER_TOKEN)
    return _BudgetedCall(
        model_requested=model,
        shed_history=frozenset(_history_key(index) for index in indices),
        bytes_per_token=ratio,
    )
