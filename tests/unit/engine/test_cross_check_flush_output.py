"""Tests for ``RowProcessor._cross_check_flush_output`` — ADR-009 §Clause 2.

Targets the batch-aware flush path that previously trusted static
``passes_through_input`` annotations. Every scenario uses real
``_FlushContext`` instances and calls the method on a real ``RowProcessor``
— no mocks of the method under test.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest

from elspeth.contracts import TokenInfo, TransformProtocol, TransformResult
from elspeth.contracts.declaration_contracts import AggregateDeclarationContractViolation, derive_effective_input_fields
from elspeth.contracts.enums import OutputMode, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import (
    OrchestrationInvariantError,
    PassThroughContractViolation,
    UnexpectedEmptyEmissionViolation,
    ZeroEmissionSuccessContractViolation,
)
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.core.config import AggregationSettings, TriggerConfig
from elspeth.engine.processor import _FlushContext
from elspeth.testing import make_contract, make_token_info
from tests.fixtures.landscape import leader_coordination_token, make_recorder_with_run


def _make_contract(fields: dict[str, type]) -> SchemaContract:
    return make_contract(fields=fields, mode="OBSERVED")


def _make_token(
    token_id: str,
    data: dict[str, Any],
    contract: SchemaContract,
    *,
    row_id: str | None = None,
) -> TokenInfo:
    row = PipelineRow(data, contract)
    token = make_token_info(token_id=token_id, row_id=row_id or f"row-{token_id}")
    return token.with_updated_data(row)


def _make_flush_transform(
    *,
    name: str = "test-transform",
    passes_through_input: bool = True,
    can_drop_rows: bool = False,
    output_schema_config: Any = None,
) -> Mock:
    transform = Mock(spec=TransformProtocol)
    transform.node_id = "agg-node"
    transform.name = name
    transform.on_error = "discard"
    transform.on_success = None
    transform.is_batch_aware = True
    transform.creates_tokens = False
    transform.declared_output_fields = frozenset()
    transform.passes_through_input = passes_through_input
    transform.can_drop_rows = can_drop_rows
    transform._output_schema_config = output_schema_config
    transform.effective_static_contract.return_value = (
        output_schema_config.get_effective_guaranteed_fields() if output_schema_config is not None else frozenset()
    )
    return transform


def _make_fctx(
    *,
    transform: Any,
    tokens: list[TokenInfo],
    output_mode: OutputMode,
    triggering_token: TokenInfo | None = None,
    node_id: str = "agg-node",
) -> _FlushContext:
    settings = AggregationSettings(
        name="agg",
        plugin="batch_transform",
        input="source-0",
        on_success="output",
        on_error="discard",
        trigger=TriggerConfig(count=len(tokens)),
        output_mode=output_mode,
    )
    return _FlushContext(
        node_id=NodeID(node_id),
        transform=transform,
        settings=settings,
        buffered_tokens=tuple(tokens),
        batch_id="batch-1",
        expand_parent_token=tokens[0],
        triggering_token=triggering_token or tokens[-1],
        coalesce_node_id=None,
        coalesce_name=None,
    )


def _make_processor() -> Any:
    """Build a minimal processor that can drive _cross_check_flush_output."""
    from elspeth.contracts.types import BranchName, CoalesceName, GateName, SinkName  # noqa: F401
    from elspeth.engine.processor import DAGTraversalContext, RowProcessor
    from elspeth.engine.spans import SpanFactory

    setup = make_recorder_with_run(
        run_id="test-run",
        source_node_id="source-0",
        source_plugin_name="test-source",
    )
    traversal = DAGTraversalContext(
        node_step_map={NodeID("source-0"): 0, NodeID("agg-node"): 1},
        node_to_plugin={},
        node_to_next={NodeID("source-0"): None, NodeID("agg-node"): None},
        coalesce_node_map={},
    )
    return RowProcessor(
        execution=setup.factory.execution,
        data_flow=setup.factory.data_flow,
        span_factory=SpanFactory(),
        run_id="test-run",
        source_node_id=NodeID("source-0"),
        source_on_success="default",
        traversal=traversal,
        scheduler=setup.factory.scheduler,
        coordination_token=leader_coordination_token(setup.factory, setup.run_id),
    )


def _register_tokens(processor: Any, tokens: list[TokenInfo]) -> None:
    """Register rows and tokens in the audit DB so FAILED recording has FKs.

    Production code records BUFFERED first (the BUFFERED → terminal contract
    for aggregation tokens) but that requires a batch record; tests that
    only want to exercise the cross-check + failure-recording can skip the
    BUFFERED step without tripping the validator, which only checks that
    error_hash is present for FAILED.
    """
    for idx, token in enumerate(tokens):
        processor._data_flow.create_row_with_token(
            coordination_token=processor.coordination_token,
            source_node_id="source-0",
            row_index=idx,
            data=token.row_data.to_dict(),
            row_id=token.row_id,
            token_id=token.token_id,
            source_row_index=idx,
            ingest_sequence=idx,
        )


class TestPassThroughFalseDoesNotFirePassThroughViolation:
    def test_passes_through_false_does_not_raise_pass_through_violation(self) -> None:
        """The pass-through contract stays quiet when the declaration is False."""
        processor = _make_processor()
        contract = _make_contract({"x": int})
        tokens = [_make_token(f"t{i}", {"x": i}, contract) for i in range(3)]
        transform = _make_flush_transform(passes_through_input=False)
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.PASSTHROUGH)
        # The processor still dispatches batch-flush contracts, but the
        # pass-through contract itself does not apply when the declaration is
        # false.
        result = TransformResult.success_multi(
            [PipelineRow({}, _make_contract({}))] * 3,
            success_reason={"action": "test"},
        )
        processor._cross_check_flush_output(fctx, result)


class TestPassthroughModePairwise:
    """Passthrough mode: 1:1 pairing. Each (input_token, output_row) checked independently."""

    def test_honest_passthrough_no_violation(self) -> None:
        processor = _make_processor()
        contract = _make_contract({"x": int})
        tokens = [_make_token(f"t{i}", {"x": i}, contract) for i in range(3)]
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.PASSTHROUGH)
        # Passthrough mode: same rows back, same contract.
        rows = [PipelineRow({"x": i}, contract) for i in range(3)]
        result = TransformResult.success_multi(rows, success_reason={"action": "passthrough"})
        # No exception — cross-check passes.
        processor._cross_check_flush_output(fctx, result)

    def test_mis_annotated_drops_field_violation(self) -> None:
        processor = _make_processor()
        contract = _make_contract({"x": int, "y": int})
        tokens = [_make_token(f"t{i}", {"x": i, "y": i * 10}, contract) for i in range(3)]
        _register_tokens(processor, tokens)
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.PASSTHROUGH)
        # Output drops 'y' — violation.
        reduced_contract = _make_contract({"x": int})
        rows = [PipelineRow({"x": i}, reduced_contract) for i in range(3)]
        result = TransformResult.success_multi(rows, success_reason={"action": "bad"})
        with pytest.raises(PassThroughContractViolation) as exc_info:
            processor._cross_check_flush_output(fctx, result)
        assert "y" in exc_info.value.divergence_set


class TestTransformModeIntersection:
    """Transform mode: batch-homogeneous intersection of input contracts."""

    def test_honest_transform_no_violation(self) -> None:
        processor = _make_processor()
        contract = _make_contract({"x": int, "y": int})
        tokens = [_make_token(f"t{i}", {"x": i, "y": i * 10}, contract) for i in range(3)]
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.TRANSFORM)
        # Output preserves both fields.
        rows = [PipelineRow({"x": i, "y": i * 10}, contract) for i in range(6)]
        result = TransformResult.success_multi(rows, success_reason={"action": "expand"})
        processor._cross_check_flush_output(fctx, result)

    def test_heterogeneous_intersection_permissive_non_shared(self) -> None:
        """Heterogeneous batch: intersection permits drop of non-shared field."""
        processor = _make_processor()
        # Token 1 has {x, y}, token 2 has {x, z}. Intersection = {x}.
        contract_xy = _make_contract({"x": int, "y": int})
        contract_xz = _make_contract({"x": int, "z": int})
        tokens = [
            _make_token("t0", {"x": 1, "y": 10}, contract_xy),
            _make_token("t1", {"x": 2, "z": 20}, contract_xz),
        ]
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.TRANSFORM)
        # Output carries only {x} — drops y and z, but intersection was {x}, so OK.
        reduced = _make_contract({"x": int})
        rows = [PipelineRow({"x": 1}, reduced), PipelineRow({"x": 2}, reduced)]
        result = TransformResult.success_multi(rows, success_reason={"action": "intersect"})
        processor._cross_check_flush_output(fctx, result)

    def test_heterogeneous_intersection_rejects_intersection_drop(self) -> None:
        """Heterogeneous batch: dropping intersection field fails."""
        processor = _make_processor()
        contract_xy = _make_contract({"x": int, "y": int})
        contract_xz = _make_contract({"x": int, "z": int})
        tokens = [
            _make_token("t0", {"x": 1, "y": 10}, contract_xy),
            _make_token("t1", {"x": 2, "z": 20}, contract_xz),
        ]
        _register_tokens(processor, tokens)
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.TRANSFORM)
        # Output drops 'x' which is in the intersection — violation.
        empty = _make_contract({})
        rows = [PipelineRow({}, empty), PipelineRow({}, empty)]
        result = TransformResult.success_multi(rows, success_reason={"action": "drop-intersection"})
        with pytest.raises(PassThroughContractViolation) as exc_info:
            processor._cross_check_flush_output(fctx, result)
        assert "x" in exc_info.value.divergence_set


class TestOptionalFieldAbsentFromPayload:
    """A buffered token's input fields are the contract fields its payload carries.

    An observed source records every field ``required: false``, and a row that
    lacks one still carries the source's contract. The flush cross-check must
    derive input fields as the single-token path does
    (``derive_effective_input_fields``): an optional field the row does not
    carry is not an input the transform can drop. A dropped field is still a
    violation, required or not, when every buffered row carried it (TRANSFORM
    mode) or when the paired row carried it (PASSTHROUGH mode). TRANSFORM mode
    does not attribute outputs to inputs, so a field only some emitting
    buffered rows carried is outside the intersection it checks (ADR-009
    2026-09-26 note).
    """

    def test_transform_mode_mixed_batch_without_optional_field_is_honest(self) -> None:
        processor = _make_processor()
        # The source's contract: both fields optional (``required=False``).
        contract = make_contract({"id": 1, "n": 2})
        tokens = [
            _make_token("t0", {"id": 1, "n": 2}, contract),
            _make_token("t1", {"id": 2}, contract),
        ]
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.TRANSFORM)
        # Each emitted row is a copy of its own input: t1's copy has no 'n'.
        rows = [
            PipelineRow({"id": 1, "n": 2}, contract),
            PipelineRow({"id": 1, "n": 2}, contract),
            PipelineRow({"id": 2}, contract),
        ]
        result = TransformResult.success_multi(rows, success_reason={"action": "replicate"})
        processor._cross_check_flush_output(fctx, result)

    def test_transform_mode_dropping_a_carried_optional_field_still_fires(self) -> None:
        processor = _make_processor()
        contract = make_contract({"id": 1, "n": 2})
        tokens = [
            _make_token("t0", {"id": 1, "n": 2}, contract),
            _make_token("t1", {"id": 2, "n": 1}, contract),
        ]
        _register_tokens(processor, tokens)
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.TRANSFORM)
        # Every input carried 'n'; the output drops it from payload and contract.
        reduced = make_contract({"id": 1})
        rows = [PipelineRow({"id": 1}, reduced), PipelineRow({"id": 2}, reduced)]
        result = TransformResult.success_multi(rows, success_reason={"action": "drop-carried-optional"})
        with pytest.raises(PassThroughContractViolation) as exc_info:
            processor._cross_check_flush_output(fctx, result)
        assert exc_info.value.divergence_set == frozenset({"n"})

    def test_transform_mode_dropping_a_required_field_from_the_payload_still_fires(self) -> None:
        processor = _make_processor()
        contract = _make_contract({"id": int, "n": int})
        tokens = [
            _make_token("t0", {"id": 1, "n": 2}, contract),
            _make_token("t1", {"id": 2, "n": 1}, contract),
        ]
        _register_tokens(processor, tokens)
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.TRANSFORM)
        # The payload-side vector: the contract still names 'n', the payload lost it.
        rows = [PipelineRow({"id": 1}, contract), PipelineRow({"id": 2}, contract)]
        result = TransformResult.success_multi(rows, success_reason={"action": "drop-required-payload"})
        with pytest.raises(PassThroughContractViolation) as exc_info:
            processor._cross_check_flush_output(fctx, result)
        assert exc_info.value.divergence_set == frozenset({"n"})

    def test_passthrough_mode_row_without_optional_field_is_honest(self) -> None:
        processor = _make_processor()
        contract = make_contract({"id": 1, "n": 2})
        tokens = [
            _make_token("t0", {"id": 1, "n": 2}, contract),
            _make_token("t1", {"id": 2}, contract),
        ]
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.PASSTHROUGH)
        rows = [PipelineRow({"id": 1, "n": 2}, contract), PipelineRow({"id": 2}, contract)]
        result = TransformResult.success_multi(rows, success_reason={"action": "annotate"})
        processor._cross_check_flush_output(fctx, result)

    def test_passthrough_mode_dropping_a_carried_optional_field_still_fires(self) -> None:
        processor = _make_processor()
        contract = make_contract({"id": 1, "n": 2})
        tokens = [
            _make_token("t0", {"id": 1}, contract),
            _make_token("t1", {"id": 2, "n": 1}, contract),
        ]
        _register_tokens(processor, tokens)
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.PASSTHROUGH)
        # t1 carried 'n'; its paired output lost it.
        rows = [PipelineRow({"id": 1}, contract), PipelineRow({"id": 2}, contract)]
        result = TransformResult.success_multi(rows, success_reason={"action": "drop-paired-optional"})
        with pytest.raises(PassThroughContractViolation) as exc_info:
            processor._cross_check_flush_output(fctx, result)
        assert exc_info.value.divergence_set == frozenset({"n"})
        assert exc_info.value.token_id == "t1"

    def test_transform_mode_payload_key_outside_the_contract_is_not_an_input(self) -> None:
        """The flush site uses the shared helper, not the payload's keys (panel F1).

        ``derive_effective_input_fields`` counts only the contract fields the
        payload carries, so a payload key the contract does not name is not an
        input on the single-token path. The flush site must agree. A
        ``PipelineRow`` with such a key constructs; whether a live pipeline
        delivers one to an aggregation has not been shown.
        """
        processor = _make_processor()
        contract = make_contract({"id": 1})
        tokens = [
            _make_token("t0", {"id": 1, "extra": 2}, contract),
            _make_token("t1", {"id": 2, "extra": 3}, contract),
        ]
        assert all(derive_effective_input_fields(token.row_data) == frozenset({"id"}) for token in tokens)
        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.TRANSFORM)
        rows = [PipelineRow({"id": 1}, contract), PipelineRow({"id": 2}, contract)]
        result = TransformResult.success_multi(rows, success_reason={"action": "copy-contract-fields"})
        processor._cross_check_flush_output(fctx, result)


def _quarantine_reason(*indices: int) -> Any:
    return {"action": "replicate", "metadata": {"quarantined_indices": list(indices)}}


class TestTransformModeExcludesInBatchQuarantinedInputs:
    """TRANSFORM mode intersects only over the inputs that produced output.

    An input the plugin quarantined in-batch emits nothing and is recorded
    FAILURE / QUARANTINED_AT_SOURCE; routing expands the outputs from the
    non-quarantined tokens only. If its (smaller) field set entered the
    intersection, a plugin could strip a field every emitting input carried
    and pass (the quarantine-dilution shape, ADR-009 2026-09-26 note). The
    quarantined set is the engine-validated one the cross-check returns and
    routing consumes.
    """

    def _diluted_tokens(self) -> tuple[SchemaContract, list[TokenInfo]]:
        contract = make_contract({"id": 1, "tag": "x", "n": 0})
        return contract, [
            _make_token("t0", {"id": 1, "tag": "x"}, contract),
            _make_token("t1", {"id": 2, "tag": "y"}, contract),
            _make_token("t2", {"id": 3, "n": 0}, contract),  # quarantined in-batch, lacks 'tag'
        ]

    def test_dropping_a_field_every_emitting_input_carried_fires_despite_a_quarantined_non_carrier(self) -> None:
        processor = _make_processor()
        contract, tokens = self._diluted_tokens()
        _register_tokens(processor, tokens)
        fctx = _make_fctx(transform=_make_flush_transform(), tokens=tokens, output_mode=OutputMode.TRANSFORM)
        rows = [PipelineRow({"id": 1}, contract), PipelineRow({"id": 2}, contract)]
        result = TransformResult.success_multi(rows, success_reason=_quarantine_reason(2))
        with pytest.raises(PassThroughContractViolation) as exc_info:
            processor._cross_check_flush_output(fctx, result)
        assert exc_info.value.divergence_set == frozenset({"tag"})

    def test_honest_emission_with_a_quarantined_non_carrier_passes_and_returns_the_validated_set(self) -> None:
        processor = _make_processor()
        contract, tokens = self._diluted_tokens()
        fctx = _make_fctx(transform=_make_flush_transform(), tokens=tokens, output_mode=OutputMode.TRANSFORM)
        rows = [PipelineRow({"id": 1, "tag": "x"}, contract), PipelineRow({"id": 2, "tag": "y"}, contract)]
        result = TransformResult.success_multi(rows, success_reason=_quarantine_reason(2))
        assert processor._cross_check_flush_output(fctx, result) == frozenset({2})

    def test_non_empty_emission_with_every_input_quarantined_is_an_invariant_violation(self) -> None:
        processor = _make_processor()
        contract, tokens = self._diluted_tokens()
        fctx = _make_fctx(transform=_make_flush_transform(), tokens=tokens, output_mode=OutputMode.TRANSFORM)
        rows = [PipelineRow({"id": 1, "tag": "x"}, contract)]
        result = TransformResult.success_multi(rows, success_reason=_quarantine_reason(0, 1, 2))
        with pytest.raises(OrchestrationInvariantError, match="all 3 buffered token"):
            processor._cross_check_flush_output(fctx, result)

    def test_zero_emission_with_every_input_quarantined_keeps_the_all_token_branch(self) -> None:
        """The zero-emission branch is unchanged: can_drop_rows governs it, not the quarantine set."""
        processor = _make_processor()
        _contract, tokens = self._diluted_tokens()
        fctx = _make_fctx(
            transform=_make_flush_transform(can_drop_rows=True),
            tokens=tokens,
            output_mode=OutputMode.TRANSFORM,
        )
        result = TransformResult.success_empty(success_reason=_quarantine_reason(0, 1, 2))
        assert processor._cross_check_flush_output(fctx, result) == frozenset({0, 1, 2})


class TestEmptyEmissionGovernance:
    @pytest.mark.parametrize(
        ("output_mode", "can_drop_rows"),
        [
            (OutputMode.PASSTHROUGH, False),
            (OutputMode.PASSTHROUGH, True),
            (OutputMode.TRANSFORM, False),
            (OutputMode.TRANSFORM, True),
        ],
    )
    def test_zero_emission_non_pass_through_raises_plugin_violation(
        self,
        output_mode: OutputMode,
        can_drop_rows: bool,
    ) -> None:
        processor = _make_processor()
        contract = _make_contract({"x": int})
        tokens = [_make_token(f"t{i}", {"x": i}, contract) for i in range(2)]
        _register_tokens(processor, tokens)
        transform = _make_flush_transform(passes_through_input=False, can_drop_rows=can_drop_rows)
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=output_mode)
        result = TransformResult.success_empty(success_reason={"action": "filtered"})

        with pytest.raises(ZeroEmissionSuccessContractViolation) as exc_info:
            processor._cross_check_flush_output(fctx, result)

        assert exc_info.value.passes_through_input is False
        assert exc_info.value.can_drop_rows is can_drop_rows
        assert exc_info.value.emitted_count == 0

    def test_zero_emission_passthrough_raises_aggregate_when_can_drop_rows_false(self) -> None:
        processor = _make_processor()
        contract = _make_contract({"x": int})
        tokens = [_make_token(f"t{i}", {"x": i}, contract) for i in range(2)]
        _register_tokens(processor, tokens)
        transform = _make_flush_transform(passes_through_input=True, can_drop_rows=False)
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.PASSTHROUGH)
        result = TransformResult.success_empty(success_reason={"action": "filtered"})

        with pytest.raises(AggregateDeclarationContractViolation) as exc_info:
            processor._cross_check_flush_output(fctx, result)

        child_types = {type(child).__name__ for child in exc_info.value.violations}
        assert child_types == {"UnexpectedEmptyEmissionViolation", "PassThroughContractViolation"}
        empty_child = next(child for child in exc_info.value.violations if isinstance(child, UnexpectedEmptyEmissionViolation))
        assert empty_child.payload["emitted_count"] == 0
        pass_through_child = next(child for child in exc_info.value.violations if isinstance(child, PassThroughContractViolation))
        assert pass_through_child.divergence_set == frozenset({"x"})

    def test_zero_emission_passthrough_is_allowed_when_can_drop_rows_true(self) -> None:
        processor = _make_processor()
        contract = _make_contract({"x": int})
        tokens = [_make_token(f"t{i}", {"x": i}, contract) for i in range(2)]
        transform = _make_flush_transform(passes_through_input=True, can_drop_rows=True)
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.PASSTHROUGH)
        result = TransformResult.success_empty(success_reason={"action": "filtered"})

        processor._cross_check_flush_output(fctx, result)


class TestRecordFlushViolation:
    """Per-token FAILED audit entries with correct per-token context."""

    def test_records_failed_per_token_with_own_token_id_in_context(self) -> None:
        """$.context.token_id in audit records matches each row's own token,
        not the triggering token."""
        processor = _make_processor()
        contract = _make_contract({"x": int, "y": int})
        tokens = [_make_token(f"t{i}", {"x": i, "y": i * 10}, contract) for i in range(3)]
        _register_tokens(processor, tokens)

        transform = _make_flush_transform()
        fctx = _make_fctx(transform=transform, tokens=tokens, output_mode=OutputMode.PASSTHROUGH)
        # Mis-annotated: output drops y.
        reduced = _make_contract({"x": int})
        rows = [PipelineRow({"x": i}, reduced) for i in range(3)]
        result = TransformResult.success_multi(rows, success_reason={"action": "bad"})

        with pytest.raises(PassThroughContractViolation):
            processor._cross_check_flush_output(fctx, result)

        # Verify: every buffered token now has FAILED recorded, and each record
        # references its OWN token_id in the context payload.
        import json as _json

        import sqlalchemy as sa

        for token in tokens:
            query = sa.text(
                "SELECT outcome, path, context_json FROM token_outcomes WHERE token_id = :tid AND outcome = :outcome AND path = :path"
            ).bindparams(
                tid=token.token_id,
                outcome=TerminalOutcome.FAILURE.value,
                path=TerminalPath.UNROUTED.value,
            )
            row = processor._data_flow._ops.execute_fetchone(query)
            assert row is not None, f"Token {token.token_id} has no FAILED record"
            ctx = _json.loads(row.context_json)
            assert ctx["token_id"] == token.token_id, (
                f"context.token_id must match row's own token, not triggering token — got {ctx['token_id']!r} for row {token.token_id!r}"
            )


def _token_ref(token_id: str) -> Any:
    from elspeth.contracts.audit import TokenRef

    return TokenRef(token_id=token_id, run_id="test-run")
