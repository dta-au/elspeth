"""Inline blob content resolver helpers.

Layer: L1 (core). Imports L0 contracts only.

This module mirrors the secret resolver's tree-walk shape while keeping
blob content resolution split into sync discovery, async fetch, and sync
substitution phases. This first slice implements pure discovery only.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast
from uuid import UUID

from elspeth.contracts.blobs import (
    ALLOWED_MIME_TYPES,
    AllowedMimeType,
    BlobContentMissingError,
    BlobIntegrityError,
    BlobNotFoundError,
    BlobRecord,
    BlobStateError,
)
from elspeth.contracts.blobs_inline import (
    BLOB_INLINE_AGGREGATE_FIELD_PATH,
    BlobContentResolutionError,
    BlobInlineRef,
    BlobInlineValidationViolation,
    InlineBlobNotTextError,
    ResolvedBlobContent,
    WidenedBlobRefShape,
    inline_blob_total_refusal,
    is_widened_blob_ref,
)

# Node-collection keys in the runtime pipeline dict whose entries can carry an
# ``options`` mapping a blob reference could hide in.
#
# The AUTHORITY here is ``generate_pipeline_dict`` — the sole producer of the
# shape this module walks (``pipeline_dict_from_record`` is
# ``generate_pipeline_dict(state_from_record(record))``, and the YAML the
# execution path loads is that same dict serialized). Asking it what it emits
# gives: options-bearing sections are ``sources``, ``transforms``,
# ``aggregations``, ``collectors`` and ``sinks``; ``gates`` and ``coalesce``
# are emitted without options. The blob authoring tool also rejects every
# plugin-free structural node before mutation, because no inline marker could
# survive into runtime YAML. Both legacy keys below are kept as tolerant
# defence-in-depth for a non-canonical persisted dict rather than removed for
# tidiness.
# ``collectors`` was the live gap (elspeth-ca79b2c63a).
#
# Do NOT replace this with ``core/config.py``'s ``_plugin_bearing_sections()``,
# tempting though it looks — it is derived, it carries container shape, and it
# returns these same names today. It requires ``plugin`` AND ``options`` on the
# element model, and this walk cares about ``options`` ALONE: a section with
# options but no plugin is exactly the case it would silently skip while
# reporting success. (No such section exists today, so this is a latent
# mismatch, not a live one.) ``outputs`` below has no settings-side name at
# all. ``tests/unit/core/test_blobs_inline.py`` derives the coverage from the
# emitter, so a new options-bearing kind fails there on the day it is emitted.
_NODE_COLLECTION_KEYS: Final = ("transforms", "gates", "aggregations", "coalesce", "collectors")
_OUTPUT_COLLECTION_KEYS: Final = ("outputs", "sinks")
BLOB_INLINE_PER_REF_BYTE_CAP: Final = 256 * 1024
# Total file-held text one pipeline may resolve, counted once per REFERENCE
# (a file bound in two places counts twice) at every site that enforces it:
# validation, approval compile, run admission, the session snapshot that YAML
# export and prompt review read, and set_pipeline's newly written files.
# What it bounds (measured 2026-11-20 in the blob-cap sizing study, 128
# files): server time, not memory. Each runtime preflight costs about
# 0.9 s + 0.75 s per MiB, so 4 MiB is about 4 s per preflight and about 17 s
# of server work in a compose round that runs four; peak RSS grows about
# 12 MB per MiB. Audit-store growth: a run's Landscape records hold the
# resolved text about twice (run settings and node configs), so about 8.5 MB
# per run at this cap. Those timings were taken while validation still dumped
# and re-parsed the resolved configuration, which was 80% of the
# size-dependent time; it now parses once, so the cost per MiB is lower and
# has not been re-measured. 4 MiB is what that measurement supports.
# The bound on the resolved configuration as a whole follows this value
# (web/execution/_rendered_pipeline_yaml.py, four times this cap, counted as
# UTF-8 text bytes), so the two move together.
BLOB_INLINE_AGGREGATE_BYTE_CAP: Final = 4 * 1024 * 1024
# One option-key segment of an inline field path, as ``BlobInlineRef`` reads a
# mapping key. Numeric segments are list positions only.
_OPTION_KEY_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


@dataclass(frozen=True)
class _BlobMetadataEvaluation:
    missing: tuple[tuple[str, UUID], ...] = ()
    not_ready: tuple[tuple[str, str], ...] = ()
    hash_mismatches: tuple[tuple[BlobInlineRef, str | None], ...] = ()
    oversized: tuple[tuple[str, int, int], ...] = ()
    not_text: tuple[str, ...] = ()
    # Bindings counted toward the total; read only when ``oversized`` holds
    # the total entry.
    aggregate_binding_count: int = 0


def _discover_blob_content_refs(config: dict[str, Any]) -> list[BlobInlineRef]:
    """Return inline-content refs from a YAML-shaped config dict.

    The returned field paths use the canonical audit form:
    ``source.options.<field>`` for legacy singular-source dicts,
    ``source:<name>.options.<field>`` for plural sources,
    ``node:<name>.options.<field>``, or ``output:<sink>.options.<field>``.
    """
    refs: list[BlobInlineRef] = []
    malformed: list[tuple[str, str]] = []

    _walk_source(config, refs, malformed)
    _walk_sources(config, refs, malformed)
    _walk_nodes(config, refs, malformed)
    _walk_outputs(config, refs, malformed)

    if malformed:
        raise BlobContentResolutionError(malformed=malformed)
    return refs


def _validate_blob_content_refs_sync(
    blob_get_metadata: Callable[[UUID], BlobRecord | None],
    config: dict[str, Any],
    *,
    per_ref_byte_cap: int | None = None,
    aggregate_byte_cap: int | None = None,
) -> list[BlobInlineValidationViolation]:
    """Sync validate-path sibling for callers that cannot await metadata lookup."""
    try:
        refs = _discover_blob_content_refs(config)
    except BlobContentResolutionError as exc:
        return _malformed_validation_violations(exc)

    records_by_blob_id: dict[UUID, BlobRecord] = {}
    for ref in refs:
        record = blob_get_metadata(ref.blob_id)
        if record is not None:
            records_by_blob_id[ref.blob_id] = record

    evaluation = _evaluate_blob_content_ref_metadata(
        refs,
        records_by_blob_id,
        per_ref_byte_cap=per_ref_byte_cap,
        aggregate_byte_cap=aggregate_byte_cap,
    )
    return _metadata_evaluation_validation_violations(refs, evaluation)


def _evaluate_blob_content_ref_metadata(
    refs: list[BlobInlineRef],
    records_by_blob_id: Mapping[UUID, BlobRecord],
    *,
    not_ready_by_blob_id: Mapping[UUID, str] | None = None,
    per_ref_byte_cap: int | None = None,
    aggregate_byte_cap: int | None = None,
) -> _BlobMetadataEvaluation:
    """Evaluate metadata-only inline-content policy without choosing an error shape."""
    missing: list[tuple[str, UUID]] = []
    not_ready: list[tuple[str, str]] = []
    hash_mismatches: list[tuple[BlobInlineRef, str | None]] = []
    oversized: list[tuple[str, int, int]] = []
    not_text: list[str] = []
    not_ready_lookup = not_ready_by_blob_id or {}

    aggregate_bytes = 0
    aggregate_binding_count = 0
    for ref in refs:
        if ref.blob_id in not_ready_lookup and ref.blob_id not in records_by_blob_id:
            not_ready.append((ref.field_path, not_ready_lookup[ref.blob_id]))
            continue
        if ref.blob_id not in records_by_blob_id:
            missing.append((ref.field_path, ref.blob_id))
            continue
        record = records_by_blob_id[ref.blob_id]
        if record.status != "ready":
            not_ready.append((ref.field_path, f"blob {ref.blob_id} status is {record.status!r}"))
            continue
        if record.mime_type not in ALLOWED_MIME_TYPES:
            not_text.append(ref.field_path)
            continue
        if record.content_hash != ref.sha256:
            hash_mismatches.append((ref, record.content_hash))
            continue
        if per_ref_byte_cap is not None and record.size_bytes > per_ref_byte_cap:
            oversized.append((ref.field_path, record.size_bytes, per_ref_byte_cap))
        aggregate_bytes += record.size_bytes
        aggregate_binding_count += 1

    if aggregate_byte_cap is not None and aggregate_bytes > aggregate_byte_cap:
        oversized.append((BLOB_INLINE_AGGREGATE_FIELD_PATH, aggregate_bytes, aggregate_byte_cap))

    return _BlobMetadataEvaluation(
        missing=tuple(missing),
        not_ready=tuple(not_ready),
        hash_mismatches=tuple(hash_mismatches),
        oversized=tuple(oversized),
        not_text=tuple(not_text),
        aggregate_binding_count=aggregate_binding_count,
    )


def _metadata_evaluation_validation_violations(
    refs: list[BlobInlineRef],
    evaluation: _BlobMetadataEvaluation,
) -> list[BlobInlineValidationViolation]:
    violations: list[BlobInlineValidationViolation] = []
    missing_by_path = dict(evaluation.missing)
    not_ready_by_path = dict(evaluation.not_ready)
    hash_mismatches_by_path = {ref.field_path: (ref, actual) for ref, actual in evaluation.hash_mismatches}
    oversized_by_path = {
        field_path: (size_bytes, cap)
        for field_path, size_bytes, cap in evaluation.oversized
        if field_path != BLOB_INLINE_AGGREGATE_FIELD_PATH
    }
    not_text_paths = set(evaluation.not_text)

    for ref in refs:
        if ref.field_path in not_text_paths:
            violations.append(
                BlobInlineValidationViolation(category="not_text", field_path=ref.field_path, detail="blob MIME is not inline text")
            )
            continue
        if ref.field_path in missing_by_path:
            violations.append(
                BlobInlineValidationViolation(
                    category="missing",
                    field_path=ref.field_path,
                    detail=f"blob {missing_by_path[ref.field_path]} not found",
                )
            )
            continue
        if ref.field_path in not_ready_by_path:
            violations.append(
                BlobInlineValidationViolation(
                    category="not_ready",
                    field_path=ref.field_path,
                    detail=not_ready_by_path[ref.field_path],
                )
            )
            continue
        if ref.field_path in hash_mismatches_by_path:
            mismatch_ref, actual = hash_mismatches_by_path[ref.field_path]
            violations.append(
                BlobInlineValidationViolation(
                    category="hash_mismatch",
                    field_path=mismatch_ref.field_path,
                    detail=f"composer-pinned hash {mismatch_ref.sha256[:16]}... != blob hash {_short_hash(actual)}",
                )
            )
            continue
        if ref.field_path in oversized_by_path:
            size_bytes, cap = oversized_by_path[ref.field_path]
            violations.append(
                BlobInlineValidationViolation(
                    category="oversized",
                    field_path=ref.field_path,
                    detail=f"{size_bytes} bytes exceeds per-ref cap {cap}",
                )
            )

    for field_path, size_bytes, cap in evaluation.oversized:
        if field_path != BLOB_INLINE_AGGREGATE_FIELD_PATH:
            continue
        violations.append(
            BlobInlineValidationViolation(
                category="oversized",
                field_path=field_path,
                detail=inline_blob_total_refusal(size_bytes, cap, evaluation.aggregate_binding_count),
            )
        )

    return violations


def _substitute_blob_content_refs_for_validation(
    config: dict[str, Any],
    fetched: dict[BlobInlineRef, bytes],
    *,
    refs: list[BlobInlineRef],
    blob_metadata: dict[UUID, tuple[AllowedMimeType, int]],
) -> dict[str, Any]:
    """Validate pinned bytes and substitute them without persisting audit rows.

    The runtime resolver owns hash/encoding verification and replacement for
    both validation and execution. Validation discards its audit-row payload;
    only run admission is allowed to persist those rows or link a run.
    """
    resolved, _audit = _substitute_blob_content_refs(config, fetched, refs=refs, blob_metadata=blob_metadata)
    return resolved


def _malformed_validation_violations(exc: BlobContentResolutionError) -> list[BlobInlineValidationViolation]:
    return [
        BlobInlineValidationViolation(category="malformed", field_path=field_path, detail=reason) for field_path, reason in exc.malformed
    ]


def _enforce_blob_content_ref_metadata(
    refs: list[BlobInlineRef],
    records_by_blob_id: Mapping[UUID, BlobRecord],
    *,
    per_ref_byte_cap: int | None = None,
    aggregate_byte_cap: int | None = None,
) -> None:
    """Fail closed on metadata-only inline-content violations before byte reads."""
    evaluation = _evaluate_blob_content_ref_metadata(
        refs,
        records_by_blob_id,
        per_ref_byte_cap=per_ref_byte_cap,
        aggregate_byte_cap=aggregate_byte_cap,
    )
    if evaluation.not_text:
        raise InlineBlobNotTextError(evaluation.not_text[0])
    if evaluation.hash_mismatches:
        ref, actual = evaluation.hash_mismatches[0]
        raise BlobIntegrityError(
            str(ref.blob_id),
            expected=ref.sha256,
            actual=actual or "<missing>",
        )
    if evaluation.missing or evaluation.oversized or evaluation.not_ready:
        over_total = any(field_path == BLOB_INLINE_AGGREGATE_FIELD_PATH for field_path, _size, _cap in evaluation.oversized)
        raise BlobContentResolutionError(
            missing=[field_path for field_path, _blob_id in evaluation.missing],
            oversized=evaluation.oversized,
            not_ready=evaluation.not_ready,
            aggregate_binding_count=evaluation.aggregate_binding_count if over_total else None,
        )


def _short_hash(value: str | None) -> str:
    if value is None:
        return "<missing>"
    return f"{value[:16]}..."


def _resolve_blob_content_results(
    refs: list[BlobInlineRef],
    unique_blob_ids: list[UUID],
    results: list[bytes | BaseException],
) -> dict[BlobInlineRef, bytes]:
    """Classify one exact-context content-read batch into ref-keyed bytes."""
    refs_by_blob = _refs_by_blob_id(refs)
    bytes_by_blob: dict[UUID, bytes] = {}
    missing: list[str] = []
    not_ready: list[tuple[str, str]] = []

    for blob_id, result in zip(unique_blob_ids, results, strict=True):
        result_type = type(result)
        if result_type is BlobIntegrityError or result_type is BlobContentMissingError:
            raise cast(BlobIntegrityError | BlobContentMissingError, result)
        if result_type is BlobNotFoundError:
            for ref in refs_by_blob[blob_id]:
                missing.append(ref.field_path)
            continue
        if result_type is BlobStateError:
            for ref in refs_by_blob[blob_id]:
                not_ready.append((ref.field_path, str(result)))
            continue
        if type(result) is not bytes:
            raise cast(BaseException, result)
        bytes_by_blob[blob_id] = result

    if missing or not_ready:
        raise BlobContentResolutionError(missing=missing, not_ready=not_ready)

    return {ref: bytes_by_blob[ref.blob_id] for ref in refs}


def _unique_blob_ids(refs: list[BlobInlineRef]) -> list[UUID]:
    unique: list[UUID] = []
    seen: set[UUID] = set()
    for ref in refs:
        if ref.blob_id in seen:
            continue
        seen.add(ref.blob_id)
        unique.append(ref.blob_id)
    return unique


def _refs_by_blob_id(refs: list[BlobInlineRef]) -> dict[UUID, list[BlobInlineRef]]:
    refs_by_blob: dict[UUID, list[BlobInlineRef]] = {}
    for ref in refs:
        if ref.blob_id not in refs_by_blob:
            refs_by_blob[ref.blob_id] = []
        refs_by_blob[ref.blob_id].append(ref)
    return refs_by_blob


def _substitute_blob_content_refs(
    config: dict[str, Any],
    fetched: dict[BlobInlineRef, bytes],
    *,
    refs: list[BlobInlineRef],
    blob_metadata: dict[UUID, tuple[AllowedMimeType, int]],
) -> tuple[dict[str, Any], list[ResolvedBlobContent]]:
    """Replace inline-content markers with decoded strings and audit rows."""
    decoded_by_ref: dict[BlobInlineRef, str] = {}
    audit: list[ResolvedBlobContent] = []
    undecodable: list[tuple[str, str]] = []

    for ref in refs:
        content = fetched[ref]
        actual_hash = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual_hash, ref.sha256):
            raise BlobIntegrityError(str(ref.blob_id), expected=ref.sha256, actual=actual_hash)
        mime_type, byte_length = blob_metadata[ref.blob_id]
        if mime_type not in ALLOWED_MIME_TYPES:
            raise InlineBlobNotTextError(ref.field_path)
        decoded, decode_error = _decode_blob_content(ref, content)
        if decode_error is not None:
            undecodable.append(decode_error)
            continue
        if decoded is None:
            raise RuntimeError("Blob inline decode helper returned neither decoded content nor an error")

        decoded_by_ref[ref] = decoded
        audit.append(
            ResolvedBlobContent(
                field_path=ref.field_path,
                blob_id=ref.blob_id,
                content_hash=actual_hash,
                byte_length=byte_length,
                mime_type=mime_type,
                encoding=ref.encoding,
            )
        )

    if undecodable:
        raise BlobContentResolutionError(undecodable=undecodable)

    for ref in refs:
        _substitute_at_path(config, ref.field_path, decoded_by_ref[ref])

    return config, audit


def _decode_blob_content(ref: BlobInlineRef, content: bytes) -> tuple[str, None] | tuple[None, tuple[str, str]]:
    try:
        return content.decode(ref.encoding), None
    except UnicodeDecodeError:
        return None, (ref.field_path, ref.encoding)


def _substitute_at_path(config: dict[str, Any], field_path: str, value: str) -> None:
    if ".options." not in field_path:
        raise ValueError(f"Unrecognised blob inline field_path: {field_path!r}")
    prefix, rest = field_path.split(".options.", 1)
    keys = rest.split(".")
    if prefix == "source":
        container = _source_options(config, None)
    elif prefix.startswith("source:"):
        container = _source_options(config, prefix[len("source:") :])
    elif prefix.startswith("node:"):
        container = _node_options(config, prefix[len("node:") :])
    elif prefix.startswith("output:"):
        container = _output_options(config, prefix[len("output:") :])
    else:
        raise ValueError(f"Unrecognised blob inline field_path prefix: {prefix!r}")
    _assign_nested_option(container, keys, value)


def _source_options(config: dict[str, Any], source_name: str | None) -> dict[str, Any]:
    if source_name is None:
        source = config["source"]
        error_prefix = "source"
    else:
        sources = config["sources"]
        if type(sources) is not dict:
            raise TypeError("sources must be a mapping")
        source = cast(dict[object, object], sources)[source_name]
        error_prefix = f"source:{source_name}"
    if type(source) is not dict:
        raise TypeError(f"{error_prefix} must be a mapping")
    source_dict = cast(dict[str, object], source)
    options = source_dict["options"]
    if type(options) is not dict:
        raise TypeError(f"{error_prefix}.options must be a mapping")
    return cast(dict[str, Any], options)


def _node_options(config: dict[str, Any], node_name: str) -> dict[str, Any]:
    for collection_key in _NODE_COLLECTION_KEYS:
        if collection_key not in config:
            continue
        nodes = config[collection_key]
        if type(nodes) is not list:
            continue
        for node in cast(list[object], nodes):
            if type(node) is not dict:
                continue
            node_dict = cast(dict[str, object], node)
            if "name" not in node_dict or node_dict["name"] != node_name:
                continue
            if "options" not in node_dict:
                raise KeyError(f"Node {node_name!r} has no options mapping")
            options = node_dict["options"]
            if type(options) is not dict:
                raise TypeError(f"Node {node_name!r}.options must be a mapping")
            return cast(dict[str, Any], options)
    raise KeyError(f"Node {node_name!r} not found")


def _output_options(config: dict[str, Any], output_name: str) -> dict[str, Any]:
    for collection_key in _OUTPUT_COLLECTION_KEYS:
        if collection_key not in config:
            continue
        outputs = config[collection_key]
        if type(outputs) is not dict:
            continue
        outputs_dict = cast(dict[str, object], outputs)
        if output_name not in outputs_dict:
            continue
        output = outputs_dict[output_name]
        if type(output) is not dict:
            raise TypeError(f"Output {output_name!r} must be a mapping")
        output_dict = cast(dict[str, object], output)
        if "options" not in output_dict:
            raise KeyError(f"Output {output_name!r} has no options mapping")
        options = output_dict["options"]
        if type(options) is not dict:
            raise TypeError(f"Output {output_name!r}.options must be a mapping")
        return cast(dict[str, Any], options)
    raise KeyError(f"Output {output_name!r} not found")


def _assign_nested_option(container: dict[str, Any], keys: list[str], value: str) -> None:
    current: object = container
    for key in keys[:-1]:
        if type(current) is dict:
            current = cast(dict[str, Any], current)[key]
        elif type(current) is list:
            entries = cast(list[object], current)
            current = entries[_inline_list_index(key, len(entries))]
        else:
            raise TypeError(f"Inline blob path segment {key!r} must point to a mapping or list")
    if type(current) is dict:
        cast(dict[str, Any], current)[keys[-1]] = value
    elif type(current) is list:
        entries = cast(list[object], current)
        entries[_inline_list_index(keys[-1], len(entries))] = value
    else:
        raise TypeError("Inline blob path leaf must belong to a mapping or list")


def _inline_list_index(segment: str, length: int) -> int:
    """Accept only an existing, canonical non-negative list position."""
    if not segment.isascii() or not segment.isdecimal():
        raise ValueError("Inline blob list position must be a non-negative integer")
    index = int(segment)
    if str(index) != segment or index >= length:
        raise ValueError("Inline blob list position must name an existing item")
    return index


def _walk_source(
    config: dict[str, Any],
    refs: list[BlobInlineRef],
    malformed: list[tuple[str, str]],
) -> None:
    if "source" not in config:
        return
    source = config["source"]
    if type(source) is not dict:
        return
    source_dict = cast(dict[str, object], source)
    if "options" not in source_dict:
        return
    options = source_dict["options"]
    if type(options) is not dict:
        return
    _walk_options(cast(dict[str, object], options), "source.options", refs, malformed, source_options=True)


def _walk_sources(
    config: dict[str, Any],
    refs: list[BlobInlineRef],
    malformed: list[tuple[str, str]],
) -> None:
    if "sources" not in config:
        return
    sources = config["sources"]
    if type(sources) is not dict:
        return
    for source_name, source in cast(dict[object, object], sources).items():
        if type(source_name) is not str or type(source) is not dict:
            continue
        source_dict = cast(dict[str, object], source)
        if "options" not in source_dict:
            continue
        options = source_dict["options"]
        if type(options) is not dict:
            continue
        _walk_options(cast(dict[str, object], options), f"source:{source_name}.options", refs, malformed, source_options=True)


def _walk_nodes(
    config: dict[str, Any],
    refs: list[BlobInlineRef],
    malformed: list[tuple[str, str]],
) -> None:
    for collection_key in _NODE_COLLECTION_KEYS:
        if collection_key not in config:
            continue
        nodes = config[collection_key]
        if type(nodes) is not list:
            continue
        for node in nodes:
            if type(node) is not dict:
                continue
            node_dict = cast(dict[str, object], node)
            if "name" not in node_dict or "options" not in node_dict:
                continue
            name = node_dict["name"]
            options = node_dict["options"]
            if type(name) is not str or type(options) is not dict:
                continue
            _walk_options(cast(dict[str, object], options), f"node:{name}.options", refs, malformed)


def _walk_outputs(
    config: dict[str, Any],
    refs: list[BlobInlineRef],
    malformed: list[tuple[str, str]],
) -> None:
    for collection_key in _OUTPUT_COLLECTION_KEYS:
        if collection_key not in config:
            continue
        outputs = config[collection_key]
        if type(outputs) is not dict:
            continue
        for output_name, output in cast(dict[object, object], outputs).items():
            if type(output_name) is not str or type(output) is not dict:
                continue
            output_dict = cast(dict[str, object], output)
            if "options" not in output_dict:
                continue
            options = output_dict["options"]
            if type(options) is not dict:
                continue
            _walk_options(cast(dict[str, object], options), f"output:{output_name}.options", refs, malformed)


def _walk_options(
    value: object,
    field_path: str,
    refs: list[BlobInlineRef],
    malformed: list[tuple[str, str]],
    *,
    source_options: bool = False,
) -> None:
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        if source_options and (_is_source_options_bind_source(mapping) or _is_source_options_unmoded_binding(mapping)):
            for key, child in mapping.items():
                if type(key) is str:
                    _walk_option_key(child, field_path, key, refs, malformed)
            return
        if "blob_ref" in mapping:
            _collect_or_reject_marker(mapping, field_path, refs, malformed)
            return
        for key, child in mapping.items():
            if type(key) is str:
                _walk_option_key(child, field_path, key, refs, malformed)
        return

    if type(value) is list:
        for index, child in enumerate(cast(list[object], value)):
            _walk_options(child, f"{field_path}.{index}", refs, malformed)


def _walk_option_key(
    child: object,
    field_path: str,
    key: str,
    refs: list[BlobInlineRef],
    malformed: list[tuple[str, str]],
) -> None:
    """Walk a mapping entry only when its key reads unambiguously as a field-path name.

    A numeric segment is a list position and a dot is a separator, so a key such
    as ``2024`` or ``v1.2`` would produce a path naming a different value. A
    marker beneath such a key is refused rather than resolved somewhere else.
    """
    if _OPTION_KEY_PATTERN.fullmatch(key) is None:
        if _contains_blob_ref_marker(child):
            malformed.append(
                (f"{field_path}.{key}", "inline blob refs under an option key that is not a field-path name are not supported")
            )
        return
    _walk_options(child, f"{field_path}.{key}", refs, malformed)


def _contains_blob_ref_marker(value: object) -> bool:
    if type(value) is dict:
        mapping = cast(dict[object, object], value)
        if "blob_ref" in mapping:
            return True
        return any(_contains_blob_ref_marker(child) for child in mapping.values())
    if type(value) is list:
        return any(_contains_blob_ref_marker(child) for child in cast(list[object], value))
    return False


def _collect_or_reject_marker(
    marker: dict[object, object],
    field_path: str,
    refs: list[BlobInlineRef],
    malformed: list[tuple[str, str]],
) -> None:
    shape = is_widened_blob_ref(marker)
    if shape is None:
        malformed.append((field_path, _malformed_reason(marker)))
        return
    if shape.mode != "inline_content":
        return
    refs.append(_ref_from_shape(field_path, shape))


def _is_source_options_bind_source(mapping: dict[object, object]) -> bool:
    if "blob_ref" not in mapping or "mode" not in mapping:
        return False
    mode = mapping["mode"]
    if mode != "bind_source":
        return False
    # This is the root source plugin configuration, not a standalone marker.
    # Its encoding (for example CSV's utf-8-sig) belongs to the source plugin.
    # Nested bind_source markers still use the strict marker recognizer.
    if "sha256" in mapping:
        return False
    return "path" not in mapping or type(mapping["path"]) is str


def _is_source_options_unmoded_binding(mapping: dict[object, object]) -> bool:
    """Recognize a root source binding that was written without ``mode``.

    A source bound to an uploaded file keeps the file's identity in
    ``blob_ref`` beside the storage ``path`` the plugin reads. The composer's
    bind tool also writes ``mode: bind_source``; the YAML import's
    ``source_blob_ids`` binding, a reviewed ``set_pipeline`` source and any
    state saved from them write only ``blob_ref`` and ``path``. Both are the
    source plugin's own configuration, never a file marker, so the keys are
    walked as ordinary options.

    Only reachable for the root options of a source: the callers that walk
    ``source``/``sources`` pass ``source_options=True`` for that one mapping
    and it is not propagated to children. A mapping holding ``blob_ref``
    without ``mode`` anywhere else (a prompt slot, a nested source option, a
    node's or a sink's options) stays a malformed marker, and so does a root
    source mapping that carries ``sha256`` or lacks a string ``path``.
    """
    if "blob_ref" not in mapping or "mode" in mapping or "sha256" in mapping:
        return False
    if type(mapping["blob_ref"]) is not str:
        return False
    return "path" in mapping and type(mapping["path"]) is str


def _ref_from_shape(field_path: str, shape: WidenedBlobRefShape) -> BlobInlineRef:
    if shape.sha256 is None:
        raise RuntimeError("inline_content marker passed recognition without sha256")
    return BlobInlineRef(
        field_path=field_path,
        blob_id=shape.blob_id,
        sha256=shape.sha256,
        encoding=shape.encoding,
    )


def _malformed_reason(marker: dict[object, object]) -> str:
    if "mode" not in marker:
        return "missing mode"
    mode = marker["mode"]
    if type(mode) is not str:
        return "mode must be string"
    if mode not in {"bind_source", "inline_content"}:
        return f"unknown mode {mode!r}"
    if "encoding" in marker and type(marker["encoding"]) is not str:
        return "encoding must be string"
    if mode == "inline_content" and "sha256" not in marker:
        return "inline_content requires sha256"
    if mode == "bind_source" and ("sha256" in marker or "encoding" in marker):
        return "bind_source cannot carry sha256 or encoding"
    return "invalid blob_ref marker"
