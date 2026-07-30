# ELSPETH AWS ECS Terraform package

This directory is the supported Terraform source for a disposable ELSPETH
installation on ECS/Fargate. It was recovered from a previously exercised
deployment topology, then made portable and brought up to the current
installation contract. It is source code, not a claim about any current AWS
environment.

## Choose a topology

### Scenario A: cold install (recommended)

`scenario-a/` is the normal path. It creates a fresh VPC, ECS service, Aurora
PostgreSQL cluster, separate session and Landscape databases, EFS data storage,
S3 object storage, logs/metrics/traces, and local ELSPETH authentication.

### Scenario B: OIDC acceptance variant

`scenario-b/` exists only for acceptance of the Cognito/OIDC path and upgrade
or rollback exercises. Its state key, namespace, networking, database, secrets,
and Cognito hostname are isolated from Scenario A. Do not start here for a cold
installation. Its tfvars must name the nonempty Cognito subject that the
acceptance run owns; the resolved inventory binds that subject to the
Terraform-owned pool.

Both scenarios create a disposable self-signed ALB certificate. It is valid for
only 24 hours. Terraform outputs its CA certificate so a client can trust it
temporarily; this is not a production certificate strategy.

## Prerequisites and identity checks

- Terraform `>= 1.14, < 2.0`.
- Explicit normal-installer and IAM-lifecycle AWS profiles, plus the account and
  region chosen by the operator.
- AWS credentials available through both named SDK/CLI profiles.
- A digest-pinned ELSPETH application image.
- Two distinct `bedrock/...` provider IDs plus the exact inference-profile and
  foundation-model ARNs those IDs may invoke.
- An active Bedrock model-access agreement for each chosen model id in the
  target account. Confirm with `aws bedrock get-foundation-model-availability
  --model-id <id>` that `agreementAvailability` reports `AVAILABLE`.
  Third-party models may additionally require an AWS Marketplace
  subscription the account must complete first; first-party Amazon models
  generally have the agreement by default. A correct IAM policy still fails
  invocation until this agreement exists.

The normal provider still requires an explicit AWS profile, account, and region;
the lifecycle provider adds a second explicit profile for the same account and
region.

Before every init, apply, or destroy, verify the named profile, identity, and
region explicitly:

```sh
export AWS_PROFILE=REPLACE_WITH_AWS_PROFILE
export IAM_LIFECYCLE_AWS_PROFILE=REPLACE_WITH_DISTINCT_IAM_LIFECYCLE_AWS_PROFILE
export AWS_REGION=REPLACE_WITH_AWS_REGION
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity
aws --profile "$IAM_LIFECYCLE_AWS_PROFILE" --region "$AWS_REGION" sts get-caller-identity
aws --profile "$AWS_PROFILE" configure get region
aws --profile "$IAM_LIFECYCLE_AWS_PROFILE" configure get region
```

Compare both returned accounts with `aws_account_id` and both configured
regions with `aws_region`. Set the same required `aws_profile` and
`iam_lifecycle_aws_profile` values in the bootstrap and scenario tfvars. Do not
rely on an implicit profile, region, or remembered account. Each provider binds
its resources to its explicit profile while `allowed_account_ids`
independently protects the account boundary.

### Producing the candidate image

Published ELSPETH images come from the repository's `Build and Push` workflow
(`.github/workflows/build-push.yaml`). It publishes `sha-<commit>` tags for
every trusted merge to `main`, and it publishes a release or release-candidate
image only when the operator pushes a git tag matching `v*` — for example
`v0.7.2`, or the pre-release shape `v0.7.2-RC-280726` for a release-candidate
handoff. The workflow builds, smoke-tests, and then promotes the verified
digest to `ghcr.io/<owner>/elspeth:<git-tag>`, where `<git-tag>` is the literal
tag name including the leading `v` and any `-RC-...` suffix. No
release-candidate image exists until that git tag is pushed: publication is an
explicit operator action, not a side effect of merging a release branch.
Resolve whichever tag you install from to its immutable digest and put the
digest reference in the tfvars.

### Installer policy and task-role boundary

The package deliberately splits IAM authority across two principals:

- `iam/installer-policy.json.tftpl` is for the normal installer. It separates
  discovery reads from mutations, limits named resources, manages only the
  known inline and managed role-policy bindings, and permits `iam:PassRole`
  only to `ecs-tasks.amazonaws.com`. It cannot create or delete the generated
  roles, change their trust or boundary, or manage the boundary policy.
- `iam/lifecycle-policy.json.tftpl` is for a separate IAM lifecycle principal.
  It can create, tag, and delete only the four bounded scenario-role patterns
  and can create, version, and delete only the exact run boundary. Explicit
  denies prevent it from adding role permissions, passing or assuming a role,
  or starting an ECS task.

Render both policies for one account, region, and run before attaching them to
their respective principals:

```sh
export aws_account_id=REPLACE_WITH_12_DIGIT_ACCOUNT
export aws_region=REPLACE_WITH_AWS_REGION
export run_id=REPLACE_WITH_LOWERCASE_UUID
export backend_state_bucket=REPLACE_WITH_EXACT_BOOTSTRAP_STATE_BUCKET
export ecr_repository=REPLACE_WITH_EXACT_BOOTSTRAP_APP_REPOSITORY
export cloudwatch_agent_ecr_repository=REPLACE_WITH_EXACT_BOOTSTRAP_AGENT_REPOSITORY
export iam_permissions_boundary_arn="arn:aws:iam::${aws_account_id}:policy/elspeth-${run_id}-ecs-boundary"
scenario_a_namespace="a-$(printf '%s\0A' "$run_id" | sha256sum | cut -c1-20)"
scenario_b_namespace="b-$(printf '%s\0B' "$run_id" | sha256sum | cut -c1-20)"
compact_run_id="$(printf '%s' "$run_id" | tr -d '-')"
export scenario_a_bucket="elspeth-${scenario_a_namespace}-$(printf '%.12s' "$compact_run_id")"
export scenario_b_bucket="elspeth-${scenario_b_namespace}-$(printf '%.12s' "$compact_run_id")"
mkdir -p bootstrap/.terraform
envsubst '${aws_account_id} ${aws_region} ${run_id} ${backend_state_bucket} ${ecr_repository} ${cloudwatch_agent_ecr_repository} ${scenario_a_bucket} ${scenario_b_bucket}' \
  < iam/installer-policy.json.tftpl \
  > bootstrap/.terraform/installer-policy.json
envsubst '${aws_account_id} ${run_id} ${iam_permissions_boundary_arn}' \
  < iam/lifecycle-policy.json.tftpl \
  > bootstrap/.terraform/iam-lifecycle-policy.json
```

Inspect the rendered JSON and attach each policy using the account's normal IAM
administration path. Keep the two profiles backed by distinct principals for
the supported least-privilege installation. A purpose-built root acceptance
profile may set both provider variables to `elspeth-acceptance` for a smoke
exercise, but that deliberately gives up the privilege separation and is not
the supported least-privilege posture. If an account-specific prerequisite is
missing, use a trusted administrator only to install or amend these policies in
the dedicated disposable account; do not run Terraform with an
account-administrator wildcard policy.

The bucket derivation above is the same deterministic formula used by the
scenario module. The rendered policy consequently limits S3 bucket/object
mutations and ECR image push or force-delete operations to the exact bootstrap,
Scenario A, and Scenario B names for this run. The three bootstrap names must
match `examples/bootstrap.tfvars` exactly.

Bootstrap creates the custom run-scoped boundary named by
`iam_permissions_boundary_arn`; copy the `iam_permissions_boundary_arn`
bootstrap output into each scenario tfvars file. Both generated ECS roles must
use that boundary. The normal installer can add the package's narrow role
policies and pass the roles but cannot widen or remove their boundary. The
lifecycle principal can create or delete the bounded roles but cannot add
permissions or activate them. Effective task permissions remain the
intersection of the lifecycle-principal-controlled boundary and the normal
installer's task/execution policies. The boundary permits only the package
runtime surfaces, including destination foundation models needed by the exact
cross-region Bedrock inference-profile inputs.

Networking relationships, EFS mount targets, Cognito children, EventBridge
targets, and X-Ray resources use supported ARN and run-tag conditions. The
`DedicatedAccountOnlyUntaggedMutations` residual contains only CloudWatch Logs
account-level resource-policy mutations, whose API does not support
resource-level authorization. The statement is region-limited and contains no
wildcard service actions, but another principal's Logs resource policy in that
region could still be affected. This installer policy is therefore supported
only in a dedicated empty account and is **not supported in a shared account**.

## 1. Bootstrap state and image repositories

Copy `examples/bootstrap.tfvars.example` to an ignored
`examples/bootstrap.tfvars`, replace every placeholder, then:

```sh
terraform -chdir=bootstrap init
terraform -chdir=bootstrap apply -var-file=../examples/bootstrap.tfvars
terraform -chdir=bootstrap output -raw iam_permissions_boundary_arn
```

Bootstrap state is local by design because it creates the remote state bucket.
Preserve that state until both scenario states have been destroyed. Keep both
credential profiles available for the entire lifecycle. Destroy Scenario A and
Scenario B first, then destroy bootstrap last so the boundary, state bucket,
and repositories remain available throughout scenario teardown.

The bootstrap creates separate ECR repositories for ELSPETH and the
shell-bearing CloudWatch agent. Temporary application tags may expire. The
agent repository expires only untagged images after 30 days. Deploy the
CloudWatch agent by digest (`repository@sha256:...`), never by tag.

Build `cloudwatch-agent-image/Dockerfile` with
`--build-arg ELSPETH_RELEASE_SHA="$CANDIDATE_SHA"`, publish it to that dedicated
repository under a retained tag, resolve the immutable digest, and place the
digest reference in `cloudwatch_agent_image`. Set
`cloudwatch_agent_ecr_repository` from the bootstrap output of the same name;
set `candidate_ecr_repository` from the bootstrap `ecr_repository` output. The
scenario refuses foreign registry, account, region, or repository references,
then pulls every credential-bearing image and verifies its baked
`org.opencontainers.image.revision` label before Terraform may register any
task definition. The resolved inventory also binds the digest and SHA-256
hashes of both tracked telemetry configuration files.

## 2. Generate partial backend inputs

