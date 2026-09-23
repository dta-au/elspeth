# tests/unit/engine/test_batch_contract_validation_parity.py
"""Every node kind that runs a batch-transform contract must check it (elspeth-c2fa61cf57).

`CollectorExecutor` called `transform.process` with no contract preflight while
`AggregationExecutor` validated both halves, so a row violating a collector's
own `mode: fixed` contract reached the plugin and the run banked a clean
COMPLETED over it.

The obligation is DERIVED, not restated. `NESTED_CONTRACT_OPTIONS_NODE_TYPES`
(`contracts/schema.py`) is already the repo's answer to "which node kinds run a
batch-transform plugin under an `options` schema". A third such kind added
there inherits this requirement automatically and fails
`test_the_registry_is_total_over_the_derived_node_kinds` until someone wires it
— which is the whole point of deriving from the set rather than listing the two
executors we happen to have today.

The ticket also recorded that `grep -rn "_validate_batch_inputs|_validate_success_outputs" tests/`
returned nothing: NEITHER half was pinned for aggregation either. These cases
cover both node kinds, so the aggregation half is no longer unpinned.
"""

from __future__ import annotations

import inspect
from types import ModuleType
from typing import ClassVar

import pytest
from pydantic import ConfigDict

from elspeth.contracts import PluginSchema, TransformResult
from elspeth.contracts.enums import NodeType
from elspeth.contracts.errors import PluginContractViolation
from elspeth.contracts.schema import NESTED_CONTRACT_OPTIONS_NODE_TYPES
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.engine.executors import aggregation as aggregation_module
from elspeth.engine.executors import collector as collector_module
from elspeth.engine.executors.batch_contract_validation import validate_batch_inputs, validate_success_outputs
from elspeth.testing import make_pipeline_row

# The executor module that owns each derived node kind. This mapping is the ONE
# thing a new node kind must extend; the test below asserts it stays total over
# the derived set, so forgetting is a red test rather than a silent gap.
_VALIDATING_EXECUTOR_MODULES: dict[NodeType, ModuleType] = {
    NodeType.AGGREGATION: aggregation_module,
    NodeType.COLLECTOR: collector_module,
}


class _StrictItemSchema(PluginSchema):
    """A locked contract admitting exactly `item`."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    item: int


class _ObservedSchema(PluginSchema):
    """An observed contract: no declared fields, any extra admitted."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")


class _FakeBatchTransform:
    """Minimal `BatchTransformProtocol` surface the validators actually read."""

    def __init__(self, *, input_schema: type[PluginSchema] = _StrictItemSchema, required: frozenset[str] = frozenset()) -> None:
        self.name = "fake_batch"
        self.input_schema: type[PluginSchema] = input_schema
        self.output_schema: type[PluginSchema] = _StrictItemSchema
        self._required = required

    def schema_required_input_fields(self) -> frozenset[str]:
        return self._required


# A row value that must never reach an audit message.
_SENTINEL = "SENTINEL-value-7f3a91"


def _row(payload: dict[str, object]) -> PipelineRow:
    return make_pipeline_row(payload)


def test_the_registry_is_total_over_the_derived_node_kinds() -> None:
    """A new nested-contract node kind must be wired, not silently skipped.

    Derived from `NESTED_CONTRACT_OPTIONS_NODE_TYPES` rather than from
    `NodeType` (too wide — most kinds run no batch transform) or `CloserKind`
    (wrong axis — aggregation is not a closer). If a third kind joins that set,
    this fails until its executor is registered below.
    """
    assert set(_VALIDATING_EXECUTOR_MODULES) == set(NESTED_CONTRACT_OPTIONS_NODE_TYPES)


@pytest.mark.parametrize("node_type", sorted(NESTED_CONTRACT_OPTIONS_NODE_TYPES))
def test_each_derived_node_kind_runs_both_contract_halves(node_type: NodeType) -> None:
    """Both halves, both kinds — the collector was missing both."""
    module = _VALIDATING_EXECUTOR_MODULES[node_type]
    source = inspect.getsource(module)

    assert "validate_batch_inputs(" in source, f"{node_type} executor does not run the input preflight"
    assert "validate_success_outputs(" in source, f"{node_type} executor does not run the output postflight"


@pytest.mark.parametrize("node_type", sorted(NESTED_CONTRACT_OPTIONS_NODE_TYPES))
def test_both_kinds_share_one_implementation_rather_than_a_copy(node_type: NodeType) -> None:
    """The check must be IMPORTED, never re-implemented per executor.

    A second copy of a rule is the same defect as a restatement: the two drift
    and nothing makes them drift together. That is how the collector ended up
    without a preflight at all while its sibling had one.
    """
    module = _VALIDATING_EXECUTOR_MODULES[node_type]
    source = inspect.getsource(module)

    assert "from elspeth.engine.executors.batch_contract_validation import" in source
    assert "model_validate" not in source, f"{node_type} executor re-implements contract validation instead of importing it"


