"""Live collectors must measure replica reads and restore fault injection."""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.http_client import AcceptanceCredentials, AcceptanceHttpClient
from elspeth.web._acceptance_common.replica_probes import decide_cross_replica_progress, decide_lease_takeover
from elspeth.web._azure_container_apps_acceptance.controller import (
    PostgresEvidenceObserver,
    RoleRevocationPartition,
    SqlReader,
)
from elspeth.web._azure_container_apps_acceptance.evidence import cross_replica_progress_observation, lease_takeover_observation
from elspeth.web.azure_container_apps_observations import (
    Capture,
    PhysicalSinkOracle,
    Polling,
    collect_progress,
    collect_takeover,
    main,
    observation_document,
)

SESSION = "11111111-1111-4111-8111-111111111111"
RUN = "33333333-3333-4333-8333-333333333333"
BLOB = "44444444-4444-4444-8444-444444444444"


def _client(label: str, transport: httpx.MockTransport) -> AcceptanceHttpClient:
    return AcceptanceHttpClient(
        origin=f"https://{label}.example", credentials=AcceptanceCredentials(mode="bearer", bearer_token="test-only"), transport=transport
    )


def test_physical_sink_oracle_requires_real_unique_effects(tmp_path: Path) -> None:
    sink = tmp_path / "output.csv"
    oracle = PhysicalSinkOracle(sink, "row_id")
    with pytest.raises(AcceptanceCheckError):
        oracle.duplicates()
    for rows, expected in ((["a", "b"], 0), (["a", "a", "b"], 1)):
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["row_id"])
        writer.writerows([[row] for row in rows])
        sink.write_text(stream.getvalue())
        assert oracle.duplicates() == expected
    for content in ("row_id\n", "wrong\na\n", 'row_id\n""\n'):
        sink.write_text(content)
        with pytest.raises(AcceptanceCheckError):
            oracle.duplicates()


@pytest.mark.parametrize("mismatch", [False, True])
def test_progress_collects_real_http_surfaces_and_uploaded_bytes(tmp_path: Path, mismatch: bool) -> None:
    uploaded = b""
    calls: list[tuple[str, str]] = []
    message_content = ""

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded, message_content
        label = request.url.host.split(".")[0]
        path = request.url.path
        calls.append((label, path))
        headers = {"X-Elspeth-Instance": label}
        if path == "/api/system/status":
            return httpx.Response(200, json={}, headers=headers)
        if path.endswith("/blobs"):
            uploaded = request.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            return httpx.Response(201, json={"id": BLOB}, headers=headers)
        if path.endswith("/execute"):
            return httpx.Response(202, json={"run_id": RUN}, headers=headers)
        if path.endswith("/outputs"):
            return httpx.Response(
                200, json={"run_id": RUN, "artifacts": [{"artifact_id": "artifact-one", "content_hash": "a" * 64}]}, headers=headers
            )
        if path.endswith("/messages"):
            assistant = {"id": "55555555-5555-4555-8555-555555555555", "role": "assistant", "content": "Acknowledged"}
            if request.method == "POST":
                message_content = json.loads(request.content)["content"]
                return httpx.Response(200, json={"message": assistant}, headers=headers)
            return httpx.Response(200, json=[{"id": "new-user", "role": "user", "content": message_content}, assistant], headers=headers)
        if path.endswith("/content"):
            return httpx.Response(200, content=b"wrong" if mismatch and label == "b" else uploaded, headers=headers)
        assert path == f"/api/runs/{RUN}"
        return httpx.Response(200, json={"run_id": RUN, "status": "completed"}, headers=headers)

    transport = httpx.MockTransport(respond)
    with _client("a", transport) as owner, _client("b", transport) as reader:
        result = collect_progress(owner=owner, reader=reader, session_id=SESSION, capture=Capture(tmp_path / "evidence"), polling=Polling())
    admitted = cross_replica_progress_observation(observation_document(result))
    assert (decide_cross_replica_progress(admitted).outcome == "pass") is not mismatch
    for surface in (
        f"/api/runs/{RUN}",
        f"/api/runs/{RUN}/outputs",
        f"/api/sessions/{SESSION}/messages",
        f"/api/sessions/{SESSION}/blobs/{BLOB}/content",
    ):
        assert ("a", surface) in calls and ("b", surface) in calls
    captures = tuple((tmp_path / "evidence").iterdir())
    assert captures and all(path.stat().st_mode & 0o777 == 0o600 for path in captures)
    assert message_content


