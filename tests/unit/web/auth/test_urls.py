"""URL validation for the OIDC endpoints ELSPETH itself contacts.

Two entry points survive identity sprint step E, and between them they carry
every check this module exists for:

* ``validate_discovered_endpoints`` — the four endpoints a discovery document
  supplies, each put through the full parse and then required to sit on an
  origin the PROFILE expects;
* ``https_url_origin`` — the origin of one HTTPS URL, running the same parse,
  for the per-profile origin policy that needs the issuer's own origin.

``validate_oidc_browser_endpoints`` and ``oidc_browser_endpoint_origin`` were
deleted with the legacy bearer path they served: no endpoint is handed to the
browser any more, so there is nothing left for a browser-endpoint rule to
validate. The SSRF and parser-equivalence checks that path exercised are NOT
gone — they live in the shared parse, and the cases below assert each of them
against the entry points that remain.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.web.auth.urls import (
    DiscoveredEndpoints,
    https_url_origin,
    validate_discovered_endpoints,
)


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

    It exists because an ISSUER may legitimately have no path, which the
    endpoint parse deliberately refuses — an endpoint with no path is a
    misconfiguration, an issuer with no path is ``https://accounts.google.com``.
    The risk in having a second entry point is that someone reaches for
    ``urlsplit`` instead and skips every check the module exists for, so these
    pin that it did not.
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


def _on_origin(origin: str) -> DiscoveredEndpoints:
    """All four endpoints on one origin, for the origin-matching cases."""
    return DiscoveredEndpoints(
        authorization_endpoint=f"{origin}/authorize",
        token_endpoint=f"{origin}/token",
        jwks_uri=f"{origin}/keys",
        userinfo_endpoint=f"{origin}/userinfo",
    )


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


def test_ordinary_non_root_paths_survive_the_parse_unchanged() -> None:
    """The dot-segment and encoded-separator rules must not eat legitimate paths.

    A leading-dot segment (``.well-known``) and a dotted final segment are
    both ordinary, and a refusal rule that could not tell them from ``/..``
    would make a correct provider unconfigurable.
    """
    result = validate_discovered_endpoints(
        _endpoints(
            authorization_endpoint=f"{_SSO_ORIGIN}/.well-known/authorize",
            token_endpoint=f"{_SSO_ORIGIN}/oauth2/token.name",
        ),
        expected_origins=frozenset({_SSO_ORIGIN}),
    )
    assert result.authorization_endpoint == f"{_SSO_ORIGIN}/.well-known/authorize"
    assert result.token_endpoint == f"{_SSO_ORIGIN}/oauth2/token.name"


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


# --- origin membership is exact -------------------------------------------


@pytest.mark.parametrize(
    "expected_origin",
    [
        "https://sibling.login.example.gov.au",
        "https://example.gov.au",
        "https://evil-login.example.gov.au",
        f"{_SSO_ORIGIN}:444",
        "https://xn--bcher-kva.example",
    ],
)
def test_expected_origin_membership_is_exact_not_suffix_or_similarity(expected_origin: str) -> None:
    """A parent domain, a sibling subdomain, a prefix-extended label and a
    different port are all DIFFERENT origins.

    Membership is a tuple comparison of scheme, canonical host and effective
    port, so none of these admits an endpoint on ``_SSO_ORIGIN``. Any check
    that compared by suffix or containment would accept the first two.
    """
    with pytest.raises(ValueError, match="failed expected-origin check"):
        validate_discovered_endpoints(_endpoints(), expected_origins=frozenset({expected_origin}))


def test_default_port_and_mixed_host_case_compare_by_normalized_origin() -> None:
    """Comparison is by canonical origin; the endpoint keeps the bytes it arrived with.

    ``:443`` is the default and drops out, and hosts are case-insensitive, so
    this endpoint IS on the expected origin. What comes back is the original
    string, because the value ELSPETH later fetches must be the value the IdP
    published, not a reconstruction of it.
    """
    result = validate_discovered_endpoints(
        _on_origin("https://LOGIN.EXAMPLE.GOV.AU:443"),
        expected_origins=frozenset({_SSO_ORIGIN}),
    )
    assert result.authorization_endpoint == "https://LOGIN.EXAMPLE.GOV.AU:443/authorize"


def test_a_non_default_port_is_part_of_the_origin() -> None:
    """``:8443`` is a different origin from the default-port one, both ways."""
    with pytest.raises(ValueError, match="failed expected-origin check"):
        validate_discovered_endpoints(_on_origin(f"{_SSO_ORIGIN}:8443"), expected_origins=frozenset({_SSO_ORIGIN}))
    accepted = validate_discovered_endpoints(
        _on_origin(f"{_SSO_ORIGIN}:8443"),
        expected_origins=frozenset({f"{_SSO_ORIGIN}:8443"}),
    )
    assert accepted.authorization_endpoint == f"{_SSO_ORIGIN}:8443/authorize"


def test_public_ipv6_literal_compares_using_canonical_address() -> None:
    """One address has many spellings; the comparison must not be textual."""
    accepted = validate_discovered_endpoints(
        _on_origin("https://[2606:4700:4700:0:0:0:0:1111]:443"),
        expected_origins=frozenset({"https://[2606:4700:4700::1111]"}),
    )
    assert accepted.jwks_uri == "https://[2606:4700:4700:0:0:0:0:1111]:443/keys"


@pytest.mark.parametrize(
    "smuggled",
    [
        f"https://evil.example/path/{_SSO_ORIGIN}/authorize",
        f"https://evil.example/authorize?next={_SSO_ORIGIN}",
        f"https://evil.example/{_SSO_ORIGIN.replace('/', '%2f')}",
        "https://evil.example/authorize%3fnext%3dhttps%3a%2f%2fexample.com",
    ],
)
def test_embedding_an_allowed_url_does_not_authorize_the_initial_destination(smuggled: str) -> None:
    """The origin is where the request GOES, never a substring of where it points.

    A URL that merely mentions an expected origin in its path or query still
    resolves to the attacker's host, and an encoded separator is an attempt to
    make the two disagree about where the authority ends.
    """
    with pytest.raises(ValueError):
        validate_discovered_endpoints(
            _endpoints(authorization_endpoint=smuggled),
            expected_origins=frozenset({_SSO_ORIGIN}),
        )


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


# The full adversarial corpus, and the check each value must fail. Shared by
# the two tests below so that "it is refused" and "it is refused without
# echoing what was sent" are asserted over exactly the same inputs. Carried
# over from the deleted browser-endpoint rule, where the same parse ran: the
# checks did not move when that entry point went away, so neither did these.
_ADVERSARIAL_VALUES: list[tuple[str, str]] = [
    ("", "nonblank"),
    ("https://login.example.gov.au/x\n", "control-character"),
    ("http://login.example.gov.au/x", "HTTPS"),
    ("https://user:password@login.example.gov.au/x", "no-credentials"),
    ("https://login.example.gov.au", "non-root-path"),
    ("https://login.example.gov.au/", "non-root-path"),
    ("https://login.example.gov.au/x?code=secret", "no-query-or-fragment"),
    ("https://login.example.gov.au/x#secret", "no-query-or-fragment"),
    ("https://login.example.gov.au/../x", "dot-segment"),
    ("https://login.example.gov.au/%zz", "percent-encoding"),
    ("https://login.example.gov.au/%2fx", "encoded-separator"),
    ("https://login.example.gov.au\\@evil.example/x", "browser-parser-equivalence"),
    ("https://login.example.gov.au:bad/x", "valid-port"),
    ("https://login.example.gov.au:0/x", "valid-port"),
    # Literal addresses ELSPETH must never be talked into fetching: loopback,
    # the cloud metadata service, and RFC 1918 space.
    ("https://127.0.0.1/x", "public-literal-IP"),
    ("https://169.254.169.254/x", "public-literal-IP"),
    ("https://10.0.0.1/x", "public-literal-IP"),
    # Host spellings a browser resolves but a URL parser reads as a name. Each
    # is the same loopback address in a different legacy notation.
    ("https://0177.0.0.1/x", "browser-host-equivalence"),
    ("https://127.1/x", "browser-host-equivalence"),
    ("https://0x7f000001/x", "browser-host-equivalence"),
    ("https://2130706433/x", "browser-host-equivalence"),
    # Hosts that are not names at all. The IPv6 zone identifier lands on the
    # canonical-host rule rather than a zone-specific one: any '%' in a host
    # is refused before the address is parsed.
    ("https://login.example.gov.au./x", "canonical-host"),
    ("https://*.example.com/x", "canonical-host"),
    ("https://[fe80::1%25eth0]/x", "canonical-host"),
    ("https://bücher.example/x", "ASCII-host"),
    ("https://bad_host.example/x", "DNS-host"),
    ("https://-bad.example/x", "DNS-host"),
    ("https://bad..example/x", "DNS-host"),
]


@pytest.mark.parametrize(
    "field",
    ["authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"],
)
@pytest.mark.parametrize(("bad_value", "check"), _ADVERSARIAL_VALUES)
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


@pytest.mark.parametrize("bad_value", [value for value, _check in _ADVERSARIAL_VALUES])
def test_no_refusal_echoes_the_value_that_was_rejected(bad_value: str) -> None:
    """Every refusal message is static, for every value in the corpus.

    The rejected URL came from a remote document, so echoing it puts
    attacker-chosen text into logs and error pages. Two entries in the corpus
    carry a credential and a query parameter for exactly this test: an error
    string that quoted what it refused would leak them.
    """
    with pytest.raises(ValueError) as raised:
        validate_discovered_endpoints(
            _endpoints(authorization_endpoint=bad_value),
            expected_origins=frozenset({_SSO_ORIGIN}),
        )
    rendered = str(raised.value)
    if bad_value:
        assert bad_value not in rendered
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
    """A browser resolves these away before the request leaves; the check must not.

    Each spelling decodes to ``.`` or ``..``, so accepting one would let a
    discovery document name a path that arrives at the IdP as a different path
    from the one that was validated.
    """
    endpoint = f"{_SSO_ORIGIN}{path}"
    with pytest.raises(ValueError, match="dot-segment") as raised:
        validate_discovered_endpoints(
            _endpoints(authorization_endpoint=endpoint),
            expected_origins=frozenset({_SSO_ORIGIN}),
        )
    assert endpoint not in str(raised.value)


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
