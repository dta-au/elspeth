"""Deployment startup profiles — one closed registry keyed by deployment target.

``web/app.py`` used to branch four times on
``settings.deployment_target == DEPLOYMENT_TARGET_AWS_ECS`` to pick the startup
contract, the mounted-directory check, the schema validator and the
engine-construction error. Those arms were the only place the target
vocabulary (``web/config.py`` ``DeploymentTarget``) was consulted at startup,
so every new target had to be threaded through each of them by hand. This
module replaces the arms with one :class:`DeploymentStartupProfile` per
target: the profile names the startup hooks the target boots through and the
platform environment variables that carry the process's replica identity.

**Closedness.** :data:`DEPLOYMENT_STARTUP_PROFILES` has exactly one entry per
``DeploymentTarget`` literal; ``tests/unit/web/test_deployment_profiles.py``
pins the two vocabularies equal, so a target added to ``config.py`` without
a profile fails that gate rather than booting through a silent default.

**Patch seam.** The hooks dereference ``web/aws_ecs_startup.py`` and
``web/external_state_startup.py`` at call time (module attribute access, not
a bound reference captured at import), so a test that replaces
``aws_ecs_startup.enforce_aws_ecs_contract`` observes the replacement through
the profile exactly as ``app.py`` observed it through its own namespace before.

**Identity.** ``instance_id`` is minted once per process
(:func:`resolve_instance_id`) unless ``WebSettings.instance_id`` pins it. The
same value is passed to the session service's ``owner_instance_id`` (so the
fence rows a replica writes carry the id the wire shows), stamped on every
response as ``X-Elspeth-Instance`` (``web/middleware/instance_identity.py``)
and reported by ``/api/system/status``. Azure Container Apps additionally
sets ``CONTAINER_APP_REPLICA_NAME`` and ``CONTAINER_APP_REVISION`` on every
replica; the ACA profile names them and :func:`read_platform_identity`
parses them as a Tier-3 boundary (bounded, allow-listed, fail-closed on a
malformed value). AWS ECS does not publish identity through the environment
— the acceptance harness reads the task metadata endpoint
(``_aws_ecs_acceptance/ecs_metadata.py``) — so its profile names no variable.

Layer: L2 (deployment policy). Pure apart from the environment read in
:func:`read_platform_identity`, which takes the mapping explicitly.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from sqlalchemy import Engine

from elspeth.web import aws_ecs_startup, external_state_startup
from elspeth.web.config import DeploymentTarget, WebSettings
from elspeth.web.deployment_contract import ResolvedDeploymentStateMode
from elspeth.web.external_state_startup import ExternalStateSchemaNotReadyError

INSTANCE_ID_MAX_LENGTH: Final = 128
"""Upper bound on an instance id (configured or minted).

The value is echoed on every response header, so it is held to the same
conservative shape as ``X-Request-ID``: no whitespace, no control characters,
nothing a log pipeline or header parser could misread. Minted ids are
``web-<uuid4>`` (40 characters); the bound leaves room for an operator-pinned
``<revision>--<replica>`` style id without admitting kilobyte payloads.
"""

_INSTANCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
_PLATFORM_IDENTITY_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")

StartupContractFamily = Literal["aws-ecs", "external-state"]
"""Which startup module a profile boots through.

