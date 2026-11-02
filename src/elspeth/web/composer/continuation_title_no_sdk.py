"""Original title custody proof for an admitted but unentered SDK call."""

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
from elspeth.web.required_work import RequiredWorkRole

if TYPE_CHECKING:
    from elspeth.web.composer.provider_quota import ProviderCallCustody
    from elspeth.web.sessions.continuation_title_provider import PreparedTitleProvider, V4TitleProviderAdmission


class TitleNoSdkRefusalReason(Enum):
    GUARD_REFUSED = "guard_refused"
    COST_REFUSED = "cost_refused"
    CHARGEABLE_REFUSED = "chargeable_refused"
    PRE_SDK_DEADLINE = "pre_sdk_deadline"
    CANCELLED_BEFORE_SDK = "cancelled_before_sdk"


@dataclass(frozen=True, slots=True)
class TitleNoSdkRefusalPermit:
    issuer: TitleNoSdkIssuer
    prepared: PreparedTitleProvider
    admission: V4TitleProviderAdmission
    reason: TitleNoSdkRefusalReason
    original_failure: BaseException
    observed_model: str
    receipt_key: str


class TitleNoSdkIssuer:
    """One original TITLE source, physical admission and observed failure.

    Missing SQL audit rows do not establish this proof. The original gateway
    must bind custody and its newly admitted attempt before SDK entry. The
    original caller records the actual raised exception before requesting the
    one permit. SQL still revalidates the current source and attempt under lock.
    """

    def __init__(self, prepared: PreparedTitleProvider) -> None:
        from elspeth.web.sessions.continuation_title_provider import PreparedTitleProvider

        if type(prepared) is not PreparedTitleProvider or prepared.work.running is not prepared.running:
            raise AuditIntegrityError("title no-SDK issuer requires its original prepared operation")
        self.prepared = prepared
        self._lock = RLock()
        self._custody: ProviderCallCustody | None = None
        self._admission: V4TitleProviderAdmission | None = None
        self._observed_model: str | None = None
        self._failure: BaseException | None = None
        self._permit: TitleNoSdkRefusalPermit | None = None
        self._sdk_entered = False

    def bind_custody(self, custody: ProviderCallCustody) -> None:
        from elspeth.web.composer.provider_quota import ProviderCallCustody

        with self._lock:
            if (
                type(custody) is not ProviderCallCustody
                or self._custody is not None
                or custody.continuation_title is not self.prepared
                or custody.context is not self.prepared.running.session_operation_context
                or custody.required_work.role is not RequiredWorkRole.TITLE
                or custody.required_work.coordinator is not self.prepared.work.coordinator
                or custody.required_work.transition_ordinal != self.prepared.work.transition_ordinal
                or custody.required_work.semantic_ordinal != 6
                or custody.attempt is not None
                or custody.call is not None
            ):
                raise AuditIntegrityError("title no-SDK issuer changed its original unadmitted custody")
            self._custody = custody

    def bind_admission(self, custody: ProviderCallCustody, admission: V4TitleProviderAdmission, *, observed_model: str) -> None:
        from elspeth.web.sessions.continuation_title_provider import V4TitleProviderAdmission

        with self._lock:
            if (
                custody is not self._custody
                or self._admission is not None
                or type(admission) is not V4TitleProviderAdmission
                or admission.tail.admitted != admission.selected
                or observed_model != self.prepared.request.kwargs["model"]
                or type(observed_model) is not str
                or not observed_model
            ):
                raise AuditIntegrityError("title no-SDK issuer changed its original TITLE source or model")
            custody.require_v4_title_unentered(admission)
            self._admission, self._observed_model = admission, observed_model

    def note_sdk_entry(self, custody: ProviderCallCustody) -> None:
        with self._lock:
            if custody is not self._custody or self._sdk_entered or self._permit is not None:
                raise AuditIntegrityError("TITLE no-SDK issuer changed its original SDK entry")
            self._sdk_entered = True

    def note_original_failure(self, custody: ProviderCallCustody, error: BaseException) -> None:
        with self._lock:
            if custody is not self._custody or self._failure is not None or self._permit is not None:
                raise AuditIntegrityError("title no-SDK issuer replaced its original failure")
            self._failure = error

    def reserve_refusal(self, *, reason: TitleNoSdkRefusalReason, original_failure: BaseException) -> TitleNoSdkRefusalPermit:
        with self._lock:
            expected = {
                TitleNoSdkRefusalReason.GUARD_REFUSED: PromptGuardRefused,
                TitleNoSdkRefusalReason.COST_REFUSED: CommonProviderCostRefused,
                TitleNoSdkRefusalReason.CHARGEABLE_REFUSED: ChargeableAdmissionRefused,
                TitleNoSdkRefusalReason.PRE_SDK_DEADLINE: TimeoutError,
                TitleNoSdkRefusalReason.CANCELLED_BEFORE_SDK: asyncio.CancelledError,
            }
            if (
                type(reason) is not TitleNoSdkRefusalReason
                or type(original_failure) is not expected[reason]
                or original_failure is not self._failure
                or self._custody is None
                or self._admission is None
                or self._observed_model is None
                or self._permit is not None
                or self._sdk_entered
            ):
                raise AuditIntegrityError("title no-SDK refusal lost its original unentered failure")
            self._custody.require_v4_title_unentered(self._admission)
            permit = TitleNoSdkRefusalPermit(
                self, self.prepared, self._admission, reason, original_failure, self._observed_model, str(uuid4())
            )
            self._permit = permit
            return permit

    def require_refusal_permit(self, permit: TitleNoSdkRefusalPermit) -> None:
        with self._lock:
            if (
                type(permit) is not TitleNoSdkRefusalPermit
                or permit is not self._permit
                or permit.issuer is not self
                or permit.prepared is not self.prepared
                or permit.admission is not self._admission
                or permit.original_failure is not self._failure
                or permit.observed_model != self._observed_model
                or self._custody is None
                or self._admission is None
                or self._sdk_entered
            ):
                raise AuditIntegrityError("title no-SDK refusal permit is foreign or copied")
            self._custody.require_v4_title_unentered(self._admission)
