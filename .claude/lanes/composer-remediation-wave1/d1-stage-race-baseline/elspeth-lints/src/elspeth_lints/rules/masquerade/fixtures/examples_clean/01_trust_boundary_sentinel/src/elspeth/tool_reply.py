"""Clean fixture: sentinel 3-arg getattr on a @trust_boundary source_param.

Amnesty (c): the receiver (``response``) is exactly the decorator's
``source_param``, the name is a string literal, and the call is the
prescribed 3-arg sentinel-defaulted form. Permanently green, no baseline
entry required.
"""

from __future__ import annotations

from elspeth.contracts.trust_boundary import trust_boundary


@trust_boundary(
    tier=3,
    source="vendor tool-call reply",
    source_param="response",
    suppresses=(),
    invariant="returns None when the field is absent",
)
def read_status(response):
    return getattr(response, "status", None)
