"""Pure values for a cross-sequence continuation handoff (a yield).

A yield ends one segment at a handoff-safe cut and queues exactly one
successor at ``(sequence + 1, generation + 1)``. These values carry no SQL
authority; the session transaction in ``sessions/continuation.py`` owns the
writes (design §2.2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, model_validator
from sqlalchemy import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, ContinuationValue, InitializeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_next_action import handoff_cut_allowance, next_continuation_action_policy
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState

if TYPE_CHECKING:
    from elspeth.web.sessions.composer_operations import ComposerOperationRunning

ContinuationYieldReason = Literal["quantum", "watermark", "action_fit", "slice_limit", "shutdown", "model_request"]
ContinuationDrainDisposition = Literal["handoff", "handover", "unresolved"]


def classify_drain_cut_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    selected: SelectedContinuationState,
    *,
    env_ref_names: frozenset[str],
) -> ContinuationDrainDisposition:
    from elspeth.web.composer.continuation_next_action import validate_continuation_structure_on_connection

    unresolved = validate_continuation_structure_on_connection(conn, running, selected, env_ref_names=env_ref_names)
    if unresolved:
        return "unresolved"
    return classify_drain_cut(selected)


def classify_drain_cut(selected: SelectedContinuationState) -> ContinuationDrainDisposition:
    """Classify only current cuts whose existing selector completes validation.

    This pure preparation grants no disposition or dispatch authority. The
    landed phase owners require same-sequence recovery lineage, even at a
    handoff-safe cut (runtime design §2.2). Future-only handoff allowances
    therefore refuse until their actual handoff-lineage owners land.

    Do not catch ``ContinuationCutNotResumable`` here: for PRIMARY/P4/unknown
    shapes the current selector raises it before detailed phase validation.
    Those refusals must survive until a shared structural premise exists.
    Dispatched TITLE completes the selector's exact phase/locator/frontier
    checks and can already be classified as an unresolved drain effect.
    """
    if type(selected) is not SelectedContinuationState:
        raise TypeError("drain classification requires the owned selected DATA")
    root = selected.stack
    if type(root) is not ContinuationFrameStack:
        raise AuditIntegrityError("drain classification lost its nominal complete root")
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    next_continuation_action_policy(selected=selected, policy=policy)
    if handoff_cut_allowance(root, policy) is not None:
        raise AuditIntegrityError("drain cut has no landed handoff-lineage owner")
    top = root.frames[-1]
    if type(top) not in (InitializeFrame, ComposeFrame, ToolBatchFrame):
        raise AuditIntegrityError("drain cut has no landed phase owner")
    if type(top) is InitializeFrame and any(effect.disposition == "dispatched" for effect in root.effect_frontier):
        return "unresolved"
    return "handover"


class ContinuationYieldEvent(ContinuationValue):
    """The ``yielded`` event payload: the exact predecessor and successor identities."""

    version: Literal[2] = 2
    reason: ContinuationYieldReason
    predecessor_sequence: int = Field(gt=0)
    predecessor_generation: int = Field(gt=0)
    successor_sequence: int = Field(gt=0)
    successor_generation: int = Field(gt=0)

    @model_validator(mode="after")
    def require_one_actual_successor(self) -> Self:
        if (self.successor_sequence, self.successor_generation) != (self.predecessor_sequence + 1, self.predecessor_generation + 1):
            raise ValueError("a yield names exactly its next sequence and generation")
        return self


def roll_slice_for_successor(stack: ContinuationFrameStack, *, predecessor_sequence: int) -> ContinuationFrameStack:
    """Start the successor's technical slice; request-wide counters are kept.

    Only ``composition_slice`` and ``discovery_slice`` reset. Totals, Advisor
    calls, repair turns and checkpoint passes are request-wide policy counts
    and survive every rollover.
    """
    if type(stack) is not ContinuationFrameStack:
        raise TypeError("slice roll requires the owned frame stack")
    frames = []
    for frame in stack.frames:
        if type(frame) is ComposeFrame:
            if frame.slice_sequence != predecessor_sequence:
                raise AuditIntegrityError("Compose slice differs from the yielding segment")
            frame = frame.model_copy(update={"slice_sequence": predecessor_sequence + 1, "composition_slice": 0, "discovery_slice": 0})
        frames.append(frame)
    return stack.model_copy(update={"frames": tuple(frames)})
