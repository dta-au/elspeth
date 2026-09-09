# Runbook: Redeploy an existing AWS ECS service

Build, scan, and deploy one immutable ELSPETH image to an existing
ECS/Fargate service. This is the everyday image/config replacement path. It
does not create or destroy AWS infrastructure.

For the release-specific, two-scenario provisioning and teardown program, use
[Full disposable AWS ECS acceptance environment](aws-ecs-deployment.md).

## Safety contract

- Discover the account, region, cluster, service, task definition, architecture,
  ECR repository, network configuration, and target group from live AWS state.
- Build a clean, exact Git commit. Push a unique transport tag, then deploy the
  immutable ECR digest.
- Preserve every task-level setting, sidecar, secret reference, role, mount,
  logging option, and plugin-policy value except the explicitly updated web
  image and release identity.
- Run a one-shot `doctor aws-ecs --json` task before mutating the service.
- Keep the one-task zero-overlap deployment contract:
  `minimumHealthyPercent=0`, `maximumPercent=100`, and `desiredCount=1`.
- Treat `/api/health` as liveness and `/api/ready` as the traffic gate.
- Never print task-definition environment or secret arrays.

Rollback is safe only for an image/config failure when both database schemas
remain compatible and every image referenced by the previous task definition
still exists. Otherwise keep traffic drained and repair forward.

## Prerequisites

- AWS CLI v2, `jq`, `curl`, Docker Buildx, and an authenticated AWS CLI profile.
- A clean source checkout with Python dependencies available through `uv`.
- Node.js 24 and npm 11; the Docker build also pins this toolchain.
- Permission to read ECS/ECR/ELB state, push to the discovered ECR repository,
  register task definitions, run a doctor task, and update the existing
  service.
- The current service is stable at desired/running/pending `1/1/0`.
- The selected database schemas are already current. `--init-schema` is only
  for an explicitly approved fresh database reported as `MISSING`; `STALE` is
  a stop.

Set only operator-selected, non-secret inputs:

```bash
set -Eeuo pipefail
umask 077
export AWS_PAGER=""

: "${AWS_PROFILE:?set the intended AWS CLI profile}"
: "${AWS_REGION:?set the ECS service region}"
: "${ECS_CLUSTER:?set the selected ECS cluster name}"
: "${ECS_SERVICE:?set the selected ECS service name}"
: "${DEPLOY_REF:?set the exact branch, tag, or commit to deploy}"
: "${ELSPETH_BASE_URL:?set the exact public HTTPS origin without a trailing slash}"

CANDIDATE_SHA=$(git rev-parse "${DEPLOY_REF}^{commit}")
test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"
test -z "$(git status --porcelain)"
aws sts get-caller-identity \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" >/dev/null
```

If authentication has expired, refresh the selected profile with `aws login`.
Do not delete or rewrite other profiles.

## 1. Capture the live deployment

```bash
WORK=$(mktemp -d -p /tmp elspeth-ecs-redeploy.XXXXXX)
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

aws ecs describe-task-definition \
  --task-definition "$PREVIOUS_TASK_DEFINITION" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/current-task-definition.json"

CURRENT_IMAGE=$(jq -er --arg name "$WEB_CONTAINER_NAME" '
  .taskDefinition.containerDefinitions[]
  | select(.name == $name) | .image
' "$WORK/current-task-definition.json")
CPU_ARCHITECTURE=$(jq -r \
  '.taskDefinition.runtimePlatform.cpuArchitecture // "X86_64"' \
  "$WORK/current-task-definition.json")
case "$CPU_ARCHITECTURE" in
  X86_64) TARGET_PLATFORM=linux/amd64; TARGET_ARCHITECTURE=amd64 ;;
  ARM64) TARGET_PLATFORM=linux/arm64; TARGET_ARCHITECTURE=arm64 ;;
  *) printf 'unsupported ECS architecture: %s\n' "$CPU_ARCHITECTURE" >&2; exit 2 ;;
esac

CURRENT_REPOSITORY_URI=${CURRENT_IMAGE%@*}
CURRENT_REPOSITORY_URI=${CURRENT_REPOSITORY_URI%:*}
ECR_REGISTRY=${CURRENT_REPOSITORY_URI%%/*}
ECR_REPOSITORY=${CURRENT_REPOSITORY_URI#*/}

ECR_REPOSITORY_URI=$(aws ecr describe-repositories \
  --repository-names "$ECR_REPOSITORY" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'repositories[0].repositoryUri' --output text)
test "$ECR_REPOSITORY_URI" = "$ECR_REGISTRY/$ECR_REPOSITORY"
```

