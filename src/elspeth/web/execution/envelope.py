"""Server-only durable execution inputs and fail-closed recovery admission.

Secret fingerprints identify the admitted value version. The current secret
store cannot retrieve historical versions: a rotation therefore refuses
recovery. Verified plaintext lives only in the returned in-memory resolver.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import distributions
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from elspeth.contracts.aws_s3 import S3ProfiledAuditIdentities
from elspeth.contracts.aws_textract import TextractProfiledAuditIdentities
from elspeth.contracts.blobs_inline import BlobContentResolutionError
from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.contracts.secrets import (
    ResolvedSecret,
    ScopedSecretResolverContract,
    SecretInventoryItem,
    SecretScope,
    WebSecretResolver,
)
from elspeth.core.blobs_inline import _discover_blob_content_refs
from elspeth.core.secrets import (
    SecretResolutionError,
    collect_credential_field_violations,
    resolve_secret_refs,
    secret_env_ref_name,
)
from elspeth.plugins.sources.blob_rows import BlobRowsEntry
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.retained_inputs import RetainedInputUnavailable, RetainedRunInput, verify_retained_input
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot


class EnvelopeRecoveryReason(StrEnum):
    INVALID_ENVELOPE = "invalid_execution_envelope"
    PRINCIPAL_CHANGED = "admitted_principal_changed"
    POLICY_CHANGED = "admitted_policy_changed"
    BINDING_CHANGED = "admitted_binding_changed"
    IMPLEMENTATION_CHANGED = "implementation_changed"
    DEPLOYMENT_CHANGED = "deployment_generation_changed"
    SECRET_VERSION_UNAVAILABLE = "secret_version_unavailable"
    SECRET_VERSION_CHANGED = "secret_version_changed"
    LITERAL_CREDENTIAL = "literal_credential_not_durable"
    ENVIRONMENT_REFERENCE = "environment_reference_not_durable"
    SOURCE_UNAVAILABLE = "retained_source_unavailable"
    RUNTIME_CHANGED = "runtime_changed"
    SCHEMA_INCOMPATIBLE = "schema_incompatible"
    PROTOCOL_INCOMPATIBLE = "coordination_protocol_incompatible"
    AUTOMATIC_RECOVERY_INELIGIBLE = "automatic_recovery_ineligible"


class ExecutionEnvelopeRefused(ValueError):
    def __init__(self, reason: EnvelopeRecoveryReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class SecretVersionReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    requested_scope: SecretScope | None
    scope: SecretScope
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ExecutionBlobReference:
    """A closed executable reference slot, excluding all content bytes."""

    blob_id: UUID
    content_hash: str
    expected_size_bytes: int | None
    field_path: str
    kind: Literal["inline_content", "blob_rows"]
    filename: str | None = None
    mime_type: str | None = None


@dataclass(frozen=True, slots=True)
class RetainedBlobInput:
    reference: ExecutionBlobReference
    retained: RetainedRunInput
    size_bytes: int
    filename: str
    mime_type: str


class _EnvelopePayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    user_id: str | None
    auth_provider_type: str | None
    implementation_fingerprint: str
    deployment_generation: str
    plugin_snapshot: PluginAvailabilitySnapshot
    web_plugin_policy_evidence: WebPluginPolicyEvidence | None = None
    executable_config: dict[str, JsonValue]
    audit_safe_config: dict[str, JsonValue]
    profiled_s3_audit_identities: S3ProfiledAuditIdentities
    profiled_textract_audit_identities: TextractProfiledAuditIdentities
    secret_versions: tuple[SecretVersionReference, ...]
    env_ref_names: frozenset[str]
    retained_inputs: tuple[RetainedRunInput, ...]
    openrouter_catalog_sha256: str | None
    openrouter_catalog_source: Literal["live", "bundled"] | None
    blob_inputs: tuple[RetainedBlobInput, ...]


@dataclass(frozen=True, slots=True)
class ExecutionEnvelope:
    """Immutable serialized server-only configuration, never an export DTO."""

    serialized: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.serialized.encode()).hexdigest()

    def to_json(self) -> str:
        return self.serialized


@dataclass(frozen=True, slots=True)
class CancelledExecutionEnvelope:
    """Admission evidence for cancellation; never executable settings."""

    audit_safe_config: Mapping[str, Any]
    plugin_snapshot: PluginAvailabilitySnapshot
    user_id: str | None
    auth_provider_type: str | None
    openrouter_catalog_sha256: str
    openrouter_catalog_source: Literal["live", "bundled"]
    web_plugin_policy_evidence: WebPluginPolicyEvidence | None

    def __post_init__(self) -> None:
        freeze_fields(self, "audit_safe_config")


def read_cancelled_execution_envelope(execution_input: RunExecutionInput) -> CancelledExecutionEnvelope:
    """Cancellation needs admitted identity, without resolving executable inputs."""
    if ExecutionEnvelope(execution_input.envelope_json).digest != execution_input.canonical_input_digest:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
    try:
        payload = _EnvelopePayload.model_validate_json(execution_input.envelope_json)
    except ValidationError:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE) from None
    if payload.openrouter_catalog_sha256 is None or payload.openrouter_catalog_source is None:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
    return CancelledExecutionEnvelope(
        payload.audit_safe_config,
        payload.plugin_snapshot,
        payload.user_id,
        payload.auth_provider_type,
        payload.openrouter_catalog_sha256,
        payload.openrouter_catalog_source,
        payload.web_plugin_policy_evidence,
    )


@dataclass(frozen=True, slots=True)
class RunExecutionInput:
    """Immutable Sessions execution-input row, without database dependencies."""

    schema_version: int
    envelope_json: str
    canonical_input_digest: str
    topology_digest: str
    source_manifest_digest: str
    application_fingerprint: str
    plugin_registry_fingerprint: str
    configuration_fingerprint: str
    graph_fingerprint: str
    runtime_fingerprint: str
    implementation_fingerprint: str
    deployment_generation: str
    session_epoch: int
    landscape_epoch: int
    coordination_protocol: int
    automatic_recovery_eligible: bool


class PinnedSecretResolver(ScopedSecretResolverContract):
    """A single principal's verified versions; no fallback to mutable storage."""

    def __init__(self, user_id: str | None, values: tuple[tuple[SecretVersionReference, ResolvedSecret], ...]) -> None:
        self._user_id = user_id
        self._values = values

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        if user_id != self._user_id:
            return []
        return [SecretInventoryItem(ref.name, ref.scope, True) for ref, _ in self._values]

    def has_ref(self, user_id: str, name: str) -> bool:
        return self.resolve(user_id, name) is not None

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        if user_id != self._user_id:
            return None
        for ref, value in self._values:
            if ref.name == name and ref.requested_scope is None:
                return value
        return None

    def resolve_scoped(self, user_id: str, name: str, scope: SecretScope) -> ResolvedSecret | None:
        if user_id != self._user_id:
            return None
        for ref, value in self._values:
            if ref.name == name and ref.scope == scope:
                return value
        return None


