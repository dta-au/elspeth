# Composer Common Provider Cost Preservation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve the formerly eligible Web v1 request's configured physical-call, exact-request-byte, completion-token and monetary continuation safeguards on the common Composer path before H2.3 retires `PlanningApplication`.

**Architecture:** Capture policy eligibility once from the original owned request and initial graph. Carry one nominal request owner through all common primary and Advisor calls, account actual settled provider records, and refuse further output consumption after a monetary accounting failure. Keep token quota, required-work settlement, tool audit and graph publication under their existing owners.

**Tech Stack:** Python 3.12+, dataclasses, Decimal, existing Pydantic continuation codecs, LiteLLM boundary doubles, pytest, SQLite and PostgreSQL session persistence.

---

## Review binding and closed scope decisions

This is an implementation plan, not executed code or test evidence. Source was inspected at staged tree `37e0b0a4e767dd18c18aa7b3f15a965ca4ff43b5`, HEAD `51bb8131723baa822c1ce65ce1a981722d6c90f6`. The only file written for this deliverable is this plan. `git check-ignore -v` for this path returned no output and exit 1 before creation. Read actual `AGENTS.md` and `CONTRIBUTING.md` before execution; root retains the integration index. Coordinate shared `service.py` and `composer_turn.py` edits with `2026-10-22-composer-common-mutation-authority.md`; do not combine monetary and mutation authority into one capability.

The original H2 policy decision remains: one common provider/tool/graph path, no restored empty-graph planner route, no tutorial/wording classifier, no server-authored pipeline. The following differences concern policy eligibility only.

1. **Existing lifecycle v1:** eligible only when the original graph is structurally empty and the request has the actual owned session service, session ID and persisted user-message ID. Capture once before covered provider work. Existing-graph v1, malformed-head recovery, title generation, diagnostics, boot probes and direct-library planner calls remain outside this Web policy. Do not recalculate eligibility after a tool creates or clears a graph.
2. **Covered roles within an eligible v1 request:** primary and ordinary Advisor physical calls, including EARLY, END and manual advice. They share one cumulative total. This is an explicit narrow role extension when the common loop performs work previously done by private primary/hatch/decline-END. Old code did not charge ordinary EARLY to this planner policy. TITLE remains excluded.
3. **New lifecycle v2:** legacy planner caps are explicitly **not applicable**, as selected by `docs/design/composer-continuation-plan.md:482` in planning commit `170ec0ba3f801034b774b31c2193f897c7d3993d`. Its lines 466–470 preserve actual accounting and applicable request policies; they do not activate a new 75-call/$5 cap on v2. Lifecycle is immutable per logical request. Never convert an active v1 request into v2 to reset an applicable policy. The future frame must retain a nominal not-applicable discriminator and actual accounting facts, including unknown cost. Existing common Advisor counters, token/storage quotas and admission policies remain request-wide across v2 segments.
4. **Repair choreography:** retire the private facade's candidate-rejection/hatch algorithm; retain direct-library repair semantics. `composer_planner_repair_budget=2` is a legacy-facade compatibility default, not an alias for common `_MAX_REPAIR_TURNS`. An eligible v1 request configured with another value refuses before covered provider admission, with explicit supported-configuration guidance. Existing-graph and v2 requests do not gain this legacy restriction.
5. **Cost is a post-call continuation policy, not a hard pre-spend cap.** One physical call can exceed the cap. Legacy primary/hatch rejects `total > cap`; zero and equality do not forbid the first/next primary call. Legacy decline-END additionally requires strict positive headroom before its call. Preserve that END distinction in the common END checkpoint. Unknown cost is never zero. Do not claim token quotas substitute for currency accounting.

## Source contract and counterevidence

All paths below are repository-relative and line numbers refer to the bound staged source unless explicitly marked HEAD.

| Fact | Actual owner and evidence |
| --- | --- |
| Former activation | HEAD `src/elspeth/web/composer/service.py:1234–1245`: structurally empty plus service/session/user-message IDs. `no_tool_policy.py:801–803` defines emptiness as no sources, nodes, edges or outputs. |
| Configured defaults | `src/elspeth/web/config.py:372–380`: 75 physical calls, 2 MiB exact request, 16,384 completion tokens, Decimal 5.00 cumulative cost, repair 2. `composer/protocol.py:1671–1684` exposes these settings. There is no separate Web `PlannerSettings`; `PlannerModelConfig` and `PlannerBudgetPolicy` are library types. |
| Former Web mapping | `planning_application.py:1114–1175` passes those settings to `plan_pipeline`, with the same provider owner. |
| Physical/request bounds | `pipeline_planner.py:3631–3633` measures canonical UTF-8 `{messages, tools}` after cache markers; `3735–3749` counts every physical attempt and requests `max_tokens`; SDK retries are disabled. |
| Post-call refusal | `pipeline_planner.py:3995–4026`: unknown cost first, missing completion usage, completion overage, then cumulative cost `>`; actual response audit precedes refusal. |
| END headroom | `PlannerContinuationBudget` at `pipeline_planner.py:719–809`: same original request identity, reservations, actual call IDs and total; strict `<` before END, `<=` for publication. `planning_application.py:916–974` passes it to decline-END and withholds publication if unavailable. |
| Repair difference | `pipeline_planner.py:4294–4339, 4433–4447, 4570, 4652–4669, 4783`: rejected candidate/truncation/terminal-batch shapes consume repair count; `> repair_budget` gets one configured hatch. Prose nudges are separate. `composition_completion.py:167,1076,1378,1591` counts different common finalization/preflight/interpretation repairs. Equal defaults do not establish equivalent policies. |
| Current common gap | `provider_gateway.py:414–448` has no planner completion bound; `747–963` records actual cost without monetary refusal. `service.py:2528` consumes the common completion and `2676` enters tool dispatch. Neither common turn counters nor Advisor call counters count all physical retries. |
| Real pricing fallback | `core/llm_pricing.py:69–100,414–434,530–542`: reported cost first, missing cost may use known catalog pricing and actual token counts; malformed explicit cost remains unavailable. `llm_response_parsing.py:574–621,694–780` retains cost/provenance. Preserve this parser; never synthesize a zero. |
| Quota counterevidence | `coordination/quota_authority.py:197–256,414–493` settles token ledger, not currency. `chargeable_admission_authority.py:127–190` refuses missing token accounting and exhausted token quotas. Known tokens with unavailable cost can pass. |
| Correct custody boundary | `provider_quota.py:222–327,343–358`: real attempt admission, SDK entry, terminal audit and required settlement; failures/cancellation preserve exact outcomes. Owner is request-local, gateway is shared. |
| Full audit is separate | `sessions/composer_turn.py:1750–1786` assumes old facade made LLM sidecars durable. `_helpers.py:2165–2270` is the real required sidecar writer. P4 `turn_audit.py` publishes tool/graph evidence; it does not replace the final LLM sidecar drain. |
| Public compatibility | `_helpers.py:3064–3071,3130–3149`: `COST_UNAVAILABLE` maps to 503 `cost_unavailable`; `COST_CAP_EXCEEDED` falls through to 500 `operation_failed`. Preserve both mappings. |

## File map and order

| File | Responsibility |
| --- | --- |
| Create `src/elspeth/web/composer/provider_cost.py` | Request identity, immutable policy/snapshot, actual charges and sticky refusal. No SQL or provider calls. |
| Modify `provider_quota.py` | Owner binding, per-custody policy sharing, account only after actual required settlement. |
| Modify `provider_gateway.py` | Exact detached wire request byte/token enforcement before SDK; existing guard stays in place. |
| Modify `service.py`, `advisor_checkpoint.py`, `tool_batch.py` | Initial capture for direct common callers; END headroom; carry refusal without classifying it as successful advice or plugin crash. |
| Modify `sessions/composer_turn.py`, `sessions/routes/_helpers.py` | Initial owned Web capture and complete required sidecar drain before safe public refusal. |
| Modify `web/config.py` | Accurate compatibility comments for retained settings. |
| Modify `contracts/composer_llm_audit.py` and its constructor tests | Admit truthful common requested-completion evidence while preserving complete planner-specific evidence. |
| Modify `composer/redaction.py`, `test_redact_tool_call_response.py` and the finite `redaction_policy_snapshot.json` | Retain the Advisor's closed policy-refusal metadata without admitting external text or arbitrary codes. |
| Create `tests/unit/web/composer/test_provider_cost.py` | Decimal boundaries, request binding, unknown/duplicate charges, cold snapshot and policy scope. |
| Modify `tests/unit/web/composer/test_provider_quota.py` | Real custody/ledger and mixed-role accounting, no extra SDK entry. |
| Modify `tests/integration/web/composer/test_freeform_planner_failure_translation.py` | Actual common primary/END SDK and HTTP/audit assertions; no legacy proposal tool dialect. |
| Future C1/F8/F9/F15/F17/F36 frame files | Explicit not-applicable v2 snapshot, actual charges, no reset or cross-lifecycle conversion. These are amendments to planned files, not claims they exist. |

Implement Tasks 1–6 before H2.3. Task 7 is a required amendment when the approved continuation frames are implemented, before v2 admission can be enabled. No new database columns or migrations are necessary for v1; existing settlement and sidecar write ordering changes still require the PostgreSQL affected gate.

## Task 1 — Nominal request policy and truthful charges

For red-first execution, add 1.2's test file, run 1.3 before adding 1.1's module, then add 1.1 and rerun 1.3. The dependency order is explicit even though the complete module is presented first for review.

- [ ] **1.1 Add the complete module below.** Keep it independent of `pipeline_planner.py` and provider custody to avoid import cycles. `CommonProviderCostRefused` deliberately shares the existing safe-code DTO shape without pretending to be the private planner.

```python
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
        if self.cost is not None and (
            type(self.cost) is not Decimal or not self.cost.is_finite() or self.cost < 0
        ):
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
            None, "COST_UNAVAILABLE", "COST_CAP_EXCEEDED", "MALFORMED_RESPONSE",
            "COMPLETION_TOKENS_EXCEEDED", "RESPONSE_TRUNCATED",
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
    *, session_id: str, request_id: str, lifecycle_version: Literal[1, 2],
    initially_empty: bool, settings: ComposerSettings,
) -> CommonProviderCostBudget:
    eligible = lifecycle_version == 1 and initially_empty
    policy = CommonProviderCostPolicy(
        settings.composer_planner_max_provider_calls,
        settings.composer_planner_max_request_bytes,
        settings.composer_planner_max_completion_tokens,
        settings.composer_planner_max_cumulative_provider_cost,
        settings.composer_planner_repair_budget,
    ) if eligible else None
    return CommonProviderCostBudget(CommonProviderCostSnapshot(
        session_id, request_id, lifecycle_version,
        "legacy_empty_v1" if eligible else "not_applicable", policy,
    ))
```

