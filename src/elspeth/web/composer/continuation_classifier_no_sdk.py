"""Live, one-use authority for a classifier PRIMARY that never entered the SDK."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_primary_no_sdk import ContinuationRetainedTailUnbounded
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot

if TYPE_CHECKING:
    from elspeth.web.composer.provider_quota import ProviderCallCustody
    from elspeth.web.sessions.continuation_classifier_primary import PreparedClassifierPrimary, V4ClassifierProviderAdmission


class ClassifierNoSdkRefusalReason(Enum):
    PREPARATION_EXPIRED = "preparation_expired"
    PRE_SDK_DEADLINE = "pre_sdk_deadline"
    CHARGEABLE_REFUSED = "chargeable_refused"
    COST_REFUSED = "cost_refused"
    STORAGE_FAILURE = "storage_failure"
    INTEGRITY_REFUSED = "integrity_refused"
    CANCELLED_BEFORE_SDK = "cancelled_before_sdk"
    RETAINED_TAIL_UNBOUNDED = "retained_tail_unbounded"


@dataclass(frozen=True, slots=True)
class ClassifierNoSdkRefusalPermit:
    issuer: ClassifierNoSdkIssuer
    prepared: PreparedClassifierPrimary
    admission: V4ClassifierProviderAdmission
    reason: ClassifierNoSdkRefusalReason
    original_failure: BaseException
    receipt_key: str


class ClassifierNoSdkIssuer:
    """Nominal invocation and custody proof, never reconstructed from SQL absence."""

    __slots__ = (
        "_attempt",
        "_attempt_model",
        "_custody",
        "_dispatched",
        "_dispatched_candidate",
        "_input_sha256",
        "_lock",
        "_permit",
        "_prepared",
        "_prepared_candidate",
        "_selected_checkpoint_id",
        "_selected_cursor",
        "_state",
        "clock",
        "running",
        "work",
    )

    def __init__(
        self,
        *,
        running: ComposerOperationRunning,
        work: RequiredWorkBinding,
        selected_checkpoint_id: str,
        selected_cursor: int,
        input_sha256: str,
        clock: ContinuationActionClock,
    ) -> None:
        if (
            type(running) is not ComposerOperationRunning
            or type(work) is not RequiredWorkBinding
            or work.running is not running
            or type(clock) is not ContinuationActionClock
            or type(selected_checkpoint_id) is not str
            or not selected_checkpoint_id
            or type(selected_cursor) is not int
            or selected_cursor < 0
            or type(input_sha256) is not str
            or len(input_sha256) != 64
        ):
            raise AuditIntegrityError("classifier no-SDK issuer requires its original nominal invocation")
        self.running = running
        self.work = work
        self.clock = clock
        self._selected_checkpoint_id = selected_checkpoint_id
        self._selected_cursor = selected_cursor
        self._input_sha256 = input_sha256
        self._lock = RLock()
        self._state = "open"
        self._prepared_candidate: PreparedClassifierPrimary | None = None
        self._prepared: PreparedClassifierPrimary | None = None
        self._dispatched_candidate: StoredContinuationSnapshot | None = None
        self._dispatched: StoredContinuationSnapshot | None = None
        self._custody: ProviderCallCustody | None = None
        self._attempt: ProviderAttempt | None = None
        self._attempt_model: str | None = None
        self._permit: ClassifierNoSdkRefusalPermit | None = None

    @property
    def prepared_candidate(self) -> PreparedClassifierPrimary | None:
        with self._lock:
            return self._prepared_candidate

    @property
    def dispatched_candidate(self) -> StoredContinuationSnapshot | None:
        with self._lock:
            return self._dispatched_candidate

    @property
    def original_checkpoint_id(self) -> str:
        return self._selected_checkpoint_id

    @property
    def original_cursor(self) -> int:
        return self._selected_cursor

    @property
    def input_sha256(self) -> str:
        return self._input_sha256

    def note_prepared_candidate(self, prepared: PreparedClassifierPrimary) -> None:
        from elspeth.web.sessions.continuation_classifier_primary import PreparedClassifierPrimary

        with self._lock:
            if self._state != "open" or self._prepared_candidate is not None or type(prepared) is not PreparedClassifierPrimary:
                raise AuditIntegrityError("classifier preparation candidate is foreign or repeated")
            self._prepared_candidate = prepared

    def bind_prepared(self, prepared: PreparedClassifierPrimary) -> None:
        from elspeth.web.sessions.continuation_classifier_primary import PreparedClassifierPrimary

        with self._lock:
            if (
                self._state != "open"
                or self._prepared is not None
                or prepared is not self._prepared_candidate
                or type(prepared) is not PreparedClassifierPrimary
                or prepared.binding.running is not self.running
                or prepared.binding.family != 0
                or prepared.binding.input_sha256 != self._input_sha256
                or prepared.snapshot.cursor != self._selected_cursor + 1
                or prepared.snapshot.checkpoint_id == self._selected_checkpoint_id
            ):
                raise AuditIntegrityError("classifier no-SDK issuer changed its committed preparation")
            self._prepared = prepared

    def bind_custody(self, custody: ProviderCallCustody) -> None:
        from elspeth.web.composer.provider_quota import ProviderCallCustody

        with self._lock:
            if (
                self._state != "open"
                or self._prepared is None
                or self._custody is not None
                or type(custody) is not ProviderCallCustody
                or custody.context is not self.running.session_operation_context
                or custody.continuation_primary_clock is not self.clock
            ):
                raise AuditIntegrityError("classifier no-SDK issuer cannot bind foreign physical custody")
            self._custody = custody

    def note_dispatched_candidate(self, dispatched: StoredContinuationSnapshot) -> None:
        with self._lock:
            if (
                self._state != "open"
                or self._prepared is None
                or self._dispatched_candidate is not None
                or type(dispatched) is not StoredContinuationSnapshot
                or dispatched.cursor != self._prepared.snapshot.cursor + 1
            ):
                raise AuditIntegrityError("classifier dispatch candidate is foreign or repeated")
            self._dispatched_candidate = dispatched

    def bind_dispatched(self, dispatched: StoredContinuationSnapshot) -> None:
        with self._lock:
            if (
                self._state != "open"
                or self._prepared is None
                or self._dispatched is not None
                or dispatched is not self._dispatched_candidate
                or dispatched.checkpoint_id == self._prepared.snapshot.checkpoint_id
            ):
                raise AuditIntegrityError("classifier no-SDK issuer changed its committed dispatch")
            self._dispatched = dispatched

    def note_admitted_attempt(self, custody: ProviderCallCustody, attempt: ProviderAttempt, *, model: str) -> None:
        with self._lock:
            if (
                self._state != "open"
                or custody is not self._custody
                or type(attempt) is not ProviderAttempt
                or type(model) is not str
                or not model
                or self._attempt is not None
            ):
                raise AuditIntegrityError("classifier no-SDK issuer lost its exact admitted attempt")
            self._attempt = attempt
            self._attempt_model = model

    def note_sdk_entry(self, custody: ProviderCallCustody) -> None:
        with self._lock:
            if self._state != "open" or custody is not self._custody or self._dispatched is None or self._attempt is None:
                raise AuditIntegrityError("classifier SDK entry conflicts with no-SDK disposition")
            self._state = "sdk_entered"

    def reserve_v4_refusal(
        self, *, admission: V4ClassifierProviderAdmission, reason: ClassifierNoSdkRefusalReason, original_failure: BaseException
    ) -> ClassifierNoSdkRefusalPermit:
        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
        from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
        from elspeth.web.sessions.continuation_classifier_primary import V4ClassifierProviderAdmission

        with self._lock:
            if self._prepared is None or self._custody is None:
                raise AuditIntegrityError("classifier no-SDK refusal has no original source or custody")
            root = decode_continuation_snapshot(self._prepared.snapshot.payload_json)
            if (
                type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
                or type(admission) is not V4ClassifierProviderAdmission
                or self._state != "open"
                or self._dispatched is not admission.dispatched
                or self._attempt is not admission.attempt
                or self._attempt_model != admission.no_sdk_tail.requested_model
                or self._prepared.binding.effect_key != admission.effect_key
                or self._permit is not None
                or type(reason) is not ClassifierNoSdkRefusalReason
                or (
                    reason in (ClassifierNoSdkRefusalReason.PREPARATION_EXPIRED, ClassifierNoSdkRefusalReason.PRE_SDK_DEADLINE)
                    and type(original_failure) is not TimeoutError
                )
                or (reason is ClassifierNoSdkRefusalReason.CHARGEABLE_REFUSED and type(original_failure) is not ChargeableAdmissionRefused)
                or (reason is ClassifierNoSdkRefusalReason.COST_REFUSED and type(original_failure) is not CommonProviderCostRefused)
                or (reason is ClassifierNoSdkRefusalReason.STORAGE_FAILURE and not isinstance(original_failure, SQLAlchemyError))
                or (reason is ClassifierNoSdkRefusalReason.INTEGRITY_REFUSED and not isinstance(original_failure, AuditIntegrityError))
                or (reason is ClassifierNoSdkRefusalReason.CANCELLED_BEFORE_SDK and type(original_failure) is not asyncio.CancelledError)
                or (
                    reason is ClassifierNoSdkRefusalReason.RETAINED_TAIL_UNBOUNDED
                    and type(original_failure) is not ContinuationRetainedTailUnbounded
                )
            ):
                raise AuditIntegrityError("classifier no-SDK refusal lacks proved unentered admitted custody")
            self._custody.require_v4_classifier_unentered(admission)
            permit = ClassifierNoSdkRefusalPermit(self, self._prepared, admission, reason, original_failure, str(uuid4()))
            self._permit = permit
            self._state = "refusing"
            return permit

    def require_refusal_permit(self, permit: ClassifierNoSdkRefusalPermit) -> None:
        with self._lock:
            if (
                self._state not in ("refusing", "committed")
                or type(permit) is not ClassifierNoSdkRefusalPermit
                or permit is not self._permit
            ):
                raise AuditIntegrityError("classifier no-SDK permit is foreign, reused or entered")

    def mark_refusal_committed(self, permit: ClassifierNoSdkRefusalPermit) -> None:
        with self._lock:
            self.require_refusal_permit(permit)
            self._state = "committed"

    def mark_unknown(self) -> None:
        with self._lock:
            if self._state == "open":
                self._state = "unknown"
