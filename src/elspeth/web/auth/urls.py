"""URL validation helpers for browser-facing OIDC auth flows."""

from __future__ import annotations

import ipaddress
import re
from typing import NamedTuple
from urllib.parse import SplitResult, unquote_to_bytes, urlsplit

from elspeth.core.security import SSRFBlockedError, validate_literal_ip_for_ssrf

_HTTPS_DEFAULT_PORT = 443
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)
_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_ENCODED_URL_SEPARATOR = re.compile(r"%(?:2f|5c|3f|23|40)", re.IGNORECASE)


class _Origin(NamedTuple):
    scheme: str
    host: str
    port: int


def _static_error(field_name: str, check: str) -> ValueError:
    return ValueError(f"{field_name} failed {check} check")


def _effective_https_port(parsed: SplitResult, *, field_name: str) -> int:
    try:
        port = parsed.port
    except ValueError:
        raise _static_error(field_name, "valid-port") from None
    if port is None:
        return _HTTPS_DEFAULT_PORT
    if port == 0:
        raise _static_error(field_name, "valid-port")
    return port


def _validate_percent_encoding(value: str, *, field_name: str) -> None:
    index = 0
    while True:
        index = value.find("%", index)
        if index < 0:
            break
        if _PERCENT_ESCAPE.match(value, index) is None:
            raise _static_error(field_name, "percent-encoding")
        index += 3
    if _ENCODED_URL_SEPARATOR.search(value):
        raise _static_error(field_name, "encoded-separator")


def _validate_path_dot_segments(path: str, *, field_name: str) -> None:
    for segment in path.split("/"):
        if unquote_to_bytes(segment) in (b".", b".."):
            raise _static_error(field_name, "dot-segment")


def _canonical_host(parsed: SplitResult, *, field_name: str) -> str:
    host = parsed.hostname
    if host is None:
        raise _static_error(field_name, "absolute-URL")
    if not host.isascii():
        raise _static_error(field_name, "ASCII-host")
    host = host.lower()
    if not host or host.endswith(".") or "*" in host or "%" in host:
        raise _static_error(field_name, "canonical-host")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Browsers accept several legacy numeric IPv4 forms that Python URL
        # parsers treat as DNS names. Reject the whole ambiguity class.
        labels = host.split(".")
        numeric_like = all(label.isdigit() for label in labels) or any(
            label.startswith("0x") or (len(label) > 1 and label.startswith("0") and label.isdigit()) for label in labels
        )
        if numeric_like:
            raise _static_error(field_name, "browser-host-equivalence") from None
        if len(host) > 253 or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            raise _static_error(field_name, "DNS-host") from None
        return host

    if address.version == 6 and "%" in host:
        raise _static_error(field_name, "IPv6-zone")
    try:
        validate_literal_ip_for_ssrf(str(address))
    except SSRFBlockedError:
        raise _static_error(field_name, "public-literal-IP") from None
    return address.compressed


