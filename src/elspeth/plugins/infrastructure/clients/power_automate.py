"""Lazy, endpoint-fixed Power Automate transport composed by the runtime.

The adapter receives JSON and audited response identities only. Endpoint,
credentials, bounds and current coordinator authority stay in this client.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self
from urllib.parse import urlsplit

import httpx

from elspeth.contracts.audit_protocols import CallRecorder
from elspeth.contracts.call_data import HTTPCallRequest
from elspeth.contracts.call_mode import CallModeSession
from elspeth.contracts.contexts import RateLimitRegistryProtocol
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError, FrameworkBugError
from elspeth.contracts.events import TelemetryEvent
from elspeth.contracts.freeze import deep_freeze, deep_thaw
from elspeth.contracts.http_policy import HTTPAuditPolicy
from elspeth.contracts.security import get_fingerprint_key
from elspeth.contracts.sink_effect_http import (
    SinkEffectHTTPBindContext,
    SinkEffectHTTPPost,
    SinkEffectHTTPPostFactory,
    SinkEffectHTTPPostRequest,
    SinkEffectHTTPPostResponse,
)
from elspeth.core.canonical import stable_hash
from elspeth.core.security.web import NetworkError, SSRFBlockedError, validate_url_for_ssrf
from elspeth.plugins.infrastructure.clients.fingerprinting import fingerprint_url
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient
from elspeth.plugins.infrastructure.clients.vendor_logging import power_automate_auth_diagnostics
from elspeth.plugins.infrastructure.power_automate import (
    POWER_AUTOMATE_SCOPE,
    PowerAutomateManagedIdentityAuth,
    PowerAutomateProtocolError,
    PowerAutomateSASAuth,
    PowerAutomateServicePrincipalAuth,
    PowerAutomateSinkConfig,
    PowerAutomateSourceConfig,
    validate_trigger_url,
)
from elspeth.plugins.infrastructure.power_automate_nonlive import (
    ArchivedPowerAutomateSASAuth,
    ArchivedPowerAutomateServicePrincipalAuth,
    ArchivedPowerAutomateSinkConfig,
    ArchivedPowerAutomateSourceConfig,
    DeferredPowerAutomateCredential,
)

if TYPE_CHECKING:
    from azure.identity import ClientSecretCredential, ManagedIdentityCredential

PowerAutomateClientErrorCode = Literal[
    "authentication_failed",
    "authority_refused",
    "fingerprint_key_required",
    "request_too_large",
    "invalid_request",
    "http_status",
    "media_type",
    "client_closed",
]


class PowerAutomateClientError(RuntimeError):
    """Closed diagnostics without external credential, URL or response values."""

    def __init__(self, code: PowerAutomateClientErrorCode) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True, repr=False)
class _AdmittedPowerAutomateToken:
    value: str


def _admit_power_automate_token(raw_token: object) -> _AdmittedPowerAutomateToken:
    """Read foreign SDK data once, then retain only an owned header-safe value."""
    missing = object()
    value = getattr(raw_token, "token", missing)
    if type(value) is not str or not value or not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise PowerAutomateClientError("authentication_failed")
    return _AdmittedPowerAutomateToken(value)


class PowerAutomateOperationClient:
    """One source operation's lazy auth and audited request lifecycle."""

    def __init__(
        self,
        config: PowerAutomateSourceConfig | PowerAutomateSinkConfig | ArchivedPowerAutomateSourceConfig | ArchivedPowerAutomateSinkConfig,
        *,
        recorder: CallRecorder,
        run_id: str,
        operation_id: str,
        coordination_token: CoordinationToken,
        telemetry_emit: Callable[[TelemetryEvent], None],
        before_send: Callable[[], None],
        rate_limit_registry: RateLimitRegistryProtocol | None = None,
        call_mode_session: CallModeSession | None = None,
        credential: DeferredPowerAutomateCredential | None = None,
    ) -> None:
        if not isinstance(
            config, (PowerAutomateSourceConfig, PowerAutomateSinkConfig, ArchivedPowerAutomateSourceConfig, ArchivedPowerAutomateSinkConfig)
        ):
            raise TypeError("Power Automate operation requires a nominal runtime config")
        if type(coordination_token) is not CoordinationToken or coordination_token.run_id != run_id:
            raise FrameworkBugError("Power Automate operation requires current run authority")
        if not operation_id or not callable(before_send):
            raise FrameworkBugError("Power Automate operation requires an operation and guard")
        if call_mode_session is not None and call_mode_session.mode is RunMode.REPLAY:
            raise AuditIntegrityError("Power Automate replay must restore archived source decisions")
        if (
            isinstance(config, (PowerAutomateSinkConfig, ArchivedPowerAutomateSinkConfig))
            and call_mode_session is not None
            and call_mode_session.mode is not RunMode.LIVE
        ):
            raise AuditIntegrityError("Power Automate sink transport is live-only")
        archived = isinstance(config, (ArchivedPowerAutomateSourceConfig, ArchivedPowerAutomateSinkConfig))
        if archived and (
            call_mode_session is None
            or call_mode_session.mode is not RunMode.VERIFY
            or type(config) is not ArchivedPowerAutomateSourceConfig
        ):
            raise AuditIntegrityError("Archived Power Automate transport requires a verified source operation")
        secret_bearing = isinstance(config.auth, (ArchivedPowerAutomateSASAuth, ArchivedPowerAutomateServicePrincipalAuth))
        if (archived and secret_bearing) != (credential is not None):
            raise AuditIntegrityError("Power Automate deferred credential differs from archived auth")
        if credential is not None and type(credential) is not DeferredPowerAutomateCredential:
            raise TypeError("Power Automate credential must be a nominal deferred locator")
        self._config = config.model_copy()
        self._recorder = recorder
        self._run_id = run_id
        self._operation_id = operation_id
        self._coordination_token = coordination_token
        self._telemetry_emit = telemetry_emit
        self._guard = before_send
        self._rate_limit_registry = rate_limit_registry
        self._session = call_mode_session
        self._deferred_credential = credential
        self._credential: ClientSecretCredential | ManagedIdentityCredential | None = None
        self._http: AuditedHTTPClient | None = None
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None) -> None:
        if exc_type is None:
            self.close()
        else:
            # Teardown must not replace an uncertain publication/audit failure.
            with suppress(PowerAutomateClientError):
                self.close()

    def _require_fingerprint_key(self) -> None:
        failed = False
        try:
            get_fingerprint_key()
        except ValueError:
            failed = True
        if failed:
            raise PowerAutomateClientError("fingerprint_key_required")

    def _endpoint(self) -> str:
        auth = self._config.auth
        if isinstance(auth, PowerAutomateSASAuth):
            self._require_fingerprint_key()
            return auth.trigger_url_secret.get_secret_value()
        if isinstance(auth, ArchivedPowerAutomateSASAuth):
            return self._resolve_credential()
        endpoint = self._config.trigger_url
        if endpoint is None:
            raise FrameworkBugError("OAuth Power Automate endpoint is missing")
        return endpoint

    def _resolve_credential(self) -> str:
        credential = self._deferred_credential
        if credential is None:
            raise AuditIntegrityError("Archived Power Automate credential is missing")
        refused = False
        value = None
        try:
            value = credential.resolve()
        except ValueError:
            refused = True
        if refused:
            raise PowerAutomateClientError("authentication_failed")
        assert value is not None
        return value.get_secret_value()

    def _verify_preflight(self, url: str, body: Mapping[str, object], *, sas: bool) -> str | None:
        if self._session is None or self._session.mode is not RunMode.VERIFY:
            return None
        parsed = urlsplit(url)
        host = parsed.hostname
        assert host is not None
        host_header = f"[{host}]" if ":" in host else host
        request_data = HTTPCallRequest(
            method="POST",
            url=fingerprint_url(url, honor_development_mode=False),
            headers={"Host": host_header, "Content-Type": "application/json"},
            json=body,
        ).to_dict()
        if sas:
            self._session.preflight_verify_http_request(
                request_data=request_data,
                current_state_id=None,
                current_operation_id=self._operation_id,
            )
            return None
        evidence = self._session.preflight_verify_http_managed_identity(
            request_data=request_data,
            current_state_id=None,
            current_operation_id=self._operation_id,
        )
        return evidence.source_call_id

    def _bearer(self) -> str:
        """Constrain the SDK exception boundary before diagnostics can emit."""
        with power_automate_auth_diagnostics():
            return self._acquire_bearer()

    def _acquire_bearer(self) -> str:
        self._require_fingerprint_key()
        from azure.identity import ClientSecretCredential, ManagedIdentityCredential

        auth = self._config.auth
        secret: str | None = None
        if isinstance(auth, PowerAutomateServicePrincipalAuth):
            self._require_fingerprint_key()
            secret = auth.client_secret.get_secret_value()
        elif isinstance(auth, ArchivedPowerAutomateServicePrincipalAuth):
            secret = self._resolve_credential()
        failed = False
        token: _AdmittedPowerAutomateToken | None = None
        try:
            if self._credential is None:
                if isinstance(auth, (PowerAutomateServicePrincipalAuth, ArchivedPowerAutomateServicePrincipalAuth)):
                    assert secret is not None
                    self._credential = ClientSecretCredential(
                        tenant_id=auth.tenant_id,
                        client_id=auth.client_id,
                        client_secret=secret,
                        retry_total=0,
                    )
                elif isinstance(auth, PowerAutomateManagedIdentityAuth):
                    self._credential = ManagedIdentityCredential(client_id=auth.client_id, retry_total=0)
                else:
                    raise FrameworkBugError("SAS requests cannot acquire bearer credentials")
            token = _admit_power_automate_token(self._credential.get_token(POWER_AUTOMATE_SCOPE))
        except (FrameworkBugError, AuditIntegrityError, PowerAutomateClientError):
            raise
        except Exception:
            # External SDK constructors and token acquisition can raise
            # value-bearing exceptions. Raising outside this handler removes
            # their exception context as well as their rendered message.
            failed = True
        if failed:
            raise PowerAutomateClientError("authentication_failed")
        assert token is not None
        return f"Bearer {token.value}"

    def post_json(self, body: Mapping[str, object]) -> SinkEffectHTTPPostResponse:
        if self._closed:
            raise PowerAutomateClientError("client_closed")
        self._guard()
        invalid = False
        try:
            detached = deep_thaw(body)
            serialized = httpx.Request("POST", "https://serialization.invalid", json=detached).content
        except (TypeError, ValueError, RecursionError, UnicodeError):
            invalid = True
        if invalid:
            raise PowerAutomateClientError("invalid_request")
        if len(serialized) > self._config.max_request_body_bytes:
            raise PowerAutomateClientError("request_too_large")
        endpoint = self._endpoint()
        sas = isinstance(self._config.auth, (PowerAutomateSASAuth, ArchivedPowerAutomateSASAuth))
        refused = False
        try:
            validate_trigger_url(endpoint, allowed_origin=self._config.allowed_origin, sas=sas)
        except PowerAutomateProtocolError:
            refused = True
        if refused:
            raise PowerAutomateClientError("authority_refused")
        admitted_call = self._verify_preflight(endpoint, detached, sas=sas)
        try:
            safe_request = validate_url_for_ssrf(endpoint)
        except (SSRFBlockedError, NetworkError):
            refused = True
        if refused:
            raise PowerAutomateClientError("authority_refused")
        headers = {"Content-Type": "application/json"}
        if not sas:
            self._guard()
            headers["Authorization"] = self._bearer()
        if self._http is None:
            limiter = self._rate_limit_registry.get_limiter("power_automate") if self._rate_limit_registry is not None else None
            self._http = AuditedHTTPClient(
                self._recorder,
                state_id=None,
                run_id=self._run_id,
                telemetry_emit=self._telemetry_emit,
                operation_id=self._operation_id,
                coordination_token=self._coordination_token,
                timeout=self._config.timeout_seconds,
                max_response_body_bytes=self._config.max_response_body_bytes,
                limiter=limiter,
                call_mode_session=self._session,
                semantic_managed_identity_verify=not sas and self._session is not None and self._session.mode is RunMode.VERIFY,
                audit_policy=HTTPAuditPolicy.POWER_AUTOMATE_V1,
            )
        response, _, call = self._http.request_ssrf_safe(
            "POST",
            safe_request,
            headers=headers,
            json=detached,
            follow_redirects=False,
            verify_source_call_id=admitted_call,
            before_send=self._guard,
        )
        self._guard()
        if response.status_code != 200:
            raise PowerAutomateClientError("http_status")
        content_type = response.headers.get("content-type")
        if content_type is None or content_type.split(";", 1)[0].strip().casefold() != "application/json":
            raise PowerAutomateClientError("media_type")
        if call.request_ref is None or call.response_ref is None:
            raise AuditIntegrityError("Power Automate response has no durable audited payload references")
        return SinkEffectHTTPPostResponse(
            response.status_code, content_type, response.content, call.call_id, call.request_ref, call.response_ref
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._http is not None:
            self._http.close()
        failed = False
        if self._credential is not None:
            try:
                with power_automate_auth_diagnostics():
                    self._credential.close()
            except (FrameworkBugError, AuditIntegrityError):
                raise
            except Exception:
                failed = True
        if failed:
            raise PowerAutomateClientError("authentication_failed")


class _PowerAutomateHTTPPost(SinkEffectHTTPPost):
    def __init__(self, config: PowerAutomateSinkConfig, context: SinkEffectHTTPBindContext) -> None:
        self._config = config.model_copy()
        self._context = context

    def post_json(self, request: SinkEffectHTTPPostRequest) -> SinkEffectHTTPPostResponse:
        if type(request) is not SinkEffectHTTPPostRequest:
            raise TypeError("Power Automate HTTP capability requires an owned JSON request")
        with PowerAutomateOperationClient(
            self._config,
            recorder=self._context.recorder,
            run_id=self._context.run_id,
            operation_id=self._context.operation_id,
            coordination_token=self._context.coordination_token,
            telemetry_emit=self._context.telemetry_emit,
            before_send=self._context.before_send,
            rate_limit_registry=self._context.rate_limit_registry,
        ) as client:
            return client.post_json(request.json_body)


class PowerAutomateHTTPPostFactory(SinkEffectHTTPPostFactory):
    """Live-only factory; construction and binding allocate no SDK or network."""

    def __init__(self, config: PowerAutomateSinkConfig, *, safe_config: Mapping[str, object]) -> None:
        if type(config) is not PowerAutomateSinkConfig:
            raise TypeError("Power Automate HTTP factory requires a live nominal sink config")
        self._config = config.model_copy()
        self._safe_config = deep_freeze(safe_config)
        self._safe_config_fingerprint = stable_hash(self._safe_config)

    @property
    def safe_config_fingerprint(self) -> str:
        return self._safe_config_fingerprint

    def bind(self, context: SinkEffectHTTPBindContext) -> SinkEffectHTTPPost:
        if type(context) is not SinkEffectHTTPBindContext:
            raise TypeError("Power Automate HTTP factory requires an owned bind context")
        return _PowerAutomateHTTPPost(self._config, context)
