"""FetchXML paging against the documented Dataverse Web API contract.

Microsoft's "Page results using FetchXml" (Web API tab) says:

* The ``@Microsoft.Dynamics.CRM.fetchxmlpagingcookie`` and
  ``@Microsoft.Dynamics.CRM.morerecords`` annotations are returned only when
  the request sends ``Prefer: odata.include-annotations=...`` naming them
  (or ``"*"``).
* On the final page the response carries neither annotation; absence means
  there are no more records.
* The paging-cookie annotation is a ``<cookie pagenumber=.. pagingcookie=..>``
  wrapper; the cookie to send back is its ``pagingcookie`` attribute,
  URL-decoded twice.

Sweep findings #71 and #72, 20261111-plugins-review.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest

from elspeth.core.security.web import SSRFSafeRequest
from elspeth.plugins.infrastructure.clients.dataverse import (
    DataverseClient,
    DataverseClientError,
    DataversePageResponse,
)

ENV_URL = "https://myorg.crm.dynamics.com"
FETCH_XML = '<fetch count="2"><entity name="contact"><attribute name="fullname" /></entity></fetch>'
MORERECORDS = "@Microsoft.Dynamics.CRM.morerecords"
PAGINGCOOKIE = "@Microsoft.Dynamics.CRM.fetchxmlpagingcookie"


class _FakeCredential:
    def get_token(self, *_scopes: str) -> SimpleNamespace:
        return SimpleNamespace(token="fake-token")


class _RecordingTransport(httpx.BaseTransport):
    def __init__(self, bodies: list[dict[str, Any]]) -> None:
        self._bodies = list(bodies)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) > len(self._bodies):
            raise AssertionError(f"unexpected request #{len(self.requests)}: {request.url}")
        return httpx.Response(200, json=self._bodies[len(self.requests) - 1])


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
        port=parsed.port or 443,
        path=path,
        scheme=parsed.scheme,
        bare_hostname=hostname,
    )


@pytest.fixture(autouse=True)
def _bypass_ssrf_dns():
    with patch(
        "elspeth.plugins.infrastructure.clients.dataverse.validate_url_for_ssrf",
        side_effect=lambda url, **_kwargs: _ssrf_safe(url),
    ):
        yield


def _client(transport: _RecordingTransport) -> DataverseClient:
    client = DataverseClient(environment_url=ENV_URL, credential=cast(Any, _FakeCredential()))
    client._client.close()
    client._client = httpx.Client(transport=transport, timeout=30.0)
    return client


def _drain(transport: _RecordingTransport) -> tuple[list[DataversePageResponse], DataverseClientError | None]:
    """Collect every page; capture a client error so the test asserts on it."""
    client = _client(transport)
    pages: list[DataversePageResponse] = []
    try:
        for page in client.paginate_fetchxml("contacts", FETCH_XML):
            pages.append(page)
    except DataverseClientError as exc:
        return pages, exc
    finally:
        client.close()
    return pages, None


def _doc_wrapper_cookie() -> str:
    """The annotation value shape shown in Microsoft's page-results doc."""
    inner = '<cookie page="1"><contactid last="{A}" first="{B}" /></cookie>'
    twice = urllib.parse.quote(urllib.parse.quote(inner, safe=""), safe="")
    return f'<cookie pagenumber="2" pagingcookie="{twice}" istracking="False" />'


# ---------------------------------------------------------------------------
# Finding #71: Prefer header and absent morerecords on the final page
# ---------------------------------------------------------------------------


def test_fetchxml_request_asks_for_paging_annotations() -> None:
    transport = _RecordingTransport([{"value": [{"contactid": "1"}]}])
    _drain(transport)

    prefer = transport.requests[0].headers.get("prefer")
    assert prefer is not None, "FetchXML request sent no Prefer header"
    assert prefer.startswith("odata.include-annotations=")
    annotations = prefer.split("=", 1)[1].strip('"')
    names = {name.strip() for name in annotations.split(",")}
    assert (
        names == {"*"}
        or {
            "Microsoft.Dynamics.CRM.fetchxmlpagingcookie",
            "Microsoft.Dynamics.CRM.morerecords",
        }
        <= names
    )


def test_single_page_without_annotations_is_end_of_results() -> None:
    """A documented final page (no annotations) ends pagination cleanly."""
    transport = _RecordingTransport([{"value": [{"contactid": "1"}, {"contactid": "2"}]}])
    pages, error = _drain(transport)

    assert error is None, f"final page without annotations raised: {error}"
    assert [len(p.rows) for p in pages] == [2]
    assert len(transport.requests) == 1


def test_multi_page_final_page_without_annotations_is_end_of_results() -> None:
    transport = _RecordingTransport(
        [
            {"value": [{"contactid": "1"}], MORERECORDS: True, PAGINGCOOKIE: _doc_wrapper_cookie()},
            {"value": [{"contactid": "2"}]},
        ]
    )
    pages, error = _drain(transport)

    assert error is None, f"final page without annotations raised: {error}"
    assert [len(p.rows) for p in pages] == [1, 1]
    assert len(transport.requests) == 2
    assert transport.requests[1].headers.get("prefer") is not None


