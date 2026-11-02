"""Typed failed END result; never a publication, retry, or partial verdict."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.web.composer.continuation_checkpoint_end_dispositions import (
    EndCheckpointDisposition,
    _EndCheckpointDispositionControl,
)
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal

if TYPE_CHECKING:
    from elspeth.web.sessions.continuation_checkpoint_end_retry import VerifiedEndFormatRetry
    from elspeth.web.sessions.continuation_semantic_review_result import SemanticReviewCommitted


@dataclass(frozen=True, slots=True)
class EndCheckpointPaidIncompleteTerminal:
    verified: VerifiedPaidIncompleteTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "deferred_cancellations")
        if type(self.verified) is not VerifiedPaidIncompleteTerminal or self.verified.receipt.producer != "checkpoint_advisor":
            raise AuditIntegrityError("END terminal requires its original checkpoint paid-incomplete receipt")


class _EndCheckpointPaidIncompleteControl(Exception):
    def __init__(self, terminal: EndCheckpointPaidIncompleteTerminal) -> None:
        if type(terminal) is not EndCheckpointPaidIncompleteTerminal:
            raise TypeError("END control requires its nominal terminal")
        super().__init__("paid_result_incomplete")
        self.terminal = terminal


@dataclass(frozen=True, slots=True)
class EndCheckpointCompleteReview:
    """The original paid Review action was retained, not yet published."""

    review: SemanticReviewCommitted

    def __post_init__(self) -> None:
        from elspeth.web.sessions.continuation_semantic_review_result import SemanticReviewCommitted

        if type(self.review) is not SemanticReviewCommitted or self.review.outcome not in ("clean", "blocked"):
            raise AuditIntegrityError("END complete Review lost its cold-proved action")


class _EndCheckpointCompleteReviewControl(Exception):
    def __init__(self, completed: EndCheckpointCompleteReview) -> None:
        if type(completed) is not EndCheckpointCompleteReview:
            raise TypeError("END Review control requires its nominal completed action")
        super().__init__("complete_review_retained")
        self.completed = completed


@dataclass(frozen=True, slots=True)
class EndCheckpointRepairContinued:
    """One paid Review completed and retained an authored repair successor."""

    completed: StoredContinuationSnapshot

    def __post_init__(self) -> None:
        if type(self.completed) is not StoredContinuationSnapshot:
            raise AuditIntegrityError("END repair continuation lost its committed successor")


class _EndCheckpointRepairContinuedControl(Exception):
    def __init__(self, continued: EndCheckpointRepairContinued) -> None:
        if type(continued) is not EndCheckpointRepairContinued:
            raise TypeError("END repair control requires its nominal successor")
        super().__init__("complete_review_repair_retained")
        self.continued = continued


@dataclass(frozen=True, slots=True)
class EndCheckpointFormatRetryReady:
    """The first paid END child closed; a later action must restore its retry gate."""

    proof: VerifiedEndFormatRetry
    completed: StoredContinuationSnapshot

    def __post_init__(self) -> None:
        from elspeth.web.sessions.continuation_checkpoint_end_retry import VerifiedEndFormatRetry

        if (
            type(self.proof) is not VerifiedEndFormatRetry
            or type(self.completed) is not StoredContinuationSnapshot
            or self.completed != self.proof.selected
        ):
            raise AuditIntegrityError("END format retry handoff changed its original current retained source")


class _EndCheckpointFormatRetryReadyControl(Exception):
    def __init__(self, ready: EndCheckpointFormatRetryReady) -> None:
        if type(ready) is not EndCheckpointFormatRetryReady:
            raise TypeError("END format retry control requires its nominal retained source")
        super().__init__("format_retry_ready")
        self.ready = ready


async def observe_end_checkpoint_terminal[T](
    execution: Awaitable[T],
) -> (
    T
    | EndCheckpointPaidIncompleteTerminal
    | EndCheckpointDisposition
    | EndCheckpointCompleteReview
    | EndCheckpointRepairContinued
    | EndCheckpointFormatRetryReady
):
    """Stop before the original END caller's effectful continuation tail."""
    try:
        return await execution
    except _EndCheckpointPaidIncompleteControl as control:
        return control.terminal
    except _EndCheckpointDispositionControl as control:
        return control.terminal
    except _EndCheckpointCompleteReviewControl as control:
        return control.completed
    except _EndCheckpointRepairContinuedControl as control:
        return control.continued
    except _EndCheckpointFormatRetryReadyControl as control:
        return control.ready
