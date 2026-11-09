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

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.identity_id.strip() or not self.reason.strip():
            raise ValueError("identity lifecycle event requires nonblank identity, event and reason")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("identity lifecycle event requires an aware timestamp")
        if self.actor_kind == "identity":
            if self.actor_identity_id is None or not self.actor_identity_id.strip():
                raise ValueError("identity lifecycle actor requires an identity ID")
        elif self.actor_kind in ("operator", "system"):
            if self.actor_identity_id is not None:
                raise ValueError("non-identity lifecycle actor cannot name an identity ID")
        else:
            raise ValueError("unknown identity lifecycle actor kind")


IdentityLifecycleEffect = Callable[[str, IdentityAuthorityRevoked], None]
