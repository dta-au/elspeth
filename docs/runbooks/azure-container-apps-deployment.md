# Runbook: Full disposable Azure Container Apps acceptance environment

Deploy ELSPETH web to Azure Container Apps at replica count > 1 with Azure
Database for PostgreSQL Flexible Server, an NFS 4.1 Azure Files share, Key
Vault secret references, a user-assigned managed identity, and Log Analytics;
exercise the four multi-replica probes; collect sanitized evidence; and destroy
the resource group. This runbook is the Azure equivalent of
[the AWS ECS disposable acceptance program](aws-ecs-deployment.md) in
**evidence**, not in code: where the Azure control plane already produces a
fact, the receipt records a sanitized projection of it.

> **Status.** This runbook is a skeleton prepared by Phase 6b before the first
> live run. Every step marked **LIVE** is filled in by the operator-run
> acceptance (6b-7) and its receipt. Until the sanitized receipt exists at
> `docs/operator/evidence/azure-container-apps/0.8.0.json`, this runbook is
> not a support claim. The public support statement in
> [Deployment Platforms](../reference/deployment-platforms.md) flips in the
> same commit as that receipt.

> **Scope.** The tracked Bicep bundle is
> [`deploy/azure-container-apps/`](../../deploy/azure-container-apps/README.md)
> (environment, workload, jobs, parameter examples, KQL evidence queries and
> the thin `scripts/acceptance.sh` driver). Every platform literal below is
> measured in the
> [platform facts](../plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md);
> cite that file by section, never restate a fact from memory. For an ordinary
> image rollout to an existing app use
> [Azure Container Apps existing-service redeploy](azure-container-apps-existing-service-redeploy.md);
> for a first installation use
> [Azure Container Apps cold install](azure-container-apps-cold-install.md).

---

## Symptoms

Use this runbook when you need to:

- provision, exercise at replicas > 1, and destroy the disposable Azure
  Container Apps acceptance environment for one release candidate;
- prove schema, persistence, identity-based blob access, replica fencing,
  run-start coordination, lease takeover and cross-replica progress before
  admitting traffic; or
- produce the sanitized receipt that flips the Azure Container Apps support
  claim.

Do not use it to publish a durable image, to operate a long-lived
environment, to automate a destructive database reset, or to infer that Log
Analytics evidence replaces the Landscape audit record.

---

## Contract summary

- Landscape is the permanent source of truth for lineage, replay and run
  decisions. Log Analytics is best-effort operational telemetry: a record
  there never proves an audit write.
- **Storage contract, stated once.** Both databases live on Azure Database for
  PostgreSQL Flexible Server: `elspeth_sessions` and `elspeth_landscape`,
  statically distinct, runtime roles without DDL. `data/`, `data/blobs` and
  `payloads/` live on one **NFS 4.1** Azure Files share mounted read-write at
  `/mnt/elspeth` on every replica and every Job. SMB Azure Files is not
  supported for this target. There is no SQLite mode at replicas > 1:
  `ELSPETH_WEB__DEPLOYMENT_TARGET=azure-container-apps` refuses
  `sqlite-single` at configuration time. **Azure Files carries no database.**
- `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS` is a required bundle
  parameter with no default. The Container Apps ingress request timeout is a
  fixed 240 seconds (facts §2.2), so the value is at most 240 minus any hop in
  front of the ingress; the example parameter file uses 210.
- The image is referenced by **digest only**, published to the registry as a
  digest-preserving copy of the GitHub Container Registry image (facts §6).
- One user-assigned managed identity carries `AcrPull`, `Key Vault Secrets
  User` and, for the acceptance blob container only, `Storage Blob Data
  Contributor`. PostgreSQL authentication is a role password held in Key
  Vault; Entra token authentication is excluded on the record (plan D4).
- Every response carries `X-Elspeth-Instance` and `/api/system/status`
  reports `instance_id`, `CONTAINER_APP_REVISION` and
  `CONTAINER_APP_REPLICA_NAME` (facts §2.1) once 6b-3 lands; the probe driver
  refuses to score a trial until it has seen two distinct values.
- No credential enters a receipt. Receipts record secret **names and
  versions**, never values.

---

## Prerequisites

- An operator-owned, non-production subscription; `az login` with
  `Contributor` on a disposable resource group, `Log Analytics Reader` on the
  workspace and `Reader` on the resource group.
