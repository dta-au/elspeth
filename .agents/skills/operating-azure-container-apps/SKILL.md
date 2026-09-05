---
name: operating-azure-container-apps
description: >
  Use when publishing, deploying, restarting, validating, testing, or
  diagnosing the ELSPETH web container on an existing Azure Container Apps
  environment. Covers digest-preserving registry publication, revision
  rollout in single revision mode, the doctor Jobs, health and readiness
  probes, NFS storage and Key Vault secret references, replica-aware
  diagnosis, targeted tests, and rollback decisions. Do not use for AWS ECS
  or VM deployments, or as permission to create a new Azure environment from
  scratch.
---

# Operating the ELSPETH Azure Container Apps container

Use this skill for the ordinary container loop:

`code -> targeted tests -> GHCR digest -> registry copy -> doctor Job -> revision -> live checks`

Keep it operational. Image digests, revision identity, database
compatibility, the managed identity and runtime evidence protect real
artifacts and behaviour. The platform facts every command relies on are
measured in
`docs/plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md`; the
procedures are `docs/runbooks/azure-container-apps-cold-install.md`,
`docs/runbooks/azure-container-apps-existing-service-redeploy.md` and
`docs/runbooks/azure-container-apps-deployment.md`.

> **Status.** Skeleton prepared by Phase 6b before the first live run. Until
> the sanitized receipt at
> `docs/operator/evidence/azure-container-apps/0.8.0.json` exists, this skill
> describes a program under acceptance, not a supported platform.

## Scope first

Choose one mode before doing anything:

- **Inspect/diagnose**: read-only `az` queries, probes and Log Analytics.
- **Publish only**: copy a tested GHCR digest into the registry and pin it.
- **Deploy existing app**: publish, run the doctor Job, roll a new revision,
  verify it.
- **Stop/resume**: scale to zero or back without deleting infrastructure.
- **Bootstrap/destroy environment**: out of scope unless the user explicitly
  asks; that is the cold-install runbook and the acceptance runbook.

Do not merge branches merely to deploy. Deploy from the worktree and commit
the user selected. ELSPETH worktrees symlink `.venv` to the main checkout, so
never install or sync dependencies from a worktree. Bind Python imports to
the selected worktree with an explicit `PYTHONPATH` covering `src` and
`elspeth-lints/src`, and execute the existing `.venv/bin/*` tools directly
(`.venv/bin/pytest`, `.venv/bin/python`). `az login` state lives under
`~/.azure` and is shared across worktrees.

## Rules that prevent the expensive mistakes

1. **Discover, do not remember.** Resolve the subscription, resource group,
   environment, app, active revision, replica names, identity, registry,
   Key Vault and workspace from live Azure state.
2. **One immutable identity, one digest.** The registry image is a
   digest-preserving copy of the GHCR image (`docker buildx imagetools
   create`); a second build never shares a digest. Deploy `@sha256:`, never a
   tag.
3. **Single revision mode is the rollout primitive.** The platform activates
   the candidate, waits for startup and readiness, shifts traffic and
   deprovisions the previous revision. Prove it with `revision list` and
   `replica list`; do not script your own traffic shift in production.
