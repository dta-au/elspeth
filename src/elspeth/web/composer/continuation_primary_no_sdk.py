"""Live, one-use proof that an issued PRIMARY has not crossed SDK entry."""

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
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import PreparedContinuationProvider, StoredContinuationSnapshot

if TYPE_CHECKING:
    from elspeth.web.composer.provider_quota import ProviderCallCustody
    from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission


class PrimaryNoSdkRefusalReason(Enum):
    PREPARATION_EXPIRED = "preparation_expired"
    PRE_SDK_DEADLINE = "pre_sdk_deadline"
    CHARGEABLE_REFUSED = "chargeable_refused"
    COST_REFUSED = "cost_refused"
    STORAGE_FAILURE = "storage_failure"
    INTEGRITY_REFUSED = "integrity_refused"
    CANCELLED_BEFORE_SDK = "cancelled_before_sdk"
    RETAINED_TAIL_UNBOUNDED = "retained_tail_unbounded"


class ContinuationRetainedTailUnbounded(Exception):
    """A complete paid result has no proven finite producer bound."""


@dataclass(frozen=True, slots=True)
class PrimaryNoSdkRefusalPermit:
    issuer: PrimaryNoSdkIssuer
    prepared: PreparedContinuationProvider
    dispatched: StoredContinuationSnapshot | None
    reason: PrimaryNoSdkRefusalReason
    original_failure: BaseException
    receipt_key: str
    undispatched_attempt_id: str | None
    undispatched_model: str | None


