"""Static retained-evidence validation, link checking, and CLI dispatch."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any

from elspeth.contracts.trust_boundary import trust_boundary

from .catalog import validate_catalog
from .common import (
    SHA256_PATTERN,
    ContractError,
    _dict,
    _fail,
    _git_root,
    _list,
    _require,
    _string,
    _strings,
    load_unique_json,
)
from .package import (
    _junit_node_ids,
    _offset_datetime,
    _validate_evidence_provenance,
    _validate_profile_report,
    initialize_full,
    validate_evidence_artifacts,
    validate_package,
)

_LIVE_ENVELOPE_FILES = {"manifest.json", "junit.xml", "profile.json", "nodes.txt", "scrub-report.json"}
_LIVE_RESULT_KEYS = ["passed", "failed", "errors", "skipped", "xfailed", "xpassed", "warnings"]
_MAX_GITHUB_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_LIVE_MEMBER_BYTES = 8 * 1024 * 1024
_MAX_LIVE_ENVELOPE_BYTES = 16 * 1024 * 1024


class _StripCrossOriginAuthorizationRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: IO[bytes],
        code: int,
        message: str,
        headers: HTTPMessage,
        new_url: str,
    ) -> urllib.request.Request | None:
        redirected = super().redirect_request(request, file_pointer, code, message, headers, new_url)
        if redirected is None:
            return None
        old_origin = urllib.parse.urlsplit(request.full_url).netloc.lower()
        new_origin = urllib.parse.urlsplit(new_url).netloc.lower()
        if old_origin != new_origin:
            redirected.remove_header("Authorization")
            redirected.remove_header("authorization")
        return redirected


class GitHubActionsClient:
    """Minimal read-only GitHub Actions provenance client."""

    def __init__(self, token: str | None = None) -> None:
        resolved_token = token if token is not None else os.environ.get("ELSPETH_GITHUB_ACTIONS_READ_TOKEN")
        _require(bool(resolved_token), "ELSPETH_GITHUB_ACTIONS_READ_TOKEN is required for live-evidence ingestion")
        self._token = resolved_token

    def _request(self, path: str, *, accept: str) -> bytes:
        url = path if path.startswith("https://") else f"https://api.github.com/{path}"
        _require(
            urllib.parse.urlsplit(url).scheme == "https" and urllib.parse.urlsplit(url).netloc.lower() == "api.github.com",
            "GitHub API request URL is not trusted",
        )
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "elspeth-state-engine-evidence-ingest/1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            opener = urllib.request.build_opener(_StripCrossOriginAuthorizationRedirect())
            with opener.open(request, timeout=30) as response:
                body = bytes(response.read(_MAX_GITHUB_RESPONSE_BYTES + 1))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError):
            _fail("GitHub Actions provenance request failed")
        _require(len(body) <= _MAX_GITHUB_RESPONSE_BYTES, "GitHub Actions provenance response is oversized")
        return body

    def request_json(self, path: str) -> dict[str, Any]:
        raw = self._request(path, accept="application/vnd.github+json")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            _fail("GitHub Actions provenance response is not valid JSON")
        return _dict(value, "GitHub Actions provenance response")

    def request_bytes(self, path: str) -> bytes:
        return self._request(path, accept="application/vnd.github.raw+json")


@trust_boundary(
    tier=3,
    source="downloaded GitHub Actions live-evidence manifest",
    source_param="raw_manifest",
    suppresses=("R1", "R5"),
    invariant="raises ContractError on any malformed, open, secret-bearing, or unsupported manifest field",
    test_ref=("tests/unit/architecture/test_state_engine_schema3_ingest.py::test_live_manifest_boundary_rejects_malformed_external_data"),
    test_fingerprint="f0c973d955d75e2c7c73a589fb7faafb4d35252cef0f46e1b78d0b0ba4a96b1c",
)
def _parse_live_manifest(raw_manifest: object) -> dict[str, Any]:
    manifest = _dict(raw_manifest, "live evidence manifest")
    fields = {
        "schema_version",
        "producer",
        "evidence_id",
        "lane_id",
        "catalog_id",
        "catalog_sha256",
        "repository",
        "workflow_path",
        "workflow_sha256",
        "baseline_sha",
        "run_id",
        "run_attempt",
        "runner_image",
        "ref",
        "head_sha",
        "artifact_name",
        "started_at",
        "ended_at",
        "duration_seconds",
        "timeout_seconds",
        "argv",
        "resources",
        "result_counts",
        "files",
    }
    _require(set(manifest) == fields, "live evidence manifest has unexpected fields")
    _require(manifest.get("schema_version") == 1, "live evidence manifest schema_version must be 1")
    _require(
        manifest.get("producer") == "elspeth-state-engine-live-evidence-v1",
        "live evidence manifest producer is not trusted",
    )
    for field in (
        "evidence_id",
        "lane_id",
        "catalog_id",
        "repository",
        "workflow_path",
        "runner_image",
        "ref",
        "artifact_name",
    ):
        _require(
            isinstance(manifest.get(field), str) and bool(manifest[field].strip()),
            f"live evidence manifest {field} must be non-empty text",
        )
    for field in ("catalog_sha256", "workflow_sha256"):
        _require(
            isinstance(manifest.get(field), str) and SHA256_PATTERN.fullmatch(manifest[field]) is not None,
            f"live evidence manifest {field} must be SHA-256",
        )
    for field in ("baseline_sha", "head_sha"):
        _require(
            isinstance(manifest.get(field), str) and re.fullmatch(r"[0-9a-f]{40}", manifest[field]) is not None,
            f"live evidence manifest {field} must be a full Git object ID",
        )
    for field in ("run_id", "run_attempt", "timeout_seconds"):
        _require(
            type(manifest.get(field)) is int and manifest[field] > 0,
            f"live evidence manifest {field} must be a positive integer",
        )
    _strings(manifest.get("argv"), "live evidence manifest argv")
    resources = _strings(manifest.get("resources"), "live evidence manifest resources")
    _require(len(resources) == len(set(resources)), "live evidence manifest resources contain duplicates")
    _require(
        all(re.fullmatch(r"[A-Z][A-Z0-9_]*", value) is not None for value in resources),
        "live evidence manifest resources must contain variable names only",
    )
    counts = _dict(manifest.get("result_counts"), "live evidence manifest result_counts")
    _require(list(counts) == _LIVE_RESULT_KEYS, "live evidence manifest result_counts has wrong keys")
    _require(
        all(type(counts[key]) is int and counts[key] >= 0 for key in _LIVE_RESULT_KEYS),
        "live evidence manifest result_counts must be non-negative integers",
    )
    files = _dict(manifest.get("files"), "live evidence manifest files")
    _require(
        set(files) == _LIVE_ENVELOPE_FILES - {"manifest.json"},
        "live evidence manifest files must bind the four non-manifest envelope members",
    )
    _require(
        all(isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None for value in files.values()),
        "live evidence manifest file digests must be SHA-256",
    )
    return manifest


def _load_live_lane(root: Path, lane_id: str) -> dict[str, Any]:
    selector_path = root / "docs/architecture/state_engine/proof-catalog/v3/evidence_selectors.json"
    _require(selector_path.is_file(), "v3 evidence selector manifest is missing")
    selector = load_unique_json(selector_path)
    _require(
        set(selector) == {"schema_version", "catalog_id", "lanes"}
        and selector.get("schema_version") == 1
        and selector.get("catalog_id") == "elspeth-state-engine-v3",
        "v3 evidence selector manifest has an unsupported schema",
    )
    lanes = _list(selector.get("lanes"), "v3 evidence selector lanes")
    matches = [
        _dict(raw_lane, "v3 evidence selector lane")
        for raw_lane in lanes
        if isinstance(raw_lane, dict) and raw_lane.get("lane_id") == lane_id
    ]
    _require(len(matches) == 1, f"unknown or duplicate live evidence lane: {lane_id}")
    lane = matches[0]
    fields = {
        "lane_id",
        "workflow_job",
        "profile_case",
        "marker_expression",
        "node_ids",
        "cells",
        "resource_variables",
        "artifact_kinds",
    }
    _require(set(lane) == fields, f"live evidence lane {lane_id} has unexpected fields")
    for field in ("lane_id", "workflow_job", "profile_case", "marker_expression"):
        _require(isinstance(lane[field], str) and bool(lane[field].strip()), f"live evidence lane {lane_id} {field} is invalid")
    nodes = _strings(lane.get("node_ids"), f"live evidence lane {lane_id} node_ids")
    _require(bool(nodes) and len(nodes) == len(set(nodes)), f"live evidence lane {lane_id} node_ids must be non-empty and unique")
    resources = _strings(lane.get("resource_variables"), f"live evidence lane {lane_id} resource_variables")
    _require(len(resources) == len(set(resources)), f"live evidence lane {lane_id} resource_variables contain duplicates")
    _require(
        lane.get("artifact_kinds") == ["manifest", "junit_xml", "profile_report", "node_index", "scrub_report"],
        f"live evidence lane {lane_id} artifact kinds are not the closed live envelope",
    )
    cells = _list(lane.get("cells"), f"live evidence lane {lane_id} cells")
    _require(bool(cells), f"live evidence lane {lane_id} has no proof cells")
    for raw_cell in cells:
        cell = _dict(raw_cell, f"live evidence lane {lane_id} cell")
        _require(
            set(cell) == {"leg_id", "dimension_id", "case_id", "profile_case"},
            f"live evidence lane {lane_id} cell has unexpected fields",
        )
        _require(cell.get("profile_case") == lane["profile_case"], f"live evidence lane {lane_id} cell profile drifts")
    return lane


def _read_live_envelope(directory: Path) -> tuple[dict[str, Any], dict[str, bytes]]:
    _require(directory.is_dir() and not directory.is_symlink(), "live evidence artifact directory is invalid")
    entries = list(directory.iterdir())
    _require(
        {entry.name for entry in entries} == _LIVE_ENVELOPE_FILES
        and len(entries) == len(_LIVE_ENVELOPE_FILES)
        and all(entry.is_file() and not entry.is_symlink() for entry in entries),
        "live evidence must contain the exact closed five-file envelope",
    )
    members = {name: (directory / name).read_bytes() for name in _LIVE_ENVELOPE_FILES}
    try:
        raw_manifest = json.loads(members["manifest.json"])
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("live evidence manifest is not valid JSON")
    manifest = _parse_live_manifest(raw_manifest)
    files = _dict(manifest["files"], "live evidence manifest files")
    for name in _LIVE_ENVELOPE_FILES - {"manifest.json"}:
        _require(
            hashlib.sha256(members[name]).hexdigest() == files[name],
            f"live evidence manifest digest mismatch: {name}",
        )
    return manifest, members


def _verify_authenticated_archive(archive_bytes: bytes, expected_digest: str, envelope: dict[str, bytes]) -> None:
    _require(
        hashlib.sha256(archive_bytes).hexdigest() == expected_digest,
        "GitHub Actions artifact digest does not match the downloaded archive",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            _require(
                len(names) == len(_LIVE_ENVELOPE_FILES) and set(names) == _LIVE_ENVELOPE_FILES,
                "GitHub Actions artifact archive must contain the exact five-file envelope without duplicates",
            )
            _require(
                sum(info.file_size for info in infos) <= _MAX_LIVE_ENVELOPE_BYTES,
                "GitHub Actions artifact archive uncompressed envelope is oversized",
            )
            for info in infos:
                _require(
                    Path(info.filename).name == info.filename and not info.is_dir(),
                    "GitHub Actions artifact archive contains an unsafe member path",
                )
                file_type = (info.external_attr >> 16) & 0o170000
                _require(file_type in {0, 0o100000}, "GitHub Actions artifact archive member is not a regular file")
                _require(info.flag_bits & 0x1 == 0, "GitHub Actions artifact archive member is encrypted")
                _require(info.file_size <= _MAX_LIVE_MEMBER_BYTES, "GitHub Actions artifact archive member is oversized")
                _require(
                    info.file_size <= max(1024, info.compress_size * 200),
                    "GitHub Actions artifact archive member has an unsafe compression ratio",
                )
                with archive.open(info) as member:
                    member_bytes = member.read(_MAX_LIVE_MEMBER_BYTES + 1)
                _require(
                    len(member_bytes) <= _MAX_LIVE_MEMBER_BYTES,
                    "GitHub Actions artifact archive member exceeds its bounded read",
                )
                _require(
                    member_bytes == envelope[info.filename],
                    f"downloaded artifact directory differs from authenticated archive member: {info.filename}",
                )
    except (zipfile.BadZipFile, RuntimeError, OSError):
        _fail("GitHub Actions artifact archive is invalid")


def _authenticate_github_actions(
    manifest: dict[str, Any],
    lane: dict[str, Any],
    client: GitHubActionsClient,
    envelope: dict[str, bytes],
) -> dict[str, Any]:
    repository = _string(manifest["repository"], "live evidence repository")
    encoded_repository = "/".join(urllib.parse.quote(part, safe="") for part in repository.split("/"))
    _require(len(encoded_repository.split("/")) == 2, "live evidence repository must be owner/name")
    run_id = manifest["run_id"]
    attempt = manifest["run_attempt"]
    run = client.request_json(f"repos/{encoded_repository}/actions/runs/{run_id}")
    repository_record = _dict(run.get("repository"), "GitHub Actions run repository")
    _require(
        run.get("id") == run_id
        and run.get("run_attempt") == attempt
        and run.get("path") == manifest["workflow_path"]
        and run.get("head_sha") == manifest["baseline_sha"] == manifest["head_sha"]
        and run.get("event") == "workflow_dispatch"
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and repository_record.get("full_name") == repository,
        "GitHub Actions run identity, attempt, workflow, baseline, or success does not authenticate the envelope",
    )
    default_branch = _string(repository_record.get("default_branch"), "GitHub repository default branch")
    _require(
        run.get("head_branch") == default_branch and manifest["ref"] == f"refs/heads/{default_branch}",
        "GitHub Actions run is not bound to the default branch",
    )
    branch = client.request_json(f"repos/{encoded_repository}/branches/{urllib.parse.quote(default_branch, safe='')}")
    branch_commit = _dict(branch.get("commit"), "GitHub protected branch commit")
    _require(
        branch.get("name") == default_branch and branch.get("protected") is True,
        "GitHub Actions evidence default branch is not protected",
    )
    _require(
        branch_commit.get("sha") == manifest["baseline_sha"],
        "GitHub protected branch no longer resolves to the evidence baseline",
    )
    jobs_record = client.request_json(f"repos/{encoded_repository}/actions/runs/{run_id}/attempts/{attempt}/jobs")
    jobs = _list(jobs_record.get("jobs"), "GitHub Actions jobs")
    matching_jobs = [
        _dict(raw_job, "GitHub Actions job")
        for raw_job in jobs
        if isinstance(raw_job, dict) and raw_job.get("name") == lane["workflow_job"]
    ]
    _require(len(matching_jobs) == 1, "GitHub Actions workflow job identity is missing or ambiguous")
    job = matching_jobs[0]
    job_id = job.get("id")
    labels = _strings(job.get("labels"), "GitHub Actions job labels")
    _require(
        type(job_id) is int
        and job_id > 0
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and manifest["runner_image"] in labels,
        "GitHub Actions job identity, success, or runner image is not authenticated",
    )
    artifacts_record = client.request_json(f"repos/{encoded_repository}/actions/runs/{run_id}/artifacts")
    artifacts = _list(artifacts_record.get("artifacts"), "GitHub Actions artifacts")
    matching_artifacts = [
        _dict(raw_artifact, "GitHub Actions artifact")
        for raw_artifact in artifacts
        if isinstance(raw_artifact, dict) and raw_artifact.get("name") == manifest["artifact_name"]
    ]
    _require(len(matching_artifacts) == 1, "GitHub Actions artifact identity is missing or ambiguous")
    artifact = matching_artifacts[0]
    workflow_run = _dict(artifact.get("workflow_run"), "GitHub Actions artifact workflow_run")
    raw_digest = _string(artifact.get("digest"), "GitHub Actions artifact digest")
    _require(raw_digest.startswith("sha256:"), "GitHub Actions artifact digest is not SHA-256")
    artifact_digest = raw_digest.removeprefix("sha256:")
    _require(SHA256_PATTERN.fullmatch(artifact_digest) is not None, "GitHub Actions artifact digest is invalid")
    _require(
        artifact.get("expired") is False and workflow_run.get("id") == run_id and workflow_run.get("head_sha") == manifest["baseline_sha"],
        "GitHub Actions artifact is expired or transplanted from another run",
    )
    archive_url = _string(artifact.get("archive_download_url"), "GitHub Actions artifact archive URL")
    _require(archive_url.startswith("https://api.github.com/"), "GitHub Actions artifact archive URL is not trusted")
    _verify_authenticated_archive(client.request_bytes(archive_url), artifact_digest, envelope)
    workflow_path = urllib.parse.quote(_string(manifest["workflow_path"], "live evidence workflow path"), safe="/")
    workflow_bytes = client.request_bytes(f"repos/{encoded_repository}/contents/{workflow_path}?ref={manifest['baseline_sha']}")
    _require(
        hashlib.sha256(workflow_bytes).hexdigest() == manifest["workflow_sha256"],
        "GitHub Actions workflow digest does not authenticate",
    )
    return {"job_id": job_id, "artifact_sha256": artifact_digest}


def _validate_live_argv(manifest: dict[str, Any], lane: dict[str, Any]) -> None:
    argv = _strings(manifest.get("argv"), "live evidence argv")
    _require(len(argv) >= 10 and Path(argv[0]).is_absolute(), "live evidence argv must use an absolute Python executable")
    _require(argv[1:3] == ["-m", "pytest"], "live evidence argv must invoke pytest through Python")
    expected_fixed = {
        ("-p", "scripts.state_engine_profile_reporter"),
        ("--state-engine-profile-report", "profile.json"),
        ("--junitxml", "junit.xml"),
        ("-m", lane["marker_expression"]),
    }
    found: set[tuple[str, str]] = set()
    selectors: list[str] = []
    index = 3
    while index < len(argv):
        item = argv[index]
        matched_equals = False
        for option in ("--state-engine-profile-report", "--junitxml"):
            prefix = f"{option}="
            if item.startswith(prefix):
                found.add((option, item.removeprefix(prefix)))
                matched_equals = True
                break
        if matched_equals:
            index += 1
            continue
        if item in {"-p", "--state-engine-profile-report", "--junitxml", "-m"}:
            _require(index + 1 < len(argv), f"live evidence argv option {item} is missing its value")
            found.add((item, argv[index + 1]))
            index += 2
            continue
        if item in {"-n", "--numprocesses"}:
            _require(index + 1 < len(argv) and argv[index + 1] == "0", "live evidence argv permits only -n 0")
            index += 2
            continue
        if (
            item in {"-q", "--quiet", "-x", "--exitfirst", "--strict-config", "--strict-markers", "--disable-warnings"}
            or re.fullmatch(r"--maxfail=[1-9][0-9]*", item)
            or re.fullmatch(r"--tb=(?:auto|long|short|line|native|no)", item)
        ):
            index += 1
            continue
        _require(not item.startswith("-"), f"live evidence argv contains an unsupported option: {item}")
        selectors.append(item)
        index += 1
    _require(found == expected_fixed, "live evidence argv reporter, outputs, or marker expression drifted")
    _require(selectors == lane["node_ids"], "live evidence argv selectors do not match the frozen lane")


def _validate_scrub_report(raw: bytes) -> str:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("live evidence scrub report is not valid JSON")
    report = _dict(value, "live evidence scrub report")
    _require(
        set(report) == {"schema_version", "producer", "status", "canaries_injected", "findings", "scanned_files"},
        "live evidence scrub report has unexpected fields",
    )
    _require(
        report.get("schema_version") == 1
        and report.get("producer") == "elspeth-live-evidence-scrubber-v1"
        and report.get("status") == "passed",
        "live evidence scrub report did not succeed",
    )
    _require(
        type(report.get("canaries_injected")) is int and report["canaries_injected"] > 0,
        "live evidence scrub report did not exercise canaries",
    )
    _require(report.get("findings") == [], "live evidence scrub report contains findings")
    _require(
        report.get("scanned_files") == ["junit.xml", "profile.json", "nodes.txt"],
        "live evidence scrub report did not scan every non-manifest payload",
    )
    return hashlib.sha256(raw).hexdigest()


def _expected_github_repository(remote: Any) -> str:
    value = _string(remote, "baseline remote")
    patterns = (
        r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?$",
        r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?$",
        r"ssh://git@github\.com/([^/]+/[^/]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if match is not None:
            return match.group(1)
    _fail("baseline origin is not a supported GitHub repository URL")


def _materialization_paths(assessment_path: Path, lane_id: str) -> dict[str, Path]:
    _require(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", lane_id) is not None, "live evidence lane ID is not filename-safe")
    directory = assessment_path.parent / "evidence"
    basename = f"live-{lane_id}"
    return {
        "manifest.json": directory / f"{basename}.manifest.json",
        "junit.xml": directory / f"{basename}.junit.xml",
        "profile.json": directory / f"{basename}.profile.json",
        "nodes.txt": directory / f"{basename}.nodes.txt",
        "scrub-report.json": directory / f"{basename}.scrub-report.json",
    }


def ingest_live_evidence(
    assessment_path: Path,
    artifact_directory: Path,
    lane_id: str,
    *,
    client: GitHubActionsClient | None = None,
) -> dict[str, Any]:
    assessment_path = assessment_path.resolve()
    root = _git_root(assessment_path)
    assessment = load_unique_json(assessment_path)
    _require(assessment.get("schema_version") == 3, "live evidence ingestion requires assessment schema 3")
    catalog_record = _dict(assessment.get("catalog"), "assessment catalog")
    _require(
        catalog_record.get("catalog_id") == "elspeth-state-engine-v3" and catalog_record.get("schema_version") == 2,
        "live evidence ingestion requires the v3 catalog/schema-2 pair",
    )
    baseline = _dict(assessment.get("baseline"), "assessment baseline")
    _require(baseline.get("mode") == "current", "live evidence ingestion requires a current frozen baseline")
    lane = _load_live_lane(root, lane_id)
    manifest, envelope = _read_live_envelope(artifact_directory.resolve())
    _require(manifest["lane_id"] == lane_id, "live evidence manifest lane does not match the selected lane")
    _require(
        manifest["catalog_id"] == catalog_record["catalog_id"]
        and manifest["catalog_sha256"] == catalog_record["sha256_at_evidence_capture"],
        "live evidence manifest catalog identity does not match the assessment",
    )
    _require(manifest["baseline_sha"] == baseline.get("commit"), "live evidence manifest baseline is stale or transplanted")
    _require(
        manifest["repository"] == _expected_github_repository(baseline.get("remote")),
        "live evidence manifest repository does not match the assessment origin",
    )
    _require(
        manifest["workflow_path"] == ".github/workflows/state-engine-live-provider.yml",
        "live evidence manifest workflow path is not the protected workflow",
    )
    _require(
        manifest["artifact_name"] == f"state-engine-live-{lane_id}",
        "live evidence artifact name is not deterministic for the lane",
    )
    _require(manifest["resources"] == lane["resource_variables"], "live evidence resources do not match the approved lane variables")
    _validate_live_argv(manifest, lane)
    authenticated = _authenticate_github_actions(manifest, lane, client or GitHubActionsClient(), envelope)
    scrub_digest = _validate_scrub_report(envelope["scrub-report.json"])
    nodes = envelope["nodes.txt"].decode("utf-8").splitlines()
    _require(nodes == lane["node_ids"], "live evidence node index does not match the frozen lane")
    _require(len(nodes) == len(lane["cells"]), "live evidence selector must map each node to exactly one proof subject")
    junit_temp = artifact_directory / "junit.xml"
    profile_temp = artifact_directory / "profile.json"
    junit_nodes, junit_outcomes = _junit_node_ids(junit_temp, manifest["evidence_id"])
    _require(junit_nodes == nodes, "live evidence JUnit nodes do not match the frozen lane")
    profile_cases = {
        lane["profile_case"]: {
            "id": lane["profile_case"],
            "state_store": "postgresql-16",
            "deployment": "aws-single-leader-landscape",
        }
    }
    profile, machine_counts = _validate_profile_report(
        profile_temp,
        manifest["evidence_id"],
        profile_cases,
        nodes,
        junit_outcomes,
    )
    _require(profile["profile_case_id"] == lane["profile_case"], "live evidence profile does not match the frozen lane")
    _require(machine_counts == manifest["result_counts"], "live evidence result counts do not match machine artifacts")
    _require(
        manifest["result_counts"]["passed"] == len(nodes)
        and manifest["result_counts"]["failed"] == 0
        and manifest["result_counts"]["errors"] == 0
        and manifest["result_counts"]["skipped"] == 0
        and manifest["result_counts"]["xfailed"] == 0
        and manifest["result_counts"]["xpassed"] == 0
        and manifest["result_counts"]["warnings"] == 0,
        "live evidence lane is not fully promotable",
    )
    started = _offset_datetime(manifest["started_at"], "live evidence started_at")
    ended = _offset_datetime(manifest["ended_at"], "live evidence ended_at")
    _require(ended >= started, "live evidence ended_at precedes started_at")
    _require(
        abs((ended - started).total_seconds() - manifest["duration_seconds"]) <= 0.001,
        "live evidence duration does not match timestamps",
    )
    destinations = _materialization_paths(assessment_path, lane_id)
    assessment_relative = assessment_path.relative_to(root).as_posix()
    required_publications = {assessment_relative, *(path.relative_to(root).as_posix() for path in destinations.values())}
    publication_paths = set(_strings(baseline.get("publication_paths"), "baseline publication_paths"))
    _require(
        required_publications <= publication_paths,
        "live evidence deterministic publication files were not frozen in baseline publication_paths",
    )
    _require(
        all(not path.exists() and path.parent == assessment_path.parent / "evidence" for path in destinations.values()),
        "live evidence lane has already been materialized",
    )
    evidence_id = _string(manifest["evidence_id"], "live evidence ID")
    existing = _list(assessment.get("evidence"), "assessment evidence")
    _require(
        all(not isinstance(item, dict) or item.get("id") != evidence_id for item in existing),
        f"duplicate evidence ID: {evidence_id}",
    )
    coverage = [
        {**_dict(raw_cell, f"live evidence lane {lane_id} cell"), "node_ids": [nodes[index]]}
        for index, raw_cell in enumerate(lane["cells"])
    ]
    retained = [
        {
            "kind": kind,
            "path": destinations[name].relative_to(root).as_posix(),
            "sha256": hashlib.sha256(envelope[name]).hexdigest(),
        }
        for name, kind in (
            ("manifest.json", "manifest"),
            ("junit.xml", "junit_xml"),
            ("profile.json", "profile_report"),
            ("scrub-report.json", "scrub_report"),
        )
    ]
    record = {
        "id": evidence_id,
        "kind": "pytest",
        "runner": "github_actions",
        "provenance": {
            "repository": manifest["repository"],
            "workflow_path": manifest["workflow_path"],
            "workflow_sha256": manifest["workflow_sha256"],
            "baseline_sha": manifest["baseline_sha"],
            "run_id": manifest["run_id"],
            "run_attempt": manifest["run_attempt"],
            "job_id": authenticated["job_id"],
            "runner_image": manifest["runner_image"],
            "selector_lane_id": lane_id,
            "artifact_sha256": authenticated["artifact_sha256"],
            "scrub_report_sha256": scrub_digest,
        },
        "argv": manifest["argv"],
        "reproducibility_class": "external_observation",
        "cwd_relative": ".",
        "timeout_seconds": manifest["timeout_seconds"],
        "safe_environment": {},
        "resources": manifest["resources"],
        "started_at": manifest["started_at"],
        "ended_at": manifest["ended_at"],
        "duration_seconds": manifest["duration_seconds"],
        "exit_code": 0,
        "result_counts": manifest["result_counts"],
        "coverage": coverage,
        "retained_artifacts": retained,
        "collected_node_index": {
            "kind": "node_index",
            "path": destinations["nodes.txt"].relative_to(root).as_posix(),
            "sha256": hashlib.sha256(envelope["nodes.txt"]).hexdigest(),
        },
        "collected_nodes": len(nodes),
        "establishes": [f"Authenticated protected live lane {lane_id} at frozen baseline {manifest['baseline_sha']}."],
        "does_not_establish": ["Any catalog cell outside the exact selector-lane coverage."],
    }
    _validate_evidence_provenance(record, assessment)
    evidence_directory = assessment_path.parent / "evidence"
    evidence_directory.mkdir(mode=0o755, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{lane_id}-", dir=evidence_directory))
    try:
        for name, destination in destinations.items():
            staged_path = staged / destination.name
            staged_path.write_bytes(envelope[name])
            os.replace(staged_path, destination)
        assessment["evidence"] = [*existing, record]
        assessment_temp = assessment_path.with_name(f".{assessment_path.name}.{lane_id}.tmp")
        assessment_temp.write_text(json.dumps(assessment, indent=2) + "\n", encoding="utf-8")
        os.replace(assessment_temp, assessment_path)
    except Exception:
        for destination in destinations.values():
            destination.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    return record


def collect_evidence(assessment_path: Path) -> int:
    return validate_evidence_artifacts(assessment_path)


def _relative_path_uses_symlink(base: Path, relative: Path) -> bool:
    current = base
    for part in relative.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def check_links(root: Path) -> int:
    documents = sorted((root / "docs/architecture/state_engine").glob("**/*.md"))
    docs_readme = root / "docs/README.md"
    if docs_readme.is_file():
        documents.append(docs_readme)
    inline_pattern = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
    reference_pattern = re.compile(r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(?:<([^>\n]+)>|(\S+))", re.MULTILINE)
    checked = 0
    missing: list[str] = []
    for document in documents:
        _require(
            not document.is_symlink(),
            f"documentation input cannot be a symlink: {document.relative_to(root)}",
        )
        text = document.read_text(encoding="utf-8")
        reference_targets = [angle or bare for angle, bare in reference_pattern.findall(text)]
        for raw_target in [*inline_pattern.findall(text), *reference_targets]:
            target = raw_target.split(maxsplit=1)[0].strip("<>")
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            checked += 1
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            relative_target = Path(path_part)
            _require(
                not relative_target.is_absolute(),
                f"link target must be repository-relative: {document.relative_to(root)} -> {target}",
            )
            candidate = document.parent / relative_target
            resolved = candidate.resolve()
            if not resolved.is_relative_to(root):
                if _relative_path_uses_symlink(document.parent, relative_target):
                    _fail(f"link target escapes through a symlink: {document.relative_to(root)} -> {target}")
                _fail(f"link target escapes the repository: {document.relative_to(root)} -> {target}")
            if not resolved.exists():
                missing.append(f"{document.relative_to(root)} -> {target}")
    _require(not missing, "missing documentation links:\n" + "\n".join(missing))
    return checked


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_catalog_parser = subparsers.add_parser("validate-catalog")
    validate_catalog_parser.add_argument("catalog", type=Path)
    init_parser = subparsers.add_parser("init-full")
    init_parser.add_argument("--catalog", required=True, type=Path)
    init_parser.add_argument("assessment_id")
    init_parser.add_argument("output_directory", type=Path)
    validate_package_parser = subparsers.add_parser("validate-package")
    validate_package_parser.add_argument("assessment", type=Path)
    collect_parser = subparsers.add_parser("collect-evidence")
    collect_parser.add_argument("assessment", type=Path)
    ingest_parser = subparsers.add_parser("ingest-live-evidence")
    ingest_parser.add_argument("assessment", type=Path)
    ingest_parser.add_argument("artifact_directory", type=Path)
    ingest_parser.add_argument("--lane", required=True)
    subparsers.add_parser("check-links")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "validate-catalog":
            catalog_path = arguments.catalog.resolve()
            validate_catalog(load_unique_json(catalog_path), catalog_path)
            print(f"state-engine catalog: valid ({catalog_path})")
        elif arguments.command == "init-full":
            output = initialize_full(arguments.assessment_id, arguments.output_directory, arguments.catalog)
            print(f"state-engine assessment initialized: {output}")
        elif arguments.command == "validate-package":
            leg_count, verdict = validate_package(arguments.assessment)
            print(f"state-engine assessment contract: valid ({leg_count} legs, {verdict})")
        elif arguments.command == "collect-evidence":
            count = collect_evidence(arguments.assessment)
            print(f"retained evidence validation: valid ({count} pytest records)")
        elif arguments.command == "ingest-live-evidence":
            record = ingest_live_evidence(arguments.assessment, arguments.artifact_directory, arguments.lane)
            print(f"live evidence ingested: {record['id']} ({arguments.lane})")
        elif arguments.command == "check-links":
            count = check_links(_git_root(Path.cwd()))
            print(f"state-engine documentation links: valid ({count} links)")
        else:
            _fail(f"unknown command: {arguments.command}")
    except (ContractError, ValueError, KeyError, OSError, ET.ParseError) as error:
        print(f"state-engine assessment: invalid: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        summary = next((line.strip() for line in str(error).splitlines() if line.strip()), "no detail")
        print(f"state-engine assessment: internal error ({type(error).__name__}): {summary}", file=sys.stderr)
        return 1
    return 0