The scenario roots contain only `backend "s3" {}`. Generate ignored
`.tfbackend` inputs from the matching examples and replace the bucket and
region/profile placeholders:

```sh
cp examples/scenario-a.s3.tfbackend.example examples/scenario-a.s3.tfbackend
cp examples/scenario-b.s3.tfbackend.example examples/scenario-b.s3.tfbackend
```

The A and B files use separate state keys, S3 server-side encryption, and S3
native state locking (`use_lockfile = true`). Their explicit `profile` must
match the scenario tfvars. Do not make their keys equal.

## 3. Install Scenario A

Copy `examples/scenario-a.tfvars.example` to an ignored
`examples/scenario-a.tfvars`, replace every placeholder, and check that both
Bedrock provider IDs are present and distinct.

Scenario A has no rollback or acceptance-coordinator inputs. Its compatibility
inventory derives the candidate baseline and absolute tracked source paths from
the package itself. `scenario-a/codeblind-compatibility.json` records the
standalone-install facts and the absence of a pre-existing transaction-search
baseline. It is deterministic package metadata, not an acceptance binding
artifact. Scenario B retains its acceptance-only inputs.

```sh
terraform -chdir=scenario-a init \
  -backend-config=../examples/scenario-a.s3.tfbackend
terraform -chdir=scenario-a workspace show
terraform -chdir=scenario-a workspace select default
terraform -chdir=scenario-a plan \
  -var-file=../examples/scenario-a.tfvars
terraform -chdir=scenario-a apply \
  -var-file=../examples/scenario-a.tfvars
terraform -chdir=scenario-a output
```

Use only an explicitly selected workspace. The `default` workspace is the
documented cold-install choice. Re-run the account and region checks before
apply.

The service is initially registered with desired count zero so an uninitialised
database cannot enter a failing restart loop. The schema-init task definition
uses schema-owner database URLs and is reserved for
`doctor aws-ecs --init-schema --json`. The separate runtime doctor definition
uses the same runtime-only database URLs as the web service and runs
`doctor aws-ecs --json`.

Provision the EFS runtime storage **before** either doctor task. The doctor
checks `payload_store_writable` and `blob_writable` by probing directories that
do not exist on a freshly created file system, so on a cold install the
schema-init doctor exits non-zero with `FileNotFoundError` until the
`provision-storage` one-shot has created them:

```sh
export AWS_PROFILE=REPLACE_WITH_AWS_PROFILE
export AWS_REGION=REPLACE_WITH_AWS_REGION
ECS_CLUSTER=$(terraform -chdir=scenario-a output -raw cluster_name)
NETWORK=$(terraform -chdir=scenario-a output -raw runtime_doctor_network_configuration)
PAYLOAD_VERIFIER_TASK_DEFINITION=$(terraform -chdir=scenario-a output -json resolved_inventory \
  | jq -er '.values.PAYLOAD_VERIFIER_TASK_DEFINITION')
PAYLOAD_TASK_ARN=$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --task-definition "$PAYLOAD_VERIFIER_TASK_DEFINITION" \
  --launch-type FARGATE \
  --network-configuration "$NETWORK" \
  --count 1 \
  --query 'tasks[0].taskArn' \
  --output text)
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs wait tasks-stopped \
  --cluster "$ECS_CLUSTER" --tasks "$PAYLOAD_TASK_ARN"
test "$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$PAYLOAD_TASK_ARN" \
  --query 'tasks[0].containers[?name==`elspeth-web`].exitCode | [0]' \
  --output text)" = 0
```

Then run the two doctor tasks in that order with the explicit task definitions,
network configurations, and command overrides:

```sh
SCHEMA_TASK_DEFINITION=$(terraform -chdir=scenario-a output -raw schema_init_doctor_task_definition_arn)
SCHEMA_NETWORK=$(terraform -chdir=scenario-a output -raw schema_init_doctor_network_configuration)
SCHEMA_OVERRIDES=$(terraform -chdir=scenario-a output -raw schema_init_doctor_overrides)
SCHEMA_TASK_ARN=$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --task-definition "$SCHEMA_TASK_DEFINITION" \
  --launch-type FARGATE \
  --network-configuration "$SCHEMA_NETWORK" \
  --overrides "$SCHEMA_OVERRIDES" \
  --count 1 \
  --query 'tasks[0].taskArn' \
  --output text)
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs wait tasks-stopped \
  --cluster "$ECS_CLUSTER" --tasks "$SCHEMA_TASK_ARN"
test "$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$SCHEMA_TASK_ARN" \
  --query 'tasks[0].containers[?name==`doctor`].exitCode | [0]' \
  --output text)" = 0

RUNTIME_TASK_DEFINITION=$(terraform -chdir=scenario-a output -raw runtime_doctor_task_definition_arn)
RUNTIME_NETWORK=$(terraform -chdir=scenario-a output -raw runtime_doctor_network_configuration)
RUNTIME_OVERRIDES=$(terraform -chdir=scenario-a output -raw runtime_doctor_overrides)
RUNTIME_TASK_ARN=$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs run-task \
  --cluster "$ECS_CLUSTER" \
  --task-definition "$RUNTIME_TASK_DEFINITION" \
  --launch-type FARGATE \
  --network-configuration "$RUNTIME_NETWORK" \
  --overrides "$RUNTIME_OVERRIDES" \
  --count 1 \
  --query 'tasks[0].taskArn' \
  --output text)
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs wait tasks-stopped \
  --cluster "$ECS_CLUSTER" --tasks "$RUNTIME_TASK_ARN"
test "$(aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs describe-tasks \
  --cluster "$ECS_CLUSTER" --tasks "$RUNTIME_TASK_ARN" \
  --query 'tasks[0].containers[?name==`doctor`].exitCode | [0]' \
  --output text)" = 0
```

