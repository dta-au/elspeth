"""The ADR-050 VALUE check: a declared concrete type against the value the transform produced (Tier 2).

A transform declares the contract of every field it creates before the first
row (``BaseTransform._stamped_output_field_contracts``); this module is the one
place the engine checks those declarations against what was actually emitted.
Two seams call it, and they differ ONLY in which fields count as produced:

* ``verify_produced_output_types`` — the per-row seam (``TransformExecutor``).
  A field is produced when its normalized name is not a key of the input
  row's data, or when the transform rewrote an input field's value (a
  different value, or an equal value of another type: ``1 == True == 1.0``).
  Both rows are read in the ONE vocabulary the input check validated and the
  completeness contract reads: normalized keys. An emitted field's
  ``normalized_name`` is never looked up as an ``original_name`` of the input
  row, so a field_mapper target spelled like a source header (``Name``, the
  header of the input field ``name``) is a created field exactly as ``given``
  would be — whether a declaration is enforced never depends on how the
  target is spelled. An input value passed through
  unchanged is not re-adjudicated: the strict ``input_schema`` check admitted
  it under pydantic's rules, which accept an ``int`` or a ``Decimal`` for a
  ``float`` field where ``SchemaContract.validate`` compares exact types, and
  a resumed row legitimately carries a type-faithful ``Decimal`` under its
  ``float`` declaration. For the same reason a ``carried_output_fields()``
  name is never produced: its value is an input field's value copied under a
  new name and its declaration is that input field's (a field_mapper rename
  whose target inherits the source's contract), already admitted by the input
  check, and the completeness contract exempts the same names. A rename the
  operator declared by its TARGET name alone is not carried — no input check
  held the value to that declaration — so it is checked like a created field.
* ``verify_created_output_types`` — the batch-flush seam
  (``batch_contract_validation.validate_success_outputs``, shared by the
  aggregation and collector executors). A batch output row has no single
  input row to compare against, so the fields produced are exactly the ones
  the transform CREATES: ``declared_output_fields`` and
  ``created_output_fields()``. The limit that follows: a passthrough batch
  output (batch_outlier_annotator, batch_replicate) carries its buffered
  rows' input fields, and the buffer preflight validated those INPUT values;
  if such a plugin REWROTE a carried input field, the rewrite would not be
  value-checked at the flush. No shipped batch plugin rewrites a carried
  field (both only add fields).

Both raise ``DeclaredOutputTypeViolation``, a ``PluginContractViolation`` the
callers route: the per-row processor sends the token through ``on_error``; the
batch executors fail the whole batch (aggregation ``on_error``) or the group
(collector verdict), exactly as they route every other Tier-2 violation. The
reason names the field, both type names, the emitted index and two bits —
never the value:

* ``declared_by`` — who declared the violated type: ``operator`` (the
  pipeline author's ``schema.fields``, or its projection onto a rename
  target), ``plugin`` (a type the plugin's own code fixes: its
  ``created_output_fields()``, a builder-typed output field, or a per-emission
  ``dynamic_created_fields`` declaration), or ``upstream`` (the field arrived
  on the input row under a declaration made before this transform, which is
  not in this transform's stamp table). Read from
  ``output_field_declared_by()``, the same table the stamp is built from.
* ``authorship`` — whether the transform created the field (``computed``:
  not a normalized key of its input row; always so at the batch seam) or rewrote an input
  field's value (``carried``). This is about the FIELD, not the declaration,
  and it is unrelated to ``carried_output_fields()``, whose names are never
  checked at all.

A multi-row emission fails its parent once. An ``any`` declaration
(``python_type is object``) checks nothing by construction.

A transform with no ``_output_schema_config`` declares nothing, so there is
nothing to enforce.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from elspeth.contracts.errors import DeclaredOutputTypeViolation, TypeMismatchViolation
from elspeth.contracts.plugin_protocols import BatchTransformProtocol, TransformProtocol
from elspeth.contracts.plugin_roles import OutputDeclaringPlugin, require_output_declaring_plugin
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.contracts.transform_contract import validate_output_against_contract


def value_rewritten(original: object, produced: object) -> bool:
    """Whether the transform REWROTE an input field's value rather than passing it through.

    A different value, or an equal value of another type — ``1 == True ==
    1.0`` in Python, so an input ``bool`` rewritten to ``1`` is a rewrite the
    type check must see. The per-row seam passes both values from the rows'
    ``to_dict()`` views, thawed the same way, so the comparison is like for
    like.
    """
    return produced != original or type(produced) is not type(original)


def verify_produced_output_types(
    *,
    transform: TransformProtocol,
    input_row: PipelineRow,
    emitted_rows: Sequence[PipelineRow],
) -> None:
    """Per-row seam: check every declared concrete-typed field the transform produced from ``input_row``."""
    _verify_declared_types(transform, emitted_rows, input_row=input_row)


def verify_created_output_types(
    *,
    transform: BatchTransformProtocol,
    emitted_rows: Sequence[PipelineRow],
) -> None:
    """Batch-flush seam: check every declared concrete-typed field the transform creates."""
    _verify_declared_types(transform, emitted_rows, input_row=None)


def _verify_declared_types(
    transform: TransformProtocol | BatchTransformProtocol,
    emitted_rows: Sequence[PipelineRow],
    *,
    input_row: PipelineRow | None,
) -> None:
    """The one check; ``input_row`` is the per-row seam's input, ``None`` at the batch flush."""
    if transform._output_schema_config is None:
        return
    declaring = require_output_declaring_plugin(transform)
    created = declaring.declared_output_fields | frozenset(definition.name for definition in declaring.created_output_fields())
    carried = declaring.carried_output_fields()
    # Both rows are read through their to_dict() views, keyed by NORMALIZED
    # name only. ``PipelineRow.__contains__`` / ``__getitem__`` are
    # deliberately not used for the input row: they also resolve a key as an
    # input field's ``original_name``, so an emitted ``Name`` would read as
    # the input field ``name`` (header ``Name``) and a copied value would pass
    # as unchanged, although the input check, keyed by normalized name, never
    # held it to the ``Name`` declaration (review-S1a-r3 F1).
    input_values = None if input_row is None else input_row.to_dict()

    for emitted_index, emitted in enumerate(emitted_rows):
        emitted_values = emitted.to_dict()
        produced_fields: list[FieldContract] = []
        for fc in emitted.contract.fields:
            key = fc.normalized_name
            if fc.source != "declared" or fc.python_type is object or key not in emitted_values:
                continue
            if input_values is None:
                produced = key in created
            else:
                produced = key not in carried and (key not in input_values or value_rewritten(input_values[key], emitted_values[key]))
            if produced:
                produced_fields.append(fc)
        if not produced_fields:
            continue
        produced_contract = SchemaContract(mode="FLEXIBLE", fields=tuple(produced_fields), locked=True)
        for violation in validate_output_against_contract(emitted_values, produced_contract):
            # Exact type: MissingFieldViolation on a required declared field is
            # ADR-011/ADR-014's finding, and no TypeMismatchViolation subclass exists.
            if type(violation) is TypeMismatchViolation:
                name = violation.normalized_name
                arrived_on_input = input_values is not None and name in input_values
                raise DeclaredOutputTypeViolation(
                    transform=transform.name,
                    field=name,
                    expected_type=violation.expected_type.__name__,
                    actual_type=violation.actual_type.__name__,
                    emitted_index=emitted_index,
                    authorship="carried" if arrived_on_input else "computed",
                    declared_by=_declarer(declaring, name, arrived_on_input=arrived_on_input),
                )


def _declarer(declaring: OutputDeclaringPlugin, name: str, *, arrived_on_input: bool) -> Literal["operator", "plugin", "upstream"]:
    """Who declared the violated type of ``name``.

    A name in this transform's stamp table carries the table's declarer. A
    name outside it was not declared by this transform: an input field keeps
    the declaration it arrived with (``upstream``); a created field outside
    the static table can only have been stamped by the plugin itself
    (blob_csv_expand's per-emission ``dynamic_created_fields``), because the
    completeness contract ends the run on any created field without a
    declared contract.
    """
    declared_by = declaring.output_field_declared_by()
    if name in declared_by:
        return declared_by[name]
    return "upstream" if arrived_on_input else "plugin"
