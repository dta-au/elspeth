"""Run P1/P4a through actual cookie affinity on the production Single revision.

ACA's HTTP cookie affinity is available only in Single mode and may reroute
clients after replica loss (learn.microsoft.com/azure/container-apps/sticky-sessions).
Every response must retain the discovered instance, including binary downloads
and multipart uploads. These probes never substitute revision-label routes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from elspeth.web._acceptance_common.errors import AcceptanceCheckError, AcceptanceInputError
from elspeth.web._acceptance_common.http_client import (
    _NO_BODY,
    AcceptanceHttpClient,
    normalize_acceptance_origin,
)
from elspeth.web._acceptance_common.replica_probes import (
    EvidenceObserver,
    ProbeRequest,
    ProbeResult,
    ReplicaAddress,
    ReplicaController,
    ReplicaProbeDriver,
    decide_cross_replica_progress,
    decide_fence_conflict,
    record_owner_affine_progress,
    require_contention_trials,
)
from elspeth.web._azure_container_apps_acceptance.evidence import project_replica_names
from elspeth.web._azure_container_apps_acceptance.receipt_contracts import (
    CheckDetails,
    ReplicaBinding,
    SingleRevisionFenceDetails,
    SingleRevisionProgressDetails,
    SingleRevisionTopologyDetails,
    encode_exec_receipt,
)
from elspeth.web.azure_container_apps_acceptance import (
    _document,
    _fence_trial_requests,
    _list_document,
    _observer,
    acceptance_error_envelope,
)
from elspeth.web.azure_container_apps_observations import Capture, Polling, collect_progress, observation_document


class _PlatformModel(BaseModel):
    model_config = ConfigDict(strict=True)


class _Sticky(_PlatformModel):
    affinity: Literal["sticky"]


class _Ingress(_PlatformModel):
    fqdn: str
    transport: Literal["auto", "http", "http2"]
    stickySessions: _Sticky


class _Configuration(_PlatformModel):
    activeRevisionsMode: Literal["Single"]
    ingress: _Ingress


class _Scale(_PlatformModel):
    minReplicas: Literal[2]
    maxReplicas: Literal[2]


class _Template(_PlatformModel):
    scale: _Scale


class _Properties(_PlatformModel):
    configuration: _Configuration
    template: _Template
    latestRevisionName: str
    latestReadyRevisionName: str


class _App(_PlatformModel):
    id: str
    name: str
    properties: _Properties


class _SystemIdentity(_PlatformModel):
    instance_id: str
    deployment_target: Literal["azure-container-apps"]
    deployment_revision: str
    deployment_replica: str


@dataclass(frozen=True)
class SingleTopology:
    container_app_id: str
    revision: str
    origin: str
    replicas: tuple[str, str]

    @classmethod
    def admit(cls, app_document: object, replicas_document: object, *, revision: str) -> SingleTopology:
        app = _App.model_validate(app_document)
        replicas = project_replica_names(replicas_document)
        if (
            len(replicas) != 2
            or app.properties.latestRevisionName != revision
            or app.properties.latestReadyRevisionName != revision
            or app.name != app.id.rsplit("/", 1)[1]
        ):
            raise AcceptanceCheckError("single_revision_topology")
        for replica in replicas:
            ReplicaBinding(app.id, revision, replica)
        origin = normalize_acceptance_origin(f"https://{app.properties.configuration.ingress.fqdn}")
        if not origin.startswith(f"https://{app.name}."):
            raise AcceptanceCheckError("single_revision_default_origin")
        return cls(app.id, revision, origin, (replicas[0], replicas[1]))


class AffinityClient(AcceptanceHttpClient):
    """One real cookie jar, pinned to the first answering process for its lifetime."""

    instance_id: str | None = None
    replica_name: str | None = None

    def _request_bounded(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: set[int],
        json_body: object = _NO_BODY,
        files: Mapping[str, tuple[str, bytes, str]] | None = None,
        limit: int,
    ) -> tuple[int, str | None, bytes]:
        status, instance, content = super()._request_bounded(
            method, path, expected_statuses=expected_statuses, json_body=json_body, files=files, limit=limit
        )
        if instance is None:
            raise AcceptanceCheckError("single_revision_instance_missing")
        if self.instance_id is not None and instance != self.instance_id:
            raise AcceptanceCheckError("single_revision_instance_changed")
        self.instance_id = instance
        return status, instance, content

    def confirm(self, topology: SingleTopology) -> str:
        body = self.request_json("GET", "/api/system/status", expected_statuses={200})
        identity = _SystemIdentity.model_validate(body)
        instance = self.instance_id
        if (
            instance is None
            or identity.instance_id != instance
            or identity.deployment_revision != topology.revision
            or identity.deployment_replica not in topology.replicas
            or (self.replica_name is not None and self.replica_name != identity.deployment_replica)
        ):
            raise AcceptanceCheckError("single_revision_replica_binding")
        self.replica_name = identity.deployment_replica
        # Require cookies applicable to this exact HTTPS origin, without
        # manufacturing a cookie or assuming an undocumented ACA cookie name.
        request = self._client.build_request("GET", "/api/system/status")
        if "Cookie" not in request.headers or not request.headers["Cookie"]:
            raise AcceptanceCheckError("single_revision_affinity_cookie_missing")
        return instance


@contextmanager
def discover_pair(
    topology: SingleTopology, factory: Callable[[], AffinityClient], *, attempts: int = 32
) -> Iterator[tuple[AffinityClient, AffinityClient]]:
    """Bounded independent cookie jars until two actual, stable replicas answer."""

    if not 2 <= attempts <= 64:
        raise AcceptanceInputError("affinity discovery attempts must be between 2 and 64")
    with ExitStack() as stack:
        first: AffinityClient | None = None
        for _ in range(attempts):
            client = stack.enter_context(factory())
            if client.origin != topology.origin or client is first:
                raise AcceptanceCheckError("single_revision_client_isolation")
            client.authenticate(register=False)
            instance = client.confirm(topology)
            client.confirm(topology)
            if first is None:
                first = client
            elif instance != first.instance_id:
                if client.replica_name == first.replica_name:
                    raise AcceptanceCheckError("single_revision_replica_binding")
                first.confirm(topology)
                yield first, client
                first.confirm(topology)
                client.confirm(topology)
                return
        raise AcceptanceCheckError("single_revision_distinct_replicas_not_observed")


@dataclass(frozen=True)
class _AffinityController(ReplicaController):
    first: ReplicaAddress
    second: ReplicaAddress

    def replicas(self) -> tuple[ReplicaAddress, ReplicaAddress]:
        return self.first, self.second

    def partition_owner(self, replica: str) -> None:
        raise AcceptanceInputError("Single revision follow-up does not inject faults")

    def stop_owner(self, replica: str) -> None:
        raise AcceptanceInputError("Single revision follow-up does not inject faults")

    def restore_owner(self, replica: str) -> None:
        raise AcceptanceInputError("Single revision follow-up does not inject faults")


def run_fence_trials(
    clients: tuple[AffinityClient, AffinityClient], observer: EvidenceObserver, requests: tuple[tuple[str, object], ...]
) -> ProbeResult:
    required = require_contention_trials(len(requests))
    if len({session for session, _ in requests}) != required:
        raise AcceptanceInputError("guided contention trials require distinct fresh sessions")
    first, second = clients
    if first.instance_id is None or second.instance_id is None:
        raise AcceptanceCheckError("single_revision_instances_unbound")

    def unused_factory(origin: str) -> AcceptanceHttpClient:
        raise AssertionError("Single revision driver must reuse authenticated cookie jars")

    driver = ReplicaProbeDriver(
        controller=_AffinityController(ReplicaAddress(first.instance_id, first.origin), ReplicaAddress(second.instance_id, second.origin)),
        observer=observer,
        client_factory=unused_factory,
        pinned_clients=clients,
    )
    trials = [
        driver.fence_conflict_trial(session, ProbeRequest("POST", f"/api/sessions/{session}/guided/respond", body))
        for session, body in requests
    ]
    return decide_fence_conflict(trials, required_trials=required)


def _write_binding(directory: Path, binding: ReplicaBinding) -> None:
    descriptor = os.open(directory / "binding.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"container_app_id": binding.container_app_id, "revision": binding.revision, "replica": binding.replica}, stream)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", required=True, choices=("P1", "P4a"))
    parser.add_argument("--app-json", required=True)
    parser.add_argument("--replicas-json", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--candidate-sha", required=True, type=_candidate_sha)
    parser.add_argument("--scenario-id", choices=("A",), default="A")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--trial-requests")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--session-id")
    parser.add_argument("--discovery-attempts", type=int, default=32)
    return parser


def _candidate_sha(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise argparse.ArgumentTypeError("candidate SHA must be 40 lowercase hexadecimal characters")
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        topology = SingleTopology.admit(_document(args.app_json), _list_document(args.replicas_json), revision=args.revision)
        requests: tuple[tuple[str, object], ...] = ()
        if args.probe == "P1":
            require_contention_trials(args.trials)
            if args.trial_requests is None:
                raise AcceptanceInputError("P1 requires --trial-requests")
            requests = _fence_trial_requests(args.trial_requests, trials=args.trials)
        elif args.session_id is None:
            raise AcceptanceInputError("P4a requires --session-id")
        else:
            args.session_id = str(UUID(args.session_id))
        capture = Capture(args.evidence_dir)
        env = dict(os.environ)
        env["ELSPETH_ACCEPTANCE_BASE_URL"] = topology.origin

        def factory() -> AffinityClient:
            return AffinityClient.from_env(env)

        details: CheckDetails
        with discover_pair(topology, factory, attempts=args.discovery_attempts) as clients:
            owner, reader = clients
            if owner.replica_name is None or reader.replica_name is None or owner.instance_id is None or reader.instance_id is None:
                raise AcceptanceCheckError("single_revision_instances_unbound")
            binding = ReplicaBinding(topology.container_app_id, topology.revision, owner.replica_name)
            reader_binding = ReplicaBinding(topology.container_app_id, topology.revision, reader.replica_name)
            topology_details: SingleRevisionTopologyDetails = {
                "container_app_id": topology.container_app_id,
                "active_revisions_mode": "Single",
                "session_affinity": "sticky",
                "min_replicas": 2,
                "max_replicas": 2,
                "revision": topology.revision,
                "replicas": [
                    {"instance_id": owner.instance_id, "replica": owner.replica_name, "replica_binding_sha256": binding.sha256},
                    {"instance_id": reader.instance_id, "replica": reader.replica_name, "replica_binding_sha256": reader_binding.sha256},
                ],
            }
            if args.probe == "P1":
                result = run_fence_trials(clients, _observer(env), requests)
                check = "single-revision-fence-conflict"
                fence: SingleRevisionFenceDetails = {**result.to_receipt_details(), "topology": topology_details}
                details = fence
            else:
                observation = collect_progress(owner=owner, reader=reader, session_id=args.session_id, capture=capture, polling=Polling())
                capture.write("progress", observation_document(observation))
                result = decide_cross_replica_progress(observation)
                progress: SingleRevisionProgressDetails = {
                    **result.to_receipt_details(),
                    "owner_affine": record_owner_affine_progress(mitigation="single_revision_sticky_sessions").to_receipt_details(),
                    "topology": topology_details,
                }
                check = "single-revision-progress"
                details = progress
            capture.write("result", details)
            receipt = encode_exec_receipt(check, details, candidate_sha=args.candidate_sha, binding=binding, scenario_id=args.scenario_id)
        # Final affinity confirmations run before any successful receipt is emitted.
        _write_binding(args.evidence_dir, binding)
        sys.stdout.write(receipt + "\n")
        return 0 if result.outcome == "pass" else 1
    except Exception as exc:
        sys.stderr.write(json.dumps(acceptance_error_envelope(exc)) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
