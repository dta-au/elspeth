"""Purge manager for PayloadStore content based on retention policy.

Identifies payloads eligible for deletion based on run completion time
and retention period. Deletes blobs while preserving hashes in Landscape
for audit integrity.
"""

import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from graphlib import CycleError, TopologicalSorter
from time import perf_counter
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import ColumnElement, CompoundSelect, FromClause, and_, or_, select, union
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

import elspeth.contracts.errors as contract_errors
from elspeth.contracts import RunStatus
from elspeth.contracts.coordination import DEFAULT_RUN_LIVENESS_WINDOW_SECONDS, mint_worker_id
from elspeth.contracts.hashing import canonical_json_loads
from elspeth.contracts.payload_store import PayloadNotFoundError, PayloadStore
from elspeth.core.canonical import stable_hash
from elspeth.core.checkpoint.recovery import NonResumableRunError
from elspeth.core.landscape.model_loaders import validate_run_lifecycle_row
from elspeth.core.landscape.reproducibility import update_grade_after_purge
from elspeth.core.landscape.run_coordination_repository import RunCoordinationRepository
from elspeth.core.landscape.schema import (
    aggregation_result_outputs_table,
    aggregation_results_table,
    calls_table,
    node_states_table,
    operations_table,
    routing_events_table,
    rows_table,
    runs_table,
    tokens_table,
)

if TYPE_CHECKING:
    from elspeth.core.landscape.database import LandscapeDB


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """Result of a purge operation."""

    deleted_count: int
    skipped_count: int  # Refs that didn't exist (already purged/never stored)
    failed_refs: tuple[str, ...]  # Failed deletions and refs held by retained or failed dependencies
    grade_update_failures: tuple[str, ...]  # Run IDs whose grade update failed after deletion
    duration_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "failed_refs", tuple(self.failed_refs))
        object.__setattr__(self, "grade_update_failures", tuple(self.grade_update_failures))
        if self.deleted_count < 0:
            raise ValueError(f"deleted_count must be non-negative, got {self.deleted_count}")
        if self.skipped_count < 0:
            raise ValueError(f"skipped_count must be non-negative, got {self.skipped_count}")
        if self.duration_seconds < 0:
            raise ValueError(f"duration_seconds must be non-negative, got {self.duration_seconds}")


logger = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class _SourceOutputDependencies:
    snapshot_children: dict[str, str]
    classification_inputs: dict[str, set[str]]


_PURGE_ELIGIBLE_RUN_STATUSES = (
    RunStatus.COMPLETED.value,
    RunStatus.COMPLETED_WITH_FAILURES.value,
    RunStatus.FAILED.value,
    RunStatus.EMPTY.value,
)
_RETENTION_ACTIVE_RUN_STATUSES = (
    RunStatus.RUNNING.value,
    RunStatus.INTERRUPTED.value,
)