def test_progress_refuses_history_that_does_not_contain_the_acknowledged_write(tmp_path: Path) -> None:
    clock = _Clock()

    def respond(request: httpx.Request) -> httpx.Response:
        headers = {"X-Elspeth-Instance": request.url.host}
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json={}, headers=headers)
        if request.url.path.endswith("/blobs"):
            return httpx.Response(201, json={"id": BLOB}, headers=headers)
        if request.url.path.endswith("/messages"):
            if request.method == "POST":
                return httpx.Response(
                    200,
                    json={"message": {"id": "55555555-5555-4555-8555-555555555555", "role": "assistant", "content": "Acknowledged"}},
                    headers=headers,
                )
            return httpx.Response(200, json=[{"id": "yesterday", "role": "user", "content": "old message"}], headers=headers)
        if request.url.path.endswith("/execute"):
            return httpx.Response(202, json={"run_id": RUN}, headers=headers)
        if request.url.path.endswith("/outputs"):
            return httpx.Response(
                200, json={"run_id": RUN, "artifacts": [{"artifact_id": "one", "content_hash": "a" * 64}]}, headers=headers
            )
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"old", headers=headers)
        return httpx.Response(200, json={"run_id": RUN, "status": "completed"}, headers=headers)

    transport = httpx.MockTransport(respond)
    with (
        _client("a", transport) as owner,
        _client("b", transport) as reader,
        pytest.raises(AcceptanceCheckError, match="probe_observation_timeout"),
    ):
        collect_progress(
            owner=owner,
            reader=reader,
            session_id=SESSION,
            capture=Capture(tmp_path / "evidence"),
            polling=Polling(clock=clock.now, sleep=clock.sleep, timeout=30),
        )


@pytest.mark.parametrize("delayed_surface", ["messages", "status", "outputs", "terminal"])
def test_progress_keeps_visibility_delays_that_precede_slow_run_completion(tmp_path: Path, delayed_surface: str) -> None:
    uploaded = b""
    user_content = ""
    message_ack = run_ack = 0.0
    output_reads: list[float] = []
    interval = 0.02
    delay = 0.08
    run_duration = 0.20
    assistant = {"id": "55555555-5555-4555-8555-555555555555", "role": "assistant", "content": "Acknowledged"}

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal uploaded, user_content, message_ack, run_ack
        label = request.url.host.split(".")[0]
        path = request.url.path
        headers = {"X-Elspeth-Instance": label}
        now = time.monotonic()
        if path == "/api/system/status":
            return httpx.Response(200, json={}, headers=headers)
        if path.endswith("/blobs"):
            uploaded = request.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            return httpx.Response(201, json={"id": BLOB}, headers=headers)
        if path.endswith("/messages"):
            if request.method == "POST":
                user_content = json.loads(request.content)["content"]
                message_ack = now
                return httpx.Response(200, json={"message": assistant}, headers=headers)
            messages = [{"id": "yesterday", "role": "user", "content": "old history"}]
            if delayed_surface != "messages" or now - message_ack >= delay:
                messages.extend([{"id": "new-user", "role": "user", "content": user_content}, assistant])
            return httpx.Response(200, json=messages, headers=headers)
        if path.endswith("/execute"):
            run_ack = now
            return httpx.Response(202, json={"run_id": RUN}, headers=headers)
        if path.endswith("/outputs"):
            output_reads.append(now - run_ack)
            artifacts = (
                []
                if label == "b" and delayed_surface == "outputs" and now - run_ack < delay
                else [{"artifact_id": "one", "content_hash": "a" * 64}]
            )
            return httpx.Response(200, json={"run_id": RUN, "artifacts": artifacts}, headers=headers)
        if path.endswith("/content"):
            return httpx.Response(200, content=uploaded, headers=headers)
        assert path == f"/api/runs/{RUN}"
        if label == "b" and delayed_surface == "status" and now - run_ack < delay:
            return httpx.Response(404, json={"detail": "Run not found"}, headers=headers)
        terminal_after = run_duration + (delay if label == "b" and delayed_surface == "terminal" else 0)
        return httpx.Response(
            200, json={"run_id": RUN, "status": "completed" if now - run_ack >= terminal_after else "running"}, headers=headers
        )

    transport = httpx.MockTransport(respond)
    with _client("a", transport) as owner, _client("b", transport) as reader:
        observation = collect_progress(
            owner=owner,
            reader=reader,
            session_id=SESSION,
            capture=Capture(tmp_path / "evidence"),
            polling=Polling(interval=interval, timeout=30),
        )
    assert decide_cross_replica_progress(observation).outcome == "fail"
    if delayed_surface == "messages":
        assert observation.messages_visible_after_seconds > interval
    elif delayed_surface == "outputs":
        assert observation.outputs_visible_after_seconds > interval
    else:
        assert observation.status_visible_after_seconds > interval
    assert min(output_reads) < run_duration, "Outputs must be watched while the run is still active"


class _Clock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def now(self) -> float:
        return self.seconds

    def sleep(self, seconds: float) -> None:
        self.seconds += seconds


class _Partition(RoleRevocationPartition):
    def __init__(self, *, fail: bool = False) -> None:
        self.partitioned = False
        self.restored = False
        self.fail = fail

    def partition(self, role: str):
        assert role == "elspeth_runtime_a"
        self.partitioned = True
        if self.fail:
            raise AcceptanceCheckError("injected_partition_failure")

    def restore(self, role: str) -> None:
        assert role == "elspeth_runtime_a"
        self.restored = True


