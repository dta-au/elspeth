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
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.web.auth import audit as audit_module
from elspeth.web.auth.audit import AuthAuditRecorder, classify_authentication_failure
from elspeth.web.auth.models import AccessPending, AuthenticationError, AuthProviderUnavailable, IdentityDisabled
from elspeth.web.auth.sso import SSO_FAILURE_CATEGORIES, SsoIdpError, SsoLoginError, SsoStateMismatch
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
        # ``sub`` is required: the recorder reads the identity it is
        # attributing from the token itself rather than from a value passed
        # alongside, so a token without one is an audit-integrity error.
        token = jwt.encode(
            {"sub": "identity-1", "iat": 1, "exp": 2},
            "bounded-test-key-that-is-at-least-32-bytes",
            algorithm="HS256",
        )
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
        # ``sub`` is required: the recorder reads the identity it is
        # attributing from the token itself rather than from a value passed
        # alongside, so a token without one is an audit-integrity error.
        token = jwt.encode(
            {"sub": "identity-1", "iat": 1, "exp": 2},
            "bounded-test-key-that-is-at-least-32-bytes",
            algorithm="HS256",
        )
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


def test_identity_retirement_is_recorded_as_an_operator_disable_with_its_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retirement event: one ``identity_disabled`` row, not request-bound, cause in the metadata.

    ``identity_disabled`` is the closed vocabulary's word for what a retirement
    does to the row; the metadata is what tells an administrator this one came
    from a credential deletion and not from an admin's disable.
    """
    db_sentinel = object()

    class _DBContext:
        def __enter__(self) -> object:
            return db_sentinel

        def __exit__(self, *args: object) -> None:
            return None

    class _FakeLandscapeDB:
        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> _DBContext:
            return _DBContext()

    auth_repository = create_autospec(AuthAuditRepository, instance=True)
    factory = SimpleNamespace(auth_audit=auth_repository)
    monkeypatch.setattr(audit_module, "LandscapeDB", _FakeLandscapeDB)
    monkeypatch.setattr(audit_module, "RecorderFactory", create_autospec(audit_module.RecorderFactory, return_value=factory))
    recorder = AuthAuditRecorder(landscape_url="sqlite:///auth-audit.db", landscape_passphrase=None, create_tables=False)

    recorder.record_identity_retired(
        provider="local",
        identity_id="identity-1",
        username="ada",
        retired_subject="ada#retired-identity-1",
        reason="local credential deleted",
    )

    auth_repository.record_auth_event.assert_called_once_with(
        event_type="identity_disabled",
        outcome="success",
        provider="local",
        identity_id="identity-1",
        user_id="ada",
        username="ada",
        failure_category=None,
        request_id=None,
        client_host=None,
        user_agent=None,
        metadata={
            "actor": "operator",
            "cause": "credential_deleted",
            "retired_subject": "ada#retired-identity-1",
            "reason": "local credential deleted",
        },
    )


def test_identity_retirement_audit_failure_propagates_and_is_logged_by_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = LandscapeRecordError("RAW_SQL_MARKER CREDENTIAL_MARKER")

    class _DBContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    class _FakeLandscapeDB:
        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> _DBContext:
            return _DBContext()

    auth_repository = create_autospec(AuthAuditRepository, instance=True)
    auth_repository.record_auth_event.side_effect = failure
    monkeypatch.setattr(audit_module, "LandscapeDB", _FakeLandscapeDB)
    monkeypatch.setattr(
        audit_module,
        "RecorderFactory",
        create_autospec(audit_module.RecorderFactory, return_value=SimpleNamespace(auth_audit=auth_repository)),
    )
    recorder = AuthAuditRecorder(landscape_url="sqlite:///auth-audit.db", landscape_passphrase=None, create_tables=False)

    with capture_logs() as logs, pytest.raises(LandscapeRecordError) as exc_info:
        recorder.record_identity_retired(
            provider="local",
            identity_id="identity-1",
            username="ada",
            retired_subject="ada#retired-identity-1",
            reason="local credential deleted",
        )

    assert exc_info.value is failure
    assert len(logs) == 1
    operation = logs[0]["operation"]
    operation_value = operation.value if isinstance(operation, audit_module.AuthAuditOperation) else operation
    assert operation_value == "identity_retired"
    assert "RAW_SQL_MARKER" not in repr(logs)


class TestAdmissionFailureCategories:
    """A correct password refused at the D12 wall is not a bad credential.

    Recording it as one poisons both trails an administrator reads: the queue
    of people waiting for approval, and the trail that would show a
    brute-force attempt. These four categories are what the login route now
    derives instead of hardcoding ``invalid_credentials``.
    """

    def test_a_pending_identity_is_not_a_credential_failure(self) -> None:
        assert classify_authentication_failure(AccessPending()) == "access_pending"

    def test_a_disabled_identity_is_its_own_category(self) -> None:
        """A revocation taking effect, not a failed guess."""
        assert classify_authentication_failure(IdentityDisabled()) == "identity_disabled"

    def test_rewording_the_message_does_not_change_the_category(self) -> None:
        """The point of classifying on the TYPE.

        A prefix match put the same literal in the raiser and the classifier
        with nothing binding them, so an ordinary copy edit to the message an
        operator reads would silently reclassify the audit event — and a test
        that built the literal itself would keep passing.
        """
        assert classify_authentication_failure(AccessPending("Hold tight, someone is reviewing this")) == "access_pending"
        assert classify_authentication_failure(IdentityDisabled("This account was closed")) == "identity_disabled"

    def test_a_genuinely_bad_credential_keeps_its_category(self) -> None:
        """The control: adding the arms above must not lose this one."""
        exc = AuthenticationError("Invalid credentials")
        assert classify_authentication_failure(exc) == "invalid_credentials"

    def test_an_unverified_email_is_distinguishable(self) -> None:
        exc = AuthenticationError("Email verification required")
        assert classify_authentication_failure(exc) == "email_unverified"

    def test_the_four_admission_outcomes_are_all_distinct(self) -> None:
        """A classifier that collapsed any two would defeat the point."""
        categories = {
            classify_authentication_failure(exc)
            for exc in (
                AccessPending(),
                IdentityDisabled(),
                AuthenticationError("Invalid credentials"),
                AuthenticationError("Email verification required"),
            )
        }
        assert len(categories) == 4


def _every_sso_error_type() -> frozenset[type[SsoLoginError]]:
    """Derived from the class tree, so a new refusal cannot be left untested."""
    found: set[type[SsoLoginError]] = set()
    pending = [SsoLoginError]
    while pending:
        base = pending.pop()
        for sub in base.__subclasses__():
            found.add(sub)
            pending.append(sub)
    return frozenset(found)


_SSO_ERROR_TYPES = sorted(_every_sso_error_type(), key=lambda cls: cls.__name__)


def _issued_token() -> str:
    return jwt.encode({"sub": "identity-1", "iat": 1, "exp": 2}, "bounded-test-key-that-is-at-least-32-bytes", algorithm="HS256")


class TestSsoFailureCategories:
    """SSO refusals carry their category on the TYPE; the classifier reads it.

    A second list of the twelve literals here would be the message-prefix
    drift the admission arms above were rewritten to remove, in another
    form: two copies of one closed set with nothing binding them.
    """

    @pytest.mark.parametrize("error", _SSO_ERROR_TYPES, ids=[cls.__name__ for cls in _SSO_ERROR_TYPES])
    def test_every_sso_refusal_classifies_to_its_own_category(self, error: type[SsoLoginError]) -> None:
        exc = error(reason="other") if error is SsoIdpError else error()
        assert classify_authentication_failure(exc) == error.category
        assert error.category in SSO_FAILURE_CATEGORIES

    def test_rewording_an_sso_message_does_not_change_the_category(self) -> None:
        assert classify_authentication_failure(SsoStateMismatch("Please try that again")) == "sso_state_mismatch"

    def test_a_provider_outage_is_the_spec_named_non_sso_category(self) -> None:
        assert classify_authentication_failure(AuthProviderUnavailable()) == "provider_unavailable"
        assert "provider_unavailable" in SSO_FAILURE_CATEGORIES


class TestSsoRowsCarryTheirJoins:
    """The two rows of one SSO login are written on two requests.

    ``login`` at the callback has no token to derive an identity from, so it
    takes ``identity_id`` explicitly; ``token_issued`` at complete takes the
    callback's request id so an auditor can join the pair without guessing
    from timestamps.
    """

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> Any:
        class _DBContext:
            def __enter__(self) -> object:
                return object()

            def __exit__(self, *args: object) -> None:
                return None

        class _FakeLandscapeDB:
            @classmethod
            def from_url(cls, url: str, **kwargs: object) -> _DBContext:
                del url, kwargs
                return _DBContext()

        auth_repository = create_autospec(AuthAuditRepository, instance=True)
        monkeypatch.setattr(audit_module, "LandscapeDB", _FakeLandscapeDB)
        monkeypatch.setattr(
            audit_module,
            "RecorderFactory",
            create_autospec(RecorderFactory, return_value=SimpleNamespace(auth_audit=auth_repository)),
        )
        return auth_repository

    @staticmethod
    def _recorder() -> AuthAuditRecorder:
        return AuthAuditRecorder(landscape_url="sqlite:///audit.db", landscape_passphrase=None, create_tables=False)

    def test_the_login_row_carries_the_identity_it_was_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repository = self._capture(monkeypatch)
        self._recorder().record_login_success(_request(), provider="oidc", user_id="ada", username="ada", identity_id="id-ada")
        assert repository.record_login_outcome.call_args.kwargs["identity_id"] == "id-ada"

    def test_the_login_row_carries_no_identity_when_none_was_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The local path's existing behaviour, unchanged: nothing is invented."""
        repository = self._capture(monkeypatch)
        self._recorder().record_login_success(_request(), provider="local", user_id="ada", username="ada")
        assert repository.record_login_outcome.call_args.kwargs["identity_id"] is None

    def test_the_token_issued_row_carries_the_login_request_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repository = self._capture(monkeypatch)
        self._recorder().record_token_issued(
            _request(),
            provider="oidc",
            user_id="ada",
            username="ada",
            access_token=_issued_token(),
            issuance_path="sso_complete",
            login_request_id="req-callback-7",
        )
        metadata = repository.record_token_issued.call_args.kwargs["metadata"]
        assert metadata["login_request_id"] == "req-callback-7"
        assert metadata["issuance_path"] == "sso_complete"

    def test_the_token_issued_row_has_no_join_key_when_there_is_nothing_to_join(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A null-valued key would read as 'joined to nothing'; the local path simply has no key."""
        repository = self._capture(monkeypatch)
        self._recorder().record_token_issued(
            _request(), provider="local", user_id="ada", username="ada", access_token=_issued_token(), issuance_path="login"
        )
        assert "login_request_id" not in repository.record_token_issued.call_args.kwargs["metadata"]


# ── Admin mutations and logout (identity sprint step D) ──────────────────


def _durable_recorder(tmp_path: Any) -> tuple[AuthAuditRecorder, str]:
    landscape_url = f"sqlite:///{tmp_path / 'audit.db'}"
    return AuthAuditRecorder(landscape_url=landscape_url, landscape_passphrase=None, create_tables=True), landscape_url


def _durable_rows(landscape_url: str) -> list[Any]:
    from sqlalchemy import select

    from elspeth.core.landscape.database import LandscapeDB
    from elspeth.core.landscape.schema import auth_events_table

    with LandscapeDB.from_url(landscape_url) as db, db.read_only_connection() as conn:
        return list(conn.execute(select(auth_events_table).order_by(auth_events_table.c.occurred_at)).fetchall())


def _metadata(row: Any) -> dict[str, Any]:
    import json

    return json.loads(row.metadata_json)


_PROVENANCE: dict[str, object] = {"on_behalf_of": None, "console_request_id": None}


def test_admin_provenance_keys_are_the_l0_pinned_names() -> None:
    """The writer and every reader must name the same keys (spec rev2.2); the contract owns them."""
    from elspeth.contracts.auth import AUTH_EVENT_CONSOLE_REQUEST_ID_KEY, AUTH_EVENT_ON_BEHALF_OF_KEY

    assert set(_PROVENANCE) == {AUTH_EVENT_ON_BEHALF_OF_KEY, AUTH_EVENT_CONSOLE_REQUEST_ID_KEY}


def test_activation_writes_identity_role_and_quota_rows_in_order_with_the_request(tmp_path: Any) -> None:
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_identity_activated(
        _request(),
        provider="oidc",
        identity_id="identity-1",
        username="ada",
        actor_identity_id="identity-admin",
        cause="admin_activation",
        note="approved",
        role="user",
        role_id="role-1",
        tokens_per_day=1000,
        storage_bytes=2000,
        on_behalf_of=None,
        console_request_id=None,
    )
    rows = _durable_rows(url)
    assert [row.event_type for row in rows] == ["identity_activated", "role_granted", "quota_set"]
    for row in rows:
        assert (row.outcome, row.provider, row.identity_id, row.user_id, row.username) == ("success", "oidc", "identity-1", "ada", "ada")
        assert (row.request_id, row.client_host, row.user_agent) == ("request-id", "127.0.0.1", "bounded-agent")
        metadata = _metadata(row)
        assert metadata["actor"] == "identity-admin"
        assert {key: metadata[key] for key in _PROVENANCE} == _PROVENANCE
        assert (metadata["method"], metadata["path"]) == ("POST", "/api/auth/login")
    assert _metadata(rows[0])["cause"] == "admin_activation" and _metadata(rows[0])["note"] == "approved"
    assert _metadata(rows[1])["role"] == "user" and _metadata(rows[1])["role_id"] == "role-1"
    assert (_metadata(rows[2])["tokens_per_day"], _metadata(rows[2])["storage_bytes"]) == (1000, 2000)


def test_a_request_less_bootstrap_activation_writes_null_request_columns_and_an_operator_actor(tmp_path: Any) -> None:
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_identity_activated(
        None,
        provider="vanguard",
        identity_id="identity-1",
        username="ada",
        actor_identity_id=None,
        cause="bootstrap",
        note="seed",
        role="admin",
        role_id="role-1",
        tokens_per_day=None,
        storage_bytes=None,
        on_behalf_of=None,
        console_request_id=None,
    )
    rows = _durable_rows(url)
    assert [row.event_type for row in rows] == ["identity_activated", "role_granted"]
    for row in rows:
        assert (row.request_id, row.client_host, row.user_agent) == (None, None, None)
        metadata = _metadata(row)
        assert metadata["actor"] == "operator"
        assert "method" not in metadata and "path" not in metadata


def test_console_provenance_is_recorded_when_supplied(tmp_path: Any) -> None:
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_identity_enabled(
        _request(),
        provider="oidc",
        identity_id="identity-1",
        username="ada",
        actor_identity_id="identity-service",
        note="re-enabled from the console",
        on_behalf_of="person@example.gov.au",
        console_request_id="console-42",
    )
    (row,) = _durable_rows(url)
    assert row.event_type == "identity_enabled"
    metadata = _metadata(row)
    assert (metadata["on_behalf_of"], metadata["console_request_id"]) == ("person@example.gov.au", "console-42")
    assert metadata["note"] == "re-enabled from the console"


def test_disable_role_and_relationship_rows(tmp_path: Any) -> None:
    from datetime import UTC, datetime

    recorder, url = _durable_recorder(tmp_path)
    recorder.record_identity_disabled(
        _request(),
        provider="oidc",
        identity_id="identity-1",
        username="ada",
        actor_identity_id="identity-admin",
        reason="left",
        revoked_relationship_ids=("rel-1", "rel-2"),
        on_behalf_of=None,
        console_request_id=None,
    )
    recorder.record_role_changed(
        _request(),
        provider="oidc",
        identity_id="identity-1",
        username=None,
        actor_identity_id="identity-admin",
        change="granted",
        role="approver",
        role_id="role-1",
        scope=None,
        expires_at=datetime(2027, 1, 1, tzinfo=UTC),
        note="lead",
        on_behalf_of=None,
        console_request_id=None,
    )
    recorder.record_role_changed(
        None,
        provider="oidc",
        identity_id="identity-1",
        username=None,
        actor_identity_id="identity-admin",
        change="revoked",
        role="approver",
        role_id="role-1",
        scope=None,
        expires_at=None,
        note=None,
        on_behalf_of=None,
        console_request_id=None,
    )
    recorder.record_relationship_changed(
        _request(),
        provider="oidc",
        actor_identity_id="identity-admin",
        change="asserted",
        relationship_id="rel-9",
        from_identity_id="identity-approver",
        to_identity_id="identity-1",
        relationship_type="approver",
        note="org chart",
        on_behalf_of=None,
        console_request_id=None,
    )
    recorder.record_relationship_changed(
        _request(),
        provider="oidc",
        actor_identity_id="identity-admin",
        change="revoked",
        relationship_id="rel-9",
        from_identity_id="identity-approver",
        to_identity_id="identity-1",
        relationship_type="approver",
        note=None,
        on_behalf_of=None,
        console_request_id=None,
    )
    rows = _durable_rows(url)
    assert [row.event_type for row in rows] == [
        "identity_disabled",
        "role_granted",
        "role_revoked",
        "relationship_asserted",
        "relationship_revoked",
    ]
    disabled = _metadata(rows[0])
    assert (disabled["cause"], disabled["reason"], disabled["revoked_relationship_ids"]) == ("admin_disable", "left", ["rel-1", "rel-2"])
    granted = _metadata(rows[1])
    assert (granted["role"], granted["role_id"], granted["expires_at"], granted["note"]) == (
        "approver",
        "role-1",
        "2027-01-01T00:00:00+00:00",
        "lead",
    )
    assert rows[1].user_id is None and rows[1].username is None and rows[1].identity_id == "identity-1"
    assert rows[2].request_id is None and _metadata(rows[2])["expires_at"] is None
    asserted = _metadata(rows[3])
    assert (asserted["relationship_id"], asserted["from_identity_id"], asserted["to_identity_id"]) == (
        "rel-9",
        "identity-approver",
        "identity-1",
    )
    assert rows[3].identity_id == "identity-1", "anchored on the overseen identity"
    assert rows[3].user_id is None and rows[3].username is None
    assert _metadata(rows[4])["note"] is None
    assert all({key: _metadata(row)[key] for key in _PROVENANCE} == _PROVENANCE for row in rows)


def test_logout_writes_a_request_bound_row(tmp_path: Any) -> None:
    recorder, url = _durable_recorder(tmp_path)
    recorder.record_logout(_request(), provider="entra", identity_id="identity-1", username="ada")
    (row,) = _durable_rows(url)
    assert (row.event_type, row.outcome, row.provider, row.identity_id, row.user_id, row.username) == (
        "logout",
        "success",
        "entra",
        "identity-1",
        "ada",
        "ada",
    )
    assert (row.request_id, row.client_host, row.user_agent) == ("request-id", "127.0.0.1", "bounded-agent")
    assert _metadata(row) == {"method": "POST", "path": "/api/auth/login"}


def test_an_admin_mutation_audit_failure_propagates_and_is_logged_by_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    failure = LandscapeRecordError("RAW_SQL_MARKER CREDENTIAL_MARKER")

    class _DBContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    class _FakeLandscapeDB:
        @classmethod
        def from_url(cls, url: str, **kwargs: object) -> _DBContext:
            return _DBContext()

    auth_repository = create_autospec(AuthAuditRepository, instance=True)
    auth_repository.record_auth_event.side_effect = failure
    monkeypatch.setattr(audit_module, "LandscapeDB", _FakeLandscapeDB)
    monkeypatch.setattr(
        audit_module,
        "RecorderFactory",
        create_autospec(audit_module.RecorderFactory, return_value=SimpleNamespace(auth_audit=auth_repository)),
    )
    recorder = AuthAuditRecorder(landscape_url="sqlite:///auth-audit.db", landscape_passphrase=None, create_tables=False)
    with capture_logs() as logs, pytest.raises(LandscapeRecordError) as exc_info:
        recorder.record_identity_activated(
            None,
            provider="local",
            identity_id="identity-1",
            username="ada",
            actor_identity_id=None,
            cause="bootstrap",
            note="seed",
            role=None,
            role_id=None,
            tokens_per_day=None,
            storage_bytes=None,
            on_behalf_of=None,
            console_request_id=None,
        )
    assert exc_info.value is failure
    assert len(logs) == 1
    operation = logs[0]["operation"]
    operation_value = operation.value if isinstance(operation, audit_module.AuthAuditOperation) else operation
    assert operation_value == "identity_activated"
    assert "RAW_SQL_MARKER" not in repr(logs)
