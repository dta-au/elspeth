"""Runtime verification that every created field carries a declared contract (ADR-050).

This contract registers for ONE dispatch site:

    * ``post_emission_check`` — single-token path from ``TransformExecutor``.

The reverse of ADR-011. ADR-011 checks that every DECLARED field is emitted;
this checks that every field the transform CREATED — a key absent from its
input row and not carried from an input field (``carried_output_fields``) —
carries a ``source="declared"`` contract, i.e. went through the declaration
stamp (``BaseTransform._apply_declared_output_field_contracts``). A created
field that bypassed the stamp is typed from this row's value, so the node's
recorded output contract would become a per-row measurement and two rows
could conflict at the node-contract merge. That is owned-code drift (a plugin
not declaring what it creates), not a row fault: Tier 1, the run ends after
the token's terminal is recorded.

The batch-flush site is deliberately NOT claimed. Aggregation and collector
outputs carry the stamp (``BaseTransform._batch_output_contract`` and the
passthrough plugins' stamp call), but the batch-flush dispatch hands a
contract only the INTERSECTION of the buffered rows' input fields (ADR-009),
so a passthrough-shaped batch output carrying an input field that only some
buffered rows had would read as an undeclared created key; and the collector
flush dispatches no declaration contracts at all. Batch-aware completeness is
enforced by the registry gate instead
(``tests/invariants/test_output_declaration_completeness.py``, static and
probe emissions, every batch-aware transform included).

The companion VALUE check — a declared concrete type against the emitted
value — is Tier 2 (routed), so it lives in ``engine/executors/declared_output_types``
(called by ``TransformExecutor`` and by the shared batch postflight) rather
than in this dispatcher, whose catch scope is the Tier-1 declaration family.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, ClassVar

from elspeth.contracts.declaration_contracts import (
    DeclarationContract,
    DispatchSite,
    ExampleBundle,
    PostEmissionInputs,
    PostEmissionOutputs,
    implements_dispatch_site,
    register_declaration_contract,
)
from elspeth.contracts.errors import (
    OrchestrationInvariantError,
    UndeclaredOutputFieldRowViolationPayload,
    UndeclaredOutputFieldsPayload,
    UndeclaredOutputFieldsViolation,
)
from elspeth.contracts.plugin_roles import require_output_declaring_plugin
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import (
    FieldContract,
    PipelineRow,
    SchemaContract,
)

_MAX_VIOLATION_SAMPLES = 10


def _build_row(fields: tuple[str, ...], *, declared: frozenset[str] = frozenset()) -> PipelineRow:
    return PipelineRow(
        dict.fromkeys(fields, "v"),
        SchemaContract(
            mode="OBSERVED",
            fields=tuple(
                FieldContract(
                    normalized_name=name,
                    original_name=name,
                    python_type=str,
                    required=True,
                    source="declared" if name in declared else "inferred",
                    nullable=False,
                )
                for name in fields
            ),
            locked=True,
        ),
    )


def verify_output_declaration_completeness(
    *,
    plugin: Any,
    emitted_rows: Sequence[PipelineRow],
    effective_input_fields: frozenset[str],
    node_id: str,
    run_id: str,
    row_id: str,
    token_id: str,
) -> None:
    """Verify every created field of every emitted row carries a declared contract."""
    if not emitted_rows:
        return
    declaring = require_output_declaring_plugin(plugin)
    carried = declaring.carried_output_fields()

    violations: list[UndeclaredOutputFieldRowViolationPayload] = []
    stamped: set[str] = set()
    for emitted_index, emitted in enumerate(emitted_rows):
        contract_fields = {fc.normalized_name: fc for fc in emitted.contract.fields}
        stamped.update(name for name, fc in contract_fields.items() if fc.source == "declared")
        undeclared = sorted(
            name
            for name in emitted.to_dict()
            if name not in effective_input_fields
            and name not in carried
            and (name not in contract_fields or contract_fields[name].source != "declared")
        )
        if undeclared:
            violations.append({"emitted_index": emitted_index, "undeclared": undeclared})
    if not violations:
        return

    sampled = violations[:_MAX_VIOLATION_SAMPLES]
    payload: UndeclaredOutputFieldsPayload = {
        "stamped": sorted(stamped),
        "carried": sorted(carried),
        "violation_count": len(violations),
        "violations_truncated": len(violations) > len(sampled),
        "violations": sampled,
    }
    raise UndeclaredOutputFieldsViolation(
        plugin=declaring.name,
        node_id=node_id,
        run_id=run_id,
        row_id=row_id,
        token_id=token_id,
        payload=payload,
        message=(
            f"Transform {declaring.name!r} (node {node_id!r}) emitted created fields it never declared "
            f"for row {row_id!r}: {sampled[0]['undeclared']!r} on emitted row {sampled[0]['emitted_index']} "
            f"({len(violations)} emitted row(s) affected). Every field a transform creates is declared "
            "before the first row (ADR-050): declare it in created_output_fields or declared_output_fields."
        ),
    )


class OutputDeclarationCompletenessContract(DeclarationContract):
    """ADR-050 adopter: every created field carries a declared contract."""

    name: ClassVar[str] = "output_declaration_completeness"
    payload_schema: ClassVar[type] = UndeclaredOutputFieldsPayload
    violation_class: ClassVar[type[UndeclaredOutputFieldsViolation]] = UndeclaredOutputFieldsViolation

    def applies_to(self, plugin: Any) -> bool:
        return plugin._output_schema_config is not None

    @implements_dispatch_site("post_emission_check")
    def post_emission_check(
        self,
        inputs: PostEmissionInputs,
        outputs: PostEmissionOutputs,
    ) -> None:
        transform_node_id = inputs.plugin.node_id
        if transform_node_id is None:
            raise OrchestrationInvariantError(f"Transform {inputs.plugin.name!r} has no node_id set at output-declaration check time.")
        verify_output_declaration_completeness(
            plugin=inputs.plugin,
            emitted_rows=outputs.emitted_rows,
            effective_input_fields=inputs.effective_input_fields,
            node_id=transform_node_id,
            run_id=inputs.run_id,
            row_id=inputs.row_id,
            token_id=inputs.token_id,
        )

    @classmethod
    def negative_example(cls) -> ExampleBundle:
        class _MinimalTransform:
            name = "NegativeOutputDeclarationExample"
            node_id = "output-declaration-neg-1"
            declared_output_fields: frozenset[str] = frozenset({"created"})
            passes_through_input = False
            _output_schema_config = SchemaConfig.from_dict({"mode": "observed"})

            def created_output_fields(self) -> tuple[FieldDefinition, ...]:
                return ()

            def carried_output_fields(self) -> frozenset[str]:
                return frozenset()

        inputs = PostEmissionInputs(
            plugin=_MinimalTransform(),
            node_id="output-declaration-neg-1",
            run_id="output-declaration-neg-run",
            row_id="output-declaration-neg-row",
            token_id="output-declaration-neg-token",
            input_row=_build_row(("source",)),
            static_contract=frozenset({"created"}),
            effective_input_fields=frozenset({"source"}),
        )
        # The created field is emitted with an INFERRED contract: it bypassed the stamp.
        outputs = PostEmissionOutputs(emitted_rows=(_build_row(("source", "created")),))
        return ExampleBundle(site=DispatchSite.POST_EMISSION, args=(inputs, outputs))

    @classmethod
    def positive_example_does_not_apply(cls) -> ExampleBundle:
        class _NonApplyingTransform:
            name = "NonApplyingOutputDeclarationExample"
            node_id = "output-declaration-non-fire-1"
            declared_output_fields: frozenset[str] = frozenset()
            passes_through_input = False
            _output_schema_config = None

            def created_output_fields(self) -> tuple[FieldDefinition, ...]:
                return ()

            def carried_output_fields(self) -> frozenset[str]:
                return frozenset()

        inputs = PostEmissionInputs(
            plugin=_NonApplyingTransform(),
            node_id="output-declaration-non-fire-1",
            run_id="output-declaration-non-fire-run",
            row_id="output-declaration-non-fire-row",
            token_id="output-declaration-non-fire-token",
            input_row=_build_row(("source",)),
            static_contract=frozenset(),
            effective_input_fields=frozenset({"source"}),
        )
        outputs = PostEmissionOutputs(emitted_rows=(_build_row(("source",)),))
        return ExampleBundle(site=DispatchSite.POST_EMISSION, args=(inputs, outputs))


register_declaration_contract(OutputDeclarationCompletenessContract())
