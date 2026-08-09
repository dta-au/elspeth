"""Binary-document format vocabulary and exact byte-signature verification.

Layer: L0. No upward imports.

One authority for what a JPEG, PNG, or PDF byte stream looks like, consumed
by BOTH the authenticated web upload/paste boundary (declared-MIME/signature
agreement at admission) and the ``aws_textract_inline_analysis`` transform
(configured-format/signature agreement immediately before the provider
call). Signature validation is deliberately narrow — an exact prefix at
offset zero — and is not a general file-validity parser: leading
whitespace, a byte-order mark, or an embedded signature later in the
payload is a mismatch, never a reclassification. The byte signature proves
agreement with a declared format; it does not choose one.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal, get_args

BinaryDocumentFormat = Literal["jpeg", "png", "pdf"]
"""Closed set of approved binary document formats (JPEG is spelled ``jpeg``)."""

BINARY_DOCUMENT_FORMATS: frozenset[str] = frozenset(get_args(BinaryDocumentFormat))
"""Runtime view derived from ``BinaryDocumentFormat`` to prevent drift."""

BINARY_DOCUMENT_MAX_BYTES: Final[int] = 5 * 1024 * 1024
"""Hard 5 MiB ceiling for the Textract-inline authoring path.

Shared by upload admission and the transform's ``max_document_bytes``
upper bound: the design resolves AWS's inconsistent 5 MB / 10 MiB
documentation conservatively, so this value may be reduced by
configuration but never raised."""

BINARY_DOCUMENT_MIME_BY_FORMAT: Final = MappingProxyType(
    {
        "jpeg": "image/jpeg",
        "png": "image/png",
        "pdf": "application/pdf",
    }
)
"""Storage MIME value declared for each approved format."""

BINARY_DOCUMENT_FORMAT_BY_MIME: Final = MappingProxyType({mime: fmt for fmt, mime in BINARY_DOCUMENT_MIME_BY_FORMAT.items()})
"""Inverse mapping used by the upload boundary to select the signature rule."""

_SIGNATURES: Final = MappingProxyType(
    {
        "jpeg": b"\xff\xd8\xff",
        "png": b"\x89PNG\r\n\x1a\n",
        "pdf": b"%PDF-",
    }
)


def binary_document_signature_matches(document_format: str, data: bytes) -> bool:
    """Return True iff ``data`` begins with ``document_format``'s exact signature.

    Raises ``ValueError`` for a format outside the closed vocabulary — the
    caller is expected to have validated the format against
    ``BINARY_DOCUMENT_FORMATS`` (or arrived via
    ``BINARY_DOCUMENT_FORMAT_BY_MIME``), so an unknown value here is a
    programming error, not a document defect.
    """
    if document_format not in _SIGNATURES:
        raise ValueError(f"unknown binary document format {document_format!r}")
    return data.startswith(_SIGNATURES[document_format])
