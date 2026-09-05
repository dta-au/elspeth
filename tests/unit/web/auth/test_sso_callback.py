"""The callback: from the browser's return to the handoff redirect.

Spec §2 callback, §ID-token validation, §Userinfo, §Handoff, §Failure
categories.

Three layers, tested separately because each has decisions of its own:

* the two Tier-3 parses (token response, userinfo body) are pure and are
  tested with hand-built inputs — that is where a wrong shape is refused;
* ``open_callback``, ``redeem_authorization_code`` and ``fetch_userinfo``
  are each tested against the in-process fake IdP so a refusal is a real
  counterparty behaving badly, not a string failing a parser;
* ``login_callback`` is the ORDER: what runs before what, what is never
  reached after a refusal, and that the login row exists before the handoff.

The injected callables are recorded into one shared sequence so that "the
login row is written before the handoff is issued" is an assertion on
observed order, not on line order in the source.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from elspeth.contracts.auth import AuthProviderType
from elspeth.web.auth.id_token import JWKSTokenValidator
from elspeth.web.auth.models import AuthenticationError, AuthProviderUnavailable, IdentityClaims
from elspeth.web.auth.providers import _mechanics
from elspeth.web.auth.sso import (
    SSO_FAILURE_CATEGORIES,
    CallbackQuery,
    RedeemedTokens,
    SsoAccessPending,
    SsoClaimCheckFailed,
    SsoClient,
    SsoCookieInvalid,
    SsoCookieMissing,
    SsoIdentityDisabled,
    SsoIdpError,
    SsoIdTokenInvalid,
    SsoStateMismatch,
    SsoTokenExchangeFailed,
    SsoUserinfoInvalid,
    admit,
    authorization_redirect,
    failure_category,
    failure_location,
    fetch_userinfo,
    handoff_location,
    login_callback,
    open_callback,
    parse_token_response,
    parse_userinfo,
    pkce_challenge,
    redeem_authorization_code,
)
from elspeth.web.auth.urls import DiscoveredEndpoints
from tests.helpers.fake_idp import FakeIdP

SECRET = "an-operator-transaction-secret-of-adequate-length-0123456789"
REDIRECT = "https://elspeth.example.gov.au/api/auth/sso/callback"
PUBLIC_BASE = "https://elspeth.example.gov.au"
REQUEST_ID = "req-0001"


# ==========================================================================
# Doubles for the injected seams, recording into ONE sequence.
# ==========================================================================


@dataclass(frozen=True)
class _Identity:
    identity_id: str
    username: str
    access_state: str


class _Handoffs:
    def __init__(self, journal: list[tuple[str, Any]]) -> None:
        self.journal = journal
        self.issued: list[tuple[str, str, str]] = []

    def issue(self, *, code_hash: str, identity_id: str, request_id: str) -> None:
        self.issued.append((code_hash, identity_id, request_id))
        self.journal.append(("handoff", code_hash))

    def consume(self, *, code_hash: str) -> str | None:
        raise AssertionError("the callback never consumes")


class _Substrate:
    """The upsert, the audit write and the handoff store, sharing a journal."""

    def __init__(self, *, access_state: str = "active") -> None:
        self.journal: list[tuple[str, Any]] = []
        self.access_state = access_state
        self.handoffs = _Handoffs(self.journal)
        self.upserted: list[IdentityClaims] = []
        self.recorded: list[_Identity] = []

    def upsert(self, claims: IdentityClaims) -> _Identity:
        self.upserted.append(claims)
        identity = _Identity(identity_id=f"id-{claims.subject}", username=claims.username, access_state=self.access_state)
        self.journal.append(("upsert", identity))
        return identity

    def record_login(self, identity: Any) -> None:
        self.recorded.append(identity)
        self.journal.append(("login", identity))


def _endpoints(idp: FakeIdP) -> DiscoveredEndpoints:
    return DiscoveredEndpoints(
        authorization_endpoint=idp.authorization_endpoint,
        token_endpoint=idp.token_endpoint,
        jwks_uri=idp.jwks_uri,
        userinfo_endpoint=idp.userinfo_endpoint,
    )


def _client(idp: FakeIdP, *, provider: AuthProviderType = "oidc", userinfo: bool = False) -> SsoClient:
    return SsoClient(
        provider=provider,
        client_id=idp.client_id,
        client_secret=idp.client_secret,
        redirect_uri=REDIRECT,
        transaction_secret=SECRET,
        public_base_url=PUBLIC_BASE,
        endpoints=_endpoints(idp),
        id_token_algorithms=("RS256",),
        userinfo=userinfo,
    )


def _validator(idp: FakeIdP) -> JWKSTokenValidator:
    return JWKSTokenValidator(issuer=idp.issuer, audience=idp.client_id, jwks_uri=idp.jwks_uri, transport=idp.transport())


@dataclass(frozen=True)
class _Browser:
    """What the browser holds after ``start``: the cookie, and what the IdP saw."""

    cookie: str
    state: str
    nonce: str
    code_challenge: str


def _start(idp: FakeIdP, client: SsoClient) -> _Browser:
    redirect = authorization_redirect(
        authorization_endpoint=client.endpoints.authorization_endpoint,
        client_id=client.client_id,
        redirect_uri=client.redirect_uri,
        scopes=("openid",),
        transaction_secret=client.transaction_secret,
        provider=client.provider,
    )
    # Read state/nonce the way the IdP does — from the URL — not by opening
    # the cookie, so the test sees exactly what a real counterparty sees.
    sent = parse_qs(urlsplit(redirect.location).query)
    return _Browser(
        cookie=redirect.cookie_value,
        state=sent["state"][0],
        nonce=sent["nonce"][0],
        code_challenge=sent["code_challenge"][0],
    )


def _no_checks(claims: Any) -> None:
    del claims


async def _walk(
    idp: FakeIdP,
    client: SsoClient,
    substrate: _Substrate,
    *,
    subject: str = "ada",
    claim_checks: Any = _no_checks,
    map_identity: Any = _mechanics.map_generic_oidc,
    nonce_override: str | None = None,
    **id_claims: Any,
) -> str:
    browser = _start(idp, client)
    code = idp.authorize(nonce=nonce_override if nonce_override is not None else browser.nonce, subject=subject, **id_claims)
    return await login_callback(
        CallbackQuery(code=code, state=browser.state, error=None),
        browser.cookie,
        client=client,
        validator=_validator(idp),
        claim_checks=claim_checks,
        map_identity=map_identity,
        upsert_identity=substrate.upsert,
        record_login=substrate.record_login,
        handoffs=substrate.handoffs,
        request_id=REQUEST_ID,
        transport=idp.transport(),
    )


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


# ==========================================================================
# The token-response boundary.
# ==========================================================================


class TestTokenResponseBoundary:
    def test_parse_token_response_non_dict_raises(self) -> None:
        with pytest.raises(SsoTokenExchangeFailed):
            parse_token_response(document=["not", "a", "dict"])

    def test_a_well_formed_response_yields_both_tokens(self) -> None:
        tokens = parse_token_response({"token_type": "Bearer", "id_token": "id.tok.en", "access_token": "acc"})
        assert (tokens.id_token, tokens.access_token) == ("id.tok.en", "acc")

    @pytest.mark.parametrize("token_type", ["bearer", "BEARER", "Bearer"])
    def test_token_type_is_case_insensitive(self, token_type: str) -> None:
        assert parse_token_response({"token_type": token_type, "id_token": "x", "access_token": "y"}).id_token == "x"

    @pytest.mark.parametrize(
        "document",
        [
            {"token_type": "mac", "id_token": "x", "access_token": "y"},
            {"id_token": "x", "access_token": "y"},
            {"token_type": "Bearer", "access_token": "y"},
            {"token_type": "Bearer", "id_token": "", "access_token": "y"},
            {"token_type": "Bearer", "id_token": None, "access_token": "y"},
            {"token_type": "Bearer", "id_token": ["x"], "access_token": "y"},
            {"token_type": "Bearer", "id_token": "x"},
            {"token_type": "Bearer", "id_token": "x", "access_token": ""},
            {"token_type": 1, "id_token": "x", "access_token": "y"},
        ],
        ids=[
            "not-bearer",
            "no-token_type",
            "no-id_token",
            "empty-id_token",
            "null-id_token",
            "list-id_token",
            "no-access_token",
            "empty-access_token",
            "int-token_type",
        ],
    )
    def test_a_malformed_response_is_refused(self, document: dict[str, Any]) -> None:
        with pytest.raises(SsoTokenExchangeFailed):
            parse_token_response(document)

    def test_a_refresh_token_is_never_read(self) -> None:
        """The owned type has no slot for it; the parse cannot have kept it."""
        tokens = parse_token_response({"token_type": "Bearer", "id_token": "x", "access_token": "y", "refresh_token": "SECRET"})
        assert "refresh" not in {*RedeemedTokens.__slots__}
        assert "SECRET" not in repr(tokens)

    def test_repr_hides_both_tokens(self) -> None:
        tokens = RedeemedTokens(id_token="ID-SECRET", access_token="ACCESS-SECRET")
        assert "ID-SECRET" not in repr(tokens) and "ACCESS-SECRET" not in repr(tokens)


# ==========================================================================
# The userinfo boundary.
# ==========================================================================


class TestUserinfoBoundary:
    def test_parse_userinfo_non_dict_raises(self) -> None:
        with pytest.raises(SsoUserinfoInvalid):
            parse_userinfo(document=["not", "a", "dict"], expected_subject="ada")

    def test_a_matching_subject_returns_the_body_for_the_profile_to_read(self) -> None:
        body = {"sub": "ada", "given_name": "Ada", "abn": "12 345 678 901"}
        assert parse_userinfo(body, expected_subject="ada") == body

    @pytest.mark.parametrize(
        "document",
        [{"sub": "not-ada"}, {}, {"sub": None}, {"sub": ["ada"]}, {"sub": "ada "}, {"sub": "Ada"}],
        ids=["different", "absent", "null", "list", "trailing-space", "case"],
    )
    def test_a_subject_that_does_not_match_exactly_is_refused(self, document: dict[str, Any]) -> None:
        with pytest.raises(SsoUserinfoInvalid):
            parse_userinfo(document, expected_subject="ada")

    def test_a_non_ascii_subject_does_not_crash_the_comparison(self) -> None:
        """compare_digest on str raises for non-ASCII; the bytes form must not."""
        with pytest.raises(SsoUserinfoInvalid):
            parse_userinfo({"sub": "adà"}, expected_subject="ada")
        assert parse_userinfo({"sub": "adà"}, expected_subject="adà")["sub"] == "adà"


# ==========================================================================
# open_callback: everything that costs nothing remote.
# ==========================================================================


class TestOpenCallback:
    @staticmethod
    def _open(browser: _Browser, query: CallbackQuery, cookie: str | None = "use-browser") -> tuple[str, Any]:
        return open_callback(
            query,
            browser.cookie if cookie == "use-browser" else cookie,
            transaction_secret=SECRET,
            provider="oidc",
            redirect_uri=REDIRECT,
        )

    def test_a_matching_state_yields_the_code_and_the_transaction(self, idp: FakeIdP) -> None:
        browser = _start(idp, _client(idp))
        code, transaction = self._open(browser, CallbackQuery(code="the-code", state=browser.state, error=None))
        assert code == "the-code"
        assert transaction.nonce == browser.nonce
        assert pkce_challenge(transaction.verifier) == browser.code_challenge

    def test_no_cookie_is_its_own_category(self, idp: FakeIdP) -> None:
        browser = _start(idp, _client(idp))
        with pytest.raises(SsoCookieMissing):
            self._open(browser, CallbackQuery(code="c", state=browser.state, error=None), cookie=None)

    def test_a_tampered_cookie_is_refused_before_anything_else_is_read(self, idp: FakeIdP) -> None:
        browser = _start(idp, _client(idp))
        with pytest.raises(SsoCookieInvalid):
            self._open(browser, CallbackQuery(code="c", state=browser.state, error=None), cookie=browser.cookie[:-4] + "AAAA")

    @pytest.mark.parametrize("state", [None, "", "some-other-state"], ids=["absent", "empty", "different"])
    def test_a_state_that_does_not_match_is_refused(self, idp: FakeIdP, state: str | None) -> None:
        browser = _start(idp, _client(idp))
        with pytest.raises(SsoStateMismatch):
            self._open(browser, CallbackQuery(code="c", state=state, error=None))

    def test_a_non_ascii_state_is_a_mismatch_not_a_crash(self, idp: FakeIdP) -> None:
        browser = _start(idp, _client(idp))
        with pytest.raises(SsoStateMismatch):
            self._open(browser, CallbackQuery(code="c", state="stâte", error=None))

    def test_another_browsers_cookie_is_a_mismatch(self, idp: FakeIdP) -> None:
        """Two tabs each called start; the callback carries tab A's state and tab B's cookie."""
        tab_a = _start(idp, _client(idp))
        tab_b = _start(idp, _client(idp))
        with pytest.raises(SsoStateMismatch):
            self._open(tab_b, CallbackQuery(code="c", state=tab_a.state, error=None))

    @pytest.mark.parametrize(
        ("error", "reason"),
        [("access_denied", "access_denied"), ("server_error", "other"), ("", "other"), ("<script>", "other")],
    )
    def test_an_idp_error_is_mapped_onto_two_values_and_never_echoed(self, idp: FakeIdP, error: str, reason: str) -> None:
        browser = _start(idp, _client(idp))
        with pytest.raises(SsoIdpError) as caught:
            self._open(browser, CallbackQuery(code=None, state=browser.state, error=error))
        assert caught.value.reason == reason
        assert error not in str(caught.value) or error == ""

    def test_an_idp_error_is_only_honoured_once_the_state_matches(self, idp: FakeIdP) -> None:
        """A crafted ?error= link must not write an idp_error row against a walk it was not part of."""
        browser = _start(idp, _client(idp))
        with pytest.raises(SsoStateMismatch):
            self._open(browser, CallbackQuery(code=None, state="forged", error="access_denied"))

    @pytest.mark.parametrize(
        "code",
        [None, "", "x" * 2049, "code\nwith-newline", "code\x00null"],
        ids=["absent", "empty", "too-long", "newline", "nul"],
    )
    def test_a_callback_with_no_usable_code_is_refused(self, idp: FakeIdP, code: str | None) -> None:
        browser = _start(idp, _client(idp))
        with pytest.raises(SsoIdpError) as caught:
            self._open(browser, CallbackQuery(code=code, state=browser.state, error=None))
        assert caught.value.reason == "other"

    def test_a_code_at_the_length_bound_is_accepted(self, idp: FakeIdP) -> None:
        browser = _start(idp, _client(idp))
        code, _ = self._open(browser, CallbackQuery(code="x" * 2048, state=browser.state, error=None))
        assert len(code) == 2048


# ==========================================================================
# The token exchange, against the fake provider.
# ==========================================================================


def _basic(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


class TestRedeem:
    @staticmethod
    async def _redeem(
        idp: FakeIdP, code: str, *, verifier: str = "v" * 43, transport: httpx.AsyncBaseTransport | None = None
    ) -> RedeemedTokens:
        return await redeem_authorization_code(
            code,
            verifier=verifier,
            token_endpoint=idp.token_endpoint,
            client_id=idp.client_id,
            client_secret=idp.client_secret,
            redirect_uri=REDIRECT,
            transport=transport or idp.transport(),
        )

    @pytest.mark.asyncio
    async def test_a_valid_code_is_exchanged_with_client_secret_basic_and_the_verifier(self, idp: FakeIdP) -> None:
        code = idp.authorize(nonce="n", subject="ada")
        tokens = await self._redeem(idp, code, verifier="the-verifier-" + "v" * 30)

        assert tokens.id_token and tokens.access_token
        request = idp.token_requests[-1]
        assert request.headers["authorization"] == _basic(idp.client_id, idp.client_secret)
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["grant_type"] == "authorization_code"
        assert form["code"] == code
        assert form["code_verifier"] == "the-verifier-" + "v" * 30
        assert form["redirect_uri"] == REDIRECT
        assert "client_secret" not in form, "the secret travels in the header, never the body"
        assert idp.codes[code].redeemed

    @pytest.mark.asyncio
    async def test_the_basic_credentials_are_form_encoded_before_base64(self) -> None:
        """RFC 6749 §2.3.1. A ':' in the id would otherwise split the pair in the wrong place."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["authorization"])
            return httpx.Response(200, json={"token_type": "Bearer", "id_token": "x", "access_token": "y"})

        await redeem_authorization_code(
            "c",
            verifier="v",
            token_endpoint="https://idp.example/token",
            client_id="id:with:colons",
            client_secret="s&cret=",
            redirect_uri=REDIRECT,
            transport=httpx.MockTransport(handler),
        )
        assert seen == ["Basic " + base64.b64encode(b"id%3Awith%3Acolons:s%26cret%3D").decode()]

    @pytest.mark.asyncio
    async def test_an_unknown_code_is_refused_and_the_status_is_named(self, idp: FakeIdP) -> None:
        with pytest.raises(SsoTokenExchangeFailed, match="HTTP 400"):
            await self._redeem(idp, "not-a-code-the-idp-issued")

    @pytest.mark.asyncio
    async def test_a_redeemed_code_cannot_be_redeemed_again(self, idp: FakeIdP) -> None:
        """Single use is the PROVIDER's; this proves the fake enforces it, so replay tests mean something."""
        code = idp.authorize(nonce="n", subject="ada")
        await self._redeem(idp, code)
        with pytest.raises(SsoTokenExchangeFailed, match="HTTP 400"):
            await self._redeem(idp, code)

    @pytest.mark.asyncio
    async def test_a_redirect_at_the_token_endpoint_is_not_followed(self, idp: FakeIdP) -> None:
        """Following it would post the client secret and the code to the redirect target."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(307, headers={"location": "https://attacker.example.net/token"})

        with pytest.raises(SsoTokenExchangeFailed, match="HTTP 307"):
            await self._redeem(idp, "c", transport=httpx.MockTransport(handler))
        assert seen == [idp.token_endpoint]

    @pytest.mark.asyncio
    async def test_an_oversized_response_is_refused_before_it_is_parsed(self, idp: FakeIdP) -> None:
        idp.token_response_override = {"token_type": "Bearer", "id_token": "x" * (64 * 1024), "access_token": "y"}
        with pytest.raises(SsoTokenExchangeFailed, match="maximum accepted size"):
            await self._redeem(idp, "c")

    @pytest.mark.asyncio
    async def test_a_non_json_body_is_refused(self, idp: FakeIdP) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        with pytest.raises(SsoTokenExchangeFailed, match="not valid JSON"):
            await self._redeem(idp, "c", transport=httpx.MockTransport(handler))

    @pytest.mark.asyncio
    async def test_a_transport_failure_names_the_class_and_never_the_address(self, idp: FakeIdP) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno 111] Connection refused to 10.0.0.7:443")

        with pytest.raises(SsoTokenExchangeFailed) as caught:
            await self._redeem(idp, "c", transport=httpx.MockTransport(handler))
        assert "ConnectError" in str(caught.value)
        assert "10.0.0.7" not in str(caught.value)


# ==========================================================================
# Userinfo, against the fake provider.
# ==========================================================================


class TestFetchUserinfo:
    @staticmethod
    async def _fetch(idp: FakeIdP, *, subject: str = "ada", transport: httpx.AsyncBaseTransport | None = None) -> Any:
        code = idp.authorize(nonce="n", subject=subject)
        idp.codes[code].redeemed = True  # the fake serves the last redeemed subject
        return await fetch_userinfo(
            userinfo_endpoint=idp.userinfo_endpoint,
            access_token="access-token",
            expected_subject=subject,
            transport=transport or idp.transport(),
        )

    @pytest.mark.asyncio
    async def test_the_body_for_the_verified_subject_is_returned(self, idp: FakeIdP) -> None:
        idp.userinfo_extra_claims = {"given_name": "Ada", "family_name": "Lovelace", "abn": "12 345 678 901"}
        body = await self._fetch(idp)
        assert body["sub"] == "ada" and body["abn"] == "12 345 678 901"

    @pytest.mark.asyncio
    async def test_the_access_token_is_presented_as_a_bearer(self, idp: FakeIdP) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["authorization"])
            return idp.respond(request)

        await self._fetch(idp, transport=httpx.MockTransport(handler))
        assert seen == ["Bearer access-token"]

    @pytest.mark.asyncio
    async def test_a_body_about_someone_else_is_refused(self, idp: FakeIdP) -> None:
        idp.userinfo_subject_override = "not-ada"
        with pytest.raises(SsoUserinfoInvalid, match="sub does not match"):
            await self._fetch(idp)

    @pytest.mark.asyncio
    async def test_a_non_json_content_type_is_refused_even_when_the_body_parses(self, idp: FakeIdP) -> None:
        idp.userinfo_content_type = "text/plain"
        with pytest.raises(SsoUserinfoInvalid, match="not application/json"):
            await self._fetch(idp)

    @pytest.mark.asyncio
    async def test_content_type_parameters_do_not_defeat_the_check(self, idp: FakeIdP) -> None:
        idp.userinfo_content_type = "application/json; charset=utf-8"
        assert (await self._fetch(idp))["sub"] == "ada"

    @pytest.mark.asyncio
    async def test_a_non_200_is_refused(self, idp: FakeIdP) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_token"})

        with pytest.raises(SsoUserinfoInvalid, match="HTTP 401"):
            await self._fetch(idp, transport=httpx.MockTransport(handler))

    @pytest.mark.asyncio
    async def test_an_oversized_body_is_refused(self, idp: FakeIdP) -> None:
        idp.userinfo_extra_claims = {"padding": "x" * (64 * 1024)}
        with pytest.raises(SsoUserinfoInvalid, match="maximum accepted size"):
            await self._fetch(idp)

    @pytest.mark.asyncio
    async def test_a_redirect_is_not_followed(self, idp: FakeIdP) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://attacker.example.net/userinfo"})

        with pytest.raises(SsoUserinfoInvalid, match="HTTP 302"):
            await self._fetch(idp, transport=httpx.MockTransport(handler))
        assert seen == [idp.userinfo_endpoint]

    @pytest.mark.asyncio
    async def test_a_transport_failure_is_the_same_category(self, idp: FakeIdP) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out")

        with pytest.raises(SsoUserinfoInvalid, match="ReadTimeout"):
            await self._fetch(idp, transport=httpx.MockTransport(handler))


# ==========================================================================
# The whole walk.
# ==========================================================================


class TestLoginCallback:
    @pytest.mark.asyncio
    async def test_the_well_behaved_provider_yields_a_handoff_in_the_fragment(self, idp: FakeIdP) -> None:
        """THE POSITIVE CONTROL. Every refusal below is vacuous without it."""
        substrate = _Substrate()
        location = await _walk(idp, _client(idp), substrate, subject="ada", preferred_username="ada.l")

        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{PUBLIC_BASE}/"
        assert parts.query == "", "nothing in the query string: it would reach the ALB and uvicorn logs"
        fragment = parse_qs(urlsplit("x://x/" + parts.fragment).query)
        (code,) = fragment["code"]

        assert substrate.handoffs.issued == [(hashlib.sha256(code.encode()).hexdigest(), "id-ada", REQUEST_ID)]
        assert [c.username for c in substrate.upserted] == ["ada.l"]
        assert substrate.recorded == [_Identity("id-ada", "ada.l", "active")]

    @pytest.mark.asyncio
    async def test_the_login_row_is_written_before_the_handoff_is_issued(self, idp: FakeIdP) -> None:
        """A handoff must never be redeemable for a login the trail does not record."""
        substrate = _Substrate()
        await _walk(idp, _client(idp), substrate)
        assert [entry for entry, _ in substrate.journal] == ["upsert", "login", "handoff"]

    @pytest.mark.asyncio
    async def test_the_handoff_code_is_never_stored_only_its_hash(self, idp: FakeIdP) -> None:
        substrate = _Substrate()
        location = await _walk(idp, _client(idp), substrate)
        code = parse_qs(urlsplit("x://x/" + urlsplit(location).fragment).query)["code"][0]
        assert code not in json.dumps(substrate.handoffs.issued)
        assert len(code) >= 43, "token_urlsafe(32) is 43 characters: sized as a credential"

    @pytest.mark.asyncio
    async def test_a_token_minted_for_a_different_nonce_is_refused(self, idp: FakeIdP) -> None:
        substrate = _Substrate()
        with pytest.raises(SsoIdTokenInvalid):
            await _walk(idp, _client(idp), substrate, nonce_override="a-nonce-from-some-other-login")
        assert substrate.journal == [], "nothing was upserted, recorded or issued"

    @pytest.mark.asyncio
    async def test_algorithm_confusion_is_refused(self, idp: FakeIdP) -> None:
        idp.id_token_algorithm = "HS256"
        substrate = _Substrate()
        with pytest.raises(SsoIdTokenInvalid):
            await _walk(idp, _client(idp), substrate)
        assert substrate.journal == []

    @pytest.mark.asyncio
    async def test_a_token_from_another_provider_with_the_same_issuer_string_is_refused(self, idp: FakeIdP) -> None:
        """Same issuer, different key. The JWKS is the authority, not the iss claim."""
        impostor = FakeIdP(issuer=idp.issuer, client_id=idp.client_id, client_secret=idp.client_secret)
        substrate = _Substrate()
        browser = _start(idp, _client(idp))
        code = impostor.authorize(nonce=browser.nonce, subject="ada")

        def handler(request: httpx.Request) -> httpx.Response:
            # Token exchange answered by the impostor; keys served by the real provider.
            if str(request.url) == idp.token_endpoint:
                return impostor.respond(request)
            return idp.respond(request)

        with pytest.raises(SsoIdTokenInvalid):
            await login_callback(
                CallbackQuery(code=code, state=browser.state, error=None),
                browser.cookie,
                client=_client(idp),
                validator=_validator(idp),
                claim_checks=_no_checks,
                map_identity=_mechanics.map_generic_oidc,
                upsert_identity=substrate.upsert,
                record_login=substrate.record_login,
                handoffs=substrate.handoffs,
                request_id=REQUEST_ID,
                transport=httpx.MockTransport(handler),
            )
        assert substrate.journal == []

    @pytest.mark.asyncio
    async def test_the_profiles_claim_check_runs_after_verification_and_refuses(self, idp: FakeIdP) -> None:
        substrate = _Substrate()
        seen: list[dict[str, Any]] = []

        def refuse(claims: Any) -> None:
            seen.append(dict(claims))
            raise AuthenticationError("wrong tenant")

        with pytest.raises(SsoClaimCheckFailed):
            await _walk(idp, _client(idp), substrate, claim_checks=refuse, tid="tenant-b")
        assert seen and seen[0]["tid"] == "tenant-b", "the check saw the VERIFIED claims"
        assert substrate.journal == []

    @pytest.mark.asyncio
    async def test_a_disabled_identity_is_refused_after_upsert_and_before_the_audit_row(self, idp: FakeIdP) -> None:
        substrate = _Substrate(access_state="disabled")
        with pytest.raises(SsoIdentityDisabled):
            await _walk(idp, _client(idp), substrate)
        assert [entry for entry, _ in substrate.journal] == ["upsert"], "no login row, no handoff"

    @pytest.mark.asyncio
    async def test_a_pending_identity_is_refused_the_same_way(self, idp: FakeIdP) -> None:
        substrate = _Substrate(access_state="pending")
        with pytest.raises(SsoAccessPending):
            await _walk(idp, _client(idp), substrate)
        assert [entry for entry, _ in substrate.journal] == ["upsert"]

    @pytest.mark.asyncio
    async def test_an_unrecognised_access_state_fails_closed(self, idp: FakeIdP) -> None:
        substrate = _Substrate(access_state="activeish")
        with pytest.raises(AuthenticationError, match="unrecognised access_state"):
            await _walk(idp, _client(idp), substrate)
        assert [entry for entry, _ in substrate.journal] == ["upsert"]

    @pytest.mark.asyncio
    async def test_a_jwks_outage_is_a_503_not_a_refusal(self, idp: FakeIdP) -> None:
        """The browser's remedy differs: 'wait', not 'start again'. Reclassifying it would say the wrong thing."""

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == idp.jwks_uri:
                return httpx.Response(503)
            return idp.respond(request)

        substrate = _Substrate()
        browser = _start(idp, _client(idp))
        code = idp.authorize(nonce=browser.nonce, subject="ada")
        validator = JWKSTokenValidator(
            issuer=idp.issuer, audience=idp.client_id, jwks_uri=idp.jwks_uri, transport=httpx.MockTransport(handler)
        )
        with pytest.raises(AuthProviderUnavailable):
            await login_callback(
                CallbackQuery(code=code, state=browser.state, error=None),
                browser.cookie,
                client=_client(idp),
                validator=validator,
                claim_checks=_no_checks,
                map_identity=_mechanics.map_generic_oidc,
                upsert_identity=substrate.upsert,
                record_login=substrate.record_login,
                handoffs=substrate.handoffs,
                request_id=REQUEST_ID,
                transport=idp.transport(),
            )
        assert substrate.journal == []

    @pytest.mark.asyncio
    async def test_a_bad_cookie_costs_no_token_endpoint_call(self, idp: FakeIdP) -> None:
        substrate = _Substrate()
        browser = _start(idp, _client(idp))
        with pytest.raises(SsoStateMismatch):
            await login_callback(
                CallbackQuery(code="c", state="forged", error=None),
                browser.cookie,
                client=_client(idp),
                validator=_validator(idp),
                claim_checks=_no_checks,
                map_identity=_mechanics.map_generic_oidc,
                upsert_identity=substrate.upsert,
                record_login=substrate.record_login,
                handoffs=substrate.handoffs,
                request_id=REQUEST_ID,
                transport=idp.transport(),
            )
        assert idp.token_requests == []

    # ── the userinfo profile ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_a_userinfo_profile_builds_the_identity_from_the_bound_body(self, idp: FakeIdP) -> None:
        idp.userinfo_extra_claims = {"given_name": "Ada", "family_name": "Lovelace", "abn": "12 345 678 901"}
        substrate = _Substrate()
        await _walk(idp, _client(idp, provider="vanguard", userinfo=True), substrate, map_identity=_mechanics.map_vanguard)
        (claims,) = substrate.upserted
        assert (claims.display_name, claims.organisation_id) == ("Ada Lovelace", "12 345 678 901")

    @pytest.mark.asyncio
    async def test_a_userinfo_body_about_someone_else_is_refused_before_upsert(self, idp: FakeIdP) -> None:
        idp.userinfo_subject_override = "someone-else"
        substrate = _Substrate()
        with pytest.raises(SsoUserinfoInvalid):
            await _walk(idp, _client(idp, provider="vanguard", userinfo=True), substrate, map_identity=_mechanics.map_vanguard)
        assert substrate.journal == []

    @pytest.mark.asyncio
    async def test_a_profile_without_userinfo_never_calls_it(self, idp: FakeIdP) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return idp.respond(request)

        substrate = _Substrate()
        browser = _start(idp, _client(idp))
        code = idp.authorize(nonce=browser.nonce, subject="ada")
        await login_callback(
            CallbackQuery(code=code, state=browser.state, error=None),
            browser.cookie,
            client=_client(idp),
            validator=_validator(idp),
            claim_checks=_no_checks,
            map_identity=_mechanics.map_generic_oidc,
            upsert_identity=substrate.upsert,
            record_login=substrate.record_login,
            handoffs=substrate.handoffs,
            request_id=REQUEST_ID,
            transport=httpx.MockTransport(handler),
        )
        assert idp.userinfo_endpoint not in seen

    def test_a_userinfo_profile_against_a_provider_without_the_endpoint_cannot_be_built(self, idp: FakeIdP) -> None:
        endpoints = _endpoints(idp)._replace(userinfo_endpoint=None)
        with pytest.raises(ValueError, match="requires userinfo"):
            SsoClient(
                provider="vanguard",
                client_id=idp.client_id,
                client_secret=idp.client_secret,
                redirect_uri=REDIRECT,
                transaction_secret=SECRET,
                public_base_url=PUBLIC_BASE,
                endpoints=endpoints,
                id_token_algorithms=("RS256",),
                userinfo=True,
            )

    def test_the_client_repr_hides_both_secrets(self, idp: FakeIdP) -> None:
        text = repr(_client(idp))
        assert idp.client_secret not in text and SECRET not in text