The known subtotal is a sum of known charges, not a claim the request total is known. A snapshot with an unknown charge has `cost_known=False` forever. The legacy primary validated accumulator excludes failed SDK attempts: `pipeline_planner.py:3645–3648,3862–3890` retains their unknown accounting while permitting the configured transient retry; only the actual response branch reaches `3995–4026`. `4399–4400` carries validated cost and the separate unknown flag into END. Therefore failed SDK unknowns do not immediately become a public cost-only 503 or prevent primary retries; they do prevent END headroom. Actual response metadata, including malformed semantic output with admitted metadata, is response-accountable. Credential/integrity failures are not reclassified based on status names. Calls that never entered the SDK do not call `dispatched()` or `consume_settled()`.

- [ ] **1.2 Add complete focused tests in `tests/unit/web/composer/test_provider_cost.py`.** These test the policy independently of transport; Task 5 adds real physical/SQL checks.

```python
from dataclasses import replace
from decimal import Decimal

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_cost import (
    CommonProviderCostBudget, CommonProviderCostCharge,
    CommonProviderCostPolicy, CommonProviderCostRefused, CommonProviderCostSnapshot,
)


def policy(**changes):
    return replace(CommonProviderCostPolicy(75, 2 * 1024 * 1024, 16384, Decimal("5.00"), 2), **changes)


def owner(*, amount="5.00", calls=0, charges=(), code=None, applicable=True, lifecycle=1):
    return CommonProviderCostBudget(CommonProviderCostSnapshot(
        "session", "request", lifecycle,
        "legacy_empty_v1" if applicable else "not_applicable",
        policy(max_cumulative_provider_cost=Decimal(amount)) if applicable else None,
        calls, charges, code,
    ))


def test_primary_equal_boundary_and_end_headroom_are_distinct():
    budget = owner(calls=1, charges=(CommonProviderCostCharge("actual", Decimal("5.00"), "response"),))
    budget.require_dispatch()
    budget.require_output()
    with pytest.raises(CommonProviderCostRefused, match="COST_CAP_EXCEEDED"):
        budget.require_end_headroom()
    owner(amount="0").require_dispatch()


def test_unknown_is_not_zero_and_is_sticky_across_cold_owner():
    original = owner(calls=1, charges=(CommonProviderCostCharge("actual", None, "not_available"),), code="COST_UNAVAILABLE")
    cold = CommonProviderCostBudget(replace(original.snapshot()))
    assert cold.snapshot().known_cost == Decimal("0")
    assert cold.snapshot().cost_known is False
    with pytest.raises(CommonProviderCostRefused, match="COST_UNAVAILABLE"):
        cold.require_dispatch()


def test_v2_retains_unknown_without_activating_legacy_cap():
    budget = owner(applicable=False, lifecycle=2, calls=1,
                   charges=(CommonProviderCostCharge("actual", None, "not_available"),), code="COST_UNAVAILABLE")
    budget.require_output()
    budget.require_end_headroom()
    budget.require_dispatch()
    assert budget.snapshot().cost_known is False
    with pytest.raises(ValueError, match="cannot activate"):
        owner(lifecycle=2)
    with pytest.raises(AuditIntegrityError, match="lifecycle"):
        budget.validate_binding(session_id="session", request_id="request", lifecycle_version=1)


def test_request_limit_measures_utf8_bytes_and_preserves_lower_advisor_limit():
    from elspeth.core.canonical import canonical_json
    request = {"messages": [{"role": "user", "content": "界"}], "tools": []}
    size = len(canonical_json(request).encode("utf-8"))
    for limit, refused in ((size, False), (size - 1, True)):
        budget = CommonProviderCostBudget(CommonProviderCostSnapshot(
            "session", "request", 1, "legacy_empty_v1", policy(max_request_bytes=limit)))
        wire = {**request, "max_tokens": 100}
        if refused:
            with pytest.raises(CommonProviderCostRefused, match="REQUEST_BYTES_EXHAUSTED"):
                budget.prepare_request(wire)
        else:
            budget.prepare_request(wire)
            assert wire["max_tokens"] == 100


def test_nondefault_legacy_repair_configuration_refuses_before_dispatch():
    budget = CommonProviderCostBudget(CommonProviderCostSnapshot(
        "session", "request", 1, "legacy_empty_v1", policy(repair_budget=3)))
    with pytest.raises(CommonProviderCostRefused, match="REPAIR_POLICY_UNSUPPORTED"):
        budget.require_dispatch()
    assert budget.snapshot().physical_calls == 0
    assert budget.snapshot().charges == ()


def test_duplicate_or_unbacked_snapshot_charge_refuses():
    charge = CommonProviderCostCharge("actual", Decimal("0.01"), "response")
    with pytest.raises(ValueError, match="distinct actual calls"):
        owner(calls=1, charges=(charge, charge))
    with pytest.raises(ValueError, match="distinct actual calls"):
        owner(calls=0, charges=(charge,))


def test_capture_keeps_existing_graph_and_v2_outside_legacy_policy(tmp_path):
    from elspeth.web.composer.provider_cost import capture_common_provider_cost
    from elspeth.web.config import WebSettings

    settings = WebSettings(
        data_dir=tmp_path, composer_model="test/planner", composer_boot_probe_enabled=False,
        composer_max_composition_turns=3, composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0, composer_rate_limit_per_minute=10,
        composer_planner_repair_budget=3, shareable_link_signing_key=b"\x00" * 32,
    )
    for lifecycle, empty in ((1, False), (2, False), (2, True)):
        budget = capture_common_provider_cost(
            session_id="session", request_id="request", lifecycle_version=lifecycle,
            initially_empty=empty, settings=settings,
        )
        assert budget.snapshot().policy is None
        assert budget.snapshot().applicability == "not_applicable"
        budget.require_dispatch()
    legacy = capture_common_provider_cost(
        session_id="session", request_id="request", lifecycle_version=1,
        initially_empty=True, settings=settings,
    )
    assert legacy.snapshot().applicability == "legacy_empty_v1"
    with pytest.raises(CommonProviderCostRefused, match="REPAIR_POLICY_UNSUPPORTED"):
        legacy.require_dispatch()


@pytest.mark.parametrize(
    "invalid_lifecycle",
    (pytest.param(True, id="bool-is-not-an-exact-int"),
     pytest.param(1.0, id="float-is-not-an-exact-int")),
)
def test_capture_rejects_non_exact_lifecycle_without_coercion(closed_local_settings, invalid_lifecycle: object):
    from elspeth.web.composer.provider_cost import capture_common_provider_cost

    with pytest.raises(ValueError, match="unsupported immutable lifecycle"):
        capture_common_provider_cost(
            session_id="session", request_id="request",
            lifecycle_version=invalid_lifecycle, initially_empty=True,
            settings=closed_local_settings,
        )
```

- [ ] **1.3 Run the focused file with the logging harness in Task 6; first run before 1.1 must fail at the missing module.** After implementation it must exit 0. No provider credentials or real endpoints are used.

## Task 2 — One custody owner, actual settlement before refusal

- [ ] **2.1 Add imports and the following owner members in `provider_quota.py`.** Initialize `_cost_budget` in the existing constructor. `mint()` passes it only for PRIMARY/ADVISOR; PLANNER retains its existing library policy and TITLE remains outside this policy.

```python
from elspeth.web.composer.provider_cost import CommonProviderCostBudget, CommonProviderCostRefused, CommonProviderCostRequest
```

```python
        self._cost_budget: CommonProviderCostBudget | None = None
```

```python
    @property
    def cost_budget(self) -> CommonProviderCostBudget | None:
        return self._cost_budget

    def bind_cost_budget(self, budget: CommonProviderCostBudget) -> None:
        if type(budget) is not CommonProviderCostBudget:
            raise AuditIntegrityError("provider owner requires nominal request policy")
        if self._cost_budget is not None:
            if self._cost_budget is not budget:
                raise AuditIntegrityError("provider owner cannot replace request policy")
            return
        if self._counters[ProviderInvocationFamily.PRIMARY] or self._counters[ProviderInvocationFamily.ADVISOR]:
            raise AuditIntegrityError("provider policy cannot start after covered invocation")
        self._cost_budget = budget
```

Replace `mint()`'s final return with:

```python
        return ProviderCallCustody(
            service=self.service,
            context=binding.coordinator.authority.context,
            required_work=binding,
            cost_budget=self.cost_budget if family in (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR) else None,
        )
```

- [ ] **2.2 Add `_cost_budget` and `_cost_response_accountable` to `ProviderCallCustody.__slots__`, add the keyword parameter, assignment and methods below.** Preserve its existing exact required-work/context validation.

```python
        cost_budget: CommonProviderCostBudget | None = None,
```

```python
        if cost_budget is not None and type(cost_budget) is not CommonProviderCostBudget:
            raise AuditIntegrityError("provider custody received foreign cost policy")
        self._cost_budget = cost_budget
        self._cost_response_accountable = False
```

```python
    def prepare_cost_request(self, kwargs: CommonProviderCostRequest) -> None:
        if self._cost_budget is not None:
            self._cost_budget.prepare_request(kwargs)

    def require_cost_dispatch(self) -> None:
        if self._cost_budget is not None:
            self._cost_budget.require_dispatch()

    def require_cost_output(self) -> None:
        if self._cost_budget is not None:
            self._cost_budget.require_output()

    def mark_cost_response_accountable(self, *, admitted_metadata: bool) -> None:
        if type(admitted_metadata) is not bool or self.attempt is None or not self._sdk_entered:
            raise AuditIntegrityError("response accountability requires actual dispatched custody")
        self._cost_response_accountable = admitted_metadata
```

In `admit()`, immediately after `await self.settle()`, add:

```python
            self.require_cost_dispatch()
            self._cost_response_accountable = False
```

In `mark_sdk_entered()`, after its existing integrity check and before setting `_sdk_entered=True`, add:

```python
        if self._cost_budget is not None:
            self._cost_budget.dispatched()
```

In `settle()`, after successful SQL/projection completion and before clearing `self.call`, add:

```python
        if self._cost_budget is not None:
            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)
```

Do not charge before `finish_provider_attempt` completes. Do not clear custody on a failed required settlement. Deferred cancellations remain raised by the original code after actual accounting.

