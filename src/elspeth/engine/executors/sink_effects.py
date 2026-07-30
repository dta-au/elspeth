"""Caller-level coordinator for durable, recoverable sink effects.

The coordinator is the only production boundary allowed to invoke an
effect-capable sink's inspection, reconciliation, or commit methods.  Every
possibly observable call is bracketed by the durable sink-effect ledger and
final publication is committed atomically with the pipeline audit outcome.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import ClassVar, Literal, Protocol, cast

from elspeth.contracts.audit import SinkEffect, SinkEffectAttempt, SinkEffectMemberRecord
from elspeth.contracts.enums import CallType
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.results import ArtifactDescriptor
from elspeth.contracts.sink_effects import (
    MemberSinkEffectCapability,
    RestagingSinkEffectCapability,
    RestrictedSinkEffectContext,
    SinkEffectAttemptAction,
    SinkEffectAttemptRequest,
    SinkEffectAttemptResult,
    SinkEffectAttemptState,
    SinkEffectCommitResult,
    SinkEffectDescriptorMode,
    SinkEffectFinalizationMember,
    SinkEffectFinalizationResult,
    SinkEffectFinalizeRequest,
    SinkEffectInputKind,
    SinkEffectInspection,
    SinkEffectInspectionMode,
    SinkEffectInspectionRequest,
    SinkEffectLease,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
    SinkEffectReconcileKind,
    SinkEffectReconcileResult,
    SinkEffectReservationRequest,
    SinkEffectState,
)
from elspeth.core.clock import Clock
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.execution.sink_effect_attempt_results import (
    SinkEffectReturnedResult,
    decode_sink_effect_returned_result,
    encode_sink_effect_returned_result,
)
from elspeth.core.landscape.execution.sink_effects import SinkEffectRepository
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.clock import DEFAULT_CLOCK

logger = logging.getLogger(__name__)


class SinkEffectExecutionSeam(StrEnum):
    """Deterministic crash seams around the externally observable commit."""

    BEFORE_EFFECT = "before_effect"
    AFTER_EFFECT_BEFORE_RETURN = "after_effect_before_return"
    AFTER_RETURN_BEFORE_FINALIZE = "after_return_before_finalize"
    AFTER_FINALIZE_BEFORE_RESPONSE = "after_finalize_before_response"


class SinkEffectInjectedFault(RuntimeError):
    """Test-only crash signal raised by a configured execution seam."""

    def __init__(self, seam: SinkEffectExecutionSeam) -> None:
        self.seam = seam
        super().__init__(f"injected sink-effect fault at {seam.value}")


class SinkEffectUnknownError(RuntimeError):
    """Raised when reconciliation cannot prove whether publication happened."""

    def __init__(self, effect_id: str) -> None:
        self.effect_id = effect_id
        super().__init__(f"sink effect {effect_id} reconciliation is UNKNOWN; refusing to commit")


class SinkEffectPredecessorPending(RuntimeError):
    """Raised when the effect's stream predecessor has not finalized yet."""


class SinkEffectLeaseHeld(RuntimeError):
    """Raised when another worker's live lease or preparation claim covers the effect.

    The holder must be left alone (see the sink-effect recovery runbook); the
    contended effect becomes actionable again once the holder finalizes or its
    lease expires and takeover fences the generation.
    """


class _SinkEffectLeaseHeartbeat:
    """Keep one preparation or execution lease live during adapter I/O."""

    def __init__(
        self,
        *,
        effects: SinkEffectRepository,
        claim: SinkEffectLease,
        ttl: timedelta,
    ) -> None:
        self._effects = effects
        self._claim = claim
        self._ttl = ttl
        self._interval_seconds = max(ttl.total_seconds() / 3, 0.001)
        self._stop_event = threading.Event()
        self._failed_event = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"sink-effect-heartbeat:{claim.effect_id[:8]}",
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join()

    def check_and_raise(self) -> None:
        if not self._failed_event.is_set():
            return
        if self._error is None:  # pragma: no cover - Event publication orders the stored error first
            raise LandscapeRecordError("sink effect preparation heartbeat failed without an error")
        raise self._error

    def refresh_and_check(self) -> None:
        """Synchronously prove authority after an observable adapter call."""
        self.check_and_raise()
        self._effects.heartbeat_lease(
            self._claim.effect_id,
            owner=self._claim.owner,
            generation=self._claim.generation,
            ttl=self._ttl,
        )
        self.check_and_raise()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self._effects.heartbeat_lease(
                    self._claim.effect_id,
                    owner=self._claim.owner,
                    generation=self._claim.generation,
                    ttl=self._ttl,
                )
            except BaseException as exc:
                self._error = exc
                self._failed_event.set()
                return


