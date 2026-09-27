"""Value-free report of a plugin's non-canonical output (elspeth-5887fb7928 engine review).

Four seams hash what a plugin emitted (``stable_hash``) and turn a
canonicalization failure into a Tier-2 ``PluginContractViolation`` built
here, so its message is audit text at each:

- the per-row transform and the aggregation flush ROUTE it through
  ``on_error``: the message becomes the reason in ``transform_errors``, in
  the DIVERT routing event of a named sink, and in the failed node state;
- the collector flush fails the whole group: the message becomes the flush
  node state's error, and each member's hold fails with the group-level
  ``CollectorGroupFailure`` (no ``transform_errors`` row, no DIVERT);
- source ingest hashes a VALID source row inside the fenced ingest
  transaction; a non-canonical valid row is the source's contract breach (it
  must quarantine such a value at its boundary) and ends the run, so the
  message becomes the source operation's error and is printed with the
  traceback (C3 fix round 1: the ingest seam carried rfc8785's own text).

The canonicalizer's own exception text is not value-free — ``rfc8785``
renders an out-of-range integer (``1152921504606859321 exceeds safe integer
domain``), and a non-finite float renders as ``nan``/``inf`` — so it is never
embedded.

The report names the exception TYPE, the emitted row's index, and the field,
but only a field the plugin's output schema DECLARES: a plugin that builds its
output keys from row values (a pivot) would otherwise leak a value through the
key. Anything else is reported without a field name.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from elspeth.contracts import PluginSchema, TransformResult
from elspeth.contracts.errors import PluginContractViolation
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.canonical import stable_hash


def _locate_in_rows(rows: Sequence[PipelineRow], declared_fields: Collection[str]) -> str:
    for index, row in enumerate(rows):
        for field_name, value in row.to_dict().items():
            try:
                stable_hash(value)
            except (TypeError, ValueError):
                if field_name in declared_fields:
                    return f"emitted row {index} field {field_name!r}"
                return f"emitted row {index}, in a field its output schema does not declare"
    return "its emitted output"


def _locate(result: TransformResult, output_schema: type[PluginSchema]) -> str:
    rows = [result.row] if result.row is not None else list(result.rows or ())
    return _locate_in_rows(rows, output_schema.model_fields)


def non_canonical_output_violation(
    *,
    producer: str,
    output_schema: type[PluginSchema],
    result: TransformResult,
    exc: TypeError | ValueError,
) -> PluginContractViolation:
    """Build the violation for output ``stable_hash`` refused, naming no emitted value.

    Args:
        producer: How the message names the plugin, e.g. ``"Transform 'x'"``.
        output_schema: The plugin's declared output schema; only its field
            names may appear in the message.
        result: The result whose ``row``/``rows`` failed to canonicalize.
        exc: The canonicalization error; only its type is reported.
    """
    return PluginContractViolation(
        f"{producer} emitted non-canonical data at {_locate(result, output_schema)} ({type(exc).__name__}). "
        "Ensure output contains only JSON-serializable types within the JSON safe integer range. "
        "Use None instead of NaN for missing values."
    )


def non_canonical_source_row_violation(
    *,
    producer: str,
    declared_fields: Collection[str],
    row: PipelineRow,
    exc: ValueError,
) -> PluginContractViolation:
    """Build the violation for a VALID source row the ingest hash refused, naming no row value.

    Args:
        producer: How the message names the source, e.g. ``"Source 'json'"``.
        declared_fields: The field names the source's output schema declares;
            only these may appear in the message (an observed source's keys
            come from the data).
        row: The valid row the ingest transaction failed to canonicalize.
        exc: The canonicalization error; only its type is reported.
    """
    return PluginContractViolation(
        f"{producer} emitted a valid row with non-canonical data at {_locate_in_rows([row], declared_fields)} "
        f"({type(exc).__name__}). A source must quarantine a value outside canonical JSON (an integer beyond the "
        "JSON safe integer range, a non-finite float) at its boundary."
    )