- [ ] **2.3 Replace `provider_call_scope` with this complete body.** A successful provider response must pass the settled cost policy before its caller can consume it. A provider/body fault remains the original member of a compound outcome; cost refusal must not disguise a cancellation or first-party fault. Existing required-settlement error grouping remains intact.

```python
@asynccontextmanager
async def provider_call_scope(custody: ProviderCallCustody) -> AsyncIterator[ProviderCallCustody]:
    if type(custody) is not ProviderCallCustody:
        raise AuditIntegrityError("Provider scope requires exact explicit custody")
    body: BaseException | None = None
    try:
        yield custody
    except BaseException as failure:
        body = failure
        raise
    finally:
        try:
            await custody.settle()
        except BaseException as settlement:
            if body is not None and settlement is not body:
                raise BaseExceptionGroup("Provider body and required settlement outcomes", [body, settlement]) from None
            raise
        try:
            custody.require_cost_output()
        except CommonProviderCostRefused as refusal:
            from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError

            if isinstance(body, _MalformedLLMResponseError):
                raise refusal from body
            if body is not None:
                raise BaseExceptionGroup("Provider body and monetary continuation refusal", [body, refusal]) from None
            raise
```

The public 503 cost-only case is an actual metadata-admitted physical response with unavailable cost. Old cost admission precedes semantic parsing, so malformed semantic output with unavailable cost retains that precedence and the malformed original as its cause. Failed SDK calls have `response_accountable=False`: their API/timeout/cancel outcome propagates unchanged, and configured transient retries remain possible within physical/deadline limits. Required settlement failures preserve the original compound-outcome protocol.

- [ ] **2.4 At `provider_gateway._litellm_acompletion`, after detached prompt-guard evidence is validated and immediately before `enforce_prompt_guard`, add:**

```python
    cost_request: CommonProviderCostRequest = {"messages": kwargs["messages"]}
    if "tools" in kwargs:
        cost_request["tools"] = kwargs["tools"]
    if "max_tokens" in kwargs:
        cost_request["max_tokens"] = kwargs["max_tokens"]
    provider_custody.prepare_cost_request(cost_request)
    if "max_tokens" in cost_request:
        kwargs["max_tokens"] = cost_request["max_tokens"]
```

Import `CommonProviderCostRequest` into `provider_gateway.py`. This small policy view projects the actual detached field values by reference; it neither claims the entire open SDK keyword shape nor rebuilds message/tool values. Preserve absent `tools` and `max_tokens`, including explicit invalid values for the policy to reject. Copy only the resulting completion allowance back to the actual SDK kwargs. The wire `messages`/`tools` are complete and detached at this point. Request preparation includes actual cache markers. No truncation is permitted to meet the byte bound. Existing credential checks and Prompt Shield remain before provider admission. `max_tokens` is transport metadata and must appear in retained actual request evidence; preserve the existing capture mechanism, and extend audit-record construction as in Task 3.5.

- [ ] **2.5 Replace the existing callback block after `admit()` with the following.** This also rechecks the same request policy immediately before SDK entry, disposing an admitted but undispatched attempt truthfully if the recheck refuses.

```python
    try:
        provider_custody.require_cost_dispatch()
        if on_provider_dispatch is not None:
            on_provider_dispatch()
    except BaseException as callback_failure:
        try:
            cancellations = await provider_custody.cancel_before_sdk(model=kwargs["model"])
        except BaseException as disposition_failure:
            raise BaseExceptionGroup(
                "Provider pre-dispatch callback and required disposition outcomes", [callback_failure, disposition_failure]
            ) from None
        if cancellations:
            raise BaseExceptionGroup(
                "Provider pre-dispatch callback and original cancellations", [callback_failure, *cancellations]
            ) from None
        raise
    provider_custody.mark_sdk_entered()
    return await litellm.acompletion(**kwargs)
```

## Task 3 — Capture once, retain public refusal and tool-effect ordering

- [ ] **3.1 Import the factory implemented and tested in Task 1 into both caller modules.** Its type-only `ComposerSettings` dependency and exact `Literal[1, 2]` lifecycle pass-through are defined in Task 1. It captures policy without routing. Today's owned Web caller passes 1; new v2 does not bind a legacy budget.

```python
from elspeth.web.composer.provider_cost import capture_common_provider_cost
```

- [ ] **3.2 In `sessions/composer_turn.py`, after original `turn_user_message_id` is known and before the E14/F-C5 final remaining-time measurement at lines 1403–1415, bind once.** The authority companion's asynchronous preference capture must also precede that measurement. Retain its `remaining <= 0` refusal after every new capture await, whether auto-title is enabled or disabled; do not pass stale `compose_budget_seconds` into provider work. This cost factory is synchronous. Add imports for this factory and `state_is_structurally_empty`. Do not enforce the repair refusal outside the normal public-error handler: binding itself is pure and cannot spend. TITLE remains excluded by `mint()`.

```python
    provider_owner.bind_cost_budget(capture_common_provider_cost(
        session_id=str(session.id),
        request_id=str(turn_user_message_id),
        lifecycle_version=1,
        initially_empty=state is not None and state_is_structurally_empty(state),
        settings=settings,
    ))
```

- [ ] **3.3 In `ComposerServiceImpl.compose`, within the existing main `try` immediately before `_compose_loop`, add the following block.** Its fallback supports direct owned common callers; it must not mint an applicable policy without all former owner/ID conditions. Add imports of `capture_common_provider_cost` and `CommonProviderCostRefused`. The owner validates the original user-message identity before using a previously captured policy.

```python
                if provider_owner is not None and session_id is not None and user_message_id is not None:
                    if provider_owner.cost_budget is None:
                        provider_owner.bind_cost_budget(capture_common_provider_cost(
                            session_id=session_id,
                            request_id=user_message_id,
                            lifecycle_version=1,
                            initially_empty=_state_is_structurally_empty(state),
                            settings=self._settings,
                        ))
                    provider_owner.cost_budget.validate_binding(
                        session_id=session_id, request_id=user_message_id, lifecycle_version=1,
                    )
                    provider_owner.cost_budget.require_supported_configuration()
```

Add this dedicated catch before `except ComposerConvergenceError`:

```python
            except CommonProviderCostRefused as exc:
                attach_llm_calls(exc, recorder)
                raise
            except BaseExceptionGroup as exc:
                attach_llm_calls(exc, recorder)
                raise
```

The attachment contains actual buffered calls only. A pre-dispatch config or byte refusal therefore attaches an empty tuple. Do not infer `llm_calls_durable=True` from recorder attachment alone. The current owned `finish_provider_attempt` already persists actual call sidecars and its ledger atomically; the turn owner's idempotent drain verifies/deduplicates attached calls and completes its observation state before public disposition.

- [ ] **3.4 In `advisor_checkpoint._run_advisor_checkpoint`, directly before each physical call inside the existing attempt loop, enforce END headroom:**

```python
            if phase == "end" and provider_owner is not None and provider_owner.cost_budget is not None:
                try:
                    provider_owner.cost_budget.require_end_headroom()
                except CommonProviderCostRefused:
                    return continuation_withheld()
```

Import `CommonProviderCostRefused` into `advisor_checkpoint.py`. Use the actual local checkpoint phase parameter (`phase`) and insert before reservation/admission of that attempt, after the existing deadline/policy eligibility checks. The existing `continuation_withheld()` is the exact no-attempt END-budget verdict; it does not fabricate a model decision. Do not apply the strict-headroom test to primary or EARLY. Keep direct-library `continuation_budget` checks unchanged. Post-response common cost exceptions are outside the enumerated recoverable provider errors, so rejected output is not consumed or retried.

- [ ] **3.5 Retain requested completion allowance in actual audit.** Add this method to `ProviderCallCustody`; use it at the two common gateway audit builders and the Advisor audit builder as the `max_completion_tokens_requested` argument. It reports the policy ceiling for primary; Advisor already has `max_completion` and uses the minimum explicitly.

First reconcile the existing `ComposerLLMCall` constructor in
`src/elspeth/contracts/composer_llm_audit.py`. Its original all-or-none planner
triad rejects the truthful common completion limit before custody can settle.
Keep the existing exact integer/range and hash checks. Replace only the triad
condition with:

```python
        planner_evidence = (
            self.max_completion_tokens_requested,
            self.planner_policy_hash,
            self.planner_call_ordinal,
        )
        planner_specific = self.planner_policy_hash is not None or self.planner_call_ordinal is not None
        if planner_specific and not all(value is not None for value in planner_evidence):
            raise ValueError("planner request evidence fields must be supplied together")
```

Add these constructor tests to the existing
`tests/unit/contracts/test_composer_llm_audit.py`, using its actual `_make_call`
helper. Run them before the condition change to establish a native assertion
failure, then afterward. Standalone requested completion describes the actual
common request; it must not invent a planner policy or ordinal. Existing default
records and complete planner records retain their bytes and validation.

```python
@pytest.mark.parametrize(
    ("limit", "policy", "ordinal", "accepted"),
    [
        (None, None, None, True),
        (800, None, None, True),
        (None, "a" * 64, None, False),
        (None, None, 1, False),
        (800, "a" * 64, None, False),
        (800, None, 1, False),
        (None, "a" * 64, 1, False),
        (800, "a" * 64, 1, True),
    ],
)
def test_requested_completion_and_planner_evidence(
    limit: int | None, policy: str | None, ordinal: int | None, accepted: bool,
) -> None:
    values = {
        "max_completion_tokens_requested": limit,
        "planner_policy_hash": policy,
        "planner_call_ordinal": ordinal,
    }
    if not accepted:
        with pytest.raises(ValueError, match="planner request evidence fields"):
            _make_call(**values)
        return
    call = _make_call(**values)
    payload = call.to_dict()
    assert payload["max_completion_tokens_requested"] == limit
    assert payload["planner_policy_hash"] == policy
    assert payload["planner_call_ordinal"] == ordinal


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_standalone_requested_completion_retains_exact_positive_integer_validation(limit: object) -> None:
    with pytest.raises((TypeError, ValueError), match="max_completion_tokens_requested"):
        _make_call(max_completion_tokens_requested=limit)
```

```python
    def cost_completion_limit(self) -> int | None:
        if self._cost_budget is None:
            return None
        policy = self._cost_budget.snapshot().policy
        return policy.max_completion_tokens if policy is not None else None
```

At each `build_llm_call_record` in `_call_llm_with_audit_body` and `_call_text_llm_with_audit_body` add:

```python
                            max_completion_tokens_requested=(
                                provider_custody.cost_completion_limit() if provider_custody is not None else None
                            ),
```

