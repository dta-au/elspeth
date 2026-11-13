"""Regression tests for the P3 cloud-transform sweep findings (20261111-plugins-review).

Each section names the sweep finding it pins. The tests drive the production
code paths the finding describes and assert on the observable outcome.
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from elspeth.contracts import RunMode, TransformResult
from elspeth.contracts.errors import PluginRetryableError
from elspeth.plugins.transforms.aws.guardrails_client import BedrockGuardrailsClient, GuardrailServiceError
from elspeth.plugins.transforms.aws.textract_bucket_region import BucketRegionCoordinator, BucketRegionUnverifiedError
from elspeth.plugins.transforms.aws.textract_document_analysis import AWSTextractDocumentAnalysis
from elspeth.plugins.transforms.aws.textract_inline_analysis import AWSTextractInlineAnalysis
from elspeth.plugins.transforms.aws.textract_result import (
    MalformedTextractResponse,
    normalize_analyze_document_result,
)
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.fixtures.mock_audit import mock_item_audit_authority
from tests.unit.plugins.transforms.azure.test_document_intelligence import _FakeClient as _DIFakeClient
from tests.unit.plugins.transforms.azure.test_document_intelligence import _post_202 as _di_post_202
from tests.unit.plugins.transforms.azure.test_document_intelligence import _Resp as _DIResp
from tests.unit.plugins.transforms.azure.test_document_intelligence import _run_with_fake as _di_run_with_fake
from tests.unit.plugins.transforms.azure.test_document_intelligence import _t_for_lro as _di_transform
from tests.unit.plugins.transforms.azure.test_prompt_shield import _create_mock_http_response as _ps_response
from tests.unit.plugins.transforms.azure.test_prompt_shield import _patch_httpx_client as _ps_patch_httpx_generator

# ---------------------------------------------------------------------------
# Finding #35: FORMS projection must carry checkbox selection state.
# ---------------------------------------------------------------------------


def _geometry() -> dict[str, Any]:
    return {
        "BoundingBox": {"Width": 0.5, "Height": 0.1, "Left": 0.1, "Top": 0.2},
        "Polygon": [
            {"X": 0.1, "Y": 0.2},
            {"X": 0.6, "Y": 0.2},
            {"X": 0.6, "Y": 0.3},
            {"X": 0.1, "Y": 0.3},
        ],
    }


def _checkbox_form_response(*statuses: str) -> dict[str, Any]:
    """One page holding one KEY/VALUE pair per status; each VALUE's CHILD is a checkbox."""
    blocks: list[dict[str, Any]] = [
        {"BlockType": "PAGE", "Id": "page-1", "Page": 1, "Geometry": _geometry()},
    ]
    for index, status in enumerate(statuses):
        blocks.extend(
            [
                {
                    "BlockType": "KEY_VALUE_SET",
                    "Id": f"key-{index}",
                    "Page": 1,
                    "EntityTypes": ["KEY"],
                    "Confidence": 95.0,
                    "Geometry": _geometry(),
                    "Relationships": [
                        {"Type": "CHILD", "Ids": [f"key-word-{index}"]},
                        {"Type": "VALUE", "Ids": [f"value-{index}"]},
                    ],
                },
                {
                    "BlockType": "WORD",
                    "Id": f"key-word-{index}",
                    "Page": 1,
                    "Text": f"Option {index}",
                    "Confidence": 95.0,
                    "Geometry": _geometry(),
                },
                {
                    "BlockType": "KEY_VALUE_SET",
                    "Id": f"value-{index}",
                    "Page": 1,
                    "EntityTypes": ["VALUE"],
                    "Confidence": 94.0,
                    "Geometry": _geometry(),
                    "Relationships": [{"Type": "CHILD", "Ids": [f"selection-{index}"]}],
                },
                {
                    "BlockType": "SELECTION_ELEMENT",
                    "Id": f"selection-{index}",
                    "Page": 1,
                    "SelectionStatus": status,
                    "Confidence": 93.0,
                    "Geometry": _geometry(),
                },
            ]
        )
    return {"DocumentMetadata": {"Pages": 1}, "AnalyzeDocumentModelVersion": "1.0", "Blocks": blocks}


def _normalize_forms(response: dict[str, Any]) -> Any:
    return normalize_analyze_document_result(
        response=response,
        feature_types=("FORMS",),
        max_blocks=1_000,
        max_result_bytes=1_000_000,
    )


