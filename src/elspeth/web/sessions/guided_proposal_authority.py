"""Guided checkpoint proposal authority and carried proposal transitions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast
from uuid import UUID

from sqlalchemy import Connection, insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.pipeline_proposal import (
    PlannerSurface,
    PresentBase,
    composition_content_hash,
    reviewed_anchor_hash,
)
from elspeth.web.coordination.repository import (
    _ForkCreationTransaction,
)
from elspeth.web.sessions.converters import state_from_record
from elspeth.web.sessions.models import (
    composition_proposals_table,
    composition_states_table,
    proposal_events_table,
)
from elspeth.web.sessions.mutation_capabilities import (
    _GuidedSessionMutationTransaction,
)
from elspeth.web.sessions.proposal_authority import (
    _PIPELINE_CREATED_SCHEMA,
    _pipeline_rebased_payload,
    _proposal_event_record_from_row,
    _proposal_record_from_row,
    _restore_authoritative_pipeline_proposal,
    _verify_pipeline_lifecycle_authority,
)
from elspeth.web.sessions.protocol import (
    AuthoritativePipelineProposal,
    CompositionStateRecord,
    GuidedPendingProposalInvalidation,
    GuidedPendingProposalRebase,
    GuidedProposalRebaseReason,
    SessionForkCreationTransaction,
)

if TYPE_CHECKING:
    from elspeth.web.composer.guided.state_machine import GuidedProposalRef, GuidedSession
    from elspeth.web.sessions.service import SessionServiceImpl


def _require_pending_guided_checkpoint_proposal_authority(
    conn: Connection | SessionForkCreationTransaction,
    *,
    service: SessionServiceImpl,
    session_id: str,
    checkpoint: CompositionStateRecord,
    guided: GuidedSession,
    role: str,
) -> AuthoritativePipelineProposal | None:
    """Validate one checkpoint's complete pending guided proposal authority."""
    from elspeth.web.composer.guided.planning import guided_private_reviewed_facts
    from elspeth.web.composer.guided.protocol import GuidedStep, TurnType

    unanswered_indices = [index for index, turn in enumerate(guided.history) if turn.response_hash is None]
    trailing = guided.history[-1] if guided.history else None
    has_exact_active_occurrence = bool(
        unanswered_indices == [len(guided.history) - 1]
        and trailing is not None
        and (
            (
                guided.step is GuidedStep.STEP_3_TRANSFORMS
                and trailing.step is GuidedStep.STEP_3_TRANSFORMS
                and trailing.turn_type is TurnType.PROPOSE_PIPELINE
            )
            or (
                guided.step is GuidedStep.STEP_4_WIRE
                and trailing.step is GuidedStep.STEP_4_WIRE
                and trailing.turn_type is TurnType.CONFIRM_WIRING
            )
        )
    )
    has_orphan_authority_occurrence = any(
        guided.history[index].turn_type in {TurnType.PROPOSE_PIPELINE, TurnType.CONFIRM_WIRING} for index in unanswered_indices
    )
    if (guided.active_proposal is not None and not has_exact_active_occurrence) or (
        guided.active_proposal is None and has_orphan_authority_occurrence
    ):
        raise AuditIntegrityError(f"{role} guided proposal reference/history coupling is malformed")
    reference = guided.active_proposal
    if reference is None:
        return None

    if type(conn) is _ForkCreationTransaction:
        transaction = cast(SessionForkCreationTransaction, conn)
        proposal_row = transaction.read_parent_proposal(reference.proposal_id)
        creation_rows = transaction.read_parent_proposal_creation_events(reference.proposal_id)
    else:
        connection = cast(Connection, conn)
        proposal_row = connection.execute(
            select(composition_proposals_table)
            .where(composition_proposals_table.c.session_id == session_id)
            .where(composition_proposals_table.c.id == str(reference.proposal_id))
        ).one_or_none()
        creation_rows = tuple(
            connection.execute(
                select(proposal_events_table)
                .where(proposal_events_table.c.session_id == session_id)
                .where(proposal_events_table.c.proposal_id == str(reference.proposal_id))
                .where(proposal_events_table.c.event_type == "proposal.created")
            ).fetchall()
        )
    if proposal_row is None:
        raise AuditIntegrityError(f"{role} guided proposal authority is missing or cross-session")
    if len(creation_rows) != 1:
        raise AuditIntegrityError(f"{role} guided proposal must have exactly one creation event")
    authority = _restore_authoritative_pipeline_proposal(
        conn=conn,
        row=_proposal_record_from_row(proposal_row),
        creation_event=_proposal_event_record_from_row(creation_rows[0]),
        reviewed_facts=guided_private_reviewed_facts(guided),
    )
    if authority.row.status != "pending":
        raise AuditIntegrityError(f"{role} guided checkpoint references a terminal pipeline proposal")
    _verify_pipeline_lifecycle_authority(
        conn,
        service=service,
        authority=authority,
    )
    proposal = authority.proposal
    if (
        reference.proposal_id != authority.row.id
        or reference.draft_hash != proposal.draft_hash
        or reference.base != proposal.base
        or reference.reviewed_anchor_hash != proposal.reviewed_anchor_hash
        or reference.covered_deferred_intent_ids != proposal.covered_deferred_intent_ids
        or reference.creation_event_schema != _PIPELINE_CREATED_SCHEMA
        or reference.supersedes_proposal_id != authority.supersedes_proposal_id
        or reference.supersedes_draft_hash != proposal.supersedes_draft_hash
    ):
        raise AuditIntegrityError(f"{role} guided proposal reference differs from canonical authority")
    if proposal.surface not in {PlannerSurface.GUIDED_STAGED, PlannerSurface.TUTORIAL_PROFILE}:
        raise AuditIntegrityError(f"{role} guided proposal authority has an invalid surface")
    if type(proposal.base) is not PresentBase:
        raise AuditIntegrityError(f"{role} guided proposal checkpoint base is malformed")
    if type(conn) is _ForkCreationTransaction:
        base_row = cast(SessionForkCreationTransaction, conn).read_parent_state(proposal.base.state_id)
    else:
        base_row = (
            cast(Connection, conn)
            .execute(
                select(composition_states_table)
                .where(composition_states_table.c.session_id == session_id)
                .where(composition_states_table.c.id == str(proposal.base.state_id))
            )
            .one_or_none()
        )
    if base_row is None:
        raise AuditIntegrityError(f"{role} guided proposal base is missing or cross-session")
    base_record = service._row_to_state_record(base_row)
    if composition_content_hash(state_from_record(base_record)) != proposal.base.composition_content_hash:
        raise AuditIntegrityError(f"{role} guided proposal base content binding is malformed")
    if proposal.base.composition_content_hash != composition_content_hash(state_from_record(checkpoint)):
        raise AuditIntegrityError(f"{role} guided proposal checkpoint base content binding is malformed")
    return authority