class _Database(SqlReader):
    def __init__(self, clock: _Clock, partition: _Partition, sink: Path) -> None:
        self.clock = clock
        self.partition = partition
        self.origin = datetime(2026, 9, 7, tzinfo=UTC)
        self.sink = sink

    def scalar(self, statement: str, **parameters: object) -> object:
        if statement == "SELECT clock_timestamp()":
            return self.origin + timedelta(seconds=self.clock.seconds)
        if "SELECT pipeline_yaml" in statement:
            assert parameters["run_id"] == RUN and parameters["session_id"] == SESSION
            return f"sinks:\n  output:\n    plugin: csv\n    options:\n      path: {self.sink}\n"
        assert "owner_instance_id FROM session_operation_fences" in statement
        return "b"

    def rows(self, statement: str, **parameters: object) -> tuple[tuple[object, ...], ...]:
        if "FROM runs" in statement:
            assert parameters["run_id"] == RUN and parameters["session_id"] == SESSION
            if self.clock.seconds > 10:
                return (("cancelled", "Orphaned by periodic cleanup — no active executor thread", "a"),)
            return (("running", None, "a"),)
        if "FROM web_instances" in statement:
            return (("a", "active", self.origin + timedelta(seconds=10)),)
        assert "released_at IS NULL" in statement and parameters["session_id"] == SESSION
        return (("a", self.origin + timedelta(seconds=10)),)


@pytest.mark.parametrize("failure", [None, "partition", "survivor", "historical_sink", "wrong_sink"])
def test_takeover_measures_held_lease_and_always_restores_role(tmp_path: Path, failure: str | None) -> None:
    clock = _Clock()
    partition = _Partition(fail=failure == "partition")
    sink = tmp_path / "outputs" / SESSION / "physical.csv"
    sink.parent.mkdir(parents=True)
    database = _Database(clock, partition, sink)
    if failure == "wrong_sink":
        database.sink = tmp_path / "unrelated.csv"
    if failure == "historical_sink":
        sink.write_text("row_id\nold-run-output\n")
    mutation_times: list[float] = []

    def respond(request: httpx.Request) -> httpx.Response:
        label = request.url.host.split(".")[0]
        headers = {"X-Elspeth-Instance": label}
        path = request.url.path
        if path == "/api/system/status":
            return httpx.Response(200, json={}, headers=headers)
        if path.endswith("/execute"):
            assert label == "a" and path == f"/api/sessions/{SESSION}/execute"
            sink.write_text("row_id\nrow-one\nrow-two\n")
            return httpx.Response(202, json={"run_id": RUN}, headers=headers)
        assert path == f"/api/sessions/{SESSION}/blobs/inline" and label == "b"
        if failure == "survivor":
            raise AcceptanceCheckError("injected_survivor_failure")
        mutation_times.append(clock.seconds)
        assert partition.partitioned and not partition.restored
        if clock.seconds <= 10:
            return httpx.Response(409, json={"detail": "Session operation is already active"}, headers=headers)
        return httpx.Response(201, json={"id": BLOB}, headers=headers)

    transport = httpx.MockTransport(respond)
    with _client("a", transport) as owner, _client("b", transport) as survivor:
        kwargs = {
            "owner": owner,
            "survivor": survivor,
            "session_id": SESSION,
            "sessions": database,
            "observer": PostgresEvidenceObserver(sessions=database, landscape=database),
            "partition": partition,
            "sink": PhysicalSinkOracle(sink, "row_id"),
            "capture": Capture(tmp_path / "evidence"),
            "polling": Polling(clock=clock.now, sleep=clock.sleep),
        }
        if failure == "partition":
            with pytest.raises(AcceptanceCheckError, match="injected_partition_failure"):
                collect_takeover(**kwargs)
        elif failure == "survivor":
            with pytest.raises(AcceptanceCheckError, match="injected_survivor_failure"):
                collect_takeover(**kwargs)
        elif failure == "historical_sink":
            with pytest.raises(AcceptanceInputError, match="fresh physical sink"):
                collect_takeover(**kwargs)
        elif failure == "wrong_sink":
            with pytest.raises(AcceptanceCheckError, match="probe_sink_binding"):
                collect_takeover(**kwargs)
        else:
            observation = collect_takeover(**kwargs)
            admitted = lease_takeover_observation(observation_document(observation))
            assert decide_lease_takeover(admitted).outcome == "pass"
            assert mutation_times[0] < 10 < mutation_times[1]
    assert partition.restored is (failure not in {"historical_sink", "wrong_sink"})
    assert all("test-only" not in path.read_text() for path in (tmp_path / "evidence").iterdir())


def test_cli_rejects_bad_identity_without_emitting_the_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    secret = "invalid-and-sensitive-id"
    assert (
        main(
            [
                "progress",
                "--session-id",
                secret,
                "--app-name",
                "elspeth-web",
                "--resource-group",
                "test-rg",
                "--default-domain",
                "example.internal",
                "--revision-suffix",
                "test",
                "--evidence-dir",
                str(tmp_path / "evidence"),
            ],
            environ={},
        )
        == 1
    )
    captured = capsys.readouterr()
    assert not captured.out and secret not in captured.err
    assert "acceptance_internal" in captured.err
