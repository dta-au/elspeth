"""Closed proposal-to-blob reference contract and custody validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.models import blobs_table, composition_proposals_table

_TOP_LEVEL_BLOB_TOOLS = frozenset(
    {
        "set_source_from_blob",
        "update_blob",
        "wire_blob_inline_ref",
    }
)
_BLOB_REFERENCE_TOOLS = _TOP_LEVEL_BLOB_TOOLS | {"set_pipeline"}
_PIPELINE_BLOB_PATH_KEYS = ("path", "file")


def _pipeline_blob_reference_ids(arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract blob custody from legacy and guided pipeline source shapes."""
    references: list[str] = []
    source = arguments["source"] if "source" in arguments else None
    if source is not None:
        if type(source) is not dict:
            raise ValueError("set_pipeline source must be a mapping when present")
        value = source["blob_id"] if "blob_id" in source else None
        if value is not None:
            if type(value) is not str or not value:
                raise ValueError("set_pipeline source.blob_id must be a non-empty string when present")
            references.append(value)

    sources = arguments["sources"] if "sources" in arguments else None
    if sources is None:
        return tuple(references)
    if type(sources) is not dict:
        raise ValueError("set_pipeline sources must be a mapping when present")
    for source_name, guided_source in sources.items():
        if type(source_name) is not str or not source_name or type(guided_source) is not dict:
            raise ValueError("set_pipeline sources must contain non-empty string names and source mappings")
        options = guided_source["options"] if "options" in guided_source else None
        if options is None:
            continue
        if type(options) is not dict:
            raise ValueError(f"set_pipeline sources[{source_name!r}].options must be a mapping when present")
        source_references: set[str] = set()
        if "blob_ref" in options:
            blob_ref = options["blob_ref"]
            if type(blob_ref) is not str or not blob_ref:
                raise ValueError(
                    f"set_pipeline sources[{source_name!r}].options.blob_ref must be a non-empty string when present"
                )
            source_references.add(blob_ref)
        for key in _PIPELINE_BLOB_PATH_KEYS:
            value = options[key] if key in options else None
            if type(value) is str and value.startswith("blob:"):
                blob_id = value.removeprefix("blob:")
                if not blob_id:
                    raise ValueError(
                        f"set_pipeline sources[{source_name!r}].options.{key} blob sentinel must contain a blob id"
                    )
                source_references.add(blob_id)
        if len(source_references) > 1:
            raise ValueError(f"set_pipeline sources[{source_name!r}] blob custody fields disagree")
        for blob_id in source_references:
            if blob_id not in references:
                references.append(blob_id)
    return tuple(references)


def proposal_blob_reference_ids(tool_name: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract authoritative blob references from the closed tool allowlist.

    This deliberately does not recursively scan arbitrary proposal JSON. Only
    tool-owned schema positions can create a custody/retention edge.
    """
    if tool_name not in _BLOB_REFERENCE_TOOLS:
        return ()

    if tool_name == "set_pipeline":
        return _pipeline_blob_reference_ids(arguments)
    else:
        value = arguments["blob_id"] if "blob_id" in arguments else None
        field_name = "blob_id"

    if value is None:
        return ()
    if type(value) is not str or not value:
        raise ValueError(f"{tool_name} {field_name} must be a non-empty string when present")
    return (value,)


def validate_proposal_blob_references(
    conn: Connection,
    *,
    session_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> None:
    """Require every authoritative proposal blob to be owned and ready."""
    for blob_id in proposal_blob_reference_ids(tool_name, arguments):
        row = conn.execute(select(blobs_table.c.session_id, blobs_table.c.status).where(blobs_table.c.id == blob_id)).first()
        if row is None:
            raise ValueError(f"Proposal references blob {blob_id!r}, but that blob does not exist")
        if row.session_id != session_id:
            raise ValueError(f"Proposal references blob {blob_id!r}, but it is not owned by this session")
        if row.status != "ready":
            raise ValueError(f"Proposal references blob {blob_id!r}, but its status is {row.status!r}, not 'ready'")


def pending_proposal_reference_id(conn: Connection, *, session_id: str, blob_id: str) -> str | None:
    """Return the pending proposal retaining a blob, if any."""
    rows = conn.execute(
        select(
            composition_proposals_table.c.id,
            composition_proposals_table.c.tool_name,
            composition_proposals_table.c.arguments_json,
        ).where(
            composition_proposals_table.c.session_id == session_id,
            composition_proposals_table.c.status == "pending",
            composition_proposals_table.c.tool_name.in_(_BLOB_REFERENCE_TOOLS),
        )
    ).fetchall()
    for proposal_id, tool_name, arguments_json in rows:
        if type(arguments_json) is not dict:
            raise AuditIntegrityError(
                f"Tier 1: pending proposal {proposal_id} arguments_json is {type(arguments_json).__name__}, expected dict"
            )
        try:
            references = proposal_blob_reference_ids(tool_name, arguments_json)
        except ValueError as exc:
            raise AuditIntegrityError(f"Tier 1: pending proposal {proposal_id} has malformed blob authority: {exc}") from exc
        if blob_id in references:
            return str(proposal_id)
    return None
