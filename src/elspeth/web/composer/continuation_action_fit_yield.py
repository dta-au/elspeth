"""Pure admission for an automatic successor at a quiescent Compose cut.

The live driver owns the timing observation. This plan grants no SQL authority:
the successor transaction rechecks the current fence, head, checkpoint,
effects, Stop and retained next action before publishing sequence + 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame
from elspeth.web.composer.continuation_clock import SegmentFit, admit_action
from elspeth.web.composer.continuation_next_action import _has_committed_primary, handoff_cut_allowance, next_continuation_action_policy
from elspeth.web.composer.continuation_policy import (
    ContinuationActionPolicy,
    ContinuationAdmissionPolicyV4,
)
from elspeth.web.composer.continuation_selection import SelectedContinuationState


@dataclass(frozen=True, slots=True)
class AutomaticSuccessorPlan:
    expected_head: UUID | None
    reason: Literal["action_fit", "watermark", "quantum"]


def plan_automatic_successor(
    *,
    selected: SelectedContinuationState,
    policy: ContinuationAdmissionPolicyV4,
    action: ContinuationActionPolicy,
    sequence: int,
    hard_remaining: float,
    cooperative_remaining: float,
) -> AutomaticSuccessorPlan | None:
    """Yield the proven later Compose lineage at its original admission boundary.

    The unconsumed first Compose and non-Compose cuts
    retain same-sequence recovery until their cross-sequence cold owner is
    proved by the live path.
    """
    if type(selected) is not SelectedContinuationState or type(action) is not ContinuationActionPolicy:
        raise TypeError("automatic successor planning requires actual selected DATA and action policy")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("automatic successor planning requires the current positive sequence")
    if type(hard_remaining) is not float or not math.isfinite(hard_remaining) or hard_remaining < 0:
        raise ValueError("automatic successor planning requires finite nonnegative hard headroom")
    if type(cooperative_remaining) is not float or not math.isfinite(cooperative_remaining) or cooperative_remaining < 0:
        raise ValueError("automatic successor planning requires finite nonnegative cooperative headroom")
    if (
        admit_action(action, policy.segment, hard_remaining=hard_remaining, cooperative_remaining=cooperative_remaining, fresh=False)
        is SegmentFit.ADMIT
    ):
        return None
    required = action.total + policy.segment.reserve
    reason: Literal["action_fit", "watermark", "quantum"]
    if hard_remaining < required:
        reason = "action_fit"
    elif hard_remaining <= policy.segment.watermark:
        reason = "watermark"
    else:
        reason = "quantum"
    root = selected.stack
    if len(root.frames) != 1 or type(root.frames[0]) is not ComposeFrame:
        return None
    compose = root.frames[0]
    if compose.phase != "action_ready" or not root.is_handoff_safe:
        return None
    # The original first-Compose initializer proves a same-sequence TITLE and
    # initialization event. A technical slice rollover alone cannot make that
    # original event a source for another sequence. A committed PRIMARY is the
    # existing cold successor lineage, as exercised by model-request yield.
    if not _has_committed_primary(root) or handoff_cut_allowance(root, policy) is None:
        return None
    if compose.slice_sequence != sequence:
        raise AuditIntegrityError("automatic successor Compose slice differs from its actual segment")
    if compose.provider_effect_key is not None or compose.pending_response is not None:
        raise AuditIntegrityError("automatic successor Compose cut retains a provider result")
    if any(effect.disposition not in ("committed", "refused") for effect in root.effect_frontier):
        raise AuditIntegrityError("automatic successor Compose cut retains an open effect")
    if next_continuation_action_policy(selected=selected, policy=policy) != action:
        raise AuditIntegrityError("automatic successor allowance differs from the selected retained action")
    return AutomaticSuccessorPlan(expected_head=None if compose.current_state_id is None else UUID(compose.current_state_id), reason=reason)
