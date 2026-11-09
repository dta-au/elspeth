"""Strict archived options and credential-free Power Automate construction.

Archived auth is a separate schema. A fingerprint is never fed to a live
credential field, and replay never owns a resolver.
"""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictStr, field_validator, model_validator

from elspeth.contracts.enums import NodeType, RunMode
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.hashing import CANONICAL_VERSION
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.contracts.security import secret_fingerprint
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape.factory import LandscapeReadRepositories
from elspeth.plugins.infrastructure.power_automate import (
    PowerAutomateManagedIdentityAuth,
    PowerAutomateSinkOptions,
    PowerAutomateSourceOptions,
    _bounded_identifier,
    validate_trigger_url,
)

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LOCATOR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
Fingerprint = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]


class ArchivedPowerAutomateSASAuth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    method: Literal["sas_url"]
    trigger_url_secret_fingerprint: Fingerprint


class ArchivedPowerAutomateServicePrincipalAuth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    method: Literal["service_principal"]
    tenant_id: StrictStr = Field(min_length=1, max_length=256)
    client_id: StrictStr = Field(min_length=1, max_length=256)
    client_secret_fingerprint: Fingerprint

    @field_validator("tenant_id", "client_id")
    @classmethod
    def _principal_id(cls, value: str) -> str:
        return _bounded_identifier(value, 256)


ArchivedPowerAutomateAuth = Annotated[
    ArchivedPowerAutomateSASAuth | ArchivedPowerAutomateServicePrincipalAuth | PowerAutomateManagedIdentityAuth,
    Field(discriminator="method"),
]


def _admit_archived_endpoint(auth: ArchivedPowerAutomateAuth, trigger_url: str | None, allowed_origin: str) -> None:
    if isinstance(auth, ArchivedPowerAutomateSASAuth):
        if trigger_url is not None:
            raise ValueError("mixed_trigger_url")
    else:
        if trigger_url is None:
            raise ValueError("trigger_url_required")
        validate_trigger_url(trigger_url, allowed_origin=allowed_origin, sas=False)


class ArchivedPowerAutomateSourceConfig(PowerAutomateSourceOptions[ArchivedPowerAutomateAuth]):
    _plugin_component_type: ClassVar[str | None] = "source"
    auth: ArchivedPowerAutomateAuth

    @model_validator(mode="after")
    def _endpoint_auth(self) -> Self:
        _admit_archived_endpoint(self.auth, self.trigger_url, self.allowed_origin)
        return self


class ArchivedPowerAutomateSinkConfig(PowerAutomateSinkOptions[ArchivedPowerAutomateAuth]):
    _plugin_component_type: ClassVar[str | None] = "sink"
    auth: ArchivedPowerAutomateAuth

    @model_validator(mode="after")
    def _endpoint_auth(self) -> Self:
        _admit_archived_endpoint(self.auth, self.trigger_url, self.allowed_origin)
        return self


ArchivedPowerAutomateRuntimeSpec = ArchivedPowerAutomateSourceConfig | ArchivedPowerAutomateSinkConfig


@dataclass(frozen=True, slots=True)
class ArchivedPowerAutomateOptions:
    component_type: Literal["source", "sink"]
    component_name: str
    source_run_id: str
    safe_options: Mapping[str, object]
    safe_options_hash: str
    runtime_spec: ArchivedPowerAutomateRuntimeSpec

    def __post_init__(self) -> None:
        if self.component_type not in ("source", "sink") or not self.component_name or not self.source_run_id:
            raise ValueError("invalid_archived_component")
        expected_type = ArchivedPowerAutomateSourceConfig if self.component_type == "source" else ArchivedPowerAutomateSinkConfig
        if type(self.runtime_spec) is not expected_type:
            raise TypeError("archived runtime spec must match component kind")
        if _HASH.fullmatch(self.safe_options_hash) is None or stable_hash(self.safe_options) != self.safe_options_hash:
            raise AuditIntegrityError("power_automate_options_hash_invalid")
        parsed = expected_type.model_validate(deep_thaw(self.safe_options))
        if parsed != self.runtime_spec:
            raise AuditIntegrityError("power_automate_runtime_spec_invalid")
        freeze_fields(self, "safe_options")


