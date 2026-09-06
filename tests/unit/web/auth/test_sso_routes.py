"""The three SSO routes over the service — and what they do when nobody wired them.

Spec §2. The service (``web/auth/sso.py``) is tested on its own; this file
is about the ROUTE layer's obligations: reading the request, clearing the
cookie on every callback outcome, ``no-store`` on the sensitive responses,
the audit rows the service leaves to the route, and — first — refusing
cleanly when ``app.state.sso`` is absent or is not an ``SsoRuntime``.

That last obligation outlived the config-shaped route to it. Since step E an
under-configured IdP deployment cannot exist: ``WebSettings`` refuses the
shape, and ``build_sso_wiring`` returning ``None`` raises in the factory. What
still reaches the routes unwired is the LIFECYCLE gap — ``app.state.sso`` is
bound in the lifespan (``app.py``, ``resolve_sso_runtime``), not in
``create_app``, so it is absent for every request served against an app whose
startup has not run, which is exactly what ``ASGITransport`` does here. The
routes must never assume a caller did the wiring, and must refuse closed
rather than ``AttributeError`` when one did not.

The walk runs through the real router against the in-process fake IdP, the
real SQLite handoff store and the real session-token issuer. The audit
recorder is a recording double: the rows are what the trail would carry, and
the join between the callback's ``login`` row and complete's
``token_issued`` row is asserted by reading the callback's request id back
out of the token row.
"""

from __future__ import annotations

from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import SecretBytes
from sqlalchemy import insert
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from elspeth.web.auth.id_token import JWKSTokenValidator
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.auth.providers import _mechanics
from elspeth.web.auth.routes import create_auth_router
from elspeth.web.auth.session_token import SessionTokenIssuer
from elspeth.web.auth.sso import COOKIE_NAME, SsoClient, SsoRuntime
from elspeth.web.auth.urls import DiscoveredEndpoints
from elspeth.web.config import WebSettings
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.middleware.rate_limit import ComposerRateLimiter
from elspeth.web.middleware.request_id import RequestIdMiddleware
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.sso_handoff_repository import SsoHandoffRepository
from tests.helpers.fake_idp import FakeIdP

PUBLIC_BASE = "https://elspeth.example.gov.au"
REDIRECT_URI = f"{PUBLIC_BASE}/api/auth/sso/callback"
SPA_CALLBACK = f"{PUBLIC_BASE}/#/auth/callback"


# ==========================================================================
# Doubles.
# ==========================================================================


