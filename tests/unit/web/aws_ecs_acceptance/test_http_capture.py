"""HTTP transport and capture contracts for AWS ECS acceptance."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import capture as capture_module
from elspeth.web._aws_ecs_acceptance import http_client


def test_moved_public_transport_and_capture_are_facade_reexports_by_identity() -> None:
    assert acceptance.AcceptanceHttpClient is http_client.AcceptanceHttpClient
    assert acceptance.build_canonical_tutorial_pipeline_yaml is capture_module.build_canonical_tutorial_pipeline_yaml
    assert acceptance.build_fixed_pipeline_yaml is capture_module.build_fixed_pipeline_yaml
    assert acceptance.capture is capture_module.capture
    assert acceptance.provision_storage is capture_module.provision_storage
    assert acceptance.verify_api is capture_module.verify_api
    assert acceptance.verify_local_auth is capture_module.verify_local_auth
    assert acceptance.verify_payloads is capture_module.verify_payloads


def _auth_env(**updates: str) -> Mapping[str, str]:
    values = {
        "ELSPETH_ACCEPTANCE_BASE_URL": "https://staging.example",
        "ELSPETH_ACCEPTANCE_BEARER_TOKEN": "bearer-secret",
        "ELSPETH_WEB__DEFAULT_LLM_PROFILE": "tutorial",
        "ELSPETH_WEB__DATA_DIR": "/var/lib/elspeth",
    }
    values.update(updates)
    return values


def test_http_client_disables_redirects_and_never_replays_bearer_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "staging.example":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        pytest.fail("redirect was followed")

    client = acceptance.AcceptanceHttpClient.from_env(_auth_env(), transport=httpx.MockTransport(handler))
    with client, pytest.raises(acceptance.AcceptanceHttpError, match="unexpected HTTP status"):
        client.request_json("GET", "/api/auth/me", expected_statuses={200})

    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer bearer-secret"


def test_http_client_rejects_cross_origin_response_and_port_mismatch() -> None:
    client = acceptance.AcceptanceHttpClient.from_env(_auth_env())

    for url in ("https://staging.example:9443/api/auth/me", "https://staging.example.evil/api/auth/me"):
        with pytest.raises(acceptance.AcceptanceHttpError, match="cross-origin"):
            client.validate_response_origin(httpx.URL(url))


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(200, content=b"x" * (1024 * 1024 + 1)), "too large"),
        (httpx.Response(200, content=b"not-json"), "malformed JSON"),
    ],
)
def test_http_client_rejects_oversized_or_malformed_json_without_echo(response: httpx.Response, match: str) -> None:
    marker = "not-json"

    def handler(request: httpx.Request) -> httpx.Response:
        response.request = request
        return response

    client = acceptance.AcceptanceHttpClient.from_env(_auth_env(), transport=httpx.MockTransport(handler))
    with client, pytest.raises(acceptance.AcceptanceHttpError, match=match) as raised:
        client.request_json("GET", "/api/value", expected_statuses={200})

    assert marker not in str(raised.value)


def test_http_client_reports_timeout_by_static_class_only() -> None:
    marker = "https://private.example/token?credential=secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(marker, request=request)

    client = acceptance.AcceptanceHttpClient.from_env(_auth_env(), transport=httpx.MockTransport(handler))
    with client, pytest.raises(acceptance.AcceptanceHttpError, match="request timeout") as raised:
        client.request_json("GET", "/api/value", expected_statuses={200})

    assert marker not in str(raised.value)


def test_http_client_rejects_absolute_and_network_path_targets_before_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    client = acceptance.AcceptanceHttpClient.from_env(_auth_env(), transport=httpx.MockTransport(handler))
    with client:
        for path in ("https://evil.example/steal", "//evil.example/steal", "api/no-leading-slash"):
            with pytest.raises(acceptance.AcceptanceInputError, match="relative API path"):
                client.request_json("GET", path, expected_statuses={200})

    assert calls == 0


def _valid_state() -> acceptance.AcceptanceState:
    return acceptance.AcceptanceState.from_dict(
        {
            "schema_version": 1,
            "session_id": "8e826f53-5f13-420f-8678-5ec0caecd15f",
            "tutorial_session_id": "f6a99a36-13f9-49c9-a3af-d9f6f7924a56",
            "blob_id": "cc742c5f-ae01-49f3-988b-7ecddf0445ef",
            "run_id": "401b6510-a37f-4375-acb8-695fe0098265",
            "landscape_run_id": "a31de342-a9f2-4b31-bb02-9043a047db72",
            "artifact_id": _ARTIFACT_ID,
            "uploaded_sha256": "a" * 64,
            "blob_sha256": "a" * 64,
            "artifact_sha256": "b" * 64,
            "run_status": "completed",
            "source_rows": 1,
            "failed_tokens": 0,
            "captured_at": "2026-07-14T04:00:00Z",
            "completed_at": "2026-07-14T04:00:01Z",
        }
    )


def test_fixed_pipeline_yaml_parses_and_passes_the_ordinary_runtime_validator(tmp_path: Path) -> None:
    from elspeth.web.composer import yaml_generator
    from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml
    from elspeth.web.config import WebSettings
    from elspeth.web.execution.validation import validate_pipeline_for_trained_operator

    session_id = "8e826f53-5f13-420f-8678-5ec0caecd15f"
    source_path = tmp_path / "blobs" / session_id / "input.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(acceptance.FIXED_INPUT_BYTES)
    (tmp_path / "outputs").mkdir()

    pipeline_yaml = acceptance.build_fixed_pipeline_yaml(session_id=session_id, source_path=str(source_path))
    state = composition_state_from_runtime_yaml(pipeline_yaml)
    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=10,
        composer_max_discovery_turns=5,
        composer_timeout_seconds=30.0,
        composer_rate_limit_per_minute=60,
        shareable_link_signing_key=b"\x00" * 32,
    )
    result = validate_pipeline_for_trained_operator(state, settings, yaml_generator, session_id=session_id)

    assert result.is_valid is True
    assert result.readiness.execution_ready is True
    assert set(state.sources) == {"source"}
    assert state.sources["source"].plugin == "csv"
    assert state.sources["source"].on_success == "output"
    assert state.sources["source"].on_validation_failure == "discard"
    assert dict(state.sources["source"].options) == {
        "path": str(source_path),
        "delimiter": ",",
        "encoding": "utf-8",
        "schema": {"mode": "fixed", "fields": ("id: int", "name: str")},
    }
    assert len(state.outputs) == 1
    assert state.outputs[0].name == "output"
    assert state.outputs[0].plugin == "csv"
    assert state.outputs[0].on_write_failure == "discard"
    assert dict(state.outputs[0].options) == {
        "path": f"outputs/aws-ecs-acceptance-{session_id}.csv",
        "delimiter": ",",
        "encoding": "utf-8",
        "mode": "write",
        "collision_policy": "fail_if_exists",
        "schema": {"mode": "fixed", "fields": ("id: int", "name: str")},
    }


def test_fixed_pipeline_yaml_rejects_noncanonical_session_id() -> None:
    with pytest.raises(acceptance.AcceptanceInputError, match="session identity"):
        acceptance.build_fixed_pipeline_yaml(session_id="../escape")


_SESSION_ID = "8e826f53-5f13-420f-8678-5ec0caecd15f"
# Derived from the sink options AFTER the server's resolve_sink_data_path()
# rewrite: path = /var/lib/elspeth/outputs/<session>/aws-ecs-acceptance-<session>.csv.
# The pre-rewrite (authored-path) id was sink_output_d4f5d8b83aa5 and must
# never match again — see test_fixed_output_sink_node_id_matches_the_real_
# preflight_and_builder below.
_FIXED_OUTPUT_SINK_NODE_ID = "sink_output_33920bb8f986"

_TUTORIAL_SESSION_ID = "f6a99a36-13f9-49c9-a3af-d9f6f7924a56"

_BLOB_ID = "cc742c5f-ae01-49f3-988b-7ecddf0445ef"

_TUTORIAL_BLOB_ID = "ef6866a0-640f-4bb0-ab18-d93213ee942b"

_RUN_ID = "401b6510-a37f-4375-acb8-695fe0098265"

_LANDSCAPE_RUN_ID = "a31de342-a9f2-4b31-bb02-9043a047db72"

# NOT a UUID: real artifact_id values are landscape `artifacts.artifact_id`
# hex identities (32 or 64 lowercase hex chars from the sink-effect producer
# path's labeled SHA-256 digest), never canonical dashed UUIDs -- see
# `_ARTIFACT_ID_PATTERN` in contracts.py.
_ARTIFACT_ID = "6d9653ae9f51e25579b040ab9ffb7d75e42b731666bbf7500a5c0e3546195d96"

_ARTIFACT_BYTES = b"id,name\r\n1,alpha\r\n"


class _AcceptanceApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.run_status = "completed"
        self.artifacts: list[dict[str, object]] | None = None
        self.artifact_bytes = _ARTIFACT_BYTES
        self.expected_token = "bearer-secret"
        self.session_creates = 0

    @property
    def blob_sha256(self) -> str:
        import hashlib

        return hashlib.sha256(acceptance.FIXED_INPUT_BYTES).hexdigest()

    @property
    def artifact_sha256(self) -> str:
        import hashlib

        return hashlib.sha256(_ARTIFACT_BYTES).hexdigest()

    def _run(self) -> dict[str, object]:
        return {
            "run_id": _RUN_ID,
            "status": self.run_status,
            "started_at": "2026-07-14T04:00:00Z",
            "finished_at": "2026-07-14T04:00:01Z" if self.run_status == "completed" else None,
            "accounting": {"source": {"rows_processed": 1}, "tokens": {"failed": 0}},
            "error": None,
            "landscape_run_id": _LANDSCAPE_RUN_ID if self.run_status == "completed" else None,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        assert request.headers["authorization"] == f"Bearer {self.expected_token}"
        if request.method == "POST" and path == "/api/sessions":
            assert json.loads(request.content) == {}
            self.session_creates += 1
            return httpx.Response(201, json={"id": _SESSION_ID if self.session_creates == 1 else _TUTORIAL_SESSION_ID})
        if request.method == "POST" and path == f"/api/sessions/{_SESSION_ID}/blobs":
            assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
            assert acceptance.FIXED_INPUT_BYTES in request.content
            return httpx.Response(201, json={"id": _BLOB_ID, "content_hash": self.blob_sha256})
        if request.method == "POST" and path == f"/api/sessions/{_SESSION_ID}/state/yaml":
            body = json.loads(request.content)
            assert body["source_blob_ids"] == {"source": _BLOB_ID}
            assert f"outputs/aws-ecs-acceptance-{_SESSION_ID}.csv" in body["yaml"]
            return httpx.Response(200, json={"id": "state-1", "is_valid": True})
        if request.method == "POST" and path == f"/api/sessions/{_SESSION_ID}/validate":
            return httpx.Response(200, json={"is_valid": True, "readiness": {"execution_ready": True}})
        if request.method == "POST" and path == f"/api/sessions/{_SESSION_ID}/execute":
            return httpx.Response(202, json={"run_id": _RUN_ID})
        if request.method == "GET" and path == f"/api/runs/{_RUN_ID}":
            return httpx.Response(200, json=self._run())
        if request.method == "GET" and path == f"/api/runs/{_RUN_ID}/results":
            return httpx.Response(200, json=self._run())
        if request.method == "GET" and path == f"/api/runs/{_RUN_ID}/outputs":
            artifacts = self.artifacts
            if artifacts is None:
                artifacts = [
                    {
                        "artifact_id": _ARTIFACT_ID,
                        "sink_node_id": _FIXED_OUTPUT_SINK_NODE_ID,
                        "artifact_type": "file",
                        "content_hash": self.artifact_sha256,
                        "exists_now": True,
                        "downloadable": True,
                    }
                ]
            return httpx.Response(200, json={"run_id": _RUN_ID, "landscape_run_id": _LANDSCAPE_RUN_ID, "artifacts": artifacts})
        if request.method == "GET" and path.startswith(f"/api/runs/{_RUN_ID}/outputs/") and path.endswith("/content"):
            # Matches whatever artifact_id _select_output_artifact() actually
            # resolved -- not hardcoded to _ARTIFACT_ID -- so tests can swap
            # in a differently-shaped valid artifact_id (see
            # test_capture_accepts_uuid4_hex_artifact_id) and still exercise
            # the full round trip; content-hash comparison downstream still
            # catches a genuinely wrong artifact_id.
            return httpx.Response(200, content=self.artifact_bytes)
        if request.method == "GET" and path == f"/api/sessions/{_SESSION_ID}/blobs/{_BLOB_ID}":
            return httpx.Response(200, json={"id": _BLOB_ID, "session_id": _SESSION_ID, "content_hash": self.blob_sha256})
        if request.method == "GET" and path == f"/api/sessions/{_SESSION_ID}/blobs/{_BLOB_ID}/content":
            return httpx.Response(200, content=acceptance.FIXED_INPUT_BYTES)
        if request.method == "POST" and path == f"/api/sessions/{_TUTORIAL_SESSION_ID}/blobs":
            assert acceptance.TUTORIAL_INPUT_BYTES in request.content
            return httpx.Response(
                201,
                json={"id": _TUTORIAL_BLOB_ID, "content_hash": hashlib.sha256(acceptance.TUTORIAL_INPUT_BYTES).hexdigest()},
            )
        if request.method == "POST" and path == f"/api/sessions/{_TUTORIAL_SESSION_ID}/state/yaml":
            body = json.loads(request.content)
            assert body["source_blob_ids"] == {"source": _TUTORIAL_BLOB_ID}
            assert all(plugin in body["yaml"] for plugin in ("web_scrape", "llm", "field_mapper"))
            assert "aws_bedrock_prompt_shield" not in body["yaml"]
            assert "aws_bedrock_content_safety" not in body["yaml"]
            return httpx.Response(200, json={"id": "tutorial-state-1", "is_valid": False})
        if request.method == "GET" and path == f"/api/sessions/{_SESSION_ID}":
            return httpx.Response(200, json={"id": _SESSION_ID})
        if request.method == "GET" and path == "/api/system/status":
            return httpx.Response(
                200,
                json={
                    "tutorial_ready": True,
                    "plugin_policy_readiness": {
                        "tutorial_ready": True,
                        "rows": [
                            {"id": "policy_compilation", "status": "ok"},
                            {"id": "required_core", "status": "ok"},
                            {"id": "local_capability_configuration", "status": "ok"},
                            {"id": "live_health", "status": "not_applicable"},
                            {"id": "tutorial_profile", "status": "warning"},
                            {"id": "tutorial_required_control_coverage", "status": "not_applicable"},
                        ],
                    },
                },
            )
        if request.method == "POST" and path == "/api/tutorial/run":
            assert json.loads(request.content) == {"session_id": _TUTORIAL_SESSION_ID}
            return httpx.Response(
                409,
                json={
                    "detail": {
                        "error_type": "tutorial_not_ready",
                        "code": "tutorial_required_control_coverage",
                        "detail": "The saved tutorial pipeline is missing required control coverage.",
                    }
                },
            )
        pytest.fail(f"unexpected acceptance request: {request.method} {path}")


def test_capture_executes_fixed_pipeline_and_persists_only_closed_state(tmp_path: Path) -> None:
    api = _AcceptanceApi()
    state_path = tmp_path / "state.json"
    timestamps = iter(
        [
            datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            datetime(2026, 7, 14, 4, 1, tzinfo=UTC),
        ]
    )

    state = acceptance.capture(
        _auth_env(),
        state_file=state_path,
        transport=httpx.MockTransport(api),
        now=lambda: next(timestamps),
        sleep=lambda _seconds: None,
    )

    assert acceptance.read_acceptance_state(state_path) == state
    assert state.session_id == _SESSION_ID
    assert state.tutorial_session_id == _TUTORIAL_SESSION_ID
    assert state.blob_id == _BLOB_ID
    assert state.run_id == _RUN_ID
    assert state.landscape_run_id == _LANDSCAPE_RUN_ID
    assert state.artifact_id == _ARTIFACT_ID
    assert state.uploaded_sha256 == api.blob_sha256
    assert state.blob_sha256 == api.blob_sha256
    assert state.artifact_sha256 == api.artifact_sha256
    assert state.source_rows == 1
    assert state.failed_tokens == 0
    persisted = state_path.read_text()
    assert "bearer-secret" not in persisted
    assert "id,name" not in persisted
    assert "https://" not in persisted
    assert api.calls == [
        ("POST", "/api/sessions"),
        ("POST", f"/api/sessions/{_SESSION_ID}/blobs"),
        ("POST", f"/api/sessions/{_SESSION_ID}/state/yaml"),
        ("POST", f"/api/sessions/{_SESSION_ID}/validate"),
        ("POST", f"/api/sessions/{_SESSION_ID}/execute"),
        ("GET", f"/api/runs/{_RUN_ID}"),
        ("GET", f"/api/runs/{_RUN_ID}/results"),
        ("GET", f"/api/runs/{_RUN_ID}/outputs"),
        ("GET", f"/api/runs/{_RUN_ID}/outputs/{_ARTIFACT_ID}/content"),
        ("GET", f"/api/sessions/{_SESSION_ID}/blobs/{_BLOB_ID}/content"),
        ("POST", "/api/sessions"),
        ("POST", f"/api/sessions/{_TUTORIAL_SESSION_ID}/blobs"),
        ("POST", f"/api/sessions/{_TUTORIAL_SESSION_ID}/state/yaml"),
    ]


@pytest.mark.parametrize(
    ("register_status", "expected_paths"), [(200, ["/api/auth/register"]), (409, ["/api/auth/register", "/api/auth/login"])]
)
def test_local_capture_registration_is_explicit_and_409_falls_back_to_login(register_status: int, expected_paths: list[str]) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        body = json.loads(request.content)
        assert body["username"] == "operator"
        assert body["password"] == "password-secret"
        if request.url.path == "/api/auth/register":
            assert body["display_name"] == "operator"
            if register_status == 200:
                return httpx.Response(200, json={"access_token": "new-token", "token_type": "bearer"})
            return httpx.Response(409, json={"detail": "duplicate sentinel that must not escape"})
        return httpx.Response(200, json={"access_token": "login-token", "token_type": "bearer"})

    client = acceptance.AcceptanceHttpClient.from_env(
        {
            "ELSPETH_ACCEPTANCE_BASE_URL": "https://staging.example",
            "ELSPETH_ACCEPTANCE_USERNAME": "operator",
            "ELSPETH_ACCEPTANCE_PASSWORD": "password-secret",
        },
        transport=httpx.MockTransport(handler),
    )
    with client:
        client.authenticate(register=True)

    assert paths == expected_paths


@pytest.mark.parametrize(
    ("failure", "match"),
    [("failed-run", "run_terminal"), ("missing-artifact", "artifact_manifest"), ("hash-mismatch", "artifact_integrity")],
)
def test_capture_fails_closed_with_static_check_names(tmp_path: Path, failure: str, match: str) -> None:
    api = _AcceptanceApi()
    if failure == "failed-run":
        api.run_status = "failed"
    elif failure == "missing-artifact":
        api.artifacts = []
    else:
        api.artifact_bytes = b"provider secret sentinel"

    with pytest.raises(acceptance.AcceptanceCheckError, match=match) as raised:
        acceptance.capture(
            _auth_env(),
            state_file=tmp_path / "state.json",
            transport=httpx.MockTransport(api),
            now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            sleep=lambda _seconds: None,
        )

    assert "provider secret sentinel" not in str(raised.value)
    assert not (tmp_path / "state.json").exists()


def test_capture_accepts_generated_output_sink_node_id(tmp_path: Path) -> None:
    """Reproduces elspeth-a811cba074: the live outputs manifest reports the
    engine's deterministic generated node id for the fixed pipeline's one
    sink. ``capture()`` must bind the exact config-derived identity.
    """
    api = _AcceptanceApi()
    api.artifacts = [
        {
            "artifact_id": _ARTIFACT_ID,
            "sink_node_id": _FIXED_OUTPUT_SINK_NODE_ID,
            "artifact_type": "file",
            "content_hash": api.artifact_sha256,
            "exists_now": True,
            "downloadable": True,
        }
    ]

    state = acceptance.capture(
        _auth_env(),
        state_file=tmp_path / "state.json",
        transport=httpx.MockTransport(api),
        now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
        sleep=lambda _seconds: None,
    )

    assert state.artifact_id == _ARTIFACT_ID
    assert state.artifact_sha256 == api.artifact_sha256


def test_capture_rejects_generated_output_sink_id_from_a_different_config(tmp_path: Path) -> None:
    api = _AcceptanceApi()
    api.artifacts = [
        {
            "artifact_id": _ARTIFACT_ID,
            "sink_node_id": "sink_output_000000000000",
            "artifact_type": "file",
            "content_hash": api.artifact_sha256,
            "exists_now": True,
            "downloadable": True,
        }
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="artifact_manifest"):
        acceptance.capture(
            _auth_env(),
            state_file=tmp_path / "state.json",
            transport=httpx.MockTransport(api),
            now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            sleep=lambda _seconds: None,
        )


def test_capture_fails_closed_when_output_sink_id_is_ambiguous(tmp_path: Path) -> None:
    api = _AcceptanceApi()
    other_artifact_id = "9e15a237-4c5e-4b09-9d9d-3c9f8db6b6c1"
    api.artifacts = [
        {
            "artifact_id": _ARTIFACT_ID,
            "sink_node_id": _FIXED_OUTPUT_SINK_NODE_ID,
            "artifact_type": "file",
            "content_hash": api.artifact_sha256,
            "exists_now": True,
            "downloadable": True,
        },
        {
            "artifact_id": other_artifact_id,
            "sink_node_id": _FIXED_OUTPUT_SINK_NODE_ID,
            "artifact_type": "file",
            "content_hash": api.artifact_sha256,
            "exists_now": True,
            "downloadable": True,
        },
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="artifact_manifest"):
        acceptance.capture(
            _auth_env(),
            state_file=tmp_path / "state.json",
            transport=httpx.MockTransport(api),
            now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            sleep=lambda _seconds: None,
        )

    assert not (tmp_path / "state.json").exists()


def test_capture_rejects_differently_named_sink_that_shares_the_output_prefix(tmp_path: Path) -> None:
    """A sink named e.g. ``output_extra`` would generate
    ``sink_output_extra_<12-hex>`` — a bare ``startswith("sink_output_")``
    heuristic would wrongly accept it. The selector must require the exact
    ``sink_output_<12-hex>`` shape and reject this near-miss (zero matches).
    """
    api = _AcceptanceApi()
    api.artifacts = [
        {
            "artifact_id": _ARTIFACT_ID,
            "sink_node_id": "sink_output_extra_ca54c90e06a7",
            "artifact_type": "file",
            "content_hash": api.artifact_sha256,
            "exists_now": True,
            "downloadable": True,
        }
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="artifact_manifest"):
        acceptance.capture(
            _auth_env(),
            state_file=tmp_path / "state.json",
            transport=httpx.MockTransport(api),
            now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            sleep=lambda _seconds: None,
        )


def test_fixed_output_sink_node_id_matches_the_real_preflight_and_builder(tmp_path: Path) -> None:
    """Regression for the resolved-path divergence: the client's expected
    sink node id must equal the id the production preflight path rewrite and
    DAG builder derive for this exact pipeline document. The pre-fix client
    hashed the AUTHORED relative sink path, but resolve_runtime_yaml_paths()
    rewrites it to the session-scoped absolute path before the builder
    hashes the sink config, so the authored-path id never matched a real
    run's manifest.
    """
    from elspeth.core.config import load_settings_from_yaml_string
    from elspeth.plugins.infrastructure.runtime_factory import instantiate_plugins_from_config
    from elspeth.web.composer import yaml_generator
    from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml
    from elspeth.web.execution.preflight import build_runtime_graph, resolve_runtime_yaml_paths

    data_dir = (tmp_path / "data").resolve()
    source_path = data_dir / "blobs" / _SESSION_ID / "aws-ecs-acceptance-input.csv"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(acceptance.FIXED_INPUT_BYTES)

    authored_yaml = acceptance.build_fixed_pipeline_yaml(session_id=_SESSION_ID, source_path=str(source_path))
    state = composition_state_from_runtime_yaml(authored_yaml)
    resolved_yaml = resolve_runtime_yaml_paths(yaml_generator.generate_yaml(state), str(data_dir), session_id=_SESSION_ID)
    settings = load_settings_from_yaml_string(resolved_yaml, expand_env_vars=False)
    graph = build_runtime_graph(settings, instantiate_plugins_from_config(settings, preflight_mode=True))

    expected = capture_module._fixed_output_sink_node_id(_SESSION_ID, data_dir=str(data_dir))
    assert dict(graph.get_sink_id_map()) == {"output": expected}
    assert expected != "sink_output_d4f5d8b83aa5"


def test_capture_rejects_the_pre_rewrite_authored_path_sink_node_id(tmp_path: Path) -> None:
    """A manifest carrying the id hashed from the authored relative sink
    path (the pre-fix client computation) must fail closed: the server only
    ever reports the id hashed from the rewritten absolute path.
    """
    api = _AcceptanceApi()
    api.artifacts = [
        {
            "artifact_id": _ARTIFACT_ID,
            "sink_node_id": "sink_output_d4f5d8b83aa5",
            "artifact_type": "file",
            "content_hash": api.artifact_sha256,
            "exists_now": True,
            "downloadable": True,
        }
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="artifact_manifest"):
        acceptance.capture(
            _auth_env(),
            state_file=tmp_path / "state.json",
            transport=httpx.MockTransport(api),
            now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            sleep=lambda _seconds: None,
        )


@pytest.mark.parametrize(
    "data_dir",
    [None, "", "var/lib/elspeth", "/var/lib/elspeth/", "/var/lib/../elspeth", "/var/./lib/elspeth"],
)
def test_capture_rejects_missing_or_noncanonical_server_data_dir_before_any_request(tmp_path: Path, data_dir: str | None) -> None:
    env = dict(_auth_env())
    if data_dir is None:
        del env["ELSPETH_WEB__DATA_DIR"]
    else:
        env["ELSPETH_WEB__DATA_DIR"] = data_dir
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    with pytest.raises(acceptance.AcceptanceInputError, match="ELSPETH_WEB__DATA_DIR"):
        acceptance.capture(
            env,
            state_file=tmp_path / "state.json",
            transport=httpx.MockTransport(handler),
            now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            sleep=lambda _seconds: None,
        )

    assert calls == 0
    assert not (tmp_path / "state.json").exists()


def test_verify_api_rejects_missing_server_data_dir_before_any_request(tmp_path: Path) -> None:
    api = _AcceptanceApi()
    state = acceptance.AcceptanceState.from_dict(
        {
            **_valid_state().to_dict(),
            "uploaded_sha256": api.blob_sha256,
            "blob_sha256": api.blob_sha256,
            "artifact_sha256": api.artifact_sha256,
        }
    )
    state_path = tmp_path / "state.json"
    acceptance.write_acceptance_state(state_path, state)
    env = dict(_auth_env())
    del env["ELSPETH_WEB__DATA_DIR"]

    with pytest.raises(acceptance.AcceptanceInputError, match="ELSPETH_WEB__DATA_DIR"):
        acceptance.verify_api(env, state_file=state_path, transport=httpx.MockTransport(api))

    assert api.calls == []


def test_capture_accepts_uuid4_hex_artifact_id(tmp_path: Path) -> None:
    """The legacy (non-sink-effect) producer path defaults ``artifact_id``
    to ``core.ids.generate_id()`` -- ``uuid.uuid4().hex`` (32 lowercase hex
    chars, no dashes). This is the *other* real production shape besides
    the 64-hex sha256 digest already covered by ``_ARTIFACT_ID``; both must
    be accepted since neither is a canonical dashed UUID.
    """
    api = _AcceptanceApi()
    generated_artifact_id = "8e82b5045dcc4dc99fe4a1c62be47153"
    api.artifacts = [
        {
            "artifact_id": generated_artifact_id,
            "sink_node_id": _FIXED_OUTPUT_SINK_NODE_ID,
            "artifact_type": "file",
            "content_hash": api.artifact_sha256,
            "exists_now": True,
            "downloadable": True,
        }
    ]

    state = acceptance.capture(
        _auth_env(),
        state_file=tmp_path / "state.json",
        transport=httpx.MockTransport(api),
        now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
        sleep=lambda _seconds: None,
    )

    assert state.artifact_id == generated_artifact_id


@pytest.mark.parametrize("artifact_id", ["a", "a" * 31, "a" * 33, "a" * 63])
def test_capture_rejects_impossible_artifact_id_lengths(tmp_path: Path, artifact_id: str) -> None:
    api = _AcceptanceApi()
    api.artifacts = [
        {
            "artifact_id": artifact_id,
            "sink_node_id": _FIXED_OUTPUT_SINK_NODE_ID,
            "artifact_type": "file",
            "content_hash": api.artifact_sha256,
            "exists_now": True,
            "downloadable": True,
        }
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="artifact_manifest"):
        acceptance.capture(
            _auth_env(),
            state_file=tmp_path / "state.json",
            transport=httpx.MockTransport(api),
            now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            sleep=lambda _seconds: None,
        )


def test_capture_rejects_non_hex_artifact_id(tmp_path: Path) -> None:
    """artifact_id must never be treated as a canonical UUID (see
    ``_ARTIFACT_ID_PATTERN``): a dashed-UUID-shaped or otherwise non-hex
    value is not a real landscape artifact identity and must fail closed.
    """
    api = _AcceptanceApi()
    api.artifacts = [
        {
            "artifact_id": "8e82b504-5dcc-4dc9-9fe4-a1c62be47153",
            "sink_node_id": _FIXED_OUTPUT_SINK_NODE_ID,
            "artifact_type": "file",
            "content_hash": api.artifact_sha256,
            "exists_now": True,
            "downloadable": True,
        }
    ]

    with pytest.raises(acceptance.AcceptanceCheckError, match="artifact_manifest"):
        acceptance.capture(
            _auth_env(),
            state_file=tmp_path / "state.json",
            transport=httpx.MockTransport(api),
            now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            sleep=lambda _seconds: None,
        )


def test_capture_times_out_on_nonterminal_run_without_persisting_state(tmp_path: Path) -> None:
    api = _AcceptanceApi()
    api.run_status = "running"
    ticks = iter([0.0, 301.0])

    with pytest.raises(acceptance.AcceptanceCheckError, match="run_poll_timeout"):
        acceptance.capture(
            _auth_env(),
            state_file=tmp_path / "state.json",
            transport=httpx.MockTransport(api),
            now=lambda: datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
            monotonic=lambda: next(ticks),
            sleep=lambda _seconds: None,
        )

    assert not (tmp_path / "state.json").exists()


def test_verify_api_reauthenticates_then_performs_read_only_hash_identical_checks(tmp_path: Path) -> None:
    api = _AcceptanceApi()
    api.expected_token = "replacement-token"
    state = acceptance.AcceptanceState.from_dict(
        {
            **_valid_state().to_dict(),
            "uploaded_sha256": api.blob_sha256,
            "blob_sha256": api.blob_sha256,
            "artifact_sha256": api.artifact_sha256,
        }
    )
    state_path = tmp_path / "state.json"
    acceptance.write_acceptance_state(state_path, state)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            assert json.loads(request.content) == {"username": "operator", "password": "password-secret"}
            return httpx.Response(200, json={"access_token": "replacement-token", "token_type": "bearer"})
        assert request.headers["authorization"] == "Bearer replacement-token"
        return api(request)

    receipt = acceptance.verify_api(
        {
            "ELSPETH_ACCEPTANCE_BASE_URL": "https://staging.example",
            "ELSPETH_ACCEPTANCE_USERNAME": "operator",
            "ELSPETH_ACCEPTANCE_PASSWORD": "password-secret",
            "ELSPETH_ACCEPTANCE_REGISTER": "1",
            "ELSPETH_WEB__DATA_DIR": "/var/lib/elspeth",
        },
        state_file=state_path,
        transport=httpx.MockTransport(handler),
    )

    assert receipt == {
        "check": "verify-api",
        "ok": True,
        "source_rows": 1,
        "failed_tokens": 0,
        "plugin_policy_ready": True,
        "tutorial_required_control_coverage": True,
    }
    assert api.calls == [
        ("GET", f"/api/sessions/{_SESSION_ID}"),
        ("GET", f"/api/sessions/{_SESSION_ID}/blobs/{_BLOB_ID}"),
        ("GET", f"/api/sessions/{_SESSION_ID}/blobs/{_BLOB_ID}/content"),
        ("GET", f"/api/runs/{_RUN_ID}"),
        ("GET", f"/api/runs/{_RUN_ID}/results"),
        ("GET", f"/api/runs/{_RUN_ID}/outputs"),
        ("GET", f"/api/runs/{_RUN_ID}/outputs/{_ARTIFACT_ID}/content"),
        ("GET", "/api/system/status"),
        ("POST", "/api/tutorial/run"),
    ]


def test_verify_api_rejects_incomplete_policy_readiness_or_missing_typed_tutorial_recheck(tmp_path: Path) -> None:
    api = _AcceptanceApi()
    state = acceptance.AcceptanceState.from_dict(
        {
            **_valid_state().to_dict(),
            "uploaded_sha256": api.blob_sha256,
            "blob_sha256": api.blob_sha256,
            "artifact_sha256": api.artifact_sha256,
        }
    )
    state_path = tmp_path / "state.json"
    acceptance.write_acceptance_state(state_path, state)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json={"tutorial_ready": False, "plugin_policy_readiness": {"rows": []}})
        return api(request)

    with pytest.raises(acceptance.AcceptanceCheckError, match="plugin_policy_readiness"):
        acceptance.verify_api(_auth_env(), state_file=state_path, transport=httpx.MockTransport(handler))


def test_verify_api_rejects_non_contractual_tutorial_launch_response(tmp_path: Path) -> None:
    api = _AcceptanceApi()
    state = acceptance.AcceptanceState.from_dict(
        {
            **_valid_state().to_dict(),
            "uploaded_sha256": api.blob_sha256,
            "blob_sha256": api.blob_sha256,
            "artifact_sha256": api.artifact_sha256,
        }
    )
    state_path = tmp_path / "state.json"
    acceptance.write_acceptance_state(state_path, state)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tutorial/run":
            return httpx.Response(409, json={"detail": {"error_type": "tutorial_not_ready", "code": "other"}})
        return api(request)

    with pytest.raises(acceptance.AcceptanceCheckError, match="tutorial_required_control_coverage"):
        acceptance.verify_api(_auth_env(), state_file=state_path, transport=httpx.MockTransport(handler))


def test_verify_local_auth_uses_shared_settings_loader_and_read_only_delete_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    auth_db = data_dir / "auth.db"
    connection = sqlite3.connect(auth_db)
    connection.execute("CREATE TABLE users (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    loads = 0

    def load_settings() -> object:
        nonlocal loads
        loads += 1
        return SimpleNamespace(auth_provider="local", data_dir=data_dir)

    monkeypatch.setattr(capture_module, "settings_from_env", load_settings)

    receipt = acceptance.verify_local_auth()

    assert loads == 1
    assert receipt == {
        "check": "verify-local-auth",
        "ok": True,
        "checks": {"auth_provider_local": True, "auth_db_exists": True, "journal_mode_delete": True},
    }


@pytest.mark.parametrize(
    ("mode", "match"), [("oidc", "auth_provider_local"), ("missing", "auth_db_exists"), ("wal", "journal_mode_delete")]
)
def test_verify_local_auth_fails_closed_without_creating_or_echoing_database_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, match: str
) -> None:
    data_dir = tmp_path / "private-database-path"
    data_dir.mkdir()
    auth_db = data_dir / "auth.db"
    if mode == "wal":
        connection = sqlite3.connect(auth_db)
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
        connection.close()
    monkeypatch.setattr(
        capture_module,
        "settings_from_env",
        lambda: SimpleNamespace(auth_provider="oidc" if mode == "oidc" else "local", data_dir=data_dir),
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match=match) as raised:
        acceptance.verify_local_auth()

    assert str(data_dir) not in str(raised.value)
    if mode == "missing":
        assert not auth_db.exists()


def _assume_published_image_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model the published image identity without requiring host UID 1654."""

    monkeypatch.setattr(capture_module.os, "geteuid", lambda: 1654)
    monkeypatch.setattr(capture_module.os, "getegid", lambda: 1654)
    monkeypatch.setattr(
        capture_module,
        "_storage_metadata",
        lambda path: SimpleNamespace(st_mode=path.lstat().st_mode, st_uid=1654, st_gid=1654),
    )