Both task exit codes must be `0`. Then print, inspect, and explicitly run
`service_enable_command`; never enable the service after only the privileged
schema-init check.

The service lifecycle ignores later desired-count and task-definition changes
so ordinary image deployments remain an explicit operator action.

### Source-free post-enable acceptance

The Terraform package and the candidate image are sufficient for acceptance;
do not require a source checkout after apply. Run the following immediately
after the two doctor tasks pass. It selects the candidate task definition,
waits for the service and target, and proves both HTTP gates.

The acceptance directory created below is later bind-mounted into the candidate
image, whose process runs as UID/GID 1654. Grant that UID access explicitly:
the acceptance client writes its state file into the mount and reads the CA
from it, and an owner-only directory
belonging to a different UID makes both fail. Acceptance failures are
self-describing: the client's error envelope names the acceptance `step` that
was executing and a closed-vocabulary `error_code` — a missing grant here
surfaces as `state_file_unwritable` or `ca_unreadable` rather than an opaque
internal error.

```sh
acceptance_dir=$(mktemp -d -p /tmp elspeth-source-free-acceptance.XXXXXX)
chmod 700 "$acceptance_dir"
setfacl -m u:1654:rwx "$acceptance_dir"
trap 'rm -rf -- "$acceptance_dir"' EXIT

INVENTORY=$(terraform -chdir=scenario-a output -json resolved_inventory)
ECS_CLUSTER=$(terraform -chdir=scenario-a output -raw cluster_name)
ECS_SERVICE=$(terraform -chdir=scenario-a output -raw service_name)
PUBLIC_URL=$(terraform -chdir=scenario-a output -raw public_url)
CANDIDATE_TASK_DEFINITION=$(printf '%s' "$INVENTORY" | jq -er '.values.CANDIDATE_TASK_DEFINITION')
TARGET_GROUP_ARN=$(printf '%s' "$INVENTORY" | jq -er '.values.TARGET_GROUP_ARN')
CANDIDATE_IMAGE=$(aws ecs describe-task-definition \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --query 'taskDefinition.containerDefinitions[?name==`elspeth-web`].image | [0]' \
  --output text)
terraform -chdir=scenario-a output -raw acceptance_tls_ca_pem >"$acceptance_dir/ca.pem"
chmod 600 "$acceptance_dir/ca.pem"
# The in-container client reads this CA as UID 1654. The certificate is public
# material (the matching private key never leaves encrypted remote state), so
# grant that read rather than widening the mode.
setfacl -m u:1654:r "$acceptance_dir/ca.pem"

aws ecs update-service \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --desired-count 1 --force-new-deployment >/dev/null
aws ecs wait services-stable \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
aws elbv2 describe-target-health \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --target-group-arn "$TARGET_GROUP_ARN" \
  --output json | jq -e \
  '[.TargetHealthDescriptions[] | select(.TargetHealth.State == "healthy")] | length == 1'
curl --fail --silent --show-error --cacert "$acceptance_dir/ca.pem" \
  "$PUBLIC_URL/api/health" | jq -e '.status == "ok"'
curl --fail --silent --show-error --cacert "$acceptance_dir/ca.pem" \
  "$PUBLIC_URL/api/ready" | jq -e '.ready == true'
```

Re-run the two EFS-backed one-shot definitions now that the service is serving.
Both must stop with exit code zero; `provision-storage` proves the runtime UID
can still create, fsync, read, and remove bounded probes alongside a live web
task (it already ran before the doctors, above), while `verify-local-auth`
proves the mounted auth store is readable through the intended local-auth
contract:

```sh
NETWORK=$(terraform -chdir=scenario-a output -raw runtime_doctor_network_configuration)
run_one_shot() {
  task_definition=$1
  expected_command=$2
  task_arn=$(aws ecs run-task \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" --task-definition "$task_definition" \
    --launch-type FARGATE --network-configuration "$NETWORK" --count 1 \
    --query 'tasks[0].taskArn' --output text) || {
    printf '%s: FAILED (run-task)\n' "$expected_command" >&2
    return 1
  }
  aws ecs wait tasks-stopped \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" --tasks "$task_arn" || {
    printf '%s: FAILED (wait tasks-stopped)\n' "$expected_command" >&2
    return 1
  }
  test "$(aws ecs describe-tasks \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" --tasks "$task_arn" \
    --query 'tasks[0].containers[?name==`elspeth-web`].exitCode | [0]' \
    --output text)" = 0 || {
    printf '%s: FAILED (nonzero container exit code)\n' "$expected_command" >&2
    return 1
  }
  printf '%s: ok\n' "$expected_command"
}
run_one_shot \
  "$(printf '%s' "$INVENTORY" | jq -er '.values.PAYLOAD_VERIFIER_TASK_DEFINITION')" \
  provision-storage
run_one_shot \
  "$(printf '%s' "$INVENTORY" | jq -er '.values.LOCAL_AUTH_VERIFIER_TASK_DEFINITION')" \
  verify-local-auth
```

