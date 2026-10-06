"""Real BuildKit records and fail-closed gateway identity regression controls."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts.cicd.check_release_required_checks import main as release_main

from elspeth_lints.release.gateway_attestations import GatewayBuildIdentity, validate_gateway_attestations

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE_SHA = "edc844699a350a90a624089e12a5a1e75b7b2dde"
IDENTITY = GatewayBuildIdentity(
    repository="dta-au/elspeth",
    source_sha=SOURCE_SHA,
    run_id="37348491709",
    run_attempt="2",
    workflow_ref="dta-au/elspeth/.github/workflows/build-push.yaml@refs/heads/main",
    workflow_sha=SOURCE_SHA,
)


@pytest.fixture
def attestations() -> tuple[dict[str, Any], dict[str, Any]]:
    sbom = json.loads((FIXTURES / "gateway-sbom-document-ids.json").read_text(encoding="utf-8"))
    provenance = json.loads((FIXTURES / "gateway-buildkit-slsa-v1.json").read_text(encoding="utf-8"))
    return sbom, provenance


def test_captured_actual_slsa_v1_build_is_admitted(attestations: tuple[dict[str, Any], dict[str, Any]]) -> None:
    sbom, provenance = attestations
    assert "https://mobyproject.org/buildkit" not in json.dumps(provenance)
    validate_gateway_attestations(sbom=sbom, provenance=provenance, identity=IDENTITY)


@pytest.mark.parametrize("document", ["sbom", "provenance"])
@pytest.mark.parametrize("mutation", ["missing", "wrong", "extra", "empty", "null"])
def test_every_expected_platform_is_required(attestations: tuple[dict[str, Any], dict[str, Any]], document: str, mutation: str) -> None:
    sbom, provenance = attestations
    value = sbom if document == "sbom" else provenance
    if mutation == "missing":
        value.pop("linux/arm64")
    elif mutation == "wrong":
        value["linux/riscv64"] = value.pop("linux/arm64")
    elif mutation == "extra":
        value["linux/riscv64"] = copy.deepcopy(value["linux/arm64"])
    elif mutation == "empty":
        value["linux/arm64"] = {}
    else:
        value["linux/arm64"] = None
    with pytest.raises(ValueError):
        validate_gateway_attestations(sbom=sbom, provenance=provenance, identity=IDENTITY)


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
@pytest.mark.parametrize(
    "path",
    [
        ("buildDefinition", "buildType"),
        ("runDetails", "builder", "id"),
        ("buildDefinition", "externalParameters", "configSource", "path"),
        ("buildDefinition", "externalParameters", "request", "root", "configSource", "path"),
        ("buildDefinition", "externalParameters", "request", "args", "build-arg:GATEWAY_REVISION"),
        ("buildDefinition", "externalParameters", "request", "args", "label:org.opencontainers.image.revision"),
        ("buildDefinition", "externalParameters", "request", "args", "label:org.opencontainers.image.source"),
        ("buildDefinition", "externalParameters", "request", "root", "request", "args", "build-arg:GATEWAY_REVISION"),
        ("buildDefinition", "externalParameters", "request", "root", "request", "args", "label:org.opencontainers.image.revision"),
        ("buildDefinition", "externalParameters", "request", "root", "request", "args", "label:org.opencontainers.image.source"),
        ("buildDefinition", "externalParameters", "request", "root", "request", "args", "vcs:revision"),
        ("buildDefinition", "externalParameters", "request", "root", "request", "args", "vcs:source"),
        ("buildDefinition", "externalParameters", "request", "root", "request", "args", "vcs:localdir:context"),
        ("buildDefinition", "externalParameters", "request", "root", "request", "args", "vcs:localdir:dockerfile"),
        ("buildDefinition", "internalParameters", "github_repository"),
        ("buildDefinition", "internalParameters", "github_server_url"),
        ("buildDefinition", "internalParameters", "github_run_id"),
        ("buildDefinition", "internalParameters", "github_run_attempt"),
        ("buildDefinition", "internalParameters", "github_workflow_ref"),
        ("buildDefinition", "internalParameters", "github_workflow_sha"),
        ("buildDefinition", "internalParameters", "github_job"),
        ("runDetails", "metadata", "buildkit_metadata", "vcs", "revision"),
        ("runDetails", "metadata", "buildkit_metadata", "vcs", "source"),
        ("runDetails", "metadata", "buildkit_metadata", "vcs", "localdir:context"),
        ("runDetails", "metadata", "buildkit_metadata", "vcs", "localdir:dockerfile"),
    ],
)
@pytest.mark.parametrize("mutation", ["missing", "wrong"])
def test_all_identity_copies_must_agree(
    attestations: tuple[dict[str, Any], dict[str, Any]], platform: str, path: tuple[str, ...], mutation: str
) -> None:
    sbom, provenance = attestations
    target = provenance[platform]["SLSA"]
    for part in path[:-1]:
        target = target[part]
    if mutation == "missing":
        target.pop(path[-1])
    else:
        target[path[-1]] = "unrelated"
    # The correct SHA still exists elsewhere: substring matching would admit it.
    assert SOURCE_SHA in json.dumps(provenance)
    with pytest.raises(ValueError):
        validate_gateway_attestations(sbom=sbom, provenance=provenance, identity=IDENTITY)


def test_swapped_architecture_predicates_are_rejected(attestations: tuple[dict[str, Any], dict[str, Any]]) -> None:
    sbom, provenance = attestations
    provenance["linux/arm64"] = copy.deepcopy(provenance["linux/amd64"])
    with pytest.raises(ValueError, match="Architecture"):
        validate_gateway_attestations(sbom=sbom, provenance=provenance, identity=IDENTITY)


@pytest.mark.parametrize("mutation", ["missing", "empty", "wrong", "target"])
def test_missing_or_wrong_llb_platform_is_rejected(attestations: tuple[dict[str, Any], dict[str, Any]], mutation: str) -> None:
    sbom, provenance = attestations
    internal = provenance["linux/arm64"]["SLSA"]["buildDefinition"]["internalParameters"]
    if mutation == "missing":
        internal.pop("buildConfig")
    elif mutation == "empty":
        internal["buildConfig"]["llbDefinition"] = []
    elif mutation == "target":
        internal["targetPlatform"] = "linux/amd64"
    else:
        for vertex in internal["buildConfig"]["llbDefinition"]:
            if "platform" in vertex["op"]:
                vertex["op"]["platform"]["OS"] = "windows"
                break
    with pytest.raises(ValueError):
        validate_gateway_attestations(sbom=sbom, provenance=provenance, identity=IDENTITY)


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
def test_spdx_document_marker_is_required_for_each_platform(attestations: tuple[dict[str, Any], dict[str, Any]], platform: str) -> None:
    sbom, provenance = attestations
    sbom[platform]["SPDX"]["SPDXID"] = "SPDXRef-unrelated"
    with pytest.raises(ValueError, match="SPDXID"):
        validate_gateway_attestations(sbom=sbom, provenance=provenance, identity=IDENTITY)


def _cli_args(sbom: Path, provenance: Path) -> list[str]:
    return [
        "--sbom",
        str(sbom),
        "--provenance",
        str(provenance),
        "--repository",
        IDENTITY.repository,
        "--source-sha",
        IDENTITY.source_sha,
        "--run-id",
        IDENTITY.run_id,
        "--run-attempt",
        IDENTITY.run_attempt,
        "--workflow-ref",
        IDENTITY.workflow_ref,
        "--workflow-sha",
        IDENTITY.workflow_sha,
        "--server-url",
        IDENTITY.server_url,
        "--platforms",
        "linux/amd64,linux/arm64",
    ]


def test_workflow_cli_admits_capture_and_exits_nonzero_for_wrong_sha(tmp_path: Path) -> None:
    sbom = FIXTURES / "gateway-sbom-document-ids.json"
    provenance = FIXTURES / "gateway-buildkit-slsa-v1.json"
    assert release_main(["gateway-attestations", *_cli_args(sbom, provenance)]) == 0
    args = _cli_args(sbom, provenance)
    args[args.index("--source-sha") + 1] = "a" * 40
    with pytest.raises(SystemExit) as exc:
        release_main(["gateway-attestations", *args])
    assert exc.value.code == 1

    invalid = tmp_path / "invalid.json"
    invalid.write_text("{invalid", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        release_main(["gateway-attestations", *_cli_args(sbom, invalid)])
    assert exc.value.code == 1


def test_production_release_entrypoint_requires_no_installed_tooling_or_github_token() -> None:
    command = [
        sys.executable,
        "-S",  # The publisher invokes host Python: prove only stdlib and the checkout are needed.
        str(REPO_ROOT / "scripts/cicd/check_release_required_checks.py"),
        "gateway-attestations",
        *_cli_args(FIXTURES / "gateway-sbom-document-ids.json", FIXTURES / "gateway-buildkit-slsa-v1.json"),
    ]
    env = {"PYTHONPATH": str(REPO_ROOT / "elspeth-lints/src")}
    accepted = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=False)
    assert accepted.returncode == 0, accepted.stderr
    assert "identities verified for linux/amd64 and linux/arm64" in accepted.stdout

    command[command.index("--source-sha") + 1] = "a" * 40
    refused = subprocess.run(command, cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=False)
    assert refused.returncode == 1
    assert "does not match the expected build identity" in refused.stderr
