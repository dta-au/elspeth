"""Endpoint-fixed Power Automate calls use the ordinary audited HTTP path."""

import io
import itertools
import json
import logging
import threading
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from unittest.mock import create_autospec

import httpx
import pytest
import respx
from azure.core.credentials import AccessToken
from pydantic import BaseModel, ConfigDict

from elspeth.contracts.audit import Call
from elspeth.contracts.audit_protocols import CallRecorder
from elspeth.contracts.call_mode import CallModeSession
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.security import secret_fingerprint
from elspeth.contracts.sink_effect_http import SinkEffectHTTPBindContext, SinkEffectHTTPPostRequest
from elspeth.core.canonical import stable_hash
from elspeth.plugins.infrastructure.clients.power_automate import (
    PowerAutomateClientError,
    PowerAutomateHTTPPostFactory,
    PowerAutomateOperationClient,
)
from elspeth.plugins.infrastructure.power_automate import PowerAutomateSinkConfig, PowerAutomateSourceConfig
from elspeth.plugins.infrastructure.power_automate_nonlive import (
    ArchivedPowerAutomateSourceConfig,
    DeferredPowerAutomateCredential,
)

URL = "https://flows.example.org/encoded%2Fpath?sig=fake-SAS-capability"
ORIGIN = "https://flows.example.org"
IP = "93.184.216.34"
BODY = {"protocol": "elspeth.power-automate.v1", "operation": "read", "query": {"value": "café"}}


def options(method="sas_url"):
    auth = {"method": method}
    result = {"auth": auth, "allowed_origin": ORIGIN, "schema": {"mode": "observed"}, "on_validation_failure": "discard"}
    if method == "sas_url":
        auth["trigger_url_secret"] = URL
    else:
        result["trigger_url"] = "https://flows.example.org/read"
        auth["client_id"] = "fake-user-client"
        if method == "service_principal":
            auth.update(tenant_id="fake-tenant", client_secret="fake-client-secret")
    return result


@pytest.fixture(autouse=True)
def fingerprint_key(monkeypatch):
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "fake-test-fingerprint-key")


def recorder():
    result = create_autospec(CallRecorder, instance=True)
    indices = itertools.count()
    result.allocate_operation_call_index.side_effect = lambda *args, **kwargs: next(indices)

    def record(**kwargs):
        request = kwargs["request_data"].to_dict()
        response = kwargs["response_data"].to_dict() if kwargs["response_data"] is not None else None
        return Call(
            call_id=f"actual-call-{kwargs['call_index']}",
            call_index=kwargs["call_index"],
            call_type=kwargs["call_type"],
            status=kwargs["status"],
            request_hash=stable_hash(request),
            request_ref=stable_hash(request),
            response_ref=stable_hash(response) if response is not None else None,
            operation_id=kwargs["operation_id"],
            created_at=datetime.now(UTC),
        )

    result.record_operation_call.side_effect = record
    return result


def client(config=None, *, audit=None, guard=None, registry=None, session=None):
    return PowerAutomateOperationClient(
        config or PowerAutomateSourceConfig.from_dict(options()),
        recorder=audit or recorder(),
        run_id="run",
        operation_id="source-load",
        coordination_token=CoordinationToken("run", "worker", 1),
        telemetry_emit=lambda event: None,
        before_send=guard or (lambda: None),
        rate_limit_registry=registry,
        call_mode_session=session,
    )


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr("elspeth.core.security.web._resolve_hostname", lambda *args, **kwargs: [IP])


