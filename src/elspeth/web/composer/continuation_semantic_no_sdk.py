"""Original family-1 custody proof, never reconstructed from missing SQL rows."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import TYPE_CHECKING
from uuid import uuid4

from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.composer_llm_audit import ComposerLLMCall
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationAdmissionPolicyV4, decode_continuation_policy
from elspeth.web.composer.prompt_guard import PromptGuardRefused
from elspeth.web.composer.provider_cost import CommonProviderCostRefused
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import PreparedContinuationProvider, StoredContinuationSnapshot

if TYPE_CHECKING:
    from elspeth.web.composer.continuation_semantic_advisor import PreparedSemanticAdvisorRequest, SemanticAdvisorResult
    from elspeth.web.composer.continuation_semantic_advisor_outcome import (
        SemanticAdvisorInvocationObservation,
        SemanticAdvisorInvocationRaised,
        SemanticAdvisorInvocationReturned,
        SemanticAdvisorInvocationV4Finalized,
        _SemanticAdvisorInvocationCapture,
    )
    from elspeth.web.composer.provider_quota import ProviderCallCustody, V4ReturnedIncompleteFinalization
    from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete
    from elspeth.web.composer.provider_sdk_observation import V4SdkFailureObservation
    from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalFailure, SdkTerminalResult
    from elspeth.web.sessions.continuation_semantic_advisor import V4SemanticProviderAdmission


class SemanticAdvisorNoSdkRefusalReason(Enum):
    GUARD_REFUSED = "guard_refused"
    COST_REFUSED = "cost_refused"
    CHARGEABLE_REFUSED = "chargeable_refused"
    PRE_SDK_DEADLINE = "pre_sdk_deadline"
    CANCELLED_BEFORE_SDK = "cancelled_before_sdk"


@dataclass(frozen=True, slots=True)
class SemanticAdvisorNoSdkRefusalPermit:
    issuer: SemanticAdvisorNoSdkIssuer
    prepared: PreparedContinuationProvider
    dispatched: StoredContinuationSnapshot
    reason: SemanticAdvisorNoSdkRefusalReason
    original_failure: BaseException
    receipt_key: str
    undispatched_attempt_id: str | None
    undispatched_model: str | None


class SemanticAdvisorNoSdkIssuer:
    """One actual preparation, original minted custody and terminal refusal."""

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
            or not is_lower_sha256_hex(input_sha256)
        ):
            raise AuditIntegrityError("Semantic no-SDK issuer requires its original nominal invocation")
        self.running, self.work, self.clock = running, work, clock
        self._clock_policy = clock.policy
        self._clock_anchor = (clock.started, clock.deadline, clock.started_monotonic, clock.preparation_deadline_monotonic)
        self.original_checkpoint_id, self.original_cursor = selected_checkpoint_id, selected_cursor
        self.input_sha256 = input_sha256
        self._lock = RLock()
        self._state = "open"
        self._prepared_candidate: PreparedContinuationProvider | None = None
        self._prepared: PreparedContinuationProvider | None = None
        self._dispatched_candidate: StoredContinuationSnapshot | None = None
        self._dispatched: StoredContinuationSnapshot | None = None
        self._custody: ProviderCallCustody | None = None
        self._attempt_id: str | None = None
        self._attempt_model: str | None = None
        self._cancelled_attempt = False
        self._original_failure: BaseException | None = None
        self._permit: SemanticAdvisorNoSdkRefusalPermit | None = None
        self._captured_call: ComposerLLMCall | None = None
        self._request: PreparedSemanticAdvisorRequest | None = None
        self._capture: _SemanticAdvisorInvocationCapture | None = None
        self._body_observation: SemanticAdvisorInvocationObservation | None = None
        self._result: SemanticAdvisorResult | None = None
        self._outcome: SemanticAdvisorInvocationReturned | SemanticAdvisorInvocationRaised | None = None
        self._v4_finalized: SemanticAdvisorInvocationV4Finalized | None = None

    @property
    def prepared_candidate(self) -> PreparedContinuationProvider | None:
        return self._prepared_candidate

    def require_original_clock(self) -> None:
        if (
            self.clock.policy is not self._clock_policy
            or (
                self.clock.started,
                self.clock.deadline,
                self.clock.started_monotonic,
                self.clock.preparation_deadline_monotonic,
            )
            != self._clock_anchor
        ):
            raise AuditIntegrityError("Semantic original action clock cannot be replaced or renewed")

    def bind_request(self, request: PreparedSemanticAdvisorRequest) -> None:
        from elspeth.web.composer.continuation_semantic_advisor import PreparedSemanticAdvisorRequest

        with self._lock:
            if (
                self._state != "open"
                or self._request is not None
                or type(request) is not PreparedSemanticAdvisorRequest
                or request.request_sha256 != self.input_sha256
            ):
                raise AuditIntegrityError("Semantic issuer requires its one original prepared request")
            self._request = request

    def require_original_request(self, request: PreparedSemanticAdvisorRequest) -> None:
        if request is not self._request:
            raise AuditIntegrityError("Semantic issuer cannot replace its original request/provider/service owners")

    def bind_capture(self, custody: ProviderCallCustody, capture: _SemanticAdvisorInvocationCapture) -> None:
        from elspeth.web.composer.continuation_semantic_advisor_outcome import _SemanticAdvisorInvocationCapture

        with self._lock:
            if (
                custody is not self._custody
                or self._capture is not None
                or self._request is None
                or type(capture) is not _SemanticAdvisorInvocationCapture
                or not capture.claimed
                or capture.source is not self._request.source
                or capture.request_sha256 != self.input_sha256
            ):
                raise AuditIntegrityError("Semantic issuer lost its original helper capture")
            self._capture = capture

    def note_original_observation(self, custody: ProviderCallCustody, capture: _SemanticAdvisorInvocationCapture) -> None:
        with self._lock:
            if custody is not self._custody or capture is not self._capture or self._body_observation is not None:
                raise AuditIntegrityError("Semantic issuer cannot replace its original admitted observation")
            original = capture.snapshot()
            if original.audit_call is not self._captured_call or original.original_failure is not self._original_failure:
                raise AuditIntegrityError("Semantic observation changed its original audit/failure provenance")
            self._body_observation = original

    def note_original_result(self, result: SemanticAdvisorResult) -> None:
        from elspeth.web.composer.continuation_semantic_advisor import SemanticAdvisorResult

        with self._lock:
            original = self._body_observation
            if (
                self._state != "sdk_entered"
                or type(result) is not SemanticAdvisorResult
                or original is None
                or self._result is not None
                or result.source is not original.source
                or result.audit_call is not original.audit_call
                or result.decision is not original.decision
                or result.request_sha256 != self.input_sha256
            ):
                raise AuditIntegrityError("Semantic issuer lost its original returned result")
            self._result = result

    def seal_original_outcome(
        self, capture: _SemanticAdvisorInvocationCapture, outcome: SemanticAdvisorInvocationReturned | SemanticAdvisorInvocationRaised
    ) -> None:
        from elspeth.web.composer.continuation_semantic_advisor_outcome import (
            SemanticAdvisorInvocationRaised,
            SemanticAdvisorInvocationReturned,
        )

        with self._lock:
            original = self._body_observation
            if capture is not self._capture or original is None:
                if type(outcome) is SemanticAdvisorInvocationRaised:
                    # An error before original custody/capture completion is
                    # still diagnostic DATA; it cannot authorize retention.
                    return
                raise AuditIntegrityError("Semantic returned outcome lacks its original physical capture")
            observed = outcome.observation
            if self._outcome is not None or observed != original:
                raise AuditIntegrityError("Semantic outcome changed its original admitted response bytes or decision")
            if type(outcome) is SemanticAdvisorInvocationReturned:
                if outcome.result is not self._result or original.original_failure is not None or observed.original_failure is not None:
                    raise AuditIntegrityError("Semantic returned wrapper changed its original result/failure")
            elif type(outcome) is SemanticAdvisorInvocationRaised:
                expected_failure = original.original_failure if original.original_failure is not None else outcome.error
                if observed.original_failure is not expected_failure:
                    raise AuditIntegrityError("Semantic raised wrapper changed its original physical/outward failure")
            else:
                raise AuditIntegrityError("Semantic outcome lost its actual returned/raised sum")
            self._outcome = outcome

    def seal_v4_finalization(self, capture: _SemanticAdvisorInvocationCapture, finalized: SemanticAdvisorInvocationV4Finalized) -> None:
        from elspeth.web.composer.continuation_semantic_advisor_outcome import SemanticAdvisorInvocationV4Finalized
        from elspeth.web.sessions.continuation_semantic_advisor import SemanticAdvisorProviderResult
        from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import VerifiedSemanticPaidOverflowTerminal

        with self._lock:
            original = self._body_observation
            if (
                type(finalized) is not SemanticAdvisorInvocationV4Finalized
                or capture is not self._capture
                or original is None
                or finalized.observation != original
                or self._v4_finalized is not None
                or self._state != "sdk_entered"
                or self._prepared is None
                or self._dispatched is None
                or self._attempt_id is None
                or self._captured_call is None
                or original.audit_call is not self._captured_call
                or original.original_failure is not None
            ):
                raise AuditIntegrityError("V4 semantic finalization changed its one original paid capture")
            result = finalized.finalization.result
            if type(result) is SemanticAdvisorProviderResult:
                if (
                    self._result is None
                    or result.audit_call != self._captured_call
                    or result.decision != original.decision
                    or result.response != self._result.response
                    or result.effect_key != self._prepared.binding.effect_key
                ):
                    raise AuditIntegrityError("V4 semantic complete finalization changed its original result")
            elif type(result) is VerifiedSemanticPaidOverflowTerminal:
                if (
                    self._result is not None
                    or result.provider_attempt_id != self._attempt_id
                    or result.source.effect_key != self._prepared.binding.effect_key
                    or result.terminal.cursor < self._dispatched.cursor + 2
                ):
                    raise AuditIntegrityError("V4 semantic terminal finalization changed its original effect")
            else:
                raise AuditIntegrityError("V4 semantic finalization lost its complete/terminal sum")
            self._v4_finalized = finalized

    def note_prepared_candidate(self, prepared: PreparedContinuationProvider) -> None:
        with self._lock:
            if self._state != "open" or self._prepared_candidate is not None or type(prepared) is not PreparedContinuationProvider:
                raise AuditIntegrityError("Semantic preparation candidate is foreign or repeated")
            binding = prepared.binding
            if (
                binding.running is not self.running
                or binding.family != 1
                or binding.effect_key != f"provider:1:{binding.ordinal}"
                or binding.input_sha256 != self.input_sha256
                or prepared.snapshot.cursor != self.original_cursor + 1
                or prepared.snapshot.checkpoint_id == self.original_checkpoint_id
            ):
                raise AuditIntegrityError("Semantic preparation candidate changed its original source")
            self._prepared_candidate = prepared

    def bind_prepared(self, prepared: PreparedContinuationProvider) -> None:
        with self._lock:
            if self._state != "open" or self._prepared is not None or prepared is not self._prepared_candidate:
                raise AuditIntegrityError("Semantic issuer lost its one original committed preparation")
            self._prepared = prepared

    def note_dispatched_candidate(self, selected: StoredContinuationSnapshot) -> None:
        with self._lock:
            if (
                self._state != "open"
                or self._prepared is None
                or self._dispatched_candidate is not None
                or type(selected) is not StoredContinuationSnapshot
                or selected.cursor != self._prepared.snapshot.cursor + 1
                or selected.checkpoint_id == self._prepared.snapshot.checkpoint_id
            ):
                raise AuditIntegrityError("Semantic begin candidate changed its committed preparation")
            self._dispatched_candidate = selected

    def bind_dispatched(self, selected: StoredContinuationSnapshot) -> None:
        with self._lock:
            if self._state != "open" or self._dispatched is not None or selected is not self._dispatched_candidate:
                raise AuditIntegrityError("Semantic issuer lost its one original committed begin")
            self._dispatched = selected

    def bind_custody(self, custody: ProviderCallCustody) -> None:
        from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationFamily

        with self._lock:
            self.require_original_clock()
            prepared = self._prepared
            v4_prepared = (
                prepared is not None
                and self._dispatched is None
                and type(
                    decode_continuation_policy(
                        decode_continuation_snapshot(prepared.snapshot.payload_json).admission_policy_json,
                        decode_continuation_snapshot(prepared.snapshot.payload_json).policy_hash,
                    )
                )
                is ContinuationAdmissionPolicyV4
            )
            if (
                self._state != "open"
                or prepared is None
                or (self._dispatched is None and not v4_prepared)
                or self._custody is not None
                or type(custody) is not ProviderCallCustody
                or custody.family is not ProviderInvocationFamily.ADVISOR
                or custody.context is not self.running.session_operation_context
                or custody.required_work.coordinator is not self.work.coordinator
            ):
                raise AuditIntegrityError("Semantic issuer requires its actual original ADVISOR custody")
            self._custody = custody

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
                raise AuditIntegrityError("Semantic issuer lost its original admitted attempt")
            self._attempt_id, self._attempt_model = attempt.attempt_id, model

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
                raise AuditIntegrityError("Semantic issuer lacks its actual successful undispatched settlement")
            self._cancelled_attempt = True

    def mark_sdk_entered(self, custody: ProviderCallCustody) -> None:
        with self._lock:
            if self._state != "open" or custody is not self._custody or self._dispatched is None:
                raise AuditIntegrityError("Semantic SDK entry conflicts with original no-SDK custody")
            self._state = "sdk_entered"

    def note_original_failure(self, custody: ProviderCallCustody, error: BaseException) -> None:
        with self._lock:
            if custody is not self._custody or self._original_failure is not None or self._state not in ("open", "sdk_entered"):
                raise AuditIntegrityError("Semantic issuer changed its one original physical failure")
            self._original_failure = error

    def note_paid_audit(self, custody: ProviderCallCustody, call: ComposerLLMCall) -> None:
        with self._lock:
            if (
                self._state != "sdk_entered"
                or custody is not self._custody
                or type(call) is not ComposerLLMCall
                or call.call_id != self._attempt_id
                or custody.call is not call
                or custody.attempt is None
                or custody.attempt.attempt_id != self._attempt_id
                or self._captured_call is not None
            ):
                raise AuditIntegrityError("Semantic issuer lost its one original paid audit before settlement")
            self._captured_call = call

    def require_original_paid_outcome(
        self,
        prepared: PreparedContinuationProvider,
        selected: StoredContinuationSnapshot,
        outcome: SemanticAdvisorInvocationReturned | SemanticAdvisorInvocationRaised,
    ) -> None:
        with self._lock:
            if (
                self._state != "sdk_entered"
                or prepared is not self._prepared
                or selected is not self._dispatched
                or outcome is not self._outcome
                or outcome.observation.audit_call is not self._captured_call
                or self._captured_call is None
                or self._captured_call.call_id != self._attempt_id
            ):
                raise AuditIntegrityError("Semantic paid retention requires its original effect, minted custody and complete outcome")

    def require_original_v4_response_observation(
        self,
        prepared: PreparedContinuationProvider,
        selected: StoredContinuationSnapshot,
    ) -> SemanticAdvisorInvocationObservation:
        """Return the original captured paid DATA before V4 SQL settlement.

        V4 must classify the complete response before the legacy returned/raised
        wrapper can be sealed. The caller must still rederive the immutable SQL
        source and quota attempt in its original locked transaction.
        """
        with self._lock:
            original = self._body_observation
            if (
                self._state != "sdk_entered"
                or prepared is not self._prepared
                or selected is not self._dispatched
                or self._custody is None
                or self._custody.continuation_semantic_v4 is not prepared
                or self._custody.attempt is None
                or self._custody.attempt.attempt_id != self._attempt_id
                or original is None
                or not original.sdk_entered
                or not original.recorder_completed
                or original.original_failure is not None
                or original.audit_call is not self._captured_call
                or self._captured_call is None
                or original.attempt is None
                or original.attempt.attempt_id != self._attempt_id
                or self._captured_call.call_id != self._attempt_id
            ):
                raise AuditIntegrityError("V4 semantic response lost its one original successful paid capture")
            return original

    def require_original_v4_incomplete_capture(
        self,
        prepared: PreparedContinuationProvider,
        selected: StoredContinuationSnapshot,
        incomplete: V4ProviderResultIncomplete,
    ) -> SemanticAdvisorInvocationObservation:
        """Bind a returned parser loss to the one retained original paid audit."""
        from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
        from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete

        with self._lock:
            original = self._body_observation
            audit_call = self._captured_call
            if (
                self._state != "sdk_entered"
                or prepared is not self._prepared
                or selected is not self._dispatched
                or type(incomplete) is not V4ProviderResultIncomplete
                or type(audit_call) is not ComposerLLMCall
                or self._custody is None
                or self._custody.continuation_semantic_v4 is not prepared
                or self._custody.attempt is None
                or self._custody.attempt.attempt_id != self._attempt_id
                or original is None
                or not original.sdk_entered
                or not original.recorder_completed
                or original.original_failure is not incomplete
                or original.audit_call is not audit_call
                or self._captured_call is not audit_call
                or audit_call.status is not ComposerLLMCallStatus.MALFORMED_RESPONSE
                or audit_call.call_id != incomplete.sdk_return.attempt_id
                or audit_call.call_id != self._attempt_id
            ):
                raise AuditIntegrityError("V4 semantic paid-incomplete capture lost its original SDK return and audit")
            self._custody.require_v4_result_incomplete(incomplete)
            return original

    def require_original_sdk_custody(
        self, prepared: PreparedContinuationProvider, observation: V4SdkFailureObservation
    ) -> ProviderCallCustody:
        from elspeth.web.composer.provider_sdk_observation import require_original_v4_sdk_failure

        with self._lock:
            custody = self._custody
            if self._state != "sdk_entered" or prepared is not self._prepared or custody is None:
                raise AuditIntegrityError("semantic SDK finalizer lost original live custody")
            require_original_v4_sdk_failure(observation, custody=custody)
            if custody.continuation_semantic_v4 is not prepared or observation.attempt.attempt_id != self._attempt_id:
                raise AuditIntegrityError("semantic SDK finalizer changed original attempt")
            return custody

    def require_v4_sdk_outcome(
        self, prepared: PreparedContinuationProvider, outcome: SemanticAdvisorInvocationRaised
    ) -> SdkTerminalResult | SdkTerminalFailure | None:
        from elspeth.web.composer.continuation_semantic_advisor_outcome import SemanticAdvisorInvocationRaised
        from elspeth.web.composer.provider_quota import V4SdkTerminalCommitted
        from elspeth.web.composer.provider_sdk_terminal import capture_sdk_terminal_failure
        from elspeth.web.sessions.continuation_sdk_tail import SdkTerminalFamily
        from elspeth.web.sessions.continuation_sdk_terminal_result import SdkTerminalResult

        with self._lock:
            custody = self._custody
            if custody is None or (custody.v4_sdk_terminal_failure is None and type(outcome.error) is not V4SdkTerminalCommitted):
                return None
            if (
                self._state != "sdk_entered"
                or prepared is not self._prepared
                or type(outcome) is not SemanticAdvisorInvocationRaised
                or self._capture is None
                or outcome.observation != self._capture.snapshot()
                or (self._outcome is not None and outcome is not self._outcome)
                or self._attempt_id is None
                or self._dispatched is None
            ):
                raise AuditIntegrityError("semantic SDK release lost its original invocation capture")
            exceptional = capture_sdk_terminal_failure(outcome.error, custody=custody)
            if exceptional is not None:
                return exceptional
            control = outcome.error
            if type(control) is not V4SdkTerminalCommitted or custody.v4_sdk_terminal_result is not control.finalization:
                raise AuditIntegrityError("semantic SDK release changed its committed original custody")
            finalized = control.finalization
            if (
                self._outcome is not outcome
                or finalized.verified.source.effect_key != prepared.binding.effect_key
                or finalized.verified.provider_attempt_id != self._attempt_id
                or finalized.verified.terminal.cursor < self._dispatched.cursor + 2
            ):
                raise AuditIntegrityError("semantic SDK release changed its original terminal")
            return SdkTerminalResult(finalized.verified, SdkTerminalFamily.SEMANTIC, finalized.deferred_cancellations)

    def require_v4_paid_incomplete_committed(
        self, prepared: PreparedContinuationProvider, outcome: SemanticAdvisorInvocationRaised
    ) -> V4ReturnedIncompleteFinalization:
        """Release only a committed terminal from this original semantic custody."""
        from elspeth.web.composer.continuation_semantic_advisor_outcome import SemanticAdvisorInvocationRaised
        from elspeth.web.composer.provider_quota import V4PaidIncompleteCommitted
        from elspeth.web.composer.provider_result_admission import V4ProviderResultIncomplete

        with self._lock:
            original = self._body_observation
            if (
                self._state != "sdk_entered"
                or prepared is not self._prepared
                or type(outcome) is not SemanticAdvisorInvocationRaised
                or outcome is not self._outcome
                or original is None
                or outcome.observation != original
                or type(original.original_failure) is not V4ProviderResultIncomplete
                or type(outcome.error) is not V4PaidIncompleteCommitted
                or self._custody is None
                or self._attempt_id is None
                or self._dispatched is None
            ):
                raise AuditIntegrityError("V4 semantic paid-incomplete release lost its original committed custody")
            finalization = self._custody.require_v4_paid_incomplete_committed(outcome.error)
            terminal = finalization.verified
            if (
                terminal.source.effect_key != prepared.binding.effect_key
                or terminal.provider_attempt_id != self._attempt_id
                or terminal.terminal.cursor < self._dispatched.cursor + 2
            ):
                raise AuditIntegrityError("V4 semantic paid-incomplete release changed original effect")
            return finalization

    def require_original_v4_admission(self, prepared: PreparedContinuationProvider) -> V4SemanticProviderAdmission:
        from elspeth.web.sessions.continuation_semantic_advisor import V4SemanticProviderAdmission

        with self._lock:
            custody = self._custody
            admission = None if custody is None else custody.continuation_semantic_v4_admission
            if (
                self._prepared is not prepared
                or custody is None
                or custody.continuation_semantic_v4 is not prepared
                or type(admission) is not V4SemanticProviderAdmission
                or self._dispatched is not admission.selected
                or self._attempt_id != admission.attempt.attempt_id
                or custody.attempt is not admission.attempt
            ):
                raise AuditIntegrityError("V4 semantic issuer lost original prepared dispatch, H or physical attempt")
            return admission

    def original_v4_admission_or_none(self, prepared: PreparedContinuationProvider) -> V4SemanticProviderAdmission | None:
        with self._lock:
            custody = self._custody
            if self._prepared is not prepared or custody is None or custody.continuation_semantic_v4 is not prepared:
                raise AuditIntegrityError("V4 semantic no-SDK source changed its original prepared custody")
            if custody.continuation_semantic_v4_admission is None:
                if self._attempt_id is not None or self._dispatched is not None or custody.attempt is not None:
                    raise AuditIntegrityError("V4 semantic no-SDK source lost a partially admitted physical attempt")
                return None
        return self.require_original_v4_admission(prepared)

    def original_v4_dispatched_candidate(self, prepared: PreparedContinuationProvider) -> StoredContinuationSnapshot | None:
        """Expose only the exact in-transaction candidate for cold admission reproof."""
        with self._lock:
            custody = self._custody
            if self._prepared is not prepared or custody is None or custody.continuation_semantic_v4 is not prepared:
                raise AuditIntegrityError("V4 semantic cold admission changed its original prepared custody")
            return self._dispatched_candidate

    def note_v4_sql_cancelled_attempt(self, admission: V4SemanticProviderAdmission) -> None:
        from elspeth.web.sessions.continuation_semantic_advisor import V4SemanticProviderAdmission

        if type(admission) is not V4SemanticProviderAdmission or self._prepared is None:
            raise AuditIntegrityError("V4 semantic SQL cancellation requires its nominal original admission")
        if self.require_original_v4_admission(self._prepared) is not admission or self._custody is None:
            raise AuditIntegrityError("V4 semantic SQL cancellation changed its original physical attempt")
        self.note_undispatched_cancel(self._custody, admission.attempt, model=admission.no_sdk_tail.requested_model)

    def reserve_refusal(
        self, *, reason: SemanticAdvisorNoSdkRefusalReason, original_failure: BaseException
    ) -> SemanticAdvisorNoSdkRefusalPermit:
        with self._lock:
            expected = {
                SemanticAdvisorNoSdkRefusalReason.GUARD_REFUSED: PromptGuardRefused,
                SemanticAdvisorNoSdkRefusalReason.COST_REFUSED: CommonProviderCostRefused,
                SemanticAdvisorNoSdkRefusalReason.CHARGEABLE_REFUSED: ChargeableAdmissionRefused,
                SemanticAdvisorNoSdkRefusalReason.PRE_SDK_DEADLINE: TimeoutError,
                SemanticAdvisorNoSdkRefusalReason.CANCELLED_BEFORE_SDK: asyncio.CancelledError,
            }
            if (
                self._state != "open"
                or self._prepared is None
                or self._dispatched is None
                or self._custody is None
                or self._original_failure is not original_failure
                or type(reason) is not SemanticAdvisorNoSdkRefusalReason
                or type(original_failure) is not expected[reason]
                or (self._attempt_id is not None and not self._cancelled_attempt)
                or self._permit is not None
            ):
                raise AuditIntegrityError("Semantic refusal lacks original unentered and successfully settled custody")
            permit = SemanticAdvisorNoSdkRefusalPermit(
                self,
                self._prepared,
                self._dispatched,
                reason,
                original_failure,
                str(uuid4()),
                self._attempt_id,
                self._attempt_model,
            )
            self._permit, self._state = permit, "refusing"
            return permit

    def require_refusal_permit(self, permit: SemanticAdvisorNoSdkRefusalPermit) -> None:
        with self._lock:
            if type(permit) is not SemanticAdvisorNoSdkRefusalPermit or permit is not self._permit or self._state != "refusing":
                raise AuditIntegrityError("Semantic refusal permit is foreign, copied or already consumed")
