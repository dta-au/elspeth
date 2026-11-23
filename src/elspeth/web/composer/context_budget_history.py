"""Context-budget history derived from a session's stored composer call records.

The session retains which history messages an earlier call already shed, so
the same messages stay shed and the provider prefix stays stable. This lives in
the public ``llm_call_audit`` envelopes on
``role="audit"`` rows. Those rows are ELSPETH's own stored data (Tier 1): a
present-but-malformed envelope raises, it is never skipped.

The shed state is read only from calls that carry a non-null ``context_budget``:
the budgeted composer calls (compose loop, planner, escape hatch). An advisor
or auto-title call stores a null ``context_budget`` and gives no shed state.

An absent ``context_budget`` key means the call predates context budgeting and
is read exactly like a null value: no shedding was recorded. Anchors come only
from live private request captures, never persisted rows.

The session also retains, per model and tool dialect, the density measured on
its newest successful budgeted call: the wire bytes that call sent over the
provider's prompt count. That density prices the byte arm of admission and
release (see :func:`decide_context_budget`), so it is the same on the first
call of a turn, which has no live anchor, as on the calls after it. Only the
stored counts and sizes are read, never content.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, final

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus, ContextBudgetAnchorStatus, ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import detach_prompt_guard_messages
from elspeth.core.canonical import canonical_json
from elspeth.web.composer.context_budget import (
    CONTEXT_BUDGET_MAX_MEASURED_BYTES_PER_TOKEN,
    CONTEXT_BUDGET_MIN_MEASURED_BYTES_PER_TOKEN,
)
from elspeth.web.sessions.protocol import ChatMessageRecord

_LLM_CALL_AUDIT_KIND = "llm_call_audit"


@dataclass(frozen=True, slots=True)
class ContextBudgetAnchor:
    """Content-free pricing evidence; the private request never enters the audit."""

    tokens: int | None = None
    wire_bytes: int = 0
    shared_bytes: int = 0
    authored_bytes: int | None = None
    reason: ContextBudgetAnchorStatus = "no_previous_call"


def request_anchor(
    *,
    calls: Sequence[ComposerLLMCall],
    model: str,
    dialect: ToolContractDialect | None,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    mutable_positions: frozenset[int],
) -> ContextBudgetAnchor:
    """Reuse the newest usable same-model, same-dialect call in this exchange.

    Match entire canonical messages, with multiplicity, and the entire tool
    block. Mutable messages (including omission-count carriers) never receive
    credit, even if this particular decision ultimately keeps them. Paying
    the anchor's full count bounds the shared subset without estimating how
    its tokens were distributed. Changed dialects/models and missing private
    capture or usage fail closed. The eight-byte plausibility limit rejects
    implausibly low counts; rejecting a compressible request is safe.
    """
    rejected = ContextBudgetAnchor()
    for call in reversed(calls):
        anchor = _call_anchor(call, model=model, dialect=dialect, messages=messages, tools=tools, mutable_positions=mutable_positions)
        if anchor.tokens is not None:
            return anchor
        if rejected.reason == "no_previous_call":
            rejected = anchor
    return rejected


def _call_anchor(
    call: ComposerLLMCall,
    *,
    model: str,
    dialect: ToolContractDialect | None,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    mutable_positions: frozenset[int],
) -> ContextBudgetAnchor:
    if call.model_requested != model or call.tool_contract_dialect != dialect:
        return ContextBudgetAnchor(reason="model_or_dialect_changed")
    if call.status is not ComposerLLMCallStatus.SUCCESS:
        return ContextBudgetAnchor(reason="previous_call_failed")
    private = call.private_request
    if private is None:
        return ContextBudgetAnchor(reason="request_unavailable")
    if private.canonical is None:
        return ContextBudgetAnchor(reason=private.reasons[0])
    captured = json.loads(private.canonical)
    old_messages = captured["messages"]
    old_tools = captured["tools"] or []
    wire = len(canonical_json({"messages": old_messages, "tools": old_tools}).encode("utf-8"))
    # A present budget count is owned Tier-1 evidence from this exchange's
    # captured request. Its producer labels were detached from the private
    # wire capture, so the classification cannot be recomputed here.
    authored_bytes = call.context_budget.authored_bytes if call.context_budget is not None else None
    if authored_bytes is not None and (type(authored_bytes) is not int or not 0 <= authored_bytes <= wire):
        raise AuditIntegrityError("Anchor authored bytes are inconsistent with the captured request")
    # LiteLLM normalizes Anthropic and Bedrock cache reads/creation into
    # prompt_tokens; OpenAI's cached count is a subset of prompt_tokens.
    # Adding the separately reported fields would charge those tokens twice.
    count = call.prompt_tokens
    if type(count) is not int or count <= 0 or count > wire or count * 8 < wire:
        return ContextBudgetAnchor(wire_bytes=wire, reason="invalid_prompt_count")
    available = Counter(canonical_json(message) for message in old_messages)
    shared = 0
    for index, message in enumerate(detach_prompt_guard_messages(messages)[0]):
        encoded = canonical_json(message)
        if index not in mutable_positions and available[encoded]:
            available[encoded] -= 1
            shared += len(encoded.encode("utf-8"))
    encoded_tools = canonical_json(list(tools) if tools is not None else [])
    if encoded_tools == canonical_json(old_tools):
        shared += len(encoded_tools.encode("utf-8"))
    reason: ContextBudgetAnchorStatus = "anchored" if authored_bytes is not None else "authored_evidence_unavailable"
    return ContextBudgetAnchor(tokens=count, wire_bytes=wire, shared_bytes=shared, authored_bytes=authored_bytes, reason=reason)


def _history_key(index: int) -> str:
    return f"history:{index}"


def _measured_density(prompt_tokens: object, wire_bytes: object) -> float | None:
    """Sent wire bytes over the provider's prompt count, when both exist and the ratio is plausible."""
    if type(prompt_tokens) is not int or prompt_tokens <= 0 or type(wire_bytes) is not int:
        return None
    density = wire_bytes / prompt_tokens
    if not CONTEXT_BUDGET_MIN_MEASURED_BYTES_PER_TOKEN <= density <= CONTEXT_BUDGET_MAX_MEASURED_BYTES_PER_TOKEN:
        return None
    return density


