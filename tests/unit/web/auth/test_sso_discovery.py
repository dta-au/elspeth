"""Endpoint resolution: discovery, the break-glass override, and ``start``.

Every URL a login walk uses is remote-supplied. An IdP that is impersonated,
compromised, or merely proxied by something misbehaving can put any string in
a discovery document, and the four endpoints it names are where ELSPETH sends
the user's browser, its client secret, and its trust in a signature.

The parse is tested separately from the fetch, because the parse is where the
decisions are and it is pure — the adversarial cases need no network and no
fake server standing between the assertion and the thing asserted.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from elspeth.web.auth.sso import (
    AuthorizationRedirect,
    SsoCookieInvalid,
    SsoDiscoveryFailed,
    authorization_redirect,
    configured_endpoint_override,
    discovery_endpoints,
    fetch_discovery_endpoints,
    open_transaction,
    pkce_challenge,
)
from elspeth.web.auth.urls import DiscoveredEndpoints

ISSUER = "https://idp.example.gov.au"
ORIGINS = frozenset({ISSUER})
SECRET = "an-operator-transaction-secret-of-adequate-length-0123456789"
REDIRECT = "https://elspeth.example.gov.au/api/auth/sso/callback"


def _document(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/keys",
        "userinfo_endpoint": f"{ISSUER}/userinfo",
    }
    base.update(overrides)
    return base


# ==========================================================================
# The parse.
# ==========================================================================


def test_a_well_formed_document_yields_all_four_endpoints() -> None:
    """Positive control: without it every refusal below is trivially true."""
    result = discovery_endpoints(_document(), issuer=ISSUER, expected_origins=ORIGINS)
    assert result == DiscoveredEndpoints(
        authorization_endpoint=f"{ISSUER}/authorize",
        token_endpoint=f"{ISSUER}/token",
        jwks_uri=f"{ISSUER}/keys",
        userinfo_endpoint=f"{ISSUER}/userinfo",
    )


def test_a_provider_publishing_no_userinfo_is_accepted() -> None:
    """Profiles that read every claim from the ID token never call it."""
    document = _document()
    del document["userinfo_endpoint"]
    assert discovery_endpoints(document, issuer=ISSUER, expected_origins=ORIGINS).userinfo_endpoint is None


@pytest.mark.parametrize(
    "document_issuer",
    [
        f"{ISSUER}/",
        ISSUER.upper(),
        "https://idp.example.gov.au.evil.example",
        "https://other-idp.example.gov.au",
        "",
    ],
)
def test_the_issuer_check_is_exact(document_issuer: str) -> None:
    """A document whose issuer differs is REFUSED, never reconciled.

    Without this an IdP that can serve the discovery URL hands back another
    provider's endpoints and relocates the whole login walk — after which
    every later check is applied faithfully to the wrong provider. A trailing
    slash and a case change are included because those are the two a
    well-meaning normaliser would have papered over.
    """
    with pytest.raises(SsoDiscoveryFailed, match="exact issuer check"):
        discovery_endpoints(_document(issuer=document_issuer), issuer=ISSUER, expected_origins=ORIGINS)


def test_a_missing_issuer_is_refused() -> None:
    document = _document()
    del document["issuer"]
    with pytest.raises(SsoDiscoveryFailed, match="exact issuer check"):
        discovery_endpoints(document, issuer=ISSUER, expected_origins=ORIGINS)


def test_neither_issuer_is_echoed_into_the_refusal() -> None:
    """One is remote-supplied; the other names the deployment's IdP."""
    hostile = "https://attacker-chosen-issuer.example.net"
    with pytest.raises(SsoDiscoveryFailed) as raised:
        discovery_endpoints(_document(issuer=hostile), issuer=ISSUER, expected_origins=ORIGINS)
    assert "attacker-chosen-issuer" not in str(raised.value)
    assert "idp.example.gov.au" not in str(raised.value)


@pytest.mark.parametrize("document", [None, [], "a string", 42, True])
def test_a_document_that_is_not_an_object_is_refused(document: object) -> None:
    """JSON-valid payloads with the wrong top-level shape must not reach a
    subscript and escape as a 500."""
    with pytest.raises(SsoDiscoveryFailed, match="not a JSON object"):
        discovery_endpoints(document, issuer=ISSUER, expected_origins=ORIGINS)


@pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
def test_a_missing_required_endpoint_is_fatal(field: str) -> None:
    document = _document()
    del document[field]
    with pytest.raises(SsoDiscoveryFailed, match=f"missing a usable '{field}'"):
        discovery_endpoints(document, issuer=ISSUER, expected_origins=ORIGINS)


@pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"])
@pytest.mark.parametrize("bad", [42, [], {}, "", "   ", True])
def test_a_wrong_typed_endpoint_is_fatal_even_when_optional(field: str, bad: object) -> None:
    """A document that puts an object where a URL belongs is not one to take
    the rest of on trust — including for the optional endpoint."""
    with pytest.raises(SsoDiscoveryFailed, match=f"'{field}' is not a non-empty string"):
        discovery_endpoints(_document(**{field: bad}), issuer=ISSUER, expected_origins=ORIGINS)


@pytest.mark.parametrize("field", ["authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"])
def test_an_endpoint_off_the_expected_origins_is_refused(field: str) -> None:
    """The origin policy reaches every endpoint, not only the browser pair.

    ``jwks_uri`` is the worst one to lose: it supplies the keys every
    signature is then verified against.
    """
    with pytest.raises(SsoDiscoveryFailed, match=f"{field} failed expected-origin check"):
        discovery_endpoints(
            _document(**{field: "https://attacker.example.net/path"}),
            issuer=ISSUER,
            expected_origins=ORIGINS,
        )


def test_a_valid_issuer_does_not_excuse_a_hostile_endpoint() -> None:
    """The two checks are independent. A compromised IdP serves a document
    with its OWN correct issuer and someone else's token endpoint."""
    with pytest.raises(SsoDiscoveryFailed, match="token_endpoint failed expected-origin check"):
        discovery_endpoints(
            _document(token_endpoint="https://attacker.example.net/token"),
            issuer=ISSUER,
            expected_origins=ORIGINS,
        )


# ==========================================================================
# The fetch.
# ==========================================================================


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_a_served_document_is_fetched_and_validated() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_document())

    result = await fetch_discovery_endpoints(issuer=ISSUER, expected_origins=ORIGINS, transport=_transport(handler))

    assert result.token_endpoint == f"{ISSUER}/token"
    assert seen == [f"{ISSUER}/.well-known/openid-configuration"]


@pytest.mark.asyncio
async def test_a_redirect_is_not_followed() -> None:
    """THE load-bearing flag. Following a redirect lets the response move the
    document off the origin the issuer names, after which every endpoint in it
    is attacker-chosen and the origin policy was applied to a URL nobody
    fetched."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if ".well-known" in str(request.url):
            return httpx.Response(302, headers={"location": "https://attacker.example.net/openid-configuration"})
        return httpx.Response(200, json=_document(issuer="https://attacker.example.net"))

    with pytest.raises(SsoDiscoveryFailed):
        await fetch_discovery_endpoints(issuer=ISSUER, expected_origins=ORIGINS, transport=_transport(handler))

    assert len(requested) == 1, "the redirect target must never be requested"
    assert "attacker.example.net" not in requested[0]


@pytest.mark.asyncio
async def test_an_oversized_body_is_refused_before_parsing() -> None:
    """A discovery document is a handful of URLs. Anything far past that is
    an attempt to make JSON parsing the expensive part of an unauthenticated
    request."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"padding":"' + b"x" * (300 * 1024) + b'"}')

    with pytest.raises(SsoDiscoveryFailed, match="maximum accepted size"):
        await fetch_discovery_endpoints(issuer=ISSUER, expected_origins=ORIGINS, transport=_transport(handler))


