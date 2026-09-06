"""Closed-origin validation for browser-facing OIDC endpoints."""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.web.auth.urls import (
    DiscoveredEndpoints,
    https_url_origin,
    validate_discovered_endpoints,
    validate_oidc_browser_endpoints,
)

ISSUER = "https://cognito-idp.ap-southeast-2.amazonaws.com/pool-id"
COGNITO_ORIGIN = "https://example.auth.ap-southeast-2.amazoncognito.com"
AUTHORIZATION_ENDPOINT = f"{COGNITO_ORIGIN}/oauth2/authorize"
TOKEN_ENDPOINT = f"{COGNITO_ORIGIN}/oauth2/token"


def test_same_issuer_origin_pair_is_accepted_without_allowlist() -> None:
    pair = validate_oidc_browser_endpoints(
        "https://issuer.example.com/oauth2/authorize",
        "https://issuer.example.com/oauth2/token",
        issuer="https://issuer.example.com/pool",
    )
    assert pair == (
        "https://issuer.example.com/oauth2/authorize",
        "https://issuer.example.com/oauth2/token",
    )


def test_cross_origin_pair_is_refused_there_is_no_allowlist() -> None:
    """Cognito's hosted domain is off the pool issuer's origin. The legacy
    browser path no longer carries an allowlist for that; the SSO profile's
    ``sso_endpoint_origins`` serves it."""
    with pytest.raises(ValueError, match="browser endpoint origin is not allowed"):
        validate_oidc_browser_endpoints(AUTHORIZATION_ENDPOINT, TOKEN_ENDPOINT, issuer=ISSUER)


@pytest.mark.parametrize(
    ("authorization_endpoint", "token_endpoint"),
    [
        ("http://issuer.example.com/authorize", "https://issuer.example.com/token"),
        ("https://issuer.example.com/authorize", "http://issuer.example.com/token"),
        ("", "https://issuer.example.com/token"),
        ("https://issuer.example.com/authorize\n", "https://issuer.example.com/token"),
        (r"https:\\issuer.example.com\authorize", "https://issuer.example.com/token"),
        ("https://issuer.example.com/%zz", "https://issuer.example.com/token"),
        ("https://issuer.example.com:bad/authorize", "https://issuer.example.com/token"),
        ("https://issuer.example.com:0/authorize", "https://issuer.example.com:0/token"),
        ("https://user:password@issuer.example.com/authorize", "https://issuer.example.com/token"),
        ("https://issuer.example.com/authorize?code=secret", "https://issuer.example.com/token"),
        ("https://issuer.example.com/authorize#secret", "https://issuer.example.com/token"),
        ("https://issuer.example.com/", "https://issuer.example.com/token"),
        ("https://issuer.example.com/authorize", "https://issuer.example.com"),
        ("https://127.0.0.1/authorize", "https://127.0.0.1/token"),
        ("https://169.254.169.254/authorize", "https://169.254.169.254/token"),
        ("https://10.0.0.1/authorize", "https://10.0.0.1/token"),
        ("https://*.example.com/authorize", "https://*.example.com/token"),
        ("https://issuer.example.com./authorize", "https://issuer.example.com./token"),
        ("https://bücher.example/authorize", "https://bücher.example/token"),
        ("https://bad_host.example/authorize", "https://bad_host.example/token"),
        ("https://-bad.example/authorize", "https://-bad.example/token"),
        ("https://bad..example/authorize", "https://bad..example/token"),
        ("https://127.1/authorize", "https://127.1/token"),
        ("https://0177.0.0.1/authorize", "https://0177.0.0.1/token"),
        ("https://0x7f000001/authorize", "https://0x7f000001/token"),
        ("https://2130706433/authorize", "https://2130706433/token"),
        ("https://[fe80::1%25eth0]/authorize", "https://[fe80::1%25eth0]/token"),
    ],
)
def test_adversarial_endpoint_values_fail_closed(
    authorization_endpoint: str,
    token_endpoint: str,
) -> None:
    with pytest.raises(ValueError) as raised:
        validate_oidc_browser_endpoints(
            authorization_endpoint,
            token_endpoint,
            issuer="https://issuer.example.com/pool",
        )
    rendered = str(raised.value)
    if authorization_endpoint:
        assert authorization_endpoint not in rendered
    if token_endpoint:
        assert token_endpoint not in rendered
    assert "password" not in rendered
    assert "secret" not in rendered


