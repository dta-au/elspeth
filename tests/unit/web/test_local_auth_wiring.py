"""What ``create_app``'s LOCAL auth wiring binds, and what that binding enforces.

``_build_local_auth_provider`` is the second of the two places a container's
identity policy reaches the authority; ``build_sso_wiring`` is the other, and
``tests/unit/web/test_sso_wiring.py`` pins that one. The two are separate
functions with separate callbacks, so a rule wired in one is NOT wired in the
other, and the local half went unpinned: R9's window and the D34 exemption row
could both be deleted from it without a single test going red.

The provider these tests build comes from ``app.py`` itself, never from the
fixture in ``tests/unit/web/auth/conftest.py``. That fixture reassembles the
same collaborators for the provider's own tests and takes its own dormancy
default, so it cannot answer what the app factory passes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretBytes
from sqlalchemy import Engine, select, update

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import auth_events_table
from elspeth.web.app import _build_local_auth_provider
from elspeth.web.auth.local import AccessPending, LocalAuthProvider
from elspeth.web.auth.models import IdentityClaims
from elspeth.web.config import WebSettings
from elspeth.web.coordination.identity_authority import RepositoryIdentityAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import identities_table
from elspeth.web.sessions.schema import initialize_session_schema

pytestmark = pytest.mark.asyncio

_COMPOSER: dict[str, Any] = {
    "composer_max_composition_turns": 15,
    "composer_max_discovery_turns": 10,
    "composer_timeout_seconds": 85.0,
    "composer_rate_limit_per_minute": 10,
    "shareable_link_signing_key": SecretBytes(b"\x00" * 32),
    "secret_key": "dev-secret",
}


def _local_settings(tmp_path: Path, **overrides: Any) -> WebSettings:
    base: dict[str, Any] = {
        "data_dir": tmp_path,
        "auth_provider": "local",
        "registration_mode": "open",
        "quota_default_tokens_per_day": 100_000,
        "quota_default_storage_bytes": 1_000_000,
        **_COMPOSER,
    }
    base.update(overrides)
    return WebSettings(**base)


@pytest.fixture
def substrate(tmp_path: Path) -> tuple[Engine, RepositoryIdentityAuthority]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    return engine, RepositoryIdentityAuthority(engine)


def _provider(settings: WebSettings, authority: RepositoryIdentityAuthority) -> LocalAuthProvider:
    """The provider the app factory builds, with its own recorder and callbacks."""
    (settings.data_dir / "runs").mkdir(parents=True, exist_ok=True)
    return _build_local_auth_provider(settings, authority, resolved_state_mode="sqlite-single")


def _auth_event_rows(settings: WebSettings) -> list[Any]:
    with LandscapeDB.from_url(settings.get_landscape_url()) as db, db.read_only_connection() as conn:
        return list(conn.execute(select(auth_events_table).order_by(auth_events_table.c.occurred_at)).fetchall())


def _backdate_login(engine: Engine, identity_id: str, *, days: int) -> None:
    """Move ``last_login_at`` into the past.

    Dormancy is measured against the DATABASE clock inside the authority's
    transaction, so a test moves the stored login rather than the clock;
    mocking the clock would prove the mock.
    """
    with engine.begin() as conn:
        conn.execute(
            update(identities_table)
            .where(identities_table.c.identity_id == identity_id)
            .values(last_login_at=datetime.now(UTC) - timedelta(days=days))
        )


def _identity_id(authority: RepositoryIdentityAuthority, username: str) -> str:
    record = authority.read_identity_by_natural_key(provider="local", subject=username)
    assert record is not None
    return record.identity_id


async def test_a_dormant_local_login_is_re_pended_and_refused_at_the_admission_wall(
    tmp_path: Path, substrate: tuple[Engine, RepositoryIdentityAuthority]
) -> None:
    """R9 is wired for local auth, not only for SSO -- and the window is the container's.

    R3 excludes local auth on facts about the local subject (it IS the
    username, freeing it retires the identity, an email change would lock the
    person out). None of those says anything about how long an account has sat
    unused, so R9 is NOT excluded, and a dormant local administrator or user
    must lose their live admission exactly as an IdP one does.

    What this pins that no provider-level test can: that
    ``settings.identity_dormancy_days`` is the number the app factory hands
    ``ensure_identity``. Replace it with any constant the operator did not
    choose and the deployment silently stops enforcing its own policy.
    """
    engine, authority = substrate
    settings = _local_settings(tmp_path, identity_dormancy_days=30)
    provider = _provider(settings, authority)
    provider.create_user("ada", "password123", display_name="Ada")
    await provider.login("ada", "password123")
    ada_id = _identity_id(authority, "ada")
    assert authority.read_identity(identity_id=ada_id).access_state == "active"
    _backdate_login(engine, ada_id, days=31)

    with pytest.raises(AccessPending):
        await provider.login("ada", "password123")

    # The state gate is what refuses: the row is pending now, so the D12 wall
    # answers. R9 needs no refusal of its own.
    assert authority.read_identity(identity_id=ada_id).access_state == "pending"
    disabled = [row for row in _auth_event_rows(settings) if row.event_type == "identity_disabled"]
    assert len(disabled) == 1
    assert (disabled[0].outcome, disabled[0].identity_id, disabled[0].provider) == ("success", ada_id, "local")
    metadata = json.loads(disabled[0].metadata_json)
    # The container's window, not a default: 30 is what the settings say.
    assert (metadata["cause"], metadata["state"], metadata["dormancy_days"]) == ("dormant", "pending", 30)


async def test_the_last_local_admins_dormancy_is_exempted_and_only_this_row_records_it(
    tmp_path: Path, substrate: tuple[Engine, RepositoryIdentityAuthority]
) -> None:
    """D34 through the local wiring: nothing changes on the identity, so the row is all there is.

    The exemption row is written by the WIRING, after ``ensure_identity``
    returns, because there is no state change for a failed audit to roll back.
    That makes it deletable without breaking anything the authority asserts --
    which is precisely why it needs a test here rather than only in the
    authority's own suite.
    """
    engine, authority = substrate
    settings = _local_settings(tmp_path, identity_dormancy_days=30)
    provider = _provider(settings, authority)
    provider.create_user("root", "password123", display_name="Root")
    await provider.login("root", "password123")
    root_id = _identity_id(authority, "root")
    authority.bootstrap_admin(
        claims=IdentityClaims(provider="local", subject="root", username="root"),
        note="the first administrator",
        quota_tokens_per_day=None,
        quota_storage_bytes=None,
        record=lambda _event: None,
    )
    assert authority.count_active_human_admins() == 1
    _backdate_login(engine, root_id, days=400)

    # NOT refused: the sole administrator keeps their admission and logs in.
    assert await provider.login("root", "password123")

    assert authority.read_identity(identity_id=root_id).access_state == "active"
    assert authority.count_active_human_admins() == 1
    exempted = [row for row in _auth_event_rows(settings) if row.failure_category == "dormancy_last_admin_exempt"]
    assert len(exempted) == 1
    assert (exempted[0].event_type, exempted[0].outcome, exempted[0].provider) == ("identity_disabled", "failure", "local")
    assert exempted[0].identity_id == root_id
    metadata = json.loads(exempted[0].metadata_json)
    assert (metadata["exemption"], metadata["dormancy_days"]) == ("last_active_human_admin", 30)
    # No re-pend row beside it: a success-outcome dormancy row would assert a
    # state change that did not happen.
    assert [row.outcome for row in _auth_event_rows(settings) if row.event_type == "identity_disabled"] == ["failure"]