@pytest.mark.parametrize("node_kind", ["Aggregation", "Collector"])
class TestSharedValidators:
    """Direct coverage of both halves for both operator-facing labels."""

    def test_conforming_input_rows_pass(self, node_kind: str) -> None:
        validate_batch_inputs(_FakeBatchTransform(), [_row({"item": 1}), _row({"item": 2})], node_kind=node_kind)

    def test_an_extra_field_on_a_buffered_row_is_rejected(self, node_kind: str) -> None:
        with pytest.raises(PluginContractViolation) as excinfo:
            validate_batch_inputs(_FakeBatchTransform(), [_row({"item": 1}), _row({"item": 2, "id": 9})], node_kind=node_kind)

        message = str(excinfo.value)
        assert message.startswith(f"{node_kind} transform 'fake_batch' input validation failed for buffered row 1")
        # The INDEX is named so an operator can find the offending row.
        assert "buffered row 1" in message

    def test_conforming_emitted_rows_pass(self, node_kind: str) -> None:
        result = TransformResult.success_multi((_row({"item": 3}),), success_reason={"action": "collected"})
        validate_success_outputs(_FakeBatchTransform(), result, node_kind=node_kind)

    def test_an_emitted_row_violating_the_output_contract_is_rejected(self, node_kind: str) -> None:
        result = TransformResult.success_multi((_row({"assembled": True}),), success_reason={"action": "collected"})

        with pytest.raises(PluginContractViolation) as excinfo:
            validate_success_outputs(_FakeBatchTransform(), result, node_kind=node_kind)

        assert str(excinfo.value).startswith(f"{node_kind} transform 'fake_batch' output validation failed for emitted row 0")

    def test_a_result_carrying_no_rows_is_not_an_output_violation(self, node_kind: str) -> None:
        """`success_empty` emits nothing; there is no row to check.

        Distinct from a zero-row FAIL state — this validator's only question is
        whether emitted rows match the declared contract, and an empty emission
        has none. The zero-row question belongs to `can_drop_rows`.
        """
        validate_success_outputs(
            _FakeBatchTransform(), TransformResult.success_empty(success_reason={"action": "noop"}), node_kind=node_kind
        )

    def test_a_rejected_buffered_row_value_never_reaches_the_message(self, node_kind: str) -> None:
        """The message names the row index, field and error type — never the VALUE.

        Pydantic's ``str(ValidationError)`` echoes ``input_value=...``. This
        message is the violation's audit text (the failed node_state, and the
        routed reason once the flush routes it), so the row value must not be
        in it.
        """
        with pytest.raises(PluginContractViolation) as excinfo:
            validate_batch_inputs(_FakeBatchTransform(), [_row({"item": 1}), _row({"item": _SENTINEL})], node_kind=node_kind)

        message = str(excinfo.value)
        assert message.startswith(f"{node_kind} transform 'fake_batch' input validation failed for buffered row 1: ")
        assert "item: " in message
        assert "[int_type]" in message
        assert _SENTINEL not in message

    def test_a_rejected_emitted_row_value_never_reaches_the_message(self, node_kind: str) -> None:
        result = TransformResult.success_multi((_row({"item": _SENTINEL}),), success_reason={"action": "collected"})

        with pytest.raises(PluginContractViolation) as excinfo:
            validate_success_outputs(_FakeBatchTransform(), result, node_kind=node_kind)

        message = str(excinfo.value)
        assert message.startswith(f"{node_kind} transform 'fake_batch' output validation failed for emitted row 0: ")
        assert "[int_type]" in message
        assert _SENTINEL not in message

    def test_a_buffered_row_omitting_a_declared_field_is_rejected_without_its_content(self, node_kind: str) -> None:
        """An observed model cannot see ``required_fields``; the presence check does (R1).

        The offending row carries a sentinel in ANOTHER column. Neither that
        value nor that column's name may reach the message: under an observed
        source a row's keys are row content too, so only the CONFIGURED field
        is named.
        """
        transform = _FakeBatchTransform(input_schema=_ObservedSchema, required=frozenset({"score", "variant"}))

        with pytest.raises(PluginContractViolation) as excinfo:
            validate_batch_inputs(
                transform,
                [_row({"score": 1, "variant": "a"}), _row({"variant": "b", "sentinel_column": _SENTINEL})],
                node_kind=node_kind,
            )

        assert str(excinfo.value) == (
            f"{node_kind} transform 'fake_batch' input validation failed for buffered row 1: "
            "required input field(s) ['score'] absent from the row. The transform's schema declares them required."
        )
        assert _SENTINEL not in str(excinfo.value)
        assert "sentinel_column" not in str(excinfo.value)

    def test_presence_is_reported_before_the_model(self, node_kind: str) -> None:
        """A row both missing a declared field and failing the model reports the absence.

        Same ordering as the per-row preflight, whose declaration check runs
        before ``model_validate`` so a missing field is not diluted into a
        schema failure.
        """
        transform = _FakeBatchTransform(required=frozenset({"item"}))

        with pytest.raises(PluginContractViolation) as excinfo:
            validate_batch_inputs(transform, [_row({"other": 1})], node_kind=node_kind)

        assert "required input field(s) ['item'] absent" in str(excinfo.value)
        assert "validation error" not in str(excinfo.value)

    def test_a_declared_field_present_as_none_is_not_absent(self, node_kind: str) -> None:
        """Presence, not value: a present ``None`` is the plugin's to count as missing."""
        transform = _FakeBatchTransform(input_schema=_ObservedSchema, required=frozenset({"score"}))

        validate_batch_inputs(transform, [_row({"score": None})], node_kind=node_kind)
