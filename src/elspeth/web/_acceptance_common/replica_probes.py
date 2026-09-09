"""The replicas > 1 probes: one driver, one decision table, a closed ``mechanism`` vocabulary.

The four probes (plan §7) are what an acceptance run must prove before a
deployment target may claim "replicas > 1". They are written once here and
driven against a :class:`ReplicaController` port (address a replica, partition
or stop the owner, restore it) and an :class:`EvidenceObserver` port (read the
database facts a probe scores). Each provider supplies its two ports; the
decisions never change per provider.

**Overclaiming is a schema violation, not a convention.** Every result names
the ``mechanism`` that produced its evidence from a closed set, each probe may
only claim the mechanisms the tree actually has for it, and P4b cannot be
constructed with any outcome but ``cannot_pass``:

- **P1** concurrent guided operations on one session from two replicas end in
  a fence conflict, never a double dispatch — ``session_operation_fence``.
- **P2** concurrent run starts end in one run and one 409 —
  ``session_operation_fence_execute``. The result has no field for a
  ``run_start_permits`` row because nothing in the tree writes one; the
  durable permit saga is a recorded follow-up, not a claim.
- **P3** a survivor takes a dead owner's lease over only after it expires —
  ``role_revocation_lease_expiry`` (the primary primitive: the owner's
  database role is revoked with ``ALTER ROLE … NOLOGIN`` and its backends
  self-terminated, so it can neither heartbeat nor release) or
  ``revision_deactivate_lease_expiry`` (the secondary, a grace-0 platform
  stop). If the owner's membership row reads ``stopped`` or ``draining`` the
  owner *did* reach its lifecycle release path, so the result **downgrades**
  to ``graceful_stop``: a takeover happened, but not from a dead owner. Until
  the membership authority ships (6b-2) no row exists and the probe is
  reported ``unreachable``, stated as such.
- **P4a** database-backed state and blob bytes written on one replica are
  visible from the other within one poll interval — ``postgresql_and_nfs``.
- **P4b** the live progress stream and the WebSocket ticket are owner-affine
  today; the probe records ``owner_affine`` and the production mitigation and
  **cannot** pass.

**Timezones.** Every datetime a decision compares must be timezone-aware; the
driver converts to UTC before comparing and refuses a naive value with
``AcceptanceCheckError('probe_timestamp')``. The decisions are therefore
indifferent to the database session timezone by construction.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, Literal, TypedDict

from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.trust_boundary import trust_boundary

from .errors import AcceptanceCheckError, AcceptanceInputError
from .http_client import AcceptanceHttpClient

Probe = Literal["P1", "P2", "P3", "P4a", "P4b"]
ProbeOutcome = Literal["pass", "fail", "cannot_pass", "unreachable"]
Mechanism = Literal[
    "session_operation_fence",
    "session_operation_fence_execute",
    "role_revocation_lease_expiry",
    "revision_deactivate_lease_expiry",
    "graceful_stop",
    "postgresql_and_nfs",
    "owner_affine",
]

PROBES: Final[frozenset[str]] = frozenset({"P1", "P2", "P3", "P4a", "P4b"})
PROBE_OUTCOMES: Final[frozenset[str]] = frozenset({"pass", "fail", "cannot_pass", "unreachable"})
MECHANISMS: Final[frozenset[str]] = frozenset(
    {
        "session_operation_fence",
        "session_operation_fence_execute",
        "role_revocation_lease_expiry",
        "revision_deactivate_lease_expiry",
        "graceful_stop",
        "postgresql_and_nfs",
        "owner_affine",
    }
)
PROBE_MECHANISMS: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "P1": frozenset({"session_operation_fence"}),
        "P2": frozenset({"session_operation_fence_execute"}),
        "P3": frozenset({"role_revocation_lease_expiry", "revision_deactivate_lease_expiry", "graceful_stop"}),
        "P4a": frozenset({"postgresql_and_nfs"}),
        "P4b": frozenset({"owner_affine"}),
    }
)
"""The mechanisms each probe may claim; anything else is a schema violation."""

SESSION_OPERATION_CONFLICT_DETAIL: Final = "Session operation is already active"
"""The 409 body ``web/app.py`` returns for a fence conflict (``_session_operation_conflict_handler``)."""

ORPHAN_CANCELLATION_REASON_PREFIX: Final = "Orphaned by periodic cleanup"
"""How the survivor's periodic sweep words the run's cancellation reason.

