"""Permit writes within an already validated session EXECUTE transaction."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Row

from elspeth.contracts.chargeable_admission import (
    AdmissionPolicyEvidence,
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionPolicy,
    QuotaDisposition,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.coordination.chargeable_admission_authority import RepositoryChargeableAdmissionAuthority
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
    def _assess(
        connection_token: str, *, run_id: str, context: SessionOperationContext, now: datetime, policy: ChargeableAdmissionPolicy
    ) -> tuple[Row[Any], ChargeableAdmissionDecision]:
        conn = _resolve_mutation_connection(connection_token)
        decision = RepositoryChargeableAdmissionAuthority.assess(connection_token, session_id=context.fence.session_id, policy=policy)
        run = conn.execute(select(runs_table).where(runs_table.c.id == run_id).with_for_update()).one()
        if run.session_id != context.fence.session_id:
            raise AuditIntegrityError("Run permit session custody mismatch")
        row = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id).with_for_update()).one()
        if row.start_state in {"refused", "cancelled_before_permit"} or row.execution_refusal is not None:
            return row, decision
        if row.start_state == "start_permitted":
            previous = RepositoryRunStartPermitAuthority._record(row)
            assert previous.admission_decision is not None
            if decision.allowed and previous.admission_decision.evidence.secret_wiring_hash != policy.secret_wiring_hash:
                decision = ChargeableAdmissionDecision(
                    refusal_reason=AdmissionRefusalReason.POLICY_GENERATION_CHANGED,
                    evidence=AdmissionPolicyEvidence(
                        quota_disposition=QuotaDisposition.NOT_ASSESSED, secret_wiring_hash=policy.secret_wiring_hash
                    ),
                )
        if not decision.allowed:
            if row.start_state == "pending":
                conn.execute(
                    update(run_start_permits_table)
                    .where(run_start_permits_table.c.run_id == run_id)
                    .values(
                        start_state="refused",
                        admission_decision=decision.model_dump(mode="json"),
                        admission_decision_hash=decision.canonical_hash,
                        decided_at=now,
                    )
                )
            else:
                conn.execute(
                    update(run_start_permits_table)
                    .where(run_start_permits_table.c.run_id == run_id)
                    .values(
                        execution_refusal=decision.model_dump(mode="json"),
                    )
                )
            assert decision.refusal_reason is not None
            conn.execute(
                update(runs_table)
                .where(runs_table.c.id == run_id)
                .values(
                    status="failed",
                    saga_state="admission_refusal_pending",
                    finished_at=now,
                    error=f"Run admission refused: {decision.refusal_reason.value}",
                )
            )
            row = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
        return row, decision

    @staticmethod
    def assess(
        connection_token: str, *, run_id: str, context: SessionOperationContext, now: datetime, policy: ChargeableAdmissionPolicy
    ) -> RunStartPermitRecord:
        """Persist refusals before restoration without authorizing a start."""
        row, _ = RepositoryRunStartPermitAuthority._assess(connection_token, run_id=run_id, context=context, now=now, policy=policy)
        return RepositoryRunStartPermitAuthority._record(row)

    @staticmethod
    def issue(
        connection_token: str, *, run_id: str, context: SessionOperationContext, now: datetime, policy: ChargeableAdmissionPolicy
    ) -> RunStartPermitRecord:
        row, decision = RepositoryRunStartPermitAuthority._assess(connection_token, run_id=run_id, context=context, now=now, policy=policy)
        conn = _resolve_mutation_connection(connection_token)
        if row.start_state == "pending":
            run = conn.execute(select(runs_table).where(runs_table.c.id == run_id)).one()
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
                "admission_decision_hash": decision.canonical_hash,
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
                    admission_decision=decision.model_dump(mode="json"),
                    decided_at=now,
                )
            )
            conn.execute(update(runs_table).where(runs_table.c.id == run_id).values(saga_state="start_permit_issued"))
            row = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
        return RepositoryRunStartPermitAuthority._record(row)

    @staticmethod
    def observe_for_cleanup(connection_token: str, *, run_id: str, context: SessionOperationContext) -> RunStartPermitRecord:
        conn = _resolve_mutation_connection(connection_token)
        run = conn.execute(select(runs_table).where(runs_table.c.id == run_id)).one()
        if run.session_id != context.fence.session_id or run.cancel_requested_at is None:
            raise AuditIntegrityError("Cleanup permit observation requires the cancelled run's exact session custody")
        row = conn.execute(select(run_start_permits_table).where(run_start_permits_table.c.run_id == run_id)).one()
        if row.start_state == "pending":
            raise AuditIntegrityError("Cancelled pending permit was not settled by cancellation authority")
        return RepositoryRunStartPermitAuthority._record(row)

    @staticmethod
    def _record(row: Row[Any]) -> RunStartPermitRecord:
        decision = (
            ChargeableAdmissionDecision.model_validate_json(json.dumps(row.admission_decision), strict=True)
            if row.admission_decision is not None
            else None
        )
        if decision is not None and decision.canonical_hash != row.admission_decision_hash:
            raise AuditIntegrityError("Run admission evidence hash mismatch")
        state = StartPermitState(row.start_state)
        if state in {StartPermitState.START_PERMITTED, StartPermitState.REFUSED} and decision is None:
            raise AuditIntegrityError("Decided permit lacks admission evidence")
        if decision is not None and decision.allowed != (state is StartPermitState.START_PERMITTED):
            raise AuditIntegrityError("Permit state contradicts admission decision")
        if state is StartPermitState.START_PERMITTED:
            subject = {
                "session_operation_id": row.session_operation_id,
                "session_operation_epoch": row.session_operation_epoch,
                "run_owner_instance_id": row.run_owner_instance_id,
                "run_owner_epoch": row.run_owner_epoch,
                "envelope_hash": row.envelope_hash,
                "topology_hash": row.topology_hash,
                "source_manifest_hash": row.source_manifest_hash,
                "checkpoint_subject_hash": row.checkpoint_subject_hash,
                "deployment_generation": row.deployment_generation,
                "session_epoch": row.session_epoch,
                "landscape_epoch": row.landscape_epoch,
                "coordination_protocol": row.coordination_protocol,
                "admission_decision_hash": row.admission_decision_hash,
            }
            if stable_hash({"run_id": row.run_id, **subject}) != row.permit_subject_hash:
                raise AuditIntegrityError("Run permit subject hash mismatch")
        refusal = (
            ChargeableAdmissionDecision.model_validate_json(json.dumps(row.execution_refusal), strict=True)
            if row.execution_refusal is not None
            else None
        )
        if refusal is not None and (refusal.allowed or state is not StartPermitState.START_PERMITTED):
            raise AuditIntegrityError("Recovery refusal contradicts permit state")
        return RunStartPermitRecord(
            run_id=row.run_id,
            state=StartPermitState(row.start_state),
            permit_id=row.permit_id,
            permit_epoch=row.permit_epoch,
            subject_hash=row.permit_subject_hash,
            issued_at=row.issued_at,
            cancelled_at=row.cancelled_at,
            admission_decision=decision,
            execution_refusal=refusal,
        )
