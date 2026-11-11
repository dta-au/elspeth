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
