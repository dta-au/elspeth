"""Fingerprinting utilities for web scraping change detection."""

import hashlib
import re
from typing import Literal


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


def compute_fingerprint(content: str, mode: Literal["content", "full"]) -> str:
    """Compute SHA-256 fingerprint of content.

    Args:
        content: Content to fingerprint
        mode: Fingerprint mode ("content" normalizes whitespace, "full" hashes raw content)

    Returns:
        SHA-256 hex digest (64 characters)

    Raises:
        ValueError: ``mode`` is not a known fingerprint mode. An unknown mode
            must not fall through to a different hash and break change detection.
    """
    if mode == "content":
        content = normalize_for_fingerprint(content)
    elif mode != "full":
        raise ValueError(f"unknown fingerprint mode {mode!r}")

    return hashlib.sha256(content.encode("utf-8")).hexdigest()