In `_call_advisor_with_audit_body`, immediately after its existing `max_completion` calculation and before building `kwargs`, add:

```python
        cost_limit = provider_custody.cost_completion_limit() if provider_custody is not None else None
        if cost_limit is not None:
            max_completion = min(max_completion, cost_limit)
```

Its audit builder receives:

```python
                            max_completion_tokens_requested=max_completion,
```

At all three actual audit builders, immediately before `provider_custody.retain_audit(call)` in the existing non-null custody branch, add:

```python
                            provider_custody.mark_cost_response_accountable(
                                admitted_metadata=response_metadata is not None,
                            )
```

`response_metadata` is the actual admitted provider record, including the owned metadata retained by `_MalformedLLMResponseError`. SDK API/timeout/cancel and credential-material error paths have no admitted metadata. Do not derive this flag from `call.status`, field names, elapsed time, or the presence of a ledger row. This marker must also be inserted in O3's future local terminal builders only if an applicable policy ever exists there; baseline v2 binds none and needs no second balance.

Do not overwrite actual `completion_tokens`, `provider_cost`, `provider_cost_source` or physical request IDs with these requested limits.

- [ ] **3.6 Preserve manual Advisor refusal as a semantic policy refusal.** Add `CommonProviderCostRefused` import to `tool_batch.py`. In the existing `_AdvisorFirstPartyFailure` branch, before its `PromptGuardRefused` branch, insert this complete arm:

```python
                if isinstance(first_party_exc, CommonProviderCostRefused):
                    refusal_payload = {
                        "success": False,
                        "status": "PROVIDER_POLICY_REFUSED",
                        "code": first_party_exc.code,
                        "outbound_call_made": first_party_exc.call_id is not None,
                    }
                    recorder.record(finish_success(audit, result_payload=refusal_payload, version_after=state.version))
                    _append_tool_outcome(
                        response=refusal_payload,
                        error_class=None,
                        error_category=None,
                        error_message=None,
                        post_version=state.version,
                    )
                    advisor_failure = first_party_exc
                    break
```

This closes the *tool's* semantic refusal; the actual provider call keeps its true status/cost. `service.py` already skips EARLY when `advisor_failure` exists, publishes the completed P3 prefix through P4, then re-raises the original failure before P5/another model turn. EARLY failures likewise run mandatory P4 and then unwind. Do not block P4 audit publication of earlier real effects, do not invent a rejected auto proposal and do not assert the entire request had no effects. This policy prevents **new semantic effects after refusal**, while permitting required bookkeeping of effects already performed.

The actual persisted tool result also passes through `redaction.py`. Add the fixed string `PROVIDER_POLICY_REFUSED` to `_SAFE_PUBLIC_RESPONSE_TEXT_BY_FIELD["status"]`; add only `code` and `outbound_call_made` to the `request_advisor_hint` manifest's `known_response_keys`. Define this closed vocabulary beside the existing response field policies:

```python
_SAFE_ADVISOR_POLICY_REFUSAL_CODES = frozenset(
    {
        "REPAIR_POLICY_UNSUPPORTED",
        "COST_UNAVAILABLE",
        "COST_CAP_EXCEEDED",
        "PROVIDER_CALLS_EXHAUSTED",
        "REQUEST_BYTES_EXHAUSTED",
        "MALFORMED_RESPONSE",
        "COMPLETION_TOKENS_EXCEEDED",
        "RESPONSE_TRUNCATED",
    }
)
```

Immediately after the existing `success` arm in `_redact_declarative_known_response_value`, add:

```python
    if tool_name == "request_advisor_hint":
        if field_name == "code":
            return (
                value
                if isinstance(value, str) and value in _SAFE_ADVISOR_POLICY_REFUSAL_CODES
                else _summarize_external_response_value(value)
            )
        if field_name == "outbound_call_made":
            return value if type(value) is bool else _summarize_external_response_value(value)
```

In `test_redact_tool_call_response.py`, test each of the eight fixed codes, both exact Boolean values, unknown/private strings, non-Boolean outbound values and another tool attempting these keys. The existing sensitive/unknown projection controls remain required. Regenerate only the measured `request_advisor_hint` manifest entry in `redaction_policy_snapshot.json`; its five sensitive paths remain unchanged. The public manual-Advisor test must inspect the actual SQL tool result and assert the complete four-field refusal payload. A fabricated response dictionary alone does not verify persistence.

- [ ] **3.7 In the public catch, change `except PipelinePlannerError as exc` to `except (PipelinePlannerError, CommonProviderCostRefused) as exc`.** Import the new exception. Immediately after `observation.pending_exception_llm_calls = _llm_calls_from_exception(exc)`, insert:

```python
            if isinstance(exc, CommonProviderCostRefused) and observation.pending_exception_llm_calls:
                await _required_audit(
                    observation,
                    _persist_llm_calls(
                        service,
                        session.id,
                        observation.pending_exception_llm_calls,
                        compose_base_state_id,
                        plugin_crash_pending=False,
                        session_operation_context=lease.context,
                        required_audit=True,
                        required_work=observation.required_work,
                    ),
                )
                observation.audit_cohort_durable = True
                observation.pending_exception_llm_calls = ()
```

Then retain the existing progress and `_handle_planner_failure` disposition. Required settlement already commits common LLM sidecars and the token ledger together. The explicit turn drain is an idempotent backstop and observation completion, followed by separate safe failure disposition and the public error. A required settlement or sidecar failure wins as integrity failure; it cannot produce a falsely complete cost-refusal audit.

- [ ] **3.8 Update `_helpers.py` error annotations and safe mapping.** Import `CommonProviderCostRefused`; use the following exact argument annotation in `planner_failure_is_policy_blocked`, `_freeform_planner_failure_code` and `_handle_planner_failure`:

```python
    exc: PipelinePlannerError | CommonProviderCostRefused,
```

Add this entry to `_FREEFORM_PLANNER_FAILURE_HTTP`:

```python
    "configuration_unsupported": (
        503,
        "The configured legacy planner repair budget is not supported by the common composer. "
        "Use the compatibility default of 2 or the direct planner library with its explicit repair policy.",
    ),
```

Add before the existing cost case in `_freeform_planner_failure_code`:

```python
    if exc.code == "REPAIR_POLICY_UNSUPPORTED":
        return "configuration_unsupported"
```

Add to `_FREEFORM_PLANNER_PROGRESS_REASONS`:

```python
    "REPAIR_POLICY_UNSUPPORTED": "service_setup_failed",
```

Replace the misleading sidecar-durability paragraph in `_handle_planner_failure`'s docstring with:

```text
The caller must complete required LLM sidecar persistence before this
separate disposition. The direct planner facade already drains its own
calls; common provider policy refusal uses the turn owner's required drain.
An empty call tuple is valid only when no covered SDK attempt occurred.
```

Replace the budget comment in `WebSettings` with:

```python
    # Direct planner policy and legacy eligible Web v1 compatibility bounds.
    # Common v1 preserves physical calls, exact request bytes, completion and
    # post-call cost; v2 has no legacy planner policy. The repair value is
    # facade compatibility only: default 2 does not control common repair;
    # a nondefault value refuses eligible v1 admission before provider work.
```

## Task 4 — Physical and public regressions using real common dialects

- [ ] **4.1 Add the imports and complete tests below to the existing integration module.** They reuse its real FastAPI/operation worker/session fixtures. Only `litellm.acompletion` is scripted. They do not patch provider accounting, common loop, authorization or audit writers. A title supplied at session creation avoids an unrelated title SDK call.

Add this keyword to `_build_app`'s existing keyword-only signature:

```python
    planner_repair_budget: int = 2,
    pricing_model: str | None = None,
```

Add this keyword to its `WebSettings` constructor, before constructing `ComposerServiceImpl`:

```python
        composer_planner_repair_budget=planner_repair_budget,
        composer_pricing_model=pricing_model,
```

```python
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
```

```python
@pytest.mark.parametrize("refused_role", ["primary", "end"])
@pytest.mark.parametrize("kind", ["unknown", "over"])
@pytest.mark.parametrize("malformed_response", [False, True])
def test_common_provider_cost_refuses_actual_role_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refused_role: str, kind: str, malformed_response: bool,
) -> None:
    requests: list[dict[str, Any]] = []
    candidate = "Please provide the CSV column names before I build the pipeline."

    async def sdk(**kwargs: Any) -> _Response:
        requests.append(deepcopy(kwargs))
        role = "end" if "response_format" in kwargs else "primary"
        assert len(requests) <= (1 if refused_role == "primary" else 2)
        usage: dict[str, object] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}
        if role == refused_role:
            if kind == "unknown":
                usage["cost"] = "malformed-explicit-cost"
            else:
                usage["cost"] = 6.0
        if role == "primary":
            names = {tool["function"]["name"] for tool in kwargs["tools"]}
            assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
            content = candidate
        else:
            review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
            assert review["candidate_reply"] == candidate
            content = json.dumps({
                "verdict": "CLEAN", "category": "other", "steps": [],
                "findings": "", "note": None, "publication_verdict": "ADMIT",
            })
        choices = [] if role == refused_role and malformed_response else [_Choice(_Message(content))]
        return _Response(choices, usage, model="unknown-cost-model", id=f"physical-{len(requests)}")

    client, engine, _sessions = _build_app(tmp_path, monkeypatch, sdk)
    session_id = client.post("/api/sessions", json={"title": "cost policy"}).json()["id"]
    settled = settle_sync(client, client.app, path=f"/api/sessions/{session_id}/messages", body={
        "content": "Build a CSV pipeline; ask me if you need its columns.",
        "operation_id": str(uuid4()), "state_id": None,
    })
    status, payload = settled.error()
    assert status == (503 if kind == "unknown" else 500)
    assert payload["detail"]["planner_code"] == ("COST_UNAVAILABLE" if kind == "unknown" else "COST_CAP_EXCEEDED")
    assert len(requests) == (1 if refused_role == "primary" else 2)
    audits = [row.tool_calls[0]["call"] for row in _llm_audit_rows(engine)]
    assert len(audits) == len(requests)
    assert len({item["call_id"] for item in audits}) == len(audits)
    refused_call = next(call for call in audits if call["provider_request_id"] == f"physical-{len(requests)}")
    assert refused_call["provider_cost"] == (None if kind == "unknown" else 6.0)
    if kind == "unknown":
        assert refused_call["provider_cost_source"] == "not_available"
    with engine.connect() as conn:
        attempts = list(conn.execute(select(quota_provider_attempts_table)).mappings())
        ledger = list(conn.execute(select(token_usage_ledger_table)).mappings())
        states = list(conn.execute(select(composition_states_table)).mappings())
        proposals = list(conn.execute(select(composition_proposals_table)).mappings())
        visible = list(conn.execute(select(chat_messages_table).where(chat_messages_table.c.role == "assistant")).mappings())
    assert len(attempts) == len(ledger) == len(requests)
    assert states == [] and proposals == []
    assert all(row["content"] != candidate for row in visible)
    assert len(_disposition_rows(engine)) == 1


@pytest.mark.parametrize("repair_budget", [0, 1, 3])
def test_common_nondefault_legacy_repair_refuses_without_sdk_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repair_budget: int,
) -> None:
    async def forbidden_sdk(**kwargs: Any) -> _Response:
        raise AssertionError("unsupported policy entered SDK")

    client, engine, _sessions = _build_app(tmp_path, monkeypatch, forbidden_sdk, planner_repair_budget=repair_budget)
    session_id = client.post("/api/sessions", json={"title": "repair policy"}).json()["id"]
    settled = settle_sync(client, client.app, path=f"/api/sessions/{session_id}/messages", body={
        "content": _EMPTY_INTENT, "operation_id": str(uuid4()), "state_id": None,
    })
    status, payload = settled.error()
    assert status == 503
    assert payload["detail"]["failure_code"] == "configuration_unsupported"
    assert payload["detail"]["planner_code"] == "REPAIR_POLICY_UNSUPPORTED"
    assert "default of 2" in payload["detail"]["detail"]
    assert _llm_audit_rows(engine) == []
    with engine.connect() as conn:
        assert list(conn.execute(select(quota_provider_attempts_table))) == []
        assert list(conn.execute(select(token_usage_ledger_table))) == []
    dispositions = _disposition_rows(engine)
    assert len(dispositions) == 1
    assert dispositions[0].tool_calls[0]["planner_code"] == "REPAIR_POLICY_UNSUPPORTED"
```

