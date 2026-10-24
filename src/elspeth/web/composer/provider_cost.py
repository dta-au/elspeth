"""Request-owned legacy Web bounds and actual common provider accounting."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict

from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json

if TYPE_CHECKING:
    from elspeth.web.composer.protocol import ComposerSettings


def _is_supported_lifecycle(value: object) -> bool:
    return type(value) is int and value in (1, 2)


class CommonProviderCostRequest(TypedDict):
    """Owned policy view retaining the actual detached SDK field values."""

    messages: object
    tools: NotRequired[object]
    max_tokens: NotRequired[object]


class CommonProviderCostRefused(RuntimeError):
    def __init__(self, code: str, *, call_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail_codes: tuple[str, ...] = ()
        self.call_id = call_id


@dataclass(frozen=True, slots=True)
class CommonProviderCostPolicy:
    max_provider_calls: int
    max_request_bytes: int
    max_completion_tokens: int
    max_cumulative_provider_cost: Decimal
    repair_budget: int

    def __post_init__(self) -> None:
        for value in (self.max_provider_calls, self.max_request_bytes, self.max_completion_tokens):
            if type(value) is not int or value < 1:
                raise ValueError("legacy provider limits require positive exact integers")
        if type(self.repair_budget) is not int or self.repair_budget < 0:
            raise ValueError("repair budget requires a nonnegative exact integer")
        maximum_cost = self.max_cumulative_provider_cost
        if type(maximum_cost) is not Decimal or not maximum_cost.is_finite() or maximum_cost < 0:
            raise ValueError("provider cost limit requires finite nonnegative Decimal")


@dataclass(frozen=True, slots=True)
class CommonProviderCostCharge:
    call_id: str
    cost: Decimal | None
    cost_source: str
    response_accountable: bool = True

    def __post_init__(self) -> None:
        if type(self.call_id) is not str or not self.call_id:
            raise ValueError("charge requires actual physical call identity")
        if type(self.cost_source) is not str or not self.cost_source:
            raise ValueError("charge requires actual cost provenance")
        if type(self.response_accountable) is not bool:
            raise ValueError("charge requires actual response-accountability evidence")
        if self.cost is not None and (type(self.cost) is not Decimal or not self.cost.is_finite() or self.cost < 0):
            raise ValueError("charge is finite nonnegative cost or unknown")


@dataclass(frozen=True, slots=True)
class CommonProviderCostSnapshot:
    session_id: str
    request_id: str
    lifecycle_version: Literal[1, 2]
    applicability: Literal["legacy_empty_v1", "not_applicable"]
    policy: CommonProviderCostPolicy | None
    physical_calls: int = 0
    charges: tuple[CommonProviderCostCharge, ...] = ()
    refusal_code: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id or not self.request_id:
            raise ValueError("cost snapshot requires original logical identity")
        if not _is_supported_lifecycle(self.lifecycle_version):
            raise ValueError("unsupported immutable lifecycle")
        if self.applicability not in ("legacy_empty_v1", "not_applicable"):
            raise ValueError("unsupported policy applicability")
        eligible = self.applicability == "legacy_empty_v1"
        if eligible:
            if type(self.policy) is not CommonProviderCostPolicy:
                raise ValueError("policy applicability lost its exact policy")
        elif self.policy is not None:
            raise ValueError("not-applicable policy cannot carry foreign limits")
        if eligible and self.lifecycle_version != 1:
            raise ValueError("legacy planner policy cannot activate on v2")
        if type(self.physical_calls) is not int or self.physical_calls < 0:
            raise ValueError("physical call count is invalid")
        if type(self.charges) is not tuple or any(type(c) is not CommonProviderCostCharge for c in self.charges):
            raise ValueError("charges require nominal immutable entries")
        ids = tuple(c.call_id for c in self.charges)
        if len(set(ids)) != len(ids) or len(ids) > self.physical_calls:
            raise ValueError("charges do not identify distinct actual calls")
        if self.refusal_code not in (
            None,
            "COST_UNAVAILABLE",
            "COST_CAP_EXCEEDED",
            "MALFORMED_RESPONSE",
            "COMPLETION_TOKENS_EXCEEDED",
            "RESPONSE_TRUNCATED",
        ):
            raise ValueError("unknown provider refusal code")

    @property
    def cost_known(self) -> bool:
        return self.physical_calls == len(self.charges) and all(c.cost is not None for c in self.charges)

    @property
    def known_cost(self) -> Decimal:
        return sum((c.cost for c in self.charges if c.cost is not None), Decimal("0"))

    @property
    def validated_cost(self) -> Decimal:
        return sum((c.cost for c in self.charges if c.response_accountable and c.cost is not None), Decimal("0"))


class CommonProviderCostBudget:
    def __init__(self, snapshot: CommonProviderCostSnapshot) -> None:
        if type(snapshot) is not CommonProviderCostSnapshot:
            raise AuditIntegrityError("provider budget requires exact snapshot")
        self._snapshot = snapshot

    def snapshot(self) -> CommonProviderCostSnapshot:
        return self._snapshot

    def validate_binding(self, *, session_id: str, request_id: str, lifecycle_version: int) -> None:
        s = self._snapshot
        if (s.session_id, s.request_id, s.lifecycle_version) != (session_id, request_id, lifecycle_version):
            raise AuditIntegrityError("provider policy changed logical request or lifecycle")

    def require_supported_configuration(self) -> None:
        p = self._snapshot.policy
        if p is not None and p.repair_budget != 2:
            raise CommonProviderCostRefused("REPAIR_POLICY_UNSUPPORTED")

    def require_output(self) -> None:
        s = self._snapshot
        if s.policy is None:
            return
        if s.physical_calls != len(s.charges):
            raise AuditIntegrityError("provider output lacks settled actual calls")
        code = s.refusal_code
        if code is None and any(c.response_accountable and c.cost is None for c in s.charges):
            code = "COST_UNAVAILABLE"
        if code is None and s.validated_cost > s.policy.max_cumulative_provider_cost:
            code = "COST_CAP_EXCEEDED"
        if code is not None:
            call_id = s.charges[-1].call_id if s.charges else None
            raise CommonProviderCostRefused(code, call_id=call_id)

    def require_end_headroom(self) -> None:
        self.require_output()
        s = self._snapshot
        if s.policy is not None and not s.cost_known:
            raise CommonProviderCostRefused("COST_UNAVAILABLE")
        if s.policy is not None and s.validated_cost >= s.policy.max_cumulative_provider_cost:
            raise CommonProviderCostRefused("COST_CAP_EXCEEDED")

    def require_dispatch(self) -> None:
        self.require_supported_configuration()
        self.require_output()
        s = self._snapshot
        if s.policy is not None and s.physical_calls >= s.policy.max_provider_calls:
            raise CommonProviderCostRefused("PROVIDER_CALLS_EXHAUSTED")

    def prepare_request(self, kwargs: CommonProviderCostRequest) -> None:
        self.require_dispatch()
        p = self._snapshot.policy
        if p is None:
            return
        wire = {"messages": kwargs["messages"], "tools": kwargs["tools"] if "tools" in kwargs else []}
        if len(canonical_json(wire).encode("utf-8")) > p.max_request_bytes:
            raise CommonProviderCostRefused("REQUEST_BYTES_EXHAUSTED")
        existing = kwargs["max_tokens"] if "max_tokens" in kwargs else p.max_completion_tokens
        if type(existing) is not int or existing < 1:
            raise AuditIntegrityError("provider request has invalid owned completion allowance")
        kwargs["max_tokens"] = min(existing, p.max_completion_tokens)

    def dispatched(self) -> None:
        self.require_dispatch()
        self._snapshot = replace(self._snapshot, physical_calls=self._snapshot.physical_calls + 1)

    def consume_settled(self, call: ComposerLLMCall, *, response_accountable: bool) -> None:
        if type(call) is not ComposerLLMCall or call.call_id is None:
            raise AuditIntegrityError("cost accounting requires actual complete call")
        s = self._snapshot
        if call.call_id in {c.call_id for c in s.charges}:
            raise AuditIntegrityError("physical provider cost was charged twice")
        cost = None if call.provider_cost is None else Decimal(str(call.provider_cost))
        charge = CommonProviderCostCharge(call.call_id, cost, call.provider_cost_source, response_accountable)
        charges = (*s.charges, charge)
        code = s.refusal_code
        if code is None and response_accountable:
            if cost is None:
                code = "COST_UNAVAILABLE"
            elif s.policy is not None and call.completion_tokens is None:
                code = "MALFORMED_RESPONSE"
            elif s.policy is not None and call.completion_tokens is not None and call.completion_tokens > s.policy.max_completion_tokens:
                code = "COMPLETION_TOKENS_EXCEEDED"
            elif s.policy is not None and s.validated_cost + cost > s.policy.max_cumulative_provider_cost:
                code = "COST_CAP_EXCEEDED"
            elif s.policy is not None and call.finish_reason == "length":
                code = "RESPONSE_TRUNCATED"
        self._snapshot = replace(s, charges=charges, refusal_code=code)


def capture_common_provider_cost(
    *,
    session_id: str,
    request_id: str,
    lifecycle_version: Literal[1, 2],
    initially_empty: bool,
    settings: ComposerSettings,
) -> CommonProviderCostBudget:
    """Capture the supported legacy policy once for this logical request."""
    if lifecycle_version == 1 and initially_empty:
        policy = CommonProviderCostPolicy(
            max_provider_calls=settings.composer_planner_max_provider_calls,
            max_request_bytes=settings.composer_planner_max_request_bytes,
            max_completion_tokens=settings.composer_planner_max_completion_tokens,
            max_cumulative_provider_cost=settings.composer_planner_max_cumulative_provider_cost,
            repair_budget=settings.composer_planner_repair_budget,
        )
        snapshot = CommonProviderCostSnapshot(session_id, request_id, lifecycle_version, "legacy_empty_v1", policy)
    else:
        snapshot = CommonProviderCostSnapshot(session_id, request_id, lifecycle_version, "not_applicable", None)
    return CommonProviderCostBudget(snapshot)
