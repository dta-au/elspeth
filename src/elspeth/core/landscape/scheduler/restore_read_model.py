"""Barrier restore read models over token outcomes.

These queries encode ADR-030 crash-window semantics for journal restore. They
live with scheduler/barrier recovery rather than the generic token-outcome
writer so the persistence layer does not own restore policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, func, select

from elspeth.contracts import (
    CommittedAggregationChild,
    CommittedAggregationResidual,
    CommittedCoalesceResidual,
    NodeStateStatus,
    TokenOutcome,
)
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import BatchStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import stable_hash
from elspeth.core.landscape._database_ops import DatabaseOps
from elspeth.core.landscape.model_loaders import TokenOutcomeLoader
from elspeth.core.landscape.schema import (
    batch_members_table,
    batches_table,
    coalesce_branch_losses_table,
    coalesce_effect_members_table,
    coalesce_effects_table,
    node_states_table,
    token_outcomes_table,
    token_parents_table,
    token_work_items_table,
    tokens_table,
)

_TOKEN_ID_CHUNK_SIZE = 500


class BarrierRestoreReadModel:
    """Read-only audit queries used by barrier journal restore."""

    def __init__(
        self,
        ops: DatabaseOps,
        *,
        token_outcome_loader: TokenOutcomeLoader,
    ) -> None:
        self._ops = ops
        self._token_outcome_loader = token_outcome_loader

    def list_live_buffered_outcomes(self, ref: TokenRef) -> list[TokenOutcome]:
        """All live BUFFERED outcomes for one token.

        "Live" means the token has no completed outcome; a flushed token's
        BUFFERED row is dead history and exempt. Multiple live rows signal a
        duplicate barrier acceptance that restore must refuse loudly.
        """
        terminal = token_outcomes_table.alias("terminal_outcomes")
        terminal_witness = (
            select(terminal.c.outcome_id)
            .where(terminal.c.token_id == ref.token_id)
            .where(terminal.c.run_id == ref.run_id)
            .where(terminal.c.completed == 1)
            .exists()
        )
        query = (
            select(token_outcomes_table)
            .where(token_outcomes_table.c.token_id == ref.token_id)
            .where(token_outcomes_table.c.run_id == ref.run_id)
            .where(token_outcomes_table.c.completed == 0)
            .where(token_outcomes_table.c.path == TerminalPath.BUFFERED.value)
            .where(~terminal_witness)
            .order_by(token_outcomes_table.c.recorded_at, token_outcomes_table.c.outcome_id)
        )
        return [self._token_outcome_loader.load(row) for row in self._ops.execute_fetchall(query)]

    def get_max_node_state_attempts(
        self,
        run_id: str,
        token_ids: Sequence[str],
        *,
        step_index: int | None = None,
    ) -> dict[str, int]:
        """Max ``node_states.attempt`` per token for resume attempt offsets."""
        result: dict[str, int] = {}
        for i in range(0, len(token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = list(token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            query = (
                select(node_states_table.c.token_id, func.max(node_states_table.c.attempt).label("max_attempt"))
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.token_id.in_(chunk))
                .group_by(node_states_table.c.token_id)
            )
            if step_index is not None:
                query = query.where(node_states_table.c.step_index == step_index)
            for row in self._ops.execute_fetchall(query):
                result[row.token_id] = int(row.max_attempt)
        return result

    def get_open_node_state_ids(
        self,
        run_id: str,
        *,
        node_ids: Sequence[str],
        token_ids: Sequence[str],
    ) -> dict[str, str]:
        """Outstanding OPEN coalesce-hold node_state ids per token."""
        if not node_ids:
            return {}
        result: dict[str, str] = {}
        for i in range(0, len(token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = list(token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            query = (
                select(node_states_table.c.token_id, node_states_table.c.state_id)
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.node_id.in_(list(node_ids)))
                .where(node_states_table.c.token_id.in_(chunk))
                .where(node_states_table.c.status == NodeStateStatus.OPEN.value)
                .order_by(node_states_table.c.token_id, node_states_table.c.attempt)
            )
            for row in self._ops.execute_fetchall(query):
                result[row.token_id] = row.state_id
        return result

    def get_completed_row_ids_for_nodes(
        self,
        run_id: str,
        node_ids: frozenset[str],
    ) -> set[tuple[str, str]]:
        """Completed ``(node_id, row_id)`` pairs for coalesce restore."""
        if not node_ids:
            return set()

        query = (
            select(node_states_table.c.node_id, tokens_table.c.row_id)
            .select_from(
                node_states_table.join(
                    tokens_table,
                    node_states_table.c.token_id == tokens_table.c.token_id,
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id.in_(node_ids),
                node_states_table.c.completed_at.isnot(None),
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return {(row.node_id, row.row_id) for row in rows}

    def has_completed_row_for_node(self, *, run_id: str, node_id: str, row_id: str) -> bool:
        """Return whether one coalesce row completed at one node in one run."""
        query = (
            select(node_states_table.c.state_id)
            .select_from(
                node_states_table.join(
                    tokens_table,
                    node_states_table.c.token_id == tokens_table.c.token_id,
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                tokens_table.c.row_id == row_id,
                node_states_table.c.completed_at.isnot(None),
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def get_released_row_ids_for_nodes(
        self,
        run_id: str,
        node_ids: frozenset[str],
    ) -> set[tuple[str, str]]:
        """Status-COMPLETED ``(node_id, row_id)`` pairs for row_union restore.

        Released-only sibling of :meth:`get_completed_row_ids_for_nodes`: a
        FAILED closure has ``completed_at`` set too, so restore's
        released-group classification must filter on status.
        """
        if not node_ids:
            return set()

        query = (
            select(node_states_table.c.node_id, tokens_table.c.row_id)
            .select_from(
                node_states_table.join(
                    tokens_table,
                    node_states_table.c.token_id == tokens_table.c.token_id,
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id.in_(node_ids),
                node_states_table.c.completed_at.isnot(None),
                node_states_table.c.status == NodeStateStatus.COMPLETED.value,
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return {(row.node_id, row.row_id) for row in rows}

    def has_released_row_for_node(self, *, run_id: str, node_id: str, row_id: str) -> bool:
        """Return whether one row completed as COMPLETED at one node in one run."""
        query = (
            select(node_states_table.c.state_id)
            .select_from(
                node_states_table.join(
                    tokens_table,
                    node_states_table.c.token_id == tokens_table.c.token_id,
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                tokens_table.c.row_id == row_id,
                node_states_table.c.completed_at.isnot(None),
                node_states_table.c.status == NodeStateStatus.COMPLETED.value,
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def has_branch_loss_for_group(self, *, run_id: str, barrier_name: str, row_id: str) -> bool:
        """Return whether the durable ledger records any loss for one barrier group."""
        query = (
            select(coalesce_branch_losses_table.c.loss_id)
            .where(
                coalesce_branch_losses_table.c.run_id == run_id,
                coalesce_branch_losses_table.c.coalesce_name == barrier_name,
                coalesce_branch_losses_table.c.row_id == row_id,
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def find_failed_unrouted_terminal_token_ids(self, run_id: str, token_ids: Sequence[str]) -> frozenset[str]:
        """Token ids holding terminal ``(FAILURE, UNROUTED)`` outcomes.

        This is the ADR-030 aggregation restore reconcile signature for a crash
        after failed-flush terminal writes but before BLOCKED scheduler rows are
        released.
        """
        if not token_ids:
            return frozenset()
        query = (
            select(token_outcomes_table.c.token_id)
            .where(token_outcomes_table.c.run_id == run_id)
            .where(token_outcomes_table.c.token_id.in_(tuple(token_ids)))
            .where(token_outcomes_table.c.completed == 1)
            .where(token_outcomes_table.c.outcome == TerminalOutcome.FAILURE.value)
            .where(token_outcomes_table.c.path == TerminalPath.UNROUTED.value)
        )
        return frozenset(row.token_id for row in self._ops.execute_fetchall(query))

    def get_committed_coalesce_residual(
        self,
        run_id: str,
        *,
        coalesce_node_id: str,
        coalesce_name: str,
        row_id: str,
        blocked_token_ids: Sequence[str],
    ) -> CommittedCoalesceResidual | None:
        """Return an exact completed coalesce effect still lacking continuation."""
        if not blocked_token_ids:
            return None
        effect_rows = self._ops.execute_fetchall(
            select(coalesce_effects_table)
            .where(coalesce_effects_table.c.run_id == run_id)
            .where(coalesce_effects_table.c.coalesce_node_id == coalesce_node_id)
            .where(coalesce_effects_table.c.row_id == row_id)
            .where(coalesce_effects_table.c.status == "completed")
            .order_by(coalesce_effects_table.c.effect_id)
        )
        if not effect_rows:
            return None
        if len(effect_rows) != 1:
            raise AuditIntegrityError(f"Coalesce residual {coalesce_name!r}/{row_id!r} has {len(effect_rows)} completed effect receipts")
        effect = effect_rows[0]
        members = self._ops.execute_fetchall(
            select(
                coalesce_effect_members_table.c.parent_token_id,
                coalesce_effect_members_table.c.parent_state_id,
                coalesce_effect_members_table.c.ordinal,
            )
            .where(coalesce_effect_members_table.c.run_id == run_id)
            .where(coalesce_effect_members_table.c.effect_id == effect.effect_id)
            .order_by(coalesce_effect_members_table.c.ordinal)
        )
        member_ids = tuple(str(row.parent_token_id) for row in members)
        if (
            not member_ids
            or tuple(int(row.ordinal) for row in members) != tuple(range(len(members)))
            or any(type(row.parent_state_id) is not str or not row.parent_state_id for row in members)
        ):
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has incomplete ordered membership")
        blocked_set = frozenset(blocked_token_ids)
        member_set = frozenset(member_ids)
        blocked_members = member_set & blocked_set
        if not blocked_members:
            return None
        if blocked_members != member_set:
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has only a partial BLOCKED member set")

        terminal_rows = []
        for i in range(0, len(member_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = member_ids[i : i + _TOKEN_ID_CHUNK_SIZE]
            terminal_rows.extend(
                self._ops.execute_fetchall(
                    select(
                        token_outcomes_table.c.token_id,
                        token_outcomes_table.c.outcome,
                        token_outcomes_table.c.path,
                        token_outcomes_table.c.join_group_id,
                    )
                    .where(token_outcomes_table.c.run_id == run_id)
                    .where(token_outcomes_table.c.token_id.in_(chunk))
                    .where(token_outcomes_table.c.completed == 1)
                )
            )
        if len(terminal_rows) != len(member_ids):
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} lacks exact terminal parent outcomes")
        terminals = {str(row.token_id): row for row in terminal_rows}
        if len(terminals) != len(member_ids):
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has duplicate terminal parent outcomes")
        for member_id in member_ids:
            outcome = terminals[member_id]
            if not (
                outcome.outcome == TerminalOutcome.SUCCESS.value
                and outcome.path == TerminalPath.COALESCED.value
                and outcome.join_group_id == effect.result_join_group_id
            ):
                raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has divergent parent outcomes")

        token = self._ops.execute_fetchone(
            select(
                tokens_table.c.token_id,
                tokens_table.c.row_id,
                tokens_table.c.join_group_id,
                tokens_table.c.token_data_ref,
                tokens_table.c.step_in_pipeline,
            )
            .where(tokens_table.c.run_id == run_id)
            .where(tokens_table.c.token_id == effect.result_token_id)
        )
        if (
            token is None
            or token.row_id != row_id
            or token.join_group_id != effect.result_join_group_id
            or token.token_data_ref != effect.expected_token_data_ref
            or type(token.token_data_ref) is not str
            or type(token.step_in_pipeline) is not int
        ):
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has divergent result-token identity")
        expected_parent_set_hash = stable_hash(tuple(sorted(member_ids)))
        expected_effect_hash = stable_hash(
            {
                "ordered_parent_ids": member_ids,
                "step_in_pipeline": token.step_in_pipeline,
                "token_data_ref": token.token_data_ref,
            }
        )
        if effect.parent_set_hash != expected_parent_set_hash or effect.effect_hash != expected_effect_hash:
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} has divergent identity hashes")
        has_result_outcome = self._ops.execute_fetchone(
            select(token_outcomes_table.c.outcome_id)
            .where(token_outcomes_table.c.run_id == run_id)
            .where(token_outcomes_table.c.token_id == token.token_id)
            .limit(1)
        )
        has_result_work = self._ops.execute_fetchone(
            select(token_work_items_table.c.work_item_id)
            .where(token_work_items_table.c.run_id == run_id)
            .where(token_work_items_table.c.token_id == token.token_id)
            .limit(1)
        )
        if has_result_outcome is not None or has_result_work is not None:
            raise AuditIntegrityError(f"Coalesce residual effect {effect.effect_id!r} result already has continuation evidence")
        return CommittedCoalesceResidual(
            effect_id=str(effect.effect_id),
            coalesce_node_id=coalesce_node_id,
            coalesce_name=coalesce_name,
            row_id=row_id,
            result_token_id=str(token.token_id),
            result_join_group_id=str(token.join_group_id),
            token_data_ref=token.token_data_ref,
            step_in_pipeline=token.step_in_pipeline,
            member_token_ids=member_ids,
        )

    def list_committed_aggregation_residuals(
        self,
        run_id: str,
        *,
        aggregation_node_id: str,
        blocked_token_ids: Sequence[str],
    ) -> tuple[CommittedAggregationResidual, ...]:
        """Return exact committed aggregation receipts still stranded BLOCKED.

        A receipt is admitted only when the completed batch/node result, exact
        ordered membership, terminal BATCH_CONSUMED inputs, claimed expansion
        group, ordered self-contained child tokens, and absence of any child
        scheduler/outcome continuation agree. Partial or mixed images fail
        closed instead of being mistaken for replayable buffered work.
        """
        if not blocked_token_ids:
            return ()

        consumed_rows = []
        for i in range(0, len(blocked_token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = tuple(blocked_token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            consumed_rows.extend(
                self._ops.execute_fetchall(
                    select(token_outcomes_table.c.token_id, token_outcomes_table.c.batch_id)
                    .where(token_outcomes_table.c.run_id == run_id)
                    .where(token_outcomes_table.c.token_id.in_(chunk))
                    .where(token_outcomes_table.c.completed == 1)
                    .where(token_outcomes_table.c.outcome == TerminalOutcome.TRANSIENT.value)
                    .where(token_outcomes_table.c.path == TerminalPath.BATCH_CONSUMED.value)
                    .order_by(token_outcomes_table.c.batch_id, token_outcomes_table.c.token_id)
                )
            )
        batch_ids = tuple(sorted({str(row.batch_id) for row in consumed_rows if row.batch_id is not None}))
        residuals: list[CommittedAggregationResidual] = []
        blocked_set = frozenset(blocked_token_ids)

        for batch_id in batch_ids:
            batch_row = self._ops.execute_fetchone(
                select(
                    batches_table.c.batch_id,
                    batches_table.c.aggregation_node_id,
                    batches_table.c.expansion_group_id,
                    batches_table.c.status,
                    node_states_table.c.status.label("node_state_status"),
                    node_states_table.c.output_hash,
                )
                .select_from(
                    batches_table.join(
                        node_states_table,
                        and_(
                            batches_table.c.aggregation_state_id == node_states_table.c.state_id,
                            batches_table.c.run_id == node_states_table.c.run_id,
                        ),
                    )
                )
                .where(batches_table.c.batch_id == batch_id)
                .where(batches_table.c.run_id == run_id)
                .where(batches_table.c.aggregation_node_id == aggregation_node_id)
            )
            if batch_row is None:
                batch_identity = self._ops.execute_fetchone(
                    select(batches_table.c.aggregation_node_id, batches_table.c.status)
                    .where(batches_table.c.batch_id == batch_id)
                    .where(batches_table.c.run_id == run_id)
                )
                if (
                    batch_identity is not None
                    and batch_identity.aggregation_node_id == aggregation_node_id
                    and batch_identity.status != BatchStatus.COMPLETED.value
                ):
                    continue
                raise AuditIntegrityError(
                    f"Committed aggregation residual batch {batch_id!r} is missing, foreign, or belongs to another node"
                )
            if (
                batch_row.status != BatchStatus.COMPLETED.value
                or batch_row.node_state_status != NodeStateStatus.COMPLETED.value
                or type(batch_row.expansion_group_id) is not str
                or not batch_row.expansion_group_id
                or type(batch_row.output_hash) is not str
                or not batch_row.output_hash
            ):
                raise AuditIntegrityError(
                    f"Committed aggregation residual batch {batch_id!r} lacks a completed node/result expansion receipt"
                )

            member_rows = self._ops.execute_fetchall(
                select(batch_members_table.c.token_id, batch_members_table.c.ordinal)
                .where(batch_members_table.c.batch_id == batch_id)
                .where(batch_members_table.c.run_id == run_id)
                .order_by(batch_members_table.c.ordinal)
            )
            member_ids = tuple(str(row.token_id) for row in member_rows)
            if not member_ids or tuple(int(row.ordinal) for row in member_rows) != tuple(range(len(member_rows))):
                raise AuditIntegrityError(f"Committed aggregation residual batch {batch_id!r} has non-contiguous membership")
            if not frozenset(member_ids).issubset(blocked_set):
                raise AuditIntegrityError(
                    f"Committed aggregation residual batch {batch_id!r} is not represented by its exact BLOCKED member set"
                )

            terminal_rows = []
            for i in range(0, len(member_ids), _TOKEN_ID_CHUNK_SIZE):
                chunk = member_ids[i : i + _TOKEN_ID_CHUNK_SIZE]
                terminal_rows.extend(
                    self._ops.execute_fetchall(
                        select(
                            token_outcomes_table.c.token_id,
                            token_outcomes_table.c.outcome,
                            token_outcomes_table.c.path,
                            token_outcomes_table.c.batch_id,
                        )
                        .where(token_outcomes_table.c.run_id == run_id)
                        .where(token_outcomes_table.c.token_id.in_(chunk))
                        .where(token_outcomes_table.c.completed == 1)
                        .order_by(token_outcomes_table.c.token_id, token_outcomes_table.c.recorded_at)
                    )
                )
            terminals_by_token: dict[str, list[Any]] = {}
            for row in terminal_rows:
                terminals_by_token.setdefault(str(row.token_id), []).append(row)
            for member_id in member_ids:
                terminal = terminals_by_token[member_id] if member_id in terminals_by_token else []
                if len(terminal) != 1:
                    raise AuditIntegrityError(
                        f"Committed aggregation residual batch {batch_id!r} member {member_id!r} does not have one terminal outcome"
                    )
                outcome = terminal[0]
                batch_consumed = (
                    outcome.outcome == TerminalOutcome.TRANSIENT.value
                    and outcome.path == TerminalPath.BATCH_CONSUMED.value
                    and outcome.batch_id == batch_id
                )
                quarantined = (
                    outcome.outcome == TerminalOutcome.FAILURE.value
                    and outcome.path == TerminalPath.QUARANTINED_AT_SOURCE.value
                    and outcome.batch_id is None
                )
                if not (batch_consumed or quarantined):
                    raise AuditIntegrityError(
                        f"Committed aggregation residual batch {batch_id!r} member {member_id!r} has a divergent terminal outcome"
                    )

            child_rows = self._ops.execute_fetchall(
                select(
                    tokens_table.c.token_id,
                    tokens_table.c.row_id,
                    tokens_table.c.expand_group_id,
                    tokens_table.c.token_data_ref,
                    tokens_table.c.step_in_pipeline,
                    token_parents_table.c.parent_token_id,
                    token_parents_table.c.ordinal,
                )
                .select_from(
                    tokens_table.join(
                        token_parents_table,
                        and_(
                            tokens_table.c.token_id == token_parents_table.c.token_id,
                            tokens_table.c.run_id == token_parents_table.c.run_id,
                        ),
                    )
                )
                .where(tokens_table.c.run_id == run_id)
                .where(tokens_table.c.expand_group_id == batch_row.expansion_group_id)
                .order_by(token_parents_table.c.ordinal)
            )
            if not child_rows or tuple(int(row.ordinal) for row in child_rows) != tuple(range(len(child_rows))):
                raise AuditIntegrityError(f"Committed aggregation residual batch {batch_id!r} has no exact ordered child set")
            parent_ids = {str(row.parent_token_id) for row in child_rows}
            if len(parent_ids) != 1 or not parent_ids.issubset(frozenset(member_ids)):
                raise AuditIntegrityError(f"Committed aggregation residual batch {batch_id!r} has divergent child parentage")

            children: list[CommittedAggregationChild] = []
            for row in child_rows:
                if (
                    type(row.token_data_ref) is not str
                    or not row.token_data_ref
                    or type(row.step_in_pipeline) is not int
                    or row.expand_group_id != batch_row.expansion_group_id
                ):
                    raise AuditIntegrityError(f"Committed aggregation residual batch {batch_id!r} has an incomplete child receipt")
                child_has_outcome = self._ops.execute_fetchone(
                    select(token_outcomes_table.c.outcome_id)
                    .where(token_outcomes_table.c.run_id == run_id)
                    .where(token_outcomes_table.c.token_id == row.token_id)
                    .limit(1)
                )
                child_has_work = self._ops.execute_fetchone(
                    select(token_work_items_table.c.work_item_id)
                    .where(token_work_items_table.c.run_id == run_id)
                    .where(token_work_items_table.c.token_id == row.token_id)
                    .limit(1)
                )
                if child_has_outcome is not None or child_has_work is not None:
                    raise AuditIntegrityError(
                        f"Committed aggregation residual batch {batch_id!r} child {row.token_id!r} already has continuation evidence"
                    )
                children.append(
                    CommittedAggregationChild(
                        token_id=str(row.token_id),
                        row_id=str(row.row_id),
                        expand_group_id=str(row.expand_group_id),
                        token_data_ref=row.token_data_ref,
                        step_in_pipeline=row.step_in_pipeline,
                        parent_token_id=str(row.parent_token_id),
                        ordinal=row.ordinal,
                    )
                )

            residuals.append(
                CommittedAggregationResidual(
                    batch_id=batch_id,
                    aggregation_node_id=aggregation_node_id,
                    output_hash=batch_row.output_hash,
                    member_token_ids=member_ids,
                    children=tuple(children),
                )
            )

        return tuple(residuals)

    def find_released_node_state_token_ids(
        self,
        run_id: str,
        *,
        node_ids: Sequence[str],
        token_ids: Sequence[str],
    ) -> frozenset[str]:
        """Token ids holding a status-COMPLETED node_state at the given nodes.

        Token-scoped sibling of :meth:`get_released_row_ids_for_nodes`: release
        evidence for a (node, row) key proves the GROUP released, but a
        late-arrival residual shares that key while its own closure at the
        barrier node is FAILED. Restore's released-group reconstruction must
        admit only tokens the release itself completed.
        """
        if not node_ids:
            return frozenset()
        result: set[str] = set()
        for i in range(0, len(token_ids), _TOKEN_ID_CHUNK_SIZE):
            chunk = list(token_ids[i : i + _TOKEN_ID_CHUNK_SIZE])
            query = (
                select(node_states_table.c.token_id)
                .where(node_states_table.c.run_id == run_id)
                .where(node_states_table.c.node_id.in_(list(node_ids)))
                .where(node_states_table.c.token_id.in_(chunk))
                .where(node_states_table.c.completed_at.isnot(None))
                .where(node_states_table.c.status == NodeStateStatus.COMPLETED.value)
            )
            result.update(row.token_id for row in self._ops.execute_fetchall(query))
        return frozenset(result)

    def find_duplicate_live_buffered_acceptances(self, run_id: str) -> list[tuple[str, int]]:
        """Run-wide sweep for tokens with more than one live BUFFERED outcome."""
        terminal = token_outcomes_table.alias("terminal_outcomes")
        terminal_witness = (
            select(terminal.c.outcome_id)
            .where(terminal.c.token_id == token_outcomes_table.c.token_id)
            .where(terminal.c.run_id == run_id)
            .where(terminal.c.completed == 1)
            .exists()
        )
        query = (
            select(token_outcomes_table.c.token_id, func.count())
            .where(token_outcomes_table.c.run_id == run_id)
            .where(token_outcomes_table.c.completed == 0)
            .where(token_outcomes_table.c.path == TerminalPath.BUFFERED.value)
            .where(~terminal_witness)
            .group_by(token_outcomes_table.c.token_id)
            .having(func.count() > 1)
            .order_by(token_outcomes_table.c.token_id)
        )
        return [(str(row[0]), int(row[1])) for row in self._ops.execute_fetchall(query)]
