"""Size observations from the original mandatory capture boundary.

These value-free observations confer no paid-result or persistence authority.
Only the paid owner can bind one to its original returned call and live attempt.
"""

from dataclasses import dataclass
from typing import Literal

from elspeth.contracts.bounded_serialization import BoundedCanonicalSize
from elspeth.web.composer.continuation_retention import RetentionSizeReason, require_retention_capacity
from elspeth.web.composer.protocol import ComposerContextUnsupported


@dataclass(frozen=True, slots=True)
class ContinuationCaptureOverflowWitness:
    unit: Literal["private_chunk", "evidence_pair"]
    codec: str
    observed_bytes: int
    limit_bytes: int
    size_is_exact: bool


class ContinuationMandatoryCaptureOverflow(Exception):
    """Nominal cause preserving the original outer refusal and public payload.

    The payload is frozen, while the exception retains normal mutable traceback
    machinery for propagation through context managers.
    """

    def __init__(self, witness: ContinuationCaptureOverflowWitness) -> None:
        self._witness = witness
        super().__init__("original mandatory capture exceeds its byte limit")

    @property
    def witness(self) -> ContinuationCaptureOverflowWitness:
        return self._witness


def require_capture_retention_capacity(measured: BoundedCanonicalSize, *, reason: RetentionSizeReason, codec: str) -> int:
    """Preserve refusal while identifying the original saturated measurement."""
    try:
        return require_retention_capacity(measured, reason=reason)
    except ComposerContextUnsupported as error:
        raise error from ContinuationMandatoryCaptureOverflow(
            ContinuationCaptureOverflowWitness(
                unit="private_chunk",
                codec=codec,
                observed_bytes=measured.limit + 1,
                limit_bytes=measured.limit,
                size_is_exact=False,
            )
        )
