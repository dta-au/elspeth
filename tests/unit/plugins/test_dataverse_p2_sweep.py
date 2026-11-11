"""Dataverse P2 sweep findings, 20261111-plugins-review.

Each section drives the real ``DataverseClient`` (or the real plugin) against
a fake Dataverse endpoint that follows the documented Web API contract, so a
test fails when the code disagrees with the service rather than with a mock.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest

from elspeth.core.security.web import SSRFSafeRequest
from elspeth.plugins.infrastructure.clients.dataverse import DataverseClient

ENV_URL = "https://myorg.crm.dynamics.com"
FORMATTED_VALUE = "OData.Community.Display.V1.FormattedValue"
AUTH = {
    "method": "service_principal",
    "tenant_id": "test-tenant",
    "client_id": "test-client",
    "client_secret": "test-secret",
}


class _FakeCredential:
    def get_token(self, *_scopes: str) -> SimpleNamespace:
        return SimpleNamespace(token="fake-token")


def _ssrf_safe(url: str) -> SSRFSafeRequest:
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or "localhost"
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return SSRFSafeRequest(
        original_url=url,
        resolved_ip="127.0.0.1",
        host_header=hostname,
        port=parsed.port or (443 if parsed.scheme == "https" else 80),
        path=path,
        scheme=parsed.scheme,
        bare_hostname=hostname,
    )


@pytest.fixture(autouse=True)
def _bypass_ssrf_dns() -> Iterator[None]:
    with patch(
        "elspeth.plugins.infrastructure.clients.dataverse.validate_url_for_ssrf",
        side_effect=lambda url, **_kwargs: _ssrf_safe(url),
    ):
        yield


def _requested_annotations(request: httpx.Request) -> set[str]:
    """Annotation names a request asks for via ``Prefer: odata.include-annotations``."""
    prefer = request.headers.get("prefer")
    if prefer is None:
        return set()
    names: set[str] = set()
    for preference in prefer.split(";"):
        key, _, value = preference.strip().partition("=")
        if key.strip() == "odata.include-annotations":
            names |= {name.strip() for name in value.strip().strip('"').split(",") if name.strip()}
    return names


def _annotation_requested(request: httpx.Request, name: str) -> bool:
    names = _requested_annotations(request)
    namespace = name.rsplit(".", 1)[0]
    return "*" in names or name in names or f"{namespace}.*" in names


class _FakeDataverse(httpx.BaseTransport):
    """Answers like Dataverse: annotations appear only when the request asks for them."""

    def __init__(self, handler: Callable[[httpx.Request], dict[str, Any]]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self._handler(request))


def _client(transport: httpx.BaseTransport, **kwargs: Any) -> DataverseClient:
    client = DataverseClient(environment_url=ENV_URL, credential=cast(Any, _FakeCredential()), **kwargs)
    client._client.close()
    client._client = httpx.Client(transport=transport, timeout=30.0)
    return client


def _source_context() -> SimpleNamespace:
    recorded: list[dict[str, Any]] = []

    def record_call(**kwargs: Any) -> None:
        recorded.append(kwargs)

    def record_validation_error(**_kwargs: Any) -> None:
        return None

    return SimpleNamespace(
        run_id="run-1",
        node_id="source-node",
        operation_id="op-1",
        call_mode_session=None,
        landscape=None,
        telemetry_emit=lambda *_args, **_kwargs: None,
        record_call=record_call,
        record_validation_error=record_validation_error,
        recorded=recorded,
    )


def _contact_row(request: httpx.Request, contactid: str) -> dict[str, Any]:
    row: dict[str, Any] = {"@odata.etag": 'W/"1"', "contactid": contactid, "statecode": 0}
    if _annotation_requested(request, FORMATTED_VALUE):
        row[f"statecode@{FORMATTED_VALUE}"] = "Active"
    return row


def _metadata_or(request: httpx.Request, data: Callable[[httpx.Request], dict[str, Any]]) -> dict[str, Any]:
    if "EntityDefinitions" in request.url.path:
        return {"LogicalName": "contact", "EntitySetName": "contacts"}
    return data(request)


# ---------------------------------------------------------------------------
# Finding #47: include_formatted_values never asks for FormattedValue annotations
# ---------------------------------------------------------------------------


def _load_rows(config: dict[str, Any], transport: _FakeDataverse, **client_kwargs: Any) -> tuple[list[Any], SimpleNamespace]:
    from elspeth.plugins.sources.dataverse import DataverseSource

    source = DataverseSource(config)
    source._client = _client(transport, **client_kwargs)
    ctx = _source_context()
    try:
        rows = list(source.load(cast(Any, ctx)))
    finally:
        source.close()
    return rows, ctx


def _odata_config(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "environment_url": ENV_URL,
        "auth": AUTH,
        "entity": "contact",
        "schema": {"mode": "observed"},
        "on_validation_failure": "quarantine",
    }
    config.update(overrides)
    return config


def test_formatted_values_are_requested_on_every_odata_page() -> None:
    """Every data page (initial and nextLink) asks for FormattedValue, and the rows carry it."""
    next_link = f"{ENV_URL}/api/data/v9.2/contacts?$skiptoken=page2"

    def data(request: httpx.Request) -> dict[str, Any]:
        if "skiptoken" in str(request.url):
            return {"value": [_contact_row(request, "2")]}
        return {"value": [_contact_row(request, "1")], "@odata.nextLink": next_link}

    transport = _FakeDataverse(lambda request: _metadata_or(request, data))
    hook_calls: list[tuple[str, dict[str, str]]] = []
    rows, ctx = _load_rows(
        _odata_config(include_formatted_values=True),
        transport,
        before_request=lambda url, extra: hook_calls.append((url, dict(extra))),
    )

    # Observed-schema field normalization folds "statecode__formatted" to "statecode_formatted".
    assert [source_row.row.get("statecode_formatted") for source_row in rows] == ["Active", "Active"]
    data_requests = [request for request in transport.requests if "EntityDefinitions" not in request.url.path]
    assert len(data_requests) == 2
    for request in data_requests:
        assert _annotation_requested(request, FORMATTED_VALUE), f"no FormattedValue Prefer on {request.url}"
    # The audited request headers carry the Prefer header that was sent.
    data_calls = [call for call in ctx.recorded if "EntityDefinitions" not in str(call["request_data"]["url"])]
    assert len(data_calls) == 2
    for call, request in zip(data_calls, data_requests, strict=True):
        assert call["request_data"]["headers"]["Prefer"] == request.headers["prefer"]
    # VERIFY preflight sees the same non-auth headers the request sends.
    data_hook_calls = [extra for url, extra in hook_calls if "EntityDefinitions" not in url]
    assert data_hook_calls
    for extra in data_hook_calls:
        assert extra.get("Prefer") == data_requests[0].headers["prefer"]


def test_formatted_values_not_requested_when_option_is_off() -> None:
    transport = _FakeDataverse(lambda request: _metadata_or(request, lambda r: {"value": [_contact_row(r, "1")]}))
    rows, _ctx = _load_rows(_odata_config(include_formatted_values=False), transport)

    assert len(rows) == 1
    data_requests = [request for request in transport.requests if "EntityDefinitions" not in request.url.path]
    assert not _annotation_requested(data_requests[0], FORMATTED_VALUE)


def test_fetchxml_formatted_values_share_one_prefer_header_with_paging_annotations() -> None:
    def data(request: httpx.Request) -> dict[str, Any]:
        return {"value": [_contact_row(request, "1")]}

    transport = _FakeDataverse(lambda request: _metadata_or(request, data))
    config = _odata_config(include_formatted_values=True)
    del config["entity"]
    config["fetch_xml"] = '<fetch count="5"><entity name="contact"><attribute name="statecode"/></entity></fetch>'
    rows, _ctx = _load_rows(config, transport)

    assert [source_row.row.get("statecode_formatted") for source_row in rows] == ["Active"]
    data_request = next(request for request in transport.requests if "fetchXml" in str(request.url))
    assert len(data_request.headers.get_list("prefer")) == 1
    assert _annotation_requested(data_request, FORMATTED_VALUE)
    assert _annotation_requested(data_request, "Microsoft.Dynamics.CRM.fetchxmlpagingcookie")
    assert _annotation_requested(data_request, "Microsoft.Dynamics.CRM.morerecords")


@pytest.mark.parametrize(
    "annotation_key",
    [
        "_parentcustomerid_value@Microsoft.Dynamics.CRM.lookuplogicalname",
        "_parentcustomerid_value@Microsoft.Dynamics.CRM.associatednavigationproperty",
        "birthdate@odata.type",
        "@odata.etag",
        "@Microsoft.Dynamics.CRM.totalrecordcount",
    ],
)
def test_property_level_annotations_are_stripped(annotation_key: str) -> None:
    from elspeth.plugins.sources.dataverse import DataverseSource

    source = DataverseSource(_odata_config(include_formatted_values=True))
    cleaned = source._strip_odata_metadata({"contactid": "1", annotation_key: "x"})
    assert cleaned == {"contactid": "1"}