Use a protected environment file for a disposable local acceptance account.
The candidate image contains the bounded acceptance client, so this creates and
executes a real pipeline, downloads the blob and output, and records only a
non-secret state file. Then replace the running task and re-read the same
database/EFS artifacts to prove durability across task replacement:

```sh
read -r -p 'Disposable acceptance username: ' acceptance_username
read -r -s -p 'Disposable acceptance password: ' acceptance_password
printf '\n'
umask 077
{
  printf 'ELSPETH_ACCEPTANCE_BASE_URL=%s\n' "$PUBLIC_URL"
  printf 'ELSPETH_ACCEPTANCE_USERNAME=%s\n' "$acceptance_username"
  printf 'ELSPETH_ACCEPTANCE_PASSWORD=%s\n' "$acceptance_password"
  printf 'ELSPETH_ACCEPTANCE_REGISTER=1\n'
  printf 'ELSPETH_WEB__DEFAULT_LLM_PROFILE=%s\n' \
    "$(printf '%s' "$INVENTORY" | jq -er '.values.ELSPETH_WEB__DEFAULT_LLM_PROFILE')"
  printf 'ELSPETH_WEB__DATA_DIR=%s\n' \
    "$(printf '%s' "$INVENTORY" | jq -er '.values.ELSPETH_WEB__DATA_DIR')"
  printf 'SSL_CERT_FILE=/acceptance/ca.pem\n'
} >"$acceptance_dir/acceptance.env"

docker pull "$CANDIDATE_IMAGE"
docker run --rm --entrypoint python \
  --env-file "$acceptance_dir/acceptance.env" \
  --mount "type=bind,src=$acceptance_dir,dst=/acceptance" \
  "$CANDIDATE_IMAGE" -m elspeth.web.aws_ecs_acceptance \
  capture --state-file /acceptance/state.json

aws ecs update-service \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --force-new-deployment >/dev/null
aws ecs wait services-stable \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE"
sed -i '/^ELSPETH_ACCEPTANCE_REGISTER=/d' "$acceptance_dir/acceptance.env"
docker run --rm --entrypoint python \
  --env-file "$acceptance_dir/acceptance.env" \
  --mount "type=bind,src=$acceptance_dir,dst=/acceptance" \
  "$CANDIDATE_IMAGE" -m elspeth.web.aws_ecs_acceptance \
  verify-api --state-file /acceptance/state.json
```

Finally prove the real Composer endpoint and the task role's S3, Bedrock, and
Textract capabilities. The Composer request must return a non-empty assistant
response. ECS Exec must already be enabled and its managed agent must be
`RUNNING`; the Session Manager plugin is a prerequisite. Each in-task verifier
must return an `ELSPETH_ACCEPTANCE_RECEIPT_V1` line and exit zero.
`verify-textract` reads the same `ELSPETH_ACCEPTANCE_S3_BUCKET`,
`ELSPETH_ACCEPTANCE_S3_PREFIX`, and `AWS_REGION` values as `verify-s3` — all
already rendered into the task definition — and proves the packaged
`aws_textract_document_analysis` transform and the task role's
`textract:StartDocumentAnalysis` / `textract:GetDocumentAnalysis` grants with
negative-space probes; no document is ever uploaded or processed:

```sh
LOGIN=$(curl --fail --silent --show-error --cacert "$acceptance_dir/ca.pem" \
  -H 'content-type: application/json' \
  -d "$(jq -nc --arg u "$acceptance_username" --arg p "$acceptance_password" \
    '{username:$u,password:$p}')" "$PUBLIC_URL/api/auth/login")
TOKEN=$(printf '%s' "$LOGIN" | jq -er 'select(.token_type == "bearer") | .access_token')
COMPOSER_SESSION=$(curl --fail --silent --show-error --cacert "$acceptance_dir/ca.pem" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{}' "$PUBLIC_URL/api/sessions" | jq -er '.id')
curl --fail --silent --show-error --max-time 180 --cacert "$acceptance_dir/ca.pem" \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"content":"Create a pipeline that reads a CSV blob and writes JSON without an LLM transform."}' \
  "$PUBLIC_URL/api/sessions/$COMPOSER_SESSION/messages" \
  | jq -e '.message.role == "assistant" and (.message.content | length > 0)'

RUNNING_TASK=$(aws ecs list-tasks \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
  --desired-status RUNNING --query 'taskArns | [0]' --output text)
test "$(aws ecs describe-tasks \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --tasks "$RUNNING_TASK" \
  --query 'tasks[0].enableExecuteCommand' --output text)" = True
test "$(aws ecs describe-tasks \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --tasks "$RUNNING_TASK" \
  --query 'tasks[0].containers[?name==`elspeth-web`].managedAgents[?name==`ExecuteCommandAgent`].lastStatus | [0]' \
  --output text)" = RUNNING
for check in verify-s3 verify-bedrock verify-textract; do
  aws ecs execute-command \
    --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    --cluster "$ECS_CLUSTER" --task "$RUNNING_TASK" \
    --container elspeth-web --interactive \
    --command "python -m elspeth.web.aws_ecs_acceptance $check" \
    | grep -F ELSPETH_ACCEPTANCE_RECEIPT_V1
done
```

