# AWS ECS container command cheat sheet

These commands operate an **existing** ELSPETH ECS/Fargate service. Run them
from the repository/worktree containing the code to deploy. They deliberately
do not bootstrap or destroy AWS infrastructure.

## 0. Shell and authentication

```bash
set -Eeuo pipefail
umask 077
export AWS_PAGER=""
: "${AWS_PROFILE:?export AWS_PROFILE to the intended AWS CLI profile}"
: "${AWS_REGION:?export AWS_REGION to the service region}"
: "${DEPLOY_REF:?export the user-selected branch, tag, or commit to deploy}"

DEPLOY_SHA=$(git rev-parse "${DEPLOY_REF}^{commit}")
test "$(git rev-parse HEAD)" = "$DEPLOY_SHA"
if test -n "$(git status --porcelain)" && test "${ALLOW_DIRTY_IMAGE:-0}" != 1; then
  printf '%s\n' 'dirty worktree: commit/stash changes or explicitly set ALLOW_DIRTY_IMAGE=1' >&2
  exit 1
fi

aws sts get-caller-identity --profile "$AWS_PROFILE" --region "$AWS_REGION"
```

If STS reports an expired session, the human runs:

```bash
aws login --profile "$AWS_PROFILE" --region "$AWS_REGION"
```

Do not delete or rewrite other profiles. The authenticated profile works from
all Git worktrees because the AWS CLI config is user-global.

## 1. Discover the live service

List choices when names are not already known:

```bash
aws ecs list-clusters \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'clusterArns' --output table

: "${ECS_CLUSTER:?export the selected short cluster name}"
aws ecs list-services --cluster "$ECS_CLUSTER" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'serviceArns' --output table

: "${ECS_SERVICE:?export the selected short service name}"
```

Capture live inventory without printing environment or secret references:

```bash
WORK=$(mktemp -d -p /tmp elspeth-ecs.XXXXXX)
chmod 700 "$WORK"
trap 'rm -rf -- "$WORK"' EXIT HUP INT TERM

aws ecs describe-services \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/service.json"

jq -e '
  (.failures | length) == 0 and (.services | length) == 1
  and .services[0].status == "ACTIVE"
  and .services[0].desiredCount == 1
  and .services[0].runningCount == 1
  and .services[0].pendingCount == 0
  and ([.services[0].deployments[]
    | select(.status == "PRIMARY" and .rolloutState == "COMPLETED")]
    | length) == 1
' "$WORK/service.json" >/dev/null

PREVIOUS_TASK_DEFINITION=$(jq -er '.services[0].taskDefinition' "$WORK/service.json")
WEB_CONTAINER_NAME=$(jq -er '.services[0].loadBalancers[0].containerName' "$WORK/service.json")
TARGET_GROUP_ARN=$(jq -er '.services[0].loadBalancers[0].targetGroupArn' "$WORK/service.json")
NETWORK_CONFIGURATION=$(jq -c '.services[0].networkConfiguration' "$WORK/service.json")
export PREVIOUS_TASK_DEFINITION WEB_CONTAINER_NAME TARGET_GROUP_ARN NETWORK_CONFIGURATION

aws ecs describe-task-definition \
  --task-definition "$PREVIOUS_TASK_DEFINITION" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/current-task-definition.json"

CURRENT_IMAGE=$(jq -er --arg name "$WEB_CONTAINER_NAME" '
  .taskDefinition.containerDefinitions[] | select(.name == $name) | .image
' "$WORK/current-task-definition.json")
CPU_ARCHITECTURE=$(jq -r '.taskDefinition.runtimePlatform.cpuArchitecture // "X86_64"' \
  "$WORK/current-task-definition.json")
case "$CPU_ARCHITECTURE" in
  X86_64) TARGET_PLATFORM=linux/amd64 ;;
  ARM64) TARGET_PLATFORM=linux/arm64 ;;
  *) printf 'unsupported ECS CPU architecture: %s\n' "$CPU_ARCHITECTURE" >&2; exit 2 ;;
esac

CURRENT_REPOSITORY_URI=${CURRENT_IMAGE%@*}
CURRENT_REPOSITORY_URI=${CURRENT_REPOSITORY_URI%:*}
ECR_REGISTRY=${CURRENT_REPOSITORY_URI%%/*}
ECR_REPOSITORY=${CURRENT_REPOSITORY_URI#*/}
export CURRENT_IMAGE TARGET_PLATFORM ECR_REGISTRY ECR_REPOSITORY

aws ecr describe-repositories --repository-names "$ECR_REPOSITORY" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/ecr-repository.json"
ECR_REPOSITORY_URI=$(jq -er '.repositories[0].repositoryUri' "$WORK/ecr-repository.json")
test "$ECR_REPOSITORY_URI" = "$ECR_REGISTRY/$ECR_REPOSITORY"
export ECR_REPOSITORY_URI
```

