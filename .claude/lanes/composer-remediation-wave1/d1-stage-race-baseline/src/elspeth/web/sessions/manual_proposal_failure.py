"""Privately issued presentation custody for a completed manual proposal."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, replace
from typing import Any

from starlette.exceptions import HTTPException

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_sql_outcomes import RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import (
    ChildFailureOutcome,
    ComposerFailureReduction,
    PublicationProjectionUnusedMetadata,
    RejectionProjectionUnusedMetadata,
    RequiredAuthorityKind,
    RequiredWorkAuthority,
    RequiredWorkCoordinator,
    RequiredWorkKey,
    RequiredWorkTicket,
    RevocationWorkUnusedMetadata,
    _category,
    project_composer_failure_witnesses,
    reduce_composer_failures,
    required_failure_leaves,
)
from elspeth.web.sessions.composer_operations import ComposerOperationError


class ComposerManualProposalFailure(Exception):
    """A nominal carrier; constructor possession does not grant presentation."""

    def __init__(self, message: str = "Completed manual proposal failure") -> None:
        super().__init__(message)
        self._coordinator: RequiredWorkCoordinator | None = None
        self._observation: ManualProposalFailureObservation | None = None


@dataclass(frozen=True, slots=True)
class _ClosedInvocation:
    lease: SessionOperationLease
    originals: tuple[BaseException, ...]
    presentation_attempted: bool = False
    presentation_consumed: bool = False
    observation: ManualProposalFailureObservation | None = None


@dataclass(frozen=True, slots=True)
class _TicketCustody:
    ticket: RequiredWorkTicket
    key: RequiredWorkKey
    originals: tuple[BaseException, ...]
    custody_originals: tuple[BaseException, ...]
    child_outcome: ChildFailureOutcome | None
    future: Future[Any] | None
    submission_unknown: bool
    generation_joined: bool
    projection_started: bool
    no_submission: bool
    result_identity: int
    handoff: RequiredSQLRaised | RequiredSQLReturned[Any] | None
    unused_metadata: PublicationProjectionUnusedMetadata | RevocationWorkUnusedMetadata | RejectionProjectionUnusedMetadata | None


@dataclass(frozen=True, slots=True)
class _InvocationCustody:
    coordinator: RequiredWorkCoordinator
    authority: RequiredWorkAuthority
    tickets: tuple[_TicketCustody, ...]
    registrations: tuple[tuple[RequiredWorkTicket, RequiredWorkCoordinator], ...]
    children: tuple[tuple[str, _InvocationCustody], ...]


@dataclass(frozen=True, slots=True)
class ManualProposalFailureObservation:
    coordinator: RequiredWorkCoordinator
    lease: SessionOperationLease
    original_root: BaseException
    reduction: ComposerFailureReduction
    selected_original: BaseException
    category_rank: int
    witnesses: tuple[BaseException, ...]
    receipt_identity: tuple[tuple[RequiredWorkKey, tuple[int, ...]], ...]
    policy_at_issue: ComposerOperationError
    ticket_identity: tuple[tuple[RequiredWorkKey, int, tuple[int, ...]], ...]
    selected_cause: BaseException | None
    selected_context: BaseException | None
    close_originals: tuple[BaseException, ...]
    invocation_custody: _InvocationCustody

    def project(self, *, request_id: str | None, timeout_seconds: float) -> ComposerOperationError:
        return project_composer_failure_witnesses(
            self.reduction.winner.key,
            self.category_rank,
            self.witnesses,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
        )


def observe_manual_proposal_close(
    lease: SessionOperationLease,
    coordinator: RequiredWorkCoordinator,
    originals: tuple[BaseException, ...],
) -> None:
    """Called only by the owned helper after joining the actual close task."""
    if type(lease) is not SessionOperationLease or type(coordinator) is not RequiredWorkCoordinator:
        raise AuditIntegrityError("Manual close observation requires owned lease and coordinator")
    if coordinator.authority.authority_kind is not RequiredAuthorityKind.MANUAL_PROPOSAL:
        raise AuditIntegrityError("Manual close observation has another authority kind")
    if lease.context != coordinator.authority.context or lease.required_work is not coordinator or not lease.closed:
        raise AuditIntegrityError("Manual close observation differs from the joined lease")
    if not coordinator.all_completed:
        return
    coordinator.assert_completed()
    with coordinator._lock:
        if coordinator._manual_proposal_close is not None or coordinator._manual_proposal_carrier is not None:
            raise AuditIntegrityError("Manual close completion cannot be observed twice")
        coordinator._manual_proposal_close = _ClosedInvocation(lease, originals)


def _receipt_identity(coordinator: RequiredWorkCoordinator) -> tuple[tuple[RequiredWorkKey, tuple[int, ...]], ...]:
    # Receipt DTOs and multi-error group wrappers are regenerated on demand;
    # the actual registered key and original witnesses remain the proof.
    return tuple(
        (receipt.key, tuple(id(witness) for witness in receipt.original_category_witnesses)) for receipt in coordinator.failure_receipts()
    )


def _explicit_originals(root: BaseException) -> tuple[BaseException, ...]:
    """Original coverage keeps cancellations even when policy follows a cause."""
    if isinstance(root, BaseExceptionGroup):
        return tuple(leaf for child in root.exceptions for leaf in _explicit_originals(child))
    return (root,)


def _capture_invocation_custody(coordinator: RequiredWorkCoordinator) -> _InvocationCustody:
    """Bind actual tickets recursively, including each producer's child SQL."""
    with coordinator._lock:
        coordinator.assert_completed()
        tickets = []
        for ticket in coordinator.tickets:
            with ticket._lock:
                handoff = ticket._finish_once_handoff
                if handoff is not None and not isinstance(handoff, (RequiredSQLRaised, RequiredSQLReturned)):
                    raise AuditIntegrityError("Manual presentation ticket has an unowned finish-once observation")
                outcome = ticket._child_outcome
                if outcome is not None:
                    stored = tuple((receipt.key, tuple(map(id, receipt.original_category_witnesses))) for receipt in outcome.child_receipts)
                    if _receipt_identity(outcome.child) != stored:
                        raise AuditIntegrityError("Manual presentation child receipts differ from actual registered custody")
                tickets.append(
                    _TicketCustody(
                        ticket,
                        ticket.key,
                        tuple(ticket._errors),
                        tuple(ticket._custody_errors),
                        outcome,
                        ticket._future,
                        ticket._submission_unknown,
                        ticket._generation_joined,
                        ticket._projection_started,
                        ticket._no_submission,
                        id(ticket._result),
                        handoff,
                        ticket._unused_metadata,
                    )
                )
        return _InvocationCustody(
            coordinator,
            coordinator.authority,
            tuple(tickets),
            tuple(coordinator._child_registrations.items()),
            tuple((proposal_id, _capture_invocation_custody(child)) for proposal_id, child in coordinator._proposal_children.items()),
        )


