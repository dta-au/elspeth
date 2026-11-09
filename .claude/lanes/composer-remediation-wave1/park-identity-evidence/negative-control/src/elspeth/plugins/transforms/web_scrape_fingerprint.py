"""Fingerprinting utilities for web scraping change detection."""

import hashlib
import re


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


def compute_fingerprint(content: str, mode: str) -> str:
    """Compute SHA-256 fingerprint of content.

    Args:
        content: Content to fingerprint
        mode: Fingerprint mode ("content", "full", "structure")

    Returns:
        SHA-256 hex digest (64 characters)
    """
    if mode == "content":
        content = normalize_for_fingerprint(content)
    elif mode == "structure":
        raise NotImplementedError("Structure mode not yet implemented")
    # mode == "full" uses raw content as-is

    return hashlib.sha256(content.encode("utf-8")).hexdigest()