- The existing container registry that `build-push.yaml` publishes to, its
  resource id (for the `AcrPull` assignment) and the candidate digest.
- Azure CLI with the `containerapp` extension, the pinned Bicep CLI
  (facts §1.1), `jq`, `curl`, `psql`, `cosign`, Node 24/npm 11 and Playwright
  Chromium installed from reviewed locks before mutation.
- The epoch-52 image (session epoch 52, Landscape epoch 37) in the registry.
  The epoch literals in this runbook are byte-bound to the live constants by
  `tests/unit/web/test_azure_container_apps_runbook_contract.py`.
- 6b-2's membership writer merged, or P3 is recorded as unreachable rather
  than run.

### Protected command capture

Start every operator shell with strict mode and protected capture. Every
`az`, `psql`, `bicep` and `curl` call in provisioning, acceptance, diagnosis
and cleanup goes through the wrappers below; raw stderr is captured to a
0600 file that is removed on return and is never printed. A non-zero call
emits only a static failure class.

```bash
set -Eeuo pipefail
umask 077
export AZURE_CORE_OUTPUT=json
export AZURE_CORE_ONLY_SHOW_ERRORS=true
export AZURE_CORE_NO_COLOR=true
export ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES=2097152
export ELSPETH_AZ_CALL_CEILING_SECONDS=120
export ELSPETH_AZ_DEPLOY_CEILING_SECONDS=3600
export ELSPETH_AZ_EXEC_CEILING_SECONDS=300
export ELSPETH_PSQL_CALL_CEILING_SECONDS=60
export ELSPETH_BICEP_CALL_CEILING_SECONDS=300
export ELSPETH_HTTP_CALL_CEILING_SECONDS=60

protected_timeout_seconds() {
  local kind="$1" ceiling
  case "$kind" in
    az) ceiling="${ELSPETH_AZ_CALL_CEILING_SECONDS:?set az call ceiling}" ;;
    az-deploy) ceiling="${ELSPETH_AZ_DEPLOY_CEILING_SECONDS:?set az deployment ceiling}" ;;
    az-exec) ceiling="${ELSPETH_AZ_EXEC_CEILING_SECONDS:?set az exec ceiling}" ;;
    psql) ceiling="${ELSPETH_PSQL_CALL_CEILING_SECONDS:?set psql call ceiling}" ;;
    bicep) ceiling="${ELSPETH_BICEP_CALL_CEILING_SECONDS:?set bicep call ceiling}" ;;
    http) ceiling="${ELSPETH_HTTP_CALL_CEILING_SECONDS:?set http call ceiling}" ;;
    *) printf '%s\n' 'command_kind_invalid' >&2; return 1 ;;
  esac
  test "$ceiling" -gt 0 2>/dev/null || {
    printf '%s\n' 'command_timeout_invalid' >&2
    return 1
  }
  printf '%s\n' "$ceiling"
}

protected_capture() {
  local kind="$1" failure_class="$2"
  shift 2
  local seconds stderr_file status
  seconds=$(protected_timeout_seconds "$kind") || return 1
  stderr_file=$(mktemp -p /tmp elspeth-capture.XXXXXX) || return 1
  chmod 600 "$stderr_file" || { rm -f -- "$stderr_file"; return 1; }
  trap 'rm -f -- "$stderr_file"' RETURN
  set +e
  ( ulimit -f 4096; timeout --signal=TERM --kill-after=5s "$seconds" "$@" 2>"$stderr_file" ) \
    | head -c "$ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES"
  status=${PIPESTATUS[0]}
  set -e
  if test "$status" -ne 0; then
    printf '%s\n' "$failure_class" >&2
    return "$status"
  fi
}

az_capture() { protected_capture az az_command_failed az "$@"; }
az_deploy_capture() { protected_capture az-deploy az_deployment_failed az "$@"; }
az_exec_capture() { protected_capture az-exec az_exec_failed az containerapp exec "$@"; }
bicep_capture() { protected_capture bicep bicep_command_failed bicep "$@"; }
curl_capture() { protected_capture http http_request_failed curl --silent --show-error --fail-with-body "$@"; }
psql_capture() { protected_capture psql psql_command_failed psql --no-psqlrc --quiet --tuples-only --no-align --set=ON_ERROR_STOP=1 "$@"; }
```