``web/app.py`` ``_periodic_orphan_cleanup`` writes ``"Orphaned by periodic
cleanup — no active executor thread"`` (optionally followed by the Landscape
reconciliation-pending suffix) into the run's error column; the startup sweep
writes ``"Orphaned by server restart …"`` instead, which P3 must not accept —
a restart is not a surviving replica taking over.
"""

TERMINAL_RUN_STATUSES: Final[frozenset[str]] = frozenset({"completed", "completed_with_failures", "failed", "empty", "cancelled"})

DEFAULT_TRIALS: Final = 20
DEFAULT_MAX_DISPATCH_SPREAD_MS: Final = 5.0


class ProbeReceiptDetails(TypedDict):
    """The closed detail set a probe result contributes to a receipt."""

    probe: str
    outcome: str
    mechanism: str
    reasons: list[str]
    evidence: dict[str, object]


@dataclass(frozen=True)
class ProbeResult:
    """One probe's verdict. Construction enforces the closed vocabularies and the P4b cannot-pass rule."""

    probe: Probe
    outcome: ProbeOutcome
    mechanism: Mechanism
    reasons: tuple[str, ...] = ()
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.probe not in PROBES:
            raise ValueError("probe must be one of P1, P2, P3, P4a, P4b")
        if self.outcome not in PROBE_OUTCOMES:
            raise ValueError("outcome must be pass, fail, cannot_pass or unreachable")
        if self.mechanism not in PROBE_MECHANISMS[self.probe]:
            raise ValueError(f"{self.probe} cannot claim mechanism {self.mechanism!r}; allowed: {sorted(PROBE_MECHANISMS[self.probe])}")
        if self.probe == "P4b" and self.outcome != "cannot_pass":
            raise ValueError("P4b records an owner-affine surface and cannot pass")
        if (self.outcome == "pass") != (not self.reasons):
            raise ValueError("a passing result carries no reasons; every other outcome names at least one")
        if any(type(reason) is not str or not reason or len(reason) > 128 for reason in self.reasons):
            raise ValueError("reasons must be non-empty bounded strings")
        if any(type(key) is not str or not key for key in self.evidence):
            raise ValueError("evidence keys must be non-empty strings")
        freeze_fields(self, "evidence")

    def to_receipt_details(self) -> ProbeReceiptDetails:
        return {
            "probe": self.probe,
            "outcome": self.outcome,
            "mechanism": self.mechanism,
            "reasons": list(self.reasons),
            "evidence": dict(self.evidence),
        }


# --------------------------------------------------------------------------- observations


@dataclass(frozen=True)
class ReplicaResponse:
    """One response as a probe saw it: which replica it was addressed to, and which answered."""

    addressed_to: str
    status: int
    instance_id: str | None
    detail: str | None = None
    run_id: str | None = None

    @property
    def succeeded(self) -> bool:
        return 200 <= self.status < 300

    @property
    def refused_by_fence(self) -> bool:
        return self.status == 409 and self.detail == SESSION_OPERATION_CONFLICT_DETAIL


@dataclass(frozen=True)
class FenceConflictTrial:
    """P1: one concurrent pair of guided operations and the fence facts around it."""

    responses: tuple[ReplicaResponse, ReplicaResponse]
    fence_epoch_before: int
    fence_epoch_after: int
    fence_owner_after: str | None
    guided_operation_rows: int
    dispatch_spread_ms: float


@dataclass(frozen=True)
class RunStartTrial:
    """P2: one concurrent pair of run starts and the run rows they produced.

    There is deliberately no ``run_start_permit`` field: nothing in the tree
    writes ``run_start_permits``, so a receipt has no way to assert one.
    """

    responses: tuple[ReplicaResponse, ReplicaResponse]
    runs_row_ids: tuple[str, ...]
    landscape_run_ids: tuple[str, ...]
    dispatch_spread_ms: float


