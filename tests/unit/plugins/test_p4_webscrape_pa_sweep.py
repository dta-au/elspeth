"""Plugin review 2026-11-11 P4 sweep: web_scrape helpers and Power Automate client.

Findings #130, #153, #157, #164. Each test fails at an assertion on the
pre-fix code: refusals are captured explicitly rather than allowed to escape
as uncaught exceptions.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest

from elspeth.contracts.audit_protocols import CallRecorder
from elspeth.contracts.call_mode import CallModeSession
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import RunMode
from elspeth.contracts.security import secret_fingerprint
from elspeth.plugins.infrastructure.clients.power_automate import PowerAutomateClientError, PowerAutomateOperationClient
from elspeth.plugins.infrastructure.power_automate_nonlive import ArchivedPowerAutomateSourceConfig, DeferredPowerAutomateCredential
from elspeth.plugins.transforms.web_scrape_auth import WebScrapeAuthConfig
from elspeth.plugins.transforms.web_scrape_fingerprint import compute_fingerprint
from elspeth.plugins.transforms.web_scrape_json_extraction import JSONRecordsConfig, extract_json_records_with_provenance

# --- #130: archived SAS verify path without a fingerprint key ---------------

_PA_URL = "https://flows.example.org/encoded%2Fpath?sig=fake-SAS-capability"
_PA_ORIGIN = "https://flows.example.org"
_PA_BODY = {"protocol": "elspeth.power-automate.v1", "operation": "read", "query": {"value": "x"}}


def test_130_archived_sas_without_fingerprint_key_reports_fingerprint_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "fake-test-fingerprint-key")
    monkeypatch.setenv("FAKE_SECRET", _PA_URL)
    fingerprint = secret_fingerprint(_PA_URL)
    config = ArchivedPowerAutomateSourceConfig.model_validate(
        {
            "auth": {"method": "sas_url", "trigger_url_secret_fingerprint": fingerprint},
            "allowed_origin": _PA_ORIGIN,
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
        }
    )
    session = create_autospec(CallModeSession, instance=True)
    session.mode = RunMode.VERIFY
    monkeypatch.setattr("elspeth.core.security.web._resolve_hostname", lambda *a, **k: pytest.fail("DNS without fingerprint key"))
    transport = PowerAutomateOperationClient(
        config,
        recorder=create_autospec(CallRecorder, instance=True),
        run_id="run",
        operation_id="source-load",
        coordination_token=CoordinationToken("run", "worker", 1),
        telemetry_emit=lambda event: None,
        before_send=lambda: None,
        call_mode_session=session,
        credential=DeferredPowerAutomateCredential("FAKE_SECRET", fingerprint),
    )
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY")
    code: str | None = None
    try:
        transport.post_json(_PA_BODY)
    except PowerAutomateClientError as exc:
        code = exc.code
    finally:
        transport.close()
    assert code == "fingerprint_key_required"
    session.preflight_verify_http_request.assert_not_called()


# --- #153: null elements inside a selected JSON array -----------------------


def _json_config(multiple: str) -> JSONRecordsConfig:
    return JSONRecordsConfig.model_validate(
        {"field": "out", "records_path": ["results"], "columns": [{"field": "name", "path": ["n"], "multiple": multiple}]}
    )


def _extract(document: str, multiple: str) -> object:
    try:
        return extract_json_records_with_provenance(document, _json_config(multiple), source_url="https://example.gov.au/").records
    except ValueError as exc:
        return f"refused: {exc}"


def test_153_top_level_null_is_absent() -> None:
    assert _extract('{"results":[{"n":null}]}', "first") == [{"name": None}]


@pytest.mark.parametrize(
    ("document", "multiple", "expected"),
    [
        ('{"results":[{"n":[null,"x"]}]}', "first", [{"name": "x"}]),
        ('{"results":[{"n":["a",null]}]}', "all", [{"name": ["a"]}]),
        ('{"results":[{"n":[null,"b"]}]}', "one", [{"name": "b"}]),
        ('{"results":[{"n":[null]}]}', "first", [{"name": None}]),
        ('{"results":[{"n":[null,null]}]}', "all", [{"name": []}]),
    ],
)
def test_153_null_array_elements_are_absent_like_top_level_null(document: str, multiple: str, expected: object) -> None:
    assert _extract(document, multiple) == expected


def test_153_non_null_non_scalar_array_elements_still_refused() -> None:
    result = _extract('{"results":[{"n":[{"k":1},"x"]}]}', "all")
    assert type(result) is str
    assert "non-scalar" in result


# --- #157: unknown fingerprint mode must not fall through to "full" ---------


@pytest.mark.parametrize("mode", ["contents", "structure", "", "FULL"])
def test_157_unknown_fingerprint_mode_is_refused(mode: Any) -> None:
    refused = False
    try:
        compute_fingerprint("a  b", mode)
    except ValueError:
        refused = True
    assert refused


def test_157_known_modes_unchanged() -> None:
    assert compute_fingerprint("a  b", "full") != compute_fingerprint("a  b", "content")
    assert compute_fingerprint(" a  b ", "content") == compute_fingerprint("a b", "full")


# --- #164: API-key header denylist and RFC 7617 basic credential ------------


@pytest.mark.parametrize(
    "header_name",
    ["Keep-Alive", "Proxy-Connection", "Proxy-Authenticate", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host"],
)
def test_164_api_key_refuses_hop_by_hop_and_proxy_headers(header_name: str) -> None:
    refused = False
    try:
        WebScrapeAuthConfig.model_validate(
            {"scheme": "api_key", "origin": "https://api.example.org", "credential": "fake-key", "header_name": header_name}
        )
    except ValueError:
        refused = True
    assert refused


def test_164_api_key_custom_header_still_admitted() -> None:
    config = WebScrapeAuthConfig.model_validate(
        {"scheme": "api_key", "origin": "https://api.example.org", "credential": "fake-key", "header_name": "X-Custom-Key"}
    )
    assert config.header() == ("X-Custom-Key", "fake-key")


def test_164_basic_credential_without_colon_is_refused() -> None:
    config = WebScrapeAuthConfig.model_validate({"scheme": "basic", "origin": "https://api.example.org", "credential": "nocolon"})
    refused = False
    try:
        config.header()
    except ValueError:
        refused = True
    assert refused


def test_164_basic_credential_with_colon_still_encoded() -> None:
    config = WebScrapeAuthConfig.model_validate({"scheme": "basic", "origin": "https://api.example.org", "credential": "user:pass"})
    assert config.header() == ("Authorization", "Basic dXNlcjpwYXNz")
