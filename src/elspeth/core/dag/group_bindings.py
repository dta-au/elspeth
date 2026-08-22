"""Unified group-binding registry (barrier-scopes spec §3).

ONE registry of bound groups. The branch→closer views the routing code
wants (`_branch_to_coalesce`, `_branch_to_row_union`) are DERIVED from it,
never assembled independently — a second assembly path is how the two-map
drift the spec retires would come back.

Exclusivity (spec §7 rule 1): each frame source binds at most one closer,
and each closer closes at most one group. The registry enforces both at
construction; the builder's per-branch duplicate checks remain as the
authoring-facing diagnostics (they fire first, with better messages).

``binding_for`` is the settle-member walk's frame resolver (spec §6.1):
FORK frames resolve statically — a FORK frame's ``member_key`` IS the
declared branch name (spec §4.1), and rosters are member-disjoint. EXPAND
group ids are runtime-minted (``generate_id()``), so the opener's mint path
registers each new group via ``register_expand_group`` (WS3 wires the
single TokenManager call site; on takeover/resume the index re-derives
from ``group_records``, which carries the opener). An unregistered frame
is inert — ``None``, nothing staged, no roster watching (spec §2).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.freeze import deep_freeze
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.types import (
    BranchName,
    CoalesceName,
    CollectorName,
    GateName,
    NodeID,
    RowUnionName,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from elspeth.core.config import CoalesceSettings, RowUnionSettings, ScopeSettings
    from elspeth.core.dag.builder import _CoalescePlan, _RowUnionBranchSpec


class CloserKind(StrEnum):
    """Closer taxonomy for bound groups.

    StrEnum (2026-08-22 synthesis): members ARE their string values
    ("coalesce"/"row_union"/"collector"), so every serialized surface —
    composer NodeSpec dicts, guidedDecoder wire shapes, audit JSON,
    ``GraphValidationError.component_type`` — keeps carrying plain strings
    with zero serialization change. WS3 compares against the MEMBERS
    (``binding.closer_kind is CloserKind.COLLECTOR``), never string
    literals.
    """

    COALESCE = "coalesce"
    ROW_UNION = "row_union"
    COLLECTOR = "collector"


@dataclass(frozen=True, slots=True)
class GroupBinding:
    """The build-time group→closer association for ONE bound group."""

    kind: FrameKind
    opener_node_id: NodeID
    opener_name: str
    closer_node_id: NodeID
    closer_name: str
    closer_kind: CloserKind
    policy: str
    on_group_failure: str | None
    member_roster: tuple[str, ...]


@dataclass(frozen=True)
class GroupBindingRegistry:
    """All bound groups of one build. Empty for pipelines with no bound group."""

    bindings: tuple[GroupBinding, ...]
    # Derived indices, built ONCE in __post_init__ and frozen (init=False
    # keeps the public constructor shape at exactly `bindings`; frozen=
    # blocks rebinding, and neither index is ever mutated again after
    # construction — deep_freeze via object.__setattr__ is the house
    # pattern for a frozen dataclass's __post_init__-computed fields).
    _fork_binding_by_member: Mapping[str, GroupBinding] = field(default_factory=dict, init=False, repr=False, compare=False)
    _expand_binding_by_opener: Mapping[str, GroupBinding] = field(default_factory=dict, init=False, repr=False, compare=False)
    # Runtime EXPAND-group index: group ids are minted at runtime, so the
    # opener's mint path feeds this via register_expand_group. Mutable BY
    # DESIGN inside the frozen registry — it is bookkeeping, not identity
    # (excluded from eq/repr); see config/cicd/enforce_frozen_annotations
    # for the reviewed exemption (precedent: ToolBatchContext.discovery_cache).
    _expand_groups: dict[str, GroupBinding] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        opener_counts = Counter(b.opener_node_id for b in self.bindings)
        dup_openers = sorted(str(n) for n, c in opener_counts.items() if c > 1)
        if dup_openers:
            raise ValueError(f"Group opener(s) {dup_openers} bound twice — each frame source binds at most one closer (spec §7 rule 1)")
        closer_counts = Counter(b.closer_node_id for b in self.bindings)
        dup_closers = sorted(str(n) for n, c in closer_counts.items() if c > 1)
        if dup_closers:
            raise ValueError(f"Closer(s) {dup_closers} bound twice — each closer closes at most one group (spec §7 rule 1)")
        fork_binding_by_member: dict[str, GroupBinding] = {}
        expand_binding_by_opener: dict[str, GroupBinding] = {}
        for binding in self.bindings:
            if binding.kind is FrameKind.FORK:
                for member in binding.member_roster:
                    if member in fork_binding_by_member:
                        raise ValueError(
                            f"Branch '{member}' appears in two bound forks' rosters — branch names are "
                            f"one-producer connections, so roster membership must be a function "
                            f"(binding_for's FORK resolution keys on it)"
                        )
                    fork_binding_by_member[member] = binding
            else:
                expand_binding_by_opener[binding.opener_name] = binding
        object.__setattr__(self, "_fork_binding_by_member", deep_freeze(fork_binding_by_member))
        object.__setattr__(self, "_expand_binding_by_opener", deep_freeze(expand_binding_by_opener))

    def by_opener_node(self) -> dict[NodeID, GroupBinding]:
        return {b.opener_node_id: b for b in self.bindings}

    def by_closer_node(self) -> dict[NodeID, GroupBinding]:
        return {b.closer_node_id: b for b in self.bindings}

    def binding_for(self, frame: LineageFrame) -> GroupBinding | None:
        """Resolve one lineage frame to its bound closer (None = inert frame).

        The settle-member walk's keyed lookup (spec §6.1): FORK frames key on
        ``member_key`` (the declared branch name); EXPAND frames key on the
        runtime-registered ``group_id``. None means nobody waits — pure
        provenance, nothing staged (spec §2).
        """
        if frame.kind is FrameKind.FORK:
            return self._fork_binding_by_member.get(frame.member_key)
        return self._expand_groups.get(frame.group_id)

    def register_expand_group(self, group_id: str, *, opener_name: str) -> GroupBinding | None:
        """Associate a runtime-minted EXPAND group id with its scope binding.

        Called unconditionally from the opener's mint path (WS3 wires the
        TokenManager call site): a declared scope opener returns (and
        records) its binding; an undeclared expand returns None and records
        nothing — its frames stay inert forever. Idempotent per group id;
        re-registering one group under a DIFFERENT opener is an integrity
        violation (group ids are unique per mint).
        """
        binding = self._expand_binding_by_opener.get(opener_name)
        if binding is None:
            return None
        existing = self._expand_groups.get(group_id)
        if existing is not None:
            if existing is not binding:
                raise ValueError(
                    f"EXPAND group '{group_id}' already registered to opener '{existing.opener_name}'; "
                    f"refusing re-registration under '{opener_name}'"
                )
            return existing
        self._expand_groups[group_id] = binding
        return binding

    def branch_to_coalesce(self) -> dict[BranchName, CoalesceName]:
        return {
            BranchName(member): CoalesceName(b.closer_name)
            for b in self.bindings
            if b.kind is FrameKind.FORK and b.closer_kind is CloserKind.COALESCE
            for member in b.member_roster
        }

    def branch_to_row_union(self) -> dict[BranchName, RowUnionName]:
        return {
            BranchName(member): RowUnionName(b.closer_name)
            for b in self.bindings
            if b.kind is FrameKind.FORK and b.closer_kind is CloserKind.ROW_UNION
            for member in b.member_roster
        }


def build_group_binding_registry(
    *,
    fork_rosters: Mapping[GateName, tuple[NodeID, tuple[str, ...]]],
    coalesce_plans: Mapping[CoalesceName, _CoalescePlan],
    coalesce_settings_by_name: Mapping[CoalesceName, CoalesceSettings],
    coalesce_ids: Mapping[CoalesceName, NodeID],
    row_union_branch_specs: Mapping[BranchName, _RowUnionBranchSpec],
    row_union_settings_by_name: Mapping[RowUnionName, RowUnionSettings],
    row_union_ids: Mapping[RowUnionName, NodeID],
    scope_settings: Sequence[ScopeSettings],
    collector_ids: Mapping[CollectorName, NodeID],
    transform_ids_by_name: Mapping[str, NodeID],
) -> GroupBindingRegistry:
    """Assemble every bound group (FORK + EXPAND) of one build into one registry."""
    # branch_name -> CoalesceName / RowUnionName, derived from coalesce_plans
    # and row_union_branch_specs (never assembled from a second,
    # independently-maintained map).
    branch_to_coalesce_name: dict[str, CoalesceName] = {
        str(branch_spec.branch_name): plan.name for plan in coalesce_plans.values() for branch_spec in plan.branches
    }
    branch_to_row_union_name: dict[str, RowUnionName] = {
        str(branch_key): spec.row_union_name for branch_key, spec in row_union_branch_specs.items()
    }

    bindings: list[GroupBinding] = []

    for gate_name, (gate_node_id, fork_to) in fork_rosters.items():
        # A gate's fork_to is not required to be homogeneous: builder.py
        # resolves each branch independently against coalesce/row_union
        # specs or a direct sink name, so one gate can mix a bound branch
        # with a sink-bound one, or (same-gate only) a coalesce branch with
        # a row_union branch. "First bound branch wins, in fork_to order" is
        # the brief's explicit interim (Task 6 rule 2 tightens this to
        # reject a gate spanning two closers at build time — see the
        # PINNED interim test in test_group_bindings.py for the exact
        # divergence this produces against the legacy graph maps until
        # then). Whichever closer wins, member_roster is filtered to ONLY
        # the fork_to branches that actually resolve to THAT closer — a
        # sink-bound or other-closer-bound sibling branch must never join
        # the roster (review round 1, finding 1 Case A).
        resolved_coalesce: CoalesceName | None = None
        resolved_row_union: RowUnionName | None = None
        for branch in fork_to:
            if branch in branch_to_coalesce_name:
                resolved_coalesce = branch_to_coalesce_name[branch]
                break
            if branch in branch_to_row_union_name:
                resolved_row_union = branch_to_row_union_name[branch]
                break

        if resolved_coalesce is not None:
            coalesce_settings = coalesce_settings_by_name[resolved_coalesce]
            bindings.append(
                GroupBinding(
                    kind=FrameKind.FORK,
                    opener_node_id=gate_node_id,
                    opener_name=str(gate_name),
                    closer_node_id=coalesce_ids[resolved_coalesce],
                    closer_name=str(resolved_coalesce),
                    closer_kind=CloserKind.COALESCE,
                    policy=coalesce_settings.policy,
                    on_group_failure=None,
                    member_roster=tuple(b for b in fork_to if branch_to_coalesce_name.get(b) == resolved_coalesce),
                )
            )
        elif resolved_row_union is not None:
            bindings.append(
                GroupBinding(
                    kind=FrameKind.FORK,
                    opener_node_id=gate_node_id,
                    opener_name=str(gate_name),
                    closer_node_id=row_union_ids[resolved_row_union],
                    closer_name=str(resolved_row_union),
                    closer_kind=CloserKind.ROW_UNION,
                    policy="require_all",
                    on_group_failure=None,
                    member_roster=tuple(b for b in fork_to if branch_to_row_union_name.get(b) == resolved_row_union),
                )
            )

    for scope in scope_settings:
        bindings.append(
            GroupBinding(
                kind=FrameKind.EXPAND,
                opener_node_id=transform_ids_by_name[scope.opener],
                opener_name=scope.opener,
                closer_node_id=collector_ids[CollectorName(scope.closer)],
                closer_name=scope.closer,
                closer_kind=CloserKind.COLLECTOR,
                policy=scope.policy,
                on_group_failure=scope.on_group_failure,
                member_roster=(),
            )
        )

    return GroupBindingRegistry(bindings=tuple(bindings))
