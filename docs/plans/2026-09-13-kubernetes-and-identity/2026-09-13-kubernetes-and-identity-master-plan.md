# Kubernetes (multi-replica) and Identity Workflow — Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the last two structural building blocks of ELSPETH: a maintained
multi-replica Kubernetes deployment target (provider-neutral base proven on
kind, AKS overlay on top) and the workflow half of the identity sprint
(approvals, reviewer attestations, shared library, per-person quotas,
compartment marking, mailbox and admin UI). With both landed the superstructure
is feature complete; remaining work is gaps and bugs, not new load-bearing
capability.

**Architecture:** Two independent workstreams that share nothing but the
session schema epoch. Workstream K adds a sixth deployment surface that plugs
into the existing `DeploymentStartupProfile` registry, membership authority,
external-state startup contract and the provider-neutral replica probes; the
multi-replica bar it targets is the one ACA already ships (external PostgreSQL
for both stores, one RWX POSIX share, platform-stamped replica identity,
sticky ingress until no-affinity is qualified, SIGTERM drain). Workstream I
finishes the identity sprint on the tables that already exist in
`sessions/models.py`: every new mutation gets one owned authority module under
`web/coordination/`, runs inside the existing fenced transaction, writes its
`auth_events` row before responding, and every numbered refusal ships with a
fire test and a mutation-derivation test.

**Tech Stack:** Python 3.12+ (3.13 in the image and as the CI primary), FastAPI, SQLAlchemy, Pydantic, pytest +
testcontainers (PostgreSQL 16), Kustomize via `kubectl kustomize`, kind,
GitHub Actions, TypeScript/React/vitest for the frontend, Azure Files NFS CSI
and Key Vault CSI for the AKS overlay.

**Spec:** Workstream K argues from
[docs/plans/2026-09-04-release-0.8.0-phase6b-azure-container-apps-plan.md](../2026-09-04-release-0.8.0-phase6b-azure-container-apps-plan.md)
§3.5 Rollout and §4 Storage and multi-replica contract (the provider-neutral
multi-replica bar), applied to Kubernetes. The July design
[docs/specs/2026-07-26-finish-deferred-deployment-platforms-design.md](../../specs/2026-07-26-finish-deferred-deployment-platforms-design.md)
§Kubernetes Bundle still specifies `strategy: Recreate` and `replicas: 1` on
HEAD and its §Non-Goals still excludes multiple steady-state replicas
(measured 2026-09-13, lines 928-946 and 1564): on that point it is
**superseded** by the Phase 6b bar and the 2026-09-06 operator ruling, and
Task K8 rewrites both sections so the spec matches what ships. Workstream I
argues from
[docs/specs/2026-09-02-pluggable-sso-design.md](../../specs/2026-09-02-pluggable-sso-design.md)
§Refusals, §Frontend, §Testing → Workflow governance and §Workflow tables.

**Review record:** this plan was reviewed on 2026-09-13 on five lenses
(reality, architecture, test strategy, spec coverage K, spec coverage I) with
every finding adversarially verified; 90 findings were confirmed and applied. On 2026-09-14 every task was rewritten to step level and reviewed a second time: two verifiers raised 42 defects, a recheck found 17 residual cross-block items, and a final pass closed them.
The decisions those findings forced are listed under Self-review notes.

## Global Constraints

