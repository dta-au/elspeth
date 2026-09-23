"""Mode-aware AWS adapters reconstruct complete typed results before SDK dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from tests.fixtures.mock_audit import mock_item_audit_authority

from elspeth.contracts import CallStatus, RunMode
from elspeth.contracts.call_mode import ReplayCallEvidence
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient
from elspeth.plugins.transforms.aws.replay_sdk import ReplayOnlySDK
from elspeth.plugins.transforms.aws.textract_bucket_region import HeadBucketClient
from elspeth.plugins.transforms.aws.textract_client import TextractClient, TextractInlineClient, TextractServiceError


@dataclass
class Recorder:
    calls: list[dict[str, Any]] = field(default_factory=list)

    def allocate_call_index(self, _state_id: str, **_kwargs: object) -> int:
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(call_id=f"new-{len(self.calls)}")


@dataclass
class Session:
    evidence: ReplayCallEvidence
    mode: RunMode = RunMode.REPLAY
    source_run_id: str = "source-run"
    requests: list[dict[str, Any]] = field(default_factory=list)
    verifications: list[dict[str, Any]] = field(default_factory=list)

    def replay_call(self, **kwargs: Any) -> ReplayCallEvidence:
        self.requests.append(kwargs)
        return self.evidence

    def verify_call(self, **kwargs: Any) -> None:
        self.verifications.append(kwargs)


def _evidence(
    payload: dict[str, Any] | None, *, status: CallStatus = CallStatus.SUCCESS, error: dict[str, Any] | None = None
) -> ReplayCallEvidence:
    return ReplayCallEvidence("source-call", status, payload, error, 4.0)


def test_textract_replay_start_and_page_without_sdk_dispatch() -> None:
    recorder = Recorder()
    session = Session(
        _evidence(
            {
                "operation": "start_document_analysis",
                "status": "success",
                "job_id": "job-1",
                "request_id_present": True,
                "attempts": 2,
            }
        )
    )
    client = TextractClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        region="ap-southeast-2",
        sdk_client=ReplayOnlySDK(),
        max_response_bytes=100_000,
        call_mode_session=session,
    )
    receipt = client.start_document_analysis(
        bucket="docs",
        key="invoice.pdf",
        version=None,
        feature_types=("FORMS",),
        queries=(),
        client_request_token="a" * 64,
        source_client_request_token_fingerprint="b" * 64,
    )
    assert receipt.job_id == "job-1"
    assert recorder.calls[0]["source_call_id"] == "source-call"
    assert session.requests[0]["current_state_id"] == "state-1"
    assert session.requests[0]["request_data"]["client_request_token_fingerprint"] == "b" * 64

    session.evidence = _evidence(
        {
            "operation": "get_document_analysis",
            "status": "success",
            "attempts": 1,
            "request_id_present": True,
            "next_token_present": False,
            "next_token": None,
            "next_token_fingerprint": None,
            "semantic_response": {"JobStatus": "SUCCEEDED", "Blocks": []},
        }
    )
    page = client.get_document_analysis(job_id="job-1", next_token=None)
    assert page.semantic_response["JobStatus"] == "SUCCEEDED"
    assert page.next_token is None
    assert len(recorder.calls) == 2


def test_textract_replay_rejects_legacy_page_without_raw_next_token() -> None:
    recorder = Recorder()
    session = Session(
        _evidence(
            {
                "operation": "get_document_analysis",
                "status": "success",
                "attempts": 1,
                "request_id_present": True,
                "next_token_present": True,
                "next_token_fingerprint": "a" * 64,
                "semantic_response": {"JobStatus": "IN_PROGRESS"},
            }
        )
    )
    client = TextractClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        region="ap-southeast-2",
        sdk_client=ReplayOnlySDK(),
        max_response_bytes=100_000,
        call_mode_session=session,
    )
    with pytest.raises(AuditIntegrityError, match="complete result page"):
        client.get_document_analysis(job_id="job-1", next_token=None)
    assert recorder.calls == []


def test_textract_inline_replay_returns_exact_semantic_payload() -> None:
    recorder = Recorder()
    session = Session(
        _evidence(
            {
                "operation": "analyze_document",
                "status": "success",
                "attempts": 3,
                "request_id_present": True,
                "semantic_response": {"DocumentMetadata": {"Pages": 1}, "Blocks": []},
            }
        )
    )
    client = TextractInlineClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        region="ap-southeast-2",
        sdk_client=ReplayOnlySDK(),
        max_response_bytes=100_000,
        call_mode_session=session,
    )
    result = client.analyze_document(
        document_bytes=b"document",
        document_sha256="a" * 64,
        document_format="pdf",
        feature_types=("FORMS",),
        queries=(),
    )
    assert result.sdk_attempts == 3
    assert result.semantic_response["DocumentMetadata"]["Pages"] == 1
    assert len(recorder.calls) == 1


def test_s3_region_replay_returns_proof_without_sdk_dispatch() -> None:
    recorder = Recorder()
    session = Session(
        _evidence(
            {
                "operation": "head_bucket_region",
                "status": "verified",
                "observed_region": "ap-southeast-2",
                "proof_source": "response_header",
                "provider_code": None,
                "http_status": 200,
                "attempts": 1,
            }
        )
    )
    client = HeadBucketClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        region="ap-southeast-2",
        sdk_client=ReplayOnlySDK(),
        call_mode_session=session,
    )
    proof = client.verify_bucket_region("docs")
    assert proof.region == "ap-southeast-2"
    assert recorder.calls[0]["source_call_id"] == "source-call"


def test_guardrail_replay_uses_source_run_target_identity_and_restores_request_id() -> None:
    recorder = Recorder()
    session = Session(
        _evidence(
            {
                "operation": "apply_guardrail",
                "status": "safe",
                "attempts": 1,
                "detected": False,
                "intervened": False,
                "matched_filters": (),
                "usage": {
                    "contentPolicyUnits": 1,
                    "contextualGroundingPolicyUnits": 0,
                    "sensitiveInformationPolicyFreeUnits": 0,
                    "sensitiveInformationPolicyUnits": 0,
                    "topicPolicyUnits": 0,
                    "wordPolicyUnits": 0,
                },
                "request_id": "provider-request",
                "request_id_present": True,
            }
        )
    )
    client = BedrockGuardrailsClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        guardrail_identifier="guardrail",
        guardrail_version="1",
        region="us-east-1",
        audit_salt=b"current-run-key-current-run-key-01",
        source_audit_salt=b"source-run-key-source-run-key-01",
        sdk_client=ReplayOnlySDK(),
        call_mode_session=session,
    )
    decision = client.apply_guardrail(text="private text", source="INPUT", required_filters=("PROMPT_ATTACK",))
    assert decision.request_id == "provider-request"
    assert session.requests[0]["request_data"]["target_fingerprint"] != recorder.calls[0]["request_data"].to_dict()["target_fingerprint"]
    assert "private text" not in repr(recorder.calls)


def test_guardrail_replay_refuses_legacy_response_missing_request_id() -> None:
    recorder = Recorder()
    session = Session(
        _evidence(
            {
                "operation": "apply_guardrail",
                "status": "safe",
                "attempts": 1,
                "detected": False,
                "intervened": False,
                "matched_filters": (),
                "usage": {},
                "request_id_present": True,
            }
        )
    )
    client = BedrockGuardrailsClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        guardrail_identifier="guardrail",
        guardrail_version="1",
        region="us-east-1",
        audit_salt=b"current-run-key-current-run-key-01",
        source_audit_salt=b"source-run-key-source-run-key-01",
        sdk_client=ReplayOnlySDK(),
        call_mode_session=session,
    )
    with pytest.raises(AuditIntegrityError, match="incomplete decision"):
        client.apply_guardrail(text="private text", source="INPUT", required_filters=("PROMPT_ATTACK",))
    assert recorder.calls == []


def test_textract_replay_restores_recorded_service_failure_without_sdk_dispatch() -> None:
    recorder = Recorder()
    session = Session(
        _evidence(
            {"operation": "start_document_analysis", "status": "service_error", "attempts": 3},
            status=CallStatus.ERROR,
            error={"type": "service_error", "code": "ThrottlingException", "retryable": True},
        )
    )
    client = TextractClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        region="ap-southeast-2",
        sdk_client=ReplayOnlySDK(),
        max_response_bytes=100_000,
        call_mode_session=session,
    )
    with pytest.raises(TextractServiceError) as exc_info:
        client.start_document_analysis(
            bucket="docs",
            key="invoice.pdf",
            version=None,
            feature_types=("FORMS",),
            queries=(),
            client_request_token="a" * 64,
            source_client_request_token_fingerprint="b" * 64,
        )
    assert exc_info.value.retryable is True
    assert recorder.calls[0]["status"] is CallStatus.ERROR
    assert recorder.calls[0]["source_call_id"] == "source-call"


def test_textract_verify_records_live_call_and_compares_source_request_identity() -> None:
    class LiveSDK:
        def start_document_analysis(self, **_kwargs: Any) -> object:
            return {"JobId": "live-job", "ResponseMetadata": {"RequestId": "req", "RetryAttempts": 0}}

        def close(self) -> None:
            return None

    recorder = Recorder()
    session = Session(_evidence(None), mode=RunMode.VERIFY)
    client = TextractClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        region="ap-southeast-2",
        sdk_client=LiveSDK(),
        max_response_bytes=100_000,
        call_mode_session=session,
    )
    receipt = client.start_document_analysis(
        bucket="docs",
        key="invoice.pdf",
        version=None,
        feature_types=("FORMS",),
        queries=(),
        client_request_token="a" * 64,
        source_client_request_token_fingerprint="b" * 64,
    )
    assert receipt.job_id == "live-job"
    assert recorder.calls[0]["source_call_id"] is None
    assert session.verifications[0]["current_call_id"] == "new-1"
    assert session.verifications[0]["request_data"]["client_request_token_fingerprint"] == "b" * 64


def test_guardrail_verify_compares_source_target_without_auditing_private_text() -> None:
    class LiveSDK:
        def apply_guardrail(self, **_kwargs: Any) -> object:
            return {
                "usage": {
                    "contentPolicyUnits": 1,
                    "contextualGroundingPolicyUnits": 0,
                    "sensitiveInformationPolicyFreeUnits": 0,
                    "sensitiveInformationPolicyUnits": 0,
                    "topicPolicyUnits": 0,
                    "wordPolicyUnits": 0,
                },
                "action": "NONE",
                "outputs": [],
                "assessments": [
                    {
                        "contentPolicy": {
                            "filters": [
                                {
                                    "type": "PROMPT_ATTACK",
                                    "confidence": "NONE",
                                    "action": "NONE",
                                    "detected": False,
                                }
                            ]
                        }
                    }
                ],
                "ResponseMetadata": {"RequestId": "provider-request", "RetryAttempts": 0},
            }

        def close(self) -> None:
            return None

    recorder = Recorder()
    session = Session(_evidence(None), mode=RunMode.VERIFY)
    client = BedrockGuardrailsClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        guardrail_identifier="guardrail",
        guardrail_version="1",
        region="us-east-1",
        audit_salt=b"current-run-key-current-run-key-01",
        source_audit_salt=b"source-run-key-source-run-key-01",
        sdk_client=LiveSDK(),
        call_mode_session=session,
    )
    decision = client.apply_guardrail(text="private text", source="INPUT", required_filters=("PROMPT_ATTACK",))
    assert decision.request_id == "provider-request"
    assert (
        session.verifications[0]["request_data"]["target_fingerprint"] != recorder.calls[0]["request_data"].to_dict()["target_fingerprint"]
    )
    assert session.verifications[0]["current_call_id"] == "new-1"
    assert "private text" not in repr(recorder.calls)
