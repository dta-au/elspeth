"""ADR-007 propagation rule: field-set aggregation for pass-through contracts.

This module is the single source of truth for the propagation semantics that
both the runtime DAG walker (``core/dag/graph.py``) and the composer preview
walker (``web/composer/state.py``) must agree on.

The function is intentionally narrow: it implements only the aggregation rule,
not the traversal. Callers compute predecessor guarantees in whatever way
their graph topology demands (recursive walk in the runtime DAG,
producer-graph walk in the composer), then pass the results here.

ADR-009 §Clause 1 makes this the canonical aggregation primitive. The
duplicated traversal logic in the two walkers is intentional (L1 vs L3
separation — see composer/state.py docstring) but the aggregation rule must
not be duplicated, since any drift between the two encodings produces
composer-preview / runtime-validation disagreement that silently corrupts
pipeline safety guarantees.
"""

from __future__ import annotations

from collections.abc import Sequence


def compose_propagation(
    self_fields: frozenset[str],
    predecessor_guarantees: Sequence[frozenset[str] | None],
) -> frozenset[str]:
    """Apply ADR-007's propagation rule.

    The rule: intersect participating predecessors, union with self.

    ``predecessor_guarantees`` entries are either:

    - ``frozenset[str]`` — a participating predecessor's effective
      guarantees. An empty frozenset means "explicit zero guarantees" and
      collapses the intersection to empty.
    - ``None`` — an abstaining predecessor. Skipped in the intersection.

    Edge cases:

    - Empty sequence, or all entries ``None``: returns ``self_fields``
      unchanged (no participants to intersect).
    - Single participant: intersection is the participant itself; result is
      ``self_fields | participant``.
    - Multiple participants: result is ``self_fields | intersect(participants)``.

    The return type is deliberately a bare ``frozenset`` rather than a
    ``frozenset | None`` that could propagate abstention: abstention flows IN
    but not OUT, and the result of an all-abstaining call is
    ``self_fields`` — the node's OWN additions. THAT SET IS A LOWER BOUND, NOT
    A COMPLETE ENUMERATION. Nothing here can say which of a predecessor's
    unenumerated columns are still riding the row.

    Callers must therefore never read this result as "the fields on rows
    leaving this node are exactly these". A validator proving a field ABSENT
    (set DIFFERENCE) needs an upper bound, which only an extras-forbidding
    schema supplies; that fact travels separately as
    ``EffectiveGuaranteeVote.closed`` (``core/dag/guarantees.py``), derived
    from ``SchemaConfig.allows_extra_fields``. Reading participation as
    completeness is the elspeth-9c5ff8fa7d family of false rejections —
    build-time rejections of pipelines that run every row green.

    Adding abstention to this signature would not close that gap: the same
    false rejection occurs with no pass-through node in the graph at all, from
    an ``observed`` source that names ``guaranteed_fields``. The defect is in
    what the result MEANS, not in what this function can express.

    Args:
        self_fields: Fields this node declares directly.
        predecessor_guarantees: Effective guarantees of each predecessor, or
            ``None`` for predecessors that abstain from the intersection.

    Returns:
        The effective guaranteed field set at this node — a LOWER bound. See
        the note above before using it to prove a field absent.
    """
    participating = [g for g in predecessor_guarantees if g is not None]
    if not participating:
        return self_fields
    return self_fields | frozenset.intersection(*participating)
