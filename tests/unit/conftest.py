# tests/unit/conftest.py
"""Unit test configuration.

Unit tests verify component logic in isolation from external services.
In-memory SQLite (LandscapeDB.in_memory()) is permitted for audit-trail
verification — it is fast, deterministic, requires no external service,
and avoids the anti-pattern of testing mocks instead of behavior.

Provides shared in-memory fixtures (payload store, plugin manager).
"""

import pytest

from elspeth.contracts.payload_store import PayloadStore
from elspeth.plugins.infrastructure.manager import PluginManager
from tests.fixtures.stores import MockPayloadStore

# The composer's strict-transport resolver (S1 T8) reads these base-URL and
# api-version variables, and pytest loads ``.env``; scrub them so a unit
# test's tool wire never depends on the developer's shell. Tests that need one
# set it with ``monkeypatch.setenv``.
_COMPOSER_ROUTE_ENV = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENROUTER_API_BASE", "AZURE_API_VERSION")


@pytest.fixture(autouse=True)
def _scrub_composer_route_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _COMPOSER_ROUTE_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def payload_store() -> PayloadStore:
    """In-memory PayloadStore for tests that need artifact storage."""
    return MockPayloadStore()


@pytest.fixture
def plugin_manager() -> PluginManager:
    """Standard plugin manager with builtin plugins registered."""
    manager = PluginManager()
    manager.register_builtin_plugins()
    return manager