def test_f35_forms_projection_distinguishes_checked_from_unchecked_checkbox() -> None:
    result = _normalize_forms(_checkbox_form_response("SELECTED", "NOT_SELECTED"))

    forms = list(result.forms)
    assert len(forms) == 2
    assert all("value_selection_status" in form for form in forms), [sorted(form) for form in forms]
    statuses = {form["key"]: form["value_selection_status"] for form in forms}
    assert statuses == {"Option 0": "SELECTED", "Option 1": "NOT_SELECTED"}


def test_f35_forms_projection_rejects_unknown_checkbox_status() -> None:
    with pytest.raises(MalformedTextractResponse, match="SELECTED or NOT_SELECTED"):
        _normalize_forms(_checkbox_form_response("MAYBE"))


# ---------------------------------------------------------------------------
# Finding #30: inline Textract must apply max_document_bytes while reading.
# ---------------------------------------------------------------------------

_F30_PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 4096
_F30_SHA = hashlib.sha256(_F30_PNG).hexdigest()


class _BoundedReadStore:
    """PayloadStore double that records full and bounded reads separately."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.full_reads: list[str] = []
        self.bounded_reads: list[tuple[str, int]] = []

    def store(self, content: bytes) -> str:
        raise AssertionError("the transform must never store payloads")

    def retrieve(self, content_hash: str) -> bytes:
        self.full_reads.append(content_hash)
        return self.content

    def retrieve_bounded(self, content_hash: str, *, max_bytes: int) -> bytes | None:
        self.bounded_reads.append((content_hash, max_bytes))
        return None if len(self.content) > max_bytes else self.content

    def exists(self, content_hash: str) -> bool:
        return True

    def delete(self, content_hash: str) -> bool:
        raise AssertionError("the transform must never delete payloads")


def test_f30_oversized_inline_document_is_rejected_by_a_bounded_read() -> None:
    transform = AWSTextractInlineAnalysis(
        {
            "region": "ap-southeast-2",
            "document_format": "png",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
            "max_document_bytes": 64,
        }
    )
    store = _BoundedReadStore(_F30_PNG)
    transform._payload_store = store

    result = transform._read_document_bytes(make_pipeline_row({"blob_ref": _F30_SHA}))

    assert isinstance(result, TransformResult)
    assert result.status == "error"
    assert result.reason["reason"] == "blob_too_large"
    assert result.reason["max_blob_bytes"] == 64
    assert store.full_reads == [], "the whole blob was read before the size cap applied"
    assert store.bounded_reads == [(_F30_SHA, 64)]
    assert result.reason["actual"] == ">64"


# ---------------------------------------------------------------------------
# Finding #36: Textract close() must not set the engine-owned shutdown event.
# ---------------------------------------------------------------------------


def _f36_document_analysis() -> AWSTextractDocumentAnalysis:
    return AWSTextractDocumentAnalysis(
        {
            "region": "ap-southeast-2",
            "bucket_field": "document_bucket",
            "key_field": "document_key",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
        }
    )


def _f36_inline_analysis() -> AWSTextractInlineAnalysis:
    return AWSTextractInlineAnalysis(
        {
            "region": "ap-southeast-2",
            "document_format": "png",
            "feature_types": ["FORMS"],
            "text_field": "textract_text",
            "schema": {"mode": "observed"},
        }
    )


def _f36_start(transform: AWSTextractDocumentAnalysis | AWSTextractInlineAnalysis, run_shutdown: threading.Event) -> None:
    transform.on_start(
        SimpleNamespace(
            landscape=object(),
            node_id="node-1",
            run_id="run-1",
            telemetry_emit=lambda _event: None,
            rate_limit_registry=None,
            shutdown_event=run_shutdown,
            payload_store=None,
            run_mode=RunMode.REPLAY,
        )
    )


_F36_FACTORIES = [
    pytest.param(_f36_document_analysis, id="document_analysis"),
    pytest.param(_f36_inline_analysis, id="inline_analysis"),
]


@pytest.mark.parametrize("factory", _F36_FACTORIES)
def test_f36_close_leaves_the_engine_shutdown_event_unset(factory: Any) -> None:
    run_shutdown = threading.Event()
    transform = factory()
    _f36_start(transform, run_shutdown)

    transform.close()

    assert not run_shutdown.is_set(), "plugin close() raised the run-wide cancellation signal"


@pytest.mark.parametrize("factory", _F36_FACTORIES)
def test_f36_engine_shutdown_event_still_stops_row_work(factory: Any) -> None:
    run_shutdown = threading.Event()
    transform = factory()
    _f36_start(transform, run_shutdown)
    run_shutdown.set()

    result = transform._process_single_with_state(
        make_pipeline_row({"blob_ref": _F30_SHA, "document_bucket": "bucket", "document_key": "key"}),
        "state-1",
        ctx=make_context(run_id="run-1"),
        token_id="token-1",
    )

    assert result.status == "error"
    assert result.reason == {"reason": "shutdown_requested"}
    transform.close()


# ---------------------------------------------------------------------------
# Finding #50: Guardrail transport failures must audit the observed attempts.
# ---------------------------------------------------------------------------


class _F50Execution:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def allocate_call_index(self, state_id: str, *, member_token: Any, work_item: Any) -> int:
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(id=f"call-{len(self.calls)}")


def test_f50_transport_failure_audits_every_botocore_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from botocore.exceptions import EndpointConnectionError
    from botocore.retries.standard import ExponentialBackoff

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(ExponentialBackoff, "delay_amount", lambda self, context: 0)

    execution = _F50Execution()
    client = BedrockGuardrailsClient(
        **mock_item_audit_authority("run-1"),
        execution=execution,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
        guardrail_identifier="privateguardrail",
        guardrail_version="7",
        region="us-east-1",
        audit_salt=b"0123456789abcdef0123456789abcdef",
        token_id="token-1",
    )
    sends: list[str] = []

    def unreachable_endpoint(request: Any, **_kwargs: Any) -> None:
        sends.append(request.url)
        raise EndpointConnectionError(endpoint_url=request.url)

    # Registered after the client's own hooks, so botocore's real standard
    # retry handler sees an HTTPClientError on every attempt.
    client.sdk_client.meta.events.register("before-send.bedrock-runtime", unreachable_endpoint)

    with pytest.raises(GuardrailServiceError):
        client.apply_guardrail(text="marker", source="INPUT", required_filters=("PROMPT_ATTACK",))

    assert len(sends) == 3, "botocore standard retries must have made three HTTP attempts"
    assert len(execution.calls) == 1
    assert execution.calls[0]["response_data"].to_dict()["attempts"] == len(sends)


# ---------------------------------------------------------------------------
# Finding #61: a poll-phase network error must not resubmit the document.
# ---------------------------------------------------------------------------


class _F61Client(_DIFakeClient):
    """Scripted DI client whose scripted entries may be exceptions to raise."""

    def post(self, url: str, *, json: Any = None, audit_json: Any = None, timeout: float | None = None) -> Any:
        response = super().post(url, json=json, audit_json=audit_json, timeout=timeout)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url: str, *, timeout: float | None = None) -> Any:
        response = super().get(url, timeout=timeout)
        if isinstance(response, Exception):
            raise response
        return response


def _f61_succeeded() -> _DIResp:
    return _DIResp(200, body={"status": "succeeded", "analyzeResult": {"modelId": "prebuilt-layout", "content": "# Doc"}})


def _f61_connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection dropped", request=httpx.Request("GET", "https://x"))


def test_f61_poll_network_error_is_retried_in_place_without_resubmitting() -> None:
    transform = _di_transform()
    client = _F61Client(_di_post_202(), [_f61_connect_error(), _DIResp(200, body={"status": "running"}), _f61_succeeded()])

    escalated: PluginRetryableError | None = None
    result = None
    try:
        result = _di_run_with_fake(transform, client)
    except PluginRetryableError as error:
        escalated = error

    assert escalated is None, f"a poll GET network error escalated to a row retry (resubmit): {escalated}"
    assert result is not None
    assert result.status == "success"
    assert len(client.posts) == 1
    assert client.get_calls == 3


def test_f61_submit_network_error_still_escalates_to_a_row_retry() -> None:
    transform = _di_transform()
    client = _F61Client([_f61_connect_error()], [])

    with pytest.raises(PluginRetryableError):
        _di_run_with_fake(transform, client)
    assert client.get_calls == 0


# ---------------------------------------------------------------------------
# Finding #98: an inherited single-flight HeadBucket failure must say so.
# ---------------------------------------------------------------------------


class _F98SignalingInFlight(dict[str, Any]):
    """In-flight map that signals when a follower looks up the leader's flight."""

    def __init__(self) -> None:
        super().__init__()
        self.follower_joined = threading.Event()

    def __getitem__(self, key: str) -> Any:
        value = super().__getitem__(key)
        self.follower_joined.set()
        return value


