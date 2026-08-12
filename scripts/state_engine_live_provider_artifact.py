"""Run and seal protected live-provider pytest evidence without retaining raw streams."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

ARTIFACT_FILENAMES = {
    "manifest.json",
    "junit.xml",
    "profile.json",
    "nodes.txt",
    "scrub-report.json",
}
RAW_ARTIFACT_FILENAMES = {"junit.xml", "profile.json"}
WORKFLOW_IDENTITY = ".github/workflows/state-engine-live-provider.yml"
PRODUCER = "elspeth-state-engine-live-evidence-v1"
SCRUBBER_PRODUCER = "elspeth-live-evidence-scrubber-v1"
PROFILE_PRODUCER = "elspeth-state-engine-profile-reporter-v2"
CANARY_ENV_NAMES = (
    "ELSPETH_STATE_ENGINE_CREDENTIAL_CANARY",
    "ELSPETH_STATE_ENGINE_PROVIDER_PAYLOAD_CANARY",
)
_NODE_ID = re.compile(r"tests/[A-Za-z0-9_./-]+\.py::[^\s\x00]+\Z")
_RESOURCE_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,127}\Z")
_SENSITIVE_RESOURCE_NAME = re.compile(r".*(?:SECRET|TOKEN|KEY|PASSWORD|CREDENTIAL|CONNECTION_STRING|POSTGRES_URL).*")
EXTERNAL_CASE_IDS_BY_JOB = {
    "live_aws": (
        "sink:aws_s3@default",
        "source:aws_s3@default",
        "transform:aws_bedrock_content_safety@default",
        "transform:aws_bedrock_prompt_shield@default",
        "transform:aws_textract_document_analysis@default_chain",
        "transform:aws_textract_document_analysis@secret_refs",
        "transform:aws_textract_inline_analysis@default_chain",
        "transform:aws_textract_inline_analysis@secret_refs",
    ),
    "live_azure": (
        "sink:azure_blob@connection_string",
        "sink:azure_blob@managed_identity",
        "sink:azure_blob@sas_token",
        "sink:azure_blob@service_principal",
        "source:azure_blob@connection_string",
        "source:azure_blob@managed_identity",
        "source:azure_blob@sas_token",
        "source:azure_blob@service_principal",
        "transform:azure_content_safety@default",
        "transform:azure_document_intelligence@default",
        "transform:azure_prompt_shield@default",
    ),
    "live_dataverse": (
        "sink:dataverse@managed_identity",
        "sink:dataverse@service_principal",
        "source:dataverse@managed_identity",
        "source:dataverse@service_principal",
    ),
    "live_chroma": (
        "sink:chroma_sink@client",
        "sink:chroma_sink@persistent",
    ),
    "live_provider": (
        "source:llm@azure",
        "source:llm@bedrock",
        "source:llm@gateway",
        "source:llm@openrouter",
        "transform:llm@azure",
        "transform:llm@bedrock",
        "transform:llm@gateway",
        "transform:llm@openrouter",
        "transform:rag_retrieval@azure-search-api-key",
        "transform:rag_retrieval@azure-search-managed-identity",
        "transform:rag_retrieval@chroma-client",
        "transform:rag_retrieval@chroma-ephemeral",
        "transform:rag_retrieval@chroma-persistent",
    ),
}
_EXTERNAL_CASE_IDS = frozenset(case_id for case_ids in EXTERNAL_CASE_IDS_BY_JOB.values() for case_id in case_ids)


class ArtifactPolicyError(ValueError):
    """Raised with a static reason when live evidence is not promotable."""


def _reject(reason: str) -> NoReturn:
    raise ArtifactPolicyError(reason)


def lane_id_for_case(case_id: str) -> str:
    """Return the deterministic per-subject protected lane identity."""
    if case_id not in _EXTERNAL_CASE_IDS:
        _reject("case_id is not in the closed external-provider inventory")
    return "protected-live-" + case_id.replace(":", "-").replace("@", "-")


def _object(value: object, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        _reject(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if type(value) is not list:
        _reject(f"{label} must be an array")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value:
        _reject(f"{label} must be non-empty text")
    return value


def _read_json(path: Path, label: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ArtifactPolicyError(f"{label} is not readable strict JSON") from exc


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _selector_lane(selector_path: Path, lane_id: str) -> tuple[str, dict[str, Any]]:
    document = _object(_read_json(selector_path, "selector manifest"), "selector manifest")
    if document.get("schema_version") != 1:
        _reject("selector manifest schema version is unsupported")
    catalog_id = _text(document.get("catalog_id"), "selector catalog_id")
    lanes = _array(document.get("lanes"), "selector lanes")
    matches = [_object(item, "selector lane") for item in lanes if type(item) is dict and item.get("lane_id") == lane_id]
    if len(matches) != 1:
        _reject("selector manifest must contain the requested lane exactly once")
    return catalog_id, matches[0]


def _lane_node_ids(lane: Mapping[str, object]) -> list[str]:
    raw_node_ids = _array(lane.get("node_ids"), "selector node_ids")
    node_ids = [_text(item, "selector node_id") for item in raw_node_ids]
    if not node_ids or len(node_ids) != len(set(node_ids)):
        _reject("selector node_ids must be non-empty and unique")
    if any(_NODE_ID.fullmatch(node_id) is None for node_id in node_ids):
        _reject("selector node_id is outside the closed pytest grammar")
    return node_ids


def _lane_resources(lane: Mapping[str, object]) -> list[str]:
    raw_names = _array(lane.get("resource_variables"), "selector resource_variables")
    names = [_text(item, "selector resource variable") for item in raw_names]
    if len(names) != len(set(names)) or any(_RESOURCE_NAME.fullmatch(name) is None for name in names):
        _reject("selector resource_variables must be unique environment names")
    return names


def pytest_argv_for_lane(
    *,
    selector_path: Path,
    lane_id: str,
    python_executable: str,
    artifact_dir: Path,
) -> list[str]:
    """Build the exact fail-closed pytest argv from one reviewed selector lane."""
    _, lane = _selector_lane(selector_path, lane_id)
    marker_expression = _text(lane.get("marker_expression"), "selector marker_expression")
    if "live_provider" not in marker_expression:
        _reject("live selector marker_expression must include live_provider")
    node_ids = _lane_node_ids(lane)
    return [
        python_executable,
        "-m",
        "pytest",
        "-n",
        "0",
        "--strict-markers",
        "--strict-config",
        "--run-live-provider",
        "-p",
        "scripts.state_engine_profile_reporter",
        "--state-engine-profile-report",
        str(artifact_dir / "profile.json"),
        "--junitxml",
        str(artifact_dir / "junit.xml"),
        "-W",
        "error",
        "-rxXs",
        "-m",
        marker_expression,
        *node_ids,
    ]


def run_pytest(
    *,
    selector_path: Path,
    lane_id: str,
    artifact_dir: Path,
    stdout_path: Path,
    stderr_path: Path,
    argv_path: Path,
    run_metadata_path: Path,
    timeout_seconds: int,
) -> int:
    """Run one exact selector lane while capturing both raw streams outside the envelope."""
    if timeout_seconds <= 0:
        _reject("pytest timeout must be positive")
    artifact_dir.mkdir(parents=True, exist_ok=False)
    argv = pytest_argv_for_lane(
        selector_path=selector_path,
        lane_id=lane_id,
        python_executable=sys.executable,
        artifact_dir=artifact_dir,
    )
    _write_json(argv_path, argv)
    started_at = datetime.now(UTC)
    started_monotonic = time.monotonic()
    exit_code = 124
    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
                timeout=timeout_seconds,
            )
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        exit_code = 124
    finally:
        ended_at = datetime.now(UTC)
        duration = time.monotonic() - started_monotonic
        _write_json(
            run_metadata_path,
            {
                "started_at": started_at.isoformat().replace("+00:00", "Z"),
                "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
                "duration_seconds": duration,
                "timeout_seconds": timeout_seconds,
            },
        )
    return exit_code


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        _reject("required protected workflow environment is missing")
    return value


def _sensitive_values(secret_env_names: Sequence[str], resource_names: Sequence[str]) -> tuple[str, ...]:
    values: list[str] = []
    for name in CANARY_ENV_NAMES:
        values.append(_required_environment(name))
    if len(set(values)) != len(values) or any(len(value) < 16 for value in values):
        _reject("scrub canaries must be distinct bounded values")
    for name in secret_env_names:
        if _RESOURCE_NAME.fullmatch(name) is None:
            _reject("secret environment name is invalid")
        values.append(_required_environment(name))
    for name in resource_names:
        value = _required_environment(name)
        if len(value) >= 8:
            values.append(value)
        elif _SENSITIVE_RESOURCE_NAME.fullmatch(name) is not None:
            _reject("credential resource values must not be short or empty")
    return tuple(dict.fromkeys(values))


def _inject_canaries(
    *,
    junit_path: Path,
    profile_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    canaries: Sequence[str],
) -> None:
    try:
        tree = ET.parse(junit_path)
    except (OSError, ET.ParseError) as exc:
        raise ArtifactPolicyError("JUnit artifact is not parseable XML") from exc
    root = tree.getroot()
    properties = ET.SubElement(root, "properties")
    for index, canary in enumerate(canaries):
        ET.SubElement(properties, "property", name=f"state_engine_scrub_canary_{index}", value=canary)
    tree.write(junit_path, encoding="unicode", xml_declaration=True)

    profile = _object(_read_json(profile_path, "profile report"), "profile report")
    profile["_state_engine_scrub_canaries"] = list(canaries)
    _write_json(profile_path, profile)
    with stdout_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(canaries) + "\n")
    with stderr_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(canaries) + "\n")


def _redact_text(text: str, sensitive_values: Sequence[str]) -> tuple[str, int]:
    count = 0
    for value in sorted(sensitive_values, key=len, reverse=True):
        occurrences = text.count(value)
        if occurrences:
            text = text.replace(value, "[REDACTED]")
            count += occurrences
    return text, count


def _sanitize_junit(path: Path, sensitive_values: Sequence[str]) -> int:
    text = path.read_text(encoding="utf-8")
    redacted, count = _redact_text(text, sensitive_values)
    root = ET.fromstring(redacted)
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"system-out", "system-err", "failure", "error"}:
            element.text = "[REDACTED]"
            element.attrib.pop("message", None)
    for properties in list(root.iter("properties")):
        for child in list(properties):
            if child.attrib.get("name", "").startswith("state_engine_scrub_canary_"):
                properties.remove(child)
        if not list(properties):
            for parent in root.iter():
                if properties in list(parent):
                    parent.remove(properties)
                    break
    ET.ElementTree(root).write(path, encoding="unicode", xml_declaration=True)
    return count


def _sanitize_profile(path: Path, sensitive_values: Sequence[str]) -> tuple[dict[str, Any], int]:
    text = path.read_text(encoding="utf-8")
    redacted, count = _redact_text(text, sensitive_values)
    profile = _object(json.loads(redacted), "profile report")
    profile.pop("_state_engine_scrub_canaries", None)
    warnings = profile.get("warnings")
    if type(warnings) is list:
        for item in warnings:
            if type(item) is dict and "message" in item:
                item["message"] = "[REDACTED]"
    _write_json(path, profile)
    return profile, count


def _scrub_raw_stream(path: Path, sensitive_values: Sequence[str]) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    redacted, count = _redact_text(text, sensitive_values)
    path.write_text(redacted, encoding="utf-8")
    return count


def _validate_junit(path: Path, expected_count: int) -> None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ArtifactPolicyError("JUnit artifact is not parseable XML") from exc
    cases = list(root.iter("testcase"))
    if len(cases) != expected_count:
        _reject("JUnit test count does not match the exact selector")
    for case in cases:
        if any(child.tag.rsplit("}", 1)[-1] in {"skipped", "failure", "error"} for child in case):
            _reject("JUnit contains a non-pass outcome")


def _validate_profile(profile: Mapping[str, object], lane: Mapping[str, object]) -> tuple[list[str], dict[str, int]]:
    if profile.get("schema_version") != 2 or profile.get("producer") != PROFILE_PRODUCER:
        _reject("runtime profile identity is invalid")
    expected_profile = _text(lane.get("profile_case"), "selector profile_case")
    if profile.get("profile_case_id") != expected_profile:
        _reject("runtime profile does not match the selector lane")
    expected_nodes = _lane_node_ids(lane)
    node_ids = [_text(item, "profile node_id") for item in _array(profile.get("node_ids"), "profile node_ids")]
    if node_ids != expected_nodes:
        _reject("profile node_ids do not match the exact selector")
    outcomes = _array(profile.get("outcomes"), "profile outcomes")
    admitted: dict[str, str] = {}
    for raw_outcome in outcomes:
        outcome = _object(raw_outcome, "profile outcome")
        node_id = _text(outcome.get("node_id"), "profile outcome node_id")
        result = _text(outcome.get("outcome"), "profile outcome value")
        if node_id in admitted:
            _reject("profile contains a duplicate outcome")
        admitted[node_id] = result
    warnings = _array(profile.get("warnings"), "profile warnings")
    if admitted != dict.fromkeys(expected_nodes, "passed") or warnings:
        _reject("every exact live-provider node must pass without warnings")
    return expected_nodes, {
        "passed": len(expected_nodes),
        "failed": 0,
        "error": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "warnings": 0,
        "deselected": 0,
    }


def _validate_no_sensitive_bytes(paths: Sequence[Path], sensitive_values: Sequence[str]) -> None:
    for path in paths:
        content = path.read_bytes()
        if any(value.encode() in content for value in sensitive_values):
            _reject("scrubbing left sensitive bytes in a retained or temporary file")


def seal_artifact(
    *,
    artifact_dir: Path,
    selector_path: Path,
    lane_id: str,
    catalog_path: Path,
    workflow_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    argv_path: Path,
    run_metadata_path: Path,
    pytest_exit_code: int,
    secret_env_names: Sequence[str],
) -> None:
    """Scrub, validate, and seal the exact five-file promotable envelope."""
    try:
        if pytest_exit_code != 0:
            _reject("live-provider pytest did not complete successfully")
        existing = {path.name for path in artifact_dir.iterdir()}
        if existing != RAW_ARTIFACT_FILENAMES:
            _reject("raw artifact directory must contain only JUnit and profile reports")
        catalog_id, lane = _selector_lane(selector_path, lane_id)
        catalog = _object(_read_json(catalog_path, "proof catalog"), "proof catalog")
        if catalog.get("catalog_id") != catalog_id:
            _reject("selector and proof catalog identities disagree")
        resources = _lane_resources(lane)
        sensitive_values = _sensitive_values(secret_env_names, resources)
        canaries = tuple(_required_environment(name) for name in CANARY_ENV_NAMES)
        junit_path = artifact_dir / "junit.xml"
        profile_path = artifact_dir / "profile.json"
        _inject_canaries(
            junit_path=junit_path,
            profile_path=profile_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            canaries=canaries,
        )
        replacement_counts = {
            "junit.xml": _sanitize_junit(junit_path, sensitive_values),
        }
        profile, replacement_counts["profile.json"] = _sanitize_profile(profile_path, sensitive_values)
        replacement_counts["stdout"] = _scrub_raw_stream(stdout_path, sensitive_values)
        replacement_counts["stderr"] = _scrub_raw_stream(stderr_path, sensitive_values)
        if any(count < len(canaries) for count in replacement_counts.values()):
            _reject("scrub canaries were not exercised in every required channel")
        _validate_no_sensitive_bytes(
            (junit_path, profile_path, stdout_path, stderr_path),
            sensitive_values,
        )
        node_ids, result_counts = _validate_profile(profile, lane)
        _validate_junit(junit_path, len(node_ids))
        argv = _array(_read_json(argv_path, "pytest argv"), "pytest argv")
        if not all(type(item) is str and item for item in argv):
            _reject("pytest argv must be a string array")
        metadata = _object(_read_json(run_metadata_path, "run metadata"), "run metadata")
        timeout_seconds = metadata.get("timeout_seconds")
        duration_seconds = metadata.get("duration_seconds")
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            _reject("run metadata timeout is invalid")
        if type(duration_seconds) not in {int, float} or duration_seconds < 0 or duration_seconds > timeout_seconds:
            _reject("run metadata duration is invalid")

        (artifact_dir / "nodes.txt").write_text("\n".join(node_ids) + "\n", encoding="utf-8")
        _validate_no_sensitive_bytes(
            (junit_path, profile_path, artifact_dir / "nodes.txt"),
            sensitive_values,
        )
        _write_json(
            artifact_dir / "scrub-report.json",
            {
                "schema_version": 1,
                "producer": SCRUBBER_PRODUCER,
                "status": "passed",
                "canaries_injected": len(canaries) * 4,
                "findings": [],
                "scanned_files": ["junit.xml", "profile.json", "nodes.txt"],
            },
        )
        retained_without_manifest = sorted(ARTIFACT_FILENAMES - {"manifest.json"})
        _validate_no_sensitive_bytes(
            [artifact_dir / name for name in retained_without_manifest],
            sensitive_values,
        )
        baseline_sha = _required_environment("GITHUB_SHA")
        head_sha = _required_environment("STATE_ENGINE_HEAD_SHA")
        if not re.fullmatch(r"[0-9a-f]{40}", baseline_sha) or head_sha != baseline_sha:
            _reject("checkout HEAD does not match the protected baseline SHA")
        _write_json(
            artifact_dir / "manifest.json",
            {
                "schema_version": 1,
                "producer": PRODUCER,
                "evidence_id": f"live-{lane_id}-{baseline_sha}",
                "lane_id": lane_id,
                "catalog_id": catalog_id,
                "catalog_sha256": _sha256(catalog_path),
                "repository": _required_environment("GITHUB_REPOSITORY"),
                "workflow_path": WORKFLOW_IDENTITY,
                "workflow_sha256": _sha256(workflow_path),
                "baseline_sha": baseline_sha,
                "run_id": _required_environment("GITHUB_RUN_ID"),
                "run_attempt": _required_environment("GITHUB_RUN_ATTEMPT"),
                "runner_image": _required_environment("STATE_ENGINE_RUNNER_IMAGE"),
                "ref": _required_environment("GITHUB_REF"),
                "head_sha": head_sha,
                "artifact_name": f"state-engine-live-{lane_id}",
                "started_at": _text(metadata.get("started_at"), "run metadata started_at"),
                "ended_at": _text(metadata.get("ended_at"), "run metadata ended_at"),
                "duration_seconds": duration_seconds,
                "timeout_seconds": timeout_seconds,
                "argv": argv,
                "resources": resources,
                "result_counts": result_counts,
                "files": {name: _sha256(artifact_dir / name) for name in retained_without_manifest},
            },
        )
        if {path.name for path in artifact_dir.iterdir()} != ARTIFACT_FILENAMES:
            _reject("sealed artifact directory does not contain the exact five-file envelope")
        _validate_no_sensitive_bytes(list(artifact_dir.iterdir()), sensitive_values)
    finally:
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    lane_parser = subparsers.add_parser("lane-id")
    lane_parser.add_argument("--case-id", required=True)
    run_parser = subparsers.add_parser("run-pytest")
    run_parser.add_argument("--selector-manifest", type=Path, required=True)
    run_parser.add_argument("--lane", required=True)
    run_parser.add_argument("--artifact-dir", type=Path, required=True)
    run_parser.add_argument("--stdout", type=Path, required=True)
    run_parser.add_argument("--stderr", type=Path, required=True)
    run_parser.add_argument("--argv-output", type=Path, required=True)
    run_parser.add_argument("--run-metadata-output", type=Path, required=True)
    run_parser.add_argument("--timeout-seconds", type=int, required=True)

    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("--selector-manifest", type=Path, required=True)
    seal_parser.add_argument("--lane", required=True)
    seal_parser.add_argument("--catalog", type=Path, required=True)
    seal_parser.add_argument("--workflow", type=Path, required=True)
    seal_parser.add_argument("--artifact-dir", type=Path, required=True)
    seal_parser.add_argument("--stdout", type=Path, required=True)
    seal_parser.add_argument("--stderr", type=Path, required=True)
    seal_parser.add_argument("--argv", type=Path, required=True)
    seal_parser.add_argument("--run-metadata", type=Path, required=True)
    seal_parser.add_argument("--pytest-exit-code", type=int, required=True)
    seal_parser.add_argument("--secret-env", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "lane-id":
            sys.stdout.write(f"lane_id={lane_id_for_case(args.case_id)}\n")
            return 0
        if args.command == "run-pytest":
            return run_pytest(
                selector_path=args.selector_manifest,
                lane_id=args.lane,
                artifact_dir=args.artifact_dir,
                stdout_path=args.stdout,
                stderr_path=args.stderr,
                argv_path=args.argv_output,
                run_metadata_path=args.run_metadata_output,
                timeout_seconds=args.timeout_seconds,
            )
        seal_artifact(
            artifact_dir=args.artifact_dir,
            selector_path=args.selector_manifest,
            lane_id=args.lane,
            catalog_path=args.catalog,
            workflow_path=args.workflow,
            stdout_path=args.stdout,
            stderr_path=args.stderr,
            argv_path=args.argv,
            run_metadata_path=args.run_metadata,
            pytest_exit_code=args.pytest_exit_code,
            secret_env_names=tuple(args.secret_env),
        )
    except ArtifactPolicyError as exc:
        sys.stderr.write(f"state-engine live evidence refused: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