If the current web image is not in ECR, set the intended ECR repository
explicitly and verify it with `describe-repositories`; do not guess from the
image string.

## 2. Run targeted pre-build tests

Do not inherit another worktree's virtual environment:

```bash
unset VIRTUAL_ENV
uv sync --frozen --all-extras
```

AWS ECS and startup contracts:

```bash
uv run --frozen pytest -q \
  tests/unit/web/test_aws_ecs_runbook_contract.py \
  tests/unit/web/test_aws_ecs_acceptance.py \
  tests/unit/web/test_aws_ecs_startup.py \
  tests/unit/web/test_deployment_contract.py \
  tests/unit/deployment/test_elspeth_web_service.py
```

Focused real-PostgreSQL lane:

```bash
uv run --frozen pytest -n auto -q -m testcontainer \
  tests/testcontainer/web/test_schema_probe_postgres.py \
  tests/testcontainer/web/test_doctor_aws_ecs_postgres.py \
  tests/testcontainer/web/test_aws_ecs_validate_only_startup.py \
  tests/testcontainer/web/test_aws_ecs_readiness_postgres.py \
  tests/testcontainer/web/test_landscape_write_gate_postgres.py
```

Run the frontend lane only when frontend/package inputs changed:

```bash
npm --prefix src/elspeth/web/frontend ci
npm --prefix src/elspeth/web/frontend run typecheck
npm --prefix src/elspeth/web/frontend run lint
npm --prefix src/elspeth/web/frontend run test
```

## 3. Build and smoke locally

```bash
CANDIDATE_SHA=$(git rev-parse HEAD)
IMAGE_TAG="dev-${CANDIDATE_SHA:0:12}-$(date -u +%Y%m%d%H%M%S)"
LOCAL_IMAGE="elspeth:ecs-${IMAGE_TAG}"
export CANDIDATE_SHA IMAGE_TAG LOCAL_IMAGE

if test -n "$(git status --porcelain)"; then
  test "${ALLOW_DIRTY_IMAGE:-0}" = 1
  printf '%s\n' 'warning: explicitly approved dirty development image' >&2
fi

docker buildx build \
  --platform "$TARGET_PLATFORM" \
  --build-arg INSTALL_EXTRAS="webui llm aws postgres" \
  --label "org.opencontainers.image.revision=$CANDIDATE_SHA" \
  --load -t "$LOCAL_IMAGE" .

test "$(docker image inspect "$LOCAL_IMAGE" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = "$CANDIDATE_SHA"
test "$(docker image inspect "$LOCAL_IMAGE" --format '{{.Os}}/{{.Architecture}}')" = "$TARGET_PLATFORM"

docker run --rm "$LOCAL_IMAGE" --version
docker run --rm --entrypoint python "$LOCAL_IMAGE" -c '
from pathlib import Path
import boto3, psycopg
import elspeth.web
root = Path(elspeth.web.__file__).parent
assert (root / "frontend" / "dist" / "index.html").is_file()
print("container smoke passed")
'
```