`verify-bedrock-guardrails` is not self-contained like the checks above: it
performs a live positive-and-negative call against each Guardrail, so the
operator supplies the probe texts rather than letting the check invent them.
Five values are required on the command itself:
`ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS=1` plus the four safe and blocked probe
texts. The `ELSPETH_LIVE_BEDROCK_{PROMPT,CONTENT}_PROFILE_ALIAS` and
`_EXPECTED_VERSION` inputs are optional when the task definition carries the
Terraform-rendered `ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES` and
`ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES` values: the check defaults each
alias and its expected version from that rendered configuration. The version
default binds only to the rendered alias, so an operator who supplies a
divergent alias must also supply its own expected version. Any input the
check cannot resolve fails with `guardrails_live_inputs_missing`, naming the
absent variables.

The defaulted aliases and versions come straight from the candidate task
definition — read the rendered values as shown below to confirm what the check
will bind to rather than assuming. Each blocked text must genuinely trip its
own filter set: a prompt-injection attempt for the prompt shield (which
screens input) and content matching a configured harm category for content
safety (which screens output). A blocked text that does not trip the Guardrail
fails the check as `guardrails_receipt`, not as a pass.

In the unmodified reference deployment the rendered aliases are
`prompt-approved` and `content-approved` (`modules/scenario/locals.tf`) and each
expected version is `1` — the first immutable version Terraform publishes for
each Guardrail.

Read the rendered aliases and versions first, then pass the five required
assignments on one in-container command. The probe texts contain spaces, so each
value stays double-quoted inside the single-quoted `sh -c` argument:

```sh
aws ecs describe-task-definition \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --query 'taskDefinition.containerDefinitions[?name==`elspeth-web`] | [0].environment' \
  --output json \
  | jq -r '.[] | select(.name | test("BEDROCK_GUARDRAIL")) | "\(.name) = \(.value)"'

aws ecs execute-command \
  --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" --task "$RUNNING_TASK" \
  --container elspeth-web --interactive \
  --command '/bin/sh -c '\''ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS=1 \
ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT="REPLACE_WITH_BENIGN_QUESTION" \
ELSPETH_LIVE_BEDROCK_PROMPT_BLOCKED_TEXT="REPLACE_WITH_PROMPT_INJECTION_ATTEMPT" \
ELSPETH_LIVE_BEDROCK_CONTENT_SAFE_TEXT="REPLACE_WITH_BENIGN_STATEMENT" \
ELSPETH_LIVE_BEDROCK_CONTENT_BLOCKED_TEXT="REPLACE_WITH_TEXT_MATCHING_A_CONFIGURED_HARM_CATEGORY" \
python -m elspeth.web.aws_ecs_acceptance verify-bedrock-guardrails'\''' \
  | grep -F ELSPETH_ACCEPTANCE_RECEIPT_V1
```

To pin a divergent alias instead of the rendered default, add the matching
`ELSPETH_LIVE_BEDROCK_{PROMPT,CONTENT}_PROFILE_ALIAS` and
`_EXPECTED_VERSION` assignments alongside the five above.

The decoded receipt must report `safe_case_passed`, `attack_case_blocked` and
`request_ids_present` true for both controls, `tutorial_profile_ready: true`,
`tutorial_ready: false`, and `landscape_evidence: true`. Both probe texts are
hashed into the receipt, never retained verbatim.

Any failed step is a failed cold install. Do not report acceptance from doctor
checks alone, from a stable service running a different task definition, or
from host-side AWS credentials standing in for the ECS task role.

The ECS task role is the only Bedrock credential source. The package does not
accept static AWS keys, a profile, a custom endpoint, a model gateway, or an
AgentCore setting. Composer model names are ordinary non-secret environment
values. `bedrock:InvokeModel` is limited to the ARNs supplied in tfvars.
Whichever of `composer_model`/`composer_advisor_model` is a cross-region
geography profile also needs a wildcard-region foundation-model grant
(`arn:aws:bedrock:*::foundation-model/<base-model-id>`) alongside the
region-pinned inference-profile ARN, because Bedrock authorizes that call
against the underlying foundation model in whichever region the profile actually
routes to; the module derives this grant automatically, and the run-scoped
permissions boundary already allows the matching wildcard resource so the grant
is not intersected away.

