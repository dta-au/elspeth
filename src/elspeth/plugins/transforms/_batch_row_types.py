"""Batch-row type rejection, shared by every batch-aware transform.

A buffered row whose VALUE has the wrong type is a row-level fact, but at a
REDUCTIVE seam it cannot be routed out on its own: dropping it would publish a
statistic over a set the operator never specified. John's ruling
(elspeth-d5034647f0) settles the disposition at BATCH granularity — the whole
batch fails, and it must record that it failed and why.

Before this, each plugin raised a bare ``TypeError`` from a value-extraction
helper. ``TypeError`` matches no clause in
``RowProcessor._execute_transform_with_retry`` and nothing in ``engine/``
converts it, so the run ABORTED: ``0 failed``, zero terminal token outcomes, a
raw traceback, exit 4 (elspeth-5887fb7928).

Most of those checks live in helpers that return a VALUE, not a
``TransformResult``, so they cannot simply return the failure. This module
carries the exception they raise instead, which ``process`` catches once and
converts — the shape ``reference_join`` already uses, where ``_coerce_key``
raises ``ReferenceTableError`` and ``process`` turns it into a routable result.

One home rather than a copy per plugin: a second implementation of a rule is
the same defect as a restatement of it, and copying is how the original
``TypeError`` convention reached seventeen sites in the first place.

SCOPE — this covers the wrong-TYPE branch only. A missing value (``None``) and
a non-finite float keep their skip-and-report behaviour; those branches were
deliberately fixed with their polarity documented (``batch_stats.py``, "None is
a missing value, not a type error"), and the ruling explicitly did not reopen
them.
"""

from __future__ import annotations

from elspeth.contracts.errors import TransformErrorReason


class BatchRowTypeError(Exception):
    """A buffered row carries a value whose type the plugin cannot process.

    Raised by value-extraction helpers, caught once in ``process``, and
    converted to a batch-level ``TransformResult.error``. Never allowed to
    escape a plugin: an escaping exception is the abort this class exists to
    replace.
    """

    def __init__(self, *, field: str, row_index: int, expected: str, found: str) -> None:
        super().__init__(f"Field {field!r} must be {expected}, got {found} in row {row_index}.")
        self.field = field
        self.row_index = row_index
        self.expected = expected
        self.found = found

    def as_reason(self) -> TransformErrorReason:
        """Render the audit reason: specific, true, and free of row content.

        Names WHICH row, WHICH field, what was required and what was found —
        the ruling's "record that it failed and why" is explicit that
        "batch failed" or "type error" is not enough.

        The VALUE is deliberately absent. It is Tier-2/3 row content, and the
        same rule ``batch_replicate`` states at its own quarantine site applies:
        record the row INDEX for traceability, never the row body.
        """
        return {
            "reason": "invalid_input",
            "error_type": "wrong_type",
            "field": self.field,
            # `expected` / `actual_type` are the keys TransformErrorReason
            # already declares for type checks ("Expected type or value",
            # "Actual Python type name for type checks"). Reused rather than
            # adding a `row_index` key to a shared contract TypedDict for one
            # caller; the row index travels in `error`, which is where the
            # sibling per-row plugins put their detail too.
            "expected": self.expected,
            "actual_type": self.found,
            "error": f"must be {self.expected}, got {self.found} in row {self.row_index}",
        }
