"""Regression tests for the P3 sweep findings in ``plugins/infrastructure/clients``.

Sweep 20261111-plugins-review, lane p3-clients.
"""

from __future__ import annotations

import importlib.util
from typing import Any
from unittest.mock import patch

import pytest

import elspeth.plugins.infrastructure.clients as clients_pkg
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
