---
name: operating-aws-ecs-container
description: >
  Use when building, publishing, deploying, restarting, validating, testing,
  or diagnosing the ELSPETH web container on an existing AWS ECS/Fargate
  acceptance environment. Covers ECR image publication, task-definition
  revisioning, one-shot doctor checks, zero-overlap ECS rollout, health and
  readiness probes, S3/Bedrock checks, targeted tests, rollback decisions,
  and stopping or resuming the service. Do not use for Azure/VM deployments
  or as permission to create a new AWS environment from scratch.
---

# Operating the ELSPETH AWS ECS Container

Use this skill for the ordinary container loop:

`code -> targeted tests -> image -> ECR digest -> task definition -> doctor -> service -> live checks`

Keep it operational. Image digests, task-definition identity, database
compatibility, task roles, and runtime evidence protect real artifacts and
behavior. Document signatures, plan receipts, CI reenactments, and unrelated
gates do not help ship or diagnose this container.

## Scope first

Choose one mode before doing anything:

- **Inspect/diagnose**: read-only AWS queries, probes, and logs.
- **Build only**: run targeted tests and produce a local image.
- **Publish only**: push a tested image and resolve its immutable digest.
- **Deploy existing service**: publish, register a revision, run doctor, update
  the existing service, and verify it.
- **Stop/resume**: change desired count without deleting infrastructure.
- **Bootstrap/destroy environment**: out of scope unless the user explicitly
  asks. The tracked repository does not contain the Terraform package needed
  to recreate the acceptance environment.

Do not merge branches merely to build or deploy. Build from the worktree and
commit the user selected. AWS CLI login lives under `~/.aws`, so it is shared
across worktrees. ELSPETH worktrees symlink `.venv` to the main checkout, so
never install or sync dependencies from a worktree. Bind Python imports to the
selected worktree with an explicit `PYTHONPATH` and execute the existing
`.venv/bin/*` tools directly.

## Rules that prevent the expensive mistakes

1. **Discover, do not remember.** Resolve account, region, cluster, service,
   task definition, web container, target group, network configuration, CPU
   architecture, log group, and ECR repository from live AWS state.
2. **Build one immutable source identity.** Record the exact full Git SHA. Use
   a unique tag for transport, then deploy the ECR `repository@sha256:...`
   reference, never a mutable tag. Require the user-selected Git ref to resolve
   to `HEAD`; stop on a dirty tree unless the user explicitly accepts a dirty
   development image.
3. **Clone the live task definition narrowly.** Change the ELSPETH web image
   and its identity environment values. Preserve task/execution roles, EFS,
   secrets, plugin/profile settings, logging, health check, runtime platform,
   and every non-web container unchanged.
4. **Verify every referenced container image still exists.** A running task
   does not prove its sidecar digest can be pulled for a replacement task.
5. **Run doctor before service mutation.** `elspeth doctor aws-ecs --json` is
   the deployment/schema gate. Use `--init-schema` only for a fresh disposable
   database whose schema is reported `MISSING` and only when initialization is
   part of the requested work. `STALE` is a stop, not an automatic migration.
6. **Treat readiness and liveness differently.** Container liveness is
   `/api/health`; the ALB traffic gate is `/api/ready`. Both must return exact
   HTTP 200, and readiness must report `ready: true`.
7. **Prove the candidate task, not merely a stable service.** Check the primary
   deployment, task-definition ARN, running task count, task image digest,
   target health, public probes, and meaningful AWS integrations.
8. **Do not run the full suite by reflex.** Run the tests covering the changed
   surface. Run the full suite only when the user or release gate requests it,
   normally once after the body of work is complete.
9. **Never bake credentials or `.env` into the image.** Preserve ECS secret
   references or deliberately update Secrets Manager/task-definition config.
   Never print secret values.
10. **Rollback is conditional.** If the candidate changed either PostgreSQL
    schema, assume code rollback is unsafe until compatibility is proved.
    Prefer fix-forward. For an image-only failure with unchanged compatible
    schemas, the captured previous task definition is the rollback target.

## Normal deploy workflow

Load [the command cheat sheet](references/command-cheatsheet.md) and follow it
in order.

### 1. Establish live identity and inventory

- Require explicit `AWS_PROFILE` and `AWS_REGION`.
- Run STS first. If the login session expired, ask the human to run `aws login`
  for that profile; do not edit credential files around the failure.
- Discover the ECS cluster and service. Capture the current task definition as
  `PREVIOUS_TASK_DEFINITION` before changing anything.
- Require the existing service baseline to be desired/running/pending `1/1/0`
  with one completed primary deployment before preparing a replacement.
- Derive the web container name, target group, network configuration, runtime
  architecture, ECR repository, and log group from the service/task definition.