@dataclass(frozen=True)
class MembershipRow:
    """The owner's ``web_instances`` row as the observer read it."""

    instance_id: str
    state: str
    lease_expires_at: datetime


@dataclass(frozen=True)
class LeaseTakeoverObservation:
    """P3: everything the driver saw around one partitioned owner."""

    primitive: Literal["role_revocation", "revision_deactivate"]
    owner_instance_id: str
    survivor_instance_id: str
    owner_row: MembershipRow | None
    before_expiry: ReplicaResponse
    after_expiry: ReplicaResponse
    takeover_observed_at: datetime
    cancelled_run_reason: str | None
    fence_owner_after: str | None
    duplicate_sink_effects: int


@dataclass(frozen=True)
class CrossReplicaProgressObservation:
    """P4a: state written through the owner, read through the other replica."""

    owner_instance_id: str
    reader_instance_id: str
    poll_interval_seconds: float
    status_visible_after_seconds: float
    outputs_visible_after_seconds: float
    messages_visible_after_seconds: float
    blob_sha256_via_owner: str
    blob_sha256_via_reader: str
    terminal_status_on_reader: str | None


# --------------------------------------------------------------------------- decisions


def _aware_utc(value: datetime, *, label: str) -> datetime:
    """Refuse a naive datetime; normalise an aware one to UTC before any comparison."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise AcceptanceCheckError("probe_timestamp", missing=(label,))
    return value.astimezone(UTC)


def _distinct_instances(responses: tuple[ReplicaResponse, ReplicaResponse]) -> bool:
    first, second = responses
    return first.instance_id is not None and second.instance_id is not None and first.instance_id != second.instance_id


def decide_fence_conflict(
    trials: Sequence[FenceConflictTrial],
    *,
    required_trials: int = DEFAULT_TRIALS,
    max_dispatch_spread_ms: float = DEFAULT_MAX_DISPATCH_SPREAD_MS,
) -> ProbeResult:
    """P1: every trial is exactly one success and one fence refusal from two distinct replicas."""

    reasons: list[str] = []
    if len(trials) != required_trials:
        reasons.append(f"trial_count:{len(trials)}!={required_trials}")
    winners: set[str] = set()
    for index, trial in enumerate(trials):
        successes = [response for response in trial.responses if response.succeeded]
        refusals = [response for response in trial.responses if response.refused_by_fence]
        if len(successes) != 1 or len(refusals) != 1:
            reasons.append(f"trial[{index}]:not_one_success_and_one_fence_refusal")
            continue
        if not _distinct_instances(trial.responses):
            reasons.append(f"trial[{index}]:instances_not_distinct")
        winner = successes[0].instance_id
        if winner is not None:
            winners.add(winner)
        if trial.fence_epoch_after != trial.fence_epoch_before + 1:
            reasons.append(f"trial[{index}]:fence_epoch_not_advanced_by_one")
        if trial.fence_owner_after != winner:
            reasons.append(f"trial[{index}]:fence_owner_is_not_the_winner")
        if trial.guided_operation_rows != 1:
            reasons.append(f"trial[{index}]:guided_operation_rows:{trial.guided_operation_rows}!=1")
        if trial.dispatch_spread_ms > max_dispatch_spread_ms:
            reasons.append(f"trial[{index}]:dispatch_spread_ms:{trial.dispatch_spread_ms:.3f}>{max_dispatch_spread_ms}")
    if trials and len(winners) < 2:
        reasons.append("winners_not_distinct_across_run")
    return ProbeResult(
        probe="P1",
        outcome="pass" if not reasons else "fail",
        mechanism="session_operation_fence",
        reasons=tuple(reasons),
        evidence={"trials": len(trials), "distinct_winners": len(winners)},
    )


def decide_run_start(
    trials: Sequence[RunStartTrial],
    *,
    required_trials: int = DEFAULT_TRIALS,
    max_dispatch_spread_ms: float = DEFAULT_MAX_DISPATCH_SPREAD_MS,
) -> ProbeResult:
    """P2: every trial is one accepted run and one fence refusal, and exactly one run row exists."""

    reasons: list[str] = []
    if len(trials) != required_trials:
        reasons.append(f"trial_count:{len(trials)}!={required_trials}")
    for index, trial in enumerate(trials):
        accepted = [response for response in trial.responses if response.status == 202 and response.run_id is not None]
        refusals = [response for response in trial.responses if response.refused_by_fence]
        if len(accepted) != 1 or len(refusals) != 1:
            reasons.append(f"trial[{index}]:not_one_accepted_run_and_one_fence_refusal")
            continue
        if not _distinct_instances(trial.responses):
            reasons.append(f"trial[{index}]:instances_not_distinct")
        run_id = accepted[0].run_id
        if trial.runs_row_ids != (run_id,):
            reasons.append(f"trial[{index}]:runs_rows:{len(trial.runs_row_ids)}!=1_or_not_the_accepted_run")
        if len(trial.landscape_run_ids) != 1:
            reasons.append(f"trial[{index}]:landscape_runs:{len(trial.landscape_run_ids)}!=1")
        if trial.dispatch_spread_ms > max_dispatch_spread_ms:
            reasons.append(f"trial[{index}]:dispatch_spread_ms:{trial.dispatch_spread_ms:.3f}>{max_dispatch_spread_ms}")
    return ProbeResult(
        probe="P2",
        outcome="pass" if not reasons else "fail",
        mechanism="session_operation_fence_execute",
        reasons=tuple(reasons),
        evidence={"trials": len(trials)},
    )


def decide_lease_takeover(observation: LeaseTakeoverObservation) -> ProbeResult:
    """P3: the survivor takes over only after the dead owner's lease expired, and only once.

    A ``stopped`` or ``draining`` owner row means the owner reached its own
    release path: the mechanism is downgraded to ``graceful_stop`` and the
    result says so. No row at all means the membership authority has not
    shipped: ``unreachable``, never ``fail``.
    """

    primary: Mechanism = (
        "role_revocation_lease_expiry" if observation.primitive == "role_revocation" else "revision_deactivate_lease_expiry"
    )
    if observation.owner_row is None:
        return ProbeResult(
            probe="P3",
            outcome="unreachable",
            mechanism=primary,
            reasons=("web_instances_has_no_row_for_the_owner:membership_authority_not_landed",),
            evidence={"owner_instance_id": observation.owner_instance_id, "primitive": observation.primitive},
        )
    row = observation.owner_row
    lease_expires_at = _aware_utc(row.lease_expires_at, label="owner_row.lease_expires_at")
    takeover_observed_at = _aware_utc(observation.takeover_observed_at, label="takeover_observed_at")
    mechanism: Mechanism = primary
    reasons: list[str] = []
    if row.instance_id != observation.owner_instance_id:
        reasons.append("owner_row_is_not_the_owner")
    if row.state in {"stopped", "draining"}:
        mechanism = "graceful_stop"
    elif row.state != "active":
        reasons.append(f"owner_row_state_unknown:{row.state}")
    if not observation.before_expiry.refused_by_fence:
        reasons.append("survivor_not_refused_before_lease_expiry")
    if takeover_observed_at <= lease_expires_at:
        reasons.append("takeover_observed_before_lease_expiry")
    if not observation.after_expiry.succeeded:
        reasons.append("survivor_not_admitted_after_lease_expiry")
    if observation.after_expiry.instance_id != observation.survivor_instance_id:
        reasons.append("after_expiry_response_not_from_survivor")
    if observation.owner_instance_id == observation.survivor_instance_id:
        reasons.append("owner_and_survivor_are_the_same_instance")
    if observation.cancelled_run_reason is None or not observation.cancelled_run_reason.startswith(ORPHAN_CANCELLATION_REASON_PREFIX):
        reasons.append("owner_run_not_cancelled_with_the_orphan_reason")
    if observation.fence_owner_after != observation.survivor_instance_id:
        reasons.append("fence_owner_did_not_move_to_the_survivor")
    if observation.duplicate_sink_effects != 0:
        reasons.append(f"duplicate_sink_effects:{observation.duplicate_sink_effects}")
    return ProbeResult(
        probe="P3",
        outcome="pass" if not reasons else "fail",
        mechanism=mechanism,
        reasons=tuple(reasons),
        evidence={
            "primitive": observation.primitive,
            "owner_row_state": row.state,
            "lease_expires_at": lease_expires_at.isoformat().replace("+00:00", "Z"),
            "takeover_observed_at": takeover_observed_at.isoformat().replace("+00:00", "Z"),
        },
    )


def decide_cross_replica_progress(observation: CrossReplicaProgressObservation) -> ProbeResult:
    """P4a: database-backed state and blob bytes are visible from the non-owner within one poll interval."""

    reasons: list[str] = []
    if observation.owner_instance_id == observation.reader_instance_id:
        reasons.append("owner_and_reader_are_the_same_instance")
    if observation.poll_interval_seconds <= 0:
        reasons.append("poll_interval_not_positive")
    for label, seconds in (
        ("status", observation.status_visible_after_seconds),
        ("outputs", observation.outputs_visible_after_seconds),
        ("messages", observation.messages_visible_after_seconds),
    ):
        if seconds < 0 or seconds > observation.poll_interval_seconds:
            reasons.append(f"{label}_not_visible_within_one_poll_interval")
    if observation.blob_sha256_via_owner != observation.blob_sha256_via_reader:
        reasons.append("blob_bytes_differ_across_replicas")
    if observation.terminal_status_on_reader not in TERMINAL_RUN_STATUSES:
        reasons.append("terminal_status_not_observed_on_reader")
    return ProbeResult(
        probe="P4a",
        outcome="pass" if not reasons else "fail",
        mechanism="postgresql_and_nfs",
        reasons=tuple(reasons),
        evidence={"poll_interval_seconds": observation.poll_interval_seconds},
    )


def record_owner_affine_progress(*, mitigation: Literal["single_revision_sticky_sessions"]) -> ProbeResult:
    """P4b: the live progress stream and WebSocket ticket are owner-affine; recorded, never passed."""

    return ProbeResult(
        probe="P4b",
        outcome="cannot_pass",
        mechanism="owner_affine",
        reasons=("progress_stream_and_websocket_ticket_are_process_local",),
        evidence={"mitigation": mitigation},
    )


# --------------------------------------------------------------------------- ports


@dataclass(frozen=True)
class ReplicaAddress:
    name: str
    origin: str


class ReplicaController(ABC):
    """What a platform must offer the driver: name its replicas, and kill or restore an owner."""

    @abstractmethod
    def replicas(self) -> tuple[ReplicaAddress, ReplicaAddress]:
        """The two addressable replicas (label URLs on Container Apps, pinned targets on ECS)."""

    @abstractmethod
    def partition_owner(self, replica: str) -> None:
        """Sever the owner from every database authority without letting it release (role revocation)."""

    @abstractmethod
    def stop_owner(self, replica: str) -> None:
        """Platform stop with no grace period (the secondary primitive)."""

    @abstractmethod
    def restore_owner(self, replica: str) -> None:
        """Undo the partition or stop."""


class EvidenceObserver(ABC):
    """The database facts a probe scores, read through the provider's own channel."""

    @abstractmethod
    def fence_epoch(self, session_id: str) -> int: ...

    @abstractmethod
    def fence_owner(self, session_id: str) -> str | None: ...

    @abstractmethod
    def guided_operation_rows(self, session_id: str, *, since_epoch: int) -> int: ...

    @abstractmethod
    def runs_row_ids(self, session_id: str) -> tuple[str, ...]: ...

    @abstractmethod
    def landscape_run_ids(self, session_id: str) -> tuple[str, ...]: ...

    @abstractmethod
    def membership_row(self, instance_id: str) -> MembershipRow | None: ...


