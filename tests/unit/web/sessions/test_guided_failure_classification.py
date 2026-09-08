"""Owned exception taxonomy retains conflict subclass semantics."""

from elspeth.web.sessions.protocol import GuidedOperationSettlementConflictError
from elspeth.web.sessions.routes.composer.guided_plan import _guided_full_failure_code


def test_guided_full_failure_code_preserves_conflict_subclasses() -> None:
    class SpecificSettlementConflict(GuidedOperationSettlementConflictError):
        pass

    assert _guided_full_failure_code(SpecificSettlementConflict()) == "stale_conflict"
    assert _guided_full_failure_code(RuntimeError("unclassified fault")) == "operation_failed"
