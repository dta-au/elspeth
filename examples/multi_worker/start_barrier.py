"""Example-local start barrier and CLI entrypoint for multi-worker demonstrations."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from elspeth.contracts import Determinism
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.hookspecs import hookimpl
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.infrastructure.results import TransformResult

# A blocked transform does not heartbeat its item. This is one fifth of the
# example's default 300-second item lease, leaving ample room for teardown.
STARTUP_BUDGET_SECONDS = 60
LEASE_SAFETY_MARGIN_SECONDS = 10
POLL_SECONDS = 0.1


class BarrierFailure(RuntimeError):
    """A demonstration worker could not pass the start barrier."""


def validate_worker_count(first_fanout: int, followers: int) -> int:
    """Require a first fan-out child for every requested process."""
    workers = followers + 1
    if workers > first_fanout:
        raise BarrierFailure(f"start barrier: {workers} workers cannot share {first_fanout} first-fan-out rows")
    return workers


def _write_json(path: Path, value: object) -> None:
    staging = path.with_name(f".{path.name}.{os.getpid()}")
    staging.write_text(json.dumps(value))
    os.replace(staging, path)


def _process_alive(pid: int) -> bool:
    """Include zombie detection: the launcher reaps children after its leader."""
    try:
        if Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] == "Z":
            return False
        os.kill(pid, 0)
    except (FileNotFoundError, ProcessLookupError):
        return False
    return True


def _live_members(db: Path, run_id: str) -> dict[str, str]:
    """Read exact active membership plus a live leased item in this run."""
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1) as conn:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """SELECT rw.worker_id, rw.role FROM run_workers AS rw
            WHERE rw.run_id = ? AND rw.status = 'active'
              AND julianday(rw.heartbeat_expires_at) > julianday('now')
              AND EXISTS (
                SELECT 1 FROM token_work_items AS wi
                WHERE wi.run_id = rw.run_id AND wi.lease_owner = rw.worker_id
                  AND wi.status = 'leased'
                  AND julianday(wi.lease_expires_at) > julianday('now')
              )""",
            (run_id,),
        ).fetchall()
    return dict(rows)


class StartBarrier:
    """One first-row rendezvous per worker process; subsequent rows bypass it."""

    def __init__(
        self,
        directory: Path,
        workers: int,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
        alive: Callable[[int], bool],
        live_members: Callable[[str], dict[str, str]],
        budget_seconds: int = STARTUP_BUDGET_SECONDS,
    ) -> None:
        self.directory = directory
        self.workers = workers
        self.monotonic = monotonic
        self.sleep = sleep
        self.alive = alive
        self.live_members = live_members
        self.budget_seconds = budget_seconds
        self._released = False

    def _release_failure(self, run_id: str) -> str | None:
        """Recheck arrival, process, membership and lease while holding the decision lock."""
        manifest = self.directory / "workers.json"
        if not manifest.exists():
            return "start barrier: worker manifest missing before release"
        pids: list[int] = json.loads(manifest.read_text())
        if len(pids) != self.workers or len(set(pids)) != self.workers:
            return "start barrier: invalid launcher worker manifest"
        arrivals = []
        for pid in pids:
            path = self.directory / f"{pid}.json"
            if not path.exists():
                return "start barrier: worker missing before release"
            arrival = json.loads(path.read_text())
            if arrival["pid"] != pid:
                return "start barrier: invalid worker arrival before release"
            arrivals.append(arrival)
        for pid in pids:
            if not self.alive(pid):
                return f"start barrier: worker process {pid} exited before release"
        identities = {item["worker_id"] for item in arrivals}
        roles = [item["role"] for item in arrivals]
        members = self.live_members(run_id)
        if (
            len(identities) != self.workers
            or roles.count("leader") != 1
            or roles.count("follower") != self.workers - 1
            or any(item["run_id"] != run_id or members.get(item["worker_id"]) != item["role"] for item in arrivals)
        ):
            return "start barrier: active membership or live lease missing before release"
        return None

    def _decide(self, run_id: str, deadline: float, timeout_message: str, failure_message: str | None = None) -> None:
        """Serialize success and failure so a late arrival cannot undo failure."""
        with (self.directory / "decision.lock").open("a+") as lock:
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        raise BarrierFailure("start barrier: decision lock unavailable at deadline") from None
                    self.sleep(min(POLL_SECONDS, remaining))
            failure = self.directory / "failed.json"
            if failure.exists():
                raise BarrierFailure(json.loads(failure.read_text())["message"])
            release = self.directory / "released.json"
            if release.exists():
                if json.loads(release.read_text())["run_id"] != run_id:
                    raise BarrierFailure("start barrier: release belongs to another run")
            else:
                if failure_message is None:
                    if self.monotonic() >= deadline:
                        failure_message = timeout_message
                    else:
                        failure_message = self._release_failure(run_id)
                    if failure_message is None and self.monotonic() >= deadline:
                        failure_message = timeout_message
                if failure_message is not None:
                    _write_json(failure, {"message": failure_message})
                    raise BarrierFailure(failure_message)
                staging = release.with_name(f".{release.name}.{os.getpid()}")
                try:
                    staging.write_text(json.dumps({"run_id": run_id, "released_at": time.time()}))
                    if self.monotonic() >= deadline:
                        _write_json(failure, {"message": timeout_message})
                        raise BarrierFailure(timeout_message)
                    os.replace(staging, release)
                finally:
                    staging.unlink(missing_ok=True)
            self._released = True

    def arrive(self, run_id: str, worker_id: str, role: str, pid: int, lease_remaining_seconds: float = 300) -> None:
        if self._released:
            return
        _write_json(self.directory / f"{pid}.json", {"pid": pid, "run_id": run_id, "worker_id": worker_id, "role": role})
        if lease_remaining_seconds <= LEASE_SAFETY_MARGIN_SECONDS:
            self._decide(
                run_id, self.monotonic(), "start barrier: item lease too close to expiry", "start barrier: item lease too close to expiry"
            )
            return
        effective_budget = min(self.budget_seconds, lease_remaining_seconds - LEASE_SAFETY_MARGIN_SECONDS)
        deadline = self.monotonic() + effective_budget
        timeout_message = (
            "start barrier: item lease safety deadline reached"
            if effective_budget < self.budget_seconds
            else f"start barrier: deadline reached after {self.budget_seconds}s"
        )
        while True:
            failure_reason = "worker missing"
            failure = self.directory / "failed.json"
            if failure.exists():
                raise BarrierFailure(json.loads(failure.read_text())["message"])
            release = self.directory / "released.json"
            if release.exists():
                if json.loads(release.read_text())["run_id"] != run_id:
                    raise BarrierFailure("start barrier: release belongs to another run")
                self._released = True
                return
            manifest = self.directory / "workers.json"
            if manifest.exists():
                pids: list[int] = json.loads(manifest.read_text())
                if len(pids) != self.workers or len(set(pids)) != self.workers:
                    raise BarrierFailure("start barrier: invalid launcher worker manifest")
                arrivals = [
                    json.loads((self.directory / f"{other}.json").read_text())
                    for other in pids
                    if (self.directory / f"{other}.json").exists()
                ]
                arrived_pids = {item["pid"] for item in arrivals}
                for other in pids:
                    if not self.alive(other):
                        phase = "before release" if other in arrived_pids else "before arrival"
                        message = f"start barrier: worker process {other} exited {phase}"
                        self._decide(run_id, deadline, timeout_message, message)
                        return
                if len(arrivals) == self.workers:
                    failure_reason = "active membership or live lease missing"
                    identities = {item["worker_id"] for item in arrivals}
                    members = self.live_members(run_id)
                    roles = [item["role"] for item in arrivals]
                    if (
                        len(identities) == self.workers
                        and roles.count("leader") == 1
                        and roles.count("follower") == self.workers - 1
                        and all(item["run_id"] == run_id and members.get(item["worker_id"]) == item["role"] for item in arrivals)
                    ):
                        self._decide(run_id, deadline, timeout_message)
                        return
            if self.monotonic() >= deadline:
                if effective_budget < self.budget_seconds:
                    message = "start barrier: item lease safety deadline reached"
                else:
                    message = f"start barrier: {failure_reason} after {self.budget_seconds}s"
                self._decide(run_id, deadline, timeout_message, message)
                return
            remaining = deadline - self.monotonic()
            if remaining > 0:
                self.sleep(min(POLL_SECONDS, remaining))


class DemonstrationStartBarrierConfig(TransformDataConfig):
    """The barrier's only authored option is its row schema."""