@pytest.mark.asyncio
async def test_a_non_json_body_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    with pytest.raises(SsoDiscoveryFailed, match="not valid JSON"):
        await fetch_discovery_endpoints(issuer=ISSUER, expected_origins=ORIGINS, transport=_transport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
async def test_an_http_error_is_a_login_failure_not_a_crash(status: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    with pytest.raises(SsoDiscoveryFailed, match="discovery request failed"):
        await fetch_discovery_endpoints(issuer=ISSUER, expected_origins=ORIGINS, transport=_transport(handler))


@pytest.mark.asyncio
async def test_a_transport_failure_does_not_leak_the_resolved_address() -> None:
    """``str(exc)`` on a connect error can carry the IdP's resolved IP."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("failed to connect to 10.1.2.3:443")

    with pytest.raises(SsoDiscoveryFailed) as raised:
        await fetch_discovery_endpoints(issuer=ISSUER, expected_origins=ORIGINS, transport=_transport(handler))
    assert "10.1.2.3" not in str(raised.value)
    assert "ConnectError" in str(raised.value)


@pytest.mark.asyncio
async def test_a_trailing_slash_issuer_still_meets_the_exact_check() -> None:
    """The fetch normalises the issuer for the URL and NOT for the check.

    ``{issuer}/`` and ``{issuer}`` name the same well-known document, so the
    request is built from the stripped form. The comparison is deliberately
    not stripped: an IdP publishing an issuer that differs from the configured
    one by a slash is a real mismatch, and the ID tokens it signs will carry
    the form it published. ``validate_oidc_issuer`` already strips the
    configured value, so this is unreachable through settings — which is why
    it is pinned here rather than left to be rediscovered as a "harmless"
    normalisation someone adds to make an error go away.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_document())

    with pytest.raises(SsoDiscoveryFailed, match="exact issuer check"):
        await fetch_discovery_endpoints(issuer=f"{ISSUER}/", expected_origins=ORIGINS, transport=_transport(handler))

    assert seen == [f"{ISSUER}/.well-known/openid-configuration"], "the path must not be doubled"


# ==========================================================================
# The break-glass override.
# ==========================================================================


class _Settings:
    def __init__(self, **values: str | None) -> None:
        self.sso_authorization_endpoint = values.get("sso_authorization_endpoint")
        self.sso_token_endpoint = values.get("sso_token_endpoint")
        self.sso_jwks_uri = values.get("sso_jwks_uri")
        self.sso_userinfo_endpoint = values.get("sso_userinfo_endpoint")


def test_no_override_configured_means_use_discovery() -> None:
    assert configured_endpoint_override(_Settings()) is None


def test_a_full_override_replaces_discovery() -> None:
    override = configured_endpoint_override(
        _Settings(
            sso_authorization_endpoint=f"{ISSUER}/a",
            sso_token_endpoint=f"{ISSUER}/t",
            sso_jwks_uri=f"{ISSUER}/k",
            sso_userinfo_endpoint=f"{ISSUER}/u",
        )
    )
    assert override is not None
    assert override.token_endpoint == f"{ISSUER}/t"


@pytest.mark.parametrize("omitted", ["sso_authorization_endpoint", "sso_token_endpoint", "sso_jwks_uri"])
def test_a_partial_override_falls_back_rather_than_half_applying(omitted: str) -> None:
    """Settings validation already refuses a partial override, so this can
    only be reached by constructing one directly. It returns None rather than
    a half-filled record: silently mixing operator-supplied and discovered
    endpoints is the outcome the all-or-none rule exists to prevent."""
    values = {
        "sso_authorization_endpoint": f"{ISSUER}/a",
        "sso_token_endpoint": f"{ISSUER}/t",
        "sso_jwks_uri": f"{ISSUER}/k",
    }
    values.pop(omitted)
    assert configured_endpoint_override(_Settings(**values)) is None


# ==========================================================================
# start.
# ==========================================================================


def _redirect(**overrides: Any) -> AuthorizationRedirect:
    kwargs: dict[str, Any] = {
        "authorization_endpoint": f"{ISSUER}/authorize",
        "client_id": "elspeth",
        "redirect_uri": REDIRECT,
        "scopes": ("openid", "profile", "email"),
        "transaction_secret": SECRET,
        "provider": "vanguard",
    }
    kwargs.update(overrides)
    return authorization_redirect(**kwargs)


def _params(location: str) -> dict[str, str]:
    return dict(httpx.URL(location).params)


def test_the_redirect_carries_everything_the_idp_needs() -> None:
    params = _params(_redirect().location)
    assert params["response_type"] == "code"
    assert params["client_id"] == "elspeth"
    assert params["redirect_uri"] == REDIRECT
    assert params["scope"] == "openid profile email"
    assert params["code_challenge_method"] == "S256"


def test_pkce_is_s256_and_the_verifier_never_travels() -> None:
    """With ``plain`` the verifier would sit in the URL the browser follows,
    and therefore in its history and in every log along the way."""
    result = _redirect()
    transaction = open_transaction(result.cookie_value, secret=SECRET, provider="vanguard", redirect_uri=REDIRECT)

    assert _params(result.location)["code_challenge"] == pkce_challenge(transaction.verifier)
    assert transaction.verifier not in result.location


def test_the_cookie_remembers_exactly_the_state_and_nonce_that_were_sent() -> None:
    """Sealing them separately would let a walk proceed with a matching state
    and a nonce from some other login."""
    result = _redirect()
    params = _params(result.location)
    transaction = open_transaction(result.cookie_value, secret=SECRET, provider="vanguard", redirect_uri=REDIRECT)

    assert params["state"] == transaction.state
    assert params["nonce"] == transaction.nonce


def test_two_starts_share_nothing() -> None:
    first, second = _redirect(), _redirect()
    assert _params(first.location)["state"] != _params(second.location)["state"]
    assert _params(first.location)["nonce"] != _params(second.location)["nonce"]
    assert first.cookie_value != second.cookie_value


def test_the_cookie_is_bound_to_this_redirect_uri() -> None:
    """A staging cookie must not open against production, even sharing a key."""
    result = _redirect()
    with pytest.raises(SsoCookieInvalid):
        open_transaction(
            result.cookie_value,
            secret=SECRET,
            provider="vanguard",
            redirect_uri="https://staging.example.gov.au/api/auth/sso/callback",
        )


def test_the_sealed_transaction_is_fresh() -> None:
    """A cookie minted at start must not already be near its own expiry."""
    before = int(time.time())
    transaction = open_transaction(_redirect().cookie_value, secret=SECRET, provider="vanguard", redirect_uri=REDIRECT)

    assert before <= transaction.issued_at <= int(time.time())
