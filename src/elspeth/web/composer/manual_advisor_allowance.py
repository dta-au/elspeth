"""Process-local reservation ownership for one manual Advisor batch.

Only the spent count is retained by continuation state. A reservation belongs
to one live call stage, and must close before that stage can be checkpointed.
Synchronous reserve/dispatch/close operations make overlapping async callers
share the same backstop without acquiring permission from audit-buffer growth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from elspeth.contracts.errors import AuditIntegrityError


@dataclass(slots=True)
class ManualAdvisorAllowance:
    limit: int
    used: int
    _reserved: int = field(default=0, init=False, repr=False)
    _active: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.limit) is not int or type(self.used) is not int or self.limit < 0 or self.used < 0:
            raise AuditIntegrityError("Manual Advisor allowance requires exact bounded accounting")

    @property
    def available(self) -> int:
        # A lowered live policy cannot erase previously spent attempts.
        return max(0, self.limit - self.used - self._reserved)

    def reserve(self) -> ManualAdvisorReservation | None:
        if self.available <= 0:
            return None
        self._reserved += 1
        self._active += 1
        return ManualAdvisorReservation(self)

    def require_settled(self, *, used: int) -> None:
        """A retained cut cannot erase live dispatch or refund obligations."""
        if self._active != 0 or self._reserved != 0:
            raise AuditIntegrityError("Manual Advisor allowance has an active reservation at the retained cut")
        if type(used) is not int or self.used != used:
            raise AuditIntegrityError("Manual Advisor allowance differs from its retained spent count")


@dataclass(slots=True)
class ManualAdvisorReservation:
    _owner: ManualAdvisorAllowance = field(repr=False)
    _state: Literal["reserved", "dispatched", "closed"] = field(default="reserved", init=False)

    def dispatch(self) -> None:
        if self._state != "reserved" or self._owner._reserved <= 0:
            raise AuditIntegrityError("Manual Advisor reservation cannot dispatch twice or after release")
        self._owner._reserved -= 1
        self._owner.used += 1
        self._state = "dispatched"

    def close(self) -> None:
        if self._state == "closed":
            raise AuditIntegrityError("Manual Advisor reservation closed twice")
        if self._state == "reserved":
            self._owner._reserved -= 1
        self._owner._active -= 1
        self._state = "closed"
