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

> **Status.** Prepared before the first live run. Until the sanitized receipt
> at `docs/operator/evidence/azure-container-apps/0.8.0.json` exists, this
> bundle is a program under acceptance, not a support claim.

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
| `environment.bicep` | resource group | VNet (delegated infrastructure subnet + private-endpoint subnet, NSG allowing 445/2049), Log Analytics, user-assigned identity, four private DNS zones, Premium FileStorage account with the NFS share (`NoRootSquash`, encryption in transit off), StorageV2 account with the payload blob container (identity is Blob Data Contributor on that container only), Key Vault (RBAC, Secrets User for the identity), Flexible Server (password auth, both databases, private endpoint, optional operator firewall rule), the Container Apps environment (Log Analytics destination, NFS storage definition), and `AcrPull` on the **existing** registry |
| `modules/registry-pull-role.bicep` | registry's resource group | the `AcrPull` assignment on the existing registry |
| `workload.bicep` | resource group | the `elspeth-web` app (digest-pinned image, Key Vault secret references, NFS volume, startup/liveness/readiness probes, session affinity, scale, grace period) and the manual Jobs `provision-storage` (root image), `doctor-schema-init` (schema-owner URLs, `doctor deployment --init-schema --json`) and `doctor-runtime[-a|-b]` (runtime URLs, `doctor deployment --json`) |
| `main.example.bicepparam` / `environment.example.bicepparam` | | production stack parameters |
| `main.acceptance.bicepparam` | | disposable acceptance group: zone redundancy off, purge protection off, Burstable server with public access + operator firewall rule, 30-day retention |
| `workload.production.bicepparam` | | `Single` mode, `sticky` affinity, 2–4 replicas, ceiling 210 s |
| `workload.acceptance.bicepparam` | | `Multiple` mode, `none` affinity, 1 replica, `runtimeRoleLabel` a (deploy again with b) |
| `kql/*.kql` | | doctor report by execution; run sentinel by replica; replica lifecycle; fence-conflict 409s — SHA-256 bound into the receipt; column names verified live, never pinned by a test |
| `scripts/acceptance.sh` | | the stage driver (group → image copy → Jobs → rollout → probes → evidence → cleanup) |

The `verify-blob-managed-identity` Job and the probe/receipt facade land with
the acceptance package (6b-5); the driver's `probes` stage stops with a static
class until then.

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
- `provisionStorageImage` — a digest-pinned root image; the runtime image is
  `USER 1654` and the platform offers no `runAsUser`.
- Every secret URL — a **versioned** Key Vault reference.

## Exclusions on the record (plan D4)

Document Intelligence, Scenario B/C, the `azure-otlp` telemetry mode, Entra
token authentication to PostgreSQL, and the ECS gate ledger / HMAC approvals.
