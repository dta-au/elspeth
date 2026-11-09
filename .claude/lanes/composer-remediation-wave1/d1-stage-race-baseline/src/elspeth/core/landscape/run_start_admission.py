"""Landscape-owned immutable permit binding and pre-effect admission state.

Observation never returns a coordination token. A successor must obtain its
own Landscape leadership before advancing a prepared admission.
"""

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, delete, select, update

from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, CoordinationToken
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.run_start import RunStartPermitBinding
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.run_coordination_repository import fenced_leader_transaction
from elspeth.core.landscape.schema import (
    checkpoints_table,
    edges_table,
    node_states_table,
    nodes_table,
    operations_table,
    preflight_results_table,
    rows_table,
    run_sources_table,
    run_start_admissions_table,
    secret_resolutions_table,
    sink_effects_table,
)


class RunStartAdmissionState(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"


@dataclass(frozen=True, slots=True)
class RunStartAdmission:
    binding: RunStartPermitBinding
    state: RunStartAdmissionState


class RunStartAdmissionRepository:
    """Write admission only with the Landscape leader's authority."""

    def __init__(self, db: LandscapeDB) -> None:
        self._db = db

    @staticmethod
    def observe_on(conn: Connection, binding: RunStartPermitBinding) -> RunStartAdmission | None:
        if type(binding) is not RunStartPermitBinding:
            raise TypeError("run admission requires an exact RunStartPermitBinding")
        row = conn.execute(select(run_start_admissions_table).where(run_start_admissions_table.c.run_id == binding.run_id)).one_or_none()
        if row is None:
            return None
        actual = RunStartPermitBinding(row.run_id, row.permit_id, row.permit_epoch, row.subject_hash)
        if actual != binding:
            raise AuditIntegrityError("Run UUID is bound to a different start permit")
        return RunStartAdmission(binding=actual, state=RunStartAdmissionState(row.state))

    def observe(self, binding: RunStartPermitBinding) -> RunStartAdmission | None:
        with self._db.engine.connect() as conn:
            return self.observe_on(conn, binding)

    def reset_prepared_initialization(self, binding: RunStartPermitBinding, *, coordination_token: CoordinationToken) -> None:
        """Rebuild setup metadata only after independently proving no effects."""
        if coordination_token.run_id != binding.run_id:
            raise AuditIntegrityError("Admission and Landscape authority belong to different runs")
        with fenced_leader_transaction(
            self._db.engine,
            token=coordination_token,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="run-start-reset-prepared",
        ) as conn:
            admission = self.observe_on(conn, binding)
            if admission is None or admission.state is not RunStartAdmissionState.PREPARED:
                raise AuditIntegrityError("Only a prepared admission can restart initialization")
            for table in (rows_table, node_states_table, operations_table, sink_effects_table):
                if conn.execute(select(table.c.run_id).where(table.c.run_id == binding.run_id).limit(1)).first() is not None:
                    raise AuditIntegrityError("Prepared initialization contains execution evidence")
            if (
                conn.execute(
                    select(checkpoints_table.c.run_id)
                    .where(
                        checkpoints_table.c.run_id == binding.run_id,
                        (checkpoints_table.c.sequence_number != 0) | checkpoints_table.c.barrier_scalars_json.is_not(None),
                    )
                    .limit(1)
                ).first()
                is not None
            ):
                raise AuditIntegrityError("Prepared initialization contains an execution checkpoint")
            if (
                conn.execute(
                    select(run_sources_table.c.run_id)
                    .where(
                        run_sources_table.c.run_id == binding.run_id,
                        run_sources_table.c.lifecycle_state != "ready",
                    )
                    .limit(1)
                ).first()
                is not None
            ):
                raise AuditIntegrityError("Prepared initialization contains an activated source")
            conn.execute(delete(checkpoints_table).where(checkpoints_table.c.run_id == binding.run_id))
            conn.execute(delete(edges_table).where(edges_table.c.run_id == binding.run_id))
            conn.execute(delete(run_sources_table).where(run_sources_table.c.run_id == binding.run_id))
            conn.execute(delete(nodes_table).where(nodes_table.c.run_id == binding.run_id))
            conn.execute(delete(preflight_results_table).where(preflight_results_table.c.run_id == binding.run_id))
            conn.execute(delete(secret_resolutions_table).where(secret_resolutions_table.c.run_id == binding.run_id))

    def mark_executing(self, binding: RunStartPermitBinding, *, coordination_token: CoordinationToken) -> None:
        if coordination_token.run_id != binding.run_id:
            raise AuditIntegrityError("Admission and Landscape authority belong to different runs")
        with fenced_leader_transaction(
            self._db.engine,
            token=coordination_token,
            window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
            verb="run-start-effects",
        ) as conn:
            admission = self.observe_on(conn, binding)
            if admission is None:
                raise AuditIntegrityError("Run has no durable start admission")
            if admission.state is RunStartAdmissionState.PREPARED:
                conn.execute(
                    update(run_start_admissions_table)
                    .where(run_start_admissions_table.c.run_id == binding.run_id)
                    .values(state="executing")
                )