- If discovery is ambiguous, stop before mutation and show the choices.

### 2. Select verification from the change

Use [the test and triage matrix](references/test-and-triage.md). The default
container lane is:

- AWS ECS/startup/runbook unit tests;
- the five focused PostgreSQL testcontainer files for doctor, schema, startup,
  readiness, and the Landscape write gate;
- frontend checks only when frontend inputs changed; and
- AWS live tests only when their real integration changed or the user asks for
  live acceptance.

Do not sync or install into the worktree's symlinked `.venv`. Unset an inherited
`VIRTUAL_ENV`, bind `PYTHONPATH` to this checkout's `src` and
`elspeth-lints/src`, and execute `.venv/bin/pytest` directly. If a required
dependency is absent, stop and have the shared environment repaired from its
owning main checkout or use a genuinely separate environment.

### 3. Build and smoke the exact image

- Discover `linux/amd64` versus `linux/arm64` from the live task definition.
- Build from repository root with locked extras:
  `INSTALL_EXTRAS="webui llm aws postgres"`.
- Stamp `org.opencontainers.image.revision` with the full candidate Git SHA.
- Smoke `elspeth --version`, import `psycopg`, `boto3`, and the web package,
  and confirm the built SPA exists.
- If the source tree is dirty, state that fact before publishing. Do not claim
  the Git SHA alone identifies uncommitted content.

### 4. Push and resolve the digest

- Log Docker into the discovered ECR registry using the selected AWS profile.
- Push a unique tag.
- Resolve and validate the ECR digest.
- Set `CANDIDATE_IMAGE` to `repositoryUri@sha256:...`.
- Log Docker out when finished.

### 5. Register one narrow task-definition revision

- Start from the exact current service task definition.
- Strip only AWS response-only fields.
- Change only the named web container's image plus release/SHA/task-revision
  identity values.
- Compute the expected next family revision immediately before registration
  from the maximum revision across both `ACTIVE` and `INACTIVE` definitions.
  ECS does not reuse deregistered revision numbers, while resolving a family
  name returns only its latest active definition. If another registration wins
  the race, do not deploy the mismatched revision; deregister/rebuild the
  candidate: rediscover the task definition currently selected by the service,
  clone that definition again, then recompute the family-wide maximum. Do not
  clone an undeployed family revision merely because its number is newest.
- Compare all non-web container definitions and all task-level settings with
  the current definition using non-printing comparisons. Canonicalize
  environment and secret arrays by name before comparing the submitted and
  registered definitions because ECS may reorder these semantically unordered
  arrays. Check each digest-pinned sidecar is still pullable; a prose review is
  not enough.
- Confirm the registered web image equals `CANDIDATE_IMAGE` exactly.

Runtime profile/model/region changes are task-definition changes even when the
image is unchanged. Preserve the complete plugin-policy bundle for an
image-only deploy. Register and restart if any of these change:

- `ELSPETH_WEB__PLUGIN_ALLOWLIST`
- `ELSPETH_WEB__PLUGIN_PREFERENCES`
- `ELSPETH_WEB__PLUGIN_CONTROL_MODES`
- `ELSPETH_WEB__LLM_PROFILES`
- `ELSPETH_WEB__TUTORIAL_LLM_PROFILE`
- `ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES`
- `ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES`

### 6. Run the candidate as a one-shot doctor

Run the new task definition on the service network with the web container
command overridden to `doctor aws-ecs --json`. Wait for it to stop and require
exit code zero from the web container before touching the service.

When doctor fails, inspect the bounded CloudWatch window for that task and
classify the failure first: contract/config, secrets, PostgreSQL connectivity,
schema state, EFS permissions, task role, or missing image. Do not blindly
retry or initialize schemas.

### 7. Deploy with zero overlap

Update the existing service to the candidate task definition with desired
count one, forced deployment, circuit breaker enabled, minimum healthy zero,
and maximum healthy one hundred. This service intentionally accepts planned
downtime during replacement.

The waiter is only a wait primitive. After it returns, independently require:

- one `PRIMARY` deployment with `rolloutState == COMPLETED`;
- exact candidate task-definition ARN;
- desired/running/pending counts `1/1/0`;
- zero deployment failed tasks;
- exactly one running service task;
- the web container's reported image digest equals the ECR digest; and
- one healthy target for the service target group.

### 8. Prove behavior

At minimum:

- exact-200 `/api/health` with `{"status":"ok"}`;
- exact-200 `/api/ready` with `ready: true`;
- expected `/api/system/status` deployment/plugin-policy facts;
- an authenticated browser/API workflow appropriate to the change; and
- CloudWatch logs without a new unhandled startup/runtime failure.

For AWS capability changes, also run the applicable in-task checks:

- `verify-s3`
- `verify-bedrock`
- `verify-bedrock-guardrails`
- `verify-operator-telemetry`