## 4. Push to ECR and pin the digest

```bash
aws ecr get-login-password \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

docker tag "$LOCAL_IMAGE" "$ECR_REPOSITORY_URI:$IMAGE_TAG"
docker push "$ECR_REPOSITORY_URI:$IMAGE_TAG"
docker logout "$ECR_REGISTRY"

IMAGE_DIGEST=$(aws ecr describe-images \
  --repository-name "$ECR_REPOSITORY" \
  --image-ids "imageTag=$IMAGE_TAG" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'imageDetails[0].imageDigest' --output text)
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
CANDIDATE_IMAGE="$ECR_REPOSITORY_URI@$IMAGE_DIGEST"
export IMAGE_DIGEST CANDIDATE_IMAGE

aws ecr describe-images \
  --repository-name "$ECR_REPOSITORY" \
  --image-ids "imageDigest=$IMAGE_DIGEST" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null
```

## 5. Register a candidate task definition

This preserves the entire live task definition and changes only the web image
and runtime identity values. The helper updates an identity key only when it
already exists, except for the release value, which is always present after
the transform.

```bash
TASK_FAMILY=$(jq -er '.taskDefinition.family' "$WORK/current-task-definition.json")
LATEST_REVISION=$(
  for STATUS in ACTIVE INACTIVE; do
    aws ecs list-task-definitions \
      --family-prefix "$TASK_FAMILY" --status "$STATUS" --sort DESC \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json
  done \
    | jq -ser --arg family "$TASK_FAMILY" '
        [.[].taskDefinitionArns[]
          | (split("/")[-1]) as $qualified
          | select($qualified | startswith($family + ":"))
          | ($qualified | split(":")[-1] | tonumber)]
        | max
      '
)
EXPECTED_REVISION=$((LATEST_REVISION + 1))
export TASK_FAMILY EXPECTED_REVISION

jq --arg container "$WEB_CONTAINER_NAME" \
  --arg image "$CANDIDATE_IMAGE" \
  --arg sha "$CANDIDATE_SHA" \
  --arg family "$TASK_FAMILY" \
  --arg revision "$EXPECTED_REVISION" '
  def setenv($name; $value):
    .environment = (
      if any((.environment // [])[]; .name == $name)
      then [(.environment // [])[]
        | if .name == $name then .value = $value else . end]
      else (.environment // []) + [{"name": $name, "value": $value}]
      end
    );
  def setenv_if_present($name; $value):
    if any((.environment // [])[]; .name == $name)
    then setenv($name; $value)
    else .
    end;
  .taskDefinition
  | del(
      .taskDefinitionArn, .revision, .status, .requiresAttributes,
      .compatibilities, .registeredAt, .registeredBy, .deregisteredAt
    )
  | (.containerDefinitions[] | select(.name == $container)) |= (
      .image = $image
      | setenv("ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE"; $sha)
      | setenv_if_present("ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY"; $family)
      | setenv_if_present("ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION"; $revision)
      | setenv_if_present("ELSPETH_ACCEPTANCE_CANDIDATE_SHA"; $sha)
    )
' "$WORK/current-task-definition.json" >"$WORK/candidate-task-definition-input.json"

chmod 600 "$WORK/candidate-task-definition-input.json"

CANDIDATE_TASK_DEFINITION=$(aws ecs register-task-definition \
  --cli-input-json "file://$WORK/candidate-task-definition-input.json" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'taskDefinition.taskDefinitionArn' --output text)
export CANDIDATE_TASK_DEFINITION

ACTUAL_REVISION=${CANDIDATE_TASK_DEFINITION##*:}
if test "$ACTUAL_REVISION" != "$EXPECTED_REVISION"; then
  aws ecs deregister-task-definition \
    --task-definition "$CANDIDATE_TASK_DEFINITION" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null
  printf '%s\n' 'task-definition revision raced; rediscover and clone the task definition currently selected by the service, then recompute the family maximum' >&2
  exit 1
fi

REGISTERED_IMAGE=$(aws ecs describe-task-definition \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  | jq -er --arg name "$WEB_CONTAINER_NAME" '
      .taskDefinition.containerDefinitions[] | select(.name == $name) | .image
    ')
test "$REGISTERED_IMAGE" = "$CANDIDATE_IMAGE"

aws ecs describe-task-definition \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/registered-task-definition.json"

REGISTERED_CANONICAL_FILTER='
  def canonical_container:
    (if has("environment") then .environment |= sort_by(.name) else . end)
    | (if has("secrets") then .secrets |= sort_by(.name) else . end);
  .taskDefinition
  | del(
      .taskDefinitionArn, .revision, .status, .requiresAttributes,
      .compatibilities, .registeredAt, .registeredBy, .deregisteredAt
    )
  | .containerDefinitions |= map(canonical_container)
'

jq -S "$REGISTERED_CANONICAL_FILTER" \
  "$WORK/registered-task-definition.json" \
  >"$WORK/registered-task-definition-input.json"
jq -S '
  def canonical_container:
    (if has("environment") then .environment |= sort_by(.name) else . end)
    | (if has("secrets") then .secrets |= sort_by(.name) else . end);
  .containerDefinitions |= map(canonical_container)
' "$WORK/candidate-task-definition-input.json" \
  >"$WORK/candidate-task-definition-input.sorted.json"

# Non-printing comparison: failure means AWS registered something other than
# the semantically equivalent candidate input. ECS may reorder environment or
# secret arrays, whose ordering has no runtime meaning. Do not emit a diff
# because those values may contain private operator configuration.
cmp -s \
  "$WORK/candidate-task-definition-input.sorted.json" \
  "$WORK/registered-task-definition-input.json"

jq -S --arg container "$WEB_CONTAINER_NAME" '
  [.taskDefinition.containerDefinitions[] | select(.name != $container)]
' "$WORK/current-task-definition.json" >"$WORK/current-sidecars.json"
jq -S --arg container "$WEB_CONTAINER_NAME" '
  [.containerDefinitions[] | select(.name != $container)]
' "$WORK/candidate-task-definition-input.json" >"$WORK/candidate-sidecars.json"
cmp -s "$WORK/current-sidecars.json" "$WORK/candidate-sidecars.json"

jq -S --arg container "$WEB_CONTAINER_NAME" '
  [.taskDefinition.containerDefinitions[]
    | select(.name == $container)
    | (.environment // [])[]
    | select(.name | IN(
        "ELSPETH_WEB__PLUGIN_ALLOWLIST",
        "ELSPETH_WEB__PLUGIN_PREFERENCES",
        "ELSPETH_WEB__PLUGIN_CONTROL_MODES",
        "ELSPETH_WEB__LLM_PROFILES",
        "ELSPETH_WEB__TUTORIAL_LLM_PROFILE",
        "ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES",
        "ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES"
      ))]
' "$WORK/current-task-definition.json" >"$WORK/current-policy.json"
jq -S --arg container "$WEB_CONTAINER_NAME" '
  [.containerDefinitions[]
    | select(.name == $container)
    | (.environment // [])[]
    | select(.name | IN(
        "ELSPETH_WEB__PLUGIN_ALLOWLIST",
        "ELSPETH_WEB__PLUGIN_PREFERENCES",
        "ELSPETH_WEB__PLUGIN_CONTROL_MODES",
        "ELSPETH_WEB__LLM_PROFILES",
        "ELSPETH_WEB__TUTORIAL_LLM_PROFILE",
        "ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES",
        "ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES"
      ))]
' "$WORK/candidate-task-definition-input.json" >"$WORK/candidate-policy.json"
cmp -s "$WORK/current-policy.json" "$WORK/candidate-policy.json"
```

