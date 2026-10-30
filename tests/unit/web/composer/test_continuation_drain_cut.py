"""Current source-owned drain classifications; no disposition is executed."""

from __future__ import annotations

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import EffectFrontierEntry, InitializeFrame
from elspeth.web.composer.continuation_handoff import classify_drain_cut
from elspeth.web.composer.continuation_next_action import ContinuationCutNotResumable
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from tests.unit.web.composer.test_continuation_cut_coverage import (
    HANDOFF_SAFE_CUTS,
    IN_FLIGHT,
    _committed,
    _compose,
    _ref,
    _selected,
    _stack,
)
from tests.unit.web.composer.test_continuation_frames import initial_stack


def _title(*, dispatched: bool) -> SelectedContinuationState:
    frame = InitializeFrame(
        request_json='{"content":"request"}',
        ingress_phase="title_pending",
        title_attempt_id="title-attempt" if dispatched else None,
        ingress_receipt_json="{}",
        bootstrap=_ref("composer.bootstrap.v1", "title-input"),
        provider_effect_key="provider:3:0",
        provider_input_sha256="a" * 64,
    )
    return _selected(
        initial_stack().model_copy(
            update={
                "frames": (frame,),
                "effect_frontier": (
                    EffectFrontierEntry(effect_id="provider:3:0", disposition="dispatched" if dispatched else "prepared", receipt_id=None),
                ),
                "pending_settlement_ids": ("title-attempt",) if dispatched else (),
            }
        )
    )


def test_reserved_ingress_uses_same_sequence_handover() -> None:
    frame = InitializeFrame(request_json="{}", ingress_phase="reserved", title_attempt_id=None)
    selected = SelectedContinuationState("checkpoint", initial_stack().model_copy(update={"frames": (frame,)}), ())
    assert classify_drain_cut(selected) == "handover"


def test_first_compose_uses_same_sequence_handover_even_when_handoff_safe() -> None:
    stack = _stack(_compose(slice_sequence=1, composition_total=0, discovery_total=0, advisor_calls=0, mutation_success_seen=False))
    assert stack.is_handoff_safe
    assert classify_drain_cut(_selected(stack)) == "handover"


def test_retained_primary_response_uses_same_sequence_handover() -> None:
    stack = _stack(
        _compose(
            phase="response_retained",
            pending_response=_ref("composer.response.v1", "response"),
            provider_effect_key="provider:0:0",
            provider_input_sha256="a" * 64,
        ),
        effects=(_committed("provider:0:0"),),
    )
    assert classify_drain_cut(_selected(stack)) == "handover"


def test_prepared_title_has_no_dispatch_and_uses_handover() -> None:
    assert classify_drain_cut(_title(dispatched=False)) == "handover"


def test_dispatched_title_is_unresolved_after_its_exact_selector_validation() -> None:
    assert classify_drain_cut(_title(dispatched=True)) == "unresolved"


def test_unknown_title_still_refuses_before_its_structural_premise_exists() -> None:
    selected = _title(dispatched=True)
    changed = _selected(
        selected.stack.model_copy(
            update={"effect_frontier": (EffectFrontierEntry(effect_id="provider:3:0", disposition="unknown", receipt_id=None),)}
        )
    )
    with pytest.raises(ContinuationCutNotResumable, match="unknown effect"):
        classify_drain_cut(changed)


@pytest.mark.parametrize("cut", sorted(HANDOFF_SAFE_CUTS))
def test_a_future_handoff_allowance_does_not_grant_an_unlanded_owner(cut: str) -> None:
    stack, _allowance = HANDOFF_SAFE_CUTS[cut]
    with pytest.raises(AuditIntegrityError, match="handoff-lineage owner"):
        classify_drain_cut(_selected(stack))


@pytest.mark.parametrize(("stack", "message"), IN_FLIGHT)
def test_early_nonresumable_selector_refusals_are_not_mistaken_for_validated_cuts(stack, message: str) -> None:
    # The current selector raises before phase/locator validation for these
    # shapes. A future shared validator must prove them before settlement.
    with pytest.raises(ContinuationCutNotResumable, match=message):
        classify_drain_cut(_selected(stack))


def test_dispatched_title_with_an_inconsistent_attempt_keeps_its_integrity_failure() -> None:
    selected = _title(dispatched=True)
    top = selected.stack.frames[-1].model_copy(update={"title_attempt_id": "different-attempt"})
    changed = _selected(selected.stack.model_copy(update={"frames": (top,)}))
    with pytest.raises(AuditIntegrityError, match="unresolved TITLE") as caught:
        classify_drain_cut(changed)
    assert type(caught.value) is AuditIntegrityError


def test_dispatched_title_with_the_wrong_locator_keeps_its_integrity_failure() -> None:
    selected = _title(dispatched=True)
    top = selected.stack.frames[-1].model_copy(update={"provider_effect_key": "provider:3:1"})
    changed = _selected(selected.stack.model_copy(update={"frames": (top,)}))
    with pytest.raises(AuditIntegrityError, match="closed TITLE") as caught:
        classify_drain_cut(changed)
    assert type(caught.value) is AuditIntegrityError


def test_classification_rechecks_the_complete_selected_private_bytes() -> None:
    selected = _title(dispatched=True)
    object.__setattr__(selected, "chunks", ())
    with pytest.raises(AuditIntegrityError, match="complete ordered chunk cohort"):
        classify_drain_cut(selected)


def test_classification_refuses_a_structural_impostor() -> None:
    with pytest.raises(TypeError, match="owned selected"):
        classify_drain_cut(object())
