"""Value-free report of a transform's non-canonical output (elspeth-5887fb7928 engine review).

Both transform seams hash what a plugin emitted (``stable_hash``) and turn a
canonicalization failure into a Tier-2 ``PluginContractViolation`` that is
ROUTED: its message becomes the reason in ``transform_errors``, the DIVERT
routing event and the failed node state. The canonicalizer's own exception
text is not value-free — ``rfc8785`` renders an out-of-range integer
(``1152921504606859321 exceeds safe integer domain``), and a non-finite float
renders as ``nan``/``inf`` — so it is never embedded.

The report names the exception TYPE, the emitted row's index, and the field,
but only a field the plugin's output schema DECLARES: a plugin that builds its
output keys from row values (a pivot) would otherwise leak a value through the
key. Anything else is reported without a field name.
"""

from __future__ import annotations

from elspeth.contracts import PluginSchema, TransformResult
from elspeth.contracts.errors import PluginContractViolation
from elspeth.core.canonical import stable_hash


def _locate(result: TransformResult, output_schema: type[PluginSchema]) -> str:
    rows = [result.row] if result.row is not None else list(result.rows or ())
    for index, row in enumerate(rows):
        for field_name, value in row.to_dict().items():
            try:
                stable_hash(value)
            except (TypeError, ValueError):
                if field_name in output_schema.model_fields:
                    return f"emitted row {index} field {field_name!r}"
                return f"emitted row {index}, in a field its output schema does not declare"
    return "its emitted output"


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
