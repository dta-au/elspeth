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

SCOPE — two row-data faults, each failing the whole batch:

* the wrong-TYPE branch (``BatchRowTypeError``). A missing value (``None``) and
  a non-finite float keep their skip-and-report behaviour wherever a plugin
  already has that branch; those branches were deliberately fixed with their
  polarity documented (``batch_stats.py``, "None is a missing value, not a type
  error"), and the ruling explicitly did not reopen them. A plugin with NO
  ``None`` branch (``batch_replicate``) reports ``None`` as a wrong type, which
  fails the batch; no skip branch is invented for it.
* a buffered row that already carries a field the plugin emits
  (``BatchRowFieldCollisionError``). Under an observed or sparse upstream only
  the row DATA decides whether the field is present, so this is a row fault,
  not a configuration fact: the operator's ruling on elspeth-d90495084c routes
  it like any other failed batch. The certain case — an explicit schema that
  declares the emitted field — is still refused at construction
  (``_reject_explicit_*_collision``, ``PluginConfigError``).

Both reasons name the batch row INDEX and field NAMES only, never a row value.
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


class BatchRowFieldCollisionError(Exception):
    """A buffered row already carries a field the plugin would write onto it.

    The batch counterpart of the per-row engine collision check
    (``TransformExecutor``), which routes the same fault through ``on_error``
    for a single-row transform. At a batch seam the plugin performs the check
    itself, so it renders the reason here: one renderer for every batch plugin
    that emits fields onto its input rows, never a copy per plugin.
    """

    def __init__(self, *, row_index: int, collisions: list[str]) -> None:
        super().__init__(f"would overwrite existing input fields {collisions} in row {row_index}")
        self.row_index = row_index
        self.collisions = collisions

    def as_reason(self) -> TransformErrorReason:
        """Render the audit reason: the colliding field NAMES and the batch row index.

        The names are the plugin's own declared output fields (configuration),
        so they are safe to record. The value the row already held under that
        name is row content and is never read.
        """
        return {
            "reason": "field_collision",
            # `collisions` is the key TransformErrorReason declares for
            # exactly this ("Field names that would be overwritten").
            "collisions": list(self.collisions),
            "error": f"would overwrite existing input fields {self.collisions} in row {self.row_index}",
        }
