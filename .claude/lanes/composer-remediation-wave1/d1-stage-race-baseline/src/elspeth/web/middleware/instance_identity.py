"""Instance-identity middleware — the process identity on every response.

Every HTTP response leaving this process carries ``X-Elspeth-Instance``: the
id ``web/deployment_profiles.resolve_instance_id`` minted (or the operator
pinned through ``ELSPETH_WEB__INSTANCE_ID``) and the session service uses as
its fence ``owner_instance_id``. At replicas > 1 the header is how a client —
the multi-replica acceptance probes in particular — learns *which* replica
answered, and how a 409 ``"Session operation is already active"`` from one
replica is paired with the 2xx from the other without trusting routing.

The header is emitted on **every** response, including 4xx/5xx envelopes and
the synthesized 500 the request-id middleware writes when a handler raises
before its response started, because a probe that scores a conflict needs the
identity of the replica that *refused* as much as the one that served.

The value is validated once at construction against the same bounded
allow-list the request-id header uses, so nothing that could smuggle a
header continuation or a control character is ever placed on the wire. The
middleware never reads inbound headers.

Layer: L3 (application middleware). Plain ASGI so it composes outside the
request-id middleware and stamps its synthesized error response too.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from elspeth.web.deployment_profiles import INSTANCE_ID_MAX_LENGTH, is_valid_instance_id

INSTANCE_HEADER = "X-Elspeth-Instance"
"""Response header carrying the answering process's instance id."""


class InstanceIdentityMiddleware:
    """Stamp ``X-Elspeth-Instance: <instance_id>`` on every HTTP response."""

    def __init__(self, app: ASGIApp, *, instance_id: str) -> None:
        if not is_valid_instance_id(instance_id):
            raise ValueError(
                f"instance_id must be 1-{INSTANCE_ID_MAX_LENGTH} characters of [A-Za-z0-9._-] with a leading alphanumeric; "
                "refusing to place an unsafe value on the wire"
            )
        self.app = app
        self._instance_id = instance_id

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_instance(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[INSTANCE_HEADER] = self._instance_id
            await send(message)

        await self.app(scope, receive, send_with_instance)