The derivation matches an explicit allowlist in `modules/scenario/locals.tf` —
the geography prefixes `global.`, `us.`, `eu.`, `apac.`, `au.`, `jp.`, `ca.`,
`br.`, `in.` (`bedrock_cross_region_prefixes`) plus a list of known provider
labels (`bedrock_known_provider_prefixes`) — because a model id's leading label
cannot be told apart structurally from a provider label (compare
`au.anthropic.claude-sonnet-4-6` with `zai.glm-4.7-flash`). A configured model
id whose leading dotted label matches neither list fails `terraform plan` with
an error naming the model, instead of silently receiving no wildcard grant and
then denying intermittently at runtime. To clear that error, extend the
matching allowlist in `modules/scenario/locals.tf`, or name the model
explicitly in `bedrock_foundation_model_arns`: the wildcard-region form
(`arn:aws:bedrock:*::foundation-model/<id-without-geography-prefix>`) for a
cross-region geography profile, or the region-pinned foundation-model ARN for
a provider model. Check a geography profile's real destinations with
`aws bedrock get-inference-profile --inference-profile-identifier <id>
--query 'models[].modelArn'` — it routes to more than one region. After
changing a Composer model, confirm the grant landed rather than assuming it:

```sh
aws iam get-role-policy \
  --profile "$AWS_PROFILE" \
  --role-name "$(terraform -chdir=scenario-a output -raw task_role_arn | sed 's|.*/||')" \
  --policy-name "$(terraform -chdir=scenario-a output -raw task_role_arn | sed 's|.*/||' | sed 's|-task-role$|-task-policy|')" \
  --query 'PolicyDocument.Statement[?Sid==`InvokeConfiguredBedrockModels`].Resource' --output json
```

The task role also needs bucket-scoped, unconditioned `s3:ListBucket` on the
acceptance bucket, because without it S3 cannot distinguish a missing object
from a forbidden one and returns `403` instead of `404`; the boundary grants
the matching bucket-level resource.

Aurora creates one administrative database first, then the database bootstrap
task creates independent `elspeth_session` and `elspeth_landscape` databases.
Schema and runtime roles are separate; the runtime role does not own schemas.
This package pins Aurora PostgreSQL `16.13`. That exact version was confirmed
available and orderable with `db.serverless` in `ap-southeast-1`. The variable
validation rejects other engine versions until they are separately verified.

## Immutable RDS trust-root admission

The image must contain `/etc/elspeth/rds/global-bundle.pem` with SHA-256
`e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3`.
Its OCI CA label must be `rds-ca-rsa2048-g1`. Every ELSPETH container in the
task definitions except the web container (`elspeth-web`, candidate and
rollback) must set `readonlyRootFilesystem` to `true`. The web container is
exempt because ECS Exec — which runs the acceptance checks inside it — is
unsupported by AWS with a read-only root filesystem, and its multipart and
telemetry paths write to `/tmp`; its trust root remains immutable through
startup digest verification of the 0444 root-owned baked file.

The schema and runtime doctor JSON must report all of these checks as green
before the web service is enabled:

- `rds_trust_root`
- `session_tls`
- `landscape_tls`
- `session_schema`
- `landscape_schema`

`session_tls` and `landscape_tls` attest only the connection that inspected
each schema: TLS is proven on the same connection the schema probe ran over,
not on every connection a run opens. A `--init-schema` DDL connection uses
the identical URL and `sslmode` posture but is not itself separately probed.

The task definitions and bootstrap must not fetch the CA bundle from AWS's
public RDS truststore endpoint at runtime, and must not stage it under a
world-writable `/tmp` path or under `/var/lib/elspeth/rds-global-bundle.pem`.
Only the baked, root-owned, 0444 image path above is trusted.

OCI digest
`sha256:c5e65357b7470cf1a702eeb084e865f0f5e0e43ab9741b76e872fa7568029700`
predates this contract. It is an acceptance-attempt artifact and is not
eligible for the `v0.7.2-RC-290726` release candidate.

Before promoting a candidate, verify the baked bundle, the OCI CA labels, the
live Aurora CA identifier, and the `readonlyRootFilesystem` split:

```sh
docker pull "$CANDIDATE_IMAGE"
docker buildx imagetools inspect "$CANDIDATE_IMAGE"
test "$(docker inspect --format \
  '{{ index .Config.Labels "io.elspeth.rds-ca-bundle-sha256" }}' \
  "$CANDIDATE_IMAGE")" = \
  e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3
test "$(docker inspect --format \
  '{{ index .Config.Labels "io.elspeth.rds-ca-certificate-identifier" }}' \
  "$CANDIDATE_IMAGE")" = rds-ca-rsa2048-g1

DB_INSTANCE_IDENTIFIER=$(terraform -chdir=scenario-a output -json resolved_inventory \
  | jq -r '.orphan_sweep.rds_db_instance_identifiers[0]')
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" rds \
  describe-db-instances \
  --db-instance-identifier "$DB_INSTANCE_IDENTIFIER" \
  --query 'DBInstances[0].CACertificateIdentifier' \
  --output text | grep -Fx rds-ca-rsa2048-g1

TASK_DEFINITION=$(terraform -chdir=scenario-a output -raw runtime_doctor_task_definition_arn)
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs \
  describe-task-definition \
  --task-definition "$TASK_DEFINITION" \
  --query 'taskDefinition.containerDefinitions[?name!=`cloudwatch-agent` && name!=`elspeth-web`].{name: name, readonlyRootFilesystem: readonlyRootFilesystem}' \
  --output json | jq -e 'length > 0 and all(.[]; .readonlyRootFilesystem == true)'

CANDIDATE_TASK_DEFINITION=$(terraform -chdir=scenario-a output -json resolved_inventory \
  | jq -r '.values.CANDIDATE_TASK_DEFINITION')
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" ecs \
  describe-task-definition \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --query 'taskDefinition.containerDefinitions[?name==`elspeth-web`].{name: name, readonlyRootFilesystem: readonlyRootFilesystem}' \
  --output json | jq -e 'length == 1 and all(.[]; .readonlyRootFilesystem != true)'
```

