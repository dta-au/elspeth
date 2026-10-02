"""Archived HTTP DNS pins are validated without current DNS access."""

import pytest

from elspeth.contracts.call_mode import ReplaySSRFRequest
from elspeth.core.security import web


def _pin(*, resolved_ip: str = "93.184.216.34", path: str = "/item?q=1") -> ReplaySSRFRequest:
    return ReplaySSRFRequest(
        original_url="https://example.com/item?q=1",
        resolved_ip=resolved_ip,
        host_header="example.com",
        port=443,
        path=path,
        scheme="https",
        bare_hostname="example.com",
    )


def test_archived_pin_is_revalidated_without_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_dns(_: str) -> None:
        raise AssertionError("Replay must not resolve DNS")

    monkeypatch.setattr(web, "_submit_dns_resolution", fail_if_dns)
    safe = web.validate_archived_ssrf_request("https://example.com/item?q=1", _pin())
    assert safe.resolved_ip == "93.184.216.34"
    assert safe.connection_url == "https://93.184.216.34:443/item?q=1"


@pytest.mark.parametrize(
    "pin",
    [_pin(resolved_ip="127.0.0.1"), _pin(path="/different")],
)
def test_archived_pin_rejects_blocked_or_tampered_evidence(pin: ReplaySSRFRequest) -> None:
    with pytest.raises(web.SSRFBlockedError):
        web.validate_archived_ssrf_request("https://example.com/item?q=1", pin)
