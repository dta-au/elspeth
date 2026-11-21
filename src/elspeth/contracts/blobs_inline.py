"""Contracts for widened ``blob_ref`` inline content markers.

Layer: L0. No upward imports.

This module defines the shared marker grammar and value objects for
audited inline blob content. Web composer code, validation code, and the
core resolver all consume these contracts, so the definitions live here
instead of in the web blob service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final, Literal, cast, get_args
from uuid import UUID

from elspeth.contracts.blobs import ALLOWED_MIME_TYPES, AllowedMimeType

ContentEncoding = Literal["utf-8", "utf-8-sig", "utf-16", "latin-1"]
"""Closed set of decoders allowed for inline content."""

ALLOWED_CONTENT_ENCODINGS: frozenset[str] = frozenset(get_args(ContentEncoding))
"""Runtime view derived from ``ContentEncoding`` to prevent drift."""

BlobRefMode = Literal["bind_source", "inline_content"]
"""Allowed widened ``blob_ref`` modes."""

ALLOWED_BLOB_REF_MODES: frozenset[str] = frozenset(get_args(BlobRefMode))
"""Runtime view derived from ``BlobRefMode`` to prevent drift."""

BlobInlineValidationCategory = Literal["missing", "oversized", "not_ready", "hash_mismatch", "malformed", "not_text"]
"""Validation failure categories for inline-content blob refs."""

_SHA256_HEX_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_UUID_PATTERN: Final = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_FIELD_PATH_PATTERN: Final = re.compile(
    r"^(?:source(?::[^.\[\]]+)?|node:[^.\[\]]+|output:[^.\[\]]+)"
    r"\.options\.[A-Za-z_][A-Za-z0-9_-]*(?:\.(?:[A-Za-z_][A-Za-z0-9_-]*|0|[1-9][0-9]*))*$"
)
_ALLOWED_MARKER_KEYS: Final = frozenset({"blob_ref", "mode", "path", "sha256", "encoding"})


def _validate_sha256(owner: str, value: str) -> None:
    if _SHA256_HEX_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{owner}: sha256 must be 64 lowercase hex characters")


def _validate_field_path(owner: str, value: str) -> None:
    if _FIELD_PATH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{owner}: field_path must use source/node/output identity prefixes and .options.<field> segments")


@dataclass(frozen=True, slots=True)
class WidenedBlobRefShape:
    """Parsed widened ``blob_ref`` marker."""

    blob_id: UUID
    mode: BlobRefMode
    sha256: str | None = None
    path: str | None = None
    encoding: ContentEncoding = "utf-8"

    def __post_init__(self) -> None:
        if self.mode not in ALLOWED_BLOB_REF_MODES:
            raise ValueError(f"{type(self).__name__}: mode must be one of {sorted(ALLOWED_BLOB_REF_MODES)}")
        if self.encoding not in ALLOWED_CONTENT_ENCODINGS:
            raise ValueError(f"{type(self).__name__}: encoding must be one of {sorted(ALLOWED_CONTENT_ENCODINGS)}")
        if self.mode == "inline_content":
            if self.path is not None:
                raise ValueError(f"{type(self).__name__}: inline_content markers cannot carry path")
            if self.sha256 is None:
                raise ValueError(f"{type(self).__name__}: inline_content markers require sha256")
            _validate_sha256(type(self).__name__, self.sha256)
            return
        if self.sha256 is not None:
            raise ValueError(f"{type(self).__name__}: bind_source markers cannot carry sha256")
        if self.encoding != "utf-8":
            raise ValueError(f"{type(self).__name__}: bind_source markers cannot carry encoding")


@dataclass(frozen=True, slots=True)
class BlobInlineRef:
    """Inline content ref discovered during config tree-walk."""

    field_path: str
    blob_id: UUID
    sha256: str
    encoding: ContentEncoding

    def __post_init__(self) -> None:
        _validate_field_path(type(self).__name__, self.field_path)
        _validate_sha256(type(self).__name__, self.sha256)
        if self.encoding not in ALLOWED_CONTENT_ENCODINGS:
            raise ValueError(f"{type(self).__name__}: encoding must be one of {sorted(ALLOWED_CONTENT_ENCODINGS)}")


@dataclass(frozen=True, slots=True)
class BlobInlineValidationViolation:
    """Structured validate-path violation for inline blob content refs."""

    category: BlobInlineValidationCategory
    field_path: str
    detail: str


INLINE_BLOB_NOT_TEXT_CODE: Final = "inline_blob_not_text"
INLINE_BLOB_NOT_TEXT_REPAIR: Final = "Upload or create the prompt as plain text (.txt or .md) and wire that file."


class InlineBlobNotTextError(ValueError):
    """A file-backed inline marker points to a non-text blob."""

    error_code = INLINE_BLOB_NOT_TEXT_CODE

    def __init__(self, field_path: str) -> None:
        super().__init__(f"Inline content blob reference at {field_path} is not text. {INLINE_BLOB_NOT_TEXT_REPAIR}")


@dataclass(frozen=True, slots=True)
class ResolvedBlobContent:
    """Audit-row payload for one successfully resolved inline content ref."""

    field_path: str
    blob_id: UUID
    content_hash: str
    byte_length: int
    mime_type: AllowedMimeType
    encoding: ContentEncoding

    def __post_init__(self) -> None:
        _validate_field_path(type(self).__name__, self.field_path)
        _validate_sha256(type(self).__name__, self.content_hash)
        if self.byte_length < 0:
            raise ValueError(f"{type(self).__name__}: byte_length must be non-negative")
        if self.mime_type not in ALLOWED_MIME_TYPES:
            raise ValueError(f"{type(self).__name__}: mime_type must be one of {sorted(ALLOWED_MIME_TYPES)}")
        if self.encoding not in ALLOWED_CONTENT_ENCODINGS:
            raise ValueError(f"{type(self).__name__}: encoding must be one of {sorted(ALLOWED_CONTENT_ENCODINGS)}")


def is_widened_blob_ref(value: Any) -> WidenedBlobRefShape | None:
    """Return a parsed widened ``blob_ref`` marker, or ``None``.

    Mode-less dictionaries with a ``blob_ref`` key are malformed markers
    and return ``None`` here. Resolver and validation walkers can report
    those as structural errors by separately checking for the key.
    """
    if type(value) is not dict:
        return None
    marker = cast(dict[str, object], value)
    if "blob_ref" not in marker:
        return None
    if not set(marker).issubset(_ALLOWED_MARKER_KEYS):
        return None
    blob_ref_value = marker["blob_ref"]
    if type(blob_ref_value) is not str:
        return None
    if _UUID_PATTERN.fullmatch(blob_ref_value) is None:
        return None
    blob_id = UUID(blob_ref_value)

    if "mode" not in marker:
        return None
    mode_value = marker["mode"]
    if type(mode_value) is not str:
        return None
    if mode_value not in ALLOWED_BLOB_REF_MODES:
        return None

    if mode_value == "bind_source":
        if "sha256" in marker or "encoding" in marker:
            return None
        path_value: str | None = None
        if "path" in marker:
            raw_path = marker["path"]
            if type(raw_path) is not str:
                return None
            path_value = raw_path
        return WidenedBlobRefShape(
            blob_id=blob_id,
            mode="bind_source",
            path=path_value,
        )

    if "path" in marker:
        return None
    if "sha256" not in marker:
        return None
    sha256_value = marker["sha256"]
    if type(sha256_value) is not str:
        return None
    if _SHA256_HEX_PATTERN.fullmatch(sha256_value) is None:
        return None
    encoding_value: object = "utf-8"
    if "encoding" in marker:
        encoding_value = marker["encoding"]
    if type(encoding_value) is not str:
        return None
    if encoding_value not in ALLOWED_CONTENT_ENCODINGS:
        return None
    return WidenedBlobRefShape(
        blob_id=blob_id,
        mode="inline_content",
        sha256=sha256_value,
        encoding=cast(ContentEncoding, encoding_value),
    )


def is_headless_blob_ref_fragment(value: object) -> bool:
    """Return whether ``value`` is a widened marker that has lost its ``blob_ref``.

    A dict made only of marker keys, carrying a blob ``mode`` but no
    ``blob_ref``, is not plugin data: it is what remains when a scrub removes
    the linkage key from a marker instead of the whole marker. Such a value
    names no file, so it can never be resolved; consumers refuse it rather
    than admit it as an option value.
    """
    if type(value) is not dict:
        return False
    fragment = cast(dict[str, object], value)
    if "blob_ref" in fragment or "mode" not in fragment:
        return False
    if not set(fragment).issubset(_ALLOWED_MARKER_KEYS):
        return False
    # Imported YAML can put any value under ``mode``; only a string can name a
    # blob mode, and an unhashable value would raise on the membership test.
    mode = fragment["mode"]
    return type(mode) is str and mode in ALLOWED_BLOB_REF_MODES


BLOB_INLINE_AGGREGATE_FIELD_PATH: Final = "(aggregate)"
"""Field path an oversized entry carries when the pipeline's total, not one file, is over its cap."""


