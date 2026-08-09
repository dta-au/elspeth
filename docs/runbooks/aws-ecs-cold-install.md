# Runbook: Deploy a new ELSPETH stack on AWS ECS

Use this procedure to install the complete disposable ELSPETH AWS stack from a
clean AWS account. The tracked Terraform package creates the network, Aurora
PostgreSQL databases, ECS/Fargate service, EFS and S3 storage, CloudWatch/X-Ray
monitoring, Bedrock guardrails, and the IAM roles needed by the application.

This is the shortest supported new-stack path:
[`deploy/aws-ecs/terraform`](../../deploy/aws-ecs/terraform). Use Scenario A.

## Choose the correct AWS procedure

| Need | Procedure |
| --- | --- |
| Create a new, complete disposable stack | This runbook |
| Replace the image or configuration of an existing ECS service | [Existing-service redeploy](aws-ecs-existing-service-redeploy.md) |
| Run the release qualification program with local-auth and OIDC scenarios | [Full disposable acceptance](aws-ecs-deployment.md) |
| Change an existing service to operator-approved Bedrock models | [Bedrock model configuration](aws-ecs-bedrock-opus-sonnet.md) |

Do not use the full acceptance runbook for an ordinary cold install. It is a
release-evidence controller, not a prerequisite for deploying Scenario A.

## Fast path

The installation sequence is:

1. prove both AWS profiles resolve to the intended account and Region;
2. render and attach the package's four narrow installer policies and its
   separate IAM-lifecycle policy;
3. apply `bootstrap/` to create remote state, ECR, and the task-role boundary;
4. build, publish by digest, and scan the application and monitoring images;
5. confirm two live Bedrock inference profiles and their exact model ARNs;
6. apply `scenario-a/`, which creates the stack with the service disabled;
7. run the schema-owner doctor and then the runtime-credential doctor;
8. enable one ECS task and verify the app, sidecar, monitoring, and Bedrock
   status; and
9. destroy Scenario A before bootstrap when the installation is no longer
   needed.

Every step below has a stop condition. Do not skip forward after a failed
identity, image, doctor, or readiness check.

## Result and limits

A successful install has all of these properties:

- one ELSPETH web task and one healthy CloudWatch agent sidecar;
- separate Aurora databases and roles for session and Landscape state;
- EFS-backed application and payload paths plus S3 object storage;
- CloudWatch logs, metrics, alarms, dashboard, deployment events, and X-Ray;
- two distinct Bedrock Composer models and package-created Bedrock guardrails;
- an ALB whose liveness and readiness endpoints both pass; and
- no static AWS keys in the task: Bedrock uses the ECS task role and the normal
  AWS credential chain.

The package is intentionally a **disposable, single-replica stack for a
dedicated empty account**. It is not supported in a shared account and is not a
production high-availability design. Its ALB certificate is self-signed and
valid for only 24 hours.

The image contains PostgreSQL clients, not a PostgreSQL server. Both
`postgresql+psycopg://` and `postgresql+psycopg2://` are supported; the tracked
package chooses psycopg v3 explicitly. Run exactly one web process. Payload
persistence is separate from database persistence, and the package provisions
both surfaces.

Aurora, NAT, ALB, Fargate, EFS, CloudWatch, X-Ray, Bedrock, and data transfer can
incur charges. Promotional credits or an account budget are not a substitute
for teardown. Set a near-term cleanup deadline before applying, and destroy the
stack when the exercise is complete.

## Prerequisites

Install these tools on the operator workstation:

- AWS CLI v2;
- Terraform `>= 1.14, < 2.0`;
- Docker with Buildx;
- `jq`, `curl`, `envsubst`, `git`, and `rg`; and
- an ELSPETH checkout that contains `deploy/aws-ecs/terraform`.

The supported least-privilege installation uses two AWS profiles in the same
account and Region:

1. a normal installer that creates and configures application resources; and
2. a separate IAM lifecycle principal that creates and deletes the narrow task
   roles and permissions boundary.

This least-privilege qualification procedure requires two distinct profiles.
It also requires the operator to name the administrator/root-capable profile
that Terraform must never use. A collapsed or administrator-backed smoke run
is a different exercise and cannot qualify this installation path.

Before continuing, choose two distinct Bedrock inference profiles that are
available in the deployment Region. The application image must include the
`webui`, `llm`, `aws`, and `postgres` extras.

## 1. Select and prove the AWS identity

Run from the repository root. Do not enable shell tracing: Terraform and AWS
commands handle sensitive values.

```bash
set -Eeuo pipefail
umask 077
export AWS_PAGER=""

: "${AWS_PROFILE:?set the normal installer profile}"
: "${IAM_LIFECYCLE_AWS_PROFILE:?set the IAM lifecycle profile}"
: "${AWS_ROOT_PROFILE:?set the administrator profile Terraform must never use}"
: "${AWS_REGION:?set the deployment Region}"
: "${OWNER:?set the operator or team name}"

test "$AWS_PROFILE" != "$IAM_LIFECYCLE_AWS_PROFILE"
test "$AWS_PROFILE" != "$AWS_ROOT_PROFILE"
test "$IAM_LIFECYCLE_AWS_PROFILE" != "$AWS_ROOT_PROFILE"

REPO_ROOT=$(git rev-parse --show-toplevel)
PACKAGE_DIR="$REPO_ROOT/deploy/aws-ecs/terraform"
test -f "$PACKAGE_DIR/scenario-a/main.tf"

AWS_ACCOUNT_ID=$(aws sts get-caller-identity \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query Account --output text)
IAM_ACCOUNT_ID=$(aws sts get-caller-identity \
  --profile "$IAM_LIFECYCLE_AWS_PROFILE" --region "$AWS_REGION" \
  --query Account --output text)
test "$AWS_ACCOUNT_ID" = "$IAM_ACCOUNT_ID"
[[ "$AWS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]

AWS_PRINCIPAL_ARN=$(aws sts get-caller-identity \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query Arn --output text)
IAM_PRINCIPAL_ARN=$(aws sts get-caller-identity \
  --profile "$IAM_LIFECYCLE_AWS_PROFILE" --region "$AWS_REGION" \
  --query Arn --output text)
test "$AWS_PRINCIPAL_ARN" != "arn:aws:iam::${AWS_ACCOUNT_ID}:root"
test "$IAM_PRINCIPAL_ARN" != "arn:aws:iam::${AWS_ACCOUNT_ID}:root"

test "$(aws configure get region --profile "$AWS_PROFILE")" = "$AWS_REGION"
test "$(aws configure get region --profile "$IAM_LIFECYCLE_AWS_PROFILE")" = "$AWS_REGION"

terraform version
aws --version
docker buildx version
```

Stop if either profile resolves to the wrong account or Region. Refresh only
the selected profile if its session has expired; do not rewrite unrelated AWS
profiles.

Set one run identity and retain it unchanged through bootstrap, Scenario A, and
teardown:

```bash
RUN_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
CLEANUP_DEADLINE=$(date -u -d "+8 hours" +"%Y-%m-%dT%H:%M:%SZ")
STATE_BUCKET="elspeth-state-${AWS_ACCOUNT_ID}-${RUN_ID%%-*}"
APP_REPOSITORY="elspeth-web"
AGENT_REPOSITORY="elspeth-cloudwatch-agent"
GATEWAY_REPOSITORY="elspeth-llm-gateway"

printf 'run_id=%s\naccount=%s\nregion=%s\ncleanup=%s\n' \
  "$RUN_ID" "$AWS_ACCOUNT_ID" "$AWS_REGION" "$CLEANUP_DEADLINE"
```

Record these non-secret values in the operator change record. A new attempt
gets a new UUID; do not reuse a failed run's state under a different identity.

## 2. Install the installer and lifecycle policies

The package contains policy templates rather than asking Terraform to run as an
administrator. Render them for this exact account, Region, run, state bucket,
and repository set:

