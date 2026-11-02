"""Typed SDK terminal projections after the PRIMARY cold reader proves closure."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.composer_operations import ComposerOperationError
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_sdk_terminal import SdkTerminalReceipt


@dataclass(frozen=True, slots=True)
class VerifiedSdkTerminal:
    source: ProviderTerminalHoldSource
    receipt: SdkTerminalReceipt
    terminal: StoredContinuationSnapshot
    provider_attempt_id: str
    hold_state: Literal["consumed"]

    def __post_init__(self) -> None:
        if (
            type(self.source) is not ProviderTerminalHoldSource
            or type(self.receipt) is not SdkTerminalReceipt
            or type(self.terminal) is not StoredContinuationSnapshot
            or self.hold_state != "consumed"
            or self.provider_attempt_id != self.receipt.identity.attempt_id
            or self.receipt.identity.session_id != self.source.session_id
            or self.receipt.identity.operation_id != self.source.operation_id
            or self.receipt.identity.effect_key != self.source.effect_key
            or self.receipt.identity.policy_sha256 != self.source.policy_sha256
            or self.receipt.identity.source_checkpoint_id != self.source.source_checkpoint_id
            or self.receipt.identity.source_cursor != self.source.source_cursor
            or self.receipt.identity.source_snapshot_sha256 != self.source.source_snapshot_sha256
            or self.receipt.identity.session_operation_id != self.source.session_operation_id
            or self.receipt.identity.session_operation_epoch != self.source.session_operation_epoch
            or self.receipt.identity.ownership_generation != self.source.creator_generation
            or self.receipt.identity.lease_sha256 != self.source.lease_sha256
        ):
            raise AuditIntegrityError("SDK terminal changed its original cold-proved hold")


@dataclass(frozen=True, slots=True)
class PrimarySdkTerminal:
    receipt: SdkTerminalReceipt
    snapshot: StoredContinuationSnapshot
    deferred_cancellations: tuple[asyncio.CancelledError, ...] = field(default=(), compare=False)


def sdk_terminal_error(request_id: str | None, receipt: SdkTerminalReceipt) -> ComposerOperationError:
    if type(receipt) is not SdkTerminalReceipt:
        raise TypeError("SDK terminal public projection requires its bounded receipt")
    return ComposerOperationError(
        http_status=502,
        failure_code="http_error",
        error_type="provider_sdk_terminal",
        body={
            "error_type": "provider_sdk_terminal",
            "detail": "The provider call did not produce an observed usable result. It was not retried.",
            "observation": receipt.observation,
            "retryable": False,
            "cost": {"status": receipt.cost.status, "amount": receipt.cost.amount},
            "request_id": request_id,
        },
        diagnostic_id=None,
    )
