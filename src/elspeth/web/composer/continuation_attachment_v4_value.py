"""One original attachment read after the value-batch owner reserved its tail.

The caller owns preissue reservation. This leaf rechecks the original source
bound, invokes the existing custody/credential reader once, and supplies its
actual result and immutable source DATA. It never commits or issues an effect.
"""

from __future__ import annotations

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_attachment_read import read_current_attachment
from elspeth.web.composer.continuation_codec import encode_private_chunk
from elspeth.web.composer.continuation_env_inventory import materialize_continuation_env_inventory
from elspeth.web.composer.continuation_runtime import _registered_read
from elspeth.web.composer.continuation_value_batch_preparation import PreparedValueBatchCall, ValueBatchComputation
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.required_work import RequiredWorkBinding
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation_attachment_v4_bound import (
    V4AttachmentReadBound,
    prepare_v4_attachment_read_bound_on_connection,
)
from elspeth.web.sessions.continuation_attachment_v4_source import read_v4_attachment_retained_value_on_connection
from elspeth.web.sessions.locking import locked_session_transaction


async def read_v4_attachment_value(prepared_value_call: PreparedValueBatchCall, bound: V4AttachmentReadBound) -> ValueBatchComputation:
    """Return actual producer DATA; no current approval or durable issue inferred."""
    if type(prepared_value_call) is not PreparedValueBatchCall or type(bound) is not V4AttachmentReadBound:
        raise TypeError("V4 attachment value requires its original prepared call and source bound")
    runtime, call = prepared_value_call.runtime, prepared_value_call.call
    work = runtime.bindings.required_work
    if type(work) is not RequiredWorkBinding or type(work.running) is not ComposerOperationRunning or call.arguments is None:
        raise AuditIntegrityError("V4 attachment value lost its original durable call/work")
    running, arguments = work.running, call.arguments
    selected = runtime.source.selected
    service, authority = runtime.materialization.service, runtime.materialization.authority
    inventory = await materialize_continuation_env_inventory(
        advisor=service._advisor_checkpoint, authority=authority, selected=selected, required_work=work
    )

    def recheck() -> V4AttachmentReadBound:
        with locked_session_transaction(authority._engine, str(running.claim.session_id)) as conn:
            candidate = prepare_v4_attachment_read_bound_on_connection(
                conn,
                running=running,
                selected=selected,
                inventory=inventory,
                tool_name=call.tool_name,
                arguments_json=canonical_json(arguments),
                pre_state=call.pre_state,
                catalog=bound.catalog,
            )
            if type(candidate) is not V4AttachmentReadBound or candidate != bound:
                raise AuditIntegrityError("V4 attachment value changed its original preissue source bound")
            return candidate

    await _registered_read(work, V4AttachmentReadBound, recheck)
    actual = await read_current_attachment(
        service=service,
        authority=authority,
        selected=selected,
        inventory=inventory,
        tool_name=call.tool_name,
        arguments=arguments,
        state=call.pre_state,
        required_work=work,
        expected_catalog=bound.catalog,
    )
    source = encode_private_chunk(
        codec="composer.owner-result.v1", canonical=canonical_json(actual.retained.model_dump(mode="json")), env_ref_names=bound.names
    )

    def verify_actual_source() -> ToolResult:
        with locked_session_transaction(authority._engine, bound.session_id) as conn:
            retained = read_v4_attachment_retained_value_on_connection(conn, bound=bound, source_chunk=source, pre_state=call.pre_state)
            if (
                canonical_json(retained.to_dict()) != canonical_json(actual.result.to_dict())
                or actual.result.updated_state is not call.pre_state
            ):
                raise AuditIntegrityError("V4 attachment value substituted its actual original handler result")
            return actual.result

    result = await _registered_read(work, ToolResult, verify_actual_source)
    return ValueBatchComputation(result=result, source_chunks=(source,))
