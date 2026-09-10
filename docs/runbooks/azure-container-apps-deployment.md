# Runbook: Full disposable Azure Container Apps acceptance environment

Deploy ELSPETH web to Azure Container Apps at replica count > 1 with Azure
Database for PostgreSQL Flexible Server, an NFS 4.1 Azure Files share, Key
Vault secret references, a user-assigned managed identity, and Log Analytics;
exercise the four multi-replica probes; collect sanitized evidence; and destroy
the resource group. This runbook is the Azure equivalent of
[the AWS ECS disposable acceptance program](aws-ecs-deployment.md) in
**evidence**, not in code: where the Azure control plane already produces a
fact, the receipt records a sanitized projection of it.

> **Status.** The implemented ACA slice received desktop acceptance:
> `elspeth-5ec3befc1a` closed on 2026-09-10 by operator ruling. No live cloud
> acceptance is claimed. This executable procedure can produce a future receipt
> at `docs/operator/evidence/azure-container-apps/0.8.1.json`. Steps marked
> **LIVE** require measurements from that operator run. Neither a live run nor
> a receipt is an outstanding closure condition. See
> [Deployment Platforms](../reference/deployment-platforms.md) for support scope.

The supported configuration retains `Single` revision mode, `sticky` session
affinity and 2–4 replicas with one web process per replica. External PostgreSQL
provides single-use tickets and durable run-event replay on authorized peer
reconnect, renewable Composer request leases with saved progress and current
inflight accounting, and shared budgets for auth, writes and Composer/execution
work. An interrupted provider request is not automatically resumed. Automatic
run handoff covers durable admission, permit-bound PREPARED initialization and
eligible checkpoint resume, using fresh web and Landscape authority. Unsafe
effects, incomplete sources and identity/compatibility failures remain
`recovery_required`; see the [handoff contract](../reference/deployment-platforms.md#durable-run-handoff).
Integrated verification is recorded in the
[ACA plan](../plans/2026-09-10-aca-pivot-and-replica-residuals.md#final-verification).
Evidence remains limited to
local PostgreSQL mechanism and integration evidence, with no cloud receipt or
no-affinity deployment qualification.

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
  run-start coordination, lease takeover and shared state visibility, while
  recording the legacy P4b receipt's conservative limitation; or
- produce a sanitized receipt of an actual cloud acceptance run.

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
  `CONTAINER_APP_REPLICA_NAME` (facts §2.1); the probe driver
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
- The epoch-54 image (session epoch 54, Landscape epoch 39) in the registry.
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

protected_capture() (
  local kind="$1" failure_class="$2" seconds scratch cleanup stderr_reader status
  shift 2
  seconds=$(protected_timeout_seconds "$kind") || exit 1
  scratch=$(mktemp -d -p /tmp elspeth-capture.XXXXXX) || exit 1
  printf -v cleanup 'rm -rf -- %q' "$scratch"
  trap "$cleanup" EXIT
  : >"$scratch/stderr"
  chmod 600 "$scratch/stderr"
  # Bound captured streams; an inherited file-size limit breaks Bicep's runtime.
  mkfifo "$scratch/stderr-pipe"
  head -c "$((ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES + 1))" <"$scratch/stderr-pipe" >"$scratch/stderr" &
  stderr_reader=$!
  set +e
  timeout --signal=TERM --kill-after=5s "$seconds" "$@" 2>"$scratch/stderr-pipe" \
    | head -c "$((ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES + 1))" >"$scratch/stdout"
  status=${PIPESTATUS[0]}
  wait "$stderr_reader"
  set -e
  if test "$(wc -c <"$scratch/stdout")" -gt "$ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES" \
    || test "$(wc -c <"$scratch/stderr")" -gt "$ELSPETH_COMMAND_OUTPUT_LIMIT_BYTES"; then
    printf '%s\n' 'command_output_limit_exceeded' >&2
    exit 1
  fi
  if test "$status" -ne 0; then
    printf '%s\n' "$failure_class" >&2
    exit "$status"
  fi
  cat "$scratch/stdout"
)

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

The executable driver uses concrete operator-local ARM JSON inputs. Compile
the local completed environment parameters into `MAIN_PARAMETERS`; replace
the sample registry identity, administrator login/password, region and IP
allowlists before what-if. After the environment stage, resolve
`WORKLOAD_PARAMETERS` with the cold-install resolver and inventory outputs.
Copy that resolved file to `WORKLOAD_A_PARAMETERS` and
`WORKLOAD_B_PARAMETERS`; in all three files set the same `acceptanceRuntimeSecretUrls`
object to `{a: {sessionDbUrl, landscapeUrl}, b: {sessionDbUrl, landscapeUrl}}`,
using each role's versioned URLs. Set Multiple mode, none affinity, min/max
replicas 1 and runtimeRoleLabel a/b on the two labelled files. Preserve the
production runtime URL parameters in all files. Every deployment retains
production and both acceptance roles' named application secrets through the
final Single-revision pass; the label selects the references used by that revision.
Keep schema-owner URLs in the dedicated schema-owner vault. Never pass a
tracked placeholder example to a deployment.

| Driver input | Required concrete value |
| --- | --- |
| `MAIN_PARAMETERS` | Local subscription-scope ARM parameter JSON with real environment inputs |
| `WORKLOAD_PARAMETERS` | Local production ARM parameters from the cold-install resolver |
| `WORKLOAD_A_PARAMETERS`, `WORKLOAD_B_PARAMETERS` | Local role-specific workload ARM parameters with pinned secret versions |
| `COMPATIBILITY_RECORD` | Candidate-bound compatibility record; the driver produces `TESTCONTAINER_RECEIPT` against the provisioned Flexible Server |
| `ELSPETH_ACCEPTANCE_PYTHON` | Existing venv Python; bind both worktree source roots with `PYTHONPATH` |
| `P1_TRIAL_REQUESTS` | JSON file of at least 20 unique `{session_id, body}` guided requests, each with its freshly minted turn token |
| `P2_SESSION_IDS` | JSON array of at least 20 unique fresh executable session IDs, one per trial |
| `P4_SESSION_ID` | Prepared executable session for cross-replica progress |
| `P3_SESSION_ID` | Prepared long-running session when running the probes stage separately |
| `P3_SINK_PATH`, `P3_SINK_KEY_FIELD` | Operator-mounted shared NFS CSV path containing `{session_id}` and unique output row key; collector checks the exact persisted sink path |
| `PG_MAX_CONNECTIONS`, `PG_APPROVED_BUDGET`, `PG_SAFETY_MARGIN` | Measured database limit and approved connection budget |
| `SENTINEL_HASH` | Expected sentinel from the selected candidate |
| `PROBE_YAML`, `P3_YAML` | Local executable P2/P4 YAML and a long-running CSV-sink P3 YAML for the `prepare` stage |
| `PROBE_SOURCE_BLOB`, `PROBE_SOURCE_NAME` | Local CreateInlineBlobRequest JSON (`filename`, `content`, `mime_type`) and source mapping name (default `input`) |
| `P4_MESSAGE_BODY` | Valid Composer message JSON asking for an explanation without changing pipeline structure |
| `P1_INTENT`, `P1_BODY` | Initial guided intent and valid action template; `prepare` merges each fresh server turn token and a new operation ID |
| `ACCEPTANCE_SECRET_DIR` | Private directory of the bootstrap password and application-secret files listed below |
| `PGSSLROOTCERT` | Operator-host CA bundle for Flexible Server TLS verification |
| `BOOTSTRAP_PRINCIPAL_ID`, `BOOTSTRAP_PRINCIPAL_TYPE` | Explicit operator object ID and `User` or `ServicePrincipal`, granted Key Vault Secrets Officer on both disposable vaults |

The `all` path invokes `scripts/bootstrap-acceptance.sh` after environment and
image publication. `ACCEPTANCE_SECRET_DIR` must contain four distinct password
files (`elspeth-schema-owner-password`, `elspeth-runtime-password`,
`elspeth-runtime-a-password`, `elspeth-runtime-b-password`) and the application
value files `elspeth-secret-key`, `elspeth-shareable-link-signing-key`,
`elspeth-fingerprint-key`, `elspeth-operator-metrics-bearer-token`. Optional
Composer credentials use `COMPOSER_ENDPOINT_SECRET_NAME` and a file of that
name. Files stay mode 0600 outside Git. The operator needs role-assignment
permission and network access to PostgreSQL and Key Vault. The helper creates
the four database roles, writes versioned secrets, and emits all three concrete
workload parameter files and a private `acceptance-env.json` containing host
observer credentials. That credential file is never receipt evidence. A failed
bootstrap stops the driver and leaves only a private bounded error log; do not
rerun the cold-only SQL against partially created roles without investigating.
The helper first proves the operator's newly assigned secret-write permission
by writing the real owner session URL to the schema-owner vault and the real
`elspeth-secret-key` value to the runtime vault. It retries only an explicit
`ForbiddenByRbac` response, for at most 600 seconds; firewall, network and other
failures stop immediately. SQL role creation starts only after both succeed,
so an ordinary RBAC propagation delay does not strand non-idempotent role
creation. This follows Microsoft's [Key Vault RBAC guidance](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide),
which requires allowing role assignments time to refresh. A shorter bound can
be selected through `KEY_VAULT_RBAC_WAIT_SECONDS` (1–600).

Use individual driver stages while establishing the environment, role grants,
secret versions and probe fixtures. The `all` path runs `prepare` through the
public API after rollout: it uploads the source and imports the supplied YAML
into fresh sessions, including one session for each P2 trial. It writes
`prepared-sessions.json` and `p2-session-ids.json` under the evidence directory.
Use an existing acceptance bearer or the driver login/registration inputs.
The standalone `probes` stage can consume the prepared session inputs listed
above. The driver stops at the first
failure; inspect retained evidence before resuming. Explicit cleanup remains
available after failure; a failed probe never becomes a passing receipt.
The `all` path runs the required PostgreSQL selection itself with the existing
venv Python and the provisioned inventory host, then constructs the shared
testcontainer receipt. It does not accept an empty or skipped test run as
evidence. Pre-prepared session IDs and a pre-existing testcontainer receipt
are not prerequisites for `all`.

The complete `all` sequence is environment, image copy, bootstrap, Jobs,
PostgreSQL tests, production rollout and initial receipts, labelled workload,
session preparation, **P1, P2, P4, P3**, `single-revision`, evidence, cleanup,
then bundle validation. The final Single-revision pass is part of `all`,
before evidence collection and deletion of the disposable resource group.

---

## 1. Create the resource group and environment

```bash
az_capture account set --subscription "$AZURE_SUBSCRIPTION_ID"
az_deploy_capture deployment sub what-if \
  --location "$AZURE_LOCATION" \
  --template-file deploy/azure-container-apps/main.bicep \
  --parameters "@$MAIN_PARAMETERS" \
  --parameters resourceGroupName="$RESOURCE_GROUP" acceptanceRunId="$ACCEPTANCE_RUN_ID" \
  >"$EVIDENCE_DIR/what-if.json"
az_deploy_capture deployment sub create \
  --name "elspeth-acc-${ACCEPTANCE_RUN_ID}" \
  --location "$AZURE_LOCATION" \
  --template-file deploy/azure-container-apps/main.bicep \
  --parameters "@$MAIN_PARAMETERS" \
  --parameters resourceGroupName="$RESOURCE_GROUP" acceptanceRunId="$ACCEPTANCE_RUN_ID" \
  >"$EVIDENCE_DIR/deployment.json"
jq -S '.properties.outputs' "$EVIDENCE_DIR/deployment.json" >"$EVIDENCE_DIR/inventory.json"
sha256sum "$EVIDENCE_DIR/inventory.json"
```

The resource group is tagged `elspeth.acceptance-run-id`. The environment
deployment creates the virtual network, the Container Apps environment with
an NFS storage definition, the Premium FileStorage account with its NFS share
(`rootSquash: NoRootSquash`, encryption in transit off, private endpoint), the
Flexible Server with both databases and its administrator login, separate
runtime and schema-owner Key Vaults (RBAC, purge protection **off** for the
disposable group), the Log Analytics workspace and separate runtime and
schema-owner user-assigned identities. Only the schema-owner identity reads
the owner vault; both identities can read application keys in the runtime
vault. Bootstrap grants the operator secret-write access to both vaults. The what-if
output replaces the ECS plan review; its SHA-256 is bound into the receipt.
Create the runtime and schema-owner roles and their grants before writing
their Key Vault URL versions and starting the doctor Jobs; the Bicep database
resources do not create application PostgreSQL roles.

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

The doctor Jobs run the candidate digest with the same NFS mount as the app.
Only `doctor-schema-init` attaches the schema-owner identity; runtime and Blob
checks attach the runtime identity. The root `provision-storage` Job has no
identity and uses its separately pinned public image. Start each Job, poll
its execution to a terminal state, and require
`Succeeded`; retrieve the doctor's `--json` report from Log Analytics by
execution name.

```bash
for label in a b; do
  if [[ "$label" == a ]]; then role_parameters=$WORKLOAD_A_PARAMETERS; else role_parameters=$WORKLOAD_B_PARAMETERS; fi
  az_deploy_capture deployment group create --resource-group "$RESOURCE_GROUP" \
    --name "elspeth-jobs-${label}" --mode Incremental \
    --template-file deploy/azure-container-apps/workload.bicep \
    --parameters "@$role_parameters" \
    --parameters deployWebApp=false candidateSourceSha="$CANDIDATE_SHA" image="$CANDIDATE_IMAGE" \
      runtimeRoleLabel="$label" >"$EVIDENCE_DIR/jobs-${label}.json"
done

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
  with the schema-owner URLs and initializes both schemas at session epoch 54
  and Landscape epoch 39.
- `doctor-runtime-a` / `doctor-runtime-b` run `elspeth doctor deployment --json`
  with each runtime role's URLs; `session_schema`, `landscape_schema`,
  `session_tls`, `landscape_tls`, `payload_store_writable` and
  `blob_writable` must all be OK. TLS is `verify-full` with
  `sslrootcert=system`: the runtime image's CA store carries both Azure roots
  (facts §4.4).
- `verify-blob-managed-identity` re-runs the two `azure_blob@managed_identity`
  cases inside the environment, the one auth mode whose truth depends on
  where the process runs.

> **LIVE:** for 0.8.1 acceptance, run the Jobs with the candidate digest and
> require both schema checks to pass after initialization at session epoch 54
> and Landscape epoch 39. Record the execution names. Any no-schema dry run
> against a `release/0.8.0` image is predecessor-only wiring evidence; it
> cannot establish the candidate's schema compatibility or acceptance.

## 4. Deploy the production shape and prove the rollout

```bash
az_deploy_capture deployment group create \
  --name "elspeth-workload-${CANDIDATE_SHA:0:12}" \
  --resource-group "$RESOURCE_GROUP" \
  --template-file deploy/azure-container-apps/workload.bicep \
  --parameters "@$WORKLOAD_PARAMETERS" \
  --parameters image="$CANDIDATE_IMAGE" revisionSuffix="r${CANDIDATE_SHA:0:12}" \
    candidateSourceSha="$CANDIDATE_SHA" deployWebApp=true \
    composerTransportIdleCeilingSeconds="$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" \
  >"$EVIDENCE_DIR/workload-production.json"
az_capture containerapp revision list --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --query "[?properties.active].{name:name,traffic:properties.trafficWeight,state:properties.runningState}" \
  >"$EVIDENCE_DIR/revisions-production.json"
az_capture containerapp replica list --name elspeth-web --resource-group "$RESOURCE_GROUP" \
  --revision "elspeth-web--r${CANDIDATE_SHA:0:12}" >"$EVIDENCE_DIR/replicas-production.json"
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
  "candidate_package_version": "0.8.1",
  "previous_source_sha": "",
  "previous_image_digest": "",
  "previous_revision_sha256": "",
  "rollback_doctor_job_sha256": "",
  "previous_package_version": "",
  "schema_facts": {
    "candidate": {"session_epoch": 54, "landscape_epoch": 39, "run_web_plugin_policy_present": true},
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

Both runtime roles need write access to `web_instances`. Each replica registers
itself in that table at boot, so a role holding only `SELECT` fails startup with
`permission denied for table web_instances` and P3 never gets a row to expire.
The minimum on the session database is read on every table plus two verbs on
this one:

```sql
GRANT USAGE ON SCHEMA public TO elspeth_runtime_a;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO elspeth_runtime_a;
GRANT INSERT, UPDATE ON web_instances TO elspeth_runtime_a;
```

Repeat for `elspeth_runtime_b`. Grant it when you provision the roles: the app
does not fall back to a single-process mode when the role cannot write, because
a PostgreSQL deployment silently running without membership is the failure this
table exists to prevent. The AWS ECS Terraform path covers the same requirement
through `ALTER DEFAULT PRIVILEGES` in
`deploy/aws-ecs/terraform/modules/scenario/database_bootstrap.tf`.

```bash
# One deployment per runtime role: the label selects the role's URL secrets
# and the doctor Job name (doctor-runtime-a / doctor-runtime-b).
for label in a b; do
  if [[ "$label" == a ]]; then role_parameters=$WORKLOAD_A_PARAMETERS; else role_parameters=$WORKLOAD_B_PARAMETERS; fi
  az_deploy_capture deployment group create \
    --name "elspeth-workload-probes-${CANDIDATE_SHA:0:12}-${label}" \
    --resource-group "$RESOURCE_GROUP" \
    --template-file deploy/azure-container-apps/workload.bicep \
    --parameters "@$role_parameters" \
    --parameters image="$CANDIDATE_IMAGE" revisionSuffix="r${CANDIDATE_SHA:0:12}-${label}" \
      candidateSourceSha="$CANDIDATE_SHA" deployWebApp=true \
      runtimeRoleLabel="$label" \
      composerTransportIdleCeilingSeconds="$ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS" \
    >"$EVIDENCE_DIR/workload-probes-${label}.json"
  az_capture containerapp revision label add --name elspeth-web --resource-group "$RESOURCE_GROUP" \
    --label "$label" --revision "elspeth-web--r${CANDIDATE_SHA:0:12}-${label}"
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
then the Single-revision `minReplicas = maxReplicas = 2` pass of P1 and P4a. Every receipt
carries its `mechanism`, a closed enum: a receipt cannot claim more than the
tree proves, and overclaiming is a schema violation rather than a convention.

| probe | action | passing evidence | `mechanism` |
|---|---|---|---|
| **P1** concurrent guided ops from two replicas | 20 trials; the same `POST /api/sessions/{id}/guided/respond` fired at `LABEL_A_URL` and `LABEL_B_URL` within 5 ms | per trial exactly one 2xx and one 409 `"Session operation is already active"`; the fence's `operation_epoch` advances by exactly one; exactly one `guided_operations` row; two distinct `owner_instance_id` values across the run | `session_operation_fence` |
| **P2** run-start coordination | 20 trials; `POST /api/sessions/{id}/execute` from both labels concurrently | exactly one `runs` row and one Landscape run per trial; one 202 and one 409. This legacy receipt does not measure durable permit admission or handoff; its field set records run-start contention only | `session_operation_fence_execute` |
| **P4** cross-replica progress | session and run created via `LABEL_A_URL`; status, outputs, messages and a blob written by `rA` read via `LABEL_B_URL` | **P4a (must pass):** all DB-backed state visible from `rB` within one poll interval; blob bytes identical through NFS; terminal status observed on `rB`. **P4b (recorded, cannot pass):** the legacy v2 receipt conservatively retains its owner-affine result and records production sticky sessions; it does not measure the new durable ticket/event replay mechanisms | `postgresql_and_nfs` (P4a); `owner_affine` (P4b) |
| **P3** lease takeover after a partitioned owner | long run started via `LABEL_A_URL` (owner `rA`); partition `rA` by role revocation (below); observe the survivor before and after the session-operation and membership lease deadlines; restore the role afterwards | before expiry `LABEL_B_URL` gets 409; after expiry the survivor's sweep cancels the run with the orphan reason and `rB` acquires the session; `rA`'s `web_instances` row is still `state='active'` with an expired lease; no duplicate sink effect; the fence's `owner_instance_id` becomes `rB`'s | `role_revocation_lease_expiry`; downgraded to `graceful_stop` if a `stopped` row landed |

P3 retains the legacy receipt's cancellation-shaped oracle; it does not measure
the new automatic dispatch or checkpoint-resume transitions. A run that takes
one of those transitions must not be relabelled as satisfying that oracle.

The legacy v2 P4b result remains `cannot_pass` and does not measure the new
runtime capabilities. Receipt evolution is explicitly deferred; keep its
existing mechanism and validators. Local PostgreSQL progress, Composer and
shared-budget tests do not promote this receipt or qualify routing without
affinity.

### Final Single-revision pass

`scripts/acceptance.sh all` restores the partitioned runtime roles, then deploys
`r<sha12>-single` in `Single` mode with `sticky` affinity and exactly two
replicas. It creates fresh guided trial sessions and a fresh executable P4a
session through the default ingress. Two persistent cookie clients discover
distinct process UUIDs, and `/api/system/status` binds their revision and
platform replica names to the live Azure inventory. P1 still requires at least
20 fresh turn-token requests; P4a checks fresh messages, run status, output
metadata and uploaded blob bytes across those clients.

To run only this final stage before cleanup, retain the same `EVIDENCE_DIR`,
inventory, verified Job reports, resolved workload parameter files and private
`parameters/acceptance-env.json`. Set the common driver inputs above plus an
existing `ELSPETH_ACCEPTANCE_BEARER_TOKEN`, `P1_INTENT`, `P1_BODY`, `PROBE_YAML`,
`PROBE_SOURCE_BLOB` and `P4_MESSAGE_BODY`:

```bash
bash deploy/azure-container-apps/scripts/acceptance.sh single-revision
```

The standalone stage validates that bearer-token input before deployment. It
uses the production runtime role. If an earlier P3 invocation was interrupted,
complete the driver's `restore` stage first. It creates its own sessions and
uses the default ingress URL.

The private evidence directory contains `single-p1-trial-requests.json`,
`prepared-single-p4-message.json`, `single-p1/` and `single-p4/` observations
with each probe's `binding.json`, and these receipt artifacts:

| Probe | Required receipt kind | Extracted receipt | Receipt-store digest |
| --- | --- | --- | --- |
| P1 | `single-revision-fence-conflict` | `single-p1.receipt.json` | `single-p1.receipt.sha256` |
| P4a | `single-revision-progress` | `single-p4.receipt.json` | `single-p4.receipt.sha256` |

The corresponding `.stream` files retain the facade output. Receipt-store
subjects use the actual cookie-selected replica, and stored receipts remain
bound to their candidate, revision and replica. Each Single-revision topology
also carries the ARM `container_app_id`, allowing every receipt admission to
recompute both replica-binding hashes from the receipt itself. Changing the deployment or
probe inputs requires fresh evidence; a committed collector and passing local
tests do not replace the live run or promote the platform support claim.

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
and superseded by a later passing one, never deleted. The receipt also
records which database ran (`database`, `database_identity_sha256`): every
suite obtains its PostgreSQL through one seam
(`tests/helpers/postgres_target.py`) that honours `ELSPETH_TEST_POSTGRES_URL`,
and the receipt derives the two fields from the same variable, so it can say
`provisioned` only when the suites ran there. Export it as the Flexible
Server admin URL — the suites create and drop throwaway databases and roles
and terminate other roles' backends, so a right the admin lacks (facts §4.1)
fails the suite that needs it and is recorded as such — with
`sslmode=verify-full` and `sslrootcert` naming a readable PEM file holding
the Azure Database for PostgreSQL roots (facts §4.4): the deployment-
acceptance suites stat and hash that file, so the libpq `system` keyword the
driver uses elsewhere is not accepted here. The step refuses to run without
the variable so the acceptance record never describes a run on the host's
Docker; unset, the seam provisions a container per suite (what CI does).

```bash
: "${PGHOST:?the Flexible Server FQDN}"
: "${PG_ADMIN_USER:?the Flexible Server admin role}"
: "${AZURE_PG_ROOTS_PEM:?path to a PEM file holding the Azure Database for PostgreSQL root CAs}"
PGPASSWORD=$(az_capture keyvault secret show --vault-name "$KEY_VAULT_NAME" \
  --name elspeth-admin-password --query value --output tsv)
export ELSPETH_TEST_POSTGRES_URL="postgresql+psycopg://${PG_ADMIN_USER}:${PGPASSWORD}@${PGHOST}:5432/postgres?sslmode=verify-full&sslrootcert=${AZURE_PG_ROOTS_PEM}"
unset PGPASSWORD
exit_status=0
rm -f testcontainer-junit.xml
uv run --frozen pytest tests/ -m testcontainer -n 0 --junitxml=testcontainer-junit.xml || exit_status=$?
uv run --frozen python -m elspeth.web._acceptance_common.testcontainer_run \
  --provider azure --junit testcontainer-junit.xml --exit-code "$exit_status" \
  --candidate-sha "$CANDIDATE_SHA" --scenario-id A >"$EVIDENCE_DIR/testcontainer-run.json"
rm -f testcontainer-junit.xml
unset ELSPETH_TEST_POSTGRES_URL
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
SCHEMA_OWNER_KEY_VAULT_NAME=$(jq -er '.schemaOwnerKeyVaultName.value' "$EVIDENCE_DIR/inventory.json")
for vault_name in "$KEY_VAULT_NAME" "$SCHEMA_OWNER_KEY_VAULT_NAME"; do
  az_capture keyvault purge --name "$vault_name" --location "$AZURE_LOCATION" \
    || printf '%s\n' "key_vault_tombstoned:$vault_name" >>"$EVIDENCE_DIR/cleanup-notes.txt"
done
```

The resource group is a true ownership boundary; Azure Resource Graph is the
subscription-wide inventory; a Key Vault that cannot be purged is recorded as
a tombstone with its scheduled purge date. Record runtime and schema-owner
vault fates separately in the cleanup receipt: `runtime_key_vault_purged` /
`runtime_key_vault_tombstoned` / `runtime_scheduled_purge_date` and
`schema_owner_key_vault_purged` / `schema_owner_key_vault_tombstoned` /
`schema_owner_scheduled_purge_date`. The ECS gate ledger and HMAC
approvals are not reproduced for this disposable group (plan D1).

---

## Receipt from an operator-run acceptance

The facade validates the bundle of receipts (`verify-doctor-job`,
`verify-storage-job`, `verify-blob-managed-identity`, `verify-log-analytics`,
`verify-connection-budget`, `compatibility-record`, `revision-rollout`,
`replica-fence-conflict`, `replica-run-start`, `replica-lease-takeover`,
`replica-progress`, `single-revision-fence-conflict`, `single-revision-progress`,
`resource-graph-cleanup`, `testcontainer-run` — the last
through the shared gate, which refuses the bundle without exactly one passing
run) and writes the sanitized
receipt to `docs/operator/evidence/azure-container-apps/0.8.1.json` only after
the live procedure completes and its evidence passes validation. Never create
a receipt from desktop analysis or treat skipped or failed probes as passes.
If a run fails, retain its diagnostics, fix the defect and rerun before claiming
live acceptance. A first run, second clean run and receipt-conditioned docs
promotion are not prerequisites to closing `elspeth-5ec3befc1a`; that task
already closed through desktop acceptance. Any later documentation claim about
live results must cite the actual validated receipt and its measured scope.
