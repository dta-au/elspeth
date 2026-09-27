"""Canonical union-merge algorithm for coalesce schema/contract merging.

This module is the single source of truth for policy-aware union merge
semantics. Two thin wrappers consume the core algorithm:

1. merge_union_fields (core/dag/coalesce_merge.py): build-time merge of
   SchemaConfig field definitions during DAG construction.
2. merge_union_contracts (this module): runtime merge of SchemaContract
   instances for all-OBSERVED union coalesces.

Both derive required/nullable flags from merge_union_field_flags so build-time
and runtime merges cannot diverge.

The certain-conflict predicate (``certain_union_type_conflict``) lives here
too: the build (``core/dag/builder.py``) and the composer
(``web/composer/state.py``) both call it to refuse, before row 1, a union
coalesce whose every row would fail ``merge_union_contracts`` — the same
exact-type comparison, over types both surfaces resolve from the plugins'
published stamp tables.

A THIRD, separate operation lives here too: ``join_batch_contracts``, the
DESCRIPTION join (J1) used only where several PRODUCERS' rows meet — the sink
batch merge and display headers (ADR-050). It never raises on a type
difference: two producers that type one field differently are both telling
the truth about their own rows, and the batch description is ``object``. It
is the wrong tool for a coalesce (a union coalesce PROMISES one type to its
consumers, so a conflict there is a routed row failure) and for a node's own
output record (a node's emissions all carry the same declared types, so a
conflict there is an owned-code bug). Those two seams keep the raising
``merge_union_contracts`` / ``SchemaContract.merge_for_node_evolution``.

Policy semantics (shared by both wrappers):

- require_all (OR semantics): A field is required if required in ANY branch.
  Since all branches always arrive under require_all, any branch's guarantee
  is honored in the merged output. Branch-exclusive fields keep their source
  branch's flags (the branch always arrives).

- other policies (AND semantics): A field is required only if required in ALL
  branches. Since some branches may be lost, only shared guarantees survive.
  Branch-exclusive fields are forced optional and nullable (the branch may
  not arrive, leaving the field absent/None).

Nullable semantics for shared fields under require_all depend on
collision_policy (D5 fix): first_wins/last_wins use the winning branch's
nullable; fail uses OR of all branches (conservative). Under non-require_all,
nullable is always OR of all branches (P1 soundness: any branch might be the
one that arrives).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast

from elspeth.contracts.errors import ContractMergeError
from elspeth.contracts.schema_contract import FieldContract, SchemaContract


class UnionTypeConflictError(Exception):
    """Two branches contribute incompatible types for the same field.

    Neutral conflict signal raised by merge_union_field_flags. Wrappers
    translate it to their domain error (GraphValidationError at build time,
    ContractMergeError at runtime).

    Attributes:
        field: The field name with conflicting types
        type_a: The type key from the first-seen branch
        type_b: The type key from the conflicting branch
        branch_a: The branch that first contributed the field
        branch_b: The branch with the conflicting type
    """

    def __init__(
        self,
        *,
        field: str,
        type_a: Hashable,
        type_b: Hashable,
        branch_a: str,
        branch_b: str,
    ) -> None:
        self.field = field
        self.type_a = type_a
        self.type_b = type_b
        self.branch_a = branch_a
        self.branch_b = branch_b
        super().__init__(
            f"Incompatible types for field '{field}' in union merge: "
            f"branch '{branch_a}' has {type_a!r}, branch '{branch_b}' has {type_b!r}."
        )


def merge_union_field_flags(
    branch_fields: Mapping[str, Sequence[tuple[str, Hashable, bool, bool]]],
    *,
    require_all: bool,
    collision_policy: Literal["last_wins", "first_wins", "fail"] = "last_wins",
    branch_order: Sequence[str] | None = None,
) -> dict[str, tuple[Hashable, bool, bool, str]]:
    """Merge per-field (type, required, nullable) flags across branches.

    The canonical per-field union algorithm. Type keys are opaque hashables:
    the build-time wrapper passes field_type strings, the runtime wrapper
    passes Python types — the algorithm is shared, the field dataclasses
    are not.

    Args:
        branch_fields: Map of branch name to sequence of
            (name, type_key, required, nullable) tuples. EVERY key counts as
            a contributing branch (even with an empty sequence) — callers
            control membership by pre-filtering non-contributing branches.
        require_all: If True, use OR semantics for required; if False, use AND
        collision_policy: How shared-field nullable resolves under require_all.
        branch_order: Iteration order for branches. Names absent from
            branch_fields are skipped; if None, uses dict iteration order.

    Returns:
        Map of field name to (type_key, required, nullable,
        first_contributing_branch), in first-seen insertion order.

    Raises:
        UnionTypeConflictError: If branches have incompatible type keys for
            the same field name.
    """
    seen_types: dict[str, tuple[Hashable, bool, bool, str]] = {}
    branches_with_field: dict[str, set[str]] = {}
    contributing_branches: set[str] = set()

    branch_names = branch_order if branch_order is not None else list(branch_fields.keys())

    for branch_name in branch_names:
        if branch_name not in branch_fields:
            # branch_order may include branches the caller filtered out (e.g.,
            # observed branches at build time, lost branches at runtime).
            continue
        contributing_branches.add(branch_name)
        for name, type_key, required, nullable in branch_fields[branch_name]:
            if name not in branches_with_field:
                branches_with_field[name] = set()
            branches_with_field[name].add(branch_name)

            if name in seen_types:
                prior_type, prior_req, prior_nullable, prior_branch = seen_types[name]
                if prior_type != type_key:
                    raise UnionTypeConflictError(
                        field=name,
                        type_a=prior_type,
                        type_b=type_key,
                        branch_a=prior_branch,
                        branch_b=branch_name,
                    )
                if require_all:
                    # OR for required: required if required in ANY branch.
                    merged_req = prior_req or required
                    # Nullable depends on collision_policy (D5 fix):
                    # - first_wins: keep first branch's nullable (prior_nullable)
                    # - last_wins: use current branch's nullable
                    # - fail: OR of all (conservative; collisions fail at runtime)
                    if collision_policy == "first_wins":
                        merged_nullable = prior_nullable  # First seen wins
                    elif collision_policy == "last_wins":
                        merged_nullable = nullable  # Last seen wins
                    else:  # "fail" — conservative OR
                        merged_nullable = prior_nullable or nullable
                else:
                    # AND: optional if optional in ANY branch.
                    merged_req = prior_req and required
                    # Under partial-arrival (non-require_all), any branch might be
                    # the one that arrives. The collision_policy determines VALUE
                    # resolution if multiple arrive, but the SCHEMA must be sound
                    # for all arrival combinations. If ANY branch can produce None,
                    # the merged schema must be nullable. (P1 soundness fix)
                    merged_nullable = prior_nullable or nullable
                seen_types[name] = (prior_type, merged_req, merged_nullable, prior_branch)
            else:
                seen_types[name] = (type_key, required, nullable, branch_name)

    # Branch-exclusive field handling (post-loop pass):
    # - require_all: keep the source-branch flags (branch always arrives)
    # - other policies: force optional + nullable (branch may not arrive)
    if not require_all:
        for field_name in list(seen_types):
            if branches_with_field[field_name] != contributing_branches:
                ftype, _, _, first_branch = seen_types[field_name]
                seen_types[field_name] = (ftype, False, True, first_branch)

    return seen_types


@dataclass(frozen=True, slots=True)
class KnownBranchFieldType:
    """A field's runtime contract type on one union-coalesce branch, known before row 1.

    ``field_type`` is the schema-DSL token (``"any"`` included: a declared
    ``any`` is the contract type ``object``, which conflicts with every
    concrete type at the runtime merge). ``declared_by`` names the
    declaring node(s) for the refusal message, already rendered by the
    surface that resolved the type (the DAG build or the composer).
    """

    field_type: str
    declared_by: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnionFieldTypeConflict:
    """Two union-coalesce branches that each certainly carry ``field`` under different contract types."""

    field: str
    branch_a: str
    type_a: KnownBranchFieldType
    branch_b: str
    type_b: KnownBranchFieldType


def certain_union_type_conflict(
    branch_fields: Mapping[str, Mapping[str, KnownBranchFieldType]],
    *,
    all_branches_merge: bool,
    branch_order: Sequence[str],
) -> UnionFieldTypeConflict | None:
    """The first union-merge type conflict that is certain from config, or None.

    The ONE predicate behind the build-time refusal
    (``coalesce_union_type_incompatible``) of a union coalesce whose every
    row would fail the runtime merge with ``contract_type_conflict``. Both
    the DAG build (``builder.py``, every schema mode including all-observed)
    and the composer (``web/composer/state.py``) call it; each surface
    resolves the per-branch inputs from the same stamp tables
    (``BaseTransform.output_field_declarations``) and presence walks.

    ``branch_fields`` maps each branch to the fields it BOTH guarantees
    present on every row AND knows the runtime contract type of; a field
    whose type or presence is not provable is simply absent (abstention).
    A conflict is two such branches typing one field differently, compared
    exactly as the runtime merge compares contract types
    (``merge_union_field_flags``: ``int`` vs ``float`` conflicts too).

    Certain only when every branch merges on every row:
    ``all_branches_merge`` is ``CoalesceSettings.has_all_branch_semantics``
    (``require_all``, or ``quorum`` equal to the branch count). Under
    ``first`` the runtime merges one arrival, and under ``best_effort`` or a
    smaller quorum a row whose conflicting sibling was diverted or late
    merges without it, so those policies abstain and the per-row
    ``contract_type_conflict`` remains the residual.

    Deterministic: fields in sorted order, branches in ``branch_order``.
    """
    if not all_branches_merge:
        return None
    ordered = [branch for branch in branch_order if branch in branch_fields]
    field_names = sorted({name for branch in ordered for name in branch_fields[branch]})
    for field_name in field_names:
        first: tuple[str, KnownBranchFieldType] | None = None
        for branch in ordered:
            if field_name not in branch_fields[branch]:
                continue
            known = branch_fields[branch][field_name]
            if first is None:
                first = (branch, known)
            elif known.field_type != first[1].field_type:
                return UnionFieldTypeConflict(
                    field=field_name,
                    branch_a=first[0],
                    type_a=first[1],
                    branch_b=branch,
                    type_b=known,
                )
    return None


def union_type_conflict_message(coalesce_label: str, conflict: UnionFieldTypeConflict) -> str:
    """The actionable refusal text for a certain union-merge type conflict (build and composer share it)."""
    concrete = sorted({conflict.type_a.field_type, conflict.type_b.field_type} - {"any"})
    declaration = f"'{conflict.field}: {concrete[0]}'" if len(concrete) == 1 else f"'{conflict.field}' with one type"
    return (
        f"Coalesce {coalesce_label} receives incompatible types for field '{conflict.field}' in union merge: "
        f"branch '{conflict.branch_a}' carries {conflict.type_a.field_type!r} "
        f"(declared by {', '.join(conflict.type_a.declared_by)}), "
        f"branch '{conflict.branch_b}' carries {conflict.type_b.field_type!r} "
        f"(declared by {', '.join(conflict.type_b.declared_by)}). "
        "Every row would fail this merge at runtime (contract_type_conflict); 'any' is a type of its own here, "
        "not a wildcard. A union coalesce needs one type per field: declare "
        f"{declaration} on the output schema of every branch's last node (mode: flexible), "
        "or write a rewritten value under a new name."
    )


def resolve_original_name_collisions(fields: Sequence[FieldContract]) -> tuple[FieldContract, ...]:
    """Break cross-branch original_name collisions deterministically.

    Sibling branches may rename one upstream field to different normalized
    names, so a union merge can produce two fields carrying the same
    original_name — violating SchemaContract's original->normalized bijection.
    The ambiguous lineage is dropped from the contract by setting
    original_name = normalized_name (identity) on every colliding field;
    cross-branch provenance stays in the union audit metadata
    (union_field_origins / collision_values), not the contract.

    Iterates to a fixed point because an identity reassignment can itself
    collide with another field's original_name. Terminates: normalized names
    are unique, so each colliding group has at most one identity member and
    every pass strictly shrinks the set of non-identity original_names.
    """
    resolved = tuple(fields)
    while True:
        counts = Counter(fc.original_name for fc in resolved)
        colliding = {name for name, count in counts.items() if count > 1}
        if not colliding:
            return resolved
        resolved = tuple(replace(fc, original_name=fc.normalized_name) if fc.original_name in colliding else fc for fc in resolved)


def merge_union_contracts(
    branch_contracts: Mapping[str, SchemaContract],
    *,
    require_all: bool,
    collision_policy: Literal["last_wins", "first_wins", "fail"] = "last_wins",
    branch_order: Sequence[str] | None = None,
    coalesce_id: str | None = None,
) -> SchemaContract:
    """Merge runtime SchemaContracts at a union coalesce, policy-aware.

    Runtime sibling of merge_union_fields (core/dag/coalesce_merge.py) — both
    delegate flag computation to merge_union_field_flags, so build-time and
    runtime union merges share one algorithm.

    Unlike the build-time wrapper, EVERY branch contributes — an arrived
    branch with zero fields still forces siblings' exclusive fields optional
    under non-require_all policies (it arrived; its rows lack those fields).

    Runtime-only attributes are layered on top of the core flags:
    - mode: most restrictive across branches (FIXED > FLEXIBLE > OBSERVED)
    - locked: True if any branch is locked
    - source: 'declared' if any branch with the field declares it
    - original_name: from the first contributing branch (in branch_order)
      that carries the field; fields whose original_name would collide
      across branches fall back to identity (resolve_original_name_collisions)

    Args:
        branch_contracts: Map of branch name to that branch's SchemaContract
        require_all: If True, use OR semantics for required; if False, use AND
        collision_policy: How shared-field nullable resolves under require_all
        branch_order: Declaration order of branches for deterministic
            first/last semantics. Names absent from branch_contracts are
            skipped; if None, uses dict iteration order.
        coalesce_id: Optional node ID (reserved for error context; the raised
            ContractMergeError carries field/type detail only)

    Returns:
        New merged SchemaContract with fields sorted by normalized_name.

    Raises:
        ContractMergeError: If branches have incompatible types for the same
            field name.
        ValueError: If branch_contracts is empty (nothing to merge).
    """
    if not branch_contracts:
        node_desc = f"'{coalesce_id}'" if coalesce_id else "coalesce node"
        raise ValueError(f"merge_union_contracts at {node_desc} requires at least one branch contract")

    branch_names = branch_order if branch_order is not None else list(branch_contracts.keys())
    ordered_names = [name for name in branch_names if name in branch_contracts]

    if len(ordered_names) == 1:
        # Single contributing branch (e.g., policy='first' merging the first
        # arrival): the union of one contract is that contract. Return it
        # unchanged — no flag rewrites apply (every field is "shared" w.r.t.
        # the contributing set) and callers rely on field order/identity
        # passing through.
        return branch_contracts[ordered_names[0]]

    branch_fields: dict[str, list[tuple[str, type, bool, bool]]] = {
        name: [(fc.normalized_name, fc.python_type, fc.required, fc.nullable) for fc in branch_contracts[name].fields]
        for name in ordered_names
    }

    try:
        merged_flags = merge_union_field_flags(
            branch_fields,
            require_all=require_all,
            collision_policy=collision_policy,
            branch_order=ordered_names,
        )
    except UnionTypeConflictError as e:
        # Type keys here are always Python types (built from fc.python_type).
        # ``object`` is rendered ``any``: it is the contract type of a declared
        # ``any``, and the build-time refusal of the same conflict
        # (``coalesce_union_type_incompatible``) names it ``any``, so the
        # residual per-row reason uses the operator's vocabulary too.
        raise ContractMergeError(
            field=e.field,
            type_a=_union_type_label(cast(type, e.type_a)),
            type_b=_union_type_label(cast(type, e.type_b)),
        ) from e

    # Mode precedence: FIXED > FLEXIBLE > OBSERVED (most restrictive wins)
    mode_order: dict[str, int] = {"FIXED": 0, "FLEXIBLE": 1, "OBSERVED": 2}
    merged_mode = min(
        (branch_contracts[name].mode for name in ordered_names),
        key=lambda m: mode_order[m],
    )
    merged_locked = any(branch_contracts[name].locked for name in ordered_names)

    merged_fields: list[FieldContract] = []
    for field_name in sorted(merged_flags):
        type_key, required, nullable, _first_branch = merged_flags[field_name]
        carriers = [fc for name in ordered_names if (fc := branch_contracts[name].find_field(field_name)) is not None]
        # carriers is non-empty: the field came from at least one branch's fields
        merged_fields.append(
            FieldContract(
                normalized_name=field_name,
                original_name=carriers[0].original_name,
                python_type=cast(type, type_key),
                required=required,
                source="declared" if any(fc.source == "declared" for fc in carriers) else "inferred",
                nullable=nullable,
            )
        )

    return SchemaContract(
        mode=merged_mode,
        fields=resolve_original_name_collisions(merged_fields),
        locked=merged_locked,
    )


def _union_type_label(python_type: type) -> str:
    """A conflicting contract type's name in the schema vocabulary (``object`` is ``any``)."""
    return "any" if python_type is object else python_type.__name__


