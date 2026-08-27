# tests/unit/engine/test_expand_width_fence.py
"""Expand-width fence (elspeth-258bd49d81) — the mint-seam backstop and the
loss-reason derivation.

Depth is fenced fail-closed at build (spec §6.3); width is data-dependent, so
the engine fences it at run: the traversal's multi-row arm refuses ahead of
the mint and routes the row through the transform error channel (integration
coverage: tests/integration/pipeline/test_expand_width_fence.py), while
TokenManager.expand_token carries the same ceiling as the fail-closed backstop
for callers without a loss channel. The backstop must fire BEFORE any DB work
— the mint is one eager transaction and the whole point is never to start it.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from elspeth.contracts import TokenInfo, TransformResult
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.core.config import ElspethSettings
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.engine.token_traversal import _branch_loss_reason
from elspeth.engine.tokens import TokenManager


class _MintReached(Exception):
    """Sentinel: the repository mint was invoked (the fence let the call through)."""


def _make_parent_token() -> TokenInfo:
    contract = Mock(spec=SchemaContract)
    contract.locked = True
    return TokenInfo(
        row_id="row-1",
        token_id="tok-parent",
        row_data=Mock(spec=PipelineRow),
        lineage_path=(),
    )


def _make_manager(*, ceiling: int | None) -> tuple[TokenManager, Mock]:
    data_flow = Mock(spec=DataFlowRepository)
    manager = TokenManager(
        data_flow,
        step_resolver=lambda node_id: 1,
        max_expand_group_width=ceiling,
    )
    return manager, data_flow


class TestExpandTokenWidthBackstop:
    def test_over_ceiling_refuses_before_any_db_work(self) -> None:
        manager, data_flow = _make_manager(ceiling=2)
        contract = Mock(spec=SchemaContract)
        contract.locked = True

        with pytest.raises(OrchestrationInvariantError, match=r"3 members.*max_expand_group_width=2"):
            manager.expand_token(
                parent_token=_make_parent_token(),
                expanded_rows=[{"v": 1}, {"v": 2}, {"v": 3}],
                output_contract=contract,
                node_id=NodeID("t-explode"),
                run_id="run-1",
            )

        data_flow.expand_token.assert_not_called()

    def test_at_ceiling_proceeds_to_mint(self) -> None:
        # The control: a mint AT the ceiling passes the fence. The sentinel
        # raise from the mocked repository proves the mint was reached without
        # modelling the rest of the mint's return contract.
        manager, data_flow = _make_manager(ceiling=2)
        contract = Mock(spec=SchemaContract)
        contract.locked = True
        data_flow.expand_token.side_effect = _MintReached

        with pytest.raises(_MintReached):
            manager.expand_token(
                parent_token=_make_parent_token(),
                expanded_rows=[{"v": 1}, {"v": 2}],
                output_contract=contract,
                node_id=NodeID("t-explode"),
                run_id="run-1",
            )

    def test_none_ceiling_is_unfenced(self) -> None:
        manager, data_flow = _make_manager(ceiling=None)
        contract = Mock(spec=SchemaContract)
        contract.locked = True
        data_flow.expand_token.side_effect = _MintReached

        with pytest.raises(_MintReached):
            manager.expand_token(
                parent_token=_make_parent_token(),
                expanded_rows=[{"v": n} for n in range(50)],
                output_contract=contract,
                node_id=NodeID("t-explode"),
                run_id="run-1",
            )


class TestBranchLossReasonDerivation:
    """ONE derivation for both error arms — the pass-through categories carry
    their own ledger meaning; everything else takes the arm's default."""

    def test_expand_width_exceeded_passes_through(self) -> None:
        result = TransformResult.error({"reason": "expand_width_exceeded", "message": "too wide"})
        assert _branch_loss_reason(result, default="quarantined") == "expand_width_exceeded"
        assert _branch_loss_reason(result, default="error_routed") == "expand_width_exceeded"

    def test_retry_exhausted_maps_to_max_retries_exceeded(self) -> None:
        result = TransformResult.error({"reason": "retry_exhausted", "message": "gave up"})
        assert _branch_loss_reason(result, default="quarantined") == "max_retries_exceeded"

    def test_other_categories_take_the_arm_default(self) -> None:
        result = TransformResult.error({"reason": "validation_failed", "message": "bad row"})
        assert _branch_loss_reason(result, default="quarantined") == "quarantined"
        assert _branch_loss_reason(result, default="error_routed") == "error_routed"


class TestSettingsField:
    def test_default_and_bounds(self) -> None:
        schema = ElspethSettings.model_json_schema()["properties"]["max_expand_group_width"]
        assert schema["default"] == 100_000
        assert schema["minimum"] == 1
        assert schema["maximum"] == 10_000_000
