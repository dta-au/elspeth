"""Composer-domain canonicalization for authority-bearing pipeline payloads.

RFC 8785 sorts object keys, while the mapping form of ``row_union.branches``
and ``coalesce.branches`` uses insertion order as authored runtime semantics:
row_union releases rows in branch order, and a coalesce merges (and, under
the default ``union_collision_policy=last_wins``, resolves field collisions)
in branch order.  Composer authority hashes therefore project those fields
into an explicitly ordered pair array, tagged with one schema per node type,
before canonicalizing.  The projection is detached: persisted and executed
pipeline payloads retain their public mapping shape.

Order inside a stored dispatch envelope is bound only by the envelope's own
hash.  Both restore callers compare the restored payload's RFC 8785 canonical
(which sorts keys) with the stored generic canonical and re-project it, so a
reorder of the projected ``items`` with a recomputed envelope hash is not
detected there.  Order is bound where a stored hash is compared with one
recomputed from an order-preserving copy of the arguments.

A real branch map whose keys are literally ``schema`` and ``items`` looks like
a projection.  That ambiguity is latent and harmless: restore runs only on
stored projections, and such a map is itself projected before it is stored.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final, cast

from pydantic import JsonValue

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import canonical_json, stable_hash

_ORDERED_BRANCH_SCHEMAS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "row_union": "composer.row-union-ordered-branches.v1",
        "coalesce": "composer.coalesce-ordered-branches.v1",
    }
)
_ORDERED_BRANCH_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "row_union": "row-union",
        "coalesce": "coalesce",
    }
)


def _project_ordered_pairs(mapping: dict[str, JsonValue], schema: str) -> dict[str, JsonValue]:
    return {
        "schema": schema,
        "items": [[key, value] for key, value in mapping.items()],
    }


def _restore_ordered_pairs(
    projection: dict[str, JsonValue],
    *,
    schema: str,
    malformed: str,
    item_malformed: str,
) -> dict[str, JsonValue]:
    """Invert ``_project_ordered_pairs``, checking only the projection's structure.

    The value position accepts any JSON value: a planner argument error is
    still projected and persisted, and value integrity is enforced by the
    callers' comparison with the stored generic canonical.
    """
    if set(projection) != {"schema", "items"}:
        raise ValueError(malformed)
    items = projection["items"]
    if projection["schema"] != schema or type(items) is not list:
        raise ValueError(malformed)
    restored: dict[str, JsonValue] = {}
    for item in items:
        if type(item) is not list or len(item) != 2 or type(item[0]) is not str or item[0] in restored:
            raise ValueError(item_malformed)
        restored[item[0]] = item[1]
    return restored


def project_composer_authority_payload(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Return a detached hash projection with ordered row-union and coalesce branches.

    Only top-level pipeline ``nodes`` are inspected.  This avoids assigning
    Composer topology meaning to plugin-owned nested dictionaries that happen
    to contain similarly named fields.  A ``node_type`` that is not a string
    passes through unprojected: projection runs on raw planner arguments
    before any schema gate.
    """
    projected = cast(dict[str, JsonValue], deep_thaw(payload))
    if type(projected) is not dict:
        raise TypeError("Composer authority payload must thaw to a dict")

    nodes = projected["nodes"] if "nodes" in projected else None
    if type(nodes) is not list:
        return projected

    for node in nodes:
        if type(node) is not dict or "node_type" not in node:
            continue
        node_type = node["node_type"]
        if type(node_type) is not str or node_type not in _ORDERED_BRANCH_SCHEMAS:
            continue
        branches = node["branches"] if "branches" in node else None
        if type(branches) is not dict:
            continue
        node["branches"] = _project_ordered_pairs(branches, _ORDERED_BRANCH_SCHEMAS[node_type])
    return projected


def restore_composer_authority_payload(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Restore the exact Composer tool shape from an authority projection.

    The ordered pair array is validated strictly before it becomes a mapping:
    a plain map where a projection must be, a missing or foreign schema tag,
    extra envelope keys, malformed entries and duplicate keys fail closed.
    Branch values may be any JSON value.  The restored mapping retains the
    pair-array order for the redaction pass that follows.
    """
    restored = cast(dict[str, JsonValue], deep_thaw(payload))
    if type(restored) is not dict:
        raise ValueError("Composer authority projection must thaw to a dict")

    nodes = restored["nodes"] if "nodes" in restored else None
    if type(nodes) is not list:
        return restored

    for node in nodes:
        if type(node) is not dict or "node_type" not in node:
            continue
        node_type = node["node_type"]
        if type(node_type) is not str or node_type not in _ORDERED_BRANCH_SCHEMAS:
            continue
        branches = node["branches"] if "branches" in node else None
        if type(branches) is not dict:
            continue
        label = _ORDERED_BRANCH_LABELS[node_type]
        node["branches"] = _restore_ordered_pairs(
            branches,
            schema=_ORDERED_BRANCH_SCHEMAS[node_type],
            malformed=f"{label} authority projection branches are malformed",
            item_malformed=f"{label} authority projection branch item is malformed",
        )
    return restored


def composer_authority_canonical_json(payload: Mapping[str, Any]) -> str:
    """Canonicalize a Composer authority payload under its semantic projection."""
    return canonical_json(project_composer_authority_payload(payload))


def composer_authority_hash(payload: Mapping[str, Any]) -> str:
    """Hash a Composer authority payload under its semantic projection."""
    return stable_hash(project_composer_authority_payload(payload))