def _parse_https_url(raw_value: str, *, field_name: str) -> tuple[str, SplitResult, _Origin]:
    if type(raw_value) is not str:
        raise _static_error(field_name, "string")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_value):
        raise _static_error(field_name, "control-character")
    value = raw_value.strip()
    if not value:
        raise _static_error(field_name, "nonblank")
    if "\\" in value:
        raise _static_error(field_name, "browser-parser-equivalence")
    _validate_percent_encoding(value, field_name=field_name)

    try:
        parsed = urlsplit(value)
    except ValueError:
        raise _static_error(field_name, "valid-URL") from None
    if parsed.scheme.lower() != "https":
        raise _static_error(field_name, "HTTPS")
    if not parsed.netloc or parsed.hostname is None:
        raise _static_error(field_name, "absolute-URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise _static_error(field_name, "no-credentials")
    host = _canonical_host(parsed, field_name=field_name)
    port = _effective_https_port(parsed, field_name=field_name)
    return value, parsed, _Origin("https", host, port)


def _parse_bare_origin(raw_value: str, *, field_name: str) -> tuple[str, _Origin]:
    _value, parsed, origin = _parse_https_url(raw_value, field_name=field_name)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise _static_error(field_name, "bare-origin")
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    port = "" if origin.port == _HTTPS_DEFAULT_PORT else f":{origin.port}"
    return f"https://{host}{port}", origin


def _parse_browser_endpoint(raw_value: str, *, field_name: str) -> tuple[str, _Origin]:
    value, parsed, origin = _parse_https_url(raw_value, field_name=field_name)
    if not parsed.path or parsed.path == "/":
        raise _static_error(field_name, "non-root-path")
    _validate_path_dot_segments(parsed.path, field_name=field_name)
    if parsed.query or parsed.fragment:
        raise _static_error(field_name, "no-query-or-fragment")
    return value, origin


def _canonical_origin(origin: _Origin) -> str:
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    port = "" if origin.port == _HTTPS_DEFAULT_PORT else f":{origin.port}"
    return f"https://{host}{port}"


def oidc_browser_endpoint_origin(endpoint: str) -> str:
    """Return the canonical origin of an already validated browser endpoint."""
    _value, origin = _parse_browser_endpoint(endpoint, field_name="browser_endpoint")
    return _canonical_origin(origin)


def https_url_origin(value: str, *, field_name: str = "issuer") -> str:
    """Return the canonical origin of any HTTPS URL, path or no path.

    An ISSUER is not a browser endpoint: ``https://accounts.google.com`` is a
    perfectly good issuer and has no path, while
    ``oidc_browser_endpoint_origin`` deliberately refuses a root path because
    an endpoint with no path is a misconfiguration. Per-profile origin policy
    needs the origin of the issuer itself, so it needs this instead — running
    the SAME parse, so the control-character, backslash, percent-encoding,
    embedded-credential, scheme and host canonicalisation checks all still
    apply. Extracting the origin with ``urlsplit`` at the call site would
    have skipped every one of them.
    """
    _value, _parsed, origin = _parse_https_url(value, field_name=field_name)
    return _canonical_origin(origin)


def validate_oidc_browser_endpoints(
    authorization_endpoint: str,
    token_endpoint: str,
    *,
    issuer: str,
) -> tuple[str, str]:
    """Return a validated authorization/token pair on the issuer's exact origin.

    Legacy browser-client path only (deleted with it in identity sprint step
    E). The per-deployment origin allowlist it used to accept is gone: an IdP
    whose endpoints live off the issuer's origin -- Cognito's hosted domain --
    is served by the SSO profile, whose ``sso_endpoint_origins`` is checked
    per profile at discovery, never by a browser-facing allowlist.
    """
    authorization_value, authorization_origin = _parse_browser_endpoint(
        authorization_endpoint,
        field_name="authorization_endpoint",
    )
    token_value, token_origin = _parse_browser_endpoint(token_endpoint, field_name="token_endpoint")
    _issuer_value, _issuer_parsed, issuer_origin = _parse_https_url(issuer, field_name="issuer")

    if authorization_origin != token_origin:
        raise ValueError("authorization_endpoint and token_endpoint must use the same origin")
    if authorization_origin != issuer_origin:
        raise ValueError("browser endpoint origin is not allowed")
    return authorization_value, token_value


class DiscoveredEndpoints(NamedTuple):
    """The endpoints a discovery document supplies, all validated together.

    A NamedTuple rather than a mapping so that FORGETTING one is a type error
    rather than an endpoint that silently skips validation. A
    ``Mapping[str, str | None]`` would have let a caller omit ``jwks_uri`` and
    still get a clean return — the shape of fail-open guard this codebase is
    trying to stop building.

    ``userinfo_endpoint`` is the one genuinely optional member: profiles that
    take every claim from the ID token never call it, and a provider that does
    not publish one is not misconfigured. Optionality is expressed in the type,
    not in a convention about empty strings.
    """

    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None


def validate_discovered_endpoints(
    endpoints: DiscoveredEndpoints,
    *,
    expected_origins: frozenset[str],
) -> DiscoveredEndpoints:
    """Validate every discovered endpoint against one closed set of origins.

    Discovery documents are fetched from a remote IdP, so every URL in one is
    attacker-reachable if the IdP is compromised or impersonated. Each is put
    through the same parse as a configured endpoint — HTTPS, no embedded
    credentials, canonical ASCII host, literal-IP SSRF block, no browser/parser
    disagreement, no dot segments, no query or fragment — and is then required
    to sit on an origin the PROFILE expects.

    WHAT CHANGED, AND WHY IT IS STRICTER
    ------------------------------------
    The rule this replaces was "same origin as the issuer, or in the operator's
    allowlist". Origin policy is a property of the identity provider, not of
    the deployment: Google serves authorization from ``accounts.google.com``
    and tokens from ``oauth2.googleapis.com``, so a same-origin rule refuses a
    correct Google deployment, and the operator allowlist existed to let a
    human widen it back — by hand, per deployment, with no way to be sure the
    widening was minimal.

    So the issuer's origin is no longer privileged. An endpoint is accepted
    only if its origin is in ``expected_origins``, which the profile computes.
    A profile whose expected origins do not include the issuer's own origin
    will now REFUSE an endpoint served from it — deliberately, and unlike the
    rule this replaces.

    Nor do the endpoints have to agree with each other any more. The old pair
    check required authorization and token to share an origin, which is the
    same same-origin assumption wearing a different hat. The closed set is the
    control; agreement between endpoints was only ever a proxy for it.

    An empty ``expected_origins`` refuses everything. That is the fail-closed
    reading: a profile that computed no origins has not authorised any, and
    treating "no constraint recorded" as "no constraint" is how origin checks
    stop being checks.
    """
    allowed = {_parse_bare_origin(value, field_name="expected origin")[1] for value in sorted(expected_origins)}

    def _checked(raw_value: str, *, field_name: str) -> str:
        value, origin = _parse_browser_endpoint(raw_value, field_name=field_name)
        if origin not in allowed:
            # Static message: the endpoint came from a remote document and its
            # host must not be echoed into a log line or an error page.
            raise _static_error(field_name, "expected-origin")
        return value

    userinfo_endpoint = endpoints.userinfo_endpoint
    return DiscoveredEndpoints(
        authorization_endpoint=_checked(endpoints.authorization_endpoint, field_name="authorization_endpoint"),
        token_endpoint=_checked(endpoints.token_endpoint, field_name="token_endpoint"),
        jwks_uri=_checked(endpoints.jwks_uri, field_name="jwks_uri"),
        userinfo_endpoint=None if userinfo_endpoint is None else _checked(userinfo_endpoint, field_name="userinfo_endpoint"),
    )


def validate_oidc_issuer(issuer: str) -> str:
    """Return an HTTPS OIDC issuer URL after syntax and literal-IP SSRF checks."""
    issuer_value, issuer_url, _origin = _parse_https_url(issuer.strip().rstrip("/"), field_name="issuer")
    if issuer_url.query or issuer_url.fragment:
        raise ValueError("issuer failed no-query-or-fragment check")
    return issuer_value