class _RecordingRecorder:
    """Every audit write the routes make, in order, with its kwargs."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def record_login_success_and_token_issued(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append(("login_success_and_token_issued", kwargs))

    def record_login_success(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append(("login_success", kwargs))

    def record_login_failure(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append(("login_failure", kwargs))

    def record_token_issued(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append(("token_issued", kwargs))

    def record_auth_failure(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append(("auth_failure", kwargs))

    def record_identity_admitted(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append(("identity_admitted", kwargs))

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.rows if name == kind]


@dataclass(frozen=True)
class _Identity:
    identity_id: str
    username: str
    access_state: str


class _Substrate:
    """The identity rows and the handoff store, on one in-memory sessions DB."""

    def __init__(self, engine: Engine, *, access_state: str = "active") -> None:
        self.engine = engine
        self.handoffs = SsoHandoffRepository(engine)
        self.access_state = access_state
        self.identities: dict[str, _Identity] = {}

    def upsert(self, claims: IdentityClaims) -> _Identity:
        identity = _Identity(f"id-{claims.subject}", claims.username, self.access_state)
        if identity.identity_id not in self.identities:
            # The handoff row's identity_id is a foreign key; give it a target.
            with self.engine.begin() as conn:
                conn.execute(
                    insert(identities_table).values(
                        identity_id=identity.identity_id,
                        provider=claims.provider,
                        subject=claims.subject,
                        username=claims.username,
                        first_seen_at=database_now(conn),
                    )
                )
        self.identities[identity.identity_id] = identity
        return identity

    def read(self, identity_id: str) -> _Identity | None:
        return self.identities.get(identity_id)


def _engine() -> Engine:
    engine = create_session_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    initialize_session_schema(engine)
    return engine


def _settings(provider: str) -> WebSettings:
    if provider == "local":
        return WebSettings(
            auth_provider="local",
            composer_max_composition_turns=15,
            composer_max_discovery_turns=10,
            composer_timeout_seconds=85.0,
            composer_rate_limit_per_minute=10,
            shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        )
    return WebSettings(
        auth_provider=provider,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        # Every setting the profile registry requires of an ``oidc`` deployment:
        # ``WebSettings`` refuses a partial one, so there is no lighter shape.
        sso_issuer="https://idp.example.gov.au",
        sso_client_id="elspeth-test-client",
        sso_client_secret="s" * 40,
        sso_transaction_secret="t" * 40,
        public_base_url=PUBLIC_BASE,
        compartment_id="example-compartment",
        quota_default_tokens_per_day=100_000,
        quota_default_storage_bytes=1_000_000,
    )


def _runtime(idp: FakeIdP, substrate: _Substrate, *, jwks_transport: httpx.AsyncBaseTransport | None = None) -> SsoRuntime:
    client = SsoClient(
        provider="oidc",
        client_id=idp.client_id,
        client_secret=idp.client_secret,
        redirect_uri=REDIRECT_URI,
        transaction_secret="t" * 40,
        public_base_url=PUBLIC_BASE,
        endpoints=DiscoveredEndpoints(
            authorization_endpoint=idp.authorization_endpoint,
            token_endpoint=idp.token_endpoint,
            jwks_uri=idp.jwks_uri,
            userinfo_endpoint=idp.userinfo_endpoint,
        ),
        userinfo=False,
    )
    return SsoRuntime(
        client=client,
        validator=JWKSTokenValidator(
            issuer=idp.issuer,
            audience=idp.client_id,
            algorithms=("RS256",),
            jwks_uri=idp.jwks_uri,
            transport=jwks_transport or idp.transport(),
        ),
        claim_checks=lambda _claims: None,
        map_identity=_mechanics.map_generic_oidc,
        handoffs=substrate.handoffs,
        upsert_identity=substrate.upsert,
        read_identity=substrate.read,
        issuer=SessionTokenIssuer(
            signing_key=b"k" * 32,
            provider="oidc",
            audience="elspeth-web",
            token_expiry_hours=1,
            max_refresh_chain_hours=2,
            principal_is_active=lambda _identity_id: True,
        ),
        transport=idp.transport(),
    )


def _app(*, provider: str = "oidc", sso: object | None = None, recorder: _RecordingRecorder | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)
    app.state.auth_provider = object()  # the SSO routes never touch it
    app.state.settings = _settings(provider)
    app.state.auth_audit_recorder = recorder or _RecordingRecorder()
    app.state.auth_rate_limiter = ComposerRateLimiter(limit=100)
    if sso is not None:
        app.state.sso = sso
    app.include_router(create_auth_router())
    return app


def _client(app: FastAPI) -> AsyncClient:
    # https, not http: the transaction cookie is Secure, and the jar (correctly)
    # withholds Secure cookies from a plain-http origin. ASGITransport does not
    # care about the scheme.
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


def _present_cookie(client: AsyncClient, value: str) -> None:
    """Hold the transaction cookie the way a browser would, in the jar."""
    client.cookies.set(COOKIE_NAME, value, domain="test", path="/")


def _set_cookie(response: Response) -> SimpleCookie:
    cookie: SimpleCookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie


def _fragment_params(location: str) -> dict[str, list[str]]:
    parts = urlsplit(location)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{PUBLIC_BASE}/"
    assert parts.query == "", "nothing in the query string: it would reach the ALB and uvicorn"
    return parse_qs(urlsplit("x://x/" + parts.fragment).query)


@dataclass(frozen=True)
class _Started:
    cookie_value: str
    state: str
    nonce: str


async def _start(client: AsyncClient) -> _Started:
    response = await client.get("/api/auth/sso/start")
    assert response.status_code == 302
    sent = parse_qs(urlsplit(response.headers["location"]).query)
    return _Started(cookie_value=_set_cookie(response)[COOKIE_NAME].value, state=sent["state"][0], nonce=sent["nonce"][0])


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


# ==========================================================================
# Unwired: the router mounted without a runtime behind it.
# ==========================================================================


class TestUnwired:
    """No ``app.state.sso``. The routes must refuse closed, never AttributeError.

    The settings here are a fully wired ``oidc`` deployment — the unwiredness
    is the missing runtime alone, which is the only half that remains
    reachable. That is not a contrived state: the attribute is bound during
    lifespan startup, so the window exists in the real app too.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path", "stage"),
        [
            ("GET", "/api/auth/sso/start", "sso_start"),
            ("GET", "/api/auth/sso/callback", "sso_callback"),
            ("POST", "/api/auth/sso/complete", "sso_complete"),
        ],
    )
    async def test_an_idp_deployment_without_the_runtime_refuses_with_503_and_an_audit_row(
        self, method: str, path: str, stage: str
    ) -> None:
        recorder = _RecordingRecorder()
        async with _client(_app(recorder=recorder)) as client:
            response = await client.request(method, path, json={"code": "x"} if method == "POST" else None)
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]
        assert recorder.of("auth_failure") == [
            {
                "provider": "oidc",
                "failure_category": "provider_unavailable",
                "failure_stage": stage,
                "user_id": None,
                "username": None,
                "exception_class": None,
            }
        ]

    @pytest.mark.asyncio
    async def test_a_wrong_typed_runtime_is_unwired_too(self) -> None:
        """isinstance, not truthiness: an impostor on app.state is refused (ADR-032)."""
        async with _client(_app(sso=object())) as client:
            response = await client.get("/api/auth/sso/start")
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_config_reports_no_start_url_when_unwired(self) -> None:
        """The SPA button is hidden by the same fact that makes the routes refuse."""
        async with _client(_app()) as client:
            response = await client.get("/api/auth/config")
        assert response.status_code == 200
        assert response.json()["sso_start_url"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path"), [("GET", "/api/auth/sso/start"), ("GET", "/api/auth/sso/callback"), ("POST", "/api/auth/sso/complete")]
    )
    async def test_a_local_deployment_has_no_sso_routes(self, method: str, path: str) -> None:
        recorder = _RecordingRecorder()
        async with _client(_app(provider="local", recorder=recorder)) as client:
            response = await client.request(method, path, json={"code": "x"} if method == "POST" else None)
        assert response.status_code == 404
        assert recorder.rows == [], "a route that does not exist writes nothing"

    @pytest.mark.asyncio
    async def test_a_local_deployment_reports_no_start_url(self) -> None:
        async with _client(_app(provider="local")) as client:
            response = await client.get("/api/auth/config")
        assert response.json()["sso_start_url"] is None