Before continuing, enumerate container images and verify every digest-pinned
non-web image is still available to pull:

```bash
aws ecs describe-task-definition \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  | jq -r '.taskDefinition.containerDefinitions[] | [.name, .image] | @tsv'
```

Verify same-registry ECR sidecars mechanically:

```bash
while IFS=$'\t' read -r CONTAINER IMAGE; do
  test "$CONTAINER" != "$WEB_CONTAINER_NAME" || continue
  case "$IMAGE" in
    "$ECR_REGISTRY"/*@sha256:*)
      SIDECAR_REPOSITORY=${IMAGE#*/}
      SIDECAR_REPOSITORY=${SIDECAR_REPOSITORY%@*}
      SIDECAR_DIGEST=${IMAGE##*@}
      aws ecr describe-images \
        --repository-name "$SIDECAR_REPOSITORY" \
        --image-ids "imageDigest=$SIDECAR_DIGEST" \
        --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null
      ;;
    *@sha256:*)
      docker buildx imagetools inspect "$IMAGE" >/dev/null
      ;;
    *)
      printf 'non-web image is not digest pinned: %s\n' "$CONTAINER" >&2
      exit 1
      ;;
  esac
done < <(aws ecs describe-task-definition \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  | jq -r '.taskDefinition.containerDefinitions[] | [.name, .image] | @tsv')
```

