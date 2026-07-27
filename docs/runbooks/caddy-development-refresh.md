# Runbook: Refresh the Caddy development install

Use this runbook to rebuild the frontend and restart the repository-specific
source-checkout service behind Caddy. It applies to the development assets
`deploy/elspeth-web.service` and `deploy/Caddyfile`; it is not the portable
production installation.

For a new production host, use
[Native Linux and Azure Ubuntu VM deployment](ansible-ubuntu-deployment.md).

## Prerequisites

- The checkout path, service user, environment file, Unix socket, and Caddy
  routes in the installed unit already match this host.
- Python 3.12 or newer and `uv` are installed.
- Node.js 24 and npm 11 are installed.
- `elspeth-web.service` and Caddy are already installed and enabled.
- The untracked environment file referenced by the service contains the
  required web secrets and provider configuration. Do not print or copy it
  into the deployment log.

Run from the repository root:

```bash
test -f pyproject.toml
test -f src/elspeth/web/frontend/package-lock.json
node --version  # must report v24.x
npm --version   # must report 11.x
sudo systemctl is-active --quiet caddy
```

Stop if the toolchain or existing proxy is not the intended one. In particular,
if `node --version` reports v20 or another unsupported major, use the team's
approved version manager to install and select the exact version in
`.node-version`. Do not continue after an npm `EBADENGINE` warning.

## 1. Synchronize the checkout environment

Do not inherit another worktree's virtual environment:

```bash
unset VIRTUAL_ENV
uv sync --frozen --all-extras
```

`--frozen` makes the lockfile authoritative. A dependency change requires a
separate, reviewed lockfile update; it is not part of an ordinary refresh.

## 2. Rebuild the frontend

Install the exact locked frontend dependency tree before every release or
checkout change. Reusing stale `node_modules` can run a different test/build
tool version from the lockfile.

```bash
npm --prefix src/elspeth/web/frontend ci
npm --prefix src/elspeth/web/frontend run build

FRONTEND_DIST=src/elspeth/web/frontend/dist
test -r "$FRONTEND_DIST/index.html"
test -z "$(find "$FRONTEND_DIST" -type d ! -perm -005 -print -quit)"
test -z "$(find "$FRONTEND_DIST" -type f ! -perm -004 -print -quit)"
```

The directory and file checks prevent a root-only generated asset from leaving
the backend healthy while the SPA returns an error.

## 3. Restart the backend

The tracked development unit is host-specific. Compare it with the installed
unit before restart:

```bash
if ! sudo cmp -s \
  deploy/elspeth-web.service \
  /etc/systemd/system/elspeth-web.service; then
  printf '%s\n' \
    'installed elspeth-web.service differs; review and install the intended unit first' >&2
  exit 1
fi

sudo systemctl restart elspeth-web.service
sudo systemctl is-active --quiet elspeth-web.service
```

Do not start a second `uvicorn` or `elspeth web` process beside the unit. The
service owns `/run/elspeth/uvicorn.sock`, and Caddy proxies to that socket.

## 4. Verify the backend and proxy

Probe the Unix socket first so an application failure is distinguishable from
a Caddy/TLS failure:

```bash
curl --fail --silent --show-error \
  --unix-socket /run/elspeth/uvicorn.sock \
  http://localhost/api/health
curl --fail --silent --show-error \
  --unix-socket /run/elspeth/uvicorn.sock \
  http://localhost/api/ready
curl --fail --silent --show-error \
  --unix-socket /run/elspeth/uvicorn.sock \
  http://localhost/ >/dev/null
```

Then validate the installed Caddy configuration and probe the operator-owned
HTTPS origin:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl is-active --quiet caddy

: "${ELSPETH_PUBLIC_ORIGIN:?set the exact HTTPS origin, for example https://elspeth.example.com}"
curl --fail --silent --show-error \
  "$ELSPETH_PUBLIC_ORIGIN/api/health"
curl --fail --silent --show-error \
  "$ELSPETH_PUBLIC_ORIGIN/api/ready"
curl --fail --silent --show-error \
  "$ELSPETH_PUBLIC_ORIGIN/" >/dev/null
```

Success requires:

- `elspeth-web.service` and Caddy are active;
- both direct-socket probes return HTTP 200;
- both public probes return HTTP 200;
- the SPA root is readable through both paths; and
- a browser reload shows no new console or failed-asset errors.

## Troubleshooting

### Backend is active but the SPA fails

Re-run the frontend readability checks and inspect only recent unit logs:

```bash
sudo journalctl -u elspeth-web.service --since=-10m --no-pager
```

If `/api/health` succeeds but `/` fails over the Unix socket, diagnose the
frontend bundle or served checkout before changing Caddy.

### Unix-socket probes pass but HTTPS fails

Inspect Caddy status and recent logs. Confirm the installed configuration points
to `/run/elspeth/uvicorn.sock`, its certificate files exist, and the service
user can traverse the socket directory. Do not weaken socket or data-directory
permissions to bypass a path mismatch.

### Readiness is not ready

`/api/health` is liveness; `/api/ready` checks dependencies. Keep the public
origin out of service and inspect the static readiness result plus bounded
backend logs. Restarting the same process does not repair an unavailable
database or unwritable data path.

## Rollback

If the refresh came from a source change and the current schema remains
compatible:

1. select the previous reviewed Git ref;
2. run `uv sync --frozen --all-extras`;
3. run frontend `npm ci` and `npm run build`;
4. restart the same systemd unit; and
5. repeat every direct and public verification above.

If the session or Landscape schema changed incompatibly, keep the public route
drained and repair forward or restore the coordinated database and payload
recovery point. Do not start older code against newer incompatible state.
