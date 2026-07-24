# Runbook: Native Linux and Azure Ubuntu VM deployment

Deploy one ELSPETH web process to Ubuntu 24.04 or 22.04 with the portable
systemd bundle. Use the same procedure for exactly one Azure Ubuntu VM,
optionally behind Azure Front Door. The commands are suitable for translation
into idempotent Ansible tasks, but this repository does not ship an Ansible
role.

This path deliberately performs a stop-before-start replacement. It trades a
short availability interruption for the guarantee that two ELSPETH web
processes never run concurrently.

## Supported shape

- one persistent Ubuntu host;
- `deploy/linux-systemd/elspeth-web.service`;
- `ELSPETH_WEB__DEPLOYMENT_TARGET=linux-systemd`;
- `WEB_CONCURRENCY=1` and one web process;
- `/var/lib/elspeth` on persistent host storage; and
- either SQLite on that single host or distinct external PostgreSQL databases.

Azure production requires external Azure Database for PostgreSQL. Azure VM
SQLite is supported only for explicitly non-production use on one persistent
host, and operators must back up its local databases with the payload store.
Azure Container Apps remains unsupported until the cross-instance fencing work
in `elspeth-b5d7aa5655` lands; the runtime target value is reserved, not an
alternative procedure in this runbook.

The ELSPETH release image contains PostgreSQL clients, not a PostgreSQL server.
Native installs include the same two drivers: `postgresql+psycopg://` selects
psycopg v3 and `postgresql+psycopg2://` selects psycopg2. PostgreSQL itself is
an operator-owned external service on this path.

Payload persistence under `/var/lib/elspeth/payloads` is separate from database
persistence. Preserve both across upgrades and restore them as one consistent
recovery point.

## Prerequisites

- Root or sudo access to the VM.
- A dedicated `elspeth` system user and group.
- `git`, `curl`, and `uv` installed from reviewed sources.
- Node.js 24 with npm 11 installed from a reviewed source. If your organization
  approves NodeSource, use the NodeSource Node 24.x apt repository
  (`https://deb.nodesource.com/node_24.x`) and pin its signing key.
- An immutable, release-specific Git tag or commit. If you build an image for
  another environment, use an immutable, release-specific image tag or digest.
- TLS termination at Caddy, another reverse proxy, or Azure Front Door.
- For external state, two existing PostgreSQL databases and an approved
  schema-owner credential for first initialization. Use a separate runtime
  principal after initialization.
- Backups for the database and payload store.

For Azure Front Door, configure the VM as one origin and choose an origin host
name whose certificate the VM presents. Restrict direct origin access with
network controls and validate the expected Front Door identifier at the
origin. Do not add a second VM for availability; ELSPETH does not yet support
that topology.

## Release compatibility

The schema-incompatible 0.7.2 upgrade from 0.7.1 is not an in-place session
database migration. Before a direct 0.7.1→0.7.2 upgrade, archive required
evidence, drain and stop the old service, recreate the session database at the
new epoch, and repair forward. Do not start 0.7.1 against the recreated 0.7.2
session database.

## Install the immutable release

The examples use `/opt/elspeth` for the checked-out release:

```bash
export ELSPETH_RELEASE_REF=v0.7.2
sudo install -d -o elspeth -g elspeth -m 0750 /opt/elspeth
if [ ! -d /opt/elspeth/.git ]; then
  sudo -u elspeth git clone --filter=blob:none \
    https://github.com/johnm-dta/elspeth.git /opt/elspeth
fi
sudo -u elspeth git -C /opt/elspeth fetch --tags --force
sudo -u elspeth git -C /opt/elspeth checkout --detach "$ELSPETH_RELEASE_REF"
cd /opt/elspeth
sudo --preserve-env=ELSPETH_JUDGE_METADATA_HMAC_KEY -u elspeth \
  uv run --frozen --extra dev elspeth-lints check --rules trust_tier.tier_model \
  --root src/elspeth --allowlist-dir config/cicd/enforce_tier_model
sudo -u elspeth uv sync --frozen --extra webui --extra azure --extra llm --extra postgres
node --version  # must report v24.x
npm --version   # must report 11.x
sudo -u elspeth npm --prefix src/elspeth/web/frontend ci
sudo -u elspeth npm --prefix src/elspeth/web/frontend run build
```

The supported gate payload is
`elspeth-lints check --rules trust_tier.tier_model --root src/elspeth --allowlist-dir config/cicd/enforce_tier_model`;
invoke it through the `uv run --frozen --extra dev` command above so its
declared dependency is present.

