"""Optional host governance for each chargeable provider attempt."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from elspeth.contracts.errors import FrameworkBugError


@dataclass(frozen=True, slots=True)
class LLMCallGovernance:
    """Admit before dispatch and account only after its durable audit outcome.

    Hook failures propagate. Hosts must make outcome accounting idempotent by
    the durable call identity; absent governance preserves standalone CLI use.
    The admitted attempt identity is also the physical outcome's Landscape
    call identity. This joins the two durable stores after a worker restart,
    including a crash after outcome commit but before ``after_call``.
    """

    before_call: Callable[[], str]
    after_call: Callable[[str, str], None]
    outcome_timestamp: Callable[[str], datetime] | None = None

    def timestamp_for_outcome(self, attempt_id: str | None) -> datetime | None:
        """Use the quota authority's clock for durable outcome chronology."""
        if self.outcome_timestamp is None:
            return None
        if attempt_id is None:
            raise FrameworkBugError("Governed LLM outcome has no admitted attempt identity")
        return self.outcome_timestamp(attempt_id)