def _same_originals(current: tuple[BaseException, ...], expected: tuple[BaseException, ...]) -> bool:
    return len(current) == len(expected) and all(a is b for a, b in zip(current, expected, strict=True))


def _registered_originals(snapshot: _InvocationCustody) -> tuple[BaseException, ...]:
    """Category witnesses never substitute for explicit observed originals."""
    originals: list[BaseException] = []
    for retained in snapshot.tickets:
        roots = (*retained.originals, *retained.custody_originals)
        if retained.child_outcome is not None:
            roots = (*roots, retained.child_outcome.original_root)
        if retained.handoff is not None:
            roots = (*roots, *retained.handoff.deferred_cancellations)
            if isinstance(retained.handoff, RequiredSQLRaised):
                roots = (*roots, retained.handoff.error)
        originals.extend(leaf for root in roots for leaf in _explicit_originals(root))
    for _proposal_id, child in snapshot.children:
        originals.extend(_registered_originals(child))
    return tuple(originals)


def _validate_invocation_custody(snapshot: _InvocationCustody) -> None:
    coordinator = snapshot.coordinator
    with coordinator._lock:
        coordinator.assert_completed()
        current_tickets = coordinator.tickets
        if coordinator.authority is not snapshot.authority or len(current_tickets) != len(snapshot.tickets):
            raise AuditIntegrityError("Manual presentation invocation ticket authority changed")
        for current, retained in zip(current_tickets, snapshot.tickets, strict=True):
            with current._lock:
                if (
                    current is not retained.ticket
                    or current._child_outcome is not retained.child_outcome
                    or not _same_originals(tuple(current._errors), retained.originals)
                    or not _same_originals(tuple(current._custody_errors), retained.custody_originals)
                ):
                    raise AuditIntegrityError("Manual presentation registered ticket originals changed")
                if (
                    current.key is not retained.key
                    or current._coordinator is not coordinator
                    or current._future is not retained.future
                    or current._submission_unknown != retained.submission_unknown
                    or current._generation_joined != retained.generation_joined
                    or current._projection_started != retained.projection_started
                    or current._no_submission != retained.no_submission
                    or id(current._result) != retained.result_identity
                    or current._finish_once_handoff is not retained.handoff
                    or current._unused_metadata is not retained.unused_metadata
                ):
                    raise AuditIntegrityError("Manual presentation actual completion observation changed")
        registrations = tuple(coordinator._child_registrations.items())
        if len(registrations) != len(snapshot.registrations) or any(
            a is not c or b is not d for (a, b), (c, d) in zip(registrations, snapshot.registrations, strict=True)
        ):
            raise AuditIntegrityError("Manual presentation child registration changed")
        children = tuple(coordinator._proposal_children.items())
        if len(children) != len(snapshot.children):
            raise AuditIntegrityError("Manual presentation child invocation changed")
        for (proposal_id, child), (retained_id, child_snapshot) in zip(children, snapshot.children, strict=True):
            if proposal_id != retained_id or child is not child_snapshot.coordinator:
                raise AuditIntegrityError("Manual presentation child invocation changed")
            _validate_invocation_custody(child_snapshot)


