"""Power Automate's closed HTTP audit policy, independent of protocol parsing."""

import base64
import gzip
import logging
import traceback
from collections.abc import Iterator
from contextlib import closing

import httpx
import pytest
import respx

from elspeth.contracts.call_data import HTTPCallResponse, HTTPDecodedBodyEvidence
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.http_policy import HTTPAuditPolicy
from elspeth.core.canonical import canonical_json
from elspeth.core.security.web import SSRFSafeRequest
from elspeth.plugins.infrastructure.clients import http as http_module
from elspeth.plugins.infrastructure.clients.deadline import HTTPDeadline
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient, HTTPPolicyError
from tests.fixtures.mock_audit import mock_audit_authority
from tests.unit.plugins.clients.test_http import _CallRecorder, _ExecutionRepositoryFake


@pytest.fixture(autouse=True)
def _fingerprint_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Power Automate requires fingerprints even with the development switch."""
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-only-power-automate-policy-key")


def client(policy=HTTPAuditPolicy.POWER_AUTOMATE_V1, *, cap=20000, limiter=None):
    execution = _ExecutionRepositoryFake()
    telemetry = _CallRecorder()
    result = AuditedHTTPClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="state",
        run_id="run",
        telemetry_emit=telemetry,
        max_response_body_bytes=cap,
        audit_policy=policy,
        limiter=limiter,
    )
    return result, execution, telemetry


def request():
    return SSRFSafeRequest(
        original_url="https://example.com/flow?sig=fake-SAS-secret",
        resolved_ip="93.184.216.34",
        host_header="example.com",
        bare_hostname="example.com",
        scheme="https",
        port=443,
        path="/flow?sig=fake-SAS-secret",
    )


def captured(execution):
    return execution.record_call.call_args.kwargs["response_data"].to_dict()


def test_decoded_dto_omitted_by_default_and_checked():
    assert "decoded_body" not in HTTPCallResponse(200, {}).to_dict()
    evidence = HTTPDecodedBodyEvidence("e30=", 2, True)
    assert HTTPCallResponse(200, {}, decoded_body=evidence).to_dict()["decoded_body"] == {
        "body_b64": "e30=",
        "decoded_size": 2,
        "complete": True,
    }
    with pytest.raises(ValueError):
        HTTPDecodedBodyEvidence("e30=", 3, True)
    with pytest.raises(ValueError):
        HTTPDecodedBodyEvidence("e30=", 2, False)


@pytest.mark.parametrize(
    "body",
    [b' { "a": 1 }\n', b'{"a":1,"a":2}', b'{"a":NaN}', b"x" * 11000],
    ids=["whitespace", "duplicates", "nonfinite", "large-malformed"],
)
@respx.mock
def test_sas_and_filtered_headers_retain_decoded_body_without_transport(body):
    instance, execution, _ = client()
    respx.get(request().connection_url).mock(
        return_value=httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json", "set-cookie": "secret-cookie"},
        )
    )
    instance.request_ssrf_safe("GET", request())
    payload = captured(execution)
    assert "transport" not in payload
    assert payload["decoded_body"] == {
        "body_b64": base64.b64encode(body).decode("ascii"),
        "decoded_size": len(body),
        "complete": True,
    }
    assert "set-cookie" not in payload["headers"]
    if body != b' { "a": 1 }\n':
        assert payload["body"] == {"_json_parse_failed": True, "_error": "invalid_json"}


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        yield from self.chunks


@respx.mock
def test_compressed_capture_is_exact():
    body = b'  {"data": [1, 2]}\n'
    instance, execution, _ = client()
    respx.get(request().connection_url).mock(
        return_value=httpx.Response(
            200,
            stream=Chunks([gzip.compress(body)]),
            headers={"content-type": "application/json", "content-encoding": "gzip"},
        )
    )
    instance.request_ssrf_safe("GET", request())
    assert base64.b64decode(captured(execution)["decoded_body"]["body_b64"]) == body


@respx.mock
def test_body_cap_retains_bounded_incomplete_prefix():
    instance, execution, _ = client(cap=5)
    respx.get(request().connection_url).mock(return_value=httpx.Response(200, stream=Chunks([b"abc", b"defgh"])))
    with pytest.raises(HTTPPolicyError, match="response_too_large") as raised:
        instance.request_ssrf_safe("GET", request())
    assert raised.value.__context__ is None
    evidence = captured(execution)["decoded_body"]
    assert base64.b64decode(evidence["body_b64"]) == b"abcde"
    assert evidence["complete"] is False
    assert evidence["incomplete_reason"] == "response_too_large"
    assert execution.record_call.call_count == 1


@respx.mock
def test_transport_exception_is_sanitized_before_audit():
    secret = "fake-non-url-cursor-secret"
    instance, execution, telemetry = client()
    route = respx.get(request().connection_url).mock(side_effect=httpx.ConnectError(secret))
    with pytest.raises(HTTPPolicyError, match="transport_failed") as raised:
        instance.request_ssrf_safe("GET", request())
    assert route.call_count == 1
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert secret not in "".join(traceback.format_exception(raised.value))
    assert secret not in repr(execution.record_call.call_args.kwargs)
    assert secret not in repr(telemetry.call_args.args)
    assert execution.record_call.call_count == 1


@respx.mock
def test_generic_client_policy_is_unchanged():
    instance, execution, _ = client(HTTPAuditPolicy.GENERIC)
    respx.get(request().connection_url).mock(side_effect=httpx.ConnectError("generic-detail"))
    with pytest.raises(httpx.ConnectError, match="generic-detail"):
        instance.request_ssrf_safe("GET", request())
    assert execution.record_call.call_args.kwargs["error"].message == "generic-detail"


@respx.mock
def test_guard_runs_after_limiter_and_prevents_dispatch():
    order = []

    class Limiter:
        def acquire(self):
            order.append("limiter")

    instance, execution, _ = client(limiter=Limiter())
    route = respx.get(request().connection_url).mock(return_value=httpx.Response(200))

    def guard():
        order.append("guard")
        raise PermissionError("fake-guard-secret")

    with pytest.raises(HTTPPolicyError, match="authority_refused"):
        instance.request_ssrf_safe("GET", request(), before_send=guard)
    assert order == ["limiter", "guard"]
    assert route.call_count == 0
    assert execution.record_call.call_count == 1


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@respx.mock
@pytest.mark.parametrize("step, fails", [(0.2, False), (0.4, True)])
def test_slow_trickle_obeys_total_deadline(monkeypatch, step, fails):
    clock = Clock()
    instance, execution, _ = client()
    instance._timeout = 1.0
    monkeypatch.setattr(http_module.time, "monotonic", clock)

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for part in [b"a", b"b", b"c"]:
                clock.now += step
                yield part

    respx.get(request().connection_url).mock(return_value=httpx.Response(200, stream=Trickle()))
    if fails:
        with pytest.raises(HTTPPolicyError, match="deadline_exceeded"):
            instance.request_ssrf_safe("GET", request())
        evidence = captured(execution)["decoded_body"]
        assert evidence["complete"] is False
        assert evidence["incomplete_reason"] == "deadline_exceeded"
        assert base64.b64decode(evidence["body_b64"]) == b"ab"
    else:
        instance.request_ssrf_safe("GET", request())
        assert captured(execution)["decoded_body"]["complete"] is True
    assert execution.record_call.call_count == 1


@respx.mock
def test_invalid_encoding_and_partial_read_keep_safe_evidence():
    instance, execution, _ = client()

    class Broken(httpx.SyncByteStream):
        def __iter__(self):
            yield b"prefix"
            raise httpx.ReadError("fake-non-url-cursor-secret")

    respx.get(request().connection_url).mock(return_value=httpx.Response(200, stream=Broken()))
    with pytest.raises(HTTPPolicyError, match="transport_failed") as raised:
        instance.request_ssrf_safe("GET", request())
    assert raised.value.__context__ is None
    evidence = captured(execution)["decoded_body"]
    assert base64.b64decode(evidence["body_b64"]) == b"prefix"
    assert evidence["complete"] is False


@respx.mock
@pytest.mark.parametrize(
    "body, code",
    [(b"[" * 65 + b"0" + b"]" * 65, "depth_exceeded"), (b'{"a":1e999}', "invalid_json"), (b'{"a":"\xff"}', "invalid_encoding")],
)
def test_json_diagnostics_are_closed(body, code):
    instance, execution, _ = client()
    respx.get(request().connection_url).mock(return_value=httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    instance.request_ssrf_safe("GET", request())
    assert captured(execution)["body"] == {"_json_parse_failed": True, "_error": code}
    assert captured(execution)["decoded_body"]["complete"] is True


@respx.mock
def test_configured_debug_logging_keeps_http_library_urls_quiet(caplog):
    from elspeth.core.logging import _NOISY_LOGGERS, configure_logging

    levels = {name: logging.getLogger(name).level for name in _NOISY_LOGGERS}
    try:
        configure_logging(level="DEBUG")
        assert logging.getLogger("httpx").isEnabledFor(logging.INFO) is False
        assert logging.getLogger("httpcore").isEnabledFor(logging.DEBUG) is False
        caplog.clear()
        instance, _, _ = client()
        respx.get(request().connection_url).mock(return_value=httpx.Response(200, content=b"{}"))
        instance.request_ssrf_safe("GET", request())
        logging.getLogger("elspeth.http-policy-control").warning("capture_positive_control")
        assert "capture_positive_control" in caplog.text
        assert "fake-SAS-secret" not in caplog.text
    finally:
        for name, level in levels.items():
            logging.getLogger(name).setLevel(level)


@respx.mock
def test_development_switch_preserves_pa_fingerprint_identity(monkeypatch):
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    instance, execution, _ = client()
    respx.get(request().connection_url).mock(
        return_value=httpx.Response(
            200,
            content=b"{}",
            headers={"location": "https://example.com/?sig=fake-response-SAS"},
        )
    )
    instance.request_ssrf_safe("GET", request(), headers={"Authorization": "Bearer fake-token"})
    payload = execution.record_call.call_args.kwargs["request_data"].to_dict()
    assert "%3Cfingerprint%3A" in payload["url"]
    assert payload["headers"]["Authorization"].startswith("<fingerprint:")
    assert "%3Cfingerprint%3A" in captured(execution)["headers"]["location"]


@respx.mock
def test_parse_work_is_inside_total_deadline(monkeypatch):
    clock = Clock()
    instance, execution, _ = client()
    instance._timeout = 1.0
    monkeypatch.setattr(http_module.time, "monotonic", clock)
    original = instance._parse_response_body

    def expensive_parse(response, url):
        clock.now += 2.0
        return original(response, url)

    monkeypatch.setattr(instance, "_parse_response_body", expensive_parse)
    respx.get(request().connection_url).mock(return_value=httpx.Response(200, content=b"{}"))
    with pytest.raises(HTTPPolicyError, match="deadline_exceeded"):
        instance.request_ssrf_safe("GET", request())
    assert captured(execution)["decoded_body"]["complete"] is False
    assert captured(execution)["decoded_body"]["incomplete_reason"] == "deadline_exceeded"


@respx.mock
def test_final_guard_runs_after_transport_preparation(monkeypatch):
    order = []
    original = http_module.DeadlineHTTPTransport

    class PreparedTransport(original):
        def __init__(self, deadline):
            order.append("transport")
            super().__init__(deadline)

    monkeypatch.setattr(http_module, "DeadlineHTTPTransport", PreparedTransport)
    instance, execution, _ = client()

    def guard():
        order.append("guard")
        raise PermissionError("refused")

    with pytest.raises(HTTPPolicyError, match="authority_refused"):
        instance.request_ssrf_safe("GET", request(), before_send=guard)
    assert order == ["transport", "guard"]
    assert execution.record_call.call_count == 1


@pytest.mark.parametrize("step, fails", [(0.1, False), (0.6, True)])
def test_decode_work_obeys_total_budget(monkeypatch, step, fails):
    clock = Clock()
    original = http_module.zlib.decompressobj

    class TimedDecoder:
        def __init__(self, *args):
            self.decoder = original(*args)

        def decompress(self, data, size):
            clock.now += step
            return self.decoder.decompress(data, size)

        def flush(self, size):
            clock.now += step
            return self.decoder.flush(size)

        @property
        def eof(self):
            return self.decoder.eof

        @property
        def unused_data(self):
            return self.decoder.unused_data

        @property
        def unconsumed_tail(self):
            return self.decoder.unconsumed_tail

    monkeypatch.setattr(http_module.zlib, "decompressobj", TimedDecoder)
    instance, _, _ = client(cap=200000)
    body = b"a" * 131072
    response = httpx.Response(
        200,
        stream=Chunks([gzip.compress(body)]),
        headers={"content-encoding": "gzip"},
        request=httpx.Request("GET", "https://93.184.216.34/"),
    )
    deadline = HTTPDeadline(1.0, clock)
    with closing(response):
        if fails:
            with pytest.raises(HTTPPolicyError, match="deadline_exceeded") as raised:
                instance._consume_policy_response(response, deadline=deadline)
            evidence = raised.value.response_payload.decoded_body
            assert evidence.complete is False
            assert 0 < evidence.decoded_size <= len(body)
        else:
            result = instance._consume_policy_response(response, deadline=deadline)
            assert result.content == body
            assert result.extensions["elspeth_decoded_body"].complete is True


@respx.mock
def test_owned_parse_bug_propagates_with_one_failed_audit_and_capture(monkeypatch):
    instance, execution, _ = client()
    failure = FrameworkBugError("owned parser bug")

    def broken_parse(response, url):
        raise failure

    monkeypatch.setattr(instance, "_parse_response_body", broken_parse)
    respx.get(request().connection_url).mock(return_value=httpx.Response(200, content=b"{}"))
    with pytest.raises(FrameworkBugError) as raised:
        instance.request_ssrf_safe("GET", request())
    assert raised.value is failure
    assert execution.record_call.call_count == 1
    assert captured(execution)["decoded_body"]["complete"] is True


@pytest.mark.parametrize("proxy", [None, "unsupported://fake-proxy-sentinel"], ids=["no-proxy", "ambient-proxy"])
@respx.mock
def test_power_automate_ignores_ambient_proxy(monkeypatch, proxy):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    instance, execution, _ = client()
    if proxy is not None:
        monkeypatch.setenv("HTTPS_PROXY", proxy)
    route = respx.get(request().connection_url).mock(return_value=httpx.Response(200, content=b"{}"))
    response, _, _ = instance.request_ssrf_safe("GET", request())
    assert response.status_code == 200
    assert route.call_count == 1
    assert execution.record_call.call_count == 1
    assert captured(execution)["decoded_body"]["complete"] is True


def test_generic_policy_still_consumes_ambient_proxy(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    instance, execution, _ = client(HTTPAuditPolicy.GENERIC)
    monkeypatch.setenv("HTTPS_PROXY", "unsupported://fake-proxy-sentinel")
    with respx.mock(assert_all_called=False) as routes:
        route = routes.get(request().connection_url).mock(return_value=httpx.Response(200, content=b"{}"))
        with pytest.raises(ValueError, match="Unknown scheme"):
            instance.request_ssrf_safe("GET", request())
        assert route.call_count == 0
    assert execution.record_call.call_count == 1
    assert "unsupported" in execution.record_call.call_args.kwargs["error"].message


@pytest.mark.parametrize(
    "body",
    [b'{"rows":[9007199254740992,{"record_id":"A"}]}', b'{"rows":["\\ud800",{"record_id":"A"}]}'],
    ids=["unsafe-integer", "lone-surrogate"],
)
@pytest.mark.parametrize("media_type", ["application/json", "APPLICATION/JSON", "Application/Json ; charset=UTF-8"])
@respx.mock
def test_uncanonical_json_is_refused_before_audit_with_complete_capture(body, media_type):
    instance, execution, telemetry = client()
    respx.get(request().connection_url).mock(return_value=httpx.Response(200, content=body, headers={"content-type": media_type}))
    with pytest.raises(HTTPPolicyError, match="invalid_json") as raised:
        instance.request_ssrf_safe("GET", request())
    assert execution.record_call.call_count == 1
    assert telemetry.call_count == 1
    payload = captured(execution)
    assert payload["body"] is None
    assert payload["decoded_body"]["complete"] is True
    assert base64.b64decode(payload["decoded_body"]["body_b64"]) == body
    assert execution.record_call.call_args.kwargs["error"].message == "invalid_json"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "9007199254740992" not in "".join(traceback.format_exception(raised.value))
    canonical_json(payload)


@pytest.mark.parametrize("media_type", ["application/json", "APPLICATION/JSON", "Application/Json ; charset=UTF-8"])
@respx.mock
def test_canonical_pa_json_positive_control(media_type):
    instance, execution, _ = client()
    body = b'{"rows":[42,{"record_id":"A"}]}'
    respx.get(request().connection_url).mock(return_value=httpx.Response(200, content=body, headers={"content-type": media_type}))
    instance.request_ssrf_safe("GET", request())
    assert captured(execution)["body"] == {"rows": [42, {"record_id": "A"}]}
    canonical_json(captured(execution))


@respx.mock
def test_generic_unsafe_integer_parsing_is_unchanged():
    instance, execution, _ = client(HTTPAuditPolicy.GENERIC)
    respx.get(request().connection_url).mock(
        return_value=httpx.Response(200, content=b'{"rows":[9007199254740992]}', headers={"content-type": "application/json"})
    )
    instance.request_ssrf_safe("GET", request())
    assert captured(execution)["body"] == {"rows": [9007199254740992]}


@pytest.mark.parametrize("media_type", ["application/json-extra", "other/application/json"])
@respx.mock
def test_pa_json_media_selection_is_exact(media_type):
    instance, execution, _ = client()
    body = b'{"rows":[9007199254740992]}'
    respx.get(request().connection_url).mock(return_value=httpx.Response(200, content=body, headers={"content-type": media_type}))
    instance.request_ssrf_safe("GET", request())
    assert captured(execution)["body"] == {"_binary": base64.b64encode(body).decode("ascii")}


@respx.mock
def test_generic_uppercase_json_media_keeps_legacy_binary_body():
    instance, execution, _ = client(HTTPAuditPolicy.GENERIC)
    body = b'{"rows":[9007199254740992]}'
    respx.get(request().connection_url).mock(return_value=httpx.Response(200, content=body, headers={"content-type": "APPLICATION/JSON"}))
    instance.request_ssrf_safe("GET", request())
    assert captured(execution)["body"] == {"_binary": base64.b64encode(body).decode("ascii")}
