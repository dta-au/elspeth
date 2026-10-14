"""Provider wire bytes stay separate from persisted audit data and bounded replies."""

from __future__ import annotations

import base64
import gzip
import json
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch

import h11
import httpx
import pytest
import respx

from elspeth.contracts import Call, CallStatus, CallType, RunMode
from elspeth.contracts.audit_protocols import CallRecorder
from elspeth.contracts.call_data import HTTPCallResponse
from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.contracts.call_mode import CallModeSession, ReplayCallEvidence
from elspeth.contracts.chat_parts import ChatMessage, ImagePart, audit_messages, image_response_is_audit_safe, wire_messages
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.events import ExternalCallCompleted, TelemetryEvent
from elspeth.core.canonical import stable_hash
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient, HTTPResponseAuditRejectedError
from elspeth.plugins.infrastructure.clients.llm import LLMClientError
from elspeth.plugins.transforms.llm.provider import LLMAuditParent
from elspeth.plugins.transforms.llm.providers import gateway, openrouter
from tests.fixtures.landscape import RecorderSetup, make_recorder_with_run
from tests.fixtures.mock_audit import mock_audit_authority


@dataclass
class _Harness:
    provider: openrouter.OpenRouterLLMProvider | gateway.GatewayLLMProvider
    setup: RecorderSetup
    store: FilesystemPayloadStore
    parent: LLMAuditParent
    events: list[TelemetryEvent]
    url: str


@pytest.fixture(params=["openrouter", "gateway"])
def harness(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Harness]:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-provider-http-projection")
    store = FilesystemPayloadStore(tmp_path / "payloads")
    setup = make_recorder_with_run(payload_store=store)
    operation = setup.factory.execution.begin_operation(setup.source_node_id, "source_load", coordination_token=setup.coordination_token)
    parent = LLMAuditParent.for_operation(operation_id=operation.operation_id, coordination_token=setup.coordination_token)
    events: list[TelemetryEvent] = []
    if request.param == "openrouter":
        provider = openrouter.OpenRouterLLMProvider(
            api_key="test-key", recorder=setup.factory.execution, run_id=setup.run_id, telemetry_emit=events.append
        )
        url = "https://openrouter.ai/api/v1/chat/completions"
    else:
        provider = gateway.GatewayLLMProvider(
            endpoint="https://gateway.example/api/llm/v1",
            api_key="test-key",
            contract_major=1,
            required_capabilities=(),
            timeout_seconds=30.0,
            recorder=setup.factory.execution,
            run_id=setup.run_id,
            telemetry_emit=events.append,
        )
        url = "https://gateway.example/api/llm/v1/chat/completions"
    yield _Harness(provider, setup, store, parent, events, url)
    provider.close()
    setup.db.close()


def _response(status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"X-ELSPETH-LLM-Gateway-Contract": "1"},
        json={
            "model": "test-model",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0},
            "error": {"code": "upstream_unavailable"},
        },
    )


def _query(harness: _Harness, messages: list[ChatMessage]) -> None:
    harness.provider.execute_query(messages, model="test-model", temperature=0.0, max_tokens=32, audit_parent=harness.parent)


@pytest.mark.parametrize("outcome", ["success", "provider_error", "network_error"])
@respx.mock
def test_image_wire_is_preserved_but_every_retained_request_is_bytes_free(harness: _Harness, outcome: str) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\nprivate-image-marker", blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    route = respx.post(harness.url)
    if outcome == "network_error":
        route.mock(side_effect=httpx.ConnectError("connection refused"))
    else:
        route.mock(return_value=_response(200 if outcome == "success" else 503))
    if outcome == "success":
        _query(harness, messages)
    else:
        with pytest.raises(LLMClientError):
            _query(harness, messages)

    encoded_image = base64.b64encode(image.data).decode("ascii")
    assert encoded_image in route.calls[0].request.content.decode()
    calls = harness.setup.factory.execution.get_operation_calls(harness.parent.operation_id)
    assert {call.call_type for call in calls} == {CallType.HTTP, CallType.LLM}
    for call in calls:
        assert call.request_ref is not None
        retained = harness.store.retrieve(call.request_ref)
        assert encoded_image.encode() not in retained
        assert b"data:image/" not in retained
        assert image.sha256.encode() in retained
        payload = json.loads(retained)
        recorded_messages = payload["json"]["messages"] if call.call_type is CallType.HTTP else payload["messages"]
        assert recorded_messages == audit_messages(messages)
    for event in harness.events:
        if isinstance(event, ExternalCallCompleted):
            assert event.request_payload is not None
            assert encoded_image not in json.dumps(event.request_payload.to_dict())


