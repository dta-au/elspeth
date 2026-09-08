"""Multi-worker run-coordination contracts (epoch 21, ADR-030).

Value objects threaded through the leader/follower coordination protocol
(design: docs/architecture/design-notes/option-c-multi-worker-coordination-design-2026-06-11.md):

- :func:`mint_worker_id` — the §A.1 single-use worker identity. Minted at
  registration (``begin_run`` / ``acquire_run_leadership`` / ``join``),
  doubles as the scheduler ``lease_owner`` string. Role is a registry
  attribute, never parsed from the string.
- :class:`CoordinationToken` — the fencing token carried by value into every
  leader-fenced verb; never re-read mid-run. ``leader_epoch`` is THE fence:
  a takeover bumps it, instantly refusing the deposed leader everywhere.
- :class:`WorkerMembershipToken` — the membership token carried by value into
  every membership-fenced verb (ADR-030 D4's second fence: the holder is an
  ``active`` ``run_workers`` row). Established by ``admit_follower``; the
  leader derives its own through ``CoordinationToken.membership``.
- :class:`LeaderInfo` — read-only seat snapshot (``live_leader``); the
  slice-4 entry-guard precision upgrade consumes it.
- :class:`CoordinationSnapshot` — returned by an admitted ``worker_heartbeat`` so
  followers learn of seat handover on their existing cadence (§A.3;
  consumed by the slice-4 heartbeat thread).
- :class:`RegisteredWorker` — forensic registry row surfaced by the §B.4
  BUSY-takeover diagnostic (``WriteLockHeldError``): pid is the one forensic
  column with a functional consumer (the operator's SIGKILL target).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal
from uuid import uuid4

__all__ = [
    "DEFAULT_ITEM_STALL_BUDGET_SECONDS",
    "DEFAULT_RUN_HEARTBEAT_SECONDS",
    "DEFAULT_RUN_LIVENESS_WINDOW_SECONDS",
    "CoordinationSnapshot",
    "CoordinationToken",
    "LeaderInfo",
    "RegisteredWorker",
    "WorkerMembershipLost",
    "WorkerMembershipToken",
    "mint_worker_id",
]

# Run-level heartbeat cadence (design §A.3 :132: run_heartbeat_seconds = 15).
# The slice-4 dedicated heartbeat thread sleeps this long between beats.
# Nominal sizing: window >= 4 x (beat + busy_timeout) = 4 x (15 + 5) = 80 s.
DEFAULT_RUN_HEARTBEAT_SECONDS: Final[float] = 15.0

# Nominal run-level liveness window (design §A.3), sized as four times the sum
# of the heartbeat interval and busy-timeout term. This is not a bound on a
# whole transaction. ADR-047 starts the window at the final post-lock sample;
# the remaining transaction tail consumes some of it before commit.
# The dedicated heartbeat is independent of LLM-call duration. Leader-fenced
# writes also renew the seat; ordinary member fences do not renew liveness.
DEFAULT_RUN_LIVENESS_WINDOW_SECONDS: Final[float] = 80.0

# Item-level stall budget (design §A.5 :140): a registry-LIVE worker holding
# an item far past its lease emits ``worker_stalled`` and the item is rotated.
# Default = 2x scheduler_lease_seconds (the caller threads the configured
# lease length; the constant here is the MULTIPLIER). Expressed as an absolute
# seconds constant (not a multiplier) so it can be imported without the
# scheduler module — callers that want a different budget pass it explicitly.
# Equals 2x DEFAULT scheduler lease (300 s) = 600 s by default.
DEFAULT_ITEM_STALL_BUDGET_SECONDS: Final[float] = 600.0


def mint_worker_id(run_id: str) -> str:
    """Mint a fresh single-use worker identity (design §A.1).

    ``worker:{run_id}:{uuid4().hex}`` — minted at registration and used as
    the scheduler ``lease_owner``. Identities are single-use: a ``departed``
    or ``evicted`` registry row never returns to ``active``; a returning
    process mints a fresh identity and re-admits.
    """
    return f"worker:{run_id}:{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class CoordinationToken:
    """Leader fencing token: ``(run_id, worker_id, leader_epoch)``.

    Minted by ``register_run_leader_on`` (epoch 1, in ``begin_run``'s
    transaction) or ``acquire_run_leadership`` (takeover CAS, epoch+1).
    Threaded by value into every leader-fenced verb; the verify-and-extend
    fence (``verify_and_extend_leader_fence``) CAS-matches all three fields
    against ``run_coordination`` as the first statement of the verb's
    transaction.
    """

    run_id: str
    worker_id: str
    leader_epoch: int

    @property
    def membership(self) -> WorkerMembershipToken:
        """The leader's own membership: the weaker authority derived from the stronger.

        Every seat mint (``register_run_leader_on``, the takeover CAS, the
        export-seat CAS) inserts the leader's ``run_workers`` row in the same
        transaction as the seat, so a leader IS a member by construction and
        this derivation never invents a row. The reverse derivation does not
        exist: a :class:`WorkerMembershipToken` cannot produce a leader token.
        """
        return WorkerMembershipToken(run_id=self.run_id, worker_id=self.worker_id)


@dataclass(frozen=True, slots=True)
class WorkerMembershipToken:
    """Membership fencing token: ``(run_id, worker_id)`` — ADR-030 D4's second fence.

    Proves the holder is an *active* member of the run, nothing more: no
    epoch, no seat. Established by ``admit_follower`` (the follower's own
    ``run_workers`` row) and derived by the leader from its
    :class:`CoordinationToken` (``CoordinationToken.membership``). Threaded by
    value into every membership-fenced verb; ``fenced_member_transaction``
    verify-UPDATEs the ``run_workers`` row for ``(run_id, worker_id,
    status='active')`` as the first statement of the verb's transaction and
    refuses with ``RunMembershipLostError`` on rowcount 0.

    Nominal (ADR-032): a distinct owned class, no Protocol, no union with and
    no inheritance from :class:`CoordinationToken`. A leader-scoped verb that
    accepted this type would be unprovable, which is exactly the fail-open
    class the two-type split exists to close (ADR-048 amendment 2026-09-07).
    """

    run_id: str
    worker_id: str


@dataclass(frozen=True, slots=True)
class WorkerMembershipLost:
    """A refused heartbeat: membership is lost and no seat state was observed.

    This outcome needs no database read after the membership fence refuses.
    An absent registration is audit corruption and raises instead.
    """

    member_token: WorkerMembershipToken


@dataclass(frozen=True, slots=True)
class LeaderInfo:
    """Read-only view of a run's leader seat (``live_leader``).

    ``seat_live`` is the §C.1 liveness predicate evaluated at the caller's
    ``now``: ``leader_heartbeat_expires_at >= now``. A dead seat
    (``seat_live=False``) is the admissible-takeover signal the slice-4
    entry guard consumes.
    """

    run_id: str
    leader_worker_id: str
    leader_epoch: int
    leader_heartbeat_expires_at: datetime
    seat_live: bool


@dataclass(frozen=True, slots=True)
class CoordinationSnapshot:
    """Seat state observed atomically by ``worker_heartbeat`` (§A.3).

    A successful heartbeat returns ``worker_active=True``. A refused heartbeat
    returns :class:`WorkerMembershipLost` instead: no seat read is needed to
    report that membership ended. ``leader_worker_id`` is None for a vacant
    seat. A leader-mode process observing a snapshot whose leader is not itself treats that as
    fatal (deposed even if its registry row was not yet evicted).
    """

    leader_worker_id: str | None
    leader_epoch: int
    seat_live: bool
    worker_active: Literal[True]
    # Role of THIS worker (the one that called worker_heartbeat). Defaults to
    # "leader" for backward-compat with slice-4 tests that construct snapshots
    # directly. The heartbeat thread uses this to gate the deposed-latch: a
    # follower seeing a foreign leader_worker_id is NORMAL, not deposed.
    worker_role: str = "leader"

    def __post_init__(self) -> None:
        if self.worker_active is not True:
            raise ValueError("Inactive membership requires WorkerMembershipLost, not a coordination snapshot")


@dataclass(frozen=True, slots=True)
class RegisteredWorker:
    """Forensic ``run_workers`` registry row (§B.4 BUSY-takeover diagnostic)."""

    worker_id: str
    role: str
    status: str
    pid: int | None
    hostname: str | None