@respx.mock
def test_source_calls_have_operation_refs_and_original_url(public_dns):
    audit = recorder()
    route = respx.post(f"https://{IP}:443/encoded%2Fpath?sig=fake-SAS-capability").mock(return_value=httpx.Response(200, json={"ok": True}))
    guards = []
    with client(audit=audit, guard=lambda: guards.append("guard")) as transport:
        response = transport.post_json(BODY)
    assert response.call_id == "actual-call-0"
    assert len(response.request_ref) == len(response.response_ref) == 64
    assert guards == ["guard", "guard", "guard"]
    assert route.call_count == 1
    assert json.loads(route.calls[0].request.content) == BODY
    assert route.calls[0].request.url.raw_path == b"/encoded%2Fpath?sig=fake-SAS-capability"
    assert route.calls[0].request.headers["host"] == "flows.example.org"
    captured = audit.record_operation_call.call_args.kwargs
    assert captured["operation_id"] == "source-load"
    assert "fake-SAS-capability" not in repr(captured)


@respx.mock
@pytest.mark.parametrize(
    "status,media,code",
    [(202, "application/json", "http_status"), (302, "application/json", "http_status"), (200, "text/plain", "media_type")],
)
def test_status_media_and_redirect_are_refused(public_dns, status, media, code):
    route = respx.post(f"https://{IP}:443/encoded%2Fpath?sig=fake-SAS-capability").mock(
        return_value=httpx.Response(status, headers={"content-type": media, "location": "https://flows.example.org/next"}, content=b"{}")
    )
    with client() as transport, pytest.raises(PowerAutomateClientError, match=code):
        transport.post_json(BODY)
    assert route.call_count == 1


def test_constructor_and_factory_bind_perform_no_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("construction performed I/O")

    config = PowerAutomateSinkConfig.from_dict(
        {
            "auth": {"method": "managed_identity", "client_id": "fake-user-client"},
            "trigger_url": "https://flows.example.org/write",
            "allowed_origin": ORIGIN,
            "fields": ["id"],
            "schema": {"mode": "flexible", "fields": ["id: str"]},
        }
    )
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", forbidden)
    monkeypatch.setattr("elspeth.core.security.web._resolve_hostname", forbidden)
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY")
    safe_config = {"auth": {"method": "managed_identity", "client_id": "fake-user-client"}}
    factory = PowerAutomateHTTPPostFactory(config, safe_config=safe_config)
    capability = factory.bind(
        SinkEffectHTTPBindContext(recorder(), "run", "member-status", CoordinationToken("run", "worker", 1), lambda e: None, lambda: None)
    )
    assert capability is not None
    assert factory.safe_config_fingerprint == stable_hash(safe_config)


@respx.mock
@pytest.mark.parametrize("method", ["managed_identity", "service_principal"])
def test_oauth_is_lazy_uses_fixed_scope_and_closes(public_dns, monkeypatch, method):
    events = []
    tokens = itertools.count()

    class Credential:
        def __init__(self, **kwargs):
            events.append(("construct", kwargs))

        def get_token(self, scope):
            events.append(("token", scope))
            return AccessToken(f"fake-renewed-bearer-{next(tokens)}", 9999999999)

        def close(self):
            events.append(("close",))

    constructor = "ManagedIdentityCredential" if method == "managed_identity" else "ClientSecretCredential"
    monkeypatch.setattr(f"azure.identity.{constructor}", Credential)
    route = respx.post(f"https://{IP}:443/read").mock(return_value=httpx.Response(200, json={"ok": True}))
    audit = recorder()
    with client(PowerAutomateSourceConfig.from_dict(options(method)), audit=audit) as transport:
        assert events == []
        transport.post_json(BODY)
        transport.post_json(BODY)
    assert route.call_count == 2
    assert [e for e in events if e[0] == "token"] == [("token", "https://service.flow.microsoft.com/.default")] * 2
    assert events[0][1]["client_id"] == "fake-user-client"
    assert events[-1] == ("close",)
    assert route.calls[0].request.headers["authorization"] == "Bearer fake-renewed-bearer-0"
    assert route.calls[1].request.headers["authorization"] == "Bearer fake-renewed-bearer-1"
    assert "fake-renewed-bearer" not in repr(audit.record_operation_call.call_args_list)
    assert "fake-client-secret" not in repr(audit.record_operation_call.call_args_list)