class _SinkEffectAdapter(Protocol):
    effect_call_type: ClassVar[CallType]

    def inspect_effect(
        self,
        request: SinkEffectInspectionRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectInspection: ...

    def prepare_effect(
        self,
        request: SinkEffectPrepareRequest,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectPlan: ...

    def commit_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectCommitResult: ...

    def reconcile_effect(self, plan: SinkEffectPlan, ctx: RestrictedSinkEffectContext) -> SinkEffectReconcileResult: ...


class _MemberSinkEffectAdapter(_SinkEffectAdapter, Protocol):
    def commit_member_effect(
        self,
        plan: SinkEffectPlan,
        member: SinkEffectMember,
        effect_input: SinkEffectPipelineMembersInput,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectCommitResult: ...

    def reconcile_member_effect(
        self,
        plan: SinkEffectPlan,
        member: SinkEffectMember,
        effect_input: SinkEffectPipelineMembersInput,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectReconcileResult: ...


class _RestagingSinkEffectAdapter(_SinkEffectAdapter, Protocol):
    """Opt-in capability for sinks whose staged bodies live in a spool.

    Spool files can be lost while the durable plan survives (reboot,
    container restart, tmp-cleaner); restage re-derives the body from the
    effect input and verifies it against the plan before commit runs.
    """

    def restage_effect(
        self,
        plan: SinkEffectPlan,
        effect_input: SinkEffectPipelineMembersInput,
        ctx: RestrictedSinkEffectContext,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class SinkEffectExecutionRequest:
    """Complete caller-owned input needed to execute and finalize one effect."""

    reservation: SinkEffectReservationRequest
    effect_input: object
    finalization_members: Sequence[SinkEffectFinalizationMember]

    def __post_init__(self) -> None:
        from elspeth.contracts.sink_effects import SinkEffectAuditExportSnapshotInput, SinkEffectPipelineMembersInput

        if type(self.reservation) is not SinkEffectReservationRequest:
            raise TypeError("reservation must be exact SinkEffectReservationRequest")
        if not isinstance(self.effect_input, (SinkEffectPipelineMembersInput, SinkEffectAuditExportSnapshotInput)):
            raise TypeError("effect_input must be a closed sink-effect input value")
        if self.effect_input.input_kind is not self.reservation.input_kind:
            raise ValueError("effect_input kind must equal reservation input kind")
        members = tuple(self.finalization_members)
        if any(type(member) is not SinkEffectFinalizationMember for member in members):
            raise TypeError("finalization_members must contain exact SinkEffectFinalizationMember values")
        if tuple(member.ordinal for member in members) != tuple(sorted({member.ordinal for member in members})):
            raise ValueError("finalization member ordinals must be unique and ascending")
        object.__setattr__(self, "finalization_members", members)
        freeze_fields(self, "finalization_members")


class SinkEffectCoordinator:
    """Drive reservation, intent, external I/O, recovery, and finalization."""

    def __init__(
        self,
        *,
        factory: RecorderFactory,
        worker_id: str,
        lease_ttl: timedelta = timedelta(minutes=5),
        fault_hook: Callable[[SinkEffectExecutionSeam], None] | None = None,
        clock: Clock = DEFAULT_CLOCK,
        sleep: Callable[[float], None] | None = None,
        poll_interval: float = 0.5,
        shutdown_event: threading.Event | None = None,
        check_coordination_latch: Callable[[], None] | None = None,
        make_shutdown_error: Callable[[], BaseException] | None = None,
    ) -> None:
        if not isinstance(factory, RecorderFactory):
            raise TypeError("factory must be RecorderFactory")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if type(lease_ttl) is not timedelta or lease_ttl <= timedelta(0):
            raise ValueError("lease_ttl must be a positive timedelta")
        if not isinstance(poll_interval, int | float) or isinstance(poll_interval, bool):
            raise TypeError("poll_interval must be a positive finite number")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be a positive finite number")
        self._factory = factory
        self._effects = factory.execution.sink_effects
        self._worker_id = worker_id
        self._lease_ttl = lease_ttl
        self._fault_hook = fault_hook
        self._clock = clock
        self._sleep = sleep if sleep is not None else time.sleep
        self._poll_interval = float(poll_interval)
        self._shutdown_event = shutdown_event
        self._check_coordination_latch = check_coordination_latch
        self._make_shutdown_error = make_shutdown_error

    def execute(
        self,
        request: SinkEffectExecutionRequest,
        sink: _SinkEffectAdapter,
    ) -> SinkEffectFinalizationResult:
        """Execute once, preserving immediate ``SinkEffectLeaseHeld`` refusal."""
        return self._execute(request, sink, wait_for_lease=False)

    def execute_with_lease_wait(
        self,
        request: SinkEffectExecutionRequest,
        sink: _SinkEffectAdapter,
    ) -> SinkEffectFinalizationResult:
        """Execute with one fixed, TTL-derived budget for foreign live leases."""
        return self._execute(request, sink, wait_for_lease=True)

    def _execute(
        self,
        request: SinkEffectExecutionRequest,
        sink: _SinkEffectAdapter,
        *,
        wait_for_lease: bool,
    ) -> SinkEffectFinalizationResult:
        if type(request) is not SinkEffectExecutionRequest:
            raise TypeError("request must be exact SinkEffectExecutionRequest")

        self._persist_pipeline_member_payloads(request.effect_input)
        reservation = self._effects.reserve(request.reservation)
        effect_ids = (*reservation.finalized_effect_ids, *reservation.open_effect_ids)
        if reservation.new_effect is not None:
            effect_ids = (*effect_ids, reservation.new_effect.effect_id)
        effect_ids = tuple(dict.fromkeys(effect_ids))
        if not effect_ids:
            raise LandscapeRecordError("sink effect reservation returned no effect partition")
        effects = sorted(
            (self._require_effect(effect_id) for effect_id in effect_ids),
            key=lambda effect: (
                effect.stream_id is None,
                -1 if effect.stream_sequence is None else effect.stream_sequence,
                effect.effect_id,
            ),
        )
        results: list[SinkEffectFinalizationResult] = []
        wait_deadline: float | None = None
        for effect in effects:
            while True:
                refreshed = self._require_effect(effect.effect_id)
                if refreshed.state is SinkEffectState.FINALIZED:
                    results.append(self._load_finalized(refreshed.effect_id))
                    break
                partition_request = self._request_for_effect(refreshed, request)
                try:
                    results.append(self._execute_effect(refreshed, partition_request, sink))
                except SinkEffectLeaseHeld as exc:
                    if not wait_for_lease:
                        raise
                    wait_deadline = self._wait_until_effect_actionable(
                        effect_id=refreshed.effect_id,
                        deadline=wait_deadline,
                        original_error=exc,
                    )
                    continue
                break
        return results[-1]

    def _wait_until_effect_actionable(
        self,
        *,
        effect_id: str,
        deadline: float | None,
        original_error: SinkEffectLeaseHeld,
    ) -> float | None:
        """Poll strict repository state until finalize/retry, cancellation, or timeout."""
        while True:
            self._check_wait_interruptions()
            effect = self._require_effect(effect_id)
            remaining_validity = self._held_lease_remaining_seconds(effect)
            if remaining_validity is None:
                return deadline

            monotonic_now = self._clock.monotonic()
            if deadline is None:
                initial_budget = min(self._lease_ttl.total_seconds(), remaining_validity)
                # Repository takeover is deliberately strict (expires_at < now).
                # One bounded poll interval permits the final authority check to
                # cross an exact equality without introducing an open-ended wait.
                deadline = monotonic_now + initial_budget + self._poll_interval
            if monotonic_now >= deadline:
                logger.warning(
                    "Bounded sink-effect lease wait exhausted for effect %s in state %s "
                    "(lease_ttl_seconds=%.3f, poll_interval_seconds=%.3f)",
                    effect.effect_id,
                    effect.state.value,
                    self._lease_ttl.total_seconds(),
                    self._poll_interval,
                )
                raise original_error

            sleep_seconds = min(self._poll_interval, deadline - monotonic_now)
            if sleep_seconds <= 0.0:  # pragma: no cover - guarded by deadline check
                raise LandscapeRecordError("sink-effect lease wait produced a non-positive poll interval")
            if self._shutdown_event is not None and self._sleep is time.sleep:
                self._shutdown_event.wait(timeout=sleep_seconds)
            else:
                self._sleep(sleep_seconds)

    def _held_lease_remaining_seconds(self, effect: SinkEffect) -> float | None:
        """Return live foreign validity, or ``None`` when existing execute may retry."""
        if effect.state in {SinkEffectState.FINALIZED, SinkEffectState.PREPARED}:
            return None
        if effect.state not in {SinkEffectState.RESERVED, SinkEffectState.IN_FLIGHT}:
            raise LandscapeRecordError(f"sink effect wait cannot poll unsupported state {effect.state.value!r}")
        if effect.lease_owner == self._worker_id:
            return None
        if effect.state is SinkEffectState.RESERVED and effect.lease_owner is None:
            return None
        if (
            effect.lease_owner is None
            or not effect.lease_owner.strip()
            or len(effect.lease_owner) > 128
            or effect.lease_expires_at is None
            or effect.lease_heartbeat_at is None
        ):
            raise LandscapeRecordError("sink effect wait encountered incomplete lease authority")
        expires_at = self._utc(effect.lease_expires_at)
        heartbeat_at = self._utc(effect.lease_heartbeat_at)
        if expires_at <= heartbeat_at:
            raise LandscapeRecordError("sink effect wait encountered non-positive lease validity")
        remaining = (expires_at - self._clock.now_utc()).total_seconds()
        return None if remaining < 0.0 else remaining

    def _check_wait_interruptions(self) -> None:
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            if self._make_shutdown_error is None:
                raise InterruptedError("shutdown requested during sink-effect lease wait")
            raise self._make_shutdown_error()
        if self._check_coordination_latch is not None:
            self._check_coordination_latch()

    def _execute_effect(
        self,
        effect: SinkEffect,
        request: SinkEffectExecutionRequest,
        sink: _SinkEffectAdapter,
    ) -> SinkEffectFinalizationResult:
        self._require_predecessor(effect)
        ctx = self._context(effect)
        plan = self._prepare(effect, request, sink, ctx)
        effect = self._require_effect(effect.effect_id)
        if effect.state is SinkEffectState.FINALIZED:
            return self._load_finalized(effect.effect_id)

        if plan.descriptor_mode is SinkEffectDescriptorMode.NO_PUBLICATION:
            result = self._finalize_no_publication(effect, plan, request)
            self._fault(SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE)
            return result

        lease = self._lease(effect)
        heartbeat = _SinkEffectLeaseHeartbeat(effects=self._effects, claim=lease, ttl=self._lease_ttl)
        heartbeat.start()
        try:
            return self._execute_effect_under_lease(
                effect=effect,
                request=request,
                sink=sink,
                ctx=ctx,
                plan=plan,
                lease=lease,
                heartbeat=heartbeat,
            )
        finally:
            heartbeat.stop()

    def _execute_effect_under_lease(
        self,
        *,
        effect: SinkEffect,
        request: SinkEffectExecutionRequest,
        sink: _SinkEffectAdapter,
        ctx: RestrictedSinkEffectContext,
        plan: SinkEffectPlan,
        lease: SinkEffectLease,
        heartbeat: _SinkEffectLeaseHeartbeat,
    ) -> SinkEffectFinalizationResult:
        self._close_abandoned_attempts(
            effect.effect_id,
            actions=(SinkEffectAttemptAction.COMMIT, SinkEffectAttemptAction.RECONCILE),
            recovery_lease=lease,
        )
        if self._is_member_effect_adapter(sink, request.effect_input):
            return self._execute_member_effects(
                plan,
                request,
                cast("_MemberSinkEffectAdapter", sink),
                ctx,
                lease,
                heartbeat,
            )
        returned_commit = self._returned_attempt(
            effect.effect_id,
            action=SinkEffectAttemptAction.COMMIT,
            request_hash=self._commit_request_hash(plan),
        )
        if returned_commit is not None:
            commit_result, commit_attempt = returned_commit
            if not isinstance(commit_result, SinkEffectCommitResult):
                raise LandscapeRecordError("durable commit attempt decoded to the wrong result type")
            heartbeat.refresh_and_check()
            result = self._finalize(
                effect_id=effect.effect_id,
                request=request,
                lease=lease,
                descriptor=commit_result.descriptor,
                evidence=commit_result.evidence,
                accepted_ordinals=tuple(commit_result.accepted_ordinals),
                diverted_ordinals=tuple(commit_result.diverted_ordinals),
                attempt_id=commit_attempt.attempt_id,
                evidence_kind="returned",
                reconcile_kind=None,
            )
            self._fault(SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE)
            return result
        reconciliation, reconcile_attempt_id = self._reconcile(plan, sink, ctx, lease, heartbeat)
        if reconciliation.kind is SinkEffectReconcileKind.UNKNOWN:
            raise SinkEffectUnknownError(effect.effect_id)
        if reconciliation.kind is SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR:
            assert reconciliation.descriptor is not None
            if plan.descriptor_mode is SinkEffectDescriptorMode.RESULT_DERIVED:
                accepted = reconciliation.accepted_ordinals
                diverted = reconciliation.diverted_ordinals
                if accepted is None or diverted is None:
                    raise LandscapeRecordError("result-derived reconciliation requires an exact accepted/diverted ordinal partition")
                requested = {member.ordinal for member in request.finalization_members}
                if set(accepted) & set(diverted) or set(accepted) | set(diverted) != requested:
                    raise LandscapeRecordError(
                        "result-derived reconciliation ordinal partition must be disjoint and exactly cover requested members"
                    )
                accepted_ordinals = tuple(accepted)
                diverted_ordinals = tuple(diverted)
            else:
                if reconciliation.accepted_ordinals is not None or reconciliation.diverted_ordinals is not None:
                    raise LandscapeRecordError("precomputed reconciliation must not carry result-derived ordinals")
                accepted_ordinals, diverted_ordinals = self._prepared_partition(effect.effect_id, request)
            heartbeat.refresh_and_check()
            result = self._finalize(
                effect_id=effect.effect_id,
                request=request,
                lease=lease,
                descriptor=reconciliation.descriptor,
                evidence=reconciliation.evidence,
                accepted_ordinals=accepted_ordinals,
                diverted_ordinals=diverted_ordinals,
                attempt_id=reconcile_attempt_id,
                evidence_kind="reconciled",
                reconcile_kind=reconciliation.kind,
            )
            self._fault(SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE)
            return result

        if self._is_restaging_adapter(sink, request.effect_input):
            # Repair a spool-lost staged body from durable inputs before the
            # commit attempt opens; a failed re-derivation is a local
            # precondition failure and must not accrete attempt rows.
            cast("_RestagingSinkEffectAdapter", sink).restage_effect(
                plan,
                cast("SinkEffectPipelineMembersInput", request.effect_input),
                ctx,
            )
            heartbeat.refresh_and_check()
        commit, commit_attempt_id = self._commit(plan, sink, ctx, lease, heartbeat)
        self._fault(SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE)
        heartbeat.refresh_and_check()
        result = self._finalize(
            effect_id=effect.effect_id,
            request=request,
            lease=lease,
            descriptor=commit.descriptor,
            evidence=commit.evidence,
            accepted_ordinals=tuple(commit.accepted_ordinals),
            diverted_ordinals=tuple(commit.diverted_ordinals),
            attempt_id=commit_attempt_id,
            evidence_kind="returned",
            reconcile_kind=None,
        )
        self._fault(SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE)
        return result

    @staticmethod
    def _is_restaging_adapter(sink: object, effect_input: object) -> bool:
        return type(effect_input) is SinkEffectPipelineMembersInput and isinstance(sink, RestagingSinkEffectCapability)

    @staticmethod
    def _is_member_effect_adapter(sink: object, effect_input: object) -> bool:
        return type(effect_input) is SinkEffectPipelineMembersInput and isinstance(sink, MemberSinkEffectCapability)

    def _execute_member_effects(
        self,
        plan: SinkEffectPlan,
        request: SinkEffectExecutionRequest,
        sink: _MemberSinkEffectAdapter,
        ctx: RestrictedSinkEffectContext,
        lease: SinkEffectLease,
        heartbeat: _SinkEffectLeaseHeartbeat,
    ) -> SinkEffectFinalizationResult:
        if not isinstance(request.effect_input, SinkEffectPipelineMembersInput):  # pragma: no cover - guarded by caller
            raise TypeError("member effect execution requires pipeline members")
        effect_input = request.effect_input
        last_exact: tuple[SinkEffectCommitResult | SinkEffectReconcileResult, SinkEffectAttempt] | None = None
        for member in effect_input.members:
            durable = self._member_record(plan.effect_id, member.ordinal)
            if durable.member_state is SinkEffectState.FINALIZED:
                continue
            if durable.prepared_disposition == "diverted":
                # Plan-diverted member: its fate was durably decided when the
                # plan bound (with attribution), so no external commit or
                # reconcile call may ever be attempted for it.
                continue

            returned_commit = self._returned_attempt(
                plan.effect_id,
                action=SinkEffectAttemptAction.COMMIT,
                request_hash=self._member_commit_request_hash(plan, member),
                member_ordinal=member.ordinal,
            )
            if returned_commit is not None:
                commit_result, attempt = returned_commit
                if not isinstance(commit_result, SinkEffectCommitResult):
                    raise LandscapeRecordError("durable member commit decoded to the wrong result type")
                self._effects.complete_member_result(attempt.attempt_id, commit_result, lease=lease)
                last_exact = (commit_result, attempt)
                continue

            latest_lost_commit = self._latest_attempt(
                plan.effect_id,
                action=SinkEffectAttemptAction.COMMIT,
                state=SinkEffectAttemptState.RESPONSE_LOST,
                member_ordinal=member.ordinal,
            )
            returned_reconcile = self._returned_attempt(
                plan.effect_id,
                action=SinkEffectAttemptAction.RECONCILE,
                request_hash=self._member_reconcile_request_hash(plan, member),
                started_after=None if latest_lost_commit is None else latest_lost_commit.started_at,
                member_ordinal=member.ordinal,
            )
            if returned_reconcile is None:
                reconciliation, reconcile_attempt = self._reconcile_member(
                    plan,
                    member,
                    effect_input,
                    sink,
                    ctx,
                    lease,
                    heartbeat,
                )
            else:
                returned_result, reconcile_attempt = returned_reconcile
                if not isinstance(returned_result, SinkEffectReconcileResult):
                    raise LandscapeRecordError("durable member reconcile decoded to the wrong result type")
                reconciliation = returned_result
            self._effects.complete_member_result(reconcile_attempt.attempt_id, reconciliation, lease=lease)
            if reconciliation.kind is SinkEffectReconcileKind.UNKNOWN:
                raise SinkEffectUnknownError(plan.effect_id)
            if reconciliation.kind is SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR:
                last_exact = (reconciliation, reconcile_attempt)
                continue

            commit_result, commit_attempt = self._commit_member(
                plan,
                member,
                effect_input,
                sink,
                ctx,
                lease,
                heartbeat,
            )
            self._effects.complete_member_result(commit_attempt.attempt_id, commit_result, lease=lease)
            last_exact = (commit_result, commit_attempt)

        if any(
            record.member_state is not SinkEffectState.FINALIZED and record.prepared_disposition != "diverted"
            for record in self._effects.get_members(plan.effect_id)
        ):
            raise LandscapeRecordError("sink effect group cannot finalize before every member is exact")
        if last_exact is None:
            last_exact = self._latest_exact_member_result(plan.effect_id)
        result, attempt = last_exact
        self._fault(SinkEffectExecutionSeam.AFTER_RETURN_BEFORE_FINALIZE)
        heartbeat.refresh_and_check()
        # The final group partition comes from the durable per-member
        # dispositions, never from the last member result's group-wide claim:
        # a diverted earlier member would otherwise be finalized as accepted
        # (elspeth-d88f8eee34).
        accepted_ordinals, diverted_ordinals = self._prepared_partition(plan.effect_id, request)
        if isinstance(result, SinkEffectCommitResult):
            finalized = self._finalize(
                effect_id=plan.effect_id,
                request=request,
                lease=lease,
                descriptor=result.descriptor,
                evidence=result.evidence,
                accepted_ordinals=accepted_ordinals,
                diverted_ordinals=diverted_ordinals,
                attempt_id=attempt.attempt_id,
                evidence_kind="returned",
                reconcile_kind=None,
            )
        else:
            if result.kind is not SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR or result.descriptor is None:
                raise LandscapeRecordError("latest durable member result is not exact")
            finalized = self._finalize(
                effect_id=plan.effect_id,
                request=request,
                lease=lease,
                descriptor=result.descriptor,
                evidence=result.evidence,
                accepted_ordinals=accepted_ordinals,
                diverted_ordinals=diverted_ordinals,
                attempt_id=attempt.attempt_id,
                evidence_kind="reconciled",
                reconcile_kind=result.kind,
            )
        self._fault(SinkEffectExecutionSeam.AFTER_FINALIZE_BEFORE_RESPONSE)
        return finalized

    def _member_record(self, effect_id: str, ordinal: int) -> SinkEffectMemberRecord:
        record = next((item for item in self._effects.get_members(effect_id) if item.ordinal == ordinal), None)
        if not isinstance(record, SinkEffectMemberRecord):
            raise LandscapeRecordError(f"sink effect {effect_id} is missing member ordinal {ordinal}")
        return record

    def _reconcile_member(
        self,
        plan: SinkEffectPlan,
        member: SinkEffectMember,
        effect_input: SinkEffectPipelineMembersInput,
        sink: _MemberSinkEffectAdapter,
        ctx: RestrictedSinkEffectContext,
        lease: SinkEffectLease,
        heartbeat: _SinkEffectLeaseHeartbeat,
    ) -> tuple[SinkEffectReconcileResult, SinkEffectAttempt]:
        attempt = self._effects.begin_attempt(
            SinkEffectAttemptRequest(
                effect_id=plan.effect_id,
                member_ordinal=member.ordinal,
                generation=lease.generation,
                action=SinkEffectAttemptAction.RECONCILE,
                call_kind=sink.effect_call_type,
                request_hash=self._member_reconcile_request_hash(plan, member),
            )
        )
        started = time.monotonic()
        try:
            result = sink.reconcile_member_effect(plan, member, effect_input, ctx)
            heartbeat.refresh_and_check()
        except BaseException:
            self._effects.mark_response_lost(attempt.attempt_id)
            raise
        self._effects.record_attempt_result(
            SinkEffectAttemptResult(
                attempt_id=attempt.attempt_id,
                evidence=encode_sink_effect_returned_result(result),
                latency_ms=(time.monotonic() - started) * 1_000,
            )
        )
        return result, self._attempt_by_id(plan.effect_id, attempt.attempt_id)

    def _commit_member(
        self,
        plan: SinkEffectPlan,
        member: SinkEffectMember,
        effect_input: SinkEffectPipelineMembersInput,
        sink: _MemberSinkEffectAdapter,
        ctx: RestrictedSinkEffectContext,
        lease: SinkEffectLease,
        heartbeat: _SinkEffectLeaseHeartbeat,
    ) -> tuple[SinkEffectCommitResult, SinkEffectAttempt]:
        attempt = self._effects.begin_attempt(
            SinkEffectAttemptRequest(
                effect_id=plan.effect_id,
                member_ordinal=member.ordinal,
                generation=lease.generation,
                action=SinkEffectAttemptAction.COMMIT,
                call_kind=sink.effect_call_type,
                request_hash=self._member_commit_request_hash(plan, member),
            )
        )
        self._fault(SinkEffectExecutionSeam.BEFORE_EFFECT)
        started = time.monotonic()
        try:
            result = sink.commit_member_effect(plan, member, effect_input, ctx)
            self._fault(SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN)
            heartbeat.refresh_and_check()
        except BaseException:
            self._effects.mark_response_lost(attempt.attempt_id)
            raise
        self._effects.record_attempt_result(
            SinkEffectAttemptResult(
                attempt_id=attempt.attempt_id,
                evidence=encode_sink_effect_returned_result(result),
                latency_ms=(time.monotonic() - started) * 1_000,
            )
        )
        return result, self._attempt_by_id(plan.effect_id, attempt.attempt_id)

    def _attempt_by_id(self, effect_id: str, attempt_id: str) -> SinkEffectAttempt:
        attempt = next((item for item in self._effects.get_attempts(effect_id) if item.attempt_id == attempt_id), None)
        if attempt is None:
            raise LandscapeRecordError(f"sink effect attempt {attempt_id} disappeared")
        return attempt

    def _latest_exact_member_result(
        self,
        effect_id: str,
    ) -> tuple[SinkEffectCommitResult | SinkEffectReconcileResult, SinkEffectAttempt]:
        for attempt in reversed(self._effects.get_attempts(effect_id)):
            if (
                attempt.member_ordinal is None
                or attempt.state is not SinkEffectAttemptState.RETURNED
                or attempt.action not in {SinkEffectAttemptAction.COMMIT, SinkEffectAttemptAction.RECONCILE}
                or attempt.evidence_json is None
            ):
                continue
            result = decode_sink_effect_returned_result(attempt.action, attempt.evidence_json)
            if isinstance(result, SinkEffectCommitResult) or (
                isinstance(result, SinkEffectReconcileResult) and result.kind is SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR
            ):
                return result, attempt
        raise LandscapeRecordError("finalized member set is missing an exact returned attempt")

    @staticmethod
    def _member_reconcile_request_hash(plan: SinkEffectPlan, member: SinkEffectMember) -> str:
        return stable_hash(
            {
                "effect_id": plan.effect_id,
                "member_effect_id": member.member_effect_id,
                "member_ordinal": member.ordinal,
                "plan_hash": plan.plan_hash,
                "schema": "sink-member-effect-reconcile-request-v1",
            }
        )

    @staticmethod
    def _member_commit_request_hash(plan: SinkEffectPlan, member: SinkEffectMember) -> str:
        return stable_hash(
            {
                "effect_id": plan.effect_id,
                "member_effect_id": member.member_effect_id,
                "member_ordinal": member.ordinal,
                "plan_hash": plan.plan_hash,
                "schema": "sink-member-effect-commit-request-v1",
            }
        )

    def _request_for_effect(
        self,
        effect: SinkEffect,
        request: SinkEffectExecutionRequest,
    ) -> SinkEffectExecutionRequest:
        if not isinstance(request.effect_input, SinkEffectPipelineMembersInput):
            return request
        durable_members = self._effects.get_members(effect.effect_id)
        caller_by_token = {member.token_id: member for member in request.effect_input.members}
        finalization_by_token = {
            member.token_id: finalization
            for member, finalization in zip(request.effect_input.members, request.finalization_members, strict=True)
        }
        current_members: list[SinkEffectMember] = []
        current_finalization: list[SinkEffectFinalizationMember] = []
        for durable in durable_members:
            if durable.token_id not in caller_by_token or durable.token_id not in finalization_by_token:
                raise LandscapeRecordError(f"open sink effect {effect.effect_id} cannot be recovered from a partial caller partition")
            caller = caller_by_token[durable.token_id]
            finalization = finalization_by_token[durable.token_id]
            current_members.append(
                replace(
                    caller,
                    ordinal=durable.ordinal,
                    member_effect_id=durable.member_effect_id,
                )
            )
            current_finalization.append(replace(finalization, ordinal=durable.ordinal))
        members = tuple(current_members)
        return SinkEffectExecutionRequest(
            reservation=request.reservation,
            effect_input=SinkEffectPipelineMembersInput(
                members=members,
                target_snapshot_members=self._target_snapshot_members(
                    effect,
                    members,
                    known_members=caller_by_token,
                ),
            ),
            finalization_members=tuple(current_finalization),
        )

    def _target_snapshot_members(
        self,
        effect: SinkEffect,
        current_members: tuple[SinkEffectMember, ...],
        *,
        known_members: Mapping[str, SinkEffectMember],
    ) -> tuple[SinkEffectMember, ...]:
        chain: list[SinkEffect] = []
        predecessor_id = effect.predecessor_effect_id
        seen = {effect.effect_id}
        while predecessor_id is not None:
            if predecessor_id in seen or len(chain) >= 256:
                raise LandscapeRecordError("sink effect predecessor chain is cyclic or exceeds 256 effects")
            seen.add(predecessor_id)
            predecessor = self._require_effect(predecessor_id)
            if predecessor.state is not SinkEffectState.FINALIZED:
                raise SinkEffectPredecessorPending(f"sink effect {effect.effect_id} is waiting for predecessor {predecessor.effect_id}")
            chain.append(predecessor)
            predecessor_id = predecessor.predecessor_effect_id
        chain.reverse()

        snapshot: list[SinkEffectMember] = []
        for predecessor in chain:
            for durable in self._effects.get_members(predecessor.effect_id):
                # Diverted members never reached the target: replaying them in
                # a cumulative successor would republish rejected rows as if
                # they were immutable predecessor content.
                if durable.prepared_disposition == "diverted":
                    continue
                if durable.prepared_disposition != "accepted":
                    raise LandscapeRecordError(
                        f"finalized sink effect predecessor {predecessor.effect_id} member ordinal "
                        f"{durable.ordinal} is missing its accepted/diverted disposition"
                    )
                if durable.token_id in known_members:
                    known = known_members[durable.token_id]
                    member = replace(known, member_effect_id=durable.member_effect_id)
                else:
                    member = self._hydrate_member(durable)
                snapshot.append(replace(member, ordinal=len(snapshot)))
        current_start = len(snapshot)
        snapshot.extend(replace(member, ordinal=current_start + ordinal) for ordinal, member in enumerate(current_members))
        return tuple(snapshot)

    def _persist_pipeline_member_payloads(self, effect_input: object) -> None:
        if not isinstance(effect_input, SinkEffectPipelineMembersInput):
            return
        store = self._factory.payload_store
        if store is None:
            return
        for member in effect_input.members:
            content = canonical_json(deep_thaw(member.row)).encode("utf-8")
            content_hash = store.store(content)
            if content_hash != member.payload_hash:
                raise LandscapeRecordError("sink effect payload store returned a divergent member content hash")

    def _hydrate_member(self, durable: object) -> SinkEffectMember:
        from elspeth.contracts.audit import SinkEffectMemberRecord

        if not isinstance(durable, SinkEffectMemberRecord):
            raise TypeError("durable must be SinkEffectMemberRecord")
        store = self._factory.payload_store
        if store is None:
            raise LandscapeRecordError("replacing sink effect recovery requires the configured payload store")
        content = store.retrieve(durable.payload_hash)
        try:
            row = json.loads(content)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LandscapeRecordError("sink effect member payload is not canonical JSON") from exc
        if type(row) is not dict or canonical_json(row).encode("utf-8") != content:
            raise LandscapeRecordError("sink effect member payload is not an exact canonical object")
        return SinkEffectMember(
            ordinal=durable.ordinal,
            token_id=durable.token_id,
            row_id=durable.row_id,
            ingest_sequence=durable.ingest_sequence,
            lineage_json=durable.lineage_json,
            lineage_hash=durable.lineage_hash,
            payload_hash=durable.payload_hash,
            row=row,
            member_effect_id=durable.member_effect_id,
            primary_effect_id=durable.primary_effect_id,
        )

    def _prepare(
        self,
        effect: SinkEffect,
        request: SinkEffectExecutionRequest,
        sink: _SinkEffectAdapter,
        ctx: RestrictedSinkEffectContext,
    ) -> SinkEffectPlan:
        if effect.state is not SinkEffectState.RESERVED:
            return self._load_plan(effect)
        # Preparation runs side-effecting adapter code, so ownership must be
        # durably claimed before any staging mutation. Refuse politely while
        # another worker's claim is live; close abandoned inspect intents
        # while their generation still matches the row; then claim (which
        # bumps the generation and fences the eventual plan bind).
        if (
            effect.lease_owner is not None
            and effect.lease_owner != self._worker_id
            and effect.lease_expires_at is not None
            and self._utc(effect.lease_expires_at) >= self._clock.now_utc()
        ):
            raise SinkEffectLeaseHeld(f"sink effect {effect.effect_id} preparation is claimed by another worker")
        self._close_abandoned_attempts(effect.effect_id, actions=(SinkEffectAttemptAction.INSPECT,))
        try:
            claim = self._effects.claim_preparation(effect.effect_id, owner=self._worker_id, ttl=self._lease_ttl)
        except LandscapeRecordError as exc:
            current = self._require_effect(effect.effect_id)
            if current.state in {SinkEffectState.PREPARED, SinkEffectState.IN_FLIGHT, SinkEffectState.FINALIZED}:
                return self._load_plan(current)
            if self._held_lease_remaining_seconds(current) is not None:
                raise SinkEffectLeaseHeld(f"sink effect {effect.effect_id} preparation advanced concurrently under another worker") from exc
            raise
        effect = self._require_effect(effect.effect_id)
        if effect.state is not SinkEffectState.RESERVED:  # pragma: no cover - claim holds the effect reserved
            return self._load_plan(effect)
        heartbeat = _SinkEffectLeaseHeartbeat(effects=self._effects, claim=claim, ttl=self._lease_ttl)
        heartbeat.start()
        try:
            predecessor = self._predecessor_descriptor(effect)
            inspection_request = SinkEffectInspectionRequest(
                effect_id=effect.effect_id,
                target=effect.target_json,
                predecessor_descriptor=predecessor,
                input_kind=request.reservation.input_kind,
            )
            request_hash = stable_hash(
                {
                    "effect_id": effect.effect_id,
                    "predecessor": None if predecessor is None else predecessor.content_hash,
                    "schema": "sink-effect-inspection-request-v1",
                    "target": effect.target_json,
                }
            )
            returned_inspection = self._returned_attempt(
                effect.effect_id,
                action=SinkEffectAttemptAction.INSPECT,
                request_hash=request_hash,
            )
            if returned_inspection is not None:
                inspection_result, _attempt = returned_inspection
                if not isinstance(inspection_result, SinkEffectInspection):
                    raise LandscapeRecordError("durable inspect attempt decoded to the wrong result type")
                inspection = inspection_result
            else:
                inspection_attempt = self._effects.begin_attempt(
                    SinkEffectAttemptRequest(
                        effect_id=effect.effect_id,
                        member_ordinal=None,
                        generation=effect.generation,
                        action=SinkEffectAttemptAction.INSPECT,
                        call_kind=sink.effect_call_type,
                        request_hash=request_hash,
                    )
                )
                started = time.monotonic()
                try:
                    inspection = sink.inspect_effect(inspection_request, ctx)
                except BaseException:
                    self._effects.mark_response_lost(inspection_attempt.attempt_id)
                    raise
                self._effects.record_attempt_result(
                    SinkEffectAttemptResult(
                        attempt_id=inspection_attempt.attempt_id,
                        evidence=encode_sink_effect_returned_result(inspection),
                        latency_ms=(time.monotonic() - started) * 1_000,
                    )
                )
            heartbeat.refresh_and_check()
            prepare_request = SinkEffectPrepareRequest(
                effect_id=effect.effect_id,
                effect_input=request.effect_input,  # type: ignore[arg-type]
                inspection=inspection,
            )
            plan = sink.prepare_effect(prepare_request, ctx)
            prepare_request.validate_plan(plan)
        finally:
            heartbeat.stop()
        heartbeat.refresh_and_check()
        self._effects.complete_plan(effect.effect_id, plan, claim=claim)
        return plan

    @staticmethod
    def _load_plan(effect: SinkEffect) -> SinkEffectPlan:
        if effect.plan_json is None:
            raise LandscapeRecordError("prepared sink effect is missing its durable plan")
        try:
            payload = json.loads(effect.plan_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LandscapeRecordError("prepared sink effect has invalid durable plan JSON") from exc
        if type(payload) is not dict:
            raise LandscapeRecordError("prepared sink effect durable plan must be an object")
        try:
            descriptor_payload = payload["expected_descriptor"]
            descriptor: ArtifactDescriptor | None
            if descriptor_payload is None:
                descriptor = None
            elif type(descriptor_payload) is dict:
                descriptor = ArtifactDescriptor(
                    artifact_type=descriptor_payload["artifact_type"],
                    path_or_uri=descriptor_payload["path_or_uri"],
                    content_hash=descriptor_payload["content_hash"],
                    size_bytes=descriptor_payload["size_bytes"],
                    metadata=descriptor_payload["metadata"],
                )
            else:
                raise LandscapeRecordError("prepared sink effect durable descriptor is invalid")
            return SinkEffectPlan(
                effect_id=payload["effect_id"],
                protocol_version=payload["protocol_version"],
                input_kind=SinkEffectInputKind(payload["input_kind"]),
                descriptor_mode=SinkEffectDescriptorMode(payload["descriptor_mode"]),
                inspection_mode=SinkEffectInspectionMode(payload["inspection_mode"]),
                target=payload["target"],
                plan_hash=payload["plan_hash"],
                payload_hash=payload["payload_hash"],
                expected_descriptor=descriptor,
                safe_evidence=payload["safe_evidence"],
            )
        except LandscapeRecordError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise LandscapeRecordError("prepared sink effect durable plan is incomplete or divergent") from exc

    def _lease(self, effect: SinkEffect) -> SinkEffectLease:
        try:
            if effect.state is SinkEffectState.PREPARED:
                return self._effects.acquire_lease(effect.effect_id, owner=self._worker_id, ttl=self._lease_ttl)
            if effect.state is not SinkEffectState.IN_FLIGHT or effect.lease_expires_at is None:
                raise LandscapeRecordError(f"sink effect cannot execute from state {effect.state.value!r}")
            expires_at = self._utc(effect.lease_expires_at)
            if effect.lease_owner == self._worker_id and expires_at >= self._clock.now_utc():
                return self._effects.acquire_lease(effect.effect_id, owner=self._worker_id, ttl=self._lease_ttl)
            if expires_at < self._clock.now_utc():
                return self._effects.takeover_expired(effect.effect_id, owner=self._worker_id, ttl=self._lease_ttl)
            raise SinkEffectLeaseHeld(f"sink effect {effect.effect_id} has a live lease owned by another worker")
        except SinkEffectLeaseHeld:
            raise
        except LandscapeRecordError as exc:
            current = self._require_effect(effect.effect_id)
            if current.state is SinkEffectState.FINALIZED or self._held_lease_remaining_seconds(current) is not None:
                raise SinkEffectLeaseHeld(
                    f"sink effect {effect.effect_id} execution authority advanced concurrently under another worker"
                ) from exc
            raise

    def _reconcile(
        self,
        plan: SinkEffectPlan,
        sink: _SinkEffectAdapter,
        ctx: RestrictedSinkEffectContext,
        lease: SinkEffectLease,
        heartbeat: _SinkEffectLeaseHeartbeat,
    ) -> tuple[SinkEffectReconcileResult, str]:
        request_hash = self._reconcile_request_hash(plan)
        latest_lost_commit = self._latest_attempt(
            plan.effect_id,
            action=SinkEffectAttemptAction.COMMIT,
            state=SinkEffectAttemptState.RESPONSE_LOST,
        )
        returned = self._returned_attempt(
            plan.effect_id,
            action=SinkEffectAttemptAction.RECONCILE,
            request_hash=request_hash,
            started_after=None if latest_lost_commit is None else latest_lost_commit.started_at,
        )
        if returned is not None:
            result, attempt = returned
            if not isinstance(result, SinkEffectReconcileResult):
                raise LandscapeRecordError("durable reconcile attempt decoded to the wrong result type")
            return result, attempt.attempt_id
        attempt = self._effects.begin_attempt(
            SinkEffectAttemptRequest(
                effect_id=plan.effect_id,
                member_ordinal=None,
                generation=lease.generation,
                action=SinkEffectAttemptAction.RECONCILE,
                call_kind=sink.effect_call_type,
                request_hash=request_hash,
            )
        )
        started = time.monotonic()
        try:
            result = sink.reconcile_effect(plan, ctx)
            heartbeat.refresh_and_check()
        except BaseException:
            self._effects.mark_response_lost(attempt.attempt_id)
            raise
        self._effects.record_attempt_result(
            SinkEffectAttemptResult(
                attempt_id=attempt.attempt_id,
                evidence=encode_sink_effect_returned_result(result),
                latency_ms=(time.monotonic() - started) * 1_000,
            )
        )
        return result, attempt.attempt_id

    def _commit(
        self,
        plan: SinkEffectPlan,
        sink: _SinkEffectAdapter,
        ctx: RestrictedSinkEffectContext,
        lease: SinkEffectLease,
        heartbeat: _SinkEffectLeaseHeartbeat,
    ) -> tuple[SinkEffectCommitResult, str]:
        request_hash = self._commit_request_hash(plan)
        attempt = self._effects.begin_attempt(
            SinkEffectAttemptRequest(
                effect_id=plan.effect_id,
                member_ordinal=None,
                generation=lease.generation,
                action=SinkEffectAttemptAction.COMMIT,
                call_kind=sink.effect_call_type,
                request_hash=request_hash,
            )
        )
        self._fault(SinkEffectExecutionSeam.BEFORE_EFFECT)
        started = time.monotonic()
        try:
            result = sink.commit_effect(plan, ctx)
            self._fault(SinkEffectExecutionSeam.AFTER_EFFECT_BEFORE_RETURN)
            heartbeat.refresh_and_check()
        except BaseException:
            self._effects.mark_response_lost(attempt.attempt_id)
            raise
        self._effects.record_attempt_result(
            SinkEffectAttemptResult(
                attempt_id=attempt.attempt_id,
                evidence=encode_sink_effect_returned_result(result),
                latency_ms=(time.monotonic() - started) * 1_000,
            )
        )
        return result, attempt.attempt_id

    @staticmethod
    def _reconcile_request_hash(plan: SinkEffectPlan) -> str:
        return stable_hash({"effect_id": plan.effect_id, "plan_hash": plan.plan_hash, "schema": "sink-effect-reconcile-request-v1"})

    @staticmethod
    def _commit_request_hash(plan: SinkEffectPlan) -> str:
        return stable_hash({"effect_id": plan.effect_id, "plan_hash": plan.plan_hash, "schema": "sink-effect-commit-request-v1"})

    def _close_abandoned_attempts(
        self,
        effect_id: str,
        *,
        actions: tuple[SinkEffectAttemptAction, ...],
        recovery_lease: SinkEffectLease | None = None,
    ) -> None:
        for attempt in self._effects.get_attempts(effect_id):
            if attempt.action in actions and attempt.state is SinkEffectAttemptState.INTENT:
                self._effects.mark_response_lost(attempt.attempt_id, recovery_lease=recovery_lease)

    def _returned_attempt(
        self,
        effect_id: str,
        *,
        action: SinkEffectAttemptAction,
        request_hash: str,
        started_after: datetime | None = None,
        member_ordinal: int | None = None,
    ) -> tuple[SinkEffectReturnedResult, SinkEffectAttempt] | None:
        winners = [
            attempt
            for attempt in self._effects.get_attempts(effect_id)
            if attempt.action is action
            and attempt.member_ordinal == member_ordinal
            and attempt.request_hash == request_hash
            and attempt.state is SinkEffectAttemptState.RETURNED
            and (started_after is None or self._utc(attempt.started_at) > self._utc(started_after))
        ]
        if not winners:
            return None
        winner = winners[-1]
        if winner.evidence_json is None:
            raise LandscapeRecordError("returned sink effect attempt is missing its durable result")
        result = decode_sink_effect_returned_result(action, winner.evidence_json)
        for attempt in winners[:-1]:
            if attempt.evidence_json is None or decode_sink_effect_returned_result(action, attempt.evidence_json) != result:
                raise LandscapeRecordError("same sink effect request has divergent durable returned results")
        return result, winner

    def _latest_attempt(
        self,
        effect_id: str,
        *,
        action: SinkEffectAttemptAction,
        state: SinkEffectAttemptState,
        member_ordinal: int | None = None,
    ) -> SinkEffectAttempt | None:
        attempts = [
            attempt
            for attempt in self._effects.get_attempts(effect_id)
            if attempt.action is action and attempt.state is state and attempt.member_ordinal == member_ordinal
        ]
        return attempts[-1] if attempts else None

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def _finalize(
        self,
        *,
        effect_id: str,
        request: SinkEffectExecutionRequest,
        lease: SinkEffectLease,
        descriptor: ArtifactDescriptor,
        evidence: Mapping[str, object],
        accepted_ordinals: tuple[int, ...],
        diverted_ordinals: tuple[int, ...],
        attempt_id: str,
        evidence_kind: str,
        reconcile_kind: SinkEffectReconcileKind | None,
    ) -> SinkEffectFinalizationResult:
        by_ordinal = {member.ordinal: member for member in request.finalization_members}
        try:
            members = tuple(by_ordinal[ordinal] for ordinal in accepted_ordinals)
        except KeyError as exc:
            raise LandscapeRecordError(f"sink result accepted unknown member ordinal {exc.args[0]}") from exc
        return self._effects.finalize(
            SinkEffectFinalizeRequest(
                effect_id=effect_id,
                lease_owner=lease.owner,
                generation=lease.generation,
                descriptor=descriptor,
                publication_performed=True,
                publication_evidence_kind=evidence_kind,  # type: ignore[arg-type]
                accepted_ordinals=accepted_ordinals,
                diverted_ordinals=diverted_ordinals,
                evidence=evidence,
                members=members,
                attempt_id=attempt_id,
                reconcile_kind=reconcile_kind,
            )
        )

    def _finalize_no_publication(
        self,
        effect: SinkEffect,
        plan: SinkEffectPlan,
        request: SinkEffectExecutionRequest,
    ) -> SinkEffectFinalizationResult:
        if plan.expected_descriptor is None:
            raise LandscapeRecordError("no-publication effect requires its precomputed descriptor")
        try:
            publication_kind = plan.safe_evidence["publication_kind"]
        except KeyError as exc:
            raise LandscapeRecordError("no-publication effect is missing publication evidence") from exc
        if publication_kind in {"inherited", "reaffirmed"}:
            # A reaffirmed effect never touched the remote target either (it
            # proved the existing object's content already matches), so it
            # is audited and walked back through predecessor chains exactly
            # like an inherited no-op.
            evidence_kind: Literal["inherited", "virtual"] = "inherited"
        elif publication_kind == "virtual":
            evidence_kind = "virtual"
        else:
            raise LandscapeRecordError("no-publication effect requires inherited, virtual, or reaffirmed publication evidence")
        accepted, diverted = self._prepared_partition(effect.effect_id, request)
        by_ordinal = {member.ordinal: member for member in request.finalization_members}
        return self._effects.finalize(
            SinkEffectFinalizeRequest(
                effect_id=effect.effect_id,
                lease_owner=None,
                generation=effect.generation,
                descriptor=plan.expected_descriptor,
                publication_performed=False,
                publication_evidence_kind=evidence_kind,
                accepted_ordinals=accepted,
                diverted_ordinals=diverted,
                evidence=plan.safe_evidence,
                members=tuple(by_ordinal[ordinal] for ordinal in accepted),
            )
        )

    def _prepared_partition(
        self,
        effect_id: str,
        request: SinkEffectExecutionRequest,
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        durable = self._effects.get_members(effect_id)
        requested = {member.ordinal for member in request.finalization_members}
        if {member.ordinal for member in durable} != requested:
            raise LandscapeRecordError("prepared sink effect partition does not cover the requested members")
        accepted = tuple(member.ordinal for member in durable if member.prepared_disposition == "accepted")
        diverted = tuple(member.ordinal for member in durable if member.prepared_disposition == "diverted")
        if tuple(sorted((*accepted, *diverted))) != tuple(sorted(requested)):
            raise LandscapeRecordError("prepared sink effect members do not carry a complete disposition partition")
        return accepted, diverted

    def _context(self, effect: SinkEffect) -> RestrictedSinkEffectContext:
        run = self._factory.run_lifecycle.get_run(effect.run_id)
        if run is None:
            raise LandscapeRecordError(f"sink effect run {effect.run_id!r} does not exist")
        operation = next(
            (item for item in self._factory.execution.get_operations_for_run(effect.run_id) if item.sink_effect_id == effect.effect_id),
            None,
        )
        if operation is None:
            raise LandscapeRecordError("sink effect stable operation does not exist")
        return RestrictedSinkEffectContext(
            run_id=effect.run_id,
            run_started_at=run.started_at,
            operation_id=operation.operation_id,
            sink_node_id=effect.sink_node_id,
        )

    def _require_predecessor(self, effect: SinkEffect) -> None:
        if effect.predecessor_effect_id is None:
            return
        predecessor = self._effects.get_effect(effect.predecessor_effect_id)
        if predecessor is None or predecessor.state is not SinkEffectState.FINALIZED:
            raise SinkEffectPredecessorPending(f"sink effect {effect.effect_id} is waiting for predecessor {effect.predecessor_effect_id}")

    def _predecessor_descriptor(self, effect: SinkEffect) -> ArtifactDescriptor | None:
        if effect.predecessor_effect_id is None:
            return None
        artifacts_by_effect = {
            item.sink_effect_id: item
            for item in self._factory.execution.get_artifacts(effect.run_id, sink_node_id=effect.sink_node_id)
            if item.sink_effect_id is not None
        }
        predecessor_id: str | None = effect.predecessor_effect_id
        seen = {effect.effect_id}
        while predecessor_id is not None:
            if predecessor_id in seen or len(seen) > 256:
                raise LandscapeRecordError("sink effect predecessor chain is cyclic or exceeds 256 effects")
            seen.add(predecessor_id)
            predecessor = self._effects.get_effect(predecessor_id)
            if predecessor is None:
                raise LandscapeRecordError("sink effect predecessor disappeared")
            if predecessor.state is not SinkEffectState.FINALIZED:
                raise SinkEffectPredecessorPending(f"sink effect {effect.effect_id} is waiting for predecessor {predecessor.effect_id}")
            if predecessor.effect_id not in artifacts_by_effect:
                raise LandscapeRecordError("finalized sink effect predecessor is missing its artifact")
            artifact = artifacts_by_effect[predecessor.effect_id]
            if not artifact.publication_performed:
                plan = self._load_plan(predecessor)
                if plan.safe_evidence.get("publication_kind") != "reaffirmed":
                    # A virtual or inherited predecessor did not establish new
                    # remote identity: walk back to the most recent real
                    # publication in the stream (elspeth-fac5260c6a).
                    predecessor_id = predecessor.predecessor_effect_id
                    continue
                # Reaffirmation performed no write, but its durable plan proved
                # that these exact descriptor bytes already occupied the
                # target. Preserve that verified descriptor as successor
                # lineage; inspection will still fence against later tampering.
            if artifact.artifact_type not in {"file", "database", "webhook"}:
                raise LandscapeRecordError("finalized sink effect predecessor has an invalid artifact type")
            return ArtifactDescriptor(
                artifact_type=cast(Literal["file", "database", "webhook"], artifact.artifact_type),
                path_or_uri=artifact.path_or_uri,
                content_hash=artifact.content_hash,
                size_bytes=artifact.size_bytes,
            )
        return None

    def _load_finalized(self, effect_id: str) -> SinkEffectFinalizationResult:
        effect = self._require_effect(effect_id)
        if effect.state is not SinkEffectState.FINALIZED:
            raise LandscapeRecordError("reservation classified a non-finalized effect as finalized")
        artifacts = self._factory.execution.get_artifacts(effect.run_id, sink_node_id=effect.sink_node_id)
        artifact = next((item for item in artifacts if item.sink_effect_id == effect.effect_id), None)
        if artifact is None:
            raise LandscapeRecordError("finalized sink effect is missing its artifact")
        return SinkEffectFinalizationResult(effect=effect, artifact=artifact, state_ids=(), outcome_ids=())

    def _require_effect(self, effect_id: str) -> SinkEffect:
        effect = self._effects.get_effect(effect_id)
        if effect is None:
            raise LandscapeRecordError(f"sink effect {effect_id!r} does not exist")
        return effect

    def _fault(self, seam: SinkEffectExecutionSeam) -> None:
        if self._fault_hook is not None:
            self._fault_hook(seam)


__all__ = [
    "SinkEffectCoordinator",
    "SinkEffectExecutionRequest",
    "SinkEffectExecutionSeam",
    "SinkEffectInjectedFault",
    "SinkEffectLeaseHeld",
    "SinkEffectPredecessorPending",
    "SinkEffectUnknownError",
]
