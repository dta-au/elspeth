"""Field name collision detection.

Pure utility used by both engine pre-emission checks and plugin
implementations (batch_replicate, etc.) when transforms enrich rows with
new fields. Silent overwrites are data loss; this helper exists to make
collision detection mandatory rather than opt-in per plugin.

Lives at L0 (contracts) because it operates on field-name sets — schema-
contract primitives — and is consumed by both L2 (engine) and L3 (plugins).
"""

from __future__ import annotations

from collections.abc import Iterable


def detect_field_collisions(
    existing_fields: set[str],
    new_fields: Iterable[str],
) -> list[str] | None:
    """Detect field name collisions between existing row fields and new fields.

    Args:
        existing_fields: Field names already present in the row.
        new_fields: Field names the transform intends to add.

    Returns:
        Sorted list of colliding field names, or None if no collisions.
    """
    collisions = sorted(f for f in new_fields if f in existing_fields)
    return collisions or None


def can_overwrite_input_fields(
    *,
    passes_through_input: bool,
    forwards_input_fields: bool,
) -> bool:
    """Whether a transform's write path can overwrite a field arriving on its input row.

    ``declared_output_fields`` is a GUARANTEE claim ("this field is on every
    successful output row"), not a write-path claim, so a collision gate keyed
    on the declaration alone asks the wrong question (elspeth-6ea3619737 /
    elspeth-0d1da6dc44). An overwrite requires the input row to SURVIVE onto
    the output; a transform that builds its output from a fresh dict (a
    ``select_only`` field_mapper, the reductive batch aggregators) consumes
    and replaces, it cannot clobber. The two ADR-007 presence channels are the
    authority on survival:

    - ``passes_through_input``: every input field survives unconditionally
      (the enricher class — llm, truncate, type_coerce ...).
    - ``forwards_input_fields``: every input field survives EXCEPT a named
      removal set (the open-branch field_mapper, both explode transforms).

    Deliberately keyed on capability, never on executor path: the batch
    aggregators are collision-gate-unreachable only because token_traversal
    routes batch-aware plugins to AggregationExecutor, and arming them
    path-wise breaks 12 plugins whose ``group_by`` field legitimately sits in
    both the required-input and declared-output sets.

    All three collision consumers (TransformExecutor._run_preflight, the
    build-time twin in core/dag/schema_validation.py, and composer Rule D in
    web/composer/state.py) must key on this one predicate in lockstep.
    """
    return passes_through_input or forwards_input_fields


__all__ = ["can_overwrite_input_fields", "detect_field_collisions"]