@dataclass(frozen=True, slots=True)
class _GuidedPendingProposalTransitionContext:
    """Frozen checkpoint and database authority for one proposal transition.

    A guided settlement does exactly one of three things to the checkpoint's
    pending-proposal reference: nothing (there is none), CLEAR it (rewind or
    terminal exit), or CARRY it forward onto the checkpoint being written.
    ``checkpoint_state_id`` is the id of that new row, and
    ``settlement_origin`` names the operation for the failure messages —
    a stale binding is undiagnosable without knowing which gesture wrote it.
    """

    service: SessionServiceImpl
    session_id: str
    current_record: CompositionStateRecord | None
    prior_guided: GuidedSession
    candidate_guided: GuidedSession
    expected_current_content_hash: str | None
    checkpoint_state_id: UUID
    candidate_content_hash: str
    settlement_origin: str


@dataclass(frozen=True, slots=True)
class _GuidedPendingProposalTransitionRefs:
    """The checkpoint reference one settlement clears or carries, never both."""

    cleared: GuidedProposalRef | None
    carried: GuidedProposalRef | None


@dataclass(frozen=True, slots=True)
class _GuidedPendingProposalRebasePlan:
    """One verified anchor move, ready to append once the checkpoint exists.

    ``reason`` rides with the authority rather than being re-read from the
    command at the write site: the verifier is what proves a rebase is
    happening at all, so the value the event records comes from the same
    place that admitted it.
    """

    authority: AuthoritativePipelineProposal
    reason: GuidedProposalRebaseReason


