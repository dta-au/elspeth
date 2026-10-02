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

Both raise a Tier-2 ``PluginContractViolation``, and both executors ROUTE it:
the whole batch fails, following the aggregation's ``on_error`` or failing the
collector's group, as a returned error would (``AggregationExecutor.
_run_flush_transform``, ``CollectorExecutor._execute_flush``). This reverses the
decision recorded here before 2026-09-23, which held that the checks were a
function of the CONFIG alone and so had to abort. Measurement refuted that: a
typed aggregation schema over an observed upstream is ordinary, and one
wrongly-typed row of three failed the check while the other two passed, so it
is a fact about that row and the run must not end on it. Operator ruling
2026-09-23 (elspeth-5887fb7928 B2) chose to route; the per-row seam already
routes the same violation (``RowProcessor._convert_contract_violation_to_error_result``).
When every row fails because the configuration is wrong, every batch is routed
and the run reports its failures instead of a traceback.

The input check also enforces PRESENCE of the fields the transform declares
required (``schema_required_input_fields``: ``schema.required_fields`` plus the
columns each batch transform folds in from its own options). An observed input
model has no fields, so pydantic never saw that declaration, and a buffered row
omitting ``value_field`` reached the plugin as a raw ``KeyError`` that ended the
run (elspeth-5887fb7928 R1). A miss is classified by the SAME rule the per-row
transform preflight applies (ADR-013 Amendment 2026-09-27,
``engine.executors.declared_input_miss``), against the node's entry in the
build's declared-input proof: a field the build never proved present and the
row does not carry is a fact about that row, like a wrong type, and is routed
the same way (``DeclaredInputFieldAbsentViolation``, B2); a field the build
PROVED present, or one the row's payload carries while its contract lost it, is
our bug and aborts (``BatchDeclaredInputFieldsViolation``, Tier 1). One fact,
one disposition, whichever seam sees it.

The message names the row index, field and error type, never the row VALUE
(``contracts.safe_validation_errors``): it becomes the routed reason. An absent
field is named from the transform's CONFIG, never from the row's own keys; the
buffered row's INDEX locates the member (as the spelling residual's does), which
is a position in the batch, not row content.

Before presence, the input check applies the field-name spelling rule's runtime
residual (operator ruling 2026-09-25): a declared read spelled by the header of
a field the buffered row carries fails the batch with
``HeaderSpelledDeclarationViolation`` (a ``PluginContractViolation``, routed the
same way), naming only the config literal and its canonical form. The build
refuses the same declaration wherever a participating, closed upstream proves
it; this settles an abstaining or open one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from elspeth.contracts import BatchTransformProtocol, PipelineRow, TransformResult
from elspeth.contracts.declaration_contracts import derive_effective_input_fields
from elspeth.contracts.errors import (
    BatchDeclaredInputFieldsViolation,
    DeclaredInputFieldAbsentViolation,
    HeaderSpelledDeclarationViolation,
    PluginContractViolation,
)
from elspeth.contracts.field_spelling import DeclaredSpellings
from elspeth.contracts.safe_validation_errors import safe_validation_error_text
from elspeth.contracts.types import NodeID
from elspeth.engine.executors.declared_input_miss import classify_declared_input_miss, declared_input_proof_entry
from elspeth.engine.executors.declared_output_types import verify_created_output_types
from elspeth.engine.executors.non_canonical_output import output_validation_violation


def batch_declared_input_proof(
    proof: Mapping[NodeID, frozenset[str]],
    *,
    node_id: NodeID,
    transform: BatchTransformProtocol,
    node_kind: str,
) -> frozenset[str]:
    """The batch node's entry in the build's declared-input proof, for ``validate_batch_inputs``.

    A node whose plugin requires no field has nothing to classify. One that
    does must have an entry (``declared_input_proof_entry`` refuses a missing
    one rather than reading it as "proves nothing").
    """
    required = transform.schema_required_input_fields()
    if not required:
        return frozenset()
    return declared_input_proof_entry(proof, node_id=node_id, declared=required, component=f"{node_kind} transform '{transform.name}'")


