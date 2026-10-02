# Run the Web Composer locally

This guide starts a local development instance from a source checkout. It uses
local authentication and SQLite stores. For deployment settings and supported
topologies, use the [deployment platform reference](../reference/deployment-platforms.md).

## Prerequisites

- Python 3.12 or newer and [uv](https://docs.astral.sh/uv/)
- Node.js 24 and npm 11 (the frontend's pinned toolchain)
- OpenSSL to generate local secret keys
- Credentials for the Composer model provider you configure

From the repository root, install the web dependencies and build the frontend:

```bash
uv sync --frozen --extra webui
npm --prefix src/elspeth/web/frontend ci
npm --prefix src/elspeth/web/frontend run build
```

## Configure and start

In the same shell that will start the server, set local-only secrets and the
Composer limits required by the web settings contract:

```bash
mkdir -p data/blobs data/outputs
export ELSPETH_WEB__SECRET_KEY="$(openssl rand -hex 32)"
export ELSPETH_WEB__SHAREABLE_LINK_SIGNING_KEY="$(openssl rand -base64 32)"
export ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS=15
export ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS=10
export ELSPETH_WEB__COMPOSER_TIMEOUT_SECONDS=180.0
export ELSPETH_WEB__COMPOSER_RATE_LIMIT_PER_MINUTE=60
```

Configure the primary Composer model and the independent advisor model before
trying LLM authoring. The defaults use separate provider credentials. The
[web LLM configuration reference](../reference/environment-variables.md#web-llm-configuration)
explains both models, provider keys, and operator LLM profiles.

Create a local user. The command prompts for a password and confirmation; use
a unique development password rather than putting one in a shell command.

```bash
uv run elspeth composer users add demo \
  --display-name "Demo User" \
  --email demo@example.com
uv run elspeth web --host 127.0.0.1 --port 8451
```

Open <http://127.0.0.1:8451> and sign in with the local credentials you just
created. The local user database defaults to `data/auth.db`, session state to
`data/sessions.db`, and run evidence to `data/runs/audit.db`.

## Frontend development

For live frontend changes, keep the API server running and start Vite in a
second shell:

```bash
npm --prefix src/elspeth/web/frontend run dev
```

Open <http://localhost:5173>. Vite proxies `/api` and `/ws` to the backend on
port 8451.

## Next steps

- [Composer user guide](../release/composer-guide.md)
- [Identity provider setup](identity-providers.md)
- [Web configuration](../reference/environment-variables.md#web-deployment-variables)
- [Architecture and data stores](../../ARCHITECTURE.md)