`llm_call_audit_envelope` owns the actual fields under its `call` member. The explicit malformed cost is deliberate: missing cost alone may legitimately recover through catalog pricing, while malformed reported cost must remain unavailable. Do not change test assertions to a private proposal ID or to the old `emit_pipeline_proposal` dialect. The existing `cost_unavailable` case of `test_send_message_freeform_planner_failure_is_translated` must remain and pass unchanged.

- [ ] **4.2 Add an actual mutation-bearing primary refusal test.** This proves a provider-authored `set_pipeline` call cannot reach effects on unknown cost. It uses the existing provider-authored pipeline fixture, replacing only its obsolete tool name with the declared common tool and a unique physical ID.

```python
def test_common_unknown_cost_blocks_received_set_pipeline_before_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    authored = _valid_pipeline_completion(tmp_path, holder)
    physical = 0

    async def sdk(**kwargs: Any) -> _Response:
        nonlocal physical
        physical += 1
        assert physical == 1
        names = {tool["function"]["name"] for tool in kwargs["tools"]}
        assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
        reply = await authored(**kwargs)
        call = reply.choices[0].message.tool_calls[0]
        call.id = "cost-refused-set-pipeline"
        call.function.name = "set_pipeline"
        reply.usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": "malformed-explicit-cost"}
        return reply

    client, engine, _sessions = _build_app(tmp_path, monkeypatch, sdk)
    holder["id"] = client.post("/api/sessions", json={"title": "no mutation after unknown cost"}).json()["id"]
    settled = settle_sync(client, client.app, path=f"/api/sessions/{holder['id']}/messages", body={
        "content": _EMPTY_INTENT, "operation_id": str(uuid4()), "state_id": None,
    })
    status, payload = settled.error()
    assert status == 503 and payload["detail"]["planner_code"] == "COST_UNAVAILABLE"
    assert physical == 1
    with engine.connect() as conn:
        assert list(conn.execute(select(composition_states_table))) == []
        assert list(conn.execute(select(composition_proposals_table))) == []
        tool_rows = list(conn.execute(select(chat_messages_table).where(chat_messages_table.c.role == "tool")))
    assert tool_rows == []
    assert len(_llm_audit_rows(engine)) == 1
```

- [ ] **4.3 Add the real catalog-recovery positive control.** Use the explicit pricing override before service construction because model/pricing configuration is constructor-cached. This is the same installed catalog fixture as direct-library `test_calculated_azure_cost_enforces_planner_continuation_cap`; it performs no network lookup. Missing cost is recoverable here; the explicit malformed-cost cases in 4.1 remain unavailable.

```python
def test_common_provider_cost_retains_actual_catalog_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm import ModelResponse
    from litellm.types.utils import Usage

    requests: list[dict[str, Any]] = []
    candidate = "Please provide the CSV column names before I build the pipeline."

    async def sdk(**kwargs: Any) -> Any:
        requests.append(deepcopy(kwargs))
        assert len(requests) <= 2
        if "response_format" in kwargs:
            review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
            assert review["candidate_reply"] == candidate
            return _Response([_Choice(_Message(json.dumps({
                "verdict": "CLEAN", "category": "other", "steps": [],
                "findings": "", "note": None, "publication_verdict": "ADMIT",
            })))], {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}, id="catalog-end")
        response = ModelResponse(
            id="catalog-primary", model="private-azure-deployment-2026-07-09",
            usage=Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
            choices=[{"index": 0, "message": {"role": "assistant", "content": candidate}, "finish_reason": "stop"}],
        )
        response._hidden_params = {}
        return response

    client, engine, _sessions = _build_app(
        tmp_path, monkeypatch, sdk, pricing_model="openai/gpt-4o-2024-08-06",
    )
    session_id = client.post("/api/sessions", json={"title": "catalog cost"}).json()["id"]
    settled = settle_sync(client, client.app, path=f"/api/sessions/{session_id}/messages", body={
        "content": "Build a CSV pipeline; ask me if you need its columns.",
        "operation_id": str(uuid4()), "state_id": None,
    })
    settled.result()
    assert len(requests) == 2
    audits = [row.tool_calls[0]["call"] for row in _llm_audit_rows(engine)]
    assert len(audits) == 2 and len({call["call_id"] for call in audits}) == 2
    primary = next(call for call in audits if call["provider_request_id"] == "catalog-primary")
    assert primary["provider_cost"] == pytest.approx(0.00045)
    assert primary["provider_cost_source"] == "litellm.cost_per_token"
    with engine.connect() as conn:
        visible = list(conn.execute(select(chat_messages_table).where(chat_messages_table.c.role == "assistant")).mappings())
        assert len(list(conn.execute(select(token_usage_ledger_table)))) == 2
    assert any(row["content"] == candidate for row in visible)
    assert _disposition_rows(engine) == []
```

- [ ] **4.4 Add the already-committed-prefix regression.** The third actual SDK entry observes the first P4 state already committed, then returns a second mutation with unavailable cost. This distinguishes preserving an earlier committed effect from merely asserting that a failed response caused no new effect. The public failure must not describe the request as having no effect; GET state remains the saved prefix. The final LLM drain includes each actual call once, while P4 is not replayed.

```python
def test_common_provider_cost_refusal_preserves_already_committed_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder: dict[str, str] = {}
    authored = _valid_pipeline_completion(tmp_path, holder)
    requests: list[dict[str, Any]] = []
    observed_saved_ids: list[str] = []

    async def sdk(**kwargs: Any) -> _Response:
        requests.append(deepcopy(kwargs))
        ordinal = len(requests)
        assert ordinal <= 3
        if ordinal == 2:
            assert "response_format" in kwargs
            return _Response([_Choice(_Message(json.dumps({
                "verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None,
            })))], {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}, id="prefix-early")
        assert "response_format" not in kwargs
        names = {tool["function"]["name"] for tool in kwargs["tools"]}
        assert "set_pipeline" in names and "emit_pipeline_proposal" not in names
        if ordinal == 3:
            with engine.connect() as conn:
                saved = list(conn.execute(select(composition_states_table)).mappings())
            assert len(saved) == 1
            observed_saved_ids.append(str(saved[0]["id"]))
        reply = await authored(**kwargs)
        call = reply.choices[0].message.tool_calls[0]
        call.function.name = "set_pipeline"
        call.id = "cost-prefix-saved" if ordinal == 1 else "cost-prefix-refused"
        reply.id = f"prefix-primary-{ordinal}"
        if ordinal == 3:
            reply.usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": "malformed-explicit-cost"}
        return reply

    client, engine, _sessions = _build_app(tmp_path, monkeypatch, sdk)
    holder["id"] = client.post("/api/sessions", json={"title": "preserve saved prefix"}).json()["id"]
    source_dir = tmp_path / "blobs" / holder["id"]
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "input.csv").write_text("name\nAlice\n", encoding="utf-8")
    (tmp_path / "outputs" / holder["id"]).mkdir(parents=True, exist_ok=True)
    settled = settle_sync(client, client.app, path=f"/api/sessions/{holder['id']}/messages", body={
        "content": _EMPTY_INTENT, "operation_id": str(uuid4()), "state_id": None,
    })
    status, payload = settled.error()
    assert status == 503 and payload["detail"]["planner_code"] == "COST_UNAVAILABLE"
    assert len(requests) == 3 and len(observed_saved_ids) == 1
    current = client.get(f"/api/sessions/{holder['id']}/state")
    assert current.status_code == 200
    assert str(current.json()["id"]) == observed_saved_ids[0]
    with engine.connect() as conn:
        states = list(conn.execute(select(composition_states_table)).mappings())
        assert len(states) == 1 and str(states[0]["id"]) == observed_saved_ids[0]
        assert list(conn.execute(select(composition_proposals_table))) == []
        tools = list(conn.execute(select(chat_messages_table).where(chat_messages_table.c.role == "tool")).mappings())
        assert [row["tool_call_id"] for row in tools] == ["cost-prefix-saved"]
        assert len(list(conn.execute(select(token_usage_ledger_table)))) == 3
        assert len(list(conn.execute(select(quota_provider_attempts_table)))) == 3
    audits = [row.tool_calls[0]["call"] for row in _llm_audit_rows(engine)]
    assert len(audits) == 3 and len({call["call_id"] for call in audits}) == 3
    refused = next(call for call in audits if call["provider_request_id"] == "prefix-primary-3")
    assert refused["provider_cost"] is None and refused["provider_cost_source"] == "not_available"
    assert len(_disposition_rows(engine)) == 1
```

## Task 5 — Real custody mixed-role and boundary checks

