"""Nominal runtime contract for row-pipelined batch transforms.

This contract is intentionally lower-level than ``BatchTransformProtocol``:
``BatchTransformProtocol`` models aggregation-style batch plugins, while this
contract models transforms that accept one row, do concurrent internal work,
and emit the row result through an output adapter.

``BatchTransformRuntime`` is a NOMINAL opt-in base class, not a structural
Protocol (ADR-032: never dispatch on a ``runtime_checkable`` Protocol — an
impostor passes and a widened Protocol silently reclassifies every
implementation tree-wide). The engine's transform executor dispatches with
``isinstance(transform, BatchTransformRuntime)``; a transform participates in
the row-pipelined batch runtime by inheriting this class (in practice via
``BatchTransformMixin``, which subclasses it and supplies the machinery).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from elspeth.contracts.contexts import TransformContext
    from elspeth.contracts.schema_contract import PipelineRow


class BatchTransformRuntime:
    """Nominal opt-in consumed by the engine's single-row transform executor.

    Subclasses must implement every member; the defaults raise so a partial
    implementation fails loudly at first use instead of degrading silently.
    """

    node_id: str | None

    @property
    def batch_runtime_enabled(self) -> bool:
        """Whether this transform participates in row-pipelined batch runtime."""
        raise NotImplementedError

    @property
    def batch_pool_size(self) -> int:
        """Preferred number of pending row submissions."""
        raise NotImplementedError

    @property
    def batch_wait_timeout(self) -> float:
        """Maximum seconds the executor should wait for one row result."""
        raise NotImplementedError

    def accept(self, row: PipelineRow, ctx: TransformContext) -> None:
        """Submit one row to the transform's internal batch runtime."""
        raise NotImplementedError

    def connect_output(self, output: Any, max_pending: int = 30) -> None:
        """Connect the engine-owned output adapter."""
        raise NotImplementedError

    def evict_submission(self, token_id: str, state_id: str) -> bool:
        """Evict a timed-out token/state submission from the internal buffer."""
        raise NotImplementedError
