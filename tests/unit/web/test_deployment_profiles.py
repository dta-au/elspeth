"""Tests for the deployment startup profile registry (6b-3, elspeth-31878c9787).

The registry replaces the four ``deployment_target == "aws-ecs"`` arms that
``web/app.py`` used to branch on. These tests pin three things the arms used
to guarantee implicitly: the vocabulary is closed (one profile per target),
the ECS target still boots through the ECS-worded startup module while every
other target boots through the provider-neutral one, and a test that patches
a startup module's function observes the patch through the profile — the
seam ``test_app.py`` relies on.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import SecretBytes
from sqlalchemy import create_engine

from elspeth.web import aws_ecs_startup, external_state_startup
from elspeth.web.aws_ecs_startup import AwsEcsSchemaNotReadyError
from elspeth.web.config import DeploymentTarget, WebSettings
from elspeth.web.deployment_profiles import (
    DEPLOYMENT_STARTUP_PROFILES,
    INSTANCE_ID_MAX_LENGTH,
    DeploymentStartupProfile,
    PlatformIdentity,
    PlatformIdentityError,
    deployment_startup_profile,
    is_valid_instance_id,
    read_platform_identity,
    resolve_instance_id,
)
from elspeth.web.external_state_startup import ExternalStateSchemaNotReadyError

_TARGETS: tuple[DeploymentTarget, ...] = get_args(DeploymentTarget)


def _settings(tmp_path: Path, **overrides: object) -> WebSettings:
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "shareable_link_signing_key": SecretBytes(b"\x00" * 32),
    }
    values.update(overrides)
    return WebSettings(**values)  # type: ignore[arg-type]


class TestRegistryIsClosed:
    def test_one_profile_per_deployment_target_literal(self) -> None:
        """A target added to config.py without a profile fails here, not at boot."""
        assert set(DEPLOYMENT_STARTUP_PROFILES) == set(_TARGETS)

    @pytest.mark.parametrize("target", _TARGETS)
    def test_profile_names_its_own_target(self, target: DeploymentTarget) -> None:
        profile = deployment_startup_profile(target)
        assert profile.target == target
        assert profile is DEPLOYMENT_STARTUP_PROFILES[target]

    def test_registry_is_immutable(self) -> None:
        with pytest.raises(TypeError):
            DEPLOYMENT_STARTUP_PROFILES["default"] = DEPLOYMENT_STARTUP_PROFILES["aws-ecs"]  # type: ignore[index]

    def test_unknown_target_is_a_registry_defect_not_a_config_error(self) -> None:
        with pytest.raises(KeyError):
            deployment_startup_profile("not-a-target")  # type: ignore[arg-type]

    def test_profiles_are_frozen(self) -> None:
        profile = deployment_startup_profile("aws-ecs")
        with pytest.raises(AttributeError):
            profile.contract_family = "external-state"  # type: ignore[misc]


class TestContractFamilies:
    def test_only_aws_ecs_boots_through_the_ecs_startup_module(self) -> None:
        families = {target: profile.contract_family for target, profile in DEPLOYMENT_STARTUP_PROFILES.items()}
        assert families == {
            "default": "external-state",
            "docker-compose": "external-state",
            "linux-systemd": "external-state",
            "aws-ecs": "aws-ecs",
            "azure-container-apps": "external-state",
            "kubernetes": "external-state",
        }

    def test_aws_ecs_diagnostics_point_at_the_ecs_doctor(self) -> None:
        profile = deployment_startup_profile("aws-ecs")
        assert profile.display_name == "AWS ECS"
        assert profile.doctor_command == "elspeth doctor aws-ecs"

    @pytest.mark.parametrize("target", [target for target in _TARGETS if target != "aws-ecs"])
    def test_external_state_diagnostics_point_at_the_deployment_doctor(self, target: DeploymentTarget) -> None:
        profile = deployment_startup_profile(target)
        assert profile.display_name == "External-state"
        assert profile.doctor_command == "elspeth doctor deployment"


class TestSessionEngineNotReadyError:
    """The engine-construction error keeps the exact class and bytes app.py raised."""

    def test_aws_ecs_error_is_the_ecs_subclass_with_the_legacy_message(self) -> None:
        error = deployment_startup_profile("aws-ecs").session_engine_not_ready_error()
        assert type(error) is AwsEcsSchemaNotReadyError
        assert str(error) == "AWS ECS session_schema engine could not be constructed. Run 'elspeth doctor aws-ecs' for full diagnostics."

    @pytest.mark.parametrize("target", [target for target in _TARGETS if target != "aws-ecs"])
    def test_external_state_error_is_the_neutral_class_with_the_legacy_message(self, target: DeploymentTarget) -> None:
        error = deployment_startup_profile(target).session_engine_not_ready_error()
        assert type(error) is ExternalStateSchemaNotReadyError
        assert str(error) == (
            "External-state session_schema engine could not be constructed. Run 'elspeth doctor deployment' for full diagnostics."
        )


class TestHooksDereferenceTheStartupModulesAtCallTime:
    """A patch on the startup module is observed through the profile.

    This is the seam ``test_app.py`` uses to stub the contract, directory and
    schema steps; if a profile ever captured the functions at import time the
    stubs would silently stop applying and those tests would hit the real
    checks (or pass for the wrong reason).
    """

    def test_aws_ecs_enforce_contract_routes_to_the_ecs_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[WebSettings, str]] = []
        settings = _settings(tmp_path)
        monkeypatch.setattr(
            aws_ecs_startup,
            "enforce_aws_ecs_contract",
            lambda passed, *, resolved_state_mode: calls.append((passed, resolved_state_mode)),
        )
        monkeypatch.setattr(
            external_state_startup,
            "enforce_external_state_contract",
            lambda *_a, **_k: pytest.fail("aws-ecs must not boot through the neutral contract"),
        )

        deployment_startup_profile("aws-ecs").enforce_contract(settings, resolved_state_mode="external-postgresql")

        assert calls == [(settings, "external-postgresql")]

    @pytest.mark.parametrize("target", [target for target in _TARGETS if target != "aws-ecs"])
    def test_external_state_enforce_contract_routes_to_the_neutral_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: DeploymentTarget
    ) -> None:
        calls: list[tuple[WebSettings, str]] = []
        settings = _settings(tmp_path)
        monkeypatch.setattr(
            external_state_startup,
            "enforce_external_state_contract",
            lambda passed, *, resolved_state_mode: calls.append((passed, resolved_state_mode)),
        )
        monkeypatch.setattr(
            aws_ecs_startup,
            "enforce_aws_ecs_contract",
            lambda *_a, **_k: pytest.fail(f"{target} must not boot through the ECS contract"),
        )

        deployment_startup_profile(target).enforce_contract(settings, resolved_state_mode="external-postgresql")

        assert calls == [(settings, "external-postgresql")]

    def test_aws_ecs_directories_and_schema_route_to_the_ecs_module(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        order: list[str] = []
        settings = _settings(tmp_path)
        engine = create_engine("sqlite:///:memory:")
        monkeypatch.setattr(
            aws_ecs_startup, "require_runtime_directories_mounted", lambda passed: order.append(f"dirs:{passed is settings}")
        )
        monkeypatch.setattr(
            aws_ecs_startup,
            "validate_only_schema_or_raise",
            lambda passed, passed_engine: order.append(f"schema:{passed is settings}:{passed_engine is engine}"),
        )
        monkeypatch.setattr(
            external_state_startup,
            "require_runtime_directories_mounted",
            lambda *_a: pytest.fail("aws-ecs directories must route through the ECS wrapper"),
        )

        profile = deployment_startup_profile("aws-ecs")
        profile.require_runtime_directories_mounted(settings)
        profile.validate_only_schema_or_raise(settings, engine)

        assert order == ["dirs:True", "schema:True:True"]

    def test_external_state_directories_and_schema_route_to_the_neutral_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []
        settings = _settings(tmp_path)
        engine = create_engine("sqlite:///:memory:")
        monkeypatch.setattr(
            external_state_startup, "require_runtime_directories_mounted", lambda passed: order.append(f"dirs:{passed is settings}")
        )
        monkeypatch.setattr(
            external_state_startup,
            "validate_only_schema_or_raise",
            lambda passed, passed_engine, **_k: order.append(f"schema:{passed is settings}:{passed_engine is engine}"),
        )
        monkeypatch.setattr(
            aws_ecs_startup,
            "require_runtime_directories_mounted",
            lambda *_a: pytest.fail("azure-container-apps directories must not route through the ECS wrapper"),
        )

        profile = deployment_startup_profile("azure-container-apps")
        profile.require_runtime_directories_mounted(settings)
        profile.validate_only_schema_or_raise(settings, engine)

        assert order == ["dirs:True", "schema:True:True"]


class TestPlatformIdentity:
    """The ACA profile names the platform's replica/revision variables; nothing else does."""

    def test_only_azure_container_apps_names_platform_identity_variables(self) -> None:
        named = {
            target: (profile.revision_env_var, profile.replica_identity_env_var)
            for target, profile in DEPLOYMENT_STARTUP_PROFILES.items()
            if profile.revision_env_var is not None or profile.replica_identity_env_var is not None
        }
        assert named == {"azure-container-apps": ("CONTAINER_APP_REVISION", "CONTAINER_APP_REPLICA_NAME")}

    def test_absent_variables_read_as_none(self) -> None:
        profile = deployment_startup_profile("azure-container-apps")
        assert read_platform_identity(profile, {}) == PlatformIdentity(revision=None, replica=None)

    def test_present_variables_are_read_verbatim(self) -> None:
        profile = deployment_startup_profile("azure-container-apps")
        environ = {
            "CONTAINER_APP_REVISION": "elspeth-web--a1b2c3d",
            "CONTAINER_APP_REPLICA_NAME": "elspeth-web--a1b2c3d-7d9f8c6b5-xk2pq",
        }
        assert read_platform_identity(profile, environ) == PlatformIdentity(
            revision="elspeth-web--a1b2c3d",
            replica="elspeth-web--a1b2c3d-7d9f8c6b5-xk2pq",
        )

    @pytest.mark.parametrize("target", [target for target in _TARGETS if target != "azure-container-apps"])
    def test_other_targets_ignore_the_container_apps_variables(self, target: DeploymentTarget) -> None:
        """An ECS or local process with stray CONTAINER_APP_* variables reports no platform identity."""
        environ = {"CONTAINER_APP_REVISION": "stray", "CONTAINER_APP_REPLICA_NAME": "stray"}
        assert read_platform_identity(deployment_startup_profile(target), environ) == PlatformIdentity(revision=None, replica=None)

    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param("", id="blank"),
            pytest.param("   ", id="whitespace"),
            pytest.param("rev\r\nX-Injected: 1", id="crlf"),
            pytest.param("-leading-dash", id="leading-dash"),
            pytest.param("has space", id="space"),
            pytest.param("a" * (INSTANCE_ID_MAX_LENGTH + 1), id="too-long"),
            pytest.param("tab\there", id="tab"),
            pytest.param("ünïcode", id="non-ascii"),
        ],
    )
    def test_malformed_platform_value_refuses_the_boot(self, malformed: str) -> None:
        """Present-but-unparseable is a platform-contract failure, never a silent null."""
        profile = deployment_startup_profile("azure-container-apps")
        with pytest.raises(PlatformIdentityError, match="CONTAINER_APP_REPLICA_NAME"):
            read_platform_identity(profile, {"CONTAINER_APP_REPLICA_NAME": malformed})
        with pytest.raises(PlatformIdentityError, match="CONTAINER_APP_REVISION"):
            read_platform_identity(profile, {"CONTAINER_APP_REVISION": malformed})

    def test_defaults_to_the_process_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTAINER_APP_REVISION", "elspeth-web--live")
        monkeypatch.delenv("CONTAINER_APP_REPLICA_NAME", raising=False)
        identity = read_platform_identity(deployment_startup_profile("azure-container-apps"))
        assert identity == PlatformIdentity(revision="elspeth-web--live", replica=None)


