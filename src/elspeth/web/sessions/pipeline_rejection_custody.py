"""Actual required rejection/creation projection and explicit original custody."""

from __future__ import annotations

import asyncio
import errno
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Never, cast
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkCoordinator, RequiredWorkSource, RequiredWorkTicket
from elspeth.web.sessions.manual_proposal_failure import issue_manual_proposal_failure, observe_manual_proposal_close
from elspeth.web.sessions.pipeline_rejection import PipelineRejectionExpected
from elspeth.web.sessions.pipeline_rejection_finish_once import (
    PipelineCreationRaised,
    PipelineCreationReturned,
    PipelineRejectionRaised,
    PipelineRejectionReturned,
    decode_pipeline_rejection_result,
)
from elspeth.web.sessions.protocol import AuthoritativeCompositionProposal, AuthoritativePipelineProposal, CompositionProposalRecord

if TYPE_CHECKING:
    from elspeth.web.coordination.lifecycle import SessionOperationLease
    from elspeth.web.sessions.pipeline_rejection_finish_once import PipelineCreationFinishOnce
    from elspeth.web.sessions.protocol import SessionServiceProtocol


def original_outcome_group(label: str, *roots: BaseException) -> BaseException:
    """Keep every explicit original leaf, deduplicating only by identity."""
    leaves: list[BaseException] = []

    def retain(root: BaseException) -> None:
        if isinstance(root, BaseExceptionGroup):
            for child in root.exceptions:
                retain(child)
        elif all(root is not existing for existing in leaves):
            leaves.append(root)

    for root in roots:
        retain(root)
    if not leaves:
        raise AuditIntegrityError("Original outcome grouping needs an actual original")
    return leaves[0] if len(leaves) == 1 else BaseExceptionGroup(label, leaves)


def _owned_projection_original(error: BaseException) -> BaseException:
    if isinstance(error, (AuditIntegrityError, SQLAlchemyError)) or (
        isinstance(error, OSError) and error.errno in (errno.EIO, errno.ENOSPC, errno.EROFS)
    ):
        return error
    marker = ComposerOwnedSettlementFailure()
    marker.__cause__ = error
    return original_outcome_group("Owned rejection projection original failure", marker, error)


def project_creation_handoff(
    binding: RequiredWorkBinding,
    creation_ticket: RequiredWorkTicket,
    projection_ticket: RequiredWorkTicket,
    handoff: PipelineCreationFinishOnce,
) -> tuple[CompositionProposalRecord | None, tuple[BaseException, ...], tuple[asyncio.CancelledError, ...]]:
    if type(handoff) not in (PipelineCreationReturned, PipelineCreationRaised):
        raise AuditIntegrityError("Creation requires its closed actual handoff")
    binding.coordinator.verify_creation_outcome(creation_ticket=creation_ticket, actual_outcome=handoff.sql_outcome)
    if type(handoff) is PipelineCreationRaised:
        projection_ticket.complete_without_submission()
        raised = handoff
        return None, (raised.sql_outcome.error,), raised.sql_outcome.deferred_cancellations
    returned = cast(PipelineCreationReturned, handoff)
    projection_ticket.begin_projection()
    failures = tuple(_owned_projection_original(error) for error in returned.post_sql_failures)
    try:
        row = returned.sql_outcome.value
        if type(row) is not CompositionProposalRecord or row.status != "pending" or str(row.session_id) != creation_ticket.key.session_id:
            raise AuditIntegrityError("Creation actual SQL result differs from its registered session/status")
    except BaseException as error:
        observed = _owned_projection_original(error)
        projection_ticket.complete_owned(observed)
        return None, (*failures, observed), returned.sql_outcome.deferred_cancellations
    if failures:
        projection_ticket.complete_owned(original_outcome_group("Creation post-SQL projection failures", *failures))
    else:
        projection_ticket.complete_owned()
    return row, failures, returned.sql_outcome.deferred_cancellations


