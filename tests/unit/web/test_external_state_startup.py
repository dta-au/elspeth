"""Provider-neutral external PostgreSQL startup contract tests."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretBytes
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from structlog.testing import capture_logs

from elspeth.core.landscape.database import SchemaCompatibilityError
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.web import external_state_startup as startup
from elspeth.web.config import WebSettings
from elspeth.web.deployment_contract import ContractCheck
from elspeth.web.schema_probe import SchemaState
from elspeth.web.sessions.schema import SessionSchemaError

_SENTINEL = "opaque-credential SELECT raw_secret /secret/runtime/path"


def _settings(tmp_path: Path, **overrides: Any) -> WebSettings:
    data_dir = tmp_path / "data"
    payload_dir = tmp_path / "payload"
    blob_dir = data_dir / "blobs"
    for directory in (data_dir, payload_dir, blob_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    values: dict[str, Any] = {
        "deployment_target": "docker-compose",
        "deployment_state_mode": "external-postgresql",
        "host": "0.0.0.0",
        "data_dir": data_dir,
        "payload_store_path": payload_dir,
        "session_db_url": "postgresql+psycopg://runtime:session-secret@db/session",
        "landscape_url": "postgresql+psycopg://runtime:landscape-secret@db/landscape",
        "secret_key": "this-external-startup-secret-is-long-enough",
        "shareable_link_signing_key": SecretBytes(bytes(range(32))),
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
    }
    values.update(overrides)
    return WebSettings(**values)


def _assert_redacted(value: object) -> None:
    rendered = repr(value)
    assert "credential" not in rendered
    assert "raw_secret" not in rendered
    assert "/secret/runtime/path" not in rendered
    assert "session-secret" not in rendered
    assert "landscape-secret" not in rendered


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Connection:
    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _AttemptEngine:
    def __init__(
        self,
        outcomes: list[BaseException | _Connection],
        *,
        clock: _Clock | None = None,
        costs: list[float] | None = None,
    ) -> None:
        self._outcomes = iter(outcomes)
        self._clock = clock
        self._costs = iter(costs or [0.0] * len(outcomes))
        self.connect_calls = 0

    def connect(self) -> _Connection:
        self.connect_calls += 1
        if self._clock is not None:
            self._clock.now += next(self._costs)
        outcome = next(self._outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _DisposableEngine:
    def __init__(self, name: str = "engine", *, dispose_error: BaseException | None = None) -> None:
        self.name = name
        self.dispose_error = dispose_error
        self.dispose_calls = 0

    def dispose(self) -> None:
        self.dispose_calls += 1
        if self.dispose_error is not None:
            raise self.dispose_error


def _operational_error() -> OperationalError:
    return OperationalError("SELECT raw_secret", {"credential": _SENTINEL}, RuntimeError(_SENTINEL))


def test_contract_failure_reports_only_ordered_check_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        startup,
        "validate_external_postgresql_settings",
        lambda _settings: [
            ContractCheck("session_db_url", False, _SENTINEL),
            ContractCheck("session_db_url", False, _SENTINEL),
            ContractCheck("separate_db_targets", False, _SENTINEL),
        ],
    )

    with pytest.raises(startup.ExternalStateStartupContractError) as exc_info:
        startup.enforce_external_state_contract(settings)

    message = str(exc_info.value)
    assert message.index("session_db_url, session_db_url") < message.index("separate_db_targets")
    assert "Run 'elspeth doctor deployment' for full diagnostics." in message
    _assert_redacted(exc_info.value)


def test_contract_enforcement_forwards_pre_resolved_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    captured: dict[str, object] = {}

    def validate(_settings: WebSettings, *, resolved_state_mode: str | None = None) -> list[ContractCheck]:
        captured["resolved_state_mode"] = resolved_state_mode
        return []

    monkeypatch.setattr(startup, "validate_external_postgresql_settings", validate)

    startup.enforce_external_state_contract(settings, resolved_state_mode="external-postgresql")

    assert captured == {"resolved_state_mode": "external-postgresql"}


@pytest.mark.parametrize(
    ("mutate", "label", "env_var"),
    [
        (
            lambda settings: ((settings.data_dir / "blobs").rmdir(), settings.data_dir.rmdir()),
            "data_dir",
            "ELSPETH_WEB__DATA_DIR",
        ),
        (lambda settings: settings.payload_store_path.rmdir(), "payload_store", "ELSPETH_WEB__PAYLOAD_STORE_PATH"),
        (lambda settings: (settings.data_dir / "blobs").rmdir(), "blob", "ELSPETH_WEB__DATA_DIR"),
    ],
)
def test_each_missing_runtime_directory_fails_without_writing(
    tmp_path: Path,
    mutate: Callable[[WebSettings], None],
    label: str,
    env_var: str,
) -> None:
    settings = _settings(tmp_path)
    mutate(settings)
    auth_db = settings.data_dir / "auth.db"

    with pytest.raises(startup.ExternalStateStartupContractError) as exc_info:
        startup.require_runtime_directories_mounted(settings)

    assert label in str(exc_info.value)
    assert env_var in str(exc_info.value)
    assert auth_db.exists() is False
    assert str(settings.data_dir) not in str(exc_info.value)
    assert "doctor deployment" in str(exc_info.value)
    _assert_redacted(exc_info.value)


def test_raw_payload_path_none_fails_without_using_fallback(tmp_path: Path) -> None:
    settings = _settings(tmp_path, payload_store_path=None)

    with pytest.raises(startup.ExternalStateStartupContractError, match="payload_store"):
        startup.require_runtime_directories_mounted(settings)


def test_payload_symlink_and_unsafe_mode_are_rejected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert settings.payload_store_path is not None
    target = tmp_path / "payload-target"
    settings.payload_store_path.rmdir()
    target.mkdir(mode=0o700)
    settings.payload_store_path.symlink_to(target, target_is_directory=True)

    with pytest.raises(startup.ExternalStateStartupContractError, match="payload_store"):
        startup.require_runtime_directories_mounted(settings)
    with pytest.raises(ValueError, match="symlink"):
        FilesystemPayloadStore(settings.payload_store_path)

    settings.payload_store_path.unlink()
    settings.payload_store_path.mkdir(mode=0o720)
    settings.payload_store_path.chmod(0o720)
    with pytest.raises(startup.ExternalStateStartupContractError, match="payload_store"):
        startup.require_runtime_directories_mounted(settings)


def test_correctly_private_runtime_directories_pass(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    startup.require_runtime_directories_mounted(settings)


@pytest.mark.parametrize(
    ("directory_of", "label"),
    [
        (lambda settings: settings.data_dir, "data_dir"),
        (lambda settings: settings.data_dir / "blobs", "blob"),
    ],
)
@pytest.mark.parametrize("mode", [0o770, 0o707, 0o777])
def test_group_or_world_writable_data_or_blob_directory_is_rejected(
    tmp_path: Path,
    directory_of: Callable[[WebSettings], Path],
    label: str,
    mode: int,
) -> None:
    settings = _settings(tmp_path)
    target = directory_of(settings)
    target.chmod(mode)

    with pytest.raises(startup.ExternalStateStartupContractError, match=label) as exc_info:
        startup.require_runtime_directories_mounted(settings)

    assert "ELSPETH_WEB__DATA_DIR" in str(exc_info.value)
    _assert_redacted(exc_info.value)


@pytest.mark.parametrize(
    ("directory_of", "label"),
    [
        (lambda settings: settings.data_dir, "data_dir"),
        (lambda settings: settings.data_dir / "blobs", "blob"),
    ],
)
def test_symlinked_data_or_blob_directory_is_rejected(
    tmp_path: Path,
    directory_of: Callable[[WebSettings], Path],
    label: str,
) -> None:
    settings = _settings(tmp_path)
    target = directory_of(settings)
    replacement_target = tmp_path / f"{label}-replacement-target"
    replacement_target.mkdir(mode=0o700)
    if label == "data_dir":
        (settings.data_dir / "blobs").rmdir()
    target.rmdir()
    target.symlink_to(replacement_target, target_is_directory=True)

    with pytest.raises(startup.ExternalStateStartupContractError, match=label):
        startup.require_runtime_directories_mounted(settings)


@pytest.mark.parametrize("operation", ["lstat", "resolve"])
def test_secret_bearing_path_failures_are_static(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    settings = _settings(tmp_path)
    assert settings.payload_store_path is not None
    original = getattr(Path, operation)

    def fail(target: Path, *args: object, **kwargs: object) -> object:
        if target == settings.payload_store_path:
            raise OSError(_SENTINEL)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail)
    with capture_logs() as logs, pytest.raises(startup.ExternalStateStartupContractError) as exc_info:
        startup.require_runtime_directories_mounted(settings)

    _assert_redacted(exc_info.value)
    _assert_redacted(logs)


def test_operational_retries_use_new_connections_and_exponential_backoff() -> None:
    clock = _Clock()
    third = _Connection("third")
    engine = _AttemptEngine([_operational_error(), _operational_error(), third], clock=clock)
    probed: list[_Connection] = []

    state = startup._probe_with_connection_budget(
        engine,  # type: ignore[arg-type]
        lambda conn: probed.append(conn) or SchemaState.CURRENT,
        label="session_schema",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert state is SchemaState.CURRENT
    assert engine.connect_calls == 3
    assert probed == [third]
    assert clock.sleeps == [1.0, 2.0]


def test_backoff_caps_and_reserves_one_connection_timeout() -> None:
    clock = _Clock()
    engine = _AttemptEngine(
        [_operational_error(), _operational_error(), _operational_error(), _operational_error(), _operational_error(), _Connection("ok")],
        clock=clock,
        costs=[0, 0, 0, 0, 18, 0],
    )

    state = startup._probe_with_connection_budget(
        engine,  # type: ignore[arg-type]
        lambda _conn: SchemaState.CURRENT,
        label="landscape_schema",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert state is SchemaState.CURRENT
    assert clock.sleeps == [1.0, 2.0, 4.0, 8.0, 2.0]
    assert clock.now == 35.0


@pytest.mark.parametrize("elapsed", [36.0, 45.0])
def test_no_attempt_starts_without_reserved_timeout_or_at_deadline(elapsed: float) -> None:
    clock = _Clock()
    engine = _AttemptEngine([_operational_error(), _Connection("must-not-connect")], clock=clock, costs=[elapsed, 0])

    with pytest.raises(startup.ExternalStateSchemaNotReadyError) as exc_info:
        startup._probe_with_connection_budget(
            engine,  # type: ignore[arg-type]
            lambda _conn: SchemaState.CURRENT,
            label="session_schema",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert engine.connect_calls == 1
    assert clock.sleeps == []
    assert exc_info.value.__cause__ is None
    assert "doctor deployment" in str(exc_info.value)
    _assert_redacted(exc_info.value)


@pytest.mark.parametrize(
    "error",
    [SQLAlchemyError(_SENTINEL), SessionSchemaError(_SENTINEL), SchemaCompatibilityError(_SENTINEL)],
)
def test_database_and_schema_errors_are_translated_without_retry(error: BaseException) -> None:
    clock = _Clock()
    engine = _AttemptEngine([_Connection("only")], clock=clock)

    with pytest.raises(startup.ExternalStateSchemaNotReadyError) as exc_info:
        startup._probe_with_connection_budget(
            engine,  # type: ignore[arg-type]
            lambda _conn: (_ for _ in ()).throw(error),
            label="landscape_schema",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    assert engine.connect_calls == 1
    assert exc_info.value.__cause__ is None
    _assert_redacted(exc_info.value)


def test_programming_and_base_exceptions_propagate_without_translation() -> None:
    clock = _Clock()
    for error in (TypeError("programmer bug"), KeyboardInterrupt()):
        engine = _AttemptEngine([_Connection("only")], clock=clock)
        with pytest.raises(type(error)):
            startup._probe_with_connection_budget(
                engine,  # type: ignore[arg-type]
                lambda _conn, error=error: (_ for _ in ()).throw(error),
                label="session_schema",
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )
        assert engine.connect_calls == 1


def test_retry_log_event_and_attributes_are_bounded_and_redacted() -> None:
    clock = _Clock()
    engine = _AttemptEngine([_operational_error()], clock=clock, costs=[45])

    with capture_logs() as logs, pytest.raises(startup.ExternalStateSchemaNotReadyError):
        startup._probe_with_connection_budget(
            engine,  # type: ignore[arg-type]
            lambda _conn: SchemaState.CURRENT,
            label="session_schema",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )

    _assert_redacted(logs)
    assert len(logs) == 1
    assert set(logs[0]) == {"event", "log_level", "label", "attempt", "elapsed_seconds", "exc_class"}
    assert logs[0]["event"] == "external_state_schema_probe_retry"
    assert logs[0]["attempt"] == 1
    assert logs[0]["exc_class"] == "OperationalError"


def test_validate_only_probes_session_then_landscape_and_disposes_only_owned_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    session_engine = _DisposableEngine("session")
    landscape_engine = _DisposableEngine("landscape")
    order: list[str] = []
    created: list[tuple[str, dict[str, object]]] = []

    def make_engine(url: str, **kwargs: object) -> _DisposableEngine:
        created.append((url, kwargs))
        return landscape_engine

    def probe(engine: _DisposableEngine, callback: Callable[[object], SchemaState], *, label: str, **_kwargs: object) -> SchemaState:
        order.append(label)
        expected = startup.probe_session_schema if label == "session_schema" else startup.probe_landscape_schema
        assert callback is expected
        assert engine is (session_engine if label == "session_schema" else landscape_engine)
        return SchemaState.CURRENT

    monkeypatch.setattr(startup, "create_engine", make_engine)
    monkeypatch.setattr(startup, "_probe_with_connection_budget", probe)

    startup.validate_only_schema_or_raise(settings, session_engine)  # type: ignore[arg-type]

    assert order == ["session_schema", "landscape_schema"]
    assert created == [
        (
            settings.landscape_url,
            {"connect_args": {"connect_timeout": 10}, "pool_size": 5, "max_overflow": 5, "pool_pre_ping": True},
        )
    ]
    assert landscape_engine.dispose_calls == 1
    assert session_engine.dispose_calls == 0


@pytest.mark.parametrize("state", [SchemaState.MISSING, SchemaState.PARTIAL, SchemaState.STALE])
def test_session_noncurrent_stops_before_constructing_landscape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: SchemaState,
) -> None:
    settings = _settings(tmp_path)
    session_engine = _DisposableEngine("session")
    monkeypatch.setattr(startup, "_probe_with_connection_budget", lambda *_args, **_kwargs: state)
    monkeypatch.setattr(startup, "create_engine", lambda *_args, **_kwargs: pytest.fail("must not construct Landscape engine"))

    with pytest.raises(startup.ExternalStateSchemaNotReadyError, match="session_schema"):
        startup.validate_only_schema_or_raise(settings, session_engine)  # type: ignore[arg-type]

    assert session_engine.dispose_calls == 0


@pytest.mark.parametrize(
    "error",
    [startup.ExternalStateSchemaNotReadyError("static"), KeyboardInterrupt(), SystemExit(2)],
)
def test_constructed_landscape_engine_is_disposed_under_base_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    settings = _settings(tmp_path)
    session_engine = _DisposableEngine("session")
    landscape_engine = _DisposableEngine("landscape")
    calls = 0

    def probe(*_args: object, **_kwargs: object) -> SchemaState:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SchemaState.CURRENT
        raise error

    monkeypatch.setattr(startup, "_probe_with_connection_budget", probe)
    monkeypatch.setattr(startup, "create_engine", lambda *_args, **_kwargs: landscape_engine)

    with pytest.raises(type(error)):
        startup.validate_only_schema_or_raise(settings, session_engine)  # type: ignore[arg-type]

    assert landscape_engine.dispose_calls == 1
    assert session_engine.dispose_calls == 0


@pytest.mark.parametrize(
    "primary",
    [KeyboardInterrupt(), SystemExit(2), startup.ExternalStateSchemaNotReadyError("static primary")],
)
def test_dispose_failure_preserves_primary_base_exception_and_logs_bounded_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary: BaseException,
) -> None:
    settings = _settings(tmp_path)
    session_engine = _DisposableEngine("session")
    cleanup_error = RuntimeError(_SENTINEL)
    landscape_engine = _DisposableEngine("landscape", dispose_error=cleanup_error)
    calls = 0

    def probe(*_args: object, **_kwargs: object) -> SchemaState:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SchemaState.CURRENT
        raise primary

    monkeypatch.setattr(startup, "_probe_with_connection_budget", probe)
    monkeypatch.setattr(startup, "create_engine", lambda *_args, **_kwargs: landscape_engine)

    with capture_logs() as logs, pytest.raises(type(primary)) as exc_info:
        startup.validate_only_schema_or_raise(settings, session_engine)  # type: ignore[arg-type]

    assert exc_info.value is primary
    assert landscape_engine.dispose_calls == 1
    assert session_engine.dispose_calls == 0
    _assert_redacted(logs)
    assert logs == [
        {
            "event": "external_state_engine_disposal_failed",
            "log_level": "error",
            "label": "landscape_schema",
            "original_exc_class": type(primary).__name__,
            "cleanup_exc_class": "RuntimeError",
        }
    ]


def test_standalone_dispose_failure_is_translated_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    session_engine = _DisposableEngine("session")
    landscape_engine = _DisposableEngine("landscape", dispose_error=RuntimeError(_SENTINEL))
    monkeypatch.setattr(startup, "_probe_with_connection_budget", lambda *_args, **_kwargs: SchemaState.CURRENT)
    monkeypatch.setattr(startup, "create_engine", lambda *_args, **_kwargs: landscape_engine)

    with capture_logs() as logs, pytest.raises(startup.ExternalStateSchemaNotReadyError) as exc_info:
        startup.validate_only_schema_or_raise(settings, session_engine)  # type: ignore[arg-type]

    assert "landscape_schema" in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert landscape_engine.dispose_calls == 1
    assert session_engine.dispose_calls == 0
    _assert_redacted(exc_info.value)
    _assert_redacted(logs)
    assert logs == [
        {
            "event": "external_state_engine_disposal_failed",
            "log_level": "error",
            "label": "landscape_schema",
            "original_exc_class": None,
            "cleanup_exc_class": "RuntimeError",
        }
    ]


def test_validate_only_module_has_no_schema_creation_or_ddl_imports() -> None:
    source = inspect.getsource(startup)

    for forbidden in (
        "init_session_schema",
        "init_landscape_schema",
        "_create_session_tables",
        "create_additive_indexes",
        "metadata.create_all",
    ):
        assert forbidden not in source
