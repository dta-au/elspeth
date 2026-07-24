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

For an Azure production deployment, use external Azure Database for
PostgreSQL. SQLite is acceptable only when the Azure VM is explicitly the one
persistent host and operators back up its local databases with the payload
store. Azure Container Apps remains unsupported until the cross-instance
fencing work in `elspeth-b5d7aa5655` lands; the runtime target value is
reserved, not an alternative procedure in this runbook.

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

## Release and integrity gate

The schema-incompatible 0.7.2 upgrade from 0.7.1 is not an in-place session
database migration. Before a direct 0.7.1→0.7.2 upgrade, archive required
evidence, drain and stop the old service, recreate the session database at the
new epoch, and repair forward. Do not start 0.7.1 against the recreated 0.7.2
session database.

Run the supported trust-tier gate from the exact reviewed source checkout:

`elspeth-lints check --rules trust_tier.tier_model --root src/elspeth --allowlist-dir config/cicd/enforce_tier_model`

Supply `ELSPETH_JUDGE_METADATA_HMAC_KEY` from the operator's protected secret
store when the gate needs signed judge metadata. Never print or retain the key
in the deployment log. Stop the deployment if this gate or the release test
suite fails.

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
sudo -u elspeth uv sync --frozen --extra webui --extra azure --extra llm --extra postgres
```

Record `git -C /opt/elspeth rev-parse HEAD` in the deployment log. Do not
deploy a moving branch.

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
4. **Install the reviewed release.** Check out the immutable tag or commit,
   run the frozen `uv sync` command above, reinstall the tracked service unit,
   and run `systemctl daemon-reload`.
5. **Validate while drained.** Run `elspeth doctor deployment` with the runtime
   environment. Do not use `--init-schema` for an ordinary upgrade.
6. **Start one process and verify locally.**

   ```bash
   sudo systemctl start elspeth-web.service
   sudo systemctl is-active --quiet elspeth-web.service
   curl -fsS http://127.0.0.1:8451/api/health
   curl -fsS http://127.0.0.1:8451/api/ready
   ```

7. **Restore public service.** Only after doctor and `/api/ready` succeed,
   re-enable the reverse-proxy route or Front Door origin. Verify the public
   `/api/ready` endpoint and one representative authenticated workflow.

If any validation fails, leave the origin drained. Do not restore public
service merely because the process started.

## Rollback

Rollback is also stop-before-start:

1. Drain the origin.
2. Stop the service and prove zero ELSPETH web processes.
3. Check whether the previous release supports the current database schema.
   If it does not, keep the service drained and repair forward.
4. Restore the coordinated database and payload recovery point when required.
5. Check out the previous immutable release, run the frozen install, and run
   `elspeth doctor deployment`.
6. Start one process, verify local health and readiness, then restore the
   origin.

Never run old and new releases together against the same state.

## Troubleshooting

### Doctor reports a schema mismatch

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

- [ ] Immutable release commit recorded.
- [ ] Exactly one `elspeth web` process runs with `WEB_CONCURRENCY=1`.
- [ ] `elspeth doctor deployment` passes with the runtime principal.
- [ ] Local `/api/health` and `/api/ready` return HTTP 200.
- [ ] Database and payload backups share a recovery-point record.
- [ ] Public routing was restored only after readiness passed.
- [ ] Deployment log records the availability interruption.

## See also

- [Deployment platforms](../reference/deployment-platforms.md)
- [Environment variables](../reference/environment-variables.md#web-deployment-variables)
- [Docker deployment](../guides/docker.md)
- [AWS ECS deployment](aws-ecs-deployment.md)
- [Configure Azure Key Vault](configure-keyvault-secrets.md)
