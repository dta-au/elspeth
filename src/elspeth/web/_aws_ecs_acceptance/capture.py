"""Pipeline capture and local verification for AWS ECS acceptance."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast
from urllib.parse import quote

import httpx
import yaml

from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.yaml_generator import generate_public_yaml
from elspeth.web.config import settings_from_env

from .contracts import (
    _TERMINAL_RUN_STATUSES,
    RUN_POLL_DEADLINE_SECONDS,
    RUN_POLL_INTERVAL_SECONDS,
    AcceptanceCheckError,
    AcceptanceInputError,
    _artifact_id_field,
    _canonical_uuid,
    _mapping,
    _sha256,
    _sha256_field,
    _string_field,
    _utc_timestamp,
    _uuid_field,
    acceptance_step,
    check_error_with_cause,
)
from .http_client import AcceptanceHttpClient
from .state import AcceptanceState, read_acceptance_state, write_acceptance_state

FIXED_INPUT_BYTES = b"id,name\n1,alpha\n"

TUTORIAL_INPUT_BYTES = b"url\nhttps://example.invalid/\n"

_CONTAINER_RUNTIME_UID = 1654
_CONTAINER_RUNTIME_GID = 1654


def build_fixed_pipeline_yaml(*, session_id: str, source_path: str = "blobs/aws-ecs-acceptance-input.csv") -> str:
    """Return the fixed no-LLM CSV source-to-sink acceptance pipeline."""

    canonical_session_id = _canonical_uuid(session_id, label="session identity")
    return yaml.safe_dump(_fixed_pipeline_document(canonical_session_id, source_path=source_path), sort_keys=False)


def _fixed_pipeline_document(canonical_session_id: str, *, source_path: str) -> dict[str, object]:
    return {
        "sources": {
            "source": {
                "plugin": "csv",
                "on_success": "output",
                "on_validation_failure": "discard",
                "options": {
                    "path": source_path,
                    "delimiter": ",",
                    "encoding": "utf-8",
                    "schema": {"mode": "fixed", "fields": ["id: int", "name: str"]},
                },
            }
        },
        "sinks": {
            "output": {
                "plugin": "csv",
                "on_write_failure": "discard",
                "options": {
                    "path": f"outputs/aws-ecs-acceptance-{canonical_session_id}.csv",
                    "delimiter": ",",
                    "encoding": "utf-8",
                    "mode": "write",
                    "collision_policy": "fail_if_exists",
                    "schema": {"mode": "fixed", "fields": ["id: int", "name: str"]},
                },
            }
        },
    }


@trust_boundary(
    tier=3,
    source="ELSPETH_WEB__DATA_DIR in the acceptance harness process environment, set from the deployment inventory",
    source_param="env",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceInputError before any request when ELSPETH_WEB__DATA_DIR is absent, is not a str, is empty, "
        "is not an absolute POSIX path, or does not equal its own canonical form (a trailing slash, a '.' segment, or "
        "a '..' segment); never coerces or normalises the operator value"
    ),
    test_ref="tests/unit/web/aws_ecs_acceptance/test_http_capture.py::test_server_data_dir_rejects_a_noncanonical_deployment_data_dir",
    test_fingerprint="f2b79dd91b3591ba0f31574cecfd4f227c8ac1bf894bda7161ca307a20871214",
)
def _server_data_dir(env: Mapping[str, str]) -> str:
    """Return the canonical absolute data directory the web service runs with.

    The value must be the server's own canonical data dir (the deployment
    inventory's ``ELSPETH_WEB__DATA_DIR``, e.g. ``/var/lib/elspeth``): the
    server rewrites relative sink paths under it before execution, and the
    expected sink node identity is derived from that rewritten path.
    """

    data_dir = env.get("ELSPETH_WEB__DATA_DIR")
    if type(data_dir) is not str or not data_dir:
        raise AcceptanceInputError("ELSPETH_WEB__DATA_DIR must be the canonical absolute data dir the web service runs with")
    path = PurePosixPath(data_dir)
    if not path.is_absolute() or str(path) != data_dir or any(part in {".", ".."} for part in path.parts):
        raise AcceptanceInputError("ELSPETH_WEB__DATA_DIR must be the canonical absolute data dir the web service runs with")
    return data_dir


def _fixed_output_sink_node_id(session_id: str, *, data_dir: str) -> str:
    canonical_session_id = _canonical_uuid(session_id, label="session identity")
    document = _fixed_pipeline_document(canonical_session_id, source_path="blobs/aws-ecs-acceptance-input.csv")
    sinks = cast(dict[str, dict[str, object]], document["sinks"])
    sink_options = dict(cast(dict[str, object], sinks["output"]["options"]))
    # Mirror the server-side resolve_sink_data_path() rewrite textually:
    # the client filesystem is not the server filesystem, so the resolved
    # path is composed as a string, never via Path.resolve().
    sink_options["path"] = str(
        PurePosixPath(data_dir) / "outputs" / canonical_session_id / f"aws-ecs-acceptance-{canonical_session_id}.csv"
    )
    return f"sink_output_{_sha256(canonical_json(sink_options).encode('utf-8'))[:12]}"


def _canonical_tutorial_policy_state(*, profile_alias: str) -> CompositionState:
    """Materialize the product's canonical core-only tutorial candidate.

    The graph is an inlined acceptance fixture, frozen from the retired recipe
    scaffolding it used to be generated by. It is harness input for the AWS ECS
    acceptance capture — never a composer authoring path, and never surfaced to
    a user as a proposal.
    """

    return CompositionState.from_dict(
        {
            "source": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {"schema": {"mode": "observed"}},
                "on_validation_failure": "discard",
            },
            "nodes": [
                {
                    "id": "url_rows",
                    "node_type": "transform",
                    "plugin": "web_scrape",
                    "input": "rows",
                    "on_success": "scraped",
                    "on_error": "discard",
                    "options": {
                        "schema": {"mode": "observed"},
                        "url_field": "url",
                        "content_field": "content",
                        "fingerprint_field": "content_fingerprint",
                        "format": "markdown",
                        "http": {
                            "abuse_contact": "aws-ecs-acceptance@example.invalid",
                            "scraping_reason": "AWS ECS tutorial policy acceptance",
                        },
                    },
                },
                {
                    "id": "rate_pages",
                    "node_type": "transform",
                    "plugin": "llm",
                    "input": "scraped",
                    "on_success": "rated",
                    "on_error": "discard",
                    "options": {
                        "profile": profile_alias,
                        "prompt_template": (
                            "Rate the appeal of this government web page from 1-10 and explain briefly:\n\n{{ row['content'] }}"
                        ),
                        "response_field": "rating",
                        "schema": {"mode": "observed"},
                        "required_input_fields": ["content"],
                    },
                },
                {
                    "id": "drop_raw_html",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "rated",
                    "on_success": "clean",
                    "on_error": "discard",
                    "options": {
                        "schema": {"mode": "observed"},
                        "select_only": True,
                        "mapping": {"url": "url", "rating": "rating"},
                        "interpretation_requirements": [
                            {
                                "kind": "pipeline_decision",
                                "user_term": "drop_raw_html_fields",
                                "draft": ("Drop the scraped raw HTML and fingerprint fields before saving the output."),
                            }
                        ],
                    },
                },
            ],
            "edges": [],
            "outputs": [
                {
                    "name": "clean",
                    "plugin": "json",
                    "options": {
                        "path": "outputs/tutorial-policy-acceptance.jsonl",
                        "format": "jsonl",
                        "schema": {"mode": "observed"},
                        "mode": "write",
                        "collision_policy": "auto_increment",
                    },
                    "on_write_failure": "discard",
                }
            ],
            "metadata": {
                "name": "web-scrape-llm-project-jsonl",
                "description": (
                    "Scrape each locator in 'url', store the LLM response in 'rating', "
                    "retain exactly ['url', 'rating'], and write JSONL to "
                    "outputs/tutorial-policy-acceptance.jsonl"
                ),
            },
            "version": 1,
        }
    )


def build_canonical_tutorial_pipeline_yaml(*, profile_alias: str) -> str:
    """Return the public-import form of the canonical core-only tutorial."""

    if type(profile_alias) is not str or not profile_alias.strip() or profile_alias != profile_alias.strip():
        raise AcceptanceInputError("tutorial profile alias is invalid")
    return generate_public_yaml(_canonical_tutorial_policy_state(profile_alias=profile_alias))


@trust_boundary(
    tier=3,
    source="JSON body of GET /api/runs/{run_id} and GET /api/runs/{run_id}/results returned by the deployed web service",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError before use on a run payload that is not a mapping, whose status is not exactly "
        "'completed', whose landscape_run_id is not a canonical UUID, whose accounting, accounting.source or "
        "accounting.tokens member is not a mapping, whose source.rows_processed is not a positive int, or whose "
        "tokens.failed is not int 0; never coerces external values"
    ),
    test_ref=(
        "tests/unit/web/aws_ecs_acceptance/test_http_capture.py::test_run_facts_rejects_a_run_payload_that_is_not_a_completed_clean_run"
    ),
    test_fingerprint="fb512cf4cca7a4bce4223fa0f7b1fefe209a7c360840e91351c36552f836067c",
)
def _run_facts(payload: object, *, check: str) -> tuple[str, int, int]:
    run = _mapping(payload, check=check)
    if _string_field(run, "status", check=check) != "completed":
        raise AcceptanceCheckError(check)
    landscape_run_id = _uuid_field(run, "landscape_run_id", check=check)
    accounting = _mapping(run.get("accounting"), check=check)
    source = _mapping(accounting.get("source"), check=check)
    tokens = _mapping(accounting.get("tokens"), check=check)
    source_rows = source.get("rows_processed")
    failed_tokens = tokens.get("failed")
    if type(source_rows) is not int or source_rows <= 0:
        raise AcceptanceCheckError(check)
    if type(failed_tokens) is not int or failed_tokens != 0:
        raise AcceptanceCheckError(check)
    return landscape_run_id, source_rows, failed_tokens


@trust_boundary(
    tier=3,
    source="JSON body of GET /api/runs/{run_id}/outputs (the artifact manifest) returned by the deployed web service",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError before use on a manifest that is not a mapping, whose artifacts member is not a "
        "list, that does not carry exactly one artifact bound to the expected sink node id, or whose selected artifact "
        "is not a file or sink_file that reports exists_now and downloadable True with a well-formed artifact_id and "
        "sha256 content_hash; never coerces external values"
    ),
    test_ref=(
        "tests/unit/web/aws_ecs_acceptance/test_http_capture.py::"
        "test_select_output_artifact_rejects_a_manifest_without_one_downloadable_matching_file"
    ),
    test_fingerprint="8ad6cf8930fee497245f7619b011679d8f1610a7a40b10eb410bc358ae8f2704",
)
def _select_output_artifact(payload: object, *, expected_sink_node_id: str, check: str) -> tuple[str, str]:
    manifest = _mapping(payload, check=check)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise AcceptanceCheckError(check)
    matches = [artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get("sink_node_id") == expected_sink_node_id]
    if len(matches) != 1:
        raise AcceptanceCheckError(check)
    artifact = matches[0]
    if artifact.get("artifact_type") not in {"file", "sink_file"}:
        raise AcceptanceCheckError(check)
    if artifact.get("exists_now") is not True or artifact.get("downloadable") is not True:
        raise AcceptanceCheckError(check)
    return _artifact_id_field(artifact, "artifact_id", check=check), _sha256_field(artifact, "content_hash", check=check)


@trust_boundary(
    tier=3,
    source="JSON body of POST /api/sessions/{session_id}/state/yaml returned by the deployed web service",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError when the state-import response is not a mapping or its is_valid member is not "
        "exactly True; never coerces external values"
    ),
    test_ref=(
        "tests/unit/web/aws_ecs_acceptance/test_http_capture.py::test_require_import_valid_rejects_a_state_import_that_did_not_validate"
    ),
    test_fingerprint="acbf8a049c930c075cb82c0df8abc497280217b28348abe4f6894041955e33a1",
)
def _require_import_valid(payload: object, *, check: str) -> None:
    """Require a state/yaml import response that reports a valid pipeline."""

    imported = _mapping(payload, check=check)
    if imported.get("is_valid") is not True:
        raise AcceptanceCheckError(check)


@trust_boundary(
    tier=3,
    source="JSON body of POST /api/sessions/{session_id}/state/yaml for the canonical tutorial candidate",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError when the tutorial state-import response is not a mapping or its is_valid member is "
        "not exactly False -- the canonical core-only tutorial candidate must fail required-control coverage, so a valid "
        "import is itself the failure; never coerces external values"
    ),
    test_ref=(
        "tests/unit/web/aws_ecs_acceptance/test_http_capture.py::"
        "test_require_import_rejected_rejects_a_tutorial_import_that_was_not_refused"
    ),
    test_fingerprint="4fd5e286a24f2db18b7b0735c022b4dbe6e05ba220d317c9796a379d9fb8efcb",
)
def _require_import_rejected(payload: object, *, check: str) -> None:
    """Require a state/yaml import response that reports an invalid pipeline."""

    imported = _mapping(payload, check=check)
    if imported.get("is_valid") is not False:
        raise AcceptanceCheckError(check)


@trust_boundary(
    tier=3,
    source="JSON body of POST /api/sessions/{session_id}/validate returned by the deployed web service",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError when the validate response is not a mapping, its readiness member is not a mapping, "
        "its is_valid member is not exactly True, or readiness.execution_ready is not exactly True; never coerces "
        "external values"
    ),
    test_ref=(
        "tests/unit/web/aws_ecs_acceptance/test_http_capture.py::"
        "test_require_execution_ready_rejects_a_validate_body_that_is_not_execution_ready"
    ),
    test_fingerprint="75335b535169244c075ffcb1eae1e7895b12d7ae9847410646dee6504a64b572",
)
def _require_execution_ready(payload: object, *, check: str) -> None:
    """Require a validate response that reports an execution-ready pipeline."""

    validated = _mapping(payload, check=check)
    readiness = _mapping(validated.get("readiness"), check=check)
    if validated.get("is_valid") is not True or readiness.get("execution_ready") is not True:
        raise AcceptanceCheckError(check)


@trust_boundary(
    tier=3,
    source="ELSPETH_ACCEPTANCE_REGISTER and ELSPETH_WEB__DEFAULT_LLM_PROFILE in the acceptance harness process environment",
    source_param="env",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceInputError before any HTTP request when ELSPETH_ACCEPTANCE_REGISTER is present and is "
        "neither '0' nor '1', when ELSPETH_WEB__DEFAULT_LLM_PROFILE is absent, is not a str, is empty or blank, or "
        "carries leading or trailing whitespace, or when _server_data_dir rejects ELSPETH_WEB__DATA_DIR; never "
        "coerces or defaults an operator environment value"
    ),
    test_ref=(
        "tests/unit/web/aws_ecs_acceptance/test_http_capture.py::"
        "test_capture_rejects_missing_or_noncanonical_server_data_dir_before_any_request"
    ),
    test_fingerprint="a35dfb625c84ce74cf0904e8a04b7b36514aee0d86bce12ea7f44c41e12847ab",
)
def capture(
    env: Mapping[str, str],
    *,
    state_file: Path,
    transport: httpx.BaseTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> AcceptanceState:
    """Capture one fixed public-API run and atomically persist its safe state."""

    with acceptance_step("env_validate"):
        register_value = env.get("ELSPETH_ACCEPTANCE_REGISTER")
        if register_value not in {None, "0", "1"}:
            raise AcceptanceInputError("ELSPETH_ACCEPTANCE_REGISTER must be 0 or 1")
        tutorial_profile = env.get("ELSPETH_WEB__DEFAULT_LLM_PROFILE")
        if type(tutorial_profile) is not str or not tutorial_profile.strip() or tutorial_profile != tutorial_profile.strip():
            raise AcceptanceInputError("tutorial profile alias is invalid")
        server_data_dir = _server_data_dir(env)
    captured_at = _utc_timestamp(now())
    client = AcceptanceHttpClient.from_env(env, transport=transport)
    with acceptance_step("env_validate"):
        register = register_value == "1"
        if register and client.credentials.mode != "local":
            raise AcceptanceInputError("registration is available only for local acceptance authentication")

    uploaded_sha256 = _sha256(FIXED_INPUT_BYTES)
    with client, acceptance_step("capture_fetch"):
        client.authenticate(register=register)
        session = _mapping(client.request_json("POST", "/api/sessions", expected_statuses={201}, json_body={}), check="session_create")
        session_id = _uuid_field(session, "id", check="session_create")

        blob = _mapping(
            client.request_multipart_json(
                "POST",
                f"/api/sessions/{session_id}/blobs",
                expected_statuses={201},
                files={"file": ("aws-ecs-acceptance.csv", FIXED_INPUT_BYTES, "text/csv")},
            ),
            check="blob_upload",
        )
        blob_id = _uuid_field(blob, "id", check="blob_upload")
        if _sha256_field(blob, "content_hash", check="blob_upload") != uploaded_sha256:
            raise AcceptanceCheckError("blob_upload_integrity")

        _require_import_valid(
            client.request_json(
                "POST",
                f"/api/sessions/{session_id}/state/yaml",
                expected_statuses={200},
                json_body={
                    "yaml": build_fixed_pipeline_yaml(session_id=session_id),
                    "source_blob_ids": {"source": blob_id},
                },
            ),
            check="yaml_import",
        )
        _require_execution_ready(
            client.request_json("POST", f"/api/sessions/{session_id}/validate", expected_statuses={200}),
            check="pipeline_validate",
        )

        launched = _mapping(
            client.request_json("POST", f"/api/sessions/{session_id}/execute", expected_statuses={202}),
            check="run_launch",
        )
        run_id = _uuid_field(launched, "run_id", check="run_launch")
        deadline = monotonic() + RUN_POLL_DEADLINE_SECONDS
        while True:
            run = _mapping(client.request_json("GET", f"/api/runs/{run_id}", expected_statuses={200}), check="run_status")
            status = _string_field(run, "status", check="run_status")
            if status in _TERMINAL_RUN_STATUSES:
                break
            if monotonic() >= deadline:
                raise AcceptanceCheckError("run_poll_timeout")
            sleep(RUN_POLL_INTERVAL_SECONDS)
        landscape_run_id, source_rows, failed_tokens = _run_facts(run, check="run_terminal")

        results = client.request_json("GET", f"/api/runs/{run_id}/results", expected_statuses={200})
        if _run_facts(results, check="run_results") != (landscape_run_id, source_rows, failed_tokens):
            raise AcceptanceCheckError("run_results")
        manifest = client.request_json("GET", f"/api/runs/{run_id}/outputs", expected_statuses={200})
        artifact_id, manifest_artifact_sha256 = _select_output_artifact(
            manifest,
            expected_sink_node_id=_fixed_output_sink_node_id(session_id, data_dir=server_data_dir),
            check="artifact_manifest",
        )
        artifact_content = client.request_bytes(
            "GET",
            f"/api/runs/{run_id}/outputs/{artifact_id}/content",
            expected_statuses={200},
        )
        artifact_sha256 = _sha256(artifact_content)
        if artifact_sha256 != manifest_artifact_sha256:
            raise AcceptanceCheckError("artifact_integrity")
        blob_content = client.request_bytes(
            "GET",
            f"/api/sessions/{session_id}/blobs/{blob_id}/content",
            expected_statuses={200},
        )
        blob_sha256 = _sha256(blob_content)
        if blob_sha256 != uploaded_sha256:
            raise AcceptanceCheckError("blob_integrity")

        tutorial_session = _mapping(
            client.request_json("POST", "/api/sessions", expected_statuses={201}, json_body={}),
            check="tutorial_session_create",
        )
        tutorial_session_id = _uuid_field(tutorial_session, "id", check="tutorial_session_create")
        tutorial_blob = _mapping(
            client.request_multipart_json(
                "POST",
                f"/api/sessions/{tutorial_session_id}/blobs",
                expected_statuses={201},
                files={"file": ("aws-ecs-tutorial-policy.csv", TUTORIAL_INPUT_BYTES, "text/csv")},
            ),
            check="tutorial_blob_upload",
        )
        tutorial_blob_id = _uuid_field(tutorial_blob, "id", check="tutorial_blob_upload")
        if _sha256_field(tutorial_blob, "content_hash", check="tutorial_blob_upload") != _sha256(TUTORIAL_INPUT_BYTES):
            raise AcceptanceCheckError("tutorial_blob_upload")
        _require_import_rejected(
            client.request_json(
                "POST",
                f"/api/sessions/{tutorial_session_id}/state/yaml",
                expected_statuses={200},
                json_body={
                    "yaml": build_canonical_tutorial_pipeline_yaml(profile_alias=tutorial_profile),
                    "source_blob_ids": {"source": tutorial_blob_id},
                },
            ),
            check="tutorial_state_import",
        )

    state = AcceptanceState(
        schema_version=1,
        session_id=session_id,
        tutorial_session_id=tutorial_session_id,
        blob_id=blob_id,
        run_id=run_id,
        landscape_run_id=landscape_run_id,
        artifact_id=artifact_id,
        uploaded_sha256=uploaded_sha256,
        blob_sha256=blob_sha256,
        artifact_sha256=artifact_sha256,
        run_status="completed",
        source_rows=source_rows,
        failed_tokens=failed_tokens,
        captured_at=captured_at,
        completed_at=_utc_timestamp(now()),
    )
    write_acceptance_state(state_file, state)
    return state


@trust_boundary(
    tier=3,
    source="JSON body of GET /api/system/status returned by the deployed web service",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError before use when the status body is not a mapping, its plugin_policy_readiness "
        "member is not a mapping, tutorial_ready is not exactly True on both the status body and the readiness block, "
        "the readiness rows are not a list of mappings each carrying a unique str id and a str status, or the observed "
        "row ids are not exactly the six required readiness controls with policy_compilation, required_core and "
        "local_capability_configuration reporting 'ok' and no row reporting 'error'; never coerces external values"
    ),
    test_ref=(
        "tests/unit/web/aws_ecs_acceptance/test_http_capture.py::test_require_plugin_policy_ready_rejects_an_incomplete_readiness_body"
    ),
    test_fingerprint="9401862e274fb84b30a99be733185e4b4c4a3ebfda62e7b1a6306df2252f4b3a",
)
def _require_plugin_policy_ready(payload: object) -> None:
    """Require the deployed service to report every plugin-policy readiness control."""

    status = _mapping(payload, check="plugin_policy_readiness")
    readiness = _mapping(status.get("plugin_policy_readiness"), check="plugin_policy_readiness")
    rows = readiness.get("rows")
    if status.get("tutorial_ready") is not True or readiness.get("tutorial_ready") is not True or not isinstance(rows, list):
        raise AcceptanceCheckError("plugin_policy_readiness")
    expected_ids = {
        "policy_compilation",
        "required_core",
        "local_capability_configuration",
        "live_health",
        "tutorial_profile",
        "tutorial_required_control_coverage",
    }
    statuses: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise AcceptanceCheckError("plugin_policy_readiness")
        row_id = row.get("id")
        row_status = row.get("status")
        if type(row_id) is not str or row_id in statuses or type(row_status) is not str:
            raise AcceptanceCheckError("plugin_policy_readiness")
        statuses[row_id] = row_status
    if (
        set(statuses) != expected_ids
        or statuses["policy_compilation"] != "ok"
        or statuses["required_core"] != "ok"
        or statuses["local_capability_configuration"] != "ok"
        or any(value == "error" for value in statuses.values())
    ):
        raise AcceptanceCheckError("plugin_policy_readiness")


@trust_boundary(
    tier=3,
    source="JSON body of the 409 rejection returned by POST /api/tutorial/run for the canonical tutorial session",
    source_param="payload",
    suppresses=("R1", "R5"),
    invariant=(
        "raises AcceptanceCheckError when the rejection body is not a mapping, its detail member is not a mapping, or "
        "detail.error_type and detail.code are not exactly 'tutorial_not_ready' and "
        "'tutorial_required_control_coverage'; never coerces external values"
    ),
    test_ref=(
        "tests/unit/web/aws_ecs_acceptance/test_http_capture.py::test_require_tutorial_not_ready_rejects_a_non_contractual_rejection_body"
    ),
    test_fingerprint="9cedf02f813de79d119e01d2d12831dc7ac3c478b55211ea608405e0ffb9ca4e",
)
def _require_tutorial_not_ready(payload: object) -> None:
    """Require the tutorial launch rejection to name required-control coverage."""

    rejected = _mapping(payload, check="tutorial_required_control_coverage")
    detail = _mapping(rejected.get("detail"), check="tutorial_required_control_coverage")
    if detail.get("error_type") != "tutorial_not_ready" or detail.get("code") != "tutorial_required_control_coverage":
        raise AcceptanceCheckError("tutorial_required_control_coverage")


def _verify_plugin_policy_http_contract(client: AcceptanceHttpClient, *, tutorial_session_id: str) -> None:
    _require_plugin_policy_ready(client.request_json("GET", "/api/system/status", expected_statuses={200}))
    _require_tutorial_not_ready(
        client.request_json(
            "POST",
            "/api/tutorial/run",
            expected_statuses={409},
            json_body={"session_id": tutorial_session_id},
        )
    )


def verify_api(
    env: Mapping[str, str],
    *,
    state_file: Path,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Re-authenticate and verify the captured API resources without mutation."""

    state = read_acceptance_state(state_file)
    with acceptance_step("env_validate"):
        server_data_dir = _server_data_dir(env)
    client = AcceptanceHttpClient.from_env(env, transport=transport)
    with client, acceptance_step("verify_fetch"):
        client.authenticate(register=False)
        session = _mapping(
            client.request_json("GET", f"/api/sessions/{state.session_id}", expected_statuses={200}),
            check="session_readback",
        )
        if _uuid_field(session, "id", check="session_readback") != state.session_id:
            raise AcceptanceCheckError("session_readback")
        blob = _mapping(
            client.request_json(
                "GET",
                f"/api/sessions/{state.session_id}/blobs/{state.blob_id}",
                expected_statuses={200},
            ),
            check="blob_metadata_readback",
        )
        if (
            _uuid_field(blob, "id", check="blob_metadata_readback") != state.blob_id
            or _uuid_field(blob, "session_id", check="blob_metadata_readback") != state.session_id
            or _sha256_field(blob, "content_hash", check="blob_metadata_readback") != state.blob_sha256
        ):
            raise AcceptanceCheckError("blob_metadata_readback")
        blob_content = client.request_bytes(
            "GET",
            f"/api/sessions/{state.session_id}/blobs/{state.blob_id}/content",
            expected_statuses={200},
        )
        if _sha256(blob_content) != state.blob_sha256:
            raise AcceptanceCheckError("blob_integrity")

        expected_facts = (state.landscape_run_id, state.source_rows, state.failed_tokens)
        run = client.request_json("GET", f"/api/runs/{state.run_id}", expected_statuses={200})
        if _run_facts(run, check="run_readback") != expected_facts:
            raise AcceptanceCheckError("run_readback")
        results = client.request_json("GET", f"/api/runs/{state.run_id}/results", expected_statuses={200})
        if _run_facts(results, check="results_readback") != expected_facts:
            raise AcceptanceCheckError("results_readback")
        manifest = client.request_json("GET", f"/api/runs/{state.run_id}/outputs", expected_statuses={200})
        artifact_id, artifact_sha256 = _select_output_artifact(
            manifest,
            expected_sink_node_id=_fixed_output_sink_node_id(state.session_id, data_dir=server_data_dir),
            check="artifact_manifest",
        )
        if artifact_id != state.artifact_id or artifact_sha256 != state.artifact_sha256:
            raise AcceptanceCheckError("artifact_manifest")
        artifact_content = client.request_bytes(
            "GET",
            f"/api/runs/{state.run_id}/outputs/{state.artifact_id}/content",
            expected_statuses={200},
        )
        if _sha256(artifact_content) != state.artifact_sha256:
            raise AcceptanceCheckError("artifact_integrity")

        _verify_plugin_policy_http_contract(client, tutorial_session_id=state.tutorial_session_id)

    return {
        "check": "verify-api",
        "ok": True,
        "source_rows": state.source_rows,
        "failed_tokens": state.failed_tokens,
        "plugin_policy_ready": True,
        "tutorial_required_control_coverage": True,
    }