_MODE_ORDER: dict[str, int] = {"FIXED": 0, "FLEXIBLE": 1, "OBSERVED": 2}


def join_batch_contracts(contracts: Sequence[SchemaContract]) -> SchemaContract:
    """Describe the rows of several producers with one contract (the J1 join).

    ``contracts`` are the row contracts of N sibling tokens bound for one sink
    (or one display-header table). Each is a truthful description of its own
    rows; the result is a truthful description of ALL of them, and it is a
    lattice join, so it is commutative, associative and idempotent (pinned by
    ``tests/property/contracts/test_schema_contract_properties.py``):

    - a field carried with ONE type keeps that type; carried with different
      types it becomes ``object`` (``int`` and ``float`` are different, and so
      are ``bool`` and ``int``); ``object`` absorbs everything. ``int`` ⊔
      ``float`` stays ``object`` although an ``int`` value satisfies a
      ``float`` declaration (``declared_type_admits``): ``object`` is sound
      for both, and the join describes the carriers rather than widening
      one of them;
    - ``nullable`` is OR across carriers, and a field some member does not
      carry is nullable (those members' rows lack it);
    - ``required`` is AND across carriers, and a field some member does not
      carry is optional;
    - ``source`` is ``declared`` if any carrier declares it;
    - ``original_name`` is kept when every carrier agrees and falls back to
      the identity (the normalized name) when they disagree, so the result
      does not depend on which producer's row arrived first;
    - mode is the most restrictive (FIXED > FLEXIBLE > OBSERVED) and
      ``locked`` is OR.

    Soundness: every contributing row validates against the join
    (``validate()`` skips ``object`` fields, admits None on a nullable or
    optional field, and never sees a missing optional field as a violation).

    Raises:
        ValueError: If ``contracts`` is empty (nothing to describe).
    """
    if not contracts:
        raise ValueError("join_batch_contracts requires at least one contract")
    if len(contracts) == 1:
        return contracts[0]

    carriers_by_name: dict[str, list[FieldContract]] = {}
    for contract in contracts:
        for fc in contract.fields:
            carriers_by_name.setdefault(fc.normalized_name, []).append(fc)

    member_count = len(contracts)
    joined: list[FieldContract] = []
    for name in sorted(carriers_by_name):
        carriers = carriers_by_name[name]
        carried_by_all = len(carriers) == member_count
        types = {fc.python_type for fc in carriers}
        python_type = next(iter(types)) if len(types) == 1 else object
        originals = {fc.original_name for fc in carriers}
        joined.append(
            FieldContract(
                normalized_name=name,
                original_name=next(iter(originals)) if len(originals) == 1 else name,
                python_type=python_type,
                required=carried_by_all and all(fc.required for fc in carriers),
                source="declared" if any(fc.source == "declared" for fc in carriers) else "inferred",
                nullable=(not carried_by_all) or any(fc.nullable for fc in carriers),
            )
        )

    return SchemaContract(
        mode=min((contract.mode for contract in contracts), key=lambda m: _MODE_ORDER[m]),
        fields=resolve_original_name_collisions(joined),
        locked=any(contract.locked for contract in contracts),
    )