Record `git -C /opt/elspeth rev-parse HEAD` in the deployment log. Do not
deploy a moving branch. Supply `ELSPETH_JUDGE_METADATA_HMAC_KEY` from the
operator's protected secret store before the gate; never print or retain it in
the deployment log. The final exact `uv sync` removes the development-only
gate dependencies and leaves the production environment with only the four
listed runtime extras. Stop if the gate or sync fails.

Install the portable service and its environment file:

```bash
sudo install -D -o root -g root -m 0644 \
  deploy/linux-systemd/elspeth-web.service \
  /etc/systemd/system/elspeth-web.service
sudo install -d -o root -g elspeth -m 0750 /etc/elspeth
sudo install -o root -g elspeth -m 0640 \
  deploy/linux-systemd/elspeth-web.env.example \
  /etc/elspeth/elspeth-web.env
sudo systemctl daemon-reload
```

Edit `/etc/elspeth/elspeth-web.env` locally. Generate real application secrets;
never copy a credential into a tracked file.

### SQLite single-host configuration

Keep these values from the example:

```dotenv
ELSPETH_WEB__DEPLOYMENT_TARGET=linux-systemd
ELSPETH_WEB__DEPLOYMENT_STATE_MODE=sqlite-single
ELSPETH_WEB__DATA_DIR=/var/lib/elspeth/data
ELSPETH_WEB__PAYLOAD_STORE_PATH=/var/lib/elspeth/payloads
```

Do not place the SQLite files on ephemeral OS storage. Snapshot them only while
the service is stopped, and coordinate that snapshot with the payload backup.

### External PostgreSQL configuration

Use distinct session and Landscape databases:

```dotenv
ELSPETH_WEB__DEPLOYMENT_TARGET=linux-systemd
ELSPETH_WEB__DEPLOYMENT_STATE_MODE=external-postgresql
ELSPETH_WEB__SESSION_DB_URL=postgresql+psycopg://elspeth@postgresql.example.invalid:5432/elspeth_sessions
ELSPETH_WEB__LANDSCAPE_URL=postgresql+psycopg://elspeth@postgresql.example.invalid:5432/elspeth_landscape
ELSPETH_WEB__DATA_DIR=/var/lib/elspeth/data
ELSPETH_WEB__PAYLOAD_STORE_PATH=/var/lib/elspeth/payloads
```

The psycopg2 spelling is also supported:
`postgresql+psycopg2://elspeth@postgresql.example.invalid:5432/elspeth_sessions`.
Store credentials in an operator secret system and render them only on the VM.
For Azure production, make these URLs point to Azure Database for PostgreSQL.

## Initialize external schemas once

Web startup never creates schemas. Before the first start, temporarily supply
the database-operator-approved schema-owner URLs and run:

```bash
sudo -u elspeth env -i PATH=/opt/elspeth/.venv/bin:/usr/bin:/bin \
  /bin/bash -c 'set -a; . /etc/elspeth/elspeth-web.env; exec elspeth doctor deployment --init-schema'
```

Replace the schema-owner URLs with the least-privileged runtime URLs
immediately after success. Then run the read-only form:

```bash
sudo -u elspeth env -i PATH=/opt/elspeth/.venv/bin:/usr/bin:/bin \
  /bin/bash -c 'set -a; . /etc/elspeth/elspeth-web.env; exec elspeth doctor deployment'
```

Stop if either command fails. Doctor validates both databases and the
persistent directories without starting the web listener.

## First start

```bash
sudo systemctl enable --now elspeth-web.service
sudo systemctl is-active --quiet elspeth-web.service
curl -fsS http://127.0.0.1:8451/api/health
curl -fsS http://127.0.0.1:8451/api/ready
```

Success requires an active unit and HTTP 200 from both endpoints. Check logs
without printing the environment file:

```bash
sudo journalctl -u elspeth-web.service --since=-10m --no-pager
```

## Stop-before-start upgrade

Every upgrade has a deliberate availability interruption. Do not start the
replacement until the old process is gone.

1. **Drain the public origin.** If Azure Front Door is present, remove or
   disable the VM origin in its route, wait for existing requests to drain,
   and confirm the public endpoint no longer reaches this VM. For a direct VM,
   enable the reverse proxy's maintenance response and drain connections.
2. **Stop and prove zero processes.**

   ```bash
   sudo systemctl stop elspeth-web.service
   ! sudo systemctl is-active --quiet elspeth-web.service
   ! pgrep -u elspeth -f '/opt/elspeth/.venv/bin/elspeth web'
   ```

