"""Optional host governance for each chargeable provider attempt."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LLMCallGovernance:
    """Admit before dispatch and account only after its durable audit outcome.

    Hook failures propagate. Hosts must make outcome accounting idempotent by
    the durable call identity; absent governance preserves standalone CLI use.
    """

    before_call: Callable[[], str]
    after_call: Callable[[str, str], None]