Record `PREVIOUS_TASK_DEFINITION`; it is the rollback candidate, not automatic
rollback authorization.

## 2. Verify and build the exact source

Run the checks appropriate to the changed surface. For a Dockerfile, lockfile,
or frontend change, the minimum lane is:

```bash
unset VIRTUAL_ENV
uv sync --frozen --all-extras
uv run --frozen pytest -q \
  tests/unit/test_build_push_release_checks.py \
  tests/unit/web/test_aws_ecs_runbook_contract.py \
  tests/unit/web/test_aws_ecs_startup.py \
  tests/unit/web/test_deployment_contract.py

npm --prefix src/elspeth/web/frontend ci
npm --prefix src/elspeth/web/frontend run typecheck
npm --prefix src/elspeth/web/frontend run lint
npm --prefix src/elspeth/web/frontend run test
```

Build the platform used by the live task:

```bash
IMAGE_TAG="dev-${CANDIDATE_SHA:0:12}-$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_IMAGE="elspeth:ecs-$IMAGE_TAG"

docker buildx build \
  --platform "$TARGET_PLATFORM" \
  --build-arg INSTALL_EXTRAS="webui llm aws postgres" \
  --label "org.opencontainers.image.revision=$CANDIDATE_SHA" \
  --load --tag "$LOCAL_IMAGE" .

test "$(docker image inspect "$LOCAL_IMAGE" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" \
  = "$CANDIDATE_SHA"
test "$(docker image inspect "$LOCAL_IMAGE" --format '{{.Os}}/{{.Architecture}}')" \
  = "$TARGET_PLATFORM"
test "$(docker run --rm --entrypoint id "$LOCAL_IMAGE" -u)" = "1654"
test "$(docker run --rm --entrypoint id "$LOCAL_IMAGE" -g)" = "1654"
docker run --rm "$LOCAL_IMAGE" --version
docker run --rm --entrypoint python "$LOCAL_IMAGE" -c '
from pathlib import Path
import boto3
import psycopg
import psycopg2
import elspeth.web

root = Path(elspeth.web.__file__).parent
index = root / "frontend" / "dist" / "index.html"
assert index.is_file() and index.stat().st_mode & 0o004
print("container smoke passed")
'
```

## 3. Push to the discovered ECR repository and require a clean scan

```bash
aws ecr get-login-password \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

docker tag "$LOCAL_IMAGE" "$ECR_REPOSITORY_URI:$IMAGE_TAG"
docker push "$ECR_REPOSITORY_URI:$IMAGE_TAG"

IMAGE_DIGEST=$(aws ecr describe-images \
  --repository-name "$ECR_REPOSITORY" \
  --image-ids "imageTag=$IMAGE_TAG" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'imageDetails[0].imageDigest' --output text)
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
CANDIDATE_IMAGE="$ECR_REPOSITORY_URI@$IMAGE_DIGEST"

RAW_MANIFEST=$(docker buildx imagetools inspect --raw "$CANDIDATE_IMAGE")
if jq -e '.manifests | type == "array"' <<<"$RAW_MANIFEST" >/dev/null; then
  SCAN_DIGEST=$(jq -er \
    --arg architecture "$TARGET_ARCHITECTURE" '
      [.manifests[]
        | select(
            .platform.os == "linux"
            and .platform.architecture == $architecture
            and (.annotations["vnd.docker.reference.type"] // "")
              != "attestation-manifest"
          )
        | .digest]
      | unique
      | if length == 1 then .[0] else error("ambiguous platform manifest") end
    ' <<<"$RAW_MANIFEST")
else
  SCAN_DIGEST=$IMAGE_DIGEST
fi

docker logout "$ECR_REGISTRY"
unset RAW_MANIFEST
```

ECR Basic scanning reports findings on the platform image manifest, not
necessarily on a parent OCI index. Wait for the discovered `SCAN_DIGEST`:

```bash
SCAN_FILE="$WORK/ecr-scan.json"
SCAN_STATUS=IN_PROGRESS
for _attempt in $(seq 1 60); do
  if aws ecr describe-image-scan-findings \
    --repository-name "$ECR_REPOSITORY" \
    --image-id "imageDigest=$SCAN_DIGEST" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
    >"$SCAN_FILE" 2>/dev/null; then
    SCAN_STATUS=$(jq -r '.imageScanStatus.status' "$SCAN_FILE")
    case "$SCAN_STATUS" in
      COMPLETE) break ;;
      FAILED|UNSUPPORTED_IMAGE)
        printf 'ECR scan failed: %s\n' "$SCAN_STATUS" >&2
        exit 1
        ;;
    esac
  fi
  sleep 5
done
test "$SCAN_STATUS" = COMPLETE

jq -e '
  .imageScanStatus.status == "COMPLETE"
  and (((.imageScanFindings.findingSeverityCounts // {})
    | [to_entries[].value] | add // 0) == 0)
' "$SCAN_FILE" >/dev/null
```

