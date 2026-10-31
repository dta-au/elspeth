from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.provider_cost import (
    CommonProviderCostBudget,
    CommonProviderCostCharge,
    CommonProviderCostPolicy,
    CommonProviderCostRefused,
    CommonProviderCostSnapshot,
)


def policy(**changes):
    return replace(CommonProviderCostPolicy(75, 2 * 1024 * 1024, 16384, Decimal("5.00"), 2), **changes)


def owner(*, amount="5.00", calls=0, charges=(), code=None, applicable=True, lifecycle=1):
    return CommonProviderCostBudget(
        CommonProviderCostSnapshot(
            "session",
            "request",
            lifecycle,
            "legacy_empty_v1" if applicable else "not_applicable",
            policy(max_cumulative_provider_cost=Decimal(amount)) if applicable else None,
            calls,
            charges,
            code,
        )
    )


def call(*, provider_cost=0.01, completion_tokens=5, finish_reason="stop", call_id="actual"):
    now = datetime.now(UTC)
    return ComposerLLMCall(
        model_requested="test/model",
        model_returned="test/model",
        status=ComposerLLMCallStatus.SUCCESS,
        prompt_tokens=10,
        completion_tokens=completion_tokens,
        total_tokens=10 + completion_tokens if completion_tokens is not None else None,
        latency_ms=1,
        provider_request_id=None,
        messages_hash="a" * 64,
        tools_spec_hash=None,
        declared_tool_names=(),
        started_at=now,
        finished_at=now,
        error_class=None,
        error_message=None,
        temperature=None,
        seed=None,
        finish_reason=finish_reason,
        provider_cost=provider_cost,
        provider_cost_source="response_usage.cost" if provider_cost is not None else "not_available",
        call_id=call_id,
    )


def test_primary_equal_boundary_and_end_headroom_are_distinct():
    budget = owner(calls=1, charges=(CommonProviderCostCharge("actual", Decimal("5.00"), "response"),))
    budget.require_dispatch()
    budget.require_output()
    with pytest.raises(CommonProviderCostRefused, match="COST_CAP_EXCEEDED"):
        budget.require_end_headroom()
    owner(amount="0").require_dispatch()


def test_unknown_is_not_zero_and_is_sticky_across_cold_owner():
    original = owner(
        calls=1,
        charges=(CommonProviderCostCharge("actual", None, "not_available"),),
        code="COST_UNAVAILABLE",
    )
    cold = CommonProviderCostBudget(replace(original.snapshot()))
    assert cold.snapshot().known_cost == Decimal("0")
    assert cold.snapshot().cost_known is False
    with pytest.raises(CommonProviderCostRefused, match="COST_UNAVAILABLE"):
        cold.require_dispatch()


def test_v2_retains_unknown_without_activating_legacy_cap():
    budget = owner(
        applicable=False,
        lifecycle=2,
        calls=1,
        charges=(CommonProviderCostCharge("actual", None, "not_available"),),
        code="COST_UNAVAILABLE",
    )
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
        budget = CommonProviderCostBudget(
            CommonProviderCostSnapshot("session", "request", 1, "legacy_empty_v1", policy(max_request_bytes=limit))
        )
        wire = {**request, "max_tokens": 100}
        if refused:
            with pytest.raises(CommonProviderCostRefused, match="REQUEST_BYTES_EXHAUSTED"):
                budget.prepare_request(wire)
        else:
            budget.prepare_request(wire)
            assert wire["max_tokens"] == 100


def test_request_view_retains_message_and_tool_values_and_leaves_absent_tools_absent():
    from elspeth.core.canonical import canonical_json

    messages = [{"role": "user", "content": "plain text"}]
    tools = [{"type": "function", "function": {"name": "inspect"}}]
    for request, expected_wire in (
        ({"messages": messages, "tools": tools}, {"messages": messages, "tools": tools}),
        ({"messages": messages}, {"messages": messages, "tools": []}),
    ):
        size = len(canonical_json(expected_wire).encode("utf-8"))
        budget = CommonProviderCostBudget(
            CommonProviderCostSnapshot("session", "request", 1, "legacy_empty_v1", policy(max_request_bytes=size))
        )
        budget.prepare_request(request)
        assert request["messages"] is messages
        assert request["max_tokens"] == 16384
        if expected_wire["tools"]:
            assert request["tools"] is tools
        else:
            assert "tools" not in request


@pytest.mark.parametrize("allowance", (True, 0, 1.0))
def test_request_view_rejects_nonpositive_or_nonexact_completion_allowance(allowance: object):
    request = {"messages": [], "max_tokens": allowance}
    budget = CommonProviderCostBudget(CommonProviderCostSnapshot("session", "request", 1, "legacy_empty_v1", policy()))

    with pytest.raises(AuditIntegrityError, match="invalid owned completion allowance"):
        budget.prepare_request(request)
    assert request["max_tokens"] is allowance


def test_nondefault_legacy_repair_configuration_refuses_before_dispatch():
    budget = CommonProviderCostBudget(CommonProviderCostSnapshot("session", "request", 1, "legacy_empty_v1", policy(repair_budget=3)))
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


