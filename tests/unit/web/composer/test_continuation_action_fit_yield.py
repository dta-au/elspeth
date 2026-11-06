"""Automatic successors require a resumable Compose cut and an original yield boundary."""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation import EffectFrontierEntry
from elspeth.web.composer.continuation_action_fit_yield import plan_automatic_successor
from elspeth.web.composer.continuation_next_action import next_continuation_action_policy
from tests.unit.web.composer.test_continuation_cut_coverage import HANDOFF_SAFE_CUTS, POLICY, _compose, _selected, _stack


def _first_compose():
    return _selected(
        _stack(
            _compose(
                slice_sequence=1,
                composition_total=0,
                discovery_total=0,
                advisor_calls=0,
                mutation_success_seen=False,
            )
        )
    )


@pytest.mark.parametrize("hard", [0.0, 1000.0], ids=("hard-fit", "quantum"))
def test_unconsumed_first_compose_stays_with_its_same_sequence_initializer(hard) -> None:
    selected = _first_compose()
    action = next_continuation_action_policy(selected=selected, policy=POLICY)
    assert (
        plan_automatic_successor(
            selected=selected, policy=POLICY, action=action, sequence=1, hard_remaining=hard, cooperative_remaining=0.0
        )
        is None
    )
    # A rolled slice number cannot substitute for the original paid PRIMARY
    # source that the current cold owner requires.
    rolled = _selected(selected.stack.model_copy(update={"frames": (selected.stack.frames[0].model_copy(update={"slice_sequence": 2}),)}))
    assert (
        plan_automatic_successor(selected=rolled, policy=POLICY, action=action, sequence=2, hard_remaining=hard, cooperative_remaining=0.0)
        is None
    )


def test_later_compose_action_fit_keeps_the_exact_slice_and_source() -> None:
    stack, _ = HANDOFF_SAFE_CUTS["later-compose"]
    selected = _selected(stack)
    action = next_continuation_action_policy(selected=selected, policy=POLICY)
    required = action.total + POLICY.segment.reserve
    plan = plan_automatic_successor(
        selected=selected, policy=POLICY, action=action, sequence=2, hard_remaining=0.0, cooperative_remaining=0.0
    )
    assert plan is not None and plan.expected_head is None and plan.reason == "action_fit"
    assert (
        plan_automatic_successor(
            selected=selected,
            policy=POLICY,
            action=action,
            sequence=2,
            hard_remaining=max(required, POLICY.segment.watermark + 1.0),
            cooperative_remaining=1.0,
        )
        is None
    )
    with pytest.raises(AuditIntegrityError, match="slice differs"):
        plan_automatic_successor(selected=selected, policy=POLICY, action=action, sequence=1, hard_remaining=0.0, cooperative_remaining=0.0)


@pytest.mark.parametrize("cut", ("advisor-input", "end-review", "end-repair", "end-publication", "tool-ready"))
@pytest.mark.parametrize("hard", [0.0, 1000.0], ids=("hard-fit", "quantum"))
def test_other_cuts_keep_their_current_same_sequence_owner(cut: str, hard) -> None:
    stack, _ = HANDOFF_SAFE_CUTS[cut]
    selected = _selected(stack)
    assert (
        plan_automatic_successor(
            selected=selected,
            policy=POLICY,
            action=POLICY.actions.primary,
            sequence=2,
            hard_remaining=hard,
            cooperative_remaining=0.0,
        )
        is None
    )


@pytest.mark.parametrize("disposition", ["dispatched", "unknown", "paid_overflow", "paid_incomplete", "sdk_terminal"])
def test_open_effect_cannot_be_turned_into_an_automatic_successor(disposition) -> None:
    stack, _ = HANDOFF_SAFE_CUTS["later-compose"]
    open_effect = EffectFrontierEntry(effect_id="provider:0:1", disposition=disposition, receipt_id=None)
    unsafe = _selected(stack.model_copy(update={"effect_frontier": (*stack.effect_frontier, open_effect)}))
    assert unsafe.stack.is_handoff_safe is False
    assert (
        plan_automatic_successor(
            selected=unsafe,
            policy=POLICY,
            action=POLICY.actions.primary,
            sequence=2,
            hard_remaining=1000.0,
            cooperative_remaining=0.0,
        )
        is None
    )


