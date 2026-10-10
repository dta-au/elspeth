"""HTTP test client shim for Python 3.13/anyio portal-sensitive route tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from threading import Thread
from types import TracebackType
from typing import Any, Self

import anyio
from httpx import ASGITransport, AsyncClient, Cookies, Response
from sniffio import AsyncLibraryNotFoundError, current_async_library


class SyncASGITestClient:
    """Small sync facade over httpx's async ASGI transport."""

    __test__ = False

    def __init__(
        self,
        app: Any,
        *,
        raise_server_exceptions: bool = True,
        transport_app: Any | None = None,
    ) -> None:
        self.app = app
        self._app = transport_app or app
        self._raise_server_exceptions = raise_server_exceptions
        self.cookies = Cookies()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        async def send(client: AsyncClient) -> Response:
            return await client.request(method, url, **kwargs)

        return self.run_in_one_loop(send)

    def run_in_one_loop[T](self, work: Callable[[AsyncClient], Awaitable[T]]) -> T:
        """Keep a sequence of requests and their owned tasks in one event loop."""

        async def run() -> T:
            async with AsyncClient(
                transport=ASGITransport(app=self._app, raise_app_exceptions=self._raise_server_exceptions),
                base_url="http://test",
                cookies=self.cookies,
            ) as client:
                result = await work(client)
                self.cookies.update(client.cookies)
                return result

        if not _inside_async_context():
            return anyio.run(run)
        outcomes: list[T] = []
        errors: list[BaseException] = []

        def run_in_thread() -> None:
            try:
                outcomes.append(anyio.run(run))
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=run_in_thread, daemon=True)
        thread.start()
        thread.join()
        if errors:
            raise errors[0]
        return outcomes[0]

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Response:
        return self.request("PATCH", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> Response:
        return self.request("OPTIONS", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Response:
        return self.request("HEAD", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Response:
        return self.request("DELETE", url, **kwargs)


def _inside_async_context() -> bool:
    try:
        current_async_library()
    except AsyncLibraryNotFoundError:
        return False
    return True
