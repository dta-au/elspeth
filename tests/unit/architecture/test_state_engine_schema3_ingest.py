from __future__ import annotations

import hashlib
import io
import json
import subprocess
import urllib.request
import zipfile
from http.client import HTTPMessage
from pathlib import Path
from typing import Any

import pytest
from scripts.state_engine_assessment_lib.common import ContractError
from scripts.state_engine_assessment_lib.operations import (
    GitHubActionsClient,
    _parse_live_manifest,
    _StripCrossOriginAuthorizationRedirect,
    ingest_live_evidence,
)
from scripts.state_engine_assessment_lib.package import (
    _cell_is_applicable,
    _profile_cases,
    _semantic_cases,
    _validate_baseline,
    _validate_evidence_provenance,
    initialize_full,
    validate_evidence_artifacts,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
V3_CATALOG = json.loads((REPOSITORY_ROOT / "docs/architecture/state_engine/proof-catalog/v3/catalog.json").read_text())


def _run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _git_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _run_git(repository, "init", "-q")
    _run_git(repository, "config", "user.email", "tests@example.invalid")
    _run_git(repository, "config", "user.name", "State Engine Tests")
    _run_git(repository, "remote", "add", "origin", "https://github.com/example/elspeth.git")
    (repository / "docs/result").mkdir(parents=True)
    (repository / "source.txt").write_text("frozen\n", encoding="utf-8")
    _run_git(repository, "add", "source.txt")
    _run_git(repository, "commit", "-qm", "frozen")
    commit = _run_git(repository, "rev-parse", "HEAD")
    tree = _run_git(repository, "rev-parse", "HEAD^{tree}")
    return repository, commit, tree


def _current_baseline(repository: Path, commit: str, tree: str, paths: list[str]) -> dict[str, Any]:
    return {
        "mode": "current",
        "commit": commit,
        "tree": tree,
        "repository_root": str(repository),
        "remote": "https://github.com/example/elspeth.git",
        "branch": _run_git(repository, "branch", "--show-current"),
        "worktree_status_at_evidence_capture": [],
        "submodules": [],
        "worktrees_at_capture": [],
        "publication_paths": paths,
    }


def test_schema3_baseline_accepts_exact_prospective_document_overlay(tmp_path: Path) -> None:
    repository, commit, tree = _git_repository(tmp_path)
    publication = "docs/result/assessment.json"
    (repository / publication).write_text("{}\n", encoding="utf-8")
    assessment: dict[str, Any] = {
        "schema_version": 3,
        "baseline": _current_baseline(repository, commit, tree, [publication]),
    }

    _validate_baseline(assessment, repository)


def test_schema3_baseline_rejects_extra_or_non_document_overlay(tmp_path: Path) -> None:
    repository, commit, tree = _git_repository(tmp_path)
    publication = "docs/result/assessment.json"
    (repository / publication).write_text("{}\n", encoding="utf-8")
    (repository / "docs/result/extra.md").write_text("extra\n", encoding="utf-8")
    assessment: dict[str, Any] = {
        "schema_version": 3,
        "baseline": _current_baseline(repository, commit, tree, [publication]),
    }

    with pytest.raises(ContractError, match="publication paths"):
        _validate_baseline(assessment, repository)

    (repository / "docs/result/extra.md").unlink()
    assessment["baseline"]["publication_paths"] = ["source.txt"]
    with pytest.raises(ContractError, match="under docs/"):
        _validate_baseline(assessment, repository)


def test_schema3_baseline_accepts_only_single_clean_docs_publication_child(tmp_path: Path) -> None:
    repository, commit, tree = _git_repository(tmp_path)
    publication = "docs/result/assessment.json"
    (repository / publication).write_text("{}\n", encoding="utf-8")
    _run_git(repository, "add", publication)
    _run_git(repository, "commit", "-qm", "publish")
    assessment = {"schema_version": 3, "baseline": _current_baseline(repository, commit, tree, [publication])}

    _validate_baseline(assessment, repository)

    (repository / "docs/result/later.md").write_text("later\n", encoding="utf-8")
    _run_git(repository, "add", "docs/result/later.md")
    _run_git(repository, "commit", "-qm", "later")
    with pytest.raises(ContractError, match="single docs-only publication child"):
        _validate_baseline(assessment, repository)


def test_schema3_local_provenance_binds_checkout_python_and_platform() -> None:
    assessment = {
        "schema_version": 3,
        "environment": {"python_executable": "/repo/.venv/bin/python", "kernel": "Linux test-kernel"},
        "baseline": {"commit": "a" * 40},
    }
    record: dict[str, Any] = {
        "id": "EV-local",
        "runner": "local",
        "provenance": {"python_executable": "/repo/.venv/bin/python", "platform": "Linux test-kernel"},
    }

    _validate_evidence_provenance(record, assessment)

    record["provenance"]["python_executable"] = "/usr/bin/python"
    with pytest.raises(ContractError, match="checkout Python"):
        _validate_evidence_provenance(record, assessment)


def test_schema3_cells_derive_cases_profiles_and_applicability_from_catalog() -> None:
    pb09 = next(leg for leg in V3_CATALOG["legs"] if leg["id"] == "PB-09")
    case_id = pb09["required_cases"][0]["case_id"]

    assert case_id in _semantic_cases(pb09)
    assert _profile_cases(V3_CATALOG, pb09) == [profile["id"] for profile in V3_CATALOG["execution_profiles"]["profile_cases"]]
    for profile_case in _profile_cases(V3_CATALOG, pb09):
        for dimension in V3_CATALOG["dimensions"]:
            expected = (
                next(case for case in pb09["required_cases"] if case["case_id"] == case_id)["cell_applicability"][profile_case][dimension][
                    "status"
                ]
                == "required"
            )
            assert _cell_is_applicable(V3_CATALOG, pb09, dimension, profile_case, case_id) is expected


def test_schema3_initialized_package_accepts_current_exact_publication_overlay(tmp_path: Path) -> None:
    repository, _initial_commit, _initial_tree = _git_repository(tmp_path)
    catalog_relative = "docs/architecture/state_engine/proof-catalog/v3/catalog.json"
    catalog_path = repository / catalog_relative
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_text(json.dumps(V3_CATALOG) + "\n", encoding="utf-8")
    _run_git(repository, "add", catalog_relative)
    _run_git(repository, "commit", "-qm", "add v3 catalog")
    assessment_directory = repository / "docs/architecture/state_engine/assessments/schema3"
    assessment_path = initialize_full("schema3", assessment_directory, Path(catalog_relative))
    (assessment_directory / "review.md").write_text("# Review\n\nReview outcome: complete\n", encoding="utf-8")
    commit = _run_git(repository, "rev-parse", "HEAD")
    tree = _run_git(repository, "rev-parse", "HEAD^{tree}")
    publication_paths = sorted(path.relative_to(repository).as_posix() for path in assessment_directory.iterdir())
    assessment = json.loads(assessment_path.read_text(encoding="utf-8"))
    assessment["baseline"] = _current_baseline(repository, commit, tree, publication_paths)
    assessment["environment"] = {
        "captured_at": "2026-08-12T01:00:00+00:00",
        "timezone": "UTC",
        "locale": "C.UTF-8",
        "kernel": "Linux test-kernel",
        "python": "3.13",
        "python_executable": "/repo/.venv/bin/python",
        "python_build": "test",
        "pytest": "9",
        "uv": "0.8",
        "git": "2.47",
        "sqlite": "3.49",
        "sqlalchemy": "2.0",
        "multiprocessing_start_method_before_tests": None,
        "pythonhashseed": "0",
        "dotenv": "disabled",
        "pyproject_sha256": "a" * 64,
        "uv_lock_sha256": "b" * 64,
        "database_profile": "postgresql-16-aws-single-leader-landscape",
        "sensitive_environment_captured": False,
    }
    snapshot = {"provider": "test", "captured_at": "2026-08-12T01:00:00+00:00", "limitation": "fixture"}
    assessment["structure_snapshot"] = snapshot
    assessment["tracker_snapshot"] = snapshot
    assessment_path.write_text(json.dumps(assessment, indent=2) + "\n", encoding="utf-8")

    assert validate_evidence_artifacts(assessment_path) == 0


class _AuthenticatedGitHub(GitHubActionsClient):
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

    def request_json(self, path: str) -> dict[str, Any]:
        value = self.responses[path]
        assert isinstance(value, dict)
        return value

    def request_bytes(self, path: str) -> bytes:
        value = self.responses[path]
        assert isinstance(value, bytes)
        return value


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_live_envelope(directory: Path, *, baseline_sha: str, workflow_digest: str) -> dict[str, Any]:
    directory.mkdir()
    nodes = ["tests/live/test_provider.py::test_lane"]
    (directory / "nodes.txt").write_text(nodes[0] + "\n", encoding="utf-8")
    (directory / "junit.xml").write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase><properties>'
        f'<property name="elspeth_node_id" value="{nodes[0]}" />'
        "</properties></testcase></testsuite>\n",
        encoding="utf-8",
    )
    profile = {
        "schema_version": 2,
        "producer": "elspeth-state-engine-profile-reporter-v2",
        "profile_case_id": "postgresql-16-aws-single-leader-landscape",
        "state_store": "postgresql-16",
        "deployment": "aws-single-leader-landscape",
        "backend_version": "16.4",
        "backend_probe": {"kind": "postgresql-server-query", "dialect": "postgresql", "query": "SHOW server_version"},
        "deployment_probe": {"kind": "trusted-test-runtime-assertion", "node_id": nodes[0]},
        "probe_node_id": nodes[0],
        "node_ids": nodes,
        "outcomes": [{"node_id": nodes[0], "outcome": "passed"}],
        "warnings": [],
    }
    (directory / "profile.json").write_text(json.dumps(profile) + "\n", encoding="utf-8")
    scrub = {
        "schema_version": 1,
        "producer": "elspeth-live-evidence-scrubber-v1",
        "status": "passed",
        "canaries_injected": 2,
        "findings": [],
        "scanned_files": ["junit.xml", "profile.json", "nodes.txt"],
    }
    (directory / "scrub-report.json").write_text(json.dumps(scrub) + "\n", encoding="utf-8")
    files = {name: _sha(directory / name) for name in ("junit.xml", "profile.json", "nodes.txt", "scrub-report.json")}
    manifest = {
        "schema_version": 1,
        "producer": "elspeth-state-engine-live-evidence-v1",
        "evidence_id": "EV-live-aws-provider",
        "lane_id": "protected-live-aws",
        "catalog_id": "elspeth-state-engine-v3",
        "catalog_sha256": "c" * 64,
        "repository": "example/elspeth",
        "workflow_path": ".github/workflows/state-engine-live-provider.yml",
        "workflow_sha256": workflow_digest,
        "baseline_sha": baseline_sha,
        "run_id": 101,
        "run_attempt": 2,
        "runner_image": "ubuntu-24.04",
        "ref": "refs/heads/main",
        "head_sha": baseline_sha,
        "artifact_name": "state-engine-live-protected-live-aws",
        "started_at": "2026-08-12T01:00:00+00:00",
        "ended_at": "2026-08-12T01:00:01+00:00",
        "duration_seconds": 1,
        "timeout_seconds": 900,
        "argv": [
            "/opt/hostedtoolcache/Python/3.12/bin/python",
            "-m",
            "pytest",
            "-p",
            "scripts.state_engine_profile_reporter",
            "--state-engine-profile-report=profile.json",
            "--junitxml=junit.xml",
            "-m",
            "live_provider and live_aws",
            nodes[0],
        ],
        "resources": ["AWS_REGION", "ELSPETH_LANDSCAPE_URL"],
        "result_counts": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0, "warnings": 0},
        "files": files,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return manifest