`psql_capture` never receives a connection URI on its command line. The role
being used is selected by exporting `PGHOST`, `PGPORT=5432`, `PGDATABASE`,
`PGUSER`, `PGPASSWORD`, `PGSSLMODE=verify-full` and `PGSSLROOTCERT=system` in
a subshell; the password is read from Key Vault into the environment and
never echoed. Port 5432 is deliberate: the built-in PgBouncer on 6432 is not
on ELSPETH's path (facts §4.3).

### Inputs

```bash
: "${ACCEPTANCE_RUN_ID:?set a fresh run id; it tags the resource group}"
: "${AZURE_SUBSCRIPTION_ID:?set the non-production subscription id}"
: "${AZURE_LOCATION:?set the region}"
: "${ACR_RESOURCE_ID:?set the existing registry's resource id}"
: "${ACR_LOGIN_SERVER:?set the existing registry's login server}"
: "${CANDIDATE_SHA:?set the exact 40-hex candidate source sha}"
: "${CANDIDATE_IMAGE_DIGEST:?set the sha256 index digest published to GHCR}"
: "${OPERATOR_PUBLIC_IP:?set the operator host public IPv4 for the PostgreSQL firewall rule}"
: "${ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS:?set the transport ceiling; at most 240 on this platform}"
test "$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" -le 240

export RESOURCE_GROUP="elspeth-acc-${ACCEPTANCE_RUN_ID}"
export EVIDENCE_DIR="${HOME}/.local/state/elspeth/azure-container-apps/${ACCEPTANCE_RUN_ID}"
mkdir -p -m 0700 "$EVIDENCE_DIR"
```

Raw Azure output stays under `$EVIDENCE_DIR` (mode 0700) outside the
worktree; only sanitized receipts are committed.

---

## 1. Create the resource group and environment

```bash
az_capture account set --subscription "$AZURE_SUBSCRIPTION_ID"
az_deploy_capture deployment sub what-if \
  --location "$AZURE_LOCATION" \
  --template-file deploy/azure-container-apps/main.bicep \
  --parameters deploy/azure-container-apps/main.acceptance.bicepparam \
  --parameters resourceGroupName="$RESOURCE_GROUP" acceptanceRunId="$ACCEPTANCE_RUN_ID" \
  >"$EVIDENCE_DIR/what-if.json"
az_deploy_capture deployment sub create \
  --name "elspeth-acc-${ACCEPTANCE_RUN_ID}" \
  --location "$AZURE_LOCATION" \
  --template-file deploy/azure-container-apps/main.bicep \
  --parameters deploy/azure-container-apps/main.acceptance.bicepparam \
  --parameters resourceGroupName="$RESOURCE_GROUP" acceptanceRunId="$ACCEPTANCE_RUN_ID" \
  >"$EVIDENCE_DIR/deployment.json"
jq -S '.properties.outputs' "$EVIDENCE_DIR/deployment.json" >"$EVIDENCE_DIR/inventory.json"
sha256sum "$EVIDENCE_DIR/inventory.json"
```

The resource group is tagged `elspeth.acceptance-run-id`. The environment
deployment creates the virtual network, the Container Apps environment with
an NFS storage definition, the Premium FileStorage account with its NFS share
(`rootSquash: NoRootSquash`, encryption in transit off, private endpoint), the
Flexible Server with both databases, two runtime roles and one schema-owner
role, the Key Vault (RBAC, purge protection **off** for the disposable group),
the Log Analytics workspace and the user-assigned identity. The what-if
output replaces the ECS plan review; its SHA-256 is bound into the receipt.

> **LIVE:** record the NFS share root's ownership and mode, the mount options
> the platform applied (`mount | grep /mnt/elspeth` through `az_exec_capture`),
> and the private-endpoint DNS resolution from inside a Job (facts §10 item 3).

## 2. Resolve and copy the image

```bash
az_capture acr login --name "${ACR_LOGIN_SERVER%%.*}"
docker buildx imagetools create \
  --tag "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" \
  "ghcr.io/dta-au/elspeth@${CANDIDATE_IMAGE_DIGEST}"
ACR_DIGEST=$(az_capture acr manifest show-metadata \
  "${ACR_LOGIN_SERVER}/elspeth:sha-${CANDIDATE_SHA}" --query digest --output tsv)
test "$ACR_DIGEST" = "$CANDIDATE_IMAGE_DIGEST"
cosign verify "${ACR_LOGIN_SERVER}/elspeth@${ACR_DIGEST}" \
  --certificate-identity-regexp '^https://github.com/dta-au/elspeth/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  >"$EVIDENCE_DIR/cosign-verify.json"
export CANDIDATE_IMAGE="${ACR_LOGIN_SERVER}/elspeth@${ACR_DIGEST}"
```