def _f98_concurrent_failure() -> tuple[BaseException, BaseException]:
    """Run a leader and one concurrent follower; the leader's HeadBucket fails."""
    coordinator = BucketRegionCoordinator()
    in_flight = _F98SignalingInFlight()
    coordinator._in_flight = in_flight
    leader_entered = threading.Event()
    outcomes: dict[str, BaseException] = {}

    def leader_verify() -> Any:
        leader_entered.set()
        assert in_flight.follower_joined.wait(timeout=5), "follower never joined the in-flight verification"
        raise BucketRegionUnverifiedError(code="AccessDenied", retryable=False, http_status=403)

    def follower_verify() -> Any:
        raise AssertionError("a concurrent follower must not run its own HeadBucket")

    def run(name: str, live_verify: Any) -> None:
        try:
            coordinator.verify("docs", live_verify)
        except BaseException as error:
            outcomes[name] = error

    leader = threading.Thread(target=run, args=("leader", leader_verify))
    leader.start()
    assert leader_entered.wait(timeout=5)
    follower = threading.Thread(target=run, args=("follower", follower_verify))
    follower.start()
    leader.join(timeout=5)
    follower.join(timeout=5)
    assert set(outcomes) == {"leader", "follower"}
    return outcomes["leader"], outcomes["follower"]