@dataclass(frozen=True, slots=True)
class _GuidedPendingProposalAuthorities:
    """Restored row authority for whichever pending transition a settlement performs."""

    invalidated: AuthoritativePipelineProposal | None
    rebased: _GuidedPendingProposalRebasePlan | None


def _require_guided_pending_proposal_transition(
    context: _GuidedPendingProposalTransitionContext,
    invalidation: GuidedPendingProposalInvalidation | None,
    rebase: GuidedPendingProposalRebase | None,
) -> _GuidedPendingProposalTransitionRefs:
    """Classify the candidate's transition on the checkpoint's active ref.

    A guided settlement either clears exactly one active reference or
    carries one forward unchanged. ``GuidedProposalRef`` mirrors the
    proposal's IMMUTABLE reviewed identity — ``base`` included, because
    ``base`` is hashed into ``draft_hash`` — so a carry is an exact
    equality, and any other edit to a live reference is illegal here.

    The carrying arm used to return unconditionally with no checks at all
    (elspeth-ed67eb9d0d). The check it was missing needs the live row, so it
    lives in :func:`_verify_guided_pending_proposal_rebase`; this function
    only says WHICH transition is happening.
    """

    prior_active = context.prior_guided.active_proposal
    candidate_active = context.candidate_guided.active_proposal
    if prior_active is not None and candidate_active is not None:
        if invalidation is not None:
            raise AuditIntegrityError("guided proposal invalidation sideband did not clear an active proposal")
        if prior_active != candidate_active:
            raise AuditIntegrityError(
                "guided settlement edited a carried pending proposal reference: "
                f"{context.settlement_origin} carried proposal {prior_active.proposal_id} as "
                f"{candidate_active.proposal_id}"
            )
        return _GuidedPendingProposalTransitionRefs(cleared=None, carried=candidate_active)
    if prior_active is None and candidate_active is None:
        if invalidation is not None:
            raise AuditIntegrityError("guided proposal invalidation sideband did not clear an active proposal")
        if rebase is not None:
            raise AuditIntegrityError("guided proposal rebase sideband did not carry an active proposal forward")
        return _GuidedPendingProposalTransitionRefs(cleared=None, carried=None)
    if rebase is not None:
        raise AuditIntegrityError("guided proposal rebase sideband did not carry an active proposal forward")
    if invalidation is None:
        raise AuditIntegrityError("clearing guided active proposal requires an exact invalidation sideband")
    active = prior_active
    if active is None or candidate_active is not None:
        raise AuditIntegrityError("guided proposal invalidation must clear one exact active proposal")
    if active.proposal_id != invalidation.proposal_id or active.draft_hash != invalidation.draft_hash:
        raise AuditIntegrityError("guided proposal invalidation sideband differs from checkpoint authority")
    if context.current_record is None:
        raise AuditIntegrityError("guided proposal invalidation requires a current checkpoint")
    return _GuidedPendingProposalTransitionRefs(cleared=active, carried=None)


