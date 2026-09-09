# Test selection and failure triage

## Verification matrix

Choose the smallest set that can falsify the change. Combine rows when a
change crosses surfaces.

| Changed surface | Before image build | After deploy |
|---|---|---|
| Dockerfile, lockfile, extras | ECS/startup unit lane; local image smoke | doctor; task digest; health; readiness |
| AWS startup/deployment contract | ECS/startup unit lane; focused PostgreSQL testcontainers | doctor; service/task/target facts; both probes |
| Session or Landscape schema | affected unit/integration tests; all five focused PostgreSQL testcontainers | doctor read-only; explicit initialization only for fresh `MISSING`; durable workflow |
| Frontend | `npm ci`, typecheck, lint, frontend tests | authenticated browser flow; console/network review |
| S3 source/sink | targeted S3 unit/integration tests | `verify-s3` or opt-in live S3 tests with disposable prefix |
| Bedrock LLM/profile/model | targeted profile/composer tests | `verify-bedrock`; confirm selected profile/model in system status and a real run |
| Bedrock Guardrails | targeted guardrail tests | `verify-bedrock-guardrails` with configured immutable versions |
| Telemetry | targeted telemetry tests | `verify-operator-telemetry`; CloudWatch signal and application behavior |
| Auth/OIDC | auth unit/frontend tests | fresh login, `/api/auth/me`, refresh, logout, re-login |
| Composer/tutorial | targeted composer/guided/tutorial tests | authenticated create/save/run flow; inspect run/session telemetry |
| Config or secret reference only | validation/profile tests; no image rebuild unless image changed | new task-definition revision, doctor, restart, affected live check |
| Documentation/skill only | syntax/link/skill validation | no container rebuild or full suite |

The repository's default pytest options exclude `slow`, `stress`,
`performance`, and `testcontainer`. Supplying `-m testcontainer` selects the
container lane. The live AWS tests are opt-in and can create temporary objects
or make billed service calls; run them only against the intended account.

## Optional host-side live AWS tests

These validate the host's default AWS credential chain and plugin behavior.
They complement, but do not replace, in-task role checks.

### S3

```bash
: "${ELSPETH_TEST_S3_BUCKET:?set a disposable acceptance bucket}"
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" \
ELSPETH_TEST_S3_BUCKET="$ELSPETH_TEST_S3_BUCKET" \
.venv/bin/pytest -q -m 'slow or integration' \
  tests/integration/plugins/sources/test_aws_s3_source_live.py \
  tests/integration/plugins/sinks/test_aws_s3_sink_live.py
```

The tests create UUID-scoped keys and delete them in `finally` blocks.

### Bedrock model

```bash
: "${ELSPETH_BEDROCK_LIVE_TEST_MODEL:?use a bedrock/model identifier available in the selected region}"
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" \
ELSPETH_RUN_BEDROCK_LIVE=1 \
ELSPETH_BEDROCK_LIVE_TEST_MODEL="$ELSPETH_BEDROCK_LIVE_TEST_MODEL" \
.venv/bin/pytest -q -m 'slow and integration' \
  tests/integration/web/composer/test_bedrock_live_smoke.py
```

Model availability comes from the operator's configured Bedrock profiles and
AWS account/region access. Do not infer it from a global provider catalogue.

### Bedrock Guardrails

Use only after the operator has supplied the documented profile aliases,
safe/blocked cases, and expected immutable versions:

```bash
ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS=1 \
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" \
.venv/bin/pytest -q -m live_aws \
  tests/integration/plugins/transforms/aws/test_bedrock_guardrails_live.py
```

## Failure ladder

Diagnose the first failing layer. Do not skip downward to application code
when ECS cannot pull or start the task.

### 1. Authentication or account

Symptoms: expired login, access denied, resources apparently absent.

Checks:

```bash
aws sts get-caller-identity --profile "$AWS_PROFILE" --region "$AWS_REGION"
aws configure get region --profile "$AWS_PROFILE"
```

Compare the returned account and selected region with the intended environment.
An expired login is repaired with `aws login`, not profile deletion.

### 2. Image build

Symptoms: dependency install failure, frontend build failure, missing module,
wrong architecture.

Checks:

- confirm build context is repository root;
- confirm `.dockerignore` did not exclude a runtime input;
- confirm `INSTALL_EXTRAS="webui llm aws postgres"`;
- run the local container import/SPA smoke; and
- inspect the image revision label and `Os/Architecture`.

If `psycopg` is missing locally, do not repair the worktree's symlinked shared
environment. Stop and have the environment repaired from its owning main
checkout, or use a genuinely separate environment. If it is missing in the
image, the build extras or lockfile are wrong.

### 3. Image pull

Symptoms: `CannotPullContainerError`, task stops before the process starts.

Checks:

```bash
aws ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'tasks[0].{stopCode:stopCode,stoppedReason:stoppedReason,containers:containers[].{name:name,reason:reason,image:image}}'
```

Verify the web digest and **every sidecar digest**. A digest used by an already
running task may have been deleted from its registry and fail only on restart.

### 4. Doctor or startup

Symptoms: doctor exit non-zero, task repeatedly stops, web socket never binds.

Get the web log group and stream prefix without printing its environment:

```bash
LOG_GROUP=$(jq -er --arg name "$WEB_CONTAINER_NAME" '
  .taskDefinition.containerDefinitions[]
  | select(.name == $name)
  | .logConfiguration.options["awslogs-group"]
' "$WORK/current-task-definition.json")
LOG_PREFIX=$(jq -er --arg name "$WEB_CONTAINER_NAME" '
  .taskDefinition.containerDefinitions[]
  | select(.name == $name)
  | .logConfiguration.options["awslogs-stream-prefix"]
' "$WORK/current-task-definition.json")
export LOG_GROUP LOG_PREFIX
```

Inspect a bounded recent window and avoid copying secret-bearing raw output
into tickets or chat:

```bash
aws logs tail "$LOG_GROUP" --since 10m --format short \
  --profile "$AWS_PROFILE" --region "$AWS_REGION"
```

Classify before acting:

- deployment contract/config missing;
- secret reference unavailable;
- PostgreSQL unreachable;
- schema `MISSING`, `STALE`, or incompatible;
- EFS mount/ownership/writability failure;
- AWS task-role denial; or
- application exception.

`MISSING` on a new disposable database may permit explicit `--init-schema`.
`STALE` never permits automatic `--init-schema`. An explicit owner request to
destroy and rebuild this disposable pre-1.0 acceptance database follows the
destructive reset procedure in the main skill; it is not a migration response
inferred from doctor output.

### 5. ECS rollout

Symptoms: waiter failure, deployment `FAILED`, running/pending mismatch.

```bash
aws ecs describe-services \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'services[0].{taskDefinition:taskDefinition,desired:desiredCount,running:runningCount,pending:pendingCount,deployments:deployments,events:events[0:10]}'

aws ecs list-tasks \
  --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
  --desired-status STOPPED \
  --profile "$AWS_PROFILE" --region "$AWS_REGION"
```

Inspect the newest stopped task's `stoppedReason` and essential-container exit
code. Do not keep forcing deployments of the same broken revision.

### 6. Target health

Symptoms: task is running but ALB reports unhealthy.

```bash
aws elbv2 describe-target-health \
  --target-group-arn "$TARGET_GROUP_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION"
```

Interpret the reason:

- response-code mismatch: inspect `/api/ready` body and dependency checks;
- timeout: inspect readiness dependency latency and target-group timeout;
- failed health checks: verify port, security groups, task IP, and startup grace.

The target group must use `/api/ready`, exact HTTP 200, and a timeout longer
than the endpoint's five-second ceiling. Container liveness uses `/api/health`.

### 7. HTTP and authentication

- `401 /api/auth/me` before login is normal.
- Repeated `401` after successful login means token/cookie/issuer/audience
  handling is broken.
- `ERR_HTTP2_PROTOCOL_ERROR` on a static asset points at ALB/TLS/proxy delivery,
  not the composer.
- A ready service with a failing authenticated API needs application/session
  diagnosis, not another ECS restart.

### 8. Long-running tutorial/composer request

For a `504` followed by `409`:

1. Record session ID, request start/end times, and any run ID.
2. Check whether the server-side operation continued after the client/ALB
   timeout.
3. Inspect bounded app logs and session/Landscape telemetry for that exact ID.
4. Distinguish model latency, network fetch latency, application timeout, ALB
   timeout, cancellation, and duplicate in-progress rejection.
5. Bring back or reconcile the existing run before asking the user to rerun.

The elapsed browser wait is not proof of the server timeout value. The `409`
may be correct if the first operation is still active.

A `400` from `guided/respond` with “does not satisfy the current turn contract”
is a different failure. Inspect the submitted choice/options against the
plugin's typed configuration before touching ECS. During the 2026-07-23 live
acceptance, the generated JSON sink used `schema.mode: flexible` without the
required `fields`; the UI presented it, then the server correctly rejected it.
Reverting the session version and replaying the turn recovered with
`schema.mode: observed`, but that is only an operator recovery path—the missing
pre-submit validation remains a product defect.

### 9. AWS integration check

If health/readiness pass but S3 or Bedrock fails, run the check inside the task
first. Compare:

- selected task role;
- configured AWS region;
- S3 bucket/prefix and object permissions;
- Bedrock model ID and account/region model access;
- Guardrail identifier and immutable version; and
- operator profile aliases and tutorial profile selection.

The deployed application learns selectable models from operator-configured LLM
profiles. It does not guarantee that every Bedrock catalogue model is usable.

## Rollback decision

Rollback only when all are true:

- the failure is isolated to image/config;
- session and Landscape schemas were not changed incompatibly;
- the previous task definition and all its image digests still exist; and
- previous configuration/secrets remain valid.

Otherwise fix forward. After rollback, repeat task-definition, running digest,
target health, public probes, and affected AWS integration checks.

## Full environment acceptance caveat

`docs/runbooks/aws-ecs-deployment.md` describes a much larger disposable
two-scenario acceptance program. It is not the everyday redeploy workflow. Its
Terraform package is external to the tracked repository, and the current text
contains hard-coded schema epochs that do not all match the live schema
constants. Do not execute that runbook mechanically until those values and the
owning Terraform state have been reconciled.
