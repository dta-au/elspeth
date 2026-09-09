"""AWS ECS specialization of the external PostgreSQL startup contract."""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import Connection, Engine, create_engine

from elspeth.web import aws_rds_trust, external_state_startup
from elspeth.web.config import WebSettings
from elspeth.web.deployment_contract import ResolvedDeploymentStateMode, validate_aws_ecs_settings
from elspeth.web.schema_probe import SchemaState

_CONNECT_TIMEOUT_SECONDS = external_state_startup._CONNECT_TIMEOUT_SECONDS
_DOCTOR_GUIDANCE = "Run 'elspeth doctor aws-ecs' for full diagnostics."


class AwsEcsStartupContractError(external_state_startup.ExternalStateStartupContractError):
    """Raised when the static AWS ECS startup contract is not satisfied."""


class AwsEcsSchemaNotReadyError(external_state_startup.ExternalStateSchemaNotReadyError):
    """Raised when a required AWS ECS database cannot prove its current schema."""


def _contract_error(detail: str) -> AwsEcsStartupContractError:
    return AwsEcsStartupContractError(f"{detail} {_DOCTOR_GUIDANCE}")


def _schema_error(label: str) -> AwsEcsSchemaNotReadyError:
    return AwsEcsSchemaNotReadyError(f"AWS ECS {label} is not ready and startup repair is disabled. {_DOCTOR_GUIDANCE}")


def _aws_detail(exc: RuntimeError) -> str:
    detail = str(exc).removesuffix(f" {external_state_startup._DOCTOR_GUIDANCE}")
    return detail.replace("External-state", "AWS ECS", 1)


def enforce_aws_ecs_contract(
    settings: WebSettings,
    *,
    resolved_state_mode: ResolvedDeploymentStateMode | None = None,
) -> None:
    """Enforce the shared external-state contract plus AWS identity settings."""
    try:
        aws_rds_trust.verify_aws_rds_trust_bundle()
    except aws_rds_trust.AwsRdsTrustBundleError as exc:
        raise _contract_error(f"AWS ECS immutable RDS trust root failed verification ({exc.code}).") from None
    checks = (
        validate_aws_ecs_settings(settings)
        if resolved_state_mode is None
        else validate_aws_ecs_settings(settings, resolved_state_mode=resolved_state_mode)
    )
    failed_names = [check.name for check in checks if not check.ok]
    if not failed_names:
        return
    if "separate_db_targets" in failed_names:
        raise _contract_error(
            "AWS ECS session and Landscape database targets must be statically provable as distinct database targets. "
            f"Failed checks: {', '.join(failed_names)}."
        )
    raise _contract_error(
        "AWS ECS deployment settings failed checks: "
        f"{', '.join(failed_names)}. Set the named ELSPETH_WEB environment variables to valid production values."
    )


def require_runtime_directories_mounted(settings: WebSettings) -> None:
    """Require AWS ECS runtime directories through the shared implementation."""
    try:
        external_state_startup.require_runtime_directories_mounted(settings)
    except external_state_startup.ExternalStateStartupContractError as exc:
        raise _contract_error(_aws_detail(exc)) from None


def _probe_with_connection_budget(
    engine: Engine,
    probe: Callable[[Engine | Connection], SchemaState],
    *,
    label: str,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> SchemaState:
    """Compatibility wrapper around the provider-neutral retry probe."""
    try:
        return external_state_startup._probe_with_connection_budget(
            engine,
            probe,
            label=label,
            sleep=sleep,
            monotonic=monotonic,
        )
    except external_state_startup.ExternalStateSchemaNotReadyError:
        raise _schema_error(label) from None


def validate_only_schema_or_raise(settings: WebSettings, session_engine: Engine) -> None:
    """Validate AWS ECS database schemas through the shared implementation."""
    try:
        external_state_startup.validate_only_schema_or_raise(
            settings,
            session_engine,
            _probe=_probe_with_connection_budget,
            _engine_factory=create_engine,
        )
    except AwsEcsSchemaNotReadyError:
        raise
    except external_state_startup.ExternalStateSchemaNotReadyError as exc:
        detail = _aws_detail(exc)
        label = "landscape_schema" if "landscape_schema" in detail else "session_schema"
        raise _schema_error(label) from None
