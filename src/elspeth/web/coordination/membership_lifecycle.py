"""Process-lifetime driver for web-instance membership.

The lifespan calls exactly three things, in order: ``start`` after the
startup sweeps (register, then keep the lease renewed), ``begin_drain`` as
the first act of shutdown (readiness fails at once, the row says
``draining`` while executor work drains), and ``stop`` after the executor
has shut down (the row says ``stopped`` with an already-expired lease, so
peers may take over immediately instead of waiting out the lease).

A single-process deployment (SQLite) has no peers and no membership rows;
it still owns the draining signal, so readiness behaves identically on
both database modes.
"""

from __future__ import annotations

import asyncio
import threading
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import final

import structlog
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from elspeth.web.async_workers import run_sync_in_worker
from elspeth.web.coordination.membership_authority import (
    RepositoryWebInstanceMembershipAuthority,
    WebInstanceIdentity,
    WebInstanceMembershipLost,
)

_HEARTBEAT_MAX_CONSECUTIVE_FAILURES = 5
"""Consecutive transient-failure bound for lease renewal.

One or two ``OperationalError`` heartbeats are contention and retry
cleanly; this many in a row means the process can no longer prove it is
alive. The heartbeat task then re-raises, its done callback cancels the
owning lifespan task, and the process exits so the supervisor restarts
it — the same escalation the periodic orphan sweeper uses.
"""


