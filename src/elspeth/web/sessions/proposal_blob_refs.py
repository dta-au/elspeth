"""Closed proposal-to-blob reference contract and custody validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Connection

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.guided.protocol import BLOB_REF_PATH_PREFIX
from elspeth.web.composer.pipeline_proposal import is_owned_composition_state_authority
from elspeth.web.sessions.models import blobs_table, composition_proposals_table

_TOP_LEVEL_BLOB_TOOLS = frozenset(
    {
        "set_source_from_blob",
        "update_blob",
        "wire_blob_inline_ref",
    }
)
_BLOB_REFERENCE_TOOLS = _TOP_LEVEL_BLOB_TOOLS | {"set_pipeline"}

_BLOB_SENTINEL_OPTION_KEYS = frozenset({"path", "file"})


def _canonical_sentinel_blob_id(value: str, *, position: str) -> str:
    raw = value.removeprefix(BLOB_REF_PATH_PREFIX)
    try:
        parsed = UUID(raw)
    except ValueError as exc:
        raise ValueError(f"{position} carries a malformed blob sentinel") from exc
    if str(parsed) != raw:
        raise ValueError(f"{position} carries a non-canonical blob sentinel")
    return raw


def _collect_option_blob_references(value: Any, *, position: str, into: list[str]) -> None:
    """Recursively collect blob references from one tool-owned options value.

    Recognizes the three authoritative reference vocabularies that composer
    tooling can author into options: top-level or nested ``blob_ref`` marker
    values, and ``path``/``file`` values carrying the guided ``blob:<uuid>``
    sentinel.  A present-but-malformed reference raises ``ValueError`` — a
    proposal whose custody positions cannot be read must never silently shed
    its retention edges.
    """
    if type(value) is dict:
        for key, child in value.items():
            if key == "blob_ref" and child is not None:
                if type(child) is not str or not child:
                    raise ValueError(f"{position} blob_ref must be a non-empty string")
                into.append(child)
            elif key in _BLOB_SENTINEL_OPTION_KEYS and type(child) is str and child.startswith(BLOB_REF_PATH_PREFIX):
                into.append(_canonical_sentinel_blob_id(child, position=f"{position}.{key}"))
            else:
                _collect_option_blob_references(child, position=f"{position}.{key}", into=into)
        return
    if type(value) is list:
        for index, child in enumerate(value):
            _collect_option_blob_references(child, position=f"{position}[{index}]", into=into)


def _collect_component_options_references(
    component: Any,
    *,
    position: str,
    into: list[str],
) -> None:
    if type(component) is not dict:
        raise ValueError(f"{position} must be a mapping")
    options = component["options"] if "options" in component else None
    if options is None:
        return
    if type(options) is not dict:
        raise ValueError(f"{position} options must be a mapping")
    _collect_option_blob_references(options, position=f"{position}.options", into=into)


def proposal_blob_reference_ids(tool_name: str, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """Extract authoritative blob references from the closed tool allowlist.

    This deliberately does not scan arbitrary proposal JSON: only tool-owned
    schema positions can create a custody/retention edge.  Within
    ``set_pipeline`` those positions are the source/node/output ``options``
    mappings, whose blob vocabulary (``blob_ref`` markers and ``blob:<uuid>``
    path/file sentinels) is walked recursively — guided proposals bind their
    reviewed blobs through exactly these positions, and missing them let a
    reviewed blob be deleted while a proposal depending on it was pending
    (elspeth-b3feba9a7c).
    """
    if tool_name not in _BLOB_REFERENCE_TOOLS:
        return ()

    if tool_name == "set_pipeline":
        collected: list[str] = []
        if is_owned_composition_state_authority(arguments):
            sources = arguments["sources"] if "sources" in arguments else None
            if type(sources) is not dict:
                raise ValueError("owned composition-state sources must be a mapping")
            for source_name, source in sources.items():
                if type(source_name) is not str or type(source) is not dict:
                    raise ValueError("owned composition-state sources must contain named mappings")
                options = source["options"] if "options" in source else None
                if type(options) is not dict:
                    raise ValueError(f"owned composition-state source {source_name!r} options must be a mapping")
                _collect_option_blob_references(options, position=f"sources[{source_name!r}].options", into=collected)
            for collection_name in ("nodes", "outputs"):
                collection = arguments[collection_name] if collection_name in arguments else None
                if collection is None:
                    continue
                if type(collection) is not list:
                    raise ValueError(f"owned composition-state {collection_name} must be a list")
                for index, component in enumerate(collection):
                    _collect_component_options_references(
                        component,
                        position=f"{collection_name}[{index}]",
                        into=collected,
                    )
            return tuple(dict.fromkeys(collected))
        source = arguments["source"] if "source" in arguments else None
        if source is None:
            return ()
        if type(source) is not dict:
            raise ValueError("set_pipeline source must be a mapping when present")
        blob_id = source["blob_id"] if "blob_id" in source else None
        if blob_id is not None:
            if type(blob_id) is not str or not blob_id:
                raise ValueError("set_pipeline source.blob_id must be a non-empty string when present")
            collected.append(blob_id)
        options = source["options"] if "options" in source else None
        if options is not None:
            if type(options) is not dict:
                raise ValueError("set_pipeline source.options must be a mapping when present")
            _collect_option_blob_references(options, position="source.options", into=collected)
        return tuple(dict.fromkeys(collected))

    value = arguments["blob_id"] if "blob_id" in arguments else None
    if value is None:
        return ()
    if type(value) is not str or not value:
        raise ValueError(f"{tool_name} blob_id must be a non-empty string when present")
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


def pending_proposal_reference_id(
    conn: Connection,
    *,
    session_id: str,
    blob_id: str,
    exclude_proposal_id: str | None = None,
) -> str | None:
    """Return the pending proposal retaining a blob, if any.

    ``exclude_proposal_id`` names the proposal currently being executed by
    a proposal-accept replay: its own retention edge must not block the
    very mutation it authorizes, while every other pending proposal still
    does.
    """
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
        if exclude_proposal_id is not None and str(proposal_id) == exclude_proposal_id:
            continue
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