def test_private_ip_precedes_auth(monkeypatch):
    touched = []
    monkeypatch.setattr("elspeth.core.security.web._resolve_hostname", lambda *a, **k: ["127.0.0.1"])
    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", lambda **kwargs: touched.append(kwargs))
    with (
        client(PowerAutomateSourceConfig.from_dict(options("managed_identity"))) as transport,
        pytest.raises(PowerAutomateClientError, match="authority_refused"),
    ):
        transport.post_json(BODY)
    assert touched == []


@respx.mock
def test_sdk_error_has_no_value_bearing_context(public_dns, monkeypatch):
    secret = "fake-exception-client-secret"

    class Credential:
        def __init__(self, **kwargs):
            raise RuntimeError(secret)

    monkeypatch.setattr("azure.identity.ClientSecretCredential", Credential)
    with (
        client(PowerAutomateSourceConfig.from_dict(options("service_principal"))) as transport,
        pytest.raises(PowerAutomateClientError, match="authentication_failed") as raised,
    ):
        transport.post_json(BODY)
    assert raised.value.__context__ is None
    assert secret not in "".join(traceback.format_exception(raised.value))


def test_actual_serialized_body_limit_precedes_dns(monkeypatch):
    monkeypatch.setattr("elspeth.core.security.web._resolve_hostname", lambda *a, **k: pytest.fail("DNS before body admission"))
    config = PowerAutomateSourceConfig.from_dict({**options(), "max_request_body_bytes": 10})
    with client(config) as transport, pytest.raises(PowerAutomateClientError, match="request_too_large"):
        transport.post_json(BODY)


def test_fingerprint_key_required_even_in_dev_mode(monkeypatch):
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY")
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    with client() as transport, pytest.raises(PowerAutomateClientError, match="fingerprint_key_required"):
        transport.post_json(BODY)


def test_revoked_guard_precedes_dns_and_sdk(monkeypatch):
    def guard():
        raise FrameworkBugError("attempt revoked")

    monkeypatch.setattr("elspeth.core.security.web._resolve_hostname", lambda *a, **k: pytest.fail("DNS under revoked authority"))
    with client(guard=guard) as transport, pytest.raises(FrameworkBugError, match="attempt revoked"):
        transport.post_json(BODY)


def test_verify_oauth_preflight_precedes_dns_and_sdk(monkeypatch):
    session = create_autospec(CallModeSession, instance=True)
    session.mode = RunMode.VERIFY
    session.preflight_verify_http_managed_identity.side_effect = FrameworkBugError("preflight refusal")
    monkeypatch.setattr("elspeth.core.security.web._resolve_hostname", lambda *a, **k: pytest.fail("DNS before verification admission"))
    with (
        client(PowerAutomateSourceConfig.from_dict(options("managed_identity")), session=session) as transport,
        pytest.raises(FrameworkBugError, match="preflight refusal"),
    ):
        transport.post_json(BODY)
    session.preflight_verify_http_managed_identity.assert_called_once()


def sink_config():
    return PowerAutomateSinkConfig.from_dict(
        {
            "auth": {"method": "sas_url", "trigger_url_secret": URL},
            "allowed_origin": ORIGIN,
            "fields": ["id"],
            "schema": {"mode": "flexible", "fields": ["id: str"]},
        }
    )


