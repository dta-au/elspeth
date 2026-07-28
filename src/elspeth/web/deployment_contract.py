"""Pure deployment contract resolution and AWS ECS validation.

No I/O, no network, no filesystem. Diagnostics and ``ContractCheck.detail``
are pre-redacted -- never a URL, path, or secret value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.exc import ArgumentError

from elspeth.web import aws_rds_trust
from elspeth.web.config import (
    DeploymentTarget,
    WebSettings,
    is_default_secret_key_placeholder,
    is_undersized_secret_key,
    is_uniform_byte_key,
)
from elspeth.web.schema_probe import DatabaseTargetConflictError, require_distinct_postgres_targets

ResolvedDeploymentStateMode = Literal["sqlite-single", "external-postgresql"]
DEPLOYMENT_TARGET_AWS_ECS = "aws-ecs"
EXTERNAL_POSTGRESQL_TARGETS: frozenset[DeploymentTarget] = frozenset({"aws-ecs", "azure-container-apps", "kubernetes"})
_SUPPORTED_EXTERNAL_POSTGRES_DRIVERS = frozenset({"postgresql", "postgresql+psycopg2", "postgresql+psycopg"})
_EXTERNAL_STATE_TARGETS: frozenset[DeploymentTarget] = frozenset(
    {"default", "docker-compose", "linux-systemd", "aws-ecs", "azure-container-apps", "kubernetes"}
)
_CONTAINER_TARGETS: frozenset[DeploymentTarget] = frozenset({"docker-compose", "aws-ecs", "azure-container-apps", "kubernetes"})
# This IS the container-serving check, not a bind-all bug; exact-match only --
# IPv6 dual-stack ("::") is out of scope for this milestone.
_CONTAINER_SERVING_HOST = "0.0.0.0"


@dataclass(frozen=True)
class ContractCheck:
    name: str
    ok: bool
    detail: str


class DeploymentConfigurationError(ValueError):
    """Raised when deployment state settings cannot form a valid contract."""


def _database_dialect(field_name: Literal["session_db_url", "landscape_url"], url: str) -> Literal["sqlite", "postgresql"]:
    try:
        drivername = make_url(url).drivername
    except (ArgumentError, ValueError):
        raise DeploymentConfigurationError(f"{field_name} could not be parsed as a SQLAlchemy URL") from None

    driver_parts = drivername.split("+")
    valid_shape = len(driver_parts) == 1 or (len(driver_parts) == 2 and bool(driver_parts[1]))
    if not valid_shape:
        raise DeploymentConfigurationError(
            "session_db_url and landscape_url SQLAlchemy driver names must use a base dialect alone or exactly one non-empty +driver suffix"
        )

    base_dialect = driver_parts[0]
    if base_dialect == "sqlite":
        return "sqlite"
    if base_dialect == "postgresql":
        return "postgresql"
    raise DeploymentConfigurationError(
        "session_db_url and landscape_url must use the exact base SQLAlchemy dialect 'sqlite' or 'postgresql'"
    )


def resolve_deployment_state_mode(settings: WebSettings) -> ResolvedDeploymentStateMode:
    """Resolve and validate the deployment's database state mode."""
    requested_mode = settings.deployment_state_mode

    if settings.deployment_target in EXTERNAL_POSTGRESQL_TARGETS and requested_mode == "sqlite-single":
        raise DeploymentConfigurationError(
            "deployment_target requires deployment_state_mode='external-postgresql'; sqlite-single is not supported"
        )

    if settings.deployment_target in EXTERNAL_POSTGRESQL_TARGETS and requested_mode == "auto":
        explicit_urls = {"session_db_url", "landscape_url"}.issubset(settings.model_fields_set)
        if not explicit_urls or settings.session_db_url is None or settings.landscape_url is None:
            raise DeploymentConfigurationError(
                "deployment_state_mode='auto' for this deployment_target requires explicit non-null session_db_url and landscape_url"
            )
        session_db_url = settings.session_db_url
        landscape_url = settings.landscape_url
    else:
        session_db_url = settings.get_session_db_url()
        landscape_url = settings.get_landscape_url()

    session_dialect = _database_dialect("session_db_url", session_db_url)
    landscape_dialect = _database_dialect("landscape_url", landscape_url)

    if requested_mode == "auto":
        if settings.deployment_target in EXTERNAL_POSTGRESQL_TARGETS:
            if session_dialect == landscape_dialect == "postgresql":
                return "external-postgresql"
            raise DeploymentConfigurationError(
                "deployment_target with deployment_state_mode='auto' requires session_db_url and landscape_url to use PostgreSQL"
            )
        if session_dialect == landscape_dialect == "sqlite":
            return "sqlite-single"
        if session_dialect == landscape_dialect == "postgresql":
            return "external-postgresql"
        raise DeploymentConfigurationError(
            "deployment_state_mode='auto' requires session_db_url and landscape_url to use the same supported base SQLAlchemy dialect"
        )

    expected_dialect = "sqlite" if requested_mode == "sqlite-single" else "postgresql"
    if session_dialect == landscape_dialect == expected_dialect:
        return requested_mode
    raise DeploymentConfigurationError("deployment_state_mode does not match session_db_url and landscape_url base SQLAlchemy dialects")


