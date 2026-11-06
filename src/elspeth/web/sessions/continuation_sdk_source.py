"""Normalized SDK source DATA, never independently a settlement grant.

Only family SQL factories construct these inside the shared writer/cold reader.
The public writer accepts original custody and the privately issued observation,
not a caller-supplied instance of either normalized carrier.
"""

from dataclasses import dataclass

from elspeth.web.sessions.composer_operations import ComposerOperationRunning
from elspeth.web.sessions.continuation import StoredContinuationSnapshot
from elspeth.web.sessions.continuation_paid_overflow import PaidOverflowIdentity
from elspeth.web.sessions.continuation_retained_capacity import ProviderTerminalHoldSource
from elspeth.web.sessions.continuation_sdk_tail import SdkTailPlan, SdkTerminalFamily


@dataclass(frozen=True, slots=True)
class SdkTerminalSource:
    hold: ProviderTerminalHoldSource
    admitted: StoredContinuationSnapshot
    requested_model: str
    tail: SdkTailPlan
    env_ref_names: frozenset[str] = frozenset()
    unresolved_outer_effects: tuple[str, ...] = ()

    @property
    def identity(self) -> PaidOverflowIdentity:
        return self.tail.identity

    @property
    def family(self) -> SdkTerminalFamily:
        return self.tail.family


@dataclass(frozen=True, slots=True)
class SdkTerminalLiveSource:
    source: SdkTerminalSource
    running: ComposerOperationRunning