@respx.mock
def test_bound_sink_capability_has_exact_audited_refs(public_dns):
    audit = recorder()
    factory = PowerAutomateHTTPPostFactory(sink_config(), safe_config={"allowed_origin": ORIGIN})
    capability = factory.bind(
        SinkEffectHTTPBindContext(
            audit,
            "run",
            "member-status",
            CoordinationToken("run", "worker", 1),
            lambda event: None,
            lambda: None,
        )
    )
    route = respx.post(f"https://{IP}:443/encoded%2Fpath?sig=fake-SAS-capability").mock(return_value=httpx.Response(200, json={"ok": True}))
    result = capability.post_json(SinkEffectHTTPPostRequest({"operation": "status"}))
    captured = audit.record_operation_call.call_args.kwargs
    assert captured["operation_id"] == "member-status"
    assert result.call_id == "actual-call-0"
    assert result.request_ref == stable_hash(captured["request_data"].to_dict())
    assert result.response_ref == stable_hash(captured["response_data"].to_dict())
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("revoke", [False, True], ids=["current-authority", "authority-lost-during-wait"])
def test_limiter_wait_cannot_send_under_revoked_authority(public_dns, revoke):
    active = True
    events = []

    class Limiter:
        def acquire(self):
            nonlocal active
            events.append("wait")
            active = not revoke

    class Registry:
        def get_limiter(self, name):
            assert name == "power_automate"
            return Limiter()

    def guard():
        events.append("guard")
        if not active:
            raise FrameworkBugError("attempt revoked")

    route = respx.post(f"https://{IP}:443/encoded%2Fpath?sig=fake-SAS-capability").mock(return_value=httpx.Response(200, json={"ok": True}))
    with client(guard=guard, registry=Registry()) as transport:
        if revoke:
            with pytest.raises(FrameworkBugError, match="attempt revoked"):
                transport.post_json(BODY)
        else:
            transport.post_json(BODY)
    assert route.call_count == (0 if revoke else 1)
    assert events[:3] == ["guard", "wait", "guard"]


@respx.mock
def test_post_return_fence_failure_does_not_return_receipt(public_dns):
    guards = 0

    def guard():
        nonlocal guards
        guards += 1
        if guards == 3:
            raise FrameworkBugError("post-send fence lost")

    audit = recorder()
    route = respx.post(f"https://{IP}:443/encoded%2Fpath?sig=fake-SAS-capability").mock(return_value=httpx.Response(200, json={"ok": True}))
    with client(audit=audit, guard=guard) as transport, pytest.raises(FrameworkBugError, match="post-send fence lost"):
        transport.post_json(BODY)
    assert route.call_count == 1
    assert audit.record_operation_call.call_count == 1


@respx.mock
def test_audit_failure_after_remote_response_never_returns_receipt(public_dns):
    audit = recorder()
    audit.record_operation_call.side_effect = AuditIntegrityError("payload-store write failed")
    route = respx.post(f"https://{IP}:443/encoded%2Fpath?sig=fake-SAS-capability").mock(return_value=httpx.Response(200, json={"ok": True}))
    with client(audit=audit) as transport, pytest.raises(AuditIntegrityError, match="payload-store write failed"):
        transport.post_json(BODY)
    assert route.call_count == 1


@respx.mock
def test_global_raw_secret_switch_keeps_power_automate_fingerprints(public_dns, monkeypatch):
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "true")
    audit = recorder()
    respx.post(f"https://{IP}:443/encoded%2Fpath?sig=fake-SAS-capability").mock(return_value=httpx.Response(200, json={"ok": True}))
    with client(audit=audit) as transport:
        transport.post_json(BODY)
    recorded = audit.record_operation_call.call_args.kwargs["request_data"].to_dict()
    assert "fingerprint" in recorded["url"]
    assert "fake-SAS-capability" not in repr(recorded)


def archived_options(method):
    raw = options(method)
    auth = raw["auth"]
    if method == "sas_url":
        del auth["trigger_url_secret"]
        auth["trigger_url_secret_fingerprint"] = secret_fingerprint(URL)
    elif method == "service_principal":
        del auth["client_secret"]
        auth["client_secret_fingerprint"] = secret_fingerprint("fake-client-secret")
    return raw


