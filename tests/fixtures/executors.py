"""Shared executor test contracts, call recorders, and construction helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from elspeth.contracts import (
    TokenInfo,
)
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.data import PluginSchema as _PermissiveSchema
from elspeth.contracts.enums import (
    BatchStatus,
    NodeStateStatus,
)
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.spans import SpanFactory
from elspeth.testing import make_field, make_row

# =============================================================================
# Shared helpers
# =============================================================================


def _make_contract() -> SchemaContract:
    """Create a simple test contract."""
    return SchemaContract(
        fields=(
            make_field(
                "value",
                python_type=str,
                original_name="value",
                required=True,
                source="declared",
            ),
        ),
        mode="FLEXIBLE",
        locked=True,
    )


def _make_token(
    data: dict[str, Any] | None = None,
    contract: SchemaContract | None = None,
    row_id: str = "row_1",
    token_id: str = "tok_1",
) -> TokenInfo:
    """Create a token with PipelineRow for testing."""
    if data is None:
        data = {"value": "test"}
    if contract is None:
        contract = _make_contract()
    row_data = make_row(data, contract=contract)
    return TokenInfo(row_id=row_id, token_id=token_id, row_data=row_data)


class _RecordedCall:
    def __init__(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.args = args
        self.kwargs = kwargs

    def __getitem__(self, index: int) -> Any:
        if index == 0:
            return self.args
        if index == 1:
            return self.kwargs
        raise IndexError(index)


class _CallRecorder:
    def __init__(self, *, return_value: Any = None, side_effect: Any = None) -> None:
        self.return_value = return_value
        self.side_effect = side_effect
        self.call_args_list: list[_RecordedCall] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.call_args_list.append(_RecordedCall(args, dict(kwargs)))
        if self.side_effect is None:
            return self.return_value

        effect = self.side_effect
        if isinstance(effect, list):
            effect = effect.pop(0)
        if isinstance(effect, BaseException):
            raise effect
        if isinstance(effect, type) and issubclass(effect, BaseException):
            raise effect()
        if callable(effect):
            return effect(*args, **kwargs)
        return effect

    @property
    def called(self) -> bool:
        return bool(self.call_args_list)

    @property
    def call_count(self) -> int:
        return len(self.call_args_list)

    @property
    def call_args(self) -> _RecordedCall:
        if not self.call_args_list:
            raise AssertionError("Recorder was not called")
        return self.call_args_list[-1]

    def assert_called_once(self) -> None:
        assert self.call_count == 1

    def assert_called_once_with(self, *args: Any, **kwargs: Any) -> None:
        self.assert_called_once()
        assert self.call_args.args == args
        assert self.call_args.kwargs == kwargs

    def assert_not_called(self) -> None:
        assert self.call_count == 0


def _make_factory() -> MagicMock:
    """Create a mock RecorderFactory with sensible defaults."""
    factory = MagicMock(spec=RecorderFactory)
    state = SimpleNamespace(state_id="state_001")
    factory.execution.begin_node_state.return_value = state
    factory.execution.register_artifact.return_value = SimpleNamespace(artifact_id="art_001")
    factory.execution.begin_operation.return_value = SimpleNamespace(operation_id="op_001")

    batch_counter = 0
    batches: dict[str, SimpleNamespace] = {}

    def create_batch_side_effect(*, coordination_token: CoordinationToken, aggregation_node_id: str) -> SimpleNamespace:
        nonlocal batch_counter
        assert coordination_token == _AGGREGATION_LEADER
        batch_counter += 1
        batch_id = f"batch_{batch_counter:03d}"
        batch = SimpleNamespace(
            batch_id=batch_id,
            aggregation_node_id=str(aggregation_node_id),
            status=BatchStatus.DRAFT,
            attempt=0,
        )
        batches[batch_id] = batch
        return batch

    def get_batch_side_effect(batch_id: str) -> SimpleNamespace | None:
        # The fake never advances a batch's status: every read sees the batch
        # as created (DRAFT), as a flush whose verdict never committed would.
        return batches.get(batch_id)

    factory.execution.create_batch.side_effect = create_batch_side_effect
    factory.execution.get_batch.side_effect = get_batch_side_effect
    return factory


def _single_complete_node_state_kwargs(factory: MagicMock, *, status: NodeStateStatus) -> dict[str, Any]:
    """Assert the node under test recorded exactly one terminal node-state completion."""
    factory.execution.complete_node_state.assert_called_once()
    kwargs = factory.execution.complete_node_state.call_args.kwargs
    assert kwargs["status"] == status
    return kwargs


def _make_span_factory() -> SpanFactory:
    """Create a real SpanFactory with no tracer — all spans are no-ops."""
    return SpanFactory()


def _make_transform(
    name: str = "test_transform",
    node_id: str | None = "node_1",
    on_error: str | None = None,
    declared_output_fields: frozenset[str] | None = None,
    passes_through_input: bool = False,
    forwards_input_fields: bool = False,
    can_drop_rows: bool = False,
    declared_input_fields: frozenset[str] | None = None,
    is_batch_aware: bool = False,
) -> MagicMock:
    """Create a mock transform (non-batch)."""
    # Use spec to avoid MagicMock auto-creating 'accept' attribute
    t = MagicMock(
        spec=[
            "name",
            "node_id",
            "on_error",
            "declared_output_fields",
            "declared_input_fields",
            "input_schema",
            "output_schema",
            "_on_start_called",
            "process",
            "passes_through_input",
            "forwards_input_fields",
            "can_drop_rows",
            "is_batch_aware",
            "_output_schema_config",
            "effective_static_contract",
            "declared_read_fields",
            "declared_created_fields",
            "removed_input_fields",
        ]
    )
    t.name = name
    t.node_id = node_id
    t.on_error = on_error
    t.declared_output_fields = declared_output_fields or frozenset()
    t.declared_input_fields = declared_input_fields or frozenset()
    t.input_schema = _PermissiveSchema  # Accepts any row — validation is a no-op
    t.output_schema = _PermissiveSchema  # Accepts any row — validation is a no-op
    t._on_start_called = True
    t.passes_through_input = passes_through_input
    t.forwards_input_fields = forwards_input_fields
    t.can_drop_rows = can_drop_rows
    t.is_batch_aware = is_batch_aware
    t._output_schema_config = None
    t.effective_static_contract.return_value = frozenset()
    # The field-name spelling surfaces (TransformProtocol): a mock declares its
    # required inputs as reads and its output fields as created names, as
    # BaseTransform does, and removes nothing it forwards.
    t.declared_read_fields = t.declared_input_fields
    t.declared_created_fields = t.declared_output_fields
    t.removed_input_fields = frozenset()
    return t


_AGGREGATION_LEADER = CoordinationToken(run_id="test-run", worker_id="mock-worker", leader_epoch=1)