def test_f98_concurrent_follower_failure_is_marked_as_inherited() -> None:
    leader_error, follower_error = _f98_concurrent_failure()
    assert isinstance(leader_error, BucketRegionUnverifiedError)
    assert isinstance(follower_error, BucketRegionUnverifiedError)
    assert follower_error is not leader_error, "the follower re-raised the leader's exception object unmarked"

    transform = _f36_document_analysis()
    transform._run_id = "run-1"
    transform._node_id = "node-1"
    reasons = {}
    for name, error in (("leader", leader_error), ("follower", follower_error)):
        transform._bucket_region_coordinator = SimpleNamespace(verify=lambda bucket, live_verify, error=error: (_ for _ in ()).throw(error))
        result = transform._process_single_with_state(
            make_pipeline_row({"document_bucket": "docs", "document_key": "invoice.pdf"}),
            f"state-{name}",
            ctx=make_context(run_id="run-1"),
            token_id=f"token-{name}",
        )
        assert result.status == "error"
        reasons[name] = result.reason

    assert reasons["leader"] == {"reason": "bucket_region_unverified", "error_type": "AccessDenied"}
    assert reasons["follower"] == {
        "reason": "bucket_region_unverified",
        "error_type": "AccessDenied",
        "cache_status": "shared_failure",
    }


# ---------------------------------------------------------------------------
# Finding #114: no Azure safety field call may start after the row deadline.
# ---------------------------------------------------------------------------


_ps_patch_httpx_client = contextlib.contextmanager(_ps_patch_httpx_generator)


class _F114Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


def test_f114_remaining_fields_make_no_call_after_the_row_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import elspeth.plugins.transforms.azure.base as azure_base
    from elspeth.plugins.transforms.azure.prompt_shield import AzurePromptShield

    clock = _F114Clock()
    monkeypatch.setattr(azure_base, "time", clock)
    safe = _ps_response({"userPromptAnalysis": {"attackDetected": False}, "documentsAnalysis": [{"attackDetected": False}]})

    def slow_first_field_call(*_args: Any, **_kwargs: Any) -> Any:
        # The first field's call succeeds only after the row's capacity
        # budget (1 s) has elapsed, as a call that ran to its HTTP timeout.
        clock.now += 30.0
        return safe

    with _ps_patch_httpx_client() as http:
        http.post.side_effect = slow_first_field_call
        transform = AzurePromptShield(
            {
                "endpoint": "https://test.cognitiveservices.azure.com",
                "api_key": "test-key",
                "fields": ["first", "second", "third"],
                "max_capacity_retry_seconds": 1,
                "schema": {"mode": "observed"},
            }
        )
        ctx = make_context()
        transform.on_start(ctx)
        result = transform._process_single_with_state(make_pipeline_row({"first": "a", "second": "b", "third": "c"}), "state-1", ctx=ctx)

    assert http.post.call_count == 1, "a field call started after the row deadline the batch waiter is sized for"
    assert result.status == "error"
    assert result.reason["reason"] == "retry_timeout"
    assert result.reason["field"] == "second"
    assert result.retryable is False


