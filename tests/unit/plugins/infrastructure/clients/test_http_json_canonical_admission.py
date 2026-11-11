"""GENERIC-policy HTTP JSON bodies must be canonical-admissible before audit.

Sweep finding #21 (20261111-plugins-review): the GENERIC branch of
``AuditedHTTPClient._parse_response_body`` admitted overflow floats
(``1e999`` -> ``inf``), integers outside the canonical JSON domain and deeply
nested arrays. The recorder's ``stable_hash`` then raised ``ValueError`` or
``RecursionError`` before any Call row was written, so a request that was
actually sent left no audit record and a Tier-3 response escaped the client
as a bare non-httpx exception.

The contract pinned here: such a body is recorded (exactly one call, hashable
by the canonical writer) with a ``_json_parse_failed`` sentinel, and the
client returns normally so callers keep a routable result.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from elspeth.contracts import json_parser
from elspeth.core.canonical import stable_hash
from elspeth.core.security.web import SSRFSafeRequest
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient
from tests.fixtures.mock_audit import mock_item_audit_authority

_RESOLVED_IP = "93.184.216.34"

_INADMISSIBLE_BODIES = [
    pytest.param(b'{"x": 1e999}', id="overflow-float"),
    pytest.param(b'{"x": -1e999}', id="negative-overflow-float"),
    pytest.param(b"[" * 900 + b"]" * 900, id="deep-array-900"),
    pytest.param(b'{"x": 1152921504606846976}', id="int-outside-canonical-domain"),
]


class _HashingRecorder:
    """Recorder double that hashes the response exactly as the Landscape recorder does."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def allocate_call_index(self, *_args: object, **_kwargs: object) -> int:
        return len(self.calls)

    def record_call(self, **kwargs: Any) -> object:
        response_data = kwargs["response_data"]
        # core/landscape/execution/calls.py hashes the response before insert.
        stable_hash(None if response_data is None else response_data.to_dict())
        self.calls.append(kwargs)
        return object()


class _HTTPClientDouble:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    def __enter__(self) -> _HTTPClientDouble:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def get(self, *_args: object, **_kwargs: object) -> httpx.Response:
        return self._response

    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        return self._response

    def request(self, *_args: object, **_kwargs: object) -> httpx.Response:
        return self._response

    def close(self) -> None:
        return None


def _json_response(body: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=body,
        request=httpx.Request("GET", f"http://{_RESOLVED_IP}/"),
    )


def _make_client(recorder: _HashingRecorder) -> AuditedHTTPClient:
    return AuditedHTTPClient(
        **mock_item_audit_authority("run-1"),
        execution=recorder,
        state_id="state-1",
        run_id="run-1",
        telemetry_emit=lambda _event: None,
    )


def _drive_post(body: bytes) -> tuple[BaseException | None, _HashingRecorder]:
    recorder = _HashingRecorder()
    with patch("httpx.Client", lambda *_a, **_k: _HTTPClientDouble(_json_response(body))):
        client = _make_client(recorder)
        try:
            client.post(f"http://{_RESOLVED_IP}/", json={"a": 1})
        except (ValueError, RecursionError) as exc:
            return exc, recorder
    return None, recorder


def _drive_get_ssrf_safe(body: bytes) -> tuple[BaseException | None, _HashingRecorder]:
    recorder = _HashingRecorder()
    client = _make_client(recorder)
    safe_request = SSRFSafeRequest(
        original_url="http://example.com/",
        resolved_ip=_RESOLVED_IP,
        host_header="example.com",
        port=80,
        path="/",
        scheme="http",
        bare_hostname="example.com",
    )
    with patch("httpx.Client", lambda *_a, **_k: _HTTPClientDouble(_json_response(body))):
        try:
            client.get_ssrf_safe(safe_request)
        except (ValueError, RecursionError) as exc:
            return exc, recorder
    return None, recorder


_DRIVERS = [
    pytest.param(_drive_post, id="post-execute_request"),
    pytest.param(_drive_get_ssrf_safe, id="get_ssrf_safe"),
]

Driver = Callable[[bytes], tuple[BaseException | None, _HashingRecorder]]


@pytest.mark.parametrize("drive", _DRIVERS)
@pytest.mark.parametrize("body", _INADMISSIBLE_BODIES)
def test_inadmissible_json_body_is_recorded_with_parse_failed_sentinel(drive: Driver, body: bytes) -> None:
    escaped, recorder = drive(body)

    assert escaped is None, f"Tier-3 body escaped the client unrecorded as {type(escaped).__name__}: {escaped}"
    assert len(recorder.calls) == 1, "exactly one audit Call must be recorded for a sent request"
    recorded_body = recorder.calls[0]["response_data"].to_dict()["body"]
    assert recorded_body["_json_parse_failed"] is True
    assert isinstance(recorded_body["_error"], str)
    assert recorded_body["_error"]


@pytest.mark.parametrize("drive", _DRIVERS)
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(b'{"x": 1.5}', {"x": 1.5}, id="finite-float"),
        pytest.param(b"[" * 40 + b"]" * 40, None, id="nested-40"),
        pytest.param(b'{"x": 9007199254740991}', {"x": 9007199254740991}, id="max-safe-int"),
    ],
)
def test_admissible_json_body_is_recorded_unchanged(drive: Driver, body: bytes, expected: object) -> None:
    escaped, recorder = drive(body)

    assert escaped is None
    assert len(recorder.calls) == 1
    recorded_body = recorder.calls[0]["response_data"].to_dict()["body"]
    if expected is None:
        nested: Any = recorded_body
        for _ in range(39):
            assert isinstance(nested, list)
            assert len(nested) == 1
            nested = nested[0]
        assert nested == []
    else:
        assert recorded_body == expected


def test_shared_bounded_parser_is_exported() -> None:
    assert "parse_json_bounded" in vars(json_parser), "contracts.json_parser must export parse_json_bounded"


@pytest.mark.parametrize(
    "text",
    [
        pytest.param('{"x": 1e999}', id="overflow-float"),
        pytest.param("[" * 65 + "]" * 65, id="depth-65"),
        pytest.param('{"x": NaN}', id="nan-literal"),
        pytest.param('{"a": 1, "a": 2}', id="duplicate-key"),
    ],
)
def test_shared_bounded_parser_rejects(text: str) -> None:
    assert "parse_json_bounded" in vars(json_parser)
    parsed, error = json_parser.parse_json_bounded(text, max_depth=64)
    assert parsed is None
    assert error


def test_shared_bounded_parser_accepts_bounded_finite_json() -> None:
    assert "parse_json_bounded" in vars(json_parser)
    assert json_parser.parse_json_bounded('{"x": [1.5, 2]}', max_depth=64) == ({"x": [1.5, 2]}, None)
    nested = "[" * 64 + "]" * 64
    parsed, error = json_parser.parse_json_bounded(nested, max_depth=64)
    assert error is None
    assert parsed is not None