def test_paging_cookie_without_morerecords_is_protocol_violation() -> None:
    """A cookie with no morerecords contradicts the documented final-page shape."""
    transport = _RecordingTransport([{"value": [], PAGINGCOOKIE: _doc_wrapper_cookie()}])
    client = _client(transport)
    try:
        pages = client.paginate_fetchxml("contacts", FETCH_XML)
        first = next(pages)
        with pytest.raises(DataverseClientError) as exc_info:
            next(pages)
    finally:
        client.close()

    assert exc_info.value.error_category == "protocol_violation"
    assert exc_info.value.request_url == first.request_url
    assert len(transport.requests) == 1


def test_morerecords_true_without_cookie_still_raises() -> None:
    transport = _RecordingTransport([{"value": [], MORERECORDS: True}])
    client = _client(transport)
    try:
        with pytest.raises(DataverseClientError, match="no paging cookie") as exc_info:
            list(client.paginate_fetchxml("contacts", FETCH_XML))
    finally:
        client.close()

    assert exc_info.value.error_category == "protocol_violation"


# ---------------------------------------------------------------------------
# Finding #72: the annotation is a wrapper element; the cookie is its
# pagingcookie attribute, URL-encoded twice.
# ---------------------------------------------------------------------------


def _sent_fetch_element(request: httpx.Request) -> ET.Element:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(str(request.url)).query)
    return ET.fromstring(query["fetchXml"][0])


def test_paging_cookie_is_inner_pagingcookie_decoded_twice() -> None:
    inner = '<cookie page="1"><contactid last="{A}" first="{B}" /></cookie>'
    transport = _RecordingTransport(
        [
            {"value": [{"contactid": "1"}], MORERECORDS: True, PAGINGCOOKIE: _doc_wrapper_cookie()},
            {"value": [{"contactid": "2"}]},
        ]
    )
    pages, error = _drain(transport)

    assert error is None, f"pagination raised: {error}"
    assert len(pages) == 2
    fetch = _sent_fetch_element(transport.requests[1])
    assert fetch.get("paging-cookie") == inner
    assert fetch.get("page") == "2"


def test_paging_cookie_with_xml_metacharacters_round_trips() -> None:
    inner = '<cookie page="1"><name last="a&amp;b&lt;c&gt;&quot;d" first="x" /></cookie>'
    twice = urllib.parse.quote(urllib.parse.quote(inner, safe=""), safe="")
    wrapper = f'<cookie pagenumber="2" pagingcookie="{twice}" istracking="False" />'
    transport = _RecordingTransport(
        [
            {"value": [{"contactid": "1"}], MORERECORDS: True, PAGINGCOOKIE: wrapper},
            {"value": []},
        ]
    )
    pages, error = _drain(transport)

    assert error is None, f"pagination raised: {error}"
    assert len(pages) == 2
    assert _sent_fetch_element(transport.requests[1]).get("paging-cookie") == inner


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param("not xml at all", id="not-xml"),
        pytest.param("<cookie pagenumber=", id="truncated-xml"),
        pytest.param('<cookie pagenumber="2" istracking="False" />', id="no-pagingcookie-attribute"),
    ],
)
def test_malformed_paging_cookie_annotation_is_protocol_violation(annotation: str) -> None:
    transport = _RecordingTransport(
        [
            {"value": [{"contactid": "1"}], MORERECORDS: True, PAGINGCOOKIE: annotation},
            {"value": []},
        ]
    )
    pages, error = _drain(transport)

    assert error is not None, "malformed paging-cookie annotation was forwarded to Dataverse"
    assert error.error_category == "protocol_violation"
    assert error.request_url == pages[0].request_url
    assert len(transport.requests) == 1


# ---------------------------------------------------------------------------
# Follow-up A: the Prefer header the FetchXML request actually sends is part
# of the audited request, and VERIFY rebuilds the same request shape.
# ---------------------------------------------------------------------------

PREFER_VALUE = 'odata.include-annotations="Microsoft.Dynamics.CRM.fetchxmlpagingcookie,Microsoft.Dynamics.CRM.morerecords"'
_SOURCE_FETCH_XML = '<fetch><entity name="contact"><attribute name="fullname" /></entity></fetch>'
_METADATA_BODY = {"value": [{"LogicalName": "contact", "EntitySetName": "contacts"}]}


def _source_config() -> dict[str, Any]:
    return {
        "environment_url": ENV_URL,
        "auth": {
            "method": "service_principal",
            "tenant_id": "test-tenant",
            "client_id": "test-client",
            "client_secret": "test-secret",
        },
        "fetch_xml": _SOURCE_FETCH_XML,
        "schema": {"mode": "observed"},
        "on_validation_failure": "quarantine",
    }


