"""Durable run inputs retain authority and pin secret versions without values."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.contracts.secrets import ResolvedSecret, ScopedSecretResolverContract, SecretInventoryItem, SecretScope
from elspeth.core.secrets import resolve_secret_refs
from elspeth.web.execution.envelope import (
    EnvelopeRecoveryReason,
    ExecutionEnvelopeRefused,
    ExecutionRecoveryAuthority,
    RetainedBlobInput,
    build_run_execution_input,
    capture_execution_envelope,
    discover_execution_blob_inputs,
    read_cancelled_execution_envelope,
    restore_execution_envelope,
    restore_run_execution_input,
    validate_run_execution_input,
)
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.retained_inputs import retain_execution_inputs, retain_source_file
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot, PluginId


class SecretStore(ScopedSecretResolverContract):
    def __init__(self) -> None:
        self.secret: ResolvedSecret | None = ResolvedSecret("ACCESS", "private-runtime-material", "server", "a" * 64)

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        return [SecretInventoryItem("ACCESS", "server", self.secret is not None)]

    def has_ref(self, user_id: str, name: str) -> bool:
        return self.resolve(user_id, name) is not None

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        return self.secret if name == "ACCESS" else None

    def resolve_scoped(self, user_id: str, name: str, scope: SecretScope) -> ResolvedSecret | None:
        value = self.resolve(user_id, name)
        return value if value is not None and value.scope == scope else None


def snapshot() -> PluginAvailabilitySnapshot:
    return PluginAvailabilitySnapshot.create(
        policy_hash="b" * 64,
        principal_scope="oidc:user",
        available=frozenset({PluginId("source", "csv")}),
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="c" * 64,
    )


def test_envelope_preserves_field_mapper_labels_in_actual_generated_runtime_configs() -> None:
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.composer.yaml_generator import generate_pipeline_dict
    from elspeth.web.dependencies import create_catalog_service

    state = CompositionState.from_dict(
        {
            "sources": {},
            "nodes": [
                {
                    "id": "rename_customer",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "rows",
                    "on_success": "output",
                    "on_error": "discard",
                    "options": {"mapping": {"customer_key": "customer_id"}, "schema": {"mode": "observed"}},
                }
            ],
            "edges": [],
            "outputs": [],
            "metadata": {"name": "synthetic", "description": ""},
            "version": 1,
        }
    )
    generated = generate_pipeline_dict(state)
    assert generated["transforms"][0]["options"]["mapping"] == {"customer_key": "customer_id"}
    frozen = FrozenRunSettings(PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service()), generated, generated)
    before = deep_thaw(frozen.executable_config)
    envelope = capture_execution_envelope(
        frozen,
        user_id="user",
        auth_provider_type="local",
        resolver=None,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    payload = json.loads(envelope.to_json())
    assert payload["executable_config"] == payload["audit_safe_config"] == before == generated
    assert deep_thaw(frozen.executable_config) == deep_thaw(frozen.audit_safe_config) == before


def test_cancellation_reads_admission_without_secret_version_or_runtime() -> None:
    store = SecretStore()
    frozen = FrozenRunSettings(snapshot(), {"api_key": {"secret_ref": "ACCESS"}}, {"profile": {"aliases": ["safe"]}})
    admitted_evidence = WebPluginPolicyEvidence(
        schema_version=1,
        policy_hash=frozen.plugin_snapshot.policy_hash,
        snapshot_hash=frozen.plugin_snapshot.snapshot_hash,
        authorized_plugin_ids=("source:csv",),
        available_plugin_ids=("source:csv",),
        control_modes=(),
        selected_implementations=(),
        selected_profile_aliases=(),
        plugin_code_identities=(("source:csv", "1.0.0", "sha256:" + "a" * 16),),
        binding_generation_fingerprint=frozen.plugin_snapshot.binding_generation_fingerprint,
        decision_codes=("admitted",),
    )
    envelope = capture_execution_envelope(
        frozen,
        user_id="user",
        auth_provider_type="oidc",
        resolver=store,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="old-image",
        openrouter_catalog_sha256="e" * 64,
        openrouter_catalog_source="bundled",
        web_plugin_policy_evidence=admitted_evidence,
    )
    execution_input = build_run_execution_input(envelope, frozen, "d" * 64, "old-image", ())
    store.secret = None
    metadata = read_cancelled_execution_envelope(replace(execution_input, runtime_fingerprint="f" * 64))
    assert deep_thaw(metadata.audit_safe_config) == {"profile": {"aliases": ["safe"]}}
    with pytest.raises(TypeError):
        metadata.audit_safe_config["profile"] = "changed"
    with pytest.raises(TypeError):
        metadata.audit_safe_config["profile"]["aliases"] = ["changed"]
    with pytest.raises(TypeError):
        metadata.audit_safe_config["profile"]["aliases"][0] = "changed"
    mutable_copy = deep_thaw(metadata.audit_safe_config)
    detached = replace(metadata, audit_safe_config=mutable_copy)
    mutable_copy["profile"]["aliases"][0] = "changed"
    assert deep_thaw(metadata.audit_safe_config) == {"profile": {"aliases": ["safe"]}}
    assert detached.audit_safe_config == metadata.audit_safe_config
    assert read_cancelled_execution_envelope(execution_input).audit_safe_config == metadata.audit_safe_config
    assert metadata.user_id == "user"
    assert metadata.openrouter_catalog_sha256 == "e" * 64
    assert metadata.web_plugin_policy_evidence == admitted_evidence
    assert "private-runtime-material" not in envelope.to_json()


def test_round_trip_pins_secret_material_in_memory_only() -> None:
    store = SecretStore()
    admitted = FrozenRunSettings(
        snapshot(),
        {"source": {"api_key": {"secret_ref": "ACCESS"}}},
        {"source": {"profile": "safe"}},
    )
    envelope = capture_execution_envelope(
        admitted,
        user_id="user",
        auth_provider_type="oidc",
        resolver=store,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    serialized = envelope.to_json()
    assert "private-runtime-material" not in serialized
    assert "a" * 64 in serialized
    restored = restore_execution_envelope(
        serialized,
        current_snapshot=snapshot(),
        user_id="user",
        auth_provider_type="oidc",
        resolver=store,
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    assert restored.settings == admitted
    store.secret = ResolvedSecret("ACCESS", "rotated-value", "server", "e" * 64)
    config, secrets = resolve_secret_refs(deep_thaw(restored.settings.executable_config), restored.secret_resolver, "user")
    assert config["source"]["api_key"] == "private-runtime-material"
    assert secrets[0].fingerprint == "a" * 64
    assert restored.secret_resolver.resolve("other-user", "ACCESS") is None


@pytest.mark.parametrize(
    "change,reason",
    [
        ("secret", EnvelopeRecoveryReason.SECRET_VERSION_CHANGED),
        ("missing", EnvelopeRecoveryReason.SECRET_VERSION_UNAVAILABLE),
        ("principal", EnvelopeRecoveryReason.PRINCIPAL_CHANGED),
        ("provider", EnvelopeRecoveryReason.PRINCIPAL_CHANGED),
        ("policy", EnvelopeRecoveryReason.POLICY_CHANGED),
        ("implementation", EnvelopeRecoveryReason.IMPLEMENTATION_CHANGED),
        ("deployment", EnvelopeRecoveryReason.DEPLOYMENT_CHANGED),
    ],
)
@pytest.mark.parametrize("combined", [False, True])
def test_restore_refuses_changed_admission(change: str, reason: EnvelopeRecoveryReason, combined: bool) -> None:
    store = SecretStore()
    admitted = FrozenRunSettings(snapshot(), {"api_key": {"secret_ref": "ACCESS"}}, {})
    envelope = capture_execution_envelope(
        admitted,
        user_id="user",
        auth_provider_type="oidc",
        resolver=store,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    if change == "secret":
        store.secret = ResolvedSecret("ACCESS", "rotated-value", "server", "e" * 64)
    elif change == "missing":
        store.secret = None
    with pytest.raises(ExecutionEnvelopeRefused) as caught:
        if combined:
            execution_input = build_run_execution_input(envelope, admitted, "d" * 64, "image-1", ())
            restore_run_execution_input(
                execution_input,
                current_authority=lambda: ExecutionRecoveryAuthority(
                    replace(snapshot(), policy_hash="f" * 64) if change == "policy" else snapshot(),
                    "f" * 64 if change == "implementation" else "d" * 64,
                    "image-2" if change == "deployment" else "image-1",
                ),
                user_id="other" if change == "principal" else "user",
                auth_provider_type="other" if change == "provider" else "oidc",
                resolver=store,
            )
            pytest.fail("changed recovery authority was accepted")
        restore_execution_envelope(
            envelope.to_json(),
            current_snapshot=replace(snapshot(), policy_hash="f" * 64) if change == "policy" else snapshot(),
            user_id="other" if change == "principal" else "user",
            auth_provider_type="other" if change == "provider" else "oidc",
            resolver=store,
            implementation_fingerprint="f" * 64 if change == "implementation" else "d" * 64,
            deployment_generation="image-2" if change == "deployment" else "image-1",
        )
    assert caught.value.reason is reason


def test_restore_refuses_missing_version_metadata_before_dispatch() -> None:
    store = SecretStore()
    admitted = FrozenRunSettings(snapshot(), {"api_key": {"secret_ref": "ACCESS"}}, {})
    envelope = capture_execution_envelope(
        admitted,
        user_id="user",
        auth_provider_type="oidc",
        resolver=store,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    payload = json.loads(envelope.to_json())
    payload["secret_versions"] = []
    with pytest.raises(ExecutionEnvelopeRefused) as caught:
        restore_execution_envelope(
            json.dumps(payload),
            current_snapshot=snapshot(),
            user_id="user",
            auth_provider_type="oidc",
            resolver=store,
            implementation_fingerprint="d" * 64,
            deployment_generation="image-1",
        )
    assert caught.value.reason is EnvelopeRecoveryReason.INVALID_ENVELOPE


@pytest.mark.parametrize(
    "config",
    [
        {"api_key": "literal-not-permitted"},
        {"path": "${UNBOUND_PATH}"},
        {"api_key": "${ACCESS:-literal-fallback}"},
    ],
)
def test_capture_rejects_literal_credentials_and_unpinned_environment(config: dict[str, str]) -> None:
    with pytest.raises(ExecutionEnvelopeRefused):
        capture_execution_envelope(
            FrozenRunSettings(snapshot(), config, {}),
            user_id="user",
            auth_provider_type="oidc",
            resolver=SecretStore(),
            env_ref_names=frozenset({"ACCESS"}),
            implementation_fingerprint="d" * 64,
            deployment_generation="image-1",
        )


def test_capture_refuses_rotation_between_scoped_and_unscoped_uses() -> None:
    class RotatingStore(SecretStore):
        def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
            value = super().resolve(user_id, name)
            self.secret = ResolvedSecret("ACCESS", "rotated-value", "server", "e" * 64)
            return value

    frozen = FrozenRunSettings(
        snapshot(),
        {
            "api_key": {"secret_ref": "ACCESS"},
            "token": {"secret_ref": "ACCESS", "secret_scope": "server"},
        },
        {},
    )
    with pytest.raises(ExecutionEnvelopeRefused) as caught:
        capture_execution_envelope(
            frozen,
            user_id="user",
            auth_provider_type="oidc",
            resolver=RotatingStore(),
            env_ref_names=frozenset(),
            implementation_fingerprint="d" * 64,
            deployment_generation="image-1",
        )
    assert caught.value.reason is EnvelopeRecoveryReason.SECRET_VERSION_CHANGED


def test_retained_source_and_catalog_survive_source_edit(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("value\nadmitted\n")
    original = FrozenRunSettings(snapshot(), {"source": {"plugin": "csv", "options": {"path": str(source)}}}, {})
    frozen, retained = retain_execution_inputs(original, root=tmp_path / "retained")
    envelope = capture_execution_envelope(
        frozen,
        user_id="user",
        auth_provider_type="oidc",
        resolver=None,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
        retained_inputs=retained,
        openrouter_catalog_sha256="e" * 64,
        openrouter_catalog_source="bundled",
    )
    inputs = build_run_execution_input(envelope, frozen, "d" * 64, "image-1", retained)
    assert inputs.canonical_input_digest == envelope.digest
    assert inputs.automatic_recovery_eligible
    source.write_text("value\nchanged\n")
    restored = restore_execution_envelope(
        envelope.to_json(),
        current_snapshot=snapshot(),
        user_id="user",
        auth_provider_type="oidc",
        resolver=None,
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    assert restored.openrouter_catalog_sha256 == "e" * 64
    assert restored.openrouter_catalog_source == "bundled"
    assert Path(restored.retained_inputs[0].retained_path).read_text() == "value\nadmitted\n"
    Path(restored.retained_inputs[0].retained_path).unlink()
    with pytest.raises(ExecutionEnvelopeRefused) as caught:
        restore_execution_envelope(
            envelope.to_json(),
            current_snapshot=snapshot(),
            user_id="user",
            auth_provider_type="oidc",
            resolver=None,
            implementation_fingerprint="d" * 64,
            deployment_generation="image-1",
        )
    assert caught.value.reason is EnvelopeRecoveryReason.SOURCE_UNAVAILABLE


@pytest.mark.parametrize(
    "changed_field", ["runtime_fingerprint", "session_epoch", "landscape_epoch", "coordination_protocol", "canonical_input_digest"]
)
def test_persisted_runtime_and_schema_must_match_current_process(changed_field: str) -> None:
    frozen = FrozenRunSettings(snapshot(), {}, {})
    envelope = capture_execution_envelope(
        frozen,
        user_id="user",
        auth_provider_type="oidc",
        resolver=None,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    original = build_run_execution_input(envelope, frozen, "d" * 64, "image-1", ())
    validate_run_execution_input(original)
    changed = {
        "runtime_fingerprint": replace(original, runtime_fingerprint="0" * 64),
        "session_epoch": replace(original, session_epoch=original.session_epoch + 1),
        "landscape_epoch": replace(original, landscape_epoch=original.landscape_epoch + 1),
        "coordination_protocol": replace(original, coordination_protocol=original.coordination_protocol + 1),
        "canonical_input_digest": replace(original, canonical_input_digest="0" * 64),
    }[changed_field]
    with pytest.raises(ExecutionEnvelopeRefused) as caught:
        validate_run_execution_input(changed)
    assert (
        caught.value.reason.value
        == {
            "runtime_fingerprint": "runtime_changed",
            "session_epoch": "schema_incompatible",
            "landscape_epoch": "schema_incompatible",
            "coordination_protocol": "coordination_protocol_incompatible",
            "canonical_input_digest": "invalid_execution_envelope",
        }[changed_field]
    )


@pytest.mark.parametrize("kind", ["blob_rows", "inline_content"])
def test_blob_manifest_requires_retained_bytes_and_current_custody(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "document.txt"
    source.write_bytes(b"admitted-document")
    retained = retain_source_file(source, root=tmp_path / "retained")
    blob_id = "ec376271-e53e-46eb-a308-c243002dfd80"
    if kind == "blob_rows":
        config = {
            "source": {
                "plugin": "blob_rows",
                "options": {
                    "blobs": [
                        {
                            "blob_id": blob_id,
                            "payload_ref": retained.content_hash,
                            "size_bytes": retained.size_bytes,
                            "filename": "document.txt",
                            "mime_type": "text/plain",
                        }
                    ]
                },
            }
        }
    else:
        config = {
            "transforms": [
                {
                    "name": "prompt",
                    "plugin": "llm",
                    "options": {
                        "prompt": {
                            "blob_ref": blob_id,
                            "mode": "inline_content",
                            "sha256": retained.content_hash,
                        }
                    },
                }
            ]
        }
    frozen = FrozenRunSettings(snapshot(), config, {})
    references = discover_execution_blob_inputs(frozen)
    assert len(references) == 1
    assert str(references[0].blob_id) == blob_id
    with pytest.raises(ExecutionEnvelopeRefused):
        capture_execution_envelope(
            frozen,
            user_id="user",
            auth_provider_type="oidc",
            resolver=None,
            env_ref_names=frozenset(),
            implementation_fingerprint="d" * 64,
            deployment_generation="image-1",
        )
    blob_inputs = (RetainedBlobInput(references[0], retained, retained.size_bytes, "document.txt", "text/plain"),)
    envelope = capture_execution_envelope(
        frozen,
        user_id="user",
        auth_provider_type="oidc",
        resolver=None,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
        blob_inputs=blob_inputs,
    )
    execution_input = build_run_execution_input(envelope, frozen, "d" * 64, "image-1", ())
    validate_run_execution_input(execution_input)
    source.unlink()
    verified: list[RetainedBlobInput] = []
    restored = restore_execution_envelope(
        envelope.to_json(),
        current_snapshot=snapshot(),
        user_id="user",
        auth_provider_type="oidc",
        resolver=None,
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
        blob_verifier=verified.append,
    )
    assert restored.blob_inputs == blob_inputs
    assert verified == list(blob_inputs)
    with pytest.raises(ExecutionEnvelopeRefused):
        restore_execution_envelope(
            envelope.to_json(),
            current_snapshot=snapshot(),
            user_id="user",
            auth_provider_type="oidc",
            resolver=None,
            implementation_fingerprint="d" * 64,
            deployment_generation="image-1",
        )


def test_combined_recovery_decodes_once_and_rechecks_later_attempts() -> None:
    from elspeth.web.execution.envelope import _EnvelopePayload

    store = SecretStore()
    frozen = FrozenRunSettings(snapshot(), {"api_key": {"secret_ref": "ACCESS"}}, {"labels": ["original"]})
    envelope = capture_execution_envelope(
        frozen,
        user_id="user",
        auth_provider_type="oidc",
        resolver=store,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    execution_input = build_run_execution_input(envelope, frozen, "d" * 64, "image-1", ())
    calls = []

    def current_authority() -> ExecutionRecoveryAuthority:
        calls.append("fresh")
        return ExecutionRecoveryAuthority(snapshot(), "d" * 64, "image-1")

    with patch.object(_EnvelopePayload, "model_validate_json", wraps=_EnvelopePayload.model_validate_json) as parse:
        restored = restore_run_execution_input(
            execution_input,
            current_authority=current_authority,
            user_id="user",
            auth_provider_type="oidc",
            resolver=store,
        )
    assert parse.call_count == 1
    assert restored.settings == frozen
    with pytest.raises(TypeError):
        restored.settings.audit_safe_config["labels"][0] = "changed"
    copy = deep_thaw(restored.settings.audit_safe_config)
    copy["labels"][0] = "changed"
    assert deep_thaw(restored.settings.audit_safe_config) == {"labels": ["original"]}

    # No decoded carrier or admitted-secret cache crosses recovery attempts.
    store.secret = ResolvedSecret("ACCESS", "rotated", "server", "f" * 64)
    with pytest.raises(ExecutionEnvelopeRefused) as caught:
        restore_run_execution_input(
            execution_input,
            current_authority=current_authority,
            user_id="user",
            auth_provider_type="oidc",
            resolver=store,
        )
    assert caught.value.reason is EnvelopeRecoveryReason.SECRET_VERSION_CHANGED
    assert calls == ["fresh", "fresh"]

    # Changed bytes and derived identities are refused before current authority.
    for corrupted in (
        replace(execution_input, envelope_json=envelope.to_json() + " "),
        replace(execution_input, configuration_fingerprint="0" * 64),
    ):
        with pytest.raises(ExecutionEnvelopeRefused) as caught:
            restore_run_execution_input(
                corrupted,
                current_authority=current_authority,
                user_id="user",
                auth_provider_type="oidc",
                resolver=store,
            )
        assert caught.value.reason is EnvelopeRecoveryReason.INVALID_ENVELOPE
    assert calls == ["fresh", "fresh"]


def test_combined_recovery_rechecks_retained_file_after_current_authority_lookup(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_text("value\nadmitted\n")
    original = FrozenRunSettings(snapshot(), {"source": {"plugin": "csv", "options": {"path": str(source)}}}, {})
    frozen, retained = retain_execution_inputs(original, root=tmp_path / "retained")
    envelope = capture_execution_envelope(
        frozen,
        user_id="user",
        auth_provider_type="oidc",
        resolver=None,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
        retained_inputs=retained,
    )
    execution_input = build_run_execution_input(envelope, frozen, "d" * 64, "image-1", retained)

    def current_authority() -> ExecutionRecoveryAuthority:
        # A valid admission check is not permission to skip a later custody
        # check: the file can disappear while resolving current authority.
        Path(retained[0].retained_path).unlink()
        return ExecutionRecoveryAuthority(snapshot(), "d" * 64, "image-1")

    with pytest.raises(ExecutionEnvelopeRefused) as caught:
        restore_run_execution_input(
            execution_input,
            current_authority=current_authority,
            user_id="user",
            auth_provider_type="oidc",
            resolver=None,
        )
    assert caught.value.reason is EnvelopeRecoveryReason.SOURCE_UNAVAILABLE


def test_implementation_fingerprint_rechecks_source_registry_and_dependencies(tmp_path, monkeypatch) -> None:
    import hashlib
    import os
    from types import SimpleNamespace

    from elspeth.web.execution import envelope as module

    package = tmp_path / "elspeth"
    origin = package / "web" / "execution" / "envelope.py"
    origin.parent.mkdir(parents=True)
    origin.write_text("# fixture origin\n")
    source = package / "plugin.py"
    source.write_text("VALUE = 1\n")
    monkeypatch.setattr(module, "__file__", str(origin))
    dependencies = [SimpleNamespace(metadata={"Name": "example"}, version="1")]
    monkeypatch.setattr(module, "distributions", lambda: dependencies)

    def reference(current):
        digest = hashlib.sha256()
        for path in sorted(path for path in (origin, source) if path.is_file()):
            digest.update(path.relative_to(package).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(json.dumps(sorted((item.metadata["Name"], item.version) for item in dependencies), separators=(",", ":")).encode())
        digest.update(json.dumps(sorted(map(str, current.available)), separators=(",", ":")).encode())
        return digest.hexdigest()

    current = snapshot()
    original = module.runtime_implementation_fingerprint(current)
    assert original == reference(current)
    assert module.runtime_implementation_fingerprint(current) == original
    prior_stat = source.stat()
    source.write_text("VALUE = 2\n")
    os.utime(source, ns=(prior_stat.st_atime_ns, prior_stat.st_mtime_ns))
    changed = module.runtime_implementation_fingerprint(current)
    assert changed == reference(current) and changed != original
    dependencies[0].version = "2"
    changed_dependencies = module.runtime_implementation_fingerprint(current)
    assert changed_dependencies == reference(current) and changed_dependencies != changed
    changed_registry = replace(current, available=frozenset())
    assert module.runtime_implementation_fingerprint(changed_registry) == reference(changed_registry)
    assert module.runtime_implementation_fingerprint(changed_registry) != changed_dependencies
    source.unlink()
    assert module.runtime_implementation_fingerprint(current) == reference(current)
    assert module.runtime_implementation_fingerprint(current) != changed_dependencies