class DemonstrationStartBarrier(BaseTransform):
    """Hold the first row until all demonstration workers own live work."""

    name = "demonstration_start_barrier"
    determinism = Determinism.IO_WRITE
    plugin_version = "1.0.0"
    config_model = DemonstrationStartBarrierConfig
    passes_through_input = True
    preserves_input_values = True

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        cfg = DemonstrationStartBarrierConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._schema_config = cfg.schema_config
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)
        self.input_schema, self.output_schema = self._create_schemas(cfg.schema_config, "DemonstrationStartBarrier")
        self._barrier: StartBarrier | None = None

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {"schema": {"mode": "observed"}}

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        # This makes the demonstration deterministic; real multi-worker pipelines need no start barrier.
        membership = ctx.require_member_token()
        lease_expires_at = ctx.require_work_item().lease_expires_at
        if lease_expires_at is None:
            raise BarrierFailure("start barrier: item has no live lease")
        lease_deadline = lease_expires_at.replace(tzinfo=UTC) if lease_expires_at.tzinfo is None else lease_expires_at
        lease_remaining_seconds = (lease_deadline - datetime.now(UTC)).total_seconds()
        role = os.environ["ELSPETH_DEMO_BARRIER_ROLE"]
        if self._barrier is None:
            directory = Path(os.environ["ELSPETH_DEMO_BARRIER_DIR"])
            workers = int(os.environ["ELSPETH_DEMO_BARRIER_WORKERS"])
            db = Path(os.environ["ELSPETH_DEMO_BARRIER_DB"])
            self._barrier = StartBarrier(
                directory, workers, time.monotonic, time.sleep, _process_alive, lambda run_id: _live_members(db, run_id)
            )
        self._barrier.arrive(ctx.run_id, membership.worker_id, role, os.getpid(), lease_remaining_seconds)
        output_contract = self._align_output_contract(self._apply_declared_output_field_contracts(row.contract))
        return TransformResult.success(PipelineRow(copy.deepcopy(row.to_dict()), output_contract), success_reason={"action": "passthrough"})


class _ExamplePlugins:
    @hookimpl
    def elspeth_get_transforms(self) -> list[type[DemonstrationStartBarrier]]:
        return [DemonstrationStartBarrier]


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--check-fanout":
        with Path(sys.argv[2]).open() as source:
            first = json.loads(source.readline())
        try:
            validate_worker_count(len(first["items"]), int(sys.argv[3]))
        except BarrierFailure as error:
            sys.stderr.write(f"{error}\n")
            raise SystemExit(2) from error
        return
    get_shared_plugin_manager().register(_ExamplePlugins())
    from elspeth.cli import app

    app()


if __name__ == "__main__":
    main()
