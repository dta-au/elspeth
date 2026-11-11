"""ChromaSearchProvider.search() must audit and classify the HTTP SDK's real failures.

The chromadb HTTP client (``chromadb.api.fastapi.FastAPI._make_request``)
calls ``httpx.Client.request`` directly, so a refused connection or a timeout
surfaces as ``httpx.ConnectError`` / ``httpx.ReadTimeout`` -- neither is an
``OSError``, ``ConnectionError`` or ``TimeoutError``. A non-Chroma HTTP error
(a proxy's 502 page) is raised as a bare ``Exception(resp.text)`` by
``BaseHTTPClient._raise_chroma_error``, and a non-JSON 2xx body raises
``orjson.JSONDecodeError`` (a ``ValueError``).

Each of these happens after ``allocate_call_index``. ``search()`` must write
an ERROR call record for the attempted external call and raise
``RetrievalError`` so the RAG transform can retry or route the row, instead of
letting the raw SDK exception escape with an allocated index and no call row.

The faults are injected with ``httpx.MockTransport`` BELOW a real chromadb
``HttpClient`` collection, so the exception types are exactly what the
installed SDK raises -- no hand-picked stand-ins.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from elspeth.contracts.enums import CallStatus, CallType
from tests.fixtures.mock_audit import mock_item_audit_authority

chromadb = pytest.importorskip("chromadb")

from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError  # noqa: E402
from elspeth.plugins.infrastructure.clients.retrieval.chroma import (  # noqa: E402
    ChromaSearchProvider,
    ChromaSearchProviderConfig,
)
from tests.unit.plugins.infrastructure.clients.retrieval.test_chroma import (  # noqa: E402
    _fake_execution,
    _FakeExecutionRecorder,
    _precreate_collection,
)

_COLLECTION_ID = str(uuid.uuid4())


def _fault_response(fault: str, request: httpx.Request) -> httpx.Response:
    if fault == "connect":
        raise httpx.ConnectError("connection refused", request=request)
    if fault == "read_timeout":
        raise httpx.ReadTimeout("read timed out", request=request)
    if fault == "proxy_502":
        return httpx.Response(502, text="<html>502 Bad Gateway</html>")
    if fault == "non_json_body":
        return httpx.Response(200, text="<html>not json</html>")
    raise AssertionError(f"unknown fault {fault!r}")


class _ChromaServer:
    """Minimal Chroma HTTP API with a switchable fault on one endpoint."""

    def __init__(self) -> None:
        self.fault: str | None = None
        self.fault_phase: str | None = None
        self.query_calls = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/count"):
            if self.fault_phase == "count" and self.fault is not None:
                return _fault_response(self.fault, request)
            return httpx.Response(200, json=3)
        if path.endswith("/query"):
            self.query_calls += 1
            if self.fault_phase == "query" and self.fault is not None:
                return _fault_response(self.fault, request)
            return httpx.Response(200, json={"ids": [[]], "documents": [[]], "distances": [[]], "metadatas": [[]]})
        if path.endswith("/auth/identity"):
            return httpx.Response(200, json={"user_id": "", "tenant": "default_tenant", "databases": ["default_database"]})
        if "/collections/" in path:
            return httpx.Response(
                200,
                json={
                    "id": _COLLECTION_ID,
                    "name": "remote",
                    "configuration_json": {},
                    "metadata": {"hnsw:space": "cosine"},
                    "dimension": None,
                    "tenant": "default_tenant",
                    "database": "default_database",
                    "version": 0,
                    "log_position": 0,
                },
            )
        if "/databases/" in path:
            return httpx.Response(200, json={"id": str(uuid.uuid4()), "name": "default_database", "tenant": "default_tenant"})
        if "/tenants/" in path:
            return httpx.Response(200, json={"name": "default_tenant"})
        return httpx.Response(200, json={})


@pytest.fixture
def chroma_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[_ChromaServer]:
    server = _ChromaServer()
    real_client: Callable[..., httpx.Client] = httpx.Client

    def _client_with_mock_transport(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(server.handle)
        return real_client(*args, **kwargs)

    # chromadb.api.fastapi builds its session with httpx.Client(...); every
    # client built during the test gets the in-process mock transport.
    monkeypatch.setattr(httpx, "Client", _client_with_mock_transport)
    yield server


def _provider_on_http_collection() -> tuple[ChromaSearchProvider, _FakeExecutionRecorder]:
    """A provider whose collection is a real chromadb HTTP Collection over the mock server."""
    name = f"cst-{uuid.uuid4().hex[:12]}"
    _precreate_collection(name)
    execution = _fake_execution()
    provider = ChromaSearchProvider(
        config=ChromaSearchProviderConfig(collection=name, mode="ephemeral"),
        execution=execution,
        run_id="test-run",
    )
    http_client = chromadb.HttpClient(host="127.0.0.1", port=8000)
    provider._collection = http_client.get_collection("remote")
    return provider, execution


# (fault, expected retryable). Transport errors and non-Chroma HTTP error
# statuses (gateway pages) are transient; a 2xx body that is not JSON is a
# malformed response that a retry will not repair.
_FAULTS = [
    pytest.param("connect", True, id="httpx-ConnectError"),
    pytest.param("read_timeout", True, id="httpx-ReadTimeout"),
    pytest.param("proxy_502", True, id="bare-Exception-502"),
    pytest.param("non_json_body", False, id="orjson-JSONDecodeError"),
]


@pytest.mark.parametrize("phase", ["count", "query"])
@pytest.mark.parametrize(("fault", "expected_retryable"), _FAULTS)
def test_sdk_failure_is_audited_and_raised_as_retrieval_error(
    chroma_server: _ChromaServer,
    phase: str,
    fault: str,
    expected_retryable: bool,
) -> None:
    provider, execution = _provider_on_http_collection()
    chroma_server.fault_phase = phase
    chroma_server.fault = fault

    with pytest.raises(Exception) as exc_info:  # broad on purpose: the assertion below names the escaping type
        provider.search("query", top_k=3, min_score=0.0, **mock_item_audit_authority(), state_id="s1", token_id=None)
    raised = exc_info.value

    assert isinstance(raised, RetrievalError), (
        f"{phase} fault {fault!r} escaped search() as {type(raised).__name__}: {raised!r}; "
        f"allocated={execution.call_indices} recorded={len(execution.recorded_calls)}"
    )
    assert raised.retryable is expected_retryable
    assert execution.call_indices == {"s1": 1}
    call = execution.only_recorded_call()
    assert call["status"] == CallStatus.ERROR
    assert call["call_type"] == CallType.VECTOR
    assert call["call_index"] == 0
    assert call["error"].to_dict()["retryable"] is expected_retryable
    if phase == "count":
        assert chroma_server.query_calls == 0


def test_successful_http_search_records_success(chroma_server: _ChromaServer) -> None:
    """Control: the mock server path itself works, so the faults above are the only variable."""
    provider, execution = _provider_on_http_collection()

    assert provider.search("query", top_k=3, min_score=0.0, **mock_item_audit_authority(), state_id="s1", token_id=None) == []
    assert chroma_server.query_calls == 1
    assert execution.only_recorded_call()["status"] == CallStatus.SUCCESS


@pytest.mark.parametrize("phase", ["count", "query"])
def test_non_sdk_exception_subclass_still_crashes_through(
    chroma_server: _ChromaServer,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Only the SDK's bare ``Exception`` is classified; a code bug (TypeError) must not be swallowed."""
    provider, execution = _provider_on_http_collection()

    def _bug(*_args: object, **_kwargs: object) -> object:
        raise TypeError("bad argument")

    monkeypatch.setattr(type(provider._collection), phase, _bug)
    with pytest.raises(TypeError, match="bad argument"):
        provider.search("query", top_k=3, min_score=0.0, **mock_item_audit_authority(), state_id="s1", token_id=None)
    assert all(call["status"] == CallStatus.SUCCESS for call in execution.recorded_calls)
