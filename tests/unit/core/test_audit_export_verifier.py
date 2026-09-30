"""Public delivered-artifact verification for audit exports."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Literal

import pytest

from elspeth.contracts.audit_export import (
    AUDIT_EXPORT_MAX_CHUNK_BYTES,
    AUDIT_EXPORT_MAX_CHUNK_RECORDS,
    AUDIT_EXPORT_MAX_DELIVERED_FILES,
    AuditExportDerivationConfig,
    derive_audit_export_bundle,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, canonical_json_loads
from elspeth.core import audit_export_verifier as verifier_module
from elspeth.core.audit_export_verifier import (
    AuditExportVerificationError,
    EnvironmentAuditExportVerificationKeyResolver,
    MappingAuditExportVerificationKeyResolver,
    verify_audit_export,
)

COMPLETED_AT = "2026-09-30T02:03:04.000005Z"
HOSTILE_BOUNDED_JSON = (
    pytest.param(
        b'{"secret":' + (b"[" * 1500) + b'"deep-secret-marker"' + (b"]" * 1500) + b"}",
        "deep-secret-marker",
        id="excessive-nesting",
    ),
    pytest.param(
        b'{"secret":"surrogate-secret-marker","value":"\\ud800"}',
        "surrogate-secret-marker",
        id="lone-surrogate",
    ),
)


def _config(
    *,
    signed: bool,
    export_format: Literal["json", "csv"] = "json",
    signer_key_id: str = "retained-key-2026-q2",
    signing_key: bytes = b"retained-secret",
) -> AuditExportDerivationConfig:
    return AuditExportDerivationConfig(
        source_run_id="run-verifier",
        source_status="completed",
        source_completed_at=COMPLETED_AT,
        export_format=export_format,
        exporter_version="landscape-exporter-auth-v2",
        serialization_version="audit-export-v3",
        chunking_algorithm_version="record-framing-v1",
        include_raw_error_rows=False,
        per_chunk_byte_limit=2048,
        per_chunk_record_limit=2,
        signing_mode="hmac_sha256" if signed else "unsigned",
        signer_key_id=signer_key_id if signed else "UNSIGNED",
        signing_key=signing_key if signed else None,
        compartment_id="test-compartment",
    )


def _target_bytes(
    *,
    signed: bool,
    export_format: Literal["json", "csv"] = "json",
    signer_key_id: str = "retained-key-2026-q2",
    signing_key: bytes = b"retained-secret",
) -> bytes:
    config = _config(
        signed=signed,
        export_format=export_format,
        signer_key_id=signer_key_id,
        signing_key=signing_key,
    )
    records = (
        {
            "completed_at": COMPLETED_AT,
            "record_type": "run",
            "run_id": config.source_run_id,
            "status": config.source_status,
        },
        {"record_type": "audit_export_config", "public_config": config.public_snapshot_config()},
        {
            "record_type": "auth_event_coverage",
            "policy": "omitted",
            "selection_cutoff": None,
            "selection_basis": None,
            "selected_count": None,
            "reason": "not_requested",
        },
    )
    return derive_audit_export_bundle(records, config).json_target_bytes


def _objects(content: bytes) -> list[dict[str, object]]:
    return [canonical_json_loads(line) for line in content.splitlines()]


def _render(objects: list[dict[str, object]]) -> bytes:
    return b"\n".join(canonical_json(item).encode("utf-8") for item in objects)


def _changed(value: object) -> object:
    if type(value) is str:
        return f"{value}-changed"
    if type(value) is int:
        return value + 1
    if type(value) is bool:
        return not value
    if value is None:
        return "changed"
    raise AssertionError(f"manifest test needs a mutator for {type(value).__name__}")


def test_signed_json_verification_resolves_the_manifest_historical_key(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    content = _target_bytes(signed=True)
    target.write_bytes(content)
    resolver = MappingAuditExportVerificationKeyResolver(
        {
            "active-key-2026-q3": b"new-secret",
            "retained-key-2026-q2": b"retained-secret",
        }
    )

    report = verify_audit_export(target, key_resolver=resolver)

    assert report.format == "json"
    assert report.authenticated is True
    assert report.signer_key_id == "retained-key-2026-q2"
    assert report.record_count == 3
    assert report.chunk_count == 2
    assert report.artifact_digest == hashlib.sha256(content).hexdigest()


def test_json_verification_is_bound_to_its_private_snapshot_when_the_source_changes_after_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "audit.jsonl"
    content = _target_bytes(signed=True)
    target.write_bytes(content)
    resolver = MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"retained-secret"})
    original_inspect_export = verifier_module._inspect_export
    changed = False

    def replace_source_after_inspection(source):
        nonlocal changed
        inspected = original_inspect_export(source)
        if not changed:
            target.write_bytes(b"source-path-changed-after-private-capture")
            changed = True
        return inspected

    monkeypatch.setattr(verifier_module, "_inspect_export", replace_source_after_inspection)

    report = verify_audit_export(target, key_resolver=resolver)

    assert changed
    assert report.artifact_digest == hashlib.sha256(content).hexdigest()
    assert hashlib.sha256(target.read_bytes()).hexdigest() != report.artifact_digest


def test_environment_resolver_uses_only_the_exact_historical_signer_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACTIVE_AUDIT_KEY", "new-secret")
    monkeypatch.setenv("RETAINED_AUDIT_KEY", "retained-secret")
    resolver = EnvironmentAuditExportVerificationKeyResolver(
        {
            "active-key-2026-q3": "ACTIVE_AUDIT_KEY",
            "retained-key-2026-q2": "RETAINED_AUDIT_KEY",
        }
    )

    assert resolver.resolve("retained-key-2026-q2") == b"retained-secret"
    with pytest.raises(AuditExportVerificationError) as unknown:
        resolver.resolve("unknown-key")
    assert unknown.value.code == "unknown_signer"

    monkeypatch.delenv("RETAINED_AUDIT_KEY")
    with pytest.raises(AuditExportVerificationError) as unavailable:
        resolver.resolve("retained-key-2026-q2")
    assert unavailable.value.code == "verification_key_unavailable"


def test_verification_key_resolvers_detach_caller_owned_mappings(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = {"retained-key-2026-q2": b"retained-secret"}
    references = {"retained-key-2026-q2": "RETAINED_AUDIT_KEY"}
    direct = MappingAuditExportVerificationKeyResolver(keys)
    environment = EnvironmentAuditExportVerificationKeyResolver(references)
    keys["retained-key-2026-q2"] = b"changed-secret"
    references["retained-key-2026-q2"] = "CHANGED_AUDIT_KEY"
    monkeypatch.setenv("RETAINED_AUDIT_KEY", "retained-secret")
    monkeypatch.setenv("CHANGED_AUDIT_KEY", "changed-secret")

    assert direct.resolve("retained-key-2026-q2") == b"retained-secret"
    assert environment.resolve("retained-key-2026-q2") == b"retained-secret"


@pytest.mark.parametrize(
    "resolver",
    [
        MappingAuditExportVerificationKeyResolver({"active-key-2026-q3": b"new-secret"}),
        MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"wrong-secret"}),
    ],
)
def test_signed_json_verification_refuses_unknown_or_wrong_historical_key(
    tmp_path: Path,
    resolver: MappingAuditExportVerificationKeyResolver,
) -> None:
    target = tmp_path / "audit.jsonl"
    target.write_bytes(_target_bytes(signed=True))

    with pytest.raises(AuditIntegrityError):
        verify_audit_export(target, key_resolver=resolver)


def test_unsigned_json_is_refused_by_default_and_explicitly_reported_as_integrity_only(tmp_path: Path) -> None:
    target = tmp_path / "audit.jsonl"
    target.write_bytes(_target_bytes(signed=False))

    with pytest.raises(AuditExportVerificationError) as refused:
        verify_audit_export(target)
    assert refused.value.code == "unsigned_refused"

    report = verify_audit_export(target, require_authenticated=False)
    assert report.authenticated is False
    assert report.signer_key_id == "UNSIGNED"


def test_record_tamper_and_authenticated_container_format_mismatch_fail_closed(tmp_path: Path) -> None:
    resolver = MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"retained-secret"})
    target = tmp_path / "audit.jsonl"
    original = _target_bytes(signed=True)
    target.write_bytes(original.replace(b'"run-verifier"', b'"run-attacker"', 1))
    with pytest.raises(AuditIntegrityError):
        verify_audit_export(target, key_resolver=resolver)

    target.write_bytes(_target_bytes(signed=True, export_format="csv"))
    with pytest.raises(AuditExportVerificationError) as mismatch:
        verify_audit_export(target, key_resolver=resolver)
    assert mismatch.value.code == "unsupported_delivered_format"


def test_manifest_truncation_duplicate_and_size_limit_fail_before_success(tmp_path: Path) -> None:
    resolver = MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"retained-secret"})
    original = _target_bytes(signed=True)
    manifest_offset = original.rfind(b"\n") + 1
    manifest = original[manifest_offset:]
    target = tmp_path / "audit.jsonl"
    for invalid in (
        original[:-1],
        original[: manifest_offset - 1],
        original + b"x",
        original + b"\n" + manifest,
        original[:manifest_offset] + b'{"record_type":"manifest"}',
        original[:manifest_offset] + b"{" + b'"padding":"x",' + b'"z":"' + b"x" * (64 * 1024) + b'"}',
    ):
        target.write_bytes(invalid)
        with pytest.raises(AuditIntegrityError):
            verify_audit_export(target, key_resolver=resolver)


def test_every_manifest_field_is_bound_to_the_signed_derivation(tmp_path: Path) -> None:
    resolver = MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"retained-secret"})
    original = _target_bytes(signed=True)
    target = tmp_path / "audit.jsonl"
    target.write_bytes(original)
    assert verify_audit_export(target, key_resolver=resolver).authenticated is True

    original_objects = _objects(original)
    manifest = original_objects[-1]
    for field_name in manifest:
        mutated = [dict(item) for item in original_objects]
        mutated[-1][field_name] = _changed(manifest[field_name])
        target.write_bytes(_render(mutated))
        with pytest.raises(AuditIntegrityError):
            verify_audit_export(target, key_resolver=resolver)


def test_record_order_signatures_shape_and_chunking_are_bound(tmp_path: Path) -> None:
    resolver = MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"retained-secret"})
    objects = _objects(_target_bytes(signed=True))
    target = tmp_path / "audit.jsonl"

    changed_body = [dict(item) for item in objects]
    changed_body[0]["status"] = "failed"
    changed_signature = [dict(item) for item in objects]
    changed_signature[0]["signature"] = "0" * 64
    missing_signature = [dict(item) for item in objects]
    del missing_signature[0]["signature"]
    unknown_field = [dict(item) for item in objects]
    unknown_field[0]["unexpected"] = "value"
    missing_field = [dict(item) for item in objects]
    del missing_field[0]["status"]
    changed_chunking = [dict(item) for item in objects]
    public_config = dict(changed_chunking[1]["public_config"])
    public_config["per_chunk_record_limit"] = 1
    changed_chunking[1]["public_config"] = public_config

    mutations = (
        changed_body,
        changed_signature,
        missing_signature,
        unknown_field,
        missing_field,
        changed_chunking,
        [objects[0], objects[0], *objects[1:]],
        [objects[1], objects[0], objects[2], objects[3]],
        [objects[0], objects[2], objects[1], objects[3]],
        [objects[0], objects[2], objects[3]],
    )
    for mutation in mutations:
        target.write_bytes(_render(mutation))
        with pytest.raises(AuditIntegrityError):
            verify_audit_export(target, key_resolver=resolver)


def test_signed_unsigned_and_historical_signer_mixtures_fail_closed(tmp_path: Path) -> None:
    retained = _objects(_target_bytes(signed=True))
    unsigned = _objects(_target_bytes(signed=False))
    rotated = _objects(
        _target_bytes(
            signed=True,
            signer_key_id="active-key-2026-q3",
            signing_key=b"active-secret",
        )
    )
    resolver = MappingAuditExportVerificationKeyResolver(
        {
            "active-key-2026-q3": b"active-secret",
            "retained-key-2026-q2": b"retained-secret",
        }
    )
    target = tmp_path / "audit.jsonl"

    signed_records_unsigned_manifest = [*retained[:-1], unsigned[-1]]
    target.write_bytes(_render(signed_records_unsigned_manifest))
    with pytest.raises(AuditIntegrityError):
        verify_audit_export(target, key_resolver=resolver, require_authenticated=False)

    unsigned_records_signed_manifest = [*unsigned[:-1], retained[-1]]
    target.write_bytes(_render(unsigned_records_signed_manifest))
    with pytest.raises(AuditIntegrityError):
        verify_audit_export(target, key_resolver=resolver)

    mixed_signer_config = [dict(item) for item in retained]
    mixed_signer_config[1] = rotated[1]
    target.write_bytes(_render(mixed_signer_config))
    with pytest.raises(AuditIntegrityError):
        verify_audit_export(target, key_resolver=resolver)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("per_chunk_byte_limit", AUDIT_EXPORT_MAX_CHUNK_BYTES + 1),
        ("per_chunk_record_limit", AUDIT_EXPORT_MAX_CHUNK_RECORDS + 1),
        ("per_chunk_byte_limit", 1),
        ("per_chunk_record_limit", 1),
    ],
)
def test_declared_chunk_limits_cannot_widen_or_repartition_the_delivered_stream(
    tmp_path: Path,
    field_name: str,
    value: int,
) -> None:
    resolver = MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"retained-secret"})
    objects = _objects(_target_bytes(signed=True))
    config_record = dict(objects[1])
    public_config = dict(config_record["public_config"])
    public_config[field_name] = value
    config_record["public_config"] = public_config
    objects[1] = config_record
    target = tmp_path / "audit.jsonl"
    target.write_bytes(_render(objects))

    with pytest.raises(AuditIntegrityError):
        verify_audit_export(target, key_resolver=resolver)


def test_noncanonical_and_malformed_record_frames_fail_closed(tmp_path: Path) -> None:
    resolver = MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"retained-secret"})
    original = _target_bytes(signed=True)
    first, remainder = original.split(b"\n", 1)
    target = tmp_path / "audit.jsonl"
    invalid_streams = (
        b"\n" + original,
        first + remainder,
        b" " + first + b"\n" + remainder,
        b'{"record_type":"run","record_type":"node"}\n' + remainder,
        b"\xff\n" + remainder,
        b"\xef\xbb\xbf" + first + b"\n" + remainder,
        b'{"record_type":"run","value":1.5}\n' + remainder,
        b'{"record_type":"run"}\n' + remainder,
    )
    for invalid in invalid_streams:
        target.write_bytes(invalid)
        with pytest.raises(AuditIntegrityError):
            verify_audit_export(target, key_resolver=resolver)


@pytest.mark.parametrize(("content", "secret_marker"), HOSTILE_BOUNDED_JSON)
def test_bounded_parser_failures_are_normalized_without_echoing_input(
    tmp_path: Path,
    content: bytes,
    secret_marker: str,
) -> None:
    target = tmp_path / "hostile.jsonl"
    target.write_bytes(content)

    with pytest.raises(AuditIntegrityError) as failure:
        verify_audit_export(target, require_authenticated=False)

    assert type(failure.value) is AuditIntegrityError
    assert str(failure.value) == "audit export final manifest is not valid canonical JSON"
    assert secret_marker not in str(failure.value)


def test_physical_container_must_match_authenticated_format(tmp_path: Path) -> None:
    resolver = MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"retained-secret"})
    json_in_directory = tmp_path / "json-directory"
    json_in_directory.mkdir()
    (json_in_directory / "audit_records.v3.jsonl").write_bytes(_target_bytes(signed=True))
    with pytest.raises(AuditExportVerificationError) as directory_mismatch:
        verify_audit_export(json_in_directory, key_resolver=resolver)
    assert directory_mismatch.value.code == "unsupported_delivered_format"

    unrelated_json = tmp_path / "unrelated.json"
    unrelated_json.write_bytes(b'{"record_type":"manifest"}')
    with pytest.raises(AuditIntegrityError):
        verify_audit_export(unrelated_json, key_resolver=resolver)

    unrelated_directory = tmp_path / "unrelated-directory"
    unrelated_directory.mkdir()
    (unrelated_directory / "audit_records.v3.jsonl").write_bytes(b'{"record_type":"manifest"}')
    with pytest.raises(AuditIntegrityError):
        verify_audit_export(unrelated_directory, key_resolver=resolver)


@pytest.mark.parametrize(
    ("limit_name", "limit_value"),
    [
        ("AUDIT_EXPORT_MAX_TOTAL_BYTES", 32),
        ("AUDIT_EXPORT_MAX_TOTAL_RECORDS", 2),
        ("AUDIT_EXPORT_MAX_CHUNK_BYTES", 32),
        ("AUDIT_EXPORT_MAX_CHUNKS", 1),
    ],
)
def test_verifier_owned_stream_limits_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit_value: int,
) -> None:
    resolver = MappingAuditExportVerificationKeyResolver({"retained-key-2026-q2": b"retained-secret"})
    target = tmp_path / "audit.jsonl"
    target.write_bytes(_target_bytes(signed=True))
    assert verify_audit_export(target, key_resolver=resolver).authenticated is True

    monkeypatch.setattr(verifier_module, limit_name, limit_value)
    with pytest.raises(AuditIntegrityError):
        verify_audit_export(target, key_resolver=resolver)


def test_csv_directory_enumeration_stops_at_the_code_owned_file_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(AUDIT_EXPORT_MAX_DELIVERED_FILES + 100):
        (tmp_path / f"entry-{index:06d}").touch()

    original_scandir = os.scandir
    consumed = 0
    closed = False

    class CountingScandir:
        def __init__(self, path: Path) -> None:
            self._entries = original_scandir(path)

        def __enter__(self):
            self._entries.__enter__()
            return self

        def __exit__(self, *exception: object) -> None:
            nonlocal closed
            closed = True
            self._entries.__exit__(*exception)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            consumed += 1
            return next(self._entries)

    monkeypatch.setattr(os, "scandir", CountingScandir)

    with pytest.raises(AuditIntegrityError, match="file count"):
        verifier_module._directory_names(tmp_path)

    assert consumed == AUDIT_EXPORT_MAX_DELIVERED_FILES + 1
    assert closed is True