These exercise the deployed task role and container configuration. A host-side
mock or local credential-chain test does not replace them. Before relying on
ECS Exec, require the service to have Exec enabled, the running task's command
agent to be `RUNNING`, and the Session Manager plugin to be installed. If that
path is unavailable, stop and use an owner-provided verifier task definition;
do not silently downgrade to host-side credentials.

## Diagnosis loop

Read [the test and triage matrix](references/test-and-triage.md), then follow:

1. Identify the failing layer: build, image pull, task start, doctor, service
   rollout, target health, HTTP, authentication, application run, or AWS API.
2. Capture the exact task ARN, task-definition ARN, image digest, service event,
   stopped reason/exit code, target-health reason, and bounded log window.
3. Form one hypothesis and run the narrowest discriminating check.
4. Fix the cause, rebuild only if code/image content changed, and repeat from
   the earliest invalidated stage.

Common interpretations:

- `401 /api/auth/me` before login is expected; after login it is an auth defect.
- `503 /api/ready` is a dependency/readiness failure, not a liveness failure.
- `504` on a long LLM/tutorial request requires server telemetry and task logs;
  it does not by itself prove the ECS deployment failed.
- A follow-up `409` may mean the first request is still running. Correlate by
  session/run ID before retrying.
- `CannotPullContainerError` after a forced deployment often means a referenced
  web or sidecar digest was deleted.

## Stop, resume, rollback, and destroy

- **Stop:** set service desired count to zero and wait for zero running tasks.
  This preserves ALB, EFS, databases, task definitions, and ECR images.
- **Resume:** select an exact task-definition ARN, set desired count to one,
  force a deployment, and repeat doctor/rollout/live verification as warranted.
- **Rollback:** only for schema-compatible image/config failures. Update to the
  captured `PREVIOUS_TASK_DEFINITION`, force deployment, and repeat all rollout
  checks.
- **Destroy:** use the Terraform state that owns the environment. Never select
  an old `~/.local/state/elspeth/aws-ecs/plan12-*` attempt merely because it is
  newest, and never manually delete a partial dependency graph as a shortcut.

## This acceptance install — discovered specifics (updated 2026-07-23)

Facts learned deploying the composer-parity merge to the live acceptance env.
Discover-don't-remember still applies, but these are the traps that cost time.

- **Accepted baseline (2026-07-23):** source commit
  `720d441336434d227c2a00caaac100db48a07d5c`, task definition
  `arn:aws:ecs:ap-southeast-1:559849758286:task-definition/a-4cb186732570bf935456-web:41`,
  image digest
  `sha256:61684a68d7752aa19f9ff402f4e91fbc9580c3c8cae1ae313682d52fa531ac53`.
  Treat this as a dated recovery breadcrumb, not a substitute for live
  discovery before the next operation.

- **Identity:** account `559849758286`, region `ap-southeast-1`, cluster
  `acceptance-a-4cb186732570bf935456-cluster`, service
  `acceptance-a-4cb186732570bf935456-service`, web
  container `elspeth-web`, arch `X86_64` (linux/amd64), ECR repo
  `elspeth-acceptance-9f088b9e1d1047a288234c690eb63141`. **Free-credit account:**
  no frontier Bedrock model access, so the composer runs on **OpenRouter**
  (`openrouter/anthropic/claude-sonnet-4-6`). `verify-s3` needs
  `ELSPETH_TEST_S3_BUCKET` (absent → opt-in). `verify-bedrock` reads
  `ELSPETH_BEDROCK_LIVE_TEST_MODEL`. Revision 41 currently sets it to
  `bedrock/zai.glm-5`, a Bedrock **Marketplace** model needing a subscription
  this account lacks, so that check fails on *access*, not transport (doctor
  `bedrock_provider` is still OK). For a temporary transport verifier, configure
  the one-shot verifier task to use `apac.amazon.nova-micro-v1:0`; do not mistake
  that recommendation for the current service configuration.

- **Bedrock `bedrock:InvokeModel` is RESOURCE-scoped on the task role**
  (`…-task-role`, inline policy `…-task-policy`). It grants InvokeModel only on:
  the `apac.amazon.nova-micro-v1:0` **inference profile** (ap-southeast-1),
  `amazon.nova-micro-v1:0` foundation-model in six APAC regions, and
  `zai.glm-5` (ap-northeast-1) — plus `bedrock:ApplyGuardrail`/`GetGuardrail`.
  An `AccessDeniedException` on `bedrock:InvokeModel` for any other model
  (e.g. Claude) is that resource scope, NOT a missing action. **Transport smoke
  (verified 2026-07-21):** a `bedrock-runtime.converse` on
  **`apac.amazon.nova-micro-v1:0`** returns a real response through the task role
  — the plumbing works. The **bare** foundation-model id `amazon.nova-micro-v1:0`
  returns AccessDenied: Nova on-demand must be invoked via the cross-region
  **inference profile**, not the plain model id. Use `apac.amazon.nova-micro-v1:0`
  as the Bedrock tech-test model here (cheap/available on free credits; frontier
  models are not).