async def reject_pipeline_with_required_custody(
    service: SessionServiceProtocol,
    *,
    expected: PipelineRejectionExpected,
    binding: RequiredWorkBinding,
) -> CompositionProposalRecord:
    if type(binding) is not RequiredWorkBinding or type(expected) is not PipelineRejectionExpected:
        raise AuditIntegrityError("Rejection requires owned binding and expectation")
    binding.validate_context(expected.context)
    sql, projection = binding.reserve_pair(RequiredWorkSource.PROPOSAL_REJECTION_SQL, RequiredWorkSource.PROPOSAL_REJECTION_PROJECTION)
    handoff = await service.reject_pipeline_composition_proposal_finish_once(
        expected=expected,
        coordinator=binding.coordinator,
        rejection_work=sql,
        rejection_projection_work=projection,
        transition_ordinal=binding.transition_ordinal,
        semantic_ordinal=binding.semantic_ordinal,
    )
    if type(handoff) is PipelineRejectionRaised:
        if handoff.projection_unused.actual_outcome is not handoff.sql_outcome or handoff.projection_unused.rejection_ticket is not sql:
            raise AuditIntegrityError("Rejection failure handoff differs from its privately issued actual outcome")
        projection.complete_unused(handoff.projection_unused)
        raise original_outcome_group(
            "Rejection actual SQL failure and deferred originals", handoff.sql_outcome.error, *handoff.sql_outcome.deferred_cancellations
        )
    if type(handoff) is not PipelineRejectionReturned:
        raise AuditIntegrityError("Rejection lacks its known nominal handoff")
    binding.coordinator.verify_rejection_returned(rejection_ticket=sql, actual_outcome=handoff.sql_outcome)
    projection.begin_projection()
    errors: list[BaseException] = []
    try:
        row = decode_pipeline_rejection_result(handoff.sql_outcome.value, expected)
    except BaseException as error:
        errors.append(_owned_projection_original(error))
        row = None
    errors.extend(_owned_projection_original(error) for error in handoff.post_sql_failures)
    if errors:
        projection.complete_owned(original_outcome_group("Rejection required projection failures", *errors))
    else:
        projection.complete_owned()
    errors.extend(handoff.sql_outcome.deferred_cancellations)
    if errors:
        raise original_outcome_group("Rejection result projection and original cancellations", *errors)
    if row is None:
        raise AuditIntegrityError("Rejection projection lost its actual result")
    return row


async def _join_owned_originals[T](task: asyncio.Future[T]) -> tuple[T, tuple[asyncio.CancelledError, ...]]:
    """Observe child completion without a synthetic shield cancellation.

    The result-only notification is never cancelled by the child. Every
    cancellation delivered while shielding it belongs to the actual caller.
    The callback never consumes the child's result/exception; result() below
    retrieves that exact original once. This is not a SQL-completion witness.
    """
    notification: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def child_finished(_child: asyncio.Future[T]) -> None:
        if not notification.done():
            notification.set_result(None)

    task.add_done_callback(child_finished)
    cancellations: list[asyncio.CancelledError] = []
    while True:
        try:
            await asyncio.shield(notification)
            break
        except asyncio.CancelledError as cancellation:
            if all(cancellation is not previous for previous in cancellations):
                cancellations.append(cancellation)
    try:
        return task.result(), tuple(cancellations)
    except BaseException as original:
        retained = original_outcome_group("Owned child original and all outer deliveries", original, *cancellations)
        if isinstance(retained, BaseExceptionGroup):
            raise retained from original
    raise retained