An external/private registry may need its own authenticated manifest command.
If `imagetools inspect` cannot prove pullability, stop and obtain the correct
registry authentication; do not waive the check.

## 6. Run one-shot doctor

```bash
DOCTOR_OVERRIDES=$(jq -cn --arg name "$WEB_CONTAINER_NAME" '
  {containerOverrides:[{name:$name, command:["doctor","aws-ecs","--json"]}]}
')

DOCTOR_TASK_ARN=$(aws ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --launch-type FARGATE \
  --network-configuration "$NETWORK_CONFIGURATION" \
  --count 1 --overrides "$DOCTOR_OVERRIDES" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'tasks[0].taskArn' --output text)
[[ "$DOCTOR_TASK_ARN" == arn:aws:ecs:* ]]

aws ecs wait tasks-stopped \
  --cluster "$ECS_CLUSTER" --tasks "$DOCTOR_TASK_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION"

aws ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$DOCTOR_TASK_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/doctor-task.json"

DOCTOR_EXIT=$(jq -er --arg name "$WEB_CONTAINER_NAME" '
  .tasks[0].containers[] | select(.name == $name) | .exitCode
' "$WORK/doctor-task.json")
test "$DOCTOR_EXIT" = 0
```

If this is an explicitly approved fresh-database initialization and doctor
reported `MISSING`, rerun the same one-shot task with command
`["doctor","aws-ecs","--init-schema","--json"]`, then rerun the read-only
doctor. Never translate `STALE` into `--init-schema`.

## 7. Update the service

```bash
aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_SERVICE" \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --desired-count 1 \
  --force-new-deployment \
  --deployment-configuration \
    '{"deploymentCircuitBreaker":{"enable":true,"rollback":true},"minimumHealthyPercent":0,"maximumPercent":100}' \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null

aws ecs wait services-stable \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION"

aws ecs describe-services \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/deployed-service.json"

jq -e --arg task_definition "$CANDIDATE_TASK_DEFINITION" '
  (.failures | length) == 0 and (.services | length) == 1
  and .services[0].taskDefinition == $task_definition
  and .services[0].desiredCount == 1
  and .services[0].runningCount == 1
  and .services[0].pendingCount == 0
  and ([.services[0].deployments[]
    | select(
        .status == "PRIMARY"
        and .taskDefinition == $task_definition
        and .rolloutState == "COMPLETED"
        and .failedTasks == 0
      )] | length) == 1
' "$WORK/deployed-service.json" >/dev/null

mapfile -t RUNNING_TASKS < <(aws ecs list-tasks \
  --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
  --desired-status RUNNING \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'taskArns[]' --output text | tr '\t' '\n')
test "${#RUNNING_TASKS[@]}" = 1
CANDIDATE_TASK_ARN=${RUNNING_TASKS[0]}
export CANDIDATE_TASK_ARN

RUNNING_DIGEST=$(aws ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$CANDIDATE_TASK_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  | jq -er --arg name "$WEB_CONTAINER_NAME" '
      .tasks[0].containers[] | select(.name == $name) | .imageDigest
    ')
test "$RUNNING_DIGEST" = "$IMAGE_DIGEST"

aws elbv2 describe-target-health \
  --target-group-arn "$TARGET_GROUP_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/target-health.json"
jq -e '[.TargetHealthDescriptions[]
  | select(.TargetHealth.State == "healthy")] | length == 1' \
  "$WORK/target-health.json" >/dev/null
```

