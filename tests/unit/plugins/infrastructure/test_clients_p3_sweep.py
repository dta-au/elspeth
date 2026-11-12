"""Regression tests for the P3 sweep findings in ``plugins/infrastructure/clients``.

Sweep 20261111-plugins-review, lane p3-clients.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import httpx
import pytest

import elspeth.plugins.infrastructure.clients as clients_pkg
from elspeth.contracts import CallStatus
from elspeth.contracts.call_mode import ReplayCallEvidence
from elspeth.contracts.enums import RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.security.web import SSRFSafeRequest
from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient
from tests.fixtures.mock_audit import mock_audit_authority
from tests.unit.plugins.clients.test_http import _CallRecorder, _ExecutionRepositoryFake

_CLIENTS = "elspeth.plugins.infrastructure.clients"


# ---------------------------------------------------------------------------
# Finding #13: CallVerifier is an unused verify path that diverges from the
# engine's real verify authority (CallModeSession.verify_call).
# ---------------------------------------------------------------------------


class TestFinding13SingleVerifyAuthority:
    def test_call_verifier_module_is_gone(self) -> None:
        assert importlib.util.find_spec(f"{_CLIENTS}.verifier") is None

    def test_call_verifier_is_not_exported(self) -> None:
        exported = set(clients_pkg.__all__) | set(vars(clients_pkg))
        for name in ("CallVerifier", "VerificationReport", "VerificationResult"):
            assert name not in exported

    def test_package_docstring_does_not_advertise_call_verifier(self) -> None:
        assert clients_pkg.__doc__ is not None
        assert "CallVerifier" not in clients_pkg.__doc__


# ---------------------------------------------------------------------------
# Finding #141: CallReplayer is an unused replay path with weaker integrity
# checks than the engine's real replay (CallModeSession.replay_call).
# ---------------------------------------------------------------------------


class TestFinding141SingleReplayAuthority:
    def test_call_replayer_module_is_gone(self) -> None:
        assert importlib.util.find_spec(f"{_CLIENTS}.replayer") is None

    def test_call_replayer_is_not_exported(self) -> None:
        exported = set(clients_pkg.__all__) | set(vars(clients_pkg))
        for name in ("CallReplayer", "ReplayedCall", "ReplayMissError", "ReplayPayloadMissingError"):
            assert name not in exported

    def test_package_docstring_does_not_advertise_call_replayer(self) -> None:
        assert clients_pkg.__doc__ is not None
        assert "CallReplayer" not in clients_pkg.__doc__


# ---------------------------------------------------------------------------
# Finding #22: request_ssrf_safe followed redirects for body-carrying /
# non-GET requests, and _follow_redirects_safe re-sends every hop as a
# body-less GET (wrong for 307/308).
# ---------------------------------------------------------------------------


class _NetworkReached(Exception):
    """Raised by the patched httpx.Client: the request got past admission."""


def _ssrf_request() -> SSRFSafeRequest:
    return SSRFSafeRequest(
        original_url="https://api.example.com/submit",
        resolved_ip="93.184.216.34",
        host_header="api.example.com",
        port=443,
        path="/submit",
        scheme="https",
        bare_hostname="api.example.com",
    )


def _admission_outcome(method: str, **kwargs: Any) -> str:
    """Return 'refused: <msg>' when admission refuses, 'network' when it dispatches."""
    execution: Any = _ExecutionRepositoryFake()
    client = AuditedHTTPClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="test-state-001",
        run_id="test-run-001",
        telemetry_emit=_CallRecorder(),
    )
    with patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=_NetworkReached("network reached")):
        try:
            client.request_ssrf_safe(method, _ssrf_request(), **kwargs)
        except ValueError as exc:
            return f"refused: {exc}"
        except _NetworkReached:
            return "network"
    return "returned"


class TestFinding22NoRedirectFollowingForBodiesOrNonGet:
    def test_get_without_body_may_follow_redirects(self) -> None:
        # Positive control: the GET redirect path stays open.
        assert _admission_outcome("GET", follow_redirects=True) == "network"

    def test_post_without_redirects_still_dispatches(self) -> None:
        assert _admission_outcome("POST", json={"q": 1}, follow_redirects=False) == "network"

    @pytest.mark.parametrize(
        ("method", "kwargs"),
        [
            pytest.param("POST", {"json": {"q": 1}}, id="post-json"),
            pytest.param("POST", {"form": (("q", "1"),)}, id="post-form"),
            pytest.param("POST", {}, id="post-no-body"),
            pytest.param("PUT", {"json": {"q": 1}}, id="put-json"),
            pytest.param("DELETE", {}, id="delete"),
        ],
    )
    def test_body_or_non_get_request_refuses_redirect_following(self, method: str, kwargs: dict[str, Any]) -> None:
        outcome = _admission_outcome(method, follow_redirects=True, **kwargs)
        assert outcome.split(":")[0] == "refused"
        assert "redirect" in outcome


# ---------------------------------------------------------------------------
# Finding #23: HTTP replay of a recorded transport failure (ERROR, no
# response payload) aborted with AuditIntegrityError instead of re-raising
# the recorded transport error the way LLM replay does.
# ---------------------------------------------------------------------------


class _ErrorReplaySession:
    mode = RunMode.REPLAY
    source_run_id = "source-run"

    def __init__(self, error_data: dict[str, Any]) -> None:
        self._error_data = error_data

    def replay_call(self, **_kwargs: Any) -> ReplayCallEvidence:
        return ReplayCallEvidence("source-call", CallStatus.ERROR, None, self._error_data, 12.0)


def _replay_client(error_data: dict[str, Any]) -> tuple[AuditedHTTPClient, Any]:
    execution: Any = _ExecutionRepositoryFake()
    session: Any = _ErrorReplaySession(error_data)
    client = AuditedHTTPClient(
        **mock_audit_authority(),
        execution=execution,
        state_id="test-state-001",
        run_id="replay-run",
        telemetry_emit=_CallRecorder(),
        call_mode_session=session,
    )
    return client, execution


def _replay_outcome(call: Callable[[AuditedHTTPClient], object], error_data: dict[str, Any]) -> tuple[BaseException | None, Any]:
    with patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("network client constructed")):
        client, execution = _replay_client(error_data)
        try:
            call(client)
        except Exception as exc:
            return exc, execution
    return None, execution


_TRANSPORT_FAILURES = [
    pytest.param("ConnectTimeout", httpx.ConnectTimeout, id="connect-timeout"),
    pytest.param("ReadTimeout", httpx.ReadTimeout, id="read-timeout"),
    pytest.param("ConnectError", httpx.ConnectError, id="connect-error"),
    pytest.param("RemoteProtocolError", httpx.RemoteProtocolError, id="remote-protocol"),
]


class TestFinding23ReplayReraisesRecordedTransportFailure:
    @pytest.mark.parametrize(("type_name", "expected"), _TRANSPORT_FAILURES)
    def test_plain_replay_reraises_recorded_transport_error(self, type_name: str, expected: type[Exception]) -> None:
        exc, execution = _replay_outcome(
            lambda client: client.get("https://api.example.com/raw"),
            {"type": type_name, "message": "recorded transport failure"},
        )
        assert type(exc) is expected
        assert str(exc) == "recorded transport failure"
        # The replay is itself recorded, bound to its source call, as an ERROR.
        assert execution.record_call.call_count == 1
        recorded = execution.record_call.call_args[1]
        assert recorded["source_call_id"] == "source-call"
        assert recorded["status"] is CallStatus.ERROR

    @pytest.mark.parametrize(("type_name", "expected"), _TRANSPORT_FAILURES)
    def test_ssrf_replay_reraises_recorded_transport_error(self, type_name: str, expected: type[Exception]) -> None:
        exc, execution = _replay_outcome(
            lambda client: client.request_ssrf_safe("GET", _ssrf_request(), follow_redirects=True),
            {"type": type_name, "message": "recorded transport failure"},
        )
        assert type(exc) is expected
        assert str(exc) == "recorded transport failure"
        assert execution.record_call.call_count == 1
        recorded = execution.record_call.call_args[1]
        assert recorded["source_call_id"] == "source-call"
        assert recorded["status"] is CallStatus.ERROR

    def test_unreconstructable_recorded_error_still_refuses(self) -> None:
        # Negative control: an error type outside the bounded transport set
        # (e.g. an SSRF refusal whose kind was never recorded) cannot be
        # rebuilt faithfully and stays an audit-integrity refusal.
        exc, execution = _replay_outcome(
            lambda client: client.get("https://api.example.com/raw"),
            {"type": "SSRFBlockedError", "message": "blocked"},
        )
        assert type(exc) is AuditIntegrityError
        assert execution.record_call.call_count == 0
