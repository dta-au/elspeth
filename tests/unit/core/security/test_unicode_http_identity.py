"""DNS, origin, and archived pin identities agree with HTTPX IDNA encoding."""

import socket
from dataclasses import replace

import pytest

from elspeth.contracts.call_mode import ReplaySSRFRequest
from elspeth.core.security.web import (
    SSRFBlockedError,
    parse_http_origin,
    validate_allowed_http_origin,
    validate_archived_ssrf_request,
    validate_url_for_ssrf,
)


def test_unicode_archived_pin_preserves_original_url_without_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://bücher.example:8443/item?q=1"
    pin = ReplaySSRFRequest(
        original_url=url,
        resolved_ip="93.184.216.34",
        host_header="xn--bcher-kva.example:8443",
        port=8443,
        path="/item?q=1",
        scheme="https",
        bare_hostname="xn--bcher-kva.example",
    )

    def forbidden_dns(*_args):
        raise AssertionError("replay must remain offline")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    safe = validate_archived_ssrf_request(url, pin)
    assert safe.original_url == url
    assert safe.host_header == pin.host_header
    assert safe.sni_hostname == pin.bare_hostname
    with pytest.raises(SSRFBlockedError, match="does not match"):
        validate_archived_ssrf_request(url, replace(pin, bare_hostname="different.example"))
    with pytest.raises(SSRFBlockedError):
        validate_archived_ssrf_request(url, replace(pin, resolved_ip="169.254.169.254"))


def test_unicode_and_ascii_spelling_share_origin_but_not_other_hosts() -> None:
    origin = parse_http_origin("https://bücher.example:8443")
    assert origin == parse_http_origin("https://xn--bcher-kva.example:8443")
    validate_allowed_http_origin("https://xn--bcher-kva.example:8443/page-2", (origin,))
    with pytest.raises(SSRFBlockedError):
        validate_allowed_http_origin("https://other.example:8443/page-2", (origin,))
    with pytest.raises(SSRFBlockedError):
        validate_allowed_http_origin("https://xn--bcher-kva.example:443/page-2", (origin,))


def test_malformed_unicode_hostname_is_refused_before_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_dns(*_args):
        raise AssertionError("malformed Unicode must be refused before DNS")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    with pytest.raises(SSRFBlockedError) as refused:
        validate_url_for_ssrf("https://\ud800.example/private?token=untrusted")
    assert refused.value.kind == "malformed_url"
    assert "/private" not in str(refused.value)
    assert "token=untrusted" not in str(refused.value)