# ==========================================================================
# Wired: the walk through the routes.
# ==========================================================================


class TestStart:
    @pytest.mark.asyncio
    async def test_start_redirects_to_the_idp_and_seals_the_cookie(self, idp: FakeIdP) -> None:
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine())))) as client:
            response = await client.get("/api/auth/sso/start")

        assert response.status_code == 302
        location = urlsplit(response.headers["location"])
        assert f"{location.scheme}://{location.netloc}{location.path}" == idp.authorization_endpoint
        sent = parse_qs(location.query)
        assert sent["response_type"] == ["code"]
        assert sent["client_id"] == [idp.client_id]
        assert sent["redirect_uri"] == [REDIRECT_URI]
        assert sent["code_challenge_method"] == ["S256"]
        assert "openid" in sent["scope"][0].split()
        assert response.headers["cache-control"] == "no-store"

        cookie = _set_cookie(response)[COOKIE_NAME]
        assert cookie.value
        assert cookie["secure"] and cookie["httponly"]
        assert cookie["samesite"].lower() == "lax"
        assert cookie["path"] == "/"
        assert cookie["max-age"] == "300"
        assert not cookie["domain"], "__Host- is void with a Domain attribute"

    @pytest.mark.asyncio
    async def test_start_ignores_every_query_parameter(self, idp: FakeIdP) -> None:
        """A return path here would be an open redirect on the one unauthenticated route."""
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine())))) as client:
            response = await client.get(
                "/api/auth/sso/start", params={"next": "https://attacker.example.net/", "redirect_uri": "https://attacker.example.net/"}
            )
        assert response.status_code == 302
        assert "attacker" not in response.headers["location"]
        assert parse_qs(urlsplit(response.headers["location"]).query)["redirect_uri"] == [REDIRECT_URI]

    @pytest.mark.asyncio
    async def test_config_reports_the_start_url_when_wired(self, idp: FakeIdP) -> None:
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine())))) as client:
            response = await client.get("/api/auth/config")
        assert response.json()["sso_start_url"] == f"{PUBLIC_BASE}/api/auth/sso/start"


