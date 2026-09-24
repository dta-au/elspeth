"""Independent vectors for the closed, compartment-marked audit-export boundary."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime
from enum import StrEnum
from io import BytesIO

import pytest

from elspeth.contracts.audit_export import (
    AUDIT_EXPORT_DERIVATION_VERSION,
    REF,
    AuditExportContentDescriptor,
    AuditExportContentStoreResolver,
    AuditExportDerivationConfig,
    C,
    H,
    derive_audit_export_bundle,
    derive_audit_export_bundle_to_spool,
    derive_public_export_config_hash,
    derive_registry_key_hash,
    final_manifest_identity_payload,
    hash_final_manifest_identity_payload,
)
from elspeth.contracts.sink_effects import AuditExportSignedManifestInput, AuditExportSigningMode

PUBLIC_CONFIG = {
    "auth_events": "omitted",
    "chunking_algorithm_version": "record-framing-v1",
    "compartment_id": "test-compartment",
    "export_format": "json",
    "exporter_version": "landscape-exporter-auth-v2",
    "include_raw_error_rows": False,
    "per_chunk_byte_limit": 1_048_576,
    "per_chunk_record_limit": 1_000,
    "serialization_version": "audit-export-v3",
    "signer_key_id": "UNSIGNED",
    "signing_mode": "unsigned",
}
PUBLIC_CONFIG_BYTES = (
    b'{"payload":{"auth_events":"omitted","chunking_algorithm_version":"record-framing-v1",'
    b'"compartment_id":"test-compartment","export_format":"json",'
    b'"exporter_version":"landscape-exporter-auth-v2","include_raw_error_rows":false,'
    b'"per_chunk_byte_limit":1048576,"per_chunk_record_limit":1000,'
    b'"serialization_version":"audit-export-v3","signer_key_id":"UNSIGNED",'
    b'"signing_mode":"unsigned"},"schema":"audit-export-public-config-v1"}'
)
PUBLIC_CONFIG_HASH = "fb8052aa1a33d59fd2f50c3233c252d1d15071dced5f5519997b7e040671efd2"

REGISTRY_KEY = {
    "export_format": "json",
    "exporter_version": "landscape-exporter-auth-v2",
    "public_export_config_hash": PUBLIC_CONFIG_HASH,
    "serialization_version": "audit-export-v3",
    "signer_key_id": "UNSIGNED",
    "signing_mode": "unsigned",
    "source_run_id": "run-golden-001",
}
REGISTRY_KEY_BYTES = (
    b'{"payload":{"export_format":"json","exporter_version":"landscape-exporter-auth-v2",'
    b'"public_export_config_hash":"fb8052aa1a33d59fd2f50c3233c252d1d15071dced5f5519997b7e040671efd2",'
    b'"serialization_version":"audit-export-v3","signer_key_id":"UNSIGNED",'
    b'"signing_mode":"unsigned","source_run_id":"run-golden-001"},'
    b'"schema":"audit-export-registry-key-v1"}'
)
REGISTRY_KEY_HASH = "09bb88137b7ea1171876a72481702b6c92c18737363a959637397a31a1a6814f"


def _golden_config(
    *,
    signing_mode: str = "unsigned",
    signer_key_id: str = "UNSIGNED",
    signing_key: bytes | None = None,
    auth_events: str = "omitted",
    per_chunk_record_limit: int = 1_000,
) -> AuditExportDerivationConfig:
    return AuditExportDerivationConfig(
        source_run_id="run-golden-001",
        source_status="completed",
        source_completed_at="2026-07-16T12:00:00.000001Z",
        export_format="json",
        exporter_version="landscape-exporter-auth-v2",
        serialization_version="audit-export-v3",
        chunking_algorithm_version="record-framing-v1",
        include_raw_error_rows=False,
        per_chunk_byte_limit=1_048_576,
        per_chunk_record_limit=per_chunk_record_limit,
        signing_mode=signing_mode,
        signer_key_id=signer_key_id,
        signing_key=signing_key,
        auth_events=auth_events,
        compartment_id="test-compartment",
    )


def _golden_record() -> dict[str, object]:
    return {
        "completed_at": "2026-07-16T12:00:00.000001Z",
        "record_type": "run",
        "run_id": "run-golden-001",
        "status": "completed",
    }


def _coverage(config: AuditExportDerivationConfig) -> dict[str, object]:
    if config.auth_events == "deployment_snapshot":
        return {
            "record_type": "auth_event_coverage",
            "policy": "deployment_snapshot",
            "selection_cutoff": config.source_completed_at,
            "selected_count": 1,
            "reason": "deployment_snapshot",
            "selection_basis": "visible_rows_at_or_before_run_completion",
        }
    return {
        "record_type": "auth_event_coverage",
        "policy": "omitted",
        "selection_cutoff": None,
        "selected_count": None,
        "reason": "not_requested",
        "selection_basis": None,
    }


def _records(config: AuditExportDerivationConfig) -> list[dict[str, object]]:
    return [
        _golden_record(),
        {"record_type": "audit_export_config", "public_config": config.public_snapshot_config()},
        _coverage(config),
    ]


def test_literal_public_config_and_registry_key_vectors() -> None:
    assert AUDIT_EXPORT_DERIVATION_VERSION == "audit-export-derivation-v1"
    assert C("audit-export-public-config-v1", PUBLIC_CONFIG) == PUBLIC_CONFIG_BYTES
    assert H(PUBLIC_CONFIG_BYTES) == PUBLIC_CONFIG_HASH
    assert REF(PUBLIC_CONFIG_HASH) == f"sha256:{PUBLIC_CONFIG_HASH}"
    assert derive_public_export_config_hash(PUBLIC_CONFIG) == PUBLIC_CONFIG_HASH
    assert C("audit-export-registry-key-v1", REGISTRY_KEY) == REGISTRY_KEY_BYTES
    assert H(REGISTRY_KEY_BYTES) == REGISTRY_KEY_HASH
    assert derive_registry_key_hash(REGISTRY_KEY) == REGISTRY_KEY_HASH


@pytest.mark.parametrize("compartment_id", ["a", "0", "research-a", "a" * 63])
def test_compartment_identifier_is_accepted_at_derivation_boundaries(compartment_id: str) -> None:
    config = replace(_golden_config(), compartment_id=compartment_id)
    assert derive_public_export_config_hash(config.public_snapshot_config())


@pytest.mark.parametrize(
    "compartment_id", [None, "", " ", "Research-A", "-research", "research_a", "research a", "research\n", "é", "a" * 64]
)
@pytest.mark.parametrize("signed", [False, True])
def test_compartment_identifier_is_required_and_validated_for_both_signing_modes(compartment_id: str | None, signed: bool) -> None:
    config = _golden_config(signing_mode="hmac_sha256", signer_key_id="test-key", signing_key=b"key") if signed else _golden_config()
    with pytest.raises(ValueError, match="compartment_id"):
        replace(config, compartment_id=compartment_id)
    with pytest.raises(ValueError, match="compartment_id"):
        derive_public_export_config_hash({**config.public_snapshot_config(), "compartment_id": compartment_id})


@pytest.mark.parametrize("version", ["landscape-exporter-v1", "landscape-exporter-auth-v1"])
@pytest.mark.parametrize("signed", [False, True])
def test_legacy_exporter_versions_are_not_derivable_or_hashable(version: str, signed: bool) -> None:
    config = _golden_config(signing_mode="hmac_sha256", signer_key_id="test-key", signing_key=b"key") if signed else _golden_config()
    with pytest.raises(ValueError, match="exporter_version"):
        replace(config, exporter_version=version)
    with pytest.raises(ValueError, match="exporter_version"):
        derive_public_export_config_hash({**config.public_snapshot_config(), "exporter_version": version})
    with pytest.raises(ValueError, match="exporter_version"):
        derive_registry_key_hash({**REGISTRY_KEY, "exporter_version": version})


@pytest.mark.parametrize("spooled", [False, True])
@pytest.mark.parametrize("signed", [False, True])
def test_config_record_rejects_legacy_version_before_coverage(spooled: bool, signed: bool) -> None:
    config = _golden_config(signing_mode="hmac_sha256", signer_key_id="test-key", signing_key=b"key") if signed else _golden_config()
    records = _records(config)
    records[1] = {
        "record_type": "audit_export_config",
        "public_config": {**config.public_snapshot_config(), "exporter_version": "landscape-exporter-auth-v1"},
    }

    with pytest.raises(ValueError, match="exporter_version"):
        if spooled:
            derive_audit_export_bundle_to_spool(records, config, BytesIO(), max_total_records=3, max_total_bytes=100_000, max_chunks=3)
        else:
            derive_audit_export_bundle(records, config)


@pytest.mark.parametrize("spooled", [False, True])
@pytest.mark.parametrize("signed", [False, True])
def test_auth_event_coverage_count_and_policy_are_enforced(spooled: bool, signed: bool) -> None:
    config = (
        _golden_config(signing_mode="hmac_sha256", signer_key_id="test-key", signing_key=b"key", auth_events="deployment_snapshot")
        if signed
        else _golden_config(auth_events="deployment_snapshot")
    )
    event = {"record_type": "auth_event", "occurred_at": config.source_completed_at}
    records = [*_records(config)[:-1], event, _coverage(config)]

    def derive(items: list[dict[str, object]]) -> object:
        if spooled:
            return derive_audit_export_bundle_to_spool(
                items, config, BytesIO(), max_total_records=100, max_total_bytes=100_000, max_chunks=10
            )
        return derive_audit_export_bundle(items, config)

    assert derive(records)
    for invalid in [
        records[:-1],
        [*records, _coverage(config)],
        [*records[:-1], {**_coverage(config), "selected_count": 0}],
        [*records, records[1]],
        [records[0], *records[2:]],
    ]:
        with pytest.raises(ValueError, match="coverage"):
            derive(invalid)


@pytest.mark.parametrize("spooled", [False, True])
def test_omission_disclosure_does_not_claim_no_events_exist(spooled: bool) -> None:
    config = _golden_config()

    def derive(items: list[dict[str, object]]) -> object:
        if spooled:
            return derive_audit_export_bundle_to_spool(
                items, config, BytesIO(), max_total_records=100, max_total_bytes=100_000, max_chunks=10
            )
        return derive_audit_export_bundle(items, config)

    assert derive(_records(config))
    with pytest.raises(ValueError, match="coverage"):
        derive([*_records(config)[:-1], {**_coverage(config), "selected_count": 0}])
    with pytest.raises(ValueError, match="coverage"):
        derive([*_records(config), {"record_type": "auth_event", "occurred_at": config.source_completed_at}])


def test_auth_events_policy_and_compartment_change_public_identity() -> None:
    omitted = _golden_config().public_snapshot_config()
    assert derive_public_export_config_hash(omitted) != derive_public_export_config_hash({**omitted, "auth_events": "deployment_snapshot"})
    assert derive_public_export_config_hash(omitted) != derive_public_export_config_hash({**omitted, "compartment_id": "research-b"})
    with pytest.raises(ValueError, match="auth_events"):
        derive_public_export_config_hash({**omitted, "auth_events": "all"})


@pytest.mark.parametrize(
    "payload",
    [
        {**PUBLIC_CONFIG, "unknown": None},
        {key: value for key, value in PUBLIC_CONFIG.items() if key != "export_format"},
        {**PUBLIC_CONFIG, "per_chunk_record_limit": 1.0},
        {**PUBLIC_CONFIG, "per_chunk_record_limit": 9_007_199_254_740_992},
        {**PUBLIC_CONFIG, "export_format": ("json",)},
        {**PUBLIC_CONFIG, "signing_mode": {"unsigned"}},
        {**PUBLIC_CONFIG, 1: "non-string-key"},
        {**PUBLIC_CONFIG, "export_format": b"json"},
        {**PUBLIC_CONFIG, "exporter_version": datetime(2026, 7, 16, tzinfo=UTC)},
    ],
)
def test_closed_public_config_rejects_non_schema_values(payload: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        C("audit-export-public-config-v1", payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("public_export_config_hash", "A" * 64),
        ("public_export_config_hash", "0" * 63),
        ("signing_mode", "HMAC_SHA256"),
        ("export_format", "xml"),
    ],
)
def test_registry_key_rejects_noncanonical_identity_fields(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        C("audit-export-registry-key-v1", {**REGISTRY_KEY, field: value})


def test_c_rejects_unknown_tags_and_h_never_canonicalizes() -> None:
    with pytest.raises(ValueError, match="unknown audit-export schema tag"):
        C("audit-export-not-real-v1", {})
    with pytest.raises(TypeError, match="bytes"):
        H(PUBLIC_CONFIG)


class _Format(StrEnum):
    JSON = "json"


def test_c_rejects_implicit_enum_conversion() -> None:
    with pytest.raises(TypeError, match="enum"):
        C("audit-export-public-config-v1", {**PUBLIC_CONFIG, "export_format": _Format.JSON})


def test_content_descriptor_requires_exact_sha256_reference() -> None:
    descriptor = AuditExportContentDescriptor(
        content_ref=f"sha256:{'a' * 64}", content_hash="a" * 64, size_bytes=7, object_kind="data_chunk"
    )
    assert descriptor.content_ref == f"sha256:{'a' * 64}"
    with pytest.raises(ValueError, match="must equal"):
        AuditExportContentDescriptor(content_ref=f"sha256:{'b' * 64}", content_hash="a" * 64, size_bytes=7, object_kind="data_chunk")


class _Store:
    content_store_id = "archive-primary-v1"
    namespace = "audit-export"

    def is_durable(self) -> bool:
        return True

    def put_immutable(self, content: bytes, *, candidate_id: str, object_kind: str) -> str:
        del content, candidate_id, object_kind
        return f"sha256:{'a' * 64}"

    def open_registered(self, registration: object) -> object:
        return registration

    def mark_candidate_orphans(self, candidate_id: str, descriptors: tuple[object, ...]) -> None:
        del candidate_id, descriptors


def test_store_resolver_keeps_content_store_id_stable_and_rejects_reinterpretation() -> None:
    resolver = AuditExportContentStoreResolver()
    store = _Store()
    resolver.register(store)
    assert resolver.resolve("archive-primary-v1") is store
    resolver.register(store)
    with pytest.raises(ValueError, match="already registered"):
        resolver.register(_Store())
    with pytest.raises(LookupError, match="unresolvable"):
        resolver.resolve("retired-store")


def _manifest_descriptor() -> AuditExportSignedManifestInput:
    return AuditExportSignedManifestInput(
        content_ref=f"sha256:{'a' * 64}",
        content_hash="a" * 64,
        size_bytes=512,
        manifest_schema="elspeth.audit-export-manifest.v2",
        derivation_version="audit-export-derivation-v1",
        signature_algorithm=AuditExportSigningMode.UNSIGNED,
        signature_key_id="UNSIGNED",
        record_chain_algorithm="sha256_concat_record_sha256_v1",
        final_hash="b" * 64,
        signature=None,
    )


@pytest.mark.parametrize(
    "field",
    [
        "content_hash",
        "content_ref",
        "size_bytes",
        "derivation_version",
        "manifest_schema",
        "signature_algorithm",
        "signature_key_id",
        "record_chain_algorithm",
        "final_hash",
        "signature",
    ],
)
def test_final_manifest_identity_component_binds_every_serialized_field(field: str) -> None:
    baseline = final_manifest_identity_payload(_manifest_descriptor())
    changed = dict(baseline)
    replacements: dict[str, object] = {
        "content_hash": "c" * 64,
        "content_ref": f"sha256:{'c' * 64}",
        "size_bytes": 513,
        "derivation_version": "alternate-version",
        "manifest_schema": "alternate-schema",
        "signature_algorithm": "hmac_sha256",
        "signature_key_id": "operator-key-2",
        "record_chain_algorithm": "sha256_concat_hmac_sha256_signatures_v1",
        "final_hash": "d" * 64,
        "signature": "e" * 64,
    }
    changed[field] = replacements[field]
    assert hash_final_manifest_identity_payload(changed, validate=False) != hash_final_manifest_identity_payload(baseline, validate=False)


def test_literal_final_manifest_identity_vector() -> None:
    payload = final_manifest_identity_payload(_manifest_descriptor())
    assert H(C("sink-effect-audit-export-final-manifest-v1", payload)) == "e2df9077ce2b991cbc0c682fdfdb86e8aa353685a336e4bfcfa5da92bfb732a2"


@pytest.mark.parametrize(
    ("signed", "expected"),
    [
        (
            False,
            (
                PUBLIC_CONFIG_HASH,
                REGISTRY_KEY_HASH,
                "d97933b0b30320fcb9184d0a0ba2952c6913fea5654cc2bacf743adaaae5b936",
                "eb33931eccd6a2e4071d7625a723af2340b84918fd5ac0b5a4324a1fb13bfb60",
                "24635ed83754783470156de43a3e47915c7e6d5ee57a92511ca5c29ac857fefc",
                "712b8332184b97a960878ac6b65ed2ebecbeecd03c1cdd9b2320d3064eca2c46",
                "1a1752560f1e044ce182cdfdd6ce9e2ea3c482e386e5c125d209e83a97ac64b0",
            ),
        ),
        (
            True,
            (
                "a822f1271e5f8dede0ca711139722630ce1c9d4ea6c9107b561ab5c1f070bb3b",
                "369e0c2b3c42e03f21dfb8e4e0876d1a0ad143f868368edf652cb7bda75df5a2",
                "c20ed3da48648143b084bd89a859f3f5635226ebc400a7e47f8a0cf0e262e52b",
                "e8a6afff5ae5752a155913373c7baed03ad45dc5547e3c2645c3f833505e2631",
                "d64514326a491e71447f576cb82a2c51c879e792702da43bf553fdaf869bd04f",
                "6ff73ae1fe60ed0c39968e69ff39302fc6f2fdaaad3bf3992a4c21e397348c10",
                "df0faddc00d3bd47d639429311e52191720d5395daf3299bb030b83b6ee224c9",
            ),
        ),
    ],
)
def test_serialization_v3_end_to_end_literal_derivation_vector(signed: bool, expected: tuple[str, ...]) -> None:
    config = (
        _golden_config(signing_mode="hmac_sha256", signer_key_id="operator-key-v1", signing_key=b"golden-key")
        if signed
        else _golden_config()
    )
    bundle = derive_audit_export_bundle(_records(config), config)
    assert (
        bundle.public_export_config_hash,
        bundle.registry_key_hash,
        bundle.snapshot_hash,
        bundle.snapshot_id,
        bundle.manifest_hash,
        bundle.snapshot_seal_hash,
        bundle.final_hash,
    ) == expected
    assert bundle.public_export_config_hash == hashlib.sha256(bundle.public_export_config_bytes).hexdigest()
    assert bundle.registry_key_hash == hashlib.sha256(bundle.registry_key_bytes).hexdigest()
    assert len(bundle.record_frames) == 3
    assert len(bundle.chunks) == 1
    assert bundle.signed_manifest.content_hash == hashlib.sha256(bundle.signed_manifest_bytes).hexdigest()
    assert bundle.json_target_bytes == b"".join(bundle.chunk_bytes) + bundle.signed_manifest_bytes
    assert b'"compartment_id":"test-compartment"' in bundle.public_export_config_bytes
    assert b'"record_type":"audit_export_config"' in bundle.record_frames[1]
    if signed:
        assert bundle.final_manifest["signature"] == hmac.new(b"golden-key", bundle.signing_body, hashlib.sha256).hexdigest()
    else:
        assert bundle.public_export_config_bytes == PUBLIC_CONFIG_BYTES
        assert bundle.registry_key_bytes == REGISTRY_KEY_BYTES
        assert bundle.final_manifest["signature"] is None


@pytest.mark.parametrize("signed", [False, True])
def test_spooled_derivation_writes_completed_chunks_before_consuming_all_records(signed: bool) -> None:
    spool = BytesIO()
    config = (
        _golden_config(signing_mode="hmac_sha256", signer_key_id="audit-key-v1", signing_key=b"golden-test-key", per_chunk_record_limit=1)
        if signed
        else _golden_config(per_chunk_record_limit=1)
    )
    records = _records(config)

    def early_records():
        yield records[0]
        assert spool.tell() > 0
        yield from records[1:]

    spooled = derive_audit_export_bundle_to_spool(early_records(), config, spool, max_total_records=3, max_total_bytes=4096, max_chunks=3)
    materialized = derive_audit_export_bundle(records, config)
    assert spooled.snapshot_id == materialized.snapshot_id
    assert spooled.snapshot_hash == materialized.snapshot_hash
    assert spooled.manifest_hash == materialized.manifest_hash
    assert spooled.snapshot_seal_hash == materialized.snapshot_seal_hash
    assert spooled.final_hash == materialized.final_hash
    assert spooled.signing_body == materialized.signing_body
    assert spooled.signed_manifest_bytes == materialized.signed_manifest_bytes
    assert tuple(spool.getvalue()[offset : offset + size] for offset, size in spooled.chunk_offsets) == materialized.chunk_bytes
    assert "record_frames" not in type(spooled).__dataclass_fields__