Two independent builds never share a digest; only a copy does (facts §6.2).
When `build-push.yaml` already copied the image, `imagetools create` is a
no-op that re-asserts the same digest.

## 3. Run the Jobs in order

Each Job runs the candidate digest with the same NFS mount and identity as
the app. Start it, poll its execution to a terminal state, and require
`Succeeded`; retrieve the doctor's `--json` report from Log Analytics by
execution name.

```bash
run_job_to_completion() {
  local job="$1" execution status
  execution=$(az_capture containerapp job start --name "$job" --resource-group "$RESOURCE_GROUP" \
    --query name --output tsv)
  while :; do
    status=$(az_capture containerapp job execution show --name "$job" --resource-group "$RESOURCE_GROUP" \
      --job-execution-name "$execution" --query properties.status --output tsv)
    case "$status" in
      Succeeded) break ;;
      Failed|Stopped|Degraded) printf '%s\n' "job_execution_${status,,}" >&2; return 1 ;;
      *) sleep 10 ;;
    esac
  done
  printf '%s\n' "$execution"
}

PROVISION_EXECUTION=$(run_job_to_completion provision-storage)
SCHEMA_INIT_EXECUTION=$(run_job_to_completion doctor-schema-init)
RUNTIME_A_EXECUTION=$(run_job_to_completion doctor-runtime-a)
RUNTIME_B_EXECUTION=$(run_job_to_completion doctor-runtime-b)
```

- `provision-storage` runs a digest-pinned root image (the runtime image is
  `USER 1654` and Container Apps offers no `runAsUser`; facts §2.8) and creates
  `/mnt/elspeth/data`, `/mnt/elspeth/data/blobs` and `/mnt/elspeth/payloads`
  owned `1654:1654`, mode `0700`.
- `doctor-schema-init` runs `elspeth doctor deployment --init-schema --json`
  with the schema-owner URLs and initializes both schemas at session epoch 52
  and Landscape epoch 37.
- `doctor-runtime-a` / `doctor-runtime-b` run `elspeth doctor deployment --json`
  with each runtime role's URLs; `session_schema`, `landscape_schema`,
  `session_tls`, `landscape_tls`, `payload_store_writable` and
  `blob_writable` must all be OK. TLS is `verify-full` with
  `sslrootcert=system`: the runtime image's CA store carries both Azure roots
  (facts §4.4).
- `verify-blob-managed-identity` re-runs the two `azure_blob@managed_identity`
  cases inside the environment, the one auth mode whose truth depends on
  where the process runs.

> **LIVE:** the no-schema dry run against the current `release/0.8.0` image
> (expected: both schema checks red, everything else green) proves the wiring
> before the epoch-52 image exists; record its execution names.

## 4. Deploy the production shape and prove the rollout

```bash
az_deploy_capture deployment group create \
  --name "elspeth-workload-${CANDIDATE_SHA:0:12}" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/workload.bicep \
  --parameters deploy/azure-container-apps/workload.production.bicepparam \
  --parameters image="$CANDIDATE_IMAGE" revisionSuffix="${CANDIDATE_SHA:0:12}" \
    composerTransportIdleCeilingSeconds="$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" \
  >"$EVIDENCE_DIR/workload-production.json"
az_capture containerapp revision list --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --query "[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState}" \
  >"$EVIDENCE_DIR/revisions-production.json"
az_capture containerapp replica list --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --revision "elspeth-web--${CANDIDATE_SHA:0:12}" >"$EVIDENCE_DIR/replicas-production.json"
```

Proof of rollout (replaces the ECS `jq` gate): exactly one active revision at
100 % carrying the candidate digest; `replica list` shows `N` running
replicas; `/api/health` and `/api/ready` return 200 through the ingress; and
`/api/system/status` reports `deployment_target: azure-container-apps`, the
`frontend_build` and, after 6b-3, `instance_id`. Production runs
`activeRevisionsMode: Single`, `stickySessions.affinity: sticky` (single
revision mode only; facts §2.3), `minReplicas: 2`, `maxReplicas: 4`,
startup probe `/api/health` 15 s × 10, liveness `/api/health` 30 s × 3,
readiness `/api/ready` 10 s × 3 and `terminationGracePeriodSeconds: 60`.

