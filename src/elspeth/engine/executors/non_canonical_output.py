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

from pydantic import ValidationError

from elspeth.contracts import PluginSchema, TransformResult
from elspeth.contracts.errors import PluginContractViolation
from elspeth.contracts.safe_validation_errors import NON_CANONICAL_NUMBER_ERROR_TYPE, safe_validation_error_text
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.core.canonical import stable_hash

_NON_CANONICAL_OUTPUT_GUIDANCE = (
    "Ensure output contains only JSON-serializable types within the JSON safe integer range. Use None instead of NaN for missing values."
)


def _field_phrase(row: PipelineRow, declared_fields: Collection[str]) -> str | None:
    """Describe where in ``row`` the first non-canonical field is, or None when every field canonicalizes.

    The phrase follows the row's own locator (``emitted row 2`` / ``source row
    7``): `` field 'x'`` for a declared field, else a phrase naming no field.
    """
    for field_name, value in row.to_dict().items():
        try:
            stable_hash(value)
        except (TypeError, ValueError):
            if field_name in declared_fields:
                return f" field {field_name!r}"
            return ", in a field its output schema does not declare"
    return None


def _locate_in_rows(rows: Sequence[PipelineRow], declared_fields: Collection[str]) -> str:
    for index, row in enumerate(rows):
        phrase = _field_phrase(row, declared_fields)
        if phrase is not None:
            return f"emitted row {index}{phrase}"
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
        f"{producer} emitted non-canonical data at {_locate(result, output_schema)} ({type(exc).__name__}). {_NON_CANONICAL_OUTPUT_GUIDANCE}"
    )


def output_validation_violation(
    *,
    producer: str,
    emitted_row_index: int,
    output_schema: type[PluginSchema],
    exc: ValidationError,
) -> PluginContractViolation:
    """Build the violation for an emitted row its output schema refused, naming no emitted value.

    The rendered errors are value-free (``safe_validation_error_text``). The
    cause sentence follows the error codes: a refusal made only of the
    schema's canonical-number rule (``non_canonical_number``: NaN/Infinity or
    an integer beyond ±(2**53-1)) is data the transform computed, not a
    schema mismatch, so it carries the canonical-JSON guidance; any other
    refusal is the plugin's output disagreeing with its own schema.

    Args:
        producer: How the message names the plugin, e.g. ``"Transform 'x'"``.
        emitted_row_index: The refused row's index among the emitted rows.
        output_schema: The schema whose ``model_validate`` raised ``exc``.
        exc: The validation error; only its locations and type codes are read.
    """
    error_types = {detail["type"] for detail in exc.errors(include_input=False, include_context=False, include_url=False)}
    if error_types == {NON_CANONICAL_NUMBER_ERROR_TYPE}:
        cause = f"It emitted a number canonical JSON cannot represent. {_NON_CANONICAL_OUTPUT_GUIDANCE}"
    else:
        cause = "This indicates a transform schema bug."
    return PluginContractViolation(
        f"{producer} output validation failed for emitted row {emitted_row_index}: "
        f"{safe_validation_error_text(exc, output_schema)}. {cause}"
    )


def non_canonical_source_row_violation(
    *,
    producer: str,
    declared_fields: Collection[str],
    row: PipelineRow,
    source_row_index: int,
    exc: ValueError,
) -> PluginContractViolation:
    """Build the violation for a VALID source row the ingest hash refused, naming no row value.

    A source that validates its rows through its schema (``schema_factory``'s
    source-boundary check) quarantines a number outside canonical JSON itself,
    so reaching this seam means the source skipped that validation or emitted a
    type canonical JSON cannot represent: its contract breach, and the run ends.

    Args:
        producer: How the message names the source, e.g. ``"Source 'json'"``.
        declared_fields: The field names the source's output schema declares;
            only these may appear in the message (an observed source's keys
            come from the data).
        row: The valid row the ingest transaction failed to canonicalize.
        source_row_index: The row's source-authored position, which locates it.
        exc: The canonicalization error; only its type is reported.
    """
    phrase = _field_phrase(row, declared_fields) or ""
    return PluginContractViolation(
        f"{producer} emitted a valid row with non-canonical data at source row {source_row_index}{phrase} "
        f"({type(exc).__name__}). A source must validate each row at its boundary, quarantining a number outside "
        "canonical JSON (NaN, Infinity, an integer beyond the JSON safe integer range), and emit only JSON types."
    )
