"""Supply-chain closure contracts shared by release and CI workflows."""

from __future__ import annotations

import copy
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
BUILD_WORKFLOW = WORKFLOW_DIR / "build-push.yaml"
MUTATION_WORKFLOW = WORKFLOW_DIR / "mutation-testing.yaml"
LOCKS = (REPO_ROOT / "uv.lock", REPO_ROOT / "gateway" / "uv.lock")

_DIGEST_IMAGE_RE = re.compile(r"^[^\s@]+(?::[^\s@]+)?@sha256:[0-9a-f]{64}$")
_EXACT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_SETUP_UV_PREFIX = "astral-sh/setup-uv@"
_TRIVY_ACTION_RE = re.compile(r"aquasecurity/trivy-action@[0-9a-f]{40}")

_MAIN_SCAN_CASES = (
    (
        "GHCR",
        "ghcr.io/${{ github.repository_owner }}/${{ env.IMAGE_NAME }}@${{ steps.ghcr-push.outputs.digest }}",
        "Build and push to GHCR",
        "Sign GHCR image digest",
        "ghcr",
    ),
    (
        "ACR-only",
        "${{ secrets.ACR_REGISTRY }}/${{ env.IMAGE_NAME }}@${{ steps.acr-push.outputs.digest }}",
        "Build and push to ACR",
        "Sign ACR image digest",
        "acr",
    ),
)
_PLATFORMS = ("amd64", "arm64")


def _workflow(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path.name} workflow root must be a mapping"
    return value


def _workflows() -> dict[Path, dict[str, Any]]:
    paths = sorted((*WORKFLOW_DIR.glob("*.yaml"), *WORKFLOW_DIR.glob("*.yml")))
    return {path: _workflow(path) for path in paths}


def _one_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one {name!r} step, found {len(matches)}"
    return matches[0]


def _step_index(job: dict[str, Any], name: str) -> int:
    indexes = [index for index, step in enumerate(job["steps"]) if step.get("name") == name]
    assert len(indexes) == 1, f"expected exactly one {name!r} step, found {len(indexes)}"
    return indexes[0]


def _assert_main_release_scan_contract(job: dict[str, Any]) -> None:
    evidence_files: list[str] = []
    for registry_label, image_ref, build_name, sign_name, file_label in _MAIN_SCAN_CASES:
        for architecture in _PLATFORMS:
            name = f"Scan published main {registry_label} {architecture} digest"
            step = _one_step(job, name)
            assert _TRIVY_ACTION_RE.fullmatch(step["uses"]), f"{name} must pin trivy-action to a commit"
            assert step["env"]["TRIVY_PLATFORM"] == f"linux/{architecture}"
            assert step["with"]["image-ref"] == image_ref, f"{name} must scan the exact build output digest"
            assert step["with"]["scanners"] == "vuln"
            assert step["with"]["severity"] == "CRITICAL,HIGH"
            assert step["with"]["ignore-unfixed"] is False
            assert step["with"]["exit-code"] == "0"
            assert step["with"]["format"] == "json"
            assert step["env"]["TRIVY_SHOW_SUPPRESSED"] == "true"
            for variable in ("TRIVY_IGNORE_POLICY", "TRIVY_IGNOREFILE", "TRIVY_CONFIG"):
                assert step["env"][variable] == ""
            report = f"main-vulnerabilities-{file_label}-linux-{architecture}.json"
            summary = f"main-qualification-{file_label}-linux-{architecture}.json"
            assert report in step["with"]["output"]
            gate_name = f"Qualify published main {registry_label} {architecture} digest"
            gate = _one_step(job, gate_name)
            assert gate["env"]["SCANNED_IMAGE"] == image_ref
            assert gate["if"] == step["if"]
            assert "check_main_image_vulnerabilities.py" in gate["run"]
            assert '--image "$SCANNED_IMAGE"' in gate["run"]
            assert f"--platform linux/{architecture}" in gate["run"]
            assert f"--report .build-evidence/{report}" in gate["run"]
            assert f"--summary .build-evidence/{summary}" in gate["run"]
            assert gate.get("continue-on-error", False) is False
            assert "||" not in gate["run"]
            assert _step_index(job, build_name) < _step_index(job, name) < _step_index(job, gate_name) < _step_index(job, sign_name)
            evidence_files.extend((report, summary))

    evidence = _one_step(job, "Upload main image qualification evidence")["with"]
    assert evidence["retention-days"] == 90
    for report in evidence_files:
        assert report in evidence["path"], f"main qualification artifact must retain {report}"


