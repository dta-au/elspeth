"""Closed nominal checkpoint timing dispatch; cold DATA grants no live clock."""

from __future__ import annotations

from weakref import ReferenceType

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.sessions.continuation_checkpoint_paid import PreparedCheckpointProvider


def require_checkpoint_prepared_clock(prepared: PreparedCheckpointProvider) -> ContinuationActionClock:
    from elspeth.web.composer.continuation_checkpoint_early_physical import (
        CheckpointEarlyPhysicalSource,
        require_checkpoint_early_prepared_clock,
    )
    from elspeth.web.composer.continuation_checkpoint_end_physical import CheckpointEndPhysicalSource, require_checkpoint_end_prepared_clock

    if type(prepared) is not PreparedCheckpointProvider or type(prepared.physical_source) is not ReferenceType:
        raise AuditIntegrityError("checkpoint timing requires its original nominal weak source")
    source = prepared.physical_source()
    if type(source) is CheckpointEndPhysicalSource:
        return require_checkpoint_end_prepared_clock(prepared)
    if type(source) is CheckpointEarlyPhysicalSource:
        return require_checkpoint_early_prepared_clock(prepared)
    raise AuditIntegrityError("checkpoint timing lost its original installed physical source")
