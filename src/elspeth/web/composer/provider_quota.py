"""Task-local quota custody around each audited Composer provider attempt."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import IntEnum
from functools import wraps
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts import errors as contract_errors
from elspeth.contracts.chargeable_admission import ChargeableAdmissionRefused
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError, ComposerOwnedSettlementFailure
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.composer.provider_cost import CommonProviderCostBudget, CommonProviderCostRefused, CommonProviderCostRequest
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.coordination.quota_authority import ProviderAttempt
from elspeth.web.required_sql_outcomes import RequiredSQLFinishOnce, RequiredSQLRaised, RequiredSQLReturned
from elspeth.web.required_work import RequiredWorkBinding, RequiredWorkRole, RequiredWorkSource, RequiredWorkTicket

if TYPE_CHECKING:
    from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion, _CallModelOutcome
    from elspeth.web.composer.continuation_checkpoint_no_sdk import CheckpointNoSdkIssuer
    from elspeth.web.composer.continuation_classifier_no_sdk import ClassifierNoSdkIssuer
    from elspeth.web.composer.continuation_clock import ContinuationActionClock
    from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer
    from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer
    from elspeth.web.composer.continuation_semantic_observation import SemanticProviderResponseObservation
    from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness, V4ProviderSdkReturn
    from elspeth.web.sessions.continuation import PreparedContinuationProvider, StoredContinuationSnapshot
    from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider, V4CheckpointProviderAdmission
    from elspeth.web.sessions.continuation_classifier_primary import (
        ClassifierPrimaryResult,
        PreparedClassifierPrimaryProvider,
        V4ClassifierProviderAdmission,
        VerifiedClassifierPaidOverflowTerminal,
    )
    from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt
    from elspeth.web.sessions.continuation_primary import (
        PrimaryNoSdkRefusalReceipt,
        PrimaryPaidOverflowTerminal,
        PrimaryProviderResult,
        V4PrimaryProviderAdmission,
    )
    from elspeth.web.sessions.continuation_semantic_advisor import SemanticAdvisorProviderResult, V4SemanticProviderAdmission
    from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import VerifiedSemanticPaidOverflowTerminal
    from elspeth.web.sessions.continuation_title_paid_overflow_terminal import VerifiedTitlePaidOverflowTerminal
    from elspeth.web.sessions.continuation_title_provider import (
        PreparedTitleProvider,
        TitleAuditSettlementRequest,
        V4TitleProviderAdmission,
    )
    from elspeth.web.sessions.protocol import SessionServiceProtocol
    from elspeth.web.sessions.service import _ProviderAdmissionRefused


def _bind_attempt_call(call: ComposerLLMCall, attempt: ProviderAttempt, admitted_ns: int) -> ComposerLLMCall:
    """Anchor monotonic call duration to the authoritative database admission.

    The request's local wall clock is useful only before quota binding. Both
    persisted endpoints must share the database clock domain, including when
    a replica's wall clock is ahead of or behind UTC midnight.
    """
    if call.call_id == attempt.attempt_id and call.started_at == attempt.started_at:
        # Caller-side classification can refine a complete audit. Preserve its
        # original clock binding, including when required settlement is replayed.
        return call
    elapsed_ns = time.monotonic_ns() - admitted_ns
    return replace(
        call,
        call_id=attempt.attempt_id,
        started_at=attempt.started_at,
        finished_at=attempt.started_at + timedelta(microseconds=elapsed_ns // 1_000),
    )


class ProviderInvocationFamily(IntEnum):
    PRIMARY = 0
    ADVISOR = 1
    # 2 was the never-written PLANNER family; values are not renumbered
    # because the ordinal is part of persisted provider effect keys.
    TITLE = 3


@dataclass(frozen=True, slots=True)
class SemanticV4Finalization:
    result: SemanticAdvisorProviderResult | VerifiedSemanticPaidOverflowTerminal
    deferred_cancellations: tuple[asyncio.CancelledError, ...]


@dataclass(frozen=True, slots=True)
class _V4AdmissionProjection[T]:
    admitted: T
    deferred_cancellations: tuple[asyncio.CancelledError, ...]
    original_failure: BaseException | None
    cold_recovered: bool


class ProviderInvocationOwner:
    """Single explicit mint owner for independently ordered provider families."""

    def __init__(self, *, service: SessionServiceProtocol, required_work: RequiredWorkBinding) -> None:
        if type(required_work) is not RequiredWorkBinding or required_work.role is not RequiredWorkRole.TURN:
            raise AuditIntegrityError("Provider invocation owner requires its exact turn binding")
        required_work.validate_context(required_work.coordinator.authority.context)
        self._service = service
        self._required_work = required_work
        self._counters = dict.fromkeys(ProviderInvocationFamily, 0)
        self._cost_budget: CommonProviderCostBudget | None = None

    @property
    def service(self) -> SessionServiceProtocol:
        return self._service

    @property
    def required_work(self) -> RequiredWorkBinding:
        return self._required_work

    @property
    def cost_budget(self) -> CommonProviderCostBudget | None:
        return self._cost_budget

    def bind_cost_budget(self, budget: CommonProviderCostBudget) -> None:
        if type(budget) is not CommonProviderCostBudget:
            raise AuditIntegrityError("provider owner requires nominal request policy")
        if self._cost_budget is not None:
            if self._cost_budget is not budget:
                raise AuditIntegrityError("provider owner cannot replace request policy")
            return
        if self._counters[ProviderInvocationFamily.PRIMARY] or self._counters[ProviderInvocationFamily.ADVISOR]:
            raise AuditIntegrityError("provider policy cannot start after covered invocation")
        self._cost_budget = budget

    def mint(self, family: ProviderInvocationFamily) -> ProviderCallCustody:
        if type(family) is not ProviderInvocationFamily:
            raise AuditIntegrityError("Provider family is not an owned nominal identity")
        ordinal = self._counters[family]
        self._counters[family] = ordinal + 1
        diagonal = int(family) + ordinal
        semantic = diagonal * (diagonal + 1) // 2 + ordinal
        binding = RequiredWorkBinding(
            self.required_work.coordinator,
            self.required_work.transition_ordinal,
            semantic,
            RequiredWorkRole.TITLE if family is ProviderInvocationFamily.TITLE else RequiredWorkRole.TURN,
        )
        return ProviderCallCustody(
            service=self.service,
            context=binding.coordinator.authority.context,
            required_work=binding,
            cost_budget=self.cost_budget if family in (ProviderInvocationFamily.PRIMARY, ProviderInvocationFamily.ADVISOR) else None,
            family=family,
        )


class ProviderCallCustody:
    """Explicit owned admission, physical dispatch and audit settlement custody."""

    __slots__ = (
        "__weakref__",
        "_admitted_ns",
        "_admitting",
        "_checkpoint_no_sdk_issuer",
        "_classifier_no_sdk_issuer",
        "_context",
        "_continuation_classifier_v4_finalizer",
        "_continuation_classifier_v4_terminal",
        "_continuation_primary_v4_finalizer",
        "_continuation_primary_v4_sql_committed",
        "_continuation_primary_v4_terminal",
        "_continuation_semantic_v4_finalizer",
        "_continuation_semantic_v4_sql_committed",
        "_continuation_semantic_v4_terminal",
        "_cost_budget",
        "_cost_response_accountable",
        "_family",
        "_primary_no_sdk_issuer",
        "_required_work",
        "_sdk_entered",
        "_semantic_no_sdk_issuer",
        "_service",
        "_settlements",
        "_undispatched_recurrence",
        "_v4_sdk_return",
        "_v4_sdk_return_claimed",
        "attempt",
        "call",
        "continuation_checkpoint_v4",
        "continuation_checkpoint_v4_admission",
        "continuation_classifier_v4",
        "continuation_classifier_v4_admission",
        "continuation_classifier_v4_deferred_cancellations",
        "continuation_classifier_v4_no_sdk_result",
        "continuation_classifier_v4_result",
        "continuation_primary_clock",
        "continuation_primary_v4",
        "continuation_primary_v4_admission",
        "continuation_primary_v4_deferred_cancellations",
        "continuation_primary_v4_no_sdk_result",
        "continuation_primary_v4_result",
        "continuation_semantic_v4",
        "continuation_semantic_v4_admission",
        "continuation_semantic_v4_result",
        "continuation_title",
        "continuation_title_result",
        "continuation_title_selected",
        "continuation_title_v4_admission",
        "continuation_title_v4_terminal",
    )

    def __init__(
        self,
        *,
        service: SessionServiceProtocol,
        context: SessionOperationContext,
        required_work: RequiredWorkBinding,
        cost_budget: CommonProviderCostBudget | None = None,
        family: ProviderInvocationFamily | None = None,
    ) -> None:
        if type(required_work) is not RequiredWorkBinding:
            raise AuditIntegrityError("Provider custody requires a nominal explicit binding")
        required_work.validate_context(context)
        if context.operation_kind is not SessionOperationKind.COMPOSE:
            raise AuditIntegrityError("Provider custody requires exact COMPOSE authority")
        if cost_budget is not None and type(cost_budget) is not CommonProviderCostBudget:
            raise AuditIntegrityError("provider custody received foreign cost policy")
        if family is not None and type(family) is not ProviderInvocationFamily:
            raise AuditIntegrityError("Provider custody family requires its owned nominal discriminator")
        self._family = family
        self._cost_budget = cost_budget
        self._cost_response_accountable = False
        self._service = service
        self._context = context
        self._required_work = required_work
        self._primary_no_sdk_issuer: PrimaryNoSdkIssuer | None = None
        self._classifier_no_sdk_issuer: ClassifierNoSdkIssuer | None = None
        self._checkpoint_no_sdk_issuer: CheckpointNoSdkIssuer | None = None
        self._semantic_no_sdk_issuer: SemanticAdvisorNoSdkIssuer | None = None
        self.attempt: ProviderAttempt | None = None
        self.call: ComposerLLMCall | None = None
        self._sdk_entered = False
        self._admitting = False
        self._admitted_ns = 0
        self._undispatched_recurrence = 0
        self._v4_sdk_return: V4ProviderSdkReturn | None = None
        self._v4_sdk_return_claimed = False
        self._settlements = 0
        self.continuation_title: PreparedTitleProvider | None = None
        self.continuation_primary_clock: ContinuationActionClock | None = None
        self.continuation_classifier_v4: PreparedClassifierPrimaryProvider | None = None
        self.continuation_classifier_v4_admission: V4ClassifierProviderAdmission | None = None
        self.continuation_classifier_v4_result: ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal | None = None
        self.continuation_classifier_v4_deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()
        self.continuation_classifier_v4_no_sdk_result: ClassifierNoSdkRefusalReceipt | None = None
        self.continuation_checkpoint_v4_admission: V4CheckpointProviderAdmission | None = None
        self.continuation_checkpoint_v4: PreparedCheckpointProvider | None = None
        self._continuation_classifier_v4_finalizer: (
            Callable[
                [_CallModelOutcome, RequiredWorkTicket],
                Awaitable[RequiredSQLFinishOnce[ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal]],
            ]
            | None
        ) = None
        self._continuation_classifier_v4_terminal = False
        self.continuation_title_selected: StoredContinuationSnapshot | None = None
        self.continuation_title_result: TitleAuditSettlementRequest | None = None
        self.continuation_title_v4_admission: V4TitleProviderAdmission | None = None
        self.continuation_title_v4_terminal: VerifiedTitlePaidOverflowTerminal | None = None
        self.continuation_primary_v4: PreparedContinuationProvider | None = None
        self.continuation_primary_v4_admission: V4PrimaryProviderAdmission | None = None
        self.continuation_primary_v4_deferred_cancellations: tuple[asyncio.CancelledError, ...] = ()
        self.continuation_primary_v4_no_sdk_result: PrimaryNoSdkRefusalReceipt | None = None
        self.continuation_primary_v4_result: PrimaryProviderResult | PrimaryPaidOverflowTerminal | None = None
        self._continuation_primary_v4_finalizer: (
            Callable[
                [_CallModelOutcome, RequiredWorkTicket],
                Awaitable[RequiredSQLFinishOnce[PrimaryProviderResult | PrimaryPaidOverflowTerminal]],
            ]
            | None
        ) = None
        self._continuation_primary_v4_sql_committed = False
        self._continuation_primary_v4_terminal = False
        self.continuation_semantic_v4: PreparedContinuationProvider | None = None
        self.continuation_semantic_v4_admission: V4SemanticProviderAdmission | None = None
        self.continuation_semantic_v4_result: SemanticAdvisorProviderResult | VerifiedSemanticPaidOverflowTerminal | None = None
        self._continuation_semantic_v4_finalizer: (
            Callable[
                [SemanticProviderResponseObservation, RequiredWorkTicket],
                Awaitable[RequiredSQLFinishOnce[SemanticAdvisorProviderResult | VerifiedSemanticPaidOverflowTerminal]],
            ]
            | None
        ) = None
        self._continuation_semantic_v4_sql_committed = False
        self._continuation_semantic_v4_terminal = False

    def bind_continuation_title(self, prepared: PreparedTitleProvider) -> None:
        from elspeth.web.composer.continuation_clock import ContinuationActionClock
        from elspeth.web.sessions.continuation_title_provider import PreparedTitleProvider

        if (
            type(prepared) is not PreparedTitleProvider
            or self.continuation_title is not None
            or self.attempt is not None
            or self.required_work.role is not RequiredWorkRole.TITLE
            or self.required_work.semantic_ordinal != 6
            or self._cost_budget is not None
            or type(prepared.clock) is not ContinuationActionClock
            or prepared.work.coordinator is not self.required_work.coordinator
            or prepared.work.transition_ordinal != self.required_work.transition_ordinal
            or prepared.running.session_operation_context is not self.context
            or prepared.composer._require_sessions_service() is not self.service
        ):
            raise AuditIntegrityError("Durable TITLE requires its actual first family-3 invocation and original context")
        self.continuation_title = prepared
        self.continuation_title_selected = prepared.selected

    def note_v4_title_complete(self, result: TitleAuditSettlementRequest) -> None:
        from elspeth.web.sessions.continuation_title_provider import TitleAuditSettlementRequest

        prepared, admission = self.continuation_title, self.continuation_title_v4_admission
        if (
            type(result) is not TitleAuditSettlementRequest
            or prepared is None
            or admission is None
            or result.running is not prepared.running
            or self.attempt is not admission.attempt
            or not self._sdk_entered
            or self.call is None
            or self.call.status is not ComposerLLMCallStatus.SUCCESS
            or result.audit_call != self.call
            or result.selected == admission.selected
            or self.continuation_title_v4_terminal is not None
            or self.continuation_title_result is not None
        ):
            raise AuditIntegrityError("V4 TITLE complete result changed its original paid admission or settled root")
        self.continuation_title_result = replace(result, audit_call=self.call)
        self.continuation_title_selected = result.selected
        self.attempt = None
        self.call = None
        self._sdk_entered = False

    def note_v4_title_terminal(self, result: VerifiedTitlePaidOverflowTerminal) -> None:
        from elspeth.web.sessions.continuation_title_paid_overflow_terminal import VerifiedTitlePaidOverflowTerminal

        admission = self.continuation_title_v4_admission
        if (
            type(result) is not VerifiedTitlePaidOverflowTerminal
            or admission is None
            or self.attempt is not admission.attempt
            or not self._sdk_entered
            or self.call is None
            or self.call.status is not ComposerLLMCallStatus.SUCCESS
            or result.source != admission.hold
            or result.provider_attempt_id != admission.attempt.attempt_id
            or result.receipt.identity.attempt_id != admission.attempt.attempt_id
            or result.receipt.identity.effect_key != admission.hold.effect_key
            or result.hold_state != "consumed"
            or self.continuation_title_result is not None
            or self.continuation_title_v4_terminal is not None
        ):
            raise AuditIntegrityError("V4 TITLE terminal result changed its original paid admission or cold hold proof")
        self.continuation_title_v4_terminal = result
        self.continuation_title_selected = result.terminal
        self.attempt = None
        self.call = None
        self._sdk_entered = False

    def bind_continuation_primary_clock(self, clock: ContinuationActionClock) -> None:
        from elspeth.web.composer.continuation_clock import ContinuationActionClock

        if (
            type(clock) is not ContinuationActionClock
            or self.continuation_title is not None
            or self.continuation_primary_clock is not None
            or self.attempt is not None
            or self.required_work.role is not RequiredWorkRole.TURN
        ):
            raise AuditIntegrityError("PRIMARY timing requires its original pre-dispatch action owner")
        self.continuation_primary_clock = clock

    def bind_primary_no_sdk_issuer(self, issuer: PrimaryNoSdkIssuer) -> None:
        from elspeth.web.composer.continuation_primary_no_sdk import PrimaryNoSdkIssuer

        if (
            type(issuer) is not PrimaryNoSdkIssuer
            or self._primary_no_sdk_issuer is not None
            or self.continuation_primary_clock is not issuer.clock
            or self.attempt is not None
        ):
            raise AuditIntegrityError("PRIMARY no-SDK issuer requires original unentered physical custody")
        issuer.bind_custody(self)
        self._primary_no_sdk_issuer = issuer

    def bind_continuation_primary_v4(
        self,
        prepared: PreparedContinuationProvider,
        finalizer: Callable[
            [_CallModelOutcome, RequiredWorkTicket], Awaitable[RequiredSQLFinishOnce[PrimaryProviderResult | PrimaryPaidOverflowTerminal]]
        ],
    ) -> None:
        from elspeth.web.sessions.continuation import PreparedContinuationProvider
        from elspeth.web.sessions.service import SessionServiceImpl

        if (
            type(prepared) is not PreparedContinuationProvider
            or type(self.service) is not SessionServiceImpl
            or self.family is not ProviderInvocationFamily.PRIMARY
            or self.required_work.role is not RequiredWorkRole.TURN
            or self._primary_no_sdk_issuer is None
            or self.continuation_primary_clock is not self._primary_no_sdk_issuer.clock
            or prepared.binding.running.session_operation_context is not self.context
            or self.continuation_primary_v4 is not None
            or self.attempt is not None
            or not callable(finalizer)
        ):
            raise AuditIntegrityError("V4 PRIMARY requires its original prepared, timed and unentered custody")
        self.continuation_primary_v4 = prepared
        self._continuation_primary_v4_finalizer = finalizer

    def bind_classifier_no_sdk_issuer(self, issuer: ClassifierNoSdkIssuer) -> None:
        from elspeth.web.composer.continuation_classifier_no_sdk import ClassifierNoSdkIssuer

        if (
            type(issuer) is not ClassifierNoSdkIssuer
            or self.family is not ProviderInvocationFamily.PRIMARY
            or self.continuation_primary_clock is not issuer.clock
            or self._classifier_no_sdk_issuer is not None
            or self._primary_no_sdk_issuer is not None
            or self._semantic_no_sdk_issuer is not None
            or self.continuation_title is not None
            or self.attempt is not None
        ):
            raise AuditIntegrityError("Classifier no-SDK issuer requires its original timed PRIMARY custody")
        issuer.bind_custody(self)
        self._classifier_no_sdk_issuer = issuer

    def bind_continuation_checkpoint_v4(self, prepared: PreparedCheckpointProvider, issuer: CheckpointNoSdkIssuer) -> None:
        from elspeth.web.composer.continuation_checkpoint_no_sdk import CheckpointNoSdkIssuer
        from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider
        from elspeth.web.sessions.service import SessionServiceImpl

        if (
            type(prepared) is not PreparedCheckpointProvider
            or type(issuer) is not CheckpointNoSdkIssuer
            or issuer.prepared is not prepared
            or type(self.service) is not SessionServiceImpl
            or self.family is not ProviderInvocationFamily.ADVISOR
            or self.required_work.role is not RequiredWorkRole.TURN
            or prepared.work is not self.required_work
            or prepared.running.session_operation_context is not self.context
            or self.continuation_checkpoint_v4 is not None
            or self.continuation_semantic_v4 is not None
            or self.continuation_primary_v4 is not None
            or self.continuation_classifier_v4 is not None
            or self.continuation_title is not None
            or self.attempt is not None
        ):
            raise AuditIntegrityError("V4 checkpoint custody requires its original prepared ADVISOR invocation")
        issuer.bind_custody(self)
        self.continuation_checkpoint_v4 = prepared
        self._checkpoint_no_sdk_issuer = issuer

    def bind_continuation_classifier_v4(
        self,
        prepared: PreparedClassifierPrimaryProvider,
        finalizer: Callable[
            [_CallModelOutcome, RequiredWorkTicket],
            Awaitable[RequiredSQLFinishOnce[ClassifierPrimaryResult | VerifiedClassifierPaidOverflowTerminal]],
        ],
    ) -> None:
        from elspeth.web.sessions.continuation_classifier_primary import PreparedClassifierPrimaryProvider
        from elspeth.web.sessions.service import SessionServiceImpl

        if (
            type(prepared) is not PreparedClassifierPrimaryProvider
            or type(self.service) is not SessionServiceImpl
            or self.family is not ProviderInvocationFamily.PRIMARY
            or self.required_work.role is not RequiredWorkRole.TURN
            or self._classifier_no_sdk_issuer is None
            or prepared.prepared.binding.running.session_operation_context is not self.context
            or self.continuation_primary_v4 is not None
            or self.continuation_classifier_v4 is not None
            or self.attempt is not None
            or not callable(finalizer)
        ):
            raise AuditIntegrityError("V4 classifier requires its original prepared, timed and unentered custody")
        self.continuation_classifier_v4 = prepared
        self._continuation_classifier_v4_finalizer = finalizer

    @property
    def family(self) -> ProviderInvocationFamily | None:
        return self._family

    @property
    def continuation_semantic_clock(self) -> ContinuationActionClock | None:
        issuer = self._semantic_no_sdk_issuer
        if issuer is None:
            return None
        if self.family is not ProviderInvocationFamily.ADVISOR:
            raise AuditIntegrityError("Semantic timing requires its nominal ADVISOR mint family")
        issuer.require_original_clock()
        return issuer.clock

    def bind_semantic_no_sdk_issuer(self, issuer: SemanticAdvisorNoSdkIssuer) -> None:
        from elspeth.web.composer.continuation_semantic_no_sdk import SemanticAdvisorNoSdkIssuer

        if (
            type(issuer) is not SemanticAdvisorNoSdkIssuer
            or self.family is not ProviderInvocationFamily.ADVISOR
            or self._semantic_no_sdk_issuer is not None
            or self._primary_no_sdk_issuer is not None
            or self.continuation_primary_clock is not None
            or self.continuation_title is not None
            or self.continuation_semantic_clock is not None
            or self.attempt is not None
        ):
            raise AuditIntegrityError("Semantic timing and no-SDK authority require original ADVISOR custody")
        issuer.bind_custody(self)
        self._semantic_no_sdk_issuer = issuer

    def bind_continuation_semantic_v4(
        self,
        prepared: PreparedContinuationProvider,
        finalizer: Callable[
            [SemanticProviderResponseObservation, RequiredWorkTicket],
            Awaitable[RequiredSQLFinishOnce[SemanticAdvisorProviderResult | VerifiedSemanticPaidOverflowTerminal]],
        ],
    ) -> None:
        from elspeth.web.sessions.continuation import PreparedContinuationProvider
        from elspeth.web.sessions.service import SessionServiceImpl

        if (
            type(prepared) is not PreparedContinuationProvider
            or type(self.service) is not SessionServiceImpl
            or self.family is not ProviderInvocationFamily.ADVISOR
            or self.required_work.role is not RequiredWorkRole.TURN
            or self._semantic_no_sdk_issuer is None
            or prepared.binding.running.session_operation_context is not self.context
            or self.continuation_primary_v4 is not None
            or self.continuation_semantic_v4 is not None
            or self.continuation_title is not None
            or self.attempt is not None
            or not callable(finalizer)
        ):
            raise AuditIntegrityError("V4 semantic custody requires its original prepared and unentered ADVISOR source")
        self.continuation_semantic_v4 = prepared
        self._continuation_semantic_v4_finalizer = finalizer

    async def retain_continuation_title(self, completion: _AdmittedLLMCompletion) -> None:
        from elspeth.web.composer.continuation_title_provider import retain_current_title_success

        if self.continuation_title is None or self.call is None or not self._sdk_entered:
            raise AuditIntegrityError("TITLE retention requires its actual physical success and original audit")
        _, cancellations = await self._join(retain_current_title_success(self, completion))
        self.raise_deferred_cancellations(cancellations)

    def prepare_cost_request(self, kwargs: CommonProviderCostRequest) -> None:
        if self._cost_budget is not None:
            self._cost_budget.prepare_request(kwargs)

    def cost_completion_limit(self) -> int | None:
        if self._cost_budget is None:
            return None
        policy = self._cost_budget.snapshot().policy
        return policy.max_completion_tokens if policy is not None else None

    @property
    def has_cost_budget(self) -> bool:
        return self._cost_budget is not None

    def require_cost_dispatch(self) -> None:
        if self._cost_budget is not None:
            self._cost_budget.require_dispatch()

    def require_cost_output(self) -> None:
        if self._continuation_primary_v4_terminal or self._continuation_classifier_v4_terminal or self._continuation_semantic_v4_terminal:
            # The paid/incomplete terminal records the original known or
            # unknown charge. It must not become a second cost refusal.
            return
        if (
            self.attempt is not None
            and self._sdk_entered
            and (
                self.continuation_primary_v4 is not None
                or self.continuation_classifier_v4_admission is not None
                or self.continuation_checkpoint_v4_admission is not None
                or self.continuation_title_v4_admission is not None
                or self.continuation_semantic_v4_admission is not None
            )
        ):
            # A V4 result has no valid ordinary settlement until its own
            # complete, overflow, or failure disposition commits. Preserve
            # the original pending physical attempt and hold on failure.
            return
        if self._cost_budget is not None:
            self._cost_budget.require_output()

    def mark_cost_response_accountable(self, *, admitted_metadata: bool) -> None:
        if type(admitted_metadata) is not bool or self.attempt is None or not self._sdk_entered:
            raise AuditIntegrityError("response accountability requires actual dispatched custody")
        self._cost_response_accountable = admitted_metadata

    @property
    def service(self) -> SessionServiceProtocol:
        return self._service

    @property
    def context(self) -> SessionOperationContext:
        return self._context

    @property
    def required_work(self) -> RequiredWorkBinding:
        return self._required_work

    async def _join[R](self, operation: Awaitable[R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:
        task = asyncio.ensure_future(operation)
        cancellations: list[asyncio.CancelledError] = []
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as cancellation:
                if all(cancellation is not original for original in cancellations):
                    cancellations.append(cancellation)
            except BaseException:
                break
        if task.cancelled():
            try:
                task.result()
            except asyncio.CancelledError as child_cancellation:
                integrity = AuditIntegrityError("Required provider child lacks an actual delivered outcome")
                integrity.__cause__ = child_cancellation
                if cancellations:
                    raise BaseExceptionGroup(
                        "Provider self-cancelled child and original cancellations", [integrity, child_cancellation, *cancellations]
                    ) from None
                raise integrity from child_cancellation
        try:
            return task.result(), tuple(cancellations)
        except BaseException as failure:
            retained = failure
            if isinstance(failure, Exception) and not isinstance(
                failure,
                (
                    *contract_errors.TIER_1_ERRORS,
                    SQLAlchemyError,
                    SessionOperationFenceLost,
                    ChargeableAdmissionRefused,
                    BaseExceptionGroup,
                ),
            ):
                retained = ComposerOwnedSettlementFailure()
                retained.__cause__ = failure
            if cancellations:
                raise BaseExceptionGroup("Provider required child and cancellation outcomes", [retained, *cancellations]) from None
            if retained is not failure:
                raise retained from failure
            raise

    async def _v4_admission_result[R](
        self,
        operation: Coroutine[Any, Any, RequiredSQLFinishOnce[R | _ProviderAdmissionRefused]],
        *,
        admission_projection: RequiredWorkTicket,
        readback: Callable[[RequiredWorkTicket], Coroutine[Any, Any, RequiredSQLFinishOnce[R | None]]] | None,
    ) -> _V4AdmissionProjection[R]:
        from elspeth.web.sessions.service import _ProviderAdmissionRefused

        physical, outer = await self._join(operation)
        if type(physical) is RequiredSQLRaised:
            admission_projection.complete_without_submission()
            read_failure: BaseException | None = None
            recovered: R | None = None
            read_deferred: tuple[asyncio.CancelledError, ...] = ()
            if readback is not None:
                read_sql, read_projection = self.required_work.reserve_pair(
                    RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION
                )
                try:
                    observed, read_outer = await self._join(readback(read_sql))
                except BaseException as error:
                    if read_sql.complete:
                        read_projection.complete_without_submission()
                    read_failure = error
                else:
                    if type(observed) is RequiredSQLRaised:
                        read_projection.complete_without_submission()
                        read_failure = observed.error
                        read_deferred = (*observed.deferred_cancellations, *read_outer)
                    elif type(observed) is RequiredSQLReturned:
                        read_projection.begin_projection()
                        read_projection.complete_owned()
                        recovered = observed.value
                        read_deferred = (*observed.deferred_cancellations, *read_outer)
                    else:
                        read_failure = AuditIntegrityError("V4 admission cold read lost its actual required SQL outcome")
            failures: list[BaseException] = [physical.error]
            if read_failure is not None and all(read_failure is not prior for prior in failures):
                failures.append(read_failure)
            deferred: list[asyncio.CancelledError] = []
            for cancellation in (*physical.deferred_cancellations, *outer, *read_deferred):
                if all(cancellation is not prior for prior in failures):
                    failures.append(cancellation)
                if all(cancellation is not prior for prior in deferred):
                    deferred.append(cancellation)
            if recovered is not None and read_failure is None:
                return _V4AdmissionProjection(recovered, tuple(deferred), physical.error, True)
            if len(failures) == 1:
                raise physical.error
            raise BaseExceptionGroup("V4 provider admission SQL and original cancellations", failures)
        if type(physical) is not RequiredSQLReturned:
            raise AuditIntegrityError("V4 provider admission lost its actual required SQL outcome")
        returned_deferred: list[asyncio.CancelledError] = []
        for cancellation in (*physical.deferred_cancellations, *outer):
            if all(cancellation is not prior for prior in returned_deferred):
                returned_deferred.append(cancellation)
        if type(physical.value) is _ProviderAdmissionRefused:
            if returned_deferred:
                raise BaseExceptionGroup(
                    "V4 provider quota refusal and original cancellations", [physical.value.refusal, *returned_deferred]
                )
            raise physical.value.refusal
        return _V4AdmissionProjection(cast("R", physical.value), tuple(returned_deferred), None, False)

    def raise_deferred_cancellations(self, cancellations: tuple[asyncio.CancelledError, ...]) -> None:
        if len(cancellations) == 1:
            raise cancellations[0]
        if cancellations:
            raise BaseExceptionGroup("Provider original cancellations", list(cancellations))

    async def join_title_update[R](self, operation: Coroutine[Any, Any, R]) -> tuple[R, tuple[asyncio.CancelledError, ...]]:
        if self.required_work.role is not RequiredWorkRole.TITLE or self.attempt is not None:
            raise AuditIntegrityError("Title publication cannot precede provider accounting completion")
        return await self._join(operation)

    async def _cancel_undispatched(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:
        if self.attempt is None or self._sdk_entered:
            raise AuditIntegrityError("Undispatched disposition lacks exact no-SDK attempt evidence")
        if (
            self.continuation_primary_v4_admission is not None
            or self.continuation_classifier_v4_admission is not None
            or self.continuation_checkpoint_v4_admission is not None
            or self.continuation_semantic_v4_admission is not None
            or self.continuation_title_v4_admission is not None
        ):
            raise AuditIntegrityError("V4 undispatched hold requires its composite SQL disposition")
        admitted = self.attempt
        binding = self.required_work
        ticket = binding.coordinator.reserve(
            RequiredWorkSource.UNDISPATCHED_ATTEMPT_CANCELLATION_SQL,
            transition_ordinal=binding.transition_ordinal,
            semantic_ordinal=binding.semantic_ordinal,
            recurrence_ordinal=self._undispatched_recurrence,
        )
        self._undispatched_recurrence += 1
        _, cancellations = await self._join(
            self.service.cancel_undispatched_provider_attempt(
                session_operation_context=self.context,
                attempt_id=self.attempt.attempt_id,
                requested_model=model,
                required_work=ticket,
            )
        )
        if self._primary_no_sdk_issuer is not None:
            self._primary_no_sdk_issuer.note_undispatched_cancel(self, admitted, model=model)
        if self._semantic_no_sdk_issuer is not None:
            self._semantic_no_sdk_issuer.note_undispatched_cancel(self, admitted, model=model)
        self.attempt = None
        self.call = None
        return cancellations

    async def cancel_before_sdk(self, *, model: str) -> tuple[asyncio.CancelledError, ...]:
        """Dispose an actually admitted attempt whose physical SDK never began."""
        return await self._cancel_undispatched(model=model)

    async def admit(self, *, model: str) -> None:
        if type(model) is not str or not model or self._admitting:
            raise AuditIntegrityError("Provider admission lacks a single owned model invocation")
        self._admitting = True
        try:
            await self.settle()
            self.require_cost_dispatch()
            self._cost_response_accountable = False
            title = self.required_work.role is RequiredWorkRole.TITLE
            sql, projection = self.required_work.reserve_pair(
                RequiredWorkSource.TITLE_PROVIDER_ADMISSION_SQL if title else RequiredWorkSource.PROVIDER_ADMISSION_SQL,
                RequiredWorkSource.TITLE_PROVIDER_ADMISSION_PROJECTION if title else RequiredWorkSource.PROVIDER_ADMISSION_PROJECTION,
            )
            cold_recovered = False
            recovered_failure: BaseException | None = None
            try:
                if self.continuation_checkpoint_v4 is not None:
                    from elspeth.web.async_workers import run_required_sql_finish_once
                    from elspeth.web.sessions.continuation_checkpoint_paid import V4CheckpointProviderAdmission
                    from elspeth.web.sessions.service import SessionServiceImpl

                    checkpoint_service = self.service
                    checkpoint_prepared = self.continuation_checkpoint_v4
                    if type(checkpoint_service) is not SessionServiceImpl or self._checkpoint_no_sdk_issuer is None:
                        raise AuditIntegrityError("V4 checkpoint admission requires its actual session and no-SDK owners")
                    checkpoint_outcome, checkpoint_outer = await self._join(
                        run_required_sql_finish_once(
                            sql,
                            checkpoint_service.begin_v4_checkpoint_provider_attempt_sync,
                            prepared=checkpoint_prepared,
                            requested_model=model,
                        )
                    )
                    if type(checkpoint_outcome) is RequiredSQLRaised:
                        projection.complete_without_submission()
                        checkpoint_failures: list[BaseException] = [checkpoint_outcome.error]
                        checkpoint_failures.extend((*checkpoint_outcome.deferred_cancellations, *checkpoint_outer))
                        if len(checkpoint_failures) == 1:
                            raise checkpoint_outcome.error
                        raise BaseExceptionGroup("V4 checkpoint admission SQL and original cancellations", checkpoint_failures)
                    if type(checkpoint_outcome) is not RequiredSQLReturned:
                        raise AuditIntegrityError("V4 checkpoint admission lost its actual required SQL outcome")
                    checkpoint_admitted = checkpoint_outcome.value
                    if type(checkpoint_admitted) is not V4CheckpointProviderAdmission:
                        raise AuditIntegrityError("V4 checkpoint admission lost its exact physical and hold result")
                    attempt = checkpoint_admitted.attempt
                    cancellations = (*checkpoint_outcome.deferred_cancellations, *checkpoint_outer)
                elif self.continuation_classifier_v4 is not None:
                    from elspeth.web.sessions.continuation_classifier_primary import V4ClassifierProviderAdmission
                    from elspeth.web.sessions.service import SessionServiceImpl

                    classifier_service = self.service
                    classifier_prepared = self.continuation_classifier_v4
                    classifier_issuer = self._classifier_no_sdk_issuer
                    if type(classifier_service) is not SessionServiceImpl or classifier_issuer is None:
                        raise AuditIntegrityError("V4 classifier admission requires its actual session and no-SDK owners")
                    classifier_projected = await self._v4_admission_result(
                        classifier_service.begin_v4_continuation_classifier_attempt(
                            prepared=classifier_prepared,
                            requested_model=model,
                            no_sdk_issuer=classifier_issuer,
                            required_work=sql,
                        ),
                        admission_projection=projection,
                        readback=lambda read_sql: classifier_service.read_v4_continuation_classifier_admission(
                            prepared=classifier_prepared,
                            no_sdk_issuer=classifier_issuer,
                            required_work=read_sql,
                        ),
                    )
                    classifier_admitted = classifier_projected.admitted
                    cancellations = classifier_projected.deferred_cancellations
                    cold_recovered = classifier_projected.cold_recovered
                    recovered_failure = classifier_projected.original_failure
                    if type(classifier_admitted) is not V4ClassifierProviderAdmission:
                        raise AuditIntegrityError("V4 classifier admission lost its exact physical and hold result")
                    classifier_issuer.bind_dispatched(classifier_admitted.dispatched)
                    attempt = classifier_admitted.attempt
                    self.continuation_classifier_v4_admission = classifier_admitted
                elif self.continuation_primary_v4 is not None:
                    from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission
                    from elspeth.web.sessions.service import SessionServiceImpl

                    primary_service = self.service
                    primary_prepared = self.continuation_primary_v4
                    primary_issuer = self._primary_no_sdk_issuer
                    if type(primary_service) is not SessionServiceImpl or primary_issuer is None:
                        raise AuditIntegrityError("V4 PRIMARY admission requires its actual session and no-SDK owners")
                    primary_projected = await self._v4_admission_result(
                        primary_service.begin_v4_continuation_primary_attempt(
                            prepared=primary_prepared,
                            requested_model=model,
                            no_sdk_issuer=primary_issuer,
                            required_work=sql,
                        ),
                        admission_projection=projection,
                        readback=lambda read_sql: primary_service.read_v4_continuation_primary_admission(
                            prepared=primary_prepared,
                            required_work=read_sql,
                        ),
                    )
                    primary_admitted = primary_projected.admitted
                    cancellations = primary_projected.deferred_cancellations
                    cold_recovered = primary_projected.cold_recovered
                    recovered_failure = primary_projected.original_failure
                    if type(primary_admitted) is not V4PrimaryProviderAdmission:
                        raise AuditIntegrityError("V4 PRIMARY admission lost its exact physical and hold result")
                    if cold_recovered:
                        primary_issuer.bind_cold_recovered_dispatched(primary_admitted.selected)
                    else:
                        primary_issuer.bind_dispatched(primary_admitted.selected)
                    attempt = primary_admitted.attempt
                    self.continuation_primary_v4_admission = primary_admitted
                elif self.continuation_semantic_v4 is not None:
                    from elspeth.web.sessions.continuation_semantic_advisor import V4SemanticProviderAdmission
                    from elspeth.web.sessions.service import SessionServiceImpl

                    semantic_service = self.service
                    semantic_prepared = self.continuation_semantic_v4
                    semantic_issuer = self._semantic_no_sdk_issuer
                    if type(semantic_service) is not SessionServiceImpl or semantic_issuer is None:
                        raise AuditIntegrityError("V4 semantic admission requires its actual session and no-SDK owners")
                    semantic_projected = await self._v4_admission_result(
                        semantic_service.begin_v4_continuation_semantic_attempt(
                            prepared=semantic_prepared,
                            requested_model=model,
                            no_sdk_issuer=semantic_issuer,
                            required_work=sql,
                        ),
                        admission_projection=projection,
                        readback=lambda read_sql: semantic_service.read_v4_continuation_semantic_admission(
                            prepared=semantic_prepared,
                            no_sdk_issuer=semantic_issuer,
                            required_work=read_sql,
                        ),
                    )
                    semantic_admitted = semantic_projected.admitted
                    cancellations = semantic_projected.deferred_cancellations
                    cold_recovered = semantic_projected.cold_recovered
                    recovered_failure = semantic_projected.original_failure
                    if type(semantic_admitted) is not V4SemanticProviderAdmission:
                        raise AuditIntegrityError("V4 semantic admission lost its exact physical and hold result")
                    semantic_issuer.bind_dispatched(semantic_admitted.selected)
                    attempt = semantic_admitted.attempt
                    self.continuation_semantic_v4_admission = semantic_admitted
                elif self.continuation_title is None:
                    attempt, cancellations = await self._join(
                        self.service.begin_provider_attempt(
                            session_operation_context=self.context,
                            source="auto_title" if title else "composer",
                            required_work=sql,
                        )
                    )
                else:
                    from elspeth.web.sessions.continuation_title_provider import TitleProviderAdmission, V4TitleProviderAdmission
                    from elspeth.web.sessions.service import SessionServiceImpl

                    if type(self.service) is not SessionServiceImpl:
                        raise AuditIntegrityError("TITLE admission requires its actual sessions owner")
                    title_admitted, cancellations = await self._join(
                        self.service.begin_continuation_title_attempt(
                            prepared=self.continuation_title, requested_model=model, required_work=sql
                        )
                    )
                    if type(title_admitted) not in (TitleProviderAdmission, V4TitleProviderAdmission):
                        raise AuditIntegrityError("TITLE admission lost its exact physical/source result")
                    attempt = title_admitted.attempt
                    self.continuation_title_selected = title_admitted.selected
                    if type(title_admitted) is V4TitleProviderAdmission:
                        self.continuation_title_v4_admission = title_admitted
            except BaseException:
                if sql.complete and not projection.complete:
                    projection.complete_without_submission()
                raise
            if not cold_recovered:
                projection.begin_projection()
            try:
                if type(attempt) is not ProviderAttempt:
                    raise AuditIntegrityError("Provider admission returned a foreign attempt")
                self.attempt = attempt
                if self.continuation_checkpoint_v4 is not None:
                    self.continuation_checkpoint_v4_admission = checkpoint_admitted
                    if self._checkpoint_no_sdk_issuer is None:
                        raise AuditIntegrityError("V4 checkpoint admission lost its original no-SDK owner")
                    self._checkpoint_no_sdk_issuer.bind_admission(self, checkpoint_admitted, observed_model=model)
                if self._primary_no_sdk_issuer is not None:
                    self._primary_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)
                if self._classifier_no_sdk_issuer is not None:
                    self._classifier_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)
                if self._semantic_no_sdk_issuer is not None:
                    self._semantic_no_sdk_issuer.note_admitted_attempt(self, attempt, model=model)
                self._admitted_ns = time.monotonic_ns()
                self.call = None
                self._sdk_entered = False
            except BaseException as failure:
                if not cold_recovered:
                    projection.complete_owned(failure)
                raise
            if not cold_recovered:
                projection.complete_owned()
            if recovered_failure is not None:
                failures: list[BaseException] = [recovered_failure]
                for cancellation in cancellations:
                    if all(cancellation is not prior for prior in failures):
                        failures.append(cancellation)
                if len(failures) == 1:
                    raise recovered_failure
                raise BaseExceptionGroup("V4 recovered admission and original cancellations", failures)
            if cancellations:
                if self.has_v4_unentered_hold():
                    # The original V4 hold needs its own zero-use SQL
                    # disposition; preserve the cancellation for that owner.
                    self.raise_deferred_cancellations(cancellations)
                additional = await self._cancel_undispatched(model=model)
                self.raise_deferred_cancellations((*cancellations, *additional))
        finally:
            self._admitting = False

    def mark_sdk_entered(self) -> None:
        if self.attempt is None or self._sdk_entered or self.call is not None:
            raise AuditIntegrityError("Physical provider dispatch lacks exact admitted custody")
        if self._primary_no_sdk_issuer is not None:
            self._primary_no_sdk_issuer.mark_sdk_entered(self)
        if self._classifier_no_sdk_issuer is not None:
            self._classifier_no_sdk_issuer.note_sdk_entry(self)
        if self._semantic_no_sdk_issuer is not None:
            self._semantic_no_sdk_issuer.mark_sdk_entered(self)
        if self._cost_budget is not None:
            self._cost_budget.dispatched()
        self._sdk_entered = True

    def has_undispatched_attempt(self) -> bool:
        return self.attempt is not None and not self._sdk_entered

    def has_v4_unentered_hold(self) -> bool:
        attempt = self.attempt
        return (
            attempt is not None
            and not self._sdk_entered
            and (
                (self.continuation_primary_v4_admission is not None and attempt is self.continuation_primary_v4_admission.attempt)
                or (self.continuation_classifier_v4_admission is not None and attempt is self.continuation_classifier_v4_admission.attempt)
                or (self.continuation_checkpoint_v4_admission is not None and attempt is self.continuation_checkpoint_v4_admission.attempt)
                or (self.continuation_semantic_v4_admission is not None and attempt is self.continuation_semantic_v4_admission.attempt)
                or (self.continuation_title_v4_admission is not None and attempt is self.continuation_title_v4_admission.attempt)
            )
        )

    def requires_v4_original_result(self) -> bool:
        """Select original SDK-return admission from this exact entered V4 attempt."""
        admissions = tuple(
            admission
            for admission in (
                self.continuation_checkpoint_v4_admission,
                self.continuation_classifier_v4_admission,
                self.continuation_primary_v4_admission,
                self.continuation_semantic_v4_admission,
                self.continuation_title_v4_admission,
            )
            if admission is not None
        )
        if not admissions:
            if any(
                prepared is not None
                for prepared in (
                    self.continuation_checkpoint_v4,
                    self.continuation_classifier_v4,
                    self.continuation_primary_v4,
                    self.continuation_semantic_v4,
                )
            ):
                raise AuditIntegrityError("V4 provider SDK return lacks its original admitted physical attempt")
            return False
        if len(admissions) != 1 or self.attempt is not admissions[0].attempt or not self._sdk_entered:
            raise AuditIntegrityError("V4 provider SDK return changed its original paid attempt")
        return True

    def note_v4_sdk_returned(self, response: object) -> None:
        from elspeth.web.composer.provider_result_admission import V4ProviderSdkReturn

        if not self.requires_v4_original_result():
            return
        if self.attempt is None or self._v4_sdk_return is not None:
            raise AuditIntegrityError("V4 provider returned more than one result for its original paid attempt")
        self._v4_sdk_return = V4ProviderSdkReturn(self.attempt.attempt_id, id(response))

    def claim_v4_sdk_returned(self, response: object) -> V4ProviderSdkReturn | None:
        if not self.requires_v4_original_result():
            return None
        witness = self._v4_sdk_return
        if (
            witness is None
            or self.attempt is None
            or witness.attempt_id != self.attempt.attempt_id
            or witness.response_identity != id(response)
            or self._v4_sdk_return_claimed
        ):
            raise AuditIntegrityError("V4 provider parser lost its actual one-time SDK return")
        self._v4_sdk_return_claimed = True
        return witness

    def require_v4_result_completeness(self, witness: V4ProviderResultCompleteness) -> None:
        from elspeth.web.composer.provider_result_admission import V4ProviderResultCompleteness

        if (
            type(witness) is not V4ProviderResultCompleteness
            or not self.requires_v4_original_result()
            or not self._v4_sdk_return_claimed
            or witness.sdk_return is not self._v4_sdk_return
        ):
            raise AuditIntegrityError("V4 result completeness lost its original one-time SDK return")

    def require_v4_primary_unentered(self, admission: V4PrimaryProviderAdmission) -> None:
        from elspeth.web.sessions.continuation_primary import V4PrimaryProviderAdmission

        if (
            type(admission) is not V4PrimaryProviderAdmission
            or self.continuation_primary_v4_admission is not admission
            or self.attempt is not admission.attempt
            or self._sdk_entered
        ):
            raise AuditIntegrityError("V4 PRIMARY no-SDK disposition lost its exact unentered physical hold")

    def require_v4_classifier_unentered(self, admission: V4ClassifierProviderAdmission) -> None:
        from elspeth.web.sessions.continuation_classifier_primary import V4ClassifierProviderAdmission

        if (
            type(admission) is not V4ClassifierProviderAdmission
            or self.continuation_classifier_v4_admission is not admission
            or self.attempt is not admission.attempt
            or self._sdk_entered
        ):
            raise AuditIntegrityError("V4 classifier no-SDK disposition lost its exact unentered physical hold")

    def require_v4_checkpoint_unentered(self, admission: V4CheckpointProviderAdmission) -> None:
        from elspeth.web.sessions.continuation_checkpoint_paid import V4CheckpointProviderAdmission

        if (
            type(admission) is not V4CheckpointProviderAdmission
            or self.continuation_checkpoint_v4_admission is not admission
            or self.attempt is not admission.attempt
            or self._sdk_entered
        ):
            raise AuditIntegrityError("V4 checkpoint no-SDK disposition lost its exact unentered physical hold")

    def note_v4_primary_no_sdk(self, receipt: PrimaryNoSdkRefusalReceipt, admission: V4PrimaryProviderAdmission) -> None:
        from elspeth.web.sessions.continuation_primary import PrimaryNoSdkRefusalReceipt

        self.require_v4_primary_unentered(admission)
        if (
            type(receipt) is not PrimaryNoSdkRefusalReceipt
            or receipt.effect_key != admission.hold.effect_key
            or receipt.terminal.status != "failed"
            or (receipt.terminal.checkpoint_id, receipt.terminal.journal_cursor) != (receipt.refused.checkpoint_id, receipt.refused.cursor)
            or self.continuation_primary_v4_no_sdk_result is not None
            or self.continuation_primary_v4_result is not None
        ):
            raise AuditIntegrityError("V4 PRIMARY no-SDK receipt lost its exact failed terminal")
        self.continuation_primary_v4_no_sdk_result = receipt
        self.attempt = None
        self.call = None

    def note_v4_classifier_no_sdk(self, receipt: ClassifierNoSdkRefusalReceipt, admission: V4ClassifierProviderAdmission) -> None:
        from elspeth.web.sessions.continuation_classifier_primary_no_sdk import ClassifierNoSdkRefusalReceipt

        self.require_v4_classifier_unentered(admission)
        if (
            type(receipt) is not ClassifierNoSdkRefusalReceipt
            or receipt.effect_key != admission.effect_key
            or receipt.terminal.status != "failed"
            or (receipt.terminal.checkpoint_id, receipt.terminal.journal_cursor) != (receipt.refused.checkpoint_id, receipt.refused.cursor)
            or self.continuation_classifier_v4_no_sdk_result is not None
            or self.continuation_classifier_v4_result is not None
        ):
            raise AuditIntegrityError("V4 classifier no-SDK receipt lost its exact failed terminal")
        self.continuation_classifier_v4_no_sdk_result = receipt
        self.attempt = None
        self.call = None

    def needs_terminal_audit(self) -> bool:
        return self.attempt is not None and self._sdk_entered and self.call is None

    def bind_call(self, call: ComposerLLMCall) -> ComposerLLMCall:
        if type(call) is not ComposerLLMCall:
            raise AuditIntegrityError("Provider audit is not an owned complete call")
        if self.attempt is None or not self._sdk_entered:
            raise AuditIntegrityError("Provider audit lacks actual dispatched admission")
        bound = _bind_attempt_call(call, self.attempt, self._admitted_ns)
        self.call = bound
        return bound

    def retain_audit(self, call: ComposerLLMCall) -> bool:
        if type(call) is not ComposerLLMCall:
            raise AuditIntegrityError("Provider retained audit is not nominal")
        if self.attempt is None:
            return False
        if not self._sdk_entered or call.call_id != self.attempt.attempt_id or call.started_at != self.attempt.started_at:
            raise AuditIntegrityError("Provider audit replaced actual admitted identity")
        if (
            self._cost_budget is not None
            and self._cost_budget.snapshot().applicability == "continuation_v2"
            and (call.cost_response_accountable is None or call.cost_response_accountable is not self._cost_response_accountable)
        ):
            raise AuditIntegrityError("Continuation audit changed its actual response accountability")
        self.call = call
        return True

    async def finalize_continuation_primary_v4(self, outcome: _CallModelOutcome) -> None:
        from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
        from elspeth.web.sessions.continuation_primary import PrimaryPaidOverflowTerminal, PrimaryProviderResult

        finalizer = self._continuation_primary_v4_finalizer
        if (
            type(outcome) is not _CallModelOutcome
            or finalizer is None
            or self.continuation_primary_v4 is None
            or self.continuation_primary_v4_admission is None
            or self.continuation_primary_v4_result is not None
            or self.attempt is None
            or not self._sdk_entered
            or self.call is None
            or self.call.status is not ComposerLLMCallStatus.SUCCESS
            or outcome.audit_call is not self.call
        ):
            raise AuditIntegrityError("V4 PRIMARY finalization requires its one actual paid success and original audit")
        binding = self.required_work
        sql, projection = binding.reserve_pair(
            RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION
        )
        try:
            settled, cancellations = await self._join(finalizer(outcome, sql))
        except BaseException:
            if sql.complete:
                projection.complete_without_submission()
            raise
        if type(settled) is RequiredSQLRaised:
            if sql.complete:
                projection.complete_without_submission()
            failures: list[BaseException] = [settled.error]
            for cancellation in (*settled.deferred_cancellations, *cancellations):
                if all(cancellation is not prior for prior in failures):
                    failures.append(cancellation)
            if len(failures) == 1:
                raise settled.error
            raise BaseExceptionGroup("PRIMARY V4 required SQL and original cancellations", failures)
        if type(settled) is not RequiredSQLReturned:
            raise AuditIntegrityError("V4 PRIMARY finalizer lost its actual required SQL outcome")
        result = settled.value
        # The SQL owner has committed the complete result or terminal receipt.
        # No later local failure may fall back to an ordinary full-audit write.
        self._continuation_primary_v4_sql_committed = True
        if type(result) not in (PrimaryProviderResult, PrimaryPaidOverflowTerminal):
            raise AuditIntegrityError("V4 PRIMARY finalizer returned a foreign paid outcome")
        self._continuation_primary_v4_terminal = type(result) is PrimaryPaidOverflowTerminal
        projection.begin_projection()
        projection.complete_owned()
        if self._cost_budget is not None:
            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)
        self.continuation_primary_v4_result = result
        self.attempt = None
        self.call = None
        self._sdk_entered = False
        deferred: list[asyncio.CancelledError] = []
        for cancellation in (*settled.deferred_cancellations, *cancellations):
            if all(cancellation is not prior for prior in deferred):
                deferred.append(cancellation)
        self.continuation_primary_v4_deferred_cancellations = tuple(deferred)

    async def finalize_continuation_classifier_v4(self, outcome: _CallModelOutcome) -> None:
        from elspeth.web.composer._compose_loop_carriers import _CallModelOutcome
        from elspeth.web.sessions.continuation_classifier_primary import ClassifierPrimaryResult, VerifiedClassifierPaidOverflowTerminal

        finalizer = self._continuation_classifier_v4_finalizer
        if (
            type(outcome) is not _CallModelOutcome
            or finalizer is None
            or self.continuation_classifier_v4 is None
            or self.continuation_classifier_v4_admission is None
            or self.continuation_classifier_v4_result is not None
            or self.attempt is None
            or not self._sdk_entered
            or self.call is None
            or self.call.status is not ComposerLLMCallStatus.SUCCESS
            or outcome.audit_call is not self.call
        ):
            raise AuditIntegrityError("V4 classifier finalization requires its one actual paid success and original audit")
        sql, projection = self.required_work.reserve_pair(
            RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION
        )
        try:
            settled, cancellations = await self._join(finalizer(outcome, sql))
        except BaseException:
            if sql.complete:
                projection.complete_without_submission()
            raise
        if type(settled) is RequiredSQLRaised:
            if sql.complete:
                projection.complete_without_submission()
            failures: list[BaseException] = [settled.error]
            for cancellation in (*settled.deferred_cancellations, *cancellations):
                if all(cancellation is not prior for prior in failures):
                    failures.append(cancellation)
            if len(failures) == 1:
                raise settled.error
            raise BaseExceptionGroup("Classifier V4 required SQL and original cancellations", failures)
        if type(settled) is not RequiredSQLReturned:
            raise AuditIntegrityError("V4 classifier finalizer lost its actual required SQL outcome")
        result = settled.value
        if type(result) not in (ClassifierPrimaryResult, VerifiedClassifierPaidOverflowTerminal):
            raise AuditIntegrityError("V4 classifier finalizer returned a foreign paid outcome")
        self._continuation_classifier_v4_terminal = type(result) is VerifiedClassifierPaidOverflowTerminal
        projection.begin_projection()
        projection.complete_owned()
        if self._cost_budget is not None:
            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)
        self.continuation_classifier_v4_result = result
        self.attempt = None
        self.call = None
        self._sdk_entered = False
        deferred: list[asyncio.CancelledError] = []
        for cancellation in (*settled.deferred_cancellations, *cancellations):
            if all(cancellation is not prior for prior in deferred):
                deferred.append(cancellation)
        self.continuation_classifier_v4_deferred_cancellations = tuple(deferred)

    async def finalize_continuation_semantic_v4(self, observation: SemanticProviderResponseObservation) -> SemanticV4Finalization:
        from elspeth.web.composer.continuation_semantic_observation import (
            SemanticCompleteResponseObservation,
            SemanticIncompleteResponseObservation,
        )
        from elspeth.web.sessions.continuation_semantic_advisor import SemanticAdvisorProviderResult
        from elspeth.web.sessions.continuation_semantic_paid_overflow_terminal import VerifiedSemanticPaidOverflowTerminal

        finalizer = self._continuation_semantic_v4_finalizer
        if (
            type(observation) not in (SemanticCompleteResponseObservation, SemanticIncompleteResponseObservation)
            or finalizer is None
            or self.continuation_semantic_v4 is None
            or self.continuation_semantic_v4_admission is None
            or self.continuation_semantic_v4_result is not None
            or self.attempt is None
            or not self._sdk_entered
            or self.call is None
            or self.call.status is not ComposerLLMCallStatus.SUCCESS
        ):
            raise AuditIntegrityError("V4 semantic finalization requires its one actual paid success and original audit")
        sql, projection = self.required_work.reserve_pair(
            RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION
        )
        try:
            outcome, cancellations = await self._join(finalizer(observation, sql))
        except BaseException:
            if sql.complete:
                projection.complete_without_submission()
            raise
        if type(outcome) is RequiredSQLRaised:
            if sql.complete:
                projection.complete_without_submission()
            failures: list[BaseException] = [outcome.error]
            for cancellation in (*outcome.deferred_cancellations, *cancellations):
                if all(cancellation is not prior for prior in failures):
                    failures.append(cancellation)
            if len(failures) == 1:
                raise outcome.error
            raise BaseExceptionGroup("Semantic V4 required SQL and original cancellations", failures)
        if type(outcome) is not RequiredSQLReturned:
            raise AuditIntegrityError("V4 semantic finalizer lost its actual required SQL outcome")
        result = outcome.value
        self._continuation_semantic_v4_sql_committed = True
        if type(result) not in (SemanticAdvisorProviderResult, VerifiedSemanticPaidOverflowTerminal):
            raise AuditIntegrityError("V4 semantic finalizer returned a foreign paid outcome")
        self._continuation_semantic_v4_terminal = type(result) is VerifiedSemanticPaidOverflowTerminal
        projection.begin_projection()
        projection.complete_owned()
        if self._cost_budget is not None:
            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)
        self.continuation_semantic_v4_result = result
        self.attempt = None
        self.call = None
        self._sdk_entered = False
        deferred: list[asyncio.CancelledError] = []
        for cancellation in (*outcome.deferred_cancellations, *cancellations):
            if all(cancellation is not prior for prior in deferred):
                deferred.append(cancellation)
        return SemanticV4Finalization(result, tuple(deferred))

    async def settle(self) -> None:
        if self.attempt is None:
            return
        if self.has_v4_unentered_hold():
            # The V4 no-SDK owner closes the physical row and H together.
            return
        if self.continuation_title_v4_admission is not None and self._sdk_entered:
            # V4 TITLE success or paid overflow must prove its complete or
            # bounded terminal SQL result before this custody can close. A
            # different post-SDK failure remains pending for cold recovery.
            return
        if self.continuation_semantic_v4_admission is not None and self._sdk_entered:
            return
        if self.continuation_primary_v4_admission is not None and self._sdk_entered:
            # Every SDK-entered V4 PRIMARY outcome needs its exact SQL
            # disposition. Generic settlement would orphan its active hold.
            return
        if self.continuation_classifier_v4_admission is not None and self._sdk_entered:
            return
        if self.continuation_checkpoint_v4_admission is not None and self._sdk_entered:
            return
        if not self._sdk_entered or self.call is None:
            raise AuditIntegrityError("Required provider attempt lacks complete terminal audit")
        current_title: TitleAuditSettlementRequest | None = None
        if self.continuation_title_result is not None:
            from elspeth.web.composer.continuation_title_provider import refresh_current_title_settlement
            from elspeth.web.sessions.service import SessionServiceImpl

            if type(self.service) is not SessionServiceImpl:
                raise AuditIntegrityError("TITLE settlement requires its actual sessions writer")
            # Current reads may refuse or be cancelled. Do not allocate the
            # accounting receipt until it can be handed to the real finisher.
            current_title = await refresh_current_title_settlement(self)
        binding = self.required_work
        projection: RequiredWorkTicket | None = None
        if binding.role is RequiredWorkRole.TITLE:
            sql = binding.coordinator.reserve(
                RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL,
                transition_ordinal=binding.transition_ordinal,
                semantic_ordinal=binding.semantic_ordinal,
                recurrence_ordinal=self._settlements,
            )
            self._settlements += 1
        else:
            sql, projection = binding.reserve_pair(
                RequiredWorkSource.PROVIDER_SETTLEMENT_SQL, RequiredWorkSource.PROVIDER_SETTLEMENT_PROJECTION
            )
        try:
            if self.continuation_title_result is None:
                _, cancellations = await self._join(
                    self.service.finish_provider_attempt(
                        session_operation_context=self.context,
                        call=self.call,
                        required_work=sql,
                    )
                )
            else:
                from elspeth.web.sessions.service import SessionServiceImpl

                if type(self.service) is not SessionServiceImpl or current_title is None:
                    raise AuditIntegrityError("TITLE settlement lost its actual current source result")
                _, cancellations = await self._join(
                    self.service.finish_provider_attempt(
                        session_operation_context=self.context,
                        call=current_title.audit_call,
                        required_work=sql,
                        continuation_title=current_title,
                    )
                )
        except BaseException:
            if projection is not None and sql.complete:
                projection.complete_without_submission()
            raise
        if projection is not None:
            projection.begin_projection()
            projection.complete_owned()
        if self._cost_budget is not None:
            if self._cost_budget.snapshot().applicability == "continuation_v2" and (
                self.call.cost_response_accountable is None or self.call.cost_response_accountable is not self._cost_response_accountable
            ):
                raise AuditIntegrityError("Continuation settlement lost its observed response accountability")
            self._cost_budget.consume_settled(self.call, response_accountable=self._cost_response_accountable)
        self.attempt = None
        self.call = None
        self._sdk_entered = False
        self.raise_deferred_cancellations(cancellations)


@contextmanager
def required_provider_audit_scope(custody: ProviderCallCustody | None) -> Iterator[None]:
    """Keep an original SDK/body outcome when owned audit construction fails."""
    original = sys.exception()
    try:
        yield
    except BaseException as audit_failure:
        if custody is not None and original is not None and audit_failure is not original:
            raise BaseExceptionGroup("Provider original body and audit construction outcomes", [original, audit_failure]) from None
        raise


@asynccontextmanager
async def provider_call_scope(custody: ProviderCallCustody) -> AsyncIterator[ProviderCallCustody]:
    if type(custody) is not ProviderCallCustody:
        raise AuditIntegrityError("Provider scope requires exact explicit custody")
    body: BaseException | None = None
    try:
        yield custody
    except BaseException as failure:
        body = failure
        raise
    finally:
        try:
            await custody.settle()
        except BaseException as settlement:
            if body is not None and settlement is not body:
                raise BaseExceptionGroup("Provider body and required settlement outcomes", [body, settlement]) from None
            raise
        try:
            custody.require_cost_output()
        except CommonProviderCostRefused as refusal:
            from elspeth.web.composer.provider_gateway import _MalformedLLMResponseError

            if isinstance(body, _MalformedLLMResponseError):
                raise refusal from body
            if body is not None:
                raise BaseExceptionGroup("Provider body and monetary continuation refusal", [body, refusal]) from None
            raise


@dataclass(frozen=True, slots=True)
class _QuotaScope:
    service: SessionServiceProtocol
    context: SessionOperationContext


@dataclass(slots=True)
class _ProviderSpan:
    scope: _QuotaScope
    attempt: ProviderAttempt | None = None
    call: ComposerLLMCall | None = None
    admitted_ns: int = 0


_SCOPE: ContextVar[_QuotaScope | None] = ContextVar("composer_quota_scope", default=None)
_SPAN: ContextVar[_ProviderSpan | None] = ContextVar("composer_provider_span", default=None)
_CONTEXTVAR_GET = ContextVar.get


@contextmanager
def composer_quota_scope(service: SessionServiceProtocol, context: SessionOperationContext | None) -> Iterator[None]:
    """Bind session authority for provider work, restoring it on every exit."""
    if type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE:
        raise AuditIntegrityError("Composer quota scope requires an exact COMPOSE operation context")
    token = _SCOPE.set(_QuotaScope(service, context))
    try:
        yield
    finally:
        _SCOPE.reset(token)


async def _settle(span: _ProviderSpan) -> None:
    if span.attempt is None:
        return
    if span.call is None:
        # The pending row remains unknown. Never invent a terminal verdict
        # when the owning audit path did not produce one.
        raise AuditIntegrityError("Composer provider attempt has no terminal audit evidence")
    settlement = asyncio.create_task(
        span.scope.service.finish_provider_attempt(session_operation_context=span.scope.context, call=span.call)
    )
    try:
        await asyncio.shield(settlement)
    except asyncio.CancelledError as cancellation:
        # The shielded write can outlive repeated caller cancellation. Observe
        # its actual outcome before releasing the operation's audit authority.
        while not settlement.done():
            with suppress(BaseException):
                await asyncio.shield(settlement)
        if settlement.cancelled():
            raise AuditIntegrityError("Composer provider settlement was cancelled before durable completion") from cancellation
        failure = settlement.exception()
        if failure is not None:
            if isinstance(failure, contract_errors.TIER_1_ERRORS):
                raise failure from cancellation
            raise cancellation from failure
        span.attempt = None
        span.call = None
        raise cancellation
    settlement.result()
    span.attempt = None
    span.call = None


def quota_provider_calls[**P, R](operation: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Enclose an audited provider operation, including its retry attempts."""

    @wraps(operation)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        scope = _CONTEXTVAR_GET(_SCOPE)
        if scope is None:
            return await operation(*args, **kwargs)
        span = _ProviderSpan(scope)
        token = _SPAN.set(span)
        try:
            return await operation(*args, **kwargs)
        finally:
            primary = sys.exception()
            try:
                try:
                    await _settle(span)
                except BaseException as settlement_error:
                    if isinstance(primary, asyncio.CancelledError) and settlement_error is not primary:
                        if isinstance(settlement_error, asyncio.CancelledError):
                            child_failure = settlement_error.__cause__
                            if child_failure is None:
                                raise primary from None
                            settlement_error = child_failure
                        if isinstance(settlement_error, contract_errors.TIER_1_ERRORS):
                            raise settlement_error from primary
                        raise primary from settlement_error
                    raise
            finally:
                _SPAN.reset(token)

    return wrapped