def issue_manual_proposal_failure(
    coordinator: RequiredWorkCoordinator,
    lease: SessionOperationLease,
    original_root: BaseException,
) -> ComposerManualProposalFailure | None:
    if type(coordinator) is not RequiredWorkCoordinator or type(lease) is not SessionOperationLease:
        raise AuditIntegrityError("Manual presentation requires its actual invocation")
    with coordinator._lock:
        closed = coordinator._manual_proposal_close
        if closed is None or closed.lease is not lease:
            return None
        if closed.presentation_attempted or coordinator._manual_proposal_carrier is not None:
            raise AuditIntegrityError("Manual presentation invocation has already been attempted")
        coordinator._manual_proposal_close = replace(closed, presentation_attempted=True)
        if (
            not lease.closed
            or lease.required_work is not coordinator
            or lease.context != coordinator.authority.context
            or not coordinator.all_completed
        ):
            return None
        coordinator.assert_completed()
        invocation_custody = _capture_invocation_custody(coordinator)
        receipts = coordinator.failure_receipts()
        if not receipts:
            return None
        reduction = reduce_composer_failures(receipts)
        leaves = required_failure_leaves(original_root)
        identities = {id(leaf) for leaf in leaves}
        registered = {id(witness) for receipt in receipts for witness in receipt.original_category_witnesses}
        close_leaves = tuple(leaf for root in closed.originals for leaf in _explicit_originals(root))
        explicit_identities = {id(leaf) for leaf in _explicit_originals(original_root)}
        registered_originals = _registered_originals(invocation_custody)
        if not registered <= identities or any(id(leaf) not in explicit_identities for leaf in close_leaves):
            return None
        if any(id(leaf) not in explicit_identities for leaf in registered_originals):
            return None
        extra = tuple(leaf for leaf in leaves if id(leaf) not in registered)
        key = reduction.winner.key
        selected_category = min(_category(witness, key) for witness in reduction.witnesses)
        # Preserve a child's actual accounting category rather than reclassify
        # its witness through the parent continuation's stage.
        selected_category = (reduction.category_rank, selected_category[1])
        if any(_category(leaf, key) < selected_category for leaf in extra):
            return None
        # Manual cancellation/fence loss has no durable worker-loss mapping.
        if selected_category[0] in (60, 70, 80):
            return None
        witnesses = (*reduction.witnesses, *(leaf for leaf in extra if _category(leaf, key) == selected_category))
        # The shared durable error DTO accepts only 400..599. A manual
        # original outside that domain remains the unchanged full root;
        # a sole HTTP original can use the existing global HTTP handler.
        if any(isinstance(witness, HTTPException) and not 400 <= witness.status_code <= 599 for witness in witnesses):
            return None
        policy = project_composer_failure_witnesses(key, reduction.category_rank, witnesses, request_id=None, timeout_seconds=0)
        observation = ManualProposalFailureObservation(
            coordinator,
            lease,
            original_root,
            reduction,
            reduction.witnesses[0],
            reduction.category_rank,
            witnesses,
            _receipt_identity(coordinator),
            policy,
            tuple((ticket.key, id(ticket), tuple(map(id, ticket.errors))) for ticket in coordinator.tickets),
            reduction.witnesses[0].__cause__,
            reduction.witnesses[0].__context__,
            closed.originals,
            invocation_custody,
        )
        carrier = ComposerManualProposalFailure()
        carrier.__cause__ = original_root
        carrier._coordinator = coordinator
        carrier._observation = observation
        coordinator._manual_proposal_close = replace(closed, presentation_attempted=True, observation=observation)
        coordinator._manual_proposal_carrier = carrier
        return carrier


