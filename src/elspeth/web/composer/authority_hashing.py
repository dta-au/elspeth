"""Composer-domain canonicalization for authority-bearing pipeline payloads.

RFC 8785 sorts object keys, while ``row_union.branches`` uses mapping insertion
order as authored runtime semantics.  Composer authority hashes therefore
project that one field into an explicitly ordered array before canonicalizing.
The projection is detached: persisted and executed pipeline payloads retain
their public mapping shape.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pydantic import JsonValue

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import canonical_json, stable_hash

_ROW_UNION_BRANCH_ORDER_SCHEMA = "composer.row-union-ordered-branches.v1"


def project_composer_authority_payload(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Return a detached hash projection with ordered row-union branches.

    Only top-level pipeline ``nodes`` are inspected.  This avoids assigning
    Composer topology meaning to plugin-owned nested dictionaries that happen
    to contain similarly named fields.
    """
    projected = cast(dict[str, JsonValue], deep_thaw(payload))
    if type(projected) is not dict:
        raise TypeError("Composer authority payload must thaw to a dict")

    nodes = projected.get("nodes")
    if type(nodes) is not list:
        return projected

    for node in nodes:
        if type(node) is not dict or node.get("node_type") != "row_union":
            continue
        branches = node.get("branches")
        if type(branches) is not dict:
            continue
        node["branches"] = {
            "schema": _ROW_UNION_BRANCH_ORDER_SCHEMA,
            "items": [[alias, connection] for alias, connection in branches.items()],
        }
    return projected


def restore_composer_authority_payload(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Restore the exact Composer tool shape from an authority projection.

    The ordered pair array is validated strictly before it becomes a mapping:
    malformed entries and duplicate aliases fail closed. The restored mapping
    retains the pair-array order for the redaction pass that follows.
    """
    restored = cast(dict[str, JsonValue], deep_thaw(payload))
    if type(restored) is not dict:
        raise ValueError("Composer authority projection must thaw to a dict")

    nodes = restored.get("nodes")
    if type(nodes) is not list:
        return restored

    for node in nodes:
        if type(node) is not dict or node.get("node_type") != "row_union":
            continue
        branches = node.get("branches")
        if type(branches) is not dict:
            continue
        if set(branches) != {"schema", "items"}:
            raise ValueError("row-union authority projection branches are malformed")
        if branches["schema"] != _ROW_UNION_BRANCH_ORDER_SCHEMA or type(branches["items"]) is not list:
            raise ValueError("row-union authority projection branches are malformed")
        restored_branches: dict[str, JsonValue] = {}
        for item in branches["items"]:
            if (
                type(item) is not list
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
                or item[0] in restored_branches
            ):
                raise ValueError("row-union authority projection branch item is malformed")
            restored_branches[item[0]] = item[1]
        node["branches"] = restored_branches
    return restored


def composer_authority_canonical_json(payload: Mapping[str, Any]) -> str:
    """Canonicalize a Composer authority payload under its semantic projection."""
    return canonical_json(project_composer_authority_payload(payload))


def composer_authority_hash(payload: Mapping[str, Any]) -> str:
    """Hash a Composer authority payload under its semantic projection."""
    return stable_hash(project_composer_authority_payload(payload))
