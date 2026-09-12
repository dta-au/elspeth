# ELSPETH on Azure Container Apps — Bicep bundle

The Azure equivalent of [`deploy/aws-ecs/terraform`](../aws-ecs/terraform/README.md)
in **evidence**, not in code: a Bicep bundle composed from Azure Verified
Modules, the parameter sets for a production stack and for the disposable
replica > 1 acceptance, the KQL evidence queries and the thin acceptance
driver. Every platform literal here is measured in
[the platform facts](../../docs/plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md);
the operator procedures are the three runbooks
([cold install](../../docs/runbooks/azure-container-apps-cold-install.md),
[existing-service redeploy](../../docs/runbooks/azure-container-apps-existing-service-redeploy.md),
[full disposable acceptance](../../docs/runbooks/azure-container-apps-deployment.md)).

> **Status.** Implemented; desktop acceptance closed `elspeth-5ec3befc1a` on
> 2026-09-10 under the operator's desktop-analysis ruling. No live cloud
> acceptance is claimed. A future operator run may produce the sanitized receipt
> at `docs/operator/evidence/azure-container-apps/0.8.1.json`; that receipt is
> no longer a tracker closure or documentation-promotion condition.

The supported operating configuration is `Single` revision mode, `sticky`
session affinity and 2–4 replicas, with one web process per replica. External
PostgreSQL provides single-use tickets, durable run-event replay on authorized
peer reconnect, renewable Composer request leases with saved progress and
current inflight accounting, and shared budgets for auth, writes and
Composer/execution work. An interrupted provider request is not automatically
resumed. Automatic run handoff is implemented for durable admission,
permit-bound PREPARED initialization and eligible checkpoint resume, with
fresh web and Landscape authority and explicit `recovery_required` exclusions.
Integrated verification is recorded in the
[ACA plan](../../docs/plans/2026-09-10-aca-pivot-and-replica-residuals.md#final-verification); see the
[handoff contract](../../docs/reference/deployment-platforms.md#durable-run-handoff).

Verification is limited to local PostgreSQL mechanism and integration evidence;
no cloud receipt or no-affinity deployment qualification is claimed. The legacy
v2 P4b receipt remains conservative `cannot_pass` with `owner_affine` and does
not measure these new runtime capabilities. Receipt evolution is deferred;
Single/sticky remains the operating configuration.

## Storage contract (stated once)

- **Both databases** (`elspeth_sessions`, `elspeth_landscape`) live on Azure
  Database for PostgreSQL Flexible Server, statically distinct, one
  schema-owner role and one runtime role (two runtime roles in the acceptance).
- `data/`, `data/blobs` and `payloads/` live on **one NFS 4.1 Azure Files
  share** mounted read-write at `/mnt/elspeth` on every replica and every Job.
  SMB Azure Files is not supported for this target.
- There is **no SQLite mode at replicas > 1**: the `azure-container-apps`
  deployment target refuses `sqlite-single` at configuration time.
- **Azure Files carries no database.**

## Files

| file | scope | content |
|---|---|---|
| `main.bicep` | subscription | resource group + `environment.bicep`; tags the group with `elspeth.acceptance-run-id` when given |
| `environment.bicep` | resource group | VNet (delegated infrastructure subnet + private-endpoint subnet, NSG allowing 445/2049), Log Analytics, separate runtime and schema-owner identities and Key Vaults, four private DNS zones, Premium FileStorage account with the NFS share (`NoRootSquash`, encryption in transit off), StorageV2 account with the payload blob container (runtime identity is Blob Data Contributor on that container only), Flexible Server (password auth, both databases, private endpoint, optional operator firewall rule), the Container Apps environment (Log Analytics destination, NFS storage definition), and `AcrPull` for both identities on the **existing** registry |
| `modules/registry-pull-role.bicep` | registry's resource group | the `AcrPull` assignment on the existing registry |
| `workload.bicep` | resource group | the `elspeth-web` app (digest-pinned image, Key Vault secret references, NFS volume, startup/liveness/readiness probes, session affinity, scale, grace period) and the manual Jobs `provision-storage` (root image), `doctor-schema-init` (schema-owner URLs, `doctor deployment --init-schema --json`), `doctor-runtime[-a|-b]` (runtime URLs, `doctor deployment --json`) and optional `verify-blob-managed-identity` (production source/sink lifecycles) |
| `main.example.bicepparam` / `environment.example.bicepparam` | | production stack parameters |
| `main.acceptance.bicepparam` | | disposable acceptance group: zone redundancy off, purge protection off, Burstable server with public access + operator firewall rule, 30-day retention |
| `workload.production.bicepparam` | | `Single` mode, `sticky` affinity, 2–4 replicas, ceiling 210 s |
| `workload.acceptance.bicepparam` | | `Multiple` mode, `none` affinity, 1 replica, `runtimeRoleLabel` a (deploy again with b) |
| `kql/*.kql` | | doctor report by execution; run sentinel by replica; replica lifecycle; fence-conflict 409s — SHA-256 bound into the receipt; column names verified live, never pinned by a test |
| `scripts/acceptance.sh` | | the stage driver (environment → image copy → bootstrap → Jobs → PostgreSQL tests → production rollout/receipts → labelled probes → Single-revision probes → evidence → cleanup → bundle validation) |

Cold installation deploys `workload.bicep` with `deployWebApp=false` in
Incremental mode before starting any Job. After storage provisioning and both
doctors succeed, deploying with `deployWebApp=true` creates the app. The pinned
managed-environment AVM uses the storage definition name as its physical NFS
share name, so both are `elspeth`.

Only `doctor-schema-init` attaches the schema-owner identity and reads owner
database URLs from the schema-owner vault. Both identities read application
keys from the runtime vault; web replicas cannot access owner credentials.
The root `provision-storage` Job has no managed identity. The required
`identityClientId` sets web `AZURE_CLIENT_ID` for Azure plugin authentication;
`schemaOwnerIdentityResourceId` selects the schema-init Job identity.
Existing shared-identity installations need the credential isolation migration
in the redeploy runbook before using the ordinary image-only path.

Acceptance parameters carry both roles in `acceptanceRuntimeSecretUrls`:
`{a: {sessionDbUrl, landscapeUrl}, b: {sessionDbUrl, landscapeUrl}}`.
The production, A and B parameter files carry this same object and preserve
the production runtime URL parameters. Every deployment retains the production
and both acceptance roles' application-scoped secret references through the
final Single-revision pass; `runtimeRoleLabel` selects one pair per revision,
including after restart.

`scripts/resolve-workload-parameters.sh` writes concrete operator-local ARM
JSON from environment outputs, verified image digests and Key Vault version
IDs. `scripts/validate-workload-parameters.jq` rejects placeholders before
what-if. Redeployment reuses the retained file to preserve secret versions and
configuration. `scripts/run-job.sh` waits on the exact newly started execution.
The acceptance driver uses the landed probe facade; claiming live evidence
requires an actual operator-run acceptance.

The final `single-revision` stage deploys `r<sha12>-single` with `Single`
revision mode, `sticky` affinity and exactly two replicas. Fresh P1 and P4a
probes use persistent cookie clients through the default ingress, with process
identities checked against the revision's replica inventory. It stores
`single-p1.receipt.json` and `single-p4.receipt.json` under the private evidence
directory and admits them to the receipt store. `all` includes this stage;
the standalone stage requires the retained environment/Job evidence, resolved
parameters, observer credentials and an existing acceptance bearer token.
These executable checks do not constitute a completed live acceptance.

For a fresh acceptance, `scripts/bootstrap-acceptance.sh` creates SQL roles and
versioned Key Vault secrets from explicit operator-local secret files before
Jobs start. It produces production and a/b workload parameter files plus a
mode-0600 `acceptance-env.json` containing host observer credentials. That file
is private operational configuration and must never enter a receipt or Git.
The cold-only SQL scripts fail on existing roles; investigate a partial failure
before retrying. The bootstrap commands have bounded execution time and output.

## Compile

```bash
bicep build deploy/azure-container-apps/main.bicep --stdout >/dev/null
bicep build deploy/azure-container-apps/environment.bicep --stdout >/dev/null
bicep build deploy/azure-container-apps/workload.bicep --stdout >/dev/null
for params in deploy/azure-container-apps/*.bicepparam; do
  bicep build-params "$params" --stdout >/dev/null
done
```

The Bicep CLI is pinned by version and SHA-256 in `.github/workflows/ci.yaml`
(the same pin the platform facts record). Modules restore from
`mcr.microsoft.com` at compile time; `tests/unit/deployment/test_azure_container_apps_bundle.py`
compiles the templates and asserts on the **compiled ARM JSON** resolved
against each parameter file, never on Bicep text.

## Parameters the operator must decide

- `composerTransportIdleCeilingSeconds` — required, no default, at most 240:
  the Container Apps ingress request timeout is a fixed 240 seconds; a Front
  Door or other hop in front lowers it further.
- `image` — the registry reference **by digest**, a digest-preserving copy of
  the GitHub Container Registry image (two builds never share a digest).
- `candidateSourceSha` — the full source commit bound to that image, used by
  membership and operator telemetry. Revision suffixes use `r<sha12>` so a
  hexadecimal SHA beginning with a digit remains a valid Azure suffix.
- `provisionStorageImage` — a digest-pinned root image; the runtime image is
  `USER 1654` and the platform offers no `runAsUser`.
- Every secret URL — a **versioned** Key Vault reference.

## Exclusions on the record (plan D4)

Document Intelligence, Scenario B/C, the `azure-otlp` telemetry mode, Entra
token authentication to PostgreSQL, and the ECS gate ledger / HMAC approvals.