def consume_manual_proposal_failure(carrier: ComposerManualProposalFailure) -> ManualProposalFailureObservation:
    if type(carrier) is not ComposerManualProposalFailure:
        raise AuditIntegrityError("Manual presentation requires its exact owned carrier")
    coordinator = carrier._coordinator
    if type(coordinator) is not RequiredWorkCoordinator:
        raise AuditIntegrityError("Manual presentation carrier has no actual issuer")
    with coordinator._lock:
        observation = carrier._observation
        if coordinator._manual_proposal_carrier is not carrier or type(observation) is not ManualProposalFailureObservation:
            raise AuditIntegrityError("Manual presentation carrier is unissued or consumed")
        closed = coordinator._manual_proposal_close
        if (
            type(closed) is not _ClosedInvocation
            or not closed.presentation_attempted
            or closed.presentation_consumed
            or closed.observation is not observation
            or closed.lease is not observation.lease
            or observation.coordinator is not coordinator
        ):
            raise AuditIntegrityError("Manual presentation differs from its actual joined invocation")
        if not _same_originals(closed.originals, observation.close_originals):
            raise AuditIntegrityError("Manual presentation joined close originals changed")
        if (
            not observation.lease.closed
            or observation.lease.required_work is not coordinator
            or observation.lease.context != coordinator.authority.context
            or coordinator.authority.authority_kind is not RequiredAuthorityKind.MANUAL_PROPOSAL
            or coordinator.authority != observation.reduction.winner.key.authority
            or not coordinator.all_completed
        ):
            raise AuditIntegrityError("Manual presentation custody is incomplete")
        coordinator.assert_completed()
        _validate_invocation_custody(observation.invocation_custody)
        if tuple((ticket.key, id(ticket), tuple(map(id, ticket.errors))) for ticket in coordinator.tickets) != observation.ticket_identity:
            raise AuditIntegrityError("Manual presentation registered tickets changed")
        if (
            observation.selected_original.__cause__ is not observation.selected_cause
            or observation.selected_original.__context__ is not observation.selected_context
        ):
            raise AuditIntegrityError("Manual presentation original cause/context changed")
        if _receipt_identity(coordinator) != observation.receipt_identity:
            raise AuditIntegrityError("Manual presentation original observations changed")
        current = reduce_composer_failures(coordinator.failure_receipts())
        if current.winner.key != observation.reduction.winner.key or current.category_rank != observation.reduction.category_rank:
            raise AuditIntegrityError("Manual presentation selected receipt changed")
        if tuple(map(id, current.witnesses)) != tuple(map(id, observation.reduction.witnesses)):
            raise AuditIntegrityError("Manual presentation selected original changed")
        if observation.selected_original is not observation.reduction.witnesses[0]:
            raise AuditIntegrityError("Manual presentation supplied another selected original")
        leaves = required_failure_leaves(observation.original_root)
        identities = {id(leaf) for leaf in leaves}
        receipts = coordinator.failure_receipts()
        registered = {id(witness) for receipt in receipts for witness in receipt.original_category_witnesses}
        close_leaves = tuple(leaf for root in observation.close_originals for leaf in _explicit_originals(root))
        explicit_identities = {id(leaf) for leaf in _explicit_originals(observation.original_root)}
        if not registered <= identities or any(id(leaf) not in explicit_identities for leaf in close_leaves):
            raise AuditIntegrityError("Manual presentation full original coverage changed")
        if any(id(leaf) not in explicit_identities for leaf in _registered_originals(observation.invocation_custody)):
            raise AuditIntegrityError("Manual presentation registered explicit original coverage changed")
        key = current.winner.key
        category = (current.category_rank, min(_category(witness, key)[1] for witness in current.witnesses))
        extra = tuple(leaf for leaf in leaves if id(leaf) not in registered)
        if category[0] in (60, 70, 80) or any(_category(leaf, key) < category for leaf in extra):
            raise AuditIntegrityError("Manual presentation retained a higher unregistered original")
        witnesses = (*current.witnesses, *(leaf for leaf in extra if _category(leaf, key) == category))
        if tuple(map(id, witnesses)) != tuple(map(id, observation.witnesses)):
            raise AuditIntegrityError("Manual presentation equal-category originals changed")
        if carrier.__cause__ is not observation.original_root:
            raise AuditIntegrityError("Manual presentation explicit original root changed")
        if observation.project(request_id=None, timeout_seconds=0) != observation.policy_at_issue:
            raise AuditIntegrityError("Manual presentation policy evidence changed")
        coordinator._manual_proposal_close = replace(closed, presentation_consumed=True)
        return observation