@respx.mock
def test_text_only_http_request_keeps_its_existing_shape(harness: _Harness) -> None:
    messages = [ChatMessage(role="user", content="hello")]
    route = respx.post(harness.url).mock(return_value=_response())
    _query(harness, messages)
    call = next(
        call for call in harness.setup.factory.execution.get_operation_calls(harness.parent.operation_id) if call.call_type is CallType.HTTP
    )
    assert call.request_ref is not None
    retained = json.loads(harness.store.retrieve(call.request_ref))
    assert retained["json"] == json.loads(route.calls[0].request.content)


@respx.mock
def test_image_identity_changes_both_transport_and_semantic_request_hashes(harness: _Harness) -> None:
    respx.post(harness.url).mock(return_value=_response())
    for marker in (b"first", b"second"):
        image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\n" + marker, blob_ref=None)
        _query(harness, [ChatMessage(role="user", content=(image,))])
    calls = harness.setup.factory.execution.get_operation_calls(harness.parent.operation_id)
    for call_type in (CallType.HTTP, CallType.LLM):
        requests = [call.request_hash for call in calls if call.call_type is call_type]
        assert len(requests) == 2
        assert requests[0] != requests[1]


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("encoding_variant", ["standard", "unpadded", "urlsafe_unpadded", "hex", "byte_array", "percent"])
@pytest.mark.parametrize(
    "echo_form",
    [
        "content",
        "metadata",
        "escaped_slashes",
        "unicode",
        "header",
        "late_duplicate",
        "late_duplicate_utf16",
        "late_duplicate_utf32",
        "malformed_prefix",
        "malformed_prefix_utf16",
        "malformed_prefix_utf32",
    ],
)
@pytest.mark.parametrize("separator", ["", " \n\t"])
@respx.mock
def test_submitted_image_echo_is_rejected_before_any_audit_or_telemetry_egress(
    harness: _Harness, status: int, echo_form: str, separator: str, encoding_variant: str
) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\n" + b"\xff" * 8, blob_ref=None)
    encoded = base64.b64encode(image.data).decode("ascii")
    if encoding_variant == "unpadded":
        encoded = encoded.rstrip("=")
    elif encoding_variant == "urlsafe_unpadded":
        encoded = base64.urlsafe_b64encode(image.data).decode("ascii").rstrip("=")
    elif encoding_variant == "hex":
        encoded = image.data.hex().upper()
    elif encoding_variant == "byte_array":
        encoded = json.dumps(list(image.data), separators=(",", ":"))
    elif encoding_variant == "percent":
        encoded = "".join(f"%{ord(char):02x}" for char in encoded)
    echo = separator.join(encoded[index : index + 4] for index in range(0, len(encoded), 4))
    if encoding_variant == "byte_array":
        echo = encoded.replace(",", "," + separator)
    data_url = echo if encoding_variant in {"hex", "byte_array"} else f"data:image/png;base64,{echo}"
    payload = _response().json()
    headers = {"content-type": "application/json", "X-ELSPETH-LLM-Gateway-Contract": "1"}
    if echo_form == "content":
        payload["choices"][0]["message"]["content"] = data_url
    elif echo_form == "header":
        headers["X-Provider-Debug"] = echo
    else:
        payload["diagnostic"] = {"submitted_image": data_url}
    body = json.dumps(payload)
    if echo_form == "escaped_slashes":
        assert "/" in data_url or encoding_variant in {"hex", "byte_array"}
        body = body.replace("/", "\\/")
    elif echo_form == "unicode":
        body = body.replace(json.dumps(echo)[1:-1], "".join(f"\\u{ord(char):04x}" for char in echo))
    elif echo_form.startswith("malformed_prefix"):
        body = '"broken "' + "".join(f"\\u{ord(char):04x}" for char in echo) + '"'
    elif echo_form.startswith("late_duplicate"):
        # Sensitive headers suppress exact transport retention. The parsed last
        # duplicate key and first 10KB diagnostic cannot stand in for the wire.
        if echo_form == "late_duplicate":
            headers["set-cookie"] = "session=private"
        escaped = "".join(f"\\u{ord(char):04x}" for char in echo)
        body = '{"padding":"' + "x" * 11000 + '","echo":"' + escaped + '","echo":"safe"}'
    encoding = {
        "late_duplicate_utf16": "utf-16",
        "late_duplicate_utf32": "utf-32",
        "malformed_prefix_utf16": "utf-16",
        "malformed_prefix_utf32": "utf-32",
    }.get(echo_form, "utf-8")
    headers["content-type"] = f"application/json; charset={encoding}"
    respx.post(harness.url).mock(return_value=httpx.Response(status, headers=headers, content=body.encode(encoding)))
    with pytest.raises(LLMClientError) as caught:
        _query(harness, [ChatMessage(role="user", content=(image,))])
    assert not caught.value.retryable
    assert encoded not in str(caught.value)
    calls = harness.setup.factory.execution.get_operation_calls(harness.parent.operation_id)
    assert {call.call_type for call in calls} == {CallType.HTTP, CallType.LLM}
    assert all(call.status is CallStatus.ERROR for call in calls)
    for call in calls:
        if echo_form.startswith("malformed_prefix") and call.call_type is CallType.HTTP:
            assert call.error_json is not None
            assert json.loads(call.error_json)["type"] == "HTTPResponseAuditRejectedError"
        for ref in (call.request_ref, call.response_ref):
            if ref is not None:
                retained = harness.store.retrieve(ref)
                assert encoded.encode() not in retained
                assert data_url.encode() not in retained
                assert b"body_b64" not in retained
                assert b"submitted_image" not in retained
                assert b"X-Provider-Debug" not in retained
    for event in harness.events:
        if isinstance(event, ExternalCallCompleted):
            for payload in (event.request_payload, event.response_payload):
                if payload is not None:
                    retained = json.dumps(payload.to_dict())
                    assert encoded not in retained
                    assert data_url not in retained


