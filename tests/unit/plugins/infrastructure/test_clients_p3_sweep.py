"""Regression tests for the P3 sweep findings in ``plugins/infrastructure/clients``.

Sweep 20261111-plugins-review, lane p3-clients.
"""

from __future__ import annotations

import importlib.util

import elspeth.plugins.infrastructure.clients as clients_pkg

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
