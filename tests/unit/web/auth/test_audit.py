"""Deployment-policy and fail-closed tests for web authentication audit writes."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import jwt
import pytest
from fastapi import Request
from structlog.testing import capture_logs

from elspeth.core.landscape.auth_audit_repository import AuthAuditRepository
from elspeth.core.landscape.database import SchemaCompatibilityError
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.web.auth import audit as audit_module
from elspeth.web.auth.audit import AuthAuditRecorder
from elspeth.web.schema_probe import EXTERNAL_POSTGRES_POOL_KWARGS

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


def _settings(deployment_target: str, state_mode: str) -> Any:
    external = state_mode == "external-postgresql"
    landscape_url = "postgresql+psycopg://runtime@db/landscape" if external else "sqlite:///auth-audit.db"
    session_url = "postgresql+psycopg://runtime@db/session" if external else "sqlite:///sessions.db"
    return SimpleNamespace(
        deployment_target=deployment_target,
        deployment_state_mode=state_mode,
        landscape_url=landscape_url,
        session_db_url=session_url,
        landscape_passphrase=None,
        get_landscape_url=lambda: landscape_url,
        get_session_db_url=lambda: session_url,
    )


@pytest.mark.parametrize(("deployment_target", "state_mode", "expected"), _STATE_POLICY_MATRIX)
def test_from_settings_schema_policy_follows_resolved_state_mode(
    deployment_target: str,
    state_mode: str,
    expected: bool,
) -> None:
    recorder = AuthAuditRecorder.from_settings(_settings(deployment_target, state_mode))

    assert recorder.create_tables is expected


def test_from_settings_external_mode_retains_raw_explicit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings("kubernetes", "external-postgresql")
    raw_url = settings.landscape_url
    settings.get_landscape_url = lambda: pytest.fail("fallback getter must not supply external Landscape URL")
    monkeypatch.setattr(audit_module, "resolve_deployment_state_mode", lambda _settings: "external-postgresql", raising=False)

    recorder = AuthAuditRecorder.from_settings(settings)

    assert recorder.landscape_url == raw_url


def test_direct_construction_requires_create_tables_policy() -> None:
    with pytest.raises(TypeError):
        AuthAuditRecorder(
            landscape_url="sqlite:///auth-audit.db",
            landscape_passphrase=None,
        )


def test_external_recorder_open_forwards_postgres_engine_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    open_calls: list[tuple[str, dict[str, object]]] = []

    class _DBContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    class _FakeLandscapeDB:
        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> _DBContext:
            open_calls.append((url, kwargs))
            return _DBContext()

    landscape_url = "postgresql+psycopg://runtime@db/landscape"
    monkeypatch.setattr(audit_module, "LandscapeDB", _FakeLandscapeDB)
    recorder = AuthAuditRecorder(
        landscape_url=landscape_url,
        landscape_passphrase=None,
        create_tables=False,
    )

    with recorder._open_landscape(audit_module.AuthAuditOperation.LOGIN_FAILURE):
        pass

    assert open_calls == [
        (
            landscape_url,
            {
                "passphrase": None,
                "create_tables": False,
                **EXTERNAL_POSTGRES_POOL_KWARGS,
            },
        )
    ]


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [(b"user-agent", b"bounded-agent")],
            "client": ("127.0.0.1", 12345),
        }
    )
    request.state.request_id = "request-id"
    return request


def _writer_kwargs(method_name: str) -> dict[str, object]:
    common: dict[str, object] = {"provider": "local"}
    if method_name == "record_login_success_and_token_issued":
        token = jwt.encode({"iat": 1, "exp": 2}, "bounded-test-key-that-is-at-least-32-bytes", algorithm="HS256")
        return {
            **common,
            "user_id": "user-1",
            "username": "alice",
            "access_token": token,
        }
    if method_name == "record_login_success":
        return {**common, "user_id": "user-1", "username": "alice"}
    if method_name == "record_login_failure":
        return {**common, "username": "alice", "failure_category": "invalid_credentials"}
    if method_name == "record_token_issued":
        token = jwt.encode({"iat": 1, "exp": 2}, "bounded-test-key-that-is-at-least-32-bytes", algorithm="HS256")
        return {
            **common,
            "user_id": "user-1",
            "username": "alice",
            "access_token": token,
            "issuance_path": "login",
        }
    if method_name == "record_auth_failure":
        return {
            **common,
            "failure_category": "invalid_token",
            "failure_stage": "authenticate",
            "user_id": None,
            "username": None,
            "exception_class": "AuthenticationError",
        }
    raise AssertionError(f"unknown writer {method_name}")


def _recorder_writer(recorder: AuthAuditRecorder, method_name: str) -> Callable[..., None]:
    if method_name == "record_login_success_and_token_issued":
        return recorder.record_login_success_and_token_issued
    if method_name == "record_login_success":
        return recorder.record_login_success
    if method_name == "record_login_failure":
        return recorder.record_login_failure
    if method_name == "record_token_issued":
        return recorder.record_token_issued
    if method_name == "record_auth_failure":
        return recorder.record_auth_failure
    raise AssertionError(f"unknown writer {method_name}")


def _repository_writer(repository: AuthAuditRepository, method_name: str) -> Any:
    if method_name == "record_login_success_and_token_issued":
        return repository.record_login_success_and_token_issued
    if method_name == "record_login_outcome":
        return repository.record_login_outcome
    if method_name == "record_token_issued":
        return repository.record_token_issued
    if method_name == "record_auth_failure":
        return repository.record_auth_failure
    raise AssertionError(f"unknown repository writer {method_name}")


@pytest.mark.parametrize(
    ("method_name", "repository_method"),
    [
        ("record_login_success_and_token_issued", "record_login_success_and_token_issued"),
        ("record_login_success", "record_login_outcome"),
        ("record_token_issued", "record_token_issued"),
        ("record_auth_failure", "record_auth_failure"),
        ("record_login_failure", "record_login_outcome"),
    ],
)
def test_every_writer_forwards_required_create_tables_policy(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    repository_method: str,
) -> None:
    open_calls: list[tuple[str, dict[str, object]]] = []
    db_sentinel = object()

    class _DBContext:
        def __enter__(self) -> object:
            return db_sentinel

        def __exit__(self, *args: object) -> None:
            return None

    class _FakeLandscapeDB:
        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> _DBContext:
            open_calls.append((url, kwargs))
            return _DBContext()

    auth_repository = create_autospec(AuthAuditRepository, instance=True)
    factory = SimpleNamespace(auth_audit=auth_repository)
    monkeypatch.setattr(audit_module, "LandscapeDB", _FakeLandscapeDB)
    monkeypatch.setattr(
        audit_module,
        "RecorderFactory",
        create_autospec(audit_module.RecorderFactory, return_value=factory),
    )
    recorder = AuthAuditRecorder(
        landscape_url="sqlite:///auth-audit.db",
        landscape_passphrase=None,
        create_tables=False,
    )

    _recorder_writer(recorder, method_name)(_request(), **_writer_kwargs(method_name))

    assert open_calls == [
        (
            "sqlite:///auth-audit.db",
            {"passphrase": None, "create_tables": False},
        )
    ]
    _repository_writer(auth_repository, repository_method).assert_called_once()


_OPERATION_NAMES = {
    "record_login_success_and_token_issued": "login_success_and_token_issued",
    "record_login_success": "login_success",
    "record_token_issued": "token_issued",
    "record_auth_failure": "auth_failure",
    "record_login_failure": "login_failure",
}


@pytest.mark.parametrize(
    ("method_name", "repository_method"),
    [
        ("record_login_success_and_token_issued", "record_login_success_and_token_issued"),
        ("record_login_success", "record_login_outcome"),
        ("record_token_issued", "record_token_issued"),
        ("record_auth_failure", "record_auth_failure"),
        ("record_login_failure", "record_login_outcome"),
    ],
)
@pytest.mark.parametrize("failure_location", ["open", "repository"])
def test_every_writer_propagates_and_redacts_expected_database_failures(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    repository_method: str,
    failure_location: str,
) -> None:
    failure: Exception
    if failure_location == "open":
        failure = SchemaCompatibilityError("RAW_SQL_MARKER CREDENTIAL_MARKER")
    else:
        failure = LandscapeRecordError("RAW_SQL_MARKER CREDENTIAL_MARKER")
    db_sentinel = object()

    class _DBContext:
        def __enter__(self) -> object:
            return db_sentinel

        def __exit__(self, *args: object) -> None:
            return None

    class _FakeLandscapeDB:
        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> _DBContext:
            del url, kwargs
            if failure_location == "open":
                raise failure
            return _DBContext()

    auth_repository = create_autospec(AuthAuditRepository, instance=True)
    if failure_location == "repository":
        _repository_writer(auth_repository, repository_method).side_effect = failure
    monkeypatch.setattr(audit_module, "LandscapeDB", _FakeLandscapeDB)
    monkeypatch.setattr(
        audit_module,
        "RecorderFactory",
        create_autospec(
            audit_module.RecorderFactory,
            return_value=SimpleNamespace(auth_audit=auth_repository),
        ),
    )
    recorder = AuthAuditRecorder(
        landscape_url="sqlite:///SENSITIVE_URL_MARKER.db",
        landscape_passphrase="PASSPHRASE_MARKER",
        create_tables=False,
    )

    with capture_logs() as logs, pytest.raises(type(failure)) as exc_info:
        _recorder_writer(recorder, method_name)(_request(), **_writer_kwargs(method_name))

    assert exc_info.value is failure
    assert len(logs) == 1
    log = logs[0]
    assert log["event"] == "auth_audit_write_failed"
    operation = log["operation"]
    operation_value = operation.value if isinstance(operation, audit_module.AuthAuditOperation) else operation
    assert operation_value == _OPERATION_NAMES[method_name]
    assert log["exception_class"] == type(failure).__name__
    assert set(log) == {"event", "operation", "exception_class", "log_level"}
    rendered = repr(logs)
    for sentinel in (
        "RAW_SQL_MARKER",
        "CREDENTIAL_MARKER",
        "SENSITIVE_URL_MARKER",
        "PASSPHRASE_MARKER",
        "bounded-agent",
        "/api/auth/login",
    ):
        assert sentinel not in rendered