3. **Back up state.** Snapshot the two external databases or the stopped SQLite
   files, then back up `/var/lib/elspeth/data` and
   `/var/lib/elspeth/payloads`. Record one recovery-point identifier for both.
4. **Install the reviewed release.** Set the approved immutable ref, then
   check it out and rebuild both the Python environment and ignored frontend
   output:

   ```bash
   export ELSPETH_RELEASE_REF=v0.7.2
   sudo -u elspeth git -C /opt/elspeth fetch --tags --force
   sudo -u elspeth git -C /opt/elspeth checkout --detach "$ELSPETH_RELEASE_REF"
   cd /opt/elspeth
   sudo -u elspeth uv sync --frozen --extra webui --extra azure --extra llm --extra postgres
   sudo -u elspeth node --version  # must report v24.x
   sudo -u elspeth npm --version   # must report 11.x
   sudo -u elspeth npm --prefix src/elspeth/web/frontend ci
   sudo -u elspeth npm --prefix src/elspeth/web/frontend run build
   sudo install -D -o root -g root -m 0644 \
     deploy/linux-systemd/elspeth-web.service \
     /etc/systemd/system/elspeth-web.service
   sudo systemctl daemon-reload
   ```

   Stop if any command fails. Git does not replace the ignored
   `src/elspeth/web/frontend/dist` tree, so every release checkout must rebuild
   it before the service starts.

Choose exactly one validation branch from the configured state mode.

### Upgrade validation: external PostgreSQL

Run the read-only external-state doctor. Do not initialize schemas during an
ordinary upgrade.

```bash
sudo -u elspeth env -i PATH=/opt/elspeth/.venv/bin:/usr/bin:/bin \
  /bin/bash -c 'set -a; . /etc/elspeth/elspeth-web.env; exec elspeth doctor deployment'
sudo systemctl start elspeth-web.service
sudo systemctl is-active --quiet elspeth-web.service
curl -fsS http://127.0.0.1:8451/api/health
curl -fsS http://127.0.0.1:8451/api/ready
```

### Upgrade validation: SQLite

The provider-neutral external-state doctor rejects SQLite, so skip it. Verify
the stopped databases and payload path directly, then start the replacement
and require readiness:

```bash
for path in \
  /var/lib/elspeth/data/sessions.db \
  /var/lib/elspeth/data/runs/audit.db \
  /var/lib/elspeth/payloads; do
  sudo -u elspeth test -r "$path"
  sudo -u elspeth test -w "$path"
  test "$(sudo stat -c '%U:%G' "$path")" = "elspeth:elspeth"
done
sudo systemctl start elspeth-web.service
sudo systemctl is-active --quiet elspeth-web.service
curl -fsS http://127.0.0.1:8451/api/health
curl -fsS http://127.0.0.1:8451/api/ready
```

### Restore public service

Only after the selected validation branch and `/api/ready` succeed, re-enable
the reverse-proxy route or Front Door origin. Verify the public `/api/ready`
endpoint and one representative authenticated workflow.

If any validation fails, leave the origin drained. Do not restore public
service merely because the process started.

## Rollback

Rollback is also stop-before-start:

1. Drain the origin.
2. Stop the service and prove zero ELSPETH web processes.
3. Check whether the previous release supports the current database schema.
   If it does not, keep the service drained and repair forward.
4. Restore the coordinated database and payload recovery point when required.
5. Check out and build the previous immutable release. Replace the example ref
   with the approved previous tag or commit:

   ```bash
   export ELSPETH_ROLLBACK_REF=v0.7.1
   sudo -u elspeth git -C /opt/elspeth fetch --tags --force
   sudo -u elspeth git -C /opt/elspeth checkout --detach "$ELSPETH_ROLLBACK_REF"
   cd /opt/elspeth
   sudo -u elspeth uv sync --frozen --extra webui --extra azure --extra llm --extra postgres
   sudo -u elspeth node --version  # must report v24.x
   sudo -u elspeth npm --version   # must report 11.x
   sudo -u elspeth npm --prefix src/elspeth/web/frontend ci
   sudo -u elspeth npm --prefix src/elspeth/web/frontend run build
   sudo install -D -o root -g root -m 0644 \
     deploy/linux-systemd/elspeth-web.service \
     /etc/systemd/system/elspeth-web.service
   sudo systemctl daemon-reload
   ```

   Stop if the previous release's frozen Python environment or frontend build
   fails.

Choose the branch matching the restored state mode.

### Rollback validation: external PostgreSQL