def test_provision_storage_creates_and_probes_required_non_root_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _assume_published_image_identity(monkeypatch)
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    payload_root = data_dir / "payloads"
    monkeypatch.setattr(
        capture_module,
        "settings_from_env",
        lambda: SimpleNamespace(
            data_dir=data_dir,
            payload_store_path=payload_root,
            get_payload_store_path=lambda: payload_root,
        ),
    )

    receipt = acceptance.provision_storage()

    assert receipt == {
        "check": "provision-storage",
        "ok": True,
        "uid": 1654,
        "gid": 1654,
        "directories": 3,
        "write_read_fsync_delete_probes": 3,
    }
    assert payload_root.is_dir()
    assert (data_dir / "blobs").is_dir()
    assert not list(data_dir.rglob(".elspeth-probe-*"))


@pytest.mark.parametrize("payload_kind", ["data", "blobs"])
def test_provision_storage_rejects_duplicate_required_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload_kind: str) -> None:
    _assume_published_image_identity(monkeypatch)
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    payload_root = data_dir if payload_kind == "data" else data_dir / "blobs"
    monkeypatch.setattr(
        capture_module,
        "settings_from_env",
        lambda: SimpleNamespace(
            data_dir=data_dir,
            payload_store_path=payload_root,
            get_payload_store_path=lambda: payload_root,
        ),
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="storage_boundary"):
        acceptance.provision_storage()


