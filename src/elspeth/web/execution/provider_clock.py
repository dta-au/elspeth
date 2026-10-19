"""Run outcome timestamps anchored to committed admission and elapsed time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from time import monotonic_ns

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.quota_authority import ProviderAttempt


@dataclass(slots=True)
class _AdmittedClock:
    started_at: datetime
    admitted_ns: int
    finished_at: datetime | None = None


class RunProviderAttemptClock:
    """Keep physical outcome recording independent of Sessions availability.

    Each successful admission binds the database UTC timestamp to its local
    monotonic receipt time. The first terminal outcome fixes its timestamp;
    subsequent classification uses that exact value. Recovery reads the
    persisted Landscape timestamp instead of reconstructing this binding.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._attempts: dict[str, _AdmittedClock] = {}

    def bind(self, attempt: ProviderAttempt) -> None:
        """Bind only a committed receipt, before physical provider dispatch."""
        if type(attempt) is not ProviderAttempt:
            raise TypeError("Run provider clock requires an exact ProviderAttempt")
        if type(attempt.started_at) is not datetime or attempt.started_at.utcoffset() is None:
            raise AuditIntegrityError("Run provider admission has no authoritative aware timestamp")
        admitted_ns = monotonic_ns()
        with self._lock:
            if attempt.attempt_id in self._attempts:
                raise AuditIntegrityError("Run provider attempt clock was bound more than once")
            self._attempts[attempt.attempt_id] = _AdmittedClock(attempt.started_at.astimezone(UTC), admitted_ns)

    def timestamp_for_outcome(self, attempt_id: str) -> datetime:
        """Stamp spent work without a Sessions query or a new dispatch guard."""
        with self._lock:
            if attempt_id not in self._attempts:
                raise AuditIntegrityError("Run provider outcome has no admitted clock binding")
            clock = self._attempts[attempt_id]
            if clock.finished_at is None:
                elapsed_ns = monotonic_ns() - clock.admitted_ns
                if elapsed_ns < 0:
                    raise AuditIntegrityError("Run provider monotonic clock moved backwards")
                clock.finished_at = clock.started_at + timedelta(microseconds=elapsed_ns // 1_000)
            return clock.finished_at