async def close_required_proposal_lease(
    lease: SessionOperationLease, *, coordinator: RequiredWorkCoordinator | None = None
) -> tuple[BaseException, ...]:
    """Join actual close and retain actual child/outer originals in all orders."""
    task = asyncio.create_task(lease.close())
    try:
        _result, cancellations = await _join_owned_originals(task)
        originals: tuple[BaseException, ...] = cancellations
    except BaseException as original:
        roots: list[BaseException] = []

        def retain(root: BaseException) -> None:
            if isinstance(root, BaseExceptionGroup):
                for child in root.exceptions:
                    retain(child)
            elif all(root is not existing for existing in roots):
                roots.append(root)

        retain(original)
        originals = tuple(roots)
    if coordinator is not None:
        observe_manual_proposal_close(lease, coordinator, originals)
    return originals


async def await_retained_originals[T](awaitable: Awaitable[T]) -> tuple[T, tuple[asyncio.CancelledError, ...]]:
    """Shield only a result notification and retrieve the actual child once."""
    return await _join_owned_originals(asyncio.ensure_future(awaitable))


async def read_rejection_authority(
    service: SessionServiceProtocol,
    *,
    binding: RequiredWorkBinding,
    session_id: UUID,
    proposal_id: UUID,
) -> tuple[AuthoritativePipelineProposal, tuple[asyncio.CancelledError, ...]]:
    sql, projection = binding.reserve_pair(RequiredWorkSource.PREPARATION_READ_SQL, RequiredWorkSource.PREPARATION_READ_PROJECTION)
    try:
        authority, cancellations = await await_retained_originals(
            service.get_authoritative_pipeline_proposal(
                session_id=session_id,
                proposal_id=proposal_id,
                required_work=sql,
            )
        )
    except BaseException as error:
        if sql.complete:
            projection.complete_without_submission(error)
        raise
    projection.begin_projection()
    try:
        if (
            type(authority) is not AuthoritativePipelineProposal
            or authority.row.session_id != session_id
            or authority.row.id != proposal_id
        ):
            raise AuditIntegrityError("Rejection preparation returned foreign immutable authority")
    except BaseException as error:
        observed = _owned_projection_original(error)
        projection.complete_owned(observed)
        retained = original_outcome_group("Preparation projection and retained originals", observed, *cancellations)
        if isinstance(retained, BaseExceptionGroup):
            raise retained from error
    else:
        projection.complete_owned()
        return authority, cancellations
    raise retained


async def read_composition_rejection_authority(
    service: SessionServiceProtocol,
    *,
    binding: RequiredWorkBinding,
    session_id: UUID,
    proposal_id: UUID,
) -> tuple[AuthoritativeCompositionProposal, tuple[asyncio.CancelledError, ...]]:
    sql, projection = binding.reserve_pair(RequiredWorkSource.PREPARATION_READ_SQL, RequiredWorkSource.PREPARATION_READ_PROJECTION)
    try:
        authority, cancellations = await await_retained_originals(
            service.get_authoritative_composition_proposal(
                session_id=session_id,
                proposal_id=proposal_id,
                required_work=sql,
            )
        )
    except BaseException as error:
        if sql.complete:
            projection.complete_without_submission(error)
        raise
    projection.begin_projection()
    try:
        if (
            type(authority) is not AuthoritativeCompositionProposal
            or authority.row.session_id != session_id
            or authority.row.id != proposal_id
        ):
            raise AuditIntegrityError("Manual rejection preparation returned foreign immutable authority")
    except BaseException as error:
        observed = _owned_projection_original(error)
        projection.complete_owned(observed)
        retained = original_outcome_group("Preparation projection and retained originals", observed, *cancellations)
        if isinstance(retained, BaseExceptionGroup):
            raise retained from error
    else:
        projection.complete_owned()
        return authority, cancellations
    raise retained


def raise_required_proposal_failure(
    coordinator: RequiredWorkCoordinator, original: BaseException, *, lease: SessionOperationLease
) -> Never:
    """Retain full originals; only privately proven manual coverage may render."""
    carrier = issue_manual_proposal_failure(coordinator, lease, original)
    if carrier is None:
        raise original
    raise carrier