@dataclass(frozen=True, slots=True)
class RestoredExecutionEnvelope:
    settings: FrozenRunSettings
    secret_resolver: PinnedSecretResolver
    env_ref_names: frozenset[str]
    retained_inputs: tuple[RetainedRunInput, ...]
    openrouter_catalog_sha256: str | None
    openrouter_catalog_source: Literal["live", "bundled"] | None
    blob_inputs: tuple[RetainedBlobInput, ...]


class _RecordingResolver(ScopedSecretResolverContract):
    def __init__(self, resolver: WebSecretResolver | None) -> None:
        self._resolver = resolver
        self.references: list[SecretVersionReference] = []

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        return [] if self._resolver is None else self._resolver.list_refs(user_id)

    def has_ref(self, user_id: str, name: str) -> bool:
        return self.resolve(user_id, name) is not None

    def _record(self, value: ResolvedSecret | None, scope: SecretScope | None) -> ResolvedSecret | None:
        if value is not None:
            reference = SecretVersionReference(name=value.name, requested_scope=scope, scope=value.scope, fingerprint=value.fingerprint)
            for previous in self.references:
                if previous.name == reference.name and previous.scope == reference.scope and previous.fingerprint != reference.fingerprint:
                    raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SECRET_VERSION_CHANGED)
                if previous.name == reference.name and previous.requested_scope == scope:
                    if previous != reference:
                        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SECRET_VERSION_CHANGED)
                    return value
            self.references.append(reference)
        return value

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        return self._record(None if self._resolver is None else self._resolver.resolve(user_id, name), None)

    def resolve_scoped(self, user_id: str, name: str, scope: SecretScope) -> ResolvedSecret | None:
        resolver: object = self._resolver
        if not isinstance(resolver, ScopedSecretResolverContract):
            return None
        return self._record(resolver.resolve_scoped(user_id, name, scope), scope)