@pytest.mark.parametrize("separator", ["", " ", "\t", "\f", "\v"])
@pytest.mark.parametrize("error_source", ["literal", "h11_repr"])
@pytest.mark.parametrize("encoding_variant", ["standard", "urlsafe_unpadded", "hex", "byte_array", "percent"])
@respx.mock
def test_protocol_error_image_echo_is_refused_before_error_audit_and_tracing(
    harness: _Harness, separator: str, error_source: str, encoding_variant: str
) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\n" + b"\xff" * 8, blob_ref=None)
    encoded = (
        base64.urlsafe_b64encode(image.data).decode().rstrip("=")
        if encoding_variant == "urlsafe_unpadded"
        else base64.b64encode(image.data).decode()
    )
    if encoding_variant == "hex":
        encoded = image.data.hex().upper()
    elif encoding_variant == "byte_array":
        encoded = json.dumps(list(image.data), separators=(",", ":"))
    elif encoding_variant == "percent":
        encoded = "".join(f"%{ord(char):02x}" for char in encoded)
    echo = separator.join(encoded[index : index + 4] for index in range(0, len(encoded), 4))
    if encoding_variant == "byte_array":
        echo = encoded.replace(",", "," + separator)
    error_message = "illegal status line: " + echo
    if error_source == "h11_repr":
        connection = h11.Connection(h11.CLIENT)
        connection.receive_data(echo.encode() + b"\r\n\r\n")
        with pytest.raises(h11.RemoteProtocolError, match="illegal status line") as protocol_error:
            connection.next_event()
        error_message = str(protocol_error.value)
        if separator == "\t":
            assert "\\t" in error_message
    respx.post(harness.url).mock(side_effect=httpx.RemoteProtocolError(error_message))
    with pytest.raises(LLMClientError) as caught:
        _query(harness, [ChatMessage(role="user", content=(image,))])
    assert not caught.value.retryable
    assert echo not in str(caught.value)
    calls = harness.setup.factory.execution.get_operation_calls(harness.parent.operation_id)
    assert {call.call_type for call in calls} == {CallType.HTTP, CallType.LLM}
    for call in calls:
        assert call.status is CallStatus.ERROR
        assert call.error_json is not None
        assert echo not in json.loads(call.error_json)["message"]
        if call.response_ref is not None:
            assert encoded.encode() not in harness.store.retrieve(call.response_ref)
    assert echo not in str(harness.events)