def test_provision_storage_rejects_outside_payload_root_without_creating_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _assume_published_image_identity(monkeypatch)
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    payload_root = tmp_path / "outside" / "payloads"
    monkeypatch.setattr(
        capture_module,
        "settings_from_env",
        lambda: SimpleNamespace(
            data_dir=data_dir,
            payload_store_path=payload_root,
            get_payload_store_path=lambda: payload_root,
        ),
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="storage_boundary"):
        acceptance.provision_storage()

    assert not payload_root.exists()
    assert not payload_root.parent.exists()


def test_verify_payloads_uses_read_only_landscape_and_retrieves_every_non_null_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload_root = tmp_path / "payloads"
    payload_root.mkdir(mode=0o700)
    settings = SimpleNamespace(
        landscape_passphrase="passphrase-secret",
        get_landscape_url=lambda: "postgresql+psycopg://private/database",
        get_payload_store_path=lambda: payload_root,
    )
    first_hash = "a" * 64
    second_hash = "b" * 64
    db_closed = False
    from_url_calls: list[tuple[str, dict[str, object]]] = []

    class FakeDB:
        def __enter__(self) -> FakeDB:
            return self

        def __exit__(self, *_args: object) -> None:
            nonlocal db_closed
            db_closed = True

    database = FakeDB()

    def from_url(url: str, **kwargs: object) -> FakeDB:
        from_url_calls.append((url, kwargs))
        return database

    queried: list[tuple[object, str]] = []

    class Query:
        def get_rows(self, run_id: str) -> list[object]:
            queried.append((database, run_id))
            return [
                SimpleNamespace(source_data_ref=first_hash),
                SimpleNamespace(source_data_ref=None),
                SimpleNamespace(source_data_ref=second_hash),
            ]

    monkeypatch.setattr(capture_module, "settings_from_env", lambda: settings)
    monkeypatch.setattr(capture_module.LandscapeDB, "from_url", from_url)
    monkeypatch.setattr(capture_module.RecorderFactory, "read_only", lambda db: SimpleNamespace(query=Query()))
    retrieved: list[str] = []

    class Store:
        def __init__(self, root: Path) -> None:
            assert root == payload_root

        def retrieve(self, content_hash: str) -> bytes:
            retrieved.append(content_hash)
            return b"content"

    monkeypatch.setattr(capture_module, "FilesystemPayloadStore", Store)

    receipt = acceptance.verify_payloads(_LANDSCAPE_RUN_ID)

    assert receipt == {
        "check": "verify-payloads",
        "ok": True,
        "payload_refs": 2,
        "content_hashes": [first_hash, second_hash],
    }
    assert from_url_calls == [
        (
            "postgresql+psycopg://private/database",
            {"passphrase": "passphrase-secret", "create_tables": False, "read_only": True},
        )
    ]
    assert queried == [(database, _LANDSCAPE_RUN_ID)]
    assert retrieved == [first_hash, second_hash]
    assert db_closed is True