def _validate_environment_refs(value: JsonValue, env_ref_names: frozenset[str]) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _validate_environment_refs(child, env_ref_names)
    elif isinstance(value, list):
        for child in value:
            _validate_environment_refs(child, env_ref_names)
    elif isinstance(value, str) and "${" in value and (":-" in value or secret_env_ref_name(value, env_ref_names) is None):
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.ENVIRONMENT_REFERENCE)


def capture_execution_envelope(
    frozen: FrozenRunSettings,
    *,
    user_id: str | None,
    auth_provider_type: str | None,
    resolver: WebSecretResolver | None,
    env_ref_names: frozenset[str],
    implementation_fingerprint: str,
    deployment_generation: str,
    retained_inputs: tuple[RetainedRunInput, ...] = (),
    openrouter_catalog_sha256: str | None = None,
    openrouter_catalog_source: Literal["live", "bundled"] | None = None,
    blob_inputs: tuple[RetainedBlobInput, ...] = (),
    web_plugin_policy_evidence: WebPluginPolicyEvidence | None = None,
) -> ExecutionEnvelope:
    """Capture admitted references and policy; never persist resolved values."""
    if user_id is None and not frozen.plugin_snapshot.is_trained_operator:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.PRINCIPAL_CHANGED)
    _verify_blob_manifest(discover_execution_blob_inputs(frozen), blob_inputs)
    executable = deep_thaw(frozen.executable_config)
    audit_safe = deep_thaw(frozen.audit_safe_config)
    for config in (executable, audit_safe):
        if collect_credential_field_violations(config, env_ref_names):
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.LITERAL_CREDENTIAL)
        _validate_environment_refs(config, env_ref_names)
    recording = _RecordingResolver(resolver)
    try:
        # The resolved copy is deliberately discarded; only version metadata
        # crosses the durable boundary. No YAML expansion runs during capture.
        resolve_secret_refs(executable, recording, user_id or "", env_ref_names=env_ref_names)
    except SecretResolutionError:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SECRET_VERSION_UNAVAILABLE) from None
    payload = _EnvelopePayload(
        user_id=user_id,
        auth_provider_type=auth_provider_type,
        implementation_fingerprint=implementation_fingerprint,
        deployment_generation=deployment_generation,
        plugin_snapshot=frozen.plugin_snapshot,
        web_plugin_policy_evidence=web_plugin_policy_evidence,
        executable_config=executable,
        audit_safe_config=audit_safe,
        profiled_s3_audit_identities=frozen.profiled_s3_audit_identities,
        profiled_textract_audit_identities=frozen.profiled_textract_audit_identities,
        secret_versions=tuple(sorted(recording.references, key=lambda ref: (ref.name, ref.requested_scope or ""))),
        env_ref_names=env_ref_names,
        retained_inputs=retained_inputs,
        openrouter_catalog_sha256=openrouter_catalog_sha256,
        openrouter_catalog_source=openrouter_catalog_source,
        blob_inputs=blob_inputs,
    )
    document = payload.model_dump(mode="json")
    document["env_ref_names"] = sorted(document["env_ref_names"])
    document["plugin_snapshot"]["available"] = sorted(
        document["plugin_snapshot"]["available"], key=lambda plugin: (plugin["kind"], plugin["name"])
    )
    return ExecutionEnvelope(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False))