If the repository uses enhanced Inspector scanning, use its corresponding
findings API and retain the same zero-finding acceptance gate. Do not interpret
an unavailable parent-index scan as a clean platform image.

## 4. Register one narrow task-definition revision

Compute the next family revision across active and inactive definitions because
ECS never reuses a deregistered number:

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
          | split("/")[-1]
          | select(startswith($family + ":"))
          | split(":")[-1] | tonumber]
        | max
      '
)
EXPECTED_REVISION=$((LATEST_REVISION + 1))

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
  .taskDefinition
  | del(
      .taskDefinitionArn, .revision, .status, .requiresAttributes,
      .compatibilities, .registeredAt, .registeredBy, .deregisteredAt
    )
  | (.containerDefinitions[] | select(.name == $container)) |= (
      .image = $image
      | setenv("ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE"; $sha)
      | setenv("ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY"; $family)
      | setenv("ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION"; $revision)
    )
' "$WORK/current-task-definition.json" \
  >"$WORK/candidate-task-definition.json"
chmod 600 "$WORK/candidate-task-definition.json"

CANDIDATE_TASK_DEFINITION=$(aws ecs register-task-definition \
  --cli-input-json "file://$WORK/candidate-task-definition.json" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'taskDefinition.taskDefinitionArn' --output text)
test "${CANDIDATE_TASK_DEFINITION##*:}" = "$EXPECTED_REVISION"

