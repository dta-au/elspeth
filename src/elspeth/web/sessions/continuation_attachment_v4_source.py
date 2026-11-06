"""Complete retained attachment DATA after the value-batch SQL source proof.

This reader never opens a file and never authorizes another physical read.
The caller must bind the source chunk to its original value-batch receipt;
matching a payload alone is not a current execution grant.
"""

from __future__ import annotations

from hashlib import sha256

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import PrivateChunk, verify_private_chunk
from elspeth.web.composer.continuation_tool_batch import decode_tool_result_json
from elspeth.web.composer.state import CompositionState
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.sessions.continuation import read_chunk
from elspeth.web.sessions.continuation_attachment_v4_bound import V4AttachmentReadBound, require_v4_attachment_read_bound
from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result, require_read_versions_on_connection
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_checkpoints_table as checkpoints
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts


def read_v4_attachment_retained_value_on_connection(
    conn: Connection, *, bound: V4AttachmentReadBound, source_chunk: PrivateChunk, pre_state: CompositionState
) -> ToolResult:
    """Prove original source/actor/fence/version and decode without rereading."""
    if type(bound) is not V4AttachmentReadBound or type(source_chunk) is not PrivateChunk or type(pre_state) is not CompositionState:
        raise TypeError("retained V4 attachment read requires owned bound/source/state")
    require_v4_attachment_read_bound(bound, pre_state=pre_state)
    if source_chunk.reference.codec != "composer.owner-result.v1":
        raise AuditIntegrityError("retained V4 attachment read changed its original source codec")
    verify_private_chunk(source_chunk.reference, source_chunk.canonical)
    retained = decode_read_owner_result(source_chunk.canonical, env_ref_names=bound.names)
    if (
        len(source_chunk.canonical.encode("utf-8")) > bound.maximum_owner_result_bytes
        or len(retained.response_json.encode("utf-8")) > bound.maximum_response_bytes
        or len(retained.tool_result_json.encode("utf-8")) > bound.maximum_tool_result_bytes
        or retained.tool_name != bound.tool_name
        or retained.arguments_json != bound.arguments_json
        or retained.blob_versions != (bound.source,)
        or canonical_json(pre_state.to_dict()) != bound.state_json
        or any(
            item.session_id != bound.session_id
            or item.blob_id != str(bound.source.blob_id)
            or item.content_hash != bound.source.content_hash
            or item.size_bytes != bound.source.size_bytes
            for item in retained.attachment_inspections
        )
        or len(retained.attachment_inspections) > 1
    ):
        raise AuditIntegrityError("retained V4 attachment read changed its complete original value/source/bound")
    parent = conn.execute(
        select(jobs.c.actor_user_id).where(jobs.c.session_id == bound.session_id, jobs.c.operation_id == bound.operation_id)
    ).one_or_none()
    original = conn.execute(
        select(checkpoints.c.cursor, checkpoints.c.snapshot_chunk_key).where(
            checkpoints.c.session_id == bound.session_id,
            checkpoints.c.operation_id == bound.operation_id,
            checkpoints.c.checkpoint_id == bound.selected_checkpoint_id,
        )
    ).one_or_none()
    creator = conn.execute(
        select(attempts).where(
            attempts.c.session_id == bound.session_id,
            attempts.c.operation_id == bound.operation_id,
            attempts.c.sequence == bound.creator_sequence,
            attempts.c.generation == bound.creator_generation,
        )
    ).one_or_none()
    if (
        parent is None
        or parent.actor_user_id != bound.actor_user_id
        or original is None
        or original.cursor != bound.selected_cursor
        or creator is None
        or creator.claim_attempt != bound.claim_attempt
        or creator.session_operation_id != bound.session_operation_id
        or creator.session_operation_epoch != bound.session_operation_epoch
        or sha256(creator.session_operation_lease_token.encode("utf-8")).hexdigest() != bound.lease_sha256
    ):
        raise AuditIntegrityError("retained V4 attachment read lost its original actor/checkpoint/fence")
    original_payload = read_chunk(conn, session_id=bound.session_id, operation_id=bound.operation_id, key=original.snapshot_chunk_key)
    if sha256(original_payload).hexdigest() != bound.selected_snapshot_sha256:
        raise AuditIntegrityError("retained V4 attachment read changed its original selected root")
    require_read_versions_on_connection(conn, session_id=bound.session_id, result=retained, env_ref_names=bound.names)
    result = decode_tool_result_json(retained.tool_result_json, pre_state=pre_state, env_ref_names=bound.names)
    if (
        result.updated_state is not pre_state
        or result._validation_snapshot_hash != bound.catalog.snapshot.snapshot_hash
        or (result.success and len(retained.attachment_inspections) != 1)
    ):
        raise AuditIntegrityError("retained V4 attachment value lost its actual result/prestate/inspection")
    return result
