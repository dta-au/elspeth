"""Contract preflight/postflight shared by every executor that runs a batch transform.

``AggregationExecutor`` and ``CollectorExecutor`` run the SAME plugin contract —
a batch transform whose input/output schemas are declared under an ``options``
key. ``contracts/schema.py``'s ``NESTED_CONTRACT_OPTIONS_NODE_TYPES``
(``{AGGREGATION, COLLECTOR}``) is the repo's existing statement of exactly that
set, and it is what the closing tests derive from.

These two checks lived as private statics on ``AggregationExecutor`` and had no
counterpart on ``CollectorExecutor``, so a row that violated a collector's own
``mode: fixed`` contract reached the plugin and the run banked a clean
COMPLETED over it (elspeth-c2fa61cf57). Under ADR-010's audit-complete posture
silence reads as "checked and passed", so the absence was not a missing
diagnostic — it wrote a wrong audit fact, and the row neither returned in good
order nor quarantined.

They live here, once, rather than being copied onto the second executor. A
second copy of a rule is the same defect as a restatement of it: the two drift,
and nothing makes them drift together. ``node_kind`` is the ONLY thing the two
callers vary, and it varies solely to name the node in the operator-facing
message.

Both raise rather than returning a routable error, deliberately. What they
check is a function of the CONFIG, not of the row: the node's declared contract
either admits the arriving shape or it does not, identically for every row in
the group. Routing it per-row would quarantine an entire dataset and report
PARTIAL, telling the operator their data was bad when the pipeline was
misconfigured — the disposition ADR-008 §Alternative 3 rejects, and the same
reasoning ``TransformExecutor._run_preflight`` records for its own lifecycle
guard. This is the opposite polarity from elspeth-5887fb7928, where the checks
WERE facts about one row and had to become routable returns.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from elspeth.contracts import BatchTransformProtocol, PipelineRow, TransformResult
from elspeth.contracts.errors import PluginContractViolation


def validate_batch_inputs(
    transform: BatchTransformProtocol,
    rows: Sequence[PipelineRow],
    *,
    node_kind: str,
) -> None:
    """Validate reconstructed batch input rows before plugin execution.

    Args:
        transform: The batch transform whose declared input contract governs.
        rows: The buffered rows about to be handed to ``process``.
        node_kind: Operator-facing name for the node kind ("Aggregation",
            "Collector") — message text only, never control flow.

    Raises:
        PluginContractViolation: If any buffered row fails the declared input
            schema.
    """
    for idx, row in enumerate(rows):
        try:
            transform.input_schema.model_validate(row.to_dict(), strict=True)
        except ValidationError as exc:
            raise PluginContractViolation(
                f"{node_kind} transform '{transform.name}' input validation failed for buffered row {idx}: {exc}. "
                "This indicates an upstream transform/source schema bug."
            ) from exc


def validate_success_outputs(
    transform: BatchTransformProtocol,
    result: TransformResult,
    *,
    node_kind: str,
) -> None:
    """Validate successful batch output rows before audit completion.

    Args:
        transform: The batch transform whose declared output contract governs.
        result: The successful result whose emitted rows are checked.
        node_kind: Operator-facing name for the node kind — message text only.

    Raises:
        PluginContractViolation: If any emitted row fails the declared output
            schema.
    """
    if result.row is not None:
        emitted_rows: tuple[PipelineRow, ...] = (result.row,)
    elif result.rows is not None:
        emitted_rows = tuple(result.rows)
    else:
        emitted_rows = ()

    for idx, row in enumerate(emitted_rows):
        try:
            transform.output_schema.model_validate(row.to_dict(), strict=True)
        except ValidationError as exc:
            raise PluginContractViolation(
                f"{node_kind} transform '{transform.name}' output validation failed for emitted row {idx}: {exc}. "
                "This indicates a transform schema bug."
            ) from exc
