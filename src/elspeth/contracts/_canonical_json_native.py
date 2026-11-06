"""Compatibility entrypoint for canonical JSON with native string quoting.

The public contracts serializer and this existing private entrypoint share a
pure normalized writer. Both keep the original normalizer as their one entry
traversal. Helper names remain explicit reexports of the actual implementation.
"""

from __future__ import annotations

from elspeth.contracts._canonical_json_writer import BytesIO as BytesIO
from elspeth.contracts._canonical_json_writer import _dumps as _dumps
from elspeth.contracts._canonical_json_writer import _native_string_bytes as _native_string_bytes
from elspeth.contracts._canonical_json_writer import _write as _write
from elspeth.contracts._canonical_json_writer import encode_basestring as encode_basestring
from elspeth.contracts._canonical_json_writer import rfc8785 as rfc8785
from elspeth.contracts.hashing import _normalize_frozen_and_reject_non_finite


def canonical_json_native_strings(value: object) -> str:
    """Return the existing canonical wire format with native string quoting."""
    normalized = _normalize_frozen_and_reject_non_finite(value)
    return _dumps(normalized).decode("utf-8")
