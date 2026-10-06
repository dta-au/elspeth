"""Fail closed on gateway SBOM/build identity before scans, signatures and smoke.

Buildx renders SLSA v1 predicates under each platform's ``SLSA`` key. The
builder is the GitHub run URL; BuildKit identifies its format in buildType.
These metadata checks do not replace the subsequent signature identity gate.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BUILDKIT_V1_BUILD_TYPE = "https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md"
GATEWAY_PLATFORMS = ("linux/amd64", "linux/arm64")


@dataclass(frozen=True, slots=True)
class GatewayBuildIdentity:
    """Expected identity supplied by the trusted GitHub workflow environment."""

    repository: str
    source_sha: str
    run_id: str
    run_attempt: str
    workflow_ref: str
    workflow_sha: str
    server_url: str = "https://github.com"

    def __post_init__(self) -> None:
        if self.server_url != "https://github.com":
            raise ValueError("gateway publication requires the GitHub.com build identity")
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None:
            raise ValueError("invalid expected repository")
        for label, value in (("source SHA", self.source_sha), ("workflow SHA", self.workflow_sha)):
            if re.fullmatch(r"[0-9a-f]{40}", value) is None:
                raise ValueError(f"invalid expected {label}")
        if any(re.fullmatch(r"[1-9][0-9]*", value) is None for value in (self.run_id, self.run_attempt)):
            raise ValueError("invalid expected run ID or attempt")
        if not self.workflow_ref.startswith(f"{self.repository}/.github/workflows/build-push.yaml@refs/"):
            raise ValueError("expected workflow must be build-push.yaml at an explicit Git ref")

    @property
    def repository_url(self) -> str:
        return f"{self.server_url}/{self.repository}"

    @property
    def builder_url(self) -> str:
        return f"{self.repository_url}/actions/runs/{self.run_id}/attempts/{self.run_attempt}"

    @property
    def gateway_source_url(self) -> str:
        return f"{self.repository_url}/tree/{self.source_sha}/gateway"


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _object(parent: Mapping[str, Any], key: str, label: str) -> Mapping[str, Any]:
    return _mapping(parent.get(key), f"{label}.{key}")


def _expect(parent: Mapping[str, Any], key: str, expected: str, label: str) -> None:
    if parent.get(key) != expected:
        raise ValueError(f"{label}.{key} does not match the expected build identity")


def _validate_platform(internal: Mapping[str, Any], platform: str) -> None:
    """Check target vertices, rather than the cross-build host's builderPlatform.

    The captured BuildKit v1 records omit targetPlatform. mode=max retains LLB
    target platforms; every platform-bearing gateway vertex must agree with its
    Buildx platform key. Platformless file vertices are legitimate.
    """
    if "targetPlatform" in internal:
        _expect(internal, "targetPlatform", platform, platform)
    config = _object(internal, "buildConfig", platform)
    vertices = config.get("llbDefinition")
    if not isinstance(vertices, list) or not vertices:
        raise ValueError(f"{platform}: mode=max LLB definition is missing")
    expected_os, expected_architecture = platform.split("/")
    measured = 0
    for vertex in vertices:
        operation = _object(_mapping(vertex, platform), "op", platform)
        if "platform" not in operation:
            continue
        target = _object(operation, "platform", platform)
        _expect(target, "OS", expected_os, platform)
        _expect(target, "Architecture", expected_architecture, platform)
        measured += 1
    if not measured:
        raise ValueError(f"{platform}: LLB definition contains no target platform")


def validate_gateway_attestations(
    *, sbom: object, provenance: object, identity: GatewayBuildIdentity, platforms: Sequence[str] = GATEWAY_PLATFORMS
) -> None:
    """Admit both platform records only when all recorded identities agree."""
    if len(platforms) != len(GATEWAY_PLATFORMS) or set(platforms) != set(GATEWAY_PLATFORMS):
        raise ValueError("qualification requires exactly linux/amd64 and linux/arm64")
    sbom_records = _mapping(sbom, "SBOM")
    provenance_records = _mapping(provenance, "provenance")
    for label, records in (("SBOM", sbom_records), ("provenance", provenance_records)):
        if set(records) != set(platforms):
            raise ValueError(f"{label} must contain exactly the expected platforms")
    for platform in platforms:
        document = _object(_mapping(sbom_records[platform], platform), "SPDX", platform)
        _expect(document, "SPDXID", "SPDXRef-DOCUMENT", platform)
        slsa = _object(_mapping(provenance_records[platform], platform), "SLSA", platform)
        definition = _object(slsa, "buildDefinition", platform)
        _expect(definition, "buildType", BUILDKIT_V1_BUILD_TYPE, platform)
        parameters = _object(definition, "externalParameters", platform)
        _expect(_object(parameters, "configSource", platform), "path", "Dockerfile", platform)
        request = _object(parameters, "request", platform)
        root = _object(request, "root", platform)
        _expect(_object(root, "configSource", platform), "path", "Dockerfile", platform)
        root_request = _object(root, "request", platform)
        for invocation in (request, root_request):
            args = _object(invocation, "args", platform)
            _expect(args, "build-arg:GATEWAY_REVISION", identity.source_sha, platform)
            _expect(args, "label:org.opencontainers.image.revision", identity.source_sha, platform)
            _expect(args, "label:org.opencontainers.image.source", identity.gateway_source_url, platform)
        root_args = _object(root_request, "args", platform)
        for key, value in (
            ("vcs:revision", identity.source_sha),
            ("vcs:source", identity.repository_url),
            ("vcs:localdir:context", "gateway"),
            ("vcs:localdir:dockerfile", "gateway"),
        ):
            _expect(root_args, key, value, platform)
        internal = _object(definition, "internalParameters", platform)
        _validate_platform(internal, platform)
        for key, value in (
            ("github_repository", identity.repository),
            ("github_server_url", identity.server_url),
            ("github_run_id", identity.run_id),
            ("github_run_attempt", identity.run_attempt),
            ("github_workflow_ref", identity.workflow_ref),
            ("github_workflow_sha", identity.workflow_sha),
            ("github_job", "build-push"),
        ):
            _expect(internal, key, value, platform)
        run = _object(slsa, "runDetails", platform)
        _expect(_object(run, "builder", platform), "id", identity.builder_url, platform)
        metadata = _object(run, "metadata", platform)
        vcs = _object(_object(metadata, "buildkit_metadata", platform), "vcs", platform)
        for key, value in (
            ("revision", identity.source_sha),
            ("source", identity.repository_url),
            ("localdir:context", "gateway"),
            ("localdir:dockerfile", "gateway"),
        ):
            _expect(vcs, key, value, platform)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--workflow-ref", required=True)
    parser.add_argument("--workflow-sha", required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--platforms", required=True)
    args = parser.parse_args(argv)
    try:
        identity = GatewayBuildIdentity(
            repository=args.repository,
            source_sha=args.source_sha,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            workflow_ref=args.workflow_ref,
            workflow_sha=args.workflow_sha,
            server_url=args.server_url,
        )
        validate_gateway_attestations(
            sbom=json.loads(args.sbom.read_text(encoding="utf-8")),
            provenance=json.loads(args.provenance.read_text(encoding="utf-8")),
            identity=identity,
            platforms=args.platforms.split(","),
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"gateway attestation qualification failed: {exc}\n")
    print("Gateway SBOM and SLSA v1 identities verified for linux/amd64 and linux/arm64")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