class PrimaryNoSdkIssuer:
    """A live invocation authority, never reconstructed from cold SQL absence."""

    __slots__ = (
        "_attempt_id",
        "_attempt_model",
        "_cancelled_attempt",
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
            raise AuditIntegrityError("PRIMARY no-SDK issuer requires the original nominal invocation")
        self.running = running
        self.work = work
        self.clock = clock
        self._selected_checkpoint_id = selected_checkpoint_id
        self._selected_cursor = selected_cursor
        self._input_sha256 = input_sha256
        self._lock = RLock()
        self._state = "open"
        self._prepared: PreparedContinuationProvider | None = None
        self._prepared_candidate: PreparedContinuationProvider | None = None
        self._dispatched: StoredContinuationSnapshot | None = None
        self._dispatched_candidate: StoredContinuationSnapshot | None = None
        self._custody: ProviderCallCustody | None = None
        self._attempt_id: str | None = None
        self._attempt_model: str | None = None
        self._cancelled_attempt = False
        self._permit: PrimaryNoSdkRefusalPermit | None = None

    @property
    def original_checkpoint_id(self) -> str:
        return self._selected_checkpoint_id

    @property
    def original_cursor(self) -> int:
        return self._selected_cursor

    @property
    def input_sha256(self) -> str:
        return self._input_sha256

    @property
    def prepared_candidate(self) -> PreparedContinuationProvider | None:
        with self._lock:
            return self._prepared_candidate

    @property
    def dispatched_candidate(self) -> StoredContinuationSnapshot | None:
        with self._lock:
            return self._dispatched_candidate

    def note_prepared_candidate(self, prepared: PreparedContinuationProvider) -> None:
        with self._lock:
            if self._state != "open" or self._prepared_candidate is not None or type(prepared) is not PreparedContinuationProvider:
                raise AuditIntegrityError("PRIMARY preparation candidate is foreign or repeated")
            self._prepared_candidate = prepared

    def note_dispatched_candidate(self, dispatched: StoredContinuationSnapshot) -> None:
        with self._lock:
            if (
                self._state != "open"
                or self._prepared is None
                or self._dispatched_candidate is not None
                or type(dispatched) is not StoredContinuationSnapshot
            ):
                raise AuditIntegrityError("PRIMARY dispatch candidate is foreign or repeated")
            self._dispatched_candidate = dispatched

    def bind_prepared(self, prepared: PreparedContinuationProvider) -> None:
        with self._lock:
            if (
                self._state != "open"
                or self._prepared is not None
                or prepared is not self._prepared_candidate
                or type(prepared) is not PreparedContinuationProvider
                or prepared.binding.running is not self.running
                or prepared.binding.family != 0
                or prepared.binding.input_sha256 != self._input_sha256
                or prepared.binding.effect_key != f"provider:0:{prepared.binding.ordinal}"
                or prepared.snapshot.cursor != self._selected_cursor + 1
                or prepared.snapshot.checkpoint_id == self._selected_checkpoint_id
            ):
                raise AuditIntegrityError("PRIMARY no-SDK issuer changed its actual committed preparation")
            self._prepared = prepared

    def bind_dispatched(self, dispatched: StoredContinuationSnapshot) -> None:
        with self._lock:
            if (
                self._state != "open"
                or self._prepared is None
                or self._dispatched is not None
                or dispatched is not self._dispatched_candidate
                or type(dispatched) is not StoredContinuationSnapshot
                or dispatched.cursor != self._prepared.snapshot.cursor + 1
                or dispatched.checkpoint_id == self._prepared.snapshot.checkpoint_id
            ):
                raise AuditIntegrityError("PRIMARY no-SDK issuer changed its actual committed dispatch")
            self._dispatched = dispatched

    def bind_cold_recovered_dispatched(self, dispatched: StoredContinuationSnapshot) -> None:
        """Replace only an equal in-worker candidate after a trusted cold SQL proof."""
        with self._lock:
            if (
                self._state != "open"
                or self._prepared is None
                or self._dispatched is not None
                or type(dispatched) is not StoredContinuationSnapshot
                or self._dispatched_candidate is None
                or self._dispatched_candidate != dispatched
            ):
                raise AuditIntegrityError("PRIMARY cold admission did not match its original SQL dispatch candidate")
            self._dispatched_candidate = dispatched
            self.bind_dispatched(dispatched)

    def bind_custody(self, custody: ProviderCallCustody) -> None:
        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
        from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
        from elspeth.web.composer.provider_quota import ProviderCallCustody

        with self._lock:
            v4_prepared = False
            if self._prepared is not None:
                root = decode_continuation_snapshot(self._prepared.snapshot.payload_json)
                v4_prepared = (
                    type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is ContinuationAdmissionPolicyV4
                )
            if (
                self._state != "open"
                or self._prepared is None
                or (self._dispatched is None and not v4_prepared)
                or self._custody is not None
                or type(custody) is not ProviderCallCustody
                or custody.context is not self.running.session_operation_context
                or custody.continuation_primary_clock is not self.clock
            ):
                raise AuditIntegrityError("PRIMARY no-SDK issuer cannot bind a foreign physical custody")
            self._custody = custody

    def reserve_v4_refusal(
        self, *, admission: V4PrimaryProviderAdmission, reason: PrimaryNoSdkRefusalReason, original_failure: BaseException
    ) -> PrimaryNoSdkRefusalPermit:
        """Reserve a proven unentered admitted attempt before one atomic SQL cancellation."""
        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
        from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
        from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission

        with self._lock:
            if self._prepared is None:
                raise AuditIntegrityError("V4 PRIMARY no-SDK refusal has no original prepared source")
            root = decode_continuation_snapshot(self._prepared.snapshot.payload_json)
            if (
                type(decode_continuation_policy(root.admission_policy_json, root.policy_hash)) is not ContinuationAdmissionPolicyV4
                or type(admission) is not V4PrimaryProviderAdmission
                or self._state != "open"
                or self._dispatched is None
                or self._dispatched != admission.selected
                or self._custody is None
                or self._attempt_id is None
                or self._attempt_id != admission.attempt.attempt_id
                or self._attempt_model is None
                or self._cancelled_attempt
                or self._permit is not None
                or type(reason) is not PrimaryNoSdkRefusalReason
                or (
                    reason in (PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED, PrimaryNoSdkRefusalReason.PRE_SDK_DEADLINE)
                    and type(original_failure) is not TimeoutError
                )
                or (reason is PrimaryNoSdkRefusalReason.CHARGEABLE_REFUSED and type(original_failure) is not ChargeableAdmissionRefused)
                or (reason is PrimaryNoSdkRefusalReason.COST_REFUSED and type(original_failure) is not CommonProviderCostRefused)
                or (reason is PrimaryNoSdkRefusalReason.STORAGE_FAILURE and not isinstance(original_failure, SQLAlchemyError))
                or (reason is PrimaryNoSdkRefusalReason.INTEGRITY_REFUSED and not isinstance(original_failure, AuditIntegrityError))
                or (reason is PrimaryNoSdkRefusalReason.CANCELLED_BEFORE_SDK and type(original_failure) is not asyncio.CancelledError)
                or (
                    reason is PrimaryNoSdkRefusalReason.RETAINED_TAIL_UNBOUNDED
                    and type(original_failure) is not ContinuationRetainedTailUnbounded
                )
            ):
                raise AuditIntegrityError("V4 PRIMARY no-SDK refusal lacks proved unentered admitted custody")
            self._custody.require_v4_primary_unentered(admission)
            permit = PrimaryNoSdkRefusalPermit(
                self,
                self._prepared,
                self._dispatched,
                reason,
                original_failure,
                str(uuid4()),
                self._attempt_id,
                self._attempt_model,
            )
            self._permit = permit
            self._state = "refusing"
            return permit

    def note_admitted_attempt(self, custody: ProviderCallCustody, attempt: ProviderAttempt, *, model: str) -> None:
        with self._lock:
            if (
                self._state != "open"
                or custody is not self._custody
                or type(attempt) is not ProviderAttempt
                or type(model) is not str
                or not model
                or self._attempt_id is not None
            ):
                raise AuditIntegrityError("PRIMARY no-SDK issuer lost its exact admitted attempt")
            self._attempt_id = attempt.attempt_id
            self._attempt_model = model

    def note_undispatched_cancel(self, custody: ProviderCallCustody, attempt: ProviderAttempt, *, model: str) -> None:
        with self._lock:
            if (
                self._state != "open"
                or custody is not self._custody
                or type(attempt) is not ProviderAttempt
                or self._attempt_id != attempt.attempt_id
                or self._attempt_model != model
                or self._cancelled_attempt
            ):
                raise AuditIntegrityError("PRIMARY no-SDK issuer lost its actual undispatched disposition")
            self._cancelled_attempt = True

    def mark_sdk_entered(self, custody: ProviderCallCustody) -> None:
        with self._lock:
            if self._state != "open" or custody is not self._custody or self._dispatched is None:
                raise AuditIntegrityError("PRIMARY SDK entry conflicts with a pending no-SDK disposition")
            self._state = "sdk_entered"

    def reserve_refusal(self, *, reason: PrimaryNoSdkRefusalReason, original_failure: BaseException) -> PrimaryNoSdkRefusalPermit:
        with self._lock:
            if (
                self._state != "open"
                or self._prepared is None
                or type(reason) is not PrimaryNoSdkRefusalReason
                or (
                    reason in (PrimaryNoSdkRefusalReason.PREPARATION_EXPIRED, PrimaryNoSdkRefusalReason.PRE_SDK_DEADLINE)
                    and type(original_failure) is not TimeoutError
                )
                or (reason is PrimaryNoSdkRefusalReason.CHARGEABLE_REFUSED and type(original_failure) is not ChargeableAdmissionRefused)
                or (reason is PrimaryNoSdkRefusalReason.COST_REFUSED and type(original_failure) is not CommonProviderCostRefused)
                or (reason is PrimaryNoSdkRefusalReason.STORAGE_FAILURE and not isinstance(original_failure, SQLAlchemyError))
                or (reason is PrimaryNoSdkRefusalReason.INTEGRITY_REFUSED and not isinstance(original_failure, AuditIntegrityError))
                or (reason is PrimaryNoSdkRefusalReason.CANCELLED_BEFORE_SDK and type(original_failure) is not asyncio.CancelledError)
                or (
                    reason is PrimaryNoSdkRefusalReason.RETAINED_TAIL_UNBOUNDED
                    and type(original_failure) is not ContinuationRetainedTailUnbounded
                )
                or (self._attempt_id is not None and not self._cancelled_attempt)
                or self._permit is not None
            ):
                raise AuditIntegrityError("PRIMARY no-SDK refusal lacks live unentered settled custody")
            permit = PrimaryNoSdkRefusalPermit(
                self,
                self._prepared,
                self._dispatched,
                reason,
                original_failure,
                str(uuid4()),
                self._attempt_id,
                self._attempt_model,
            )
            self._permit = permit
            self._state = "refusing"
            return permit

    def can_refuse(self) -> bool:
        with self._lock:
            return self._state == "open" and self._prepared is not None and (self._attempt_id is None or self._cancelled_attempt)

    def require_refusal_permit(self, permit: PrimaryNoSdkRefusalPermit) -> None:
        with self._lock:
            if self._state not in ("refusing", "committed") or type(permit) is not PrimaryNoSdkRefusalPermit or permit is not self._permit:
                raise AuditIntegrityError("PRIMARY no-SDK refusal permit is foreign, reused or entered")

    def mark_refusal_committed(self, permit: PrimaryNoSdkRefusalPermit) -> None:
        with self._lock:
            self.require_refusal_permit(permit)
            self._state = "committed"

    def mark_unknown(self) -> None:
        with self._lock:
            if self._state == "open":
                self._state = "unknown"