def test_archived_oauth_admission_precedes_deferred_resolution(monkeypatch):
    session = create_autospec(CallModeSession, instance=True)
    session.mode = RunMode.VERIFY
    session.preflight_verify_http_managed_identity.side_effect = AuditIntegrityError("preflight refused")
    monkeypatch.setattr(DeferredPowerAutomateCredential, "resolve", lambda self: pytest.fail("credential before archive admission"))
    config = ArchivedPowerAutomateSourceConfig.model_validate(archived_options("service_principal"))
    with (
        PowerAutomateOperationClient(
            config,
            recorder=recorder(),
            run_id="run",
            operation_id="source-load",
            coordination_token=CoordinationToken("run", "worker", 1),
            telemetry_emit=lambda event: None,
            before_send=lambda: None,
            call_mode_session=session,
            credential=DeferredPowerAutomateCredential("FAKE_SECRET", secret_fingerprint("fake-client-secret")),
        ) as transport,
        pytest.raises(AuditIntegrityError, match="preflight refused"),
    ):
        transport.post_json(BODY)


def test_archived_sas_rotation_refused_before_dns(monkeypatch):
    session = create_autospec(CallModeSession, instance=True)
    session.mode = RunMode.VERIFY
    monkeypatch.setenv("FAKE_SECRET", "https://different.example.org/secret?sig=fake-new-secret")
    monkeypatch.setattr("elspeth.core.security.web._resolve_hostname", lambda *a: pytest.fail("DNS before credential identity"))
    config = ArchivedPowerAutomateSourceConfig.model_validate(archived_options("sas_url"))
    with (
        PowerAutomateOperationClient(
            config,
            recorder=recorder(),
            run_id="run",
            operation_id="source-load",
            coordination_token=CoordinationToken("run", "worker", 1),
            telemetry_emit=lambda event: None,
            before_send=lambda: None,
            call_mode_session=session,
            credential=DeferredPowerAutomateCredential("FAKE_SECRET", secret_fingerprint(URL)),
        ) as transport,
        pytest.raises(PowerAutomateClientError, match="authentication_failed") as raised,
    ):
        transport.post_json(BODY)
    assert raised.value.__context__ is None
    session.preflight_verify_http_request.assert_not_called()


def test_verify_sink_transport_is_refused_without_io():
    session = create_autospec(CallModeSession, instance=True)
    session.mode = RunMode.VERIFY
    with pytest.raises(AuditIntegrityError, match="live-only"):
        client(sink_config(), session=session)


def test_closed_client_refuses_all_operations(public_dns):
    transport = client()
    transport.close()
    transport.close()
    with pytest.raises(PowerAutomateClientError, match="client_closed"):
        transport.post_json(BODY)


@respx.mock
@pytest.mark.parametrize("below_limit", [False, True], ids=["exact-wire-byte-cap", "one-byte-over"])
def test_request_bound_measures_httpx_wire_serialization(public_dns, below_limit):
    size = len(httpx.Request("POST", "https://serialization.invalid", json=BODY).content)
    config = PowerAutomateSourceConfig.from_dict({**options(), "max_request_body_bytes": size - int(below_limit)})
    route = respx.post(f"https://{IP}:443/encoded%2Fpath?sig=fake-SAS-capability").mock(return_value=httpx.Response(200, json={"ok": True}))
    with client(config) as transport:
        if below_limit:
            with pytest.raises(PowerAutomateClientError, match="request_too_large"):
                transport.post_json(BODY)
        else:
            transport.post_json(BODY)
    assert route.call_count == (0 if below_limit else 1)