def restore_execution_envelope(
    serialized: str,
    *,
    current_snapshot: PluginAvailabilitySnapshot,
    user_id: str | None,
    auth_provider_type: str | None,
    resolver: WebSecretResolver | None,
    implementation_fingerprint: str,
    deployment_generation: str,
    blob_verifier: Callable[[RetainedBlobInput], None] | None = None,
) -> RestoredExecutionEnvelope:
    """Re-admit current authority and pin exact admitted credential versions."""
    try:
        payload = _EnvelopePayload.model_validate_json(serialized)
    except ValidationError:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE) from None
    admitted = payload.plugin_snapshot
    if (payload.user_id, payload.auth_provider_type) != (
        user_id,
        auth_provider_type,
    ) or admitted.principal_scope != current_snapshot.principal_scope:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.PRINCIPAL_CHANGED)
    if admitted.policy_hash != current_snapshot.policy_hash:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.POLICY_CHANGED)
    if admitted != current_snapshot:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.BINDING_CHANGED)
    if payload.implementation_fingerprint != implementation_fingerprint:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.IMPLEMENTATION_CHANGED)
    if payload.deployment_generation != deployment_generation:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.DEPLOYMENT_CHANGED)
    try:
        for retained in payload.retained_inputs:
            verify_retained_input(retained)
    except RetainedInputUnavailable:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SOURCE_UNAVAILABLE) from None
    frozen = FrozenRunSettings(
        plugin_snapshot=admitted,
        executable_config=payload.executable_config,
        audit_safe_config=payload.audit_safe_config,
        profiled_s3_audit_identities=payload.profiled_s3_audit_identities,
        profiled_textract_audit_identities=payload.profiled_textract_audit_identities,
    )
    _verify_blob_manifest(discover_execution_blob_inputs(frozen), payload.blob_inputs)
    if payload.blob_inputs and blob_verifier is None:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SOURCE_UNAVAILABLE)
    if blob_verifier is not None:
        for blob in payload.blob_inputs:
            blob_verifier(blob)
    pinned: list[tuple[SecretVersionReference, ResolvedSecret]] = []
    for reference in payload.secret_versions:
        if resolver is None or user_id is None:
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SECRET_VERSION_UNAVAILABLE)
        if isinstance(resolver, ScopedSecretResolverContract):
            value = resolver.resolve_scoped(user_id, reference.name, reference.scope)
        else:
            value = resolver.resolve(user_id, reference.name)
        if value is None:
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SECRET_VERSION_UNAVAILABLE)
        if (value.name, value.scope, value.fingerprint) != (reference.name, reference.scope, reference.fingerprint):
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SECRET_VERSION_CHANGED)
        pinned.append((reference, value))
    pinned_resolver = PinnedSecretResolver(user_id, tuple(pinned))
    try:
        resolve_secret_refs(payload.executable_config, pinned_resolver, user_id or "", env_ref_names=payload.env_ref_names)
    except SecretResolutionError:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE) from None
    return RestoredExecutionEnvelope(
        settings=frozen,
        secret_resolver=pinned_resolver,
        env_ref_names=payload.env_ref_names,
        retained_inputs=payload.retained_inputs,
        openrouter_catalog_sha256=payload.openrouter_catalog_sha256,
        openrouter_catalog_source=payload.openrouter_catalog_source,
        blob_inputs=payload.blob_inputs,
    )