@pytest.mark.parametrize(
    ("failure", "match"), [("missing-root", "payload_root"), ("zero-refs", "payload_refs"), ("retrieve", "payload_retrieval")]
)
def test_verify_payloads_fails_closed_and_closes_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, match: str
) -> None:
    payload_root = tmp_path / "payloads"
    if failure != "missing-root":
        payload_root.mkdir(mode=0o700)
    settings = SimpleNamespace(
        landscape_passphrase=None,
        get_landscape_url=lambda: "sqlite:////private/audit.db",
        get_payload_store_path=lambda: payload_root,
    )
    db_closed = False

    class FakeDB:
        def __enter__(self) -> FakeDB:
            return self

        def __exit__(self, *_args: object) -> None:
            nonlocal db_closed
            db_closed = True

    monkeypatch.setattr(capture_module, "settings_from_env", lambda: settings)
    monkeypatch.setattr(capture_module.LandscapeDB, "from_url", lambda *_args, **_kwargs: FakeDB())
    rows = [] if failure == "zero-refs" else [SimpleNamespace(source_data_ref="a" * 64)]
    monkeypatch.setattr(
        capture_module.RecorderFactory,
        "read_only",
        lambda _db: SimpleNamespace(query=SimpleNamespace(get_rows=lambda _run_id: rows)),
    )

    class Store:
        def __init__(self, _root: Path) -> None:
            pass

        def retrieve(self, _content_hash: str) -> bytes:
            raise OSError("raw retrieval failure /private/payload")

    monkeypatch.setattr(capture_module, "FilesystemPayloadStore", Store)

    with pytest.raises(acceptance.AcceptanceCheckError, match=match) as raised:
        acceptance.verify_payloads(_LANDSCAPE_RUN_ID)

    assert "/private" not in str(raised.value)
    assert db_closed is True


def test_verify_payloads_rejects_invalid_landscape_identity_before_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_module, "settings_from_env", lambda: pytest.fail("settings must not load"))

    with pytest.raises(acceptance.AcceptanceInputError, match="landscape run identity"):
        acceptance.verify_payloads("../not-a-run")
