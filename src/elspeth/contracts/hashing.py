"""Canonical hashing for the contracts layer.

Provides canonical JSON serialization (RFC 8785/JCS) and stable hashing
for data that contains JSON-safe primitives and their frozen equivalents.
Frozen container types produced by ``deep_freeze`` (``MappingProxyType``,
``tuple``) are normalized to their mutable equivalents before serialization.

This module exists to break the circular dependency between contracts/
and core/canonical.py. For data containing pandas/numpy types or
PipelineRow, use elspeth.core.canonical instead — it adds a normalization
phase for domain-specific types before delegating to rfc8785.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from typing import Any, TypeGuard

import rfc8785

from elspeth.contracts.trust_boundary import trust_boundary

# Version string stored with every run for hash verification.
# Single source of truth — core/canonical.py imports this constant.
CANONICAL_VERSION = "sha256-rfc8785-v1"
_LOWER_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def is_lower_sha256_hex(value: object) -> TypeGuard[str]:
    """Return whether ``value`` is exactly one lowercase SHA-256 hex digest."""
    return isinstance(value, str) and _LOWER_SHA256_HEX_RE.fullmatch(value) is not None


def _normalize_frozen_and_reject_non_finite(obj: Any) -> Any:
    """Normalize frozen containers and reject non-finite floats.

    Single recursive traversal that:
    - Converts any ``Mapping`` (including ``MappingProxyType``) → ``dict``
    - Converts ``tuple`` → ``list``
    - Rejects ``frozenset`` with ``TypeError`` (no canonical JSON ordering)
    - Rejects NaN/Infinity with ``ValueError``
    - Returns the normalized structure ready for ``rfc8785.dumps()``
    """
    if isinstance(obj, float):
        if math.isnan(obj):
            raise ValueError(f"Cannot canonicalize NaN. Use None for missing values, not NaN. Got: {obj!r}")
        if math.isinf(obj):
            raise ValueError(f"Cannot canonicalize Infinity. Use None for missing values, not Infinity. Got: {obj!r}")
        return obj
    if isinstance(obj, frozenset):
        # Type only, never the set's repr: its members can be row values, and
        # this text reaches audit records through every hashing seam.
        raise TypeError("frozenset is not JSON-serializable and has no canonical ordering. Use list or tuple for ordered collections.")
    # Mapping ABC covers both dict and MappingProxyType. The dict
    # comprehension normalizes MappingProxyType → dict for rfc8785.
    if isinstance(obj, Mapping):
        return {k: _normalize_frozen_and_reject_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalize_frozen_and_reject_non_finite(item) for item in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Produce canonical JSON per RFC 8785/JCS.

    Handles JSON-safe primitives and their frozen equivalents
    (``MappingProxyType`` → ``dict``, ``tuple`` → ``list``).
    For data containing pandas/numpy types or PipelineRow, use
    ``elspeth.core.canonical.canonical_json()`` instead.

    Args:
        obj: JSON-safe data structure, optionally containing frozen containers

    Returns:
        Canonical JSON string (deterministic key order, no whitespace)

    Raises:
        ValueError: If data contains NaN or Infinity
        TypeError: If data contains frozenset or other non-serializable types
    """
    normalized = _normalize_frozen_and_reject_non_finite(obj)
    result: bytes = rfc8785.dumps(normalized)
    return result.decode("utf-8")


# The largest magnitude an integer may have in canonical JSON: rfc8785 refuses
# any int beyond it (IntegerDomainError), so it never writes one.
_CANONICAL_SAFE_INTEGER_MAX = 2**53 - 1


def _parse_canonical_integer_literal(literal: str) -> int | float:
    value = int(literal)
    if -_CANONICAL_SAFE_INTEGER_MAX <= value <= _CANONICAL_SAFE_INTEGER_MAX:
        return value
    # Beyond the safe range only a double can have produced this literal:
    # RFC 8785 writes an integral double below 1e21 in integer notation
    # (1e17 -> 100000000000000000), and the encoder refuses such ints.
    # The literal is that double's shortest round-trip form, so float() of
    # it is exactly the value that was encoded.
    return float(literal)


