"""One codec for the WorkItem <-> durable scheduler payload mapping.

Extracted from RowProcessor (elspeth-6291c51766): the scheduler field bundle
used to be derived by hand at four sites — source ingest initial claim,
READY barrier emission, idempotent enqueue, and resume rehydrate — kept in
sync only by "MUST mirror" comments. The scheduler row is authoritative for
resume and the idempotent enqueue reconciles by deterministic
``work_item_id`` + strict field equality, so a drifted encoder is a
replay-only corruption risk. The codec owns the derivation in both
directions; the invariant is enforced by
``tests/unit/engine/test_scheduler_work_codec.py``.

The codec is pure: every environmental lookup (payload serialization, node
cursor/step resolution, ingest-sequence lookup, queue/barrier keying,
WorkItem construction) is an injected resolver bound by RowProcessor.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from elspeth.contracts import TokenInfo
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import BarrierEmission, TokenWorkItem
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.contracts.types import CoalesceName, NodeID, RowUnionName
from elspeth.engine.work_items import WorkItem

#: Legacy durable node-cursor marker for terminal-lane rows. Current writers
#: persist NULL past the last node; rehydrate accepts both spellings.
TERMINAL_NODE_SENTINEL = "__terminal__"


class WorkItemFactory(Protocol):
    """Rehydrate seam — matches ``WorkItemFactory.create``."""

    def __call__(
        self,
        *,
        token: TokenInfo,
        current_node_id: NodeID | None,
        coalesce_name: CoalesceName | None = None,
        coalesce_node_id: NodeID | None = None,
        row_union_name: RowUnionName | None = None,
        on_success_sink: str | None = None,
        join_group_id: str | None = None,
    ) -> WorkItem: ...


@dataclass(frozen=True, slots=True)
class ScheduledWorkFields:
    """The durable scheduler field bundle derived from one READY work item.

    Exactly the fields the enqueue/ingest/emission lanes persist for a READY
    continuation; ``BarrierEmission`` and the repository enqueue verbs consume
    it field-for-field.
    """

    token_id: str
    row_id: str
    node_id: str | None
    step_index: int
    ingest_sequence: int
    row_payload_json: str
    queue_key: str | None
    barrier_key: str | None
    on_success_sink: str | None
    branch_name: str | None
    fork_group_id: str | None
    join_group_id: str | None
    expand_group_id: str | None
    lineage_path: tuple[LineageFrame, ...]
    coalesce_node_id: str | None
    coalesce_name: str | None
    row_union_name: str | None


@dataclass(frozen=True)
class SchedulerWorkCodec:
    """Bidirectional WorkItem <-> scheduler payload mapper."""

    serialize_row_payload: Callable[[PipelineRow], str]
    deserialize_row_payload: Callable[[str], PipelineRow]
    resolve_node_cursor: Callable[[NodeID | None], str | None]
    resolve_step_index: Callable[[NodeID | None], int]
    resolve_ingest_sequence: Callable[[str], int]
    queue_key_for_item: Callable[[WorkItem], str | None]
    barrier_key_for_item: Callable[[WorkItem], str | None]
    create_work_item: WorkItemFactory

    def ready_fields(self, item: WorkItem, *, ingest_sequence: int | None = None) -> ScheduledWorkFields:
        """Derive the durable field bundle for a READY continuation.

        ``ingest_sequence`` is resolver-derived from the row by default; the
        fenced source-ingest path passes it explicitly because the row is
        inserted in the same transaction and is not yet resolvable.
        """
        token = item.token
        return ScheduledWorkFields(
            token_id=token.token_id,
            row_id=token.row_id,
            node_id=self.resolve_node_cursor(item.current_node_id),
            step_index=self.resolve_step_index(item.current_node_id),
            ingest_sequence=(self.resolve_ingest_sequence(token.row_id) if ingest_sequence is None else ingest_sequence),
            row_payload_json=self.serialize_row_payload(token.row_data),
            queue_key=self.queue_key_for_item(item),
            barrier_key=self.barrier_key_for_item(item),
            on_success_sink=item.on_success_sink,
            branch_name=token.branch_name,
            fork_group_id=token.fork_group_id,
            join_group_id=item.join_group_id,
            expand_group_id=token.expand_group_id,
            lineage_path=token.lineage_path,
            coalesce_node_id=str(item.coalesce_node_id) if item.coalesce_node_id is not None else None,
            coalesce_name=str(item.coalesce_name) if item.coalesce_name is not None else None,
            row_union_name=str(item.row_union_name) if item.row_union_name is not None else None,
        )

    def ready_emission(self, item: WorkItem) -> BarrierEmission:
        """Build the READY continuation emission for an atomic barrier completion."""
        fields = self.ready_fields(item)
        return BarrierEmission(
            token_id=fields.token_id,
            row_id=fields.row_id,
            node_id=fields.node_id,
            step_index=fields.step_index,
            ingest_sequence=fields.ingest_sequence,
            row_payload_json=fields.row_payload_json,
            queue_key=fields.queue_key,
            barrier_key=fields.barrier_key,
            on_success_sink=fields.on_success_sink,
            branch_name=fields.branch_name,
            fork_group_id=fields.fork_group_id,
            join_group_id=fields.join_group_id,
            expand_group_id=fields.expand_group_id,
            lineage_path=fields.lineage_path,
            coalesce_node_id=fields.coalesce_node_id,
            coalesce_name=fields.coalesce_name,
            row_union_name=fields.row_union_name,
        )

    def work_item_from_scheduler(self, scheduled: TokenWorkItem) -> WorkItem:
        """Rehydrate a scheduler work item from its durable payload snapshot."""
        current_node_id = None if scheduled.node_id is None or scheduled.node_id == TERMINAL_NODE_SENTINEL else NodeID(scheduled.node_id)
        token = TokenInfo(
            row_id=scheduled.row_id,
            token_id=scheduled.token_id,
            row_data=self.deserialize_row_payload(scheduled.row_payload_json),
            branch_name=scheduled.branch_name,
            fork_group_id=scheduled.fork_group_id,
            expand_group_id=scheduled.expand_group_id,
            lineage_path=scheduled.lineage_path,
        )
        return self.create_work_item(
            token=token,
            current_node_id=current_node_id,
            coalesce_node_id=NodeID(scheduled.coalesce_node_id) if scheduled.coalesce_node_id is not None else None,
            coalesce_name=CoalesceName(scheduled.coalesce_name) if scheduled.coalesce_name is not None else None,
            row_union_name=RowUnionName(scheduled.row_union_name) if scheduled.row_union_name is not None else None,
            on_success_sink=scheduled.on_success_sink,
            join_group_id=scheduled.join_group_id,
        )