@pytest.mark.parametrize(
    "path",
    [
        "/.",
        "/..",
        "/%2e",
        "/%2E",
        "/.%2e",
        "/%2e.",
        "/%2e%2e",
        "/%2E%2e",
        "/oauth2/./authorize",
        "/oauth2/%2E%2e/authorize",
    ],
)
def test_endpoint_paths_reject_browser_normalized_dot_segments(path: str) -> None:
    endpoint = f"https://issuer.example.com{path}"
    with pytest.raises(ValueError, match="dot-segment") as raised:
        validate_oidc_browser_endpoints(
            endpoint,
            "https://issuer.example.com/oauth2/token",
            issuer="https://issuer.example.com/pool",
        )
    assert endpoint not in str(raised.value)


def test_endpoint_paths_preserve_ordinary_non_root_segments() -> None:
    assert validate_oidc_browser_endpoints(
        "https://issuer.example.com/.well-known/authorize",
        "https://issuer.example.com/oauth2/token.name",
        issuer="https://issuer.example.com/pool",
    ) == (
        "https://issuer.example.com/.well-known/authorize",
        "https://issuer.example.com/oauth2/token.name",
    )


@pytest.mark.parametrize(
    "issuer_origin",
    [
        "https://sibling.auth.ap-southeast-2.amazoncognito.com",
        "https://auth.ap-southeast-2.amazoncognito.com",
        "https://evil-example.auth.ap-southeast-2.amazoncognito.com",
        f"{COGNITO_ORIGIN}:444",
        "https://xn--bcher-kva.example",
    ],
)
def test_origin_equality_does_not_use_suffix_or_similarity(issuer_origin: str) -> None:
    """The issuer's origin is the only origin the pair may use, compared exactly."""
    with pytest.raises(ValueError, match="browser endpoint origin is not allowed"):
        validate_oidc_browser_endpoints(AUTHORIZATION_ENDPOINT, TOKEN_ENDPOINT, issuer=f"{issuer_origin}/pool")


def test_default_port_and_mixed_host_case_compare_by_normalized_origin() -> None:
    assert validate_oidc_browser_endpoints(
        "https://EXAMPLE.AUTH.ap-southeast-2.amazoncognito.com:443/oauth2/authorize",
        "https://example.auth.ap-southeast-2.amazoncognito.com/oauth2/token",
        issuer=f"{COGNITO_ORIGIN}/pool",
    )[0].startswith("https://EXAMPLE.AUTH")


def test_nondefault_port_must_match_the_issuer_and_both_endpoints() -> None:
    with pytest.raises(ValueError, match="same origin"):
        validate_oidc_browser_endpoints(
            f"{COGNITO_ORIGIN}:8443/oauth2/authorize",
            f"{COGNITO_ORIGIN}/oauth2/token",
            issuer=f"{COGNITO_ORIGIN}:8443/pool",
        )
    assert validate_oidc_browser_endpoints(
        f"{COGNITO_ORIGIN}:8443/oauth2/authorize",
        f"{COGNITO_ORIGIN}:8443/oauth2/token",
        issuer=f"{COGNITO_ORIGIN}:8443/pool",
    )


def test_public_ipv6_literal_compares_using_canonical_address() -> None:
    assert validate_oidc_browser_endpoints(
        "https://[2606:4700:4700::1111]/authorize",
        "https://[2606:4700:4700:0:0:0:0:1111]:443/token",
        issuer="https://[2606:4700:4700::1111]/pool",
    )


def test_authorization_and_token_endpoint_origins_must_match() -> None:
    with pytest.raises(ValueError, match="same origin"):
        validate_oidc_browser_endpoints(
            AUTHORIZATION_ENDPOINT,
            "https://other.auth.ap-southeast-2.amazoncognito.com/oauth2/token",
            issuer=f"{COGNITO_ORIGIN}/pool",
        )


@pytest.mark.parametrize(
    "smuggled",
    [
        f"https://evil.example/path/{COGNITO_ORIGIN}/oauth2/authorize",
        f"https://evil.example/authorize?next={COGNITO_ORIGIN}",
        f"https://evil.example/{COGNITO_ORIGIN.replace('/', '%2f')}",
        "https://evil.example/authorize%3fnext%3dhttps%3a%2f%2fexample.com",
    ],
)
def test_embedding_allowed_url_does_not_authorize_initial_destination(smuggled: str) -> None:
    with pytest.raises(ValueError):
        validate_oidc_browser_endpoints(smuggled, TOKEN_ENDPOINT, issuer=f"{COGNITO_ORIGIN}/pool")


class _LyingStr(str):
    """A ``str`` subclass whose scanning methods deny every separator it carries."""

    consulted = False

    def __contains__(self, item: object) -> bool:
        type(self).consulted = True
        return False

    def find(self, *args: Any, **kwargs: Any) -> int:
        type(self).consulted = True
        return -1


