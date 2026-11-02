"""Typed failed END result; never a publication, retry, or partial verdict."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.web.composer.continuation_checkpoint_end_dispositions import (
    EndCheckpointDisposition,
    _EndCheckpointDispositionControl,
)
from elspeth.web.sessions.continuation_paid_incomplete_terminal import VerifiedPaidIncompleteTerminal


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


async def observe_end_checkpoint_terminal[T](execution: Awaitable[T]) -> T | EndCheckpointPaidIncompleteTerminal | EndCheckpointDisposition:
    """Stop before the original END caller's effectful continuation tail."""
    try:
        return await execution
    except _EndCheckpointPaidIncompleteControl as control:
        return control.terminal
    except _EndCheckpointDispositionControl as control:
        return control.terminal
