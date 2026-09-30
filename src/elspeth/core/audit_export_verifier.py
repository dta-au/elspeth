"""Standalone verification for delivered JSON and portable CSV audit exports.

The verifier deliberately reuses the producer's closed derivation.  A
delivered export is accepted only when its canonical record frames rederive
the same chunk content hashes, chunk seals, snapshot identity, record chain,
and detached final manifest bytes.  Signed exports resolve the HMAC key by the
manifest's public ``signature_key_id`` so retained historical keys can verify
older evidence without making a current key an ambient default.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryFile
from typing import IO, BinaryIO, Final, Literal, Protocol, cast

from rfc8785 import CanonicalizationError

from elspeth.contracts.audit_export import (
    AUDIT_EXPORT_DELIVERED_MANIFEST_NAME,
    AUDIT_EXPORT_DERIVATION_VERSION,
    AUDIT_EXPORT_MANIFEST_SCHEMA,
    AUDIT_EXPORT_MAX_CHUNK_BYTES,
    AUDIT_EXPORT_MAX_CHUNKS,
    AUDIT_EXPORT_MAX_DELIVERED_BYTES,
    AUDIT_EXPORT_MAX_DELIVERED_FILES,
    AUDIT_EXPORT_MAX_TOTAL_BYTES,
    AUDIT_EXPORT_MAX_TOTAL_RECORDS,
    AUDIT_EXPORT_PORTABLE_RECORDS_NAME,
    MAX_AUDIT_EXPORT_SIGNED_MANIFEST_BYTES,
    AuditExportDerivationConfig,
    ClosedAuditExportJSON,
    audit_export_directory_bundle_hash,
    derive_audit_export_bundle_to_spool,
    validate_closed_stage_payload,
    validate_credential_free_identifier,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import freeze_fields
from elspeth.contracts.hashing import canonical_json, canonical_json_loads
from elspeth.core.landscape.formatters import CSVFormatter

_ENVIRONMENT_REFERENCE = re.compile(r"[A-Z_][A-Z0-9_]*\Z")
_RECORD_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_MAX_RECORD_TYPE_BYTES: Final = 192
_CSV_FORMULA_PREFIXES: Final = ("=", "+", "-", "@", "\t", "\r", "\n")
_MANIFEST_FIELDS: Final = frozenset(
    {
        "chunk_count",
        "derivation_version",
        "export_format",
        "exported_at",
        "final_hash",
        "hash_algorithm",
        "last_chunk_seal_hash",
        "manifest_hash",
        "record_chain_algorithm",
        "record_count",
        "record_type",
        "registry_key_hash",
        "run_id",
        "schema",
        "signature",
        "signature_algorithm",
        "signature_key_id",
        "snapshot_hash",
        "snapshot_id",
        "snapshot_seal_hash",
        "source_completed_at",
        "source_status",
        "total_bytes",
    }
)


class AuditExportVerificationError(AuditIntegrityError):
    """Expected refusal for malformed, unauthenticated, or changed evidence."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AuditExportVerificationKeyResolver(Protocol):
    """Resolve one retained verification key by its public signer identity."""

    def resolve(self, signer_key_id: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class MappingAuditExportVerificationKeyResolver:
    """Exact in-memory keyring suitable for API callers and tests."""

    keys: Mapping[str, bytes]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("audit-export verification keyring must not be empty")
        for signer_key_id, key in self.keys.items():
            validate_credential_free_identifier(signer_key_id, "signer_key_id")
            if type(key) is not bytes or not key:
                raise ValueError("audit-export verification keys must be non-empty exact bytes")
        freeze_fields(self, "keys")

    def resolve(self, signer_key_id: str) -> bytes:
        validate_credential_free_identifier(signer_key_id, "signer_key_id")
        try:
            key = self.keys[signer_key_id]
        except KeyError as exc:
            raise AuditExportVerificationError(
                "unknown_signer",
                f"no retained audit-export verification key for signer {signer_key_id!r}",
            ) from exc
        if type(key) is not bytes or not key:
            raise AuditIntegrityError("resolved audit-export verification key must be non-empty exact bytes")
        return key


@dataclass(frozen=True, slots=True)
class EnvironmentAuditExportVerificationKeyResolver:
    """Keyring whose values name environment variables rather than key bytes."""

    references: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.references:
            raise ValueError("audit-export verification key references must not be empty")
        for signer_key_id, reference in self.references.items():
            validate_credential_free_identifier(signer_key_id, "signer_key_id")
            if type(reference) is not str or _ENVIRONMENT_REFERENCE.fullmatch(reference) is None:
                raise ValueError("audit-export verification key references must be environment variable names")
        freeze_fields(self, "references")

    def resolve(self, signer_key_id: str) -> bytes:
        validate_credential_free_identifier(signer_key_id, "signer_key_id")
        try:
            reference = self.references[signer_key_id]
        except KeyError as exc:
            raise AuditExportVerificationError(
                "unknown_signer",
                f"no retained audit-export verification key for signer {signer_key_id!r}",
            ) from exc
        try:
            value = os.environ[reference]
        except KeyError as exc:
            raise AuditExportVerificationError(
                "verification_key_unavailable",
                f"verification key material is unavailable for signer {signer_key_id!r}",
            ) from exc
        if not value:
            raise AuditExportVerificationError(
                "verification_key_unavailable",
                f"verification key material is unavailable for signer {signer_key_id!r}",
            )
        return value.encode("utf-8")


def environment_audit_export_verification_key_resolver(
    references: dict[str, str],
) -> AuditExportVerificationKeyResolver:
    """Construct the core environment resolver across the CLI module seam."""
    return EnvironmentAuditExportVerificationKeyResolver(references)


@dataclass(frozen=True, slots=True)
class AuditExportVerificationReport:
    """Authenticated identity and graph summary for one verified export."""

    path: Path
    format: Literal["json", "csv"]
    run_id: str
    snapshot_id: str
    signer_key_id: str
    authenticated: bool
    record_count: int
    chunk_count: int
    total_bytes: int
    chunk_content_hashes: tuple[str, ...]
    manifest_content_hash: str
    artifact_digest: str


@dataclass(frozen=True, slots=True)
class _InspectedExport:
    manifest: Mapping[str, ClosedAuditExportJSON]
    manifest_bytes: bytes
    public_config: Mapping[str, ClosedAuditExportJSON]
    data_size: int
    data_hash: str

    def __post_init__(self) -> None:
        freeze_fields(self, "manifest", "public_config")


def _parse_canonical_object(content: bytes, label: str) -> dict[str, ClosedAuditExportJSON]:
    try:
        value = canonical_json_loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise AuditIntegrityError(f"audit export {label} is not valid canonical JSON") from exc
    if type(value) is not dict or any(type(key) is not str for key in value):
        raise AuditIntegrityError(f"audit export {label} must be an exact string-keyed JSON object")
    try:
        canonical_content = canonical_json(value).encode("utf-8")
    except (CanonicalizationError, RecursionError) as exc:
        raise AuditIntegrityError(f"audit export {label} is not valid canonical JSON") from exc
    if canonical_content != content:
        raise AuditIntegrityError(f"audit export {label} is not canonical JSON")
    return cast(dict[str, ClosedAuditExportJSON], value)


def _iter_lines(source: BinaryIO) -> Iterator[tuple[bytes, bool]]:
    """Yield bounded frames with a final-line marker and no unbounded readline."""

    source.seek(0)
    current = source.readline(AUDIT_EXPORT_MAX_CHUNK_BYTES + 2)
    if not current:
        raise AuditIntegrityError("audit export is empty")
    while True:
        following = source.readline(AUDIT_EXPORT_MAX_CHUNK_BYTES + 2)
        if not following:
            yield current, True
            return
        yield current, False
        current = following


def _inspect_export(source: BinaryIO) -> _InspectedExport:
    manifest: dict[str, ClosedAuditExportJSON] | None = None
    manifest_bytes: bytes | None = None
    public_config: dict[str, ClosedAuditExportJSON] | None = None
    config_count = 0
    record_count = 0
    data_size = 0
    digest = hashlib.sha256()
    for raw_line, final in _iter_lines(source):
        if final:
            if raw_line.endswith(b"\n") or len(raw_line) > MAX_AUDIT_EXPORT_SIGNED_MANIFEST_BYTES:
                raise AuditIntegrityError("audit export final manifest must be bounded and must not end with a newline")
            manifest = _parse_canonical_object(raw_line, "final manifest")
            manifest_bytes = raw_line
            break
        if not raw_line.endswith(b"\n") or raw_line == b"\n":
            raise AuditIntegrityError("audit export contains an incomplete or empty record frame")
        record = _parse_canonical_object(raw_line[:-1], f"record {record_count}")
        if "record_type" in record and record["record_type"] == "manifest":
            raise AuditIntegrityError("audit export contains a non-final manifest record")
        if "signature" in record:
            unsigned = dict(record)
            del unsigned["signature"]
        else:
            unsigned = record
        if "record_type" in unsigned and unsigned["record_type"] == "audit_export_config":
            config_count += 1
            if set(unsigned) != {"record_type", "public_config"} or type(unsigned["public_config"]) is not dict:
                raise AuditIntegrityError("audit export configuration record has a divergent shape")
            public_config = unsigned["public_config"]
        record_count += 1
        if record_count > AUDIT_EXPORT_MAX_TOTAL_RECORDS:
            raise AuditIntegrityError("audit export record count exceeds the code-owned maximum")
        data_size += len(raw_line)
        if data_size > AUDIT_EXPORT_MAX_TOTAL_BYTES:
            raise AuditIntegrityError("audit export data exceeds the code-owned maximum")
        digest.update(raw_line)

    assert manifest is not None and manifest_bytes is not None
    if set(manifest) != _MANIFEST_FIELDS:
        raise AuditIntegrityError("audit export final manifest has a divergent field set")
    if manifest["record_type"] != "manifest" or manifest["schema"] != AUDIT_EXPORT_MANIFEST_SCHEMA:
        raise AuditIntegrityError("audit export final manifest has an unsupported identity")
    core = dict(manifest)
    del core["signature"]
    try:
        validate_closed_stage_payload("audit-export-final-manifest-signing-body-v2", core)
    except (TypeError, ValueError, KeyError) as exc:
        raise AuditIntegrityError("audit export final manifest violates the closed schema") from exc
    if config_count != 1 or public_config is None:
        raise AuditIntegrityError("audit export requires exactly one public configuration record")
    if manifest["record_count"] != record_count or manifest["total_bytes"] != data_size:
        raise AuditIntegrityError("audit export manifest record or byte count differs from delivered data")
    return _InspectedExport(manifest, manifest_bytes, public_config, data_size, digest.hexdigest())


def _exact_string(value: ClosedAuditExportJSON, field_name: str) -> str:
    if type(value) is not str or not value:
        raise AuditIntegrityError(f"audit export {field_name} must be a non-empty exact string")
    return value


def _exact_integer(value: ClosedAuditExportJSON, field_name: str) -> int:
    if type(value) is not int or value < 1:
        raise AuditIntegrityError(f"audit export {field_name} must be a positive exact integer")
    return value


def _exact_boolean(value: ClosedAuditExportJSON, field_name: str) -> bool:
    if type(value) is not bool:
        raise AuditIntegrityError(f"audit export {field_name} must be an exact boolean")
    return value


def _export_format(value: ClosedAuditExportJSON) -> Literal["json", "csv"]:
    if value == "json" or value == "csv":
        return cast(Literal["json", "csv"], value)
    raise AuditExportVerificationError("unsupported_delivered_format", "audit export declares an unsupported delivered format")


def _signing_mode(value: ClosedAuditExportJSON) -> Literal["unsigned", "hmac_sha256"]:
    if value == "unsigned" or value == "hmac_sha256":
        return cast(Literal["unsigned", "hmac_sha256"], value)
    raise AuditExportVerificationError("unsupported_signature_algorithm", "audit export uses an unsupported signature algorithm")


def _auth_event_policy(value: ClosedAuditExportJSON) -> Literal["omitted", "deployment_snapshot"]:
    if value == "omitted" or value == "deployment_snapshot":
        return cast(Literal["omitted", "deployment_snapshot"], value)
    raise AuditIntegrityError("audit export declares an unsupported auth-event policy")


def _config_for_verification(
    inspected: _InspectedExport,
    *,
    key_resolver: AuditExportVerificationKeyResolver | None,
    require_authenticated: bool,
) -> AuditExportDerivationConfig:
    manifest = inspected.manifest
    public = inspected.public_config
    signing_mode = _signing_mode(manifest["signature_algorithm"])
    signer_key_id = _exact_string(manifest["signature_key_id"], "signature_key_id")
    signing_key: bytes | None = None
    if signing_mode == "unsigned":
        if require_authenticated:
            raise AuditExportVerificationError(
                "unsigned_refused",
                "audit export is unsigned but authenticated verification is required",
            )
    elif signing_mode == "hmac_sha256":
        if key_resolver is None:
            raise AuditExportVerificationError(
                "unknown_signer",
                f"no retained audit-export verification key for signer {signer_key_id!r}",
            )
        signing_key = key_resolver.resolve(signer_key_id)
        if type(signing_key) is not bytes or not signing_key:
            raise AuditIntegrityError("resolved audit-export verification key must be non-empty exact bytes")
    try:
        return AuditExportDerivationConfig(
            source_run_id=_exact_string(manifest["run_id"], "run_id"),
            source_status=_exact_string(manifest["source_status"], "source_status"),
            source_completed_at=_exact_string(manifest["source_completed_at"], "source_completed_at"),
            export_format=_export_format(public["export_format"]),
            exporter_version=_exact_string(public["exporter_version"], "exporter_version"),
            serialization_version=_exact_string(public["serialization_version"], "serialization_version"),
            chunking_algorithm_version=_exact_string(public["chunking_algorithm_version"], "chunking_algorithm_version"),
            include_raw_error_rows=_exact_boolean(public["include_raw_error_rows"], "include_raw_error_rows"),
            per_chunk_byte_limit=_exact_integer(public["per_chunk_byte_limit"], "per_chunk_byte_limit"),
            per_chunk_record_limit=_exact_integer(public["per_chunk_record_limit"], "per_chunk_record_limit"),
            signing_mode=_signing_mode(public["signing_mode"]),
            signer_key_id=_exact_string(public["signer_key_id"], "signer_key_id"),
            signing_key=signing_key,
            auth_events=_auth_event_policy(public["auth_events"]),
            compartment_id=_exact_string(public["compartment_id"], "compartment_id"),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise AuditIntegrityError("audit export public configuration is invalid") from exc


def _unsigned_records(source: BinaryIO) -> Iterator[dict[str, ClosedAuditExportJSON]]:
    for record_index, (raw_line, final) in enumerate(_iter_lines(source)):
        if final:
            return
        record = _parse_canonical_object(raw_line[:-1], f"record {record_index}")
        if "signature" in record:
            del record["signature"]
        yield record


def _spooled_data_hash(spool: BinaryIO, offsets: tuple[tuple[int, int], ...]) -> str:
    digest = hashlib.sha256()
    for offset, size in offsets:
        spool.seek(offset)
        remaining = size
        while remaining:
            block = spool.read(min(1024 * 1024, remaining))
            if type(block) is not bytes or not block:
                raise AuditIntegrityError("audit export verifier spool ended before the derived content")
            digest.update(block)
            remaining -= len(block)
    return digest.hexdigest()


def _verify_stream(
    source: BinaryIO,
    *,
    delivered_path: Path,
    expected_format: Literal["json", "csv"],
    artifact_digest: str,
    key_resolver: AuditExportVerificationKeyResolver | None = None,
    require_authenticated: bool = True,
) -> AuditExportVerificationReport:
    inspected = _inspect_export(source)
    config = _config_for_verification(
        inspected,
        key_resolver=key_resolver,
        require_authenticated=require_authenticated,
    )
    if config.export_format != expected_format or inspected.manifest["export_format"] != expected_format:
        raise AuditExportVerificationError(
            "unsupported_delivered_format",
            f"audit export {expected_format} container carries a different authenticated export format",
        )
    try:
        with TemporaryFile(mode="w+b") as spool:
            derived = derive_audit_export_bundle_to_spool(
                _unsigned_records(source),
                config,
                spool,
                max_total_records=AUDIT_EXPORT_MAX_TOTAL_RECORDS,
                max_total_bytes=AUDIT_EXPORT_MAX_TOTAL_BYTES,
                max_chunks=AUDIT_EXPORT_MAX_CHUNKS,
            )
            derived_data_hash = _spooled_data_hash(spool, derived.chunk_offsets)
    except AuditIntegrityError:
        raise
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise AuditIntegrityError("audit export could not be rederived from its delivered records") from exc

    if not hmac.compare_digest(derived_data_hash, inspected.data_hash):
        raise AuditIntegrityError("audit export delivered record bytes differ from the authenticated derivation")
    if derived.total_bytes != inspected.data_size:
        raise AuditIntegrityError("audit export delivered byte count differs from the authenticated derivation")
    if derived.signed_manifest_bytes != inspected.manifest_bytes:
        raise AuditIntegrityError("audit export final manifest differs from the authenticated derivation")
    if derived.config.signer_key_id != inspected.manifest["signature_key_id"]:
        raise AuditIntegrityError("audit export signer identity differs between configuration and manifest")
    if derived.config.signing_mode == "hmac_sha256" and derived.signed_manifest.signature is None:
        raise AuditIntegrityError("authenticated audit export is missing its final manifest signature")
    if derived.config.signing_mode == "unsigned" and derived.signed_manifest.signature is not None:
        raise AuditIntegrityError("unsigned audit export unexpectedly carries a final manifest signature")
    if inspected.manifest["derivation_version"] != AUDIT_EXPORT_DERIVATION_VERSION:
        raise AuditIntegrityError("audit export derivation version is unsupported")

    return AuditExportVerificationReport(
        path=delivered_path,
        format=expected_format,
        run_id=derived.config.source_run_id,
        snapshot_id=derived.snapshot_id,
        signer_key_id=derived.config.signer_key_id,
        authenticated=derived.config.signing_mode == "hmac_sha256",
        record_count=derived.record_count,
        chunk_count=len(derived.chunks),
        total_bytes=derived.total_bytes,
        chunk_content_hashes=tuple(chunk.descriptor.content_hash for chunk in derived.chunks),
        manifest_content_hash=derived.signed_manifest.content_hash,
        artifact_digest=artifact_digest,
    )


@dataclass(slots=True)
class _CsvProjectionSpool:
    relative_path: str
    stream: IO[str]
    fieldnames: set[str]


class _HashingTextSink:
    __slots__ = ("_digest", "size")

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self.size = 0

    def write(self, content: str) -> int:
        encoded = content.encode("utf-8")
        self._digest.update(encoded)
        self.size += len(encoded)
        return len(content)

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _neutralize_csv_formula(value: object) -> object:
    if type(value) is str and value.startswith(_CSV_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _csv_relative_path(record_type: object, seen: dict[str, str]) -> str:
    if type(record_type) is not str or not record_type:
        raise AuditIntegrityError("every audit-export data record requires a non-empty record_type")
    if len(record_type.encode("utf-8")) > _MAX_RECORD_TYPE_BYTES or _RECORD_TYPE.fullmatch(record_type) is None:
        raise AuditIntegrityError("audit-export record_type cannot form a safe bounded CSV filename")
    relative_path = f"{record_type}.csv"
    folded = relative_path.casefold()
    if folded in {
        AUDIT_EXPORT_DELIVERED_MANIFEST_NAME.casefold(),
        AUDIT_EXPORT_PORTABLE_RECORDS_NAME.casefold(),
    }:
        raise AuditIntegrityError("audit-export record type collides with a reserved portable-bundle name")
    prior = seen[folded] if folded in seen else None
    if prior is not None and prior != relative_path:
        raise AuditIntegrityError("audit-export CSV filenames contain a case-fold collision")
    seen[folded] = relative_path
    return relative_path


def _signed_records(source: BinaryIO) -> Iterator[dict[str, ClosedAuditExportJSON]]:
    for record_index, (raw_line, final) in enumerate(_iter_lines(source)):
        if final:
            return
        yield _parse_canonical_object(raw_line[:-1], f"record {record_index}")


def _expected_csv_projections(source: BinaryIO) -> dict[str, tuple[str, int]]:
    formatter = CSVFormatter()
    spools: dict[str, _CsvProjectionSpool] = {}
    seen_names: dict[str, str] = {}
    with ExitStack() as temporary_files:
        for record in _signed_records(source):
            if "record_type" not in record:
                raise AuditIntegrityError("audit-export record is missing record_type")
            relative_path = _csv_relative_path(record["record_type"], seen_names)
            flattened = formatter.format(cast(dict[str, object], record))
            safe_record = {key: _neutralize_csv_formula(item) for key, item in flattened.items()}
            spool = spools[relative_path] if relative_path in spools else None
            if spool is None:
                if len(spools) >= AUDIT_EXPORT_MAX_DELIVERED_FILES - 2:
                    raise AuditIntegrityError("audit-export CSV file count exceeds the code-owned maximum")
                stream = temporary_files.enter_context(TemporaryFile("w+", encoding="utf-8", newline="\n"))
                spool = _CsvProjectionSpool(relative_path, stream, set())
                spools[relative_path] = spool
            spool.fieldnames.update(safe_record)
            spool.stream.write(canonical_json(safe_record))
            spool.stream.write("\n")
        if not spools:
            raise AuditIntegrityError("audit-export CSV bundle contains no data record projections")

        expected: dict[str, tuple[str, int]] = {}
        for relative_path, spool in spools.items():
            sink = _HashingTextSink()
            writer = csv.DictWriter(sink, fieldnames=sorted(spool.fieldnames), lineterminator="\r\n")
            writer.writeheader()
            spool.stream.seek(0)
            for line in spool.stream:
                row = canonical_json_loads(line)
                if type(row) is not dict:
                    raise AuditIntegrityError("audit-export verifier CSV spool contains a non-object row")
                writer.writerow(row)
            expected[relative_path] = (sink.hexdigest(), sink.size)
        return expected


@dataclass(frozen=True, slots=True)
class _RegularFileMetadata:
    size_bytes: int
    device: int
    inode: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class _DirectoryEvidence:
    device: int
    inode: int
    modified_ns: int
    changed_ns: int


@dataclass(slots=True)
class _CapturedFile:
    relative_path: str
    stream: BinaryIO
    content_hash: str
    size_bytes: int
    source_metadata: _RegularFileMetadata


@dataclass(slots=True)
class _CapturedCsvBundle:
    names: tuple[str, ...]
    files: tuple[_CapturedFile, ...]


@contextmanager
def _private_binary_snapshot() -> Iterator[BinaryIO]:
    with TemporaryFile("w+b") as snapshot:
        yield snapshot


def _regular_file_metadata(path: Path) -> _RegularFileMetadata:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise AuditIntegrityError(f"audit-export bundle file {path.name!r} cannot be opened") from exc
    if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise AuditIntegrityError(f"audit-export bundle entry {path.name!r} must be a regular file")
    return _RegularFileMetadata(
        size_bytes=observed.st_size,
        device=observed.st_dev,
        inode=observed.st_ino,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _metadata_from_status(observed: os.stat_result) -> _RegularFileMetadata:
    return _RegularFileMetadata(
        size_bytes=observed.st_size,
        device=observed.st_dev,
        inode=observed.st_ino,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _capture_regular_file(
    path: Path,
    *,
    relative_path: str,
    maximum_bytes: int,
    expected_metadata: _RegularFileMetadata,
    resources: ExitStack,
) -> _CapturedFile:
    observed = _regular_file_metadata(path)
    if observed != expected_metadata:
        raise AuditIntegrityError(f"audit-export bundle file {path.name!r} changed during capture")
    if observed.size_bytes > maximum_bytes:
        raise AuditIntegrityError("audit-export artifact exceeds the code-owned byte maximum")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise AuditIntegrityError(f"audit-export bundle file {path.name!r} cannot be opened") from exc
    try:
        before_status = os.fstat(descriptor)
        if not stat.S_ISREG(before_status.st_mode):
            raise AuditIntegrityError(f"audit-export bundle entry {path.name!r} must be a regular file")
        before = _metadata_from_status(before_status)
        if before != observed:
            raise AuditIntegrityError(f"audit-export bundle file {path.name!r} changed during capture")
        snapshot = resources.enter_context(_private_binary_snapshot())
        digest = hashlib.sha256()
        remaining = maximum_bytes
        captured_size = 0
        while block := os.read(descriptor, min(1024 * 1024, remaining + 1)):
            if len(block) > remaining:
                raise AuditIntegrityError("audit-export artifact exceeds the code-owned byte maximum")
            written = snapshot.write(block)
            if written != len(block):
                raise AuditIntegrityError("audit-export private snapshot ended before the captured content")
            digest.update(block)
            captured_size += len(block)
            remaining -= len(block)
        after_status = os.fstat(descriptor)
        if not stat.S_ISREG(after_status.st_mode):
            raise AuditIntegrityError(f"audit-export bundle entry {path.name!r} must remain a regular file")
        after = _metadata_from_status(after_status)
        final = _regular_file_metadata(path)
        if before != after or after != final or captured_size != after.size_bytes:
            raise AuditIntegrityError(f"audit-export bundle file {path.name!r} changed during capture")
        snapshot.flush()
        snapshot.seek(0)
        return _CapturedFile(
            relative_path=relative_path,
            stream=snapshot,
            content_hash=digest.hexdigest(),
            size_bytes=captured_size,
            source_metadata=after,
        )
    finally:
        os.close(descriptor)


def _directory_evidence(path: Path) -> _DirectoryEvidence:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise AuditIntegrityError("audit-export CSV bundle cannot be opened") from exc
    if not stat.S_ISDIR(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise AuditIntegrityError("audit-export CSV bundle must remain the original regular directory")
    return _DirectoryEvidence(
        device=observed.st_dev,
        inode=observed.st_ino,
        modified_ns=observed.st_mtime_ns,
        changed_ns=observed.st_ctime_ns,
    )


def _directory_names(path: Path) -> tuple[str, ...]:
    names: list[str] = []
    folded_names: set[str] = set()
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                name = entry.name
                names.append(name)
                if len(names) > AUDIT_EXPORT_MAX_DELIVERED_FILES:
                    raise AuditIntegrityError("audit-export CSV bundle file count exceeds the code-owned maximum")
                folded = name.casefold()
                if folded in folded_names:
                    raise AuditIntegrityError("audit-export CSV bundle contains a case-fold name collision")
                folded_names.add(folded)
                if name in {"", ".", ".."} or "/" in name or "\\" in name:
                    raise AuditIntegrityError("audit-export CSV bundle contains an unsafe entry name")
    except AuditIntegrityError:
        raise
    except OSError as exc:
        raise AuditIntegrityError("audit-export CSV bundle cannot be enumerated") from exc
    return tuple(sorted(names))


def _capture_csv_bundle(path: Path, *, resources: ExitStack) -> _CapturedCsvBundle:
    directory = _directory_evidence(path)
    names = _directory_names(path)
    remaining = AUDIT_EXPORT_MAX_DELIVERED_BYTES
    expected_metadata: dict[str, _RegularFileMetadata] = {}
    for name in names:
        metadata = _regular_file_metadata(path / name)
        if metadata.size_bytes > remaining:
            raise AuditIntegrityError("audit-export CSV bundle exceeds the code-owned byte maximum")
        remaining -= metadata.size_bytes
        expected_metadata[name] = metadata

    files: list[_CapturedFile] = []
    for name in names:
        captured = _capture_regular_file(
            path / name,
            relative_path=name,
            maximum_bytes=expected_metadata[name].size_bytes,
            expected_metadata=expected_metadata[name],
            resources=resources,
        )
        files.append(captured)

    if _directory_evidence(path) != directory or _directory_names(path) != names:
        raise AuditIntegrityError("audit-export CSV bundle changed during capture")
    return _CapturedCsvBundle(names=names, files=tuple(files))


def _verify_csv_bundle(
    bundle: _CapturedCsvBundle,
    *,
    delivered_path: Path,
    artifact_digest: str,
    key_resolver: AuditExportVerificationKeyResolver | None,
    require_authenticated: bool,
) -> AuditExportVerificationReport:
    captured_files = {captured.relative_path: captured for captured in bundle.files}
    if AUDIT_EXPORT_PORTABLE_RECORDS_NAME not in captured_files:
        raise AuditIntegrityError("audit-export CSV bundle is missing its portable record stream")
    raw = captured_files[AUDIT_EXPORT_PORTABLE_RECORDS_NAME]
    report = _verify_stream(
        raw.stream,
        delivered_path=delivered_path,
        expected_format="csv",
        artifact_digest=artifact_digest,
        key_resolver=key_resolver,
        require_authenticated=require_authenticated,
    )
    if AUDIT_EXPORT_DELIVERED_MANIFEST_NAME not in captured_files:
        raise AuditIntegrityError("audit-export CSV bundle is missing its delivered manifest")
    inspected = _inspect_export(raw.stream)
    projections = _expected_csv_projections(raw.stream)
    expected_names = tuple(
        sorted(
            {
                *projections,
                AUDIT_EXPORT_DELIVERED_MANIFEST_NAME,
                AUDIT_EXPORT_PORTABLE_RECORDS_NAME,
            }
        )
    )
    if bundle.names != expected_names:
        raise AuditIntegrityError("audit-export CSV bundle has missing, renamed, or unlisted files")

    manifest = captured_files[AUDIT_EXPORT_DELIVERED_MANIFEST_NAME]
    if manifest.size_bytes != len(inspected.manifest_bytes) or not hmac.compare_digest(
        manifest.content_hash,
        hashlib.sha256(inspected.manifest_bytes).hexdigest(),
    ):
        raise AuditIntegrityError("audit-export CSV bundle manifest differs from the authenticated portable record stream")

    total_size = manifest.size_bytes + raw.size_bytes
    for relative_path, expected in projections.items():
        observed = captured_files[relative_path]
        expected_hash, expected_size = expected
        if observed.size_bytes != expected_size or not hmac.compare_digest(observed.content_hash, expected_hash):
            raise AuditIntegrityError(f"audit-export CSV projection {relative_path!r} differs from authenticated records")
        total_size += observed.size_bytes
        if total_size > AUDIT_EXPORT_MAX_DELIVERED_BYTES:
            raise AuditIntegrityError("audit-export CSV bundle exceeds the code-owned byte maximum")
    return report


def verify_audit_export(
    path: Path,
    *,
    key_resolver: AuditExportVerificationKeyResolver | None = None,
    require_authenticated: bool = True,
) -> AuditExportVerificationReport:
    """Verify one bounded private snapshot of a delivered audit export.

    ``require_authenticated`` defaults to true.  Callers performing an
    integrity-only review of a deliberately unsigned export must opt in with
    ``require_authenticated=False``; the resulting report records
    ``authenticated=False``.  ``artifact_digest`` binds the exact captured
    file or directory bundle; callers retaining the source path must preserve
    custody or revalidate that digest before later use.
    """

    if type(require_authenticated) is not bool:
        raise TypeError("require_authenticated must be an exact bool")
    try:
        status = path.lstat()
    except OSError as exc:
        raise AuditIntegrityError("audit export cannot be opened") from exc
    if stat.S_ISLNK(status.st_mode):
        raise AuditIntegrityError("audit export path must not be a symlink")
    with ExitStack() as resources:
        if stat.S_ISREG(status.st_mode):
            expected_metadata = _regular_file_metadata(path)
            captured = _capture_regular_file(
                path,
                relative_path=AUDIT_EXPORT_PORTABLE_RECORDS_NAME,
                maximum_bytes=AUDIT_EXPORT_MAX_TOTAL_BYTES + MAX_AUDIT_EXPORT_SIGNED_MANIFEST_BYTES,
                expected_metadata=expected_metadata,
                resources=resources,
            )
            return _verify_stream(
                captured.stream,
                delivered_path=path,
                expected_format="json",
                artifact_digest=captured.content_hash,
                key_resolver=key_resolver,
                require_authenticated=require_authenticated,
            )
        if stat.S_ISDIR(status.st_mode):
            bundle = _capture_csv_bundle(path, resources=resources)
            artifact_digest = audit_export_directory_bundle_hash(
                (captured.relative_path, captured.content_hash, captured.size_bytes) for captured in bundle.files
            )
            return _verify_csv_bundle(
                bundle,
                delivered_path=path,
                artifact_digest=artifact_digest,
                key_resolver=key_resolver,
                require_authenticated=require_authenticated,
            )
    raise AuditIntegrityError("audit export path must be a regular JSON file or CSV bundle directory")


__all__ = [
    "AuditExportVerificationError",
    "AuditExportVerificationKeyResolver",
    "AuditExportVerificationReport",
    "EnvironmentAuditExportVerificationKeyResolver",
    "MappingAuditExportVerificationKeyResolver",
    "environment_audit_export_verification_key_resolver",
    "verify_audit_export",
]
