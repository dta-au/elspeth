"""Unit tests for the closed-vocabulary acceptance failure projection (F8/F12)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import contracts
from elspeth.web._aws_ecs_acceptance.http_client import AcceptanceHttpClient
from elspeth.web._aws_ecs_acceptance.state import (
    AcceptanceCredentials,
    AcceptanceState,
    read_acceptance_state,
    write_acceptance_state,
)


@pytest.fixture(autouse=True)
def _clean_step() -> None:
    contracts.reset_acceptance_step()
    yield
    contracts.reset_acceptance_step()


def _state() -> AcceptanceState:
    return AcceptanceState(
        schema_version=1,
        session_id="00000000-0000-4000-8000-000000000001",
        tutorial_session_id="00000000-0000-4000-8000-000000000002",
        blob_id="00000000-0000-4000-8000-000000000003",
        run_id="00000000-0000-4000-8000-000000000004",
        landscape_run_id="00000000-0000-4000-8000-000000000005",
        artifact_id="a" * 64,
        uploaded_sha256="b" * 64,
        blob_sha256="b" * 64,
        artifact_sha256="c" * 64,
        run_status="completed",
        source_rows=1,
        failed_tokens=0,
        captured_at="2026-07-30T00:00:00Z",
        completed_at="2026-07-30T00:00:01Z",
    )


def test_error_code_vocabulary_is_closed_and_coerces_unknown_codes() -> None:
    assert "state_file_unwritable" in contracts.ACCEPTANCE_ERROR_CODES
    assert "ca_unreadable" in contracts.ACCEPTANCE_ERROR_CODES

    coerced = contracts.AcceptanceHttpError("static", error_code="not-in-the-vocabulary")
    assert coerced.error_code == "acceptance_internal"


def test_acceptance_step_requires_a_vocabulary_name() -> None:
    with pytest.raises(ValueError, match="unknown acceptance step"), contracts.acceptance_step("made_up_step"):
        pytest.fail("step body must not run")


def test_unwritable_state_directory_projects_state_file_unwritable(tmp_path: Path) -> None:
    # F12: the bind-mounted acceptance dir is not writable by the container
    # user; this must surface as a named code, not an internal error.
    state_dir = tmp_path / "acceptance"
    state_dir.mkdir(mode=0o500)

    with pytest.raises(contracts.AcceptanceStateError) as raised:
        write_acceptance_state(state_dir / "state.json", _state())

    assert raised.value.error_code == "state_file_unwritable"
    assert contracts.current_acceptance_step() == "state_persist"


def test_state_read_failures_project_distinct_closed_codes(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(contracts.AcceptanceStateError) as raised:
        read_acceptance_state(missing)
    assert raised.value.error_code == "state_file_unreadable"

    unreadable = tmp_path / "unreadable.json"
    unreadable.write_bytes(b"{}")
    unreadable.chmod(0o000)
    with pytest.raises(contracts.AcceptanceStateError) as raised:
        read_acceptance_state(unreadable)
    assert raised.value.error_code == "state_file_unreadable"
    unreadable.chmod(0o600)

    exposed = tmp_path / "exposed.json"
    exposed.write_bytes(b"{}")
    exposed.chmod(0o644)
    with pytest.raises(contracts.AcceptanceStateError) as raised:
        read_acceptance_state(exposed)
    assert raised.value.error_code == "state_file_untrusted"

    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"not-json")
    invalid.chmod(0o600)
    with pytest.raises(contracts.AcceptanceStateError) as raised:
        read_acceptance_state(invalid)
    assert raised.value.error_code == "state_file_invalid"
    assert contracts.current_acceptance_step() == "state_load"


def _client_env(**updates: str) -> dict[str, str]:
    values = {
        "ELSPETH_ACCEPTANCE_BASE_URL": "https://acceptance.example.invalid",
        "ELSPETH_ACCEPTANCE_BEARER_TOKEN": "acceptance-token",
    }
    values.update(updates)
    return values


def test_declared_ca_bundle_must_be_readable_before_any_request(tmp_path: Path) -> None:
    # F12: a 0600 root-owned ca.pem was unreadable by uid 1654 and produced
    # only AcceptanceInternalError.  The client now preflights the declared
    # trust bundle and names the failure.
    with pytest.raises(contracts.AcceptanceHttpError) as raised:
        AcceptanceHttpClient.from_env(_client_env(SSL_CERT_FILE=str(tmp_path / "missing-ca.pem")))
    assert raised.value.error_code == "ca_unreadable"
    assert contracts.current_acceptance_step() == "client_setup"

    contracts.reset_acceptance_step()
    unreadable = tmp_path / "ca.pem"
    unreadable.write_bytes(b"pem")
    unreadable.chmod(0o000)
    with pytest.raises(contracts.AcceptanceHttpError) as raised:
        AcceptanceHttpClient.from_env(_client_env(SSL_CERT_FILE=str(unreadable)))
    assert raised.value.error_code == "ca_unreadable"
    unreadable.chmod(0o600)

    client = AcceptanceHttpClient.from_env(
        _client_env(SSL_CERT_FILE=str(unreadable)),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    with client:
        assert client.request_json("GET", "/api/health", expected_statuses={200}) == {}


def test_requests_ca_bundle_is_ignored_because_httpx_does_not_consume_it(tmp_path: Path) -> None:
    client = AcceptanceHttpClient.from_env(
        _client_env(REQUESTS_CA_BUNDLE=str(tmp_path / "missing-requests-ca.pem")),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )

    with client:
        assert client.request_json("GET", "/api/health", expected_statuses={200}) == {}


def test_unexpected_http_status_projects_code_and_integer_status() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(503, json={"detail": "server secret"}))
    client = AcceptanceHttpClient(
        origin="https://acceptance.example.invalid",
        credentials=AcceptanceCredentials(mode="bearer", bearer_token="token"),
        transport=transport,
    )

    with client, pytest.raises(contracts.AcceptanceHttpError) as raised:
        client.request_json("GET", "/api/runs/x", expected_statuses={200})

    assert raised.value.error_code == "unexpected_http_status"
    assert raised.value.status == 503
    assert "server secret" not in str(raised.value)


def test_login_failures_are_tagged_with_the_login_step() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(502, json={}))
    client = AcceptanceHttpClient(
        origin="https://acceptance.example.invalid",
        credentials=AcceptanceCredentials(mode="local", username="operator", password="secret"),
        transport=transport,
    )

    with client, pytest.raises(contracts.AcceptanceHttpError) as raised:
        client.authenticate(register=False)

    assert raised.value.status == 502
    assert contracts.current_acceptance_step() == "login"


def test_connection_and_timeout_failures_project_closed_codes() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to private host")

    client = AcceptanceHttpClient(
        origin="https://acceptance.example.invalid",
        credentials=AcceptanceCredentials(mode="bearer", bearer_token="token"),
        transport=httpx.MockTransport(refuse),
    )
    with client, pytest.raises(contracts.AcceptanceHttpError) as raised:
        client.request_json("GET", "/api/health", expected_statuses={200})
    assert raised.value.error_code == "connection_failed"

    def stall(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = AcceptanceHttpClient(
        origin="https://acceptance.example.invalid",
        credentials=AcceptanceCredentials(mode="bearer", bearer_token="token"),
        transport=httpx.MockTransport(stall),
    )
    with client, pytest.raises(contracts.AcceptanceHttpError) as raised:
        client.request_json("GET", "/api/health", expected_statuses={200})
    assert raised.value.error_code == "request_timeout"


def test_malformed_json_projects_response_shape_invalid() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"\xff not json"))
    client = AcceptanceHttpClient(
        origin="https://acceptance.example.invalid",
        credentials=AcceptanceCredentials(mode="bearer", bearer_token="token"),
        transport=transport,
    )

    with client, pytest.raises(contracts.AcceptanceHttpError) as raised:
        client.request_json("GET", "/api/health", expected_statuses={200})

    assert raised.value.error_code == "response_shape_invalid"


def test_main_projects_step_error_code_and_status_for_http_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_capture(_env: object, *, state_file: object) -> None:
        del state_file
        with contracts.acceptance_step("capture_fetch"), contracts.acceptance_step("login"):
            raise contracts.AcceptanceHttpError(
                "acceptance request returned an unexpected HTTP status",
                error_code="unexpected_http_status",
                status=502,
            )

    monkeypatch.setattr(acceptance, "capture", failing_capture)
    assert acceptance.main(["capture", "--state-file", "state.json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "error_class": "AcceptanceHttpError",
        "error_code": "unexpected_http_status",
        "status": 502,
        "step": "login",
    }


def test_main_projects_state_file_unwritable_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_dir = tmp_path / "acceptance"
    state_dir.mkdir(mode=0o500)

    def capture_then_persist(_env: object, *, state_file: Path) -> None:
        write_acceptance_state(state_file, _state())

    monkeypatch.setattr(acceptance, "capture", capture_then_persist)
    assert acceptance.main(["capture", "--state-file", str(state_dir / "state.json")]) == 1
    envelope = json.loads(capsys.readouterr().err)
    assert envelope == {
        "error_class": "AcceptanceStateError",
        "error_code": "state_file_unwritable",
        "step": "state_persist",
    }


def test_main_projects_missing_live_inputs_with_static_names(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def failing_guardrails(_env: object) -> dict[str, object]:
        raise contracts.AcceptanceCheckError(
            "guardrails_live_inputs_missing",
            missing=("ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT", "ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS"),
        )

    monkeypatch.setattr(acceptance, "run_bedrock_guardrails_live", failing_guardrails)
    assert acceptance.main(["verify-bedrock-guardrails"]) == 1
    assert json.loads(capsys.readouterr().err) == {
        "error_class": "AcceptanceCheckError",
        "check": "guardrails_live_inputs_missing",
        "missing": [
            "ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT",
            "ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS",
        ],
        "step": None,
    }


def test_main_resets_stale_steps_between_invocations(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def step_tagged_failure() -> None:
        with contracts.acceptance_step("state_persist"):
            raise RuntimeError("boom")

    monkeypatch.setattr(acceptance, "provision_storage", step_tagged_failure)
    assert acceptance.main(["provision-storage"]) == 1
    assert json.loads(capsys.readouterr().err)["step"] == "state_persist"

    def plain_failure() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(acceptance, "provision_storage", plain_failure)
    assert acceptance.main(["provision-storage"]) == 1
    assert json.loads(capsys.readouterr().err)["step"] is None


def test_check_error_with_cause_projects_static_env_tokens_only() -> None:
    """The cause taxonomy carries the exception class and ELSPETH_* tokens —
    never message text or values (elspeth-dfd09564d5: storage_settings was
    undiagnosable because the underlying cause was swallowed entirely)."""
    exc = RuntimeError("Unknown ELSPETH_WEB__ setting: ELSPETH_WEB__DEFAULT_LLM_PROFILE (password=hunter2)")  # secret-scan: allow-this-line
    error = contracts.check_error_with_cause("storage_settings", exc)
    envelope = contracts.acceptance_error_envelope(error)
    assert envelope["check"] == "storage_settings"
    assert envelope["cause_class"] == "RuntimeError"
    assert envelope["cause_fields"] == ["ELSPETH_WEB__DEFAULT_LLM_PROFILE"]
    flattened = json.dumps(envelope)
    assert "hunter2" not in flattened
    assert "password" not in flattened


def test_check_error_with_cause_projects_pydantic_field_locations() -> None:
    import pydantic

    class _Probe(pydantic.BaseModel):
        composer_max_discovery_turns: int

    try:
        _Probe.model_validate({})
    except pydantic.ValidationError as exc:
        error = contracts.check_error_with_cause("storage_settings", exc)
    envelope = contracts.acceptance_error_envelope(error)
    assert envelope["cause_class"] == "ValidationError"
    assert envelope["cause_fields"] == ["composer_max_discovery_turns"]


def test_check_error_with_cause_redacts_operator_controlled_mapping_keys() -> None:
    """A malformed llm_profiles alias must never reach the envelope verbatim.

    ``llm_profiles`` is ``Mapping[str, LLMProfileSettings]`` — the dict key is
    an operator-chosen alias, not schema identity, but pydantic's ``loc``
    tuple places it in the same position as a static field name
    (``llm_profiles.<alias>.credential_ref``). Only declared field names may
    survive into stderr/retained evidence; the alias itself must be redacted.
    """
    import pydantic

    from elspeth.web.config import WebSettings

    try:
        WebSettings.model_validate({"llm_profiles": {"MALICIOUS_SECRET_ALIAS": {}}})
    except pydantic.ValidationError as exc:
        error = contracts.check_error_with_cause("settings_load", exc)
    envelope = contracts.acceptance_error_envelope(error)
    flattened = json.dumps(envelope)
    assert "MALICIOUS_SECRET_ALIAS" not in flattened
    assert any(field.startswith("llm_profiles.<redacted>.") for field in envelope["cause_fields"])


def test_check_error_without_cause_keeps_prior_envelope_shape() -> None:
    envelope = contracts.acceptance_error_envelope(contracts.AcceptanceCheckError("storage_identity"))
    assert "cause_class" not in envelope
    assert "cause_fields" not in envelope


def test_provision_storage_settings_failure_names_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web._aws_ecs_acceptance import capture

    def _boom() -> None:
        raise RuntimeError("Unknown ELSPETH_WEB__ setting: ELSPETH_WEB__DEFAULT_LLM_PROFILE")

    monkeypatch.setattr(capture, "settings_from_env", _boom)
    with pytest.raises(contracts.AcceptanceCheckError) as excinfo:
        capture.provision_storage()
    envelope = contracts.acceptance_error_envelope(excinfo.value)
    assert envelope["check"] == "storage_settings"
    assert envelope["cause_class"] == "RuntimeError"
    assert envelope["cause_fields"] == ["ELSPETH_WEB__DEFAULT_LLM_PROFILE"]