```bash
sudo -u elspeth env -i PATH=/opt/elspeth/.venv/bin:/usr/bin:/bin \
  /bin/bash -c 'set -a; . /etc/elspeth/elspeth-web.env; exec elspeth doctor deployment'
sudo systemctl start elspeth-web.service
sudo systemctl is-active --quiet elspeth-web.service
curl -fsS http://127.0.0.1:8451/api/health
curl -fsS http://127.0.0.1:8451/api/ready
```

### Rollback validation: SQLite

```bash
for path in \
  /var/lib/elspeth/data/sessions.db \
  /var/lib/elspeth/data/runs/audit.db \
  /var/lib/elspeth/payloads; do
  sudo -u elspeth test -r "$path"
  sudo -u elspeth test -w "$path"
  test "$(sudo stat -c '%U:%G' "$path")" = "elspeth:elspeth"
done
sudo systemctl start elspeth-web.service
sudo systemctl is-active --quiet elspeth-web.service
curl -fsS http://127.0.0.1:8451/api/health
curl -fsS http://127.0.0.1:8451/api/ready
```

### Restore the rollback

Restore the origin only after the selected branch succeeds. Verify public
readiness and one representative authenticated workflow.

Never run old and new releases together against the same state.

## Troubleshooting

### External PostgreSQL doctor reports a schema mismatch

Keep the service stopped. Confirm the candidate release and database target.
Use `--init-schema` only for a new empty database with the approved schema-owner
credential; it is not an in-place migration command.

### Service cannot write payloads

Check the systemd-managed paths and ownership:

```bash
sudo namei -l /var/lib/elspeth/data/blobs
sudo namei -l /var/lib/elspeth/payloads
sudo -u elspeth test -w /var/lib/elspeth/data/blobs
sudo -u elspeth test -w /var/lib/elspeth/payloads
```

The service unit creates these paths for the `elspeth` account with mode 0700.
Do not weaken permissions to make a displaced or ephemeral mount appear valid.

### Front Door is healthy but the application is unavailable

Probe `http://127.0.0.1:8451/api/ready` on the VM first. Then verify the origin
host name, TLS certificate, route association, health-probe path, and origin
network restrictions. Restore Front Door only after the local readiness gate
passes.

## Completion checklist

### External PostgreSQL completion

```bash
sudo -u elspeth env -i PATH=/opt/elspeth/.venv/bin:/usr/bin:/bin \
  /bin/bash -c 'set -a; . /etc/elspeth/elspeth-web.env; exec elspeth doctor deployment'
sudo systemctl is-active --quiet elspeth-web.service
curl -fsS http://127.0.0.1:8451/api/health
curl -fsS http://127.0.0.1:8451/api/ready
```

- [ ] Immutable release commit recorded.
- [ ] Exactly one `elspeth web` process runs with `WEB_CONCURRENCY=1`.
- [ ] `elspeth doctor deployment` passes with the runtime principal.
- [ ] Local `/api/health` and `/api/ready` return HTTP 200.
- [ ] Database and payload backups share a recovery-point record.
- [ ] Public routing was restored only after readiness passed.
- [ ] Deployment log records the availability interruption.

### SQLite completion

```bash
for path in \
  /var/lib/elspeth/data/sessions.db \
  /var/lib/elspeth/data/runs/audit.db \
  /var/lib/elspeth/payloads; do
  sudo -u elspeth test -r "$path"
  sudo -u elspeth test -w "$path"
  test "$(sudo stat -c '%U:%G' "$path")" = "elspeth:elspeth"
done
sudo systemctl is-active --quiet elspeth-web.service
curl -fsS http://127.0.0.1:8451/api/health
curl -fsS http://127.0.0.1:8451/api/ready
```

- [ ] Immutable release commit recorded.
- [ ] Exactly one `elspeth web` process runs with `WEB_CONCURRENCY=1`.
- [ ] `/var/lib/elspeth/data/sessions.db` and
  `/var/lib/elspeth/data/runs/audit.db` are readable, writable, and owned by
  `elspeth:elspeth`.
- [ ] `/var/lib/elspeth/payloads` is readable, writable, and owned by
  `elspeth:elspeth`.
- [ ] Local `/api/health` and `/api/ready` return HTTP 200 after the service
  starts.
- [ ] SQLite and payload backups share a recovery-point record.
- [ ] Public routing was restored only after readiness passed.
- [ ] Deployment log records the availability interruption.

## See also

- [Deployment platforms](../reference/deployment-platforms.md)
- [Environment variables](../reference/environment-variables.md#web-deployment-variables)
- [Docker deployment](../guides/docker.md)
- [AWS ECS deployment](aws-ecs-deployment.md)
- [Configure Azure Key Vault](configure-keyvault-secrets.md)
