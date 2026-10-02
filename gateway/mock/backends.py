"""Run only the deterministic OAuth and invoke backends for image qualification.

The gateway itself runs from the built container. Keeping the two mock
dependencies in this host process lets CI exercise the image over HTTP without
putting test helpers or credentials into the runtime image.
"""

from __future__ import annotations

import argparse
import asyncio

import uvicorn

from mock.oauth import create_mock_oauth_app
from mock.stack import MOCK_CLIENT_ID, MOCK_CLIENT_SECRET
from mock.upstream import create_mock_upstream_app


def _port(value: str) -> int:
    port = int(value)
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


async def serve(*, oauth_port: int, upstream_port: int) -> None:
    """Serve both deterministic dependencies until the process is stopped."""
    servers = (
        uvicorn.Server(
            uvicorn.Config(
                create_mock_oauth_app(client_id=MOCK_CLIENT_ID, client_secret=MOCK_CLIENT_SECRET),
                host="127.0.0.1",
                port=oauth_port,
                log_level="warning",
            )
        ),
        uvicorn.Server(
            uvicorn.Config(
                create_mock_upstream_app(),
                host="127.0.0.1",
                port=upstream_port,
                log_level="warning",
            )
        ),
    )
    await asyncio.gather(*(server.serve() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oauth-port", required=True, type=_port)
    parser.add_argument("--upstream-port", required=True, type=_port)
    args = parser.parse_args()
    asyncio.run(serve(oauth_port=args.oauth_port, upstream_port=args.upstream_port))


if __name__ == "__main__":
    main()
