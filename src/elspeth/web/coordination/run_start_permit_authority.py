"""Permit writes within an already validated session EXECUTE transaction."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.coordination.contracts import SessionOperationContext, StartPermitState
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import run_execution_inputs_table, run_start_permits_table, runs_table, session_operation_fences_table
from elspeth.web.sessions.protocol import RunStartPermitRecord


class RepositoryRunStartPermitAuthority:
    """No connection escapes; the registry token expires with the fenced transaction."""

    @staticmethod
    def create_pending(connection_token: str, *, run_id: str) -> None:
        _resolve_mutation_connection(connection_token).execute(insert(run_start_permits_table).values(run_id=run_id, start_state="pending"))

    @staticmethod
    def issue(connection_token: str, *, run_id: str, context: SessionOperationContext, now: datetime) -> RunStartPermitRecord:
        conn = _resolve_mutation_connection(connection_token)
        run = conn.execute(select(runs_table).where(runs_table.c.id == run_id).with_for_update()).one()
        if run.session_id != context.fence.session_id:
            raise AuditIntegrityError("Run permit session custody mismatch")
        row = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id).with_for_update()).one()
        if row.start_state == "pending":
            if run.status != "pending" or run.cancel_requested_at is not None:
                raise AuditIntegrityError("Pending permit has an inconsistent run state")
            envelope = conn.execute(select(run_execution_inputs_table).where(run_execution_inputs_table.c.run_id == run_id)).one()
            fence = conn.execute(
                select(session_operation_fences_table).where(session_operation_fences_table.c.session_id == run.session_id)
            ).one()
            subject = {
                "session_operation_id": context.fence.operation_id,
                "session_operation_epoch": context.fence.operation_epoch,
                "run_owner_instance_id": fence.owner_instance_id,
                "run_owner_epoch": context.fence.operation_epoch,
                "envelope_hash": envelope.canonical_input_digest,
                "topology_hash": envelope.topology_digest,
                "source_manifest_hash": envelope.source_manifest_digest,
                "checkpoint_subject_hash": envelope.canonical_input_digest,
                "deployment_generation": envelope.deployment_generation,
                "session_epoch": envelope.session_epoch,
                "landscape_epoch": envelope.landscape_epoch,
                "coordination_protocol": envelope.coordination_protocol,
            }
            conn.execute(
                update(run_start_permits_table)
                .where(run_start_permits_table.c.run_id == run_id)
                .values(
                    **subject,
                    start_state="start_permitted",
                    permit_id=str(uuid4()),
                    permit_epoch=1,
                    permit_subject_hash=stable_hash({"run_id": run_id, **subject}),
                    issued_at=now,
                )
            )
            conn.execute(update(runs_table).where(runs_table.c.id == run_id).values(saga_state="start_permit_issued"))
            row = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
        return RunStartPermitRecord(
            run_id=run_id,
            state=StartPermitState(row.start_state),
            permit_id=row.permit_id,
            permit_epoch=row.permit_epoch,
            subject_hash=row.permit_subject_hash,
            issued_at=row.issued_at,
            cancelled_at=row.cancelled_at,
        )