def _verify_guided_pending_proposal_invalidation(
    conn: Connection,
    *,
    context: _GuidedPendingProposalTransitionContext,
    invalidation: GuidedPendingProposalInvalidation | None,
    active: GuidedProposalRef | None,
) -> AuthoritativePipelineProposal | None:
    """Verify one exact active-reference clear and restore pending row authority."""

    if active is None:
        return None
    if invalidation is None or context.current_record is None:  # pragma: no cover - transition verifier owns this
        raise AuditIntegrityError("guided proposal invalidation lost its verified authority")
    proposal_row = conn.execute(
        select(composition_proposals_table)
        .where(composition_proposals_table.c.session_id == context.session_id)
        .where(composition_proposals_table.c.id == str(invalidation.proposal_id))
    ).one_or_none()
    if proposal_row is None:
        raise AuditIntegrityError("guided proposal invalidation authority is missing or cross-session")
    creation_rows = conn.execute(
        select(proposal_events_table)
        .where(proposal_events_table.c.session_id == context.session_id)
        .where(proposal_events_table.c.proposal_id == str(invalidation.proposal_id))
        .where(proposal_events_table.c.event_type == "proposal.created")
    ).fetchall()
    if len(creation_rows) != 1:
        raise AuditIntegrityError("guided proposal invalidation requires one creation event")
    authority = _restore_authoritative_pipeline_proposal(
        conn=conn,
        row=_proposal_record_from_row(proposal_row),
        creation_event=_proposal_event_record_from_row(creation_rows[0]),
        reviewed_facts=deep_thaw(invalidation.reviewed_facts),
    )
    _verify_pipeline_lifecycle_authority(conn, service=context.service, authority=authority)
    if authority.row.status != "pending":
        raise AuditIntegrityError("guided proposal invalidation requires a pending proposal")
    proposal = authority.proposal
    if (
        proposal.draft_hash != active.draft_hash
        or proposal.base != active.base
        or proposal.reviewed_anchor_hash != active.reviewed_anchor_hash
        or proposal.covered_deferred_intent_ids != active.covered_deferred_intent_ids
        or authority.supersedes_proposal_id != active.supersedes_proposal_id
        or proposal.supersedes_draft_hash != active.supersedes_draft_hash
    ):
        raise AuditIntegrityError("guided proposal invalidation restored authority differs from checkpoint")
    if type(proposal.base) is not PresentBase:
        raise AuditIntegrityError("guided proposal invalidation base is not a persisted checkpoint")
    base_row = conn.execute(
        select(composition_states_table)
        .where(composition_states_table.c.session_id == context.session_id)
        .where(composition_states_table.c.id == str(proposal.base.state_id))
    ).one_or_none()
    if base_row is None:
        raise AuditIntegrityError("guided proposal invalidation base is missing or cross-session")
    base_record = context.service._row_to_state_record(base_row)
    if composition_content_hash(state_from_record(base_record)) != proposal.base.composition_content_hash:
        raise AuditIntegrityError("guided proposal invalidation base content binding changed")
    if proposal.base.composition_content_hash != context.expected_current_content_hash:
        raise AuditIntegrityError("guided proposal invalidation base content hash changed")
    return authority