@dataclass(frozen=True, slots=True)
class DeferredPowerAutomateCredential:
    env_name: str
    expected_fingerprint: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.env_name) is None or _HASH.fullmatch(self.expected_fingerprint) is None:
            raise ValueError("power_automate_credential_locator_invalid")

    def resolve(self) -> SecretStr:
        """Resolve only at an admitted VERIFY source read; errors are value-free."""
        refused = False
        value = ""
        actual = ""
        try:
            value = os.environ[self.env_name]
            actual = secret_fingerprint(value)
        except (KeyError, ValueError):
            refused = True
        if refused or not value or not hmac.compare_digest(actual, self.expected_fingerprint):
            raise ValueError("power_automate_credential_refused") from None
        return SecretStr(value)


@dataclass(frozen=True, slots=True)
class PowerAutomateNonliveConstruction:
    mode: RunMode
    source_run_id: str
    sources: Mapping[str, ArchivedPowerAutomateOptions]
    sinks: Mapping[str, ArchivedPowerAutomateOptions]
    source_credentials: Mapping[str, DeferredPowerAutomateCredential] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RunMode) or self.mode is RunMode.LIVE or not self.source_run_id:
            raise ValueError("invalid_nonlive_construction")
        for kind, components in (("source", self.sources), ("sink", self.sinks)):
            for name, options in components.items():
                if type(options) is not ArchivedPowerAutomateOptions or options.component_type != kind:
                    raise TypeError("archived component must be nominal and kind-bound")
                if options.component_name != name or options.source_run_id != self.source_run_id:
                    raise ValueError("archived component identity differs")
        if self.mode is RunMode.REPLAY and self.source_credentials:
            raise ValueError("replay cannot own credentials")
        if not set(self.source_credentials) <= set(self.sources):
            raise ValueError("credential source identity differs")
        if any(type(item) is not DeferredPowerAutomateCredential for item in self.source_credentials.values()):
            raise TypeError("credentials must be nominal deferred locators")
        if self.mode is RunMode.VERIFY:
            expected_sources = {
                name
                for name, options in self.sources.items()
                if isinstance(options.runtime_spec.auth, ArchivedPowerAutomateSASAuth | ArchivedPowerAutomateServicePrincipalAuth)
            }
            if set(self.source_credentials) != expected_sources:
                raise ValueError("verify credentials must bind exactly the secret-bearing sources")
            for name, credential in self.source_credentials.items():
                auth = self.sources[name].runtime_spec.auth
                if isinstance(auth, ArchivedPowerAutomateSASAuth):
                    expected = auth.trigger_url_secret_fingerprint
                elif isinstance(auth, ArchivedPowerAutomateServicePrincipalAuth):
                    expected = auth.client_secret_fingerprint
                else:
                    raise ValueError("managed identity cannot own a secret locator")
                if credential.expected_fingerprint != expected:
                    raise ValueError("credential fingerprint differs from archived source")
        freeze_fields(self, "sources", "sinks", "source_credentials")


@dataclass(frozen=True, slots=True)
class PowerAutomateArchive:
    source_run_id: str
    settings: Mapping[str, object]
    sources: Mapping[str, ArchivedPowerAutomateOptions]
    sinks: Mapping[str, ArchivedPowerAutomateOptions]

    def __post_init__(self) -> None:
        if not self.source_run_id:
            raise ValueError("source run identity required")
        freeze_fields(self, "settings", "sources", "sinks")


def has_power_automate_components(raw: Mapping[str, object]) -> bool:
    for section in ("sources", "sinks"):
        entries = raw[section] if section in raw else {}
        if isinstance(entries, Mapping):
            for entry in entries.values():
                if isinstance(entry, Mapping) and "plugin" in entry and entry["plugin"] == "power_automate":
                    return True
    return False