Both checks project `{name, readonlyRootFilesystem}` objects rather than the
bare field because a JMESPath filter-then-field projection drops null
results: against a container whose field is absent, the bare-field form
yields the same empty array as a query that matched nothing at all, and any
`all(...)` over an empty array is vacuously true. Run the first check only
against `$TASK_DEFINITION` above — the schema-init or runtime doctor
definition, container name `doctor` — never against the payload or
local-auth verifier task definitions
(`resolved_inventory.values.PAYLOAD_VERIFIER_TASK_DEFINITION` /
`LOCAL_AUTH_VERIFIER_TASK_DEFINITION`): those two bind their read-only
container under the `elspeth-web` container name, so against either of them
the `name!='elspeth-web'` projection returns an empty array and the
`length > 0` guard fails closed rather than silently passing. Run the second
check only against a candidate or rollback web task definition —
`$CANDIDATE_TASK_DEFINITION` above, or
`resolved_inventory.values.PREVIOUS_TASK_DEFINITION` on an upgrade: its
`readonlyRootFilesystem` field is absent (not a literal `false`), so the
projection yields `[{"name": "elspeth-web", "readonlyRootFilesystem":
null}]`; `length == 1` proves the query found exactly the intended container
(a bare-field projection would drop the null and yield `[]`, which an
unguarded `all()` would pass with zero evidentiary value — including against
a wrong task-definition ARN or a typo'd container name), and
`.readonlyRootFilesystem != true` proves the documented exemption rather
than a silent `true` that would break ECS Exec. The payload and
local-auth verifier task definitions run a read-only container
(`readonlyRootFilesystem = true`) under the `elspeth-web` container name;
neither jq command above exercises them — that source-level contract is
asserted directly against `modules/scenario/ecs.tf` by
`tests/unit/deployment/test_aws_ecs_terraform_package.py`.

### Upgrading an existing install

Applying this module version to an existing install rotates all five Secrets
Manager database URLs to the canonical immutable-trust query immediately,
while `aws_ecs_service.web` ignores task-definition changes
(`lifecycle.ignore_changes`). An old-image task that restarts inside that
window injects the new canonical URL, lacks the baked bundle, and
crash-loops. Apply and roll the service in one operation: run
`terraform apply`, then immediately select the registered candidate task
definition explicitly and deploy that revision:

```sh
CANDIDATE_TASK_DEFINITION=$(terraform -chdir=scenario-a output -json resolved_inventory \
  | jq -er '.values.CANDIDATE_TASK_DEFINITION')
ECS_CLUSTER=$(terraform -chdir=scenario-a output -raw cluster_name)
ECS_SERVICE=$(terraform -chdir=scenario-a output -raw service_name)
test -n "$CANDIDATE_TASK_DEFINITION"
aws ecs update-service \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --cluster "$ECS_CLUSTER" \
  --service "$ECS_SERVICE" \
  --task-definition "$CANDIDATE_TASK_DEFINITION" \
  --force-new-deployment
```

`--force-new-deployment` by itself restarts the service's currently selected
task definition; it does not select the revision Terraform just registered.
Pinning `ca_cert_identifier` on an existing Aurora instance triggers
a database modification with engine-dependent restart semantics — expect and
schedule it. No pre-trust-root image digest is rollback-eligible after the
upgrade.

## Scenario B

Use the B backend and tfvars examples only when the OIDC acceptance variant is
required. Reuse the same `run_id` and permissions-boundary output from the
single bootstrap; `scenario_id` already isolates Scenario B's names, networks,
state, and resources from Scenario A:

```sh
terraform -chdir=scenario-b init \
  -backend-config=../examples/scenario-b.s3.tfbackend
terraform -chdir=scenario-b workspace show
terraform -chdir=scenario-b workspace select default
terraform -chdir=scenario-b apply \
  -var-file=../examples/scenario-b.tfvars
```

## Outputs and teardown

The roots output the public URL, temporary CA certificate, ECS cluster and
service names, task-role ARN, runtime database secret ARN, resolved inventory,
and a teardown reminder. These are intended to be useful without reading the
Terraform source.

For teardown, select the same backend config and exact workspace used for
installation, verify both explicit AWS profiles, account, and region again,
and run the ordinary full destroy:

```sh
terraform -chdir=scenario-a destroy \
  -var-file=../examples/scenario-a.tfvars
```

Use `scenario-b` and its inputs for the B variant. Once every scenario state is
empty, destroy bootstrap last with the same two profiles:

```sh
terraform -chdir=bootstrap destroy \
  -var-file=../examples/bootstrap.tfvars
```

Terraform's provider split and resource dependencies ensure the normal
installer removes inline policies and the known managed attachment before the
lifecycle principal deletes each role. The lifecycle principal then deletes
the custom boundary only during the final bootstrap destroy. Repeated apply
and full `terraform destroy` commands remain the supported idempotent workflow.
