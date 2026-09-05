"""Tests for the instance-identity middleware (6b-3, elspeth-31878c9787).

Every HTTP response — success, client error, validation error, the
synthesized 500 for a handler that raised before responding — must carry the
answering process's ``X-Elspeth-Instance``. At replicas > 1 that header is
the only routing-independent way a client (the acceptance probes) can tell
which replica served or refused a request.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from pydantic import BaseModel
from starlette.types import Receive, Scope, Send

from elspeth.web.deployment_profiles import INSTANCE_ID_MAX_LENGTH
from elspeth.web.middleware.instance_identity import INSTANCE_HEADER, InstanceIdentityMiddleware
from elspeth.web.middleware.request_id import REQUEST_ID_HEADER, RequestIdMiddleware
from tests.unit.web._sync_asgi_client import SyncASGITestClient as TestClient

_INSTANCE = "web-7f3a2c1e-9b4d-4f6a-8c2e-1d5b7a9c3e0f"


class _Body(BaseModel):
    count: int


def _make_app(instance_id: str = _INSTANCE) -> FastAPI:
    app = FastAPI()
    # Same composition as web/app.py: request-id inside, instance identity
    # outermost so the request-id middleware's synthesized 500 is stamped too.
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(InstanceIdentityMiddleware, instance_id=instance_id)

    @app.api_route("/_ok", methods=["GET", "HEAD"])
    async def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/_validate")
    async def validate(body: _Body) -> dict[str, int]:
        return {"count": body.count}

    @app.get("/_boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    return app


class TestHeaderOnEveryResponse:
    def test_header_name_is_pinned(self) -> None:
        assert INSTANCE_HEADER == "X-Elspeth-Instance"

    def test_success_response_carries_the_instance(self) -> None:
        response = TestClient(_make_app()).get("/_ok")
        assert response.status_code == 200
        assert response.headers[INSTANCE_HEADER] == _INSTANCE

    def test_not_found_carries_the_instance(self) -> None:
        response = TestClient(_make_app()).get("/_missing")
        assert response.status_code == 404
        assert response.headers[INSTANCE_HEADER] == _INSTANCE

    def test_validation_error_carries_the_instance(self) -> None:
        response = TestClient(_make_app()).post("/_validate", json={"count": "not-a-number"})
        assert response.status_code == 422
        assert response.headers[INSTANCE_HEADER] == _INSTANCE

    def test_synthesized_500_carries_the_instance_and_the_request_id(self) -> None:
        """A handler that raises before responding still identifies the replica that failed."""
        client = TestClient(_make_app(), raise_server_exceptions=False)
        response = client.get("/_boom")
        assert response.status_code == 500
        assert response.headers[INSTANCE_HEADER] == _INSTANCE
        assert REQUEST_ID_HEADER in response.headers

    def test_head_response_carries_the_instance(self) -> None:
        response = TestClient(_make_app()).head("/_ok")
        assert response.status_code == 200
        assert response.headers[INSTANCE_HEADER] == _INSTANCE

    def test_inbound_header_is_never_echoed(self) -> None:
        """The value is the process's own identity; a client cannot assert one."""
        response = TestClient(_make_app()).get("/_ok", headers={INSTANCE_HEADER: "spoofed"})
        assert response.headers[INSTANCE_HEADER] == _INSTANCE


class TestNonHttpScopesPassThrough:
    @pytest.mark.asyncio
    async def test_lifespan_scope_is_forwarded_untouched(self) -> None:
        seen: list[Scope] = []

        async def inner(scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(scope)

        middleware = InstanceIdentityMiddleware(inner, instance_id=_INSTANCE)
        scope: Scope = {"type": "lifespan"}

        async def receive() -> dict[str, str]:
            return {"type": "lifespan.startup"}

        async def send(_message: object) -> None:
            pytest.fail("lifespan scope must not be answered by the middleware")

        await middleware(scope, receive, send)
        assert seen == [scope]


class TestConstructionIsFailClosed:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("", id="blank"),
            pytest.param("web\r\nX-Injected: 1", id="crlf"),
            pytest.param("-leading", id="leading-dash"),
            pytest.param("has space", id="space"),
            pytest.param("A" * (INSTANCE_ID_MAX_LENGTH + 1), id="too-long"),
        ],
    )
    def test_unsafe_instance_id_is_rejected_before_any_response(self, value: str) -> None:
        with pytest.raises(ValueError, match="instance_id"):
            InstanceIdentityMiddleware(FastAPI(), instance_id=value)

    def test_instance_id_is_readable_for_diagnostics(self) -> None:
        middleware = InstanceIdentityMiddleware(FastAPI(), instance_id=_INSTANCE)
        assert middleware.instance_id == _INSTANCE