def _compliant_main_release_job() -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for registry_label, image_ref, build_name, sign_name, file_label in _MAIN_SCAN_CASES:
        steps.append({"name": build_name})
        for architecture in _PLATFORMS:
            report = f"main-vulnerabilities-{file_label}-linux-{architecture}.json"
            summary = f"main-qualification-{file_label}-linux-{architecture}.json"
            condition = "steps.test.outputs.digest != ''"
            steps.append(
                {
                    "name": f"Scan published main {registry_label} {architecture} digest",
                    "uses": "aquasecurity/trivy-action@" + "a" * 40,
                    "if": condition,
                    "env": {
                        "TRIVY_PLATFORM": f"linux/{architecture}",
                        "TRIVY_IGNORE_POLICY": "",
                        "TRIVY_IGNOREFILE": "",
                        "TRIVY_CONFIG": "",
                        "TRIVY_SHOW_SUPPRESSED": "true",
                    },
                    "with": {
                        "image-ref": image_ref,
                        "format": "json",
                        "output": f".build-evidence/{report}",
                        "scanners": "vuln",
                        "severity": "CRITICAL,HIGH",
                        "ignore-unfixed": False,
                        "exit-code": "0",
                    },
                }
            )
            steps.append(
                {
                    "name": f"Qualify published main {registry_label} {architecture} digest",
                    "if": condition,
                    "env": {"SCANNED_IMAGE": image_ref},
                    "run": (
                        'python3 scripts/cicd/check_main_image_vulnerabilities.py --image "$SCANNED_IMAGE" '
                        f"--platform linux/{architecture} --report .build-evidence/{report} --summary .build-evidence/{summary}"
                    ),
                }
            )
        steps.append({"name": sign_name})
    evidence_paths = "\n".join(
        f".build-evidence/main-{kind}-{registry}-linux-{architecture}.json"
        for registry in ("ghcr", "acr")
        for architecture in _PLATFORMS
        for kind in ("vulnerabilities", "qualification")
    )
    steps.append(
        {
            "name": "Upload main image qualification evidence",
            "with": {"retention-days": 90, "path": evidence_paths},
        }
    )
    return {"steps": steps}


def test_main_release_images_are_scanned_by_exact_platform_digest_before_signing_and_retain_evidence() -> None:
    build = _workflow(BUILD_WORKFLOW)["jobs"]["build-push"]

    _assert_main_release_scan_contract(build)


