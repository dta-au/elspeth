from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from elspeth.web.deployment_contract import DeploymentConfigurationError

_STATE_POLICY_MATRIX = [
    ("default", "sqlite-single", True),
    ("docker-compose", "sqlite-single", True),
    ("linux-systemd", "sqlite-single", True),
    ("default", "external-postgresql", False),
    ("docker-compose", "external-postgresql", False),
    ("linux-systemd", "external-postgresql", False),
    ("aws-ecs", "external-postgresql", False),
    ("azure-container-apps", "external-postgresql", False),
    ("kubernetes", "external-postgresql", False),
]


class _FakeLandscapeDB:
    calls: ClassVar[list[tuple[str, dict[str, object]]]] = []
    sentinel = object()

    @classmethod
    def from_url(cls, url: str, **kwargs: object) -> object:
        cls.calls.append((url, kwargs))
        return cls.sentinel


def _settings(
    deployment_target: str,
    *,
    state_mode: str = "sqlite-single",
    url: str = "sqlite:///landscape.db",
    session_url: str = "sqlite:///sessions.db",
    passphrase: str | None = None,
) -> Any:
    return SimpleNamespace(
        deployment_target=deployment_target,
        deployment_state_mode=state_mode,
        landscape_url=url,
        session_db_url=session_url,
        landscape_passphrase=passphrase,
        get_landscape_url=lambda: url,
        get_session_db_url=lambda: session_url,
    )


@pytest.fixture(autouse=True)
def _patch_landscape_db(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeLandscapeDB.calls = []
    monkeypatch.setattr("elspeth.web.landscape_access.LandscapeDB", _FakeLandscapeDB)


@pytest.mark.parametrize(("deployment_target", "state_mode", "expected"), _STATE_POLICY_MATRIX)
def test_schema_creation_policy_follows_resolved_state_mode(
    deployment_target: str,
    state_mode: str,
    expected: bool,
) -> None:
    from elspeth.web.landscape_access import landscape_create_tables_allowed

    if state_mode == "external-postgresql":
        landscape_url = "postgresql+psycopg://runtime@db/landscape"
        session_url = "postgresql+psycopg://runtime@db/session"
    else:
        landscape_url = "sqlite:///landscape.db"
        session_url = "sqlite:///sessions.db"

    assert (
        landscape_create_tables_allowed(
            _settings(
                deployment_target,
                state_mode=state_mode,
                url=landscape_url,
                session_url=session_url,
            )
        )
        is expected
    )


def test_invalid_target_mode_fails_before_url_or_db_open() -> None:
    from elspeth.web.landscape_access import open_landscape_db

    url_was_read = False

    def _get_url() -> str:
        nonlocal url_was_read
        url_was_read = True
        return "sqlite:///must-not-open.db"

    settings = SimpleNamespace(
        deployment_target="aws-ecs",
        deployment_state_mode="sqlite-single",
        session_db_url="sqlite:///sessions.db",
        landscape_url="sqlite:///must-not-open.db",
        landscape_passphrase=None,
        get_landscape_url=_get_url,
        get_session_db_url=lambda: "sqlite:///sessions.db",
    )

    with pytest.raises(DeploymentConfigurationError, match="external-postgresql"):
        open_landscape_db(settings)

    assert url_was_read is False
    assert _FakeLandscapeDB.calls == []


def test_forwards_url_and_passphrase() -> None:
    from elspeth.web.landscape_access import open_landscape_db

    url = "sqlite:///specific.db"
    passphrase = "passphrase-sentinel"

    open_landscape_db(_settings("default", url=url, passphrase=passphrase))

    assert _FakeLandscapeDB.calls == [
        (url, {"passphrase": passphrase, "create_tables": True}),
    ]


def test_postgres_url_gets_pool_kwargs() -> None:
    from elspeth.web.landscape_access import open_landscape_db
    from elspeth.web.schema_probe import EXTERNAL_POSTGRES_POOL_KWARGS

    open_landscape_db(
        _settings(
            "azure-container-apps",
            state_mode="external-postgresql",
            url="postgresql+psycopg://u@h/landscape",
            session_url="postgresql+psycopg://u@h/session",
        )
    )

    _, kwargs = _FakeLandscapeDB.calls[0]
    assert kwargs.items() >= EXTERNAL_POSTGRES_POOL_KWARGS.items()


def test_external_mode_opens_raw_explicit_landscape_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from elspeth.web import landscape_access

    raw_url = "postgresql+psycopg://runtime@db/landscape"
    settings = _settings(
        "linux-systemd",
        state_mode="external-postgresql",
        url=raw_url,
        session_url="postgresql+psycopg://runtime@db/session",
    )
    settings.get_landscape_url = lambda: pytest.fail("fallback getter must not open the Landscape database")
    monkeypatch.setattr(landscape_access, "resolve_deployment_state_mode", lambda _settings: "external-postgresql")

    landscape_access.open_landscape_db(settings)

    assert _FakeLandscapeDB.calls[0][0] == raw_url


def test_sqlite_url_gets_no_pool_kwargs() -> None:
    from elspeth.web.landscape_access import open_landscape_db

    open_landscape_db(_settings("default", url="sqlite:///x.db"))

    _, kwargs = _FakeLandscapeDB.calls[0]
    assert "pool_size" not in kwargs
