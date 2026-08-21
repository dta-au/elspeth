"""Work queue cursor objects and construction helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from elspeth.contracts import TokenInfo
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.types import CoalesceName, NodeID, RowUnionName


class WorkItemNavigation(Protocol):
    """Navigator surface needed to resolve cursor metadata."""

    def resolve_coalesce_node(self, coalesce_name: CoalesceName) -> NodeID: ...
    def resolve_coalesce_name(self, coalesce_node_id: NodeID) -> CoalesceName: ...
    def resolve_row_union_node(self, row_union_name: RowUnionName) -> NodeID: ...
    def resolve_plugin_for_node(self, node_id: NodeID) -> object | None: ...
    def resolve_next_node(self, node_id: NodeID) -> NodeID | None: ...
    def resolve_branch_first_node(self, branch_name: str) -> NodeID: ...
    def is_fork_gate_node(self, node_id: NodeID) -> bool: ...


@dataclass(frozen=True, slots=True)
class WorkItem:
    """Item in the work queue for DAG processing.

    TRAP — do not express group/scope bindings as WorkItem fields. The
    coalesce_*/row_union_* pairs below are the CURSOR ADDRESS of the barrier
    this item is currently travelling to, not a lineage membership: group
    membership rides TokenInfo (fork/expand lineage), and the group→closer
    binding is a build-time property of the graph (the barrier-scopes spec's
    binding registry, spec §3). The mutual-exclusion check in __post_init__
    therefore constrains only the cursor — one item cannot be blocked at two
    barriers — it does NOT mean a token belongs to at most one group: a token
    can be a fork branch AND an expand child at once. A new barrier kind
    (e.g. the collector) gets a cursor pair here only if work items actually
    block at it; its binding lives in the builder registry, never on this
    dataclass.
    """

    token: TokenInfo
    current_node_id: NodeID | None
    coalesce_node_id: NodeID | None = None
    coalesce_name: CoalesceName | None = None
    row_union_node_id: NodeID | None = None
    row_union_name: RowUnionName | None = None
    on_success_sink: str | None = None

    def __post_init__(self) -> None:
        has_id = self.coalesce_node_id is not None
        has_name = self.coalesce_name is not None
        if has_id != has_name:
            raise OrchestrationInvariantError(
                f"WorkItem coalesce fields must be both set or both None: "
                f"coalesce_node_id={self.coalesce_node_id}, coalesce_name={self.coalesce_name}"
            )
        has_union_id = self.row_union_node_id is not None
        has_union_name = self.row_union_name is not None
        if has_union_id != has_union_name:
            raise OrchestrationInvariantError(
                f"WorkItem row_union fields must be both set or both None: "
                f"row_union_node_id={self.row_union_node_id}, row_union_name={self.row_union_name}"
            )
        if has_name and has_union_name:
            raise OrchestrationInvariantError(
                f"WorkItem cannot target both a coalesce and a row_union barrier: "
                f"coalesce_name={self.coalesce_name}, row_union_name={self.row_union_name}"
            )


@dataclass(frozen=True, slots=True)
class WorkItemFactory:
    """Build WorkItem cursors using topology resolver helpers."""

    navigation: WorkItemNavigation

    def create(
        self,
        *,
        token: TokenInfo,
        current_node_id: NodeID | None,
        coalesce_name: CoalesceName | None = None,
        coalesce_node_id: NodeID | None = None,
        row_union_name: RowUnionName | None = None,
        on_success_sink: str | None = None,
    ) -> WorkItem:
        """Create a cursor with validated coalesce or resolved row-union metadata.

        A single supplied coalesce identity is resolved in the other direction;
        when both are supplied, they must describe the same barrier. Row-union
        names are always resolved to their structural node ids.
        """
        resolved_coalesce_node_id = coalesce_node_id
        resolved_coalesce_name = coalesce_name

        if resolved_coalesce_node_id is None and resolved_coalesce_name is not None:
            resolved_coalesce_node_id = self.navigation.resolve_coalesce_node(resolved_coalesce_name)
        elif resolved_coalesce_node_id is not None and resolved_coalesce_name is None:
            resolved_coalesce_name = self.navigation.resolve_coalesce_name(resolved_coalesce_node_id)
        elif resolved_coalesce_node_id is not None and resolved_coalesce_name is not None:
            expected_node_id = self.navigation.resolve_coalesce_node(resolved_coalesce_name)
            if resolved_coalesce_node_id != expected_node_id:
                raise OrchestrationInvariantError(
                    f"WorkItem coalesce metadata mismatch: coalesce_name={resolved_coalesce_name!r} "
                    f"resolves to node_id={expected_node_id!r}, not supplied "
                    f"coalesce_node_id={resolved_coalesce_node_id!r}"
                )

        row_union_node_id = self.navigation.resolve_row_union_node(row_union_name) if row_union_name is not None else None

        return WorkItem(
            token=token,
            current_node_id=current_node_id,
            coalesce_node_id=resolved_coalesce_node_id,
            coalesce_name=resolved_coalesce_name,
            row_union_node_id=row_union_node_id,
            row_union_name=row_union_name,
            on_success_sink=on_success_sink,
        )

    def create_continuation(
        self,
        *,
        token: TokenInfo,
        current_node_id: NodeID,
        coalesce_name: CoalesceName | None = None,
        row_union_name: RowUnionName | None = None,
        on_success_sink: str | None = None,
    ) -> WorkItem:
        """Create a child item that continues after current node or resumes at a barrier."""
        if coalesce_name is not None or row_union_name is not None:
            # Fork children route to the first processing node in their branch.
            # Non-fork continuations are already mid-branch and advance normally.
            if self.navigation.is_fork_gate_node(current_node_id):
                branch_name = token.branch_name
                if branch_name is None:
                    raise OrchestrationInvariantError(
                        f"Token '{token.token_id}' targets barrier "
                        f"'{coalesce_name or row_union_name}' but branch_name is None. "
                        "Fork children must have branch_name set."
                    )
                return self.create(
                    token=token,
                    current_node_id=self.navigation.resolve_branch_first_node(branch_name),
                    coalesce_name=coalesce_name,
                    row_union_name=row_union_name,
                    on_success_sink=on_success_sink,
                )

            return self.create(
                token=token,
                current_node_id=self.navigation.resolve_next_node(current_node_id),
                coalesce_name=coalesce_name,
                row_union_name=row_union_name,
                on_success_sink=on_success_sink,
            )

        return self.create(
            token=token,
            current_node_id=self.navigation.resolve_next_node(current_node_id),
            on_success_sink=on_success_sink,
        )
