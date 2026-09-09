"""Tests for the shared binary-document format vocabulary and signatures.

The helper is the ONE authority both the web upload boundary (MIME/signature
agreement) and the aws_textract_inline_analysis transform (configured-format/
signature agreement) consume, so admission and runtime can never disagree
about what a JPEG, PNG, or single-page-capable PDF byte stream looks like
(elspeth-0c6a343921 design: "verify MIME/signature agreement with the same
exact signature rules used by the transform").
"""

from __future__ import annotations

import pytest

from elspeth.contracts.binary_documents import (
    BINARY_DOCUMENT_FORMAT_BY_MIME,
    BINARY_DOCUMENT_FORMATS,
    BINARY_DOCUMENT_MIME_BY_FORMAT,
    binary_document_signature_matches,
)

_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 8
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
_PDF = b"%PDF-1.7\n%%EOF"


def test_format_vocabulary_is_the_approved_closed_set() -> None:
    assert frozenset({"jpeg", "png", "pdf"}) == BINARY_DOCUMENT_FORMATS


def test_mime_mapping_round_trips() -> None:
    assert BINARY_DOCUMENT_MIME_BY_FORMAT == {
        "jpeg": "image/jpeg",
        "png": "image/png",
        "pdf": "application/pdf",
    }
    assert {mime: fmt for fmt, mime in BINARY_DOCUMENT_MIME_BY_FORMAT.items()} == BINARY_DOCUMENT_FORMAT_BY_MIME


@pytest.mark.parametrize(
    ("document_format", "data"),
    [("jpeg", _JPEG), ("png", _PNG), ("pdf", _PDF)],
)
def test_exact_signatures_match(document_format: str, data: bytes) -> None:
    assert binary_document_signature_matches(document_format, data) is True


@pytest.mark.parametrize(
    ("document_format", "data"),
    [
        # A different known signature is a mismatch, not a reclassification.
        ("jpeg", _PNG),
        ("png", _JPEG),
        ("pdf", _PNG),
        # Leading whitespace / BOM defeats the offset-zero rule.
        ("pdf", b" %PDF-1.7"),
        ("pdf", b"\xef\xbb\xbf%PDF-1.7"),
        ("png", b"\x00" + _PNG),
        # An embedded signature later in the payload does not count.
        ("pdf", b"garbage%PDF-1.7"),
        ("jpeg", b"junk\xff\xd8\xff"),
        # Empty and truncated prefixes fail.
        ("jpeg", b""),
        ("jpeg", b"\xff\xd8"),
        ("png", _PNG[:7]),
        ("pdf", b"%PDF"),
    ],
)
def test_signature_mismatches_fail(document_format: str, data: bytes) -> None:
    assert binary_document_signature_matches(document_format, data) is False


def test_unknown_format_raises() -> None:
    with pytest.raises(ValueError, match="unknown binary document format"):
        binary_document_signature_matches("tiff", _PNG)


def test_storage_vocabulary_split_and_union() -> None:
    from elspeth.contracts.blobs import (
        ALLOWED_MIME_TYPES,
        BINARY_DOCUMENT_MIME_TYPES,
        STORAGE_MIME_TYPES,
    )

    assert frozenset({"image/jpeg", "image/png", "application/pdf"}) == BINARY_DOCUMENT_MIME_TYPES
    # The text/data set must NOT grow: every existing text consumer keeps
    # rejecting binary MIME values by construction.
    assert BINARY_DOCUMENT_MIME_TYPES.isdisjoint(ALLOWED_MIME_TYPES)
    assert STORAGE_MIME_TYPES == ALLOWED_MIME_TYPES | BINARY_DOCUMENT_MIME_TYPES


def test_signature_vocabulary_covers_every_format() -> None:
    for document_format in BINARY_DOCUMENT_FORMATS:
        assert binary_document_signature_matches(document_format, b"") is False
