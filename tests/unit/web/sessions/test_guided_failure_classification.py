"""Owned exception taxonomy retains conflict subclass semantics."""

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.protocol import GuidedOperationSettlementConflictError
from elspeth.web.sessions.routes.composer.guided_plan import _guided_full_failure_code


def test_guided_full_failure_code_preserves_conflict_subclasses() -> None:
    class SpecificSettlementConflict(GuidedOperationSettlementConflictError):
        pass

    assert _guided_full_failure_code(SpecificSettlementConflict()) == "stale_conflict"
    assert _guided_full_failure_code(RuntimeError("unclassified fault")) == "operation_failed"


def test_guided_full_failure_code_preserves_integrity_subclasses() -> None:
    """Any ``AuditIntegrityError`` subtype keeps the ``integrity_error`` code.

    The registered Tier-1 family is subclassed across the tree; an exact-type
    check would silently downgrade a subclass to ``operation_failed`` and lose
    the integrity signal in the durable failure record.
    """

    class SpecificIntegrityFailure(AuditIntegrityError):
        pass

    assert _guided_full_failure_code(AuditIntegrityError("custody proof unreadable")) == "integrity_error"
    assert _guided_full_failure_code(SpecificIntegrityFailure("subclassed integrity failure")) == "integrity_error"