- **Three-role database model** (Secrets Manager; keys `session_url` + `landscape_url`):
  `…-database-runtime` — the app's least-privilege role (owns nothing; the task
  injects it as `ELSPETH_WEB__SESSION_DB_URL`/`LANDSCAPE_URL` via
  `<runtime-arn>:session_url::`); `…-database-schema` — **owns all tables**, used
  for DDL/init; `…-database-bootstrap` — master. Two Postgres DBs: `elspeth_session`
  (17 tables), `elspeth_landscape` (40 tables).

- **STALE normally blocks automatic deployment.** For this disposable pre-1.0
  acceptance install only, an explicit owner request to destroy and rebuild the
  database activates the reset procedure below. That is a destructive reset,
  not an automatic migration and not permission inferred from a `STALE` result.
  `doctor --init-schema` connects as the **runtime** role,
  which cannot reset a STALE schema (init only repairs `MISSING`; runtime owns no
  objects and can't `DROP SCHEMA`). To reset: register a throwaway web-only
  task-def with the DB-URL secrets repointed to the **`…-database-schema`** ARN
  and `entryPoint:["python","-c"]`, then drop **all** `_schema`-owned public
  objects filtered by `owner=current_user`, `CASCADE`, in order: tables →
  **ROUTINES** → sequences → enum types → (mat)views. **Gotcha:** dropping tables
  alone leaves the `elspeth_chat_messages_immutable_content` function →
  `DuplicateFunction` on re-init — you MUST drop routines. Then run
  `doctor --init-schema` **as `_schema`** (same repointed task-def) to rebuild at
  the current epoch, and confirm with a read-only `doctor` **as runtime**
  (schemas → `CURRENT`; default-priv grants flow to runtime automatically).
  `run-task` cannot override `entryPoint` — a throwaway task-def is the only way
  to run `python -c` DB surgery in-container.

- **The cloudwatch-agent sidecar is fragile.** It is `essential:false`, pinned
  to an ECR digest the lifecycle policy (`Expire temporary acceptance images`,
  tag prefix `acceptance-`, 1 day) evicts — so a *fresh* task can't pull it
  (`CannotPull`) even while the running task is healthy. The official agent
  images (public-ECR **and** DockerHub) are now **distroless (no `/bin/sh`)**, but
  the sidecar's entrypoint is a `/bin/sh` config-wrapper, so they are not a
  drop-in. Either rebuild a shell-bearing agent image (amazonlinux base + copied
  agent) or drop the non-essential sidecar (also strip the web container's
  `dependsOn: cloudwatch-agent HEALTHY`; the app degrades OTLP export to
  `127.0.0.1:4317` gracefully — expect repeating `Failed to export metrics …
  UNAVAILABLE` log noise until restored). Push any replacement under a
  **non-`acceptance-`** tag so the lifecycle policy doesn't re-evict it.

- **Session tokens are short-lived.** They expired mid-operation twice; a
  `run-task` launch survives, but the following `wait`/`describe`/`logs` fail on
  expiry. Refresh before long steps; state is resumable from live AWS + the
  captured task/def IDs.

- **ECS task-definition revision numbers include deregistered definitions.**
  This family had active revision 30 and inactive revisions 31–39; the next
  registration became revision 40, not 31. Query both `ACTIVE` and `INACTIVE`
  definitions and take the maximum revision immediately before registration.
  ECS also reordered the web container's environment array on registration;
  preserve existing key positions when updating values and sort environment
  and secret arrays by name for the non-printing semantic comparison.

## Repository authority map

- `Dockerfile` — image build, extras, frontend bundling, runtime user/entrypoint.
- `.dockerignore` — build-context and secret exclusions.
- `pyproject.toml` / `uv.lock` — locked Python/test dependencies.
- `docs/operator/aws-ecs-health-and-readiness.md` — probe semantics.
- `src/elspeth/web/aws_ecs_startup.py` — startup and schema gate.
- `src/elspeth/web/aws_ecs_acceptance.py` — deployed acceptance checks.
- `docs/runbooks/aws-ecs-deployment.md` — exhaustive disposable-environment
  acceptance, not the everyday redeploy command sheet.

Before using the exhaustive runbook for a new environment, reconcile every
hard-coded schema epoch with the live constants. It currently contains stale
epoch prose/commands and is not safe to execute mechanically from top to
bottom. The Terraform package it describes is not tracked in this repository.