Advisory-lock classes are internal to the PostgreSQL connections the replicas
hold; nothing in the bundle or the schema names them. The blob
custody lock has its own class (`ELSPETH_BLOB_CUSTODY_LOCK_CLASSID`) so that
a session-operation lease renew on one replica never waits behind another
replica's blob write to the NFS share. No operator action attaches to it.
Replicas running different versions serialise custody on different keys, so
a rollout that overlaps old and new replicas is outside the contract: the
proof of rollout above (exactly one active revision carrying the candidate
digest) is what makes the custody serialisation claim hold.

> **LIVE:** the public-behaviour pass (Playwright tutorial through the
> ingress, a fork and a guided convert, the two seams Phase 3 trialled) and
> the WebSocket behaviour at the 240 s request timeout.

---

## Bound release/schema compatibility record

Scenario A only. The controller validates the record with
`compatibility-record-validate` and passes it through the shared
`compatibility-record-gate` command (the ECS runbook keeps its `jq` fence; a
parity test feeds one corpus through both).

```json
{
  "schema": "elspeth.azure-container-apps-compatibility-receipt.v1",
  "record_id": "change-record-id",
  "acceptance_run_id": "acceptance-run-id",
  "scenario_id": "A",
  "candidate_sha": "40-lowercase-hex",
  "candidate_image_digest": "sha256:64-lowercase-hex",
  "candidate_revision_sha256": "64-lowercase-hex",
  "candidate_doctor_job_sha256": "64-lowercase-hex",
  "candidate_package_version": "0.8.0",
  "previous_source_sha": "",
  "previous_image_digest": "",
  "previous_revision_sha256": "",
  "rollback_doctor_job_sha256": "",
  "previous_package_version": "",
  "schema_facts": {
    "candidate": {"session_epoch": 52, "landscape_epoch": 37, "run_web_plugin_policy_present": true},
    "previous": null,
    "structural_changes": "initial_create",
    "semantics_only_changes": "none",
    "archive_export_decision": "not_applicable",
    "destructive_reset_required": false
  },
  "forward_compatible": true,
  "backward_compatible": false,
  "rollback_permitted": false,
  "decision": "approved",
  "approver_identity": "database-operator",
  "countersigner_identity": "release-operator",
  "approved_at": "RFC3339-UTC",
  "countersigned_at": "RFC3339-UTC",
  "expires_at": "RFC3339-UTC"
}
```

The `schema_facts` object is the one shared derivation (`_expected_schema_facts`
in the acceptance package); the literals above are byte-bound to it by the
runbook-contract test, which is what keeps a prose literal honest when the
epoch moves. `candidate_revision_sha256` is `sha256` of the canonical JSON of
the deployed revision's `properties.template`; `candidate_doctor_job_sha256`
likewise for the doctor Job's template. With `rollback_permitted: false` the
rollback section says "repair forward", exactly as ECS does for Scenario A.

---

## Replica probes

Reconfigure the app to two revisions `rA` and `rB`, `minReplicas = maxReplicas
= 1` each, `activeRevisionsMode: Multiple`, traffic 50/50, labels `a` and `b`,
and `stickySessions.affinity: none` (session affinity is unavailable outside
single revision mode). The two revisions differ only in which runtime-role
URL secret they reference: `rA` runs as `elspeth_runtime_a`, `rB` as
`elspeth_runtime_b`. Each replica is addressed on its label URL
`https://elspeth-web---<label>.<defaultDomain>` (facts §2.3); the driver
asserts two distinct `X-Elspeth-Instance` values before scoring a trial and
cross-checks every replica name against `az containerapp replica list`.