def test_settled_call_is_charged_once_and_failed_attempt_does_not_count_as_validated():
    budget = owner(calls=1)
    budget.consume_settled(call(), response_accountable=False)
    charge = budget.snapshot().charges[0]
    assert charge.call_id == "actual"
    assert charge.cost == Decimal("0.01")
    assert budget.snapshot().known_cost == Decimal("0.01")
    assert budget.snapshot().validated_cost == Decimal("0")
    with pytest.raises(AuditIntegrityError, match="charged twice"):
        budget.consume_settled(call(), response_accountable=False)


def test_dispatch_counts_physical_call_and_refuses_output_until_settled():
    budget = CommonProviderCostBudget(CommonProviderCostSnapshot("session", "request", 1, "legacy_empty_v1", policy(max_provider_calls=1)))

    budget.dispatched()
    assert budget.snapshot().physical_calls == 1
    assert budget.snapshot().charges == ()
    with pytest.raises(AuditIntegrityError, match="lacks settled actual calls"):
        budget.require_output()

    budget.consume_settled(call(), response_accountable=True)
    budget.require_output()
    with pytest.raises(CommonProviderCostRefused, match="PROVIDER_CALLS_EXHAUSTED"):
        budget.require_dispatch()


def test_unknown_nonaccountable_attempt_allows_retry_but_blocks_end_headroom():
    budget = owner()
    budget.dispatched()
    budget.consume_settled(call(provider_cost=None, call_id="failed"), response_accountable=False)

    budget.require_dispatch()
    budget.dispatched()
    budget.consume_settled(call(call_id="retry"), response_accountable=True)
    budget.require_output()
    assert budget.snapshot().known_cost == Decimal("0.01")
    assert budget.snapshot().validated_cost == Decimal("0.01")
    assert budget.snapshot().cost_known is False

    with pytest.raises(CommonProviderCostRefused, match="COST_UNAVAILABLE"):
        budget.require_end_headroom()


@pytest.mark.parametrize(
    ("call_record", "expected_code"),
    (
        (call(completion_tokens=None, call_id="missing-usage"), "MALFORMED_RESPONSE"),
        (call(completion_tokens=16385, call_id="too-many-tokens"), "COMPLETION_TOKENS_EXCEEDED"),
        (call(finish_reason="length", call_id="truncated"), "RESPONSE_TRUNCATED"),
    ),
)
def test_accountable_completion_metadata_sets_precise_refusal_code(call_record, expected_code):
    budget = owner(calls=1)
    budget.consume_settled(call_record, response_accountable=True)

    with pytest.raises(CommonProviderCostRefused, match=expected_code) as caught:
        budget.require_output()
    assert caught.value.code == expected_code
    assert caught.value.call_id == call_record.call_id


def test_accountable_unknown_cost_and_over_limit_response_refuse():
    unavailable = owner(calls=1)
    unavailable.consume_settled(call(provider_cost=None), response_accountable=True)
    assert unavailable.snapshot().cost_known is False
    with pytest.raises(CommonProviderCostRefused, match="COST_UNAVAILABLE"):
        unavailable.require_output()

    excessive = owner(amount="0.009", calls=1)
    excessive.consume_settled(call(), response_accountable=True)
    with pytest.raises(CommonProviderCostRefused, match="COST_CAP_EXCEEDED"):
        excessive.require_output()


def test_capture_keeps_existing_graph_and_v2_outside_legacy_policy(tmp_path):
    from elspeth.web.composer.provider_cost import capture_common_provider_cost
    from elspeth.web.config import WebSettings

    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_boot_probe_enabled=False,
        composer_max_composition_turns=3,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        composer_planner_repair_budget=3,
        shareable_link_signing_key=b"\x00" * 32,
    )
    for lifecycle, empty in ((1, False), (2, False), (2, True)):
        budget = capture_common_provider_cost(
            session_id="session",
            request_id="request",
            lifecycle_version=lifecycle,
            initially_empty=empty,
            settings=settings,
        )
        assert budget.snapshot().policy is None
        assert budget.snapshot().applicability == "not_applicable"
        budget.require_dispatch()
    legacy = capture_common_provider_cost(
        session_id="session",
        request_id="request",
        lifecycle_version=1,
        initially_empty=True,
        settings=settings,
    )
    assert legacy.snapshot().applicability == "legacy_empty_v1"
    with pytest.raises(CommonProviderCostRefused, match="REPAIR_POLICY_UNSUPPORTED"):
        legacy.require_dispatch()


@pytest.mark.parametrize(
    "invalid_lifecycle",
    (pytest.param(True, id="bool-is-not-an-exact-int"), pytest.param(1.0, id="float-is-not-an-exact-int")),
)
def test_capture_rejects_non_exact_lifecycle_without_coercion(closed_local_settings, invalid_lifecycle: object):
    from elspeth.web.composer.provider_cost import capture_common_provider_cost

    with pytest.raises(ValueError, match="unsupported immutable lifecycle"):
        capture_common_provider_cost(
            session_id="session",
            request_id="request",
            lifecycle_version=invalid_lifecycle,
            initially_empty=True,
            settings=closed_local_settings,
        )