def test_pending_settlement_prevents_a_cooperative_successor() -> None:
    stack, _ = HANDOFF_SAFE_CUTS["later-compose"]
    selected = _selected(stack.model_copy(update={"pending_settlement_ids": ("original-pending-settlement",)}))
    assert (
        plan_automatic_successor(
            selected=selected,
            policy=POLICY,
            action=POLICY.actions.advisor,
            sequence=2,
            hard_remaining=1000.0,
            cooperative_remaining=0.0,
        )
        is None
    )


def test_action_fit_rejects_a_substituted_allowance() -> None:
    stack, _ = HANDOFF_SAFE_CUTS["later-compose"]
    selected = _selected(stack)
    actual = next_continuation_action_policy(selected=selected, policy=POLICY)
    changed = actual.model_copy(update={"physical": actual.physical + 1.0})
    with pytest.raises(AuditIntegrityError, match="allowance differs"):
        plan_automatic_successor(
            selected=selected, policy=POLICY, action=changed, sequence=2, hard_remaining=0.0, cooperative_remaining=0.0
        )


@pytest.mark.parametrize(
    ("hard", "soft", "reason"),
    [
        (1000.0, 1.0, None),
        (1000.0, 0.0, "quantum"),
        (300.0, 1.0, "watermark"),
        (300.0, 0.0, "watermark"),
        (0.0, 0.0, "action_fit"),
    ],
)
def test_automatic_reason_uses_original_admission_and_deterministic_priority(hard, soft, reason):
    from tests.helpers.continuation_policy import minimal_continuation_policy

    # Fresh, valid settings make the watermark strictly larger than the
    # selected advisor allowance; this is a pure policy boundary control.
    policy = minimal_continuation_policy(composer_continuation_watermark_seconds=300.0)
    stack, _ = HANDOFF_SAFE_CUTS["later-compose"]
    selected = _selected(
        stack.model_copy(
            update={
                "admission_policy_json": canonical_json(policy.model_dump(mode="json")),
                "policy_hash": policy.policy_hash,
                "provider_cost_policy": stack.provider_cost_policy.model_copy(update={"admission_policy_hash": policy.policy_hash}),
            }
        )
    )
    action = next_continuation_action_policy(selected=selected, policy=policy)
    assert action.total + policy.segment.reserve < policy.segment.watermark
    plan = plan_automatic_successor(
        selected=selected, policy=policy, action=action, sequence=2, hard_remaining=hard, cooperative_remaining=soft
    )
    assert (None if plan is None else plan.reason) == reason
    required = action.total + policy.segment.reserve
    at_equality = plan_automatic_successor(
        selected=selected, policy=policy, action=action, sequence=2, hard_remaining=required, cooperative_remaining=0.0
    )
    assert at_equality is not None and at_equality.reason == "watermark"
    below = plan_automatic_successor(
        selected=selected, policy=policy, action=action, sequence=2, hard_remaining=required - 0.001, cooperative_remaining=0.0
    )
    assert below is not None and below.reason == "action_fit"


@pytest.mark.parametrize(("hard", "soft"), [(float("nan"), 0.0), (1000.0, float("inf")), (1000.0, -1.0), (1.0, 2.0)])
def test_automatic_plan_rejects_invalid_or_unordered_timing(hard, soft):
    stack, _ = HANDOFF_SAFE_CUTS["later-compose"]
    selected = _selected(stack)
    action = next_continuation_action_policy(selected=selected, policy=POLICY)
    with pytest.raises(ValueError):
        plan_automatic_successor(
            selected=selected, policy=POLICY, action=action, sequence=2, hard_remaining=hard, cooperative_remaining=soft
        )