```bash
# One deployment per runtime role: the label selects the role's URL secrets
# and the doctor Job name (doctor-runtime-a / doctor-runtime-b).
for label in a b; do
  az_deploy_capture deployment group create \
    --name "elspeth-workload-probes-${CANDIDATE_SHA:0:12}-${label}" \
    --resource-group "$RESOURCE_GROUP" \
    --template-file deploy/azure-container-apps/workload.bicep \
    --parameters deploy/azure-container-apps/workload.acceptance.bicepparam \
    --parameters image="$CANDIDATE_IMAGE" revisionSuffix="${CANDIDATE_SHA:0:12}-${label}" \
      runtimeRoleLabel="$label" \
      composerTransportIdleCeilingSeconds="$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" \
    >"$EVIDENCE_DIR/workload-probes-${label}.json"
  az_capture containerapp revision label add --name elspeth-web --resource-group "$RESOURCE_GROUP" \
    --label "$label" --revision "elspeth-web--${CANDIDATE_SHA:0:12}-${label}"
done
# Traffic weights and labels are application-scope changes: no new revision.
az_capture containerapp ingress traffic set --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --label-weight a=50 b=50
APP_DOMAIN=$(az_capture containerapp env show --name elspeth-env --resource-group "$RESOURCE_GROUP" \
  --query properties.defaultDomain --output tsv)
export LABEL_A_URL="https://elspeth-web---a.${APP_DOMAIN}"
export LABEL_B_URL="https://elspeth-web---b.${APP_DOMAIN}"
curl_capture "$LABEL_A_URL/api/system/status" | jq -e '.instance_id' >/dev/null
curl_capture "$LABEL_B_URL/api/system/status" | jq -e '.instance_id' >/dev/null
```

Run the probes in the order **P1, P2, P4, P3** (P3 is destructive and last),
then the single-revision `maxReplicas = 2` pass of P1 and P4a. Every receipt
carries its `mechanism`, a closed enum: a receipt cannot claim more than the
tree proves, and overclaiming is a schema violation rather than a convention.

| probe | action | passing evidence | `mechanism` |
|---|---|---|---|
| **P1** concurrent guided ops from two replicas | 20 trials; the same `POST /api/sessions/{id}/guided/respond` fired at `LABEL_A_URL` and `LABEL_B_URL` within 5 ms | per trial exactly one 2xx and one 409 `"Session operation is already active"`; the fence's `operation_epoch` advances by exactly one; exactly one `guided_operations` row; two distinct `owner_instance_id` values across the run | `session_operation_fence` |
| **P2** run-start coordination | 20 trials; `POST /api/sessions/{id}/execute` from both labels concurrently | exactly one `runs` row and one Landscape run per trial; one 202 and one 409. The receipt asserts that no `run_start_permits` row exists: the table has no writer, and the driver has no code path that could claim one | `session_operation_fence_execute` |
| **P4** cross-replica progress | session and run created via `LABEL_A_URL`; status, outputs, messages and a blob written by `rA` read via `LABEL_B_URL` | **P4a (must pass):** all DB-backed state visible from `rB` within one poll interval; blob bytes identical through NFS; terminal status observed on `rB`. **P4b (recorded, cannot pass):** the live progress stream and the WebSocket ticket are owner-affine; the driver records the production sticky-session setting as the mitigation | `postgresql_and_nfs` (P4a); `owner_affine` (P4b) |
| **P3** lease takeover after a partitioned owner | long run started via `LABEL_A_URL` (owner `rA`); partition `rA` by role revocation (below); wait past `session_operation_lease_seconds` (30) and the membership lease; then `az containerapp revision deactivate` on `rA`; restore the role afterwards | before expiry `LABEL_B_URL` gets 409; after expiry the survivor's sweep cancels the run with the orphan reason and `rB` acquires the session; `rA`'s `web_instances` row is still `state='active'` with an expired lease; no duplicate sink effect; the fence's `owner_instance_id` becomes `rB`'s | `role_revocation_lease_expiry`; downgraded to `graceful_stop` if a `stopped` row landed |

### P3 primary primitive: role revocation by self-termination

The Flexible Server admin is not a superuser and cannot grant
`pg_signal_backend` (facts §4.1). The deterministic sequence needs no grant:
a session opened *as* the runtime role may always terminate that role's other
backends, and `NOLOGIN` affects only new connections.

The runtime session `S` must therefore be **one session held open across the
admin step**, exactly as `RoleRevocationPartition.partition` implements it in
`src/elspeth/web/_azure_container_apps_acceptance/controller.py`. Three
separate `psql_capture -c` calls would be three separate logins, and the third
one — the terminate — would be refused by the `NOLOGIN` it is supposed to
follow. `S` reads its statements from a FIFO, so the shell can run the admin
step between them while `S` stays connected; the terminate then runs inside
`S`, where `pg_backend_pid()` is `S`'s own backend and needs no captured pid.
`ELSPETH_PSQL_CALL_CEILING_SECONDS` (60) bounds the whole kept-open session,
the admin step included.

