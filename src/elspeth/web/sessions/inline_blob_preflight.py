"""Immutable inline-blob evidence prepared before a session write transaction."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypedDict, cast
from uuid import UUID

from sqlalchemy import Connection, select

from elspeth.contracts.blobs import BlobNotFoundError, BlobRecord, BlobStateError
from elspeth.contracts.blobs_inline import BlobContentResolutionError, BlobInlineRef
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.blobs_inline import (
    BLOB_INLINE_AGGREGATE_BYTE_CAP,
    BLOB_INLINE_PER_REF_BYTE_CAP,
    _discover_blob_content_refs,
)
from elspeth.web.sessions.models import blobs_table
from elspeth.web.sessions.protocol import CompositionStateData, CompositionStateRecord

if TYPE_CHECKING:
    from elspeth.web.composer.state import CompositionState


@dataclass(frozen=True, slots=True)
class InlinePreflightState:
    sources: object
    nodes: object
    outputs: object

    @classmethod
    def from_stored_state(cls, state: CompositionStateRecord | CompositionStateData) -> InlinePreflightState:
        return cls(
            sources=deep_thaw(state.sources) if state.sources is not None else {},
            nodes=deep_thaw(state.nodes) if state.nodes is not None else [],
            outputs=deep_thaw(state.outputs) if state.outputs is not None else [],
        )

    @classmethod
    def from_composition_state(cls, state: CompositionState) -> InlinePreflightState:
        data = state.to_dict()
        return cls(sources=data["sources"], nodes=data["nodes"], outputs=data["outputs"])


class _DiscoveryNode(TypedDict):
    name: str
    options: object


class _DiscoveryConfig(TypedDict):
    sources: object
    transforms: list[_DiscoveryNode]
    outputs: object


def _inline_discovery_config(state: InlinePreflightState) -> _DiscoveryConfig:
    """Project Composer's node IDs onto the runtime ref walk's name paths."""
    nodes = state.nodes if type(state.nodes) is list else []
    outputs = state.outputs if type(state.outputs) is list else []
    projected_nodes: list[_DiscoveryNode] = [
        _DiscoveryNode(name=node["id"], options=node["options"])
        for node in nodes
        if type(node) is dict and type(node.get("id")) is str and type(node.get("options")) is dict
    ]
    projected_outputs = {
        output["name"]: {"options": output["options"]}
        for output in outputs
        if type(output) is dict and type(output.get("name")) is str and type(output.get("options")) is dict
    }
    return {
        "sources": state.sources,
        "transforms": projected_nodes,
        "outputs": projected_outputs,
    }


@dataclass(frozen=True, slots=True)
class SessionInlineBlobSnapshot:
    """Only verified byte pairs and marker identities; no service or DB handle."""

    refs: frozenset[BlobInlineRef]
    records: Mapping[UUID, tuple[BlobRecord, bytes]] = field(repr=False)

    def content(self, blob_id: UUID) -> tuple[BlobRecord, bytes]:
        return self.records[blob_id]

    def assert_covers(self, config: InlinePreflightState) -> bool:
        """A changed or newly introduced marker must not reuse prior bytes."""
        return set(_discover_blob_content_refs(cast(dict[str, Any], _inline_discovery_config(config)))).issubset(self.refs)

    def assert_current_rows(self, conn: Connection, *, session_id: UUID) -> None:
        """Recheck metadata on the existing SESSIONS connection, without custody I/O."""
        for blob_id, (record, _content) in self.records.items():
            row = conn.execute(
                select(
                    blobs_table.c.session_id,
                    blobs_table.c.status,
                    blobs_table.c.content_hash,
                    blobs_table.c.size_bytes,
                    blobs_table.c.filename,
                    blobs_table.c.mime_type,
                    blobs_table.c.storage_path,
                    blobs_table.c.created_by,
                    blobs_table.c.source_description,
                    blobs_table.c.creation_modality,
                    blobs_table.c.created_from_message_id,
                    blobs_table.c.creating_model_identifier,
                    blobs_table.c.creating_model_version,
                    blobs_table.c.creating_provider,
                    blobs_table.c.creating_composer_skill_hash,
                    blobs_table.c.creating_arguments_hash,
                ).where(blobs_table.c.id == str(blob_id))
            ).one_or_none()
            if (
                row is None
                or row.session_id != str(session_id)
                or row.status != record.status
                or row.content_hash != record.content_hash
                or row.size_bytes != record.size_bytes
                or row.filename != record.filename
                or row.mime_type != record.mime_type
                or row.storage_path != record.storage_path
                or row.created_by != record.created_by
                or row.source_description != record.source_description
                or row.creation_modality != record.creation_modality.value
                or row.created_from_message_id != record.created_from_message_id
                or row.creating_model_identifier != record.creating_model_identifier
                or row.creating_model_version != record.creating_model_version
                or row.creating_provider != record.creating_provider
                or row.creating_composer_skill_hash != record.creating_composer_skill_hash
                or row.creating_arguments_hash != record.creating_arguments_hash
            ):
                raise AuditIntegrityError("inline blob changed after preflight snapshot")


def prepare_session_inline_blob_snapshot(
    config: InlinePreflightState,
    *,
    session_id: UUID,
    read_blob: Callable[[UUID], tuple[BlobRecord, bytes]],
    metadata_hint: Callable[[UUID], tuple[str, str | None, int] | None] | None = None,
) -> SessionInlineBlobSnapshot | None:
    try:
        refs = _discover_blob_content_refs(cast(dict[str, Any], _inline_discovery_config(config)))
    except BlobContentResolutionError:
        # Runtime validation reports the malformed field in blob_inline_refs.
        return None
    if not refs:
        return None
    records: dict[UUID, tuple[BlobRecord, bytes]] = {}
    aggregate = 0
    declared_aggregate = 0
    for ref in refs:
        if ref.blob_id in records:
            continue
        if metadata_hint is not None:
            hint = metadata_hint(ref.blob_id)
            if hint is None or hint[0] != "ready" or hint[1] != ref.sha256 or hint[2] > BLOB_INLINE_PER_REF_BYTE_CAP:
                continue
            declared_aggregate += hint[2]
            if declared_aggregate > BLOB_INLINE_AGGREGATE_BYTE_CAP:
                break
        try:
            record, content = read_blob(ref.blob_id)
        except (BlobNotFoundError, BlobStateError):
            # Metadata admission in validate_pipeline will produce the
            # field-scoped refusal. Integrity/fence failures still propagate.
            continue
        if record.session_id != session_id:
            raise AuditIntegrityError("inline blob snapshot crossed a session boundary")
        if record.size_bytes > BLOB_INLINE_PER_REF_BYTE_CAP or len(content) > BLOB_INLINE_PER_REF_BYTE_CAP:
            continue
        aggregate += len(content)
        if aggregate > BLOB_INLINE_AGGREGATE_BYTE_CAP:
            break
        if record.content_hash is None or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), record.content_hash):
            raise AuditIntegrityError("inline blob snapshot metadata disagrees with verified bytes")
        records[ref.blob_id] = (record, content)
    return SessionInlineBlobSnapshot(refs=frozenset(refs), records=MappingProxyType(records))