def test_main_release_scan_validator_rejects_missing_rebound_late_or_unretained_scans() -> None:
    job = _compliant_main_release_job()
    _assert_main_release_scan_contract(job)

    missing = copy.deepcopy(job)
    missing["steps"] = [step for step in missing["steps"] if step.get("name") != "Scan published main GHCR amd64 digest"]
    with pytest.raises(AssertionError, match="expected exactly one"):
        _assert_main_release_scan_contract(missing)

    rebound = copy.deepcopy(job)
    rebound_scan = _one_step(rebound, "Scan published main ACR-only arm64 digest")
    rebound_scan["with"]["image-ref"] = "registry.example/elspeth:mutable"
    with pytest.raises(AssertionError, match="exact build output digest"):
        _assert_main_release_scan_contract(rebound)

    late = copy.deepcopy(job)
    late_scan = late["steps"].pop(_step_index(late, "Scan published main GHCR arm64 digest"))
    late["steps"].insert(_step_index(late, "Sign GHCR image digest") + 1, late_scan)
    with pytest.raises(AssertionError):
        _assert_main_release_scan_contract(late)

    unretained = copy.deepcopy(job)
    evidence = _one_step(unretained, "Upload main image qualification evidence")
    evidence["with"]["path"] = evidence["with"]["path"].replace("main-vulnerabilities-acr-linux-amd64.json", "")
    with pytest.raises(AssertionError, match="must retain"):
        _assert_main_release_scan_contract(unretained)

    bypassed = copy.deepcopy(job)
    _one_step(bypassed, "Qualify published main GHCR amd64 digest")["continue-on-error"] = True
    with pytest.raises(AssertionError):
        _assert_main_release_scan_contract(bypassed)

    rebound_gate = copy.deepcopy(job)
    _one_step(rebound_gate, "Qualify published main ACR-only arm64 digest")["env"]["SCANNED_IMAGE"] = "registry.example/elspeth:mutable"
    with pytest.raises(AssertionError):
        _assert_main_release_scan_contract(rebound_gate)


def _assert_container_is_digest_pinned(job_name: str, job: dict[str, Any]) -> None:
    container = job["container"]
    image = container if isinstance(container, str) else container["image"]
    matrix_match = re.fullmatch(r"\$\{\{ matrix\.([a-zA-Z0-9_-]+) }}", image)
    if matrix_match is None:
        assert _DIGEST_IMAGE_RE.fullmatch(image), f"{job_name} container is mutable: {image}"
        return

    image_key = matrix_match.group(1)
    include = job.get("strategy", {}).get("matrix", {}).get("include", [])
    mappings = [entry[image_key] for entry in include if image_key in entry]
    assert mappings, f"{job_name} matrix container {image_key!r} has no concrete mappings"
    for mapped_image in mappings:
        assert _DIGEST_IMAGE_RE.fullmatch(mapped_image), f"{job_name} matrix container is mutable: {mapped_image}"


def test_every_workflow_job_container_is_digest_pinned_including_matrix_mappings() -> None:
    measured = 0
    for path, workflow in _workflows().items():
        for job_name, job in workflow["jobs"].items():
            if "container" not in job:
                continue
            measured += 1
            _assert_container_is_digest_pinned(f"{path.name}:{job_name}", job)

    assert measured > 0, "container pinning test was inert"


def test_container_pin_validator_controls_literal_and_matrix_references() -> None:
    digest = "python:3.13-bookworm@sha256:" + "a" * 64
    _assert_container_is_digest_pinned("literal", {"container": {"image": digest}})
    _assert_container_is_digest_pinned(
        "matrix",
        {
            "container": {"image": "${{ matrix.ci-container-image }}"},
            "strategy": {"matrix": {"include": [{"ci-container-image": digest}]}},
        },
    )

    with pytest.raises(AssertionError, match="mutable"):
        _assert_container_is_digest_pinned("literal", {"container": {"image": "python:3.13-bookworm"}})
    with pytest.raises(AssertionError, match="no concrete mappings"):
        _assert_container_is_digest_pinned(
            "matrix",
            {"container": {"image": "${{ matrix.ci-container-image }}"}, "strategy": {"matrix": {"include": []}}},
        )


def _resolved_setup_uv_versions(workflow: dict[str, Any]) -> list[str]:
    versions: list[str] = []
    workflow_env = workflow.get("env", {})
    for job in workflow["jobs"].values():
        job_env = {**workflow_env, **job.get("env", {})}
        for step in job["steps"]:
            if not str(step.get("uses", "")).startswith(_SETUP_UV_PREFIX):
                continue
            requested = str(step.get("with", {}).get("version", ""))
            env_match = re.fullmatch(r"\$\{\{ env\.([a-zA-Z0-9_]+) }}", requested)
            versions.append(str(job_env.get(env_match.group(1), "")) if env_match is not None else requested)
    return versions