```bash
partition_runtime_a() {
  local sql_fifo runtime_session
  sql_fifo=$(mktemp -u -p /tmp elspeth-partition.XXXXXX) || return 1
  mkfifo -m 600 "$sql_fifo" || return 1
  trap 'rm -f -- "$sql_fifo"' RETURN
  (
    export PGDATABASE=elspeth_sessions PGUSER=elspeth_runtime_a PGSSLMODE=verify-full PGSSLROOTCERT=system
    PGPASSWORD=$(az_capture keyvault secret show --vault-name "$KEY_VAULT_NAME" \
      --name elspeth-runtime-a-password --query value --output tsv)
    export PGPASSWORD
    psql_capture -f "$sql_fifo"
  ) &
  runtime_session=$!
  exec {partition_sql}>"$sql_fifo"
  printf '%s\n' 'SELECT pg_backend_pid();' >&"$partition_sql"
  (
    export PGDATABASE=elspeth_sessions PGUSER="$PG_ADMIN_USER" PGSSLMODE=verify-full PGSSLROOTCERT=system
    PGPASSWORD=$(az_capture keyvault secret show --vault-name "$KEY_VAULT_NAME" \
      --name elspeth-admin-password --query value --output tsv)
    export PGPASSWORD
    psql_capture -c 'ALTER ROLE elspeth_runtime_a NOLOGIN;'
  )
  printf '%s\n' 'SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity
    WHERE usename = current_user AND pid <> pg_backend_pid();' >&"$partition_sql"
  exec {partition_sql}>&-
  wait "$runtime_session"
}

restore_runtime_a() {
  (
    export PGDATABASE=elspeth_sessions PGUSER="$PG_ADMIN_USER" PGSSLMODE=verify-full PGSSLROOTCERT=system
    PGPASSWORD=$(az_capture keyvault secret show --vault-name "$KEY_VAULT_NAME" \
      --name elspeth-admin-password --query value --output tsv)
    export PGPASSWORD
    psql_capture -c 'ALTER ROLE elspeth_runtime_a LOGIN;'
  )
}
```

From that moment `rA`'s pools (`pool_pre_ping`, 5 + 5 per engine) detect the
dead sockets, try to reconnect and are refused: heartbeat, renew, release,
cancel and the `stopped` write all fail; `rB` is untouched. The secondary
primitive is `az containerapp revision deactivate` with
`terminationGracePeriodSeconds: 0` ("stop immediately via the kill signal";
facts §2.5); the receipt records the observed row state and downgrades to
`graceful_stop` if a `stopped` write landed. `kill -9 1` through
`az containerapp exec` is a kernel no-op for a PID-namespace init (measured;
facts §7.1) and is recorded, not used.

> **LIVE:** P3's row and fence observations need 6b-2's membership writer;
> until it is merged the driver records `mechanism: unreachable` and the
> probe is not waived.

---

## Connection budget

Per replica per engine the pool is `pool_size 5 + max_overflow 5`; two
engines per replica; two replicas plus Jobs and the operator's `psql`
(facts §4.5). `verify-connection-budget` reads
`az monitor metrics list --metric active_connections` on the server and
passes the series through the shared validator against the SKU's
`max_connections`.

```bash
az_capture monitor metrics list --resource "$POSTGRES_RESOURCE_ID" \
  --metric active_connections --interval PT1M --aggregation Maximum \
  --start-time "$PROBE_WINDOW_START" --end-time "$PROBE_WINDOW_END" \
  >"$EVIDENCE_DIR/active-connections.json"
```

---

## Testcontainer run

The PostgreSQL contention proofs (`pytest tests/ -m testcontainer -n 0
--junitxml=testcontainer-junit.xml`, the exact selection CI's required
testcontainer job runs) are recorded as the `testcontainer-run` receipt:
selection, pytest exit code and the junit id counts, bound to the candidate
sha. The shared gate (`testcontainer_run_gate`, provider `azure`) REFUSES the
bundle unless exactly one passing run is on record — absence is
`testcontainer_run_missing`, a failing run `testcontainer_run_failed`, two
passing runs `testcontainer_run_ambiguous`; a failed run is kept as evidence
and superseded by a later passing one, never deleted. The suites provision
their own PostgreSQL through testcontainers on the acceptance host (no
external-DSN seam exists in the tree), so Docker must be available there.