def _refuse_non_finite_literal(literal: str) -> Any:
    # The literal is one of NaN / Infinity / -Infinity — never row data.
    raise json.JSONDecodeError(f"non-finite JSON constant {literal!r} is not canonical JSON", literal, 0)


def canonical_json_loads(text: str | bytes) -> Any:
    """Parse text that ``canonical_json`` produced back into the value it encoded.

    The one inverse of the encoder, for every reader of canonical row
    material (the sink-effect member rows, their payload-store content, the
    durable sink-effect plan whose ``safe_evidence`` can carry member rows, a
    source row's stored payload, a validation error's ``row_data_json``).
    A plain ``json.loads`` is not an inverse: RFC 8785 serializes numbers as
    IEEE 754 doubles, printing an integral double in [2**53, 1e21) in integer
    notation, so ``json.loads`` reads it back as an ``int`` the canonical
    encoder itself refuses — the round trip of a valid row then fails.

    Integer literals inside ±(2**53-1) stay ``int``: the encoder writes an
    int and an integral double there identically (``5`` and ``5.0`` both as
    ``5``), and ``int`` is the established reading of that text. Outside the
    safe range the literal can only have come from a double, so it is read
    back as that double. NaN / Infinity never appear in canonical JSON and
    are refused as a decode error.
    """
    return json.loads(text, parse_int=_parse_canonical_integer_literal, parse_constant=_refuse_non_finite_literal)


def stable_hash(obj: Any) -> str:
    """Compute SHA-256 hash of canonical JSON for primitive data.

    For data containing pandas/numpy types or PipelineRow, use
    ``elspeth.core.canonical.stable_hash()`` instead.

    Args:
        obj: JSON-safe data structure (no pandas/numpy types)

    Returns:
        SHA-256 hex digest of canonical JSON
    """
    canonical = canonical_json(obj)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@trust_boundary(
    tier=3,
    source=(
        "already-malformed external data on the repr_hash quarantine fallback path — arbitrary Python "
        "values that failed canonical_json (NaN, Infinity, non-serializable types)"
    ),
    source_param="obj",
    suppresses=("R5",),
    invariant=(
        "returns a deterministic repr string for every input — unordered containers are sorted, anything "
        "unrecognized falls through to repr(obj); never raises on malformed input"
    ),
    non_raising=True,
)
def _stable_repr(obj: Any) -> str:
    """Produce a deterministic repr by sorting unordered containers.

    Dicts are sorted by key, sets/frozensets are sorted by repr of elements.
    Applied recursively so nested containers are also deterministic.
    """
    if isinstance(obj, dict):
        items = ", ".join(f"{_stable_repr(k)}: {_stable_repr(v)}" for k, v in sorted(obj.items(), key=lambda kv: repr(kv[0])))
        return "{" + items + "}"
    if isinstance(obj, (set, frozenset)):
        items = ", ".join(sorted(_stable_repr(e) for e in obj))
        prefix = "frozenset" if isinstance(obj, frozenset) else ""
        return f"{prefix}{{{items}}}" if items else f"{prefix}()"
    if isinstance(obj, (list, tuple)):
        items = ", ".join(_stable_repr(e) for e in obj)
        if isinstance(obj, tuple):
            return f"({items},)" if len(obj) == 1 else f"({items})"
        return f"[{items}]"
    return repr(obj)


def repr_hash(obj: Any) -> str:
    """Generate SHA-256 hash of repr() for non-canonical data.

    Used as fallback when canonical_json fails (NaN, Infinity, or other
    non-serializable types). Deterministic within the same Python version
    but NOT stable across versions due to repr() implementation differences.

    Sorts dict keys and set elements before repr() to ensure deterministic
    hashes regardless of insertion order.

    Appropriate for Tier-3 (external data) trust boundary where data is
    already malformed and being quarantined.

    Args:
        obj: Any Python object

    Returns:
        SHA-256 hex digest of stable repr(obj)
    """
    return hashlib.sha256(_stable_repr(obj).encode("utf-8")).hexdigest()
