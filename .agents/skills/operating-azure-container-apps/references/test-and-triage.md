# Test selection and failure triage

## Verification matrix

Choose the smallest set that can falsify the change. Combine rows when a
change crosses surfaces. Run every lane with `PYTHONPATH` bound to the
worktree's `src` and `elspeth-lints/src` and `.venv/bin/pytest` executed
directly; never sync into the shared `.venv` from a worktree.

| Changed surface | Before publish | After deploy |
|---|---|---|
| Dockerfile, lockfile, extras | deployment/startup unit lane; local image smoke | doctor Job; revision image digest; both probes |
| Deployment contract, doctor, readiness | `tests/unit/web/test_deployment_contract.py`, doctor and readiness tests; focused PostgreSQL testcontainers | doctor Job; revision/replica facts; both probes |
| Bicep bundle or parameters | `tests/unit/deployment/test_azure_container_apps_bundle.py` (compiled ARM, never Bicep text) | `what-if` shows only the intended change |
| Runbooks or this skill | `tests/unit/web/test_azure_container_apps_runbook_contract.py` | no container redeploy |
| Session or Landscape schema | affected unit/integration tests; PostgreSQL testcontainers | doctor read-only; `--init-schema` only for a fresh `MISSING` schema |
| Frontend | `npm ci`, typecheck, lint, frontend tests | authenticated browser flow |
| Azure Blob source/sink | targeted blob tests | the `verify-blob-managed-identity` Job |
| Auth/SSO | auth unit/frontend tests | fresh login, `/api/auth/me`, refresh, logout |
| Composer/tutorial | targeted composer/guided/tutorial tests | authenticated create/save/run flow |
| Config or secret reference only | validation/profile tests | new Key Vault secret version, new revision, doctor, affected live check |

The repository's default pytest options exclude `slow`, `stress`,
`performance` and `testcontainer`; `-m testcontainer` selects the container
lane explicitly.

## Failure ladder

### 1. Authentication or subscription

`az account show` fails or names the wrong subscription: the human runs
`az login`; never edit `~/.azure` around the failure.

### 2. Image copy

`imagetools create` fails: the GHCR digest for the commit does not exist
(CI did not publish) or `az acr login` expired. A digest mismatch after the
copy means something rebuilt instead of copying; stop.

### 3. Revision provisioning

`Provisioning failed` with `ErrImagePull`: the user-assigned identity lacks
`AcrPull` on this registry (role assignments can take up to 24 h to reach a
cached token) or the digest is absent. `ContainerCrashing`: read the console
logs, then run the doctor Job. `Timeout`: the startup budget (15 s × 10) is
too small for the SKU; raise CPU/memory before the period.

### 4. Doctor or startup

Classify from the `--json` report: `session_schema`/`landscape_schema`
(`MISSING` needs the schema-owner init Job; `STALE` is a compatibility
decision), `session_tls`/`landscape_tls` (`verify-full` hostname mismatch
through the private endpoint → `verify-ca`), `payload_store_writable`/
`blob_writable` (NFS ownership; the `provision-storage` Job runs as root
with `NoRootSquash`), or a Key Vault reference version that does not exist.

### 5. Readiness

`Degraded`: one replica fails `/api/ready`; the platform restarts it up to
the failure threshold. `503` is a dependency failure, not liveness.

### 6. HTTP and authentication

`401 /api/auth/me` before login is expected. A `504` after roughly four
minutes is the fixed ingress request timeout; the composer transport ceiling
must sit below it.

### 7. Replica behaviour

A `409 Session operation is already active` from the other replica is the
fence working. Correlate by session id and `X-Elspeth-Instance`; a repeated
409 after the lease (30 s) has expired points at the membership writer or
the orphan sweep.

## Rollback decision

Rollback only when all are true:

- the compatibility record says `rollback_permitted: true`;
- the failure is isolated to image/config;
- the previous revision's digest still exists in the registry; and
- the previous secret versions remain valid.

Otherwise fix forward. After rollback, repeat the revision, replica, probe and
identity checks.

## Full environment acceptance caveat

`docs/runbooks/azure-container-apps-deployment.md` is the disposable
replica > 1 acceptance program with its four probes and evidence
collection. It is not the everyday redeploy workflow, and until its
sanitized receipt exists it describes a program under acceptance.
