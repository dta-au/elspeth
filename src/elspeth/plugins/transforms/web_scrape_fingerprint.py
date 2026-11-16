"""Fingerprinting utilities for web scraping change detection.

Two modes:

- ``content``: SHA-256 of the extracted content with whitespace collapsed.
  Response metadata is ignored, so only a change of the extracted text
  changes the fingerprint.
- ``full``: SHA-256 of a canonical JSON document holding the extracted
  content exactly as extracted and, for every fetched page in fetch order,
  the HTTP status, the persisted final URL (the ``fetch_url_final``
  rendering), the raw ``Content-Type`` header (``None`` when absent) and the
  SHA-256 of the content-decoded body (after transfer and content decoding,
  before text extraction). Other response headers are not
  covered: they carry per-request values (``Date``, ``Age``, cookies) that
  would change the fingerprint on every fetch and defeat change detection.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

FULL_FINGERPRINT_VERSION = 1


@dataclass(frozen=True, slots=True)
class FetchResponseContext:
    """The fetch-response facts a ``full`` fingerprint covers for one page."""

    status_code: int
    final_url: str
    content_type: str | None
    body_sha256: str

    def __post_init__(self) -> None:
        if type(self.status_code) is not int:
            raise TypeError("status_code must be int")
        if type(self.final_url) is not str:
            raise TypeError("final_url must be str")
        if self.content_type is not None and type(self.content_type) is not str:
            raise TypeError("content_type must be str or None")
        if type(self.body_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.body_sha256) is None:
            raise ValueError("body_sha256 must be a lowercase SHA-256 hex digest")


def normalize_for_fingerprint(content: str) -> str:
    """Normalize content for change-resistant fingerprinting.

    Collapses whitespace sequences to single space and strips
    leading/trailing whitespace.

    Args:
        content: Raw content

    Returns:
        Normalized content
    """
    # Collapse all whitespace sequences to single space
    normalized = re.sub(r"\s+", " ", content)
    # Strip leading/trailing
    return normalized.strip()


def compute_fingerprint(
    content: str,
    mode: Literal["content", "full"],
    *,
    responses: Sequence[FetchResponseContext] = (),
) -> str:
    """Compute the SHA-256 change-detection fingerprint of a fetch.

    Args:
        content: Extracted content to fingerprint
        mode: ``"content"`` hashes whitespace-normalized content only;
            ``"full"`` hashes the exact content together with ``responses``
        responses: Per-page fetch response context, in fetch order. Required
            (non-empty) for ``"full"``; ignored by ``"content"``.

    Returns:
        SHA-256 hex digest (64 characters)

    Raises:
        ValueError: ``mode`` is not a known fingerprint mode, or ``"full"`` was
            asked for without response context. Neither may fall through to a
            different hash and silently weaken change detection.
    """
    if mode == "content":
        return hashlib.sha256(normalize_for_fingerprint(content).encode("utf-8")).hexdigest()
    if mode != "full":
        raise ValueError(f"unknown fingerprint mode {mode!r}")
    if not responses:
        raise ValueError("full fingerprint requires the fetch response context")
    document = {
        "version": FULL_FINGERPRINT_VERSION,
        "content": content,
        "responses": [
            {
                "status_code": page.status_code,
                "final_url": page.final_url,
                "content_type": page.content_type,
                "body_sha256": page.body_sha256,
            }
            for page in responses
        ],
    }
    # ensure_ascii keeps lone surrogates in extracted text encodable.
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
