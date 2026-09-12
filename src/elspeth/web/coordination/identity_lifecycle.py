"""Owned facts emitted when identity authority is withdrawn.

Effects execute synchronously with an expiring Sessions transaction token.
The composition root supplies consumers; identity owns no consumer SQL.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class IdentityAuthorityRevoked:
    event_id: str
    identity_id: str
    occurred_at: datetime
    actor_kind: Literal["identity", "system", "operator"]
    actor_identity_id: str | None
    reason: str


IdentityLifecycleEffect = Callable[[str, IdentityAuthorityRevoked], None]