def verify_local_auth() -> dict[str, object]:
    """Verify the drained one-shot local-auth database contract read-only."""

    settings = settings_from_env()
    if settings.auth_provider != "local":
        raise AcceptanceCheckError("auth_provider_local")
    auth_db = settings.data_dir / "auth.db"
    if not auth_db.is_file():
        raise AcceptanceCheckError("auth_db_exists")

    uri = f"file:{quote(str(auth_db.resolve()), safe='/')}?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        row = connection.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.Error:
        raise AcceptanceCheckError("auth_db_read_only") from None
    finally:
        if connection is not None:
            connection.close()
    if row is None or len(row) != 1 or type(row[0]) is not str or row[0].lower() != "delete":
        raise AcceptanceCheckError("journal_mode_delete")
    return {
        "check": "verify-local-auth",
        "ok": True,
        "checks": {"auth_provider_local": True, "auth_db_exists": True, "journal_mode_delete": True},
    }


def _storage_metadata(path: Path) -> os.stat_result:
    """Return ownership metadata for a provisioned runtime directory."""

    return path.lstat()


def provision_storage() -> dict[str, object]:
    """Create and prove the required EFS-backed directories as the image user."""

    try:
        settings = settings_from_env()
        data_dir = settings.data_dir
        if settings.payload_store_path is None:
            raise AcceptanceCheckError("storage_settings")
        payload_root = settings.get_payload_store_path()
    except AcceptanceCheckError:
        raise
    except Exception as exc:
        raise check_error_with_cause("storage_settings", exc) from None
    if os.geteuid() != _CONTAINER_RUNTIME_UID or os.getegid() != _CONTAINER_RUNTIME_GID:
        raise AcceptanceCheckError("storage_identity")
    if not isinstance(data_dir, Path) or not isinstance(payload_root, Path):
        raise AcceptanceCheckError("storage_settings")
    if data_dir.is_symlink() or not data_dir.is_dir():
        raise AcceptanceCheckError("storage_root")
    blob_root = data_dir / "blobs"
    try:
        data_resolved = data_dir.resolve(strict=True)
        preflight_roots = (payload_root.resolve(strict=False), blob_root.resolve(strict=False))
    except OSError:
        raise AcceptanceCheckError("storage_provision") from None
    if (
        len({data_resolved, *preflight_roots}) != 3
        or any(path == data_resolved or not path.is_relative_to(data_resolved) for path in preflight_roots)
        or payload_root.is_symlink()
        or blob_root.is_symlink()
    ):
        raise AcceptanceCheckError("storage_boundary")
    try:
        for path in (payload_root, blob_root):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        roots = (data_dir, payload_root, blob_root)
        resolved_roots = tuple(path.resolve(strict=True) for path in roots)
    except OSError:
        raise AcceptanceCheckError("storage_provision") from None
    if (
        len(set(resolved_roots)) != 3
        or resolved_roots[1] == data_resolved
        or resolved_roots[2] == data_resolved
        or not resolved_roots[1].is_relative_to(data_resolved)
        or not resolved_roots[2].is_relative_to(data_resolved)
        or any(path.is_symlink() for path in roots)
    ):
        raise AcceptanceCheckError("storage_boundary")

    probe_bytes = b"elspeth-efs-storage-probe\n"
    for path in roots:
        probe: Path | None = None
        try:
            metadata = _storage_metadata(path)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != _CONTAINER_RUNTIME_UID or metadata.st_gid != _CONTAINER_RUNTIME_GID:
                raise AcceptanceCheckError("storage_ownership")
            probe = path / f".elspeth-probe-{uuid.uuid4().hex}"
            descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                os.write(descriptor, probe_bytes)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if probe.read_bytes() != probe_bytes:
                raise AcceptanceCheckError("storage_probe")
            probe.unlink()
            directory_descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except AcceptanceCheckError:
            raise
        except OSError:
            raise AcceptanceCheckError("storage_probe") from None
        finally:
            if probe is not None:
                probe.unlink(missing_ok=True)

    return {
        "check": "provision-storage",
        "ok": True,
        "uid": _CONTAINER_RUNTIME_UID,
        "gid": _CONTAINER_RUNTIME_GID,
        "directories": 3,
        "write_read_fsync_delete_probes": 3,
    }


