"""Nominal read DATA for reconstructing actual already-owed work after Stop.

Every use re-enters the canonical current SQL proof. This object never grants
issue, provider dispatch, P4 mutation or a new clock/physical owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Connection, select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_codec import encode_continuation_snapshot
from elspeth.web.composer.continuation_env_inventory import CurrentContinuationEnvInventory
from elspeth.web.composer.continuation_evidence import AdmittedTurnEvidence
from elspeth.web.composer.continuation_selection import SelectedContinuationState
from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import _require_current_continuation_running, require_snapshot_cas
from elspeth.web.sessions.models import composer_async_operations_table as jobs

if TYPE_CHECKING:
    from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
    from elspeth.web.sessions.continuation_runtime import ContinuationRuntimeSource


@dataclass(frozen=True, slots=True)
class ContinuationFinishRead:
    running: ComposerOperationRunning
    selected: SelectedContinuationState
    inventory: CurrentContinuationEnvInventory

    def require_selection(self, selected: SelectedContinuationState, inventory: CurrentContinuationEnvInventory) -> None:
        if (
            type(self.running) is not ComposerOperationRunning
            or type(selected) is not SelectedContinuationState
            or type(inventory) is not CurrentContinuationEnvInventory
            or selected is not self.selected
            or inventory is not self.inventory
            or inventory.running is not self.running
            or inventory.selection.checkpoint_id != selected.checkpoint_id
            or inventory.selection.cursor != selected.stack.cursor
            or inventory.selection.snapshot_json != encode_continuation_snapshot(selected.stack)
        ):
            raise AuditIntegrityError("owed read changed its actual selected/current physical owners")
        selected.__post_init__()
        compose = tuple(frame for frame in selected.stack.frames if type(frame) is ComposeFrame)
        if (
            len(compose) != 1
            or compose[0].slice_sequence != self.running.claim.segment_sequence
            or selected.stack.logical.session_id != str(self.running.claim.session_id)
            or selected.stack.logical.operation_id != self.running.claim.operation_id
        ):
            raise AuditIntegrityError("owed read crossed its actual continuation sequence/request")

    def read_evidence(self, conn: Connection) -> AdmittedTurnEvidence:
        from elspeth.web.composer.continuation_evidence_restore import read_selected_turn_evidence_graph_on_connection
        from elspeth.web.composer.continuation_phase_transitions import (
            require_completed_tool_prefix_on_connection,
            require_issued_tool_runtime_on_connection,
        )
        from elspeth.web.sessions.continuation_p4_application_store import require_p4_application_journal_on_connection

        self.require_selection(self.selected, self.inventory)
        running, selected, inventory = self.running, self.selected, self.inventory
        _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
        parent = conn.execute(
            select(jobs.c.cancel_requested_at).where(
                jobs.c.session_id == str(running.claim.session_id), jobs.c.operation_id == running.claim.operation_id
            )
        ).one()
        if parent.cancel_requested_at is None:
            raise AuditIntegrityError("owed Stop read has no actual durable Stop")
        current = require_snapshot_cas(
            conn,
            session_id=str(running.claim.session_id),
            operation_id=running.claim.operation_id,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.stack.cursor,
        )
        if current.payload_json != inventory.selection.snapshot_json:
            raise AuditIntegrityError("owed Stop read replaced its complete current SQL cut")
        top = selected.stack.frames[-1]
        if type(top) is not ToolBatchFrame:
            raise AuditIntegrityError("owed Stop read has no actual issued/completed tool source")
        if top.accumulator.codec == "composer.tool-batch.v1":
            require_issued_tool_runtime_on_connection(conn, running, selected=selected, env_ref_names=inventory.names)
        elif top.accumulator.codec == "composer.coupled-batch-data.v1":
            require_completed_tool_prefix_on_connection(conn, running, selected=selected, env_ref_names=inventory.names)
        else:
            raise AuditIntegrityError("owed Stop read has no implemented source proof for this phase")
        admitted = read_selected_turn_evidence_graph_on_connection(conn, running, selected=selected, names=inventory.names)
        if admitted.snapshot.application_entry is not None:
            require_p4_application_journal_on_connection(conn, running, selected=selected, evidence=admitted, env_ref_names=inventory.names)
        return admitted

    def read_runtime(self, authority: ComposerAsyncOperationAuthority) -> ContinuationRuntimeSource:
        from elspeth.web.composer.continuation_phase_transitions import (
            require_completed_tool_prefix_on_connection,
            require_issued_tool_runtime_on_connection,
        )
        from elspeth.web.composer.continuation_prompt_producers import _inputs_from_current_evidence_on_connection
        from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
        from elspeth.web.sessions.continuation_runtime import _runtime_source_from_inputs_on_connection
        from elspeth.web.sessions.locking import locked_session_transaction

        if type(authority) is not ComposerAsyncOperationAuthority:
            raise AuditIntegrityError("owed runtime requires its actual canonical SQL owner")
        with locked_session_transaction(authority._engine, str(self.running.claim.session_id)) as conn:
            admitted = self.read_evidence(conn)
            inputs = _inputs_from_current_evidence_on_connection(conn, selected=self.selected, inventory=self.inventory, evidence=admitted)
            top = self.selected.stack.frames[-1]
            if type(top) is not ToolBatchFrame:
                raise AuditIntegrityError("owed runtime lost its proved tool frame")
            if top.accumulator.codec == "composer.tool-batch.v1":
                issue = require_issued_tool_runtime_on_connection(
                    conn, self.running, selected=self.selected, env_ref_names=self.inventory.names
                )
                return _runtime_source_from_inputs_on_connection(
                    conn, self.running, selected=self.selected, inventory=self.inventory, inputs=inputs, owed_tool_issue=issue
                )
            prefix = require_completed_tool_prefix_on_connection(
                conn, self.running, selected=self.selected, env_ref_names=self.inventory.names
            )
            return _runtime_source_from_inputs_on_connection(
                conn, self.running, selected=self.selected, inventory=self.inventory, inputs=inputs, owed_tool_prefix=prefix
            )