# ---------------------------------------------------------------------------
# Finding #144: local guardrail input rejection is not a service failure.
# ---------------------------------------------------------------------------


def _f144_started_transform() -> tuple[Any, Any, _F144RefusingSDK]:
    from elspeth.plugins.transforms.aws.bedrock_prompt_shield import AWSBedrockPromptShield

    transform = AWSBedrockPromptShield(
        {
            "guardrail_identifier": "privateguardrail",
            "guardrail_version": "7",
            "region": "us-east-1",
            "fields": ["prompt"],
            "schema": {"mode": "observed"},
        }
    )
    sdk = _F144RefusingSDK()
    transform._sdk_client = sdk
    landscape = _F50Execution()
    context = make_context(landscape=landscape, **mock_item_audit_authority())
    transform.on_start(context)
    return transform, context, sdk


class _F144RefusingSDK:
    def __init__(self) -> None:
        self.calls = 0

    def apply_guardrail(self, **_kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("no Bedrock call may be made for locally rejected input")

    def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("value", "error_type"),
    [
        pytest.param("", "empty_field", id="empty"),
        pytest.param("x" * 1_000_001, "field_too_long", id="over_length"),
    ],
)
def test_f144_locally_rejected_field_is_invalid_input_not_a_service_failure(value: str, error_type: str) -> None:
    transform, context, sdk = _f144_started_transform()

    result = transform.process(make_pipeline_row({"prompt": value}), context)

    assert result.status == "error"
    assert result.reason == {"reason": "invalid_input", "field": "prompt", "error_type": error_type}
    assert result.retryable is False
    assert sdk.calls == 0


def test_f144_internal_invariant_value_error_is_not_masked_as_a_row_error(monkeypatch: pytest.MonkeyPatch) -> None:
    transform, context, _sdk = _f144_started_transform()

    def broken_invariant(_client: Any, **_kwargs: Any) -> Any:
        raise ValueError("required_filters must name a supported closed policy")

    monkeypatch.setattr(BedrockGuardrailsClient, "apply_guardrail", broken_invariant)

    with pytest.raises(ValueError, match="supported closed policy"):
        transform.process(make_pipeline_row({"prompt": "hello"}), context)


# ---------------------------------------------------------------------------
# Finding #134: a required PROMPT_SHIELD input control is credited only when the
# shield runs both analyses (maintainer ruling 2026-11-12: option (a), "both").
# ---------------------------------------------------------------------------


def _prompt_shield_credit(options: dict[str, object]) -> bool:
    from elspeth.contracts.plugin_capabilities import ControlRole, PluginCapability
    from elspeth.plugins.transforms.azure.prompt_shield import AzurePromptShield

    return AzurePromptShield.is_effective_blocking_control(
        capability=PluginCapability.PROMPT_SHIELD, role=ControlRole.INPUT, options=options
    )


@pytest.mark.parametrize("analysis_type", ["user_prompt", "document"])
def test_single_analysis_prompt_shield_gets_no_control_credit(analysis_type: str) -> None:
    assert _prompt_shield_credit({"analysis_type": analysis_type}) is False


@pytest.mark.parametrize("analysis_type", [None, "BOTH", "", 1])
def test_unrecognised_analysis_type_gets_no_control_credit(analysis_type: object) -> None:
    assert _prompt_shield_credit({"analysis_type": analysis_type}) is False


def test_both_analyses_prompt_shield_keeps_control_credit() -> None:
    assert _prompt_shield_credit({"analysis_type": "both"}) is True


def test_omitted_analysis_type_is_credited_as_the_both_default() -> None:
    from elspeth.plugins.transforms.azure.prompt_shield import AzurePromptShieldConfig

    assert AzurePromptShieldConfig.model_fields["analysis_type"].default == "both"
    assert _prompt_shield_credit({}) is True


def test_detect_only_prompt_shield_still_gets_no_credit_with_both() -> None:
    assert _prompt_shield_credit({"analysis_type": "both", "detect_only": True}) is False
