"""Shared assertions for audit hash binding tests."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

from elspeth.core.canonical import canonical_json, stable_hash


def fake_sha256(label: str) -> str:
    """Return a real lowercase SHA-256 hex digest derived from a readable label.

    The audit stores CHECK the shape of every digest column, so a placeholder
    such as ``"abc123"`` is a value no producer emits and no store admits. Use
    this where a test needs *a* digest and the bytes behind it do not matter:
    the label keeps the call site legible and equal labels give equal digests.
    """
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def fake_error_hash(label: str) -> str:
    """Return a 16-character audit error fingerprint derived from a label."""
    return fake_sha256(label)[:16]


def fake_prefixed_sha256(label: str) -> str:
    """Return a ``sha256:<hex>`` reference derived from a label."""
    return f"sha256:{fake_sha256(label)}"


def canonical_sha256_hex(payload: Any) -> str:
    """Return SHA-256 over ELSPETH canonical JSON bytes."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def assert_stable_hash(actual_hash: str | None, payload: Any) -> None:
    """Assert an audit hash is bound to the exact canonical payload."""
    assert actual_hash == stable_hash(payload)


def assert_prefixed_canonical_sha256(actual_hash: str, payload: Any, *, prefix: str = "sha256:") -> None:
    """Assert a prefixed SHA-256 digest is bound to canonical JSON bytes."""
    assert actual_hash == f"{prefix}{canonical_sha256_hex(payload)}"


def assert_hmac_sha256_hex(actual_signature: str, key: bytes, payload: Any) -> None:
    """Assert a hex HMAC-SHA256 signature is bound to canonical JSON bytes."""
    canonical = canonical_json(payload).encode("utf-8")
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    assert actual_signature == expected