class TestInstanceId:
    @pytest.mark.parametrize(
        "value",
        ["web-7f3a2c1e-9b4d-4f6a-8c2e-1d5b7a9c3e0f", "a", "rA--replica.1_x", "A" * INSTANCE_ID_MAX_LENGTH],
    )
    def test_valid_shapes(self, value: str) -> None:
        assert is_valid_instance_id(value)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("", id="blank"),
            pytest.param(" web", id="leading-space"),
            pytest.param("web ", id="trailing-space"),
            pytest.param("-web", id="leading-dash"),
            pytest.param(".web", id="leading-dot"),
            pytest.param("web\r\nX: y", id="crlf"),
            pytest.param("A" * (INSTANCE_ID_MAX_LENGTH + 1), id="too-long"),
            pytest.param("wéb", id="non-ascii"),
        ],
    )
    def test_invalid_shapes(self, value: str) -> None:
        assert not is_valid_instance_id(value)

    def test_minted_id_is_fresh_per_call_and_wire_safe(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path)
        first = resolve_instance_id(settings)
        second = resolve_instance_id(settings)
        assert first != second
        for minted in (first, second):
            assert re.fullmatch(r"web-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", minted), minted
            assert is_valid_instance_id(minted)

    def test_configured_id_is_returned_verbatim(self, tmp_path: Path) -> None:
        settings = _settings(tmp_path, instance_id="rA--pinned.01")
        assert resolve_instance_id(settings) == "rA--pinned.01"
        assert resolve_instance_id(settings) == "rA--pinned.01"


class TestProfileType:
    def test_profile_is_a_plain_frozen_dataclass_of_facts(self) -> None:
        """No callables are stored on the profile: the hooks are methods that dereference the modules."""
        profile = DeploymentStartupProfile(
            target="kubernetes",
            contract_family="external-state",
            display_name="External-state",
            doctor_command="elspeth doctor deployment",
            replica_identity_env_var=None,
            revision_env_var=None,
        )
        assert profile == deployment_startup_profile("kubernetes")
