"""Assert the anchor move a guided settlement makes for a carried proposal.

Every guided settlement mints a fresh ``composition_states`` row. In guided
mode that row is a SESSION checkpoint, not a graph-change record: the whole
``GuidedSession`` — ``chat_history`` included — is persisted inside
``composer_meta``, so an advisory chat or a declined revision is structurally
a new version even though the composition is untouched. A settlement that
carries a still-pending proposal across one must therefore move the
proposal's anchor onto the row it writes, or the anchor keeps naming the
PREVIOUS checkpoint and every later currency check fails closed
(elspeth-ed67eb9d0d): ``GET /guided`` at Step 3 refuses to serve the session
at all, and "edit this component" at Step 4 stops working.

The anchor is NOT ``PipelineProposal.base``. That base is hashed into
``draft_hash`` (``pipeline_draft_hash``), so it is the reviewed artifact's
identity and can never move. The anchor is the lifecycle-managed
``composition_proposals.base_state_id``, derived by the settlement as the
creation base plus every appended ``proposal.rebased`` hop, and surfaced as
``AuthoritativePipelineProposal.current_base``.

This helper builds the caller's assertion of one such move. It asserts only;
the settlement re-derives every binding from the live tree, and admits the
move only while the composition content is unchanged. All four carrying
sites — the planner-decline settlement, the Step-3 acceptance into wire
review, the atomic guided chat, and the guided-full escape-hatch decline —
go through it, so no site can move an anchor without recording why. Each
names itself with a closed ``reason``, because the persisted event is
otherwise identical across all four modulo ids: two of them settle the same
``guided_respond`` operation kind, so the settlement's own identity cannot
tell them apart.

This closes the guided SETTLEMENT paths, not every checkpoint writer. A
writer that inserts a ``composition_states`` row carrying a live
``active_proposal`` forward without going through a guided settlement still
strands the anchor exactly as before, and that is still measured on
``POST /api/sessions/{id}/messages`` and ``POST /api/sessions/{id}/compose``
(elspeth-f561d651c8, pre-existing).

The guided-full STAGING outcome is the third such writer and is handled
differently, because a rebase is the wrong remedy there: staging mints a
SECOND pending proposal against the checkpoint it writes, so which proposal
the walk then owns is a semantics ruling rather than an anchor move.
``stage_guided_full_pipeline_proposal`` therefore REFUSES fail-closed when
the observed head still carries a live ``active_proposal`` — a coded
``integrity_error`` instead of an unreadable session — and the durable
remedy stays with elspeth-da0e3db919.
"""

from __future__ import annotations

from uuid import UUID

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.planning import guided_private_reviewed_facts
from elspeth.web.composer.guided.state_machine import GuidedSession
from elspeth.web.sessions.protocol import GuidedPendingProposalRebase, GuidedProposalRebaseReason

__all__ = ["carried_pending_proposal_rebase"]


def carried_pending_proposal_rebase(
    guided: GuidedSession | None,
    *,
    from_state_id: UUID | None,
    base_composition_content_hash: str | None,
    reason: GuidedProposalRebaseReason,
) -> GuidedPendingProposalRebase | None:
    """Assert the anchor move for the proposal ``guided`` carries forward.

    ``from_state_id`` and ``base_composition_content_hash`` describe the
    checkpoint this settlement is written against — the head the carried
    proposal is expected to still be anchored to. Both are ``None`` only
    when there is no persisted head at all, in which case there is no
    carried proposal either. Returns ``None`` when the checkpoint carries no
    pending proposal, which is the ordinary case for every guided settlement
    outside Steps 3 and 4, and when there is no guided session at all — the
    guided-full surface also writes checkpoints over freeform heads.
    """

    if guided is None or guided.active_proposal is None:
        return None
    if from_state_id is None or base_composition_content_hash is None:
        raise AuditIntegrityError("a carried guided pending proposal requires a persisted current checkpoint")
    active = guided.active_proposal
    return GuidedPendingProposalRebase(
        proposal_id=active.proposal_id,
        draft_hash=active.draft_hash,
        reviewed_facts=guided_private_reviewed_facts(guided),
        from_state_id=from_state_id,
        composition_content_hash=base_composition_content_hash,
        reason=reason,
    )