@pytest.mark.parametrize("method", ["service_principal", "managed_identity"])
@pytest.mark.parametrize("json_output", [False, True], ids=["console", "json"])
def test_real_azure_sdk_diagnostics_are_sanitized_before_factories_and_handlers(monkeypatch, caplog, request, method, json_output):
    """Keep the real SDK decorators that emitted the independently found leak."""
    from azure.identity import ClientSecretCredential
    from azure.identity._credentials.imds import ImdsCredential

    from elspeth.core.logging import configure_logging

    secret = "fake-private-sdk-secret-sentinel"  # secret-scan: allow-this-line
    seen = []
    original_factory = logging.getLogRecordFactory()

    def inspecting_factory(name, level, pathname, lineno, msg, args, exc_info, func=None, sinfo=None, **kwargs):
        seen.append((name, msg, args, exc_info, sinfo, kwargs))
        return original_factory(name, level, pathname, lineno, msg, args, exc_info, func, sinfo, **kwargs)

    logging.setLogRecordFactory(inspecting_factory)
    request.addfinalizer(lambda: logging.setLogRecordFactory(original_factory))
    credential_type = ClientSecretCredential if method == "service_principal" else ImdsCredential
    if method == "service_principal":
        monkeypatch.setattr(credential_type, "_acquire_token_silently", lambda *args, **kwargs: None)

    def fail_request(*args, **kwargs):
        for namespace in ("msal", "msal_extensions", "requests", "urllib3", "httpx", "httpcore"):
            logging.getLogger(namespace + ".pa-test").error("SDK transport detail %s", secret, stack_info=True)
        raise RuntimeError(secret)

    monkeypatch.setattr(credential_type, "_request_token", fail_request)
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        configure_logging(level="WARNING", color_output=False, json_output=json_output)
        with (
            caplog.at_level(logging.WARNING),
            client(PowerAutomateSourceConfig.from_dict(options(method))) as transport,
            pytest.raises(PowerAutomateClientError, match="authentication_failed") as raised,
        ):
            transport._bearer()
    assert raised.value.__context__ is None
    assert secret not in caplog.text
    assert secret not in repr(seen)
    assert secret not in stdout.getvalue()
    assert secret not in stderr.getvalue()
    assert "Logging error" not in stderr.getvalue()
    assert "power_automate_auth_vendor_diagnostic" in stdout.getvalue()
    assert "power_automate_auth_vendor_diagnostic" in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    logging.getLogger("azure.identity.unrelated").warning("unscoped SDK detail %s", secret)
    assert secret in caplog.text


def test_scoped_sdk_policy_preserves_concurrent_non_power_automate_diagnostics(monkeypatch, caplog):
    from azure.identity import ClientSecretCredential

    pa_secret = "fake-pa-sdk-warning-secret"
    other_secret = "fake-unrelated-sdk-warning-secret"  # secret-scan: allow-this-line
    entered = threading.Event()
    release = threading.Event()
    errors = []
    monkeypatch.setattr(ClientSecretCredential, "_acquire_token_silently", lambda *args, **kwargs: None)

    def fail_request(*args, **kwargs):
        if threading.current_thread().name == "pa-auth-test":
            entered.set()
            assert release.wait(5)
            raise RuntimeError(pa_secret)
        raise RuntimeError(other_secret)

    monkeypatch.setattr(ClientSecretCredential, "_request_token", fail_request)

    def pa_request():
        with client(PowerAutomateSourceConfig.from_dict(options("service_principal"))) as transport:
            try:
                transport._bearer()
            except PowerAutomateClientError as exc:
                errors.append(str(exc))

    with caplog.at_level(logging.WARNING):
        thread = threading.Thread(target=pa_request, name="pa-auth-test")
        thread.start()
        assert entered.wait(5)
        try:
            with (
                ClientSecretCredential("fake-tenant", "fake-other-client", "fake-other-secret") as other,
                pytest.raises(RuntimeError, match=other_secret),
            ):
                other.get_token("https://service.flow.microsoft.com/.default")
        finally:
            release.set()
            thread.join(5)
        assert not thread.is_alive()
    assert errors == ["authentication_failed"]
    assert pa_secret not in caplog.text
    assert other_secret in caplog.text