def runtime_implementation_fingerprint(snapshot: PluginAvailabilitySnapshot) -> str:
    """Bind actual application source, installed dependencies, and plugin IDs.

    Deployment images must remain immutable while workers are running. This
    measures shared helpers and engine code as well as individual plugin files.
    """
    digest = hashlib.sha256()
    package_root = Path(__file__).resolve().parents[2]
    for source in sorted(package_root.rglob("*.py")):
        digest.update(source.relative_to(package_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(source.read_bytes()).digest())
    dependencies = sorted((distribution.metadata["Name"], distribution.version) for distribution in distributions())
    digest.update(json.dumps(dependencies, separators=(",", ":")).encode())
    digest.update(json.dumps(sorted(map(str, snapshot.available)), separators=(",", ":")).encode())
    return digest.hexdigest()


def _digest_json(value: JsonValue) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def current_runtime_fingerprint() -> str:
    """The interpreter and platform identity required by admitted execution."""
    return _digest_json({"python": sys.version, "implementation": sys.implementation.name, "platform": platform.platform()})


def discover_execution_blob_inputs(frozen: FrozenRunSettings) -> tuple[ExecutionBlobReference, ...]:
    """Extract inline markers and blob_rows entries from the executable tree."""
    config = deep_thaw(frozen.executable_config)
    try:
        inline = _discover_blob_content_refs(config)
    except BlobContentResolutionError:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE) from None
    references = [ExecutionBlobReference(ref.blob_id, ref.sha256, None, ref.field_path, "inline_content") for ref in inline]
    sources: list[tuple[str, JsonValue]] = []
    if "source" in config:
        sources.append(("source", config["source"]))
    if "sources" in config:
        named = config["sources"]
        if not isinstance(named, dict):
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
        sources.extend((f"source:{name}", source) for name, source in named.items())
    for label, source in sources:
        if not isinstance(source, dict) or "plugin" not in source or source["plugin"] != "blob_rows":
            continue
        if "options" not in source:
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
        options = source["options"]
        if not isinstance(options, dict) or "blobs" not in options:
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
        entries = options["blobs"]
        if not isinstance(entries, list) or not entries:
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
        for index, raw in enumerate(entries):
            try:
                entry = BlobRowsEntry.model_validate(raw)
            except ValidationError:
                raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE) from None
            references.append(
                ExecutionBlobReference(
                    UUID(entry.blob_id),
                    entry.payload_ref,
                    entry.size_bytes,
                    f"{label}.options.blobs[{index}]",
                    "blob_rows",
                    entry.filename,
                    entry.mime_type,
                )
            )
    return tuple(sorted(references, key=lambda ref: ref.field_path))


def _verify_blob_manifest(references: tuple[ExecutionBlobReference, ...], retained: tuple[RetainedBlobInput, ...]) -> None:
    if tuple(sorted((item.reference for item in retained), key=lambda ref: ref.field_path)) != references:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SOURCE_UNAVAILABLE)
    for blob in retained:
        ref = blob.reference
        if (
            ref.content_hash != blob.retained.content_hash
            or blob.size_bytes != blob.retained.size_bytes
            or (ref.expected_size_bytes is not None and ref.expected_size_bytes != blob.size_bytes)
            or (ref.filename is not None and ref.filename != blob.filename)
            or (ref.mime_type is not None and ref.mime_type != blob.mime_type)
        ):
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SOURCE_UNAVAILABLE)
        try:
            verify_retained_input(blob.retained)
        except RetainedInputUnavailable:
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SOURCE_UNAVAILABLE) from None


def _declared_source_paths(config: dict[str, JsonValue]) -> tuple[str, ...]:
    sources: list[JsonValue] = []
    if "source" in config:
        sources.append(config["source"])
    if "sources" in config:
        named = config["sources"]
        if not isinstance(named, dict):
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
        sources.extend(named.values())
    paths: list[str] = []
    for source in sources:
        if not isinstance(source, dict) or "options" not in source:
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
        options = source["options"]
        if not isinstance(options, dict):
            raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
        if "path" in options:
            path = options["path"]
            if not isinstance(path, str):
                raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
            paths.append(path)
    return tuple(paths)