@final
@dataclass(frozen=True, slots=True)
class MeasuredDensity:
    """The density measured on a session's newest successful budgeted call of one model and tool dialect."""

    model: str
    dialect: ToolContractDialect | None
    bytes_per_token: float


@final
@dataclass(frozen=True, slots=True)
class ContextBudgetHistory:
    """Previously shed history keys for one session, and its measured densities, newest first."""

    previously_shed_history: frozenset[str]
    measured: tuple[MeasuredDensity, ...] = ()

    @classmethod
    def empty(cls) -> ContextBudgetHistory:
        return cls(previously_shed_history=frozenset())

    def measured_bytes_per_token(
        self, *, calls: Sequence[ComposerLLMCall], model: str, dialect: ToolContractDialect | None
    ) -> float | None:
        """The density the byte rate is priced from, or ``None`` for the fallback rate.

        The newest successful budgeted call of this model and dialect in this
        exchange's ``calls`` wins; otherwise the session's stored one. A call
        whose count is missing or implausible for its size gives none. A
        different model or dialect never gives one: its tokens are another
        tokenizer's.
        """
        for call in reversed(calls):
            if (
                call.context_budget is None
                or call.model_requested != model
                or call.tool_contract_dialect != dialect
                or call.status is not ComposerLLMCallStatus.SUCCESS
            ):
                continue
            density = _measured_density(call.prompt_tokens, call.context_budget.request_wire_bytes)
            if density is not None:
                return density
        for measured in self.measured:
            if measured.model == model and measured.dialect == dialect:
                return measured.bytes_per_token
        return None

    def shed_history_after(self, calls: Sequence[ComposerLLMCall]) -> frozenset[str]:
        """History state after the newest budgeted call, including an explicit release.

        Calls without history carry this state forward; they must not union
        it with an older set that a history-bearing call already released.
        """
        for call in reversed(calls):
            if call.context_budget is not None:
                return frozenset(_history_key(index) for index in call.context_budget.shed_history_indices)
        return self.previously_shed_history

    @classmethod
    def from_records(cls, records: Sequence[ChatMessageRecord]) -> ContextBudgetHistory:
        shed: frozenset[str] | None = None
        measured: dict[tuple[str, ToolContractDialect | None], MeasuredDensity] = {}
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
                if call.density is not None and (call.model, call.dialect) not in measured:
                    measured[(call.model, call.dialect)] = MeasuredDensity(call.model, call.dialect, call.density)
        return cls(
            previously_shed_history=shed if shed is not None else frozenset(),
            measured=tuple(measured.values()),
        )


@final
@dataclass(frozen=True, slots=True)
class _BudgetedCall:
    """What the history needs from one stored budgeted call, parsed and validated."""

    shed_history: frozenset[str]
    model: str
    dialect: ToolContractDialect | None
    density: float | None


def _budgeted_call(call: Mapping[str, Any]) -> _BudgetedCall | None:
    """Parse a stored call; ``None`` when it was not a budgeted composer call.

    ``context_budget`` is absent on records written before context budgeting
    and null on advisor and auto-title calls. Required audit fields and the
    shed-index key are stored on every budgeted call; malformed owned records
    are corruption and raise.
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
    wire_bytes: int | None = None
    if "request_wire_bytes" in budget:
        wire_bytes = budget["request_wire_bytes"]
        if type(wire_bytes) is not int or wire_bytes < 0:
            raise AuditIntegrityError("Stored composer call context_budget.request_wire_bytes is not a non-negative integer")
    # Records written before the dialect was stamped have no key; they match
    # only a dialect-free route, never a stamped one.
    dialect: ToolContractDialect | None = None
    if "tool_contract_dialect" in call and call["tool_contract_dialect"] is not None:
        raw_dialect = call["tool_contract_dialect"]
        if type(raw_dialect) is not str or raw_dialect not in {member.value for member in ToolContractDialect}:
            raise AuditIntegrityError("Stored composer call tool_contract_dialect is not a known dialect")
        dialect = ToolContractDialect(raw_dialect)
    return _BudgetedCall(
        shed_history=frozenset(_history_key(index) for index in indices),
        model=model,
        dialect=dialect,
        density=_measured_density(prompt_tokens, wire_bytes) if status == ComposerLLMCallStatus.SUCCESS.value else None,
    )