```bash
exit_status=0
rm -f testcontainer-junit.xml
uv run --frozen pytest tests/ -m testcontainer -n 0 --junitxml=testcontainer-junit.xml || exit_status=$?
uv run --frozen python -m elspeth.web._acceptance_common.testcontainer_run \
  --provider azure --junit testcontainer-junit.xml --exit-code "$exit_status" \
  --candidate-sha "$CANDIDATE_SHA" --scenario-id A >"$EVIDENCE_DIR/testcontainer-run.json"
rm -f testcontainer-junit.xml
```

---

## Evidence

Log Analytics is the environment's log destination (`log-analytics`,
facts §5.1). The checked-in KQL files under `deploy/azure-container-apps/kql/`
are executed by the driver; the SHA-256 of each file is bound into the
receipt, and the table and column names inside them are verified live and
never pinned by a unit test.

```bash
kql_capture() {
  local query_file="$1" out="$2"
  az_capture monitor log-analytics query --workspace "$LOG_ANALYTICS_CUSTOMER_ID" \
    --analytics-query "$(<"$query_file")" \
    --timespan "${PROBE_WINDOW_START}/${PROBE_WINDOW_END}" >"$out"
}
kql_capture deploy/azure-container-apps/kql/doctor-report.kql "$EVIDENCE_DIR/doctor-report.json"
kql_capture deploy/azure-container-apps/kql/run-sentinel-by-replica.kql "$EVIDENCE_DIR/run-sentinel.json"
kql_capture deploy/azure-container-apps/kql/replica-lifecycle.kql "$EVIDENCE_DIR/replica-lifecycle.json"
kql_capture deploy/azure-container-apps/kql/fence-conflict-409.kql "$EVIDENCE_DIR/fence-409.json"
```

Ingestion lags by minutes (facts §5.1); the driver polls with a ten-minute
ceiling and records `ingestion_time() - TimeGenerated`. Raw output is
projected onto the closed detail sets by the acceptance package, redacted
through the shared visitor, and checked for canary tokens before it becomes
a receipt.

---

## Secret rotation

Container Apps secrets are versioned Key Vault references (facts §2.7):
rotation is a new secret version plus a new revision, and receipts record the
secret name and version only. Which application keys a rotation invalidates
is decided by `src/elspeth/web/key_derivation.py`, which derives every
purpose key from `secret_key` by HKDF purpose, and is pinned by
`tests/unit/web/test_key_derivation_wiring.py`: rotating the SSO transaction
secret does not invalidate user secrets or session tokens, while rotating
`secret_key` itself invalidates all four derived keys at once. Cite those two
files; do not restate the consumer list here.

---

## Disposable acceptance cleanup

```bash
export ELSPETH_CLEANUP_MODE=1
az_deploy_capture group delete --name "$RESOURCE_GROUP" --yes
remaining=$(az_capture graph query \
  -q "Resources | where resourceGroup =~ '${RESOURCE_GROUP}' | count" \
  --query 'data[0].Count' --output tsv)
test "$remaining" = 0
az_capture keyvault purge --name "$KEY_VAULT_NAME" --location "$AZURE_LOCATION" \
  || printf '%s\n' 'key_vault_tombstoned' >>"$EVIDENCE_DIR/cleanup-notes.txt"
```

The resource group is a true ownership boundary; Azure Resource Graph is the
subscription-wide inventory; a Key Vault that cannot be purged is recorded as
a tombstone with its scheduled purge date. The ECS gate ledger and HMAC
approvals are not reproduced for this disposable group (plan D1).

---

## Receipt and docs flip

The facade validates the bundle of receipts (`verify-doctor-job`,
`verify-storage-job`, `verify-blob-managed-identity`, `verify-log-analytics`,
`verify-connection-budget`, `compatibility-record`, `revision-rollout`,
`replica-fence-conflict`, `replica-run-start`, `replica-lease-takeover`,
`replica-progress`, `resource-graph-cleanup`, `testcontainer-run` — the last
through the shared gate, which refuses the bundle without exactly one passing
run) and writes the sanitized
receipt to `docs/operator/evidence/azure-container-apps/0.8.0.json`. That
receipt, the support-claim flip in
[Deployment Platforms](../reference/deployment-platforms.md) and the CHANGELOG
rewording land in **one commit** with a three-seat sign-off. The first run is
expected to fail forward on the platform-facing pieces; the bar is a second
clean run end to end.
