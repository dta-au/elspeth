"""The one on-disk envelope rule for ``composition_states`` JSON columns.

Every JSON column of ``composition_states`` (``source``, ``sources``,
``nodes``, ``edges``, ``outputs``, ``metadata_``, ``composer_meta``) is stored
as ``{"_version": 1, "data": <raw>}``; the ``_version`` field is reserved for
schema evolution. Writers wrap through :func:`envelope_state_column` and every
reader — the session service, the coordination repository, and the blob
layer's active-run guard — unwraps through :func:`unwrap_state_column`. A
reader that consumed the column raw would misread every production row (the
blob active-run guard did exactly that before this module existed), so the
rule lives in one place and a non-envelope value is a Tier-1 integrity
failure, never a tolerated legacy shape.
"""

from __future__ import annotations

from typing import Any, TypedDict

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw

STATE_COLUMN_ENVELOPE_VERSION = 1


class StateColumnEnvelope(TypedDict):
    """The stored shape of one composition_states JSON column."""

    _version: int
    data: Any


def envelope_state_column(value: Any) -> StateColumnEnvelope | None:
    """Wrap one raw column value in the versioned envelope (``None`` stays ``None``).

    ``deep_thaw()`` handles ``MappingProxyType``/``frozenset``/tuple unwrap from
    ``freeze_fields()`` so the stored JSON is plain containers.
    """
    raw = deep_thaw(value)
    if raw is None:
        return None
    return {"_version": STATE_COLUMN_ENVELOPE_VERSION, "data": raw}


def unwrap_state_column(value: Any) -> Any:
    """Return the raw value inside one stored envelope (``None`` stays ``None``)."""
    if value is None:
        return None
    if type(value) is not dict or "_version" not in value:
        raise AuditIntegrityError("composition state column has no version envelope; write-path defect or database corruption")
    if value["_version"] != STATE_COLUMN_ENVELOPE_VERSION or "data" not in value:
        raise AuditIntegrityError(f"composition state column envelope version {value['_version']!r} is not supported")
    return value["data"]