def _verify_guided_pending_proposal_rebase(
    conn: Connection,
    *,
    context: _GuidedPendingProposalTransitionContext,
    rebase: GuidedPendingProposalRebase | None,
    carried: GuidedProposalRef | None,
) -> _GuidedPendingProposalRebasePlan | None:
    """Require a carried pending proposal to be anchored to the checkpoint written.

    THIS IS THE GUARD the carrying arm never had (elspeth-ed67eb9d0d).
    Every guided settlement mints a fresh ``composition_states`` row —
    ``GuidedSession``, ``chat_history`` included, lives inside
    ``composer_meta``, so an advisory chat or a declined revision is
    structurally a new version even when the graph is untouched — and a
    proposal carried across one kept its anchor on the PREVIOUS checkpoint.
    Nothing on the write path noticed, and the content-hash halves of the
    downstream guards could not notice either: the graph is empty until
    commit, so every version through Steps 1-3 shares one content hash. The
    damage surfaced only later, as a permanently unreadable guided session
    or a dead "edit this component" affordance.

    So a settlement that carries a live proposal must move its anchor onto
    the row being written, and moving it is a lifecycle fact requiring the
    exact rebase sideband. Sibling of
    :func:`_verify_guided_pending_proposal_invalidation`: every binding is
    re-derived from the live tree rather than trusted from the caller. The
    LOAD-BEARING admission is the last check — the checkpoint the anchor
    moves ONTO must carry the same composition content as the one it moves
    OFF, so a rebase can never launder a graph that actually changed.
    Without it the sideband would be a hole: any settlement could re-anchor
    a reviewed proposal onto an arbitrary later composition.
    """

    if carried is None:
        if rebase is not None:  # pragma: no cover - transition verifier owns this
            raise AuditIntegrityError("guided proposal rebase sideband did not carry an active proposal forward")
        return None
    if context.current_record is None:  # pragma: no cover - a carried ref implies a persisted prior checkpoint
        raise AuditIntegrityError("carrying a guided pending proposal requires a current checkpoint")
    proposal_row = conn.execute(
        select(composition_proposals_table)
        .where(composition_proposals_table.c.session_id == context.session_id)
        .where(composition_proposals_table.c.id == str(carried.proposal_id))
    ).one_or_none()
    if proposal_row is None:
        raise AuditIntegrityError("guided carried proposal authority is missing or cross-session")
    creation_rows = conn.execute(
        select(proposal_events_table)
        .where(proposal_events_table.c.session_id == context.session_id)
        .where(proposal_events_table.c.proposal_id == str(carried.proposal_id))
        .where(proposal_events_table.c.event_type == "proposal.created")
    ).fetchall()
    if len(creation_rows) != 1:
        raise AuditIntegrityError("guided carried proposal requires one creation event")
    # The reviewed-anchor assertion rides on the sideband, so it is checked
    # below rather than here: this restore also runs on the path where NO
    # sideband was supplied, which is the path whose whole purpose is to
    # refuse the settlement.
    authority = _restore_authoritative_pipeline_proposal(
        conn=conn,
        row=_proposal_record_from_row(proposal_row),
        creation_event=_proposal_event_record_from_row(creation_rows[0]),
        reviewed_facts=None,
    )
    _verify_pipeline_lifecycle_authority(conn, service=context.service, authority=authority)
    if authority.row.status != "pending":
        raise AuditIntegrityError("guided carried proposal requires a pending proposal")
    proposal = authority.proposal
    if (
        proposal.draft_hash != carried.draft_hash
        or proposal.base != carried.base
        or proposal.reviewed_anchor_hash != carried.reviewed_anchor_hash
        or proposal.covered_deferred_intent_ids != carried.covered_deferred_intent_ids
        or authority.supersedes_proposal_id != carried.supersedes_proposal_id
        or proposal.supersedes_draft_hash != carried.supersedes_draft_hash
    ):
        raise AuditIntegrityError("guided carried proposal restored authority differs from checkpoint")
    current_base = authority.current_base
    if type(current_base) is not PresentBase:
        raise AuditIntegrityError("guided carried proposal anchor is not a persisted checkpoint")
    if current_base.state_id == context.checkpoint_state_id:  # pragma: no cover - the checkpoint id is freshly minted
        if rebase is not None:
            raise AuditIntegrityError("guided proposal rebase sideband did not move a carried proposal anchor")
        return None
    if rebase is None:
        raise AuditIntegrityError(
            "a guided settlement carrying a pending proposal must rebase its anchor onto the checkpoint it writes: "
            f"{context.settlement_origin} writes checkpoint {context.checkpoint_state_id} while proposal "
            f"{carried.proposal_id} is still anchored to {current_base.state_id}"
        )
    if carried.proposal_id != rebase.proposal_id or carried.draft_hash != rebase.draft_hash:
        raise AuditIntegrityError(
            "guided proposal rebase sideband differs from checkpoint authority: "
            f"{context.settlement_origin} carried proposal {carried.proposal_id}"
        )
    if proposal.reviewed_anchor_hash != reviewed_anchor_hash(deep_thaw(rebase.reviewed_facts)):
        raise AuditIntegrityError("guided proposal rebase reviewed anchor does not match current server facts")
    if current_base.state_id != rebase.from_state_id:
        raise AuditIntegrityError(
            "guided proposal rebase sideband names an anchor the live proposal does not hold: "
            f"{context.settlement_origin} claims anchor {rebase.from_state_id} while proposal "
            f"{carried.proposal_id} holds {current_base.state_id}"
        )
    if current_base.composition_content_hash != rebase.composition_content_hash:
        raise AuditIntegrityError("guided proposal rebase sideband content hash differs from the live anchor")
    base_row = conn.execute(
        select(composition_states_table)
        .where(composition_states_table.c.session_id == context.session_id)
        .where(composition_states_table.c.id == str(current_base.state_id))
    ).one_or_none()
    if base_row is None:
        raise AuditIntegrityError("guided proposal rebase anchor is missing or cross-session")
    base_record = context.service._row_to_state_record(base_row)
    if composition_content_hash(state_from_record(base_record)) != current_base.composition_content_hash:
        raise AuditIntegrityError("guided proposal rebase anchor content binding changed")
    if current_base.composition_content_hash != context.expected_current_content_hash:
        raise AuditIntegrityError("guided proposal rebase anchor content hash changed")
    if context.candidate_content_hash != current_base.composition_content_hash:
        raise AuditIntegrityError(
            "guided proposal rebase would move a pending proposal onto a checkpoint whose composition content changed: "
            f"{context.settlement_origin} writes checkpoint {context.checkpoint_state_id} with content "
            f"{context.candidate_content_hash} while proposal {carried.proposal_id} was reviewed against "
            f"{current_base.composition_content_hash}"
        )
    return _GuidedPendingProposalRebasePlan(authority=authority, reason=rebase.reason)