def admit_archived_invocation_settings(settings: Mapping[str, object]) -> tuple[RunMode, str | None]:
    """Admit persisted invocation values before intentional mode normalization."""
    if "run_mode" not in settings or "replay_from" not in settings:
        raise AuditIntegrityError("power_automate_archive_settings_invalid")
    raw_mode = settings["run_mode"]
    replay_from = settings["replay_from"]
    if type(raw_mode) is not str or raw_mode not in {mode.value for mode in RunMode}:
        raise AuditIntegrityError("power_automate_archive_settings_invalid")
    if replay_from is not None and type(replay_from) is not str:
        raise AuditIntegrityError("power_automate_archive_settings_invalid")
    mode = RunMode(raw_mode)
    if mode is not RunMode.LIVE and not replay_from:
        raise AuditIntegrityError("power_automate_archive_settings_invalid")
    return mode, replay_from


def admit_power_automate_archive(factory: LandscapeReadRepositories, source_run_id: str) -> PowerAutomateArchive:
    """Admit completed archive, settings and actual builtin node identity without construction."""
    from elspeth.contracts.enums import RunStatus
    from elspeth.plugins.sinks.power_automate import PowerAutomateSink
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    source = factory.run_lifecycle.get_run(source_run_id)
    if source is None or source.status not in (RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FAILURES, RunStatus.EMPTY):
        raise AuditIntegrityError("power_automate_archive_not_completed")
    settings, error = parse_json_strict(source.settings_json)
    if error is not None or type(settings) is not dict or stable_hash(settings) != source.config_hash:
        raise AuditIntegrityError("power_automate_archive_settings_invalid")
    archived_mode, archived_replay_from = admit_archived_invocation_settings(settings)
    if archived_mode is not source.run_mode or (archived_mode is not RunMode.LIVE and archived_replay_from != source.replay_from_run_id):
        raise AuditIntegrityError("power_automate_archive_settings_invalid")
    if source.canonical_version != CANONICAL_VERSION:
        raise AuditIntegrityError("power_automate_archive_canonical_version_invalid")
    nodes = factory.data_flow.get_nodes(source_run_id)
    lifecycle = factory.run_lifecycle.get_run_source_lifecycle_records(source_run_id)
    source_hashes = factory.run_lifecycle.get_run_source_config_hashes(source_run_id)
    admitted: dict[str, dict[str, ArchivedPowerAutomateOptions]] = {"source": {}, "sink": {}}
    expected_nodes: set[str] = set()
    for kind, node_type, cls, spec_type in (
        ("source", NodeType.SOURCE, PowerAutomateSource, ArchivedPowerAutomateSourceConfig),
        ("sink", NodeType.SINK, PowerAutomateSink, ArchivedPowerAutomateSinkConfig),
    ):
        entries = settings[kind + "s"] if kind + "s" in settings else {}
        if type(entries) is not dict:
            raise AuditIntegrityError("power_automate_archive_components_invalid")
        for name, entry in entries.items():
            if type(entry) is not dict or "plugin" not in entry or entry["plugin"] != "power_automate":
                continue
            options = entry["options"] if "options" in entry else None
            if type(options) is not dict:
                raise AuditIntegrityError("power_automate_archive_options_invalid")
            node_options = dict(options)
            if kind == "source":
                node_options["source_name"] = name
            expected_id = f"{kind}_{name}_{stable_hash(node_options)[:12]}"
            expected_nodes.add(expected_id)
            matches = [node for node in nodes if node.node_id == expected_id]
            if len(matches) != 1:
                raise AuditIntegrityError("power_automate_archive_node_missing")
            node = matches[0]
            parsed, node_error = parse_json_strict(node.config_json)
            if (
                node_error is not None
                or type(parsed) is not dict
                or stable_hash(parsed) != node.config_hash
                or parsed != node_options
                or node.node_type is not node_type
                or node.plugin_name != cls.name
                or node.plugin_version != cls.plugin_version
                or node.source_file_hash != cls.source_file_hash
            ):
                raise AuditIntegrityError("power_automate_archive_node_invalid")
            if kind == "source" and (
                expected_id not in lifecycle
                or lifecycle[expected_id].source_name != name
                or lifecycle[expected_id].lifecycle_state not in ("loaded", "exhausted")
                or expected_id not in source_hashes
                or source_hashes[expected_id] != stable_hash(options)
            ):
                raise AuditIntegrityError("power_automate_archive_source_binding_invalid")
            try:
                spec = spec_type.model_validate(options)
            except ValueError:
                raise AuditIntegrityError("power_automate_archive_options_invalid") from None
            component_type: Literal["source", "sink"] = "source" if kind == "source" else "sink"
            admitted[kind][name] = ArchivedPowerAutomateOptions(component_type, name, source_run_id, options, stable_hash(options), spec)
    if {node.node_id for node in nodes if node.plugin_name == "power_automate"} != expected_nodes:
        raise AuditIntegrityError("power_automate_archive_node_identity_invalid")
    return PowerAutomateArchive(source_run_id, settings, admitted["source"], admitted["sink"])


