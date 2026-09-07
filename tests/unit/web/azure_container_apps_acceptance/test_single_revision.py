"""Actual cookie routing, identity loss and production topology admission."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

import elspeth.web.azure_container_apps_single_revision as single_revision
from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.http_client import AcceptanceCredentials
from elspeth.web._acceptance_common.replica_probes import SESSION_OPERATION_CONFLICT_DETAIL, EvidenceObserver, MembershipRow
from elspeth.web._azure_container_apps_acceptance.receipt_contracts import ReplicaBinding, extract_exec_receipt
from elspeth.web.azure_container_apps_single_revision import AffinityClient, SingleTopology, discover_pair, main, run_fence_trials

APP_ID = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/test/providers/Microsoft.App/containerApps/web"
REVISION = "web--production"
REPLICAS = (f"{REVISION}-aaa", f"{REVISION}-bbb")
INSTANCES = ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002")
ORIGIN = "https://web.example.azurecontainerapps.io"
TOPOLOGY = SingleTopology(APP_ID, REVISION, ORIGIN, REPLICAS)


def app_document() -> dict:
    return {
        "id": APP_ID,
        "name": "web",
        "properties": {
            "latestRevisionName": REVISION,
            "latestReadyRevisionName": REVISION,
            "configuration": {
                "activeRevisionsMode": "Single",
                "ingress": {"fqdn": ORIGIN.removeprefix("https://"), "transport": "auto", "stickySessions": {"affinity": "sticky"}},
            },
            "template": {"scale": {"minReplicas": 2, "maxReplicas": 2}},
        },
    }


def test_admits_current_production_single_revision() -> None:
    assert SingleTopology.admit(app_document(), [{"name": name} for name in REPLICAS], revision=REVISION) == TOPOLOGY


@pytest.mark.parametrize("fault", ["multiple", "no_affinity", "scale", "not_ready", "labels", "wrong_replica", "one_replica"])
def test_refuses_wrong_topology(fault: str) -> None:
    app = app_document()
    replicas = [{"name": name} for name in REPLICAS]
    match fault:
        case "multiple":
            app["properties"]["configuration"]["activeRevisionsMode"] = "Multiple"
        case "no_affinity":
            app["properties"]["configuration"]["ingress"]["stickySessions"]["affinity"] = "none"
        case "scale":
            app["properties"]["template"]["scale"]["maxReplicas"] = 4
        case "not_ready":
            app["properties"]["latestReadyRevisionName"] = "web--old"
        case "labels":
            app["properties"]["configuration"]["ingress"]["fqdn"] = "web---probe-a.example.azurecontainerapps.io"
        case "wrong_replica":
            replicas[1]["name"] = "web--old-bbb"
        case "one_replica":
            replicas.pop()
    with pytest.raises((AcceptanceCheckError, AcceptanceInputError, ValidationError)):
        SingleTopology.admit(app, replicas, revision=REVISION)


class Routing:
    def __init__(self, assignments: tuple[str, ...] = REPLICAS) -> None:
        self.assignments = assignments
        self.created = 0
        self.cookies = True
        self.override: str | None = None
        self.identity_fault: str | None = None
        self.uploaded = b""
        self.message_content = ""
        self.paths: list[tuple[str, str]] = []
        self.observer = Observer()
        self.lock = threading.Lock()
        self.won: dict[str, threading.Event] = {}

    def factory(self) -> AffinityClient:
        assigned = self.assignments[min(self.created, len(self.assignments) - 1)]
        self.created += 1

        def handle(request: httpx.Request) -> httpx.Response:
            cookie = request.headers.get("Cookie", "")
            if cookie:
                assert cookie == f"affinity={assigned}"
            self.paths.append((assigned, request.url.path))
            instance = INSTANCES[REPLICAS.index(assigned)]
            headers = {"X-Elspeth-Instance": self.override or instance}
            if self.cookies:
                headers["Set-Cookie"] = f"affinity={assigned}; Path=/; Secure; HttpOnly"
            if request.url.path.endswith("/blobs"):
                self.uploaded = request.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
                return httpx.Response(201, json={"id": str(uuid4())}, headers=headers)
            if request.url.path.endswith("/messages"):
                assistant = {"id": INSTANCES[0], "role": "assistant", "content": "Acknowledged"}
                if request.method == "POST":
                    self.message_content = json.loads(request.content)["content"]
                    return httpx.Response(200, json={"message": assistant}, headers=headers)
                return httpx.Response(
                    200, json=[{"id": "new-user", "role": "user", "content": self.message_content}, assistant], headers=headers
                )
            if request.url.path.endswith("/execute"):
                return httpx.Response(202, json={"run_id": INSTANCES[1]}, headers=headers)
            if request.url.path.endswith("/outputs"):
                return httpx.Response(
                    200, json={"run_id": INSTANCES[1], "artifacts": [{"artifact_id": "one", "content_hash": "a" * 64}]}, headers=headers
                )
            if request.url.path.endswith("/content"):
                return httpx.Response(200, content=self.uploaded, headers=headers)
            if request.url.path.startswith("/api/runs/"):
                return httpx.Response(200, json={"run_id": INSTANCES[1], "status": "completed"}, headers=headers)
            if request.url.path.endswith("/guided/respond"):
                session = request.url.path.split("/")[3]
                with self.lock:
                    event = self.won.setdefault(session, threading.Event())
                if json.loads(request.content)["winner"] != assigned:
                    assert event.wait(2)
                with self.lock:
                    if session in self.observer.owners:
                        return httpx.Response(409, json={"detail": SESSION_OPERATION_CONFLICT_DETAIL}, headers=headers)
                    self.observer.owners[session] = instance
                    event.set()
                    return httpx.Response(200, json={}, headers=headers)
            return httpx.Response(
                200,
                json={
                    "instance_id": instance,
                    "deployment_target": "azure-container-apps",
                    "deployment_revision": "web--old" if self.identity_fault == "revision" else REVISION,
                    "deployment_replica": REPLICAS[0] if self.identity_fault == "same_replica" else assigned,
                },
                headers=headers,
            )

        return AffinityClient(
            origin=ORIGIN,
            credentials=AcceptanceCredentials(mode="bearer", bearer_token="acceptance-test"),
            transport=httpx.MockTransport(handle),
        )


class Observer(EvidenceObserver):
    def __init__(self) -> None:
        self.owners: dict[str, str] = {}

    def fence_epoch(self, session_id: str) -> int:
        return 1 if session_id in self.owners else 0

    def fence_owner(self, session_id: str) -> str | None:
        return self.owners[session_id] if session_id in self.owners else None

    def guided_operation_rows(self, session_id: str, *, since_epoch: int) -> int:
        return 1 if session_id in self.owners else 0

    def runs_row_ids(self, session_id: str) -> tuple[str, ...]:
        return ()

    def landscape_run_ids(self, session_id: str) -> tuple[str, ...]:
        return ()

    def membership_row(self, instance_id: str) -> MembershipRow | None:
        return None


def test_discovery_discards_duplicate_routes_and_retains_cookie_jars_for_all_twenty_trials() -> None:
    routing = Routing((REPLICAS[0], REPLICAS[0], REPLICAS[1]))
    requests = tuple((str(uuid4()), {"operation_id": str(uuid4()), "winner": REPLICAS[i % 2]}) for i in range(20))
    with discover_pair(TOPOLOGY, routing.factory, attempts=3) as clients:
        assert clients[0].instance_id == INSTANCES[0]
        assert clients[1].instance_id == INSTANCES[1]
        result = run_fence_trials(clients, routing.observer, requests)
        assert result.outcome == "pass", result.reasons
        assert routing.created == 3
    assert len([path for _, path in routing.paths if path.endswith("/guided/respond")]) == 40


def test_discovery_is_bounded_when_all_cookies_route_to_one_replica() -> None:
    routing = Routing((REPLICAS[0],))
    with (
        pytest.raises(AcceptanceCheckError, match="single_revision_distinct_replicas_not_observed"),
        discover_pair(TOPOLOGY, routing.factory, attempts=4),
    ):
        pytest.fail("one actual replica cannot qualify as two")
    assert routing.created == 4


def test_discovery_requires_an_applicable_cookie() -> None:
    routing = Routing()
    routing.cookies = False
    with pytest.raises(AcceptanceCheckError, match="single_revision_affinity_cookie_missing"), discover_pair(TOPOLOGY, routing.factory):
        pytest.fail("cookie-free route is not affinity")


@pytest.mark.parametrize("operation", ["json", "multipart", "bytes", "final_confirmation"])
def test_every_response_checks_routing_identity(operation: str) -> None:
    routing = Routing()
    with pytest.raises(AcceptanceCheckError, match="single_revision_instance_changed"), discover_pair(TOPOLOGY, routing.factory) as clients:
        routing.override = INSTANCES[1]
        match operation:
            case "json":
                clients[0].request_json("GET", "/api/system/status", expected_statuses={200})
            case "multipart":
                clients[0].request_multipart_json("POST", "/upload", expected_statuses={200}, files={"file": ("x.txt", b"x", "text/plain")})
            case "bytes":
                clients[0].request_bytes("GET", "/blob", expected_statuses={200})
            case "final_confirmation":
                pass


@pytest.mark.parametrize("sessions", [19, 20])
def test_p1_refuses_insufficient_or_repeated_prepared_sessions(sessions: int) -> None:
    routing = Routing()
    requests = tuple(("same-session", {}) for _ in range(sessions))
    with discover_pair(TOPOLOGY, routing.factory) as clients, pytest.raises(AcceptanceInputError):
        run_fence_trials(clients, routing.observer, requests)
    assert all(not path.endswith("/guided/respond") for _, path in routing.paths)


@pytest.mark.parametrize("fault", ["same_replica", "revision"])
def test_process_ids_must_join_distinct_current_platform_replicas(fault: str) -> None:
    routing = Routing()
    routing.identity_fault = fault
    with pytest.raises(AcceptanceCheckError, match="single_revision_replica_binding"), discover_pair(TOPOLOGY, routing.factory):
        pytest.fail("distinct process UUIDs do not replace platform binding")


@pytest.mark.parametrize("probe", ["P1", "P4a"])
def test_cli_emits_distinct_single_receipt_and_topology_after_real_cookie_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], probe: str
) -> None:
    routing = Routing()
    monkeypatch.setattr(AffinityClient, "from_env", lambda env: routing.factory())
    monkeypatch.setattr(single_revision, "_observer", lambda env: routing.observer)
    app = tmp_path / "app.json"
    app.write_text(json.dumps(app_document()))
    app.chmod(0o600)
    replicas = tmp_path / "replicas.json"
    replicas.write_text(json.dumps([{"name": name} for name in REPLICAS]))
    evidence = tmp_path / "evidence"
    requests = tmp_path / "requests.json"
    requests.write_text(
        json.dumps([{"session_id": str(uuid4()), "body": {"operation_id": str(uuid4()), "winner": REPLICAS[i % 2]}} for i in range(20)])
    )
    exit_code = main(
        [
            "--probe",
            probe,
            "--app-json",
            str(app),
            "--replicas-json",
            str(replicas),
            "--revision",
            REVISION,
            "--candidate-sha",
            "a" * 40,
            "--session-id",
            str(uuid4()),
            "--trial-requests",
            str(requests),
            "--evidence-dir",
            str(evidence),
        ]
    )
    output = capsys.readouterr()
    assert exit_code == 0, output.err
    receipt = extract_exec_receipt(
        output.out,
        expected_candidate_sha="a" * 40,
        expected_binding=ReplicaBinding(APP_ID, REVISION, REPLICAS[0]),
        expected_scenario_id="A",
        expected_check="single-revision-progress" if probe == "P4a" else "single-revision-fence-conflict",
    )
    document = json.loads(receipt.canonical_json)
    assert document["details"]["outcome"] == "pass"
    assert document["details"]["topology"] == {
        "active_revisions_mode": "Single",
        "session_affinity": "sticky",
        "min_replicas": 2,
        "max_replicas": 2,
        "revision": REVISION,
        "replicas": [
            {
                "instance_id": INSTANCES[0],
                "replica": REPLICAS[0],
                "replica_binding_sha256": ReplicaBinding(APP_ID, REVISION, REPLICAS[0]).sha256,
            },
            {
                "instance_id": INSTANCES[1],
                "replica": REPLICAS[1],
                "replica_binding_sha256": ReplicaBinding(APP_ID, REVISION, REPLICAS[1]).sha256,
            },
        ],
    }
    assert "topology" not in document["details"]["evidence"]
    assert json.loads((evidence / "binding.json").read_text()) == {
        "container_app_id": APP_ID,
        "revision": REVISION,
        "replica": REPLICAS[0],
    }
    assert (evidence / "binding.json").stat().st_mode & 0o777 == 0o600
    assert routing.created == 2
    if probe == "P4a":
        assert document["details"]["owner_affine"]["outcome"] == "cannot_pass"
        assert routing.uploaded and routing.message_content
        for replica in REPLICAS:
            assert any(actual == replica and path.endswith("/content") for actual, path in routing.paths)
            assert any(actual == replica and path.endswith("/outputs") for actual, path in routing.paths)
    else:
        assert len([path for _, path in routing.paths if path.endswith("/guided/respond")]) == 40


def test_invalid_candidate_refused_before_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_factory(env: object) -> AffinityClient:
        pytest.fail("invalid candidate must not reach HTTP")

    monkeypatch.setattr(AffinityClient, "from_env", forbidden_factory)
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "--probe",
                "P4a",
                "--app-json",
                "unused",
                "--replicas-json",
                "unused",
                "--revision",
                REVISION,
                "--candidate-sha",
                "invalid",
                "--evidence-dir",
                "unused",
            ]
        )
    assert exc.value.code == 2