def test_provider_sets_explicit_decoded_and_encoded_response_limits(harness: _Harness) -> None:
    client = harness.provider._get_http_client(harness.parent)
    try:
        assert client._max_response_body_bytes == 16 * 1024 * 1024
        assert client._max_encoded_response_bytes == 16 * 1024 * 1024
    finally:
        harness.provider._release_http_client(harness.parent.cache_key)


@pytest.mark.parametrize("separator", ["", " "])
@respx.mock
def test_response_limit_error_cannot_retain_image_echo_in_headers(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, separator: str
) -> None:
    monkeypatch.setattr(openrouter, "_MAX_RESPONSE_BODY_BYTES", 1024)
    monkeypatch.setattr(gateway, "_MAX_RESPONSE_BODY_BYTES", 1024)
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\nprivate-image", blob_ref=None)
    encoded = base64.b64encode(image.data).decode()
    echo = separator.join(encoded[index : index + 4] for index in range(0, len(encoded), 4))
    respx.post(harness.url).mock(return_value=httpx.Response(503, headers={"x-debug": echo}, content=b"x" * 1025))
    with pytest.raises(LLMClientError) as caught:
        _query(harness, [ChatMessage(role="user", content=(image,))])
    assert not caught.value.retryable
    calls = harness.setup.factory.execution.get_operation_calls(harness.parent.operation_id)
    assert {call.call_type for call in calls} == {CallType.HTTP, CallType.LLM}
    for call in calls:
        assert call.status is CallStatus.ERROR
        if call.response_ref is not None:
            retained = harness.store.retrieve(call.response_ref)
            assert encoded.encode() not in retained
            assert b"x-debug" not in retained
    assert encoded not in str(harness.events)


@pytest.mark.parametrize("outcome", ["success", "provider_error", "cap_error"])
@respx.mock
def test_admission_programming_error_propagates_without_audit_or_telemetry(outcome: str) -> None:
    recorder = Mock(spec=CallRecorder)
    recorder.allocate_operation_call_index.return_value = 0
    events: list[TelemetryEvent] = []
    client = AuditedHTTPClient(
        execution=recorder,
        state_id=None,
        operation_id="operation-1",
        run_id="run-1",
        telemetry_emit=events.append,
        max_response_body_bytes=1024,
        **mock_audit_authority(),
    )
    body = b"x" * 1025 if outcome == "cap_error" else b'{"ok":true}'
    respx.post("https://provider.example/raw").mock(return_value=httpx.Response(503 if outcome == "provider_error" else 200, content=body))

    def broken_admission(response: HTTPCallResponse, body: bytes) -> bool:
        raise TypeError("admission programming bug")

    try:
        with pytest.raises(TypeError, match="admission programming bug"):
            client.post("https://provider.example/raw", response_admission=broken_admission)
        recorder.record_operation_call.assert_not_called()
        assert not events
    finally:
        client.close()


