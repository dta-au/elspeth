"""Pipeline capture and local verification for AWS ECS acceptance."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import quote

import httpx
import yaml

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web.composer.recipes import apply_recipe
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.yaml_generator import generate_public_yaml
from elspeth.web.config import settings_from_env

from .contracts import (
    _TERMINAL_RUN_STATUSES,
    RUN_POLL_DEADLINE_SECONDS,
    RUN_POLL_INTERVAL_SECONDS,
    AcceptanceCheckError,
    AcceptanceInputError,
    _canonical_uuid,
    _mapping,
    _sha256,
    _sha256_field,
    _string_field,
    _utc_timestamp,
    _uuid_field,
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
    document = {
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
    return yaml.safe_dump(document, sort_keys=False)


def _canonical_tutorial_policy_state(*, profile_alias: str) -> CompositionState:
    """Materialize the product's canonical core-only tutorial candidate."""

    recipe = apply_recipe(
        "web-scrape-llm-rate-jsonl",
        {
            "source_blob_id": "00000000-0000-4000-8000-000000000001",
            "source_plugin": "csv",
            "profile": profile_alias,
            "abuse_contact": "aws-ecs-acceptance@example.invalid",
            "scraping_reason": "AWS ECS tutorial policy acceptance",
            "output_path": "outputs/tutorial-policy-acceptance.jsonl",
        },
    )
    source = dict(cast(Mapping[str, object], recipe["source"]))
    source.pop("blob_id")
    outputs = []
    for raw_output in cast(list[dict[str, object]], recipe["outputs"]):
        output = dict(raw_output)
        output["name"] = output.pop("sink_name")
        outputs.append(output)
    return CompositionState.from_dict(
        {
            "source": source,
            "nodes": recipe["nodes"],
            "edges": recipe["edges"],
            "outputs": outputs,
            "metadata": recipe["metadata"],
            "version": 1,
        }
    )


def build_canonical_tutorial_pipeline_yaml(*, profile_alias: str) -> str:
    """Return the public-import form of the canonical core-only tutorial."""

    if type(profile_alias) is not str or not profile_alias.strip() or profile_alias != profile_alias.strip():
        raise AcceptanceInputError("tutorial profile alias is invalid")
    return generate_public_yaml(_canonical_tutorial_policy_state(profile_alias=profile_alias))


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


def _select_output_artifact(payload: object, *, check: str) -> tuple[str, str]:
    manifest = _mapping(payload, check=check)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise AcceptanceCheckError(check)
    matches = [artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get("sink_node_id") == "output"]
    if len(matches) != 1:
        raise AcceptanceCheckError(check)
    artifact = matches[0]
    if artifact.get("artifact_type") not in {"file", "sink_file"}:
        raise AcceptanceCheckError(check)
    if artifact.get("exists_now") is not True or artifact.get("downloadable") is not True:
        raise AcceptanceCheckError(check)
    return _uuid_field(artifact, "artifact_id", check=check), _sha256_field(artifact, "content_hash", check=check)


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

    register_value = env.get("ELSPETH_ACCEPTANCE_REGISTER")
    if register_value not in {None, "0", "1"}:
        raise AcceptanceInputError("ELSPETH_ACCEPTANCE_REGISTER must be 0 or 1")
    tutorial_profile = env.get("ELSPETH_WEB__TUTORIAL_LLM_PROFILE")
    if type(tutorial_profile) is not str or not tutorial_profile.strip() or tutorial_profile != tutorial_profile.strip():
        raise AcceptanceInputError("tutorial profile alias is invalid")
    captured_at = _utc_timestamp(now())
    client = AcceptanceHttpClient.from_env(env, transport=transport)
    register = register_value == "1"
    if register and client.credentials.mode != "local":
        raise AcceptanceInputError("registration is available only for local acceptance authentication")

    uploaded_sha256 = _sha256(FIXED_INPUT_BYTES)
    with client:
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

        imported = _mapping(
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
        if imported.get("is_valid") is not True:
            raise AcceptanceCheckError("yaml_import")
        validated = _mapping(
            client.request_json("POST", f"/api/sessions/{session_id}/validate", expected_statuses={200}),
            check="pipeline_validate",
        )
        readiness = _mapping(validated.get("readiness"), check="pipeline_validate")
        if validated.get("is_valid") is not True or readiness.get("execution_ready") is not True:
            raise AcceptanceCheckError("pipeline_validate")

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
        artifact_id, manifest_artifact_sha256 = _select_output_artifact(manifest, check="artifact_manifest")
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
        imported_tutorial = _mapping(
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
        if imported_tutorial.get("is_valid") is not False:
            raise AcceptanceCheckError("tutorial_state_import")

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


def _verify_plugin_policy_http_contract(client: AcceptanceHttpClient, *, tutorial_session_id: str) -> None:
    status = _mapping(
        client.request_json("GET", "/api/system/status", expected_statuses={200}),
        check="plugin_policy_readiness",
    )
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

    rejected = _mapping(
        client.request_json(
            "POST",
            "/api/tutorial/run",
            expected_statuses={409},
            json_body={"session_id": tutorial_session_id},
        ),
        check="tutorial_required_control_coverage",
    )
    detail = _mapping(rejected.get("detail"), check="tutorial_required_control_coverage")
    if detail.get("error_type") != "tutorial_not_ready" or detail.get("code") != "tutorial_required_control_coverage":
        raise AcceptanceCheckError("tutorial_required_control_coverage")


def verify_api(
    env: Mapping[str, str],
    *,
    state_file: Path,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Re-authenticate and verify the captured API resources without mutation."""

    state = read_acceptance_state(state_file)
    client = AcceptanceHttpClient.from_env(env, transport=transport)
    with client:
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
        artifact_id, artifact_sha256 = _select_output_artifact(manifest, check="artifact_manifest")
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
    except Exception:
        raise AcceptanceCheckError("storage_settings") from None
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
    except Exception:
        raise AcceptanceCheckError("settings_load") from None

    try:
        with LandscapeDB.from_url(
            landscape_url,
            passphrase=passphrase,
            create_tables=False,
            read_only=True,
        ) as database:
            rows = RecorderFactory.read_only(database).query.get_rows(canonical_run_id)
            refs = [row.source_data_ref for row in rows if row.source_data_ref is not None]
    except Exception:
        raise AcceptanceCheckError("landscape_payload_query") from None
    if not refs:
        raise AcceptanceCheckError("payload_refs")
    if payload_root.is_symlink() or not payload_root.is_dir():
        raise AcceptanceCheckError("payload_root")
    try:
        store = FilesystemPayloadStore(payload_root)
    except Exception:
        raise AcceptanceCheckError("payload_store") from None
    try:
        for ref in refs:
            store.retrieve(ref)
    except Exception:
        raise AcceptanceCheckError("payload_retrieval") from None
    return {
        "check": "verify-payloads",
        "ok": True,
        "payload_refs": len(refs),
        "content_hashes": refs,
    }