def _rebind_guided_pending_proposal(
    conn: Connection,
    *,
    mutation: _GuidedSessionMutationTransaction,
    authority: AuthoritativePipelineProposal,
    reason: GuidedProposalRebaseReason,
    actor: str,
    created_at: datetime,
    to_state_id: UUID,
) -> None:
    """Append one immutable rebase event and re-pin the pending row's base.

    Non-terminal sibling of the mutation transaction's
    ``composer.reject_pending_proposal``: the row
    stays pending and keeps its ``audit_event_id`` terminal binding slot
    free, but ``base_state_id`` becomes lifecycle-managed. The event is the
    only durable record of the hop, so it is appended in the same
    transaction as the column write and the checkpoint that motivated it.

    RULING on the admission fence, which is deliberate and unconditional. A
    carrying settlement refuses with ``stale_conflict`` while an admitted
    confirmation still owns dispatch on this proposal, exactly as the
    terminalizing sibling does. The anchor move is inseparable from the
    checkpoint that motivates it, so an in-flight dispatch would fail its
    own ``expected_current_state_id`` check against that new head anyway:
    the fence buys nothing but the diagnosis, turning a late failure named
    after the wrong thing into an early conflict named after the right one.
    The window is bounded by the lease — an abandoned admission (the
    realistic producer is process death mid-confirm, the scenario
    ``test_expired_confirmation_takeover_recovers_without_duplicate_dispatch``
    models) releases its proposal locator on the first call past expiry,
    which is the release the fence performs.

    The fence is ``mutation.guided.require_no_active_confirmation``, the
    guided facet's own ``guided_operations`` writer, so the release and the
    active-owner check run under the settling operation's exact dual fence
    like every other guided-operation write. A module-level duplicate of
    that facet method once lived here on a raw connection; the narrow-domain
    rule (``test_guided_composite_authority_has_only_narrow_domain_mutations``)
    forbids exactly that, and the facet scopes the check by the fence's
    session, which is the proposal's session because both the fence and the
    rebase authority derive from the same settlement command.

    No owner discrimination is needed here, unlike
    :meth:`SessionServiceImpl.admit_guided_pipeline_confirmation`, which
    excepts its own operation so a re-entering confirmation can re-admit.
    That admission is the only writer that binds
    ``guided_operations.proposal_id`` and leaves the operation
    ``in_progress`` past its transaction; every other binder
    (``stage_``/``back_edit_``/``reject_``/``accept_guided_pipeline_proposal``)
    completes the operation in the same transaction, and the two settlements
    that reach here bind their own operation with no ``proposal_id`` at all.
    So the fence can never fire on the settling operation itself.
    """

    mutation.guided.require_no_active_confirmation(proposal_id=authority.row.id, now=created_at)
    _append_verified_guided_proposal_rebase(
        conn,
        authority=authority,
        reason=reason,
        actor=actor,
        created_at=created_at,
        to_state_id=to_state_id,
    )


