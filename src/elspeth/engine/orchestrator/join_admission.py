"""JoinAdmissionService: §B.1 atomic follower admission (ADR-030).

Extracted from ``Orchestrator.join_run`` / ``Orchestrator._join_preflight``
(archived issue elspeth-9e71ae82a4). ``Orchestrator.join_run`` remains the public
entry point and delegates here verbatim.

Behaviour-preserving: preflight ordering (filesystem checks BEFORE touching
the registry), the config-hash recipe (``stable_hash(resolve_config(settings))``),
and the single-transaction ``admit_follower`` call are unchanged. Tests that
previously patched ``…orchestrator.core.resolve_config`` / ``.stable_hash`` /
``.os.access`` patch this module instead.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.exc import SQLAlchemyError

from elspeth.contracts import RunStatus, hashing
from elspeth.contracts import errors as contract_errors
from elspeth.contracts.coordination import (
    DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
    WorkerMembershipToken,
    mint_worker_id,
)
from elspeth.contracts.errors import AuditIntegrityError, JoinNotReadyError, JoinRefusedError
from elspeth.core.canonical import stable_hash
from elspeth.core.config import resolve_config
from elspeth.core.landscape.factory import LandscapeReadRepositories, RecorderFactory
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.engine.orchestrator.implementation_compatibility import check_implementation_compatibility

if TYPE_CHECKING:
    from elspeth.contracts.audit import Run
    from elspeth.core.config import ElspethSettings
    from elspeth.core.dag import ExecutionGraph
    from elspeth.core.landscape import LandscapeDB
    from elspeth.engine.orchestrator.types import PipelineConfig


class JoinAdmissionService:
    """Owns the cooperative follower attach path (NOT a ``resume()`` variant)."""

    def __init__(self, *, db: LandscapeDB) -> None:
        self._db = db

    def _check_join_compatibility(
        self,
        read_factory: LandscapeReadRepositories,
        run: Run,
        run_id: str,
        config: PipelineConfig,
        graph: ExecutionGraph,
        *,
        defer_to_admission_on_dead_leader: bool = True,
    ) -> None:
        if run.canonical_version != hashing.CANONICAL_VERSION:
            raise JoinRefusedError(
                run_id,
                f"canonical version changed from {run.canonical_version!r} to {hashing.CANONICAL_VERSION!r}",
            )
        recorded_edges = {
            (edge.from_node_id, edge.to_node_id, edge.label, edge.default_mode) for edge in read_factory.data_flow.get_edges(run_id)
        }
        expected_edges = {(edge.from_node, edge.to_node, edge.label, edge.mode) for edge in graph.get_edges()}
        if recorded_edges < expected_edges:
            if defer_to_admission_on_dead_leader:
                leader = RunCoordinationRepository(self._db.engine).live_leader(run_id=run_id)
                if leader is None or not leader.seat_live:
                    return
            raise JoinNotReadyError(run_id, "the run's leader has not finished registering its graph; retry the join")
        if recorded_edges != expected_edges:
            raise JoinRefusedError(run_id, "recorded graph edges do not match follower graph")
        implementation_check = check_implementation_compatibility(read_factory, run_id, config, graph)
        if not implementation_check.can_resume:
            assert implementation_check.reason is not None
            raise JoinRefusedError(run_id, implementation_check.reason)

    @staticmethod
    def _join_preflight(db: LandscapeDB, run_id: str) -> None:
        """§B.1 step 0: filesystem write-access preflight for the join path.

        Verifies that the joining process can write to the DB file, its
        containing directory, and any existing ``-wal`` / ``-shm`` sidecars
        BEFORE touching the registry.  Raises :class:`JoinRefusedError` with
        an actionable path and description on any failure so the operator
        knows exactly what permission is missing.

        SQLCipher: passphrase is verified implicitly by LandscapeDB's PRAGMA
        probe at open time (the engine construction that precedes this call
        fails with an opaque SQLite error if the passphrase is wrong).  This
        preflight only handles filesystem-level write access.

        Non-SQLite (Postgres): no filesystem sidecars exist; skip silently.
        """
        url = db.connection_string
        if not url.startswith("sqlite"):
            # Postgres / other backends: no filesystem sidecars to check.
            return

        # Use SQLAlchemy's make_url to reliably extract the database path
        # (handles sqlite:///path, sqlite:////abs/path, and :memory: forms).
        from sqlalchemy.engine.url import make_url as _make_url

        parsed_url = _make_url(url)
        db_file = parsed_url.database
        if db_file is None or db_file in ("", ":memory:"):
            return  # in-memory: no filesystem checks needed

        db_path = Path(db_file)
        if not db_path.is_absolute():
            db_path = Path.cwd() / db_path

        checks: list[tuple[Path, str]] = [
            (db_path, "write access to the audit DB file"),
            (db_path.parent, "write access to the audit DB directory"),
        ]
        for sidecar_suffix in ("-wal", "-shm"):
            sidecar = db_path.parent / (db_path.name + sidecar_suffix)
            if sidecar.exists():
                checks.append((sidecar, f"write access to {sidecar_suffix} sidecar"))

        for path, description in checks:
            if not os.access(path, os.W_OK):
                raise JoinRefusedError(
                    run_id,
                    f"filesystem preflight failed: {description} at {path} — "
                    "ensure the joining process has write permission "
                    "(shared group + group-writable state directory required for "
                    "cross-uid joins; see ADR-030 §B.1 step 0 and the slice-6 runbook)",
                )

    def join_run(
        self,
        run_id: str,
        settings: ElspethSettings,
        *,
        config: PipelineConfig,
        graph: ExecutionGraph,
        window_seconds: float | None = None,
    ) -> WorkerMembershipToken:
        """§B.1: atomic follower admission — new public entry point (ADR-030).

        NOT a ``resume()`` variant.  ``resume()`` keeps refusing
        RUNNING-with-live-leader; ``join_run`` is the cooperative follower
        attach path.

        Steps performed (design §B.1):

        0. Filesystem preflight — write access to DB file + dir + any
           existing ``-wal``/``-shm`` sidecars.  Raises :class:`JoinRefusedError`
           naming the path if the joining process cannot write.

        1. DB is already open through ``self._db`` (the caller's
           ``LandscapeDB`` carries the PRAGMA probe + epoch check — G28
           cross-process uniformity by construction).

        2. Read the run before admission. A matching, RUNNING run must use the
           current canonical version, have its full graph registered, and
           match the follower's plugin implementation evidence. Incomplete
           registration is retryable while the leader seat is live; an absent
           or still-expired seat receives the admission transaction's seat refusal.

        3. Atomic admission (one ``BEGIN IMMEDIATE`` transaction via
           :meth:`RunCoordinationRepository.admit_follower`):

           - ``SELECT runs.status, runs.config_hash`` — status must be
             ``RUNNING``, else :class:`JoinRefusedError`;
           - joiner's resolved settings hash must equal ``config_hash``,
             else refused (different pipeline ⇒ different graph + barrier
             keys);
           - ``run_coordination`` seat must be live against the Landscape
             database clock (``leader_heartbeat_expires_at >= database_now``,
             ADR-047), else refused ("no live leader — use ``elspeth resume``");
           - ``INSERT run_workers`` (role='follower', status='active') +
             ``worker_register`` event.  COMMIT.

        After every admission, the run's version, graph, and plugin evidence
        are checked again. A refusal then records registration and departure
        for that follower.

        Args:
            run_id: Landscape run ID to join.
            settings: The joining process's resolved ``ElspethSettings``.
                Its ``stable_hash(resolve_config(settings))`` is compared
                to ``runs.config_hash``; they must be equal.
            window_seconds: Heartbeat liveness window (defaults to
                :data:`~elspeth.contracts.coordination.DEFAULT_RUN_LIVENESS_WINDOW_SECONDS`).

        Returns:
            The follower's :class:`WorkerMembershipToken` — ``(run_id,
            worker_id)`` with the minted ``worker:{run_id}:{uuid4().hex}``
            identity — exactly as ``admit_follower`` established it. The
            caller threads it by value into ``build_follower_processor``; it is
            the follower's only authority (ADR-030 D4; ADR-048 amendment) and
            is never constructed outside the repository.

        Raises:
            JoinRefusedError: Filesystem preflight failed, version, graph edge
                identity or plugin implementation differs, run is not RUNNING,
                config hash mismatches, or no live leader seat.
            JoinNotReadyError: The leader is live but graph registration is incomplete.
        """

        _window = window_seconds if window_seconds is not None else DEFAULT_RUN_LIVENESS_WINDOW_SECONDS

        # Step 0: filesystem preflight BEFORE touching the registry.
        self._join_preflight(self._db, run_id)

        # Step 1: DB already open through self._db (PRAGMA probe + epoch
        # check inherited at LandscapeDB construction — G28 uniformity).

        # Step 2: atomic admission via the slice-5 RunCoordinationRepository
        # surface.  The joiner computes its own config_hash from its settings
        # using the same stable_hash(resolve_config(settings)) recipe that
        # begin_run stored.
        joiner_config_hash = stable_hash(resolve_config(settings))
        worker_id = mint_worker_id(run_id)

        read_factory = RecorderFactory.read_only(self._db)
        run = read_factory.run_lifecycle.get_run(run_id)
        if run is not None and run.status is RunStatus.RUNNING and run.config_hash == joiner_config_hash:
            self._check_join_compatibility(read_factory, run, run_id, config, graph)

        factory = RecorderFactory(self._db, payload_store=None)
        token = factory.run_coordination.admit_follower(
            run_id=run_id,
            worker_id=worker_id,
            config_hash=joiner_config_hash,
            window_seconds=_window,
        )
        try:
            current_factory = RecorderFactory.read_only(self._db)
            current_run = current_factory.run_lifecycle.get_run(run_id)
            if current_run is None:
                raise AuditIntegrityError(f"Admitted follower's run {run_id!r} is missing from the Landscape")
            self._check_join_compatibility(current_factory, current_run, run_id, config, graph, defer_to_admission_on_dead_leader=False)
        except contract_errors.TIER_1_ERRORS as error:
            try:
                factory.run_coordination.depart_worker(member_token=token)
            except Exception as departure_error:
                error.add_note(f"follower {token.worker_id} departure failed: {type(departure_error).__name__}")
            raise
        except JoinRefusedError as refusal:
            try:
                factory.run_coordination.depart_worker(member_token=token)
            except contract_errors.TIER_1_ERRORS as departure_error:
                raise departure_error from refusal
            except SQLAlchemyError as departure_error:
                refusal.departure_failure = f"{type(departure_error).__name__}: {departure_error}"
            except Exception as departure_error:
                refusal.add_note(f"follower {token.worker_id} departure failed: {type(departure_error).__name__}")
            raise
        except Exception as error:
            try:
                factory.run_coordination.depart_worker(member_token=token)
            except contract_errors.TIER_1_ERRORS as departure_error:
                raise departure_error from error
            except SQLAlchemyError as departure_error:
                error.add_note(f"follower {token.worker_id} departure failed: {type(departure_error).__name__}: {departure_error}")
            except Exception as departure_error:
                error.add_note(f"follower {token.worker_id} departure failed: {type(departure_error).__name__}")
            raise
        return token