@dataclass(frozen=True)
class ProbeRequest:
    method: str
    path: str
    json_body: object


@trust_boundary(
    tier=3,
    source="the JSON body and X-Elspeth-Instance header of one response from a deployed replica under probe",
    source_param="body",
    suppresses=("R1", "R5"),
    invariant=(
        "returns an owned ReplicaResponse whose detail and run_id are bounded strings taken only from a dict body's "
        "'detail' and 'run_id' members and are None otherwise; never raises on the body's shape and never coerces "
        "external values"
    ),
    non_raising=True,
)
def replica_response_from_envelope(*, addressed_to: str, status: int, instance_id: str | None, body: object) -> ReplicaResponse:
    detail: str | None = None
    run_id: str | None = None
    if isinstance(body, dict):
        candidate_detail = body.get("detail")
        if type(candidate_detail) is str and 0 < len(candidate_detail) <= 256:
            detail = candidate_detail
        candidate_run_id = body.get("run_id")
        if type(candidate_run_id) is str and 0 < len(candidate_run_id) <= 128:
            run_id = candidate_run_id
    return ReplicaResponse(addressed_to=addressed_to, status=status, instance_id=instance_id, detail=detail, run_id=run_id)


class ReplicaProbeDriver:
    """Fire one request at two replicas inside the dispatch window and collect what each answered."""

    def __init__(
        self,
        *,
        controller: ReplicaController,
        observer: EvidenceObserver,
        client_factory: Callable[[str], AcceptanceHttpClient],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller = controller
        self._observer = observer
        self._client_factory = client_factory
        self._clock = clock

    def fire_pair(self, request: ProbeRequest, *, expected_statuses: set[int]) -> tuple[tuple[ReplicaResponse, ReplicaResponse], float]:
        """Send ``request`` to both replicas, released together; return the responses and the dispatch spread in ms."""

        first, second = self._controller.replicas()
        if first.name == second.name or first.origin == second.origin:
            raise AcceptanceInputError("replica probes need two distinct replicas")
        barrier = threading.Barrier(2)
        sent_at: dict[str, float] = {}

        def fire(address: ReplicaAddress) -> ReplicaResponse:
            client = self._client_factory(address.origin)
            barrier.wait()
            sent_at[address.name] = self._clock()
            status, instance_id, body = client.request_json_with_instance(
                request.method,
                request.path,
                expected_statuses=expected_statuses,
                json_body=request.json_body,
            )
            return replica_response_from_envelope(addressed_to=address.name, status=status, instance_id=instance_id, body=body)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = (pool.submit(fire, first), pool.submit(fire, second))
            responses = (futures[0].result(), futures[1].result())
        spread_ms = abs(sent_at[first.name] - sent_at[second.name]) * 1000.0
        return responses, spread_ms

    def fence_conflict_trial(self, session_id: str, request: ProbeRequest) -> FenceConflictTrial:
        """One P1 trial: read the fence, fire the pair, read the fence and the operation rows again."""

        epoch_before = self._observer.fence_epoch(session_id)
        responses, spread_ms = self.fire_pair(request, expected_statuses={200, 202, 409})
        return FenceConflictTrial(
            responses=responses,
            fence_epoch_before=epoch_before,
            fence_epoch_after=self._observer.fence_epoch(session_id),
            fence_owner_after=self._observer.fence_owner(session_id),
            guided_operation_rows=self._observer.guided_operation_rows(session_id, since_epoch=epoch_before),
            dispatch_spread_ms=spread_ms,
        )

    def run_start_trial(self, session_id: str, request: ProbeRequest) -> RunStartTrial:
        """One P2 trial: fire the pair, then read the run rows the session now has."""

        responses, spread_ms = self.fire_pair(request, expected_statuses={202, 409})
        return RunStartTrial(
            responses=responses,
            runs_row_ids=self._observer.runs_row_ids(session_id),
            landscape_run_ids=self._observer.landscape_run_ids(session_id),
            dispatch_spread_ms=spread_ms,
        )