def test_live_manifest_boundary_rejects_malformed_external_data() -> None:
    with pytest.raises(ContractError, match="manifest"):
        _parse_live_manifest({"schema_version": 1, "provider_secret": "must-not-be-accepted"})


def test_github_archive_redirect_strips_bearer_token_cross_origin() -> None:
    request = urllib.request.Request(
        "https://api.github.com/repos/example/elspeth/actions/artifacts/1/zip",
        headers={"Authorization": "Bearer test-only-value"},
    )

    redirected = _StripCrossOriginAuthorizationRedirect().redirect_request(
        request,
        io.BytesIO(),
        302,
        "Found",
        HTTPMessage(),
        "https://objects.githubusercontent.com/signed-artifact",
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") is None


def _assessment_and_selector(repository: Path, baseline_sha: str, publication_paths: list[str]) -> Path:
    assessment_directory = repository / "docs/architecture/state_engine/assessments/run"
    assessment_directory.mkdir(parents=True)
    assessment_path = assessment_directory / "assessment.json"
    assessment = {
        "schema_version": 3,
        "catalog": {
            "path": "docs/architecture/state_engine/proof-catalog/v3/catalog.json",
            "catalog_id": "elspeth-state-engine-v3",
            "schema_version": 2,
            "sha256_at_evidence_capture": "c" * 64,
        },
        "baseline": {
            "mode": "current",
            "commit": baseline_sha,
            "remote": "https://github.com/example/elspeth.git",
            "publication_paths": publication_paths,
        },
        "environment": {"python_executable": "/repo/.venv/bin/python", "kernel": "Linux"},
        "evidence": [],
    }
    assessment_path.write_text(json.dumps(assessment) + "\n", encoding="utf-8")
    selector_path = repository / "docs/architecture/state_engine/proof-catalog/v3/evidence_selectors.json"
    selector_path.parent.mkdir(parents=True, exist_ok=True)
    selector = {
        "schema_version": 1,
        "catalog_id": "elspeth-state-engine-v3",
        "lanes": [
            {
                "lane_id": "protected-live-aws",
                "workflow_job": "live_aws",
                "profile_case": "postgresql-16-aws-single-leader-landscape",
                "marker_expression": "live_provider and live_aws",
                "node_ids": ["tests/live/test_provider.py::test_lane"],
                "cells": [
                    {
                        "leg_id": "PB-09",
                        "dimension_id": "boundary_composition",
                        "case_id": "source:aws_s3",
                        "profile_case": "postgresql-16-aws-single-leader-landscape",
                    }
                ],
                "resource_variables": ["AWS_REGION", "ELSPETH_LANDSCAPE_URL"],
                "artifact_kinds": ["manifest", "junit_xml", "profile_report", "node_index", "scrub_report"],
            }
        ],
    }
    selector_path.write_text(json.dumps(selector) + "\n", encoding="utf-8")
    return assessment_path


def _archive_bytes(directory: Path) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in ("manifest.json", "junit.xml", "profile.json", "nodes.txt", "scrub-report.json"):
            archive.writestr(name, (directory / name).read_bytes())
    return target.getvalue()


def _archive_with_names(directory: Path, names: list[tuple[str, str]]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_name, source_name in names:
            archive.writestr(archive_name, (directory / source_name).read_bytes())
    return target.getvalue()


def _github_responses(manifest: dict[str, Any], workflow: bytes, envelope: Path) -> dict[str, Any]:
    repository = manifest["repository"]
    run_id = manifest["run_id"]
    archive_bytes = _archive_bytes(envelope)
    archive_digest = hashlib.sha256(archive_bytes).hexdigest()
    archive_url = f"https://api.github.com/repos/{repository}/actions/artifacts/404/zip"
    return {
        f"repos/{repository}/actions/runs/{run_id}": {
            "id": run_id,
            "run_attempt": manifest["run_attempt"],
            "path": manifest["workflow_path"],
            "head_sha": manifest["baseline_sha"],
            "head_branch": "main",
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": repository, "default_branch": "main"},
        },
        f"repos/{repository}/actions/runs/{run_id}/attempts/{manifest['run_attempt']}/jobs": {
            "jobs": [
                {
                    "id": 303,
                    "name": "live_aws",
                    "status": "completed",
                    "conclusion": "success",
                    "runner_name": "GitHub Actions 1",
                    "runner_group_name": "GitHub Actions",
                    "labels": [manifest["runner_image"]],
                }
            ]
        },
        f"repos/{repository}/actions/runs/{run_id}/artifacts": {
            "artifacts": [
                {
                    "name": manifest["artifact_name"],
                    "digest": f"sha256:{archive_digest}",
                    "archive_download_url": archive_url,
                    "expired": False,
                    "workflow_run": {"id": run_id, "head_sha": manifest["baseline_sha"]},
                }
            ]
        },
        f"repos/{repository}/branches/main": {"name": "main", "protected": True, "commit": {"sha": manifest["baseline_sha"]}},
        f"repos/{repository}/contents/{manifest['workflow_path']}?ref={manifest['baseline_sha']}": workflow,
        archive_url: archive_bytes,
    }


def test_ingest_live_evidence_authenticates_and_materializes_closed_envelope(tmp_path: Path) -> None:
    repository, baseline_sha, _tree = _git_repository(tmp_path)
    workflow = b"name: state engine live\n"
    workflow_digest = hashlib.sha256(workflow).hexdigest()
    envelope = tmp_path / "artifact"
    manifest = _write_live_envelope(envelope, baseline_sha=baseline_sha, workflow_digest=workflow_digest)
    deterministic = [
        "docs/architecture/state_engine/assessments/run/assessment.json",
        *[
            f"docs/architecture/state_engine/assessments/run/evidence/live-protected-live-aws.{suffix}"
            for suffix in ("manifest.json", "junit.xml", "profile.json", "nodes.txt", "scrub-report.json")
        ],
    ]
    assessment_path = _assessment_and_selector(repository, baseline_sha, deterministic)
    client = _AuthenticatedGitHub(_github_responses(manifest, workflow, envelope))

    record = ingest_live_evidence(assessment_path, envelope, "protected-live-aws", client=client)

    assert record["runner"] == "github_actions"
    assert record["provenance"]["run_attempt"] == 2
    assert record["provenance"]["job_id"] == 303
    assert record["coverage"][0]["case_id"] == "source:aws_s3"
    assert json.loads(assessment_path.read_text(encoding="utf-8"))["evidence"] == [record]
    assert sorted(path.name for path in (assessment_path.parent / "evidence").iterdir()) == [
        "live-protected-live-aws.junit.xml",
        "live-protected-live-aws.manifest.json",
        "live-protected-live-aws.nodes.txt",
        "live-protected-live-aws.profile.json",
        "live-protected-live-aws.scrub-report.json",
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("transplanted", "baseline"),
        ("rerun", "attempt"),
        ("unprotected", "protected"),
        ("artifact", "artifact digest"),
    ],
)
def test_ingest_live_evidence_rejects_untrusted_producer_facts(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    repository, baseline_sha, _tree = _git_repository(tmp_path)
    workflow = b"name: state engine live\n"
    envelope = tmp_path / "artifact"
    manifest = _write_live_envelope(
        envelope,
        baseline_sha=baseline_sha,
        workflow_digest=hashlib.sha256(workflow).hexdigest(),
    )
    assessment_path = _assessment_and_selector(
        repository,
        baseline_sha,
        [
            "docs/architecture/state_engine/assessments/run/assessment.json",
            *[
                f"docs/architecture/state_engine/assessments/run/evidence/live-protected-live-aws.{suffix}"
                for suffix in ("manifest.json", "junit.xml", "profile.json", "nodes.txt", "scrub-report.json")
            ],
        ],
    )
    responses = _github_responses(manifest, workflow, envelope)
    run = responses[f"repos/{manifest['repository']}/actions/runs/{manifest['run_id']}"]
    artifacts = responses[f"repos/{manifest['repository']}/actions/runs/{manifest['run_id']}/artifacts"]
    branch = responses[f"repos/{manifest['repository']}/branches/main"]
    if mutation == "transplanted":
        run["head_sha"] = "d" * 40
    elif mutation == "rerun":
        run["run_attempt"] = 3
    elif mutation == "unprotected":
        branch["protected"] = False
    else:
        artifacts["artifacts"][0]["digest"] = "sha256:" + "e" * 64

    with pytest.raises(ContractError, match=message):
        ingest_live_evidence(assessment_path, envelope, "protected-live-aws", client=_AuthenticatedGitHub(responses))


def test_ingest_live_evidence_rejects_extra_files_and_failed_canary_scrub(tmp_path: Path) -> None:
    repository, baseline_sha, _tree = _git_repository(tmp_path)
    workflow = b"name: state engine live\n"
    envelope = tmp_path / "artifact"
    manifest = _write_live_envelope(envelope, baseline_sha=baseline_sha, workflow_digest=hashlib.sha256(workflow).hexdigest())
    assessment_path = _assessment_and_selector(
        repository,
        baseline_sha,
        [
            "docs/architecture/state_engine/assessments/run/assessment.json",
            *[
                f"docs/architecture/state_engine/assessments/run/evidence/live-protected-live-aws.{suffix}"
                for suffix in ("manifest.json", "junit.xml", "profile.json", "nodes.txt", "scrub-report.json")
            ],
        ],
    )
    client = _AuthenticatedGitHub(_github_responses(manifest, workflow, envelope))
    (envelope / "stdout.txt").write_text("raw output\n", encoding="utf-8")
    with pytest.raises(ContractError, match="five-file envelope"):
        ingest_live_evidence(assessment_path, envelope, "protected-live-aws", client=client)
    (envelope / "stdout.txt").unlink()
    scrub_path = envelope / "scrub-report.json"
    scrub = json.loads(scrub_path.read_text(encoding="utf-8"))
    scrub["findings"] = ["canary survived"]
    scrub_path.write_text(json.dumps(scrub) + "\n", encoding="utf-8")
    manifest["files"]["scrub-report.json"] = _sha(scrub_path)
    (envelope / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    client = _AuthenticatedGitHub(_github_responses(manifest, workflow, envelope))
    with pytest.raises(ContractError, match="scrub"):
        ingest_live_evidence(assessment_path, envelope, "protected-live-aws", client=client)


@pytest.mark.parametrize("archive_mutation", ["duplicate", "traversal", "extra"])
def test_ingest_live_evidence_rejects_unsafe_authenticated_archives(tmp_path: Path, archive_mutation: str) -> None:
    repository, baseline_sha, _tree = _git_repository(tmp_path)
    workflow = b"name: state engine live\n"
    envelope = tmp_path / "artifact"
    manifest = _write_live_envelope(envelope, baseline_sha=baseline_sha, workflow_digest=hashlib.sha256(workflow).hexdigest())
    assessment_path = _assessment_and_selector(
        repository,
        baseline_sha,
        [
            "docs/architecture/state_engine/assessments/run/assessment.json",
            *[
                f"docs/architecture/state_engine/assessments/run/evidence/live-protected-live-aws.{suffix}"
                for suffix in ("manifest.json", "junit.xml", "profile.json", "nodes.txt", "scrub-report.json")
            ],
        ],
    )
    responses = _github_responses(manifest, workflow, envelope)
    base_names = [(name, name) for name in ("manifest.json", "junit.xml", "profile.json", "nodes.txt", "scrub-report.json")]
    if archive_mutation == "duplicate":
        names = [*base_names, ("manifest.json", "manifest.json")]
    elif archive_mutation == "traversal":
        names = [("../manifest.json", "manifest.json"), *base_names[1:]]
    else:
        names = [*base_names, ("stdout.txt", "nodes.txt")]
    if archive_mutation == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive_bytes = _archive_with_names(envelope, names)
    else:
        archive_bytes = _archive_with_names(envelope, names)
    archive_url = responses[f"repos/{manifest['repository']}/actions/runs/{manifest['run_id']}/artifacts"]["artifacts"][0][
        "archive_download_url"
    ]
    responses[archive_url] = archive_bytes
    responses[f"repos/{manifest['repository']}/actions/runs/{manifest['run_id']}/artifacts"]["artifacts"][0]["digest"] = (
        "sha256:" + hashlib.sha256(archive_bytes).hexdigest()
    )

    with pytest.raises(ContractError, match="archive"):
        ingest_live_evidence(assessment_path, envelope, "protected-live-aws", client=_AuthenticatedGitHub(responses))


def test_ingest_live_evidence_rejects_transplanted_directory_bytes(tmp_path: Path) -> None:
    repository, baseline_sha, _tree = _git_repository(tmp_path)
    workflow = b"name: state engine live\n"
    envelope = tmp_path / "artifact"
    manifest = _write_live_envelope(envelope, baseline_sha=baseline_sha, workflow_digest=hashlib.sha256(workflow).hexdigest())
    assessment_path = _assessment_and_selector(
        repository,
        baseline_sha,
        [
            "docs/architecture/state_engine/assessments/run/assessment.json",
            *[
                f"docs/architecture/state_engine/assessments/run/evidence/live-protected-live-aws.{suffix}"
                for suffix in ("manifest.json", "junit.xml", "profile.json", "nodes.txt", "scrub-report.json")
            ],
        ],
    )
    responses = _github_responses(manifest, workflow, envelope)
    junit_path = envelope / "junit.xml"
    junit_path.write_bytes(junit_path.read_bytes() + b"\n")
    manifest["files"]["junit.xml"] = _sha(junit_path)
    (envelope / "manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")

    with pytest.raises(ContractError, match="differs from authenticated archive"):
        ingest_live_evidence(assessment_path, envelope, "protected-live-aws", client=_AuthenticatedGitHub(responses))