def project_power_automate_nonlive(
    raw: Mapping[str, object],
    archive: PowerAutomateArchive,
) -> tuple[dict[str, object], PowerAutomateNonliveConstruction]:
    """Compare effective nonsecret inputs, then retain exact archived authored options."""
    if type(archive) is not PowerAutomateArchive:
        raise TypeError("archive must be nominal PowerAutomateArchive")
    raw_mode = raw["run_mode"] if "run_mode" in raw else RunMode.LIVE
    if not isinstance(raw_mode, str):
        raise ValueError("power_automate_nonlive_identity_invalid")
    mode = RunMode(raw_mode)
    if mode is RunMode.LIVE or "replay_from" not in raw or raw["replay_from"] != archive.source_run_id:
        raise ValueError("power_automate_nonlive_identity_invalid")
    result = deep_thaw(raw)
    credentials: dict[str, DeferredPowerAutomateCredential] = {}
    for kind, archived in (("source", archive.sources), ("sink", archive.sinks)):
        entries = result[kind + "s"] if kind + "s" in result else {}
        if type(entries) is not dict:
            raise ValueError("power_automate_components_invalid")
        names = {
            name for name, entry in entries.items() if type(entry) is dict and "plugin" in entry and entry["plugin"] == "power_automate"
        }
        if names != set(archived):
            raise ValueError("power_automate_component_identity_invalid")
        for name in names:
            options = entries[name]["options"] if "options" in entries[name] else None
            if type(options) is not dict or "auth" not in options or type(options["auth"]) is not dict:
                raise ValueError("power_automate_options_invalid")
            auth = options["auth"]
            if any(key.endswith("_fingerprint") for key in auth):
                raise ValueError("power_automate_authored_fingerprint_refused")
            expected = archived[name]
            archived_auth = expected.runtime_spec.auth
            if isinstance(archived_auth, ArchivedPowerAutomateSASAuth):
                secret_key, fingerprint = "trigger_url_secret", archived_auth.trigger_url_secret_fingerprint
            elif isinstance(archived_auth, ArchivedPowerAutomateServicePrincipalAuth):
                secret_key, fingerprint = "client_secret", archived_auth.client_secret_fingerprint
            else:
                secret_key, fingerprint = None, None
            if secret_key is not None:
                value = auth[secret_key] if secret_key in auth else None
                match = _LOCATOR.fullmatch(value) if isinstance(value, str) else None
                if match is None:
                    raise ValueError("power_automate_credential_locator_invalid")
                del auth[secret_key]
                auth[secret_key + "_fingerprint"] = fingerprint
                if kind == "source" and mode is RunMode.VERIFY:
                    assert fingerprint is not None
                    credentials[name] = DeferredPowerAutomateCredential(match[1], fingerprint)
            spec_type = ArchivedPowerAutomateSourceConfig if kind == "source" else ArchivedPowerAutomateSinkConfig
            try:
                candidate = spec_type.model_validate(options)
            except ValueError:
                raise ValueError("power_automate_options_invalid") from None
            if candidate != expected.runtime_spec:
                raise ValueError("power_automate_options_differ")
            entries[name]["options"] = deep_thaw(expected.safe_options)
    return result, PowerAutomateNonliveConstruction(mode, archive.source_run_id, archive.sources, archive.sinks, credentials)