@pytest.mark.parametrize(
    "impostor",
    [
        pytest.param(_LyingStr("https://allowed.example.com\\@evil.example"), id="str-subclass"),
        pytest.param(b"https://allowed.example.com", id="bytes"),
        pytest.param(None, id="none"),
    ],
)
def test_url_values_must_be_exact_str_not_a_subclass_or_lookalike(impostor: Any) -> None:
    _LyingStr.consulted = False
    with pytest.raises(ValueError, match="issuer failed string check"):
        https_url_origin(impostor)
    assert _LyingStr.consulted is False


class TestHttpsUrlOrigin:
    """``https_url_origin`` runs the full parse, not a convenience split.

    It exists because an ISSUER may legitimately have no path, which
    ``oidc_browser_endpoint_origin`` refuses. The risk in adding it is that
    someone reaches for ``urlsplit`` instead and skips every check the module
    exists for, so these pin that it did not.
    """

    def test_it_returns_the_origin_with_and_without_a_path(self) -> None:
        assert https_url_origin("https://accounts.google.com") == "https://accounts.google.com"
        assert https_url_origin("https://issuer.example.gov.au/realms/x") == "https://issuer.example.gov.au"

    def test_the_default_https_port_is_canonicalised_away(self) -> None:
        assert https_url_origin("https://issuer.example.gov.au:443/x") == "https://issuer.example.gov.au"

    def test_a_non_default_port_is_part_of_the_origin(self) -> None:
        assert https_url_origin("https://issuer.example.gov.au:8443/x") == "https://issuer.example.gov.au:8443"

    @pytest.mark.parametrize(
        "hostile",
        [
            "http://issuer.example.gov.au",
            "https://user:pass@issuer.example.gov.au",
            "https://issuer.example.gov.au\\@evil.example",
            "https://evil.example/%2f..%2f",
            "https://",
            "   ",
            "https://issuer.example.gov.au\x00",
        ],
    )
    def test_it_refuses_what_the_endpoint_parser_refuses(self, hostile: str) -> None:
        with pytest.raises(ValueError):
            https_url_origin(hostile)


# ==========================================================================
# validate_discovered_endpoints — the generalised policy.
#
# Discovery documents come from a remote IdP, so every URL in one is
# attacker-reachable if that IdP is impersonated or compromised. These tests
# assert two things the rule this replaced did NOT: that all four endpoints
# are checked (not just the authorization/token pair), and that the issuer's
# own origin carries no privilege of its own.
# ==========================================================================

_SSO_ORIGIN = "https://login.example.gov.au"
_TOKEN_ORIGIN = "https://oauth2.example.gov.au"


def _endpoints(**overrides: Any) -> DiscoveredEndpoints:
    base = {
        "authorization_endpoint": f"{_SSO_ORIGIN}/authorize",
        "token_endpoint": f"{_SSO_ORIGIN}/token",
        "jwks_uri": f"{_SSO_ORIGIN}/keys",
        "userinfo_endpoint": f"{_SSO_ORIGIN}/userinfo",
    }
    base.update(overrides)
    return DiscoveredEndpoints(**base)


# --- positive controls ----------------------------------------------------


def test_every_endpoint_on_an_expected_origin_is_accepted() -> None:
    """Without this the refusals below would all be trivially true."""
    result = validate_discovered_endpoints(_endpoints(), expected_origins=frozenset({_SSO_ORIGIN}))
    assert result == _endpoints()


def test_a_provider_without_userinfo_is_not_a_misconfiguration() -> None:
    """Profiles that take every claim from the ID token never call userinfo."""
    result = validate_discovered_endpoints(
        _endpoints(userinfo_endpoint=None),
        expected_origins=frozenset({_SSO_ORIGIN}),
    )
    assert result.userinfo_endpoint is None


def test_endpoints_may_sit_on_DIFFERENT_expected_origins() -> None:
    """The deliberate relaxation, and the reason the old rule was wrong.

    Google serves authorization from accounts.google.com and tokens from
    oauth2.googleapis.com. The replaced rule required the pair to agree, which
    refuses a correct deployment; the closed set is the control, and agreement
    between endpoints was only ever a proxy for it.
    """
    result = validate_discovered_endpoints(
        _endpoints(token_endpoint=f"{_TOKEN_ORIGIN}/token"),
        expected_origins=frozenset({_SSO_ORIGIN, _TOKEN_ORIGIN}),
    )
    assert result.token_endpoint == f"{_TOKEN_ORIGIN}/token"


# --- the case the replaced rule accepted ----------------------------------