def validate_batch_inputs(
    transform: BatchTransformProtocol,
    rows: Sequence[PipelineRow],
    *,
    node_kind: str,
    proven: frozenset[str],
) -> None:
    """Validate reconstructed batch input rows before plugin execution.

    Args:
        transform: The batch transform whose declared input contract governs.
        rows: The buffered rows about to be handed to ``process``.
        node_kind: Operator-facing name for the node kind ("Aggregation",
            "Collector") — message text only, never control flow.
        proven: The node's entry in the build's declared-input proof — the
            required fields every arriving row provably carries.

    Presence is checked before the model, for the reason the per-row preflight
    runs its declaration check first: an absent field is reported as absent,
    not diluted into a schema failure.

    Raises:
        DeclaredInputFieldAbsentViolation: A buffered row lacks required fields
            the build never proved present (routed, like any
            ``PluginContractViolation`` here).
        BatchDeclaredInputFieldsViolation: A buffered row lacks a PROVEN field,
            or its payload carries a required field its contract lost (Tier 1).
        PluginContractViolation: A buffered row fails the declared input schema.
    """
    required = transform.schema_required_input_fields()
    declared_spellings = DeclaredSpellings.of(reads=transform.declared_read_fields, creates=())
    for idx, row in enumerate(rows):
        # The field-name spelling rule's runtime residual, checked before the
        # presence check below: ``in`` on a PipelineRow resolves a header
        # spelling to the field it names, so a header-spelled ``group_by`` or
        # ``value_field`` passes presence while the plugin keys its output by
        # the literal. Created names are not checked at a batch seam: its
        # output does not carry the buffered rows forward (the build's
        # TRANSFORM-only scope, elspeth-cfcd333f83).
        spellings = declared_spellings.in_row(row_keys=frozenset(row.to_dict()), forwarded_keys=(), contract=row.contract)
        if spellings:
            raise HeaderSpelledDeclarationViolation(
                component=f"{node_kind} transform '{transform.name}' (buffered row {idx})",
                spellings=spellings,
            )
        # The row's effective fields (contract names ∩ payload keys) — the set
        # the per-row seam checks. After the spelling check above, every
        # required name is canonical, so a field present here is present under
        # the name the plugin reads and cannot raise KeyError inside it.
        missing = required - derive_effective_input_fields(row)
        if missing:
            miss_kind = classify_declared_input_miss(missing=missing, proven=proven, payload_keys=frozenset(row.keys()))
            if miss_kind == "absent":
                raise DeclaredInputFieldAbsentViolation(
                    component=f"{node_kind} transform '{transform.name}' (buffered row {idx})",
                    fields=tuple(sorted(missing)),
                )
            raise BatchDeclaredInputFieldsViolation(
                f"{node_kind} transform '{transform.name}': buffered row {idx} lacks required input field(s) {sorted(missing)} "
                + (
                    "that the build proved present on every arriving row. "
                    if miss_kind == "proven"
                    else "that its payload carries but its contract does not. "
                )
                + "This is an engine defect (contract propagation, merge or restore), not a fact about the row.",
                failure_kind="proven_field_absent" if miss_kind == "proven" else "contract_payload_divergence",
                plugin=transform.name,
                node_kind=node_kind,
                missing=frozenset(missing),
            )
        try:
            transform.input_schema.model_validate(row.to_dict(), strict=True)
        except ValidationError as exc:
            raise PluginContractViolation(
                f"{node_kind} transform '{transform.name}' input validation failed for buffered row {idx}: "
                f"{safe_validation_error_text(exc, transform.input_schema)}. "
                "This indicates an upstream transform/source schema bug."
            ) from exc


def validate_success_outputs(
    transform: BatchTransformProtocol,
    result: TransformResult,
    *,
    node_kind: str,
) -> None:
    """Validate successful batch output rows before audit completion.

    Two checks, both Tier 2: the declared output schema (pydantic strict,
    which types nothing for an observed output model), then the ADR-050
    VALUE check — every created field (``declared_output_fields`` and
    ``created_output_fields()``) whose stamped type is concrete, against the
    emitted value, through the same ``declared_output_types`` module the
    per-row seam uses. Input fields a passthrough batch output carries are
    not checked here: the buffer preflight validated their INPUT values, and
    a batch output row has no single input row to detect a rewrite against,
    so a passthrough batch plugin that rewrote a carried input field would
    not be caught at the flush (no shipped batch plugin does; both
    passthrough shapes only add fields). A batch transform's emitted
    contract carries the declaration stamp
    (``BaseTransform._batch_output_contract`` and the passthrough shapes'
    ``_apply_declared_output_field_contracts`` call), so a plugin computing
    the wrong type for its own statistic is caught here, value-free, with
    ``authorship: computed`` and the declarer of the broken type
    (``declared_by: plugin`` for a type the plugin fixes, ``operator`` for
    the pipeline's ``schema.fields``), and fails the whole batch like any
    other violation of this postflight.

    Args:
        transform: The batch transform whose declared output contract governs.
        result: The successful result whose emitted rows are checked.
        node_kind: Operator-facing name for the node kind — message text only.

    Raises:
        PluginContractViolation: If any emitted row fails the declared output
            schema, or (``DeclaredOutputTypeViolation``) a created field's
            value breaks the type the transform declared for it.
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
            raise output_validation_violation(
                producer=f"{node_kind} transform '{transform.name}'",
                emitted_row_index=idx,
                output_schema=transform.output_schema,
                exc=exc,
            ) from exc

    verify_created_output_types(transform=transform, emitted_rows=emitted_rows)