# ==========================================================================
# admit, and the redirect vocabulary.
# ==========================================================================


class TestAdmitAndLocations:
    def test_admit_passes_only_active(self) -> None:
        admit(_Identity("i", "u", "active"))
        with pytest.raises(SsoIdentityDisabled):
            admit(_Identity("i", "u", "disabled"))
        with pytest.raises(SsoAccessPending):
            admit(_Identity("i", "u", "pending"))
        with pytest.raises(AuthenticationError):
            admit(_Identity("i", "u", ""))

    def test_handoff_and_failure_share_one_spa_route_in_the_fragment(self) -> None:
        assert handoff_location(PUBLIC_BASE, "abc") == f"{PUBLIC_BASE}/#/auth/callback?code=abc"
        assert handoff_location(PUBLIC_BASE + "/", "abc") == f"{PUBLIC_BASE}/#/auth/callback?code=abc"
        assert failure_location(PUBLIC_BASE, "sso_state_mismatch") == f"{PUBLIC_BASE}/#/auth/callback?error=sso_state_mismatch"

    def test_a_failure_location_refuses_anything_outside_the_closed_set(self) -> None:
        with pytest.raises(ValueError):
            failure_location(PUBLIC_BASE, "access_denied: <script>")
        for category in SSO_FAILURE_CATEGORIES:
            assert failure_location(PUBLIC_BASE, category).endswith(f"error={category}")

    def test_failure_category_is_total_over_what_the_route_catches(self) -> None:
        assert failure_category(SsoStateMismatch()) == "sso_state_mismatch"
        assert failure_category(SsoIdpError(reason="other")) == "sso_idp_error"
        assert failure_category(AuthProviderUnavailable()) == "provider_unavailable"
        assert failure_category(AuthProviderUnavailable()) in SSO_FAILURE_CATEGORIES