def _has_approved_aws_ecs_tls_query(parsed: URL) -> bool:
    sslmode = parsed.query.get("sslmode")
    sslrootcert = parsed.query.get("sslrootcert")
    return (
        type(sslmode) is str
        and sslmode == "verify-full"
        and type(sslrootcert) is str
        and sslrootcert == str(aws_rds_trust.AWS_RDS_GLOBAL_BUNDLE_PATH)
    )


def _check_external_postgres_url(
    name: str,
    env_var: str,
    url: str | None,
    *,
    require_aws_authenticated_tls: bool,
) -> ContractCheck:
    if url is None:
        return ContractCheck(name, False, f"{env_var} must be explicitly set for external PostgreSQL state")
    try:
        parsed = make_url(url)
    except (ArgumentError, TypeError, ValueError):
        return ContractCheck(name, False, f"{env_var} must be a supported synchronous PostgreSQL SQLAlchemy URL")
    if parsed.drivername not in _SUPPORTED_EXTERNAL_POSTGRES_DRIVERS or not parsed.host or not parsed.database:
        return ContractCheck(
            name,
            False,
            f"{env_var} must be a supported synchronous PostgreSQL SQLAlchemy URL",
        )
    if require_aws_authenticated_tls and not _has_approved_aws_ecs_tls_query(parsed):
        return ContractCheck(
            name,
            False,
            f"{env_var} must require authenticated PostgreSQL TLS with sslmode=verify-full and the immutable ELSPETH AWS RDS trust root",
        )
    return ContractCheck(name, True, f"{env_var} is configured for supported synchronous PostgreSQL access")


def _check_required_path(name: str, env_var: str, value: Path | None, *, explicitly_set: bool) -> ContractCheck:
    # `explicitly_set` is `field_name in settings.model_fields_set` at the
    # call site. `payload_store_path` truly defaults to None, so a bare
    # None-check would already catch its omission -- but `data_dir` defaults
    # to `Path("data")` (never None, never blank), so without this flag an
    # omitted ELSPETH_WEB__DATA_DIR would silently report ok=True. Checking
    # explicit-construction presence closes that gap for both fields
    # uniformly.
    if not explicitly_set or value is None or not str(value).strip():
        return ContractCheck(
            name,
            False,
            f"{env_var} must be explicitly set and non-blank for external PostgreSQL state",
        )
    return ContractCheck(name, True, f"{env_var} is set")


def _check_external_host(settings: WebSettings) -> ContractCheck:
    host_ok = settings.deployment_target not in _CONTAINER_TARGETS or settings.host == _CONTAINER_SERVING_HOST
    return ContractCheck(
        "host",
        host_ok,
        (
            "host is suitable for the selected external-state deployment"
            if host_ok
            else "ELSPETH_WEB__HOST must use the container-serving bind address for this deployment target"
        ),
    )