def test_real_sdk_close_diagnostics_and_failure_are_value_free(monkeypatch, caplog):
    from azure.core.credentials import AccessTokenInfo
    from azure.identity import ClientSecretCredential
    from azure.identity._internal.msal_client import MsalClient

    secret = "fake-sdk-close-secret"
    monkeypatch.setattr(ClientSecretCredential, "_acquire_token_silently", lambda *a, **k: None)
    monkeypatch.setattr(ClientSecretCredential, "_request_token", lambda *a, **k: AccessTokenInfo("fake-token", 9999999999))

    def close_error(*args):
        logging.getLogger("azure.identity.transport").warning("close failed %s", secret)
        raise RuntimeError(secret)

    monkeypatch.setattr(MsalClient, "__exit__", close_error)
    transport = client(PowerAutomateSourceConfig.from_dict(options("service_principal")))
    with caplog.at_level(logging.WARNING):
        assert transport._bearer() == "Bearer fake-token"
        with pytest.raises(PowerAutomateClientError, match="authentication_failed") as raised:
            transport.close()
    assert raised.value.__context__ is None
    assert secret not in caplog.text
    assert "power_automate_auth_vendor_diagnostic" in caplog.text
    logging.getLogger("azure.identity.transport").warning("unscoped close detail %s", secret)
    assert secret in caplog.text


def test_foreign_token_parser_accepts_real_sdk_and_dynamic_external_values():
    from elspeth.plugins.infrastructure.clients.power_automate import _admit_power_automate_token

    class VendorToken(BaseModel):
        model_config = ConfigDict(extra="allow")

    actual = _admit_power_automate_token(AccessToken("fake-sdk-token", 9999999999))
    dynamic = _admit_power_automate_token(VendorToken.model_validate({"token": "fake-sdk-token"}))
    assert actual == dynamic
    assert actual.value == "fake-sdk-token"
    assert "fake-sdk-token" not in repr(actual)


@pytest.mark.parametrize("value", [None, False, 4, b"fake-token", "", " ", "leading token", "fake-token\r\n", "é-token"])
def test_foreign_token_parser_refuses_invalid_values(value):
    from elspeth.plugins.infrastructure.clients.power_automate import _admit_power_automate_token

    class VendorToken(BaseModel):
        model_config = ConfigDict(extra="allow")

    raw = VendorToken.model_validate({"token": value})
    with pytest.raises(PowerAutomateClientError, match="authentication_failed"):
        _admit_power_automate_token(raw)


def test_foreign_token_parser_refuses_missing_token():
    from elspeth.plugins.infrastructure.clients.power_automate import _admit_power_automate_token

    with pytest.raises(PowerAutomateClientError, match="authentication_failed"):
        _admit_power_automate_token(object())


def test_foreign_token_parser_reads_value_once_without_unused_expiry():
    from elspeth.plugins.infrastructure.clients.power_automate import _admit_power_automate_token

    reads = []

    class VendorToken:
        @property
        def token(self):
            reads.append("token")
            return "fake-sdk-token"

        @property
        def expires_on(self):
            pytest.fail("unused vendor expiry was read")

    assert _admit_power_automate_token(VendorToken()).value == "fake-sdk-token"
    assert reads == ["token"]


def test_foreign_token_attribute_failure_is_closed_at_sdk_boundary(monkeypatch):
    secret = "fake-token-property-secret"

    class VendorToken:
        @property
        def token(self):
            raise RuntimeError(secret)

    class Credential:
        def __init__(self, **kwargs):
            pass

        def get_token(self, *args):
            return VendorToken()

        def close(self):
            pass

    monkeypatch.setattr("azure.identity.ManagedIdentityCredential", Credential)
    with (
        client(PowerAutomateSourceConfig.from_dict(options("managed_identity"))) as transport,
        pytest.raises(PowerAutomateClientError, match="authentication_failed") as raised,
    ):
        transport._bearer()
    assert raised.value.__context__ is None
    assert secret not in "".join(traceback.format_exception(raised.value))