- Every shipped web profile runs one uvicorn process per replica: `WEB_CONCURRENCY=1`.
- Replicas > 1 require external PostgreSQL for both stores; `sqlite-single` is refused at config time for the `kubernetes` target (`deployment_contract.py:77-79`; the target set is `:28`).
- Two PostgreSQL roles per store: a schema-owner role used only by the schema-init Job, and a DDL-less runtime role used by the web pods (Phase 6b §4 row 1). They live in two Secrets; no manifest gives the web pod the schema-owner credentials.
- Files (`data/`, `data/blobs`, `payloads/`) live on one RWX share with POSIX rename atomicity; NFS 4.1 qualifies, SMB does not.
- Container runs as UID/GID 1654 (`Dockerfile:125`), listens on 8451, probes `/api/health` (liveness) and `/api/ready` (readiness).
- Session affinity stays ON until Task K7 qualifies routing without affinity; the base never claims horizontal scale.
- No secret value in a tracked file; manifests reference Secret keys only. Example Secret files are excluded from every `kustomization.yaml`.
- `DeploymentStartupProfile` is a frozen dataclass of facts; `app.py` never branches on the target vocabulary.
- Identity mutations lock the participating `identities` rows `FOR UPDATE`, evaluate cross-row rules in the same transaction, and write `auth_events` before responding (R4).
- Every workflow FK on `sessions` declares `ondelete="RESTRICT"`; `library_entries.published_from_session_id` is a provenance column, not an FK.
- Notes (`request_note`, decision `note`, `rejection_note`) are bounded plain text, 4 KiB, rendered as text.
- Schema changes for Workstream I land in ONE sessions epoch bump (Task I0, 56 → 57, `sessions/models.py:333`); no later task may add a column, a CHECK, or a NOT NULL constraint, and no task bumps the Landscape epoch (40).
- `auth_events` is a Landscape table (`core/landscape/schema.py:2542`). Sessions-side authorities never open the Landscape: they take a required `record` callback (`identity_authority.py:15-38`) whose writer is `AuthAuditWriter` in `web/auth/audit.py`.
- Every new production writer of a sessions table is admitted through the fail-closed mutation-authority manifest (`tests/unit/architecture/test_session_db_mutation_authority.py`, `_TABLE_POLICIES` :99-121, `_REVIEWED_WRITERS`, gate :18316); the task that adds the writer edits the manifest.
- Workflow-governance tests run against a closed local deployment (`registration_mode` not `open`) with `workflow_governance="on"` (Task I8) because R11 refuses enforcement otherwise; I8 therefore precedes every task that reads the switch.
- Run every suite to a file and read the exit code (`AGENTS.md` § Test Verification Policy); use `scripts/full-suite-gate.sh --execute --detach` before merging a task branch.
- Commit by file pathspec with the message BEFORE the separator: `git commit -m "<msg>" -- <file> <file>` (shared checkout rule; `git commit -- <files> -m ...` aborts because everything after `--` is a pathspec, and a directory pathspec sweeps a sibling lane's edits in).
- K and I share exactly two files: `CHANGELOG.md` (each workstream appends its own lines under the `## 0.8.1` heading and rebases) and `docs/runbooks/staging-session-db-recreation.md` (I11 creates the `### Cutover by deployment shape` table; K8 appends its Kubernetes row, so K8 runs after I11). Which release section receives them is a decision to confirm with the operator before the first commit (the top heading is `## 0.8.1 - 2026-09-10 (Replica recovery and deployment hardening)` and no 0.8.1 tag exists).
- Line citations were measured on 072141b75 and 818d04577. Five files they cite had uncommitted edits from a concurrent session when the plan was written: `src/elspeth/web/app.py`, `src/elspeth/web/sessions/service.py`, `src/elspeth/web/execution/service.py`, `src/elspeth/web/execution/routes.py` and `tests/unit/architecture/test_session_db_mutation_authority.py`. Re-measure every `path:line` into those files, and the mutation-authority gate's XFAIL counts, before executing I1, I2 or I3.
- Kubernetes tool binaries: `kubectl` and `kind` are absent on the development box and on the CI `test` runner. Tests that need `kubectl` use the `_require_kubectl` skip-locally/fail-in-CI pattern (`ELSPETH_CI_KUBECTL_REQUIRED`); tests that need a cluster carry the `kind` marker, which the default and Testcontainer selections exclude and only the `kubernetes-kind` job selects.

---

## Workstream layout and ordering

```text
K0 spike facts ──▶ K1 base manifests ──▶ K2 profile arm ──▶ K3 render gate ──▶ K4 kind harness ──▶ K5 acceptance probes ──▶ K7 no-affinity ──▶ K8 docs flip
                   └──▶ K6 AKS overlay ─────────────────────────────────────────────────────────────────────────────────┘

I0 schema pass ──▶ I8 governance switch + R11 ──▶ I1 token ledger + R14 ──▶ I2 storage quota R13 ──▶ I3 approvals + R2 ──▶ I4 reviews ──┐
                                                                                                     └──────────────▶ I5 library ─┴─▶ I6 compartment marking ──▶ I7 scoped reads ──▶ I9 frontend ──▶ I10 governance suite ──▶ I11 cutover
                                             └───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────▶ I10 (closed_local_app)
```

K and I share only `CHANGELOG.md` and `docs/runbooks/staging-session-db-recreation.md`, and K8 runs after I11 because it appends a row to the cutover table I11 creates. Inside K, K6 runs in
parallel with K5 and K7 follows both (K7 edits K6's ingress annotations).
Inside I the order is I0 → I8 → I1 → I2 → I3 → {I4 ∥ I5} → I6 → I7 → I9 →
I10 → I11: I8 first because I3's R2 gate, I5's publish and every governance
test read `workflow_governance`; I2 after I1 because it consumes
`RepositoryQuotaAuthority.active_policy` and the I1 fixtures; I6 after I5
because it pins and extends the compartment stamping I5's publish path writes.

## Task files

Each task lives in its own file in this directory. Hand an implementer the task file together with
the Global Constraints above; when a task cites a step in another task, open that task's file from
the table below. The plan was one 39,604-line document until 2026-09-14 and was split because a
single file exceeded the repository's 1,000 KB commit limit. The split added a one-line navigation
preamble under each task heading and changed K0's two references to the old path; the task text is
otherwise unchanged. The subagent-driven-development `task-brief` script only extracts numeric task
headings (`Task 3`), so dispatch with the task file path instead.

Decisions taken while the plan was written are stated in the task text that relies on them. Where
a task mentions an "earlier working decision" or an "earlier form", it describes that form in place;
the working note itself is not published.

Rows are in execution order within each workstream.

| Task | File | Runs after |
| --- | --- | --- |
| K0: Spike — measure the platform facts the manifests depend on | [K0-platform-facts-spike.md](K0-platform-facts-spike.md) | — |
| K1: Provider-neutral Kustomize base at the multi-replica bar | [K1-kustomize-base.md](K1-kustomize-base.md) | K0 |
| K2: A real startup profile for the `kubernetes` target | [K2-startup-profile.md](K2-startup-profile.md) | K1 |
| K3: Checksum-pinned render gate in CI | [K3-render-gate.md](K3-render-gate.md) | K2 |
| K4: kind harness — two replicas, shared RWX, real PostgreSQL, provider-free run | [K4-kind-harness.md](K4-kind-harness.md) | K3 |
| K5: Acceptance probes — the four replica probes on Kubernetes | [K5-acceptance-probes.md](K5-acceptance-probes.md) | K4 |
| K6: AKS overlay | [K6-aks-overlay.md](K6-aks-overlay.md) | K1 |
| K7: Qualify routing without affinity | [K7-no-affinity-routing.md](K7-no-affinity-routing.md) | K5, K6 |
| K8: Runbook and public-claim flip | [K8-runbook-and-claim-flip.md](K8-runbook-and-claim-flip.md) | K7, I11 |
| I0: One schema pass: token ledger nullability | [I0-ledger-nullability.md](I0-ledger-nullability.md) | — |
| I8: The enforcement switch and the R11 startup refusal | [I8-governance-switch.md](I8-governance-switch.md) | I0 |
| I1: Token usage ledger and R14 daily enforcement | [I1-token-ledger-quota.md](I1-token-ledger-quota.md) | I8 |
| I2: Storage quota R13 at every byte-admitting site | [I2-storage-quota.md](I2-storage-quota.md) | I1 |
| I3: Approvals — request, decide, withdraw, supersede, and the R2 execute gate | [I3-approvals.md](I3-approvals.md) | I2 |
| I4: Review requests and reviewer attestations | [I4-reviews.md](I4-reviews.md) | I3 |
| I5: Shared library — publish, curate, browse, fork | [I5-shared-library.md](I5-shared-library.md) | I3 |
| I6: Compartment marking | [I6-compartment-marking.md](I6-compartment-marking.md) | I4, I5 |
| I7: Scoped reads — workflow inspect, approver audit view, delegated administration | [I7-scoped-reads.md](I7-scoped-reads.md) | I6 |
| I9: Frontend — mailbox, completion bar, readiness row, admin UI, library, quota status | [I9-frontend.md](I9-frontend.md) | I7 |
| I10: Workflow-governance suite (fire + mutation per refusal) | [I10-governance-suite.md](I10-governance-suite.md) | I8, I9 |
| I11: Cutover mechanics — the operator's instructions for the workflow-epoch window | [I11-cutover.md](I11-cutover.md) | I10 |

---

## Self-review notes

- **Spec coverage, K:** Kubernetes Bundle section → K1; render CI → K3; kind smoke → K4; the ACA §4 storage contract → K1 PVC + K6 storage class; §3.5 identity/probes/drain → K1 + K2; replica probes → K5; affinity qualification → K7; docs → K8. AKS Key Vault refs, Flexible Server and Azure Files CSI RWX from the 09-06 ruling → K6.
- **Spec coverage, I:** R2 → I3; R7/R8/R9 already ship (fire+mutation in I10); R11 → I8; R13 → I2 (including "refuse when the accounting query fails"); R14 → I1; §Frontend mailbox/admin/quota/library, the identity-row quota usage, the blob-list identity total and the single-admin advisory → I9; §Workflow tables → already on HEAD at epoch 55/56 except the ledger nullability (I0); the five compartment stampings and the ingress record → I6; delegated admin, audit view, `workflow_inspect` → I7; §Testing → I10; §Rollout 6 → I11 (the OIDC harness rewrite landed 2026-09-05, 851a15dfb, and is not re-planned).
- **Type consistency:** `ReplicaAddress(name, origin)` as defined at `replica_probes.py:487-489`; `ProbePod` is new and only in K5; `RoleRevocationPartition.partition(role) -> PartitionRecord` is the moved ACA class; `ApprovalBinding` fields match the `binding_json` keys the spec table names and are built by `build_approval_binding(...)` with the two Landscape-side shas passed explicitly; `AdmissionRefusalReason.APPROVAL_REQUIRED` / `APPROVAL_BINDING_MISMATCH` / `QUOTA_EXCEEDED` and `QuotaDisposition.EXCEEDED` / `WITHIN_CAP` are new members of `contracts/chargeable_admission.py`; the fixtures `fenced_session` and `fenced_session_with_policy` are defined in I1's `tests/unit/web/coordination/conftest.py` over the dataclasses in `tests/helpers/fenced_session.py`, `pg_fenced` in `tests/testcontainer/web/conftest.py` (I1), `closed_local_app` in I8; no task defines or consumes a `fenced_execute` fixture. The 413 storage-quota body (`error_type` `storage_quota_exceeded` with `dimension`, `cap`, `ceiling`, `usage`) is emitted by I2 and decoded by I9 with the same fields. The sessions mutation-authority gate XFAILs on a clean HEAD (measured 2026-09-14 on a `git archive` export of 818d04577); I1, I3 and I10 all compare against that baseline rather than expect a pass.
- **Decisions the 2026-09-13 review forced (each is a check for the operator, not a verdict):**
  1. I0 shrank to the one schema change the sprint really needs, ledger nullability, because every other column, CHECK and partial index it planned is already on HEAD at epoch 55/56 (measured). The alternative, no bump at all with `record()` refusing unknown usage, would make the day total treat unknown usage as zero, which the spec forbids.
  2. I6's ingress record lives in the composition state's `composer_meta` JSON, not in a new `auth_events` type, so no Landscape epoch bump happens in this sprint. An `auth_events` row instead would be a Landscape bump riding the same cutover window.
  3. K5 no longer ships a Kubernetes receipt family, an acceptance facade, or a `CLOUD_PROVIDERS` widening. No spec or ruling asked for them, the milestone scopes kind acceptance to identity, health/readiness, persistence, configuration and teardown, and a third provider is the recorded trigger for the deferred receipt-v3 consolidation. The four replica probes run as pytest outcomes in the kind lane.
  4. P3 (role revocation) needs one role per pod, so K5 adds an acceptance overlay with two one-replica Deployments and their own runtime Secrets, mirroring ACA's rA/rB revisions. The K4 overlay (one Deployment, two replicas) stays as the shared-state proof.
  5. I8 moved to directly after I0: I3, I5 and every governance test read the switch it creates.
  6. The CHANGELOG section for both workstreams is a decision to confirm with the operator (see Global Constraints).
- **Operator decisions still open (raised by the task authors, 2026-09-14; each task states the default it took):**
  - **K5:** the P4a probe needs a composer provider in the kind lane. Either the `kubernetes-kind` job gets a provider secret, or P4a gives no evidence in CI.
  - **K5:** the acceptance controller takes `replicas: tuple[ProbePod, ProbePod]` with `ProbePod(address, deployment, role)`, the shape of ACA's `ProbeReplica`, instead of a `deployments=` mapping. Confirm that shape, or require `deployments=` with each replica's origin and role derived inside the controller.
  - **K6:** the application-routing NGINX ingress is supported only through November 2026. Ship it for 0.8.1, or choose the Gateway API implementation or Application Gateway for Containers now.
  - **K6:** Key Vault CSI identity: the add-on's user-assigned managed identity (the task's default), or Workload ID with two ServiceAccounts mirroring ACA's two-identity split.
  - **K6:** keep the Azure Load Balancer's 4-minute idle default (230 s transport ceiling, 180 s composer timeout), or raise it and move both pinned values.
  - **K6:** TLS for `elspeth-web-tls`: cert-manager, or a Key Vault certificate synced by a third SecretProviderClass.
  - **Adjacent finding:** the ACA bundle sets none of the required composer settings (`config.py:306-309`), so an ACA pod refuses to boot unless the operator's parameter file adds them. The Kubernetes base now carries them. This is an ACA defect for its own ticket, not a task here.
  - **I2:** when the identity storage cap refuses a composer `create_blob` call, the tool message still says "Session blob quota exceeded" (with correct byte counts). Keep that wording, or reword it to name the identity cap.
  - **I3:** a superseded approval writes no `auth_events` row, because supersede runs below the audit seam. Accept, or thread a `record` callback through every composition-state writer.
  - **I3:** the binding's `config_hash` is the web envelope hash, not Landscape `runs.config_hash`. The spec's workflow-tables row should say which.
  - **I3:** compiling a binding on the approval-request route freezes the runtime-VAL registries as a side effect (idempotent, and what the first run does anyway).
  - **I6:** signed Landscape run exports are not stamped with `compartment_id`, because `public_config` is a closed hashed contract. Take a derivation-version bump, or wait for the next Landscape window.
  - **I6:** ingress is recorded only on YAML seed paths (paste import, library fork), not on chat compose turns. Confirm that reading of "user-pasted text".
  - **I6:** the audit proxy raises the soft-mapping census by 8, and the AWS acceptance compartments change from A/B/C to a/b/c under the new validator. Accept both, or type auth metadata first.
  - **I7:** inspect admits only the addressed approver or reviewer on an open request, and decision ends access. The spec read literally admits any active approver on any live row. Also confirm the approver audit view needs no `audit_access_log` row, and that delegated curator grants may be an authority arm, direct edge only.
  - **I9:** the approver-picker and quota routes are added in I9. Quota policies are editable by admins only, not oversight. The approver audit view has no frontend. Request approval and Request review live on the Audit panel, not the pinned completion bar.
  - **I10:** the suite is a fail-closed inventory of the fire and mutation pairs I1-I9 already own, plus the gap tests, rather than a re-typing of every pair.
  - **I11:** which task closes ticket `elspeth-5ef01c6ad1` (epoch fan-out in I0, runbook split and operator notice in I11), or whether it should be split.
- **Not in this plan:** receipt v3 consolidation of the ECS and ACA methodologies (no third provider is added, so its trigger does not fire here), a Kubernetes acceptance receipt or facade, Scenario B/C on ACA, `azure-otlp`, Entra PostgreSQL auth, generated platform profiles, `required_count > 1` quorum, service identities (R10), a Landscape epoch bump (the `run_web_plugin_policy` additions on `elspeth-ff89d2bea0` ride whichever Landscape bump comes next), and email or push notification.

---

# Workstream K — Kubernetes at the multi-replica bar

### File structure (K)

An overview of the main files. Each task's **Files:** block is the authoritative and complete list.

```text
deploy/kubernetes/base/kustomization.yaml          resource inventory, `labels:` (kustomize v5; `commonLabels` is deprecated)
deploy/kubernetes/base/configmap.yaml              non-secret ELSPETH_WEB__* settings
deploy/kubernetes/base/secret.example.yaml         RUNTIME-role keys only, inert values, NOT in kustomization
deploy/kubernetes/base/secret-schema-owner.example.yaml  SCHEMA-OWNER-role URL keys, referenced only by job-schema-init, NOT in kustomization
deploy/kubernetes/base/bootstrap-roles.sql         schema-owner + runtime roles (mirrors deploy/azure-container-apps/scripts/bootstrap-roles.sql)
deploy/kubernetes/base/pvc.yaml                    ReadWriteMany claim for /mnt/elspeth
deploy/kubernetes/base/service.yaml                ClusterIP :8451 -> http, sessionAffinity ClientIP
deploy/kubernetes/base/deployment.yaml             replicas 2, RollingUpdate 1/0, downward-API identity, probes, drain
deploy/kubernetes/base/job-provision-storage.yaml  one-shot root Job: mkdir/chown/chmod the share subtree (ACA provision-storage pattern); ttlSecondsAfterFinished
deploy/kubernetes/base/job-schema-init.yaml        one-shot `elspeth doctor deployment --init-schema` as the schema-owner role; ttlSecondsAfterFinished
deploy/kubernetes/overlays/kind-test/              K4: one Deployment, replicas 2, NodePort 30451, placeholders patched (shared-state proof)
deploy/kubernetes/overlays/kind-acceptance/        K5: two one-replica Deployments elspeth-web-a|b, own Secret/role/Service each (P1-P4 run)
deploy/kubernetes/overlays/aks/kustomization.yaml  AKS overlay: storage class, KV CSI, ingress patch
deploy/kubernetes/overlays/aks/storageclass-azurefile-nfs.yaml
deploy/kubernetes/overlays/aks/secretproviderclass-web.yaml
deploy/kubernetes/overlays/aks/secretproviderclass-schema-owner.yaml
deploy/kubernetes/overlays/aks/ingress.yaml
deploy/kubernetes/overlays/aks/patch-deployment.yaml
src/elspeth/web/deployment_profiles.py             kubernetes arm becomes a real profile
src/elspeth/web/_acceptance_common/postgres_observer.py  SqlSession, SqlReader, RoleRevocationPartition, PostgresEvidenceObserver + SQL constants moved out of the ACA package (K5)
src/elspeth/web/_kubernetes_acceptance/__init__.py
src/elspeth/web/_kubernetes_acceptance/controller.py   KubernetesReplicaController + KubectlCommands (no facade, no receipts: see Self-review § Not in this plan)
pyproject.toml                                     `kind` marker; default and testcontainer selections exclude it
tests/unit/deployment/test_kubernetes_bundle.py    source-contract test over the rendered base (`_require_kubectl`)
tests/unit/web/test_deployment_profiles.py         kubernetes arm pins
tests/unit/web/kubernetes_acceptance/test_controller.py
tests/testcontainer/deployment/kubernetes/kind-config.yaml
tests/testcontainer/deployment/kubernetes/pv-rwx-hostpath.yaml
tests/testcontainer/deployment/kubernetes/postgresql.yaml
tests/testcontainer/deployment/kubernetes/no-affinity-overlay/kustomization.yaml
tests/testcontainer/deployment/test_kubernetes_kind.py          marker `kind`
tests/testcontainer/deployment/test_kubernetes_replica_probes.py  marker `kind`; P1-P4 as pytest outcomes
scripts/cicd/kubernetes-kind-smoke.sh
.github/workflows/ci.yaml                          kubectl pin in the test job; kubernetes-render + kubernetes-kind jobs; ci-success result checks
docs/plans/2026-09-13-kubernetes-platform-facts.md spike output (K0)
docs/runbooks/kubernetes-deployment.md
docs/reference/deployment-platforms.md             row + section flip
docs/guides/docker.md                              section flip
docs/specs/2026-07-26-finish-deferred-deployment-platforms-design.md  §Kubernetes Bundle and §Non-Goals rewritten to the multi-replica bar (K8)
```

---

# Workstream I — Identity workflow sprint

### File structure (I)

An overview of the main files. Each task's **Files:** block is the authoritative and complete list.

```text
src/elspeth/web/sessions/models.py                     I0: token_usage_ledger.prompt_tokens/completion_tokens nullable; SESSION_SCHEMA_EPOCH 56 -> 57 (:333) — the only schema change; everything else the sprint needs is on HEAD at epoch 55/56
src/elspeth/web/config.py                              I8: workflow_governance switch; I6: compartment_id validator (the field exists at :545)
src/elspeth/web/readiness.py                           I8: R11 refusal in _check_auth_mode (:315)
src/elspeth/web/coordination/quota_authority.py        I1: RepositoryQuotaAuthority — sole writer of token_usage_ledger, daily aggregate, active_policy; I2: admit_storage_bytes
src/elspeth/contracts/chargeable_admission.py          I1: QUOTA_EXCEEDED / EXCEEDED / WITHIN_CAP, evidence fields, schema_version 2
src/elspeth/web/coordination/chargeable_admission_authority.py  I1: R14 exhaustion arm
src/elspeth/web/auth/audit.py                          I1-I5: AuthAuditWriter.record_quota_exceeded / record_approval_* / record_review_* / record_library_* (auth_events is a Landscape table; sessions authorities reach it only through the record callback)
src/elspeth/web/blobs/service.py                       I2: the three call sites of _enforce_session_blob_quota call admit_storage_bytes
src/elspeth/web/coordination/approval_authority.py     I3: request / decide / withdraw / supersede_open / approved_binding / build_approval_binding
src/elspeth/web/coordination/run_start_permit_authority.py  I3: R2 gate inside _assess; assess()/issue() widened
src/elspeth/web/coordination/repository.py             I3: append_state (:793) supersedes open approvals; permit call sites (:1557/:1568) pass evidence + shas
src/elspeth/web/sessions/routes/workflow/__init__.py   I3: router package; each later task adds its module
src/elspeth/web/sessions/routes/workflow/approvals.py  I3: routes
src/elspeth/web/coordination/review_authority.py       I4: review_requests + review_attestations
src/elspeth/web/sessions/routes/workflow/reviews.py    I4: routes
src/elspeth/web/coordination/library_authority.py      I5: publish / curate / fork
src/elspeth/web/sessions/routes/workflow/library.py    I5: routes
src/elspeth/web/composer/yaml_generator.py             I6: metadata stamp
src/elspeth/web/sessions/routes/workflow/inspect.py    I7: workflow_inspect read + audit_access_log
src/elspeth/web/auth/identity_admin_routes.py          I7: delegated administration predicate
src/elspeth/web/sessions/routes/workflow/audit_view.py I7: approver audit view
src/elspeth/web/sessions/routes/workflow/mailbox.py    I9 backend: inbox/sent/summary
src/elspeth/web/frontend/src/components/mailbox/*      I9
src/elspeth/web/frontend/src/components/admin/*        I9
src/elspeth/web/frontend/src/components/library/*      I9
tests/helpers/fenced_session.py                        I1: FencedSession / FencedSessionWithPolicy dataclasses and seed_token_policies
tests/unit/web/coordination/conftest.py                I1: fenced_session, fenced_session_with_policy (I2, I3 reuse them)
tests/testcontainer/web/conftest.py                    I1: pg_fenced on the shared PostgreSQL container (I2 reuses it)
tests/unit/web/conftest.py                             I8: closed_local_app (I10 consumes it)
tests/unit/architecture/test_session_db_mutation_authority.py  I1-I5: manifest rows for every new sessions writer (fail-closed gate)
tests/integration/web/workflow/test_governance_refusals.py  I10
docs/runbooks/staging-session-db-recreation.md, aws-ecs-deployment.md  I11
```
