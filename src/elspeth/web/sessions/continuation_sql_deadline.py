"""Timing DATA for one live V2 SQL owner; never a mutation capability.

The bridge installs this owner inside its actual SQL worker. The canonical
transaction owner uses it before advisory/row locks and before each statement.
An in-flight statement already has a finite cap when SIGTERM arrives.
"""

from __future__ import annotations

import math
import threading
import time

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, ContinuationAdmissionPolicy
from elspeth.web.sessions.composer_drain import (
    COMPOSER_DRAIN_DISPOSITION_SECONDS,
    COMPOSER_DRAIN_FINISH_RESERVE_SECONDS,
    ComposerDrainDeadline,
    ComposerDrainSignal,
)
from elspeth.web.sessions.composer_operations import ComposerOperationRunning


class ContinuationSQLDeadlineOwner:
    def __init__(
        self,
        running: ComposerOperationRunning,
        *,
        policy: ContinuationAdmissionPolicy,
        drain_signal: ComposerDrainSignal,
        drain_seconds: float,
        hard_expires_at: float,
    ) -> None:
        if (
            type(running) is not ComposerOperationRunning
            or running.claim.segment_sequence is None
            or running.claim.ownership_generation is None
            or type(policy) is not ContinuationAdmissionPolicy
            or type(drain_signal) is not ComposerDrainSignal
            or type(drain_seconds) not in (float, int)
            or not math.isfinite(drain_seconds)
            or drain_seconds <= COMPOSER_DRAIN_FINISH_RESERVE_SECONDS
            or type(hard_expires_at) not in (float, int)
            or not math.isfinite(hard_expires_at)
        ):
            raise AuditIntegrityError("V2 SQL timing lost its actual owner or finite retained bounds")
        self.running, self.policy, self.drain_signal = running, policy, drain_signal
        self.drain_seconds, self.hard_expires_at = float(drain_seconds), float(hard_expires_at)
        # Reserve the process drain disposition margin even when a statement
        # was installed before the signal. The retained action reserve is a
        # different budget. This is a server statement/lock bound;
        # connection checkout, transport and commit still require real proofs.
        self.drain_finish_reserve_seconds = COMPOSER_DRAIN_FINISH_RESERVE_SECONDS
        self.statement_cap = min(policy.actions.session_sql.physical, self.drain_seconds - self.drain_finish_reserve_seconds)
        self._action_expires_at = self.hard_expires_at
        self._settlement_expires_at = self.hard_expires_at
        self._settling = False
        self._lock = threading.RLock()

    def validate_context(self, context: SessionOperationContext, *, operation_id: str | None, attempt: int | None) -> None:
        if (
            context is not self.running.session_operation_context
            or operation_id != self.running.claim.operation_id
            or attempt != self.running.claim.attempt
        ):
            raise AuditIntegrityError("SQL timing crossed its actual physical/logical owner")

    def begin_action(self, action: ContinuationActionPolicy, *, started_at: float) -> None:
        if (
            type(action) is not ContinuationActionPolicy
            or action
            not in (
                self.policy.actions.primary,
                self.policy.actions.advisor,
                self.policy.actions.title,
                self.policy.actions.read,
                self.policy.actions.session_sql,
                self.policy.actions.blob,
                self.policy.actions.external,
            )
            or type(started_at) not in (float, int)
            or not math.isfinite(started_at)
        ):
            raise AuditIntegrityError("SQL timing substituted an unretained action or clock")
        with self._lock:
            self._action_expires_at = min(self.hard_expires_at, started_at + action.total)
            self._settlement_expires_at = min(self.hard_expires_at, self._action_expires_at + self.policy.segment.reserve)
            self._settling = False

    def begin_settlement(self) -> None:
        # Settlement consumes the reserve attached to the original action,
        # clipped by hard/drain bounds. Repeated calls cannot extend it or
        # change the business-action clock. This grants no write authority.
        with self._lock:
            self._settling = True

    def remaining_action(self, *, monotonic_now: float) -> float:
        """Observe the original action/hard/drain bound for a nested action."""
        if type(monotonic_now) is not float or not math.isfinite(monotonic_now):
            raise ValueError("Continuation action timing requires a finite monotonic observation")
        with self._lock:
            expires_at = self._action_expires_at
        signal_start = self.drain_signal.started_at
        if signal_start is not None:
            expires_at = min(expires_at, signal_start + self.drain_seconds)
        return expires_at - monotonic_now

    def remaining_dispatch(self, *, monotonic_now: float) -> float:
        """Observe business time without spending the drain finish margin."""
        remaining = self.remaining_action(monotonic_now=monotonic_now)
        signal_start = self.drain_signal.started_at
        if signal_start is not None:
            remaining = min(remaining, signal_start + self.drain_seconds - self.drain_finish_reserve_seconds - monotonic_now)
        return remaining

    def remaining_statement(self, *, session_id: str, deadline: ComposerDrainDeadline | None = None, lifecycle: bool = False) -> float:
        if session_id != str(self.running.claim.session_id):
            raise AuditIntegrityError("SQL timing cannot cover another session")
        now = time.monotonic()
        with self._lock:
            disposition = lifecycle or self._settling
            expires_at = self.hard_expires_at if lifecycle else self._settlement_expires_at if self._settling else self._action_expires_at
        signal_start = self.drain_signal.started_at
        statement_cap = self.statement_cap
        if signal_start is not None:
            if disposition:
                expires_at = min(expires_at, signal_start + self.drain_seconds)
                statement_cap = min(statement_cap, COMPOSER_DRAIN_DISPOSITION_SECONDS / 2)
            else:
                expires_at = min(expires_at, signal_start + self.drain_seconds - self.drain_finish_reserve_seconds)
        if deadline is not None:
            expires_at = min(expires_at, deadline.expires_at)
        remaining = min(statement_cap, expires_at - now)
        if remaining <= 0:
            raise TimeoutError("Continuation SQL exhausted its original action/drain deadline")
        return remaining
