"""Original checkpoint custody proof for an admitted but unentered SDK call."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import TYPE_CHECKING
from uuid import uuid4

from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.prompt_guard import PromptGuardRefused
from elspeth.web.composer.provider_cost import CommonProviderCostRefused

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_clock import ContinuationActionClock
    from elspeth.web.composer.provider_quota import ProviderCallCustody
    from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider, V4CheckpointProviderAdmission


class CheckpointNoSdkRefusalReason(Enum):
    GUARD_REFUSED = "guard_refused"
    COST_REFUSED = "cost_refused"
    CHARGEABLE_REFUSED = "chargeable_refused"
    PRE_SDK_DEADLINE = "pre_sdk_deadline"
    CANCELLED_BEFORE_SDK = "cancelled_before_sdk"


@dataclass(frozen=True, slots=True)
class CheckpointNoSdkRefusalPermit:
    issuer: CheckpointNoSdkIssuer
    prepared: PreparedCheckpointProvider
    admission: V4CheckpointProviderAdmission
    reason: CheckpointNoSdkRefusalReason
    original_failure: BaseException
    observed_model: str
    receipt_key: str


class CheckpointNoSdkIssuer:
    """One phase/retry source, original physical admission and observed failure.

    Missing SQL audit rows do not establish this proof. The original gateway
    must bind custody and its newly admitted attempt before SDK entry. The
    original caller records the actual raised exception before requesting the
    one permit. SQL still revalidates the current source and attempt under lock.
    """

    def __init__(self, prepared: PreparedCheckpointProvider, *, clock: ContinuationActionClock) -> None:
        from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkBinding, RequiredWorkRole
        from elspeth.web.sessions.composer_operations import ComposerOperationRunning
        from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider

        if (
            type(prepared) is not PreparedCheckpointProvider
            or type(prepared.running) is not ComposerOperationRunning
            or type(prepared.work) is not RequiredWorkBinding
            or prepared.work.running is not None
            or prepared.work.role is not RequiredWorkRole.TURN
            or prepared.work.coordinator.authority.authority_kind is not RequiredAuthorityKind.DURABLE_COMPOSE
            or prepared.work.coordinator.authority.context is not prepared.running.session_operation_context
            or prepared.work.coordinator.authority.durable_operation_id != prepared.running.claim.operation_id
            or prepared.work.coordinator.authority.claim_attempt != prepared.running.claim.attempt
        ):
            raise AuditIntegrityError("checkpoint no-SDK issuer requires its original prepared operation")
        self.prepared = prepared
        self.clock = clock
        self.require_original_clock()
        self._lock = RLock()
        self._custody: ProviderCallCustody | None = None
        self._admission: V4CheckpointProviderAdmission | None = None
        self._observed_model: str | None = None
        self._failure: BaseException | None = None
        self._permit: CheckpointNoSdkRefusalPermit | None = None

    def require_original_clock(self) -> None:
        from elspeth.web.composer.continuation_checkpoint_end_physical import require_checkpoint_end_prepared_clock

        if require_checkpoint_end_prepared_clock(self.prepared) is not self.clock:
            raise AuditIntegrityError("checkpoint no-SDK issuer replaced its original driver clock")

    def bind_custody(self, custody: ProviderCallCustody) -> None:
        from elspeth.web.composer.provider_quota import ProviderCallCustody

        with self._lock:
            if (
                type(custody) is not ProviderCallCustody
                or self._custody is not None
                or custody.required_work is not self.prepared.work
                or custody.attempt is not None
                or custody.call is not None
            ):
                raise AuditIntegrityError("checkpoint no-SDK issuer changed its original unadmitted custody")
            self._custody = custody

    def bind_admission(self, custody: ProviderCallCustody, admission: V4CheckpointProviderAdmission, *, observed_model: str) -> None:
        from elspeth.web.sessions.continuation_checkpoint_paid import V4CheckpointProviderAdmission

        with self._lock:
            if (
                custody is not self._custody
                or self._admission is not None
                or type(admission) is not V4CheckpointProviderAdmission
                or admission.original_source is not self.prepared.prepared_source
                or observed_model != self.prepared.requested_model
                or type(observed_model) is not str
                or not observed_model
            ):
                raise AuditIntegrityError("checkpoint no-SDK issuer changed its original phase, retry or model")
            custody.require_v4_checkpoint_unentered(admission)
            self._admission, self._observed_model = admission, observed_model

    def note_original_failure(self, custody: ProviderCallCustody, error: BaseException) -> None:
        with self._lock:
            if custody is not self._custody or self._failure is not None or self._permit is not None:
                raise AuditIntegrityError("checkpoint no-SDK issuer replaced its original failure")
            self._failure = error

    def reserve_refusal(self, *, reason: CheckpointNoSdkRefusalReason, original_failure: BaseException) -> CheckpointNoSdkRefusalPermit:
        with self._lock:
            expected = {
                CheckpointNoSdkRefusalReason.GUARD_REFUSED: PromptGuardRefused,
                CheckpointNoSdkRefusalReason.COST_REFUSED: CommonProviderCostRefused,
                CheckpointNoSdkRefusalReason.CHARGEABLE_REFUSED: ChargeableAdmissionRefused,
                CheckpointNoSdkRefusalReason.PRE_SDK_DEADLINE: TimeoutError,
                CheckpointNoSdkRefusalReason.CANCELLED_BEFORE_SDK: asyncio.CancelledError,
            }
            if (
                type(reason) is not CheckpointNoSdkRefusalReason
                or type(original_failure) is not expected[reason]
                or original_failure is not self._failure
                or self._custody is None
                or self._admission is None
                or self._observed_model is None
                or self._permit is not None
            ):
                raise AuditIntegrityError("checkpoint no-SDK refusal lost its original unentered failure")
            self._custody.require_v4_checkpoint_unentered(self._admission)
            permit = CheckpointNoSdkRefusalPermit(
                self, self.prepared, self._admission, reason, original_failure, self._observed_model, str(uuid4())
            )
            self._permit = permit
            return permit

    def require_refusal_permit(self, permit: CheckpointNoSdkRefusalPermit) -> None:
        with self._lock:
            if (
                type(permit) is not CheckpointNoSdkRefusalPermit
                or permit is not self._permit
                or permit.issuer is not self
                or permit.prepared is not self.prepared
                or permit.admission is not self._admission
                or permit.original_failure is not self._failure
                or permit.observed_model != self._observed_model
                or self._custody is None
                or self._admission is None
            ):
                raise AuditIntegrityError("checkpoint no-SDK refusal permit is foreign or copied")
            self._custody.require_v4_checkpoint_unentered(self._admission)