def test_the_issuers_own_origin_carries_no_privilege() -> None:
    """THE discriminating case. An endpoint served from the issuer's own
    origin is REFUSED when the profile does not expect that origin.

    The replaced rule was "same origin as the issuer, or in the operator
    allowlist", so it accepted exactly this. Origin policy belongs to the
    identity provider, not to whichever host happens to publish the issuer
    string, and no shipped profile exhibits the divergence today — which is
    why this is tested against a synthetic expected_origins rather than
    through a profile, where it would have proven nothing.
    """
    issuer_origin = "https://issuer.example.gov.au"
    with pytest.raises(ValueError, match="authorization_endpoint failed expected-origin check"):
        validate_discovered_endpoints(
            _endpoints(authorization_endpoint=f"{issuer_origin}/authorize"),
            expected_origins=frozenset({_SSO_ORIGIN}),
        )


def test_no_expected_origins_refuses_everything() -> None:
    """Fail closed. A profile that computed no origins has authorised none,
    and reading "no constraint recorded" as "no constraint" is how an origin
    check stops being a check."""
    with pytest.raises(ValueError, match="failed expected-origin check"):
        validate_discovered_endpoints(_endpoints(), expected_origins=frozenset())


# --- all four are checked, not just the pair ------------------------------


@pytest.mark.parametrize(
    "field",
    ["authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"],
)
def test_each_endpoint_is_origin_checked_individually(field: str) -> None:
    """jwks_uri and userinfo were outside the replaced rule entirely.

    A jwks_uri on an attacker's origin is the worst of the four: it supplies
    the keys every signature is then verified against.
    """
    with pytest.raises(ValueError, match=f"{field} failed expected-origin check"):
        validate_discovered_endpoints(
            _endpoints(**{field: "https://attacker.example.net/path"}),
            expected_origins=frozenset({_SSO_ORIGIN}),
        )


@pytest.mark.parametrize(
    "field",
    ["authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"],
)
@pytest.mark.parametrize(
    ("bad_value", "check"),
    [
        ("http://login.example.gov.au/x", "HTTPS"),
        ("https://user:pw@login.example.gov.au/x", "no-credentials"),
        ("https://login.example.gov.au", "non-root-path"),
        ("https://login.example.gov.au/", "non-root-path"),
        ("https://login.example.gov.au/x?a=1", "no-query-or-fragment"),
        ("https://login.example.gov.au/x#f", "no-query-or-fragment"),
        ("https://login.example.gov.au/../x", "dot-segment"),
        ("https://login.example.gov.au/%2fx", "encoded-separator"),
        ("https://login.example.gov.au\\@evil.example/x", "browser-parser-equivalence"),
        ("https://127.0.0.1/x", "public-literal-IP"),
        ("https://0177.0.0.1/x", "browser-host-equivalence"),
        ("https://login.example.gov.au./x", "canonical-host"),
    ],
)
def test_every_ssrf_check_still_applies_to_every_endpoint(field: str, bad_value: str, check: str) -> None:
    """The SSRF checks are KEPT by the generalisation, not traded for it.

    Each one is asserted against each of the four endpoints, because the
    generalisation's whole risk is that a URL reaches a fetch through the one
    parameter someone forgot to route through the same parse.
    """
    with pytest.raises(ValueError, match=f"{field} failed {check} check"):
        validate_discovered_endpoints(
            _endpoints(**{field: bad_value}),
            expected_origins=frozenset({_SSO_ORIGIN}),
        )


def test_a_refusal_never_echoes_the_remote_host() -> None:
    """The rejected URL came from a remote document. Putting its host into an
    error string puts attacker-chosen text into logs and error pages."""
    attacker = "https://attacker-chosen-host.example.net/path"
    with pytest.raises(ValueError) as raised:
        validate_discovered_endpoints(
            _endpoints(authorization_endpoint=attacker),
            expected_origins=frozenset({_SSO_ORIGIN}),
        )
    assert "attacker-chosen-host" not in str(raised.value)


@pytest.mark.parametrize(
    ("bad_origin", "check"),
    [
        ("http://login.example.gov.au", "HTTPS"),
        # A bare ORIGIN is scheme+host+port and nothing else. Accepting one
        # with a path and quietly keeping only its origin would hide a profile
        # bug rather than report it — and the value silently used would not be
        # the value the profile computed. Found by the guard-integrity sweep:
        # the scheme case alone left this uncovered.
        ("https://login.example.gov.au/authorize", "bare-origin"),
        ("https://login.example.gov.au?a=1", "bare-origin"),
        ("https://login.example.gov.au#f", "bare-origin"),
    ],
)
def test_a_malformed_expected_origin_is_refused_rather_than_ignored(bad_origin: str, check: str) -> None:
    """A profile that computed a bad origin must not silently contribute
    nothing to the closed set — that would widen it by removing a member."""
    with pytest.raises(ValueError, match=f"expected origin failed {check} check"):
        validate_discovered_endpoints(_endpoints(), expected_origins=frozenset({bad_origin}))