```bash
cd "$PACKAGE_DIR"

export aws_account_id="$AWS_ACCOUNT_ID"
export aws_region="$AWS_REGION"
export run_id="$RUN_ID"
export backend_state_bucket="$STATE_BUCKET"
export ecr_repository="$APP_REPOSITORY"
export cloudwatch_agent_ecr_repository="$AGENT_REPOSITORY"
export gateway_ecr_repository="$GATEWAY_REPOSITORY"
export iam_permissions_boundary_arn="arn:aws:iam::${AWS_ACCOUNT_ID}:policy/elspeth-${RUN_ID}-ecs-boundary"

export scenario_a_namespace="a-$(printf '%s\0A' "$RUN_ID" | sha256sum | cut -c1-20)"
export scenario_b_namespace="b-$(printf '%s\0B' "$RUN_ID" | sha256sum | cut -c1-20)"
export scenario_c_namespace="c-$(printf '%s\0C' "$RUN_ID" | sha256sum | cut -c1-20)"
compact_run_id=$(printf '%s' "$RUN_ID" | tr -d '-')
export scenario_a_bucket="elspeth-${scenario_a_namespace}-$(printf '%.12s' "$compact_run_id")"
export scenario_b_bucket="elspeth-${scenario_b_namespace}-$(printf '%.12s' "$compact_run_id")"
export scenario_c_bucket="elspeth-${scenario_c_namespace}-$(printf '%.12s' "$compact_run_id")"

installer_substitutions='${aws_account_id} ${aws_region} ${run_id} ${backend_state_bucket} ${ecr_repository} ${cloudwatch_agent_ecr_repository} ${gateway_ecr_repository} ${scenario_a_namespace} ${scenario_b_namespace} ${scenario_c_namespace} ${scenario_a_bucket} ${scenario_b_bucket} ${scenario_c_bucket}'

render_installer_policies() {
  local policy
  mkdir -p bootstrap/.terraform || return
  for policy in control-plane regional-resources relationships runtime-observation; do
    envsubst "$installer_substitutions" \
      < "iam/installer-${policy}-policy.json.tftpl" \
      > "bootstrap/.terraform/installer-${policy}-policy.json" || return
    jq -e . "bootstrap/.terraform/installer-${policy}-policy.json" >/dev/null || return
  done
  envsubst '${aws_account_id} ${run_id} ${iam_permissions_boundary_arn}' \
    < iam/lifecycle-policy.json.tftpl \
    > bootstrap/.terraform/iam-lifecycle-policy.json || return
  jq -e . bootstrap/.terraform/iam-lifecycle-policy.json >/dev/null || return
}

render_installer_policies
```