## 8. Public and in-task verification

Use the operator-owned HTTPS origin; the raw ALB hostname may not match the
certificate:

```bash
: "${ELSPETH_BASE_URL:?export the exact public HTTPS origin without a trailing slash}"

HEALTH_STATUS=$(curl --silent --show-error --max-time 10 \
  --output "$WORK/health.json" --write-out '%{http_code}' \
  "$ELSPETH_BASE_URL/api/health")
test "$HEALTH_STATUS" = 200
jq -e '.status == "ok"' "$WORK/health.json" >/dev/null

READY_STATUS=$(curl --silent --show-error --max-time 10 \
  --output "$WORK/ready.json" --write-out '%{http_code}' \
  "$ELSPETH_BASE_URL/api/ready")
test "$READY_STATUS" = 200
jq -e '.ready == true' "$WORK/ready.json" >/dev/null
```

Run checks relevant to the change through the deployed task role:

```bash
test "$(jq -r '.services[0].enableExecuteCommand' "$WORK/deployed-service.json")" = true
command -v session-manager-plugin >/dev/null

aws ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$CANDIDATE_TASK_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/running-task.json"
jq -e --arg name "$WEB_CONTAINER_NAME" '
  [.tasks[0].containers[]
    | select(.name == $name)
    | .managedAgents[]?
    | select(.name == "ExecuteCommandAgent" and .lastStatus == "RUNNING")]
  | length == 1
' "$WORK/running-task.json" >/dev/null

for CHECK in verify-s3 verify-bedrock verify-bedrock-guardrails; do
  aws ecs execute-command \
    --cluster "$ECS_CLUSTER" --task "$CANDIDATE_TASK_ARN" \
    --container "$WEB_CONTAINER_NAME" --interactive \
    --command "python -m elspeth.web.aws_ecs_acceptance $CHECK" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION"
done
```

`verify-bedrock-guardrails` requires the deployed policy/profile inputs. Run
only checks that the environment is configured to support, but do not replace
an applicable failed check with a mock. If ECS Exec is unavailable, stop and
use a dedicated verifier task definition supplied by the environment owner;
host credentials are not an equivalent fallback.

Complete one authenticated browser/API workflow matching the changed feature.
For composer/tutorial changes, require the intended operator profile/model,
successful completion, durable run/session state, and no timeout or duplicate
in-progress conflict.

## 9. Stop, resume, and compatible rollback

Stop without deleting infrastructure:

```bash
aws ecs update-service \
  --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" --desired-count 0 \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null
```

Resume a selected immutable task definition:

```bash
: "${RESUME_TASK_DEFINITION:?export the exact task-definition ARN}"
aws ecs update-service \
  --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --task-definition "$RESUME_TASK_DEFINITION" --desired-count 1 \
  --force-new-deployment \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null
```

For a proven schema-compatible rollback, set
`RESUME_TASK_DEFINITION=$PREVIOUS_TASK_DEFINITION`, resume, and repeat the
post-wait task/digest/target/probe checks. Otherwise fix forward.
