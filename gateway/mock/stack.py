"""Wires the mock OAuth server, the mock fictional upstream, and the real
gateway app together into three uvicorn servers running side by side -- the
development quick-start an agency runs first, with no real agency
credentials required, and the upstream/OAuth backing Task 13's conformance
kit runs against.

This file is not part of an installed package during development: the
``gateway/src`` directory is inserted onto ``sys.path`` (relative to this
file, so it works from any working directory) before anything under
``elspeth_llm_gateway`` is imported.
"""

import sys
from pathlib import Path

_GATEWAY_ROOT = Path(__file__).resolve().parent.parent
_GATEWAY_SRC = _GATEWAY_ROOT / "src"
if str(_GATEWAY_SRC) not in sys.path:
    sys.path.insert(0, str(_GATEWAY_SRC))

import asyncio  # noqa: E402

import uvicorn  # noqa: E402
from elspeth_llm_gateway.core.app import create_app  # noqa: E402
from elspeth_llm_gateway.core.config import load_config  # noqa: E402

from mock.oauth import create_mock_oauth_app  # noqa: E402
from mock.upstream import create_mock_upstream_app  # noqa: E402

# Documented mock secrets: DEVELOPMENT-ONLY fixed values, never used against
# a real credential store or a real agency. Fixed (not random) on purpose --
# determinism is the point of this whole stack.
MOCK_CLIENT_ID = "mock-client"
MOCK_CLIENT_SECRET = "mock-secret-0123456789abcdef0123456789abcdef"  # secret-scan: allow-this-line
MOCK_INBOUND_BEARER = "mock-inbound-bearer-0123456789abcdef012345"  # secret-scan: allow-this-line

_DEFAULT_GATEWAY_PORT = 8787
_DEFAULT_OAUTH_PORT = 8788
_DEFAULT_UPSTREAM_PORT = 8789
_MODEL_ALIAS = "mock-model"
_MODEL_TARGET = "mock-target"


def _build_gateway_env(*, oauth_port: int, upstream_port: int) -> dict[str, str]:
    return {
        "ELSPETH_LLM_GATEWAY_INBOUND_BEARER": MOCK_INBOUND_BEARER,
        "ELSPETH_LLM_GATEWAY_ADAPTER": "reference_v1_invoke",
        "ELSPETH_LLM_GATEWAY_UPSTREAM_ORIGIN": f"http://127.0.0.1:{upstream_port}",
        "ELSPETH_LLM_GATEWAY_OAUTH_TOKEN_URL": f"http://127.0.0.1:{oauth_port}/token",
        "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_ID": MOCK_CLIENT_ID,
        "ELSPETH_LLM_GATEWAY_OAUTH_CLIENT_SECRET": MOCK_CLIENT_SECRET,
        "ELSPETH_LLM_GATEWAY_OAUTH_AUTH_METHOD": "client_secret_basic",
        "ELSPETH_LLM_GATEWAY_MAX_MESSAGES": "50",
        "ELSPETH_LLM_GATEWAY_MAX_TOOLS": "10",
        "ELSPETH_LLM_GATEWAY_MAX_STRING_CHARS": "10000",
        "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_BYTES": "65536",
        "ELSPETH_LLM_GATEWAY_MAX_SCHEMA_DEPTH": "10",
        "ELSPETH_LLM_GATEWAY_MODEL_MAPPINGS": f'{{"{_MODEL_ALIAS}": {{"target": "{_MODEL_TARGET}"}}}}',
    }


def _curl_example(gateway_port: int) -> str:
    return (
        "Mock stack ready. Try:\n\n"
        f"curl -s http://127.0.0.1:{gateway_port}/v1/chat/completions \\\n"
        f'  -H "Authorization: Bearer {MOCK_INBOUND_BEARER}" \\\n'
        '  -H "X-ELSPETH-LLM-Gateway-Contract: 1" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        f'  -d \'{{"model": "{_MODEL_ALIAS}", "messages": [{{"role": "user", "content": "hello"}}]}}\'\n'
    )


async def start_local_stack(
    gateway_port: int = _DEFAULT_GATEWAY_PORT,
    oauth_port: int = _DEFAULT_OAUTH_PORT,
    upstream_port: int = _DEFAULT_UPSTREAM_PORT,
) -> None:
    """Run the mock OAuth server, the mock upstream server, and the real gateway together.

    All three run as uvicorn servers on ``127.0.0.1``, wired to each other
    entirely via the gateway's normal ``ELSPETH_LLM_GATEWAY_*`` environment
    contract -- nothing here reaches into gateway internals that a real
    deployment wouldn't also configure through that same environment.
    """
    gateway_env = _build_gateway_env(oauth_port=oauth_port, upstream_port=upstream_port)
    gateway_config = load_config(gateway_env)

    oauth_app = create_mock_oauth_app(client_id=MOCK_CLIENT_ID, client_secret=MOCK_CLIENT_SECRET)
    upstream_app = create_mock_upstream_app()
    gateway_app = create_app(gateway_config)

    servers = [
        uvicorn.Server(uvicorn.Config(oauth_app, host="127.0.0.1", port=oauth_port, log_level="warning")),
        uvicorn.Server(uvicorn.Config(upstream_app, host="127.0.0.1", port=upstream_port, log_level="warning")),
        uvicorn.Server(uvicorn.Config(gateway_app, host="127.0.0.1", port=gateway_port, log_level="info")),
    ]

    print(_curl_example(gateway_port), flush=True)  # noqa: T201 -- ready-to-paste curl example is this entry point's stdout deliverable

    await asyncio.gather(*(server.serve() for server in servers))


if __name__ == "__main__":
    asyncio.run(start_local_stack())
