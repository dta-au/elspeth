"""Owner-fixture headroom for continuation tests that are not about time.

The design-protocol budget ruling records serial decode/SQL phases up to
70 seconds on a loaded shared host. Retain its 600-second owner allowance
for live fixture grants, renewals and adopted positive lifetimes. Adoption
does not itself extend the current database lease, so the grant must agree.

Explicit SQL expiry and cancellation controls keep their original meaning.
This fixture allowance does not qualify production 30-second lease liveness.
"""

OWNER_LEASE_SECONDS = 600