- [ ] **5.1 Add the following complete test to `test_provider_quota.py`.** Reuse `_quota_service`, `_ledger`, the imported required-work types and `build_llm_call_record`. Add `Decimal` and the four policy types to imports. This reaches the real `_litellm_acompletion`, real admitted custody and real persisted token ledger. Its physical sequence is primary, Advisor (EARLY), Advisor (END); the latter two share the same family by design. The separate HTTP test proves END's structured response parser and publication boundary.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("bad_index", [0, 1, 2])
@pytest.mark.parametrize("bad_cost", [None, 6.0])
async def test_actual_common_roles_share_one_settled_cost_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_index: int, bad_cost: float | None,
) -> None:
    from decimal import Decimal
    from elspeth.web.composer.provider_cost import (
        CommonProviderCostBudget, CommonProviderCostPolicy, CommonProviderCostRefused, CommonProviderCostSnapshot,
    )
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="cost", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60)
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60)
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(
        RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4())))
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    budget = CommonProviderCostBudget(CommonProviderCostSnapshot(
        str(session.id), "original-user-message", 1, "legacy_empty_v1",
        CommonProviderCostPolicy(75, 2097152, 16384, Decimal("5.00"), 2)))
    owner.bind_cost_budget(budget)
    recorder = BufferingRecorder()
    calls: list[dict[str, Any]] = []

    async def sdk(**kwargs: Any) -> SimpleNamespace:
        assert len(_ledger(engine)) == len(calls)
        calls.append(kwargs)
        assert kwargs["max_tokens"] == 16384
        index = len(calls) - 1
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        usage["cost"] = 0.01 if index != bad_index else "malformed-explicit-cost" if bad_cost is None else bad_cost
        return SimpleNamespace(usage=usage)

    monkeypatch.setattr("litellm.acompletion", sdk)
    families = (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR, ProviderInvocationFamily.ADVISOR)
    with pytest.raises(CommonProviderCostRefused) as refused:
        for family in families:
            custody = owner.mint(family)
            async with provider_call_scope(custody):
                started_at, started_ns = datetime.now(UTC), time.monotonic_ns()
                response = await _litellm_acompletion(
                    prompt_guard_mount=None, prompt_guard_mode="debug-when-unconfigured",
                    prompt_guard_role="composer" if family is ProviderInvocationFamily.PRIMARY else "advisor",
                    provider_custody=custody, model="test/unknown-cost-model",
                    messages=[{"role": "user", "content": "Actual policy boundary fixture."}],
                )
                custody.mark_cost_response_accountable(admitted_metadata=True)
                recorder.record_llm_call(build_llm_call_record(
                    model_requested="test/unknown-cost-model", messages=[], tools=None,
                    status=ComposerLLMCallStatus.SUCCESS, started_at=started_at, started_ns=started_ns,
                    temperature=None, seed=None, response=response, provider_custody=custody,
                ), provider_custody=custody)
    assert refused.value.code == ("COST_UNAVAILABLE" if bad_cost is None else "COST_CAP_EXCEEDED")
    assert len(calls) == len(_ledger(engine)) == len(recorder.llm_calls) == bad_index + 1
    assert budget.snapshot().physical_calls == bad_index + 1
    assert tuple(c.call_id for c in budget.snapshot().charges) == tuple(c.call_id for c in recorder.llm_calls)
    assert recorder.llm_calls[-1].provider_cost == bad_cost
    assert budget.snapshot().cost_known is (bad_cost is not None)
    with pytest.raises(CommonProviderCostRefused):
        budget.require_dispatch()
    with pytest.raises(AuditIntegrityError, match="cannot replace"):
        owner.bind_cost_budget(CommonProviderCostBudget(budget.snapshot()))
```

- [ ] **5.2 Add exact configured-call and completion boundary cases to the same file using the following parameterized test.** It uses the same nominal complete record constructor and a real custody-independent budget; the physical count linkage is independently covered by 5.1.

```python
@pytest.mark.parametrize(("cost", "tokens", "expected"), [
    (5.0, 5, None), (5.01, 5, "COST_CAP_EXCEEDED"),
    (0.01, 16384, None), (0.01, 16385, "COMPLETION_TOKENS_EXCEEDED"),
    (None, 5, "COST_UNAVAILABLE"), (0.01, None, "MALFORMED_RESPONSE"),
])
def test_common_settled_response_exact_boundaries(cost, tokens, expected):
    from dataclasses import replace
    from decimal import Decimal
    from elspeth.web.composer.provider_cost import (
        CommonProviderCostBudget, CommonProviderCostPolicy, CommonProviderCostRefused, CommonProviderCostSnapshot,
    )
    budget = CommonProviderCostBudget(CommonProviderCostSnapshot(
        "session", "request", 1, "legacy_empty_v1",
        CommonProviderCostPolicy(1, 2097152, 16384, Decimal("5.00"), 2)))
    budget.dispatched()
    usage = {"prompt_tokens": 10}
    if tokens is not None:
        usage["completion_tokens"] = tokens
    if cost is not None:
        usage["cost"] = cost
    call = build_llm_call_record(
        model_requested="test/unknown-cost-model", messages=[], tools=None,
        status=ComposerLLMCallStatus.SUCCESS, started_at=datetime.now(UTC), started_ns=time.monotonic_ns(),
        temperature=None, seed=None, response=SimpleNamespace(usage=usage),
    )
    call = replace(call, call_id="unit-physical-call")
    budget.consume_settled(call, response_accountable=True)
    if expected is None:
        budget.require_output()
        with pytest.raises(CommonProviderCostRefused, match="PROVIDER_CALLS_EXHAUSTED"):
            budget.require_dispatch()
    else:
        with pytest.raises(CommonProviderCostRefused, match=expected):
            budget.require_output()
    with pytest.raises(AuditIntegrityError, match="charged twice"):
        budget.consume_settled(call, response_accountable=True)
```

- [ ] **5.3 Add this complete real-custody timeout/cancellation test to `test_provider_quota.py`.** Its actual failed SDK call remains unknown; no immediate cost-only exception replaces the original object.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["timeout", "cancelled"])
async def test_failed_sdk_unknown_keeps_original_outcome_and_end_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str,
) -> None:
    from decimal import Decimal
    from elspeth.web.composer.provider_cost import (
        CommonProviderCostBudget, CommonProviderCostPolicy, CommonProviderCostRefused, CommonProviderCostSnapshot,
    )
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="failed cost", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60)
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60)
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(
        RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4())))
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    budget = CommonProviderCostBudget(CommonProviderCostSnapshot(
        str(session.id), "original-user-message", 1, "legacy_empty_v1",
        CommonProviderCostPolicy(75, 2097152, 16384, Decimal("5.00"), 2)))
    owner.bind_cost_budget(budget)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    recorder = BufferingRecorder()
    failure = TimeoutError("actual transport timeout") if failure_kind == "timeout" else asyncio.CancelledError("actual cancel")
    status = ComposerLLMCallStatus.TIMEOUT if failure_kind == "timeout" else ComposerLLMCallStatus.CANCELLED
    physical = 0

    async def sdk(**kwargs: Any):
        nonlocal physical
        physical += 1
        raise failure

    monkeypatch.setattr("litellm.acompletion", sdk)
    with pytest.raises(type(failure)) as caught:
        async with provider_call_scope(custody):
            started_at, started_ns = datetime.now(UTC), time.monotonic_ns()
            try:
                await _litellm_acompletion(
                    prompt_guard_mount=None, prompt_guard_mode="debug-when-unconfigured", prompt_guard_role="composer",
                    provider_custody=custody, model="test/unknown-cost-model",
                    messages=[{"role": "user", "content": "Actual failed call fixture."}],
                )
            finally:
                custody.mark_cost_response_accountable(admitted_metadata=False)
                recorder.record_llm_call(build_llm_call_record(
                    model_requested="test/unknown-cost-model", messages=[], tools=None,
                    status=status, started_at=started_at, started_ns=started_ns,
                    temperature=None, seed=None, error_class=type(failure).__name__,
                    error_message=type(failure).__name__, provider_custody=custody,
                ), provider_custody=custody)
    assert caught.value is failure
    assert physical == len(_ledger(engine)) == len(recorder.llm_calls) == 1
    assert recorder.llm_calls[0].status is status
    assert recorder.llm_calls[0].provider_cost is None
    assert recorder.llm_calls[0].prompt_tokens is None and recorder.llm_calls[0].completion_tokens is None
    assert budget.snapshot().cost_known is False
    assert budget.snapshot().validated_cost == Decimal("0")
    budget.require_output()
    budget.require_dispatch()
    with pytest.raises(CommonProviderCostRefused, match="COST_UNAVAILABLE"):
        budget.require_end_headroom()
```

- [ ] **5.4 Add this actual HTTP transient-error → retry → admitted reply → withheld END test to the integration module.** It uses the real retry loop and actual END path with no token quota policy, which must be asserted from the actual SQL rows. Failed unknown is retained; this fixture permits the retry; END cannot invent monetary headroom or dispatch another physical call. Separately retain a real-custody active-token-policy control where missing failed-attempt usage refuses the next admission as `TOKEN_ACCOUNTING_UNAVAILABLE`. Neither fixture establishes a blanket retry rule. The existing saved-scope renderer supplies the withheld result.

```python
def test_common_transient_api_retry_keeps_failed_cost_unknown_for_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []
    candidate = "Please provide the CSV column names before I build the pipeline."

    async def sdk(**kwargs: Any) -> _Response:
        requests.append(deepcopy(kwargs))
        assert "response_format" not in kwargs, "END must not dispatch after an unknown failed cost"
        assert len(requests) <= 2, "one transient failure and its one admitted retry are supplied"
        if len(requests) == 1:
            raise LiteLLMAPIError(status_code=503, message="transient fixture", llm_provider="test", model="test/planner")
        return _Response([_Choice(_Message(candidate))], {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01,
        }, id="successful-retry")

    client, engine, _sessions = _build_app(tmp_path, monkeypatch, sdk)
    session_id = client.post("/api/sessions", json={"title": "transient policy trace"}).json()["id"]
    settled = settle_sync(client, client.app, path=f"/api/sessions/{session_id}/messages", body={
        "content": "Build a CSV pipeline; ask me if you need its columns.",
        "operation_id": str(uuid4()), "state_id": None,
    })
    result = settled.result()
    assert len(requests) == 2
    assert result["state"] is None
    assert candidate not in result["message"]["content"]
    assert _disposition_rows(engine) == []
    audits = [row.tool_calls[0]["call"] for row in _llm_audit_rows(engine)]
    assert len(audits) == 2
    failed = [call for call in audits if call["provider_request_id"] != "successful-retry"]
    succeeded = [call for call in audits if call["provider_request_id"] == "successful-retry"]
    assert len(failed) == len(succeeded) == 1
    assert failed[0]["provider_cost"] is None and failed[0]["provider_cost_source"] == "not_available"
    assert failed[0]["status"] == "api_error"
    assert failed[0]["prompt_tokens"] is None and failed[0]["completion_tokens"] is None
    assert succeeded[0]["provider_cost"] == 0.01
    with engine.connect() as conn:
        assert list(conn.execute(select(quota_policies_table))) == []
        assert len(list(conn.execute(select(token_usage_ledger_table)))) == 2
        assert len(list(conn.execute(select(quota_provider_attempts_table)))) == 2
```

