"""Collect P3/P4 evidence from authenticated replicas and PostgreSQL.

The caller prepares runnable sessions through the normal Composer. P3 needs
an operator-mounted physical CSV sink with unique input keys. The execution
worker retains the session-operation lease until its run finishes; the probe
observes that actual authority before partitioning. No pipeline structure is authored here. Fault
injection is confined to runtime role A and LOGIN is restored in finally.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import stat
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ConfigDict, TypeAdapter

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.http_client import AcceptanceCredentials, AcceptanceHttpClient
from elspeth.web._acceptance_common.replica_probes import (
    TERMINAL_RUN_STATUSES,
    CrossReplicaProgressObservation,
    LeaseTakeoverObservation,
    ReplicaResponse,
    replica_response_from_envelope,
)
from elspeth.web._azure_container_apps_acceptance.controller import (
    DATABASE_NOW_SQL,
    PostgresEvidenceObserver,
    RoleRevocationPartition,
    SqlReader,
    label_url,
)
from elspeth.web.azure_container_apps_acceptance import (
    _engine,
    _session_factory,
    _SqlAlchemyReader,
    acceptance_error_envelope,
)

_FENCE_SQL = (
    "SELECT owner_instance_id, lease_expires_at FROM session_operation_fences "
    "WHERE session_id = :session_id AND released_at IS NULL AND operation_kind = 'execute'"
)
_RUN_SQL = "SELECT status, error, owner_instance_id FROM runs WHERE id = :run_id AND session_id = :session_id"
_RUN_PIPELINE_SQL = "SELECT pipeline_yaml FROM runs WHERE id = :run_id AND session_id = :session_id"
_MAX_SINK_BYTES = 8 * 1024 * 1024


class _Identity(BaseModel):
    model_config = ConfigDict(strict=True)
    id: str


class _RunStart(BaseModel):
    model_config = ConfigDict(strict=True)
    run_id: str


class _RunStatus(_RunStart):
    status: str


class _Artifact(BaseModel):
    model_config = ConfigDict(strict=True)
    artifact_id: str
    content_hash: str


class _Outputs(_RunStart):
    artifacts: list[_Artifact]


class _Message(_Identity):
    role: str
    content: str


class _MessageWrite(BaseModel):
    model_config = ConfigDict(strict=True)
    message: _Message


_MESSAGES = TypeAdapter(list[_Message])


class _SinkOptions(BaseModel):
    model_config = ConfigDict(strict=True)
    path: str


class _Sink(BaseModel):
    model_config = ConfigDict(strict=True)
    plugin: str
    options: _SinkOptions


class _Pipeline(BaseModel):
    model_config = ConfigDict(strict=True)
    sinks: dict[str, _Sink]


@dataclass(frozen=True)
class PhysicalSinkOracle:
    """Count repeated unique input keys in actual CSV bytes, never audit rows."""

    path: Path
    key_field: str

    def bind(self, *, sessions: SqlReader, run_id: str, session_id: str, capture: Capture) -> None:
        """Bind this file to the run's persisted, fully resolved CSV sink.

        The operator must mount the ACA NFS share at the same absolute path as
        the workload. A different path or another session's output is refused.
        """
        pipeline = sessions.scalar(_RUN_PIPELINE_SQL, run_id=run_id, session_id=session_id)
        if type(pipeline) is not str or not pipeline or len(pipeline.encode()) > _MAX_SINK_BYTES:
            raise AcceptanceCheckError("probe_sink_binding")
        config = _Pipeline.model_validate(yaml.safe_load(pipeline))
        if len(config.sinks) != 1:
            raise AcceptanceCheckError("probe_sink_binding")
        configured = next(iter(config.sinks.values()))
        runtime_path = Path(configured.options.path)
        if (
            configured.plugin != "csv"
            or not runtime_path.is_absolute()
            or runtime_path != self.path.absolute()
            or ("outputs", session_id) not in tuple(zip(runtime_path.parts, runtime_path.parts[1:], strict=False))
        ):
            raise AcceptanceCheckError("probe_sink_binding")
        capture.write(
            "physical-sink-binding",
            {
                "run_id": run_id,
                "session_id": session_id,
                "path": str(runtime_path),
                "pipeline_sha256": hashlib.sha256(pipeline.encode()).hexdigest(),
                "key_field": self.key_field,
            },
        )

    def _keys(self) -> list[str]:
        try:
            metadata = self.path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= _MAX_SINK_BYTES:
                raise AcceptanceCheckError("probe_sink_oracle")
            with self.path.open("rb") as stream:
                content = stream.read(_MAX_SINK_BYTES + 1)
            if len(content) > _MAX_SINK_BYTES:
                raise AcceptanceCheckError("probe_sink_oracle")
            reader = csv.DictReader(io.StringIO(content.decode("utf-8")), strict=True)
            if reader.fieldnames is None or reader.fieldnames.count(self.key_field) != 1:
                raise AcceptanceCheckError("probe_sink_oracle")
            keys: list[str] = []
            for row in reader:
                key = row[self.key_field]
                if type(key) is not str or not key or None in row:
                    raise AcceptanceCheckError("probe_sink_oracle")
                keys.append(key)
        except (OSError, UnicodeError, csv.Error):
            raise AcceptanceCheckError("probe_sink_oracle") from None
        return keys

    def has_effects(self) -> bool:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return False
        return bool(self._keys())

    def duplicates(self) -> int:
        keys = self._keys()
        if not keys:
            raise AcceptanceCheckError("probe_sink_oracle")
        return len(keys) - len(set(keys))


def _json_default(value: object) -> str:
    if type(value) is datetime and value.tzinfo is not None:
        return value.isoformat()
    raise TypeError("unsupported observation value")


def observation_document(observation: CrossReplicaProgressObservation | LeaseTakeoverObservation) -> object:
    document = asdict(observation)
    if type(observation) is LeaseTakeoverObservation:
        for name, response in (("before_expiry", observation.before_expiry), ("after_expiry", observation.after_expiry)):
            document[name] = {
                "addressed_to": response.addressed_to,
                "status": response.status,
                "instance_id": response.instance_id,
                "body": {"detail": response.detail, "run_id": response.run_id},
            }
    return json.loads(json.dumps(document, default=_json_default))


@dataclass(frozen=True)
class Capture:
    directory: Path

    def __post_init__(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = self.directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise AcceptanceInputError("observation evidence directory must be owner-only")

    def write(self, label: str, document: object) -> None:
        path = self.directory / f"{label}-{uuid4().hex}.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(document, stream, default=_json_default)


@dataclass(frozen=True)
class Polling:
    interval: float = 2.0
    timeout: float = 180.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if not math.isfinite(self.interval) or not math.isfinite(self.timeout) or not 0 < self.interval <= 30 <= self.timeout <= 600:
            raise AcceptanceInputError("poll interval must be at most 30 seconds; timeout must be 30 to 600 seconds")

    def until(self, predicate: Callable[[], bool]) -> None:
        deadline = self.clock() + self.timeout
        while not predicate():
            if self.clock() >= deadline:
                raise AcceptanceCheckError("probe_observation_timeout")
            self.sleep(min(self.interval, max(0, deadline - self.clock())))


def _request(
    client: AcceptanceHttpClient, label: str, path: str, capture: Capture, *, body: object = None, post: bool = False
) -> tuple[ReplicaResponse, object]:
    observed = _exchange(client, label, path, capture, body=body, post=post)
    return observed.response, observed.body


@dataclass(frozen=True)
class _TimedResponse:
    response: ReplicaResponse
    body: object
    received_at: float


def _exchange(
    client: AcceptanceHttpClient,
    label: str,
    path: str,
    capture: Capture,
    *,
    body: object = None,
    post: bool = False,
    clock: Callable[[], float] = time.monotonic,
    allow_unavailable: bool = False,
) -> _TimedResponse:
    if post:
        status, instance, document = client.request_json_with_instance("POST", path, expected_statuses={200, 201, 202, 409}, json_body=body)
    else:
        status, instance, document = client.request_json_with_instance(
            "GET", path, expected_statuses={200, 404, 503} if allow_unavailable else {200}
        )
    received_at = clock()
    response = replica_response_from_envelope(addressed_to=label, status=status, instance_id=instance, body=document)
    capture.write(label, {"path": path, "response": asdict(response), "body": document, "received_at_monotonic": received_at})
    return _TimedResponse(response, document, received_at)


def _instance(client: AcceptanceHttpClient, label: str, capture: Capture) -> str:
    response, _body = _request(client, label, "/api/system/status", capture)
    if response.instance_id is None:
        raise AcceptanceCheckError("probe_instance_header")
    return response.instance_id


def _start(client: AcceptanceHttpClient, session_id: str, owner: str, capture: Capture) -> str:
    response, body = _request(client, "a", f"/api/sessions/{session_id}/execute", capture, body={}, post=True)
    if response.status != 202 or response.instance_id != owner:
        raise AcceptanceCheckError("probe_run_start")
    return str(UUID(_RunStart.model_validate(body).run_id))


def _fresh_message_visibility(
    owner: AcceptanceHttpClient,
    reader: AcceptanceHttpClient,
    session_id: str,
    owner_id: str,
    reader_id: str,
    capture: Capture,
    polling: Polling,
) -> float:
    path = f"/api/sessions/{session_id}/messages"
    content = f"Keep the current pipeline unchanged. Briefly acknowledge this visibility check: {uuid4()}."
    written = _exchange(owner, "a", path, capture, post=True, body={"content": content}, clock=polling.clock)
    if written.response.status != 200 or written.response.instance_id != owner_id:
        raise AcceptanceCheckError("probe_message_write")
    message = _MessageWrite.model_validate(written.body).message
    message_id = str(UUID(message.id))
    if message.role != "assistant":
        raise AcceptanceCheckError("probe_message_write")
    seen_at: float | None = None

    def visible() -> bool:
        nonlocal seen_at
        observed = _exchange(reader, "b", path + "?limit=500", capture, clock=polling.clock)
        if observed.response.instance_id != reader_id:
            raise AcceptanceCheckError("probe_instance_changed")
        messages = _MESSAGES.validate_python(observed.body)
        if any(item.id == message_id and item.role == "assistant" and item.content == message.content for item in messages) and any(
            item.role == "user" and item.content == content for item in messages
        ):
            seen_at = observed.received_at
            return True
        return False

    polling.until(visible)
    assert seen_at is not None
    return seen_at - written.received_at


def _status_visibility(
    owner: AcceptanceHttpClient,
    reader: AcceptanceHttpClient,
    run_id: str,
    owner_id: str,
    reader_id: str,
    acknowledged_at: float,
    capture: Capture,
    polling: Polling,
) -> tuple[float, str]:
    path = f"/api/runs/{run_id}"
    first_reader_at: float | None = None
    owner_terminal_at: float | None = None
    owner_terminal_status: str | None = None
    terminal_delay: float | None = None

    def visible() -> bool:
        nonlocal first_reader_at, owner_terminal_at, owner_terminal_status, terminal_delay
        # Read B immediately after start; never wait for the owner's run to
        # finish before checking whether the fresh run is visible there.
        peer = _exchange(reader, "b", path, capture, clock=polling.clock, allow_unavailable=True)
        if peer.response.instance_id != reader_id:
            raise AcceptanceCheckError("probe_instance_changed")
        if peer.response.status == 200:
            initial_status = _RunStatus.model_validate(peer.body)
            if initial_status.run_id != run_id:
                raise AcceptanceCheckError("probe_run_identity")
            if first_reader_at is None:
                first_reader_at = peer.received_at
        writer = _exchange(owner, "a", path, capture, clock=polling.clock)
        if writer.response.instance_id != owner_id or peer.response.instance_id != reader_id:
            raise AcceptanceCheckError("probe_instance_changed")
        writer_status = _RunStatus.model_validate(writer.body)
        if writer_status.run_id != run_id:
            raise AcceptanceCheckError("probe_run_identity")
        if writer_status.status in TERMINAL_RUN_STATUSES and owner_terminal_at is None:
            owner_terminal_at = writer.received_at
            owner_terminal_status = writer_status.status
            # The first B read preceded the owner's terminal observation.
            # Re-read immediately so our own polling order does not add a
            # whole interval to an already visible terminal write.
            peer = _exchange(reader, "b", path, capture, clock=polling.clock, allow_unavailable=True)
            if peer.response.instance_id != reader_id:
                raise AcceptanceCheckError("probe_instance_changed")
        if peer.response.status != 200:
            return False
        peer_status = _RunStatus.model_validate(peer.body)
        if peer_status.run_id != run_id:
            raise AcceptanceCheckError("probe_run_identity")
        if first_reader_at is None:
            first_reader_at = peer.received_at
        if owner_terminal_at is not None and peer_status.status == owner_terminal_status:
            terminal_delay = max(0.0, peer.received_at - owner_terminal_at)
            return True
        return False

    polling.until(visible)
    assert first_reader_at is not None and terminal_delay is not None and owner_terminal_status is not None
    return max(first_reader_at - acknowledged_at, terminal_delay), owner_terminal_status


def _output_visibility(
    owner: AcceptanceHttpClient,
    reader: AcceptanceHttpClient,
    run_id: str,
    owner_id: str,
    reader_id: str,
    capture: Capture,
    polling: Polling,
) -> float:
    path = f"/api/runs/{run_id}/outputs"
    first_outputs: tuple[_Artifact, ...] = ()
    appeared_at: float | None = None
    seen_at: float | None = None

    def visible() -> bool:
        nonlocal first_outputs, appeared_at, seen_at
        writer = _exchange(owner, "a", path, capture, clock=polling.clock, allow_unavailable=True)
        peer = _exchange(reader, "b", path, capture, clock=polling.clock, allow_unavailable=True)
        if writer.response.instance_id != owner_id or peer.response.instance_id != reader_id:
            raise AcceptanceCheckError("probe_instance_changed")
        if writer.response.status == 200:
            outputs = _Outputs.model_validate(writer.body)
            if outputs.run_id != run_id:
                raise AcceptanceCheckError("probe_run_identity")
            if outputs.artifacts and appeared_at is None:
                first_outputs = tuple(outputs.artifacts)
                appeared_at = writer.received_at
        if peer.response.status == 200:
            peer_outputs = _Outputs.model_validate(peer.body)
            if peer_outputs.run_id != run_id:
                raise AcceptanceCheckError("probe_run_identity")
            if first_outputs and all(artifact in peer_outputs.artifacts for artifact in first_outputs):
                seen_at = peer.received_at
                return True
        return False

    polling.until(visible)
    assert seen_at is not None and appeared_at is not None
    return seen_at - appeared_at


def collect_progress(
    *, owner: AcceptanceHttpClient, reader: AcceptanceHttpClient, session_id: str, capture: Capture, polling: Polling
) -> CrossReplicaProgressObservation:
    owner_id = _instance(owner, "a", capture)
    reader_id = _instance(reader, "b", capture)
    if owner_id == reader_id:
        raise AcceptanceCheckError("probe_instances_not_distinct")
    # Upload real, unpredictable bytes through A before starting the run.
    content = f"acceptance-{uuid4()}\n".encode()
    uploaded = owner.request_multipart_json(
        "POST", f"/api/sessions/{session_id}/blobs", expected_statuses={201}, files={"file": ("acceptance.txt", content, "text/plain")}
    )
    capture.write("uploaded-blob", uploaded)
    blob_id = str(UUID(_Identity.model_validate(uploaded).id))
    messages_delay = _fresh_message_visibility(owner, reader, session_id, owner_id, reader_id, capture, polling)
    started = _exchange(owner, "a", f"/api/sessions/{session_id}/execute", capture, post=True, body={}, clock=polling.clock)
    if started.response.status != 202 or started.response.instance_id != owner_id:
        raise AcceptanceCheckError("probe_run_start")
    run_id = str(UUID(_RunStart.model_validate(started.body).run_id))
    # Independently poll status and outputs from the start. A slow pipeline
    # cannot reset either timer by reaching terminal state much later.
    with ThreadPoolExecutor(max_workers=2) as executor:
        status_future = executor.submit(
            _status_visibility, owner, reader, run_id, owner_id, reader_id, started.received_at, capture, polling
        )
        outputs_future = executor.submit(_output_visibility, owner, reader, run_id, owner_id, reader_id, capture, polling)
        status_delay, terminal_status = status_future.result()
        outputs_delay = outputs_future.result()
    blob_path = f"/api/sessions/{session_id}/blobs/{blob_id}/content"
    owner_bytes = owner.request_bytes("GET", blob_path, expected_statuses={200})
    reader_bytes = reader.request_bytes("GET", blob_path, expected_statuses={200})
    if owner_bytes != content:
        raise AcceptanceCheckError("probe_uploaded_bytes_changed")
    return CrossReplicaProgressObservation(
        owner_instance_id=owner_id,
        reader_instance_id=reader_id,
        poll_interval_seconds=polling.interval,
        status_visible_after_seconds=status_delay,
        outputs_visible_after_seconds=outputs_delay,
        messages_visible_after_seconds=messages_delay,
        blob_sha256_via_owner=hashlib.sha256(owner_bytes).hexdigest(),
        blob_sha256_via_reader=hashlib.sha256(reader_bytes).hexdigest(),
        terminal_status_on_reader=terminal_status,
    )


def _database_time(reader: SqlReader) -> datetime:
    now = reader.scalar(DATABASE_NOW_SQL)
    if type(now) is not datetime or now.tzinfo is None:
        raise AcceptanceCheckError("probe_database_clock")
    return now


def collect_takeover(
    *,
    owner: AcceptanceHttpClient,
    survivor: AcceptanceHttpClient,
    session_id: str,
    sessions: SqlReader,
    observer: PostgresEvidenceObserver,
    partition: RoleRevocationPartition,
    sink: PhysicalSinkOracle,
    capture: Capture,
    polling: Polling,
) -> LeaseTakeoverObservation:
    if sink.path.exists():
        raise AcceptanceInputError("takeover requires a fresh physical sink path")
    owner_id = _instance(owner, "a", capture)
    survivor_id = _instance(survivor, "b", capture)
    if owner_id == survivor_id:
        raise AcceptanceCheckError("probe_instances_not_distinct")
    run_id = _start(owner, session_id, owner_id, capture)

    def running() -> bool:
        rows = sessions.rows(_RUN_SQL, run_id=run_id, session_id=session_id)
        capture.write("owner-run", rows)
        return len(rows) == 1 and rows[0][0] == "running" and rows[0][2] == owner_id

    polling.until(running)
    sink.bind(sessions=sessions, run_id=run_id, session_id=session_id, capture=capture)
    # A missing or header-only sink cannot establish a duplicate-write
    # oracle. Wait for a real effect before injecting failure, then require
    # the worker still owns an unreleased operation fence.
    polling.until(sink.has_effects)

    def owner_holds_fence() -> bool:
        rows = sessions.rows(_FENCE_SQL, session_id=session_id)
        capture.write("held-fence", rows)
        return len(rows) == 1 and rows[0][0] == owner_id and running()

    polling.until(owner_holds_fence)
    try:
        partition.partition("elspeth_runtime_a")
        owner_row = observer.membership_row(owner_id)
        rows = sessions.rows(_FENCE_SQL, session_id=session_id)
        if owner_row is None or len(rows) != 1 or rows[0][0] != owner_id or type(rows[0][1]) is not datetime:
            raise AcceptanceCheckError("probe_partition_authority")
        fence_expiry = rows[0][1]
        membership_expiry = owner_row.lease_expires_at
        if fence_expiry.tzinfo is None or _database_time(sessions) >= min(fence_expiry, membership_expiry):
            raise AcceptanceCheckError("probe_before_expiry_window_missed")
        mutation = f"/api/sessions/{session_id}/blobs/inline"
        mutation_body = {"filename": "takeover.txt", "content": str(uuid4()), "mime_type": "text/plain"}
        before, _ = _request(survivor, "b", mutation, capture, body=mutation_body, post=True)
        if _database_time(sessions) >= min(fence_expiry, membership_expiry):
            raise AcceptanceCheckError("probe_before_expiry_window_missed")
        polling.until(lambda: _database_time(sessions) > max(fence_expiry, membership_expiry))
        after, _ = _request(survivor, "b", mutation, capture, body=mutation_body, post=True)
        takeover_at = _database_time(sessions)
        fence_owner = observer.fence_owner(session_id)

        def orphan_terminal() -> bool:
            rows = sessions.rows(_RUN_SQL, run_id=run_id, session_id=session_id)
            capture.write("orphan-run", rows)
            return len(rows) == 1 and rows[0][0] in TERMINAL_RUN_STATUSES

        polling.until(orphan_terminal)
        rows = sessions.rows(_RUN_SQL, run_id=run_id, session_id=session_id)
        reason = rows[0][1]
        if reason is not None and type(reason) is not str:
            raise AcceptanceCheckError("probe_orphan_reason")
        owner_row = observer.membership_row(owner_id)
        return LeaseTakeoverObservation(
            primitive="role_revocation",
            owner_instance_id=owner_id,
            survivor_instance_id=survivor_id,
            owner_row=owner_row,
            before_expiry=before,
            after_expiry=after,
            takeover_observed_at=takeover_at,
            cancelled_run_reason=reason,
            fence_owner_after=fence_owner,
            duplicate_sink_effects=sink.duplicates(),
        )
    finally:
        partition.restore("elspeth_runtime_a")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("progress", "takeover"))
    for name in ("app-name", "resource-group", "default-domain", "revision-suffix", "session-id", "evidence-dir"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--sink-path")
    parser.add_argument("--sink-key-field")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env = os.environ if environ is None else environ
    try:
        session_id = str(UUID(args.session_id))
        polling = Polling(interval=args.poll_interval, timeout=args.timeout)
        capture = Capture(Path(args.evidence_dir))
        credentials = AcceptanceCredentials.from_env(env)
        with ExitStack() as stack:
            owner_url = label_url(app_name=args.app_name, label="a", default_domain=args.default_domain)
            reader_url = label_url(app_name=args.app_name, label="b", default_domain=args.default_domain)
            owner = stack.enter_context(AcceptanceHttpClient(origin=owner_url, credentials=credentials))
            reader = stack.enter_context(AcceptanceHttpClient(origin=reader_url, credentials=credentials))
            owner.authenticate(register=False)
            reader.authenticate(register=False)
            observation: CrossReplicaProgressObservation | LeaseTakeoverObservation
            if args.command == "progress":
                observation = collect_progress(owner=owner, reader=reader, session_id=session_id, capture=capture, polling=polling)
            else:
                if None in (args.sink_path, args.sink_key_field):
                    raise AcceptanceInputError("takeover requires physical sink path/key field")
                engines = [
                    _engine(env, key)
                    for key in (
                        "ELSPETH_ACCEPTANCE_SESSION_DB_URL",
                        "ELSPETH_ACCEPTANCE_LANDSCAPE_URL",
                        "ELSPETH_ACCEPTANCE_PG_ADMIN_URL",
                        "ELSPETH_ACCEPTANCE_PG_RUNTIME_A_URL",
                    )
                ]
                for engine in engines:
                    stack.callback(engine.dispose)
                sessions = _SqlAlchemyReader(engines[0])
                observer = PostgresEvidenceObserver(sessions=sessions, landscape=_SqlAlchemyReader(engines[1]))
                partition = RoleRevocationPartition(
                    admin=_session_factory(engines[2]), roles={"elspeth_runtime_a": _session_factory(engines[3])}
                )
                observation = collect_takeover(
                    owner=owner,
                    survivor=reader,
                    session_id=session_id,
                    sessions=sessions,
                    observer=observer,
                    partition=partition,
                    sink=PhysicalSinkOracle(Path(args.sink_path), args.sink_key_field),
                    capture=capture,
                    polling=polling,
                )
            document = observation_document(observation)
            capture.write("observation", document)
            sys.stdout.write(json.dumps(document) + "\n")
        return 0
    except Exception as exc:
        # The facade's existing closed envelope never prints provider payloads
        # or database credentials. This is the CLI's final reporting boundary.
        sys.stderr.write(json.dumps(acceptance_error_envelope(exc)) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
