"""Property-test fixtures for Phase 3 compose-loop persistence."""

from __future__ import annotations

import pytest
from sqlalchemy.pool import StaticPool
from tests.helpers.composer_lease import install_fenced_compose_adapter

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.fixture(autouse=True)
def _fenced_compose_for_legacy_tests(monkeypatch):
    """See tests/helpers/composer_lease.py."""
    install_fenced_compose_adapter(monkeypatch)


@pytest.fixture
def populated_audit_db():
    """Real initialized engine reserved for mixed compose-loop audit traces."""

    engine = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(engine)
    return engine