Run existing cancellation/settlement tests unchanged alongside these. They retain failure priority and required child joins independently of policy. The existing direct-library retry and decline tests remain counterevidence against accidentally converting all failed calls into a primary monetary refusal.

- [ ] **5.5 Add the actual public `set_pipeline` → EARLY checkpoint test below to the integration module.** The original user scope, request policy, guard input and session fence come from the real public entry. Only physical SDK responses are scripted. The initial direct private gateway fixture was refused by the real PromptGuard as `unsupported_input`; that fixture cannot establish cost-policy behavior. Preserve PromptGuard and use the canonical public path. The known-cost control must parse CLEAN and publish the exact candidate and trusted notice segments separately.

```python
@pytest.mark.parametrize("unknown", [False, True])
def test_common_primary_then_actual_early_checkpoint_share_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unknown: bool,
) -> None:
    holder: dict[str, str] = {}
    authored = _valid_pipeline_completion(tmp_path, holder)
    requests: list[dict[str, Any]] = []
    candidate = "The pipeline draft is saved."

    async def sdk(**kwargs: Any) -> _Response:
        requests.append(deepcopy(kwargs))
        ordinal = len(requests)
        assert ordinal <= (2 if unknown else 4), "refused EARLY cannot dispatch more semantic work"
        usage: dict[str, object] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}
        if ordinal == 1:
            assert "response_format" not in kwargs
            reply = await authored(**kwargs)
            reply.choices[0].message.tool_calls[0].function.name = "set_pipeline"
            reply.choices[0].message.tool_calls[0].id = "early-policy-set-pipeline"
            reply.id = "early-policy-primary"
            return reply
        if ordinal in (2, 4):
            assert "response_format" in kwargs
            assert kwargs["model"] == client.app.state.settings.composer_advisor_model
            assert kwargs.get("tools") is None
            verdict = {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None}
            if ordinal == 4:
                review = parse_advisor_publication_review(kwargs["messages"][1]["content"])
                assert review["candidate_reply"] == candidate
                verdict["publication_verdict"] = "ADMIT"
            if ordinal == 2 and unknown:
                usage["cost"] = "malformed-explicit-cost"
            return _Response([_Choice(_Message(json.dumps(verdict)))], usage, model=kwargs["model"], id=f"early-policy-advisor-{ordinal}")
        assert ordinal == 3 and "response_format" not in kwargs
        return _Response([_Choice(_Message(candidate))], usage, id="early-policy-reply")

    client, engine, _sessions = _build_app(tmp_path, monkeypatch, sdk)
    holder["id"] = client.post("/api/sessions", json={"title": "actual EARLY policy"}).json()["id"]
    source_dir = tmp_path / "blobs" / holder["id"]
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "input.csv").write_text("name\nAlice\n", encoding="utf-8")
    (tmp_path / "outputs" / holder["id"]).mkdir(parents=True, exist_ok=True)
    settled = settle_sync(
        client,
        client.app,
        path=f"/api/sessions/{holder['id']}/messages",
        body={
            "content": _EMPTY_INTENT,
            "operation_id": str(uuid4()),
            "state_id": None,
        },
    )
    if unknown:
        status, payload = settled.error()
        assert status == 503 and payload["detail"]["planner_code"] == "COST_UNAVAILABLE"
    else:
        result = settled.result()
        expected_segments = (
            AssistantTextSegment(candidate),
            TrustedSystemNoticeSegment(
                "Checkpoint save status: Verified checkpoint evidence covers 1 saved draft revisions. "
                "There are net authored changes from the starting draft. Checkpoint evidence does not establish task completion."
            ),
        )
        assert result["message"]["segments"] == [{"kind": segment.kind, "content": segment.content} for segment in expected_segments]
        assert result["state"] is not None
    assert len(requests) == (2 if unknown else 4)
    audits = [row.tool_calls[0]["call"] for row in _llm_audit_rows(engine)]
    assert len(audits) == len(requests)
    assert len({call["call_id"] for call in audits}) == len(audits)
    early = next(call for call in audits if call["provider_request_id"] == "early-policy-advisor-2")
    assert early["provider_cost"] == (None if unknown else 0.01)
    for call in audits:
        request = next(request for request in requests if request["model"] == call["model_requested"])
        assert call["max_completion_tokens_requested"] == request["max_tokens"]
    with engine.connect() as conn:
        assert len(list(conn.execute(select(token_usage_ledger_table)))) == len(requests)
        assert len(list(conn.execute(select(quota_provider_attempts_table)))) == len(requests)
        rows = conn.execute(select(chat_messages_table.c.tool_calls).where(chat_messages_table.c.role == "audit")).all()
    passes = [item["pass"] for row in rows for item in row.tool_calls if item.get("_kind") == "advisor_checkpoint_pass_audit"]
    if not unknown:
        assert any(record["phase"] == "early" and record["verdict"] == "clean" for record in passes)
```

## Task 6 — Evidence, mutation controls and commit boundary

- [ ] **6.0 Add this settlement-failure control to `test_provider_quota.py` before running the mutation matrix.** It deliberately faults the real required settlement seam. An early monetary projection must fail its final assertions; a ledger-order assertion only at the next SDK entry would not detect that mutation.

```python
@pytest.mark.asyncio
async def test_cost_charge_does_not_precede_failed_required_settlement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from decimal import Decimal
    from elspeth.web.composer.provider_cost import CommonProviderCostBudget, CommonProviderCostPolicy, CommonProviderCostSnapshot

    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="settlement failure", auth_provider_type="local", owner_instance_id="owner", lease_seconds=60)
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=60)
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(
        RequiredAuthorityKind.SYNCHRONOUS_COMPOSE, context, invocation_id=str(uuid4())))
    owner = ProviderInvocationOwner(service=service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))
    budget = CommonProviderCostBudget(CommonProviderCostSnapshot(
        str(session.id), "request", 1, "legacy_empty_v1",
        CommonProviderCostPolicy(75, 2097152, 16384, Decimal("5.00"), 2)))
    owner.bind_cost_budget(budget)
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    recorder = BufferingRecorder()
    failure = AuditIntegrityError("injected required settlement failure")

    async def fail_settlement(**kwargs):
        assert kwargs["call"] is custody.call
        raise failure

    async def sdk(**kwargs):
        return SimpleNamespace(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01})

    monkeypatch.setattr(service, "finish_provider_attempt", fail_settlement)
    monkeypatch.setattr("litellm.acompletion", sdk)
    with pytest.raises(AuditIntegrityError) as caught:
        async with provider_call_scope(custody):
            started_at, started_ns = datetime.now(UTC), time.monotonic_ns()
            response = await _litellm_acompletion(
                prompt_guard_mount=None, prompt_guard_mode="debug-when-unconfigured", prompt_guard_role="composer",
                provider_custody=custody, model="test/unknown-cost-model",
                messages=[{"role": "user", "content": "Settlement failure control."}],
            )
            custody.mark_cost_response_accountable(admitted_metadata=True)
            recorder.record_llm_call(build_llm_call_record(
                model_requested="test/unknown-cost-model", messages=[], tools=None,
                status=ComposerLLMCallStatus.SUCCESS, started_at=started_at, started_ns=started_ns,
                temperature=None, seed=None, response=response, provider_custody=custody,
            ), provider_custody=custody)
    assert caught.value is failure
    assert custody.attempt is not None and custody.call is recorder.llm_calls[0]
    assert _ledger(engine) == []
    assert budget.snapshot().physical_calls == 1
    assert budget.snapshot().charges == ()
    assert budget.snapshot().cost_known is False
```

- [ ] **6.1 Set a project-local, short temp/log root and prove interpreter provenance.** Execute from the integration worktree selected by the coordinator. These commands are for implementation time; none ran while writing this plan.

```bash
cd "$COMPOSER_WORKTREE" && export COMPOSER_WORKTREE="$(pwd -P)"
cd "$COMPOSER_WORKTREE" && export COMPOSER_PYTHON="$COMPOSER_WORKTREE/.venv/bin/python"
cd "$COMPOSER_WORKTREE" && export PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src"
cd "$COMPOSER_WORKTREE" && export COMPOSER_COST_LOG="$COMPOSER_WORKTREE/.claude/lanes/first-carry-20261021/cost-checks"
cd "$COMPOSER_WORKTREE" && mkdir -p "$COMPOSER_COST_LOG" "$COMPOSER_WORKTREE/.claude/cost-tmp"
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)' >"$COMPOSER_COST_LOG/provenance.log" 2>&1
```

Read the complete provenance file and confirm both roots resolve inside this worktree. Never let an install repair a shared venv. Stop if the interpreter is unavailable.