class TestTheWalk:
    @pytest.mark.asyncio
    async def test_start_callback_complete_yields_a_session_token_and_a_joined_trail(self, idp: FakeIdP) -> None:
        """THE POSITIVE CONTROL for the route layer. Every refusal below is vacuous without it."""
        recorder = _RecordingRecorder()
        substrate = _Substrate(_engine())
        runtime = _runtime(idp, substrate)
        async with _client(_app(sso=runtime, recorder=recorder)) as client:
            started = await _start(client)
            _present_cookie(client, started.cookie_value)
            code = idp.authorize(nonce=started.nonce, subject="ada", preferred_username="ada.l")

            callback = await client.get("/api/auth/sso/callback", params={"code": code, "state": started.state})
            assert callback.status_code == 302, callback.text
            assert callback.headers["cache-control"] == "no-store"
            (handoff,) = _fragment_params(callback.headers["location"])["code"]
            cleared = _set_cookie(callback)[COOKIE_NAME]
            assert cleared.value == "" and cleared["max-age"] == "0", "cleared on every callback outcome"
            callback_request_id = callback.headers["x-request-id"]

            complete = await client.post("/api/auth/sso/complete", json={"code": handoff})

        assert complete.status_code == 200, complete.text
        body = complete.json()
        assert set(body) == {"access_token", "token_type"} and body["token_type"] == "bearer"
        assert complete.headers["cache-control"] == "no-store"
        claims = runtime.issuer.decode(body["access_token"])
        assert (claims.identity_id, claims.username, claims.provider) == ("id-ada", "ada.l", "oidc")

        # The trail: one login row at the callback, one token_issued row at
        # complete, joined by the callback's request id.
        assert recorder.of("login_success") == [{"provider": "oidc", "user_id": "id-ada", "username": "ada.l", "identity_id": "id-ada"}]
        (issued,) = recorder.of("token_issued")
        assert issued["issuance_path"] == "sso_complete"
        assert issued["login_request_id"] == callback_request_id
        assert issued["access_token"] == body["access_token"]
        assert recorder.of("auth_failure") == []
        assert handoff not in complete.text, "the handoff is not echoed"

    @pytest.mark.asyncio
    async def test_a_replayed_handoff_is_refused_with_its_category(self, idp: FakeIdP) -> None:
        recorder = _RecordingRecorder()
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine())), recorder=recorder)) as client:
            started = await _start(client)
            _present_cookie(client, started.cookie_value)
            code = idp.authorize(nonce=started.nonce, subject="ada")
            callback = await client.get("/api/auth/sso/callback", params={"code": code, "state": started.state})
            (handoff,) = _fragment_params(callback.headers["location"])["code"]
            first = await client.post("/api/auth/sso/complete", json={"code": handoff})
            second = await client.post("/api/auth/sso/complete", json={"code": handoff})

        assert first.status_code == 200
        assert second.status_code == 401
        assert recorder.of("auth_failure") == [
            {
                "provider": "oidc",
                "failure_category": "sso_handoff_invalid",
                "failure_stage": "sso_complete",
                "user_id": None,
                "username": None,
                "exception_class": "SsoHandoffInvalid",
            }
        ]
        assert len(recorder.of("token_issued")) == 1

    @pytest.mark.asyncio
    async def test_a_pending_identity_gets_a_login_row_and_a_handoff_but_no_token(self, idp: FakeIdP) -> None:
        """R6: the refusal is complete's, and the callback still records the login."""
        recorder = _RecordingRecorder()
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine(), access_state="pending")), recorder=recorder)) as client:
            started = await _start(client)
            _present_cookie(client, started.cookie_value)
            code = idp.authorize(nonce=started.nonce, subject="ada")
            callback = await client.get("/api/auth/sso/callback", params={"code": code, "state": started.state})
            (handoff,) = _fragment_params(callback.headers["location"])["code"]
            complete = await client.post("/api/auth/sso/complete", json={"code": handoff})

        assert complete.status_code == 401
        assert len(recorder.of("login_success")) == 1
        assert recorder.of("token_issued") == []
        assert [row["failure_category"] for row in recorder.of("auth_failure")] == ["sso_access_pending"]


