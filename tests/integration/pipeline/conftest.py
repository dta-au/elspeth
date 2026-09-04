"""Pipeline-level integration fixtures."""

from __future__ import annotations

import pytest

from tests.helpers.composer_lease import install_fenced_compose_adapter


@pytest.fixture(autouse=True)
def _fenced_compose_for_legacy_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy characterization tests drive the compose loop with no context; lease one."""
    install_fenced_compose_adapter(monkeypatch)