def heartbeat_interval_seconds(lease_seconds: int) -> int:
    """Renew three times per lease so one missed beat never expires it."""
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
        raise ValueError("lease_seconds must be an exact integer from 1 through 3600")
    return max(1, lease_seconds // 3)


class MembershipShutdownOutcome(StrEnum):
    """What a shutdown step's row write did; the local draining signal is set regardless."""

    RECORDED = "recorded"
    FAILED = "failed"
    NO_MEMBERSHIP = "no_membership"


class WebInstanceMembership(ABC):
    """What the lifespan and the readiness gate need from membership."""

    __slots__ = ("_draining",)

    def __init__(self) -> None:
        self._draining = threading.Event()

    @property
    def draining(self) -> threading.Event:
        """Set once ``begin_drain`` has been called; readiness fails while set."""
        return self._draining

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def begin_drain(self) -> MembershipShutdownOutcome: ...

    @abstractmethod
    async def stop(self) -> MembershipShutdownOutcome: ...


@final
class SingleProcessWebInstanceMembership(WebInstanceMembership):
    """No peers, no rows: only the draining signal exists."""

    __slots__ = ()

    async def start(self) -> None:
        return None

    async def begin_drain(self) -> MembershipShutdownOutcome:
        self._draining.set()
        return MembershipShutdownOutcome.NO_MEMBERSHIP

    async def stop(self) -> MembershipShutdownOutcome:
        return MembershipShutdownOutcome.NO_MEMBERSHIP


@final
class RegisteredWebInstanceMembership(WebInstanceMembership):
    """Registers one process, renews its lease, and records drain and stop."""

    __slots__ = ("_authority", "_heartbeat_task", "_identity", "_interval_seconds", "_lease_seconds", "_log")

    def __init__(
        self,
        authority: RepositoryWebInstanceMembershipAuthority,
        identity: WebInstanceIdentity,
        *,
        lease_seconds: int,
        interval_seconds: int | None = None,
    ) -> None:
        if type(authority) is not RepositoryWebInstanceMembershipAuthority:
            raise TypeError("authority must be a RepositoryWebInstanceMembershipAuthority")
        if type(identity) is not WebInstanceIdentity:
            raise TypeError("identity must be a WebInstanceIdentity")
        super().__init__()
        self._authority = authority
        self._identity = identity
        self._lease_seconds = lease_seconds
        resolved_interval = heartbeat_interval_seconds(lease_seconds) if interval_seconds is None else interval_seconds
        if type(resolved_interval) is not int or not 1 <= resolved_interval <= lease_seconds:
            raise ValueError("interval_seconds must be an exact integer from 1 through lease_seconds")
        self._interval_seconds = resolved_interval
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._log = structlog.get_logger("web.membership")

    @property
    def identity(self) -> WebInstanceIdentity:
        return self._identity

    async def start(self) -> None:
        """Register, then renew the lease until ``stop``; failure to register fails boot."""
        if self._heartbeat_task is not None:
            raise RuntimeError("web instance membership already started")
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("web instance membership must start inside an asyncio task")
        await run_sync_in_worker(self._authority.register, self._identity, lease_seconds=self._lease_seconds)
        task = asyncio.create_task(self._heartbeat_loop())

        def _stop_owner_on_failure(completed: asyncio.Task[None]) -> None:
            if not completed.cancelled() and completed.exception() is not None:
                owner.cancel()

        task.add_done_callback(_stop_owner_on_failure)
        self._heartbeat_task = task

    async def _heartbeat_loop(self) -> None:
        consecutive_failures = 0
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await run_sync_in_worker(self._authority.heartbeat, self._identity.instance_id, lease_seconds=self._lease_seconds)
                consecutive_failures = 0
            except OperationalError as exc:
                # Only OperationalError is retried, and only a bounded number
                # of times: it is the one failure class that models transient
                # contention. WebInstanceMembershipLost and every other error
                # propagate at once — the row is gone or the fault is
                # standing, and renewing cannot fix either. exc_info is
                # deliberately omitted: SQLAlchemy cause chains carry the
                # database URL.
                consecutive_failures += 1
                if consecutive_failures >= _HEARTBEAT_MAX_CONSECUTIVE_FAILURES:
                    self._log.error(
                        "web_instance_heartbeat_escalating",
                        exc_class=type(exc).__name__,
                        consecutive_failures=consecutive_failures,
                    )
                    raise
                self._log.error(
                    "web_instance_heartbeat_failed",
                    exc_class=type(exc).__name__,
                    consecutive_failures=consecutive_failures,
                )

    async def begin_drain(self) -> MembershipShutdownOutcome:
        """Fail readiness locally first; the row write's outcome is returned, never hidden."""
        self._draining.set()
        try:
            await run_sync_in_worker(self._authority.begin_drain, self._identity.instance_id, lease_seconds=self._lease_seconds)
        except (SQLAlchemyError, WebInstanceMembershipLost) as exc:
            # Shutdown proceeds whether or not the database can be reached or
            # the row still exists: a drain write that fails leaves the row
            # active with a live lease, which peers treat as a dead owner once
            # it expires. Any other failure class is a defect and propagates.
            self._log.error("web_instance_drain_failed", exc_class=type(exc).__name__)
            return MembershipShutdownOutcome.FAILED
        return MembershipShutdownOutcome.RECORDED

    async def stop(self) -> MembershipShutdownOutcome:
        """Cancel renewal and record ``stopped`` with an expired lease.

        A heartbeat task that had already died re-raises its stored failure
        here, after the stop write has been attempted, so the fault that
        cancelled the lifespan surfaces at shutdown instead of being lost.
        """
        task = self._heartbeat_task
        self._heartbeat_task = None
        heartbeat_failure: BaseException | None = None
        if task is not None:
            task.cancel()
            # Wait for the task to settle without letting its own
            # cancellation surface here.
            await asyncio.wait({task})
            if not task.cancelled():
                heartbeat_failure = task.exception()
        try:
            await run_sync_in_worker(self._authority.stop, self._identity.instance_id)
        except (SQLAlchemyError, WebInstanceMembershipLost) as exc:
            # A stop write that fails leaves the row active or draining with a
            # live lease; peers take over once it expires instead of at once.
            # Any other failure class propagates.
            self._log.error("web_instance_stop_failed", exc_class=type(exc).__name__)
            if heartbeat_failure is not None:
                raise heartbeat_failure from exc
            return MembershipShutdownOutcome.FAILED
        if heartbeat_failure is not None:
            raise heartbeat_failure
        return MembershipShutdownOutcome.RECORDED