def test_every_setup_uv_step_uses_one_full_exact_version() -> None:
    versions: list[str] = []
    for workflow in _workflows().values():
        versions.extend(_resolved_setup_uv_versions(workflow))

    assert versions, "setup-uv version test was inert"
    assert all(_EXACT_VERSION_RE.fullmatch(version) for version in versions), f"setup-uv versions must be exact: {versions}"
    assert len(set(versions)) == 1, f"setup-uv versions drifted across workflows: {sorted(set(versions))}"


def test_setup_uv_version_resolver_controls_direct_and_environment_versions() -> None:
    workflow = {
        "env": {"UV_VERSION": "0.5.31"},
        "jobs": {
            "direct": {"steps": [{"uses": _SETUP_UV_PREFIX + "a" * 40, "with": {"version": "0.5.31"}}]},
            "environment": {"steps": [{"uses": _SETUP_UV_PREFIX + "a" * 40, "with": {"version": "${{ env.UV_VERSION }}"}}]},
        },
    }
    assert _resolved_setup_uv_versions(workflow) == ["0.5.31", "0.5.31"]

    workflow["env"]["UV_VERSION"] = "0.5"
    assert not all(_EXACT_VERSION_RE.fullmatch(version) for version in _resolved_setup_uv_versions(workflow))


@pytest.mark.parametrize("lock", LOCKS, ids=lambda path: str(path.relative_to(REPO_ROOT)))
def test_audit_tools_are_locked_in_each_project(lock: Path) -> None:
    required = {"pip-audit", "pip-licenses"}
    parsed = tomllib.loads(lock.read_text(encoding="utf-8"))
    packages = {package["name"] for package in parsed["package"]}

    assert required <= packages, f"{lock.relative_to(REPO_ROOT)} lacks locked audit tools: {sorted(required - packages)}"


def test_workflows_do_not_dynamically_resolve_audit_tools() -> None:
    measured = 0
    dynamic_tool = re.compile(r"--with\s+[\"']?(?:pip-audit|pip-licenses)(?:[<>=!~].*)?[\"']?")

    for path, workflow in _workflows().items():
        for job_name, job in workflow["jobs"].items():
            for step in job["steps"]:
                run = str(step.get("run", ""))
                measured += run.count("pip-audit") + run.count("pip-licenses")
                assert dynamic_tool.search(run) is None, f"{path.name}:{job_name} dynamically resolves an audit tool"

    assert measured > 0, "audit-tool resolution test was inert"


def _assert_frozen_mutation_install(job_name: str, job: dict[str, Any]) -> None:
    install = _one_step(job, "Install dependencies")["run"]
    assert "uv sync" in install, f"{job_name} must install from the project lock"
    assert "--frozen" in install, f"{job_name} must refuse lock drift"
    assert "uv pip install" not in install, f"{job_name} must not resolve dependencies outside uv.lock"


def test_mutation_jobs_install_from_the_frozen_root_lock() -> None:
    jobs = _workflow(MUTATION_WORKFLOW)["jobs"]
    assert jobs, "mutation dependency test was inert"
    for job_name, job in jobs.items():
        _assert_frozen_mutation_install(job_name, job)


def test_mutation_install_validator_rejects_unfrozen_sync_and_uv_pip_install() -> None:
    _assert_frozen_mutation_install("control", {"steps": [{"name": "Install dependencies", "run": "uv sync --frozen --extra dev"}]})

    with pytest.raises(AssertionError, match="refuse lock drift"):
        _assert_frozen_mutation_install("unfrozen", {"steps": [{"name": "Install dependencies", "run": "uv sync --extra dev"}]})
    with pytest.raises(AssertionError, match="must install from the project lock"):
        _assert_frozen_mutation_install(
            "pip-install",
            {"steps": [{"name": "Install dependencies", "run": 'uv venv\nuv pip install -e ".[dev]"'}]},
        )