async def _join_after_cancellation[R](task: asyncio.Task[R], cancellation: asyncio.CancelledError) -> R:
    """Observe an owned task after cancellation without losing its result."""
    while not task.done():
        with suppress(BaseException):
            await asyncio.shield(task)
    if task.cancelled():
        raise AuditIntegrityError("Composer provider custody task cancelled before its outcome was known") from cancellation
    try:
        return task.result()
    except BaseException as failure:
        if isinstance(failure, contract_errors.TIER_1_ERRORS):
            raise failure from cancellation
        if isinstance(failure, (ChargeableAdmissionRefused, SessionOperationFenceLost, SQLAlchemyError)):
            raise cancellation from failure
        raise ComposerOwnedSettlementFailure() from failure


async def admit_provider_attempt(*, model: str) -> None:
    """Commit pending evidence and admission before the physical dispatch."""
    if _CONTEXTVAR_GET(_SCOPE) is None:
        return
    span = _CONTEXTVAR_GET(_SPAN)
    if span is None:
        raise AuditIntegrityError("Composer provider dispatch lacks an audited quota span")
    if type(model) is not str or not model:
        raise AuditIntegrityError("Composer provider attempt requires an owned requested model")
    # Retrying is another chargeable call. Settle the preceding attempt
    # before admission so one retry loop cannot run past a measured cap.
    await _settle(span)
    admission = asyncio.create_task(
        span.scope.service.begin_provider_attempt(session_operation_context=span.scope.context, source="composer")
    )
    try:
        span.attempt = await asyncio.shield(admission)
        span.admitted_ns = time.monotonic_ns()
    except asyncio.CancelledError as cancellation:
        attempt = await _join_after_cancellation(admission, cancellation)
        # The SDK has not been entered: only this narrow path can assert zero
        # usage without inventing a provider call. The authority records that
        # explicit disposition under the original admission fence.
        disposition = asyncio.create_task(
            span.scope.service.cancel_undispatched_provider_attempt(
                session_operation_context=span.scope.context,
                attempt_id=attempt.attempt_id,
                requested_model=model,
            )
        )
        try:
            await asyncio.shield(disposition)
        except asyncio.CancelledError:
            try:
                await _join_after_cancellation(disposition, cancellation)
            except ComposerOwnedSettlementFailure as failure:
                raise AuditIntegrityError("Composer undispatched provider attempt could not be settled") from failure
            except asyncio.CancelledError as interrupted:
                if interrupted.__cause__ is not None:
                    raise AuditIntegrityError("Composer undispatched provider attempt could not be settled") from interrupted.__cause__
                raise
        except BaseException as failure:
            if isinstance(failure, contract_errors.TIER_1_ERRORS):
                raise failure from cancellation
            raise AuditIntegrityError("Composer undispatched provider attempt could not be settled") from failure
        raise cancellation
    except (ChargeableAdmissionRefused, SessionOperationFenceLost, SQLAlchemyError):
        raise
    except contract_errors.TIER_1_ERRORS:
        raise
    except Exception as failure:
        raise ComposerOwnedSettlementFailure() from failure


def bind_provider_attempt(call: ComposerLLMCall) -> ComposerLLMCall:
    """Use the database-clock identity of the actual dispatched attempt."""
    span = _CONTEXTVAR_GET(_SPAN)
    if span is None or span.attempt is None:
        return call
    bound = _bind_attempt_call(call, span.attempt, span.admitted_ns)
    span.call = bound
    return bound


def retain_provider_audit(call: ComposerLLMCall) -> bool:
    """Retain the final classification after caller validation of a response."""
    span = _CONTEXTVAR_GET(_SPAN)
    if span is not None:
        if span.attempt is None:
            # Admission failed before dispatch. The surrounding exception
            # remains authoritative; there was no provider call to audit.
            return False
        if call.call_id != span.attempt.attempt_id:
            raise AuditIntegrityError("Composer terminal audit does not identify its provider attempt")
        span.call = call
    return True