- [ ] **6.2 Run focused checks with native exits and unpiped logs.** First run the new tests before code to establish a red control; then run after each task. Record every exit immediately.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 --basetemp .claude/cost-tmp/unit tests/unit/web/composer/test_provider_cost.py tests/unit/web/composer/test_provider_quota.py tests/unit/web/composer/test_provider_quota_end_to_end.py tests/unit/web/composer/test_chargeable_admission.py >"$COMPOSER_COST_LOG/unit.log" 2>&1
COMPOSER_COST_EXIT=$?
printf '%s\n' "$COMPOSER_COST_EXIT" >"$COMPOSER_COST_LOG/unit.exit"
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 --basetemp .claude/cost-tmp/http tests/integration/web/composer/test_freeform_planner_failure_translation.py -k 'common_provider_cost or common_nondefault_legacy_repair or common_unknown_cost or common_transient_api or common_primary_then_actual_early or cost_unavailable' >"$COMPOSER_COST_LOG/http.log" 2>&1
COMPOSER_COST_EXIT=$?
printf '%s\n' "$COMPOSER_COST_EXIT" >"$COMPOSER_COST_LOG/http.exit"
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 --basetemp .claude/cost-tmp/library tests/unit/web/composer/test_pipeline_planner.py >"$COMPOSER_COST_LOG/library.log" 2>&1
COMPOSER_COST_EXIT=$?
printf '%s\n' "$COMPOSER_COST_EXIT" >"$COMPOSER_COST_LOG/library.exit"
```

Expected: each process exits 0 after implementation. Read native exit files and complete failure summaries; a grep count is not qualification. The old 18-fail/6-pass unfrozen diagnostic is not evidence against this implementation.

- [ ] **6.3 Apply each reversible mutation separately in the implementation owner's isolated tree and require the named real test to fail.** Restore the exact implementation after each control using the editor, never a shared-tree restore command. Do not commit a mutant.

| Exact mutation | Required red witness |
| --- | --- |
| Remove `custody.require_cost_output()` from the scope | `test_common_unknown_cost_blocks_received_set_pipeline_before_effect` must fail or expose an unexpected extra SDK call; it must not merely pass on a different failure status. |
| Change `cost = None if call.provider_cost is None` to `cost = Decimal("0") if call.provider_cost is None` | Unknown-cost physical/HTTP tests must fail. |
| Pass `cost_budget=None` for ADVISOR in owner `mint()` | Mixed-role cases `bad_index=1,2` and actual END HTTP test must fail. |
| Move `consume_settled()` before the SQL completion | Physical fixture's ledger-order assertion or injected settlement failure test must fail; no charged snapshot may be used as proof of durable completion. |
| Change `>` to `>=` for cumulative cost | Equality unit must fail. |
| Remove request-byte check or count characters instead of UTF-8 bytes | Exact boundary test with `界` must fail. |
| Remove the real SDK physical counter increment | Mixed-role physical/snapshot identity assertions must fail. |
| Delete only the turn-sidecar drain in public catch | Measure actual SQL audit counts and order. The current settlement owner already commits those sidecars atomically, so this single deletion survives. Record that redundancy as counterevidence; do not claim a semantic RED or remove the durable-owner invariant. |
| Replace v2 not-applicable policy with default policy | v2 unknown-cost/no-legacy-cap unit must fail. |

Record each unsafe mutant's exact edit, native nonzero exit and target assertion, then record restored green. A syntax/import error is not a valid semantic red control. Report the surviving turn-drain-only deletion separately with the actual settlement call path and SQL sidecar-before-disposition evidence; do not fabricate a compound mutation merely to obtain another RED.

- [ ] **6.4 Run affected PostgreSQL and whole-tree gates after source is frozen.** Required because this changes settlement/refusal/audit write ordering even without a schema change. Reserve shared test capacity with root first. Use the canonical gate, lane-local log paths and read its final `summary.txt`; `frozen=NO` is not qualification.

```bash
cd "$COMPOSER_WORKTREE" && scripts/full-suite-gate.sh --execute --detach --root "$COMPOSER_WORKTREE" --stages ruff,mypy,contracts --log-dir "$COMPOSER_COST_LOG/static"
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 -m testcontainer --basetemp .claude/cost-tmp/pg tests/testcontainer/web/test_chargeable_admission_postgres.py >"$COMPOSER_COST_LOG/postgres.log" 2>&1
COMPOSER_COST_EXIT=$?
printf '%s\n' "$COMPOSER_COST_EXIT" >"$COMPOSER_COST_LOG/postgres.exit"
```

Also run the dynamic-attribute and masquerade whole-tree contract modules named by actual `CONTRIBUTING.md` after these files/tests change; do not add structural fakes or suppressions to appease them. Root owns any required full default suite and full PostgreSQL checkpoint before integration. A blocked Docker/PostgreSQL run remains explicitly unexecuted.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 --basetemp .claude/cost-tmp/ast tests/unit/web/test_sessions_composer_attribute_contracts.py tests/unit/elspeth_lints/test_masquerade_gate.py >"$COMPOSER_COST_LOG/ast.log" 2>&1
COMPOSER_COST_EXIT=$?
printf '%s\n' "$COMPOSER_COST_EXIT" >"$COMPOSER_COST_LOG/ast.exit"
```

- [ ] **6.5 Review the whole touched files and commit only the root-approved path set.** Use `scripts/branch-safety-check.sh --intent commit`, then root verifies current HEAD/staged paths. Do not stage lane logs, this plan's evidence temp roots, shared unrelated changes, or signing material. Root controls the actual integration commit. Completion claim must say code implemented, named tests/native exits, frozen state and PostgreSQL status separately.

## Task 7 — Closed future-frame integration, no new v2 balance

This section amends C1 and F36 at planning commit `170ec0ba3f801034b774b31c2193f897c7d3993d`. No continuation module is claimed implemented. V2 needs a nominal not-applicable policy descriptor; its real cost facts already belong to physical-outcomes O1/O3 `audit_call_json`, O2 `BufferingRecorder.restore_received_llm_call`, and the existing token ledger. Do not introduce a second aggregate, replay-settlement mechanism or legacy budget binding. V1 cannot cold-convert into v2; its original applicable total remains on the same owner across current iterations.

- [ ] **7.1 Add this complete nominal value next to C1 `ContinuationValue` in `src/elspeth/web/composer/continuation.py`, then add the required field to `ContinuationFrameStack`.** The field has no default; missing policy classification is not equivalent to a known zero balance.

```python
class ContinuationProviderCostPolicy(ContinuationValue):
    applicability: Literal["not_applicable"]
    lifecycle_version: Literal[2]
    accounting_owner: Literal["physical_audit_and_token_ledger"]
```

```python
    provider_cost_policy: ContinuationProviderCostPolicy
```

- [ ] **7.2 In C1 `prepare_composer_continuation_admission`, add this exact constructor keyword to the initial `ContinuationFrameStack`.** It is captured at logical ingress, before initialization/title/provider work. All subsequent `model_copy` transitions retain the descriptor; no F8 bootstrap or F9 iteration field duplicates it.

```python
        provider_cost_policy=ContinuationProviderCostPolicy(
            applicability="not_applicable",
            lifecycle_version=2,
            accounting_owner="physical_audit_and_token_ledger",
        ),
```

- [ ] **7.3 In F36 `run_composer_continuation_segment`, after decoding `stack`, add:**

```python
    if (
        stack.provider_cost_policy.applicability != "not_applicable"
        or stack.provider_cost_policy.lifecycle_version != 2
        or stack.provider_cost_policy.accounting_owner != "physical_audit_and_token_ledger"
        or provider_owner.cost_budget is not None
    ):
        raise AuditIntegrityError("v2 changed its selected provider policy scope")
```

The same function's existing `read_setup` checks the real parent `lifecycle_version == 2`. Keep it; a snapshot descriptor does not authorize converting a v1 parent. Future `mint_continuation()` continues to use its physical outcome owner without a legacy `cost_budget`. Common Advisor counters and quota authority are unaffected.

- [ ] **7.4 Add these complete tests to C1 `test_continuation_frames.py`, which already imports `json`, `pytest`, `initial_stack`, `encode_continuation_snapshot` and `decode_continuation_snapshot`.** Its `initial_stack()` constructor supplies the descriptor from 7.2. The descriptor cannot contain fabricated cost facts; those remain in actual complete physical records.

```python
from elspeth.contracts.errors import AuditIntegrityError


def test_v2_provider_policy_is_explicit_and_survives_cold_codec():
    original = initial_stack()
    restored = decode_continuation_snapshot(encode_continuation_snapshot(original))
    assert restored.provider_cost_policy.applicability == "not_applicable"
    assert restored.provider_cost_policy.lifecycle_version == 2
    assert restored.provider_cost_policy.accounting_owner == "physical_audit_and_token_ledger"
    assert restored.provider_cost_policy == original.provider_cost_policy


@pytest.mark.parametrize("mutation", ["missing", "legacy", "lifecycle", "invented_total"])
def test_v2_policy_codec_refuses_scope_substitution(mutation):
    data = json.loads(encode_continuation_snapshot(initial_stack()))
    if mutation == "missing":
        del data["provider_cost_policy"]
    elif mutation == "legacy":
        data["provider_cost_policy"]["applicability"] = "legacy_empty_v1"
    elif mutation == "lifecycle":
        data["provider_cost_policy"]["lifecycle_version"] = 1
    else:
        data["provider_cost_policy"]["total_cost"] = "0"
    with pytest.raises(AuditIntegrityError, match="owned codec"):
        decode_continuation_snapshot(json.dumps(data))
```

- [ ] **7.5 Run the exact existing complete-record/replay tests from physical-outcomes O4 and O6 together with C1.** `test_complete_actual_record_survives_json_without_rebuilding_clocks`, `test_restored_historical_call_is_once_only_and_conflicting_bytes_refuse`, `test_failed_outcome_roundtrip_keeps_the_actual_complete_call_and_null_usage`, and `test_actual_title_failure_survives_new_owner_without_retry_or_unknown_usage_zeroing` are defined in that companion; they exercise the actual audit owner, not this policy descriptor. Use its named test files and the Task 6 native-exit harness. Unknown cost remains `None` in the actual retained record. `restore_received_llm_call` checks complete-byte equality before duplicate suppression. No new monetary replay mutator is added, so no new acknowledgment protocol or duplicated balance is required.

## Acceptance boundaries and required review

The proposed code restores real configured v1 safeguards. It intentionally changes eligible v1 role coverage to include common EARLY/END/manual advice, preserves END strict headroom, preserves cost-only HTTP 503 and legacy overcap HTTP 500, and refuses unsupported nondefault facade repair configuration. It does not promise compatibility for the removed auto-proposal audit row shape or the facade's repair choreography. The direct planner library and its existing manifest-mutation/budget tests remain.

Primary unknown/overcap refusal must produce no mutation. An EARLY refusal can follow a real tool effect: retain that effect and its causal audit, stop further semantic work, and never call it a no-effect request. Cancellation and failed settlement can remain compound outcomes; no monetary refusal overrides a Tier-1 failure or erases the original cancellation. V2 follows its explicitly selected policy scope and preserves actual accounting across frames without inheriting legacy planner caps.

The plan is ready for independent root review before implementation. Source tracing is high confidence; proposed code and tests have not been imported, executed or qualified. The code packet uses the actual current audit envelope, SDK gateway, required-work and public failure helpers. Future C1/F36 changes are explicitly scheduled with those planned modules; no second monetary replay owner is introduced. No provider/network/account/service operations or signing occurred during planning.