class PurgeManager:
    """Manages payload purging based on retention policy.

    Identifies expired payloads from completed runs and deletes them
    from the PayloadStore while preserving audit hashes in Landscape.
    """

    def __init__(self, db: "LandscapeDB", payload_store: PayloadStore) -> None:
        """Initialize PurgeManager.

        Args:
            db: Landscape database connection
            payload_store: PayloadStore instance for blob operations
        """
        self._db = db
        self._payload_store = payload_store

    def _source_output_dependencies(self, refs: set[str], *, include_input_dependents: bool = False) -> _SourceOutputDependencies:
        """Admit source outputs and the payloads needed to purge them safely."""
        if not refs:
            return _SourceOutputDependencies({}, {})
        bindings: list[tuple[str, str | None, str | None, str | None]] = []
        ordered_refs = sorted(refs)
        with self._db.connection() as conn:
            for start in range(0, len(ordered_refs), self._PURGE_CHUNK_SIZE):
                chunk = ordered_refs[start : start + self._PURGE_CHUNK_SIZE]
                ref_condition: ColumnElement[bool] = operations_table.c.output_data_ref.in_(chunk)
                if include_input_dependents:
                    ref_condition = or_(ref_condition, operations_table.c.input_data_ref.in_(chunk))
                for metadata_ref, output_hash, input_ref, input_hash in conn.execute(
                    select(
                        operations_table.c.output_data_ref,
                        operations_table.c.output_data_hash,
                        operations_table.c.input_data_ref,
                        operations_table.c.input_data_hash,
                    )
                    .where(operations_table.c.operation_type == "source_load")
                    .where(operations_table.c.output_data_ref.isnot(None))
                    .where(ref_condition)
                ):
                    bindings.append((metadata_ref, output_hash, input_ref, input_hash))
        children: dict[str, str] = {}
        classification_inputs: dict[str, set[str]] = {}
        for metadata_ref, output_hash, input_ref, input_hash in bindings:
            # The operation writer stores canonical JSON bytes, so the
            # content-addressed reference and semantic hash must be identical.
            # Check this before a bounded read can exclude a large payload.
            if metadata_ref != output_hash:
                raise contract_errors.AuditIntegrityError("Source-load operation output has inconsistent canonical bindings during purge")
            try:
                content = self._payload_store.retrieve_bounded(metadata_ref, max_bytes=1024)
            except PayloadNotFoundError:
                continue  # A previous partial purge already removed the metadata.
            if content is None:
                if self._source_input_declares_snapshot(input_ref, input_hash):
                    raise contract_errors.AuditIntegrityError("Declared source snapshot operation metadata exceeds its bound during purge")
                # The generic operation API permits larger output mappings.
                # Its authenticated input proves this is not a snapshot load.
                if input_ref is not None:
                    if metadata_ref not in classification_inputs:
                        classification_inputs[metadata_ref] = set()
                    classification_inputs[metadata_ref].add(input_ref)
                continue
            try:
                metadata = canonical_json_loads(content)
            except (UnicodeDecodeError, ValueError) as exc:
                raise contract_errors.AuditIntegrityError("Source-load operation output is malformed during purge") from exc
            if type(metadata) is not dict:
                raise contract_errors.AuditIntegrityError("Source-load operation output must be an object during purge")
            try:
                metadata_hash = stable_hash(metadata)
            except ValueError as exc:
                raise contract_errors.AuditIntegrityError("Source-load operation output cannot be canonicalized during purge") from exc
            if metadata_hash != output_hash:
                raise contract_errors.AuditIntegrityError("Source-load operation output differs from its operation hash during purge")
            if "source_snapshot_ref" not in metadata:
                if self._source_input_declares_snapshot(input_ref, input_hash):
                    raise contract_errors.AuditIntegrityError(
                        "Declared source snapshot operation metadata is missing its child during purge"
                    )
                if input_ref is not None:
                    if metadata_ref not in classification_inputs:
                        classification_inputs[metadata_ref] = set()
                    classification_inputs[metadata_ref].add(input_ref)
                continue
            child = metadata["source_snapshot_ref"]
            if (
                set(metadata) != {"source_snapshot_ref", "source_snapshot_version"}
                or type(metadata["source_snapshot_version"]) is not int
                or metadata["source_snapshot_version"] != 1
                or type(child) is not str
                or re.fullmatch(r"[a-f0-9]{64}", child) is None
            ):
                raise contract_errors.AuditIntegrityError("Source snapshot operation metadata is malformed during purge")
            if metadata_ref in refs:
                children[metadata_ref] = child
        return _SourceOutputDependencies(children, classification_inputs)

    def _source_input_declares_snapshot(self, input_ref: str | None, input_hash: str | None) -> bool:
        """Classify owned source-load input without an unbounded payload read."""
        if input_ref is None:
            if input_hash is not None:
                raise contract_errors.AuditIntegrityError("Source-load operation input is unavailable for classification during purge")
            return False
        if input_ref != input_hash:
            raise contract_errors.AuditIntegrityError("Source-load operation input has inconsistent canonical bindings during purge")
        try:
            content = self._payload_store.retrieve_bounded(input_ref, max_bytes=1024)
        except PayloadNotFoundError as exc:
            raise contract_errors.AuditIntegrityError("Source-load operation input is missing for classification during purge") from exc
        if content is None:
            raise contract_errors.AuditIntegrityError("Source-load operation input exceeds the classification bound during purge")
        try:
            input_data = canonical_json_loads(content)
        except (UnicodeDecodeError, ValueError) as exc:
            raise contract_errors.AuditIntegrityError("Source-load operation input is malformed during purge") from exc
        if type(input_data) is not dict:
            raise contract_errors.AuditIntegrityError("Source-load operation input must be an object during purge")
        try:
            parsed_hash = stable_hash(input_data)
        except ValueError as exc:
            raise contract_errors.AuditIntegrityError("Source-load operation input cannot be canonicalized during purge") from exc
        if parsed_hash != input_hash:
            raise contract_errors.AuditIntegrityError("Source-load operation input differs from its operation hash during purge")
        if "snapshot_for_resume" not in input_data:
            return False
        if type(input_data["snapshot_for_resume"]) is not bool:
            raise contract_errors.AuditIntegrityError("Source-load operation snapshot declaration must be a boolean during purge")
        return input_data["snapshot_for_resume"] is True

    def _validate_run_lifecycle_rows(self, conn: Connection) -> None:
        """Crash on impossible Tier-1 run lifecycle rows before purge queries."""
        query = select(runs_table.c.run_id, runs_table.c.status, runs_table.c.completed_at)
        result = conn.execute(query)

        for row in result:
            try:
                status = RunStatus(row.status)
            except ValueError as exc:
                raise contract_errors.AuditIntegrityError(
                    f"Invalid run status '{row.status}' for run {row.run_id} — expected one of {[status.value for status in RunStatus]}"
                ) from exc

            validate_run_lifecycle_row(row.run_id, status, row.completed_at)

    def _build_ref_union_query(
        self,
        run_condition: ColumnElement[bool],
        *,
        rows_join: FromClause,
        operation_join: FromClause,
        call_state_join: FromClause,
        call_op_join: FromClause,
        routing_join: FromClause,
        token_join: FromClause,
        aggregation_output_join: FromClause,
    ) -> CompoundSelect[Any]:
        """Build a UNION of all payload ref sub-queries for a given run condition.

        Each sub-query selects a single ref column from a different table/join,
        filtered by the run condition and a NOT NULL guard on the ref column.

        Args:
            run_condition: SQLAlchemy WHERE clause for run filtering
                (e.g. expired condition or active condition)
            rows_join: Pre-built join for rows → runs
            operation_join: Pre-built join for operations → runs
            call_state_join: Pre-built join for calls → node_states → runs
            call_op_join: Pre-built join for calls → operations → runs
            routing_join: Pre-built join for routing_events → node_states → runs
            token_join: Pre-built join for tokens → runs

        Returns:
            UNION of all payload-ref sub-queries
        """
        return union(
            # 1. Row payloads
            select(rows_table.c.source_data_ref)
            .select_from(rows_join)
            .where(and_(run_condition, rows_table.c.source_data_ref.isnot(None))),
            # 2. Operation input payloads
            select(operations_table.c.input_data_ref)
            .select_from(operation_join)
            .where(and_(run_condition, operations_table.c.input_data_ref.isnot(None))),
            # 3. Operation output payloads
            select(operations_table.c.output_data_ref)
            .select_from(operation_join)
            .where(and_(run_condition, operations_table.c.output_data_ref.isnot(None))),
            # 4. Call request payloads (transform calls via state_id)
            select(calls_table.c.request_ref)
            .select_from(call_state_join)
            .where(and_(run_condition, calls_table.c.request_ref.isnot(None))),
            # 5. Call response payloads (transform calls via state_id)
            select(calls_table.c.response_ref)
            .select_from(call_state_join)
            .where(and_(run_condition, calls_table.c.response_ref.isnot(None))),
            # 6. Call request payloads (source/sink calls via operation_id)
            select(calls_table.c.request_ref).select_from(call_op_join).where(and_(run_condition, calls_table.c.request_ref.isnot(None))),
            # 7. Call response payloads (source/sink calls via operation_id)
            select(calls_table.c.response_ref).select_from(call_op_join).where(and_(run_condition, calls_table.c.response_ref.isnot(None))),
            # 8. Routing reason payloads
            select(routing_events_table.c.reason_ref)
            .select_from(routing_join)
            .where(and_(run_condition, routing_events_table.c.reason_ref.isnot(None))),
            # 9. Token payloads (expand/coalesce per-token row data)
            select(tokens_table.c.token_data_ref)
            .select_from(token_join)
            .where(and_(run_condition, tokens_table.c.token_data_ref.isnot(None))),
            # 10. Pre-expansion aggregation output receipts
            select(aggregation_result_outputs_table.c.token_data_ref)
            .select_from(aggregation_output_join)
            .where(and_(run_condition, aggregation_result_outputs_table.c.token_data_ref.isnot(None))),
        )

    def find_expired_payload_refs(
        self,
        retention_days: int,
        as_of: datetime | None = None,
    ) -> list[str]:
        """Find all payload refs eligible for deletion based on retention policy.

        This includes payloads from:
        - rows.source_data_ref (source row payloads)
        - operations.input_data_ref and operations.output_data_ref (source/sink operation payloads)
        - calls.request_ref and calls.response_ref (external call payloads)
        - routing_events.reason_ref (routing reason payloads)
        - tokens.token_data_ref (expand/coalesce token payloads)

        IMPORTANT: Because payloads are content-addressable, the same hash can
        appear in multiple runs. We must exclude refs that are still used by
        non-expired runs to avoid breaking replay/explain for active runs.

        Args:
            retention_days: Number of days to retain payloads after run completion
            as_of: Reference datetime for cutoff calculation (defaults to now)

        Returns:
            Deduplicated list of payload refs for expired payloads that are NOT
            used by any non-expired or incomplete runs
        """
        if as_of is None:
            as_of = datetime.now(UTC)

        cutoff = as_of - timedelta(days=retention_days)

        # Condition for expired runs: purge-eligible terminal status AND older than cutoff.
        # Interrupted is terminal in the lifecycle repository, but it remains
        # a recovery candidate and is protected by the active condition below.
        run_expired_condition = and_(
            runs_table.c.status.in_(_PURGE_ELIGIBLE_RUN_STATUSES),
            runs_table.c.completed_at.isnot(None),
            runs_table.c.completed_at < cutoff,
        )

        # Condition for active runs: NOT expired (recent or still running)
        # A run is "active" if any of:
        # - completed_at >= cutoff (recent, within retention period)
        # - completed_at IS NULL (still running, hasn't finished yet)
        # - status is running or interrupted (explicitly protected)
        run_active_condition = or_(
            runs_table.c.completed_at >= cutoff,
            runs_table.c.completed_at.is_(None),
            runs_table.c.status.in_(_RETENTION_ACTIVE_RUN_STATUSES),
        )

        # === Build joins (shared between expired and active queries) ===
        # NOTE: Use node_states.run_id directly (denormalized column) instead of
        # joining through nodes table. The nodes table has composite PK (node_id, run_id),
        # so joining on node_id alone would be ambiguous when node_id is reused across runs.
        rows_join = rows_table.join(runs_table, rows_table.c.run_id == runs_table.c.run_id)
        operation_join = operations_table.join(runs_table, operations_table.c.run_id == runs_table.c.run_id)
        call_state_join = calls_table.join(node_states_table, calls_table.c.state_id == node_states_table.c.state_id).join(
            runs_table, node_states_table.c.run_id == runs_table.c.run_id
        )
        # XOR constraint: calls have either state_id OR operation_id, not both
        call_op_join = calls_table.join(operations_table, calls_table.c.operation_id == operations_table.c.operation_id).join(
            runs_table, operations_table.c.run_id == runs_table.c.run_id
        )
        routing_join = routing_events_table.join(node_states_table, routing_events_table.c.state_id == node_states_table.c.state_id).join(
            runs_table, node_states_table.c.run_id == runs_table.c.run_id
        )
        token_join = tokens_table.join(runs_table, tokens_table.c.run_id == runs_table.c.run_id)
        aggregation_output_join = aggregation_result_outputs_table.join(
            aggregation_results_table,
            aggregation_result_outputs_table.c.batch_id == aggregation_results_table.c.batch_id,
        ).join(runs_table, aggregation_results_table.c.run_id == runs_table.c.run_id)

        expired_refs_query = self._build_ref_union_query(
            run_expired_condition,
            rows_join=rows_join,
            operation_join=operation_join,
            call_state_join=call_state_join,
            call_op_join=call_op_join,
            routing_join=routing_join,
            token_join=token_join,
            aggregation_output_join=aggregation_output_join,
        )
        active_refs_query = self._build_ref_union_query(
            run_active_condition,
            rows_join=rows_join,
            operation_join=operation_join,
            call_state_join=call_state_join,
            call_op_join=call_op_join,
            routing_join=routing_join,
            token_join=token_join,
            aggregation_output_join=aggregation_output_join,
        )

        # === Execute both queries and compute set difference ===
        # We use Python set difference rather than SQL EXCEPT because:
        # 1. SQLite's EXCEPT can have performance issues with complex UNIONs
        # 2. The result sets are typically small enough for in-memory operation
        # 3. Python set operations are clearer for this anti-join pattern

        with self._db.connection() as conn:
            self._validate_run_lifecycle_rows(conn)

            # Get all refs from expired runs
            expired_result = conn.execute(expired_refs_query)
            expired_refs = {row[0] for row in expired_result}

            # Get all refs from active runs
            active_result = conn.execute(active_refs_query)
            active_refs = {row[0] for row in active_result}

        # Snapshot spools are referenced inside completed source-load output
        # metadata. Expand both sides before the active-run anti-join so a
        # content-addressed spool shared with a retained run stays protected.
        expired_dependencies = self._source_output_dependencies(expired_refs)
        expired_children = expired_dependencies.snapshot_children
        expired_refs.update(expired_children.values())
        active_dependencies = self._source_output_dependencies(active_refs)
        active_refs.update(active_dependencies.snapshot_children.values())
        # A shared output can outlive an expired operation's classification
        # input. Preserve every input needed by any retained output binding.
        retained_dependencies: dict[str, set[str]] = {}
        for source_dependencies in (expired_dependencies, active_dependencies):
            for output_ref, input_refs in source_dependencies.classification_inputs.items():
                if output_ref not in retained_dependencies:
                    retained_dependencies[output_ref] = set()
                retained_dependencies[output_ref].update(input_refs)
            for metadata_ref, child_ref in source_dependencies.snapshot_children.items():
                if metadata_ref not in retained_dependencies:
                    retained_dependencies[metadata_ref] = set()
                retained_dependencies[metadata_ref].add(child_ref)
        pending = list(active_refs)
        while pending:
            protected_ref = pending.pop()
            if protected_ref not in retained_dependencies:
                continue
            for dependency_ref in retained_dependencies[protected_ref]:
                if dependency_ref not in active_refs:
                    active_refs.add(dependency_ref)
                    pending.append(dependency_ref)

        # Return refs that are ONLY in expired runs (not in any active run)
        safe_to_delete = expired_refs - active_refs
        # Metadata must stay reachable while its child is protected by a
        # retained run. Otherwise a later purge cannot discover that child.
        for metadata_ref, child_ref in expired_children.items():
            if child_ref in active_refs:
                safe_to_delete.discard(metadata_ref)
        return sorted(safe_to_delete)

    # SQLite default SQLITE_MAX_VARIABLE_NUMBER is 999. Chunk IN clauses
    # to stay well under this limit (8 queries x chunk_size variables each).
    _PURGE_CHUNK_SIZE = 100

    def _find_affected_run_ids(self, refs: list[str]) -> set[str]:
        """Find run IDs that have payloads in the given refs list.

        Queries all payload reference columns to find which runs are affected
        by purging the specified refs. Used to update reproducibility grades
        after purge completes.

        Large ref lists are chunked to avoid exceeding SQLite's bind variable
        limit (SQLITE_MAX_VARIABLE_NUMBER, default 999).

        Args:
            refs: List of payload references (content hashes) being purged

        Returns:
            Set of run_ids that have at least one payload in the refs list
        """
        if not refs:
            return set()

        refs_list = list(set(refs))
        all_run_ids: set[str] = set()

        for offset in range(0, len(refs_list), self._PURGE_CHUNK_SIZE):
            chunk = refs_list[offset : offset + self._PURGE_CHUNK_SIZE]
            all_run_ids |= self._find_affected_run_ids_chunk(chunk)

        return all_run_ids

    def _find_affected_run_ids_chunk(self, refs_chunk: list[str]) -> set[str]:
        """Find run IDs affected by a single chunk of refs."""
        # 1. From rows.source_data_ref
        row_runs_query = select(rows_table.c.run_id).distinct().where(rows_table.c.source_data_ref.in_(refs_chunk))

        # 2. From operations.input_data_ref and operations.output_data_ref
        operation_input_runs_query = select(operations_table.c.run_id).distinct().where(operations_table.c.input_data_ref.in_(refs_chunk))
        operation_output_runs_query = select(operations_table.c.run_id).distinct().where(operations_table.c.output_data_ref.in_(refs_chunk))

        # 3. From calls.request_ref and calls.response_ref (transform calls via state_id)
        # Use node_states.run_id directly (denormalized column)
        call_state_join = calls_table.join(node_states_table, calls_table.c.state_id == node_states_table.c.state_id)

        call_state_request_runs_query = (
            select(node_states_table.c.run_id).distinct().select_from(call_state_join).where(calls_table.c.request_ref.in_(refs_chunk))
        )

        call_state_response_runs_query = (
            select(node_states_table.c.run_id).distinct().select_from(call_state_join).where(calls_table.c.response_ref.in_(refs_chunk))
        )

        # 4. From calls.request_ref and calls.response_ref (source/sink calls via operation_id)
        # XOR constraint: calls have either state_id OR operation_id, not both
        call_op_join = calls_table.join(operations_table, calls_table.c.operation_id == operations_table.c.operation_id)

        call_op_request_runs_query = (
            select(operations_table.c.run_id).distinct().select_from(call_op_join).where(calls_table.c.request_ref.in_(refs_chunk))
        )

        call_op_response_runs_query = (
            select(operations_table.c.run_id).distinct().select_from(call_op_join).where(calls_table.c.response_ref.in_(refs_chunk))
        )

        # 5. From routing_events.reason_ref
        routing_join = routing_events_table.join(node_states_table, routing_events_table.c.state_id == node_states_table.c.state_id)

        routing_runs_query = (
            select(node_states_table.c.run_id).distinct().select_from(routing_join).where(routing_events_table.c.reason_ref.in_(refs_chunk))
        )

        # 6. From tokens.token_data_ref (expand/coalesce per-token row data)
        token_runs_query = select(tokens_table.c.run_id).distinct().where(tokens_table.c.token_data_ref.in_(refs_chunk))
        aggregation_output_runs_query = (
            select(aggregation_results_table.c.run_id)
            .distinct()
            .select_from(
                aggregation_result_outputs_table.join(
                    aggregation_results_table,
                    aggregation_result_outputs_table.c.batch_id == aggregation_results_table.c.batch_id,
                )
            )
            .where(aggregation_result_outputs_table.c.token_data_ref.in_(refs_chunk))
        )

        # Union all run_id queries
        all_runs_query = union(
            row_runs_query,
            operation_input_runs_query,
            operation_output_runs_query,
            call_state_request_runs_query,
            call_state_response_runs_query,
            call_op_request_runs_query,
            call_op_response_runs_query,
            routing_runs_query,
            token_runs_query,
            aggregation_output_runs_query,
        )

        with self._db.connection() as conn:
            result = conn.execute(
                select(runs_table.c.run_id)
                .where(runs_table.c.run_id.in_(all_runs_query))
                .where(runs_table.c.status.in_(_PURGE_ELIGIBLE_RUN_STATUSES))
            )
            return {row[0] for row in result}

    def purge_payloads(self, refs: list[str]) -> PurgeResult:
        """Purge payloads from the PayloadStore.

        Deletes each payload by reference, tracking successes and failures.
        Hashes in Landscape rows are preserved - only blobs are deleted.

        After deletion, updates reproducibility_grade for affected runs:
        - REPLAY_REPRODUCIBLE -> ATTRIBUTABLE_ONLY (payloads needed for replay are gone)
        - FULL_REPRODUCIBLE -> unchanged (doesn't depend on payloads)
        - ATTRIBUTABLE_ONLY -> unchanged (already at lowest grade)

        Grade updates only occur for runs whose payloads were actually deleted.
        Runs that only had failed deletions retain their grade (payloads still exist).

        Args:
            refs: List of payload references (content hashes) to delete

        Returns:
            PurgeResult with deletion statistics. Note that skipped_count
            tracks refs that didn't exist (already purged or never stored),
            while failed_refs tracks deletion errors and dependency refs
            retained because their dependent output could not be deleted.
        """
        start_time = perf_counter()
        requested_counts = Counter(refs)

        # Delete snapshot children before their metadata, preserving a retry
        # path on interruption. Do not add refs beyond the caller's admitted
        # set: discovery may have excluded a child held by an active run.
        source_dependencies = self._source_output_dependencies(set(refs), include_input_dependents=True)
        snapshot_children = source_dependencies.snapshot_children
        child_refs = set(snapshot_children.values())
        missing_children = {child for child in child_refs if child not in refs and self._payload_store.exists(child)}
        if missing_children:
            raise ValueError("Cannot purge source snapshot metadata without its admitted child payload")
        dependencies: dict[str, set[str]] = {ref: set() for ref in refs}
        retained_inputs: set[str] = set()
        for metadata_ref, child_ref in snapshot_children.items():
            if child_ref in dependencies and child_ref != metadata_ref:
                dependencies[metadata_ref].add(child_ref)
        for output_ref, input_refs in source_dependencies.classification_inputs.items():
            for input_ref in input_refs.intersection(dependencies):
                if output_ref == input_ref:
                    continue
                if output_ref in dependencies:
                    dependencies[input_ref].add(output_ref)
                else:
                    retained_inputs.add(input_ref)
        try:
            refs = [ref for ref in TopologicalSorter(dependencies).static_order() for _ in range(requested_counts[ref])]
        except CycleError as exc:
            raise ValueError("Cannot purge cyclic source-output payload dependencies safely") from exc

        # Step 1: Delete the payloads, tracking which refs were actually deleted
        deleted_count = 0
        skipped_count = 0
        failed_refs: list[str] = []
        deleted_refs: list[str] = []
        failed_deletions: set[str] = set()

        for ref in refs:
            if ref in retained_inputs or dependencies[ref].intersection(failed_deletions):
                failed_refs.append(ref)
                failed_deletions.add(ref)
                continue  # Keep dependency evidence reachable for a retry.
            try:
                deleted = self._payload_store.delete(ref)
            except OSError as e:
                logger.warning(
                    "payload_deletion_failed",
                    ref=ref,
                    error_type=type(e).__name__,
                    error=str(e),
                )
                failed_refs.append(ref)
                failed_deletions.add(ref)
                continue

            if deleted:
                deleted_count += 1
                deleted_refs.append(ref)
            else:
                # Ref doesn't exist - already purged or never stored
                # This is not a failure, just skip it
                skipped_count += 1

        # Step 2: Find runs affected by ONLY the successfully deleted refs
        # Runs with only failed refs still have their payloads and should not be downgraded
        affected_run_ids = self._find_affected_run_ids(deleted_refs)

        # Step 3: Update reproducibility grades for affected runs
        # This degrades REPLAY_REPRODUCIBLE -> ATTRIBUTABLE_ONLY since
        # nondeterministic runs can no longer be replayed without payloads.
        # Each update is wrapped individually because payloads are already
        # irreversibly deleted — a transient DB failure for one run must not
        # prevent grade updates for the remaining runs.
        #
        # Database I/O and a concurrent seat owner can prevent the update.
        # Every semantic anomaly is raised as AuditIntegrityError (a
        # Tier-1 error that is NOT a SQLAlchemyError), which therefore
        # propagates uncaught and crashes the purge — corruption of our audit
        # trail must never be recorded as a recoverable "grade update failure".
        # Any other exception (TypeError, AttributeError, RuntimeError) is a bug
        # in our own code and likewise crashes rather than being swallowed.
        grade_update_failures: list[str] = []
        coordination = RunCoordinationRepository(self._db.engine)
        for run_id in sorted(affected_run_ids):
            try:
                authority = coordination.acquire_export_leadership(
                    run_id=run_id,
                    worker_id=mint_worker_id(run_id),
                    window_seconds=DEFAULT_RUN_LIVENESS_WINDOW_SECONDS,
                )
                try:
                    update_grade_after_purge(self._db, coordination_token=authority, deleted_refs=deleted_refs)
                except BaseException as mutation_error:
                    try:
                        coordination.release_seat(token=authority)
                    except BaseException as release_error:
                        # Cleanup must not replace an integrity failure with a
                        # recoverable database error caught by the outer block.
                        raise mutation_error from release_error
                    raise
                else:
                    coordination.release_seat(token=authority)
            except (
                SQLAlchemyError,
                NonResumableRunError,
                contract_errors.WriteLockHeldError,
                contract_errors.RunLeadershipLostError,
            ) as exc:
                logger.warning(
                    "grade_update_failed",
                    run_id=run_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                    msg="Payloads already deleted but grade update failed — run may have stale reproducibility grade",
                )
                grade_update_failures.append(run_id)

        duration_seconds = perf_counter() - start_time

        return PurgeResult(
            deleted_count=deleted_count,
            skipped_count=skipped_count,
            failed_refs=tuple(failed_refs),
            grade_update_failures=tuple(grade_update_failures),
            duration_seconds=duration_seconds,
        )