def _append_verified_guided_proposal_rebase(
    conn: Connection,
    *,
    authority: AuthoritativePipelineProposal,
    reason: GuidedProposalRebaseReason,
    actor: str,
    created_at: datetime,
    to_state_id: UUID,
) -> None:
    """Append the verified hop under the caller's exact mutation authority.

    The guided settlement and ordinary checkpoint callers independently prove
    their operation fence and absence of live confirmation admission before
    entering this shared event/anchor write.
    """
    session_id = str(authority.row.session_id)
    proposal_id = str(authority.row.id)
    if type(authority.current_base) is not PresentBase:
        raise AuditIntegrityError("guided proposal rebase lost its persisted anchor")
    from_state_id = authority.current_base.state_id
    event_id = str(uuid.uuid4())
    conn.execute(
        insert(proposal_events_table).values(
            id=event_id,
            session_id=session_id,
            proposal_id=proposal_id,
            event_type="proposal.rebased",
            actor=actor,
            payload=_pipeline_rebased_payload(
                authority=authority,
                reason=reason,
                from_state_id=from_state_id,
                to_state_id=to_state_id,
                composition_content_hash_value=authority.current_base.composition_content_hash,
            ),
            created_at=created_at,
        )
    )
    updated = conn.execute(
        update(composition_proposals_table)
        .where(composition_proposals_table.c.session_id == session_id)
        .where(composition_proposals_table.c.id == proposal_id)
        .where(composition_proposals_table.c.status == "pending")
        .where(composition_proposals_table.c.base_state_id == str(from_state_id))
        .values(
            base_state_id=str(to_state_id),
            updated_at=created_at,
        )
    )
    if updated.rowcount != 1:
        raise AuditIntegrityError("guided proposal rebase lost the pending proposal CAS")


def _carried_guided_checkpoint_session(composer_meta: object, *, role: str) -> GuidedSession:
    """Parse a checkpoint's guided session, treating absence as "no guided walk".

    Deliberately more tolerant than the parser inside
    :meth:`SessionServiceImpl.settle_guided_state_operation`, and the
    difference is the question being asked. A RESPOND/CHAT settlement IS a
    guided state transition, so a checkpoint of one without a guided session
    is corruption. The guided-full surface instead writes a checkpoint over
    whatever head it observed — including a freeform head that never had a
    guided session — so absence there means only that there is no proposal to
    carry, and the transition classifies as "nothing to do".

    A guided session that is PRESENT but unparseable is still corruption,
    both here and there: an unreadable checkpoint must never be silently
    downgraded to "no pending proposal", which is exactly how a carried
    proposal would slip past the transition guard.
    """

    from elspeth.web.composer.guided.errors import InvariantError
    from elspeth.web.composer.guided.state_machine import GuidedSession

    if composer_meta is None:
        return GuidedSession.initial()
    metadata = deep_thaw(composer_meta)
    if type(metadata) is not dict:
        raise AuditIntegrityError(f"{role} guided checkpoint metadata is malformed")
    if "guided_session" not in metadata:
        return GuidedSession.initial()
    if type(metadata["guided_session"]) is not dict:
        raise AuditIntegrityError(f"{role} guided checkpoint is malformed")
    try:
        return GuidedSession.from_dict(metadata["guided_session"])
    except (InvariantError, KeyError, TypeError, ValueError) as exc:
        raise AuditIntegrityError(f"{role} guided checkpoint is malformed") from exc


def _verify_guided_pending_proposal_transition(
    conn: Connection,
    *,
    context: _GuidedPendingProposalTransitionContext,
    invalidation: GuidedPendingProposalInvalidation | None,
    rebase: GuidedPendingProposalRebase | None,
) -> _GuidedPendingProposalAuthorities:
    """Classify one settlement's pending-proposal transition and verify it."""

    refs = _require_guided_pending_proposal_transition(context, invalidation, rebase)
    return _GuidedPendingProposalAuthorities(
        invalidated=_verify_guided_pending_proposal_invalidation(
            conn,
            context=context,
            invalidation=invalidation,
            active=refs.cleared,
        ),
        rebased=_verify_guided_pending_proposal_rebase(
            conn,
            context=context,
            rebase=rebase,
            carried=refs.carried,
        ),
    )