class _Stream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded: list[bytes] = []
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            self.yielded.append(chunk)
            yield chunk

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
@pytest.mark.parametrize("length_headers", [{}, {"content-length": "1"}, {"transfer-encoding": "chunked"}])
@respx.mock
def test_oversized_response_stops_before_parse_and_keeps_only_bounded_error_evidence(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, status: int, encoding: str, length_headers: dict[str, str]
) -> None:
    monkeypatch.setattr(openrouter, "_MAX_RESPONSE_BODY_BYTES", 1024)
    monkeypatch.setattr(gateway, "_MAX_RESPONSE_BODY_BYTES", 1024)
    monkeypatch.setattr(openrouter, "_MAX_ENCODED_RESPONSE_BYTES", 4096)
    monkeypatch.setattr(gateway, "_MAX_ENCODED_RESPONSE_BYTES", 4096)
    settlements: list[tuple[str, str]] = []
    harness.provider._llm_call_governance = LLMCallGovernance(
        before_call=lambda: "attempt-1", after_call=lambda attempt, call_id: settlements.append((attempt, call_id))
    )
    body = b"private-provider-body" * 1024
    encoded = gzip.compress(body) if encoding == "gzip" else zlib.compress(body) if encoding == "deflate" else body
    stream = _Stream([encoded[:1025], encoded[1025:]])
    respx.post(harness.url).mock(
        return_value=httpx.Response(
            status,
            headers={"content-type": "application/octet-stream", "content-encoding": encoding, **length_headers},
            stream=stream,
        )
    )
    with pytest.raises(LLMClientError) as failure:
        _query(harness, [ChatMessage(role="user", content="hello")])
    assert failure.value.retryable is False
    assert "private-provider-body" not in str(failure.value)
    assert stream.closed
    if encoding == "identity":
        assert stream.yielded == [encoded[:1025]]
    calls = harness.setup.factory.execution.get_operation_calls(harness.parent.operation_id)
    assert {call.call_type for call in calls} == {CallType.HTTP, CallType.LLM}
    assert all(call.status is CallStatus.ERROR for call in calls)
    transport_call = next(call for call in calls if call.call_type is CallType.HTTP)
    assert transport_call.response_ref is not None
    evidence = json.loads(harness.store.retrieve(transport_call.response_ref))
    assert evidence["body"]["_truncated"] is True
    assert evidence["body"]["_captured_body"] is False
    assert b"private-provider-body" not in harness.store.retrieve(transport_call.response_ref)
    assert harness.provider._http_clients == {}
    assert harness.provider._http_client_refs == {}
    semantic_call = next(call for call in calls if call.call_type is CallType.LLM)
    assert settlements == [("attempt-1", semantic_call.call_id)]


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
@respx.mock
def test_response_at_decoded_limit_succeeds(harness: _Harness, monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    monkeypatch.setattr(openrouter, "_MAX_RESPONSE_BODY_BYTES", 1024)
    monkeypatch.setattr(gateway, "_MAX_RESPONSE_BODY_BYTES", 1024)
    body = _response().content
    body += b" " * (1024 - len(body))
    encoded = gzip.compress(body) if encoding == "gzip" else zlib.compress(body) if encoding == "deflate" else body
    respx.post(harness.url).mock(
        return_value=httpx.Response(
            200,
            headers={
                "content-type": "application/json",
                "content-encoding": encoding,
                "X-ELSPETH-LLM-Gateway-Contract": "1",
            },
            stream=_Stream([encoded]),
        )
    )
    _query(harness, [ChatMessage(role="user", content="hello")])


@pytest.mark.parametrize("failure", ["encoded_limit", "unsupported_encoding", "invalid_gzip"])
@respx.mock
def test_encoding_failures_are_bounded_non_retryable_provider_errors(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(openrouter, "_MAX_ENCODED_RESPONSE_BYTES", 1024)
    monkeypatch.setattr(gateway, "_MAX_ENCODED_RESPONSE_BYTES", 1024)
    encoding = "br" if failure == "unsupported_encoding" else "gzip" if failure == "invalid_gzip" else "identity"
    body = b"private-provider-body" * 1024 if failure == "encoded_limit" else b"invalid compressed data"
    respx.post(harness.url).mock(return_value=httpx.Response(200, headers={"content-encoding": encoding}, stream=_Stream([body])))
    with pytest.raises(LLMClientError) as caught:
        _query(harness, [ChatMessage(role="user", content="hello")])
    assert not caught.value.retryable
    calls = harness.setup.factory.execution.get_operation_calls(harness.parent.operation_id)
    assert {call.call_type for call in calls} == {CallType.HTTP, CallType.LLM}
    assert all(call.status is CallStatus.ERROR for call in calls)
    http_call = next(call for call in calls if call.call_type is CallType.HTTP)
    assert http_call.response_ref is not None
    evidence = json.loads(harness.store.retrieve(http_call.response_ref))
    assert evidence["body"]["_truncated"] is True
    assert evidence["body"]["_captured_body"] is False


@respx.mock
def test_runtime_preflight_uses_bounded_transport(harness: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openrouter, "_MAX_RESPONSE_BODY_BYTES", 1024)
    monkeypatch.setattr(gateway, "_MAX_RESPONSE_BODY_BYTES", 1024)
    if isinstance(harness.provider, gateway.GatewayLLMProvider):
        route = respx.get(f"{harness.provider._readyz_base_url()}/readyz")
        expected_call_types = {CallType.HTTP}
    else:
        route = respx.post(harness.url)
        expected_call_types = {CallType.HTTP, CallType.LLM}
    route.mock(return_value=httpx.Response(200, stream=_Stream([b"x" * 1025, b"must-not-read"])))
    assert harness.parent.operation_id is not None
    with pytest.raises(LLMClientError) as caught:
        harness.provider.runtime_preflight(
            operation_id=harness.parent.operation_id,
            model="test-model",
            temperature=0.0,
            coordination_token=harness.setup.coordination_token,
        )
    assert not caught.value.retryable
    calls = harness.setup.factory.execution.get_operation_calls(harness.parent.operation_id)
    assert {call.call_type for call in calls} == expected_call_types
    assert all(call.status is CallStatus.ERROR for call in calls)


@pytest.mark.parametrize(
    ("mode", "echo_source"),
    [
        (RunMode.REPLAY, "wire"),
        (RunMode.VERIFY, "wire"),
        (RunMode.REPLAY, "archived_body"),
        (RunMode.REPLAY, "archived_header"),
        (RunMode.REPLAY, "archived_error"),
    ],
)
@pytest.mark.parametrize("echo", [False, True])
@pytest.mark.parametrize("separator", ["", "\n\t"])
@pytest.mark.parametrize("encoding_variant", ["standard", "urlsafe_unpadded", "hex", "byte_array", "percent"])
@respx.mock
def test_http_audit_projection_also_binds_replay_verify_and_telemetry(
    mode: RunMode, echo: bool, separator: str, echo_source: str, encoding_variant: str
) -> None:
    image = ImagePart.from_bytes(format="png", data=b"\x89PNG\r\n\x1a\nprivate-image-marker", blob_ref=None)
    messages = [ChatMessage(role="user", content=(image,))]
    wire = {"messages": wire_messages(messages)}
    audit = {"messages": audit_messages(messages)}
    encoded = base64.b64encode(image.data).decode()
    if encoding_variant == "urlsafe_unpadded":
        encoded = base64.urlsafe_b64encode(image.data).decode().rstrip("=")
    elif encoding_variant == "hex":
        encoded = image.data.hex()
    elif encoding_variant == "byte_array":
        encoded = json.dumps(list(image.data), separators=(",", ":"))
    elif encoding_variant == "percent":
        encoded = "".join(f"%{ord(char):02x}" for char in encoded)
    response_body = separator.join(encoded[index : index + 4] for index in range(0, len(encoded), 4)) if echo else "ok"
    if echo and encoding_variant == "byte_array":
        response_body = encoded.replace(",", "," + separator)
    wire_body = response_body if echo_source == "wire" else "ok"
    session = Mock(spec=CallModeSession)
    session.mode = mode
    session.admit_verify_call.return_value = "source-call"
    session.replay_call.return_value = ReplayCallEvidence(
        source_call_id="source-call",
        status=CallStatus.ERROR if echo_source == "archived_error" else CallStatus.SUCCESS,
        response_data={
            "status_code": 503 if echo_source == "archived_error" else 200,
            "headers": {"x-debug": response_body} if echo_source == "archived_header" else {},
            "body_size": len(wire_body),
            "body": response_body if echo_source in {"wire", "archived_body"} else "ok",
            "transport": {
                "body_b64": base64.b64encode(wire_body.encode()).decode(),
                "headers": [],
                "request_url": "https://example.com/raw",
            },
        },
        error_data={"type": "HTTPError", "message": response_body, "status_code": 503} if echo_source == "archived_error" else None,
        latency_ms=0.1,
    )
    recorder = Mock(spec=CallRecorder)
    recorder.allocate_operation_call_index.return_value = 0
    call = Mock(spec=Call)
    call.call_id = "current-call"
    recorder.record_operation_call.return_value = call
    events: list[TelemetryEvent] = []
    client = AuditedHTTPClient(
        execution=recorder,
        state_id=None,
        operation_id="operation-1",
        run_id="run-1",
        telemetry_emit=events.append,
        call_mode_session=session,
        **mock_audit_authority(),
    )
    route = respx.post("https://example.com/raw").mock(return_value=httpx.Response(200, text=response_body))
    try:
        if echo:
            error = AuditIntegrityError if mode is RunMode.REPLAY else HTTPResponseAuditRejectedError
            with pytest.raises(error):
                client.post(
                    "https://example.com/raw",
                    json=wire,
                    audit_json=audit,
                    response_admission=lambda response, body: image_response_is_audit_safe(messages, response, body=body),
                )
            assert response_body not in str(recorder.record_operation_call.call_args)
            assert response_body not in str(events)
            if mode is RunMode.REPLAY:
                assert not route.called
                recorder.record_operation_call.assert_not_called()
                assert not events
            else:
                verified = session.verify_call.call_args.kwargs
                assert verified["live_status"] is CallStatus.ERROR
                assert response_body not in json.dumps(verified["live_response_data"])
            return
        if mode is RunMode.REPLAY:
            with patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("unexpected network")):
                response = client.post("https://example.com/raw", json=wire, audit_json=audit)
            assert not route.called
            compared = session.replay_call.call_args.kwargs["request_data"]
        else:
            response = client.post("https://example.com/raw", json=wire, audit_json=audit)
            assert json.loads(route.calls[0].request.content) == wire
            compared = session.admit_verify_call.call_args.kwargs["request_data"]
            assert session.verify_call.call_args.kwargs["request_data"] == compared
        assert response.text == "ok"
        assert compared["json"] == audit
        retained = recorder.record_operation_call.call_args.kwargs["request_data"].to_dict()
        assert retained == compared
        assert len(events) == 1
        event = events[0]
        assert isinstance(event, ExternalCallCompleted)
        assert event.request_hash == stable_hash(compared)
        assert event.request_payload is not None
        assert event.request_payload.to_dict() == compared
        assert "data:image/" not in json.dumps(compared)
    finally:
        client.close()