def verify_payloads(landscape_run_id: str) -> dict[str, object]:
    """Retrieve every source-row payload for one run through the production store."""

    canonical_run_id = _canonical_uuid(landscape_run_id, label="landscape run identity")
    try:
        settings = settings_from_env()
        landscape_url = settings.get_landscape_url()
        passphrase = settings.landscape_passphrase
        payload_root = settings.get_payload_store_path()
    except Exception as exc:
        raise check_error_with_cause("settings_load", exc) from None

    try:
        with LandscapeDB.from_url(
            landscape_url,
            passphrase=passphrase,
            create_tables=False,
            read_only=True,
        ) as database:
            rows = RecorderFactory.read_only(database).query.get_rows(canonical_run_id)
            refs = [row.source_data_ref for row in rows if row.source_data_ref is not None]
    except Exception as exc:
        raise check_error_with_cause("landscape_payload_query", exc) from None
    if not refs:
        raise AcceptanceCheckError("payload_refs")
    if payload_root.is_symlink() or not payload_root.is_dir():
        raise AcceptanceCheckError("payload_root")
    try:
        store = FilesystemPayloadStore(payload_root)
    except Exception as exc:
        raise check_error_with_cause("payload_store", exc) from None
    try:
        for ref in refs:
            store.retrieve(ref)
    except Exception as exc:
        raise check_error_with_cause("payload_retrieval", exc) from None
    return {
        "check": "verify-payloads",
        "ok": True,
        "payload_refs": len(refs),
        "content_hashes": refs,
    }