class _SourceContext:
    def __init__(self, call_mode_session: Any = None) -> None:
        self.run_id = "run-1"
        self.node_id = "source-node"
        self.operation_id = "op-1"
        self.call_mode_session = call_mode_session
        self.landscape = None
        self.recorded: list[dict[str, Any]] = []

    def telemetry_emit(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_validation_error(self, **_kwargs: Any) -> None:
        raise AssertionError("unexpected validation error")

    def record_call(self, **kwargs: Any) -> SimpleNamespace:
        self.recorded.append(kwargs)
        index = len(self.recorded) - 1
        return SimpleNamespace(call_index=index, call_id=f"call-{index}")


class _ArchiveReplaySession:
    """VERIFY session that matches requests with the production comparator."""

    def __init__(self, archived: list[dict[str, Any]]) -> None:
        from elspeth.contracts.enums import RunMode

        self.mode = RunMode.VERIFY
        self._archived = archived
        self.verified: list[int] = []

    def preflight_verify_operation_http_managed_identity(self, *, request_data: Any, current_operation_id: str) -> SimpleNamespace:
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.engine.orchestrator.call_mode_session import _mi_non_auth_request

        current = _mi_non_auth_request(request_data, before_dns=True)
        matches = [i for i, archived in enumerate(self._archived) if _mi_non_auth_request(archived, before_dns=False) == current]
        if len(matches) != 1:
            raise AuditIntegrityError(f"archived request missing or ambiguous for {request_data!r}")
        return SimpleNamespace(source_call_id=f"source-{matches[0]}")

    def admit_verify_operation_http_managed_identity(
        self, *, request_data: Any, current_operation_id: str, current_call_index: int, source_call_id: str
    ) -> str:
        from elspeth.contracts.errors import AuditIntegrityError
        from elspeth.engine.orchestrator.call_mode_session import _mi_non_auth_request

        archived = self._archived[int(source_call_id.removeprefix("source-"))]
        if _mi_non_auth_request(archived, before_dns=False) != _mi_non_auth_request(request_data, before_dns=False):
            raise AuditIntegrityError("non-auth request fields differ")
        return source_call_id

    def verify_call(self, **kwargs: Any) -> None:
        self.verified.append(kwargs["current_call_index"])


def _source_bodies() -> list[dict[str, Any]]:
    return [_METADATA_BODY, {"value": [{"contactid": "1", "fullname": "Ada"}]}]


def test_recorded_fetchxml_page_includes_prefer_header() -> None:
    from elspeth.plugins.sources.dataverse import DataverseSource

    transport = _RecordingTransport(_source_bodies())
    source = DataverseSource(_source_config())
    source._client = _client(transport)
    ctx = _SourceContext()
    try:
        rows = list(source.load(cast(Any, ctx)))
    finally:
        source._client.close()

    assert len(rows) == 1
    page_request = ctx.recorded[-1]["request_data"]
    assert "fetchXml=" in page_request["url"]
    assert page_request["headers"].get("Prefer") == PREFER_VALUE, "audit record omits the Prefer header that was sent"
    assert transport.requests[-1].headers.get("prefer") == PREFER_VALUE


def test_verify_of_freshly_recorded_fetchxml_run_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Record a FetchXML run, then VERIFY it with the production comparator."""
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.plugins.sources.dataverse import DataverseSource

    # The production comparator requires a fingerprinted Authorization header.
    monkeypatch.setenv("ELSPETH_ALLOW_RAW_SECRETS", "false")
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "lane-02-test-fingerprint-key")

    record_transport = _RecordingTransport(_source_bodies())
    recorder = DataverseSource(_source_config())
    recorder._client = _client(record_transport)
    record_ctx = _SourceContext()
    try:
        assert len(list(recorder.load(cast(Any, record_ctx)))) == 1
    finally:
        recorder._client.close()
    archived = [call["request_data"] for call in record_ctx.recorded]
    assert len(archived) == 2
    assert archived[-1]["headers"].get("Prefer") == PREFER_VALUE, "audit record omits the Prefer header that was sent"

    verify_transport = _RecordingTransport(_source_bodies())
    real_httpx_client = httpx.Client
    session = _ArchiveReplaySession(archived)
    verifier = DataverseSource(_source_config())
    verify_ctx = _SourceContext(call_mode_session=session)
    error: AuditIntegrityError | None = None
    with (
        patch.object(type(verifier._auth_config), "create_credential", return_value=_FakeCredential()),
        patch(
            "elspeth.plugins.infrastructure.clients.dataverse.httpx.Client",
            side_effect=lambda **_kwargs: real_httpx_client(transport=verify_transport, timeout=30.0),
        ),
    ):
        try:
            rows = list(verifier.load(cast(Any, verify_ctx)))
        except AuditIntegrityError as exc:
            error = exc
            rows = []
        finally:
            if verifier._client is not None:
                verifier._client.close()

    assert error is None, f"VERIFY refused a freshly recorded FetchXML run: {error}"
    assert len(rows) == 1
    assert session.verified == [0, 1]
