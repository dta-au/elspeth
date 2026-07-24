"""Tests for the pure AWS ECS deployment contract validator."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretBytes

from elspeth.web import deployment_contract
from elspeth.web.config import WebSettings
from elspeth.web.deployment_contract import ContractCheck, validate_aws_ecs_settings, validate_external_postgresql_settings

_SQLITE_SESSION_URL = "sqlite+pysqlite:///session.db"
_SQLITE_LANDSCAPE_URL = "sqlite:///landscape.db"
_POSTGRESQL_SESSION_URL = "postgresql://session_user:session_password@db/session"  # secret-scan: allow-this-line
_POSTGRESQL_LANDSCAPE_URL = "postgresql+future_driver://landscape_user:landscape_password@db/landscape"


def _base_kwargs() -> dict[str, Any]:
    return {
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "secret_key": "a" * 40,
        "shareable_link_signing_key": bytes(range(32)),
    }


def _checks(**overrides: Any) -> dict[str, ContractCheck]:
    settings = WebSettings(**(_base_kwargs() | overrides))
    return {check.name: check for check in validate_aws_ecs_settings(settings)}


def _settings(**overrides: Any) -> WebSettings:
    return WebSettings(**(_base_kwargs() | overrides))


def _external_settings(tmp_path: Path, *, target: str = "docker-compose", **overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "deployment_target": target,
        "deployment_state_mode": "external-postgresql",
        "session_db_url": "postgresql+psycopg://session_user:session_password@db.example/session_database",
        "landscape_url": "postgresql+psycopg2://landscape_user:landscape_password@db.example/landscape_database",
        "data_dir": tmp_path / "private-data-path",
        "payload_store_path": tmp_path / "private-payload-path",
        "host": "0.0.0.0",
    }
    values.update(overrides)
    return _settings(**values)


_EXTERNAL_CHECK_NAMES = [
    "deployment_target",
    "deployment_state_mode",
    "session_db_url",
    "landscape_url",
    "separate_db_targets",
    "data_dir",
    "payload_store_path",
    "host",
    "secret_key",
    "shareable_link_signing_key",
]


def test_external_postgresql_targets_are_the_cloud_and_orchestrated_targets() -> None:
    assert frozenset({"aws-ecs", "azure-container-apps", "kubernetes"}) == deployment_contract.EXTERNAL_POSTGRESQL_TARGETS


@pytest.mark.parametrize(
    ("target", "overrides", "expected"),
    [
        pytest.param("default", {}, "sqlite-single", id="default-neither-url"),
        pytest.param("default", {"session_db_url": _SQLITE_SESSION_URL}, "sqlite-single", id="default-session-sqlite"),
        pytest.param("default", {"landscape_url": _SQLITE_LANDSCAPE_URL}, "sqlite-single", id="default-landscape-sqlite"),
        pytest.param(
            "default",
            {"session_db_url": _SQLITE_SESSION_URL, "landscape_url": _SQLITE_LANDSCAPE_URL},
            "sqlite-single",
            id="default-both-sqlite",
        ),
        pytest.param("docker-compose", {}, "sqlite-single", id="docker-compose-neither-url"),
        pytest.param("linux-systemd", {}, "sqlite-single", id="linux-systemd-neither-url"),
        pytest.param(
            "default",
            {"session_db_url": _POSTGRESQL_SESSION_URL, "landscape_url": _POSTGRESQL_LANDSCAPE_URL},
            "external-postgresql",
            id="default-both-postgresql",
        ),
        pytest.param(
            "aws-ecs",
            {"session_db_url": _POSTGRESQL_SESSION_URL, "landscape_url": _POSTGRESQL_LANDSCAPE_URL},
            "external-postgresql",
            id="aws-ecs-both-postgresql",
        ),
        pytest.param(
            "azure-container-apps",
            {"session_db_url": _POSTGRESQL_SESSION_URL, "landscape_url": _POSTGRESQL_LANDSCAPE_URL},
            "external-postgresql",
            id="azure-container-apps-both-postgresql",
        ),
        pytest.param(
            "kubernetes",
            {"session_db_url": _POSTGRESQL_SESSION_URL, "landscape_url": _POSTGRESQL_LANDSCAPE_URL},
            "external-postgresql",
            id="kubernetes-both-postgresql",
        ),
    ],
)
def test_auto_mode_resolves_from_target_and_database_dialects(
    target: str,
    overrides: dict[str, str],
    expected: str,
) -> None:
    settings = _settings(deployment_target=target, **overrides)

    assert deployment_contract.resolve_deployment_state_mode(settings) == expected


@pytest.mark.parametrize(
    ("target", "state_mode", "urls"),
    [
        pytest.param(
            "default",
            "sqlite-single",
            {"session_db_url": _SQLITE_SESSION_URL, "landscape_url": _SQLITE_LANDSCAPE_URL},
            id="default-sqlite-single",
        ),
        pytest.param("docker-compose", "sqlite-single", {}, id="docker-compose-sqlite-single"),
        pytest.param("linux-systemd", "sqlite-single", {}, id="linux-systemd-sqlite-single"),
        *[
            pytest.param(
                target,
                "external-postgresql",
                {"session_db_url": _POSTGRESQL_SESSION_URL, "landscape_url": _POSTGRESQL_LANDSCAPE_URL},
                id=f"{target}-external-postgresql",
            )
            for target in (
                "default",
                "docker-compose",
                "linux-systemd",
                "aws-ecs",
                "azure-container-apps",
                "kubernetes",
            )
        ],
    ],
)
def test_explicit_mode_with_matching_urls_resolves_to_that_mode(
    target: str,
    state_mode: str,
    urls: dict[str, str],
) -> None:
    settings = _settings(deployment_target=target, deployment_state_mode=state_mode, **urls)

    assert deployment_contract.resolve_deployment_state_mode(settings) == state_mode


@pytest.mark.parametrize("target", ["aws-ecs", "azure-container-apps", "kubernetes"])
def test_external_targets_reject_sqlite_single_mode(target: str) -> None:
    settings = _settings(
        deployment_target=target,
        deployment_state_mode="sqlite-single",
        session_db_url=_SQLITE_SESSION_URL,
        landscape_url=_SQLITE_LANDSCAPE_URL,
    )

    with pytest.raises(deployment_contract.DeploymentConfigurationError) as caught:
        deployment_contract.resolve_deployment_state_mode(settings)

    detail = str(caught.value)
    assert "deployment_target" in detail
    assert "deployment_state_mode" in detail
    assert _SQLITE_SESSION_URL not in detail
    assert _SQLITE_LANDSCAPE_URL not in detail


@pytest.mark.parametrize("target", ["aws-ecs", "azure-container-apps", "kubernetes"])
def test_external_target_auto_rejects_explicit_sqlite_urls(target: str) -> None:
    settings = _settings(
        deployment_target=target,
        session_db_url=_SQLITE_SESSION_URL,
        landscape_url=_SQLITE_LANDSCAPE_URL,
    )

    with pytest.raises(deployment_contract.DeploymentConfigurationError) as caught:
        deployment_contract.resolve_deployment_state_mode(settings)

    detail = str(caught.value)
    assert "deployment_target" in detail
    assert "deployment_state_mode" in detail
    assert "session_db_url" in detail
    assert "landscape_url" in detail
    assert _SQLITE_SESSION_URL not in detail
    assert _SQLITE_LANDSCAPE_URL not in detail


@pytest.mark.parametrize("target", ["aws-ecs", "azure-container-apps", "kubernetes"])
@pytest.mark.parametrize(
    "urls",
    [
        pytest.param({}, id="neither"),
        pytest.param({"session_db_url": _POSTGRESQL_SESSION_URL}, id="session-only"),
        pytest.param({"landscape_url": _POSTGRESQL_LANDSCAPE_URL}, id="landscape-only"),
        pytest.param(
            {"session_db_url": None, "landscape_url": _POSTGRESQL_LANDSCAPE_URL},
            id="session-explicit-none",
        ),
    ],
)
def test_external_target_auto_requires_two_raw_explicit_urls(target: str, urls: dict[str, str | None]) -> None:
    settings = _settings(deployment_target=target, **urls)

    with pytest.raises(deployment_contract.DeploymentConfigurationError) as caught:
        deployment_contract.resolve_deployment_state_mode(settings)

    detail = str(caught.value)
    assert "session_db_url" in detail
    assert "landscape_url" in detail
    assert _POSTGRESQL_SESSION_URL not in detail
    assert _POSTGRESQL_LANDSCAPE_URL not in detail


@pytest.mark.parametrize(
    "url_field",
    [
        pytest.param("session_db_url", id="session-explicit"),
        pytest.param("landscape_url", id="landscape-explicit"),
    ],
)
def test_local_auto_rejects_one_explicit_postgresql_url(url_field: str) -> None:
    settings = _settings(**{url_field: _POSTGRESQL_SESSION_URL})

    with pytest.raises(deployment_contract.DeploymentConfigurationError) as caught:
        deployment_contract.resolve_deployment_state_mode(settings)

    detail = str(caught.value)
    assert "session_db_url" in detail
    assert "landscape_url" in detail
    assert _POSTGRESQL_SESSION_URL not in detail
    assert "session_password" not in detail


@pytest.mark.parametrize(
    ("session_url", "landscape_url"),
    [
        pytest.param(_SQLITE_SESSION_URL, _POSTGRESQL_LANDSCAPE_URL, id="sqlite-postgresql"),
        pytest.param(_POSTGRESQL_SESSION_URL, _SQLITE_LANDSCAPE_URL, id="postgresql-sqlite"),
    ],
)
def test_auto_rejects_mixed_sqlite_and_postgresql_urls(session_url: str, landscape_url: str) -> None:
    settings = _settings(session_db_url=session_url, landscape_url=landscape_url)

    with pytest.raises(deployment_contract.DeploymentConfigurationError) as caught:
        deployment_contract.resolve_deployment_state_mode(settings)

    detail = str(caught.value)
    assert "session_db_url" in detail
    assert "landscape_url" in detail
    assert session_url not in detail
    assert landscape_url not in detail
    assert "session_password" not in detail
    assert "landscape_password" not in detail


@pytest.mark.parametrize("url_field", ["session_db_url", "landscape_url"])
def test_sqlite_single_rejects_either_postgresql_url(url_field: str) -> None:
    settings = _settings(deployment_state_mode="sqlite-single", **{url_field: _POSTGRESQL_SESSION_URL})

    with pytest.raises(deployment_contract.DeploymentConfigurationError) as caught:
        deployment_contract.resolve_deployment_state_mode(settings)

    detail = str(caught.value)
    assert "session_db_url" in detail
    assert "landscape_url" in detail
    assert _POSTGRESQL_SESSION_URL not in detail
    assert "session_password" not in detail


@pytest.mark.parametrize("unsupported_base", ["mysql+pymysql", "postgres"])
def test_auto_rejects_two_urls_with_unsupported_dialect(unsupported_base: str) -> None:
    session_url = f"{unsupported_base}://session_user:session_password@db/session"
    landscape_url = f"{unsupported_base}://landscape_user:landscape_password@db/landscape"
    settings = _settings(session_db_url=session_url, landscape_url=landscape_url)

    with pytest.raises(deployment_contract.DeploymentConfigurationError) as caught:
        deployment_contract.resolve_deployment_state_mode(settings)

    detail = str(caught.value)
    assert "session_db_url" in detail
    assert "landscape_url" in detail
    assert session_url not in detail
    assert landscape_url not in detail
    assert "session_password" not in detail
    assert "landscape_password" not in detail


@pytest.mark.parametrize("malformed_driver", ["postgresql+", "postgresql+psycopg+extra"])
def test_auto_rejects_malformed_postgresql_driver_suffixes_and_redacts(malformed_driver: str) -> None:
    session_url = f"{malformed_driver}://session_user:session_password@db/session"
    landscape_url = f"{malformed_driver}://landscape_user:landscape_password@db/landscape"
    settings = _settings(session_db_url=session_url, landscape_url=landscape_url)

    with pytest.raises(deployment_contract.DeploymentConfigurationError) as caught:
        deployment_contract.resolve_deployment_state_mode(settings)

    detail = str(caught.value)
    assert "session_db_url" in detail
    assert "landscape_url" in detail
    assert session_url not in detail
    assert landscape_url not in detail
    assert "session_password" not in detail
    assert "landscape_password" not in detail


def test_url_parse_failure_is_caught_and_redacted() -> None:
    raw_url = "not a url containing secret_password"
    settings = _settings(
        session_db_url=_POSTGRESQL_SESSION_URL,
        landscape_url=_POSTGRESQL_LANDSCAPE_URL,
    ).model_copy(update={"session_db_url": raw_url})

    with pytest.raises(deployment_contract.DeploymentConfigurationError) as caught:
        deployment_contract.resolve_deployment_state_mode(settings)

    detail = str(caught.value)
    assert "session_db_url" in detail
    assert raw_url not in detail
    assert "secret_password" not in detail


@pytest.mark.parametrize(
    "target",
    ["docker-compose", "linux-systemd", "aws-ecs", "azure-container-apps", "kubernetes"],
)
def test_external_postgresql_check_names_are_exact_ordered_and_all_pass(tmp_path: Path, target: str) -> None:
    checks = validate_external_postgresql_settings(_external_settings(tmp_path, target=target))

    assert [check.name for check in checks] == _EXTERNAL_CHECK_NAMES
    assert all(check.ok for check in checks)
    rendered = repr(checks)
    for fragment in (
        "psycopg2",
        "session_user",
        "session_password",
        "db.example",
        "session_database",
        "landscape_user",
        "landscape_password",
        "landscape_database",
        str(tmp_path),
    ):
        assert fragment not in rendered


@pytest.mark.parametrize("driver", ["postgresql", "postgresql+psycopg2", "postgresql+psycopg"])
def test_external_postgresql_accepts_only_supported_sync_driver_forms(tmp_path: Path, driver: str) -> None:
    settings = _external_settings(
        tmp_path,
        session_db_url=f"{driver}://session_user:session_password@db.example/session_database",
        landscape_url=f"{driver}://landscape_user:landscape_password@db.example/landscape_database",
    )

    checks = {check.name: check for check in validate_external_postgresql_settings(settings)}

    assert checks["session_db_url"].ok
    assert checks["landscape_url"].ok
    assert checks["separate_db_targets"].ok


@pytest.mark.parametrize(
    ("url", "secret_fragments"),
    [
        pytest.param(
            "sqlite:///private-driver-path.db",
            ("sqlite", "private-driver-path.db"),
            id="sqlite",
        ),
        pytest.param(
            "postgresql+asyncpg://driver_user:driver_password@driver-host/driver_database",
            ("asyncpg", "driver_user", "driver_password", "driver-host", "driver_database"),
            id="asyncpg",
        ),
        pytest.param(
            "postgresql+pg8000://driver_user:driver_password@driver-host/driver_database",
            ("pg8000", "driver_user", "driver_password", "driver-host", "driver_database"),
            id="pg8000",
        ),
        pytest.param(
            "postgresql+://driver_user:driver_password@driver-host/driver_database",
            ("postgresql+", "driver_user", "driver_password", "driver-host", "driver_database"),
            id="empty-driver",
        ),
        pytest.param(
            "postgresql+psycopg+extra://driver_user:driver_password@driver-host/driver_database",
            ("psycopg+extra", "driver_user", "driver_password", "driver-host", "driver_database"),
            id="multiple-driver-suffixes",
        ),
        pytest.param(
            "not-a-url-with-private-driver-fragment",
            ("private-driver-fragment",),
            id="unparseable-shape",
        ),
        pytest.param(
            "postgresql+psycopg://driver_user:driver_password@/driver_database",
            ("psycopg", "driver_user", "driver_password", "driver_database"),
            id="missing-host",
        ),
    ],
)
def test_external_postgresql_rejects_unsupported_url_forms_and_redacts(
    tmp_path: Path,
    url: str,
    secret_fragments: tuple[str, ...],
) -> None:
    settings = _external_settings(tmp_path).model_copy(update={"session_db_url": url})

    checks = validate_external_postgresql_settings(settings)

    assert {check.name: check for check in checks}["session_db_url"].ok is False
    rendered = repr(checks)
    assert url not in rendered
    for fragment in secret_fragments:
        assert fragment not in rendered


def test_external_postgresql_missing_url_fails_closed_and_redacts_other_url(tmp_path: Path) -> None:
    settings = _external_settings(tmp_path).model_copy(update={"session_db_url": None})

    checks = validate_external_postgresql_settings(settings)

    by_name = {check.name: check for check in checks}
    assert by_name["deployment_state_mode"].ok is False
    assert by_name["session_db_url"].ok is False
    assert by_name["separate_db_targets"].ok is False
    assert settings.landscape_url not in repr(checks)


def test_external_postgresql_rejects_same_logical_database_through_driver_aliases(tmp_path: Path) -> None:
    settings = _external_settings(
        tmp_path,
        session_db_url="postgresql://first_user:first_password@shared-host/shared_database",  # secret-scan: allow-this-line
        landscape_url="postgresql+psycopg://second_user:second_password@shared-host/shared_database",
    )

    check = {check.name: check for check in validate_external_postgresql_settings(settings)}["separate_db_targets"]

    assert check.ok is False
    for fragment in ("first_user", "first_password", "second_user", "second_password", "shared-host", "shared_database"):
        assert fragment not in check.detail


@pytest.mark.parametrize("field", ["data_dir", "payload_store_path"])
def test_external_postgresql_requires_explicit_paths_and_redacts_values(tmp_path: Path, field: str) -> None:
    values = _external_settings(tmp_path).model_dump()
    values.pop(field)
    settings = _settings(**values)

    checks = validate_external_postgresql_settings(settings)

    assert {check.name: check for check in checks}[field].ok is False
    assert str(tmp_path) not in repr(checks)


def test_external_postgresql_rejects_unsupported_target_mode_pair(tmp_path: Path) -> None:
    settings = _external_settings(
        tmp_path,
        target="aws-ecs",
        deployment_state_mode="sqlite-single",
        session_db_url="sqlite:///private-session-path.db",
        landscape_url="sqlite:///private-landscape-path.db",
    )

    checks = validate_external_postgresql_settings(settings)

    assert {check.name: check for check in checks}["deployment_state_mode"].ok is False
    assert "private-session-path.db" not in repr(checks)
    assert "private-landscape-path.db" not in repr(checks)


def test_external_postgresql_rejects_unsafe_secrets(tmp_path: Path) -> None:
    settings = _external_settings(tmp_path).model_copy(
        update={"secret_key": "unsafe-secret-sentinel", "shareable_link_signing_key": SecretBytes(b"x" * 32)}
    )

    checks = {check.name: check for check in validate_external_postgresql_settings(settings)}

    assert checks["secret_key"].ok is False
    assert checks["shareable_link_signing_key"].ok is False
    assert "unsafe-secret-sentinel" not in repr(checks)


def test_default_deployment_target_fails() -> None:
    assert _checks()["deployment_target"].ok is False


def test_aws_ecs_deployment_target_passes() -> None:
    assert _checks(deployment_target="aws-ecs")["deployment_target"].ok is True


def test_missing_session_db_url_fails() -> None:
    assert _checks()["session_db_url"].ok is False


def test_missing_landscape_url_fails() -> None:
    assert _checks()["landscape_url"].ok is False


def test_sqlite_session_db_url_rejected() -> None:
    assert _checks(session_db_url="sqlite:///x.db")["session_db_url"].ok is False


def test_sqlite_landscape_url_rejected() -> None:
    assert _checks(landscape_url="sqlite:///x.db")["landscape_url"].ok is False


def test_postgresql_psycopg_driver_accepted() -> None:
    assert _checks(session_db_url="postgresql+psycopg://u:p@host/db")["session_db_url"].ok is True


@pytest.mark.parametrize(
    ("url", "operator_fragment"),
    [
        ("postgresql+://host/db", "postgresql+"),
        ("postgresql+psycopg+extra://host/db", "psycopg+extra"),
    ],
)
def test_malformed_postgresql_driver_rejected_and_redacted(url: str, operator_fragment: str) -> None:
    check = _checks(session_db_url=url)["session_db_url"]

    assert check.ok is False
    assert operator_fragment not in check.detail


def test_unknown_driver_and_credentials_are_redacted() -> None:
    check = _checks(session_db_url="x_secret_123://user:hunter2@host/db")["session_db_url"]

    assert check.ok is False
    assert "x_secret_123" not in check.detail
    assert "hunter2" not in check.detail


def test_missing_payload_store_path_fails() -> None:
    assert _checks()["payload_store_path"].ok is False


def test_missing_data_dir_fails() -> None:
    assert _checks()["data_dir"].ok is False


def test_non_container_host_fails() -> None:
    assert _checks()["host"].ok is False


def test_container_host_passes() -> None:
    assert _checks(host="0.0.0.0")["host"].ok is True


def test_placeholder_secret_key_fails() -> None:
    assert _checks(secret_key="change-me-in-production", host="127.0.0.1")["secret_key"].ok is False


def test_undersized_secret_key_fails() -> None:
    assert _checks(secret_key="short", host="127.0.0.1")["secret_key"].ok is False


def test_uniform_byte_signing_key_fails() -> None:
    check = _checks(shareable_link_signing_key=b"\x00" * 32, host="127.0.0.1")

    assert check["shareable_link_signing_key"].ok is False


def test_check_names_are_exact_ordered_and_unique() -> None:
    names = [check.name for check in validate_aws_ecs_settings(WebSettings(**_base_kwargs()))]

    assert names == [
        "deployment_target",
        "deployment_state_mode",
        "session_db_url",
        "landscape_url",
        "separate_db_targets",
        "data_dir",
        "payload_store_path",
        "host",
        "secret_key",
        "shareable_link_signing_key",
        "operator_telemetry",
        "operator_telemetry_environment",
        "operator_telemetry_release",
        "operator_telemetry_ecs_cluster",
        "operator_telemetry_ecs_service",
        "operator_telemetry_task_definition_family",
        "operator_telemetry_task_definition_revision",
    ]
    assert len(names) == len(set(names))


def test_all_checks_pass_for_fully_valid_ecs_settings(tmp_path: Path) -> None:
    settings = WebSettings(
        **(
            _base_kwargs()
            | {
                "deployment_target": "aws-ecs",
                "operator_telemetry": "aws-otlp",
                "operator_telemetry_environment": "test",
                "operator_telemetry_release": "git-deadbeef",
                "operator_telemetry_ecs_cluster": "elspeth-test",
                "operator_telemetry_ecs_service": "elspeth-web",
                "operator_telemetry_task_definition_family": "elspeth-web-task",
                "operator_telemetry_task_definition_revision": "42",
                "host": "0.0.0.0",
                "session_db_url": "postgresql://u:p@host/session",
                "landscape_url": "postgresql://u:p@host/landscape",
                "data_dir": tmp_path / "data",
                "payload_store_path": tmp_path / "payloads",
            }
        )
    )

    checks = validate_aws_ecs_settings(settings)

    assert all(check.ok for check in checks)
    assert all(check.detail for check in checks)


@pytest.mark.parametrize(
    ("field", "env_var"),
    [
        ("operator_telemetry_release", "ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE"),
        ("operator_telemetry_ecs_cluster", "ELSPETH_WEB__OPERATOR_TELEMETRY_ECS_CLUSTER"),
        ("operator_telemetry_ecs_service", "ELSPETH_WEB__OPERATOR_TELEMETRY_ECS_SERVICE"),
        ("operator_telemetry_task_definition_family", "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY"),
        ("operator_telemetry_task_definition_revision", "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION"),
    ],
)
def test_aws_ecs_requires_bounded_operator_deployment_identity(field: str, env_var: str) -> None:
    check = _checks()[field]

    assert check.ok is False
    assert env_var in check.detail