``aws-ecs`` wraps the provider-neutral checks with the RDS trust-root
verification and ECS-worded diagnostics; every other target boots through
the provider-neutral ``external_state_startup`` module directly.
"""


class PlatformIdentityError(RuntimeError):
    """Raised when a platform-set identity variable is present but malformed.

    The variable is set by the platform, never by an operator, so a value that
    fails the bounded allow-list means the process is not running where its
    ``deployment_target`` says it is (or the platform contract changed). Both
    are boot-refusing facts: an unparseable replica name would otherwise be
    reported as ``null`` and the multi-replica probes would score a trial
    against a replica they cannot name.
    """


@dataclass(frozen=True)
class PlatformIdentity:
    """Replica identity the platform stamped on this process, when it did.

    ``revision`` and ``replica`` are ``None`` when the profile names no
    variable for them (every target except Azure Container Apps today) or the
    named variable is absent from the environment (an ACA-targeted process
    booted outside Container Apps, such as the unit suite).
    """

    revision: str | None
    replica: str | None


@dataclass(frozen=True)
class DeploymentStartupProfile:
    """Startup hooks and identity sources for one deployment target."""

    target: DeploymentTarget
    contract_family: StartupContractFamily
    display_name: str
    """Prefix the startup diagnostics use (``"AWS ECS"``, ``"External-state"``)."""
    doctor_command: str
    """The ``elspeth doctor`` invocation the diagnostics point the operator at."""
    replica_identity_env_var: str | None
    """Platform variable naming this replica, or ``None`` when the platform sets none."""
    revision_env_var: str | None
    """Platform variable naming the deployed revision, or ``None``."""

    def enforce_contract(self, settings: WebSettings, *, resolved_state_mode: ResolvedDeploymentStateMode) -> None:
        """Reject incomplete deployment policy before any provider is installed."""
        if self.contract_family == "aws-ecs":
            aws_ecs_startup.enforce_aws_ecs_contract(settings, resolved_state_mode=resolved_state_mode)
            return
        external_state_startup.enforce_external_state_contract(settings, resolved_state_mode=resolved_state_mode)

    def require_runtime_directories_mounted(self, settings: WebSettings) -> None:
        """Require the external-state runtime directories to be mounted and safe."""
        if self.contract_family == "aws-ecs":
            aws_ecs_startup.require_runtime_directories_mounted(settings)
            return
        external_state_startup.require_runtime_directories_mounted(settings)

    def validate_only_schema_or_raise(self, settings: WebSettings, session_engine: Engine) -> None:
        """Validate both external schemas without repair."""
        if self.contract_family == "aws-ecs":
            aws_ecs_startup.validate_only_schema_or_raise(settings, session_engine)
            return
        external_state_startup.validate_only_schema_or_raise(settings, session_engine)

    def session_engine_not_ready_error(self) -> ExternalStateSchemaNotReadyError:
        """The error raised when the session engine itself cannot be constructed."""
        detail = f"{self.display_name} session_schema engine could not be constructed. Run '{self.doctor_command}' for full diagnostics."
        if self.contract_family == "aws-ecs":
            return aws_ecs_startup.AwsEcsSchemaNotReadyError(detail)
        return ExternalStateSchemaNotReadyError(detail)


def _external_state_profile(target: DeploymentTarget) -> DeploymentStartupProfile:
    return DeploymentStartupProfile(
        target=target,
        contract_family="external-state",
        display_name="External-state",
        doctor_command="elspeth doctor deployment",
        replica_identity_env_var=None,
        revision_env_var=None,
    )


DEPLOYMENT_STARTUP_PROFILES: Final[Mapping[DeploymentTarget, DeploymentStartupProfile]] = MappingProxyType(
    {
        "default": _external_state_profile("default"),
        "docker-compose": _external_state_profile("docker-compose"),
        "linux-systemd": _external_state_profile("linux-systemd"),
        "aws-ecs": DeploymentStartupProfile(
            target="aws-ecs",
            contract_family="aws-ecs",
            display_name="AWS ECS",
            doctor_command="elspeth doctor aws-ecs",
            # ECS publishes task identity through the metadata endpoint, not
            # the environment; the acceptance harness reads it there.
            replica_identity_env_var=None,
            revision_env_var=None,
        ),
        "azure-container-apps": DeploymentStartupProfile(
            target="azure-container-apps",
            contract_family="external-state",
            display_name="External-state",
            doctor_command="elspeth doctor deployment",
            # Set by Container Apps on every replica of every revision.
            replica_identity_env_var="CONTAINER_APP_REPLICA_NAME",
            revision_env_var="CONTAINER_APP_REVISION",
        ),
        "kubernetes": _external_state_profile("kubernetes"),
    }
)
"""One profile per ``DeploymentTarget`` literal; closed, immutable, test-pinned."""


def deployment_startup_profile(target: DeploymentTarget) -> DeploymentStartupProfile:
    """The profile for ``target``.

    ``DeploymentTarget`` is a closed literal validated by ``WebSettings``, so
    a missing key here is a registry defect, not a configuration error; the
    ``KeyError`` is deliberately not translated.
    """
    return DEPLOYMENT_STARTUP_PROFILES[target]


def is_valid_instance_id(value: str) -> bool:
    """Is ``value`` safe to carry as the process identity on every response?"""
    return len(value) <= INSTANCE_ID_MAX_LENGTH and _INSTANCE_ID.fullmatch(value) is not None


def resolve_instance_id(settings: WebSettings) -> str:
    """The identity this process presents: the configured id, else a fresh mint."""
    if settings.instance_id is not None:
        return settings.instance_id
    return f"web-{uuid.uuid4()}"


def _read_platform_value(name: str | None, environ: Mapping[str, str]) -> str | None:
    if name is None:
        return None
    # Tier-3 boundary: the platform wrote this, ELSPETH did not. Absent means
    # "not running on that platform"; present-but-malformed refuses the boot.
    if name not in environ:
        return None
    value = environ[name]
    if _PLATFORM_IDENTITY_VALUE.fullmatch(value) is None:
        raise PlatformIdentityError(
            f"{name} is set but is not a bounded platform identity "
            f"(1-{INSTANCE_ID_MAX_LENGTH} characters of [A-Za-z0-9._-], leading alphanumeric); refusing to boot."
        )
    return value


def read_platform_identity(profile: DeploymentStartupProfile, environ: Mapping[str, str] | None = None) -> PlatformIdentity:
    """Parse the replica identity the profile's platform stamps on the process."""
    source = os.environ if environ is None else environ
    return PlatformIdentity(
        revision=_read_platform_value(profile.revision_env_var, source),
        replica=_read_platform_value(profile.replica_identity_env_var, source),
    )