4. **Session affinity exists only in single revision mode.** Multiple mode
   (the acceptance's two labelled revisions) cannot carry `sticky`.
5. **Run the doctor Job before mutating the app.** `elspeth doctor
   deployment --json` is the deployment/schema gate; `--init-schema` only for
   a fresh `MISSING` schema with the schema-owner URLs; `STALE` is a stop.
6. **Treat readiness and liveness differently.** `/api/health` is liveness
   (and the startup probe); `/api/ready` is the traffic gate; both must be
   exact HTTP 200 and readiness must report `ready: true`.
7. **Storage contract, stated once.** Both databases on Azure Database for
   PostgreSQL Flexible Server; `data`, `data/blobs`, `payloads` on one NFS 4.1
   Azure Files share at `/mnt/elspeth`; SMB is not supported; no SQLite at
   replicas > 1. **Azure Files carries no database.**
8. **The transport ceiling is bound below 240 s.** The ingress request
   timeout is fixed; `ELSPETH_WEB__COMPOSER_TRANSPORT_IDLE_CEILING_SECONDS` is
   a required parameter with no default and is the minimum across every hop.
9. **Never bake credentials into the image or the template.** Secrets are
   versioned Key Vault references resolved by the user-assigned identity;
   never print a value or a resolved reference.
10. **Rollback is conditional.** Only when the compatibility record says
    `rollback_permitted: true`; a Scenario A install says `false`, so repair
    forward.
11. **Do not run the full suite by reflex.** Run the tests covering the
    changed surface; the full suite runs once when the body of work is done.

## Normal deploy workflow

Load [the command cheat sheet](references/command-cheatsheet.md) and follow
it in order.

### 1. Establish live identity and inventory

- Require explicit `AZURE_SUBSCRIPTION_ID`, `RESOURCE_GROUP` and
  `CONTAINER_APP`; run `az account show` first and ask the human to run
  `az login` when the session has expired.
- Capture the active revision name and image digest as
  `PREVIOUS_REVISION` / `PREVIOUS_IMAGE` before changing anything.
- Require exactly one active revision at 100 % with all replicas `Running`.

### 2. Select verification from the change

Use [the test and triage matrix](references/test-and-triage.md). The default
container lane is the deployment/doctor/readiness unit tests, the
runbook-contract test, the bundle test, and the focused PostgreSQL
testcontainer files for doctor, schema, startup and readiness. Run them with
`PYTHONPATH` bound to the worktree and `.venv/bin/pytest` executed directly.

### 3. Publish by digest

- Resolve the GHCR digest for the exact commit.
- Copy it into the registry with `docker buildx imagetools create` and assert
  `az acr manifest show-metadata` returns the same digest.
- `cosign verify` the registry reference against the GHCR-signed identity.

### 4. Run the doctor Job with the candidate digest

Point the `doctor-runtime` Job at the candidate digest, start it, and require
`Succeeded`. Classify a failure first: contract/config, Key Vault reference
or version, PostgreSQL connectivity or TLS, schema state, NFS mount or
ownership, identity role assignment (role assignments can take up to 24 h to
reach a cached token), or a missing image.

### 5. Roll the revision

`az containerapp update --image <digest> --revision-suffix <sha12>`; then
independently require one active revision at 100 % with the candidate image,
`N` replicas `Running`, HTTP 200 on both probes, an `X-Elspeth-Instance`
header and the expected `/api/system/status` facts.

### 6. Prove behaviour

At minimum the two probes, `/api/system/status`, an authenticated browser or
API flow appropriate to the change, and a console-log query by revision name
without a new unhandled startup or runtime failure (Log Analytics lags by
minutes).

## Diagnosis loop

1. Identify the failing layer: image copy, revision provisioning, Job
   execution, doctor, readiness, ingress, authentication, application run,
   or Azure API.
2. Capture the revision name, replica name, execution name, `runningState`,
   the system-log message for the revision, and a bounded console-log window.
3. Form one hypothesis and run the narrowest discriminating check.
4. Fix the cause, republish only if the image changed, and repeat from the
   earliest invalidated stage.

Common interpretations:

- `Provisioning failed` with `ErrImagePull`: the identity lacks `AcrPull` on
  this registry or the digest is absent; `ContainerCrashing`: read the console
  logs, then the doctor Job.
- A revision `Degraded`: at least one replica fails readiness; the platform
  restarts it up to the failure threshold.
- `mount.nfs: access denied by server while mounting` in the system log:
  encryption in transit is still required on the storage account, or the NSG
  blocks 2049.
- `409 Session operation is already active` from a second replica is the
  fence working, not a defect; correlate by session id and
  `X-Elspeth-Instance`.
- `503 /api/ready` is a dependency/readiness failure, not a liveness failure.
- A `504` after roughly four minutes is the ingress request timeout, not a
  platform failure of the deployment.

## Stop, resume, rollback, destroy

- **Stop:** `az containerapp update --min-replicas 0 --max-replicas 0`.
- **Resume:** restore the production scale settings and repeat the rollout
  proof.
- **Rollback:** only when permitted by the compatibility record; activate the
  previous revision, move 100 % of traffic, deactivate the candidate, repeat
  the checks.
- **Destroy:** delete the resource group that owns the environment (the
  acceptance runbook); never delete parts of the dependency graph by hand.

## Repository authority map

- `deploy/azure-container-apps/` — the Bicep bundle, parameter examples, KQL
  evidence queries and the acceptance driver.
- `docs/plans/2026-09-05-phase6b-azure-container-apps-platform-facts.md` —
  every platform literal, with provenance.
- `src/elspeth/web/deployment_contract.py` — the external-PostgreSQL target
  contract; `src/elspeth/web/doctor.py` — `doctor deployment`.
- `src/elspeth/web/key_derivation.py` and
  `tests/unit/web/test_key_derivation_wiring.py` — which rotations invalidate
  which derived keys.
- `tests/unit/web/test_azure_container_apps_runbook_contract.py` — the
  runbook and skill contract, including the epoch literals.
