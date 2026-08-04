"""Unit tests for the verify-textract acceptance lane (F15)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import textract as textract_module

_RUN_ID = "6ad6bff9-5e84-48ea-8588-f49cfb93cc62"


def _textract_env(**updates: str) -> dict[str, str]:
    values = {
        "ELSPETH_ACCEPTANCE_S3_BUCKET": "acceptance-bucket",
        "ELSPETH_ACCEPTANCE_S3_PREFIX": f"acceptance/{_RUN_ID}",
        "AWS_REGION": "ap-southeast-2",
    }
    values.update(updates)
    return values


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "raw provider message sentinel"}}, operation)


class _FakeSDKClient:
    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        get_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.start_error = start_error
        self.get_error = get_error
        self.close_error = close_error
        self.start_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []
        self.closed = False

    def start_document_analysis(self, **kwargs: Any) -> object:
        self.start_calls.append(kwargs)
        if self.start_error is not None:
            raise self.start_error
        return {"JobId": "a" * 64}

    def get_document_analysis(self, **kwargs: Any) -> object:
        self.get_calls.append(kwargs)
        if self.get_error is not None:
            raise self.get_error
        return {"JobStatus": "FAILED"}

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _RegisteredManager:
    @staticmethod
    def get_transforms() -> list[object]:
        return [SimpleNamespace(name="aws_textract_document_analysis"), SimpleNamespace(name="llm")]


def _verify(
    env: dict[str, str],
    client: _FakeSDKClient,
    *,
    manager: object = _RegisteredManager,
    client_factory: Any = None,
) -> dict[str, object]:
    factories: list[dict[str, Any]] = []

    def sdk_client_factory(**kwargs: Any) -> _FakeSDKClient:
        factories.append(kwargs)
        return client

    return acceptance.verify_textract(
        env,
        plugin_manager_factory=manager,
        sdk_client_factory=client_factory or sdk_client_factory,
        probe_suffix_factory=lambda: "0" * 32,
    )


def test_facade_reexports_textract_owner_by_identity() -> None:
    assert acceptance.verify_textract is textract_module.verify_textract


def test_verify_textract_proves_registration_client_and_both_probe_actions() -> None:
    client = _FakeSDKClient(
        start_error=_client_error("InvalidS3ObjectException", "StartDocumentAnalysis"),
        get_error=_client_error("InvalidJobIdException", "GetDocumentAnalysis"),
    )

    details = _verify(_textract_env(), client)

    assert details == {
        "transform_registered": True,
        "client_constructed": True,
        "start_document_analysis_invocable": True,
        "get_document_analysis_invocable": True,
        "profiles_configured": 0,
        "profile_locations_invocable": True,
    }
    assert client.closed is True
    assert client.start_calls == [
        {
            "DocumentLocation": {
                "S3Object": {
                    "Bucket": "acceptance-bucket",
                    "Name": f"acceptance/{_RUN_ID}/verify-textract-{'0' * 32}.probe",
                }
            },
            "FeatureTypes": ["TABLES"],
        }
    ]
    assert client.get_calls == [{"JobId": client.get_calls[0]["JobId"], "MaxResults": 1}]
    assert len(client.get_calls[0]["JobId"]) == 64
    rendered = json.dumps(details)
    assert "raw provider message sentinel" not in rendered


def test_verify_textract_probes_each_operator_document_profile_location() -> None:
    """F8 (elspeth-cd0f6a6cd9): the harness must exercise the operator profile
    path, not only the raw SDK — each configured profile's binding must satisfy
    the engine's own bucket-mode rules and its granted location must be
    invocable by the task role."""
    client = _FakeSDKClient(
        start_error=_client_error("InvalidS3ObjectException", "StartDocumentAnalysis"),
        get_error=_client_error("InvalidJobIdException", "GetDocumentAnalysis"),
    )
    env = _textract_env(
        ELSPETH_WEB__AWS_TEXTRACT_PROFILES=json.dumps(
            [{"alias": "acceptance-docs", "bucket": "operator-owned-docs", "key_prefix": "org/acme"}]
        )
    )

    details = _verify(env, client)

    assert details["profiles_configured"] == 1
    assert details["profile_locations_invocable"] is True
    profile_probe = client.start_calls[1]
    assert profile_probe["DocumentLocation"]["S3Object"]["Bucket"] == "operator-owned-docs"
    assert profile_probe["DocumentLocation"]["S3Object"]["Name"] == f"org/acme/verify-textract-{'0' * 32}.probe"


@pytest.mark.parametrize(
    "raw_profiles",
    [
        "not-json",
        "{}",
        "[]",
        '[{"alias": "acceptance-docs"}]',
        '[{"alias": "acceptance-docs", "bucket": "b", "key_prefix": "../up"}]',
    ],
)
def test_verify_textract_rejects_malformed_profile_settings(raw_profiles: str) -> None:
    client = _FakeSDKClient(
        start_error=_client_error("InvalidS3ObjectException", "StartDocumentAnalysis"),
        get_error=_client_error("InvalidJobIdException", "GetDocumentAnalysis"),
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="textract_profile_settings"):
        _verify(_textract_env(ELSPETH_WEB__AWS_TEXTRACT_PROFILES=raw_profiles), client)


def test_verify_textract_maps_denied_profile_location_to_static_check() -> None:
    class _ProfileDeniedClient(_FakeSDKClient):
        def start_document_analysis(self, **kwargs: Any) -> object:
            self.start_calls.append(kwargs)
            if len(self.start_calls) == 1:
                raise _client_error("InvalidS3ObjectException", "StartDocumentAnalysis")
            raise _client_error("AccessDeniedException", "StartDocumentAnalysis")

    client = _ProfileDeniedClient(get_error=_client_error("InvalidJobIdException", "GetDocumentAnalysis"))
    env = _textract_env(ELSPETH_WEB__AWS_TEXTRACT_PROFILES=json.dumps([{"alias": "acceptance-docs", "bucket": "operator-owned-docs"}]))

    with pytest.raises(acceptance.AcceptanceCheckError, match="textract_profile_start_document_analysis"):
        _verify(env, client)
    assert client.closed is True


def test_verify_textract_rejects_aws_credential_overrides_before_any_call() -> None:
    raw = "raw-credential-endpoint-role-arn-sentinel"
    with pytest.raises(acceptance.AcceptanceCheckError, match="textract_aws_override") as raised:
        acceptance.verify_textract(
            _textract_env(AWS_ACCESS_KEY_ID=raw),
            plugin_manager_factory=pytest.fail,
            sdk_client_factory=pytest.fail,
        )
    assert raw not in str(raised.value)


@pytest.mark.parametrize(
    "updates",
    [
        {"ELSPETH_ACCEPTANCE_S3_BUCKET": ""},
        {"ELSPETH_ACCEPTANCE_S3_PREFIX": "no-run-identity"},
        {"ELSPETH_ACCEPTANCE_S3_PREFIX": f"/acceptance/{_RUN_ID}/"},
        {"AWS_REGION": ""},
    ],
)
def test_verify_textract_rejects_invalid_inputs_before_any_call(updates: dict[str, str]) -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match="textract_input"):
        acceptance.verify_textract(
            _textract_env(**updates),
            plugin_manager_factory=pytest.fail,
            sdk_client_factory=pytest.fail,
        )


def test_verify_textract_fails_when_transform_is_not_registered() -> None:
    class _Missing:
        @staticmethod
        def get_transforms() -> list[object]:
            return [SimpleNamespace(name="llm")]

    with pytest.raises(acceptance.AcceptanceCheckError, match="textract_plugin"):
        _verify(_textract_env(), _FakeSDKClient(), manager=_Missing)


def test_verify_textract_fails_statically_when_discovery_or_client_construction_breaks() -> None:
    def raising_manager() -> object:
        raise RuntimeError("raw registry failure sentinel")

    with pytest.raises(acceptance.AcceptanceCheckError, match="textract_plugin") as raised:
        _verify(_textract_env(), _FakeSDKClient(), manager=raising_manager)
    assert "raw registry failure sentinel" not in str(raised.value)

    def failing_factory(**_kwargs: Any) -> object:
        raise RuntimeError("raw boto construction sentinel")

    with pytest.raises(acceptance.AcceptanceCheckError, match="textract_client") as raised:
        _verify(_textract_env(), _FakeSDKClient(), client_factory=failing_factory)
    assert "raw boto construction sentinel" not in str(raised.value)


@pytest.mark.parametrize(
    ("start_error", "get_error", "check"),
    [
        (
            _client_error("AccessDeniedException", "StartDocumentAnalysis"),
            None,
            "textract_start_document_analysis",
        ),
        (
            EndpointConnectionError(endpoint_url="https://textract.invalid"),
            None,
            "textract_start_document_analysis",
        ),
        (
            _client_error("InvalidS3ObjectException", "StartDocumentAnalysis"),
            _client_error("AccessDeniedException", "GetDocumentAnalysis"),
            "textract_get_document_analysis",
        ),
    ],
)
def test_verify_textract_maps_denied_or_unreachable_probes_to_static_checks(
    start_error: Exception | None,
    get_error: Exception | None,
    check: str,
) -> None:
    client = _FakeSDKClient(start_error=start_error, get_error=get_error)

    with pytest.raises(acceptance.AcceptanceCheckError, match=check) as raised:
        _verify(_textract_env(), client)

    assert client.closed is True
    assert "raw provider message sentinel" not in str(raised.value)


def test_verify_textract_accepts_outright_probe_success_as_invocable() -> None:
    details = _verify(_textract_env(), _FakeSDKClient())

    assert details["start_document_analysis_invocable"] is True
    assert details["get_document_analysis_invocable"] is True


def test_verify_textract_reports_close_failure_after_successful_probes() -> None:
    client = _FakeSDKClient(
        start_error=_client_error("InvalidS3ObjectException", "StartDocumentAnalysis"),
        get_error=_client_error("InvalidJobIdException", "GetDocumentAnalysis"),
        close_error=RuntimeError("close failed"),
    )

    with pytest.raises(acceptance.AcceptanceCheckError, match="textract_resource_close"):
        _verify(_textract_env(), client)


def _receipt_env() -> dict[str, str]:
    return {
        "ELSPETH_ACCEPTANCE_CANDIDATE_SHA": "a" * 40,
        "ELSPETH_ACCEPTANCE_TASK_ARN": "arn:aws:ecs:ap-southeast-2:123456789012:task/cluster/task",
        "ELSPETH_ACCEPTANCE_SCENARIO_ID": "A",
    }


def _textract_details() -> dict[str, object]:
    return {
        "transform_registered": True,
        "client_constructed": True,
        "start_document_analysis_invocable": True,
        "get_document_analysis_invocable": True,
        "profiles_configured": 1,
        "profile_locations_invocable": True,
    }


def test_textract_exec_receipt_round_trip_binds_check_and_details() -> None:
    env = _receipt_env()
    sentinel = acceptance.encode_exec_receipt("verify-textract", _textract_details(), env)

    payload = acceptance.extract_exec_receipt(
        f"noise\n{sentinel}\nnoise\n",
        expected_candidate_sha=env["ELSPETH_ACCEPTANCE_CANDIDATE_SHA"],
        expected_task_arn=env["ELSPETH_ACCEPTANCE_TASK_ARN"],
        expected_scenario_id="A",
        expected_check="verify-textract",
    )

    assert payload["check"] == "verify-textract"
    assert payload["details"] == _textract_details()


@pytest.mark.parametrize(
    "mutation",
    ["drop_field", "extra_field", "false_probe"],
)
def test_textract_exec_receipt_rejects_open_or_failed_details(mutation: str) -> None:
    details = _textract_details()
    if mutation == "drop_field":
        details.pop("client_constructed")
    elif mutation == "extra_field":
        details["raw_response"] = "leak"
    else:
        details["start_document_analysis_invocable"] = False

    with pytest.raises(acceptance.AcceptanceCheckError, match="exec_receipt_schema"):
        acceptance.encode_exec_receipt("verify-textract", details, _receipt_env())