def _check_secret_key(settings: WebSettings) -> ContractCheck:
    secret_ok = not (is_default_secret_key_placeholder(settings.secret_key) or is_undersized_secret_key(settings.secret_key))
    return ContractCheck(
        "secret_key",
        secret_ok,
        (
            "secret_key is production-shaped"
            if secret_ok
            else "ELSPETH_WEB__SECRET_KEY must not be the default placeholder and must meet the length floor"
        ),
    )


def _check_shareable_link_signing_key(settings: WebSettings) -> ContractCheck:
    key_ok = not is_uniform_byte_key(settings.shareable_link_signing_key.get_secret_value())
    return ContractCheck(
        "shareable_link_signing_key",
        key_ok,
        (
            "shareable_link_signing_key is production-shaped"
            if key_ok
            else "ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY is a known-weak uniform-byte placeholder"
        ),
    )


def _check_required_operator_identity(name: str, env_var: str, value: str | None) -> ContractCheck:
    if value is None:
        return ContractCheck(name, False, f"{env_var} is required in aws-ecs deployment mode")
    return ContractCheck(name, True, f"{env_var} is set")


def _check_external_deployment_state_mode(
    settings: WebSettings,
    *,
    target_ok: bool,
    resolved_state_mode: ResolvedDeploymentStateMode | None,
) -> ContractCheck:
    if resolved_state_mode is None:
        try:
            resolved_state_mode = resolve_deployment_state_mode(settings)
        except DeploymentConfigurationError:
            return ContractCheck(
                "deployment_state_mode",
                False,
                "ELSPETH_WEB__DEPLOYMENT_STATE_MODE must resolve to external PostgreSQL for this startup contract",
            )

    mode_ok = target_ok and resolved_state_mode == "external-postgresql"
    return ContractCheck(
        "deployment_state_mode",
        mode_ok,
        (
            "deployment state resolves to external PostgreSQL"
            if mode_ok
            else "ELSPETH_WEB__DEPLOYMENT_STATE_MODE must resolve to external PostgreSQL for this startup contract"
        ),
    )


def _check_distinct_postgres_targets(
    settings: WebSettings,
    *,
    session_check: ContractCheck,
    landscape_check: ContractCheck,
) -> ContractCheck:
    failure = ContractCheck(
        "separate_db_targets",
        False,
        "session and Landscape database targets must be statically provable as distinct",
    )
    if not session_check.ok or not landscape_check.ok:
        return failure

    assert settings.session_db_url is not None
    assert settings.landscape_url is not None
    try:
        require_distinct_postgres_targets(settings.session_db_url, settings.landscape_url)
    except DatabaseTargetConflictError:
        return failure
    return ContractCheck(
        "separate_db_targets",
        True,
        "session and Landscape database targets are statically distinct",
    )


