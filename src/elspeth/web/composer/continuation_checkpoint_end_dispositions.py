"""Distinct END overflow and admitted-but-unentered terminal carriers.

These carry verified SQL results to the driver, which rechecks them cold before
releasing the operation. A source-only refusal has no physical attempt and must
never be represented by the admitted no-SDK carrier.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.web.sessions.continuation_checkpoint_no_sdk_terminal import VerifiedCheckpointNoSdkTerminal
from elspeth.web.sessions.continuation_checkpoint_paid_terminal import VerifiedCheckpointPaidOverflowTerminal
from elspeth.web.sessions.continuation_checkpoint_unbound_refusal import VerifiedCheckpointUnboundRefusal


@dataclass(frozen=True, slots=True)
class EndCheckpointPaidOverflowTerminal:
    verified: VerifiedCheckpointPaidOverflowTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "deferred_cancellations")
        if (
            type(self.verified) is not VerifiedCheckpointPaidOverflowTerminal
            or self.verified.phase != "end"
            or self.verified.hold_state != "consumed"
        ):
            raise AuditIntegrityError("END overflow requires its original settled checkpoint terminal")


@dataclass(frozen=True, slots=True)
class EndCheckpointNoSdkTerminal:
    verified: VerifiedCheckpointNoSdkTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "deferred_cancellations")
        if (
            type(self.verified) is not VerifiedCheckpointNoSdkTerminal
            or self.verified.phase != "end"
            or self.verified.hold_state != "consumed"
        ):
            raise AuditIntegrityError("END no-SDK requires its original admitted and cancelled checkpoint terminal")


@dataclass(frozen=True, slots=True)
class EndCheckpointUnboundRefusalTerminal:
    verified: VerifiedCheckpointUnboundRefusal
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()

    def __post_init__(self) -> None:
        freeze_fields(self, "deferred_cancellations")
        if type(self.verified) is not VerifiedCheckpointUnboundRefusal or self.verified.error.http_status != 422:
            raise AuditIntegrityError("END unbound refusal requires its original unpaid source terminal")


type EndCheckpointDisposition = EndCheckpointPaidOverflowTerminal | EndCheckpointNoSdkTerminal | EndCheckpointUnboundRefusalTerminal


class _EndCheckpointDispositionControl(Exception):
    def __init__(self, terminal: EndCheckpointDisposition) -> None:
        if type(terminal) not in (EndCheckpointPaidOverflowTerminal, EndCheckpointNoSdkTerminal, EndCheckpointUnboundRefusalTerminal):
            raise TypeError("END disposition requires its exact terminal family")
        super().__init__("checkpoint_end_terminal")
        self.terminal = terminal
