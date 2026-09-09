"""Client-safe top failure categories for run-level error messages.

The composer-side run row exposes a single ``error`` text field on the runs
table.  Bare structural facts (``rows_succeeded=0``) tell the operator *that*
the run failed but not *why*.  This module reads the audit DB after the run
finishes and aggregates the most common ``transform_errors`` rows so the
run-level error string can name the dominant failure modes without forcing
the operator to drill into the per-run diagnostics panel.

Egress discipline (elspeth-30416e67cc).  Everything this module returns is
inlined into three surfaces that live OUTSIDE the audit boundary:

1. the non-audit sessions DB ``runs.error`` column,
2. ``RunStatusResponse.error`` on ``GET /api/runs/{run_id}``, and
3. the SSE ``failed`` event's ``FailedData.detail``.

``transform_errors.error_details_json`` is written by
``canonical_or_recorded_error_details_json``, which self-declares a **Tier-3**
boundary: the payload "may carry arbitrary row-derived data".  Its human
message fields (``error`` / ``message`` / ``repr`` / ``serialization_error``)
are free text carrying row values and provider/LLM output, validated nowhere.
Its ``reason`` is better off: ``DataFlowErrorRepository.record_transform_error``
enforces membership of the closed ``TransformErrorCategory`` vocabulary at the
Tier-1 write boundary.  The membership check repeated below is deliberate
defence in depth — an egress property must not rest on a sibling module's
invariant, and the raw column can still hold a value that guard never saw
(rows written by an earlier schema, or by any future second writer).

This module therefore never lets free text out.  Only three things leave it:

* the failing node id (``transform_id``), which is authored pipeline
  configuration and never row-derived — node identity is already an
  established disclosure on these same surfaces (``FailedData.node_id``, and
  the node line in ``_operator_failure_diagnostic``);
* an error *category* proved to be a member of the closed
  :data:`~elspeth.contracts.errors.TransformErrorCategory` vocabulary, or one
  of two fixed sentinels when it is not; and
* a row count.

Per-row free text is not dropped from the system — it stays behind the
authenticated, ownership-verified ``GET /api/runs/{run_id}/diagnostics``,
which reads ``node_states.error_json`` and ``operations.error_message``
directly and applies its own redaction at the LLM boundary.

Read discipline: the audit DB is our own store, so a *malformed* record still
fails loudly (``json.loads`` raising, a missing required ``reason`` key
raising ``KeyError``) rather than being defaulted past — see
``ExecutionServiceImpl`` for the narrow transient-failure wrap.  The
membership check below is about untrusted *values*, not about shape: it must
not be relaxed into a shape guard, and the shape guards must not be relaxed
into it.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import get_args

from sqlalchemy import select

from elspeth.contracts.errors import TransformErrorCategory
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.schema import transform_errors_table

# The closed category vocabulary, derived from the Literal itself so the two
# can never drift.  Membership in this set is what makes an emitted category
# provably content-free.
KNOWN_ERROR_CATEGORIES: frozenset[str] = frozenset(get_args(TransformErrorCategory))

# Emitted instead of a category that is not a member of the closed vocabulary
# (an unknown token, or a non-string value).  Fail closed: the offending value
# itself is never rendered, hashed, measured, or repr'd.  Deliberately NOT
# ``unknown_category``, which IS a real member of the vocabulary — the two
# must stay distinguishable on the operator surface.
UNRECOGNIZED_CATEGORY = "unrecognized_category"

# Emitted for the ``{"__non_canonical__": True, "repr": ..., "serialization_error": ...}``
# envelope that ``canonical_or_recorded_error_details_json`` writes when the
# plugin's ``error_details`` could not be canonically serialized.  Both of its
# payload fields are Tier-3 free text, so neither is read here.
NON_CANONICAL_CATEGORY = "non_canonical_error_details"

# Bound on the rendered node id.  Node ids are authored configuration and the
# runtime name rule already caps them well below this, so this never fires in
# practice; it exists because ``runs.error`` is a single text column rendered
# inline and every component of it must be bounded.  This is a bound, not
# redaction.
_MAX_NODE_ID_CHARS = 64


@dataclass(frozen=True, slots=True)
class ClientSafeFailureSummary:
    """One (failing node, error category) pair with the number of rows it hit.

    Deliberately carries **no** free-text field.  The client-path formatter
    accepts only this type, so the per-row ``error``/``message``/``repr`` text
    is not merely unrendered — it is not representable here, and there is no
    formatter overload that could route it to a client surface by accident.
    """

    transform_id: str
    category: str
    count: int


def _client_safe_category(details: dict[str, object]) -> str:
    """Return a provably content-free category token for one audit record.

    The two documented ``error_details_json`` shapes are discriminated the
    same way the audit writer produces them:

    1. the non-canonical envelope, keyed by ``__non_canonical__`` — mapped to
       :data:`NON_CANONICAL_CATEGORY` without reading either of its Tier-3
       fields; and
    2. the canonical :class:`~elspeth.contracts.errors.TransformErrorReason`,
       whose ``reason`` is the error *category*.

    ``reason`` is read with direct subscription: its absence from a canonical
    record is corruption of our own audit data and the ``KeyError`` is the
    prescribed loud failure.  Its *value* is admitted only by membership in
    :data:`KNOWN_ERROR_CATEGORIES`; anything else — an unknown token, or a
    non-``str`` value — becomes :data:`UNRECOGNIZED_CATEGORY`, and the
    offending value itself is never rendered, hashed, or measured.  The
    explicit ``str`` check is load-bearing and not redundant: an unhashable
    value (a list or dict smuggled in as ``reason``) would raise ``TypeError``
    from the membership test alone.

    ``error_type`` is deliberately NOT used, even though it reads like a
    category.  It is documented as a free-form sub-category, is
    ``NotRequired``, and is validated nowhere at runtime — so unlike
    ``reason`` it has no closed vocabulary to prove it content-free against.
    """
    if "__non_canonical__" in details:
        return NON_CANONICAL_CATEGORY
    reason = details["reason"]
    if type(reason) is not str or reason not in KNOWN_ERROR_CATEGORIES:
        return UNRECOGNIZED_CATEGORY
    return reason


def _bounded_node_id(transform_id: str) -> str:
    """Bound the rendered node id so the parent error field stays bounded."""
    if len(transform_id) > _MAX_NODE_ID_CHARS:
        return transform_id[: _MAX_NODE_ID_CHARS - 1] + "…"
    return transform_id


def load_top_failure_categories(
    db: LandscapeDB,
    landscape_run_id: str,
    *,
    limit: int = 3,
    scan_cap: int = 500,
) -> list[ClientSafeFailureSummary]:
    """Return the top ``limit`` distinct (node, error category) pairs for a run.

    Aggregation is by ``(transform_id, category)`` over the whole scanned
    window, and the top-``limit`` slice is taken *after* that aggregation.
    That ordering matters: the previous form keyed the counter on the per-row
    message and sliced before collapsing, so a category spread across many
    distinct message texts lost to a category with one repeated text, and the
    reported counts were per-message rather than per-category.  A count here
    means "rows in this run that failed at this node with this category".

    The scan is bounded by ``scan_cap`` to stay safe on runs with very large
    error counts; accuracy on the long tail is not the goal — naming the
    dominant failure modes for the operator is.
    """
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if scan_cap < limit:
        raise ValueError("scan_cap must be >= limit")

    counter: Counter[tuple[str, str]] = Counter()
    with db.read_only_connection() as conn:
        stmt = (
            select(
                transform_errors_table.c.transform_id,
                transform_errors_table.c.error_details_json,
            )
            .where(transform_errors_table.c.run_id == landscape_run_id)
            .limit(scan_cap)
        )
        for transform_id, error_details_json in conn.execute(stmt):
            # No NULL guard and no ``WHERE error_details_json IS NOT NULL``:
            # the column is nominally nullable (schema.py: ``Column(..., Text)``)
            # but its sole writer — ``DataFlowRepository.
            # _canonical_or_recorded_error_details_json`` (typed ``-> str``) —
            # always persists a non-NULL JSON object on every
            # transform_errors INSERT. A NULL or non-JSON value read here is
            # therefore corruption of our own audit data, and the bare
            # ``json.loads`` raising is the prescribed loud failure — not a
            # case to filter away or default past.
            details = json.loads(error_details_json)
            # Aggregate on the FULL node id — bounding is a rendering concern,
            # and truncating here would collapse two distinct long ids into
            # one bucket.
            counter[(transform_id, _client_safe_category(details))] += 1

    return [
        ClientSafeFailureSummary(transform_id=tid, category=category, count=count) for (tid, category), count in counter.most_common(limit)
    ]


def format_failure_categories(summaries: list[ClientSafeFailureSummary]) -> str:
    """Render summaries as a single multi-line string suitable for inlining.

    Each summary renders on its own bullet with the row count, the failing
    node, and the error category.  The node is shown unconditionally: it is
    half of what the operator needs to act, and hiding it on single-node runs
    (as the previous renderer did) saved a few characters at the cost of the
    fact that makes the category actionable.

    Accepting only :class:`ClientSafeFailureSummary` is the boundary: there is
    no free-text field on the input, so no call site can widen this into a
    raw-text renderer without changing the type.
    """
    if not summaries:
        return ""
    return "\n".join(f"  • {summary.count}x [{_bounded_node_id(summary.transform_id)}] {summary.category}" for summary in summaries)