def validate_aws_ecs_settings(
    settings: WebSettings,
    *,
    resolved_state_mode: ResolvedDeploymentStateMode | None = None,
) -> list[ContractCheck]:
    """Run every strict AWS ECS contract check, including deployment target."""
    target_ok = settings.deployment_target == DEPLOYMENT_TARGET_AWS_ECS
    target_check = ContractCheck(
        "deployment_target",
        target_ok,
        "deployment_target is aws-ecs" if target_ok else "ELSPETH_WEB__DEPLOYMENT_TARGET must be aws-ecs",
    )
    aws_host_ok = settings.host == _CONTAINER_SERVING_HOST
    host_check = ContractCheck(
        "host",
        aws_host_ok,
        (
            "host is suitable for AWS ECS container serving"
            if aws_host_ok
            else "ELSPETH_WEB__HOST must use the container-serving bind address for AWS ECS reachability"
        ),
    )
    external_checks = (
        validate_external_postgresql_settings(settings)
        if resolved_state_mode is None
        else validate_external_postgresql_settings(settings, resolved_state_mode=resolved_state_mode)
    )
    checks = [
        target_check if check.name == "deployment_target" else host_check if check.name == "host" else check for check in external_checks
    ]
    checks.extend(
        [
            ContractCheck(
                "operator_telemetry",
                settings.operator_telemetry == "aws-otlp",
                (
                    "operator telemetry uses the task-local OTLP collector"
                    if settings.operator_telemetry == "aws-otlp"
                    else "ELSPETH_WEB__OPERATOR_TELEMETRY must be aws-otlp in aws-ecs deployment mode"
                ),
            ),
            ContractCheck(
                "operator_telemetry_environment",
                settings.operator_telemetry_environment is not None,
                (
                    "operator telemetry environment is set"
                    if settings.operator_telemetry_environment is not None
                    else "ELSPETH_WEB__OPERATOR_TELEMETRY_ENVIRONMENT is required in aws-ecs deployment mode"
                ),
            ),
            _check_required_operator_identity(
                "operator_telemetry_release",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE",
                settings.operator_telemetry_release,
            ),
            _check_required_operator_identity(
                "operator_telemetry_ecs_cluster",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_ECS_CLUSTER",
                settings.operator_telemetry_ecs_cluster,
            ),
            _check_required_operator_identity(
                "operator_telemetry_ecs_service",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_ECS_SERVICE",
                settings.operator_telemetry_ecs_service,
            ),
            _check_required_operator_identity(
                "operator_telemetry_task_definition_family",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY",
                settings.operator_telemetry_task_definition_family,
            ),
            _check_required_operator_identity(
                "operator_telemetry_task_definition_revision",
                "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION",
                settings.operator_telemetry_task_definition_revision,
            ),
        ]
    )
    return checks


def validate_external_postgresql_settings(
    settings: WebSettings,
    *,
    resolved_state_mode: ResolvedDeploymentStateMode | None = None,
) -> list[ContractCheck]:
    """Run redacted checks for the provider-neutral external PostgreSQL contract."""
    target_ok = settings.deployment_target in _EXTERNAL_STATE_TARGETS
    mode_check = _check_external_deployment_state_mode(
        settings,
        target_ok=target_ok,
        resolved_state_mode=resolved_state_mode,
    )

    session_check = _check_external_postgres_url(
        "session_db_url",
        "ELSPETH_WEB__SESSION_DB_URL",
        settings.session_db_url,
        require_aws_authenticated_tls=settings.deployment_target == DEPLOYMENT_TARGET_AWS_ECS,
    )
    landscape_check = _check_external_postgres_url(
        "landscape_url",
        "ELSPETH_WEB__LANDSCAPE_URL",
        settings.landscape_url,
        require_aws_authenticated_tls=settings.deployment_target == DEPLOYMENT_TARGET_AWS_ECS,
    )
    distinct_targets_check = _check_distinct_postgres_targets(
        settings,
        session_check=session_check,
        landscape_check=landscape_check,
    )

    return [
        ContractCheck(
            "deployment_target",
            target_ok,
            (
                "deployment_target supports external PostgreSQL state"
                if target_ok
                else "ELSPETH_WEB__DEPLOYMENT_TARGET does not support external PostgreSQL state"
            ),
        ),
        mode_check,
        session_check,
        landscape_check,
        distinct_targets_check,
        _check_required_path(
            "data_dir",
            "ELSPETH_WEB__DATA_DIR",
            settings.data_dir,
            explicitly_set="data_dir" in settings.model_fields_set,
        ),
        _check_required_path(
            "payload_store_path",
            "ELSPETH_WEB__PAYLOAD_STORE_PATH",
            settings.payload_store_path,
            explicitly_set="payload_store_path" in settings.model_fields_set,
        ),
        _check_external_host(settings),
        _check_secret_key(settings),
        _check_shareable_link_signing_key(settings),
    ]