class TestCallbackRefusals:
    """Every refusal is a redirect with the category in the fragment, the cookie cleared, and an audit row."""

    @staticmethod
    async def _refused(client: AsyncClient, *, params: dict[str, str]) -> Response:
        response = await client.get("/api/auth/sso/callback", params=params)
        assert response.status_code == 302
        cleared = _set_cookie(response)[COOKIE_NAME]
        assert cleared.value == "" and cleared["max-age"] == "0"
        return response

    @pytest.mark.asyncio
    async def test_a_forged_state_costs_no_token_call_and_is_named_in_the_fragment(self, idp: FakeIdP) -> None:
        recorder = _RecordingRecorder()
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine())), recorder=recorder)) as client:
            started = await _start(client)
            _present_cookie(client, started.cookie_value)
            response = await self._refused(client, params={"code": "c", "state": "forged"})

        assert _fragment_params(response.headers["location"]) == {"error": ["sso_state_mismatch"]}
        assert idp.token_requests == []
        assert [row["failure_category"] for row in recorder.of("auth_failure")] == ["sso_state_mismatch"]
        assert recorder.of("auth_failure")[0]["failure_stage"] == "sso_callback"

    @pytest.mark.asyncio
    async def test_a_missing_cookie_is_its_own_category(self, idp: FakeIdP) -> None:
        recorder = _RecordingRecorder()
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine())), recorder=recorder)) as client:
            client.cookies.clear()
            response = await self._refused(client, params={"code": "c", "state": "s"})
        assert _fragment_params(response.headers["location"]) == {"error": ["sso_cookie_missing"]}

    @pytest.mark.asyncio
    async def test_a_duplicated_state_parameter_is_treated_as_absent(self, idp: FakeIdP) -> None:
        """Two states is not a value with a tie-break; it is not the request the IdP sends.

        The REAL state is last on purpose: a "last one wins" reading would
        pass the state check and fail later at the token exchange with a
        different category, so only treat-as-absent yields the mismatch.
        """
        recorder = _RecordingRecorder()
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine())), recorder=recorder)) as client:
            started = await _start(client)
            _present_cookie(client, started.cookie_value)
            response = await client.get(f"/api/auth/sso/callback?code=c&state=forged&state={started.state}")
        assert response.status_code == 302
        assert _fragment_params(response.headers["location"]) == {"error": ["sso_state_mismatch"]}

    @pytest.mark.asyncio
    async def test_an_idp_refusal_is_mapped_not_echoed(self, idp: FakeIdP) -> None:
        recorder = _RecordingRecorder()
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine())), recorder=recorder)) as client:
            started = await _start(client)
            _present_cookie(client, started.cookie_value)
            response = await self._refused(
                client,
                params={"state": started.state, "error": "access_denied", "error_description": "<script>alert(1)</script>"},
            )
        assert _fragment_params(response.headers["location"]) == {"error": ["sso_idp_error"]}
        assert "script" not in response.headers["location"]
        assert "script" not in repr(recorder.rows)

    @pytest.mark.asyncio
    async def test_a_jwks_outage_redirects_with_provider_unavailable(self, idp: FakeIdP) -> None:
        """Mid-navigation there is no 503 to show; the category says 'wait', and the audit row says why."""

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == idp.jwks_uri:
                return httpx.Response(503)
            return idp.respond(request)

        recorder = _RecordingRecorder()
        async with _client(
            _app(sso=_runtime(idp, _Substrate(_engine()), jwks_transport=httpx.MockTransport(handler)), recorder=recorder)
        ) as client:
            started = await _start(client)
            _present_cookie(client, started.cookie_value)
            code = idp.authorize(nonce=started.nonce, subject="ada")
            response = await self._refused(client, params={"code": code, "state": started.state})

        assert _fragment_params(response.headers["location"]) == {"error": ["provider_unavailable"]}
        (row,) = recorder.of("auth_failure")
        assert (row["failure_category"], row["exception_class"]) == ("provider_unavailable", "AuthProviderUnavailable")


class TestCompleteShape:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [{}, {"code": ""}, {"code": "x" * 129}, {"code": 1}, {"code": "x", "extra": 1}],
        ids=["missing", "empty", "too-long", "int", "extra-field"],
    )
    async def test_a_malformed_body_is_refused_at_the_boundary(self, idp: FakeIdP, body: dict[str, Any]) -> None:
        async with _client(_app(sso=_runtime(idp, _Substrate(_engine())))) as client:
            response = await client.post("/api/auth/sso/complete", json=body)
        assert response.status_code == 422
