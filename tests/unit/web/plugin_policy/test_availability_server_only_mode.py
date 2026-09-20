"""Server-only secret mode must reach the plugin-policy inventory too.

``_BoundSecretInventory`` reads the user store directly rather than through
``WebSecretService``, so it needs its own lockdown: otherwise a user-scoped
profile would report available and then fail to resolve at run time.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from tests.fixtures.identities import ensure_test_identity

from elspeth.web.plugin_policy.availability import _BoundSecretInventory
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.fixture(autouse=True)
def _fingerprint_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-availability-fp-key")
    monkeypatch.setenv("SERVER_KEY", "server-value")


@pytest.fixture()
def engine() -> sa.engine.Engine:
    eng = create_session_engine("sqlite:///:memory:")
    initialize_session_schema(eng)
    with eng.begin() as conn:
        ensure_test_identity(conn, identity_id="user-1")
    return eng


def _inventory(engine: sa.engine.Engine, *, user_secrets_enabled: bool) -> _BoundSecretInventory:
    user_store = UserSecretStore(engine=engine, master_key="test-master-key-32chars-minimum!")
    server_store = ServerSecretStore(allowlist=("SERVER_KEY",))
    user_store.set_secret("USER_KEY", value="user-value", user_id="user-1", auth_provider_type="local")
    return _BoundSecretInventory(
        user_id="user-1",
        auth_provider="local",
        service=WebSecretService(user_store, server_store, user_secrets_enabled=user_secrets_enabled),
        server_store=server_store,
        user_store=user_store,
    )


def test_enabled_inventory_sees_the_stored_user_secret(engine: sa.engine.Engine) -> None:
    """Positive control: the row exists and is visible when the flag is on."""
    inventory = _inventory(engine, user_secrets_enabled=True)

    assert inventory.has_user_ref("local:user-1", "USER_KEY") is True
    assert inventory.user_generation("local:user-1", "USER_KEY") is not None


def test_server_only_inventory_hides_the_same_stored_user_secret(engine: sa.engine.Engine) -> None:
    inventory = _inventory(engine, user_secrets_enabled=False)

    assert inventory.has_user_ref("local:user-1", "USER_KEY") is False
    assert inventory.user_generation("local:user-1", "USER_KEY") is None
    assert inventory.has_ref("local:user-1", "USER_KEY") is False
    # Server scope is untouched by the lockdown.
    assert inventory.has_server_ref("SERVER_KEY") is True
    assert inventory.server_generation("SERVER_KEY") is not None
    assert inventory.has_ref("local:user-1", "SERVER_KEY") is True