REGISTERED_IMAGE=$(aws ecs describe-task-definition \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  | jq -er --arg name "$WEB_CONTAINER_NAME" '
      .taskDefinition.containerDefinitions[]
      | select(.name == $name) | .image
    ')
test "$REGISTERED_IMAGE" = "$CANDIDATE_IMAGE"
```

The release value must be the full Git SHA, and the static task-definition
revision must be the revision just registered. The web launch wrapper can
derive a revision for ordinary `web` and `doctor` starts, but direct ECS Exec
inherits the task definition's static environment. Keeping both values honest
prevents telemetry series from splitting across identities.

Compare the current and candidate definitions without printing environment or
secret values. Normalize only the three permitted identity values and the web
image:

```bash
jq -S --arg web "$WEB_CONTAINER_NAME" '
  def canonical_container:
    if .name == $web then
      .image = "__WEB_IMAGE__"
      | .environment = [
          (.environment // [])[]
          | select(.name | IN(
              "ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE",
              "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY",
              "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION"
            ) | not)
        ] | .environment |= sort_by(.name)
    else
      (if has("environment") then .environment |= sort_by(.name) else . end)
    end
    | (if has("secrets") then .secrets |= sort_by(.name) else . end);
  .taskDefinition
  | del(
      .taskDefinitionArn, .revision, .status, .requiresAttributes,
      .compatibilities, .registeredAt, .registeredBy, .deregisteredAt
    )
  | .containerDefinitions |= map(canonical_container)
' "$WORK/current-task-definition.json" >"$WORK/current-normalized.json"

jq -S --arg web "$WEB_CONTAINER_NAME" '
  def canonical_container:
    if .name == $web then
      .image = "__WEB_IMAGE__"
      | .environment = [
          (.environment // [])[]
          | select(.name | IN(
              "ELSPETH_WEB__OPERATOR_TELEMETRY_RELEASE",
              "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_FAMILY",
              "ELSPETH_WEB__OPERATOR_TELEMETRY_TASK_DEFINITION_REVISION"
            ) | not)
        ] | .environment |= sort_by(.name)
    else
      (if has("environment") then .environment |= sort_by(.name) else . end)
    end
    | (if has("secrets") then .secrets |= sort_by(.name) else . end);
  .containerDefinitions |= map(canonical_container)
' "$WORK/candidate-task-definition.json" >"$WORK/candidate-normalized.json"

cmp -s "$WORK/current-normalized.json" "$WORK/candidate-normalized.json"
```

Require the registered task definition to be semantically identical to the
submitted input. ECS may reorder environment and secret arrays, so canonicalize
those arrays before a non-printing comparison:

```bash
aws ecs describe-task-definition \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  >"$WORK/registered-task-definition.json"

jq -S '
  def canonical_container:
    (if has("environment") then .environment |= sort_by(.name) else . end)
    | (if has("secrets") then .secrets |= sort_by(.name) else . end);
  .taskDefinition
  | del(
      .taskDefinitionArn, .revision, .status, .requiresAttributes,
      .compatibilities, .registeredAt, .registeredBy, .deregisteredAt
    )
  | .containerDefinitions |= map(canonical_container)
' "$WORK/registered-task-definition.json" \
  >"$WORK/registered-task-definition-input.json"

jq -S '
  def canonical_container:
    (if has("environment") then .environment |= sort_by(.name) else . end)
    | (if has("secrets") then .secrets |= sort_by(.name) else . end);
  .containerDefinitions |= map(canonical_container)
' "$WORK/candidate-task-definition.json" \
  >"$WORK/candidate-task-definition-input.json"

cmp -s \
  "$WORK/candidate-task-definition-input.json" \
  "$WORK/registered-task-definition-input.json"
```

Finally, prove every digest-pinned sidecar remains pullable. A running task does
not prove a deleted sidecar can start again:

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

If another actor registers the expected revision first, deregister the unused
candidate and restart from live service discovery. Do not deploy a revision
whose identity value disagrees with its actual revision.

## 5. Run doctor before service mutation

```bash
DOCTOR_OVERRIDES=$(jq -cn --arg name "$WEB_CONTAINER_NAME" '
  {containerOverrides:[{name:$name,command:["doctor","aws-ecs","--json"]}]}
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

DOCTOR_EXIT=$(aws ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$DOCTOR_TASK_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  | jq -er --arg name "$WEB_CONTAINER_NAME" '
      .tasks[0].containers[] | select(.name == $name) | .exitCode
    ')
test "$DOCTOR_EXIT" = 0
```

Classify a failure before acting: configuration, secret reference, PostgreSQL
connectivity, schema state, EFS ownership, task-role permission, image pull, or
application exception. Never translate `STALE` into `--init-schema`.

## 6. Deploy with zero overlap

Keep circuit-breaker failure detection enabled, but disable automatic rollback.
A failed candidate remains operator-gated by the checks in [Rollback](#rollback);
if they do not pass, keep traffic drained and repair forward.

```bash
aws ecs update-service \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_SERVICE" \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --desired-count 1 \
  --force-new-deployment \
  --deployment-configuration \
    '{"deploymentCircuitBreaker":{"enable":true,"rollback":false},"minimumHealthyPercent":0,"maximumPercent":100}' \
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

RUNNING_DIGEST=$(aws ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$CANDIDATE_TASK_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  | jq -er --arg name "$WEB_CONTAINER_NAME" '
      .tasks[0].containers[] | select(.name == $name) | .imageDigest
    ')
test "$RUNNING_DIGEST" = "$SCAN_DIGEST"

aws elbv2 describe-target-health \
  --target-group-arn "$TARGET_GROUP_ARN" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" --output json \
  | jq -e '
      [.TargetHealthDescriptions[]
        | select(.TargetHealth.State == "healthy")]
      | length == 1
    ' >/dev/null
```

The service waiter is only a wait primitive. The explicit service, task,
runtime digest, and target checks above are the deployment result.

## 7. Prove public behavior and telemetry identity

```bash
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

Also require:

- `/api/system/status` reports the intended deployment and plugin policy;
- one authenticated browser/API workflow matching the changed surface passes;
- browser console and network inspection show no new frontend asset failure;
- bounded CloudWatch logs show no new unhandled startup/runtime error; and
- operator telemetry uses the full `CANDIDATE_SHA` and actual
  `EXPECTED_REVISION`, with no collector-unavailable, export-failure, or
  queue-drop signal.

`verify-operator-telemetry` is an acceptance workflow, not an unconditional
ECS Exec smoke. It requires `ELSPETH_ACCEPTANCE_BASE_URL` and the acceptance
authentication inputs. Existing service task definitions may deliberately omit
them. In that case, verify the deployed CloudWatch series and application
behavior directly or use an owner-provided verifier task definition; do not add
acceptance credentials or secrets to the service merely for an ad-hoc Exec.

## Rollback

Rollback only when all of these are true:

- the failure is isolated to image or configuration;
- session and Landscape schemas remain backward compatible;
- the previous task definition and all of its image digests are still
  pullable; and
- its secret references and external dependencies remain valid.

Then redeploy `PREVIOUS_TASK_DEFINITION` with the same zero-overlap deployment
configuration and repeat the service, task digest, target health, public probe,
and affected integration checks. Otherwise keep traffic drained and repair
forward.
