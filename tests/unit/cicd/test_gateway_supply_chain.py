"""Regression contracts for the standalone gateway supply chain."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-push.yaml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
GATEWAY = REPO_ROOT / "gateway"


def _yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def test_gateway_lock_is_current_and_rejects_stale_project_metadata(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "the test harness must provide the same uv executable CI uses"

    project = tmp_path / "gateway"
    project.mkdir()
    shutil.copy2(GATEWAY / "pyproject.toml", project / "pyproject.toml")
    shutil.copy2(GATEWAY / "uv.lock", project / "uv.lock")
    env = {**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_OFFLINE": "1"}

    current = subprocess.run(
        [uv, "lock", "--check", "--project", str(project)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    assert current.returncode == 0, current.stderr

    pyproject = project / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('"fastapi>=0.115,<1"', '"fastapi==0.1.0"'),
        encoding="utf-8",
    )
    stale = subprocess.run(
        [uv, "lock", "--check", "--project", str(project)],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )
    assert stale.returncode != 0


def test_dependabot_updates_gateway_uv_and_docker_inputs_independently() -> None:
    updates = _yaml(DEPENDABOT)["updates"]
    matches = {(entry["package-ecosystem"], entry["directory"]): entry for entry in updates}

    uv = matches[("uv", "/gateway")]
    docker = matches[("docker", "/gateway")]
    assert uv["schedule"]["interval"] == "weekly"
    assert docker["schedule"]["interval"] == "weekly"
    assert uv["groups"]["gateway-python"]["patterns"] == ["*"]
    assert {"dependencies", "python", "gateway"} <= set(uv["labels"])
    assert {"dependencies", "docker", "gateway"} <= set(docker["labels"])


def test_required_ci_audits_and_qualifies_the_standalone_gateway() -> None:
    jobs = _yaml(CI_WORKFLOW)["jobs"]
    supply_chain = jobs["supply-chain-audit"]
    gateway = jobs["gateway"]
    aggregate = jobs["ci-success"]

    audit = _step(supply_chain, "Audit standalone gateway dependencies")["run"]
    assert "uv lock --check --project gateway" in audit
    assert "--project gateway" in audit
    assert "--frozen" in audit
    assert "--all-extras" in audit
    assert "--all-groups" in audit
    assert "pip-audit" in audit and "--strict" in audit and "--disable-pip" in audit

    licence = _step(supply_chain, "Check standalone gateway dependency licenses")["run"]
    assert "--project gateway" in licence
    assert "--all-extras" in licence and "--all-groups" in licence
    assert '--fail-on "GPL;AGPL"' in licence

    tests = _step(gateway, "Run gateway tests and in-process conformance")["run"]
    assert "--project gateway" in tests and "--frozen" in tests
    assert "pytest gateway/tests gateway/conformance" in tests

    image = _step(gateway, "Build frozen gateway image")["with"]
    assert image["context"] == "${{ env.CI_CHECKOUT_PATH }}/gateway"
    assert image["build-args"] == "GATEWAY_REVISION=${{ github.sha }}"
    assert image["load"] is True and image["push"] is False

    scan = _step(gateway, "Refuse High or Critical gateway image findings")
    assert re.fullmatch(r"aquasecurity/trivy-action@[0-9a-f]{40}", scan["uses"])
    assert scan["with"]["severity"] == "CRITICAL,HIGH"
    assert scan["with"]["exit-code"] == "1"
    assert scan["with"]["ignore-unfixed"] is False

    runtime = _step(gateway, "Start gateway image read-only as its fixed non-root identity")["run"]
    assert "--read-only" in runtime and "--tmpfs /tmp" in runtime
    assert '"$GATEWAY_CI_IMAGE" -u)" = 65532' in runtime
    assert '"$GATEWAY_CI_IMAGE" -g)" = 65532' in runtime
    assert "/healthz" in runtime
    qualification = _step(gateway, "Run conformance against the read-only gateway image")["run"]
    assert "GATEWAY_CONFORMANCE_URL" in qualification
    assert "pytest gateway/conformance" in qualification

    assert "gateway" in aggregate["needs"]
    aggregate_run = _step(aggregate, "Check all jobs passed")["run"]
    assert "needs.gateway.result" in aggregate_run
    assert '!= "success"' in aggregate_run


def test_gateway_qualification_helpers_run_from_gateway_package() -> None:
    gateway = _yaml(CI_WORKFLOW)["jobs"]["gateway"]
    expected_working_directory = "${{ env.CI_CHECKOUT_PATH }}/gateway"

    credentials = _step(gateway, "Allocate isolated gateway qualification ports and credentials")
    assert credentials["working-directory"] == expected_working_directory
    assert "uv run --frozen --extra test --no-default-groups python" in credentials["run"]
    assert "--project gateway" not in credentials["run"]
    assert "from mock.stack import" in credentials["run"]

    backends = _step(gateway, "Start deterministic gateway qualification backends")
    assert backends["working-directory"] == expected_working_directory
    assert "uv run --frozen --extra test --no-default-groups" in backends["run"]
    assert "--project gateway" not in backends["run"]
    assert "python -m mock.backends" in backends["run"]

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    helper_commands = (
        [sys.executable, "-c", "from mock.stack import MOCK_CLIENT_ID"],
        [sys.executable, "-m", "mock.backends", "--help"],
    )
    for command in helper_commands:
        old_directory = subprocess.run(
            command,
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )
        assert old_directory.returncode != 0
        assert "No module named 'mock'" in old_directory.stderr

        repaired_directory = subprocess.run(
            command,
            cwd=GATEWAY,
            check=False,
            capture_output=True,
            env=env,
            text=True,
        )
        assert repaired_directory.returncode == 0, repaired_directory.stderr


def test_root_audit_ignore_set_contains_only_current_chromadb_exceptions() -> None:
    job = _yaml(CI_WORKFLOW)["jobs"]["supply-chain-audit"]
    run = _step(job, "Audit Python dependencies")["run"]
    ignored = set(re.findall(r"--ignore-vuln\s+(\S+)", run))

    assert ignored == {
        "CVE-2026-45829",
        "CVE-2026-45830",
        "CVE-2026-45831",
        "CVE-2026-45833",
    }
    assert "PYSEC-2025-183" not in run


def test_release_publishes_signed_attested_gateway_digest_and_smokes_it() -> None:
    jobs = _yaml(BUILD_WORKFLOW)["jobs"]
    build = jobs["build-push"]
    smoke = jobs["smoke-test"]
    release = jobs["release"]

    assert build["outputs"]["gateway_ghcr_digest"] == "${{ steps.gateway-ghcr-push.outputs.digest }}"
    publish = _step(build, "Build and push gateway to GHCR")["with"]
    assert publish["context"] == "${{ env.CI_CHECKOUT_PATH }}/gateway"
    assert publish["platforms"] == "${{ env.PLATFORMS }}"
    assert publish["build-args"] == "GATEWAY_REVISION=${{ env.IMAGE_SHA }}"
    assert publish["push"] is True
    assert publish["provenance"] == "mode=max"
    assert publish["sbom"] is True
    assert "GATEWAY_IMAGE_NAME" in publish["tags"]

    inspect = _step(build, "Inspect gateway SBOM and provenance attestations")["run"]
    assert ".SBOM" in inspect and ".Provenance" in inspect
    assert 'os.environ["IMAGE_SHA"]' in inspect
    for platform in ("amd64", "arm64"):
        scan = _step(build, f"Scan published gateway {platform} digest")
        assert re.fullmatch(r"aquasecurity/trivy-action@[0-9a-f]{40}", scan["uses"])
        assert scan["env"]["TRIVY_PLATFORM"] == f"linux/{platform}"
        assert "gateway-ghcr-push.outputs.digest" in scan["with"]["image-ref"]
        assert scan["with"]["severity"] == "CRITICAL,HIGH"
        assert scan["with"]["exit-code"] == "1"
        assert scan["with"]["ignore-unfixed"] is False
    sign = _step(build, "Sign gateway GHCR image digest")["run"]
    verify = _step(build, "Verify gateway signature identity")["run"]
    assert "cosign sign --yes" in sign
    assert "cosign verify" in verify
    assert "build-push.yaml@refs/" in verify
    assert "gateway-signature-verification.json" in verify

    evidence = _step(build, "Upload gateway qualification evidence")["with"]
    assert evidence["retention-days"] == 90
    assert "gateway-sbom.spdx.json" in evidence["path"]
    assert "gateway-provenance.json" in evidence["path"]
    assert "gateway-signature-verification.json" in evidence["path"]
    assert "gateway-vulnerabilities-linux-amd64.txt" in evidence["path"]
    assert "gateway-vulnerabilities-linux-arm64.txt" in evidence["path"]

    selection = _step(smoke, "Determine smoke-test images")
    assert selection["env"]["GATEWAY_GHCR_DIGEST"] == "${{ needs.build-push.outputs.gateway_ghcr_digest }}"
    gateway_smoke = _step(smoke, "Verify published gateway digest runtime contract")["run"]
    assert 'image="$GATEWAY_SMOKE_IMAGE"' in gateway_smoke
    assert "--read-only" in gateway_smoke and "/healthz" in gateway_smoke
    assert "elspeth_llm_gateway.image_identity" in gateway_smoke

    promote = _step(release, "Promote verified image digest to release tag")["run"]
    assert "GATEWAY_GHCR_DIGEST" in promote
    assert "GATEWAY_IMAGE_NAME" in promote