def build_run_execution_input(
    envelope: ExecutionEnvelope,
    frozen: FrozenRunSettings,
    implementation_fingerprint: str,
    deployment_generation: str,
    retained_inputs: tuple[RetainedRunInput, ...],
) -> RunExecutionInput:
    """Derive Sessions identities from actual admitted material.

    The graph/topology identity describes the declared executable pipeline.
    Engine checkpoint topology remains independently verified against the
    instantiated ExecutionGraph by the engine's normal resume admission.
    """
    from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
    from elspeth.web.coordination.contracts import WEB_COORDINATION_PROTOCOL_VERSION
    from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

    payload = _EnvelopePayload.model_validate_json(envelope.to_json())
    if (
        payload.executable_config != deep_thaw(frozen.executable_config)
        or payload.audit_safe_config != deep_thaw(frozen.audit_safe_config)
        or payload.plugin_snapshot != frozen.plugin_snapshot
        or payload.implementation_fingerprint != implementation_fingerprint
        or payload.deployment_generation != deployment_generation
        or payload.retained_inputs != retained_inputs
    ):
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
    retained_paths = {item.retained_path for item in retained_inputs}
    if not set(_declared_source_paths(payload.executable_config)).issubset(retained_paths):
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SOURCE_UNAVAILABLE)
    for item in retained_inputs:
        verify_retained_input(item)
    _verify_blob_manifest(discover_execution_blob_inputs(frozen), payload.blob_inputs)
    graph_digest = _digest_json(payload.executable_config)
    document = payload.model_dump(mode="json")
    registry_ids: list[JsonValue] = [str(plugin_id) for plugin_id in sorted(frozen.plugin_snapshot.available)]
    return RunExecutionInput(
        schema_version=1,
        envelope_json=envelope.to_json(),
        canonical_input_digest=envelope.digest,
        topology_digest=graph_digest,
        source_manifest_digest=_digest_json({"files": document["retained_inputs"], "blobs": document["blob_inputs"]}),
        application_fingerprint=implementation_fingerprint,
        plugin_registry_fingerprint=_digest_json(registry_ids),
        configuration_fingerprint=_digest_json({"executable": payload.executable_config, "audit": payload.audit_safe_config}),
        graph_fingerprint=graph_digest,
        runtime_fingerprint=current_runtime_fingerprint(),
        implementation_fingerprint=implementation_fingerprint,
        deployment_generation=deployment_generation,
        session_epoch=SESSION_SCHEMA_EPOCH,
        landscape_epoch=SQLITE_SCHEMA_EPOCH,
        coordination_protocol=WEB_COORDINATION_PROTOCOL_VERSION,
        automatic_recovery_eligible=True,
    )


def validate_run_execution_input(execution_input: RunExecutionInput) -> None:
    """Verify the persisted admission identities against this worker before use."""
    from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
    from elspeth.web.coordination.contracts import WEB_COORDINATION_PROTOCOL_VERSION
    from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

    envelope = ExecutionEnvelope(execution_input.envelope_json)
    if envelope.digest != execution_input.canonical_input_digest:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
    if execution_input.schema_version != 1 or (execution_input.session_epoch, execution_input.landscape_epoch) != (
        SESSION_SCHEMA_EPOCH,
        SQLITE_SCHEMA_EPOCH,
    ):
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.SCHEMA_INCOMPATIBLE)
    if execution_input.coordination_protocol != WEB_COORDINATION_PROTOCOL_VERSION:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.PROTOCOL_INCOMPATIBLE)
    if execution_input.runtime_fingerprint != current_runtime_fingerprint():
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.RUNTIME_CHANGED)
    if not execution_input.automatic_recovery_eligible:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.AUTOMATIC_RECOVERY_INELIGIBLE)
    try:
        payload = _EnvelopePayload.model_validate_json(envelope.to_json())
    except ValidationError:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE) from None
    frozen = FrozenRunSettings(
        plugin_snapshot=payload.plugin_snapshot,
        executable_config=payload.executable_config,
        audit_safe_config=payload.audit_safe_config,
        profiled_s3_audit_identities=payload.profiled_s3_audit_identities,
        profiled_textract_audit_identities=payload.profiled_textract_audit_identities,
    )
    expected = build_run_execution_input(
        envelope,
        frozen,
        payload.implementation_fingerprint,
        payload.deployment_generation,
        payload.retained_inputs,
    )
    if execution_input != expected:
        raise ExecutionEnvelopeRefused(EnvelopeRecoveryReason.INVALID_ENVELOPE)