Inspect all five JSON files. Attach the four
`installer-*-policy.json` documents to the normal installer principal as
separate customer-managed policies — their aggregate size exceeds IAM's
inline-policy limit, so do not merge them — and attach
`iam-lifecycle-policy.json` to the lifecycle principal through the account's
trusted IAM administration path. Do not combine them into a wildcard
administrator policy. Record all five policy ARNs in the operator change
record: these policies exist outside every Terraform state, so teardown
detaches and deletes them by recorded ARN rather than by tag or state. The
detailed authority split and its one account-level CloudWatch Logs
limitation are documented in the
[Terraform package README](../../deploy/aws-ecs/terraform/README.md#installer-policy-and-task-role-boundary).

Export the four recorded installer policy ARNs. Presence is not currency: the
gate below re-renders all four templates, retrieves each live policy's declared
default version using the documented `get-policy` then `get-policy-version`
sequence, and compares the complete flattened action sets in both directions.
The source-free validator rejects malformed or ambiguous JSON and reports both
actions missing from live policy and actions unexpectedly present there.

```bash
: "${INSTALLER_CONTROL_PLANE_POLICY_ARN:?set the recorded control-plane policy ARN}"
: "${INSTALLER_REGIONAL_RESOURCES_POLICY_ARN:?set the recorded regional-resources policy ARN}"
: "${INSTALLER_RELATIONSHIPS_POLICY_ARN:?set the recorded relationships policy ARN}"
: "${INSTALLER_RUNTIME_OBSERVATION_POLICY_ARN:?set the recorded runtime-observation policy ARN}"

INSTALLER_POLICY_NAMES=(
  control-plane
  regional-resources
  relationships
  runtime-observation
)
INSTALLER_POLICY_ARNS=(
  "$INSTALLER_CONTROL_PLANE_POLICY_ARN"
  "$INSTALLER_REGIONAL_RESOURCES_POLICY_ARN"
  "$INSTALLER_RELATIONSHIPS_POLICY_ARN"
  "$INSTALLER_RUNTIME_OBSERVATION_POLICY_ARN"
)

verify_installer_policy_currency() (
  set -Eeuo pipefail
  local index name arn metadata version_id live_path unique_count

  render_installer_policies || return
  test "${#INSTALLER_POLICY_NAMES[@]}" = 4 || return
  test "${#INSTALLER_POLICY_ARNS[@]}" = 4 || return
  unique_count=$(printf '%s\n' "${INSTALLER_POLICY_ARNS[@]}" | LC_ALL=C sort -u | wc -l) || return
  test "$unique_count" = 4 || return
  for index in "${!INSTALLER_POLICY_NAMES[@]}"; do
    name=${INSTALLER_POLICY_NAMES[$index]}
    arn=${INSTALLER_POLICY_ARNS[$index]}
    case "$arn" in
      "arn:aws:iam::${AWS_ACCOUNT_ID}:policy/"?*) ;;
      *) printf 'invalid installer policy ARN for %s\n' "$name" >&2; return 1 ;;
    esac
    metadata=$(aws iam get-policy \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" \
      --policy-arn "$arn" --output json) || return
    jq -e --arg arn "$arn" '.Policy.Arn == $arn' <<<"$metadata" >/dev/null || return
    version_id=$(jq -er \
      '.Policy.DefaultVersionId | select(type == "string" and test("^v[1-9][0-9]*$"))' \
      <<<"$metadata") || return
    live_path="bootstrap/.terraform/live-${name}-policy-version.json"
    aws iam get-policy-version \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" \
      --policy-arn "$arn" --version-id "$version_id" --output json \
      >"$live_path" || return
    python3 scripts/verify-iam-policy-actions.py \
      --rendered-policy "bootstrap/.terraform/installer-${name}-policy.json" \
      --live-policy-version "$live_path" \
      --label "$name" || return
  done
)

IAM_POLICY_SETTLE_MAX_SECONDS=120
IAM_POLICY_SETTLE_QUIET_SECONDS=30
IAM_POLICY_SETTLE_POLL_SECONDS=5

await_installer_policy_currency() (
  set -Eeuo pipefail
  local started_at quiet_started_at=-1 elapsed quiet_elapsed remaining sleep_seconds
  test "$IAM_POLICY_SETTLE_MAX_SECONDS" -gt 0
  test "$IAM_POLICY_SETTLE_QUIET_SECONDS" -gt 0
  test "$IAM_POLICY_SETTLE_POLL_SECONDS" -gt 0
  test "$IAM_POLICY_SETTLE_QUIET_SECONDS" -le "$IAM_POLICY_SETTLE_MAX_SECONDS"

  started_at=$SECONDS
  while true; do
    if verify_installer_policy_currency; then
      if test "$quiet_started_at" -lt 0; then quiet_started_at=$SECONDS; fi
      quiet_elapsed=$((SECONDS - quiet_started_at))
      if test "$quiet_elapsed" -ge "$IAM_POLICY_SETTLE_QUIET_SECONDS"; then
        printf 'installer_policy_currency_stable quiet_seconds=%s\n' "$quiet_elapsed"
        return 0
      fi
    else
      quiet_started_at=-1
    fi

    elapsed=$((SECONDS - started_at))
    if test "$elapsed" -ge "$IAM_POLICY_SETTLE_MAX_SECONDS"; then
      printf 'installer_policy_currency_not_stable elapsed_seconds=%s\n' "$elapsed" >&2
      return 1
    fi
    remaining=$((IAM_POLICY_SETTLE_MAX_SECONDS - elapsed))
    sleep_seconds=$IAM_POLICY_SETTLE_POLL_SECONDS
    if test "$sleep_seconds" -gt "$remaining"; then sleep_seconds=$remaining; fi
    sleep "$sleep_seconds"
  done
)

verify_installer_policy_currency
await_installer_policy_currency
```

Any action-set drift makes the preflight fail closed. Repair a stale policy
through the trusted IAM administration path with a new version and
`--set-as-default`, then rerun both commands above. The second command requires
a bounded quiet window of repeated default-document matches so Terraform is
not started immediately after an IAM policy-version change. It fails rather
than waiting without a bound.

## 3. Bootstrap remote state and image repositories

```bash
cp examples/bootstrap.tfvars.example examples/bootstrap.tfvars
```

Edit the ignored `examples/bootstrap.tfvars` so it contains the exact values
selected above:

| Input | Value |
| --- | --- |
| `run_id` | `$RUN_ID` |
| `aws_account_id` | `$AWS_ACCOUNT_ID` |
| `aws_region` | `$AWS_REGION` |
| `aws_profile` | `$AWS_PROFILE` |
| `iam_lifecycle_aws_profile` | `$IAM_LIFECYCLE_AWS_PROFILE` |
| `backend_state_bucket` | `$STATE_BUCKET` |
| `ecr_repository` | `$APP_REPOSITORY` |
| `cloudwatch_agent_ecr_repository` | `$AGENT_REPOSITORY` |
| `gateway_ecr_repository` | Empty for an A/B-only run; `$GATEWAY_REPOSITORY` for Scenario C |
| `gateway_bearer_secret_arn` | Empty for an A/B-only run; the exact operator-created ARN for Scenario C |
| `gateway_oauth_client_id_secret_arn` | Empty for an A/B-only run; the exact operator-created ARN for Scenario C |
| `gateway_oauth_client_secret_secret_arn` | Empty for an A/B-only run; the exact operator-created ARN for Scenario C |
| `owner` | `$OWNER` |
| `cleanup_deadline` | `$CLEANUP_DEADLINE` |

The four gateway inputs are an all-or-none group. Leaving all four empty keeps
the existing Scenario A/B bootstrap valid and omits gateway repository/secret
ARNs from the shared ECS-role boundary. A Scenario C bootstrap must provide all four,
and Terraform rejects a repository outside the exact ECR grammar or a secret
ARN outside the commercial `aws` partition, selected Region, or selected
account. For a Scenario C run, export the same exact values before editing the
tfvars file:

```bash
export gateway_ecr_repository="$GATEWAY_REPOSITORY"
export gateway_bearer_secret_arn="REPLACE_WITH_EXACT_GATEWAY_BEARER_SECRET_ARN"
export gateway_oauth_client_id_secret_arn="REPLACE_WITH_EXACT_GATEWAY_OAUTH_CLIENT_ID_SECRET_ARN"
export gateway_oauth_client_secret_secret_arn="REPLACE_WITH_EXACT_GATEWAY_OAUTH_CLIENT_SECRET_ARN"
```

Refuse unresolved placeholders, then initialize, review, and apply:

```bash
! rg -n 'REPLACE_WITH|2030-01-01' examples/bootstrap.tfvars
terraform fmt -check -recursive
terraform -chdir=bootstrap init
terraform -chdir=bootstrap validate
verify_installer_policy_currency
await_installer_policy_currency
terraform -chdir=bootstrap plan \
  -var-file=../examples/bootstrap.tfvars \
  -out=.terraform/bootstrap.tfplan
terraform -chdir=bootstrap show -no-color .terraform/bootstrap.tfplan
terraform -chdir=bootstrap show -json .terraform/bootstrap.tfplan >.terraform/bootstrap.json
python3 scripts/verify-terraform-profiles.py plan \
  --plan-json .terraform/bootstrap.json \
  --installer-profile "$AWS_PROFILE" \
  --iam-lifecycle-profile "$IAM_LIFECYCLE_AWS_PROFILE" \
  --forbidden-profile "$AWS_ROOT_PROFILE"
terraform -chdir=bootstrap apply .terraform/bootstrap.tfplan

BOUNDARY_ARN=$(terraform -chdir=bootstrap output -raw iam_permissions_boundary_arn)
APP_REPOSITORY_URL=$(terraform -chdir=bootstrap output -raw ecr_repository_url)
AGENT_REPOSITORY_URL=$(terraform -chdir=bootstrap output -raw cloudwatch_agent_repository_url)
test "$BOUNDARY_ARN" = "$iam_permissions_boundary_arn"
```

Preserve `bootstrap/terraform.tfstate`. Bootstrap state is local because it
creates the remote-state bucket. Do not destroy bootstrap while Scenario A
still exists.

## 4. Build, publish, and scan both images

Build the exact clean commit that owns this Terraform package:

```bash
cd "$REPO_ROOT"
CANDIDATE_SHA=$(git rev-parse HEAD)
test -z "$(git status --porcelain)"

APP_TAG="acceptance-${RUN_ID}"
LOCAL_APP_IMAGE="elspeth:aws-${CANDIDATE_SHA:0:12}"
docker buildx build \
  --platform linux/amd64 \
  --build-arg INSTALL_EXTRAS="webui llm aws postgres" \
  --label "org.opencontainers.image.revision=$CANDIDATE_SHA" \
  --load --tag "$LOCAL_APP_IMAGE" .

test "$(docker image inspect "$LOCAL_APP_IMAGE" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" \
  = "$CANDIDATE_SHA"
test "$(docker image inspect "$LOCAL_APP_IMAGE" --format '{{.Os}}/{{.Architecture}}')" \
  = linux/amd64
test "$(docker run --rm --entrypoint id "$LOCAL_APP_IMAGE" -u)" = 1654
docker run --rm --entrypoint python "$LOCAL_APP_IMAGE" -c '
import boto3
import psycopg
import psycopg2
import elspeth.web
print("ELSPETH AWS runtime imports passed")
'

LOCAL_AGENT_IMAGE="elspeth-cloudwatch-agent:${CANDIDATE_SHA:0:12}"
docker buildx build \
  --platform linux/amd64 \
  --build-arg ELSPETH_RELEASE_SHA="$CANDIDATE_SHA" \
  --load --tag "$LOCAL_AGENT_IMAGE" \
  --file "$PACKAGE_DIR/cloudwatch-agent-image/Dockerfile" \
  "$PACKAGE_DIR"

test "$(docker image inspect "$LOCAL_AGENT_IMAGE" \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" \
  = "$CANDIDATE_SHA"
```

The agent build-arg is not optional. `cloudwatch-agent-image/Dockerfile`
sets `org.opencontainers.image.revision` from `ELSPETH_RELEASE_SHA`, and the
scenario re-inspects that label before Terraform may register any task
definition. Omitting the build-arg bakes an empty label and the apply in
step 7 fails with `cloudwatch_agent_image_revision_mismatch`.

Publish both images to the repositories created by bootstrap:

```bash
ECR_REGISTRY=${APP_REPOSITORY_URL%%/*}
aws ecr get-login-password \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

docker tag "$LOCAL_APP_IMAGE" "$APP_REPOSITORY_URL:$APP_TAG"
docker push "$APP_REPOSITORY_URL:$APP_TAG"
docker tag "$LOCAL_AGENT_IMAGE" "$AGENT_REPOSITORY_URL:agent-${CANDIDATE_SHA:0:12}"
docker push "$AGENT_REPOSITORY_URL:agent-${CANDIDATE_SHA:0:12}"

APP_DIGEST=$(aws ecr describe-images \
  --repository-name "$APP_REPOSITORY" \
  --image-ids "imageTag=$APP_TAG" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'imageDetails[0].imageDigest' --output text)
AGENT_DIGEST=$(aws ecr describe-images \
  --repository-name "$AGENT_REPOSITORY" \
  --image-ids "imageTag=agent-${CANDIDATE_SHA:0:12}" \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query 'imageDetails[0].imageDigest' --output text)
[[ "$APP_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$AGENT_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]

CANDIDATE_IMAGE="$APP_REPOSITORY_URL@$APP_DIGEST"
CLOUDWATCH_AGENT_IMAGE="$AGENT_REPOSITORY_URL@$AGENT_DIGEST"
docker logout "$ECR_REGISTRY"
```

Both references are digests, never tags. Digest references are still not
expiry-proof — ECR lifecycle expiry deletes the matching image itself, so any
rule that expires a tagged image also makes its digest unpullable. Both
bootstrap repositories therefore expire only untagged images (after 30 days);
tagged images persist until teardown deletes the repositories.

Require both ECR Basic scans to complete with zero findings.

Scan the **platform child**, never the index. The documented publish flow
produces an OCI image index whose children are the `linux/amd64` image and a
buildx attestation; ECR Basic scans attach only to the platform child, and
`describe-image-scan-findings` on the index digest returns
`ScanNotFoundException` forever (observed on both images in round 4). The
resolver below uses the ECR API rather than `docker buildx imagetools`
because this section runs after `docker logout`; a digest that is already a
plain manifest passes through unchanged.

```bash
resolve_scannable_digest() {
  local repository=$1
  local digest=$2
  local manifest child
  manifest=$(aws ecr batch-get-image \
    --repository-name "$repository" \
    --image-ids "imageDigest=$digest" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --query 'images[0].imageManifest' --output text)
  # Attestation children carry platform "unknown"; select the runnable child.
  child=$(jq -r '
    [.manifests[]?
      | select(.platform.architecture == "amd64" and .platform.os == "linux")
    ][0].digest // empty
  ' <<<"$manifest")
  if [ -n "$child" ]; then printf '%s\n' "$child"; else printf '%s\n' "$digest"; fi
}

require_clean_scan() {
  local repository=$1
  local digest=$2
  local result
  aws ecr wait image-scan-complete \
    --repository-name "$repository" \
    --image-id "imageDigest=$digest" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION"
  result=$(aws ecr describe-image-scan-findings \
    --repository-name "$repository" \
    --image-id "imageDigest=$digest" \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --output json)
  jq -e '
    .imageScanStatus.status == "COMPLETE"
    and (((.imageScanFindings.findingSeverityCounts // {})
      | [to_entries[].value] | add // 0) == 0)
  ' <<<"$result" >/dev/null
}

require_clean_scan "$APP_REPOSITORY" "$(resolve_scannable_digest "$APP_REPOSITORY" "$APP_DIGEST")"
require_clean_scan "$AGENT_REPOSITORY" "$(resolve_scannable_digest "$AGENT_REPOSITORY" "$AGENT_DIGEST")"
```

If the account uses enhanced Inspector scanning, use the corresponding
Inspector findings API and retain the same zero-finding gate.

## 5. Confirm Bedrock inputs

Set two distinct system-defined inference-profile IDs. Discover them live; do
not guess IDs or copy ARNs from another account or Region.

```bash
: "${PRIMARY_PROFILE_ID:?set the primary Bedrock inference-profile ID}"
: "${ADVISOR_PROFILE_ID:?set a distinct advisor inference-profile ID}"
test "$PRIMARY_PROFILE_ID" != "$ADVISOR_PROFILE_ID"

WORK=$(mktemp -d -p /tmp elspeth-aws-install.XXXXXX)
chmod 700 "$WORK"
trap 'rm -rf -- "$WORK"' EXIT HUP INT TERM

for profile in "$PRIMARY_PROFILE_ID" "$ADVISOR_PROFILE_ID"; do
  aws bedrock get-inference-profile \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --inference-profile-identifier "$profile" \
    --output json >"$WORK/$profile.json"
  jq -e --arg profile "$profile" '
    .inferenceProfileId == $profile
    and .status == "ACTIVE"
    and (.models | length > 0)
  ' "$WORK/$profile.json" >/dev/null
done

jq -sr '[.[].inferenceProfileArn] | unique[]' "$WORK"/*.json
jq -sr '[.[].models[].modelArn] | unique[]' "$WORK"/*.json
```

The two command outputs are the exact inference-profile and destination
foundation-model ARN lists for Scenario A. The application values are
`bedrock/$PRIMARY_PROFILE_ID` and `bedrock/$ADVISOR_PROFILE_ID`. The ECS task
role receives only the supplied model ARNs; no static access key, profile, or
custom Bedrock endpoint is injected.

If either profile is unavailable or inactive, stop and select another live
profile. Follow the [Bedrock model configuration runbook](aws-ecs-bedrock-opus-sonnet.md)
when a model requires one-time account access or agreement.

## 6. Configure Scenario A

Create the ignored backend and Scenario A inputs:

```bash
cd "$PACKAGE_DIR"
cp examples/scenario-a.s3.tfbackend.example examples/scenario-a.s3.tfbackend
cp examples/scenario-a.tfvars.example examples/scenario-a.tfvars
```

In `scenario-a.s3.tfbackend`, set:

- `bucket = "$STATE_BUCKET"`;
- `region = "$AWS_REGION"`; and
- `profile = "$AWS_PROFILE"`.

In `scenario-a.tfvars`, set the same run, account, Region, profiles, owner, and
cleanup deadline used by bootstrap, then set:

- `candidate_sha = "$CANDIDATE_SHA"`;
- `candidate_image = "$CANDIDATE_IMAGE"`;
- `candidate_ecr_repository = "$APP_REPOSITORY"`, the exact bootstrap-created
  application repository name;
- `alb_https_ingress_cidrs` to the explicit operator CIDRs allowed to reach the
  ALB over HTTPS — the package rejects an empty list, duplicates, and any
  `/0` prefix, so do not open the listener to the internet;
- `iam_permissions_boundary_arn = "$BOUNDARY_ARN"`;
- `cloudwatch_agent_image = "$CLOUDWATCH_AGENT_IMAGE"`;
- `cloudwatch_agent_ecr_repository = "$AGENT_REPOSITORY"`;
- `composer_model = "bedrock/$PRIMARY_PROFILE_ID"`;
- `composer_advisor_model = "bedrock/$ADVISOR_PROFILE_ID"`;
- `bedrock_inference_profile_arns` to the exact two profile ARNs;
- `bedrock_foundation_model_arns` to every destination model ARN returned
  above; and
- `target_platform = "linux/amd64"`.

Keep the pinned Aurora major/version pair from the example. The package
currently admits PostgreSQL `16.13` because that exact Aurora version was
verified for `db.serverless` in `ap-southeast-1`.

```bash
! rg -n 'REPLACE_WITH|2030-01-01' \
  examples/scenario-a.tfvars examples/scenario-a.s3.tfbackend
terraform -chdir=scenario-a init -reconfigure \
  -backend-config=../examples/scenario-a.s3.tfbackend
terraform -chdir=scenario-a workspace select default
test "$(terraform -chdir=scenario-a workspace show)" = default
terraform -chdir=scenario-a validate
terraform -chdir=scenario-a test \
  -filter=codeblind.tftest.hcl -no-color
```

## 7. Plan and create the stack

Re-run both identity checks immediately before the mutating operation:

```bash
test "$(aws sts get-caller-identity \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query Account --output text)" = "$AWS_ACCOUNT_ID"
test "$(aws sts get-caller-identity \
  --profile "$IAM_LIFECYCLE_AWS_PROFILE" --region "$AWS_REGION" \
  --query Account --output text)" = "$AWS_ACCOUNT_ID"

verify_installer_policy_currency
await_installer_policy_currency
terraform -chdir=scenario-a plan \
  -var-file=../examples/scenario-a.tfvars \
  -out=.terraform/scenario-a.tfplan
terraform -chdir=scenario-a show -no-color .terraform/scenario-a.tfplan
terraform -chdir=scenario-a show -json .terraform/scenario-a.tfplan >.terraform/scenario-a.json
python3 scripts/verify-terraform-profiles.py plan \
  --plan-json .terraform/scenario-a.json \
  --installer-profile "$AWS_PROFILE" \
  --iam-lifecycle-profile "$IAM_LIFECYCLE_AWS_PROFILE" \
  --forbidden-profile "$AWS_ROOT_PROFILE"
terraform -chdir=scenario-a apply .terraform/scenario-a.tfplan
```

Expected result: Terraform creates the stack, but the ECS service remains at
desired count zero. This is intentional. Do not enable it until both database
doctor tasks pass.

## 8. Initialize schemas, then prove runtime credentials

Provision the EFS runtime storage **before** either doctor. Both doctors check
`payload_store_writable` and `blob_writable` by probing directories that do not
exist on a freshly created file system, so on a cold install the schema-init
doctor exits non-zero with `FileNotFoundError` until the `provision-storage`
one-shot has created them. This is the expected first-run ordering, not a
fault:

```bash
ECS_CLUSTER=$(terraform -chdir=scenario-a output -raw cluster_name)
NETWORK=$(terraform -chdir=scenario-a output -raw runtime_doctor_network_configuration)
PAYLOAD_VERIFIER_TASK_DEFINITION=$(terraform -chdir=scenario-a output -json resolved_inventory \
  | jq -er '.values.PAYLOAD_VERIFIER_TASK_DEFINITION')

PAYLOAD_TASK_ARN=$(aws ecs run-task \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" \
  --task-definition "$PAYLOAD_VERIFIER_TASK_DEFINITION" \
  --launch-type FARGATE \
  --network-configuration "$NETWORK" \
  --count 1 \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --tasks "$PAYLOAD_TASK_ARN"
test "$(aws ecs describe-tasks \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --tasks "$PAYLOAD_TASK_ARN" \
  --query 'tasks[0].containers[?name==`elspeth-web`].exitCode | [0]' \
  --output text)" = 0
```

The first doctor uses schema-owner credentials. The second uses the same
least-privilege runtime credentials as the web service. Run them in that order:

```bash
run_doctor_task() {
  local task_definition=$1
  local network_configuration=$2
  local overrides=$3
  local task_arn
  local exit_code

  task_arn=$(aws ecs run-task \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" \
    --task-definition "$task_definition" \
    --launch-type FARGATE \
    --network-configuration "$network_configuration" \
    --overrides "$overrides" \
    --count 1 \
    --query 'tasks[0].taskArn' --output text)
  test -n "$task_arn" && test "$task_arn" != None
  aws ecs wait tasks-stopped \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" --tasks "$task_arn"
  exit_code=$(aws ecs describe-tasks \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" --tasks "$task_arn" \
    --query 'tasks[0].containers[?name==`doctor`].exitCode | [0]' \
    --output text)
  test "$exit_code" = 0
}

run_doctor_task \
  "$(terraform -chdir=scenario-a output -raw schema_init_doctor_task_definition_arn)" \
  "$(terraform -chdir=scenario-a output -raw schema_init_doctor_network_configuration)" \
  "$(terraform -chdir=scenario-a output -raw schema_init_doctor_overrides)"

run_doctor_task \
  "$(terraform -chdir=scenario-a output -raw runtime_doctor_task_definition_arn)" \
  "$(terraform -chdir=scenario-a output -raw runtime_doctor_network_configuration)" \
  "$(terraform -chdir=scenario-a output -raw runtime_doctor_overrides)"
```

Both task exit codes must be `0`. A passing runtime doctor proves the packaged
PostgreSQL driver, generated TLS URL, Secrets Manager injection, task role,
network path, and least-privilege database roles together.

Both doctor reports must also show `rds_trust_root`, `session_tls`,
`landscape_tls`, `session_schema`, and `landscape_schema` green before the
service is enabled. Those checks bind the baked, root-owned RDS trust root and
the TLS posture of the connection that inspected each schema; the package
README's
[immutable RDS trust-root admission](../../deploy/aws-ecs/terraform/README.md#immutable-rds-trust-root-admission)
section states the required bundle digest, CA label, and
`readonlyRootFilesystem` split.

## 9. Enable and verify the service

Inspect Terraform's generated command, then enable the service explicitly:
Define `resolve_target_ecr_digest` and `verify_candidate_service` exactly as
shown in the Terraform package README's source-free acceptance section; those
helpers admit the ECR parent index, selected platform child, service, task, and
container response shapes before comparing them.

```bash
SERVICE_ENABLE_COMMAND="$(terraform -chdir=scenario-a output -raw service_enable_command)"
printf '%s\n' "$SERVICE_ENABLE_COMMAND"
bash -Eeuo pipefail -c "$SERVICE_ENABLE_COMMAND"

ECS_SERVICE=$(terraform -chdir=scenario-a output -raw service_name)
aws ecs wait services-stable \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"

read -r DESIRED RUNNING PENDING < <(
  aws ecs describe-services \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
    --query 'services[0].[desiredCount,runningCount,pendingCount]' \
    --output text
)
test "$DESIRED/$RUNNING/$PENDING" = 1/1/0

INVENTORY=$(terraform -chdir=scenario-a output -json resolved_inventory)
CANDIDATE_TASK_DEFINITION=$(printf '%s' "$INVENTORY" | jq -er '.values.CANDIDATE_TASK_DEFINITION')
TARGET_PLATFORM=$(printf '%s' "$INVENTORY" | jq -er '.values.TARGET_PLATFORM')
CANDIDATE_IMAGE=$(aws ecs describe-task-definition \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --query 'taskDefinition.containerDefinitions[?name==`elspeth-web`].image | [0]' \
  --output text)
CANDIDATE_PLATFORM_DIGEST=$(resolve_target_ecr_digest "$CANDIDATE_IMAGE" "$TARGET_PLATFORM")
verify_candidate_service "$ECS_CLUSTER" "$ECS_SERVICE" \
  "$CANDIDATE_TASK_DEFINITION" "$CANDIDATE_IMAGE" "$CANDIDATE_PLATFORM_DIGEST"
```

Verify the application and monitoring sidecar:

```bash
TASK_ARN=$(aws ecs list-tasks \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
  --desired-status RUNNING --query 'taskArns[0]' --output text)
test -n "$TASK_ARN" && test "$TASK_ARN" != None
aws ecs describe-tasks \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[].[name,lastStatus,healthStatus]' \
  --output table

NAMESPACE="$scenario_a_namespace"
aws logs describe-log-groups \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --log-group-name-prefix "/aws/ecs/${NAMESPACE}" \
  --query 'logGroups[].logGroupName' --output table
aws cloudwatch get-dashboard \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --dashboard-name "${NAMESPACE}-elspeth-aws-operator-v1" \
  --query DashboardName --output text
aws xray get-groups \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query "Groups[?GroupName==\`${NAMESPACE}-xray\`].GroupName" \
  --output text
```

The `web` and `cloudwatch-agent` containers must be `RUNNING` and `HEALTHY`;
the log-group, dashboard, and X-Ray queries must return this stack's resources.

Trust the temporary ALB certificate only for this installation:

```bash
CA_FILE="$PACKAGE_DIR/scenario-a/.terraform/acceptance-ca.pem"
terraform -chdir=scenario-a output -raw acceptance_tls_ca_pem >"$CA_FILE"
chmod 600 "$CA_FILE"
BASE_URL=$(terraform -chdir=scenario-a output -raw public_url)

curl --fail --silent --show-error --cacert "$CA_FILE" \
  "$BASE_URL/api/health" | jq -e '.status == "ok"'
curl --fail --silent --show-error --cacert "$CA_FILE" \
  "$BASE_URL/api/ready" | jq -e '.ready == true'
curl --fail --silent --show-error --cacert "$CA_FILE" \
  "$BASE_URL/api/system/status" \
  | jq -e '
      .tutorial_ready == true
      and .composer_available == true
      and .composer_provider == "bedrock"
    '
```

Readiness confirms both Composer boot probes completed through the ECS task
role. For release evidence, also make one authenticated Composer request and
follow the Bedrock/guardrail lane in the
[full acceptance runbook](aws-ecs-deployment.md).

## Troubleshooting

### The runtime doctor fails database authentication

Do not enable the service. The generated runtime secret contains separate
`session_url` and `landscape_url` values using explicit
`postgresql+psycopg://` URLs, `sslmode=verify-full`, and the downloaded RDS CA
bundle. Do not rewrite the driver prefix or remove either TLS parameter.

Use this diagnostic order:

1. Confirm the application and CloudWatch images are the exact ECR digests in
   `scenario-a.tfvars`.
2. Confirm the runtime doctor task definition is the Terraform output, not the
   schema-init task definition.
3. Confirm the secret has the expected keys without printing its values:

   ```bash
   RUNTIME_SECRET_ARN=$(terraform -chdir=scenario-a output -raw runtime_database_secret_arn)
   aws secretsmanager get-secret-value \
     --profile "$AWS_PROFILE" --region "$AWS_REGION" \
     --secret-id "$RUNTIME_SECRET_ARN" \
     --query SecretString --output text \
     | jq -e 'keys | sort == ["landscape_url", "secret_key", "session_url", "shareable_link_signing_key"]'
   ```

4. Re-run the runtime doctor. It uses the same image, task role, network,
   secret references, URL validation, SQLAlchemy engine construction, and
   database roles as the web task.
5. If the runtime doctor passes but the web task fails, compare the registered
   web and runtime-doctor task definitions for image digest, secret references,
   mount points, command, and wrapper. Do not paste environment or secret arrays
   into a ticket.
6. Capture only the stopped reason, exit code, and redacted CloudWatch error
   class. A raw `psycopg.connect()` success does not supersede a failing
   application-path doctor; preserve both results.

### Terraform fails before creating resources

- `AccessDenied`: verify which of the two principals owns the denied action and
  attach only the matching rendered policy.
- `allowed_account_ids`: stop; a profile resolved to the wrong account.
- existing bucket or repository: this is not an empty-account cold install.
  Do not import or delete it without establishing ownership.
- unavailable Aurora version: stop. Do not silently change the pinned version;
  qualify the replacement in the selected Region first.
- unavailable Bedrock profile or model: select live IDs and regenerate both ARN
  lists together.

### The service does not stabilize

Keep desired count at zero or set it back to zero. Inspect ECS service events,
the stopped task reason, `web` and `cloudwatch-agent` health, and the redacted
CloudWatch logs. Fix forward, rerun both doctors, and only then re-enable.

## Teardown

Destroy Scenario A before bootstrap. Use the same repository checkout, tfvars,
backend, workspace, profiles, account, and Region used to install it.

```bash
cd "$PACKAGE_DIR"
test "$(aws sts get-caller-identity \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --query Account --output text)" = "$AWS_ACCOUNT_ID"
test "$(aws sts get-caller-identity \
  --profile "$IAM_LIFECYCLE_AWS_PROFILE" --region "$AWS_REGION" \
  --query Account --output text)" = "$AWS_ACCOUNT_ID"

! rg -n 'REPLACE_WITH|2030-01-01' \
  examples/scenario-a.tfvars examples/scenario-a.s3.tfbackend
terraform -chdir=scenario-a init -reconfigure \
  -backend-config=../examples/scenario-a.s3.tfbackend
python3 scripts/verify-terraform-profiles.py backend \
  --backend-state scenario-a/.terraform/terraform.tfstate \
  --installer-profile "$AWS_PROFILE" \
  --forbidden-profile "$AWS_ROOT_PROFILE"
test "$(terraform -chdir=scenario-a workspace show)" = default
verify_installer_policy_currency
await_installer_policy_currency

ECS_CLUSTER=$(terraform -chdir=scenario-a output -raw cluster_name)
ECS_SERVICE=$(terraform -chdir=scenario-a output -raw service_name)
NAMESPACE=$(terraform -chdir=scenario-a output -raw namespace)

aws ecs update-service \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --desired-count 0 >/dev/null
aws ecs wait services-stable \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
aws ecs list-tasks \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
  --desired-status RUNNING --output json \
  | jq -e '.taskArns | length == 0'

terraform -chdir=scenario-a plan -destroy \
  -var-file=../examples/scenario-a.tfvars \
  -out=.terraform/scenario-a-destroy.tfplan
terraform -chdir=scenario-a show -no-color .terraform/scenario-a-destroy.tfplan
terraform -chdir=scenario-a show -json .terraform/scenario-a-destroy.tfplan >.terraform/scenario-a-destroy.json
python3 scripts/verify-terraform-profiles.py plan \
  --plan-json .terraform/scenario-a-destroy.json \
  --installer-profile "$AWS_PROFILE" \
  --iam-lifecycle-profile "$IAM_LIFECYCLE_AWS_PROFILE" \
  --forbidden-profile "$AWS_ROOT_PROFILE"
terraform -chdir=scenario-a apply .terraform/scenario-a-destroy.tfplan
test -z "$(terraform -chdir=scenario-a state list)"
```

Both values are captured **before** the destroy on purpose: the outputs are
gone once state is empty, and two later steps — the task-definition sweep and
the Container Insights cleanup — need them afterwards. `namespace` is the
run-scoped prefix every resource name is built from; read it rather than
re-deriving it from another name.

The backend bucket is shared by every scenario and workspace, so destroying
Scenario A proves nothing about Scenario B. Census every state object in the
bucket — bootstrap teardown must not proceed while any of them still tracks
resources.

`aws s3api get-object … /dev/stdout` emits **two** JSON documents: the object
body first, then the CLI's own response metadata. `jq -e` takes its exit status
from the *last* value it produced, so a bare `jq -e '(.resources // []) |
length == 0'` grades the metadata document — which has no `.resources`, always
satisfies the test, and makes the census pass unconditionally. Slurp the stream
and assert against the first document, which is the state:

```bash
for key in $(aws s3api list-objects-v2 \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --bucket "$STATE_BUCKET" \
  --query 'Contents[].Key' --output text); do
  test "$key" != None || break
  aws s3api get-object \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --bucket "$STATE_BUCKET" --key "$key" /dev/stdout \
    | jq -e -s '(.[0].resources // []) | length == 0' >/dev/null || {
    printf 'state object %s still tracks live resources\n' "$key" >&2
    exit 1
  }
done
```

Confirm the gate discriminates before resting on it — a census that cannot fail
is worse than none, because it reads as proof. Both directions:

```bash
printf '%s\n%s\n' '{"resources":[{"a":1}]}' '{"ETag":"x"}' \
  | jq -e -s '(.[0].resources // []) | length == 0' >/dev/null; echo "tracked -> $? (want 1)"
printf '%s\n%s\n' '{"resources":[]}' '{"ETag":"x"}' \
  | jq -e -s '(.[0].resources // []) | length == 0' >/dev/null; echo "empty   -> $? (want 0)"
```

The versioned backend bucket deliberately carries no `force_destroy`:
bootstrap destroy refuses a non-empty bucket, so a live state can never be
erased by teardown ordering alone. Empty the bucket explicitly — object
versions and delete markers both — only after the census above has passed:

```bash
while :; do
  batch=$(aws s3api list-object-versions \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --bucket "$STATE_BUCKET" --max-items 1000 --output json \
    | jq -c '{Objects: ([.Versions[]?, .DeleteMarkers[]?] | map({Key, VersionId})), Quiet: true}')
  test "$(jq '.Objects | length' <<<"$batch")" -gt 0 || break
  aws s3api delete-objects \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --bucket "$STATE_BUCKET" --delete "$batch" >/dev/null
done

terraform -chdir=bootstrap plan -destroy \
  -var-file=../examples/bootstrap.tfvars \
  -out=.terraform/bootstrap-destroy.tfplan
terraform -chdir=bootstrap show -no-color .terraform/bootstrap-destroy.tfplan
terraform -chdir=bootstrap show -json .terraform/bootstrap-destroy.tfplan >.terraform/bootstrap-destroy.json
python3 scripts/verify-terraform-profiles.py plan \
  --plan-json .terraform/bootstrap-destroy.json \
  --installer-profile "$AWS_PROFILE" \
  --iam-lifecycle-profile "$IAM_LIFECYCLE_AWS_PROFILE" \
  --forbidden-profile "$AWS_ROOT_PROFILE"
terraform -chdir=bootstrap apply .terraform/bootstrap-destroy.tfplan
test -z "$(terraform -chdir=bootstrap state list)"
```

### Task definitions registered outside Terraform

`terraform destroy` deregisters only the revisions Terraform holds in state —
one revision per family, the current one. Any revision registered out of band
during the run is invisible to it and survives a destroy that reports complete.
Round 4 left twelve ACTIVE revisions behind this way (`elspeth-a9967c55ff`).

This is not a cost leak; task definitions are not chargeable. It is a hygiene
and evidence-accuracy defect: the terminal tag query below reads as proof the
account is clean, and on its own it is not.

Do not enumerate the families. The scenario module defines eight, a run may
register more, and a hand-typed list silently under-sweeps the ones it forgot.
Discover them from the live registry, filtered by this run's namespace:

```bash
active_revision_arns() (
  set -Eeuo pipefail
  aws ecs list-task-definitions \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --family-prefix "$1" --status ACTIVE --output json \
    | jq -r --arg family "$1" \
      '.taskDefinitionArns[]? | select((split("/") | last | split(":") | first) == $family)'
)

sweep_run_task_definitions() (
  set -Eeuo pipefail
  local namespace families family arns arn all_families foreign=""
  local family_count=0 deregistered=0 remaining=0 foreign_count=0
  namespace="${NAMESPACE:?capture the namespace before the Scenario A destroy}"

  # Every AWS result is assigned to a variable before it is used. Under
  # `set -e` a failing command substitution aborts only in a plain assignment:
  # inside `for x in $(...)` or `$(( ))` the failure is swallowed, so an
  # AccessDenied or a throttle would read as an empty, clean account.
  #
  # ListTaskDefinitionFamilies documents familyPrefix as a prefix;
  # ListTaskDefinitions documents it as the full family name. Discover
  # families with the first, then query each with its exact name, so the sweep
  # is correct under either reading.
  families="$(aws ecs list-task-definition-families \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --family-prefix "$namespace-" --status ALL \
    --query 'families[]' --output text)"

  for family in $families; do
    test "$family" != None || continue
    family_count=$((family_count + 1))
    arns="$(active_revision_arns "$family")"
    for arn in $arns; do
      aws ecs deregister-task-definition \
        --profile "$AWS_PROFILE" --region "$AWS_REGION" \
        --task-definition "$arn" >/dev/null
      deregistered=$((deregistered + 1))
    done
  done

  # Re-list after the sweep. The assertion is on live account state, never on
  # the loop's own bookkeeping: a deregister that did not take effect must
  # fail this step rather than be counted as a success.
  for family in $families; do
    test "$family" != None || continue
    arns="$(active_revision_arns "$family")"
    remaining=$((remaining + $(printf '%s' "$arns" | grep -c . || true)))
  done

  # Families outside this run's namespace are enumerated and reported, never
  # touched. Deleting them is an operator decision, not a teardown step.
  all_families="$(aws ecs list-task-definition-families \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" --status ACTIVE \
    --query 'families[]' --output text)"
  for family in $all_families; do
    test "$family" != None || continue
    case "$family" in "${namespace}-"*) continue ;; esac
    foreign="${foreign:+$foreign }$family"
    foreign_count=$((foreign_count + 1))
  done

  printf 'task_definition_sweep namespace=%s families=%s deregistered=%s remaining_active=%s foreign_families=%s\n' \
    "$namespace" "$family_count" "$deregistered" "$remaining" "$foreign_count"
  for family in $foreign; do
    arns="$(active_revision_arns "$family")"
    printf 'foreign_task_definition_family %s active_revisions=%s\n' \
      "$family" "$(printf '%s' "$arns" | grep -c . || true)"
  done
  test "$remaining" = 0
)

sweep_run_task_definitions
```

Record the `task_definition_sweep` line in the run evidence. On a teardown with
nothing registered out of band it reads:

```
task_definition_sweep namespace=<ns> families=8 deregistered=0 remaining_active=0 foreign_families=0
```

`families=8` is expected and is not the sweep over-reaching: a family survives
the deregistration of every one of its revisions, so `--status ALL` still lists
all eight the module defines. `deregistered` and `remaining_active` are both
reported because they disagree exactly when the sweep believed it worked and
the account did not.

The criterion is **zero ACTIVE**, not zero revisions.
`deregister-task-definition` leaves the revision INACTIVE by design and there is
no way to deregister without producing one, so even a flawless Terraform destroy
leaves INACTIVE revisions behind — a zero-revisions test could never pass, which
is the mirror image of a test that could never fail.
`delete-task-definitions` accepts only INACTIVE revisions and leaves a
`DELETE_IN_PROGRESS` window that cannot be proven closed inside the teardown.
ACTIVE is also the status that carries the operational risk: only an ACTIVE
revision can be run or referenced by a new service.

Confirm the gate discriminates before resting on it. Both directions, plus the
shape that must **not** trip it:

```bash
FILTER='[.taskDefinitionArns[]? | select((split("/") | last | split(":") | first) == $family)] | length == 0'
printf '%s\n' '{"taskDefinitionArns":["arn:aws:ecs:'"$AWS_REGION"':1:task-definition/a-x-web:7"]}' \
  | jq -e --arg family a-x-web "$FILTER" >/dev/null; echo "survivor     -> $? (want 1)"
printf '%s\n' '{"taskDefinitionArns":[]}' \
  | jq -e --arg family a-x-web "$FILTER" >/dev/null; echo "clean        -> $? (want 0)"
printf '%s\n' '{"taskDefinitionArns":["arn:aws:ecs:'"$AWS_REGION"':1:task-definition/a-x-web-extra:1"]}' \
  | jq -e --arg family a-x-web "$FILTER" >/dev/null; echo "other family -> $? (want 0)"
```

### Container Insights log-group orphan (R2-D3, elspeth-a229c247a1)

ECS's service-linked role re-creates
`/aws/ecs/containerinsights/${ECS_CLUSTER}/performance` a few minutes after
the cluster goes INACTIVE — a final Container Insights metrics flush. That
recreated log group is untagged and lives outside every Terraform state, so
the tag query below cannot see it, and a same-namespace redeploy retry can
hit `ResourceAlreadyExistsException` on `CreateLogGroup`.

Point-in-time absence is not terminal: the final flush may not have landed.
Poll for the delayed recreation, delete every exact-name appearance, and
require one full quiet window whether the group was already absent or was
deleted here. Every deletion restarts the quiet window. The separate maximum
bounds the procedure; if a late recreation leaves too little time to prove a
full quiet window, cleanup fails closed and stays open for the named owner.
Any AWS API error also fails under the strict shell instead of being
suppressed as if it were `ResourceNotFoundException`:

```bash
ELSPETH_CONTAINER_INSIGHTS_MAX_WAIT_SECONDS=1200
ELSPETH_CONTAINER_INSIGHTS_POLL_INTERVAL_SECONDS=10
ELSPETH_CONTAINER_INSIGHTS_QUIET_SECONDS=600

cleanup_container_insights_log_group() (
  set -Eeuo pipefail
  local log_group listing count started_at quiet_started_at samples=0 deletions=0
  local elapsed quiet_elapsed remaining sleep_seconds
  log_group="/aws/ecs/containerinsights/${ECS_CLUSTER}/performance"
  test "$ELSPETH_CONTAINER_INSIGHTS_MAX_WAIT_SECONDS" -gt 0
  test "$ELSPETH_CONTAINER_INSIGHTS_POLL_INTERVAL_SECONDS" -gt 0
  test "$ELSPETH_CONTAINER_INSIGHTS_QUIET_SECONDS" -gt 0
  test "$ELSPETH_CONTAINER_INSIGHTS_QUIET_SECONDS" -le "$ELSPETH_CONTAINER_INSIGHTS_MAX_WAIT_SECONDS"

  started_at=$SECONDS
  quiet_started_at=-1
  while true; do
    listing="$(aws logs describe-log-groups \
      --profile "$AWS_PROFILE" --region "$AWS_REGION" \
      --log-group-name-prefix "$log_group" --output json)"
    count="$(jq --arg name "$log_group" \
      '[.logGroups[]? | select(.logGroupName == $name)] | length' \
      <<<"$listing")"
    test "$count" = 0 || test "$count" = 1
    samples=$((samples + 1))
    if test "$count" = 1; then
      aws logs delete-log-group \
        --profile "$AWS_PROFILE" --region "$AWS_REGION" \
        --log-group-name "$log_group"
      deletions=$((deletions + 1))
      quiet_started_at=$SECONDS
    else
      if test "$quiet_started_at" -lt 0; then
        quiet_started_at=$SECONDS
      fi
      elapsed=$((SECONDS - started_at))
      quiet_elapsed=$((SECONDS - quiet_started_at))
      if test "$quiet_elapsed" -ge "$ELSPETH_CONTAINER_INSIGHTS_QUIET_SECONDS"; then
        printf 'container_insights_log_group_stable elapsed_seconds=%s quiet_seconds=%s samples=%s deletions=%s\n' \
          "$elapsed" "$quiet_elapsed" "$samples" "$deletions"
        return 0
      fi
    fi

    elapsed=$((SECONDS - started_at))
    if test "$elapsed" -ge "$ELSPETH_CONTAINER_INSIGHTS_MAX_WAIT_SECONDS"; then
      printf 'container_insights_log_group_not_stabilized elapsed_seconds=%s samples=%s deletions=%s\n' \
        "$elapsed" "$samples" "$deletions" >&2
      return 1
    fi
    remaining=$((ELSPETH_CONTAINER_INSIGHTS_MAX_WAIT_SECONDS - elapsed))
    sleep_seconds=$ELSPETH_CONTAINER_INSIGHTS_POLL_INTERVAL_SECONDS
    if test "$sleep_seconds" -gt "$remaining"; then
      sleep_seconds=$remaining
    fi
    sleep "$sleep_seconds"
  done
)

cleanup_container_insights_log_group
```

If a later same-namespace redeploy still hits
`ResourceAlreadyExistsException`, first confirm the exact orphan exists. Do
not reuse the failed saved plan: an apply may already have changed remote
objects and a saved plan has sealed its variable values. Generate and inspect
a replacement plan with adoption enabled, then apply only that plan file; do
not add `-var` to the saved-plan apply:

```bash
ORPHAN_LOG_GROUP="/aws/ecs/containerinsights/${ECS_CLUSTER}/performance"
test "$(aws logs describe-log-groups \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --log-group-name-prefix "$ORPHAN_LOG_GROUP" \
  --query "length(logGroups[?logGroupName=='$ORPHAN_LOG_GROUP'])" \
  --output text)" = 1

terraform -chdir=scenario-a plan \
  -var-file=../examples/scenario-a.tfvars \
  -var='adopt_container_insights_log_group=true' \
  -out=.terraform/scenario-a-adopt.tfplan
terraform -chdir=scenario-a show -no-color .terraform/scenario-a-adopt.tfplan
terraform -chdir=scenario-a show -json .terraform/scenario-a-adopt.tfplan >.terraform/scenario-a-adopt.json
python3 scripts/verify-terraform-profiles.py plan \
  --plan-json .terraform/scenario-a-adopt.json \
  --installer-profile "$AWS_PROFILE" \
  --iam-lifecycle-profile "$IAM_LIFECYCLE_AWS_PROFILE" \
  --forbidden-profile "$AWS_ROOT_PROFILE"
terraform -chdir=scenario-a apply .terraform/scenario-a-adopt.tfplan
```

### Remove the Step 2 policies

Both destroys ran under the Step 2 policies, and so did the task-definition
sweep and the Container Insights cleanup — the sweep needs the exact
`ecs:ListTaskDefinitionFamilies` and `ecs:ListTaskDefinitions` actions plus
`ecs:DeregisterTaskDefinition`, the cleanup needs `logs:DeleteLogGroup`, and
all three grants live in the policies about to be deleted. Those policies had
to outlive every one of those steps, which is why this is the first point at
which they can go.

Only now, remove the five policies recorded in the operator change record:
detach the four `installer-*` policies from the normal installer principal
and delete them, and detach and delete the lifecycle policy through the
account's trusted IAM administration path. Do not rely on the tag query below
to find them — they were created outside Terraform and may carry no run tag.
`aws iam delete-policy` refuses while any attachment or non-default policy
version remains, so a successful delete confirms the detach.

### Confirm the account is clean

Finally, query the run tag:

```bash
aws resourcegroupstaggingapi get-resources \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --tag-filters "Key=ACCEPTANCE_RUN_ID,Values=$RUN_ID" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text
```

Expected result: no live resource ARNs. The tagging API can briefly retain
non-billable tombstones, so reconcile any result against its owning service API
before declaring a survivor. Reconcile on **status**, not on whether the
resource is chargeable: a task-definition revision that is still ACTIVE is a
survivor even though it costs nothing. Grep the output for `:task-definition/`
specifically and reconcile any hit against the `task_definition_sweep` line.
The two views are scoped differently — the query sees tagged revisions, the
sweep sees the namespace prefix — so do not expect equal lists: after a sweep
that exited 0, a hit must reconcile to an INACTIVE tombstone or a
foreign-namespace revision, and anything else is a real survivor. Do not
delete an untagged or differently tagged resource merely because its name
resembles this run.

Completion requires empty Scenario A and bootstrap state, no live run-tagged
resources, no matching Container Insights log group (the tag query above
cannot see it — confirm separately with `aws logs describe-log-groups
--profile "$AWS_PROFILE" --region "$AWS_REGION" --log-group-name-prefix
"/aws/ecs/containerinsights/${ECS_CLUSTER}"` and expect an empty
`logGroups` list), **zero ACTIVE task-definition revisions under
`$NAMESPACE`** — `sweep_run_task_definitions` exited 0 and reported
`remaining_active=0` — none of the four Step 2 installer or lifecycle policies
remaining, and no active ECS tasks, Aurora instances, ALBs, NAT gateways, EFS
filesystems, Secrets Manager secrets, or retained ECR images owned by this run.

`foreign_families` is reported, not gated. A task-definition family outside
this run's namespace belongs to a different run and is left for the operator —
the same rule that forbids deleting a resource because its name resembles this
one. Record the count and the per-family lines in the run evidence so an
accumulating orphan namespace is found by design rather than by accident.