def inline_blob_total_refusal(total_bytes: int, cap_bytes: int, binding_count: int) -> str:
    """The one sentence every site uses to refuse file-held text over the pipeline total.

    ``binding_count`` counts references, not distinct files: the total is
    counted once per place a file is bound, because each binding is resolved
    and stored again.
    """
    return (
        f"File-held text totals {total_bytes} bytes across {binding_count} file bindings; the limit is {cap_bytes} bytes. "
        "A file bound in more than one place counts once per place. Remove a binding or shorten the largest files."
    )


class BlobContentResolutionError(Exception):
    """Batched recoverable failures from inline blob content resolution.

    ``aggregate_binding_count`` is the number of bindings counted toward the
    pipeline total; it is set exactly when ``oversized`` carries the
    :data:`BLOB_INLINE_AGGREGATE_FIELD_PATH` entry, and that refusal's
    sentence is then part of the message.
    """

    missing: tuple[str, ...]
    oversized: tuple[tuple[str, int, int], ...]
    undecodable: tuple[tuple[str, str], ...]
    not_ready: tuple[tuple[str, str], ...]
    cross_session: tuple[str, ...]
    malformed: tuple[tuple[str, str], ...]
    aggregate_binding_count: int | None

    def __init__(
        self,
        *,
        missing: list[str] | tuple[str, ...] | None = None,
        oversized: list[tuple[str, int, int]] | tuple[tuple[str, int, int], ...] | None = None,
        undecodable: list[tuple[str, str]] | tuple[tuple[str, str], ...] | None = None,
        not_ready: list[tuple[str, str]] | tuple[tuple[str, str], ...] | None = None,
        cross_session: list[str] | tuple[str, ...] | None = None,
        malformed: list[tuple[str, str]] | tuple[tuple[str, str], ...] | None = None,
        aggregate_binding_count: int | None = None,
    ) -> None:
        self.missing = tuple(missing or ())
        self.oversized = tuple(oversized or ())
        self.undecodable = tuple(undecodable or ())
        self.not_ready = tuple(not_ready or ())
        self.cross_session = tuple(cross_session or ())
        self.malformed = tuple(malformed or ())
        has_total_refusal = any(field_path == BLOB_INLINE_AGGREGATE_FIELD_PATH for field_path, _size, _cap in self.oversized)
        if has_total_refusal != (aggregate_binding_count is not None):
            raise ValueError("BlobContentResolutionError: aggregate_binding_count is required exactly for a total refusal")
        self.aggregate_binding_count = aggregate_binding_count
        super().__init__(self._summary())

    def _summary(self) -> str:
        counts = {
            "missing": len(self.missing),
            "oversized": len(self.oversized),
            "undecodable": len(self.undecodable),
            "not_ready": len(self.not_ready),
            "cross_session": len(self.cross_session),
            "malformed": len(self.malformed),
        }
        non_zero = [f"{key}={value}" for key, value in counts.items() if value]
        if not non_zero:
            return "Blob inline content resolution failed"
        summary = "Blob inline content resolution failed: " + ", ".join(non_zero)
        if self.aggregate_binding_count is None:
            return summary
        totals = [(size, cap) for field_path, size, cap in self.oversized if field_path == BLOB_INLINE_AGGREGATE_FIELD_PATH]
        return f"{summary}. " + " ".join(inline_blob_total_refusal(size, cap, self.aggregate_binding_count) for size, cap in totals)
